"""Frame-by-frame numpy TCN equals the whole-sequence PyTorch model with the same weights."""

import numpy as np
import torch

from pcg_hr import net, train_net


def test_numpy_tcn_matches_torch(cfg):
    """Max difference below 1e-5 on random band energies."""
    weights = net.load_weights(cfg["paths"]["models_dir"] / cfg["net"]["weights"])
    model = train_net.CausalTCN(4, cfg["net"]["channels"], cfg["net"]["kernel"],
                                cfg["net"]["dilations"])
    with torch.no_grad():
        for i, conv in enumerate(model.convs):
            conv.weight.copy_(torch.from_numpy(weights[f"conv{i}_w"]))
            conv.bias.copy_(torch.from_numpy(weights[f"conv{i}_b"]))
        model.out.weight.copy_(torch.from_numpy(weights["out_w"]).reshape(1, -1, 1))
        model.out.bias.copy_(torch.from_numpy(weights["out_b"]).reshape(1))
    x = np.random.default_rng(0).normal(-10, 2, size=(4, 300)).astype(np.float32)
    streamed = net.process(net.init(cfg, weights), x)[0]
    assert np.abs(streamed - train_net.torch_activation(model, cfg, x)).max() < 1e-5
