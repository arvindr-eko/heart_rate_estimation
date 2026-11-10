"""C port (c/) against the Python device reference, through ctypes: synthetic test signals, every
block's and frame's output of the reference (record) and of the C debug library (run), their
comparison, a long session (long_session), and the self-test cases of c/tools/hr_selftest.c
(write_selftest).

Heartbeat cases: every stage within float32 tolerances (absolute); a binary feature may flip
where its two compared values tie to rounding level, and the log-likelihoods of those states are
not compared. Cases without a heart sound throughout (no heart, contact lost): front end,
activation, activation SD, frame times and readings only (features of a near-flat or noise-only
window are rounding-sensitive in both implementations). Both: the same heart-present flags.
Recordings: reading times, BPM and confidence. Reading times count from the start of the input;
frame times from the start of the measurement, which a restart resets. Used by
tests/test_c_port.py.
"""

import ctypes
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

from device import estimator, features, frontend, net, scorer
from training import augment, c_model, config, data

log = logging.getLogger(__name__)

C_DIR = config.PROJECT_ROOT / "c"
LIB = C_DIR / "build" / ("libhr_debug.dylib" if sys.platform == "darwin" else "libhr_debug.so")
SYNTHETIC = [("hr72", 72, {}), ("hr45_slow", 45, {}), ("hr150", 150, {}), ("hr250_fast", 250, {}),
             ("hr110_loud_s2", 110, {"s2_ratio": 1.3}),
             ("hr80_irregular", 80, {"interval_cv": 0.10}), ("silence", 0, {}),
             ("hum_only", 0, {"noise": "hum"}),   # periodic ripple, no beats: activation-SD gate
             ("hr72_lift_off", 72, {"off_s": 8.0}),   # heart, room noise, heart: restart
             ("hr72_silent_gap", 72, {"off_s": 10.0, "off": "silence"})]  # digital zeros
HEART_THROUGHOUT = [name for name, hr, kw in SYNTHETIC if hr and "off_s" not in kw]
SYNTHETIC_S = 10.0                   # length of a synthetic heart or noise signal
PEAK = 16000                         # int16 peak of the synthetic cases
TOL = {"band_log_energy": 1e-3, "activation": 1e-4, "features": 1e-3, "log_emission": 1e-3,
       "posterior": 1e-4, "t_s": 1e-4, "hr": 1e-3, "frame_hr": 1e-3, "periodicity": 1e-4,
       "activation_sd": 1e-5, "cv": 1e-4, "beat_hr": 1e-3, "p_fine": 1e-4, "p_rough": 1e-4}
READING_TOL = {"bpm": 1e-3, "confidence": 1e-3}
RECORDING_TOL = {"bpm": 0.1, "confidence": 0.02}   # recordings, gaps, target: flips on ties
FLAG_COLUMNS = [features.FEATURE_NAMES.index(n) for n in ("valid_2T", "acf_peak", "valid_3T")]
MAX_FLAG_FLIPS = 1e-4                              # share of feature entries
NO_HEART_KEYS = ("band_log_energy", "activation", "activation_sd", "t_s")
SELFTEST_CASES = ("hr72_lift_off", "hr45_slow", "silence")  # regular path and restart, slow
                                                            # path, no heart sound
LONG_SESSION = {"minutes": 30.0, "bpm": 72.0, "snr_db": 20.0, "edge_minutes": 5.0, "seed": 7}
PCM_PER_LINE = 16
INFO_FIELDS = ("t_s", "estimate_bpm", "frame_bpm", "periodicity", "activation_sd",
               "interval_cv", "beat_bpm", "p_fine", "p_rough")


class Reading(ctypes.Structure):
    """hr_reading_t."""
    _fields_ = [("t_s", ctypes.c_float), ("bpm", ctypes.c_float), ("confidence", ctypes.c_float),
                ("path", ctypes.c_uint8), ("first", ctypes.c_uint8)]


class FrameInfo(ctypes.Structure):
    """hr_frame_info_t."""
    _fields_ = [(name, ctypes.c_float) for name in INFO_FIELDS] + [
        ("path", ctypes.c_uint8), ("phase", ctypes.c_uint8), ("heart_present", ctypes.c_uint8)]


# Test signals and the Python reference

