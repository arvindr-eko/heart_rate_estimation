"""C port (c/) against the Python device reference (training.c_port): every block's output and
the readings on the synthetic cases, a 30 min session, the self-test on the host with the
state size against the RAM budget, and the readings on every test recording against the frozen
Python readings when the local data is available. Skipped without a C toolchain."""

import json
import re
import subprocess

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
    cases: PASS, and its state fits the RAM budget (configs/training.yml)."""
    c_port.write_selftest(cfg)
    out = subprocess.run(["make", "-s", "-C", str(c_port.C_DIR), "selftest"],
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.rstrip().endswith("hr_selftest: PASS"), out.stdout
    state_bytes = int(re.search(r"state (\d+) bytes", out.stdout).group(1))
    assert state_bytes <= cfg["budget"]["ram_bytes"]


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
