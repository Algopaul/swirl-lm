import numpy as np
from absl import flags, logging, app


_RANDOM_SEED_UQ = flags.DEFINE_integer(
  'random_seed_uq',
  0,
  'The random seed used for sampling the uncertain parameters.'
)
_N_SAMPLES_UQ = flags.DEFINE_integer(
  'n_samples_uq',
  100,
  'The number of samples collected for uncertainty quantification.'
)
_FUEL_LOAD_LB = flags.DEFINE_float(
  'fuel_load_lb',
  0.09,
  'Lower bound of fuel load for uncertainty quantification.'
)
_FUEL_LOAD_UB = flags.DEFINE_float(
  'fuel_load_ub',
  2.25,
  'Upper bound of fuel load for uncertainty quantification.'
)
_LARGE_SCALE_FUEL_BED_HEIGHT = flags.DEFINE_float(
  'large_scale_fuel_bed_height',
  9.785,
  'fuel_bed_height of the large scale simulation'
)
_MOISTURE_CONTENT_LB = flags.DEFINE_float(
  'moisture_content_lb',
  0.03,
  'Lower bound of moisture content for uncertainty quantification.'
)
_MOISTURE_CONTENT_UB = flags.DEFINE_float(
  'moisture_content_ub',
  0.12,
  'Upper bound of moisture content for uncertainty quantification.'
)
_WIND_SPEED_LB = flags.DEFINE_float(
  'wind_speed_lb',
  5.0,
  'Lower bound of wind speed for uncertainty quantification.'
)
_WIND_SPEED_UB = flags.DEFINE_float(
  'wind_speed_ub',
  12.0,
  'Upper bound of wind speed for uncertainty quantification.'
)
_MODIFY_INDIVIDUAL = flags.DEFINE_bool(
  'modify_indiviual',
  False,
  'Whether to modify the different values used in uncertainty quantification'
  'one at a time. This produces only 4 trajectories and should only be used'
  'for validation.'
)
_READ_UQ_FILE = flags.DEFINE_bool(
  'read_uq_file',
  False,
  'Whether to read the UQ-parameters from a file',
)
_UQ_FILENAME = flags.DEFINE_string(
  'uq_filename',
  './large_scale_uq_params.npy',
  'The location of the .npy file that contains the uq values'
)
_UQ_START_ID = flags.DEFINE_integer(
  'uq_start_id',
  0,
  'The start id for uq.'
)


class FireUQSampler:
  def __init__(self):
    self.sampler = np.random.default_rng(_RANDOM_SEED_UQ.value)
    self.start_id = _UQ_START_ID.value
    if _MODIFY_INDIVIDUAL.value:
      logging.warn(
        'Modifying uncertain parameters one at a time. Only generating 4'
        'trajectories (1 trajectory for baseline and 1 for each varied'
        'parameter). Ignores n_samples_uq flag.'
      )
    pass

  def _uniform(self, lower_bound, upper_bound, n_samples):
    """
    Returns random numbers sampled uniformly in [lower_bound, uppber_bound].
    We do not use `self.sampler.uniform` for compatibility with previous
    version of random number generation.
    """
    norm_samples = self.sampler.random(size=(n_samples,), dtype=np.float32)
    return (upper_bound - lower_bound) * norm_samples + lower_bound

  def number_of_samples(self):
    if _MODIFY_INDIVIDUAL.value:
      return 4
    elif _READ_UQ_FILE.value:
      return 20
    else:
      return _N_SAMPLES_UQ.value

  def generate_samples(self):
    """
    Generates samples for random variables considered in uncertainty
    quantification.
    Returns:
      fuel_density_samples, moisture_density_samples, wind_speed_samples
    """
    if _READ_UQ_FILE.value:
      uq_values = np.load(_UQ_FILENAME.value)
      fuel_density_samples = np.float32(uq_values[:, 0])
      moisture_density_samples = np.float32(uq_values[:, 1])
      wind_speed_samples = np.float32(uq_values[:, 2])
    else:
      if _MODIFY_INDIVIDUAL.value:
        fuel_load_samples = np.ones(4) * _FUEL_LOAD_LB.value
        fuel_load_samples[1] = _FUEL_LOAD_UB.value
        fuel_density_samples = fuel_load_samples / _LARGE_SCALE_FUEL_BED_HEIGHT.value
        moisture_content_samples = np.ones(4) * _MOISTURE_CONTENT_LB.value
        moisture_content_samples[2] = _MOISTURE_CONTENT_UB.value
        moisture_density_samples = moisture_content_samples * fuel_density_samples
        wind_speed_samples = np.ones(4) * _WIND_SPEED_LB.value
        wind_speed_samples[3] = _WIND_SPEED_UB.value
      else:
        fuel_load_samples = self._uniform(
          _FUEL_LOAD_LB.value, _FUEL_LOAD_UB.value, _N_SAMPLES_UQ.value
        )
        fuel_density_samples = fuel_load_samples / _LARGE_SCALE_FUEL_BED_HEIGHT.value
        moisture_content_samples = self._uniform(
          _MOISTURE_CONTENT_LB.value,
          _MOISTURE_CONTENT_UB.value,
          _N_SAMPLES_UQ.value
        )
        moisture_density_samples = moisture_content_samples * fuel_density_samples
        wind_speed_samples = self._uniform(
          _WIND_SPEED_LB.value, _WIND_SPEED_UB.value, _N_SAMPLES_UQ.value
        )
    return fuel_density_samples, moisture_density_samples, wind_speed_samples

  def generate_data_dump_prefixes(self, data_dump_prefix):
    data_dump_prefix_base = data_dump_prefix[:-1] + "_"
    if _MODIFY_INDIVIDUAL.value:
      data_dump_prefix_base = data_dump_prefix_base + "testrun_"
    data_dump_prefixes = []
    for i in range(self.number_of_samples()):
      data_dump_prefixes.append(data_dump_prefix_base + f"{i}/")
    return data_dump_prefixes


def main(_):
  uq_sampler = FireUQSampler()
  fd_samples, md_samples, ws_samples = uq_sampler.generate_samples()
  print("Fueld")
  print(np.mean(fd_samples))
  print("Moistd")
  print(np.mean(md_samples))
  print("Wind speed")
  print(np.mean(ws_samples))


if __name__ == "__main__":
  app.run(main)
