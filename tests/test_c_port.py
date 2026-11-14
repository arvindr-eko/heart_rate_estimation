"""C port (c/) against the Python device reference (training.c_port): every block's output and
the readings on the synthetic cases, the firmware calls against hr_process_block and the
reference display rule, restarts after input gaps, a 30 min session, the self-test on the host
with the state size against the RAM budget, and the readings on every test recording against
the frozen Python readings when the local data is available. Skipped without a C toolchain."""

import ctypes
import json
import re
import shutil
import subprocess

import numpy as np
import pytest

from tests.conftest import golden_file, needs_data
from training import c_port


@pytest.fixture(scope="module")
def lib(cfg):
    """Debug build of the C port for the configured model."""
    if not c_port.available():
        pytest.skip("no C toolchain")
    return c_port.load(cfg)


@pytest.fixture(scope="module")
def cases(cfg):
    """Synthetic reference cases by name: int16 PCM."""
    return {name: pcm for name, pcm, _ in c_port.synthetic(cfg)}


@pytest.mark.parametrize("case", c_port.HEART_THROUGHOUT)
def test_blocks_match_reference(cfg, lib, cases, case):
    """Heartbeat case: every stage within its tolerance, same paths and readings."""
    r = c_port.compare_case(cfg, lib, cases[case], heart=True)
    assert r["flip_share"] <= c_port.MAX_FLAG_FLIPS
    assert all(r["diffs"][k] <= c_port.TOL[k] for k in r["diffs"]), r["diffs"]
    assert r["same_path"] and r["same_presence"] and r["reading_same_times"]
    assert r["reading_bpm"] <= c_port.READING_TOL["bpm"]
    assert r["reading_confidence"] <= c_port.READING_TOL["confidence"]


@pytest.mark.parametrize("case", [name for name, hr, _ in c_port.SYNTHETIC if not hr])
def test_no_heart_matches_reference(cfg, lib, cases, case):
    """No heart sound: same front end, activation and activation SD; no reading in either."""
    r = c_port.compare_case(cfg, lib, cases[case], heart=False)
    assert all(r["diffs"][k] <= c_port.TOL[k] for k in r["diffs"]), r["diffs"]
    assert r["same_presence"] and r["readings"] == r["readings_c"] == 0


@pytest.mark.parametrize("case", [name for name, _, kw in c_port.SYNTHETIC if "off_s" in kw])
def test_lift_off_matches_reference(cfg, lib, cases, case):
    """Contact lost and back: same front end, activation, activation SD, presence flags, restart
    frame and reading times; BPM and confidence within RECORDING_TOL (frames of the gap are
    rounding-sensitive in both implementations)."""
    r = c_port.compare_case(cfg, lib, cases[case], heart=False)
    assert all(r["diffs"][k] <= c_port.TOL[k] for k in r["diffs"]), r["diffs"]
    assert r["same_presence"] and r["reading_same_times"] and r["readings"] > 0
    assert r["reading_bpm"] <= c_port.RECORDING_TOL["bpm"]
    assert r["reading_confidence"] <= c_port.RECORDING_TOL["confidence"]


@pytest.mark.parametrize("case", [name for name, _, _ in c_port.SYNTHETIC])
def test_firmware_calls_match_blocks(cfg, lib, cases, case):
    """Firmware calls at random processing intervals: every shown value equals the reference
    display rule over hr_process_block's readings, every latest reading equals its own."""
    for seed in range(3):
        r = c_port.compare_stream(cfg, lib, cases[case], seed)
        assert r["processed"] == r["blocks"] and r["checks"] > 0
        assert r["shown_mismatches"] == r["reading_mismatches"] == 0, r


