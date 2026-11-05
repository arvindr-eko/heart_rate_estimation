"""Periodicity scores over the HR state grid from a sliding history window.

Input: samples pushed per block (channels, n) at a fixed rate. Output per frame: one score per
HR state (log-spaced grid), NaN where the window is too short to support that period.
Methods: acf, gacf (spectral exponent k; k = 0 is full PHAT), nccf, yin, gcc_segment
(generalised cross-correlation of the latest segment against the preceding history), dft
(windowed envelope spectrum at each state's frequency) and product (a temporal method times
dft). Temporal methods err toward sub-harmonics (2T), the spectrum toward harmonics (2f); the
product suppresses both.
"""

from dataclasses import dataclass

import numpy as np


@dataclass
class PeriodicityState:
    """History ring (kept as a bounded array here) and fixed analysis parameters."""
    history: np.ndarray
    rate_hz: float
    max_len: int
    lags: np.ndarray            # period of each HR state, in samples (float)
    cfg: dict


def hr_grid(cfg: dict) -> np.ndarray:
    """Log-spaced HR states (BPM) from hr_min to hr_max, grid_step_rel apart."""
    trk = cfg["tracker"]
    step = np.log1p(trk["grid_step_rel"])
    n = int(np.floor(np.log(trk["hr_max_bpm"] / trk["hr_min_bpm"]) / step)) + 1
    return trk["hr_min_bpm"] * np.exp(step * np.arange(n))


def input_rate(cfg: dict) -> float:
    """Sample rate of the configured periodicity input."""
    key = "wave_rate_hz" if cfg["periodicity"]["input"] == "waveform" else "env_rate_hz"
    return float(cfg["frontend"][key])


def init(cfg: dict, n_channels: int) -> PeriodicityState:
    """Empty history for n_channels at the configured input rate."""
    rate = input_rate(cfg)
    max_len = int(round(cfg["periodicity"]["window_s"] * rate))
    lags = 60.0 * rate / hr_grid(cfg)
    return PeriodicityState(history=np.zeros((n_channels, 0)), rate_hz=rate, max_len=max_len,
                            lags=lags, cfg=cfg["periodicity"])


def push(state: PeriodicityState, samples: np.ndarray) -> None:
    """Append samples (channels, n) and keep the latest max_len."""
    state.history = np.hstack([state.history, samples])[:, -state.max_len:]


def raw_correlation(x: np.ndarray, nfft: int) -> np.ndarray:
    """Unnormalised linear autocorrelation per channel, lags 0..nfft/2."""
    spec = np.fft.rfft(x, nfft)
    return np.fft.irfft(np.abs(spec) ** 2, nfft)


def band_mask(nfft: int, rate: float, band_hz: list) -> np.ndarray:
    """1 inside band_hz, 0 outside, over rfft bins; limits whitening to HR harmonics."""
    freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
    return ((freqs >= band_hz[0]) & (freqs <= band_hz[1])).astype(float)


def lag_function(x: np.ndarray, p: dict, rate: float) -> tuple[np.ndarray, int]:
    """Per-channel lag function r[k] and the largest valid lag for the method."""
    n = x.shape[1]
    nfft = int(2 ** np.ceil(np.log2(2 * n)))
    method = p["method"]
    max_lag = n - 1 - int(round(p["min_overlap_s"] * rate))
    if method in ("acf", "gacf"):
        k = 2.0 if method == "acf" else p["gacf_k"]
        spec = np.abs(np.fft.rfft(x, nfft)) ** k
        if method == "gacf" and p["phat_band_hz"]:
            spec = spec * band_mask(nfft, rate, p["phat_band_hz"])
        r = np.fft.irfft(spec, nfft)[:, :n]
        return r / np.maximum(r[:, :1], 1e-30), max_lag
    acf = raw_correlation(x, nfft)[:, :n]
    energy = np.cumsum(x ** 2, axis=1)
    total = energy[:, -1:]
    lags = np.arange(n)
    e_head = np.take_along_axis(energy, np.broadcast_to(n - 1 - lags, x.shape), axis=1)
    e_tail = total - np.hstack([np.zeros((x.shape[0], 1)), energy[:, :-1]])
    if method == "nccf":
        return acf / np.sqrt(np.maximum(e_head * e_tail, 1e-30)), max_lag
    if method == "yin":
        diff = (e_head + e_tail - 2 * acf) / (n - lags)
        cum = np.cumsum(diff[:, 1:], axis=1)
        cmnd = np.ones_like(diff)
        cmnd[:, 1:] = diff[:, 1:] * lags[1:] / np.maximum(cum, 1e-30)
        return 1.0 - cmnd, max_lag
    raise ValueError(f"unknown periodicity method {method}")


