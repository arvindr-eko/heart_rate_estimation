"""Corner cases of the streaming estimator on synthetic PCG with a known HR: real-time
conditions (start-up, contact loss, gain, DC offset), extreme rates, rhythm and sound variants,
mains hum; and inputs without heart sounds (silence, noise only), which must give no reading.
Expected behaviour follows the readout rule: accurate first reading for regular hearts at
common rates, rough first reading for slow ones, refined to ±fine_bpm by latest_s. Signals pass
through the device channel (augment.channel), as every recorded sound does."""

import numpy as np
import pytest

from device import estimator
from training import augment, config

FS = 4000
CFG = config.load()


def readings(cfg, audio):
    """Readings (t_s, bpm, confidence) of one signal."""
    return estimator.run(cfg, audio)[0]


def shown_at(reads, t):
    """BPM of the reading on show at time t (None before the first reading)."""
    past = [bpm for ts, bpm, _ in reads if ts <= t + 1e-9]
    return past[-1] if past else None


def noisy(hr, seed=1, snr_db=20.0, **kw):
    """Synthetic PCG at hr with white noise at snr_db, through the device channel."""
    audio, _ = augment.synthetic_pcg(hr, seed=seed, **kw)
    return augment.channel(augment.add_noise(audio, FS, "white", snr_db, seed), FS,
                           CFG).astype(np.float32)


@pytest.mark.parametrize("hr", [55, 72, 100, 130])
def test_normal_rate_accurate_by_5s(cfg, hr):
    """Normal range: first reading by 5 s, within ±fine_bpm."""
    reads = readings(cfg, noisy(hr))
    t0, bpm, _ = reads[0]
    assert t0 <= 5.0 and abs(bpm - hr) <= cfg["eval"]["fine_bpm"]


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
    """17 s of one noise type without heart sounds, as recorded (augment.no_heart_clip)."""
    x = augment.no_heart_clip(CFG, kind, 17 * FS, np.random.default_rng(seed))
    return x.astype(np.float32)


@pytest.mark.parametrize("seed", range(5))
def test_hum_only_no_reading(cfg, seed):
    """Mains hum without heart sounds: no reading."""
    assert readings(cfg, noise_only("hum", seed)) == []


@pytest.mark.parametrize("kind", ["white", "breath", "scratch"])
def test_noise_only_no_reading(cfg, kind):
    """Noise without heart sounds (room, breath, scratching): no reading for any of 5 seeds."""
    assert all(readings(cfg, noise_only(kind, seed)) == [] for seed in range(5))


@pytest.mark.parametrize("kind", ["taps", "handling", "friction"])
def test_transient_noise_rare_readings(cfg, kind):
    """Finger taps on the chest piece, handling, friction: at most one reading per 3 s of input
    over 5 seeds (the release gave about one per 2 s for taps and handling)."""
    n = sum(len(readings(cfg, noise_only(kind, seed))) for seed in range(5))
    assert n <= 5 * 17 / 3


@pytest.mark.parametrize("kind", [
    pytest.param("rhythmic_taps", marks=pytest.mark.xfail(
        strict=True, reason="known limitation: taps at a heart rate can pass as heartbeats")),
    "speech"])
def test_rhythmic_noise_no_reading(cfg, kind):
    """Rhythmic taps or speech without heart sounds: no reading for any of 5 seeds."""
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
    """1.5 s of silence mid-measurement (the measurement may end: contact loss is detected within
    about 2-3 s): a reading within ±fine_bpm is on show 4 s after the silence."""
    audio = noisy(72)
    audio[6 * FS:int(7.5 * FS)] = 0.0
    reads = readings(cfg, audio)
    assert abs(shown_at(reads, 11.5) - 72) <= cfg["eval"]["fine_bpm"]


@pytest.mark.parametrize("gap", ["silence", "white", "hum"])
def test_lift_off_stops_and_restarts(cfg, gap):
    """Stethoscope lifted for 10 s (silence, room noise or hum) and put back: readings stop
    within 4 s of the lift-off, none appear without the heart, and its return gives a new first
    reading within ±fine_bpm."""
    on1, on2 = (augment.channel(augment.add_noise(augment.synthetic_pcg(72, duration_s=8.0,
                                                                       seed=s)[0], FS, "white",
                                                  25.0, s), FS, CFG) for s in (1, 2))
    off = (np.zeros(10 * FS) if gap == "silence" else augment.channel(
        augment.noise(gap, 10 * FS, FS, np.random.default_rng(0)), FS, CFG))
    off = off * on1.std() / max(off.std(), 1e-9)
    reads = readings(cfg, np.concatenate([on1, off, on2]).astype(np.float32))
    times = np.array([t for t, _, _ in reads])
    assert not ((times > 8.0 + 4.0) & (times <= 18.0)).any()
    back = [r for r in reads if r[0] > 18.0]
    assert back and abs(back[0][1] - 72) <= cfg["eval"]["fine_bpm"]


@pytest.mark.parametrize("kind", ["silence", "white"])
def test_placement_while_running(cfg, kind):
    """Estimator running on 10 s of off-chest sound (silence or room noise), then the heart:
    first reading no more than one frame after a fresh start's, within ±fine_bpm (the analysis
    restarts while no heartbeat is heard)."""
    heart = noisy(72)
    off = (np.zeros(10 * FS) if kind == "silence" else augment.channel(
        augment.noise(kind, 10 * FS, FS, np.random.default_rng(0)), FS, CFG))
    off = off * heart.std() / max(off.std(), 1e-9)
    fresh = readings(cfg, heart)[0]
    placed = [r for r in readings(cfg, np.concatenate([off, heart]).astype(np.float32))
              if r[0] > 10.0][0]
    assert placed[0] - 10.0 <= fresh[0] + 0.25
    assert abs(placed[1] - 72) <= cfg["eval"]["fine_bpm"]


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
