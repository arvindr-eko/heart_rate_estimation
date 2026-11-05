"""Streaming beat-activation network (causal TCN or GRU), frame-by-frame numpy inference.

Input per frame: multi-band log energies (n_bands,) at env_rate_hz. Input normalisation:
subtraction of a causal exponential running mean per band. Output per frame: S1 activation in
[0, 1] (head = activation), or a log-softmax over the HR states (head = hr; end-to-end model,
logits = fixed Gaussian basis over log-HR x learned coefficients). State is fixed-size: one
delay line per TCN layer, or the GRU hidden vector.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class NetState:
    """Weights, running input mean, and per-layer delay lines or recurrent state."""
    arch: str
    weights: dict
    dilations: list
    kernel: int
    norm_alpha: float
    run_mean: np.ndarray | None
    buffers: list
    hidden: np.ndarray | None


def load_weights(path: Path) -> dict:
    """Weights saved by net_train.export."""
    return dict(np.load(path, allow_pickle=False))


def init(cfg: dict, weights: dict | None = None) -> NetState:
    """Empty streaming state for the configured architecture."""
    nc = cfg["net"]
    w = weights or load_weights(cfg["paths"]["models_dir"] / nc["weights"])
    alpha = 1.0 / (nc["norm_tau_s"] * cfg["frontend"]["env_rate_hz"])
    if nc["arch"] == "tcn":
        dil = list(nc["tcn_dilations"])
        ins = [w["conv0_w"].shape[1]] + [w[f"conv{i}_w"].shape[1] for i in range(1, len(dil))]
        buffers = [np.zeros((c, d * (nc["tcn_kernel"] - 1))) for c, d in zip(ins, dil)]
        return NetState("tcn", w, dil, nc["tcn_kernel"], alpha, None, buffers, None)
    hidden = np.zeros(w["gru_whh"].shape[1])
    return NetState("gru", w, [], 0, alpha, None, [], hidden)


def normalise(state: NetState, frame: np.ndarray) -> np.ndarray:
    """Subtract the causal running mean of each band."""
    if state.run_mean is None:
        state.run_mean = frame.copy()
    state.run_mean += state.norm_alpha * (frame - state.run_mean)
    return frame - state.run_mean


def tcn_step(state: NetState, x: np.ndarray) -> np.ndarray:
    """One frame through the dilated causal convolutions (residual from layer 1 on)."""
    w = state.weights
    for i, (dil, buf) in enumerate(zip(state.dilations, state.buffers)):
        taps = [buf[:, -(state.kernel - 1 - j) * dil] if j < state.kernel - 1 else x
                for j in range(state.kernel)]
        y = np.einsum("ock,ck->o", w[f"conv{i}_w"], np.stack(taps, axis=1)) + w[f"conv{i}_b"]
        y = np.maximum(y, 0.0)
        state.buffers[i] = np.hstack([buf[:, 1:], x[:, None]]) if buf.shape[1] else buf
        x = y + x if i > 0 else y
    return x


def gru_step(state: NetState, x: np.ndarray) -> np.ndarray:
    """One frame through the GRU cell (PyTorch gate order r, z, n); returns the hidden vector."""
    w = state.weights
    h = state.hidden
    gi = w["gru_wih"] @ x + w["gru_bih"]
    gh = w["gru_whh"] @ h + w["gru_bhh"]
    n_h = h.size
    r = 1 / (1 + np.exp(-(gi[:n_h] + gh[:n_h])))
    z = 1 / (1 + np.exp(-(gi[n_h:2 * n_h] + gh[n_h:2 * n_h])))
    n = np.tanh(gi[2 * n_h:] + r * gh[2 * n_h:])
    state.hidden = (1 - z) * n + z * h
    return state.hidden


def hr_basis(grid_bpm: np.ndarray, n_basis: int) -> np.ndarray:
    """Gaussian bumps evenly spaced in log-HR over the grid, (n_states, n_basis); not stored."""
    log_hr = np.log(grid_bpm)
    centres = np.linspace(log_hr[0], log_hr[-1], n_basis)
    width = centres[1] - centres[0]
    return np.exp(-0.5 * ((log_hr[:, None] - centres[None]) / width) ** 2).astype(np.float32)


def head(state: NetState, feats: np.ndarray, basis: np.ndarray | None) -> np.ndarray:
    """Output layer: sigmoid activation (1,), or log-softmax over HR states."""
    w = state.weights
    if basis is None:
        return 1.0 / (1.0 + np.exp(-(w["out_w"] @ feats + w["out_b"])))[None]
    z = basis @ (w["out_w"] @ feats + w["out_b"])
    z = z - z.max()
    return z - np.log(np.exp(z).sum())


def process(state: NetState, env: np.ndarray, basis: np.ndarray | None = None) -> np.ndarray:
    """Outputs for each frame of env (n_bands, n_frames) -> (n_out, n_frames)."""
    step = tcn_step if state.arch == "tcn" else gru_step
    return np.stack([head(state, step(state, normalise(state, env[:, i])), basis)
                     for i in range(env.shape[1])], axis=1)


def resources(cfg: dict, weights: dict) -> dict:
    """MAC/s, state RAM (16-bit) and weight bytes (8-bit) of the network."""
    rate = cfg["frontend"]["env_rate_hz"]
    n_params = sum(v.size for v in weights.values())
    if cfg["net"]["arch"] == "tcn":
        k = cfg["net"]["tcn_kernel"]
        ram = sum(weights[f"conv{i}_w"].shape[1] * d * (k - 1)
                  for i, d in enumerate(cfg["net"]["tcn_dilations"]))
    else:
        ram = weights["gru_whh"].shape[1]
    return {"macs_per_s": n_params * rate, "ram_bytes": 2 * (ram + 16), "const_bytes": n_params}
