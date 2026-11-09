"""C port (c/) against the Python device reference, on the synthetic reference cases
(training.vectors) and, with local data, on test recordings against the frozen Python readings.
Heartbeat cases: every block's output within float32 tolerances (absolute), same paths and
readings; a binary feature may flip where its two compared values tie to rounding level (at most
MAX_FLAG_FLIPS of the entries; those states' log-likelihoods are not compared). Cases without a
heart sound: front end, activation, activation SD and readings (none) only, since features of a
near-flat window are rounding noise in both implementations and readings are gated there.
Builds the debug library with make; skipped without a C toolchain."""

import ctypes
import json
import shutil
import subprocess
import sys

import numpy as np
import pytest

from device import features
from tests.conftest import needs_data
from training import c_model, config, data, vectors

C_DIR = config.PROJECT_ROOT / "c"
LIB = C_DIR / "build" / ("libhr_debug.dylib" if sys.platform == "darwin" else "libhr_debug.so")
TOL = {"band_log_energy": 1e-3, "activation": 1e-4, "features": 1e-3, "log_emission": 1e-3,
       "posterior": 1e-4, "hr": 1e-3, "frame_hr": 1e-3, "periodicity": 1e-4,
       "activation_sd": 1e-5, "cv": 1e-4, "beat_hr": 1e-3, "p_fine": 1e-4, "p_rough": 1e-4}
READING_TOL = {"bpm": 1e-3, "confidence": 1e-3}
# test recordings: covers binary-feature flips on near-ties
RECORDING_TOL = {"bpm": 0.1, "confidence": 0.02}
FLAG_COLUMNS = [features.FEATURE_NAMES.index(n) for n in ("valid_2T", "acf_peak", "valid_3T")]
MAX_FLAG_FLIPS = 1e-4                        # share of feature entries
NO_HEART_KEYS = ("band_log_energy", "activation", "activation_sd")
INFO_FIELDS = ("t_s", "estimate_bpm", "frame_bpm", "periodicity", "activation_sd",
               "interval_cv", "beat_bpm", "p_fine", "p_rough")


class Reading(ctypes.Structure):
    """hr_reading_t."""
    _fields_ = [("t_s", ctypes.c_float), ("bpm", ctypes.c_float), ("confidence", ctypes.c_float),
                ("path", ctypes.c_uint8), ("first", ctypes.c_uint8)]


class FrameInfo(ctypes.Structure):
    """hr_frame_info_t."""
    _fields_ = [(name, ctypes.c_float) for name in INFO_FIELDS] + [("path", ctypes.c_uint8),
                                                                    ("phase", ctypes.c_uint8)]


@pytest.fixture(scope="module")
def lib(cfg):
    """Debug build of the C port for the configured model."""
    if not (shutil.which("make") and shutil.which("cc")):
        pytest.skip("no C toolchain")
    c_model.write(cfg)
    subprocess.run(["make", "-s", "-C", str(C_DIR), str(LIB.relative_to(C_DIR))], check=True)
    lib = ctypes.CDLL(str(LIB))
    for name in ("hr_debug_band_energy", "hr_debug_activation", "hr_debug_features",
                 "hr_debug_log_emission", "hr_get_posterior"):
        getattr(lib, name).restype = ctypes.POINTER(ctypes.c_float)
    lib.hr_state_size.restype = ctypes.c_uint32
    return lib


@pytest.fixture(scope="module")
def cases(cfg):
    """Synthetic reference cases by name: int16 PCM."""
    return {name: pcm for name, pcm, _ in vectors.synthetic(cfg)}


