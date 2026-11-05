"""Streaming evaluation: run the estimator over recordings, score readings, write a run.

Recordings whose answer key is the ECG beat count (label_source ecg_rpeaks: more ECG beats than
heard) are excluded from the main metrics and summarised separately as sep_*.
"""

import json
import logging
import multiprocessing as mp
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from pcg_hr import cost_model, data, estimator, reference, synth

log = logging.getLogger(__name__)

_WORKER_CFG: dict = {}


def tolerance(cfg: dict, ref: np.ndarray) -> np.ndarray:
    """EC13 tolerance per reference value: max(tolerance_bpm, tolerance_rel * ref)."""
    ev = cfg["eval"]
    return np.maximum(ev["tolerance_bpm"], ev["tolerance_rel"] * ref)


def is_within(cfg: dict, hr: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """True where the estimate is inside the EC13 tolerance (NaN estimates are False)."""
    return np.abs(hr - ref) <= tolerance(cfg, ref)


def is_half_double(cfg: dict, hr: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """True where the estimate lies within half_double_rel of 0.5x or 2x the reference."""
    rel = cfg["eval"]["half_double_rel"]
    return (np.abs(hr - 0.5 * ref) <= rel * 0.5 * ref) | (np.abs(hr - 2 * ref) <= rel * 2 * ref)


def _init_worker(cfg: dict) -> None:
    """Store the config in the worker process."""
    _WORKER_CFG.clear()
    _WORKER_CFG.update(cfg)


def _run_one(task: tuple) -> dict:
    """Load (and for the stress set, corrupt) one recording, stream it, return its readings."""
    filename, stress = task
    cfg = _WORKER_CFG
    audio = data.load_wav(cfg, filename)
    if stress is not None:
        audio = synth.add_noise(audio, cfg["stream"]["fs_hz"], *stress)
    readings, _ = estimator.run(cfg, audio)
    return {"output_filename": filename, "stress": stress, "readings": readings}


def run_records(cfg: dict, df: pd.DataFrame, stress: list | None = None) -> pd.DataFrame:
    """Readings for every recording in df (parallel); stress is one (type, snr, seed) per row."""
    tasks = list(zip(df["output_filename"], stress or [None] * len(df)))
    results = []
    start = time.time()
    step = max(1, len(tasks) // 10)
    with mp.get_context("spawn").Pool(cfg["eval"]["workers"], _init_worker, (cfg,)) as pool:
        for i, res in enumerate(pool.imap(_run_one, tasks, chunksize=8), 1):
            results.append(res)
            if i % step == 0 or i == len(tasks):
                elapsed = time.time() - start
                log.info("%s: %d/%d recordings, %.0f s elapsed, %.0f s left", cfg["name"], i,
                         len(tasks), elapsed, elapsed / i * (len(tasks) - i))
    out = pd.DataFrame(results)
    out["reference_bpm"] = df["reference_bpm"].to_numpy()
    out["label_source"] = df["label_source"].to_numpy()
    return score_records(cfg, out)


def score_records(cfg: dict, rec: pd.DataFrame) -> pd.DataFrame:
    """Per-recording TTFR, first and steady-state (median) estimates, and their correctness."""
    def given(readings):
        """Readings that carry an HR."""
        return [(t, hr) for t, hr, _ in readings if not np.isnan(hr)]

    rec["ttfr_s"] = [given(r)[0][0] if given(r) else np.inf for r in rec["readings"]]
    rec["first_bpm"] = [given(r)[0][1] if given(r) else np.nan for r in rec["readings"]]
    rec["steady_bpm"] = [np.median([h for _, h in given(r)]) if given(r) else np.nan
                         for r in rec["readings"]]
    rec["n_given"] = [len(given(r)) for r in rec["readings"]]
    rec["n_given_within"] = [
        int(is_within(cfg, np.array([h for _, h in given(r)]), ref).sum()) if given(r) else 0
        for r, ref in zip(rec["readings"], rec["reference_bpm"])]
    ref = rec["reference_bpm"].to_numpy()
    for col in ("first", "steady"):
        est = rec[f"{col}_bpm"].to_numpy()
        rec[f"{col}_within"] = is_within(cfg, est, ref)
        rec[f"{col}_half_double"] = is_half_double(cfg, est, ref)
        rec[f"{col}_err"] = est - ref
    return rec


def accuracy_at(cfg: dict, rec: pd.DataFrame, t_s: float) -> float:
    """Share of all recordings whose latest HR reading at or before t_s is within tolerance."""
    hits = 0
    for readings, ref in zip(rec["readings"], rec["reference_bpm"]):
        given = [hr for t, hr, _ in readings if t <= t_s + 1e-9 and not np.isnan(hr)]
        hits += bool(given) and bool(is_within(cfg, np.array(given[-1]), ref))
    return hits / max(len(rec), 1)


def summarize(cfg: dict, rec: pd.DataFrame) -> dict:
    """Aggregate metrics of the main group, plus sep_n and sep_first_within for recordings
    labelled with the ECG beat count."""
    sep = rec["label_source"] == "ecg_rpeaks" if "label_source" in rec else np.zeros(len(rec), bool)
    main = rec[~sep]
    given = main[np.isfinite(main["ttfr_s"])]
    low = main["reference_bpm"] < cfg["targets"]["low_hr_bpm"]
    low_given = given["reference_bpm"] < cfg["targets"]["low_hr_bpm"]
    err = given["steady_err"]
    out = {
        "n": len(main),
        "coverage": len(given) / max(len(main), 1),
        "first_within": given["first_within"].mean(),
        "first_within_hr_ge_low": given.loc[~low_given, "first_within"].mean(),
        "first_within_low_hr": given.loc[low_given, "first_within"].mean(),
        "steady_within": given["steady_within"].mean(),
        "readings_within": given["n_given_within"].sum() / max(given["n_given"].sum(), 1),
        "first_half_double": given["first_half_double"].mean(),
        "steady_half_double": given["steady_half_double"].mean(),
        "first_mae": given["first_err"].abs().mean(),
        "steady_mae": err.abs().mean(),
        "steady_bias": err.mean(),
        "steady_loa": 1.96 * err.std(),
        "ttfr_median_s": float(np.median(main["ttfr_s"])),
        "ttfr_median_hr_ge_low_s": float(np.median(main.loc[~low, "ttfr_s"])),
        "ttfr_p95_hr_ge_low_s": float(np.quantile(main.loc[~low, "ttfr_s"], 0.95)),
        "ttfr_median_low_hr_s": float(np.median(main.loc[low, "ttfr_s"])) if low.any()
        else np.nan,
        "ttfr_max_s": float(main["ttfr_s"].max()),
        "sep_n": int(sep.sum()),
        "sep_first_within": float(rec.loc[sep, "first_within"].mean()) if sep.any() else np.nan,
    }
    out.update({f"acc_at_{t:g}s": accuracy_at(cfg, main, t) for t in cfg["eval"]["curve_times_s"]})
    return {k: float(v) for k, v in out.items()}


def check_targets(cfg: dict, summary: dict) -> dict:
    """Pass/fail per criterion of the targets section."""
    tg = cfg["targets"]
    return {
        "steady_within": summary["steady_within"] >= tg["steady_within"],
        "first_within": summary["first_within"] >= tg["first_within"],
        "half_double": max(summary["first_half_double"], summary["steady_half_double"])
        <= tg["half_double_max"],
        "coverage": summary["coverage"] >= tg["coverage_min"],
        "ttfr_median": summary["ttfr_median_hr_ge_low_s"] <= tg["ttfr_median_s"],
        "ttfr_p95": summary["ttfr_p95_hr_ge_low_s"] <= tg["ttfr_p95_s"],
        "ttfr_low_hr": not summary["ttfr_median_low_hr_s"] > tg["ttfr_low_hr_median_s"],
        "ttfr_max": summary["ttfr_max_s"] <= tg["ttfr_max_s"],
    }


def strata(rec: pd.DataFrame, meta: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """First-reading and steady-state accuracy, coverage and median TTFR per stratum."""
    joined = rec.merge(meta[["output_filename", *columns]], on="output_filename")
    rows = []
    for col in columns:
        for value, grp in joined.groupby(col, observed=True):
            given = grp[np.isfinite(grp["ttfr_s"])]
            rows.append({"stratum": col, "value": value, "n": len(grp),
                         "coverage": len(given) / len(grp),
                         "first_within": given["first_within"].mean(),
                         "steady_within": given["steady_within"].mean(),
                         "half_double": given["first_half_double"].mean(),
                         "ttfr_median_s": float(np.median(grp["ttfr_s"]))})
    return pd.DataFrame(rows)


def clean_window(beats_s: np.ndarray, t_s: float, ref_cfg: dict) -> bool:
    """Regular ECG beats in the reference window ending at t_s: at least 3 intervals, variation
    at most 8 %, none more than 15 % from the median."""
    inside = beats_s[(beats_s >= t_s - ref_cfg["window_s"]) & (beats_s <= t_s + 1e-9)]
    rr = np.diff(inside)
    if rr.size < 3:
        return False
    return bool(rr.std() / rr.mean() <= 0.08
                and np.max(np.abs(rr - np.median(rr)) / np.median(rr)) <= 0.15)


def windowed_scores(cfg: dict, rec: pd.DataFrame, beats: dict) -> dict:
    """First readings against the ECG HR over the reference window ending at the reading, on
    recordings with a trusted ECG reference and a clean window (main group only)."""
    ref_cfg = cfg["reference"]
    n = hits = 0
    for name, ttfr, first, ref, src in zip(rec["output_filename"], rec["ttfr_s"],
                                           rec["first_bpm"], rec["reference_bpm"],
                                           rec["label_source"]):
        b = beats[name]
        if (src == "ecg_rpeaks" or not np.isfinite(ttfr) or not reference.trusted(b, ref, ref_cfg)
                or not clean_window(b, ttfr, ref_cfg)):
            continue
        n += 1
        hits += bool(is_within(cfg, np.array(first),
                               np.array(reference.windowed_hr(b, ttfr, ref_cfg["window_s"]))))
    return {"clean_window_n": n, "clean_window_first_within": hits / max(n, 1)}


def write_run(cfg: dict, rec: pd.DataFrame, summary: dict, tag: str, split: str) -> Path:
    """Write config, per-recording results, readings, summary and run record under runs_dir."""
    run_id = f"{datetime.now():%Y%m%d_%H%M%S}_{cfg['name']}_{split}{'_' + tag if tag else ''}"
    out = cfg["paths"]["runs_dir"] / run_id
    out.mkdir(parents=True)
    plain_cfg = {**cfg, "paths": {k: str(v) for k, v in cfg["paths"].items()}}
    (out / "config.yml").write_text(yaml.safe_dump(plain_cfg, sort_keys=False))
    rec.drop(columns=["readings"]).to_csv(out / "per_record.csv", index=False)
    rec[["output_filename", "readings"]].to_json(out / "readings.json", orient="records",
                                                 double_precision=15)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    info = {"run_id": run_id, "variant": cfg["name"], "split": split, "tag": tag,
            "n_records": len(rec), "manifest": str(cfg["paths"]["manifest"]),
            "split_manifest": str(cfg["paths"]["split_manifest"]),
            "reference_labels": str(cfg["paths"]["reference_labels"]),
            "resources": cost_model.totals(cost_model.profile(cfg), cfg)}
    (out / "run_info.json").write_text(json.dumps(info, indent=1, default=float))
    return out
