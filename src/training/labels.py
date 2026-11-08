"""ECG reference: R-peaks by consensus of established detectors over three leads, the HR over
the seconds of each reading, and the per-recording label table. Offline (non-causal) processing
of the ECG only; the ECG is never an estimator input.

Per lead, every detector in `detectors` (NeuroKit2 implementations of published methods) runs
on the polarity-corrected lead; a beat of the lead is a cluster of detections within match_tol_s
found by at least min_detectors detectors. A beat of the recording is found on at least
min_leads leads; a gap longer than gap_ratio x the median interval is filled with beats found on
a single lead (a beat missed where the other leads are noisy). The recording is trusted when at
least min_leads leads match the recording's beats (F1 >= lead_f1_min) and its beat intervals are
plausible.
"""

import json
import logging
import warnings
from pathlib import Path

import neurokit2 as nk
import numpy as np
import pandas as pd
import soundfile as sf

from training import data

log = logging.getLogger(__name__)

TABLE_COLUMNS = ["output_filename", "ecg_hr_bpm", "ecg_trusted", "ecg_leads_ok", "unheard"]
OWN_CLEANING = {"neurokit", "pantompkins1985", "hamilton2002", "elgendi2010"}  # others: neurokit


def ecg_filename(s3_key: str, suffix: str) -> str:
    """Local ECG file name for a manifest s3_key (PCG file name with the lead suffix)."""
    return Path(s3_key).name.replace(".wav", f"{suffix}.wav")


def detector_peaks(ecg: np.ndarray, fs: float, method: str) -> np.ndarray:
    """R-peak times (s) from one NeuroKit2 method, with that method's own cleaning where it has
    one; empty when the detector fails on the signal."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            clean = nk.ecg_clean(ecg, sampling_rate=fs,
                                 method=method if method in OWN_CLEANING else "neurokit")
            peaks = nk.ecg_peaks(clean, sampling_rate=fs, method=method)[1]["ECG_R_Peaks"]
    except Exception:  # a detector can fail on flat or very noisy leads: no detections
        return np.zeros(0)
    return np.asarray(peaks, dtype=float) / fs


def cluster(times_by_source: list, tol_s: float) -> tuple[np.ndarray, np.ndarray]:
    """Group pooled detection times (a new group starts more than tol_s after the group's first
    time); median time and number of distinct sources per group."""
    if not any(len(t) for t in times_by_source):
        return np.zeros(0), np.zeros(0, dtype=int)
    pooled = np.concatenate([np.asarray(t, float) for t in times_by_source])
    source = np.concatenate([np.full(len(t), i) for i, t in enumerate(times_by_source)])
    order = np.argsort(pooled)
    pooled, source = pooled[order], source[order]
    starts = [0]
    for i in range(1, pooled.size):
        if pooled[i] - pooled[starts[-1]] > tol_s:
            starts.append(i)
    bounds = list(zip(starts, starts[1:] + [pooled.size]))
    return (np.array([np.median(pooled[a:b]) for a, b in bounds]),
            np.array([np.unique(source[a:b]).size for a, b in bounds]))


def match_f1(a: np.ndarray, b: np.ndarray, tol_s: float) -> float:
    """F1 of two beat sequences: beats of a with a beat of b within tol_s, over both counts."""
    if a.size == 0 or b.size == 0:
        return 0.0
    matched = int((np.min(np.abs(a[:, None] - b[None, :]), axis=1) <= tol_s).sum())
    return 2.0 * matched / (a.size + b.size)


def fill_gaps(beats_s: np.ndarray, lead_beats: list, gap_ratio: float) -> np.ndarray:
    """Beats with every gap longer than gap_ratio x the median interval filled by the beats one
    lead found inside it, at least 0.3 median intervals from the gap ends."""
    rr = np.diff(beats_s)
    if rr.size < 3:
        return beats_s
    med = np.median(rr)
    extra = []
    for g in np.flatnonzero(rr > gap_ratio * med):
        lo, hi = beats_s[g] + 0.3 * med, beats_s[g + 1] - 0.3 * med
        inside = [lb[(lb > lo) & (lb < hi)] for lb in lead_beats]
        extra.append(max(inside, key=len))
    return np.sort(np.concatenate([beats_s, *extra])) if extra else beats_s


def plausible(beats_s: np.ndarray, labels_cfg: dict) -> bool:
    """At least min_beats beats and every R-R interval within rr_range_s."""
    rr = np.diff(beats_s)
    lo, hi = labels_cfg["rr_range_s"]
    return bool(beats_s.size >= labels_cfg["min_beats"] and rr.size and rr.min() >= lo
                and rr.max() <= hi)


def consensus(leads: list, fs: float, labels_cfg: dict) -> dict:
    """Recording beats, trust flag and number of agreeing leads from the leads' ECG signals."""
    lc = labels_cfg
    lead_beats = []
    for ecg in leads:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ecg, _ = nk.ecg_invert(ecg, sampling_rate=fs)
        times, n_det = cluster([detector_peaks(ecg, fs, m) for m in lc["detectors"]],
                               lc["match_tol_s"])
        lead_beats.append(times[n_det >= lc["min_detectors"]])
    times, n_leads = cluster(lead_beats, lc["match_tol_s"])
    beats = fill_gaps(times[n_leads >= lc["min_leads"]], lead_beats, lc["gap_ratio"])
    leads_ok = sum(match_f1(b, beats, lc["match_tol_s"]) >= lc["lead_f1_min"] for b in lead_beats)
    return {"beats": beats.round(4).tolist(), "leads_ok": int(leads_ok),
            "trusted": bool(leads_ok >= lc["min_leads"] and plausible(beats, lc))}


