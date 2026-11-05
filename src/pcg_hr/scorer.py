"""Block D, scorer: per-state features -> log-likelihood over HR states.

A two-layer MLP with the same weights for every state scores each state from its features; the
log-softmax over states is the tracker's evidence. HR itself is not an input.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class ScorerModel:
    """Feature standardisation and MLP weights (float32)."""
    mean: np.ndarray
    std: np.ndarray
    w1: np.ndarray        # (n_features, hidden)
    b1: np.ndarray
    w2: np.ndarray        # (hidden,)
    b2: float


def load(path: Path) -> ScorerModel:
    """Load weights saved by train_scorer.save."""
    z = np.load(path)
    return ScorerModel(mean=z["mean"], std=z["std"], w1=z["w1"], b1=z["b1"], w2=z["w2"],
                       b2=float(z["b2"]))


def logits(model: ScorerModel, feats: np.ndarray) -> np.ndarray:
    """Unnormalised score per state from (n_states, n_features)."""
    hidden = np.maximum((feats - model.mean) / model.std @ model.w1 + model.b1, 0.0)
    return hidden @ model.w2 + model.b2


def log_emission(model: ScorerModel, feats: np.ndarray) -> np.ndarray:
    """Log-softmax of the logits over states."""
    z = logits(model, feats)
    z = z - z.max()
    return z - np.log(np.exp(z).sum())
