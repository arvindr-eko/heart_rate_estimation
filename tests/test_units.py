"""Unit checks on hand-written inputs with known outputs."""

import ast
from pathlib import Path

import numpy as np
import pandas as pd

from device import features, frontend, readout, rhythm, tracker
from training import augment, evaluate, labels


def test_hr_grid(cfg):
    """232 log-spaced states, 1 % apart, from 30 BPM to 298.8 BPM."""
    grid = features.hr_grid(cfg)
    assert grid.size == 232 and grid[0] == 30 and 298 < grid[-1] <= 300
    assert np.allclose(grid[1:] / grid[:-1], 1.01)


def test_stored_codes():
    """int16 storage: half a step rounds away from zero, beyond CODE_MAX steps saturates."""
    x = np.array([0.0, 0.124, 0.125, -0.125, 0.374, -0.375, 1e5, -1e5], np.float32)
    codes = features.stored(x, 4.0) * 4
    assert codes.tolist() == [0, 0, 1, -1, 1, -2, 32767, -32767]


def test_tolerance_and_octaves(cfg):
    """EC13: max(5 BPM, 10 %); half/double within 15 % of 0.5x or 2x."""
    ref = np.array([40.0, 100.0, 100.0, 100.0])
    est = np.array([45.0, 110.0, 50.0, 205.0])
    assert evaluate.is_within(cfg, est, ref).tolist() == [True, True, False, False]
    assert evaluate.is_half_double(cfg, est, ref).tolist() == [False, False, True, True]


def test_grading_groups_and_errors(cfg):
    """Readings graded against the ECG over the same seconds; only the graded group counts."""
    beats = {"a": np.arange(0.5, 17, 1.0), "b": np.arange(0.5, 17, 0.5), "c": np.arange(0, 17, 1.0)}
    rec = pd.DataFrame({
        "output_filename": ["a", "b", "c"],
        "readings": [[(3.0, 62.0, 1.0), (4.0, 61.0, 1.0)], [(3.0, 140.0, 1.0)],
                     [(3.0, 60.0, 1.0)]],
        "beats": [None, None, None], "case": ["", "", ""],
        "reference_bpm": [60.0, 120.0, 60.0], "efast_bpm": [60.0, 120.0, 30.0],
        "ecg_trusted": [True, True, True], "unheard": [False, False, True],
        "rhythm": ["Normal Sinus Rhythm"] * 3})
    per_rec, per_read = evaluate.grade(cfg, rec, {k: {"beats": v} for k, v in beats.items()})
    assert per_rec["group"].tolist() == ["graded", "graded", "unheard"]
    assert np.allclose(per_rec["first_err"], [2.0, 20.0, 0.0])
    assert per_rec["converge_s"].tolist()[:2] == [3.0, np.inf]
    s = evaluate.summarize(cfg, per_rec, per_read)
    assert s["all.n"] == 2 and s["all.first_within_fine"] == 0.5
    assert s["regular_common.later_within_fine"] == 1.0 and s["unheard.n"] == 1


def test_window_hr():
    """HR over the window; widened to the nearest beats for min_intervals; NaN when short."""
    beats = np.arange(0.3, 17, 1.0)
    assert abs(labels.window_hr(beats, 4.0, 4.0, 3) - 60) < 1e-9
    slow = np.array([0.5, 2.0, 3.5, 5.0, 6.5])
    assert abs(labels.window_hr(slow, 3.0, 4.0, 3) - 40) < 1e-9
    assert np.isnan(labels.window_hr(np.array([1.0, 2.0]), 3.0, 4.0, 3))