def _record(cfg: dict, task: tuple) -> tuple:
    """Consensus beats of one recording from its ECG files, truncated to max_duration_s."""
    output_filename, s3_key = task
    leads, fs = [], None
    for suffix in cfg["labels"]["leads"]:
        path = cfg["paths"]["ecg_dir"] / ecg_filename(s3_key, suffix)
        if path.is_file():
            ecg, fs = sf.read(path)
            leads.append(ecg[: int(cfg["data"]["max_duration_s"] * fs)])
    if not leads:
        return output_filename, {"beats": [], "leads_ok": 0, "trusted": False}
    return output_filename, consensus(leads, fs, cfg["labels"])


def build_beats(cfg: dict, df: pd.DataFrame) -> dict:
    """Consensus result {beats, leads_ok, trusted} per recording in df, cached as JSON."""
    cache = cfg["paths"]["cache_dir"] / "ecg_consensus.json"
    known = json.loads(cache.read_text()) if cache.is_file() else {}
    todo = [(f, k) for f, k in zip(df["output_filename"], df["s3_key"]) if f not in known]
    if todo:
        known.update(dict(data.map_records(cfg, _record, todo, "ECG consensus", chunksize=8)))
        cache.write_text(json.dumps(known))
    return {f: {**known[f], "beats": np.array(known[f]["beats"])} for f in df["output_filename"]}


def window_hr(beats_s: np.ndarray, t_end: float, window_s: float, min_intervals: int) -> float:
    """HR (BPM) from the beats in [t_end - window_s, t_end], widened to the nearest beats outside
    the window (earlier first on ties) until it holds min_intervals intervals; NaN without enough
    beats."""
    if beats_s.size < min_intervals + 1:
        return np.nan
    distance = np.maximum(t_end - window_s - beats_s, 0) + np.maximum(beats_s - t_end, 0)
    chosen = beats_s[distance == 0]
    if chosen.size < min_intervals + 1:
        chosen = np.sort(beats_s[np.lexsort((beats_s, distance))[:min_intervals + 1]])
    return 60.0 * (chosen.size - 1) / (chosen[-1] - chosen[0])


def full_hr(beats_s: np.ndarray) -> float:
    """HR (BPM) over the whole beat sequence; NaN with fewer than two beats."""
    if beats_s.size < 2 or beats_s[-1] <= beats_s[0]:
        return np.nan
    return 60.0 * (beats_s.size - 1) / (beats_s[-1] - beats_s[0])


def write_table(cfg: dict) -> pd.DataFrame:
    """Label table of every recording, written to paths.labels: consensus ECG HR, trust flag,
    agreeing leads, and `unheard` where the trusted ECG HR is at least unheard_ratio x the EFAST
    ECG HR (EFAST counting one beat per group of beats; those beats are often not heard)."""
    df = data.load_manifest({**cfg, "paths": {**cfg["paths"], "labels": None}})
    beats = build_beats(cfg, df)
    hr = np.array([full_hr(beats[f]["beats"]) for f in df["output_filename"]])
    trusted = np.array([beats[f]["trusted"] for f in df["output_filename"]])
    table = pd.DataFrame({
        "output_filename": df["output_filename"], "ecg_hr_bpm": hr.round(3),
        "ecg_trusted": trusted,
        "ecg_leads_ok": [beats[f]["leads_ok"] for f in df["output_filename"]],
        "unheard": trusted & (hr >= cfg["labels"]["unheard_ratio"] * df["efast_bpm"].to_numpy())})
    out = cfg["paths"]["labels"]
    out.parent.mkdir(parents=True, exist_ok=True)
    table[TABLE_COLUMNS].to_csv(out, index=False)
    log.info("labels: %d recordings, %d with a trusted ECG, %d unheard", len(table),
             int(table["ecg_trusted"].sum()), int(table["unheard"].sum()))
    return table


def external(cfg: dict, snapshot_dir: str) -> tuple[dict, pd.DataFrame]:
    """Config and recordings of another snapshot (manifest.csv, wavs/, ecg/) for evaluation
    only: its own ECG label table is built; recordings and clinician accounts of our training
    and validation splits are left out."""
    root = Path(snapshot_dir)
    paths = {**cfg["paths"], "manifest": root / "manifest.csv", "wav_dir": root / "wavs",
             "ecg_dir": root / "ecg",
             "labels": cfg["paths"]["labels"].with_name(f"{root.name}_ecg_labels.csv")}
    ext = {**cfg, "paths": paths}
    write_table(ext)
    df = data.load_manifest(ext)
    used = pd.read_csv(cfg["paths"]["split_manifest"])
    used = used[used["split"].isin(["train", "val"])]
    keep = (~df["user_id"].astype(str).isin(used["user_id"].astype(str))
            & ~df["output_filename"].isin(used["output_filename"]))
    log.info("snapshot %s: %d recordings, %d outside the training and validation accounts",
             root.name, len(df), int(keep.sum()))
    return ext, df[keep].reset_index(drop=True)
