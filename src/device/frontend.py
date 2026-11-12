"""Block A, front end: band-pass filter bank, log energy and sound-source features.

Input: one block of block_samples PCG samples (200 at 4000 Hz = 50 ms), float32, full scale 1.0.
Output per 10 ms (env_rate_hz 100), (n_inputs, 5) per block, the TCN inputs before the running-mean
removal (net.normalise, which applies to the heart bands only):
  heart bands   log mean energy of the first heart_bands bands (25-50, 50-100, 100-200,
                200-400 Hz)
  high bands    ln(E band / E heart bands) of the bands above (400-800, 800-1600 Hz): sound
                above the heart band (speech, impacts, friction), level-free
  voicing       highest local maximum of the normalised cross-correlation (NCCF) of the source
                signal over lags of voicing_lag_ms (pitch 83-400 Hz); near 1 for voiced speech,
                below 0.25 for heart sounds
  impulsiveness ln(highest energy over impulse_window_ms / mean energy) of the source signal;
                high for knocks and taps (under 20 ms), low for heart sounds (about 100 ms)
Source signal: the sum of the band-pass outputs from voicing_from_hz up (100-1600 Hz). Voicing
uses only samples of the current block: lag pairs look forward from the frame where the block
holds them, backward otherwise, so no sample history is kept.
Bands: Butterworth of order 2 per edge (2 biquads each). State: 2 values per biquad, started in
steady state for the first sample so a DC offset does not ring through. C (c/src/frontend.c): the
same direct form II transposed biquads as scipy sosfilt; coefficients generated from
configs/device.yml.
"""

from dataclasses import dataclass

import numpy as np
from scipy import signal

LOG_FLOOR = 1e-12
NCCF_FLOOR = 1e-9       # lag pairs with less energy (summed over the frame) give no voicing


@dataclass
class FrontendState:
    """Second-order sections, their states and their unit-step steady states per band; samples
    per envelope value; feature settings; whether a block has been seen."""
    sos: list
    zi: list
    zi_step: list
    decimation: int
    heart_bands: int
    source_from: int            # first band summed into the source signal
    lags: np.ndarray            # voicing lags (samples)
    impulse: int                # impulsiveness window (samples); 0: no source features
    started: bool = False


def n_inputs(cfg: dict) -> int:
    """TCN inputs per 10 ms: one per band, plus voicing and impulsiveness when enabled."""
    fe = cfg["frontend"]
    return len(fe["bands_hz"]) + (2 if fe["source_features"] else 0)


def init(cfg: dict) -> FrontendState:
    """Butterworth band-pass per configured band (filter_order per edge); states are set on the
    first sample (see process)."""
    fe, fs = cfg["frontend"], cfg["stream"]["fs_hz"]
    decimation = fs // fe["env_rate_hz"]
    lo, hi = (int(round(ms * fs / 1000)) for ms in fe["voicing_lag_ms"])
    if cfg["stream"]["block_samples"] < decimation + 2 * hi:
        raise ValueError("voicing_lag_ms: lag pairs of every interval must fit in one block")
    sos = [signal.butter(fe["filter_order"], band, btype="band", fs=fs, output="sos")
           .astype(np.float32) for band in fe["bands_hz"]]
    return FrontendState(sos=sos, zi=[np.zeros((s.shape[0], 2), np.float32) for s in sos],
                         zi_step=[signal.sosfilt_zi(s).astype(np.float32) for s in sos],
                         decimation=decimation, heart_bands=fe["heart_bands"],
                         source_from=next(i for i, b in enumerate(fe["bands_hz"])
                                          if b[0] >= fe["voicing_from_hz"]),
                         lags=np.arange(lo, hi + 1),
                         impulse=(int(round(fe["impulse_window_ms"] * fs / 1000))
                                  if fe["source_features"] else 0))


def voicing(x: np.ndarray, frame: int, lags: np.ndarray) -> np.ndarray:
    """Per frame of x (block of frames): highest NCCF at a local maximum over lags (a pitch
    period; the NCCF of a low-frequency sound only falls over these lags), 0 without one. Lag
    pairs inside the block: forward where the block holds frame + lag samples, backward
    otherwise; NCCF 0 without energy."""
    starts = np.arange(x.size // frame)[:, None] * frame                 # (frames, 1)
    shift = np.where(starts + frame + lags[None] <= x.size, lags[None], -lags[None])
    a = x[starts + np.arange(frame)[None]]                               # (frames, frame)
    b = x[(starts + shift)[:, :, None] + np.arange(frame)[None, None]]   # (frames, lags, frame)
    ea = np.einsum("fi,fi->f", a, a)[:, None]
    eb = np.einsum("fli,fli->fl", b, b)
    num = np.einsum("fi,fli->fl", a, b)
    energy = ea * eb
    ncc = np.where(energy > np.float32(NCCF_FLOOR * NCCF_FLOOR),
                   num / np.sqrt(np.maximum(energy, np.float32(1e-30))), np.float32(0.0))
    mid = ncc[:, 1:-1]
    peak = (mid > ncc[:, :-2]) & (mid >= ncc[:, 2:])
    return np.maximum(np.where(peak, mid, np.float32(0.0)).max(axis=1),
                      np.float32(0.0)).astype(np.float32)


def impulsiveness(x: np.ndarray, frame: int, window: int) -> np.ndarray:
    """Per frame of x: ln((highest window energy + floor) / (mean energy + floor))."""
    e = (x * x).reshape(-1, frame // window, window).mean(axis=2)
    return (np.log(e.max(axis=1) + np.float32(LOG_FLOOR))
            - np.log(e.mean(axis=1) + np.float32(LOG_FLOOR))).astype(np.float32)


def process(state: FrontendState, block: np.ndarray) -> np.ndarray:
    """Filter one block; the TCN inputs (module docstring) per decimation interval. On the first
    block the filter states start in steady state for the first sample's level, so a DC offset
    at start-up does not ring through the band-passes."""
    block = np.asarray(block, np.float32)
    if not state.started:
        state.zi = [z * block[0] for z in state.zi_step]
        state.started = True
    energy, source = [], np.zeros(block.size, np.float32)
    for band, sos in enumerate(state.sos):
        filtered, state.zi[band] = signal.sosfilt(sos, block, zi=state.zi[band])
        filtered = filtered.astype(np.float32)
        energy.append((filtered ** 2).reshape(-1, state.decimation).mean(axis=1))
        if band >= state.source_from:
            source += filtered
    energy = np.array(energy, np.float32)
    floor = np.float32(LOG_FLOOR)
    h = state.heart_bands
    rows = [np.log(energy[:h] + floor)]
    if energy.shape[0] > h:
        rows.append(np.log(energy[h:] + floor) - np.log(energy[:h].sum(axis=0) + floor))
    if state.impulse:
        rows += [voicing(source, state.decimation, state.lags)[None],
                 impulsiveness(source, state.decimation, state.impulse)[None]]
    return np.vstack(rows).astype(np.float32)
