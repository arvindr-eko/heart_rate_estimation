"""C constants of the device model, generated from configs/device.yml and the model files.

Writes c/build/generated/hr_model.h (sizes, scalar constants, array declarations) and
hr_model.c (coefficient and weight arrays) for the C port in c/. Values come from the device
modules' own init and load functions, so the C constants equal the Python reference's: filter
sections and steady-state step responses (frontend), TCN weights (net), scorer weights (scorer;
the input standardisation folded into the first layer: w1 / std and b1 - (mean / std) @ w1),
calibration (readout), random-walk kernel, octave shift and estimate window (tracker), and the
period of every HR state in envelope samples (features). Floats are written with the shortest
decimal that round-trips in float32. The header records the config and weight checksums.
"""

import hashlib
import logging
from pathlib import Path

import numpy as np

from device import estimator, features, frontend, net, readout, scorer, tracker
from training import config

log = logging.getLogger(__name__)

C_DIR = config.PROJECT_ROOT / "c"
OUT_DIR = C_DIR / "build" / "generated"


def _f(x: float) -> str:
    """C float literal that round-trips to the same float32."""
    return np.format_float_scientific(np.float32(x), unique=True) + "f"


def _array(name: str, values: np.ndarray, per_line: int = 6) -> str:
    """const float array definition, flattened in C order."""
    flat = [_f(v) for v in np.asarray(values, np.float32).ravel()]
    rows = [", ".join(flat[i:i + per_line]) for i in range(0, len(flat), per_line)]
    return f"const float {name}[{len(flat)}] = {{\n    " + ",\n    ".join(rows) + "\n};\n"