def test_firmware_input_gap_restarts(cfg, lib, cases):
    """Input pushed beyond firmware.ring_blocks before processing is dropped, as are samples
    pushed as NULL; the next complete block starts a new measurement."""
    block, ring = cfg["stream"]["block_samples"], cfg["firmware"]["ring_blocks"]
    block_s = block / cfg["stream"]["fs_hz"]
    pcm, state, out = cases["hr72"], ctypes.create_string_buffer(lib.hr_state_size()), \
        c_port.Reading()
    lib.hr_init(state, None)

    def push(first: int, last: int) -> bool:
        """Push blocks first to last - 1 of the case."""
        piece = np.ascontiguousarray(pcm[first * block:last * block])
        return lib.hr_push_data(state, piece.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
                                piece.size)

    def process() -> tuple[int, float]:
        """Process and release the queued blocks; their number and the measurement time."""
        n = lib.hr_process_chunk(state)
        assert lib.hr_update_chunk(state) == 0
        lib.hr_calculate(state, ctypes.byref(out))
        return n, out.t_s

    assert push(0, ring + 3)
    assert process() == (ring, pytest.approx(ring * block_s))
    assert push(ring + 3, ring + 5)
    assert process() == (2, pytest.approx(2 * block_s))
    assert not lib.hr_push_data(state, None, c_port.PUSH_SAMPLES)
    assert push(ring + 5, ring + 6)
    assert process() == (1, pytest.approx(block_s))


def test_long_session(cfg, lib):
    """30 min at 72 BPM: C reading times identical to Python, BPM and confidence within
    READING_TOL, no restart, no loss of accuracy at the end, lag sums within 1e-3 of a direct
    computation."""
    r = c_port.long_session(cfg, lib)
    assert r["same_times"] and r["restarts"] == 0
    assert r["bpm_diff_max"] <= c_port.READING_TOL["bpm"]
    assert r["confidence_diff_max"] <= c_port.READING_TOL["confidence"]
    assert r["mae_last_bpm"] <= r["mae_first_bpm"] + 0.5
    assert r["lag_sum_drift"] <= 1e-3


def test_selftest_passes(cfg, lib):
    """Self-test of the C port (c/tools/hr_selftest.c, product build) on the host with new
    cases: PASS, and its state without the input ring fits the RAM budget (c_port.BUDGET)."""
    c_port.write_selftest(cfg)
    out = subprocess.run(["make", "-s", "-C", str(c_port.C_DIR), "selftest"],
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.rstrip().endswith("hr_selftest: PASS"), out.stdout
    sizes = re.search(r"state (\d+) bytes, input ring (\d+)", out.stdout)
    assert int(sizes.group(1)) - int(sizes.group(2)) <= c_port.BUDGET["ram_bytes"]


def test_m4_budget(cfg, lib):
    """Cortex-M4 build (make m4): state without the input ring and the deepest stack path of
    hr_process_chunk (firmware calls; includes hr_process_block) within c_port.BUDGET."""
    if not shutil.which("clang"):
        pytest.skip("no clang for the Cortex-M4 build")
    out = subprocess.run(["make", "-s", "-C", str(c_port.C_DIR), "m4"], capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    state_bytes = int(re.search(r"(\d+) B hr_state_size_probe", out.stdout).group(1))
    state_bytes -= int(re.search(r"(\d+) B hr_input_ring_probe", out.stdout).group(1))
    stack = c_port.m4_stack()
    assert state_bytes <= c_port.BUDGET["ram_bytes"]
    assert stack["bytes"] <= c_port.BUDGET["stack_bytes"], stack


def test_readings_match_golden(cfg, lib):
    """C readings on every test recording: the frozen Python reading times, BPM and confidence
    within RECORDING_TOL."""
    if not needs_data(cfg):
        pytest.skip("local data or golden readings not available")
    golden = json.loads(golden_file(cfg).read_text())
    stats = c_port.compare_recordings(cfg, lib, golden)
    assert stats["same_times"] == stats["recordings"]
    assert stats["bpm_max"] <= c_port.RECORDING_TOL["bpm"]
    assert stats["confidence_max"] <= c_port.RECORDING_TOL["confidence"]
