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
             slow_irregular_early_confidence, from slow_irregular_relaxed_s on
             slow_irregular_confidence.
  forced     due at latest_s when the mass within ±conf_rough_bpm reaches forced_min_mass.
A reading is (t_s, bpm, confidence), the confidence being the calibrated probability for the
frame's path. After the first reading, one reading per output period while a heartbeat is
heard; with none for lost_after_s the measurement restarts (heart_absent, lost).
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

PRESENCE_FEATURES = ("periodicity", "recent", "ln_activation_sd", "agree", "rough_mass",
                     "fine_mass", "high_band", "high_band_x_periodicity")
PRESENCE_SD_FLOOR = 1e-4    # activation SD floor before the logarithm
PRESENCE_HF_RANGE = (-12.0, 4.0)   # high-band ratio clipped to this range (-inf: no input yet)

@dataclass
class Calibration:
    """Logistic coefficients (intercept, posterior mass, periodicity) of the probability of being
    within ±3 BPM on the regular path (fine) and within ±10 BPM otherwise (rough)."""
    fine: np.ndarray
    rough: np.ndarray


@dataclass
class ReadoutState:
    """Times of the first and the latest reading (None before the first); frames without a
    heartbeat since the first reading; consecutive frames with a heart present."""
    first_at: float | None = None
    last_output: float | None = None
    absent_frames: int = 0
    present_frames: int = 0


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


@dataclass
class Presence:
    """Learned heart-present gate (training.train.fit_presence): small MLP over
    PRESENCE_FEATURES, one ReLU hidden layer (input standardisation folded into it) and a
    logistic output."""
    w1: np.ndarray      # (features, hidden)
    b1: np.ndarray      # (hidden,)
    w2: np.ndarray      # (hidden,)
    b2: float


def presence_path(cfg: dict) -> Path:
    """Presence gate file of the configured model: <models_dir>/<weights>_presence.npz."""
    return cfg["paths"]["models_dir"] / f"{cfg['weights']}_presence.npz"


def load_presence(cfg: dict) -> Presence | None:
    """The model's presence gate; None when not fitted (no gate)."""
    path = presence_path(cfg)
    if not path.is_file():
        return None
    z = np.load(path)
    return Presence(w1=z["w1"].astype(np.float32), b1=z["b1"].astype(np.float32),
                    w2=z["w2"].astype(np.float32), b2=float(z["b2"]))


def presence_features(periodicity: float, recent: float, activation_sd: float, hf: float,
                      a: dict) -> np.ndarray:
    """Inputs of the presence gate (PRESENCE_FEATURES): periodicity, recent heartbeat, ln
    activation SD (floored), frame agreement, the posterior masses, the high-band ratio (block Q,
    clipped to PRESENCE_HF_RANGE) and its product with the periodicity (high-band sound in step
    with the heartbeat, e.g. a murmur, against sound without it)."""
    h = min(max(hf, PRESENCE_HF_RANGE[0]), PRESENCE_HF_RANGE[1])
    return np.array([periodicity, recent, np.log(max(activation_sd, PRESENCE_SD_FLOOR)),
                     float(a["agree"]), a["rough_mass"], a["fine_mass"], h, h * periodicity],
                    np.float32)


def presence(model: Presence | None, x: np.ndarray) -> float:
    """Heart-present probability of the gate; 1 without a gate."""
    if model is None:
        return 1.0
    hidden = np.maximum(x @ model.w1 + model.b1, np.float32(0.0))
    return float(1.0 / (1.0 + np.exp(-(model.b2 + float(np.dot(model.w2, hidden))))))


def heart_present(cfg: dict, periodicity: float, activation_sd: float, recent: float = 1.0,
                  clean: bool = True, p_present: float = 1.0) -> bool:
    """True while a heartbeat is heard: periodicity, activation SD and the recent heartbeat
    (estimator.recent_heartbeat) at or above their gates (silence, steady noise and mains hum
    fall below the first two; noise after the heart stops, the third) and the signal clean
    (block Q: no excess 200-400 Hz energy)."""
    dc = cfg["decision"]
    return (clean and p_present >= dc["presence_min"] and periodicity >= dc["min_periodicity"]
            and activation_sd >= dc["min_activation_sd"] and recent >= dc["recent_min"])


def heart_absent(cfg: dict, periodicity: float, activation_sd: float, recent: float = 1.0,
                 noisy: bool = False, p_present: float = 1.0) -> bool:
    """During a measurement: no heartbeat heard. Hysteresis against heart_present: periodicity
    below lost_periodicity (lower than min_periodicity, so irregular rhythms, whose periodicity
    dips, are not dropped), activation SD below min_activation_sd, the recent heartbeat below
    recent_lost, or the signal noisy (block Q: 200-400 Hz energy above hf_lost)."""
    dc = cfg["decision"]
    return (noisy or p_present < dc["presence_lost"] or periodicity < dc["lost_periodicity"]
            or activation_sd < dc["min_activation_sd"] or recent < dc["recent_lost"])


def frames_of(seconds: float, frame_s: float) -> int:
    """Whole frames covering seconds."""
    return int(np.ceil(seconds / frame_s - 1e-9))


def track(state: ReadoutState, present: bool) -> None:
    """Per frame: run of frames with a heart present."""
    state.present_frames = state.present_frames + 1 if present else 0


def lost_frames(cfg: dict, frame_s: float) -> int:
    """Frames without a heartbeat after which the measurement restarts (lost_after_s)."""
    return int(np.ceil(cfg["decision"]["lost_after_s"] / frame_s - 1e-9))


def lost(state: ReadoutState, cfg: dict, absent: bool, frame_s: float) -> bool:
    """After the first reading: True once no heartbeat has been heard (heart_absent) for
    lost_after_s, so the estimator restarts the measurement (stethoscope lifted, contact
    lost)."""
    if state.first_at is None:
        return False
    state.absent_frames = state.absent_frames + 1 if absent else 0
    return state.absent_frames >= lost_frames(cfg, frame_s)


def first_due(cfg: dict, a: dict, periodicity: float, activation_sd: float, hr: float,
              t: float, recent: float = 1.0, state: ReadoutState | None = None,
              frame_s: float = 0.25, clean: bool = True, p_present: float = 1.0) -> bool:
    """True when the first reading is due at time t (module docstring), the heart present for
    the last confirm_s (state.present_frames, counted by track)."""
    rd, dc = cfg["readout"], cfg["decision"]
    if (np.isnan(hr) or t + 1e-9 < dc["min_s"]
            or not heart_present(cfg, periodicity, activation_sd, recent, clean, p_present)
            or (state is not None and state.present_frames < frames_of(dc["confirm_s"], frame_s))):
        return False
    if t + 1e-9 >= rd["latest_s"]:
        return a["rough_mass"] >= dc["forced_min_mass"]
    if a["path"] == "regular":
        return a["agree"] and a["p_fine"] >= rd["confidence"]
    if t + 1e-9 < rd["slow_irregular_after_s"]:
        return False
    early = t + 1e-9 < dc["slow_irregular_relaxed_s"]
    return a["p_rough"] >= dc["slow_irregular_early_confidence" if early
                              else "slow_irregular_confidence"]


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
