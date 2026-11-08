"""Block R, rhythm: beats picked from the activation, their regularity and the beat-count HR.

Input per frame: the activation over the feature window (block C ring buffer, oldest first) and
the tracker's HR estimate. Beats: local maxima of the activation of at least peak_min_rel x the
window maximum, taken greedily from the highest and kept at least refractory_rel x the
estimate's period apart (suppresses S2). Output: the beat-interval CV (SD / mean; NaN with fewer
than min_intervals intervals) and the beat-count HR, 60 x (n - 1) / (last - first beat), the
same definition as the ECG reference. Per frame cost: one pass over the window plus a sort of
the local maxima.
"""

import numpy as np


def pick_beats(act: np.ndarray, period: float, peak_min_rel: float,
               refractory_rel: float) -> np.ndarray:
    """Indices of the picked beats, ascending."""
    if act.size < 3 or not np.isfinite(period):
        return np.zeros(0, dtype=int)
    k = np.arange(1, act.size - 1)
    peaks = k[(act[k] > act[k - 1]) & (act[k] >= act[k + 1])
              & (act[k] >= peak_min_rel * act.max())]
    kept: list[int] = []
    for i in peaks[np.argsort(-act[peaks], kind="stable")]:
        if all(abs(int(i) - j) >= refractory_rel * period for j in kept):
            kept.append(int(i))
    return np.sort(np.array(kept, dtype=int))


def analyse(act: np.ndarray, rate_hz: float, hr: float, rh: dict) -> tuple[float, float]:
    """Beat-interval CV and beat-count HR (BPM) of the activation window; NaN when undefined."""
    if np.isnan(hr):
        return np.nan, np.nan
    beats = pick_beats(act, 60.0 * rate_hz / hr, rh["peak_min_rel"], rh["refractory_rel"])
    rr = np.diff(beats)
    if rr.size < rh["min_intervals"]:
        return np.nan, np.nan
    return float(rr.std() / rr.mean()), float(60.0 * rate_hz * rr.size / (beats[-1] - beats[0]))
