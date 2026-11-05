"""End-to-end checks of the streaming estimator on synthetic PCG with known HR."""

import numpy as np
import pytest

from pcg_hr import estimator, evaluate, synth


@pytest.mark.parametrize("hr, latest_s, earliest_s", [(72, 4.0, 1.5), (150, 3.5, 1.5),
                                                      (45, 8.0, 3.0)])
def test_first_reading(cfg, hr, latest_s, earliest_s):
    """First reading within tolerance; answered in the expected time window (slow hearts wait
    beyond the default cutoff)."""
    for seed in (1, 2):
        audio, _ = synth.pcg(hr, seed=seed)
        readings, _ = estimator.run(cfg, synth.add_noise(audio, 4000, "white", 20.0, seed))
        t0, bpm, _ = readings[0]
        assert evaluate.is_within(cfg, np.array(bpm), np.array(float(hr)))
        assert earliest_s <= t0 <= latest_s


def test_readings_every_second(cfg):
    """After the first reading, one reading per output period."""
    audio, _ = synth.pcg(90, seed=3)
    readings, _ = estimator.run(cfg, audio)
    gaps = np.diff([t for t, _, _ in readings])
    assert np.allclose(gaps, cfg["release"]["output_period_s"])
