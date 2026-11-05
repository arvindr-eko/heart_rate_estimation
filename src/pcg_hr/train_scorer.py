"""Training of the scorer MLP (block D): feature collection over recordings and fitting.

Features come from the estimator's own blocks A-C, so training and streaming share one code
path. Training code only; not ported to the MCU.
"""

import logging
import multiprocessing as mp
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from pcg_hr import augment, data, estimator, features

log = logging.getLogger(__name__)

_CFG: dict = {}


def frame_times(cfg: dict, duration_s: float) -> np.ndarray:
    """Training frame times: every frame up to dense_until_s, then every sparse_step_s."""
    sc = cfg["scorer"]
    frame_s = estimator.frame_seconds(cfg)
    dense = np.arange(frame_s, sc["dense_until_s"] + 1e-9, frame_s)
    sparse = np.arange(sc["dense_until_s"] + sc["sparse_step_s"], duration_s + 1e-9,
                       sc["sparse_step_s"])
    return np.round(np.concatenate([dense, sparse]), 6)


def _init(cfg: dict) -> None:
    """Store the config in the worker."""
    _CFG.clear()
    _CFG.update(cfg)


def record_features(task: tuple) -> tuple:
    """Stream one (possibly augmented) row; scorer inputs at the training frame times."""
    key, filename, ref_bpm, spec, beats = task
    cfg = _CFG
    audio = data.load_wav(cfg, filename)
    if spec is not None:
        audio, ref_bpm = augment.apply(cfg, audio, beats, ref_bpm, spec)
    block = cfg["stream"]["block_samples"]
    state = estimator.init(cfg, with_scorer=False)
    wanted = set(frame_times(cfg, audio.size / cfg["stream"]["fs_hz"]).tolist())
    feats, times = [], []
    for i, start in enumerate(range(0, audio.size - block + 1, block), 1):
        estimator.feed(state, audio[start:start + block])
        t = round(i * block / cfg["stream"]["fs_hz"], 6)
        if t in wanted:
            f = estimator.scorer_input(state)
            if f is not None:
                feats.append(f)
                times.append(t)
    return key, np.stack(feats).astype(np.float16), np.array(times), ref_bpm


def collect(cfg: dict, rows: pd.DataFrame) -> dict:
    """Features (float16), positive-state masks and frame times per row of augment.plan."""
    grid = features.hr_grid(cfg)
    tol = cfg["scorer"]["positive_tol_rel"]
    out = {"feats": [], "pos": [], "times": [], "rec": []}
    start = time.time()
    tasks = list(zip(rows["key"], rows["output_filename"], rows["reference_bpm"], rows["spec"],
                     rows["beats"]))
    with mp.get_context("spawn").Pool(cfg["eval"]["workers"], _init, (cfg,)) as pool:
        for i, (_, feats, times, ref) in enumerate(pool.imap(record_features, tasks, 4), 1):
            pos = np.abs(np.log(grid / ref)) <= np.log1p(tol)
            out["feats"].append(feats)
            out["pos"].append(np.repeat(pos[None], len(feats), axis=0))
            out["times"].append(times)
            out["rec"].append(np.full(len(feats), i - 1))
            if i % 500 == 0 or i == len(tasks):
                log.info("features: %d/%d rows, %.0f s", i, len(tasks), time.time() - start)
    return {k: np.concatenate(v) for k, v in out.items()}


class SharedMLP(torch.nn.Module):
    """Per-state MLP with weights shared across states."""

    def __init__(self, n_features: int, hidden: int):
        """Two linear layers with ReLU."""
        super().__init__()
        self.l1 = torch.nn.Linear(n_features, hidden)
        self.l2 = torch.nn.Linear(hidden, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """(frames, states, features) -> (frames, states) logits."""
        return self.l2(torch.relu(self.l1(x))).squeeze(-1)


def fit(cfg: dict, dataset: dict) -> dict:
    """Train with the multi-positive softmax loss over states; return numpy weights."""
    sc = cfg["scorer"]
    torch.manual_seed(cfg["data"]["seed"])
    x = torch.from_numpy(dataset["feats"])
    flat = dataset["feats"].reshape(-1, dataset["feats"].shape[-1])
    sums = np.zeros((2, flat.shape[1]))
    for i in range(0, flat.shape[0], 1_000_000):
        chunk = flat[i:i + 1_000_000].astype(np.float64)
        sums += [chunk.sum(0), (chunk ** 2).sum(0)]
    mean_np = sums[0] / flat.shape[0]
    std_np = np.sqrt(np.maximum(sums[1] / flat.shape[0] - mean_np ** 2, 1e-12))
    mean = torch.from_numpy(mean_np.astype(np.float32))
    std = torch.from_numpy(std_np.astype(np.float32)).clamp_min(1e-6)
    pos = torch.from_numpy(dataset["pos"])
    model = SharedMLP(x.shape[-1], sc["hidden"])
    opt = torch.optim.Adam(model.parameters(), lr=sc["lr"], weight_decay=sc["weight_decay"])
    n = x.shape[0]
    for epoch in range(sc["epochs"]):
        perm = torch.randperm(n)
        total = 0.0
        for i in range(0, n, sc["batch_frames"]):
            idx = perm[i:i + sc["batch_frames"]]
            logp = torch.log_softmax(model((x[idx].float() - mean) / std), dim=1)
            has_pos = pos[idx].any(dim=1)
            loss = -torch.logsumexp(logp.masked_fill(~pos[idx], -1e9), dim=1)[has_pos].mean()
            opt.zero_grad()
            loss.backward()
            opt.step()
            total += loss.item() * len(idx)
        log.info("scorer epoch %d/%d: loss %.4f", epoch + 1, sc["epochs"], total / n)
    return {"mean": mean.numpy(), "std": std.numpy(),
            "w1": model.l1.weight.detach().numpy().T.copy(),
            "b1": model.l1.bias.detach().numpy(), "w2": model.l2.weight.detach().numpy()[0],
            "b2": np.float32(model.l2.bias.item())}


def save(weights: dict, path: Path) -> None:
    """Write weights as .npz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, **weights)
