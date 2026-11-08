"""Block A, front end: band-pass filter bank and log energy.

Input: one block of block_samples PCG samples (200 at 4000 Hz = 50 ms), float32, full scale 1.0.
Output: log mean energy per band per 10 ms, (4 bands, 5 values) per block (env_rate_hz 100).
Bands 25-50, 50-100, 100-200, 200-400 Hz, Butterworth of order 2 per edge (2 biquads each).
State: 2 values per biquad, started in steady state for the first sample so a DC offset does not
ring through. C: CMSIS-DSP arm_biquad_cascade_df2T_f32 (the direct form II transposed structure
of scipy sosfilt); coefficients generated from configs/device.yml.
"""

from dataclasses import dataclass

import numpy as np
from scipy import signal

LOG_FLOOR = 1e-12


@dataclass
class FrontendState:
    """Second-order sections, their states and their unit-step steady states per band; samples
    per envelope value; whether a block has been seen."""
    sos: list
    zi: list
    zi_step: list
    decimation: int
    started: bool = False


def init(cfg: dict) -> FrontendState:
    """Butterworth band-pass per configured band (filter_order per edge); states are set on the
    first sample (see process)."""
    fe, fs = cfg["frontend"], cfg["stream"]["fs_hz"]
    sos = [signal.butter(fe["filter_order"], band, btype="band", fs=fs, output="sos")
           .astype(np.float32) for band in fe["bands_hz"]]
    return FrontendState(sos=sos, zi=[np.zeros((s.shape[0], 2), np.float32) for s in sos],
                         zi_step=[signal.sosfilt_zi(s).astype(np.float32) for s in sos],
                         decimation=fs // fe["env_rate_hz"])


def process(state: FrontendState, block: np.ndarray) -> np.ndarray:
    """Filter one block; log of the mean energy over each decimation interval, per band. On the
    first block the filter states start in steady state for the first sample's level, so a DC
    offset at start-up does not ring through the band-passes."""
    env = []
    block = np.asarray(block, np.float32)
    if not state.started:
        state.zi = [z * block[0] for z in state.zi_step]
        state.started = True
    for band, sos in enumerate(state.sos):
        filtered, state.zi[band] = signal.sosfilt(sos, block, zi=state.zi[band])
        energy = (filtered ** 2).reshape(-1, state.decimation).mean(axis=1)
        env.append(np.log(energy + np.float32(LOG_FLOOR)))
    return np.vstack(env)
