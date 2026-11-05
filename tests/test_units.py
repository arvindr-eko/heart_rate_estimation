"""Unit checks on hand-written inputs with known outputs."""

import numpy as np
import pandas as pd

from pcg_hr import augment, evaluate, features, reference, release, synth, tracker


def test_hr_grid(cfg):
    """205 log-spaced states, 1 % apart, from 30 BPM."""
    grid = features.hr_grid(cfg)
    assert grid.size == 205 and grid[0] == 30
    assert np.allclose(grid[1:] / grid[:-1], 1.01)


def test_tolerance_and_octaves(cfg):
    """EC13: max(5 BPM, 10 %); half/double within 15 % of 0.5x or 2x."""
    ref = np.array([40.0, 100.0, 100.0, 100.0])
    est = np.array([45.0, 110.0, 50.0, 205.0])
    assert evaluate.is_within(cfg, est, ref).tolist() == [True, True, False, False]
    assert evaluate.is_half_double(cfg, est, ref).tolist() == [False, False, True, True]


def test_separate_group_excluded(cfg):
    """Recordings labelled with the ECG beat count are left out of the main metrics."""
    rec = pd.DataFrame({"output_filename": list("abcd"),
                        "readings": [[(3.0, 60.0, 1.0)], [(3.0, 100.0, 1.0)],
                                     [(3.0, 40.0, 1.0)], [(3.0, 70.0, 1.0)]],
                        "reference_bpm": [60.0, 100.0, 80.0, 90.0],
                        "label_source": ["efast", "efast", "ecg_rpeaks", "efast"]})
    s = evaluate.summarize(cfg, evaluate.score_records(cfg, rec))
    assert s["n"] == 3 and abs(s["first_within"] - 2 / 3) < 1e-12
    assert s["sep_n"] == 1 and s["sep_first_within"] == 0.0


def test_windowed_hr():
    """HR from the beats in the window; widened to two beats; NaN without two beats."""
    beats = np.arange(0.3, 17, 1.0)
    assert abs(reference.windowed_hr(beats, 4.0, 4.0) - 60) < 1e-9
    assert abs(reference.windowed_hr(np.array([0.5, 2.5, 4.5]), 1.0, 4.0) - 30) < 1e-9
    assert np.isnan(reference.windowed_hr(np.array([1.0]), 3.0, 4.0))


def test_release_threshold(cfg):
    """c_early before the cutoff, c_cutoff at the cutoff, 0 at max_s; slow hearts wait to 6 s."""
    rel = cfg["release"]
    assert release.threshold(rel, 2.0, slow=False) == rel["c_early"]
    assert abs(release.threshold(rel, rel["default_s"], slow=False) - rel["c_cutoff"]) < 1e-12
    assert release.threshold(rel, rel["max_s"], slow=False) == 0.0
    assert release.threshold(rel, 5.0, slow=True) == rel["c_early"]


def test_tracker_estimate(cfg):
    """A posterior concentrated on one state gives that state's HR with confidence 1."""
    trk = tracker.init(cfg, 0.25)
    trk.posterior = np.zeros(trk.hr.size)
    trk.posterior[100] = 1.0
    hr, conf = tracker.estimate(trk)
    assert abs(hr - trk.hr[100]) < 1e-9 and abs(conf - 1.0) < 1e-12


def test_time_warp(cfg):
    """Warping 70 BPM by 0.6 gives 42 BPM beats and sample-continuous joins."""
    audio, s1 = synth.pcg(70, seed=1, interval_cv=0.0)
    beats = s1[s1 > 0.05] - 0.05
    out, new_beats = augment.time_warp(audio, 4000, beats, 0.6, 0.0, cfg,
                                       np.random.default_rng(0))
    assert abs(60 / np.diff(new_beats).mean() - 42.0) < 0.1
    assert np.abs(np.diff(out)).max() <= np.abs(np.diff(audio)).max() + 1e-6