def gcc_segment(x: np.ndarray, p: dict, rate: float) -> tuple[np.ndarray, int]:
    """Weighted cross-correlation of the latest template_s against the history, per lag."""
    n = x.shape[1]
    tpl_len = int(round(p["gcc_template_s"] * rate))
    nfft = int(2 ** np.ceil(np.log2(2 * n)))
    template = x[:, n - tpl_len:]
    cross = np.fft.rfft(x, nfft) * np.conj(np.fft.rfft(template, nfft))
    weighted = cross / np.maximum(np.abs(cross), 1e-30) ** p["gcc_gamma"]
    if p["phat_band_hz"]:
        weighted = weighted * band_mask(nfft, rate, p["phat_band_hz"])
    cc = np.fft.irfft(weighted, nfft)
    start = n - tpl_len
    r = cc[:, start::-1]                       # r[k] = cc[start - k], k = 0..start
    return r / np.maximum(np.abs(r[:, :1]), 1e-30), start


def pool(r: np.ndarray, lags: np.ndarray, max_lag: int, mode: str, half_width: float):
    """Score per HR state from lag function r (1-D); NaN beyond max_lag."""
    valid = (lags <= max_lag) & (lags >= 1)
    out = np.full(lags.shape, np.nan)
    if not valid.any():
        return out
    if mode == "interp":
        out[valid] = np.interp(lags[valid], np.arange(r.size), r)
        return out
    lo = np.floor(lags[valid] / (1 + half_width)).astype(int)
    hi = np.minimum(np.ceil(lags[valid] * (1 + half_width)).astype(int), max_lag)
    idx = np.minimum(lo[:, None] + np.arange((hi - lo).max() + 1), hi[:, None])
    out[valid] = r[idx].max(axis=1)
    return out


def scores_from_lags(r: np.ndarray, max_lag: int, state: PeriodicityState) -> np.ndarray:
    """Comb (harmonic mean) and sub-harmonic penalty applied to the pooled lag function.

    A state is scored only when at least min_harmonics multiples of its period fit the window,
    i.e. the period has been observed repeating; this suppresses the S1-S2 lag early on.
    """
    p = state.cfg
    half_width = 0.5 * (state.lags[0] / state.lags[1] - 1)
    total = np.zeros_like(state.lags)
    count = np.zeros_like(state.lags)
    for m in range(1, p["comb_harmonics"] + 1):
        s = pool(r, m * state.lags, max_lag, p["pooling"], half_width)
        ok = ~np.isnan(s)
        total[ok] += s[ok]
        count[ok] += 1
    with np.errstate(invalid="ignore"):
        score = np.where(count >= p["min_harmonics"], total / np.maximum(count, 1), np.nan)
    if p["subharmonic_penalty"]:
        half = pool(r, state.lags / 2, max_lag, p["pooling"], half_width)
        score = score - p["subharmonic_penalty"] * np.maximum(np.nan_to_num(half), 0.0)
    return score


def spectral_scores(x: np.ndarray, state: PeriodicityState) -> np.ndarray:
    """Hann-windowed DFT magnitude at each state's frequency, normalised to the maximum.

    States whose period does not fit min_harmonics times in the window are NaN.
    """
    n = x.shape[1]
    phase = 2 * np.pi * np.outer(1.0 / state.lags, np.arange(n))
    windowed = x * np.hanning(n)
    mag = np.hypot(windowed @ np.cos(phase).T, windowed @ np.sin(phase).T).mean(axis=0)
    mag = mag / max(mag.max(), 1e-30)
    return np.where(state.lags * state.cfg["min_harmonics"] <= n, mag, np.nan)


def temporal_scores(x: np.ndarray, method: str, state: PeriodicityState) -> np.ndarray:
    """Lag-domain method pooled onto the HR states."""
    p = {**state.cfg, "method": method}
    if method == "gcc_segment":
        r, max_lag = gcc_segment(x, p, state.rate_hz)
    else:
        r, max_lag = lag_function(x, p, state.rate_hz)
    return scores_from_lags(r.mean(axis=0), max_lag, state)


FEATURE_NAMES = ["acf_T", "acf_half_T", "acf_2T", "valid_2T", "nccf_T", "yin_T", "dft_f",
                 "dft_2f", "dft_half_f", "acf_peak", "acf_rel_max", "periods_in_window"]


