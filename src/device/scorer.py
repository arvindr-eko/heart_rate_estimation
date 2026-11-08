"""Block D, scorer: per-state features -> log-likelihood over HR states.

Input per frame: (232 states, 17): 13 activation features, 3 features of the 25-50 Hz band and
their validity flag (estimator.scorer_input). The same MLP (standardise, 17 -> 16 ReLU -> 1;
339 parameters) scores every state from its own row; the log-softmax over states is the
tracker's evidence. HR itself is never an input.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class ScorerModel:
    """Feature standardisation and MLP weights (float32); inputs are the features of block C
    in features.FEATURE_NAMES order."""
    mean: np.ndarray
    std: np.ndarray
    w1: np.ndarray        # (n_features, hidden)
    b1: np.ndarray
    w2: np.ndarray        # (hidden,)
    b2: np.float32


def weights_path(cfg: dict) -> Path:
    """Scorer weight file of the configured model: <models_dir>/<weights>_scorer.npz."""
    return cfg["paths"]["models_dir"] / f"{cfg['weights']}_scorer.npz"


def load(path: Path) -> ScorerModel:
    """Load weights saved by training.train."""
    z = np.load(path)
    return ScorerModel(mean=z["mean"], std=z["std"], w1=z["w1"], b1=z["b1"], w2=z["w2"],
                       b2=np.float32(z["b2"]))


def logits(model: ScorerModel, feats: np.ndarray) -> np.ndarray:
    """Unnormalised score per state from (n_states, n_features)."""
    hidden = np.maximum((feats - model.mean) / model.std @ model.w1 + model.b1, 0.0)
    return hidden @ model.w2 + model.b2


def log_emission(model: ScorerModel, feats: np.ndarray) -> np.ndarray:
    """Log-softmax of the logits over states."""
    z = logits(model, feats)
    z = z - z.max()
    return z - np.log(np.exp(z).sum())
