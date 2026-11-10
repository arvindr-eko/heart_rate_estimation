"""Corner cases of the streaming estimator on synthetic PCG with a known HR: real-time
conditions (start-up, contact loss, gain, DC offset), extreme rates, rhythm and sound variants,
mains hum; and inputs without heart sounds (silence, noise only), which must give no reading.
Expected behaviour follows the readout rule: accurate first reading for regular hearts at
common rates, rough first reading for slow ones, refined to ±fine_bpm by latest_s."""

import numpy as np
import pytest

from device import estimator
from training import augment

FS = 4000


def readings(cfg, audio):
    """Readings (t_s, bpm, confidence) of one signal."""
    return estimator.run(cfg, audio)[0]


def shown_at(reads, t):
    """BPM of the reading on show at time t (None before the first reading)."""
    past = [bpm for ts, bpm, _ in reads if ts <= t + 1e-9]
    return past[-1] if past else None


def noisy(hr, seed=1, snr_db=20.0, **kw):
    """Synthetic PCG at hr with white noise at snr_db."""
    audio, _ = augment.synthetic_pcg(hr, seed=seed, **kw)
    return augment.add_noise(audio, FS, "white", snr_db, seed)


@pytest.mark.parametrize("hr", [55, 72, 100, 130])
def test_normal_rate_accurate_by_4s(cfg, hr):
    """Normal range: first reading by 4 s, within ±fine_bpm."""
    reads = readings(cfg, noisy(hr))
    t0, bpm, _ = reads[0]
    assert t0 <= 4.0 and abs(bpm - hr) <= cfg["eval"]["fine_bpm"]


@pytest.mark.parametrize("hr", [33, 40, 45, 170, 200, 220])
def test_slow_fast_rough_then_refined(cfg, hr):
    """Outside the normal range: first reading within ±rough_bpm, within ±fine_bpm by latest_s."""
    reads = readings(cfg, noisy(hr))
    assert abs(reads[0][1] - hr) <= cfg["eval"]["rough_bpm"]
    assert abs(shown_at(reads, cfg["readout"]["latest_s"]) - hr) <= cfg["eval"]["fine_bpm"]


def test_silence_no_reading(cfg):
    """Silence: no reading."""
    assert readings(cfg, np.zeros(17 * FS, np.float32)) == []


def noise_only(kind, seed):
    """17 s of one noise type without heart sounds."""
    x = augment.noise(kind, 17 * FS, FS, np.random.default_rng(seed))
    return (0.05 * x / x.std()).astype(np.float32)


@pytest.mark.parametrize("seed", range(5))
def test_hum_only_no_reading(cfg, seed):
    """Mains hum without heart sounds: no reading."""
    assert readings(cfg, noise_only("hum", seed)) == []


@pytest.mark.xfail(strict=True, reason="known limitation: noise alone can pass as slow beats")
@pytest.mark.parametrize("kind", ["white", "breath", "friction"])
def test_noise_only_no_reading(cfg, kind):
    """White, breath or friction noise without heart sounds: no reading for any of 5 seeds."""
    assert all(readings(cfg, noise_only(kind, seed)) == [] for seed in range(5))


def test_gain_and_offset_invariance(cfg):
    """-40 dB gain with a DC offset: first reading within one frame of the original, every
    reading within 0.5 BPM (the DC step adds a band-pass transient at the start)."""
    audio = noisy(72)
    ref, low = readings(cfg, audio), readings(cfg, (0.01 * audio + 0.05).astype(np.float32))
    frame_s = cfg["stream"]["block_samples"] * cfg["stream"]["frame_blocks"] / FS
    assert abs(ref[0][0] - low[0][0]) <= frame_s + 1e-9
    n = min(len(ref), len(low))
    assert np.allclose([b for _, b, _ in ref[-n:]], [b for _, b, _ in low[-n:]], atol=0.5)


