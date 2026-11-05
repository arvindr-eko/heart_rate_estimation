"""Learned salience: per-state periodicity features -> emission log-likelihood over HR states.

A small MLP with weights shared across states scores every state from its features; the
log-softmax over states is the tracker emission. HR itself is not an input, so the model
cannot learn the HR distribution of the training data.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class SalienceModel:
    """Feature standardisation and MLP weights (float32)."""
    mean: np.ndarray
    std: np.ndarray
    w1: np.ndarray        # (n_features, hidden)
    b1: np.ndarray
    w2: np.ndarray        # (hidden,)
    b2: float


def load(path: Path) -> SalienceModel:
    """Load weights saved by salience_train.save."""
    z = np.load(path)
    return SalienceModel(mean=z["mean"], std=z["std"], w1=z["w1"], b1=z["b1"], w2=z["w2"],
                         b2=float(z["b2"]))


def logits(model: SalienceModel, feats: np.ndarray) -> np.ndarray:
    """Unnormalised score per state from (n_states, n_features)."""
    hidden = np.maximum((feats - model.mean) / model.std @ model.w1 + model.b1, 0.0)
    return hidden @ model.w2 + model.b2


def log_emission(model: SalienceModel, feats: np.ndarray) -> np.ndarray:
    """Log-softmax of the logits over states."""
    z = logits(model, feats)
    z = z - z.max()
    return z - np.log(np.exp(z).sum())


def resources(model: SalienceModel, n_states: int, frames_per_s: float) -> dict:
    """MAC/s and weight bytes (8-bit weights) of the MLP over all states."""
    n_in, hidden = model.w1.shape
    macs = n_states * (n_in * hidden + hidden + 2 * n_in)
    params = model.w1.size + model.b1.size + model.w2.size + 1 + 2 * n_in
    return {"macs_per_s": macs * frames_per_s, "ram_bytes": 4 * n_states,
            "const_bytes": params}