def run_c(cfg, lib, pcm: np.ndarray, stages: bool = True) -> dict:
    """Stream int16 PCM through the C port; the outputs of vectors.record (readings only
    without stages)."""
    st = cfg["stream"]
    n_states = features.hr_grid(cfg).size
    n_in = len(features.FEATURE_NAMES) + len(features.S1_FEATURES) + 1
    state = ctypes.create_string_buffer(lib.hr_state_size())
    assert lib.hr_init(state, None) == 0
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
        due = lib.hr_process_block(state, block.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
                                   ctypes.byref(reading))
        if due:
            readings.append((reading.t_s, reading.bpm, reading.confidence))
        if not stages:
            continue
        out["band_log_energy"].append(grab(lib.hr_debug_band_energy(state), (4, 5)))
        out["activation"].append(grab(lib.hr_debug_activation(state), (5,)))
        if (i + 1) % st["frame_blocks"]:
            continue
        has = lib.hr_debug_has_features(state)
        out["features"].append(grab(lib.hr_debug_features(state), (n_states, n_in)) if has
                               else np.full((n_states, n_in), np.nan, np.float32))
        out["log_emission"].append(grab(lib.hr_debug_log_emission(state), (n_states,)) if has
                                   else np.full(n_states, np.nan))
        out["posterior"].append(grab(lib.hr_get_posterior(state), (n_states,)))
        lib.hr_get_frame_info(state, ctypes.byref(info))
        out["info"].append({k: getattr(info, k) for k in INFO_FIELDS} | {"path": info.path})
    result = {"readings": np.array(readings, dtype=float).reshape(-1, 3)}
    if stages:
        rows = out.pop("info")
        result |= {"band_log_energy": np.hstack(out["band_log_energy"]),
                   "activation": np.concatenate(out["activation"]),
                   "features": np.stack(out["features"]),
                   "log_emission": np.stack(out["log_emission"]),
                   "posterior": np.stack(out["posterior"]),
                   "hr": np.array([r["estimate_bpm"] for r in rows]),
                   "frame_hr": np.array([r["frame_bpm"] for r in rows]),
                   "periodicity": np.array([r["periodicity"] for r in rows]),
                   "activation_sd": np.array([r["activation_sd"] for r in rows]),
                   "cv": np.array([r["interval_cv"] for r in rows]),
                   "beat_hr": np.array([r["beat_bpm"] for r in rows]),
                   "p_fine": np.array([r["p_fine"] for r in rows]),
                   "p_rough": np.array([r["p_rough"] for r in rows]),
                   "regular_path": np.array([r["path"] == 0 for r in rows])}
    return result


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


def assert_same_readings(ref: np.ndarray, got: np.ndarray, tol: dict = READING_TOL) -> None:
    """Same reading times; BPM and confidence within tol."""
    ref, got = np.asarray(ref, float).reshape(-1, 3), np.asarray(got, float).reshape(-1, 3)
    assert ref.shape == got.shape
    assert np.allclose(ref[:, 0], got[:, 0], rtol=0, atol=1e-6)
    assert np.max(np.abs(ref[:, 1] - got[:, 1]), initial=0) <= tol["bpm"]
    assert np.max(np.abs(ref[:, 2] - got[:, 2]), initial=0) <= tol["confidence"]


def flag_flips(ref: dict, got: dict) -> np.ndarray:
    """(frame, state) mask of binary-feature flips; raises if anything else exceeds the
    feature tolerance."""
    a, b = np.nan_to_num(ref["features"]), np.nan_to_num(got["features"])
    off = np.abs(a - b) > TOL["features"]
    other = np.ones(a.shape[-1], bool)
    other[FLAG_COLUMNS] = False
    assert not off[..., other].any(), "continuous feature beyond tolerance"
    return off.any(axis=-1)


@pytest.mark.parametrize("case", [name for name, hr, _ in vectors.SYNTHETIC if hr])
def test_blocks_match_reference(cfg, lib, cases, case):
    """Heartbeat case: every stage within its tolerance, same paths and readings."""
    ref, got = vectors.record(cfg, cases[case]), run_c(cfg, lib, cases[case])
    flips = flag_flips(ref, got)
    assert flips.mean() <= MAX_FLAG_FLIPS * ref["features"].shape[-1]
    keep = ~flips
    ref["log_emission"], got["log_emission"] = ref["log_emission"][keep], got["log_emission"][keep]
    diffs = max_differences(ref, got, [k for k in TOL if k != "features"])
    assert all(diffs[k] <= TOL[k] for k in diffs), diffs
    assert np.array_equal(ref["regular_path"], got["regular_path"])
    assert_same_readings(ref["readings"], got["readings"])


@pytest.mark.parametrize("case", [name for name, hr, _ in vectors.SYNTHETIC if not hr])
def test_no_heart_matches_reference(cfg, lib, cases, case):
    """No heart sound: same front end, activation and activation SD; no reading in either."""
    ref, got = vectors.record(cfg, cases[case]), run_c(cfg, lib, cases[case])
    diffs = max_differences(ref, got, NO_HEART_KEYS)
    assert all(diffs[k] <= TOL[k] for k in diffs), diffs
    assert len(ref["readings"]) == len(got["readings"]) == 0


def test_readings_match_golden(cfg, lib):
    """C readings on every test recording: the frozen Python reading times, BPM and confidence
    within RECORDING_TOL."""
    if not needs_data(cfg["paths"]):
        pytest.skip("local data or golden readings not available")
    golden = json.loads((cfg["paths"]["golden_dir"] / f"{cfg['weights']}_test_readings.json")
                        .read_text())
    for row in golden:
        pcm = vectors.to_pcm(data.load_wav(cfg, row["output_filename"]))
        assert_same_readings(row["readings"], run_c(cfg, lib, pcm, stages=False)["readings"],
                             RECORDING_TOL)
