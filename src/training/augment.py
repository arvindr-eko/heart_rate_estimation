"""Synthetic PCG, noise at a set SNR, and training-data augmentation (training split only).

Synthetic PCG: S1 and S2 as windowed sinusoids at a known HR, for unit tests and stress cases.
Noise: white, friction, hum, breath, taps, handling, scaled to an in-band SNR.
Augmentation: beat-synchronous time warping, speed-up to very fast rates, rhythm jitter, gain,
spectral tilt, noise. Time warping keeps each cycle's first keep_s after the R-peak (S1, systole,
S2) and resamples only the diastole, as HR changes mostly shorten or lengthen diastole; rates
above about 200 BPM need a shorter systole too, so they are made by speeding up the whole
signal (sound frequencies scale with it). Linear interpolation keeps the
segment end samples, so joins are sample-continuous (no clicks at beat times).
"""

import numpy as np
import pandas as pd
from scipy import signal

SNR_BAND_HZ = (25, 400)
NO_HEART_PEAK = 0.3       # no-heart clips: noise peak (full scale 1.0)
NO_HEART_FLOOR = 0.003    # and the SD of the room floor under it


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


def synthetic_pcg(hr_bpm: float, duration_s: float = 17.0, fs: int = 4000, seed: int = 0,
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


def events(n: int, fs: int, rate_hz: float, shape, rng: np.random.Generator) -> np.ndarray:
    """Transients at random times (exponential gaps, mean 1 / rate_hz) and random amplitudes;
    shape(rng) returns one event's samples."""
    x, t = np.zeros(n + fs), rng.exponential(1.0 / rate_hz)
    while t < n / fs:
        ev = rng.uniform(0.3, 3.0) * shape(rng)
        i = int(t * fs)
        x[i:i + ev.size] += ev[: x.size - i]
        t += 0.1 + rng.exponential(1.0 / rate_hz)
    return x[:n]


def tap(fs: int, rng: np.random.Generator) -> np.ndarray:
    """Finger tap on the chest piece: 2 ms broadband click and a damped diaphragm ring
    (40-200 Hz, decay 10-40 ms)."""
    t = np.arange(int(0.15 * fs)) / fs
    ring = np.exp(-t / rng.uniform(0.01, 0.04)) * np.sin(2 * np.pi * rng.uniform(40, 200) * t)
    ring[: int(0.002 * fs)] += rng.standard_normal(int(0.002 * fs))
    return ring


def thump(fs: int, rng: np.random.Generator) -> np.ndarray:
    """Handling thump: low-frequency damped pulse (10-40 Hz, decay 30-100 ms)."""
    t = np.arange(int(0.3 * fs)) / fs
    return np.exp(-t / rng.uniform(0.03, 0.1)) * np.sin(2 * np.pi * rng.uniform(10, 40) * t)


def noise(kind: str, n: int, fs: int, rng: np.random.Generator) -> np.ndarray:
    """Noise of one type: white, friction (bursts), hum (mains + harmonics), breath, taps (finger
    taps on the chest piece, about 1.5 per s), rhythmic_taps (taps at a steady 50-110 per min),
    handling (thumps and friction), scratch (fingernail on the diaphragm: 300-1500 Hz strokes,
    about 3 per s), speech (talking nearby: 100-400 Hz noise at a 3-6 Hz syllable rate)."""
    t = np.arange(n) / fs
    if kind == "taps":
        return events(n, fs, 1.5, lambda r: tap(fs, r), rng)
    if kind == "rhythmic_taps":
        x, period = np.zeros(n + fs), 60.0 / rng.uniform(50, 110)
        for t0 in np.arange(rng.uniform(0, period), n / fs, period):
            ev = rng.uniform(0.7, 1.3) * tap(fs, rng)
            i = int((t0 + 0.03 * period * rng.standard_normal()) * fs)
            x[max(i, 0):max(i, 0) + ev.size] += ev
        return x[:n]
    if kind == "handling":
        return (events(n, fs, 1.0, lambda r: thump(fs, r), rng)
                + 0.5 * noise("friction", n, fs, rng))
    if kind == "scratch":
        gate = np.zeros(n)
        for start in rng.uniform(0, n / fs, size=max(1, int(3 * n / fs))):
            i, length = int(start * fs), int(rng.uniform(0.08, 0.25) * fs)
            gate[i:i + length] = rng.uniform(0.5, 2.0) * np.hanning(len(gate[i:i + length]))
        return band_noise(n, fs, (300, 1500), rng) * gate
    if kind == "speech":
        rate = rng.uniform(3.0, 6.0)
        syllables = np.maximum(np.sin(2 * np.pi * rate * t + rng.uniform(0, 2 * np.pi)), 0) ** 2
        return band_noise(n, fs, (100, 400), rng) * syllables
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


def channel(x: np.ndarray, fs: int, cfg: dict) -> np.ndarray:
    """Synthetic sound as the device records it: linear-phase FIR with the measured channel
    magnitude (augment.channel_hz / channel_db), group delay removed."""
    ag = cfg["augment"]
    gain = 10 ** (np.asarray(ag["channel_db"], float) / 20)
    taps = signal.firwin2(255, np.asarray(ag["channel_hz"], float), gain, fs=fs)
    return signal.fftconvolve(x, taps, mode="same")


def no_heart_clip(cfg: dict, kind: str, n: int, rng: np.random.Generator) -> np.ndarray:
    """No heart sound, as recorded: n samples of one noise kind (peak NO_HEART_PEAK) over a
    faint room floor (NO_HEART_FLOOR), through the device channel."""
    fs = cfg["stream"]["fs_hz"]
    x = noise(kind, n, fs, rng)
    x = (x / max(np.abs(x).max(), 1e-9) * NO_HEART_PEAK
         + NO_HEART_FLOOR * band_noise(n, fs, (20, 1000), rng))
    return channel(x, fs, cfg)


def lift_off(cfg: dict, audio: np.ndarray, at_s: float, off_s: float, kind: str, level: float,
             rng: np.random.Generator) -> np.ndarray:
    """Recording with the stethoscope lifted at at_s for off_s: noise of one kind (silence:
    zeros) at level x the SD of the recording before at_s, through the device channel; then the
    rest of the recording."""
    fs = cfg["stream"]["fs_hz"]
    i, n_off = int(at_s * fs), int(off_s * fs)
    off = np.zeros(n_off) if kind == "silence" else channel(noise(kind, n_off, fs, rng), fs, cfg)
    off = off / max(off.std(), 1e-12) * audio[:i].std() * level
    return np.concatenate([audio[:i], off, audio[i:]]).astype(np.float32)


def murmur(cfg: dict, audio: np.ndarray, beats_s: np.ndarray, level: float,
           rng: np.random.Generator) -> np.ndarray:
    """Recording with a systolic murmur: 150-400 Hz noise from 40 ms after each beat (ECG
    R-peak) to the end of systole (shortening with HR as in synthetic_pcg), Hann-shaped, through
    the device channel, at level x the recording's SD."""
    fs = cfg["stream"]["fs_hz"]
    beats_s = np.asarray(beats_s, float)
    hr = 60.0 / np.median(np.diff(beats_s)) if beats_s.size > 1 else 70.0
    systole_s = float(np.clip(0.35 - 0.0012 * (hr - 60), 0.18, 0.40))
    gate = np.zeros(audio.size)
    for t1 in beats_s:
        i, j = int((t1 + 0.04) * fs), int((t1 + systole_s - 0.02) * fs)
        if 0 <= i < j <= audio.size:
            gate[i:j] = np.hanning(j - i)
    mur = channel(band_noise(audio.size, fs, (150, 400), rng) * gate, fs, cfg)
    mur = mur / max(mur.std(), 1e-12) * np.std(audio) * level
    return (audio + mur).astype(np.float32)


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


def regular(beats_s: np.ndarray, cfg: dict) -> bool:
    """Beat intervals regular enough to retime: cv and maximum deviation from the median
    bounded."""
    aug = cfg["augment"]
    rr = np.diff(beats_s)
    if rr.size < 3:
        return False
    deviation = np.max(np.abs(rr - np.median(rr)) / np.median(rr))
    return bool(rr.std() / rr.mean() <= aug["warp_rr_cv_max"]
                and deviation <= aug["warp_rr_dev_max"])


def sample_spec(rng: np.random.Generator, cfg: dict, ref_bpm: float, eligible: bool) -> dict:
    """Random augmentation settings for one copy."""
    aug = cfg["augment"]
    spec = {"seed": int(rng.integers(2 ** 31)), "hr_factor": 1.0, "jitter_cv": 0.0,
            "speed": 1.0, "noise": None, "snr_db": None,
            "gain_db": float(rng.uniform(*aug["gain_db"])), "tilt_alpha": None}
    lo_fast, hi_fast = aug["fast_target_bpm"]
    hi_fast = min(hi_fast, ref_bpm * aug["speed_max"])
    if eligible and hi_fast > lo_fast and rng.random() < aug["p_fast"]:
        target = float(np.exp(rng.uniform(np.log(lo_fast), np.log(hi_fast))))
        spec["speed"] = target / ref_bpm
    elif eligible and rng.random() < aug["p_warp"]:
        lo, hi = aug["slow_target_bpm"] if rng.random() < aug["p_slow"] else aug["target_bpm"]
        target = float(np.exp(rng.uniform(np.log(lo), np.log(hi))))
        spec["hr_factor"] = float(np.clip(target / ref_bpm, *aug["hr_factor_range"]))
        if rng.random() < aug["p_jitter"]:
            spec["jitter_cv"] = float(rng.uniform(*aug["jitter_cv"]))
    if rng.random() < aug["p_noise"]:
        spec["noise"] = str(rng.choice(aug["noise_types"]))
        spec["snr_db"] = float(rng.uniform(*aug["snr_db"]))
    if rng.random() < aug["p_tilt"]:
        spec["tilt_alpha"] = float(rng.uniform(*aug["tilt_alpha"]))
    return spec


def resample_segment(segment: np.ndarray, n_out: int) -> np.ndarray:
    """Linear resampling to n_out samples that keeps the first and last sample."""
    if n_out == segment.size or segment.size < 2:
        return segment
    return np.interp(np.linspace(0, segment.size - 1, n_out), np.arange(segment.size), segment)


def time_warp(audio: np.ndarray, fs: int, beats_s: np.ndarray, hr_factor: float,
              jitter_cv: float, cfg: dict, rng: np.random.Generator) -> tuple:
    """Warped audio and its beat times; cycle i gets R-R / (hr_factor x jitter_i)."""
    aug = cfg["augment"]
    idx = np.round(beats_s * fs).astype(int)
    idx = idx[(idx >= 0) & (idx < audio.size)]
    parts, new_beats, pos = [audio[:idx[0]]], [], idx[0]
    for start, end in zip(idx[:-1], idx[1:]):
        rr = end - start
        keep = int(min(aug["keep_s"] * fs, aug["keep_frac"] * rr))
        jitter = np.exp(rng.normal(0.0, jitter_cv)) if jitter_cv else 1.0
        rr_new = rr / (hr_factor * jitter)
        diastole_new = max(int(round(rr_new - keep)), int(aug["min_diastole_s"] * fs))
        parts += [audio[start:start + keep],
                  resample_segment(audio[start + keep:end], diastole_new)]
        new_beats.append(pos)
        pos += keep + diastole_new
    new_beats.append(pos)
    parts.append(audio[idx[-1]:])
    return np.concatenate(parts).astype(np.float32), np.array(new_beats) / fs


def speed_change(audio: np.ndarray, beats_s: np.ndarray, factor: float) -> tuple:
    """Whole signal played factor x faster (linear resampling): HR and sound frequencies both
    scale by factor."""
    n_out = int(audio.size / factor)
    out = np.interp(np.arange(n_out) * factor, np.arange(audio.size), audio)
    return out.astype(np.float32), beats_s / factor


def retime(cfg: dict, audio: np.ndarray, beats_s: np.ndarray, ref_bpm: float,
           target_bpm: float) -> tuple[np.ndarray, np.ndarray]:
    """Audio near target_bpm and its beat times, truncated to max_duration_s: diastole stretched
    for a slower target (time_warp; systole kept), whole signal sped up for a faster one."""
    fs = cfg["stream"]["fs_hz"]
    if target_bpm < ref_bpm:
        audio, beats_s = time_warp(audio, fs, beats_s, target_bpm / ref_bpm, 0.0, cfg,
                                   np.random.default_rng(0))
    else:
        audio, beats_s = speed_change(audio, beats_s, target_bpm / ref_bpm)
    n = int(cfg["data"]["max_duration_s"] * fs)
    return audio[:n], beats_s[beats_s < n / fs]


def tilt(audio: np.ndarray, fs: int, alpha: float, cutoff_hz: float) -> np.ndarray:
    """Spectral tilt: alpha x audio + (1 - alpha) x low-passed audio (attenuates highs)."""
    sos = signal.butter(1, cutoff_hz, btype="low", fs=fs, output="sos")
    return (alpha * audio + (1 - alpha) * signal.sosfilt(sos, audio)).astype(np.float32)


def apply(cfg: dict, audio: np.ndarray, beats_s: np.ndarray | None, ref_bpm: float,
          spec: dict) -> tuple[np.ndarray, float, np.ndarray | None]:
    """Augmented audio (max_duration_s), its reference HR (the original scaled by the ratio of
    beat spans before and after warping) and its beat times (warped with the audio)."""
    fs = cfg["stream"]["fs_hz"]
    rng = np.random.default_rng(spec["seed"])
    if spec["hr_factor"] != 1.0 or spec["jitter_cv"]:
        audio, new_beats = time_warp(audio, fs, beats_s, spec["hr_factor"], spec["jitter_cv"],
                                     cfg, rng)
        ref_bpm = ref_bpm * (beats_s[-1] - beats_s[0]) / (new_beats[-1] - new_beats[0])
        beats_s = new_beats
    if spec["speed"] != 1.0:
        audio, beats_s = speed_change(audio, beats_s, spec["speed"])
        ref_bpm = ref_bpm * spec["speed"]
    audio = audio * np.float32(10 ** (spec["gain_db"] / 20))
    if spec["tilt_alpha"] is not None:
        audio = tilt(audio, fs, spec["tilt_alpha"], cfg["augment"]["tilt_cutoff_hz"])
    if spec["noise"] is not None:
        audio = add_noise(audio, fs, spec["noise"], spec["snr_db"], spec["seed"])
    n = int(cfg["data"]["max_duration_s"] * fs)
    if beats_s is not None:
        beats_s = beats_s[beats_s < n / fs]
    return audio[:n], float(ref_bpm), beats_s


def plan(cfg: dict, df: pd.DataFrame, beats: dict) -> pd.DataFrame:
    """Training rows: each real recording (spec None) plus `copies` augmented copies; time
    warping only for trusted, regular ECG beats. A row carries its beats when the ECG is
    trusted (per-frame targets), None otherwise."""
    rng = np.random.default_rng(cfg["augment"]["seed"])
    copies = cfg["augment"]["copies"]
    rows = []
    for name, ref in zip(df["output_filename"], df["reference_bpm"]):
        info = beats[name]
        b = info["beats"] if info["trusted"] else None
        eligible = b is not None and regular(b, cfg)
        rows.append({"key": name, "output_filename": name, "reference_bpm": ref, "spec": None,
                     "beats": b})
        for k in range(copies):
            rows.append({"key": f"{name}#aug{k}", "output_filename": name, "reference_bpm": ref,
                         "spec": sample_spec(rng, cfg, ref, eligible), "beats": b})
    return pd.DataFrame(rows)
