"""Training of the TCN (block B) with the periodicity loss; export to the streaming layout.

The loss needs no beat labels: the biased autocorrelation of the network output over a random
2-4 s crop tail must peak at the reference period (softmax over HR states, reward on states
within positive_tol_rel). Training code only; not ported to the MCU.
"""

import logging
import multiprocessing as mp
import time

import numpy as np
import pandas as pd
import torch
from torch import nn

from pcg_hr import augment, data, features, frontend

log = logging.getLogger(__name__)

_CFG: dict = {}


def _init(cfg: dict) -> None:
    """Store the config in the worker."""
    _CFG.clear()
    _CFG.update(cfg)


def record_example(task: tuple) -> tuple:
    """Band log energies (n_bands, frames) and reference HR of one (possibly augmented) row."""
    key, filename, ref_bpm, spec, beats = task
    cfg = _CFG
    audio = data.load_wav(cfg, filename)
    if spec is not None:
        audio, ref_bpm = augment.apply(cfg, audio, beats, ref_bpm, spec)
    block = cfg["stream"]["block_samples"]
    fe = frontend.init(cfg)
    x = np.hstack([frontend.process(fe, audio[i:i + block])
                   for i in range(0, audio.size - block + 1, block)]).astype(np.float32)
    return key, x, ref_bpm


def build_dataset(cfg: dict, rows: pd.DataFrame) -> dict:
    """Inputs, reference HR and valid length per row of augment.plan (parallel), zero-padded."""
    tasks = list(zip(rows["key"], rows["output_filename"], rows["reference_bpm"], rows["spec"],
                     rows["beats"]))
    xs, names, refs = [], [], []
    start = time.time()
    with mp.get_context("spawn").Pool(cfg["eval"]["workers"], _init, (cfg,)) as pool:
        for i, (key, x, ref) in enumerate(pool.imap(record_example, tasks, 4), 1):
            xs.append(x)
            names.append(key)
            refs.append(ref)
            if i % 500 == 0 or i == len(tasks):
                log.info("net dataset: %d/%d rows, %.0f s", i, len(tasks), time.time() - start)
    lengths = np.array([x.shape[1] for x in xs])
    x_all = np.zeros((len(xs), xs[0].shape[0], lengths.max()), dtype=np.float32)
    for j, x in enumerate(xs):
        x_all[j, :, :x.shape[1]] = x
    return {"x": x_all, "names": np.array(names), "ref": np.array(refs, dtype=np.float32),
            "lengths": lengths}


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


def fit(cfg: dict, dataset: dict) -> nn.Module:
    """Train on random crops with the periodicity loss; return the model."""
    nc = cfg["net"]
    torch.manual_seed(cfg["data"]["seed"])
    rng = np.random.default_rng(cfg["data"]["seed"])
    rate = cfg["frontend"]["env_rate_hz"]
    x = causal_normalise(torch.from_numpy(dataset["x"]), 1.0 / (nc["norm_tau_s"] * rate))
    model = CausalTCN(x.shape[1], nc["channels"], nc["kernel"], nc["dilations"])
    scale = torch.nn.Parameter(torch.tensor(10.0))
    opt = torch.optim.Adam([*model.parameters(), scale], lr=nc["lr"])
    crop = int(nc["crop_s"] * rate)
    win_lo, win_hi = (int(w * rate) for w in nc["periodicity_window_s"])
    grid = torch.from_numpy(features.hr_grid(cfg)).float()
    lags = 60.0 * rate / grid
    ref = torch.from_numpy(dataset["ref"])
    lengths = dataset["lengths"]
    for epoch in range(nc["epochs"]):
        perm = rng.permutation(len(x))
        total = 0.0
        for i in range(0, len(perm), nc["batch"]):
            idx = perm[i:i + nc["batch"]]
            start = rng.integers(0, lengths[idx] - crop)
            xb = torch.stack([x[j, :, s:s + crop] for j, s in zip(idx, start)])
            window = int(rng.integers(win_lo, win_hi + 1))
            act = torch.sigmoid(model(xb)[:, -window:])
            loss = periodicity_loss(periodicity_logits(act, lags), ref[idx], grid,
                                    (lags < window - 1)[None],
                                    cfg["scorer"]["positive_tol_rel"], scale)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total += loss.item() * len(idx)
        log.info("net epoch %d/%d: loss %.4f", epoch + 1, nc["epochs"], total / len(perm))
    return model


def export(model: CausalTCN) -> dict:
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
