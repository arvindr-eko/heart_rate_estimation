"""PSoC 6 cost model of the final algorithm: operations, Cortex-M4F cycles, RAM and flash.

Operation counts per module follow the planned MCU implementation at the worst case (full
features window). Each count is per second of input; cycles = sum over operation types of count
x profile.cycles x profile.overhead. RAM lists every persistent and scratch buffer; flash lists
weights, coefficients and the code-size estimate. number_format selects float32 or fixed
(16-bit data, 32-bit accumulators, 8-bit weights).

Planned feature memory layout (per input, inputs processed one after the other): history,
one autocorrelation array up to the largest lag, and the per-state DFT magnitude at f (needed for
the per-frame normalisation); overlap energies are running sums and the other features are
computed per state on the fly, so the 24 x 205 feature matrix is never stored.
"""

import numpy as np
import pandas as pd

from pcg_hr import features, net


def _sizes(cfg: dict) -> dict:
    """Dimensions used by the counts (from config and the weight files)."""
    rate = cfg["frontend"]["env_rate_hz"]
    w = net.load_weights(cfg["paths"]["models_dir"] / cfg["net"]["weights"])
    sc = np.load(cfg["paths"]["models_dir"] / cfg["scorer"]["weights"])
    n_window = int(round(cfg["features"]["window_s"] * rate))
    n_lags = min(n_window - 1, int(np.ceil(2 * 60.0 * rate / cfg["grid"]["hr_min_bpm"])))
    st = cfg["stream"]
    frame_s = st["block_samples"] * st["frame_blocks"] / st["fs_hz"]
    return {"fs": st["fs_hz"], "rate": rate, "bands": len(cfg["frontend"]["bands_hz"]),
            "sections": cfg["frontend"]["filter_order"], "n": n_window, "lags": n_lags,
            "states": features.hr_grid(cfg).size, "frames_per_s": 1.0 / frame_s,
            "tcn": w, "dilations": cfg["net"]["dilations"], "scorer_in": sc["w1"].shape[0],
            "scorer_hidden": sc["w1"].shape[1], "inputs": 2,
            "blocks_per_s": st["fs_hz"] / st["block_samples"]}


def _frontend(s: dict, b: dict) -> dict:
    """Block A: per sample per band, direct-form II transposed biquads and energy."""
    return {"ops": {"mac": s["fs"] * s["bands"] * (5 * s["sections"] + 1),
                    "log": s["rate"] * s["bands"], "mac_misc": s["rate"] * s["bands"]},
            "ram": {"biquad states": s["bands"] * s["sections"] * 2 * b["acc"],
                    "energy accumulators": s["bands"] * b["acc"]},
            "flash": {"filter coefficients": s["bands"] * s["sections"] * 5 * b["coef"]}}


def _net(s: dict, b: dict) -> dict:
    """Block B: per envelope frame, every layer's MACs, bias, ReLU, residual, sigmoid."""
    w = s["tcn"]
    convs = [w[k] for k in sorted(w) if k.endswith("_w") and k.startswith("conv")]
    macs = sum(c.size for c in convs) + w["out_w"].size
    channels = sum(c.shape[0] for c in convs)
    delay = sum(c.shape[1] * d * (c.shape[2] - 1) for c, d in zip(convs, s["dilations"]))
    n_weights = sum(v.size for v in w.values())
    return {"ops": {"mac": s["rate"] * macs, "add": s["rate"] * 3 * channels,
                    "mac_misc": s["rate"] * 2 * s["bands"], "exp": s["rate"], "div": s["rate"]},
            "ram": {"delay lines": delay * b["data"], "layer outputs": 2 * convs[0].shape[0] * 4,
                    "running mean": s["bands"] * 4},
            "flash": {"weights": n_weights * b["weight"]}}


def _features(s: dict, b: dict) -> dict:
    """Block C, per frame per input: mean removal, direct ACF up to the largest lag, NCCF and
    YIN per lag, interpolation at 5 lag sets, Goertzel DFT at 3 multiples per state, peak and
    relative-maximum flags, log(window / T)."""
    n, lags, states, f = s["n"], s["lags"], s["states"], s["frames_per_s"] * s["inputs"]
    acf_macs = sum(n - k for k in range(lags + 1))
    return {"ops": {"mac": f * (acf_macs + 3 * states * n + n + 3 * 3 * states + 5 * 2 * states),
                    "add": f * (2 * n + lags * 6 + 3 * states * n + 6 * states),
                    "div": f * (lags * 3 + 1), "sqrt": f * (lags + 3 * states),
                    "log": f * states, "exp": 0},
            "ram": {"histories": s["inputs"] * n * b["data"],
                    "ACF arrays": s["inputs"] * (lags + 1) * b["acc"],
                    "DFT magnitude at f": s["inputs"] * states * 4},
            "flash": {}}


