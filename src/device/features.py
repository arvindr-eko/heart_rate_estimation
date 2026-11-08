"""Block C, periodicity features per HR state, updated sample by sample.

Two inputs use this code (device.estimator): the activation (window_s 4 s at 100 Hz, all
FEATURE_NAMES) and the normalised 25-50 Hz band where S1 dominates (s1_window_s 2 s at 50 Hz,
S1_FEATURES), whose periodicity separates the true period from S1-S2 halves.

Input: one value per envelope sample (env_rate_hz), pushed as it is produced. State: a ring
buffer of the last window_s of samples and lag sums S[k], the sum of x_i * x_(i-k) over the
sample pairs inside the window, k = 0 .. window - 1. Each new sample adds its pairs and, once
the window is full, the oldest sample's pairs are removed (2 MAC per lag per sample), so the
window is never recomputed.
Per frame: mean removal from S and the head and tail sums of the window (prefix sums here; a
running sum in the lag loop on the device), then the lag functions ACF (normalised to lag 0),
NCCF (normalised by the overlap energies) and YIN (1 - cumulative-mean normalised difference),
read at each state's lag T by linear interpolation. Output per frame: (n_states, n_features),
float32; None before min_window_s.
All state is float32 with fixed sizes set at init, as on the device.
"""

from dataclasses import dataclass

import numpy as np

FEATURE_NAMES = ["acf_T", "acf_half_T", "acf_2T", "valid_2T", "nccf_T", "yin_T", "acf_peak",
                 "acf_rel_max", "periods_in_window", "acf_3T", "valid_3T", "peak_offset",
                 "nccf_2T"]
S1_FEATURES = ["acf_T", "acf_half_T", "acf_2T"]   # of the 25-50 Hz band, where S1 dominates
ACF_T = FEATURE_NAMES.index("acf_T")
PEAK_OFFSET_MAX = 0.1   # relative lag offset beyond which no nearby ACF peak is assumed
EPS = 1e-12
VAR_FLOOR = 1e-6        # window variance below this: no periodicity (flat input, e.g. silence)


@dataclass
class FeatureState:
    """Ring buffer, lag sums, sample count, state lags (samples) and feature settings."""
    hist: np.ndarray            # (n_max,) last samples, ring buffer
    sums: np.ndarray            # (n_max,) lag sums S[k]
    count: int                  # samples pushed since init
    lags: np.ndarray            # (n_states,) period of each HR state in samples
    rate_hz: float
    cfg: dict


def hr_grid(cfg: dict) -> np.ndarray:
    """Log-spaced HR states (BPM) from hr_min to hr_max, step_rel apart."""
    g = cfg["grid"]
    step = np.log1p(g["step_rel"])
    n = int(np.floor(np.log(g["hr_max_bpm"] / g["hr_min_bpm"]) / step)) + 1
    return g["hr_min_bpm"] * np.exp(step * np.arange(n))


def init(cfg: dict, window_s: float | None = None, rate_hz: float | None = None) -> FeatureState:
    """Empty buffer and sums of window_s at rate_hz (defaults: features.window_s, the envelope
    rate)."""
    rate = float(rate_hz or cfg["frontend"]["env_rate_hz"])
    n_max = int(round((window_s or cfg["features"]["window_s"]) * rate))
    return FeatureState(hist=np.zeros(n_max, np.float32), sums=np.zeros(n_max, np.float32),
                        count=0, lags=(60.0 * rate / hr_grid(cfg)).astype(np.float32),
                        rate_hz=rate, cfg=cfg["features"])


def window(state: FeatureState) -> np.ndarray:
    """Samples in the window, oldest first."""
    n_max = state.hist.size
    n = min(state.count, n_max)
    return state.hist[(state.count - n + np.arange(n)) % n_max]


def push(state: FeatureState, samples: np.ndarray) -> None:
    """Add samples one by one: remove the oldest sample's lag pairs once the window is full,
    then add the new sample's pairs."""
    n_max = state.hist.size
    for x in np.asarray(samples, np.float32).ravel():
        w = window(state)
        if w.size == n_max:
            state.sums -= w[0] * w
            w = w[1:]
        w = np.append(w, x)
        state.sums[:w.size] += x * w[::-1]
        state.hist[state.count % n_max] = x
        state.count += 1


