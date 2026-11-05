"""Training of the beat-activation network: pseudo S1 labels, dataset, models, export.

Pseudo labels come from offline (non-causal) beat tracking on the whole recording with the
reference HR known; the deployed network is causal. Training code only; not ported to the MCU.
"""

import logging
import multiprocessing as mp
import time

import numpy as np
import pandas as pd
import torch
from torch import nn

from hr import augment, frontend, io, net, periodicity

log = logging.getLogger(__name__)

_CFG: dict = {}


def track_beats(env: np.ndarray, period: float, tightness: float) -> np.ndarray:
    """Dynamic-programming beat tracking with a known period (Ellis 2007); frame indices."""
    n = env.size
    score = env.astype(float).copy()
    back = np.full(n, -1)
    lo, hi = int(round(0.8 * period)), int(round(1.25 * period))
    for t in range(lo, n):
        prev = np.arange(max(0, t - hi), t - lo + 1)
        cost = score[prev] - tightness * np.log((t - prev) / period) ** 2
        best = int(np.argmax(cost))
        if cost[best] > 0:
            score[t] += cost[best]
            back[t] = prev[best]
    t = int(np.argmax(score[-hi:])) + n - hi
    beats = [t]
    while back[beats[-1]] >= 0:
        beats.append(back[beats[-1]])
    return np.array(beats[::-1])


def s1_times(env: np.ndarray, period: float, tightness: float, systole: float) -> np.ndarray:
    """Beat frames placed on S1: the tracked sound is S1 when its partner follows after about
    the expected systole (frames), otherwise it is S2 and the partner is S1."""
    beats = track_beats(env, period, tightness)
    partners = []
    for b in beats:
        lo, hi = b + int(0.15 * period), min(env.size, b + int(0.85 * period))
        if hi > lo:
            partners.append(lo + int(np.argmax(env[lo:hi])) - b)
    if partners:
        offset = float(np.median(partners))
        if abs(period - offset - systole) < abs(offset - systole):
            beats = beats + int(offset)
    return beats[(beats >= 0) & (beats < env.size)]


def _init(cfg: dict) -> None:
    """Store the config in the worker."""
    _CFG.clear()
    _CFG.update(cfg)


def record_example(task: tuple) -> tuple:
    """Multi-band input, soft S1 target and reference HR for one (possibly augmented) row."""
    key, filename, ref_bpm, spec, beats = task
    cfg = _CFG
    nc = cfg["net"]
    audio = io.load_wav(cfg, filename)
    if spec is not None:
        audio, ref_bpm = augment.apply(cfg, audio, beats, ref_bpm, spec)
    block = cfg["stream"]["block_samples"]
    fe_net = frontend.init(cfg, bands_hz=nc["bands_hz"])
    fe_lab = frontend.init(cfg)
    inputs, label_env = [], []
    for start in range(0, audio.size - block + 1, block):
        inputs.append(frontend.process(fe_net, audio[start:start + block])[0])
        label_env.append(frontend.process(fe_lab, audio[start:start + block])[0][0])
    x = np.hstack(inputs).astype(np.float32)
    env = np.hstack(label_env)
    env = (env - np.median(env)) / (env.std() + 1e-9)
    rate = cfg["frontend"]["env_rate_hz"]
    a, b = nc["label_systole_s"]
    systole = float(np.clip(a + b * ref_bpm, 0.12, 0.40)) * rate
    beats = s1_times(np.maximum(env, 0.0), 60.0 * rate / ref_bpm, nc["label_tightness"], systole)
    target = np.zeros(env.size, dtype=np.float32)
    frames = np.arange(env.size)
    delay = int(round(nc["label_delay_s"] * rate))
    sigma = nc["label_sigma_s"] * rate
    for b in beats + delay:
        target = np.maximum(target, np.exp(-0.5 * ((frames - b) / sigma) ** 2))
    return key, x, target, ref_bpm