def features(state: PeriodicityState) -> np.ndarray | None:
    """Per-state periodicity features (n_states, len(FEATURE_NAMES)); None before min_window_s.

    Lag functions are computed once per frame; each feature relates the state's period T (or
    frequency f) to its multiples, so octave errors become separable.
    """
    x = state.history
    n = x.shape[1]
    if n < state.cfg["min_window_s"] * state.rate_hz:
        return None
    x = x - x.mean(axis=1, keepdims=True)
    rate = state.rate_hz
    p = {**state.cfg, "min_overlap_s": 0.0}
    half_width = 0.5 * (state.lags[0] / state.lags[1] - 1)

    def lagged(method, mult):
        """Method's lag function pooled at mult x each state's period; NaN -> 0."""
        r, max_lag = lag_function(x, {**p, "method": method}, rate)
        return pool(r.mean(axis=0), state.lags * mult, max_lag, "interp", half_width)

    acf = {m: lagged("acf", m) for m in (1.0, 0.5, 2.0)}
    nccf = lagged("nccf", 1.0)
    nccf[state.lags > n - state.cfg["min_overlap_s"] * rate] = np.nan
    yin = lagged("yin", 1.0)
    dft = spectral_scores_at(x, state, (1.0, 2.0, 0.5))
    acf_t = np.nan_to_num(acf[1.0])
    peak = (acf_t >= np.roll(acf_t, 2)) & (acf_t >= np.roll(acf_t, -2))
    cols = [acf[1.0], acf[0.5], np.nan_to_num(acf[2.0]), (~np.isnan(acf[2.0])).astype(float),
            nccf, yin, *dft, peak.astype(float), acf_t - acf_t.max(), np.log(n / state.lags)]
    return np.nan_to_num(np.column_stack(cols)).astype(np.float32)


def spectral_scores_at(x: np.ndarray, state: PeriodicityState, mults: tuple) -> list:
    """Hann-windowed DFT magnitude at mult x each state's frequency, normalised per frame."""
    n = x.shape[1]
    windowed = (x * np.hanning(n)).mean(axis=0)
    out = []
    for mult in mults:
        phase = 2 * np.pi * np.outer(mult / state.lags, np.arange(n))
        out.append(np.hypot(np.cos(phase) @ windowed, np.sin(phase) @ windowed))
    norm = max(out[0].max(), 1e-30)
    return [m / norm for m in out]


def compute(state: PeriodicityState) -> np.ndarray:
    """Score per HR state for the current history; all NaN before min_window_s."""
    p = state.cfg
    x = state.history
    if x.shape[1] < p["min_window_s"] * state.rate_hz:
        return np.full(state.lags.shape, np.nan)
    x = x - x.mean(axis=1, keepdims=True)
    if p["method"] == "dft":
        return spectral_scores(x, state)
    if p["method"] == "product":
        temporal = temporal_scores(x, p["product_temporal"], state)
        return np.maximum(temporal, 0.0) * spectral_scores(x, state) ** p["product_dft_power"]
    return temporal_scores(x, p["method"], state)


def resources(cfg: dict, n_channels: int, frames_per_s: float) -> dict:
    """Estimated MAC/s, RAM and constants (bytes; 16-bit history, 32-bit lag values)."""
    p = cfg["periodicity"]
    rate = input_rate(cfg)
    n = int(round(p["window_s"] * rate))
    n_lags = int(np.ceil(60.0 * rate / cfg["tracker"]["hr_min_bpm"] * p["comb_harmonics"]))
    n_lags = min(n_lags, n)
    nfft = int(2 ** np.ceil(np.log2(2 * n)))
    if p["method"] in ("dft", "product"):
        n_states = hr_grid(cfg).size
        per_frame = 2 * n_states * n + (n_lags * n if p["method"] == "product" else 0)
        scratch = 4 * n_lags + 8 * n_states
    elif p["method"] in ("gacf", "gcc_segment"):
        ffts = 2 if p["method"] == "gacf" else 3
        per_frame = ffts * 2 * nfft * np.log2(nfft) + 50 * nfft
        scratch = (2 if p["method"] == "gacf" else 3) * 2 * nfft
    else:
        per_frame = n_lags * n
        scratch = 4 * n_lags
    return {"macs_per_s": n_channels * per_frame * frames_per_s,
            "ram_bytes": n_channels * 2 * n + scratch, "const_bytes": 0}


def feature_resources(cfg: dict, frames_per_s: float) -> dict:
    """MAC/s and RAM of features() as planned for the MCU: 16-bit history, 32-bit ACF up to the
    largest lag used (2 x the longest period), overlap energies updated per lag (no arrays),
    state DFT by Goertzel (no tables), per-state feature rows computed on the fly."""
    rate = input_rate(cfg)
    n = int(round(cfg["periodicity"]["window_s"] * rate))
    n_lags = min(n, int(np.ceil(2 * 60.0 * rate / cfg["tracker"]["hr_min_bpm"])))
    n_states = hr_grid(cfg).size
    per_frame = n_lags * n + 2 * n_lags + 3 * 2 * n_states * n + 20 * n_states
    return {"macs_per_s": per_frame * frames_per_s, "ram_bytes": 2 * n + 4 * n_lags + 64,
            "const_bytes": 0}
