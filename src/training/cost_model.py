"""PSoC 6 cost model of the final algorithm: operations, Cortex-M4F cycles, RAM and flash.

Operation counts per module follow the planned MCU implementation at the worst case (full
features window). Each count is per second of input; cycles = sum over operation types of count
x profile.cycles x profile.overhead. RAM lists every persistent and scratch buffer; flash lists
weights, coefficients and the code-size estimate. number_format selects float32 or fixed
(16-bit data, 32-bit accumulators, 8-bit weights).

Feature block layout on the device: ring buffer and lag sums persistent; per frame, one pass
over the lags fills three lag-function arrays (ACF, NCCF, YIN; scratch) with the head and tail
window sums accumulated in that pass (device/features.py uses prefix-sum arrays for the same
sums); state lags are computed on the fly from the grid; the 232 x 17 feature matrix is produced
state by state and never stored.
"""

import numpy as np
import pandas as pd

from device import features, net, readout, scorer, tracker

BUDGET = {"mips": 30, "ram_bytes": 15360, "flash_bytes": 5120}  # PSoC 6 CM4 share for the algorithm
CLOCK_HZ = 150_000_000                    # PSoC 6 CM4 maximum clock
CYCLES = {"mac_float": 3,                 # Cortex-M4F cycles per operation: VFMA.F32
          "mac_fixed": 0.5,               # SMLAD, two 16-bit MACs per cycle
          "add": 1, "div": 14, "sqrt": 14,  # VDIV.F32, VSQRT.F32
          "exp": 40, "log": 40}           # polynomial approximations (assumption)
OVERHEAD = 1.3                            # loop control, addressing, flash wait states
CODE_BYTES_ESTIMATE = 3000                # program code, until the C build exists
WEIGHT_BYTES = 1                          # 8-bit weights in flash


def _sizes(cfg: dict) -> dict:
    """Dimensions used by the counts (from config and the weight files)."""
    rate = cfg["frontend"]["env_rate_hz"]
    w = net.load_weights(net.weights_path(cfg))
    sc = np.load(scorer.weights_path(cfg))
    n_window = int(round(cfg["features"]["window_s"] * rate))
    rate_s1 = rate / cfg["features"]["s1_decimation"]
    st = cfg["stream"]
    frame_s = st["block_samples"] * st["frame_blocks"] / st["fs_hz"]
    cal = readout.load_calibration(cfg)
    return {"fs": st["fs_hz"], "rate": rate, "bands": len(cfg["frontend"]["bands_hz"]),
            "sections": cfg["frontend"]["filter_order"], "n": n_window,
            "states": features.hr_grid(cfg).size, "frames_per_s": 1.0 / frame_s,
            "tcn": w, "dilations": cfg["net"]["dilations"], "scorer_in": sc["w1"].shape[0],
            "scorer_hidden": sc["w1"].shape[1],
            "n_features": len(features.FEATURE_NAMES), "rate_s1": rate_s1,
            "n_s1": int(round(cfg["features"]["s1_window_s"] * rate_s1)),
            "blocks_per_s": st["fs_hz"] / st["block_samples"],
            "kernel": tracker.init(cfg, frame_s).kernel.size,
            "calibration": cal.fine.size + cal.rough.size}


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


def _features_sample(s: dict, b: dict) -> dict:
    """Block C per envelope sample: remove the oldest sample's lag pairs and add the new
    sample's (2 MAC per lag), ring-buffer write."""
    return {"ops": {"mac": s["rate"] * 2 * s["n"], "add": s["rate"] * 2},
            "ram": {"ring buffer": s["n"] * b["data"], "lag sums": s["n"] * b["acc"]},
            "flash": {}}