def lag_functions(state: FeatureState) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """ACF, NCCF and YIN over lags 0 .. n - 1 of the mean-removed window."""
    w = window(state)
    n = w.size
    mu = np.float32(w.mean())
    k = np.arange(n)
    p = np.concatenate([[0], np.cumsum(w)]).astype(np.float32)
    q = np.concatenate([[0], np.cumsum(w * w)]).astype(np.float32)
    head, tail = p[n - k], p[n] - p[k]
    pairs = (n - k).astype(np.float32)
    r = state.sums[:n] - mu * (head + tail) + pairs * mu * mu
    e_head = q[n - k] - 2 * mu * head + pairs * mu * mu
    e_tail = q[n] - q[k] - 2 * mu * tail + pairs * mu * mu
    if r[0] <= n * VAR_FLOOR:
        zeros = np.zeros(n, np.float32)
        return zeros, zeros, zeros
    acf = r / max(r[0], EPS)
    nccf = r / np.sqrt(np.maximum(e_head * e_tail, EPS))
    diff = (e_head + e_tail - 2 * r) / pairs
    cmnd = np.ones(n, np.float32)
    cmnd[1:] = diff[1:] * k[1:] / np.maximum(np.cumsum(diff[1:]), EPS)
    return acf, nccf, 1.0 - cmnd


def at_lags(r: np.ndarray, lags: np.ndarray, max_lag: int) -> np.ndarray:
    """Lag function r linearly interpolated at the given lags; NaN outside 1 .. max_lag."""
    valid = (lags <= max_lag) & (lags >= 1)
    out = np.full(lags.shape, np.nan, np.float32)
    if valid.any():
        out[valid] = np.interp(lags[valid], np.arange(r.size), r)
    return out


def peak_offsets(r: np.ndarray, lags: np.ndarray) -> np.ndarray:
    """Relative offset (T_peak - T) / T from each lag T to the nearest local maximum of r,
    located to a fraction of a sample by parabolic interpolation; clipped to PEAK_OFFSET_MAX."""
    k = np.arange(1, r.size - 1)
    peaks = k[(r[k] > r[k - 1]) & (r[k] >= r[k + 1])]
    if peaks.size == 0:
        return np.full(lags.shape, PEAK_OFFSET_MAX, np.float32)
    a, b, c = r[peaks - 1], r[peaks], r[peaks + 1]
    curvature = a - 2 * b + c
    pos = peaks + np.where(curvature < 0, 0.5 * (a - c) / np.where(curvature < 0, curvature, 1),
                           0)
    j = np.clip(np.searchsorted(pos, lags), 1, max(pos.size - 1, 1))
    left, right = pos[j - 1], pos[np.minimum(j, pos.size - 1)]
    nearest = np.where(np.abs(left - lags) < np.abs(right - lags), left, right)
    return np.clip((nearest - lags) / lags, -PEAK_OFFSET_MAX, PEAK_OFFSET_MAX).astype(np.float32)


def compute(state: FeatureState) -> np.ndarray | None:
    """Features (n_states, len(FEATURE_NAMES)) of the current window; None before
    min_window_s."""
    n = min(state.count, state.hist.size)
    if n < state.cfg["min_window_s"] * state.rate_hz:
        return None
    acf, nccf, yin = lag_functions(state)
    lags = state.lags
    acf_at = {m: at_lags(acf, lags * m, n - 1) for m in (1.0, 0.5, 2.0, 3.0)}
    min_overlap = state.cfg["min_overlap_s"] * state.rate_hz
    nccf_t = at_lags(nccf, lags, n - 1)
    nccf_t[lags > n - min_overlap] = np.nan
    nccf_2t = at_lags(nccf, 2 * lags, n - 1)
    nccf_2t[2 * lags > n - min_overlap] = np.nan
    acf_t = np.nan_to_num(acf_at[1.0])
    peak = (acf_t >= np.roll(acf_t, 2)) & (acf_t >= np.roll(acf_t, -2))
    cols = {"acf_T": acf_at[1.0], "acf_half_T": acf_at[0.5],
            "acf_2T": np.nan_to_num(acf_at[2.0]), "valid_2T": ~np.isnan(acf_at[2.0]),
            "nccf_T": nccf_t, "yin_T": at_lags(yin, lags, n - 1), "acf_peak": peak,
            "acf_rel_max": acf_t - acf_t.max(), "periods_in_window": np.log(n / lags),
            "acf_3T": np.nan_to_num(acf_at[3.0]), "valid_3T": ~np.isnan(acf_at[3.0]),
            "peak_offset": peak_offsets(acf, lags), "nccf_2T": nccf_2t}
    return np.nan_to_num(np.column_stack([cols[name] for name in FEATURE_NAMES])).astype(
        np.float32)
