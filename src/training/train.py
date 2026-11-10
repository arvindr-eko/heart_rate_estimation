"""Training of the TCN (block B) and the scorer (block D); training code, not ported to the MCU.

Both train on the training split plus `augment.copies` augmented copies per recording.
TCN: no beat labels. The biased autocorrelation of the network output over a random 2-4 s crop
tail must peak at the target period (softmax over HR states, reward on states within
positive_tol_rel).
Scorer: per-state features from the estimator's own blocks A-C (one code path for training
and streaming), multi-positive softmax over states.
Targets: the ECG HR over the window the network or scorer sees (labels.window_hr) where the
row has trusted or warped beat times, the row's reference HR otherwise.
"""

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import torch
from sklearn.linear_model import LogisticRegression
from torch import nn

from device import estimator, features, frontend, net, readout
from training import augment, data, labels

log = logging.getLogger(__name__)


def training_rows(cfg: dict) -> pd.DataFrame:
    """Training-split recordings (without unheard beats) plus their augmented copies."""
    df = data.load_split(cfg, "train")
    df = df[~df["unheard"]].reset_index(drop=True)
    rows = augment.plan(cfg, df, labels.build_beats(cfg, df))
    log.info("training rows: %d (%d augmented)", len(rows), rows["spec"].notna().sum())
    return rows


def _tasks(rows: pd.DataFrame) -> list:
    """One (key, filename, reference HR, augmentation spec, beats) tuple per training row."""
    return list(zip(rows["key"], rows["output_filename"], rows["reference_bpm"], rows["spec"],
                    rows["beats"]))


def load_row(cfg: dict, task: tuple) -> tuple[np.ndarray, float, np.ndarray | None]:
    """Audio, reference HR and beat times (None without a trusted ECG) of one training row,
    augmented when it carries a spec."""
    _, filename, ref_bpm, spec, beats = task
    audio = data.load_wav(cfg, filename)
    if spec is not None:
        audio, ref_bpm, beats = augment.apply(cfg, audio, beats, ref_bpm, spec)
    return audio, ref_bpm, beats


# TCN (block B)

def _net_example(cfg: dict, task: tuple) -> tuple:
    """Band log energies (n_bands, frames), reference HR and beat times of one row."""
    audio, ref_bpm, beats = load_row(cfg, task)
    block = cfg["stream"]["block_samples"]
    fe = frontend.init(cfg)
    x = np.hstack([frontend.process(fe, audio[i:i + block])
                   for i in range(0, audio.size - block + 1, block)]).astype(np.float32)
    return task[0], x, ref_bpm, beats


def net_dataset(cfg: dict, rows: pd.DataFrame) -> dict:
    """Zero-padded TCN inputs, reference HR, valid length and NaN-padded beat times per
    training row."""
    _, xs, refs, beats = zip(*data.map_records(cfg, _net_example, _tasks(rows), "TCN inputs"))
    lengths = np.array([x.shape[1] for x in xs])
    x_all = np.zeros((len(xs), xs[0].shape[0], lengths.max()), dtype=np.float32)
    for j, x in enumerate(xs):
        x_all[j, :, :x.shape[1]] = x
    beat_all = np.full((len(xs), max(len(b) for b in beats if b is not None)), np.nan)
    for j, b in enumerate(beats):
        if b is not None:
            beat_all[j, :len(b)] = b
    return {"x": x_all, "ref": np.array(refs, dtype=np.float32), "lengths": lengths,
            "beats": beat_all}


def crop_targets(cfg: dict, dataset: dict, idx: np.ndarray, end_frames: np.ndarray,
                 window: int) -> np.ndarray:
    """Target HR of each crop: ECG HR over the crop's loss window where the row has beat
    times, the row's reference HR otherwise."""
    lc, rate = cfg["labels"], cfg["frontend"]["env_rate_hz"]
    ref = dataset["ref"][idx].astype(np.float64)
    for j, (row, end) in enumerate(zip(idx, end_frames)):
        b = dataset["beats"][row]
        hr = labels.window_hr(b[~np.isnan(b)], end / rate, window / rate, lc["min_intervals"])
        if not np.isnan(hr):
            ref[j] = hr
    return ref