def test_readout_rules(cfg):
    """Regular path at `confidence`; slow or irregular path not before slow_irregular_after_s,
    at slow_irregular_early_confidence until slow_irregular_relaxed_s; forced reading at latest_s
    only with enough mass; no reading without periodicity."""
    rd, dc, grid = cfg["readout"], cfg["decision"], features.hr_grid(cfg)
    cal = readout.Calibration(fine=np.array([-5.0, 10.0, 0.0]), rough=np.array([-5.0, 10.0, 0.0]))

    def posterior(hr, spread_bpm):
        """Uniform posterior mass over the states within ±spread_bpm of hr."""
        p = (np.abs(grid - hr) <= spread_bpm).astype(float)
        return p / p.sum()

    def due(post, hr, t, cv=0.02, acf=0.6, frame_hr=None, sd=0.2):
        """first_due for one frame."""
        a = readout.assess(cfg, cal, post, grid, hr, hr if frame_hr is None else frame_hr, cv, acf)
        return readout.first_due(cfg, a, acf, sd, hr, t)

    assert due(posterior(72, 1), 72, 3.0)
    assert not due(posterior(72, 1), 72, dc["min_s"] - 0.25)
    assert not due(posterior(72, 1), 72, 3.0, frame_hr=90)
    assert not due(posterior(72, 1), 72, 3.0, acf=0.0)
    assert not due(posterior(72, 1), 72, 3.0, sd=dc["min_activation_sd"] - 0.01)
    assert not due(posterior(72, 8), 72, 3.0)
    assert not due(posterior(40, 1), 40, rd["slow_irregular_after_s"] - 0.25)
    assert due(posterior(40, 1), 40, rd["slow_irregular_after_s"])
    assert not due(posterior(72, 1), 72, rd["slow_irregular_after_s"] - 0.25, cv=0.2)
    assert due(posterior(72, 8), 72, rd["latest_s"])
    assert not due(np.full(grid.size, 1 / grid.size), 72, rd["latest_s"])
    # slow/irregular: early confidence until slow_irregular_relaxed_s, then the relaxed one
    a = {"path": "slow_irregular", "rough_mass": 1.0,
         "p_rough": (dc["slow_irregular_early_confidence"] + dc["slow_irregular_confidence"]) / 2}
    early_t = rd["slow_irregular_after_s"]
    assert early_t < dc["slow_irregular_relaxed_s"]
    assert not readout.first_due(cfg, a, 0.6, 0.2, 40, early_t)
    assert readout.first_due(cfg, {**a, "p_rough": dc["slow_irregular_early_confidence"]}, 0.6,
                             0.2, 40, early_t)
    assert readout.first_due(cfg, a, 0.6, 0.2, 40, dc["slow_irregular_relaxed_s"])


def test_heart_presence_and_loss(cfg):
    """Presence gates, hysteresis of absence, restart only after lost_after_s of absence and
    only once a reading was given."""
    dc = cfg["decision"]
    assert readout.heart_present(cfg, dc["min_periodicity"], dc["min_activation_sd"])
    assert not readout.heart_present(cfg, dc["min_periodicity"] - 0.01, 0.2)
    between = (dc["lost_periodicity"] + dc["min_periodicity"]) / 2
    assert not readout.heart_present(cfg, between, 0.2)
    assert not readout.heart_absent(cfg, between, 0.2)          # hysteresis: still measuring
    assert readout.heart_absent(cfg, dc["lost_periodicity"] - 0.01, 0.2)
    assert readout.heart_absent(cfg, 0.6, dc["min_activation_sd"] - 0.001)
    frame_s = 0.25
    for lost_after_s, frames in ((2.0, 8), (2.1, 9), (0.25, 1)):
        c = {**cfg, "decision": {**dc, "lost_after_s": lost_after_s}}
        assert readout.lost_frames(c, frame_s) == frames
    n = readout.lost_frames(cfg, frame_s)
    state = readout.ReadoutState()
    assert not any(readout.lost(state, cfg, True, frame_s) for _ in range(2 * n))  # no reading
    state.first_at = 2.0
    assert [readout.lost(state, cfg, True, frame_s) for _ in range(n - 1)] == [False] * (n - 1)
    readout.lost(state, cfg, False, frame_s)                     # a heartbeat resets the count
    assert not readout.lost(state, cfg, True, frame_s)
    assert [readout.lost(state, cfg, True, frame_s) for _ in range(n - 1)][-1]


def test_rhythm_beats():
    """Beat picking keeps one peak per period (S2 suppressed); CV and beat-count HR."""
    act = np.zeros(400)
    for start in range(10, 400, 80):            # beats every 0.8 s at 100 Hz = 75 BPM
        act[start] = 1.0
        act[start + 30] = 0.5                    # S2-like peak 0.3 s later
    beats = rhythm.pick_beats(act, 80.0, 0.4, 0.6)
    assert beats.tolist() == list(range(10, 400, 80))
    cv, hr = rhythm.analyse(act, 100.0, 75.0, {"peak_min_rel": 0.4, "refractory_rel": 0.6,
                                               "min_intervals": 2})
    assert abs(cv) < 1e-12 and abs(hr - 75.0) < 1e-9


