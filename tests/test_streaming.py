"""Streaming blocks equal their whole-sequence references: the frame-by-frame TCN and the
per-sample lag sums of the feature block."""

import numpy as np
import torch

from device import features, net
from training import train


def test_numpy_tcn_matches_torch(cfg):
    """Max difference below 1e-5 on random band energies."""
    weights = net.load_weights(net.weights_path(cfg))
    model = train.CausalTCN(4, cfg["net"]["channels"], cfg["net"]["kernel"],
                                cfg["net"]["dilations"])
    with torch.no_grad():
        for i, conv in enumerate(model.convs):
            conv.weight.copy_(torch.from_numpy(weights[f"conv{i}_w"]))
            conv.bias.copy_(torch.from_numpy(weights[f"conv{i}_b"]))
        model.out.weight.copy_(torch.from_numpy(weights["out_w"]).reshape(1, -1, 1))
        model.out.bias.copy_(torch.from_numpy(weights["out_b"]).reshape(1))
    x = np.random.default_rng(0).normal(-10, 2, size=(4, 300)).astype(np.float32)
    streamed = net.process(net.init(cfg, weights), x)[0]
    assert np.abs(streamed - train.torch_activation(model, cfg, x)).max() < 1e-5


def batch_lag_functions(window: np.ndarray) -> tuple:
    """Reference ACF, NCCF and YIN of a window: FFT correlation of the mean-removed window,
    float64 (independent of the per-sample lag sums)."""
    x = window.astype(np.float64) - window.mean()
    n = x.size
    nfft = int(2 ** np.ceil(np.log2(2 * n)))
    raw = np.fft.irfft(np.abs(np.fft.rfft(x, nfft)) ** 2, nfft)[:n]
    energy = np.cumsum(x ** 2)
    lags = np.arange(n)
    e_head = energy[n - 1 - lags]
    e_tail = energy[-1] - np.concatenate([[0.0], energy[:-1]])
    diff = (e_head + e_tail - 2 * raw) / (n - lags)
    cmnd = np.ones(n)
    cmnd[1:] = diff[1:] * lags[1:] / np.cumsum(diff[1:])
    return raw / raw[0], raw / np.sqrt(e_head * e_tail), 1.0 - cmnd


def test_lag_sums_match_batch(cfg):
    """Per-sample lag sums (float32) give the batch lag functions within 1e-4 while the window
    fills and after 60 s of sliding."""
    rng = np.random.default_rng(1)
    t = np.arange(6000) / 100.0
    signal = (np.sin(2 * np.pi * 1.3 * t) ** 8 + 0.2 * rng.standard_normal(t.size)).astype(
        np.float32)
    state = features.init(cfg)
    usable = state.hist.size - int(cfg["features"]["min_overlap_s"] * 100)
    for i in range(0, t.size, 25):
        features.push(state, signal[i:i + 25])
        if state.count in (150, 400, 6000):
            got = features.lag_functions(state)
            want = batch_lag_functions(features.window(state))
            for g, w in zip(got, want):
                lim = min(g.size, usable)
                assert np.abs(g[:lim] - w[:lim]).max() < 1e-4