def synthetic(cfg: dict) -> list[tuple[str, np.ndarray, float]]:
    """(name, int16 PCM, true BPM) of the synthetic cases. off_s: the heart signal, then off_s
    of white noise at its level or of silence (off), then the heart signal again."""
    fs = cfg["stream"]["fs_hz"]
    n = int(SYNTHETIC_S * fs)
    cases = []
    for name, hr, kw in SYNTHETIC:
        kw = dict(kw)
        off_s, off_kind = kw.pop("off_s", 0.0), kw.pop("off", "white")
        if hr:
            audio, _ = augment.synthetic_pcg(hr, duration_s=SYNTHETIC_S, fs=fs, seed=hr, **kw)
            audio = augment.add_noise(audio, fs, "white", 25.0, hr)
            if off_s:
                off = (np.zeros(int(off_s * fs)) if off_kind == "silence" else
                       augment.noise(off_kind, int(off_s * fs), fs, np.random.default_rng(0)))
                off = off * audio.std() / max(off.std(), 1e-12)
                audio = np.concatenate([audio, off, audio])
            pcm = np.round(audio / np.abs(audio).max() * PEAK).astype(np.int16)
        elif kw.get("noise"):
            x = augment.noise(kw["noise"], n, fs, np.random.default_rng(0))
            pcm = np.round(x / np.abs(x).max() * PEAK).astype(np.int16)
        else:
            pcm = np.zeros(n, np.int16)
        cases.append((name, pcm, float(hr)))
    return cases


def to_pcm(audio: np.ndarray) -> np.ndarray:
    """int16 PCM of a recording loaded as float in [-1, 1] (exact for 16-bit WAVs)."""
    return np.round(np.clip(audio, -1.0, 32767 / 32768) * 32768).astype(np.int16)


def restart_times(cfg: dict, trace: list) -> list[float]:
    """Input times (s) at which the estimator restarted the measurement: the trace's measurement
    time falls back."""
    frame_s = estimator.frame_seconds(cfg)
    return [(i + 1) * frame_s for i in range(len(trace) - 1) if trace[i + 1]["t"] < trace[i]["t"]]


def record(cfg: dict, pcm: np.ndarray) -> dict:
    """Stream int16 PCM through the Python reference, keeping every block's and frame's outputs:
    band_log_energy (4, envelope samples), activation, per frame t_s, features (frames, 232, 17;
    NaN before the first window), log_emission, posterior, hr, frame_hr, periodicity,
    activation_sd, cv, beat_hr, p_fine, p_rough, regular_path, heart_present; readings."""
    x = pcm.astype(np.float32) / 32768.0
    state = estimator.init(cfg, keep_trace=True)
    fe, nn = frontend.init(cfg), net.init(cfg)
    block, n_grid = cfg["stream"]["block_samples"], state.grid.size
    n_in = state.model.w1.shape[0]
    env, act, feats, loglik, post, readings = [], [], [], [], [], []
    for start in range(0, x.size - block + 1, block):
        piece = x[start:start + block]
        band = frontend.process(fe, piece)
        env.append(band)
        act.append(net.process(nn, band)[0])
        reading = estimator.process(state, piece)
        if reading is not None:
            readings.append(((start + block) / cfg["stream"]["fs_hz"], *reading[1:]))
        if state.n_blocks == 0:          # restart: the estimator's front end and TCN are new
            fe, nn = frontend.init(cfg), net.init(cfg)
        if state.n_blocks % cfg["stream"]["frame_blocks"] == 0:
            f = estimator.scorer_input(state)
            feats.append(np.full((n_grid, n_in), np.nan, np.float32) if f is None else f)
            loglik.append(np.full(n_grid, np.nan) if f is None
                          else scorer.log_emission(state.model, f))
            post.append(state.trk.posterior.copy())
    trace = state.trace
    return {"band_log_energy": np.hstack(env), "activation": np.concatenate(act),
            "t_s": np.array([f["t"] for f in trace]), "features": np.stack(feats),
            "log_emission": np.stack(loglik), "posterior": np.stack(post),
            **{k: np.array([f[k] for f in trace], dtype=float) for k in (
                "hr", "frame_hr", "periodicity", "activation_sd", "cv", "beat_hr", "p_fine",
                "p_rough")},
            "regular_path": np.array([f["path"] == "regular" for f in trace]),
            "heart_present": np.array([f["heart_present"] for f in trace], dtype=bool),
            "readings": np.array(readings, dtype=float).reshape(-1, 3)}


# C port

def available() -> bool:
    """True when make and a C compiler are on the path."""
    return bool(shutil.which("make") and shutil.which("cc"))


