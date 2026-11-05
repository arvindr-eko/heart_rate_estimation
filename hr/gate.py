"""Learned release confidence: P(current HR estimate is within tolerance) per frame.

Input per frame: statistics of the tracker posterior and the frame salience (FEATURE_NAMES).
Output: probability that the tracker estimate is within the EC13 tolerance; the release rule
compares it with a time-dependent threshold.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

FEATURE_NAMES = ["tracker_conf", "frame_conf", "octave_mass", "low_hr_mass", "entropy", "log_hr",
                 "log_t", "frame_disagree", "hr_change_1s", "unscored_share", "log_periods"]


@dataclass
class GateModel:
    """Feature standardisation and MLP weights (one hidden ReLU layer, sigmoid output)."""
    mean: np.ndarray
    std: np.ndarray
    w1: np.ndarray        # (n_features, hidden)
    b1: np.ndarray
    w2: np.ndarray        # (hidden,)
    b2: float


def load(path: Path) -> GateModel:
    """Load weights saved by gate_train.save."""
    z = np.load(path)
    return GateModel(mean=z["mean"], std=z["std"], w1=z["w1"], b1=z["b1"], w2=z["w2"],
                     b2=float(z["b2"]))


def features(posterior: np.ndarray, grid: np.ndarray, conf_halfwidth: int, octave_shift: int,
             hr: float, conf: float, frame_hr: float, frame_conf: float, unscored_share: float,
             t: float, hr_1s_ago: float, window_s: float, low_hr_bpm: float) -> np.ndarray:
    """Frame statistics in FEATURE_NAMES order; missing values mapped to fixed sentinels."""
    i = int(np.argmax(posterior))
    octave = 0.0
    for j in (i - octave_shift, i + octave_shift):
        if 0 <= j < posterior.size:
            octave += float(posterior[max(0, j - conf_halfwidth):j + conf_halfwidth + 1].sum())
    nz = posterior[posterior > 0]
    entropy = float(-(nz * np.log(nz)).sum() / np.log(posterior.size))
    disagree = abs(np.log(frame_hr / hr)) if np.isfinite(frame_hr) and frame_hr > 0 else 1.0
    change = abs(np.log(hr / hr_1s_ago)) if np.isfinite(hr_1s_ago) else 1.0
    return np.array([conf, frame_conf if np.isfinite(frame_conf) else 0.0, octave,
                     float(posterior[grid < low_hr_bpm].sum()), entropy, np.log(hr), np.log(t),
                     disagree, change, unscored_share, np.log(min(t, window_s) * hr / 60.0)],
                    dtype=np.float32)


def predict(model: GateModel, feats: np.ndarray) -> float:
    """P(estimate within tolerance)."""
    hidden = np.maximum((feats - model.mean) / model.std @ model.w1 + model.b1, 0.0)
    return float(1.0 / (1.0 + np.exp(-(hidden @ model.w2 + model.b2))))


def resources(model: GateModel, frames_per_s: float) -> dict:
    """MAC/s and weight bytes (8-bit) of the gate, plus its posterior statistics."""
    n_in, hidden = model.w1.shape
    params = model.w1.size + model.b1.size + model.w2.size + 1 + 2 * n_in
    return {"macs_per_s": (n_in * hidden + hidden + 3 * 205) * frames_per_s, "ram_bytes": 64,
            "const_bytes": params}
