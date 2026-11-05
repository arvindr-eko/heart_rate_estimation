"""Block A, front end: band-pass filter bank and log energy.

Input: PCG block (block_samples,) at fs_hz. Output: log mean energy per band per envelope
sample, (n_bands, block_samples / decimation) at env_rate_hz. State: biquad states per band.
"""

from dataclasses import dataclass

import numpy as np
from scipy import signal

LOG_FLOOR = 1e-12


@dataclass
class FrontendState:
    """Second-order sections and their states per band; samples per envelope value."""
    sos: list
    zi: list
    decimation: int


def init(cfg: dict) -> FrontendState:
    """Butterworth band-pass per configured band (filter_order per edge), zero states."""
    fe, fs = cfg["frontend"], cfg["stream"]["fs_hz"]
    sos = [signal.butter(fe["filter_order"], band, btype="band", fs=fs, output="sos")
           for band in fe["bands_hz"]]
    return FrontendState(sos=sos, zi=[np.zeros((s.shape[0], 2)) for s in sos],
                         decimation=fs // fe["env_rate_hz"])


def process(state: FrontendState, block: np.ndarray) -> np.ndarray:
    """Filter one block; log of the mean energy over each decimation interval, per band."""
    env = []
    for band, sos in enumerate(state.sos):
        filtered, state.zi[band] = signal.sosfilt(sos, block, zi=state.zi[band])
        energy = (filtered ** 2).reshape(-1, state.decimation).mean(axis=1)
        env.append(np.log(energy + LOG_FLOOR))
    return np.vstack(env)
