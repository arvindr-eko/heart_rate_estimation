"""Block F, readout: when the first reading is given, then one reading per output period.

User settings (config `readout`, runtime, no retraining): confidence (probability of being
within ±3 BPM the first reading of a regular heart must reach), slow_irregular_after_s (earliest
first reading for slow or irregular hearts) and latest_s (latest first reading when a heart
sound is present). Internal constants: config `decision`. Calibration, per model and path
(training.train.calibrate, validation split): logistic model of the probability of being within
tolerance from the posterior mass near the estimate and the periodicity, 3 coefficients.

Per frame, before the first reading (never before min_s; never while the activation's ACF at
the estimate's period is below min_periodicity or its standard deviation over the features
window is below min_activation_sd, i.e. without heartbeats: silence, steady sounds such as
mains hum):
  path       slow_irregular when the estimate is below slow_bpm or the picked beats' interval CV
             is at least regular_max_cv; regular otherwise.
  regular    due when the frame's own best HR agrees with the tracker (estimate_tol_rel) and the
             probability of being within ±3 BPM (from the mass within ±conf_fine_bpm and the
             periodicity) reaches confidence.
  slow/irr.  due from slow_irregular_after_s when the probability of being within ±10 BPM (from
             the mass within ±conf_rough_bpm and the periodicity) reaches
             slow_irregular_confidence.
  forced     due at latest_s when the mass within ±conf_rough_bpm reaches forced_min_mass.
A reading is (t_s, bpm, confidence), the confidence being the calibrated probability for the
frame's path.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

@dataclass
class Calibration:
    """Logistic coefficients (intercept, posterior mass, periodicity) of the probability of being
    within ±3 BPM on the regular path (fine) and within ±10 BPM otherwise (rough)."""
    fine: np.ndarray
    rough: np.ndarray


@dataclass
class ReadoutState:
    """Times of the first and the latest reading; None before the first."""
    first_at: float | None = None
    last_output: float | None = None


def calibration_path(cfg: dict) -> Path:
    """Calibration file of the configured model: <models_dir>/<weights>_calibration.npz."""
    return cfg["paths"]["models_dir"] / f"{cfg['weights']}_calibration.npz"


def load_calibration(cfg: dict) -> Calibration:
    """The model's calibration; probability = mass when not fitted yet (logit(mass) slope
    approximated by a steep line through mass 0.5)."""
    path = calibration_path(cfg)
    if not path.is_file():
        neutral = np.array([-5.0, 10.0, 0.0], np.float32)
        return Calibration(fine=neutral, rough=neutral)
    z = np.load(path)
    return Calibration(fine=z["fine"], rough=z["rough"])


def mass_within(posterior: np.ndarray, grid: np.ndarray, hr: float,
                half_width_bpm: float) -> float:
    """Posterior mass of the states within ±half_width_bpm of hr."""
    return float(posterior[np.abs(grid - hr) <= half_width_bpm].sum())


def probability(weights: np.ndarray, mass: float, periodicity: float) -> float:
    """Logistic probability from (intercept, mass, periodicity) coefficients."""
    return float(1.0 / (1.0 + np.exp(-(weights[0] + weights[1] * mass + weights[2] * periodicity))))


def assess(cfg: dict, cal: Calibration, posterior: np.ndarray, grid: np.ndarray, hr: float,
           frame_hr: float, cv: float, periodicity: float) -> dict:
    """Frame assessment: path, probabilities, raw masses, agreement."""
    dc = cfg["decision"]
    fine = mass_within(posterior, grid, hr, dc["conf_fine_bpm"])
    rough = mass_within(posterior, grid, hr, dc["conf_rough_bpm"])
    slow_irregular = hr < dc["slow_bpm"] or (not np.isnan(cv) and cv >= dc["regular_max_cv"])
    return {"path": "slow_irregular" if slow_irregular else "regular",
            "p_fine": probability(cal.fine, fine, periodicity),
            "p_rough": probability(cal.rough, rough, periodicity),
            "fine_mass": fine, "rough_mass": rough,
            "agree": not np.isnan(frame_hr) and abs(np.log(hr / frame_hr)) <= np.log1p(
                cfg["tracker"]["estimate_tol_rel"])}


def confidence_of(a: dict) -> float:
    """Calibrated probability reported with a reading of this frame's path."""
    return a["p_fine"] if a["path"] == "regular" else a["p_rough"]


def first_due(cfg: dict, a: dict, periodicity: float, activation_sd: float, hr: float,
              t: float) -> bool:
    """True when the first reading is due at time t (module docstring)."""
    rd, dc = cfg["readout"], cfg["decision"]
    if (np.isnan(hr) or t + 1e-9 < dc["min_s"] or periodicity < dc["min_periodicity"]
            or activation_sd < dc["min_activation_sd"]):
        return False
    if t + 1e-9 >= rd["latest_s"]:
        return a["rough_mass"] >= dc["forced_min_mass"]
    if a["path"] == "regular":
        return a["agree"] and a["p_fine"] >= rd["confidence"]
    return (t + 1e-9 >= rd["slow_irregular_after_s"]
            and a["p_rough"] >= dc["slow_irregular_confidence"])


def schedule(state: ReadoutState, cfg: dict, due: bool, hr: float, confidence: float,
             t: float) -> tuple | None:
    """Reading (t_s, bpm, confidence) on output slots, None otherwise."""
    if state.first_at is None:
        if not due:
            return None
        state.first_at = state.last_output = t
        return (t, hr, confidence)
    if t + 1e-9 < state.last_output + cfg["stream"]["output_period_s"]:
        return None
    state.last_output = t
    return (t, hr, confidence)
