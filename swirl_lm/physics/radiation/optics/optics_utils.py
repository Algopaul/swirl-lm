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

# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utility library for common operations on the RRTMGP tables."""

import collections
import dataclasses
from typing import Callable, Optional, Sequence

from swirl_lm.physics.radiation.optics import lookup_gas_optics_base
from swirl_lm.physics.radiation.optics import lookup_volume_mixing_ratio
import tensorflow as tf


LookupGasOpticsBase = lookup_gas_optics_base.AbstractLookupGasOptics
LookupVolumeMixingRatio = lookup_volume_mixing_ratio.LookupVolumeMixingRatio
OrderedDict = collections.OrderedDict


@dataclasses.dataclass
class IndexAndWeight:
  """Wrapper for a pair of index tensor and associated interpolation weight."""
  # Index tensor.
  idx: tf.Tensor
  # Interpolant weight tensor associated with index.
  weight: tf.Tensor


@dataclasses.dataclass
class Interpolant:
  """Wrapper for a single dimension interpolant."""
  # Index and interpolation weight of floor reference value.
  interp_low: IndexAndWeight
  # Index and interpolation weight of upper endpoint of matching reference
  # interval.
  interp_high: IndexAndWeight


def validate_range(
    f: tf.Tensor,
    reference_values: tf.Tensor,
) -> None:
  """Verifies that no value in `f` is outside the range of reference values."""
  min_t_ref = tf.math.reduce_min(reference_values)
  max_t_ref = tf.math.reduce_max(reference_values)
  min_t = tf.math.reduce_min(f)
  max_t = tf.math.reduce_max(f)
  tf.compat.v1.assert_greater_equal(
      min_t,
      min_t_ref,
      message='At least one value is below the reference range.',
  )
  tf.compat.v1.assert_less_equal(
      max_t,
      max_t_ref,
      message='At least one value is above the reference range.',
  )


def lookup_values(
    vals: tf.Tensor,
    idx_list: Sequence[tf.Tensor],
) -> tf.Tensor:
  """Gathers values from `vals` as specified by a list of index tensors.

  Args:
    vals: A tensor of coefficients to be gathered.
    idx_list: A list of length equal to the rank of `vals` containing tensors of
      indices, one for each axis of `vals` and in the same order.

  Returns:
    A tensor having the same shape as an element of `idx_list` where the indices
    have been replaced by the corresponding value from `vals`.
  """
  idx_list = [tf.expand_dims(idx, axis=-1) for idx in idx_list]
  idx = tf.concat(idx_list, axis=-1)
  return tf.gather_nd(vals, idx)


def evaluate_weighted_lookup(
    coeffs: tf.Tensor,
    weight_idx_list: Sequence[IndexAndWeight],
) -> tf.Tensor:
  """Performs a lookup of coefficients and scales them with pointwise weights.

  Args:
    coeffs: The tensor of coefficients that will be gathered.
    weight_idx_list: A list of `IndexAndWeight`s containing a pair of index
      tensor and weight tensor for each axis of the `coeffs` tensor.

  Returns:
    A tensor of the same shape as an element of `weight_idx_list` containing
    the gathered coefficients scaled by the pointwise product of corresponding
    weights.
  """
  vals = lookup_values(coeffs, [idx.idx for idx in weight_idx_list])
  vals *= tf.math.reduce_prod(
      tf.stack([idx.weight for idx in weight_idx_list]), axis=0
  )
  return vals


def floor_idx(
    f: tf.Tensor,
    reference_values: tf.Tensor,
) -> tf.Tensor:
  """Returns the indices of the floor reference values.

  Note that the `reference_values` should consist of evenly spaced points. Each
  index returned corresponds to the highest index k such that
  reference_values[k] <= f.

  Args:
    f: The tensor whose values will be mapped to a floor reference value.
    reference_values: A 1-D tensor of reference values.

  Returns:
    A `tf.Tensor` of the same shape as `f` containing the indices of the floor
    reference values for the values of `f`. Each index corresponds to the
    highest index k such that reference_values[k] <= f.
  """
  delta = reference_values[1] - reference_values[0]
  size = reference_values.shape[0]
  truncated_div = tf.cast(
      tf.math.floordiv(f - reference_values[0], delta), tf.int32)
  return tf.clip_by_value(truncated_div, 0, size - 1)