def build_dataset(cfg: dict, rows: pd.DataFrame) -> dict:
    """Inputs, targets, reference HR and valid length per row of augment.plan (parallel);
    rows are zero-padded to the longest."""
    tasks = list(zip(rows["key"], rows["output_filename"], rows["reference_bpm"], rows["spec"],
                     rows["beats"]))
    xs, ys, names, refs = [], [], [], []
    start = time.time()
    with mp.get_context("spawn").Pool(cfg["eval"]["workers"], _init, (cfg,)) as pool:
        for i, (key, x, y, ref) in enumerate(pool.imap(record_example, tasks, 4), 1):
            xs.append(x)
            ys.append(y)
            names.append(key)
            refs.append(ref)
            if i % 500 == 0 or i == len(tasks):
                log.info("net dataset: %d/%d rows, %.0f s", i, len(tasks), time.time() - start)
    lengths = np.array([x.shape[1] for x in xs])
    n_max = lengths.max()
    x_all = np.zeros((len(xs), xs[0].shape[0], n_max), dtype=np.float32)
    y_all = np.zeros((len(xs), n_max), dtype=np.float32)
    for j, (x, y) in enumerate(zip(xs, ys)):
        x_all[j, :, :x.shape[1]] = x
        y_all[j, :y.size] = y
    return {"x": x_all, "y": y_all, "names": np.array(names),
            "ref": np.array(refs, dtype=np.float32), "lengths": lengths}


class CausalTCN(nn.Module):
    """Dilated causal 1-D convolutions, residual from layer 1 on, sigmoid output."""

    def __init__(self, n_in: int, channels: int, kernel: int, dilations: list, n_out: int = 1):
        """Layer 0 maps n_in to channels; later layers keep the width."""
        super().__init__()
        self.kernel, self.dilations = kernel, dilations
        self.convs = nn.ModuleList(
            [nn.Conv1d(n_in if i == 0 else channels, channels, kernel, dilation=d)
             for i, d in enumerate(dilations)])
        self.out = nn.Conv1d(channels, n_out, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """(batch, n_in, frames) -> logits (batch, frames) or (batch, n_out, frames)."""
        for i, (conv, d) in enumerate(zip(self.convs, self.dilations)):
            y = torch.relu(conv(nn.functional.pad(x, (d * (self.kernel - 1), 0))))
            x = y + x if i > 0 else y
        out = self.out(x)
        return out.squeeze(1) if out.shape[1] == 1 else out


class GRUNet(nn.Module):
    """Single-layer GRU with a linear output."""

    def __init__(self, n_in: int, hidden: int, n_out: int = 1):
        """GRU of the given width."""
        super().__init__()
        self.gru = nn.GRU(n_in, hidden, batch_first=True)
        self.out = nn.Linear(hidden, n_out)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """(batch, n_in, frames) -> logits (batch, frames) or (batch, n_out, frames)."""
        h, _ = self.gru(x.transpose(1, 2))
        out = self.out(h).transpose(1, 2)
        return out.squeeze(1) if out.shape[1] == 1 else out


def causal_normalise(x: torch.Tensor, alpha: float) -> torch.Tensor:
    """Running-mean subtraction identical to net.normalise (mean starts at the first frame)."""
    out = torch.empty_like(x)
    mean = x[:, :, 0].clone()
    for t in range(x.shape[2]):
        mean = mean + alpha * (x[:, :, t] - mean)
        out[:, :, t] = x[:, :, t] - mean
    return out


def make_model(cfg: dict, n_in: int) -> nn.Module:
    """Model of the configured architecture and head."""
    nc = cfg["net"]
    n_out = nc["hr_basis"] if nc["head"] == "hr" else 1
    if nc["arch"] == "tcn":
        return CausalTCN(n_in, nc["tcn_channels"], nc["tcn_kernel"], nc["tcn_dilations"], n_out)
    return GRUNet(n_in, nc["gru_hidden"], n_out)


def hr_head_loss(coeffs: torch.Tensor, ref: torch.Tensor, grid: torch.Tensor,
                 basis: torch.Tensor, first: int, tol: float) -> torch.Tensor:
    """Multi-positive softmax loss over HR states for every frame from index first on."""
    logits = torch.einsum("sk,bkt->bst", basis, coeffs[:, :, first:])
    pos = (torch.log(grid[None] / ref[:, None]).abs() <= np.log1p(tol))[:, :, None]
    has_pos = pos[:, :, 0].any(dim=1)
    if not has_pos.any():
        return torch.zeros(())
    logp = torch.log_softmax(logits, dim=1)
    return -torch.logsumexp(logp.masked_fill(~pos, -1e9), dim=1)[has_pos].mean()


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
    """Multi-positive softmax loss over valid states: the ACF must peak at the true period.
    Crops without a valid positive state (reference outside the grid or window) are skipped."""
    z = (scale * logits).masked_fill(~valid, -1e9)
    pos = (torch.log(grid[None] / ref[:, None]).abs() <= np.log1p(tol)) & valid
    has_pos = pos.any(dim=1)
    if not has_pos.any():
        return torch.zeros(())
    logp = torch.log_softmax(z, dim=1)
    return -torch.logsumexp(logp.masked_fill(~pos, -1e9), dim=1)[has_pos].mean()


def fit(cfg: dict, data: dict) -> nn.Module:
    """Train on random crops (weighted BCE on pseudo labels and/or periodicity loss)."""
    nc = cfg["net"]
    torch.manual_seed(cfg["data"]["seed"])
    rng = np.random.default_rng(cfg["data"]["seed"])
    alpha = 1.0 / (nc["norm_tau_s"] * cfg["frontend"]["env_rate_hz"])
    x = causal_normalise(torch.from_numpy(data["x"]), alpha)
    y = torch.from_numpy(data["y"])
    model = make_model(cfg, x.shape[1])
    scale = torch.nn.Parameter(torch.tensor(10.0))
    opt = torch.optim.Adam([*model.parameters(), scale], lr=nc["lr"])
    rate = cfg["frontend"]["env_rate_hz"]
    crop = int(nc["crop_s"] * rate)
    win_lo, win_hi = (int(w * rate) for w in nc["periodicity_window_s"])
    grid = torch.from_numpy(periodicity.hr_grid(cfg)).float()
    basis = torch.from_numpy(net.hr_basis(periodicity.hr_grid(cfg), nc["hr_basis"]))
    lags = 60.0 * rate / grid
    ref = torch.from_numpy(data["ref"])
    lengths = data.get("lengths", np.full(len(x), x.shape[2]))
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=torch.tensor(nc["pos_weight"]))
    for epoch in range(nc["epochs"]):
        perm = rng.permutation(len(x))
        total = 0.0
        for i in range(0, len(perm), nc["batch"]):
            idx = perm[i:i + nc["batch"]]
            start = rng.integers(0, lengths[idx] - crop)
            xb = torch.stack([x[j, :, s:s + crop] for j, s in zip(idx, start)])
            yb = torch.stack([y[j, s:s + crop] for j, s in zip(idx, start)])
            logits = model(xb)
            loss = torch.zeros(())
            if nc["head"] == "hr":
                loss = hr_head_loss(logits, ref[idx], grid, basis, crop - win_hi,
                                    cfg["salience"]["positive_tol_rel"])
            if nc["head"] == "activation" and nc["loss"] in ("bce", "both"):
                loss = loss + loss_fn(logits, yb)
            if nc["head"] == "activation" and nc["loss"] in ("periodicity", "both"):
                window = int(rng.integers(win_lo, win_hi + 1))
                valid = (lags < window - 1)[None]
                act = torch.sigmoid(logits[:, -window:])
                per = periodicity_logits(act, lags)
                loss = loss + periodicity_loss(per, ref[idx], grid, valid,
                                               cfg["salience"]["positive_tol_rel"], scale)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total += loss.item() * len(idx)
        log.info("net epoch %d/%d: loss %.4f", epoch + 1, nc["epochs"], total / len(perm))
    return model