def test_tracker_estimate(cfg):
    """A posterior concentrated on one state gives that state's HR."""
    trk = tracker.init(cfg, 0.25)
    trk.posterior = np.zeros(trk.hr.size)
    trk.posterior[100] = 1.0
    assert abs(tracker.estimate(trk) - trk.hr[100]) < 1e-3


def _source_features(cfg: dict, x: np.ndarray) -> np.ndarray:
    """Voicing and impulsiveness rows of the front end over a whole signal."""
    fe = frontend.init(cfg)
    out = np.hstack([frontend.process(fe, x[i:i + 200]) for i in range(0, x.size - 199, 200)])
    return out[-2:]


def test_voicing_and_impulsiveness(cfg):
    """Voicing near 1 on pitched sounds (120 and 220 Hz harmonics), below 0.3 on S1 and S2
    bursts (45 and 70 Hz); impulsiveness of a 2 ms click above that of S1."""
    fs = 4000
    t = np.arange(fs) / fs
    vowel = sum(np.sin(2 * np.pi * 120 * k * t) / k for k in range(1, 8)) * 0.05
    high = sum(np.sin(2 * np.pi * 220 * k * t) / k for k in range(1, 6)) * 0.05
    s1 = np.zeros(fs)
    s1[1000:1400] = np.hanning(400) * np.sin(2 * np.pi * 45 * np.arange(400) / fs)
    s2 = np.zeros(fs)
    s2[1000:1320] = np.hanning(320) * np.sin(2 * np.pi * 70 * np.arange(320) / fs)
    click = np.zeros(fs)
    click[2000:2008] = np.random.default_rng(0).standard_normal(8)
    v = {name: _source_features(cfg, x.astype(np.float32))
         for name, x in (("vowel", vowel), ("high", high), ("s1", s1), ("s2", s2),
                         ("click", click))}
    assert v["vowel"][0, 25:75].mean() > 0.95 and v["high"][0, 25:75].mean() > 0.95
    assert v["s1"][0, 25:35].mean() < 0.3 and v["s2"][0, 25:33].mean() < 0.3
    assert v["click"][1, 50] > v["s1"][1, 25:35].mean() + 0.2


def test_voicing_against_loops():
    """Voicing equals a plain-loop NCCF peak search on random blocks (forward lag pairs where
    the block holds them, backward otherwise)."""
    rng = np.random.default_rng(3)
    lags = np.arange(10, 49)
    for _ in range(5):
        x = rng.standard_normal(200).astype(np.float32)
        got = frontend.voicing(x, 40, lags)
        for f in range(5):
            s, ncc = f * 40, []
            for lag in lags:
                b0 = s + lag if s + 40 + lag <= 200 else s - lag
                num = sum(float(x[s + i]) * float(x[b0 + i]) for i in range(40))
                ea = sum(float(x[s + i]) ** 2 for i in range(40))
                eb = sum(float(x[b0 + i]) ** 2 for i in range(40))
                ncc.append(num / np.sqrt(ea * eb))
            peaks = [ncc[k] for k in range(1, len(ncc) - 1)
                     if ncc[k] > ncc[k - 1] and ncc[k] >= ncc[k + 1]]
            assert abs(got[f] - max(0.0, max(peaks, default=0.0))) < 1e-5


def test_time_warp(cfg):
    """Warping 70 BPM by 0.6 gives 42 BPM beats; a smooth input stays smooth across the joins
    (a misplaced join of a 7 Hz sine jumps by many times its largest step)."""
    audio, s1 = augment.synthetic_pcg(70, seed=1, interval_cv=0.0)
    beats = s1[s1 > 0.05] - 0.05
    out, new_beats = augment.time_warp(audio, 4000, beats, 0.6, 0.0, cfg,
                                       np.random.default_rng(0))
    assert abs(60 / np.diff(new_beats).mean() - 42.0) < 0.1
    sine = np.sin(2 * np.pi * 7.0 * np.arange(audio.size) / 4000).astype(np.float32)
    out = augment.time_warp(sine, 4000, beats, 0.6, 0.0, cfg, np.random.default_rng(0))[0]
    assert np.abs(np.diff(out)).max() <= np.abs(np.diff(sine)).max() + 1e-6


def test_device_never_imports_training():
    """The on-device package depends on nothing in training (it ports to C on its own)."""
    for path in (Path(__file__).resolve().parents[1] / "src" / "device").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                     else [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            assert not any(n.split(".")[0] == "training" for n in names), path.name