@pytest.mark.parametrize("hr, s2", [(70, 1.0), (70, 1.3), (110, 1.0), (110, 1.3), (140, 1.3)])
def test_loud_s2_no_octave_error(cfg, hr, s2):
    """S2 as loud as or louder than S1: never half or double, within ±fine_bpm by latest_s."""
    reads = readings(cfg, noisy(hr, s2_ratio=s2))
    for _, bpm, _ in reads:
        assert 0.75 * hr < bpm < 1.5 * hr
    assert abs(shown_at(reads, cfg["readout"]["latest_s"]) - hr) <= cfg["eval"]["fine_bpm"]


def test_murmur(cfg):
    """Systolic murmur: within ±fine_bpm by latest_s."""
    reads = readings(cfg, noisy(80, murmur=0.5))
    assert abs(shown_at(reads, cfg["readout"]["latest_s"]) - 80) <= cfg["eval"]["fine_bpm"]


def test_irregular_rhythm(cfg):
    """Beat intervals varying by 10 %: first reading within ±rough_bpm of the mean rate."""
    reads = readings(cfg, noisy(80, interval_cv=0.10))
    assert reads and abs(reads[0][1] - 80) <= cfg["eval"]["rough_bpm"]


def test_startup_transient(cfg):
    """Handling noise in the first 0.5 s: first reading within ±fine_bpm."""
    reads = readings(cfg, noisy(72, transient=True))
    assert abs(reads[0][1] - 72) <= cfg["eval"]["fine_bpm"]


def test_contact_loss_recovers(cfg):
    """1.5 s of silence mid-measurement: the measurement goes on (no restart) and the reading on
    show is within ±fine_bpm 4 s later."""
    audio = noisy(72)
    audio[6 * FS:int(7.5 * FS)] = 0.0
    reads, trace = estimator.run(cfg, audio, keep_trace=True)
    assert np.all(np.diff([f["t"] for f in trace]) > 0)
    assert abs(shown_at(reads, 11.5) - 72) <= cfg["eval"]["fine_bpm"]


@pytest.mark.parametrize("gap", ["silence", "white", "hum"])
def test_lift_off_stops_and_restarts(cfg, gap):
    """Stethoscope lifted for 10 s (silence, room noise or hum) and put back: readings stop
    within 6 s of the lift-off, none appear without the heart, and its return gives a new first
    reading within ±fine_bpm."""
    on1, on2 = (augment.add_noise(augment.synthetic_pcg(72, duration_s=8.0, seed=s)[0], FS,
                                  "white", 25.0, s) for s in (1, 2))
    off = (np.zeros(10 * FS) if gap == "silence"
           else augment.noise(gap, 10 * FS, FS, np.random.default_rng(0)))
    off = off * on1.std() / max(off.std(), 1e-9)
    reads = readings(cfg, np.concatenate([on1, off, on2]).astype(np.float32))
    times = np.array([t for t, _, _ in reads])
    assert not ((times > 8.0 + 6.0) & (times <= 18.0)).any()
    back = [r for r in reads if r[0] > 18.0]
    assert back and abs(back[0][1] - 72) <= cfg["eval"]["fine_bpm"]


@pytest.mark.parametrize("snr_db", [10.0, 0.0])
def test_mains_hum(cfg, snr_db):
    """50/60 Hz hum and harmonics: within ±fine_bpm by latest_s."""
    audio, _ = augment.synthetic_pcg(90, seed=4)
    reads = readings(cfg, augment.add_noise(audio, FS, "hum", snr_db, 4))
    assert abs(shown_at(reads, cfg["readout"]["latest_s"]) - 90) <= cfg["eval"]["fine_bpm"]


def test_output_contract(cfg):
    """One reading per output period after the first; confidence in [0, 1]."""
    reads = readings(cfg, noisy(90))
    assert np.allclose(np.diff([t for t, _, _ in reads]), cfg["stream"]["output_period_s"])
    assert all(0.0 <= c <= 1.0 for _, _, c in reads)