def export(model: nn.Module, cfg: dict) -> dict:
    """Numpy weights in the layout read by net.init."""
    if cfg["net"]["arch"] == "tcn":
        w = {f"conv{i}_w": c.weight.detach().numpy() for i, c in enumerate(model.convs)}
        w.update({f"conv{i}_b": c.bias.detach().numpy() for i, c in enumerate(model.convs)})
        w["out_w"] = model.out.weight.detach().numpy()[:, :, 0]
    else:
        g = model.gru
        w = {"gru_wih": g.weight_ih_l0.detach().numpy(), "gru_whh": g.weight_hh_l0.detach().numpy(),
             "gru_bih": g.bias_ih_l0.detach().numpy(), "gru_bhh": g.bias_hh_l0.detach().numpy(),
             "out_w": model.out.weight.detach().numpy()}
    w["out_b"] = model.out.bias.detach().numpy()
    if w["out_w"].shape[0] == 1:
        w["out_w"], w["out_b"] = w["out_w"][0], w["out_b"][0]
    return {k: v.astype(np.float32) for k, v in w.items()}


def torch_activation(model: nn.Module, cfg: dict, x: np.ndarray) -> np.ndarray:
    """Whole-sequence activation from the torch model, for checking the streaming numpy path."""
    alpha = 1.0 / (cfg["net"]["norm_tau_s"] * cfg["frontend"]["env_rate_hz"])
    with torch.no_grad():
        xn = causal_normalise(torch.from_numpy(x[None]), alpha)
        out = model(xn)[0]
        if out.ndim == 1:
            return torch.sigmoid(out).numpy()[None]
        basis = torch.from_numpy(net.hr_basis(periodicity.hr_grid(cfg), out.shape[0]))
        return torch.log_softmax(basis @ out, dim=0).numpy()