def load(cfg: dict) -> ctypes.CDLL:
    """Generate the model files of the configured model, build and load the debug library."""
    c_model.write(cfg)
    subprocess.run(["make", "-s", "-C", str(C_DIR), str(LIB.relative_to(C_DIR))], check=True)
    lib = ctypes.CDLL(str(LIB))
    for name in ("hr_debug_band_energy", "hr_debug_activation", "hr_debug_features",
                 "hr_debug_log_emission", "hr_get_posterior"):
        getattr(lib, name).restype = ctypes.POINTER(ctypes.c_float)
    lib.hr_state_size.restype = ctypes.c_uint32
    return lib


def run(cfg: dict, lib: ctypes.CDLL, pcm: np.ndarray, stages: bool = True) -> dict:
    """Stream int16 PCM through the C port; the outputs of record (readings only without
    stages)."""
    st = cfg["stream"]
    n_bands = len(cfg["frontend"]["bands_hz"])
    env_per_block = st["block_samples"] * cfg["frontend"]["env_rate_hz"] // st["fs_hz"]
    n_states = features.hr_grid(cfg).size
    n_in = len(features.FEATURE_NAMES) + len(features.S1_FEATURES) + 1
    state = ctypes.create_string_buffer(lib.hr_state_size())
    if lib.hr_init(state, None) != 0:
        raise RuntimeError("hr_init failed")
    reading, info = Reading(), FrameInfo()
    out = {k: [] for k in ("band_log_energy", "activation", "features", "log_emission",
                           "posterior", "info")}
    readings = []
    blocks = np.ascontiguousarray(pcm[:pcm.size - pcm.size % st["block_samples"]]).reshape(
        -1, st["block_samples"])

    def grab(ptr, shape):
        """Copy a C float array."""
        return np.ctypeslib.as_array(ptr, shape=shape).copy()

    for i, block in enumerate(blocks):
        if lib.hr_process_block(state, block.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
                                ctypes.byref(reading)):
            readings.append(((i + 1) * st["block_samples"] / st["fs_hz"], reading.bpm,
                             reading.confidence))
        if not stages:
            continue
        out["band_log_energy"].append(grab(lib.hr_debug_band_energy(state),
                                           (n_bands, env_per_block)))
        out["activation"].append(grab(lib.hr_debug_activation(state), (env_per_block,)))
        if (i + 1) % st["frame_blocks"]:
            continue
        has = lib.hr_debug_has_features(state)
        out["features"].append(grab(lib.hr_debug_features(state), (n_states, n_in)) if has
                               else np.full((n_states, n_in), np.nan, np.float32))
        out["log_emission"].append(grab(lib.hr_debug_log_emission(state), (n_states,)) if has
                                   else np.full(n_states, np.nan))
        out["posterior"].append(grab(lib.hr_get_posterior(state), (n_states,)))
        lib.hr_get_frame_info(state, ctypes.byref(info))
        out["info"].append({k: getattr(info, k) for k in INFO_FIELDS}
                           | {"path": info.path, "heart_present": info.heart_present})
    result = {"readings": np.array(readings, dtype=float).reshape(-1, 3)}
    if stages:
        rows = out.pop("info")
        column = {"t_s": "t_s", "hr": "estimate_bpm", "frame_hr": "frame_bpm",
                  "periodicity": "periodicity", "activation_sd": "activation_sd",
                  "cv": "interval_cv", "beat_hr": "beat_bpm", "p_fine": "p_fine",
                  "p_rough": "p_rough"}
        result |= {"band_log_energy": np.hstack(out["band_log_energy"]),
                   "activation": np.concatenate(out["activation"]),
                   "features": np.stack(out["features"]),
                   "log_emission": np.stack(out["log_emission"]),
                   "posterior": np.stack(out["posterior"]),
                   "regular_path": np.array([r["path"] == 0 for r in rows]),
                   "heart_present": np.array([r["heart_present"] == 1 for r in rows]),
                   **{k: np.array([r[c] for r in rows]) for k, c in column.items()}}
    return result


# Comparison

def max_differences(ref: dict, got: dict, keys=tuple(TOL)) -> dict:
    """Largest absolute difference per stage; inf where the NaN patterns differ."""
    diffs = {}
    for key in keys:
        a, b = np.asarray(ref[key], float), np.asarray(got[key], float)
        if a.shape != b.shape or not np.array_equal(np.isnan(a), np.isnan(b)):
            diffs[key] = np.inf
            continue
        both = ~np.isnan(a)
        diffs[key] = float(np.max(np.abs(a - b)[both])) if both.any() else 0.0
    return diffs


