"""Training-data augmentation of PCG: beat-synchronous time warping, rhythm jitter, gain,
spectral tilt and additive noise at a set SNR. Training splits only; never val or test.

Time warping keeps each cycle's first keep_s after the R-peak (S1, systole, S2) and resamples
only the diastole, as HR changes mostly shorten or lengthen diastole. Linear interpolation keeps
the segment end samples, so joins are sample-continuous (no clicks at beat times).
"""

import numpy as np
import pandas as pd
from scipy import signal

from pcg_hr import reference, synth


def warp_eligible(beats_s: np.ndarray, efast_ecg_bpm: float, cfg: dict) -> bool:
    """Trusted ECG beats with regular R-R intervals (cv and maximum deviation bounded)."""
    aug = cfg["augment"]
    if not reference.trusted(beats_s, efast_ecg_bpm, cfg["reference"]):
        return False
    rr = np.diff(beats_s)
    deviation = np.max(np.abs(rr - np.median(rr)) / np.median(rr))
    return bool(rr.std() / rr.mean() <= aug["warp_rr_cv_max"]
                and deviation <= aug["warp_rr_dev_max"])


def sample_spec(rng: np.random.Generator, cfg: dict, ref_bpm: float, eligible: bool) -> dict:
    """Random augmentation settings for one copy."""
    aug = cfg["augment"]
    spec = {"seed": int(rng.integers(2 ** 31)), "hr_factor": 1.0, "jitter_cv": 0.0,
            "noise": None, "snr_db": None, "gain_db": float(rng.uniform(*aug["gain_db"])),
            "tilt_alpha": None}
    if eligible and rng.random() < aug["p_warp"]:
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


def tilt(audio: np.ndarray, fs: int, alpha: float, cutoff_hz: float) -> np.ndarray:
    """Spectral tilt: alpha x audio + (1 - alpha) x low-passed audio (attenuates highs)."""
    sos = signal.butter(1, cutoff_hz, btype="low", fs=fs, output="sos")
    return (alpha * audio + (1 - alpha) * signal.sosfilt(sos, audio)).astype(np.float32)


def apply(cfg: dict, audio: np.ndarray, beats_s: np.ndarray, ref_bpm: float,
          spec: dict) -> tuple[np.ndarray, float]:
    """Augmented audio (max_duration_s) and its reference HR: the original HR scaled by the
    ratio of beat spans before and after warping."""
    fs = cfg["stream"]["fs_hz"]
    rng = np.random.default_rng(spec["seed"])
    if spec["hr_factor"] != 1.0 or spec["jitter_cv"]:
        audio, new_beats = time_warp(audio, fs, beats_s, spec["hr_factor"], spec["jitter_cv"],
                                     cfg, rng)
        ref_bpm = ref_bpm * (beats_s[-1] - beats_s[0]) / (new_beats[-1] - new_beats[0])
    audio = audio * np.float32(10 ** (spec["gain_db"] / 20))
    if spec["tilt_alpha"] is not None:
        audio = tilt(audio, fs, spec["tilt_alpha"], cfg["augment"]["tilt_cutoff_hz"])
    if spec["noise"] is not None:
        audio = synth.add_noise(audio, fs, spec["noise"], spec["snr_db"], spec["seed"])
    return audio[: int(cfg["data"]["max_duration_s"] * fs)], float(ref_bpm)


def plan(cfg: dict, df: pd.DataFrame, beats: dict | None = None) -> pd.DataFrame:
    """Training rows: each real recording (spec None) plus `copies` augmented copies when beats
    are given; real recordings only otherwise."""
    rng = np.random.default_rng(cfg["augment"]["seed"])
    copies = cfg["augment"]["copies"] if beats is not None else 0
    rows = []
    for name, ref in zip(df["output_filename"], df["reference_bpm"]):
        b = beats[name] if beats is not None else None
        eligible = copies > 0 and warp_eligible(b, ref, cfg)
        rows.append({"key": name, "output_filename": name, "reference_bpm": ref, "spec": None,
                     "beats": b})
        for k in range(copies):
            rows.append({"key": f"{name}#aug{k}", "output_filename": name, "reference_bpm": ref,
                         "spec": sample_spec(rng, cfg, ref, eligible), "beats": b})
    return pd.DataFrame(rows)
