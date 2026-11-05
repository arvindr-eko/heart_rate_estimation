"""Streaming beat picking on the network activation and beat-interval HR evidence.

Input per frame: activation value (env_rate_hz), smoothed by a causal moving average of
smooth_s. A beat is a local maximum (one frame of look-ahead) of the smoothed activation above
threshold_rel x a decaying peak level, after warmup_s, at least the refractory spacing after the
previous beat; the spacing becomes rr_frac x the median interval once intervals exist.
Output: the last max_intervals beat intervals, from which per-state match features are formed.
"""

from dataclasses import dataclass, field

import numpy as np

FEATURE_NAMES = ["int_match_T", "int_match_2T", "int_match_half_T", "log_n_intervals",
                 "interval_cv"]


@dataclass
class BeatState:
    """Smoothing buffer, look-ahead values, decaying peak level, frame counter, recent beats."""
    cfg: dict
    rate_hz: float
    decay: float
    smooth: np.ndarray
    prev2: float = -np.inf
    prev1: float = -np.inf
    peak_level: float = 0.0
    n_frames: int = 0
    beat_frames: list = field(default_factory=list)


def init(cfg: dict) -> BeatState:
    """Empty detector; peak level halves every half_life_s."""
    b = cfg["beats"]
    rate = float(cfg["frontend"]["env_rate_hz"])
    return BeatState(cfg=b, rate_hz=rate, decay=0.5 ** (1.0 / (b["half_life_s"] * rate)),
                     smooth=np.zeros(max(1, int(round(b["smooth_s"] * rate)))))


def spacing_frames(state: BeatState) -> float:
    """Minimum beat spacing: refractory_s, or rr_frac x the median interval when known."""
    floor = state.cfg["refractory_s"] * state.rate_hz
    if len(state.beat_frames) < 3:
        return floor
    return max(floor, state.cfg["rr_frac"] * float(np.median(np.diff(state.beat_frames))))


def push(state: BeatState, activation: np.ndarray) -> None:
    """Consume activation frames; register beats at local maxima one frame back."""
    warmup = state.cfg["warmup_s"] * state.rate_hz
    for raw in np.ravel(activation):
        state.smooth = np.roll(state.smooth, -1)
        state.smooth[-1] = raw
        value = float(state.smooth[-min(state.n_frames + 1, state.smooth.size):].mean())
        state.peak_level = max(value, state.peak_level * state.decay)
        candidate = state.n_frames - 1
        is_peak = state.prev1 > state.prev2 and state.prev1 >= value
        above = state.prev1 >= state.cfg["threshold_rel"] * state.peak_level
        spaced = (not state.beat_frames
                  or candidate - state.beat_frames[-1] >= spacing_frames(state))
        if is_peak and above and spaced and candidate >= warmup:
            keep = state.cfg["max_intervals"] + 1
            state.beat_frames = (state.beat_frames + [candidate])[-keep:]
        state.prev2, state.prev1 = state.prev1, float(value)
        state.n_frames += 1


def features(state: BeatState, grid_bpm: np.ndarray) -> np.ndarray:
    """Per-state interval features (n_states, len(FEATURE_NAMES)); zeros before two beats.

    Match = Gaussian in log-HR between the state (and its double and half) and the HR of the
    median interval, width the larger of match_width_rel and the interval variation.
    """
    out = np.zeros((grid_bpm.size, len(FEATURE_NAMES)), dtype=np.float32)
    if len(state.beat_frames) < 2:
        return out
    intervals = np.diff(state.beat_frames) / state.rate_hz
    hr_int = 60.0 / float(np.median(intervals))
    cv = float(intervals.std() / intervals.mean()) if intervals.size > 1 else 0.0
    width = max(state.cfg["match_width_rel"], cv)
    log_grid = np.log(grid_bpm)
    for j, mult in enumerate((1.0, 0.5, 2.0)):
        out[:, j] = np.exp(-0.5 * ((log_grid - np.log(hr_int * mult)) / width) ** 2)
    out[:, 3] = np.log(intervals.size)
    out[:, 4] = cv
    return out


def resources(cfg: dict, n_states: int, frames_per_s: float) -> dict:
    """MAC/s and RAM: per-frame peak test plus per-state matches each tracker frame."""
    rate = cfg["frontend"]["env_rate_hz"]
    return {"macs_per_s": 10 * rate + 3 * 10 * n_states * frames_per_s,
            "ram_bytes": 4 * (cfg["beats"]["max_intervals"] + 1) + 32, "const_bytes": 0}