class CausalTCN(nn.Module):
    """Dilated causal 1-D convolutions, ReLU, residual from layer 1 on, one output channel."""

    def __init__(self, n_in: int, channels: int, kernel: int, dilations: list):
        """Layer 0 maps n_in to channels; later layers keep the width."""
        super().__init__()
        self.kernel, self.dilations = kernel, dilations
        self.convs = nn.ModuleList(
            [nn.Conv1d(n_in if i == 0 else channels, channels, kernel, dilation=d)
             for i, d in enumerate(dilations)])
        self.out = nn.Conv1d(channels, 1, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """(batch, n_in, frames) -> logits (batch, frames)."""
        for i, (conv, d) in enumerate(zip(self.convs, self.dilations)):
            y = torch.relu(conv(nn.functional.pad(x, (d * (self.kernel - 1), 0))))
            x = y + x if i > 0 else y
        return self.out(x).squeeze(1)


def causal_normalise(x: torch.Tensor, alpha: float) -> torch.Tensor:
    """Running-mean subtraction identical to net.normalise (mean starts at the first frame)."""
    out = torch.empty_like(x)
    mean = x[:, :, 0].clone()
    for t in range(x.shape[2]):
        mean = mean + alpha * (x[:, :, t] - mean)
        out[:, :, t] = x[:, :, t] - mean
    return out


def periodicity_logits(act: torch.Tensor, lags: torch.Tensor) -> torch.Tensor:
    """Biased normalised ACF of each activation crop, interpolated at the state lags."""
    a = act - act.mean(dim=1, keepdim=True)
    n = a.shape[1]
    nfft = int(2 ** np.ceil(np.log2(2 * n)))
    r = torch.fft.irfft(torch.fft.rfft(a, nfft).abs() ** 2, nfft)[:, :n]
    r = r / r[:, :1].clamp_min(1e-8)
    lo = lags.floor().long().clamp(max=n - 2)
    frac = lags - lo
    return r[:, lo] * (1 - frac) + r[:, lo + 1] * frac


def periodicity_loss(logits: torch.Tensor, ref: torch.Tensor, grid: torch.Tensor,
                     valid: torch.Tensor, tol: float, scale: torch.Tensor) -> torch.Tensor:
    """Multi-positive softmax loss over valid states; crops without a valid positive state
    (reference outside the grid or window) are skipped."""
    z = (scale * logits).masked_fill(~valid, -1e9)
    pos = (torch.log(grid[None] / ref[:, None]).abs() <= np.log1p(tol)) & valid
    has_pos = pos.any(dim=1)
    if not has_pos.any():
        return torch.zeros(())
    logp = torch.log_softmax(z, dim=1)
    return -torch.logsumexp(logp.masked_fill(~pos, -1e9), dim=1)[has_pos].mean()


def fit_net(cfg: dict, dataset: dict) -> nn.Module:
    """Train on random crops with the periodicity loss; return the model."""
    nc, tr = cfg["net"], cfg["training"]["net"]
    torch.manual_seed(cfg["data"]["seed"])
    rng = np.random.default_rng(cfg["data"]["seed"])
    rate = cfg["frontend"]["env_rate_hz"]
    x = causal_normalise(torch.from_numpy(dataset["x"]), 1.0 / (nc["norm_tau_s"] * rate))
    model = CausalTCN(x.shape[1], nc["channels"], nc["kernel"], nc["dilations"])
    scale = torch.nn.Parameter(torch.tensor(10.0))
    opt = torch.optim.Adam([*model.parameters(), scale], lr=tr["lr"])
    crop = int(tr["crop_s"] * rate)
    win_lo, win_hi = (int(w * rate) for w in tr["periodicity_window_s"])
    grid = torch.from_numpy(features.hr_grid(cfg)).float()
    lags = 60.0 * rate / grid
    lengths = dataset["lengths"]
    for epoch in range(tr["epochs"]):
        perm = rng.permutation(len(x))
        total = 0.0
        for i in range(0, len(perm), tr["batch"]):
            idx = perm[i:i + tr["batch"]]
            start = rng.integers(0, lengths[idx] - crop)
            xb = torch.stack([x[j, :, s:s + crop] for j, s in zip(idx, start)])
            window = int(rng.integers(win_lo, win_hi + 1))
            act = torch.sigmoid(model(xb)[:, -window:])
            target = torch.from_numpy(crop_targets(cfg, dataset, idx, start + crop, window)
                                      .astype(np.float32))
            loss = periodicity_loss(periodicity_logits(act, lags), target, grid,
                                    (lags < window - 1)[None],
                                    cfg["training"]["scorer"]["positive_tol_rel"], scale)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total += loss.item() * len(idx)
        log.info("TCN epoch %d/%d: loss %.4f", epoch + 1, tr["epochs"], total / len(perm))
    return model


def export_net(model: CausalTCN) -> dict:
    """Numpy float32 weights in the layout read by net.init."""
    w = {f"conv{i}_w": c.weight.detach().numpy() for i, c in enumerate(model.convs)}
    w.update({f"conv{i}_b": c.bias.detach().numpy() for i, c in enumerate(model.convs)})
    w["out_w"] = model.out.weight.detach().numpy()[0, :, 0]
    w["out_b"] = np.array(model.out.bias.item())
    return {k: v.astype(np.float32) for k, v in w.items()}


def torch_activation(model: CausalTCN, cfg: dict, x: np.ndarray) -> np.ndarray:
    """Whole-sequence activation from the torch model, to check the streaming numpy path."""
    alpha = 1.0 / (cfg["net"]["norm_tau_s"] * cfg["frontend"]["env_rate_hz"])
    with torch.no_grad():
        return torch.sigmoid(model(causal_normalise(torch.from_numpy(x[None]), alpha)))[0].numpy()


# Scorer (block D)

def frame_times(cfg: dict, duration_s: float) -> np.ndarray:
    """Training frame times: every frame up to dense_until_s, then every sparse_step_s."""
    sc = cfg["training"]["scorer"]
    frame_s = estimator.frame_seconds(cfg)
    dense = np.arange(frame_s, sc["dense_until_s"] + 1e-9, frame_s)
    sparse = np.arange(sc["dense_until_s"] + sc["sparse_step_s"], duration_s + 1e-9,
                       sc["sparse_step_s"])
    return np.round(np.concatenate([dense, sparse]), 6)


def _scorer_example(cfg: dict, task: tuple) -> tuple:
    """Scorer inputs (float16) at the training frame times of one row and the target HR of
    each frame: the ECG HR over the frame's window where the row has beat times, the row's
    reference HR otherwise."""
    audio, ref_bpm, beats = load_row(cfg, task)
    lc = cfg["labels"]
    block = cfg["stream"]["block_samples"]
    state = estimator.init(cfg, with_scorer=False)
    wanted = set(frame_times(cfg, audio.size / cfg["stream"]["fs_hz"]).tolist())
    feats, targets = [], []
    for i, start in enumerate(range(0, audio.size - block + 1, block), 1):
        estimator.feed(state, audio[start:start + block])
        t = round(i * block / cfg["stream"]["fs_hz"], 6)
        if t in wanted:
            f = estimator.scorer_input(state)
            if f is not None:
                feats.append(f)
                window = (np.nan if beats is None
                          else labels.window_hr(beats, t, lc["window_s"], lc["min_intervals"]))
                targets.append(ref_bpm if np.isnan(window) else window)
    return np.stack(feats).astype(np.float16), np.array(targets)


def scorer_dataset(cfg: dict, rows: pd.DataFrame) -> dict:
    """Scorer inputs (frames, states, features) and the target HR of each frame."""
    results = data.map_records(cfg, _scorer_example, _tasks(rows), "scorer inputs")
    return {"feats": np.concatenate([f for f, _ in results]),
            "ref": np.concatenate([t for _, t in results])}


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


def fit_scorer(cfg: dict, dataset: dict) -> dict:
    """Train with the multi-positive softmax loss over states (positives: states within
    positive_tol_rel of the frame's target HR); return numpy weights."""
    sc = {**cfg["training"]["scorer"], "hidden": cfg["scorer"]["hidden"]}
    torch.manual_seed(cfg["data"]["seed"])
    grid = features.hr_grid(cfg)
    feats, target = dataset["feats"], dataset["ref"]
    x = torch.from_numpy(feats)
    flat = feats.reshape(-1, feats.shape[-1])
    sums = np.zeros((2, flat.shape[1]))
    for i in range(0, flat.shape[0], 1_000_000):
        chunk = flat[i:i + 1_000_000].astype(np.float64)
        sums += [chunk.sum(0), (chunk ** 2).sum(0)]
    mean_np = sums[0] / flat.shape[0]
    std_np = np.sqrt(np.maximum(sums[1] / flat.shape[0] - mean_np ** 2, 1e-12))
    mean = torch.from_numpy(mean_np.astype(np.float32))
    std = torch.from_numpy(std_np.astype(np.float32)).clamp_min(1e-6)
    pos = torch.from_numpy(np.abs(np.log(grid[None] / target[:, None]))
                           <= np.log1p(sc["positive_tol_rel"]))
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


# Orchestration

def _cache_path(cfg: dict, stem: str, settings: dict, files: list) -> Path:
    """Cache file named by a hash of the settings and file contents it depends on, so changed
    settings, labels or weights never reuse a stale cache."""
    h = hashlib.sha1(json.dumps(settings, sort_keys=True, default=str).encode())
    for f in files:
        if f.is_file():
            h.update(f.read_bytes())
    return cfg["paths"]["cache_dir"] / f"{stem}_{h.hexdigest()[:8]}.npz"


def _cached(path: Path, build: Callable[[], dict]) -> dict:
    """Arrays from the cache file, built and saved on the first call."""
    if path.is_file():
        return dict(np.load(path))
    dataset = build()
    np.savez(path, **dataset)
    return dataset


def run(cfg: dict, out_name: str, scorer_only: bool = False) -> None:
    """Train the TCN (unless scorer_only: the configured TCN is copied) and the scorer, then fit
    the readout calibration; write <models_dir>/<out_name>_tcn.npz, _scorer.npz and
    _calibration.npz."""
    models = cfg["paths"]["models_dir"]
    rows = training_rows(cfg)
    inputs = {k: cfg[k] for k in ("augment", "data", "split", "stream", "frontend")}
    if scorer_only:
        shutil.copyfile(net.weights_path(cfg), models / f"{out_name}_tcn.npz")
    else:
        dataset = _cached(_cache_path(cfg, "tcn_inputs", inputs, [cfg["paths"]["labels"]]),
                          lambda: net_dataset(cfg, rows))
        model = fit_net(cfg, dataset)
        weights = export_net(model)
        np.savez(models / f"{out_name}_tcn.npz", **weights)
        x = dataset["x"][0][:, :400]
        diff = np.abs(net.process(net.init(cfg, weights), x)[0] - torch_activation(model, cfg, x))
        log.info("saved %s_tcn.npz: %d weights; streaming vs torch max difference %.1e",
                 out_name, sum(v.size for v in weights.values()), float(diff.max()))
    cfg = {**cfg, "weights": out_name}
    np.savez(models / f"{out_name}_scorer.npz", **fit_scorer(cfg, scorer_inputs(cfg, rows)))
    log.info("saved %s_scorer.npz", out_name)
    calibrate(cfg)


def scorer_inputs(cfg: dict, rows: pd.DataFrame) -> dict:
    """Scorer training inputs for the configured TCN, from the cache when its settings, labels
    and TCN weights are unchanged."""
    settings = {**{k: cfg[k] for k in ("augment", "data", "split", "stream", "frontend", "net",
                                       "features", "grid", "labels")},
                "frames": {k: cfg["training"]["scorer"][k]
                           for k in ("dense_until_s", "sparse_step_s")}}
    return _cached(_cache_path(cfg, "scorer_inputs", settings,
                               [cfg["paths"]["labels"], net.weights_path(cfg)]),
                   lambda: scorer_dataset(cfg, rows))


# Confidence calibration (validation split)

def _frame_rows(cfg: dict, task: tuple) -> np.ndarray:
    """Per frame from min_s on, with heartbeat periodicity: path (1 = regular), fine and rough
    posterior masses, periodicity, and the absolute error against the ECG over the same
    seconds (frame i ends at input time (i + 1) x frame length; the trace's t restarts with
    each measurement)."""
    filename, beats = task
    lc, dc = cfg["labels"], cfg["decision"]
    _, trace = estimator.run(cfg, data.load_wav(cfg, filename), keep_trace=True)
    frame_s = estimator.frame_seconds(cfg)
    rows = []
    for i, f in enumerate(trace):
        if f["t"] + 1e-9 < dc["min_s"] or f["periodicity"] < dc["min_periodicity"]:
            continue
        ref = labels.window_hr(beats, (i + 1) * frame_s, lc["window_s"], lc["min_intervals"])
        rows.append((f["path"] == "regular", f["fine_mass"], f["rough_mass"], f["periodicity"],
                     abs(f["hr"] - ref)))
    return np.array(rows, dtype=float).reshape(-1, 5)


def calibrate(cfg: dict) -> None:
    """Fit the readout calibration on the validation split (trusted ECG, beats heard): logistic
    probability of being within ±fine_bpm on regular-path frames (from the mass within
    ±conf_fine_bpm and the periodicity) and within ±rough_bpm on the other frames (mass within
    ±conf_rough_bpm); written to <models_dir>/<weights>_calibration.npz."""
    df = data.load_split(cfg, "val")
    df = df[df["ecg_trusted"] & ~df["unheard"]].reset_index(drop=True)
    beats = labels.build_beats(cfg, df)
    tasks = [(f, beats[f]["beats"]) for f in df["output_filename"]]
    rows = np.vstack(data.map_records(cfg, _frame_rows, tasks, "calibration"))
    rows = rows[np.isfinite(rows[:, 4])]
    ev = cfg["eval"]
    weights = {}
    for name, regular, mass_col, tol in (("fine", 1.0, 1, ev["fine_bpm"]),
                                         ("rough", 0.0, 2, ev["rough_bpm"])):
        sel = rows[rows[:, 0] == regular]
        fit = LogisticRegression(C=100.0).fit(sel[:, [mass_col, 3]], sel[:, 4] <= tol)
        weights[name] = np.r_[fit.intercept_, fit.coef_[0]].astype(np.float32)
    np.savez(readout.calibration_path(cfg), **weights)
    log.info("calibration: %d frames; fine %s, rough %s", len(rows),
             np.round(weights["fine"], 2).tolist(), np.round(weights["rough"], 2).tolist())
