"""Block B, causal TCN: beat activation from band energies, one 10 ms frame at a time.

Input per frame: the front end's values (frontend.process), the heart-band log energies minus a
causal running mean per band (time constant norm_tau_s, so recording gain drops out), the other
inputs (level-free) as they are. Output per frame: activation in [0, 1], high at each heartbeat,
0 for other sounds. 5 dilated causal convolutions (kernel 3, dilations 1-16, 8 channels, ReLU,
residual from layer 1) and a sigmoid output; receptive field 63 frames = 0.63 s.
State: one delay line per layer holding dilation x (kernel - 1) past inputs, and the running
mean. float32 throughout.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class NetState:
    """Weights, running input mean and per-layer delay lines."""
    weights: dict
    dilations: list
    kernel: int
    norm_alpha: float
    n_norm: int                 # leading inputs with the running mean removed (heart bands)
    run_mean: np.ndarray | None
    buffers: list


def weights_path(cfg: dict) -> Path:
    """TCN weight file of the configured model: <models_dir>/<weights>_tcn.npz."""
    return cfg["paths"]["models_dir"] / f"{cfg['weights']}_tcn.npz"


def load_weights(path: Path) -> dict:
    """Weights saved by training.train: conv<i>_w (out, in, kernel), conv<i>_b, out_w, out_b."""
    return dict(np.load(path, allow_pickle=False))


def init(cfg: dict, weights: dict | None = None) -> NetState:
    """Empty streaming state; weights of the configured model unless given."""
    nc = cfg["net"]
    w = weights or load_weights(weights_path(cfg))
    dilations = list(nc["dilations"])
    inputs = [w[f"conv{i}_w"].shape[1] for i in range(len(dilations))]
    buffers = [np.zeros((c, d * (nc["kernel"] - 1)), np.float32)
               for c, d in zip(inputs, dilations)]
    return NetState(weights=w, dilations=dilations, kernel=nc["kernel"],
                    norm_alpha=1.0 / (nc["norm_tau_s"] * cfg["frontend"]["env_rate_hz"]),
                    n_norm=cfg["frontend"]["heart_bands"], run_mean=None, buffers=buffers)


def normalise(state: NetState, frame: np.ndarray) -> np.ndarray:
    """Subtract the causal running mean of each heart band (mean starts at the first frame);
    the other inputs pass unchanged."""
    h = state.n_norm
    if state.run_mean is None:
        state.run_mean = frame[:h].astype(np.float32)
    state.run_mean += np.float32(state.norm_alpha) * (frame[:h] - state.run_mean)
    out = frame.astype(np.float32).copy()
    out[:h] = frame[:h] - state.run_mean
    return out


def step(state: NetState, x: np.ndarray) -> float:
    """One frame through the dilated causal convolutions (ReLU, residual from layer 1 on) and
    the sigmoid output."""
    w = state.weights
    for i, (dil, buf) in enumerate(zip(state.dilations, state.buffers)):
        taps = [buf[:, -(state.kernel - 1 - j) * dil] if j < state.kernel - 1 else x
                for j in range(state.kernel)]
        y = np.einsum("ock,ck->o", w[f"conv{i}_w"], np.stack(taps, axis=1)) + w[f"conv{i}_b"]
        y = np.maximum(y, 0.0)
        state.buffers[i] = np.hstack([buf[:, 1:], x[:, None]]) if buf.shape[1] else buf
        x = y + x if i > 0 else y
    return np.float32(1.0) / (np.float32(1.0) + np.exp(-(w["out_w"] @ x + w["out_b"])))


def process(state: NetState, env: np.ndarray) -> np.ndarray:
    """Activation for each frame of env (n_bands, n_frames) -> (1, n_frames)."""
    return np.stack([np.atleast_1d(step(state, normalise(state, env[:, i])))
                     for i in range(env.shape[1])], axis=1)
