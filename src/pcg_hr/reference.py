"""ECG reference: R-peak times from the ECG lead and HR over the window of each reading.

Offline (non-causal) processing of the reference signal only; never part of the estimator.
"""

import json
import logging
import multiprocessing as mp
import time
from pathlib import Path

import numpy as np
import pandas as pd
import soundfile as sf
from scipy import signal

log = logging.getLogger(__name__)

_CFG: dict = {}


def ecg_filename(s3_key: str, suffix: str) -> str:
    """Local ECG file name for a manifest s3_key (PCG file name with the lead suffix)."""
    return Path(s3_key).name.replace(".wav", f"{suffix}.wav")


def detect_rpeaks(ecg: np.ndarray, fs: float, ref: dict) -> np.ndarray:
    """R-peak times (s): zero-phase band-pass, squared, peaks above peak_height_rel x the median
    of the stronger candidate peaks; second pass with the minimum spacing set to rr_frac x the
    median R-R of the first pass (suppresses T waves at low HR); refined to the |ECG| maximum."""
    sos = signal.butter(2, ref["band_hz"], btype="band", fs=fs, output="sos")
    filtered = signal.sosfiltfilt(sos, ecg)
    energy = filtered ** 2
    peaks, props = signal.find_peaks(energy, distance=int(ref["refractory_s"] * fs), height=0.0)
    if peaks.size < 3:
        return np.zeros(0)
    heights = props["peak_heights"]
    threshold = ref["peak_height_rel"] * np.median(heights[heights >= np.median(heights)])
    peaks = peaks[heights >= threshold]
    if peaks.size >= 3:
        spacing = np.clip(ref["rr_frac"] * np.median(np.diff(peaks)) / fs, ref["refractory_s"],
                          ref["max_spacing_s"])
        peaks, _ = signal.find_peaks(energy, distance=int(spacing * fs), height=threshold)
    half = int(ref["refine_s"] * fs)
    refined = [p - half + int(np.argmax(np.abs(filtered[max(0, p - half):p + half + 1])))
               if p >= half else p for p in peaks]
    return np.unique(refined) / fs


def windowed_hr(beats_s: np.ndarray, t_end: float, window_s: float) -> float:
    """HR (BPM) from the beats in [t_end - window_s, t_end]; widened backwards to the last two
    beats before t_end when the window holds fewer than two; NaN without two beats."""
    inside = beats_s[(beats_s >= t_end - window_s) & (beats_s <= t_end + 1e-9)]
    if inside.size < 2:
        before = beats_s[beats_s <= t_end + 1e-9]
        inside = before[-2:] if before.size >= 2 else beats_s[:2]
    if inside.size < 2 or inside[-1] <= inside[0]:
        return np.nan
    return 60.0 * (inside.size - 1) / (inside[-1] - inside[0])


def _init(cfg: dict) -> None:
    """Store the config in the worker."""
    _CFG.clear()
    _CFG.update(cfg)


def record_beats(task: tuple) -> tuple:
    """R-peaks of one recording's ECG lead, truncated to max_duration_s."""
    output_filename, s3_key = task
    cfg = _CFG
    ref = cfg["reference"]
    ecg, fs = sf.read(cfg["paths"]["ecg_dir"] / ecg_filename(s3_key, ref["ecg_suffix"]))
    ecg = ecg[: int(cfg["data"]["max_duration_s"] * fs)]
    return output_filename, detect_rpeaks(ecg, fs, ref).tolist()


def build_beats(cfg: dict, df: pd.DataFrame) -> dict:
    """R-peak times for every recording in df (parallel), cached as JSON by lead suffix."""
    cache = cfg["paths"]["cache_dir"] / f"rpeaks{cfg['reference']['ecg_suffix']}.json"
    beats = json.loads(cache.read_text()) if cache.is_file() else {}
    todo = [(f, k) for f, k in zip(df["output_filename"], df["s3_key"]) if f not in beats]
    start = time.time()
    if todo:
        with mp.get_context("spawn").Pool(cfg["eval"]["workers"], _init, (cfg,)) as pool:
            for i, (name, times) in enumerate(pool.imap(record_beats, todo, 16), 1):
                beats[name] = times
                if i % 500 == 0 or i == len(todo):
                    log.info("r-peaks: %d/%d recordings, %.0f s", i, len(todo),
                             time.time() - start)
        cache.write_text(json.dumps(beats))
    return {name: np.array(beats[name]) for name in df["output_filename"]}


def reliable(beats_s: np.ndarray, ref: dict) -> bool:
    """Plausible beat sequence: at least min_beats, and every RR interval within rr_range_s."""
    rr = np.diff(beats_s)
    lo, hi = ref["rr_range_s"]
    return bool(beats_s.size >= ref["min_beats"] and rr.size and rr.min() >= lo and rr.max() <= hi)


def trusted(beats_s: np.ndarray, efast_ecg_bpm: float, ref: dict) -> bool:
    """Reliable beats whose full-recording HR agrees with the EFAST ECG HR within agree_bpm:
    two independent ECG estimates agree, so the windowed reference is trusted."""
    if not reliable(beats_s, ref):
        return False
    hr = 60.0 * (beats_s.size - 1) / (beats_s[-1] - beats_s[0])
    return bool(abs(hr - efast_ecg_bpm) <= ref["agree_bpm"])


def full_hr(beats_s: np.ndarray) -> float:
    """HR (BPM) over the whole beat sequence; NaN with fewer than two beats."""
    if beats_s.size < 2 or beats_s[-1] <= beats_s[0]:
        return np.nan
    return 60.0 * (beats_s.size - 1) / (beats_s[-1] - beats_s[0])


def relabel(cfg: dict, df: pd.DataFrame) -> pd.DataFrame:
    """Corrected reference per recording: the ECG R-peak HR where every lead in consensus_leads
    gives a reliable beat sequence, the lead HRs agree within consensus_rel of their median, and
    that HR is at least relabel_min_ratio x the EFAST ECG HR (EFAST counting one beat per group
    of beats); EFAST otherwise. Lower R-peak counts are not used: the detector's spacing rule can
    suppress closely coupled beats."""
    ref = cfg["reference"]
    hrs, ok = [], np.ones(len(df), dtype=bool)
    for suffix in ref["consensus_leads"]:
        lead_cfg = {**cfg, "reference": {**ref, "ecg_suffix": suffix}}
        beats = build_beats(lead_cfg, df)
        hrs.append(np.array([full_hr(beats[f]) for f in df["output_filename"]]))
        ok &= np.array([reliable(beats[f], ref) for f in df["output_filename"]])
    hrs = np.vstack(hrs)
    median = np.median(hrs, axis=0)
    agree = ok & (np.nanmax(np.abs(hrs - median) / median, axis=0) <= ref["consensus_rel"])
    efast = df["reference_bpm"].to_numpy()
    use_ecg = agree & (median >= ref["relabel_min_ratio"] * efast)
    return pd.DataFrame({"output_filename": df["output_filename"], "efast_ecg_bpm": efast,
                         "ecg_rpeak_bpm": median, "leads_agree": agree,
                         "label_source": np.where(use_ecg, "ecg_rpeaks", "efast"),
                         "reference_bpm": np.where(use_ecg, median, efast)})
