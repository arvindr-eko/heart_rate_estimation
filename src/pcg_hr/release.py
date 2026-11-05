"""Block F, deadline release: when the first reading is shown, then one reading per second.

The frame confidence (scorer probability within conf_tol_rel of its best state) must agree with
the tracker and reach a threshold that depends on time: c_early before the cutoff, then falling
linearly from c_cutoff to 0 at max_s. The cutoff is low_hr_s while the heart may be slower than
low_hr_bpm (estimate below it, or at least low_mass_min of the posterior below it), otherwise
default_s. Every recording is answered by max_s.
"""

from dataclasses import dataclass

import numpy as np


@dataclass
class ReleaseState:
    """Time of the first reading and of the last reading, None before release."""
    released_at: float | None = None
    last_output: float | None = None


def threshold(rel: dict, t: float, slow: bool) -> float:
    """Confidence needed at time t."""
    cutoff = rel["low_hr_s"] if slow else rel["default_s"]
    if t + 1e-9 < cutoff:
        return rel["c_early"]
    span = max(rel["max_s"] - cutoff, 1e-9)
    return rel["c_cutoff"] * max(0.0, (rel["max_s"] - t) / span)


def ready(cfg: dict, posterior: np.ndarray, grid: np.ndarray, hr: float, frame_hr: float,
          frame_conf: float, t: float) -> bool:
    """True when the first reading may be shown at time t."""
    rel = cfg["release"]
    if np.isnan(hr) or t + 1e-9 < rel["min_s"]:
        return False
    if t + 1e-9 >= rel["max_s"]:
        return True
    slow = (hr < rel["low_hr_bpm"]
            or float(posterior[grid < rel["low_hr_bpm"]].sum()) >= rel["low_mass_min"])
    agree = abs(np.log(hr / frame_hr)) <= np.log1p(cfg["tracker"]["conf_tol_rel"])
    return bool(agree and frame_conf >= threshold(rel, t, slow))


def schedule(state: ReleaseState, cfg: dict, is_ready: bool, hr: float, conf: float,
             t: float) -> tuple | None:
    """Reading (t_s, bpm, confidence) on output slots, None otherwise."""
    if state.released_at is None:
        if not is_ready:
            return None
        state.released_at = state.last_output = t
        return (t, hr, conf)
    if t + 1e-9 < state.last_output + cfg["release"]["output_period_s"]:
        return None
    state.last_output = t
    return (t, hr, conf)
