"""C constants of the device model, generated from configs/device.yml and the model files.

Writes c/build/generated/hr_model.h (sizes, scalar constants, array declarations) and
hr_model.c (coefficient and weight arrays) for the C port in c/. Values come from the device
modules' own init and load functions, so the C constants equal the Python reference's: filter
sections and steady-state step responses (frontend), TCN weights (net), scorer weights (scorer;
the input standardisation folded into the first layer: w1 / std and b1 - (mean / std) @ w1),
calibration (readout), random-walk kernel, octave shift and estimate window (tracker), the
period of every HR state in envelope samples and the int16 scale of the ACF and NCCF
(features). Floats are written with the shortest decimal that round-trips in float32. The
header records the software version and the config and weight checksums.
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


def c_float(x: float) -> str:
    """C float literal that round-trips to the same float32."""
    return np.format_float_scientific(np.float32(x), unique=True) + "f"


def _array(name: str, values: np.ndarray, per_line: int = 6) -> str:
    """const float array definition, flattened in C order."""
    flat = [c_float(v) for v in np.asarray(values, np.float32).ravel()]
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
    presence = readout.load_presence(cfg)
    qc = cfg["quality"]
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
        "HR_HEART_BANDS": fe.heart_bands, "HR_FE_OUT": frontend.n_inputs(cfg),
        "HR_SOURCE_FROM": fe.source_from, "HR_SOURCE_FEATURES": int(fe.impulse > 0),
        "HR_VOICING_LAG_MIN": int(fe.lags[0]), "HR_VOICING_LAG_MAX": int(fe.lags[-1]),
        "HR_IMPULSE_WINDOW": max(fe.impulse, 1),
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
        "HR_LOST_FRAMES": readout.lost_frames(cfg, estimator.frame_seconds(cfg)),
        "HR_OUTPUT_BLOCKS": _blocks(cfg, st["output_period_s"]),
        "HR_RECENT_N": int(round(dc["recent_s"] * env_rate)),
        "HR_RECENT_OVERLAP": int(round(dc["recent_overlap_s"] * env_rate)),
        "HR_CONFIRM_FRAMES": readout.frames_of(dc["confirm_s"], estimator.frame_seconds(cfg)),
        "HR_PRESENCE_ENABLED": int(presence is not None),
        "HR_PRESENCE_N": len(readout.PRESENCE_FEATURES),
        "HR_PRESENCE_HIDDEN": presence.w1.shape[1] if presence is not None else 0}
    floats = {
        "HR_LOG_FLOOR": frontend.LOG_FLOOR,
        "HR_NCCF_FLOOR_SQ": float(np.float32(frontend.NCCF_FLOOR * frontend.NCCF_FLOOR)),
        "HR_NORM_ALPHA": 1.0 / (cfg["net"]["norm_tau_s"]
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
        "HR_LOST_PERIODICITY": dc["lost_periodicity"],
        "HR_DEFAULT_CONFIDENCE": rd["confidence"],
        "HR_DEFAULT_SLOW_IRREGULAR_AFTER_S": rd["slow_irregular_after_s"],
        "HR_DEFAULT_LATEST_S": rd["latest_s"],
        "HR_Q_HF_ALPHA": 1.0 / (qc["hf_tau_s"] * env_rate),
        "HR_HF_MAX": qc["hf_max"], "HR_HF_LOST": qc["hf_lost"],
        "HR_RECENT_TOL": dc["recent_tol_rel"], "HR_RECENT_MIN": dc["recent_min"],
        "HR_RECENT_LOST": dc["recent_lost"], "HR_PRESENCE_MIN": dc["presence_min"],
        "HR_PRESENCE_LOST": dc["presence_lost"],
        "HR_PRESENCE_SD_FLOOR": readout.PRESENCE_SD_FLOOR,
        "HR_PRESENCE_HF_LOW": readout.PRESENCE_HF_RANGE[0],
        "HR_PRESENCE_HF_HIGH": readout.PRESENCE_HF_RANGE[1],
        "HR_PRESENCE_B2": presence.b2 if presence is not None else 0.0}
    # int16 storage of the per-frame ACF and NCCF: scale (value to code), step (code to value)
    ints["HR_CODE_MAX"] = features.CODE_MAX
    floats |= {"HR_LAG_SCALE": features.LAG_SCALE, "HR_LAG_STEP": 1.0 / features.LAG_SCALE}
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
    if presence is not None:
        arrays |= {"hr_presence_w1": presence.w1, "hr_presence_b1": presence.b1,
                   "hr_presence_w2": presence.w2}
    if ints["HR_FE_OUT"] != ints["HR_NET_IN"]:
        raise ValueError(f"front end gives {ints['HR_FE_OUT']} inputs, the TCN takes "
                         f"{ints['HR_NET_IN']}")
    ints["HR_NET_DILATIONS"] = "{" + ", ".join(map(str, dil)) + "}"
    ints["HR_NET_LINE_OFFSETS"] = "{" + ", ".join(map(str, np.cumsum([0] + lines[:-1]))) + "}"
    ints["HR_NET_IN_CH"] = "{" + ", ".join(map(str, conv_in)) + "}"
    return {"ints": ints, "floats": floats}, arrays


def write(cfg: dict) -> Path:
    """Write hr_model.h and hr_model.c; return the output folder."""
    consts, arrays = model(cfg)
    weights = [net.weights_path(cfg), scorer.weights_path(cfg), readout.calibration_path(cfg)]
    gate = readout.presence_path(cfg)
    weights += [gate] if gate.is_file() else []
    source = (f"configs/device.yml ({_sha(config.CONFIG_FILES[0])}), "
              + ", ".join(f"{p.name} ({_sha(p)})" for p in weights))
    model_id = (f"{cfg['weights']} {_sha(weights[0])[:6]}{_sha(weights[1])[:6]}"
                + (f" gate {_sha(gate)[:6]}" if gate.is_file() else ""))
    head = ["/* Generated by training/c_model.py; do not edit. Source: " + source + ". */",
            "#ifndef HR_MODEL_H", "#define HR_MODEL_H", "",
            f'#define HR_VERSION "{config.version()}"', f'#define HR_MODEL_ID "{model_id}"']
    head += [f"#define {k} {v}" for k, v in consts["ints"].items()]
    head += [f"#define {k} {c_float(v)}" for k, v in consts["floats"].items()]
    head += [""] + [f"extern const float {k}[{np.asarray(v).size}];" for k, v in arrays.items()]
    head += ["", "#endif"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "hr_model.h").write_text("\n".join(head) + "\n")
    body = [f"/* Generated by training/c_model.py; do not edit. Source: {source}. */",
            '#include "hr_model.h"', ""] + [_array(k, v) for k, v in arrays.items()]
    (OUT_DIR / "hr_model.c").write_text("\n".join(body))
    log.info("C model %s: %d constants, %d arrays (%d floats) -> %s", model_id,
             len(consts["ints"]) + len(consts["floats"]), len(arrays),
             sum(np.asarray(v).size for v in arrays.values()), OUT_DIR.relative_to(C_DIR.parent))
    return OUT_DIR