def create_linear_interpolant(
    f: tf.Tensor,
    f_ref: tf.Tensor,
    offset: Optional[tf.Tensor] = None,
) -> Interpolant:
  """Creates a linear interpolant based on the evenly spaced reference values.

  The linear interpolant is created by matching the values of `f` to an interval
  of the reference values and storing information about the location of the
  endpoints. Linear nterpolation weights are computed based on the distance of
  the value from each endpoint.

  Args:
    f: A tensor of arbitrary shape whose values must be in the range of
      reference values in `f_ref`.
    f_ref: The 1-D tensor of evenly spaced reference values for the variable.
    offset: An optional tensor of the same shape as `f` that should be added
      to the interpolant indices.

  Returns:
    An `Interpolant` object containing the pointwise floor and ceiling indices
    and interpolation weights of `f`.
  """
  with tf.control_dependencies(
      [validate_range(f, f_ref)]):
    size = f_ref.shape[0]
    delta = f_ref[1] - f_ref[0]
    idx_low = floor_idx(f, f_ref)
    idx_high = tf.math.minimum(idx_low + 1, size - 1)
    # Compute the interpolant weights for the two endpoints.
    lower_reference_vals = lookup_values(f_ref, (idx_low,))
    weight2 = tf.math.abs((f - lower_reference_vals) / delta)
    weight1 = 1.0 - weight2
    if offset is not None:
      idx_low += offset
      idx_high += offset
    idx_weight_low = IndexAndWeight(idx_low, weight1)
    idx_weight_high = IndexAndWeight(idx_high, weight2)
    return Interpolant(idx_weight_low, idx_weight_high)


def interpolate(
    coeffs: tf.Tensor,
    interpolant_fns: OrderedDict[str, Callable[..., Interpolant]],
    idx_weight_by_varname: Optional[OrderedDict[str, IndexAndWeight]] = None):
  """Recursively interpolates coefficients according to the `interpolant_fns`.

  The `interpolant_fns` are provided as functions taking in a dictionary of
  `IndexAndWeight` objects that the interpolant might depend on and returning
  an `Interpolant` object. This is particularly useful when the variables used
  to index into `coeffs` have dependencies between them. Take as an example the
  RRTMGP `kmajor` coefficient table, which is indexed by temperature, pressure,
  and the relative abundance fraction. Temperature and pressure are independent
  variables, but the relative abundance calculation depends on both of them.
  Providing the interpolant as a function allows a straightforward expression of
  this dependency.

  For example, if `coeffs` has rank 2, where the first axis is indexed by a
  variable `x`, the second axis is indexed by a variable `y`, and `y` depends on
  `x`, the interpolation can be done as follows:

  independent_x_interpolant = create_linear_interpolant(x, x_ref)

  def dependent_y_interpolant_func(dep: Dict[Text, IndexAndWeight]):
    x_idx = dep['x'].idx
    y = compute_y(x_idx, ...)
    return create_linear_interpolant(y, y_ref)

  interpolant_fns = {
      'x': lambda _: independent_x_interp,
      'y': dependent_y_interpolant_func,
  }

  interpolated_vals = interpolate(coeffs, interpolant_fns)

  This function is implemented recursively, but the depth of the recursive tree
  never exceeds the rank of the `coeffs` tensor, which is at most 3 in the
  RRTMGP interpolations. The `idx_weight_list` contains indices and weights for
  a particular branch of interpolation. The sum of lengths of `interpolant_fns`
  and `idx_weight_list` will always equal the rank of `coeffs`. At each call of
  this function, a branch of the next interpolant is selected until there are no
  more branches left. At that point a weighted lookup is evaluated rendering the
  interpolant value for that path. Recursively adding the weighted lookups of
  the upper index branch and the lower index branch for each index variable
  gives the interpolated values of the coefficients.

  Args:
    coeffs: The tensor of coefficients of arbitrary shape whose values will be
      interpolated.
    interpolant_fns: An ordered dictionary of interpolant functions keyed by the
      name of the variable they correspond to. There should be one for each axis
      of `coeffs` and their order should match the order of the axes. Note that
      they should be sorted in topological order (dependent indices appearing
      after the indices they depend on). The axes of `coeffs` are assumed to
      already conform to this ordering.
    idx_weight_by_varname: An ordered dictionary of `IndexAndWeight` objects
      that will be used in the interpolation, indexed by variable name.

  Returns:
    A `tf.Tensor` of the same shape as any of the index tensors, but with the
    indices replaced by the interpolated coefficients.
  """
  if idx_weight_by_varname is None:
    idx_weight_by_varname = {}
  if not interpolant_fns:
    if not idx_weight_by_varname:
      raise ValueError(
          'The number of interpolant functions must equal the rank of `coeffs`.'
      )
    # Recursion base case.
    return evaluate_weighted_lookup(
        coeffs, list(idx_weight_by_varname.values())
    )
  else:
    interpolant_fns = interpolant_fns.copy()
    varname, interpolant_fn = interpolant_fns.popitem(last=False)
    interpolant = interpolant_fn(idx_weight_by_varname)
    # Branch into the lower index.
    lower_idx_weight_dict = idx_weight_by_varname.copy()
    lower_idx_weight_dict.update({varname: interpolant.interp_low})
    lower = interpolate(
        coeffs, interpolant_fns.copy(), lower_idx_weight_dict
    )
    # Branch into the higher index.
    higher_idx_weight_dict = idx_weight_by_varname.copy()
    higher_idx_weight_dict.update({varname: interpolant.interp_high})
    higher = interpolate(
        coeffs, interpolant_fns.copy(), higher_idx_weight_dict
    )
    return lower + higher