def _sha(path: Path) -> str:
    """First 12 hex digits of a file's SHA-256."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _blocks(cfg: dict, seconds: float) -> int:
    """Number of input blocks at or after which a time threshold in seconds is reached."""
    st = cfg["stream"]
    return int(np.ceil(seconds * st["fs_hz"] / st["block_samples"] - 1e-6))


def model(cfg: dict) -> tuple[dict, dict]:
    """Integer and float constants (name -> value) and arrays (name -> float32 array)."""
    st, ft, dc, rd = cfg["stream"], cfg["features"], cfg["decision"], cfg["readout"]
    fe = frontend.init(cfg)
    w = net.load_weights(net.weights_path(cfg))
    sc = scorer.load(scorer.weights_path(cfg))
    cal = readout.load_calibration(cfg)
    trk = tracker.init(cfg, estimator.frame_seconds(cfg))
    grid = features.hr_grid(cfg)
    lags = features.init(cfg).lags
    env_rate = cfg["frontend"]["env_rate_hz"]
    n_win = int(round(ft["window_s"] * env_rate))
    n_s1 = int(round(ft["s1_window_s"] * env_rate / ft["s1_decimation"]))
    dil = list(cfg["net"]["dilations"])
    conv_in = [w[f"conv{i}_w"].shape[1] for i in range(len(dil))]
    lines = [c * d * (cfg["net"]["kernel"] - 1) for c, d in zip(conv_in, dil)]
    min_overlap = ft["min_overlap_s"] * env_rate
    ints = {
        "HR_FS_HZ": st["fs_hz"], "HR_BLOCK_SAMPLES": st["block_samples"],
        "HR_FRAME_BLOCKS": st["frame_blocks"], "HR_DECIMATION": fe.decimation,
        "HR_ENV_PER_BLOCK": st["block_samples"] // fe.decimation, "HR_ENV_RATE_HZ": env_rate,
        "HR_N_BANDS": len(fe.sos), "HR_N_SECTIONS": fe.sos[0].shape[0],
        "HR_NET_LAYERS": len(dil), "HR_NET_IN": conv_in[0], "HR_NET_CH": w["conv0_w"].shape[0],
        "HR_NET_KERNEL": cfg["net"]["kernel"], "HR_NET_LINE_TOTAL": sum(lines),
        "HR_N_STATES": grid.size, "HR_WIN": n_win,
        "HR_WIN_MIN": int(np.ceil(ft["min_window_s"] * env_rate)),
        "HR_S1_WIN": n_s1, "HR_S1_DECIMATION": ft["s1_decimation"],
        "HR_S1_WIN_MIN": int(np.ceil(ft["min_window_s"] * env_rate / ft["s1_decimation"])),
        "HR_S1_LAST_STATE": int(np.flatnonzero(grid <= ft["s1_max_bpm"])[-1]),
        "HR_N_NCCF": min(n_win, int(np.floor(n_win - min_overlap)) + 2),
        "HR_N_YIN": min(n_win, int(np.ceil(lags.max())) + 2), "HR_MAX_PEAKS": n_win // 2,
        "HR_N_FEAT": len(features.FEATURE_NAMES), "HR_N_IN": sc.w1.shape[0],
        "HR_HIDDEN": sc.w1.shape[1], "HR_KERNEL_TAPS": trk.kernel.size,
        "HR_OCTAVE_SHIFT": trk.octave_shift, "HR_EST_HALFWIDTH": trk.halfwidth,
        "HR_MIN_INTERVALS": cfg["rhythm"]["min_intervals"],
        "HR_MIN_BLOCKS": _blocks(cfg, dc["min_s"]),
        "HR_OUTPUT_BLOCKS": _blocks(cfg, st["output_period_s"])}
    floats = {
        "HR_LOG_FLOOR": frontend.LOG_FLOOR, "HR_NORM_ALPHA": 1.0 / (cfg["net"]["norm_tau_s"]
                                                                   * env_rate),
        "HR_PEAK_OFFSET_MAX": features.PEAK_OFFSET_MAX, "HR_EPS": features.EPS,
        "HR_VAR_FLOOR": features.VAR_FLOOR, "HR_MIN_OVERLAP": min_overlap,
        "HR_LAG_TO_BPM": 60.0 * env_rate, "HR_GRID_LOG_MIN": np.log(cfg["grid"]["hr_min_bpm"]),
        "HR_GRID_LOG_STEP": np.log1p(cfg["grid"]["step_rel"]),
        "HR_EMISSION_BETA": cfg["scorer"]["emission_beta"],
        "HR_OCTAVE_PROB": cfg["tracker"]["octave_prob"],
        "HR_FLOOR_PROB": cfg["tracker"]["floor_prob"],
        "HR_EST_TOL_LOG": np.log1p(cfg["tracker"]["estimate_tol_rel"]),
        "HR_PEAK_MIN_REL": cfg["rhythm"]["peak_min_rel"],
        "HR_REFRACTORY_REL": cfg["rhythm"]["refractory_rel"],
        "HR_SLOW_BPM": dc["slow_bpm"], "HR_REGULAR_MAX_CV": dc["regular_max_cv"],
        "HR_SLOW_IRREGULAR_CONFIDENCE": dc["slow_irregular_confidence"],
        "HR_CONF_FINE_BPM": dc["conf_fine_bpm"], "HR_CONF_ROUGH_BPM": dc["conf_rough_bpm"],
        "HR_FORCED_MIN_MASS": dc["forced_min_mass"], "HR_MIN_PERIODICITY": dc["min_periodicity"],
        "HR_MIN_ACTIVATION_SD": dc["min_activation_sd"], "HR_MIN_S": dc["min_s"],
        "HR_DEFAULT_CONFIDENCE": rd["confidence"],
        "HR_DEFAULT_SLOW_IRREGULAR_AFTER_S": rd["slow_irregular_after_s"],
        "HR_DEFAULT_LATEST_S": rd["latest_s"]}
    w1 = (sc.w1 / sc.std[:, None]).astype(np.float32)
    b1 = (sc.b1 - (sc.mean / sc.std) @ sc.w1).astype(np.float32)
    arrays = {
        "hr_sos": np.stack(fe.sos), "hr_zi_step": np.stack(fe.zi_step),
        "hr_conv_w": np.concatenate([w[f"conv{i}_w"].ravel() for i in range(len(dil))]),
        "hr_conv_b": np.concatenate([w[f"conv{i}_b"] for i in range(len(dil))]),
        "hr_out_w": w["out_w"], "hr_out_b": np.atleast_1d(w["out_b"]),
        "hr_sc_w1": w1, "hr_sc_b1": b1, "hr_sc_w2": sc.w2, "hr_sc_b2": np.atleast_1d(sc.b2),
        "hr_cal_fine": cal.fine, "hr_cal_rough": cal.rough, "hr_kernel": trk.kernel,
        "hr_lag": lags}
    ints["HR_NET_DILATIONS"] = "{" + ", ".join(map(str, dil)) + "}"
    ints["HR_NET_LINE_OFFSETS"] = "{" + ", ".join(map(str, np.cumsum([0] + lines[:-1]))) + "}"
    ints["HR_NET_IN_CH"] = "{" + ", ".join(map(str, conv_in)) + "}"
    return {"ints": ints, "floats": floats}, arrays


def write(cfg: dict, out_dir: Path = OUT_DIR) -> Path:
    """Write hr_model.h and hr_model.c; return the output folder."""
    consts, arrays = model(cfg)
    weights = [net.weights_path(cfg), scorer.weights_path(cfg), readout.calibration_path(cfg)]
    source = (f"configs/device.yml ({_sha(config.CONFIG_FILES[0])}), "
              + ", ".join(f"{p.name} ({_sha(p)})" for p in weights))
    model_id = f"{cfg['weights']} {_sha(weights[0])[:6]}{_sha(weights[1])[:6]}"
    head = ["/* Generated by training/c_model.py; do not edit. Source: " + source + ". */",
            "#ifndef HR_MODEL_H", "#define HR_MODEL_H", "",
            f'#define HR_MODEL_ID "{model_id}"']
    head += [f"#define {k} {v}" for k, v in consts["ints"].items()]
    head += [f"#define {k} {_f(v)}" for k, v in consts["floats"].items()]
    head += [""] + [f"extern const float {k}[{np.asarray(v).size}];" for k, v in arrays.items()]
    head += ["", "#endif"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "hr_model.h").write_text("\n".join(head) + "\n")
    body = [f"/* Generated by training/c_model.py; do not edit. Source: {source}. */",
            '#include "hr_model.h"', ""] + [_array(k, v) for k, v in arrays.items()]
    (out_dir / "hr_model.c").write_text("\n".join(body))
    log.info("C model %s: %d constants, %d arrays (%d floats) -> %s", model_id,
             len(consts["ints"]) + len(consts["floats"]), len(arrays),
             sum(np.asarray(v).size for v in arrays.values()), out_dir.relative_to(C_DIR.parent))
    return out_dir
