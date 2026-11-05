"""Streaming front end: band-pass, optional notch, energy envelope, decimation.

Input: blocks of PCG at fs_hz. Output per block: envelope (n_bands, block / env_decim) at
env_rate_hz and band-limited waveform (block / wave_decim,) at wave_rate_hz.
"""

from dataclasses import dataclass

import numpy as np
from scipy import signal

LOG_FLOOR = 1e-12


@dataclass
class FrontendState:
    """Filter states and previous compressed envelope value per band."""
    sos: list
    zi: list
    env_decim: int
    wave_decim: int
    compress: str
    compress_power: float
    onset: bool
    prev_env: np.ndarray


def design_sos(cfg: dict, band_hz: list) -> np.ndarray:
    """Second-order sections of one band-pass (plus the notch when configured)."""
    fe = cfg["frontend"]
    fs = cfg["stream"]["fs_hz"]
    sos = signal.butter(fe["filter_order"], band_hz, btype="band", fs=fs, output="sos")
    if fe["notch_hz"]:
        b, a = signal.iirnotch(fe["notch_hz"], Q=30.0, fs=fs)
        sos = np.vstack([sos, signal.tf2sos(b, a)])
    return sos


def init(cfg: dict, bands_hz: list | None = None) -> FrontendState:
    """Create the front-end state for the configured (or given) bands."""
    fe = cfg["frontend"]
    bands = bands_hz or fe["bands_hz"]
    sos = [design_sos(cfg, band) for band in bands]
    fs = cfg["stream"]["fs_hz"]
    return FrontendState(
        sos=sos, zi=[np.zeros((s.shape[0], 2)) for s in sos],
        env_decim=fs // fe["env_rate_hz"], wave_decim=fs // fe["wave_rate_hz"],
        compress=fe["compress"], compress_power=fe["compress_power"], onset=fe["onset"],
        prev_env=np.full(len(bands), np.nan))


def compress(energy: np.ndarray, state: FrontendState) -> np.ndarray:
    """Amplitude compression of the mean energy per envelope sample."""
    if state.compress == "log":
        return np.log(energy + LOG_FLOOR)
    return energy ** state.compress_power


def process(state: FrontendState, block: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Filter one block; return (envelope per band, band-limited waveform of band 0)."""
    envs = []
    wave = None
    for band, sos in enumerate(state.sos):
        filtered, state.zi[band] = signal.sosfilt(sos, block, zi=state.zi[band])
        if band == 0:
            wave = filtered.reshape(-1, state.wave_decim).mean(axis=1)
        energy = (filtered ** 2).reshape(-1, state.env_decim).mean(axis=1)
        envs.append(compress(energy, state))
    env = np.vstack(envs)
    if state.onset:
        previous = np.where(np.isnan(state.prev_env), env[:, 0], state.prev_env)
        diff = np.diff(np.hstack([previous[:, None], env]), axis=1)
        state.prev_env = env[:, -1].copy()
        env = np.maximum(diff, 0.0)
    return env, wave


def resources(cfg: dict, n_bands: int | None = None) -> dict:
    """Estimated MAC/s and state RAM (bytes, 32-bit states) of the front end."""
    fe = cfg["frontend"]
    bands = n_bands or len(fe["bands_hz"])
    sections = fe["filter_order"] + (1 if fe["notch_hz"] else 0)
    fs = cfg["stream"]["fs_hz"]
    macs_per_sample = bands * (5 * sections + 2)
    return {"macs_per_s": macs_per_sample * fs + bands * fe["env_rate_hz"] * 20,
            "ram_bytes": bands * (sections * 2 * 4 + 8), "const_bytes": bands * sections * 5 * 4}