def _features_frame(s: dict, b: dict) -> dict:
    """Block C per frame: one pass over the lags with running head and tail sums (4 adds),
    mean-removed correlation and overlap energies (9 MAC), ACF/NCCF/YIN (2 divisions, 1 square
    root, YIN cumulative sum); peak search and parabolic refinement; then per state: its lag
    (1 exp), 7 interpolated lag-function reads (2 MAC each), peak-offset lookup (binary search,
    1 division), flags."""
    n, states, f = s["n"], s["states"], s["frames_per_s"]
    return {"ops": {"mac": f * (n * 9 + states * 7 * 2), "add": f * (n * 8 + states * 12),
                    "div": f * (n * 2 + states), "sqrt": f * n, "exp": f * states, "log": f},
            "ram": {"ACF, NCCF, YIN (scratch)": 3 * n * 4, "peak positions": 64 * 4},
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
    f, states, kernel = s["frames_per_s"], s["states"], s["kernel"]
    return {"ops": {"mac": f * states * (kernel + 4), "add": f * states * 4,
                    "exp": f * (states + 1), "div": f * 2, "log": f * 11},
            "ram": {"posterior and prior": 2 * states * 4, "kernel": kernel * 4},
            "flash": {}}


def _rhythm(s: dict, b: dict) -> dict:
    """Block R, per frame: local maxima of the activation window (2 compares per sample), greedy
    selection of up to 64 maxima with refractory checks, interval CV and beat-count HR."""
    f, n = s["frames_per_s"], s["n"]
    return {"ops": {"add": f * (3 * n + 64 * 16 + 20), "mac": f * 20, "div": f * 3,
                    "sqrt": f},
            "ram": {"maxima and beats (scratch)": 2 * 64 * 4}, "flash": {}}


def _readout(s: dict, b: dict) -> dict:
    """Block F, per frame: activation SD over the window (sum and sum of squares), posterior
    mass within ±conf_fine_bpm and ±conf_rough_bpm of the estimate (compare and add per state),
    two logistic probabilities, agreement test, rules."""
    f, n = s["frames_per_s"], s["n"]
    return {"ops": {"mac": f * (n + 4), "add": f * (n + 4 * s["states"] + 30),
                    "exp": f * 2, "div": f * 2, "sqrt": f},
            "ram": {"times and status": 16},
            "flash": {"calibration coefficients": s["calibration"] * 4}}


def _s1_sample(s: dict, b: dict) -> dict:
    """Block C, S1 band, per envelope sample: decimation average and lag-sum update at the
    band's rate."""
    m = _features_sample({**s, "rate": s["rate_s1"], "n": s["n_s1"]}, b)
    m["ops"]["add"] += s["rate"]
    return m


def _s1_frame(s: dict, b: dict) -> dict:
    """Block C, S1 band, per frame: lag functions over the band's window and its features per
    state; scratch shared with the activation features (computed one after the other)."""
    m = _features_frame({**s, "n": s["n_s1"]}, b)
    m["ram"] = {}
    return m


def profile(cfg: dict, number_format: str = "float32") -> pd.DataFrame:
    """Per-module operations per second, average and peak-block cycles, RAM and flash bytes, in
    float32 or fixed point (16-bit data, 32-bit accumulators)."""
    fixed = number_format == "fixed"
    b = {"data": 2 if fixed else 4, "acc": 4, "coef": 4, "weight": WEIGHT_BYTES}
    mac_cycles = CYCLES["mac_fixed"] if fixed else CYCLES["mac_float"]
    unit = {"mac": mac_cycles, "mac_misc": mac_cycles, **{k: CYCLES[k] for k in (
        "add", "div", "sqrt", "exp", "log")}}
    s = _sizes(cfg)
    rows = []
    for name, fn, per_frame in [("A front end", _frontend, False), ("B TCN", _net, False),
                                ("C features, per sample", _features_sample, False),
                                ("C features, per frame", _features_frame, True),
                                ("C S1 band, per sample", _s1_sample, False),
                                ("C S1 band, per frame", _s1_frame, True),
                                ("D scorer", _scorer, True),
                                ("E tracker", _tracker, True), ("R rhythm", _rhythm, True),
                                ("F readout", _readout, True)]:
        m = fn(s, b)
        cycles = OVERHEAD * sum(unit[k] * v for k, v in m["ops"].items())
        # frame work runs once per frame_blocks; streaming work spreads over every block
        peak = cycles / s["frames_per_s"] if per_frame else cycles / s["blocks_per_s"]
        rows.append({"module": name, "macs_per_s": m["ops"].get("mac", 0),
                     "cycles_per_s": cycles, "peak_block_cycles": peak,
                     "ram_bytes": sum(m["ram"].values()), "flash_bytes": sum(m["flash"].values()),
                     "ram_detail": m["ram"], "per_frame": per_frame})
    table = pd.DataFrame(rows)
    table.attrs["number_format"] = number_format
    return table


def totals(table: pd.DataFrame, cfg: dict) -> dict:
    """Average MIPS-equivalent (cycles / s / 1e6), worst block time and share of the block
    period, RAM and flash (constants plus the code estimate) against the budget."""
    streaming = table.loc[~table["per_frame"], "peak_block_cycles"].sum()
    frame = table.loc[table["per_frame"], "peak_block_cycles"].sum()
    block_s = cfg["stream"]["block_samples"] / cfg["stream"]["fs_hz"]
    peak_ms = 1e3 * (streaming + frame) / CLOCK_HZ
    flash = table["flash_bytes"].sum() + CODE_BYTES_ESTIMATE
    return {"mips": table["cycles_per_s"].sum() / 1e6, "peak_block_ms": peak_ms,
            "peak_block_share": peak_ms / (1e3 * block_s),
            "ram_bytes": int(table["ram_bytes"].sum()), "flash_bytes": int(flash),
            "number_format": table.attrs["number_format"],
            "within_budget": bool(table["cycles_per_s"].sum() / 1e6 <= BUDGET["mips"]
                                  and table["ram_bytes"].sum() <= BUDGET["ram_bytes"]
                                  and flash <= BUDGET["flash_bytes"])}
