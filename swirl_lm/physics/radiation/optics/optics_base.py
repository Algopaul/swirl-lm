# Copyright 2023 The swirl_lm Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Abstract base class defining the interface of an optics scheme."""

import abc
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
from swirl_lm.communication import halo_exchange
from swirl_lm.numerics import interpolation
from swirl_lm.physics.radiation.config import radiative_transfer_pb2
from swirl_lm.utility import common_ops
from swirl_lm.utility import get_kernel_fn
from swirl_lm.utility import types

import tensorflow as tf

FlowFieldVal = types.FlowFieldVal
FlowFieldMap = types.FlowFieldMap


class OpticsScheme(metaclass=abc.ABCMeta):
  """Abstract base class for optics scheme."""

  def __init__(
      self,
      params: radiative_transfer_pb2.OpticsParameters,
      kernel_op: get_kernel_fn.ApplyKernelOp,
      g_dim: int,
      halos: int,
  ):
    self._g_dim = g_dim
    self._halos = halos
    self._face_interp_scheme_order = params.face_interp_scheme_order
    self._kernel_op = kernel_op
    self._kernel_op.add_kernel({'shift_dn': ([0.0, 0.0, 1.0], 1)})
    self._shift_down_fn = (
        lambda f: kernel_op.apply_kernel_op_x(f, 'shift_dnx'),
        lambda f: kernel_op.apply_kernel_op_y(f, 'shift_dny'),
        lambda f: kernel_op.apply_kernel_op_z(f, 'shift_dnz', 'shift_dnzsh'),
    )[g_dim]

  @abc.abstractmethod
  def compute_lw_optical_properties(
      self,
      pressure: FlowFieldVal,
      temperature: FlowFieldVal,
      mols: FlowFieldVal,
      igpt: int,
      vmr_fields: Optional[Dict[int, FlowFieldVal]] = None,
  ) -> FlowFieldMap:
    """Computes the monochromatic longwave optical properties.

    Args:
      pressure: The pressure field [Pa].
      temperature: The temperature [K].
      mols: The number of molecules in an atmospheric grid cell per area
        [molecules / m^2].
      igpt: The spectral interval index, or g-point.
      vmr_fields: An optional dictionary containing precomputed volume mixing
        ratio fields, keyed by gas index, that will overwrite the global means.

    Returns:
      A dictionary containing (for a single g-point):
        'optical_depth': The longwave optical depth.
        'ssa': The longwave single-scattering albedo.
        'asymmetry_factor': The longwave asymmetry factor.
    """

  @abc.abstractmethod
  def compute_sw_optical_properties(
      self,
      pressure: FlowFieldVal,
      temperature: FlowFieldVal,
      mols: FlowFieldVal,
      igpt: int,
      vmr_fields: Optional[Dict[int, FlowFieldVal]] = None,
  ) -> FlowFieldMap:
    """Computes the monochromatic shortwave optical properties.

    Args:
      pressure: The pressure field [Pa].
      temperature: The temperature [K].
      mols: The number of molecules in an atmospheric grid cell per area
        [molecules / m^2].
      igpt: The spectral interval index, or g-point.
      vmr_fields: An optional dictionary containing precomputed volume mixing
        ratio fields, keyed by gas index, that will overwrite the global means.

    Returns:
      A dictionary containing (for a single g-point):
        'optical_depth': The shortwave optical depth.
        'ssa': The shortwave single-scattering albedo.
        'asymmetry_factor': The shortwave asymmetry factor.
    """

  @abc.abstractmethod
  def compute_planck_sources(
      self,
      replica_id: tf.Tensor,
      replicas: np.ndarray,
      pressure: FlowFieldVal,
      temperature: FlowFieldVal,
      igpt: int,
      vmr_fields: Optional[Dict[int, FlowFieldVal]] = None,
  ) -> FlowFieldMap:
    """Computes the Planck sources used in the longwave problem.

    Args:
      replica_id: The index of the current TPU replica.
      replicas: The mapping from the core coordinate to the local replica id
        `replica_id`.
      pressure: The pressure field [Pa].
      temperature: The temperature [K].
      igpt: The spectral interval index, or g-point.
      vmr_fields: An optional dictionary containing precomputed volume mixing
        ratio fields, keyed by gas index, that will overwrite the global means.

    Returns:
      A dictionary containing the Planck source at the cell center
      (`planck_src`), the top cell boundary (`planck_src_top`), and the bottom
      cell boundary (`planck_src_bottom`).
    """

  @property
  @abc.abstractmethod
  def n_gpt_lw(self) -> int:
    """The number of g-points in the longwave bands."""

  @property
  @abc.abstractmethod
  def n_gpt_sw(self) -> int:
    """The number of g-points in the shortwave bands."""

  def _slice_field(
      self,
      f: FlowFieldVal,
      dim: int,
      face: int,
      idx: int,
  ) -> FlowFieldVal:
    """Slices a plane from `f` normal to `dim`."""
    face_slice = common_ops.get_face(f, dim, face, idx)
    if isinstance(f, tf.Tensor) or dim != 2:
      # Remove the outer list.
      return face_slice[0]
    return face_slice

  def _field_shape(
      self,
      f: FlowFieldVal,
  ) -> FlowFieldVal:
    """Returns the x, y, and z dimensions of the field `f`."""
    if isinstance(f, tf.Tensor):
      input_shape = f.get_shape().as_list()
      return input_shape[1:] + input_shape[:1]
    else:
      return f[0].get_shape().as_list() + [len(f)]

  def _exchange_halos(
      self,
      replica_id: tf.Tensor,
      replicas: np.ndarray,
      f: FlowFieldVal,
  ) -> FlowFieldVal:
    """Exchanges halos, preserving the boundary values along the vertical."""
    boundary_vals = [
        [self._slice_field(f, self._g_dim, face, i) for i in range(self._halos)]
        for face in range(2)
    ]
    # Reverse the order of the top boundary values to restore ascending order.
    boundary_vals[1].reverse()

    if self._g_dim == 2 and isinstance(f, Sequence):
      # Unnest the 2D plane.
      boundary_vals = [[v_i[0] for v_i in v] for v in boundary_vals]

    bc = [[(halo_exchange.BCType.NEUMANN, 0.0)] * 2 for _ in range(3)]
    bc[self._g_dim] = [
        (halo_exchange.BCType.DIRICHLET, bv) for bv in boundary_vals
    ]
    return halo_exchange.inplace_halo_exchange(
        f,
        (0, 1, 2),
        replica_id,
        replicas,
        (0, 1, 2),
        (False, False, False),
        boundary_conditions=bc,
        width=self._halos,
    )

  def _reconstruct_face_values(
      self,
      replica_id: tf.Tensor,
      replicas: np.ndarray,
      f: FlowFieldVal,
  ) -> Tuple[FlowFieldVal, FlowFieldVal]:
    """Reconstructs the face values using a high-order scheme.

    Args:
      replica_id: The index of the current TPU replica.
      replicas: The mapping from the core coordinate to the local replica id
        `replica_id`.
      f: The cell-center values that will be interpolated.

    Returns:
      A tuple with the reconstructed temperature at the bottom and top face,
      respectively.
    """
    dim = ('x', 'y', 'z')[self._g_dim]
    f_neg, f_pos = interpolation.weno(
        f, dim=dim, k=self._face_interp_scheme_order
    )
    f_bottom = self._exchange_halos(
        replica_id,
        replicas,
        tf.nest.map_structure(
            lambda x, y: 0.5 * (x + y),
            f_neg,
            f_pos,
        ),
    )

    # Shift down to obtain the top cell face values and pad the top outermost
    # halo layer with a copy of the adjacent inner layer.
    f_top = self._shift_down_fn(f_bottom)
    outermost_valid_top_layer = self._slice_field(
        f_top, self._g_dim, face=1, idx=1
    )
    shape = self._field_shape(f_top)
    # Update the last halo layer along the vertical.
    f_top = common_ops.tensor_scatter_1d_update(
        f_top,
        self._g_dim,
        shape[self._g_dim] - 1,
        outermost_valid_top_layer,
    )
    return f_bottom, f_top
