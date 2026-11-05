"""Block C, periodicity features per HR state from a sliding window.

Input: samples pushed per block (1, n) at env_rate_hz, for the activation and for the band-mean
envelope. Output per frame: 12 features per HR state (n_states, 12), relating the state's period
T (or frequency f) to its multiples so that octave errors become separable. The window grows
from min_window_s to window_s.
"""

from dataclasses import dataclass

import numpy as np

FEATURE_NAMES = ["acf_T", "acf_half_T", "acf_2T", "valid_2T", "nccf_T", "yin_T", "dft_f",
                 "dft_2f", "dft_half_f", "acf_peak", "acf_rel_max", "periods_in_window"]


@dataclass
class FeatureState:
    """History (bounded to window_s), sample rate, state periods and feature settings."""
    history: np.ndarray
    rate_hz: float
    max_len: int
    lags: np.ndarray            # period of each HR state, in samples (float)
    cfg: dict


def hr_grid(cfg: dict) -> np.ndarray:
    """Log-spaced HR states (BPM) from hr_min to hr_max, step_rel apart."""
    g = cfg["grid"]
    step = np.log1p(g["step_rel"])
    n = int(np.floor(np.log(g["hr_max_bpm"] / g["hr_min_bpm"]) / step)) + 1
    return g["hr_min_bpm"] * np.exp(step * np.arange(n))


def init(cfg: dict) -> FeatureState:
    """Empty single-channel history at the envelope rate."""
    rate = float(cfg["frontend"]["env_rate_hz"])
    return FeatureState(history=np.zeros((1, 0)), rate_hz=rate,
                        max_len=int(round(cfg["features"]["window_s"] * rate)),
                        lags=60.0 * rate / hr_grid(cfg), cfg=cfg["features"])


def push(state: FeatureState, samples: np.ndarray) -> None:
    """Append samples (1, n) and keep the latest max_len."""
    state.history = np.hstack([state.history, samples])[:, -state.max_len:]


def acf(x: np.ndarray, nfft: int) -> np.ndarray:
    """Biased autocorrelation normalised to lag 0, lags 0..n-1."""
    n = x.shape[1]
    spec = np.abs(np.fft.rfft(x, nfft)) ** 2.0
    r = np.fft.irfft(spec, nfft)[:, :n]
    return r / np.maximum(r[:, :1], 1e-30)


def nccf_yin(x: np.ndarray, nfft: int) -> tuple[np.ndarray, np.ndarray]:
    """NCCF (correlation normalised by the overlap energies) and YIN score (1 - cumulative-mean
    normalised difference), lags 0..n-1."""
    n = x.shape[1]
    raw = np.fft.irfft(np.abs(np.fft.rfft(x, nfft)) ** 2, nfft)[:, :n]
    energy = np.cumsum(x ** 2, axis=1)
    lags = np.arange(n)
    e_head = np.take_along_axis(energy, np.broadcast_to(n - 1 - lags, x.shape), axis=1)
    e_tail = energy[:, -1:] - np.hstack([np.zeros((x.shape[0], 1)), energy[:, :-1]])
    nccf = raw / np.sqrt(np.maximum(e_head * e_tail, 1e-30))
    diff = (e_head + e_tail - 2 * raw) / (n - lags)
    cum = np.cumsum(diff[:, 1:], axis=1)
    cmnd = np.ones_like(diff)
    cmnd[:, 1:] = diff[:, 1:] * lags[1:] / np.maximum(cum, 1e-30)
    return nccf, 1.0 - cmnd


def at_lags(r: np.ndarray, lags: np.ndarray, max_lag: int) -> np.ndarray:
    """Lag function r (1-D) linearly interpolated at the given lags; NaN beyond max_lag."""
    valid = (lags <= max_lag) & (lags >= 1)
    out = np.full(lags.shape, np.nan)
    if valid.any():
        out[valid] = np.interp(lags[valid], np.arange(r.size), r)
    return out


def dft_at(x: np.ndarray, state: FeatureState, mults: tuple) -> list:
    """Hann-windowed DFT magnitude at mult x each state's frequency, normalised per frame."""
    n = x.shape[1]
    windowed = (x * np.hanning(n)).mean(axis=0)
    out = []
    for mult in mults:
        phase = 2 * np.pi * np.outer(mult / state.lags, np.arange(n))
        out.append(np.hypot(np.cos(phase) @ windowed, np.sin(phase) @ windowed))
    norm = max(out[0].max(), 1e-30)
    return [m / norm for m in out]


def compute(state: FeatureState) -> np.ndarray | None:
    """Features (n_states, 12) for the current history; None before min_window_s."""
    x = state.history
    n = x.shape[1]
    if n < state.cfg["min_window_s"] * state.rate_hz:
        return None
    x = x - x.mean(axis=1, keepdims=True)
    nfft = int(2 ** np.ceil(np.log2(2 * n)))
    r_acf = acf(x, nfft).mean(axis=0)
    r_nccf, r_yin = (r.mean(axis=0) for r in nccf_yin(x, nfft))
    acf_at = {m: at_lags(r_acf, state.lags * m, n - 1) for m in (1.0, 0.5, 2.0)}
    nccf = at_lags(r_nccf, state.lags, n - 1)
    nccf[state.lags > n - state.cfg["min_overlap_s"] * state.rate_hz] = np.nan
    yin = at_lags(r_yin, state.lags, n - 1)
    dft = dft_at(x, state, (1.0, 2.0, 0.5))
    acf_t = np.nan_to_num(acf_at[1.0])
    peak = (acf_t >= np.roll(acf_t, 2)) & (acf_t >= np.roll(acf_t, -2))
    cols = [acf_at[1.0], acf_at[0.5], np.nan_to_num(acf_at[2.0]),
            (~np.isnan(acf_at[2.0])).astype(float), nccf, yin, *dft, peak.astype(float),
            acf_t - acf_t.max(), np.log(n / state.lags)]
    return np.nan_to_num(np.column_stack(cols)).astype(np.float32)