def _scorer(s: dict, b: dict) -> dict:
    """Block D, per frame: standardise and MLP every state, log-softmax, frame confidence."""
    per_state = s["scorer_in"] * (s["scorer_hidden"] + 1) + 2 * s["scorer_hidden"]
    return {"ops": {"mac": s["frames_per_s"] * s["states"] * per_state,
                    "add": s["frames_per_s"] * s["states"] * (s["scorer_hidden"] + 2),
                    "exp": s["frames_per_s"] * s["states"], "log": s["frames_per_s"]},
            "ram": {"feature row": s["scorer_in"] * 4, "hidden": s["scorer_hidden"] * 4,
                    "log-likelihoods": s["states"] * 4},
            "flash": {"weights": (s["scorer_in"] * (s["scorer_hidden"] + 2)
                                  + 2 * s["scorer_hidden"] + 1) * b["weight"]}}


def _tracker(s: dict, b: dict) -> dict:
    """Block E, per frame: random-walk convolution, octave mix, normalisation, emission and
    estimate."""
    kernel = 15
    f, states = s["frames_per_s"], s["states"]
    return {"ops": {"mac": f * states * (kernel + 4), "add": f * states * 4,
                    "exp": f * (states + 1), "div": f * 2, "log": f * 11},
            "ram": {"posterior and prior": 2 * states * 4, "kernel": kernel * 4},
            "flash": {}}


def _release(s: dict, b: dict) -> dict:
    """Block F, per frame: posterior mass below low_hr_bpm, agreement test, threshold."""
    return {"ops": {"add": s["frames_per_s"] * (s["states"] // 4 + 10),
                    "log": s["frames_per_s"]},
            "ram": {"timers and status": 16}, "flash": {}}


def profile(cfg: dict) -> pd.DataFrame:
    """Per-module operations per second, average and peak-block cycles, RAM and flash bytes."""
    p = cfg["profile"]
    fixed = p["number_format"] == "fixed"
    b = {"data": 2 if fixed else 4, "acc": 4, "coef": 4, "weight": p["weight_bytes"]}
    c = p["cycles"]
    mac_cycles = c["mac_fixed"] if fixed else c["mac_float"]
    unit = {"mac": mac_cycles, "mac_misc": mac_cycles, "add": c["add"], "div": c["div"],
            "sqrt": c["sqrt"], "exp": c["exp"], "log": c["log"]}
    s = _sizes(cfg)
    rows = []
    for name, fn, per_frame in [("A front end", _frontend, False), ("B TCN", _net, False),
                                ("C features", _features, True), ("D scorer", _scorer, True),
                                ("E tracker", _tracker, True), ("F release", _release, True)]:
        m = fn(s, b)
        cycles = p["overhead"] * sum(unit[k] * v for k, v in m["ops"].items())
        # frame work runs once per frame_blocks; streaming work spreads over every block
        peak = cycles / s["frames_per_s"] if per_frame else cycles / s["blocks_per_s"]
        rows.append({"module": name, "macs_per_s": m["ops"].get("mac", 0),
                     "cycles_per_s": cycles, "peak_block_cycles": peak,
                     "ram_bytes": sum(m["ram"].values()), "flash_bytes": sum(m["flash"].values()),
                     "ram_detail": m["ram"], "per_frame": per_frame})
    return pd.DataFrame(rows)


def totals(table: pd.DataFrame, cfg: dict) -> dict:
    """Average MIPS-equivalent (cycles / s / 1e6), worst block time and share of the block
    period, RAM and flash (constants plus the code estimate) against the budget."""
    p = cfg["profile"]
    streaming = table.loc[~table["per_frame"], "peak_block_cycles"].sum()
    frame = table.loc[table["per_frame"], "peak_block_cycles"].sum()
    block_s = cfg["stream"]["block_samples"] / cfg["stream"]["fs_hz"]
    peak_ms = 1e3 * (streaming + frame) / p["clock_hz"]
    flash = table["flash_bytes"].sum() + p["code_bytes_estimate"]
    return {"mips": table["cycles_per_s"].sum() / 1e6, "peak_block_ms": peak_ms,
            "peak_block_share": peak_ms / (1e3 * block_s),
            "ram_bytes": int(table["ram_bytes"].sum()), "flash_bytes": int(flash),
            "number_format": p["number_format"],
            "within_budget": bool(table["cycles_per_s"].sum() / 1e6 <= cfg["budget"]["mips"]
                                  and table["ram_bytes"].sum() <= cfg["budget"]["ram_bytes"]
                                  and flash <= cfg["budget"]["flash_bytes"])}
