"""Synthetic PCG with known HR, and noise mixing at a set in-band SNR for the stress set."""

import numpy as np
from scipy import signal

SNR_BAND_HZ = (25, 400)


def sound(fs: int, freq_hz: float, dur_s: float, rng: np.random.Generator) -> np.ndarray:
    """One heart sound: Hann-windowed sinusoid with a small random frequency spread."""
    t = np.arange(int(dur_s * fs)) / fs
    f = freq_hz * (1 + 0.1 * rng.standard_normal())
    return np.hanning(t.size) * np.sin(2 * np.pi * f * t + rng.uniform(0, 2 * np.pi))


def beat_times(hr_bpm: float, duration_s: float, interval_cv: float,
               rng: np.random.Generator) -> np.ndarray:
    """S1 times with a random start phase and Gaussian interval jitter (cv = std / mean)."""
    period = 60.0 / hr_bpm
    times = [rng.uniform(0, period)]
    while times[-1] < duration_s:
        times.append(times[-1] + period * max(0.3, 1 + interval_cv * rng.standard_normal()))
    return np.array(times[:-1])


def pcg(hr_bpm: float, duration_s: float = 17.0, fs: int = 4000, seed: int = 0,
        s2_ratio: float = 0.6, interval_cv: float = 0.02, murmur: float = 0.0,
        transient: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Synthetic PCG and its S1 times; systole shortens with HR as in adults."""
    rng = np.random.default_rng(seed)
    n = int(duration_s * fs)
    audio = np.zeros(n + fs)
    systole_s = float(np.clip(0.35 - 0.0012 * (hr_bpm - 60), 0.18, 0.40))
    s1_times = beat_times(hr_bpm, duration_s, interval_cv, rng)
    for t1 in s1_times:
        for t0, amp, freq, dur in ((t1, 1.0, 45.0, 0.10), (t1 + systole_s, s2_ratio, 70.0, 0.08)):
            snd = amp * sound(fs, freq, dur, rng)
            i = int(t0 * fs)
            audio[i:i + snd.size] += snd
        if murmur:
            i, j = int((t1 + 0.04) * fs), int((t1 + systole_s - 0.02) * fs)
            audio[i:j] += murmur * band_noise(j - i, fs, (150, 400), rng)
    if transient:
        audio[: fs // 2] += 5 * band_noise(fs // 2, fs, (20, 1000), rng) * np.hanning(fs // 2)
    return audio[:n].astype(np.float32), s1_times


def band_noise(n: int, fs: int, band_hz: tuple, rng: np.random.Generator) -> np.ndarray:
    """Unit-variance Gaussian noise band-limited to band_hz."""
    sos = signal.butter(4, band_hz, btype="band", fs=fs, output="sos")
    x = signal.sosfilt(sos, rng.standard_normal(n + fs))[fs:]
    return x / max(x.std(), 1e-12)


def noise(kind: str, n: int, fs: int, rng: np.random.Generator) -> np.ndarray:
    """Noise of one stress type: white, friction (bursts), hum (mains + harmonics), breath."""
    t = np.arange(n) / fs
    if kind == "white":
        return band_noise(n, fs, (20, 1000), rng)
    if kind == "friction":
        gate = np.zeros(n)
        for start in rng.uniform(0, n / fs, size=max(1, int(n / fs))):
            i, length = int(start * fs), int(rng.uniform(0.05, 0.3) * fs)
            gate[i:i + length] = rng.uniform(0.5, 2.0) * np.hanning(len(gate[i:i + length]))
        return band_noise(n, fs, (100, 1000), rng) * gate
    if kind == "hum":
        f0 = rng.choice([50.0, 60.0])
        return sum(np.sin(2 * np.pi * h * f0 * t + rng.uniform(0, 2 * np.pi)) / h
                   for h in (1, 2, 3))
    if kind == "breath":
        rate = rng.uniform(0.2, 0.33)
        envelope = np.sin(np.pi * rate * t + rng.uniform(0, np.pi)) ** 2
        return band_noise(n, fs, (100, 800), rng) * envelope
    raise ValueError(f"unknown noise type {kind}")


def band_power(x: np.ndarray, fs: int) -> float:
    """Mean power inside SNR_BAND_HZ."""
    sos = signal.butter(2, SNR_BAND_HZ, btype="band", fs=fs, output="sos")
    return float(np.mean(signal.sosfilt(sos, x) ** 2))


def add_noise(audio: np.ndarray, fs: int, kind: str, snr_db: float, seed: int) -> np.ndarray:
    """Audio plus noise scaled to snr_db, both powers measured in SNR_BAND_HZ."""
    rng = np.random.default_rng(seed)
    nz = noise(kind, audio.size, fs, rng)
    scale = np.sqrt(band_power(audio, fs) / max(band_power(nz, fs), 1e-20) / 10 ** (snr_db / 10))
    return (audio + scale * nz).astype(np.float32)


def stress_tasks(filenames: list[str], cfg: dict) -> tuple[list[str], list[tuple]]:
    """Every recording crossed with every (noise type, SNR); one seed per combination."""
    st = cfg["stress"]
    files, specs = [], []
    seed = st["seed"]
    for kind in st["noise_types"]:
        for snr in st["snr_db"]:
            for name in filenames:
                files.append(name)
                specs.append((kind, float(snr), seed))
                seed += 1
    return files, specs