def reading_differences(ref, got) -> dict:
    """Reading agreement: same count and times, largest BPM and confidence differences."""
    ref, got = np.asarray(ref, float).reshape(-1, 3), np.asarray(got, float).reshape(-1, 3)
    same = ref.shape == got.shape and np.allclose(ref[:, 0], got[:, 0], rtol=0, atol=1e-6)
    if not same:
        return {"same_times": False, "bpm": np.inf, "confidence": np.inf}
    return {"same_times": True,
            "bpm": float(np.max(np.abs(ref[:, 1] - got[:, 1]), initial=0)),
            "confidence": float(np.max(np.abs(ref[:, 2] - got[:, 2]), initial=0))}


def compare_case(cfg: dict, lib: ctypes.CDLL, pcm: np.ndarray, heart: bool) -> dict:
    """Python reference against the C port on one case: stage differences, binary-feature
    flips (heart: heartbeats throughout), paths, presence flags and readings."""
    ref, got = record(cfg, pcm), run(cfg, lib, pcm)
    result = {"readings": len(ref["readings"]), "readings_c": len(got["readings"]),
              "same_presence": bool(np.array_equal(ref["heart_present"], got["heart_present"])),
              **{f"reading_{k}": v for k, v in reading_differences(ref["readings"],
                                                                   got["readings"]).items()}}
    if not heart:
        return result | {"diffs": max_differences(ref, got, NO_HEART_KEYS)}
    a, b = np.nan_to_num(ref["features"]), np.nan_to_num(got["features"])
    off = np.abs(a - b) > TOL["features"]
    continuous = np.ones(a.shape[-1], bool)
    continuous[FLAG_COLUMNS] = False
    flips = off.any(axis=-1)
    ref["log_emission"], got["log_emission"] = ref["log_emission"][~flips], got["log_emission"][
        ~flips]
    diffs = max_differences(ref, got, [k for k in TOL if k != "features"])
    diffs["features"] = float(np.abs(a - b)[..., continuous].max())
    return result | {"diffs": diffs, "flips": int(off[..., ~continuous].sum()),
                     "flip_share": float(off[..., ~continuous].sum() / a.size),
                     "same_path": bool(np.array_equal(ref["regular_path"], got["regular_path"]))}


def compare_recordings(cfg: dict, lib: ctypes.CDLL, golden: list[dict]) -> dict:
    """C readings against frozen Python readings (training-side readings.json rows) on
    recordings: identical reading times, BPM and confidence differences."""
    rows = [reading_differences(g["readings"], run(cfg, lib, to_pcm(
        data.load_wav(cfg, g["output_filename"])), stages=False)["readings"]) for g in golden]
    same = [r for r, g in zip(rows, golden) if r["same_times"] and len(g["readings"])]
    bpm = np.array([r["bpm"] for r in same])
    conf = np.array([r["confidence"] for r in same])
    return {"recordings": len(rows), "same_times": sum(r["same_times"] for r in rows),
            "readings": int(sum(len(g["readings"]) for g in golden)),
            "bpm_median": float(np.median(bpm)), "bpm_p99": float(np.percentile(bpm, 99)),
            "bpm_max": float(bpm.max()), "confidence_p99": float(np.percentile(conf, 99)),
            "confidence_max": float(conf.max())}


# Long session

def long_session(cfg: dict, lib: ctypes.CDLL, ls: dict = LONG_SESSION) -> dict:
    """Synthetic PCG at a constant rate with white noise through the Python reference and the C
    port: reading agreement, restarts, absolute error against the true rate over the first and
    the last edge_minutes, and the drift of the activation lag sums (updated per sample in
    float32) from a float64 computation on the final window, relative to lag 0."""
    fs = cfg["stream"]["fs_hz"]
    audio, _ = augment.synthetic_pcg(ls["bpm"], duration_s=ls["minutes"] * 60.0, fs=fs,
                                     seed=ls["seed"])
    audio = augment.add_noise(audio, fs, "white", ls["snr_db"], ls["seed"])
    pcm = np.round(audio / np.abs(audio).max() * PEAK).astype(np.int16)
    state = estimator.init(cfg, keep_trace=True)
    reads, trace = estimator.run(cfg, pcm.astype(np.float32) / 32768.0, state=state)
    ref = np.array(reads, float).reshape(-1, 3)
    got = run(cfg, lib, pcm, stages=False)["readings"]
    w = features.window(state.feat).astype(np.float64)
    direct = np.array([w[k:] @ w[:w.size - k] for k in range(w.size)])
    edge = ls["edge_minutes"] * 60.0
    first, last = ref[ref[:, 0] <= edge, 1], ref[ref[:, 0] > ref[-1, 0] - edge, 1]
    diff = reading_differences(ref, got)
    return {**ls, "readings": len(ref), "same_times": diff["same_times"],
            "bpm_diff_max": diff["bpm"], "confidence_diff_max": diff["confidence"],
            "restarts": len(restart_times(cfg, trace)),
            "mae_first_bpm": float(np.mean(np.abs(first - ls["bpm"]))),
            "mae_last_bpm": float(np.mean(np.abs(last - ls["bpm"]))),
            "lag_sum_drift": float(np.max(np.abs(state.feat.sums[:w.size] - direct))
                                   / direct[0])}


# Self-test cases

def _c_array(ctype: str, name: str, items: list[str], per_line: int) -> str:
    """static const C array definition from formatted items."""
    rows = [", ".join(items[i:i + per_line]) for i in range(0, len(items), per_line)]
    return f"static const {ctype} {name}[{len(items)}] = {{\n    " + ",\n    ".join(rows) + "\n};\n"


def _reading_rows(cfg: dict, pcm: np.ndarray) -> list[str]:
    """C initialisers {block, bpm, confidence, first} of the Python readings of one case; first
    marks the first reading of each measurement (after the start or a restart)."""
    st = cfg["stream"]
    reads, trace = estimator.run(cfg, pcm.astype(np.float32) / 32768.0, keep_trace=True)
    starts = [0.0] + restart_times(cfg, trace)
    rows, previous = [], -1.0
    for t, bpm, conf in reads:
        first = any(previous < s < t + 1e-9 for s in starts)
        rows.append(f"{{{round(t * st['fs_hz'] / st['block_samples'])}, {c_model.c_float(bpm)}, "
                    f"{c_model.c_float(conf)}, {int(first)}}}")
        previous = t
    return rows


def write_selftest(cfg: dict) -> Path:
    """Write hr_selftest_cases.h for c/tools/hr_selftest.c: the int16 PCM of SELFTEST_CASES
    (all-zero signals as NULL) and the Python reference readings, with RECORDING_TOL as the
    tolerances on the target (other compiler and math library; rounding-sensitive gap frames)."""
    body, table = [], []
    for name, pcm, _ in synthetic(cfg):
        if name not in SELFTEST_CASES:
            continue
        rows = _reading_rows(cfg, pcm)
        pcm_ref = reads_ref = "0"
        if pcm.any():
            pcm_ref = f"pcm_{name}"
            body.append(_c_array("int16_t", pcm_ref, [str(v) for v in pcm], PCM_PER_LINE))
        if rows:
            reads_ref = f"readings_{name}"
            body.append(_c_array("hr_selftest_reading_t", reads_ref, rows, 1))
        table.append(f'    {{"{name}", {pcm_ref}, {pcm.size}u, {len(rows)}u, {reads_ref}}}')
    head = [f"/* Generated by training/c_port.py; do not edit. Synthetic signals (no recorded "
            f"data) and the readings of the Python reference, version {config.version()}. */",
            "#ifndef HR_SELFTEST_CASES_H", "#define HR_SELFTEST_CASES_H", "",
            "#include <stdint.h>", "",
            f"#define HR_SELFTEST_BPM_TOL {c_model.c_float(RECORDING_TOL['bpm'])}",
            f"#define HR_SELFTEST_CONFIDENCE_TOL {c_model.c_float(RECORDING_TOL['confidence'])}",
            f"#define HR_SELFTEST_N_CASES {len(table)}", "",
            "typedef struct {",
            "    uint32_t block;                 /* input blocks processed at the reading */",
            "    float bpm;", "    float confidence;",
            "    uint8_t first;                  /* first reading of a measurement */",
            "} hr_selftest_reading_t;", "",
            "typedef struct {", "    const char *name;",
            "    const int16_t *pcm;             /* NULL: zeros */",
            "    uint32_t n_samples;", "    uint16_t n_readings;",
            "    const hr_selftest_reading_t *readings;", "} hr_selftest_case_t;", ""]
    cases = ("static const hr_selftest_case_t hr_selftest_cases[HR_SELFTEST_N_CASES] = {\n"
             + ",\n".join(table) + "\n};\n")
    path = c_model.OUT_DIR / "hr_selftest_cases.h"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(head) + "\n" + "\n".join(body) + "\n" + cases + "\n#endif\n")
    log.info("self-test cases: %s -> %s", ", ".join(SELFTEST_CASES),
             path.relative_to(config.PROJECT_ROOT))
    return path
