"""Streaming evaluation harness: run an estimator over recordings, score readings, write a run."""

import json
import logging
import multiprocessing as mp
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from hr import estimator, io, reference, synth

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
    """Load (and optionally corrupt) one recording, stream it, return its readings."""
    filename, stress = task
    cfg = _WORKER_CFG
    audio = io.load_wav(cfg, filename)
    if stress is not None:
        audio = synth.add_noise(audio, cfg["stream"]["fs_hz"], *stress)
    readings, _ = estimator.run(cfg, audio)
    return {"output_filename": filename, "stress": stress, "readings": readings,
            "duration_s": audio.size / cfg["stream"]["fs_hz"]}


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
    if "label_source" in df:
        out["label_source"] = df["label_source"].to_numpy()
    return score_records(cfg, out)


def score_records(cfg: dict, rec: pd.DataFrame) -> pd.DataFrame:
    """Per-recording TTFR, first and steady-state estimates, and their correctness."""
    def first(readings):
        """First reading carrying an HR, else (inf, NaN)."""
        given = [(t, hr) for t, hr, _ in readings if not np.isnan(hr)]
        return given[0] if given else (np.inf, np.nan)

    firsts = rec["readings"].map(first)
    rec["ttfr_s"] = [f[0] for f in firsts]
    rec["first_bpm"] = [f[1] for f in firsts]
    rec["steady_bpm"] = rec["readings"].map(
        lambda r: np.nanmedian([hr for _, hr, _ in r]) if any(not np.isnan(h) for _, h, _ in r)
        else np.nan)
    rec["n_slots"] = rec["readings"].map(len)
    rec["n_given"] = rec["readings"].map(lambda r: sum(not np.isnan(h) for _, h, _ in r))
    rec["n_given_within"] = [
        int(is_within(cfg, np.array([h for _, h, _ in r]), ref).sum()) if r else 0
        for r, ref in zip(rec["readings"], rec["reference_bpm"])]
    ref = rec["reference_bpm"].to_numpy()
    for col in ("first", "steady"):
        est = rec[f"{col}_bpm"].to_numpy()
        rec[f"{col}_within"] = is_within(cfg, est, ref)
        rec[f"{col}_half_double"] = is_half_double(cfg, est, ref)
        rec[f"{col}_err"] = est - ref
    return rec


def add_windowed_scores(cfg: dict, rec: pd.DataFrame, beats: dict,
                        efast_ecg_bpm: dict) -> pd.DataFrame:
    """Per-recording scores against the ECG HR over the reference window ending at each
    reading; only recordings with a trusted ECG reference (reference.trusted) are scored."""
    ref_cfg = cfg["reference"]
    window = ref_cfg["window_s"]
    rec["ref_trusted"] = [reference.trusted(beats[f], efast_ecg_bpm[f], ref_cfg)
                          for f in rec["output_filename"]]
    first_ref, first_ok, n_ok = [], [], []
    for name, readings, ttfr, first, ok in zip(rec["output_filename"], rec["readings"],
                                               rec["ttfr_s"], rec["first_bpm"],
                                               rec["ref_trusted"]):
        b = beats[name]
        if not ok or not np.isfinite(ttfr):
            first_ref.append(np.nan)
            first_ok.append(False)
            n_ok.append(0)
            continue
        ref_first = reference.windowed_hr(b, ttfr, window)
        first_ref.append(ref_first)
        first_ok.append(bool(is_within(cfg, np.array(first), np.array(ref_first))))
        n_ok.append(sum(bool(is_within(cfg, np.array(h),
                                       np.array(reference.windowed_hr(b, t, window))))
                        for t, h, _ in readings if not np.isnan(h)))
    rec["win_first_ref_bpm"] = first_ref
    rec["win_first_within"] = first_ok
    rec["win_n_given_within"] = n_ok
    return rec


def windowed_summary(cfg: dict, rec: pd.DataFrame, beats: dict) -> dict:
    """Aggregate scores against the windowed ECG reference, trusted recordings only."""
    sub = rec[rec["ref_trusted"] & np.isfinite(rec["ttfr_s"])]
    low = sub["reference_bpm"] < cfg["targets"]["low_hr_bpm"]
    window = cfg["reference"]["window_s"]
    out = {"win_n": len(sub), "win_trusted_share": float(rec["ref_trusted"].mean()),
           "win_first_within": sub["win_first_within"].mean(),
           "win_first_within_hr_ge_low": sub.loc[~low, "win_first_within"].mean(),
           "win_first_within_low_hr": sub.loc[low, "win_first_within"].mean(),
           "win_readings_within": sub["win_n_given_within"].sum() / max(sub["n_given"].sum(), 1),
           "win_first_mae": float(np.nanmean(np.abs(sub["first_bpm"] - sub["win_first_ref_bpm"])))}
    for t_s in cfg["eval"]["curve_times_s"]:
        hits = 0
        for name, readings in zip(sub["output_filename"], sub["readings"]):
            given = [h for t, h, _ in readings if t <= t_s + 1e-9 and not np.isnan(h)]
            if given:
                ref_t = reference.windowed_hr(beats[name], t_s, window)
                hits += bool(is_within(cfg, np.array(given[-1]), np.array(ref_t)))
        out[f"win_acc_at_{t_s:g}s"] = hits / max(len(sub), 1)
    return {k: float(v) for k, v in out.items()}


def accuracy_at(cfg: dict, rec: pd.DataFrame, t_s: float) -> float:
    """Share of all recordings whose latest HR reading at or before t_s is within tolerance."""
    hits = 0
    for readings, ref in zip(rec["readings"], rec["reference_bpm"]):
        given = [hr for t, hr, _ in readings if t <= t_s + 1e-9 and not np.isnan(hr)]
        hits += bool(given) and bool(is_within(cfg, np.array(given[-1]), ref))
    return hits / max(len(rec), 1)


def summarize(cfg: dict, rec: pd.DataFrame) -> dict:
    """Aggregate metrics; recordings relabelled to the ECG beat count (more ECG beats than heard,
    label_source ecg_rpeaks) are excluded from the main metrics and reported as sep_*."""
    if "label_source" in rec and (rec["label_source"] == "ecg_rpeaks").any():
        sep = rec[rec["label_source"] == "ecg_rpeaks"]
        out = summarize(cfg, rec[rec["label_source"] != "ecg_rpeaks"].drop(columns="label_source"))
        out["sep_n"] = float(len(sep))
        out["sep_first_within"] = float(sep["first_within"].mean())
        return out
    given = rec[np.isfinite(rec["ttfr_s"])]
    low = rec["reference_bpm"] < cfg["targets"]["low_hr_bpm"]
    steady_err = given["steady_err"]
    out = {
        "n": len(rec),
        "coverage": len(given) / max(len(rec), 1),
        "first_within": given["first_within"].mean(),
        "steady_within": given["steady_within"].mean(),
        "readings_within": given["n_given_within"].sum() / max(given["n_given"].sum(), 1),
        "first_half_double": given["first_half_double"].mean(),
        "steady_half_double": given["steady_half_double"].mean(),
        "first_mae": given["first_err"].abs().mean(),
        "steady_mae": steady_err.abs().mean(),
        "steady_bias": steady_err.mean(),
        "steady_loa": 1.96 * steady_err.std(),
        "withheld_share": 1 - given["n_given"].sum() / max(given["n_slots"].sum(), 1),
        "ttfr_median_s": float(np.median(rec["ttfr_s"])),
        "ttfr_p90_s": float(np.quantile(rec["ttfr_s"], 0.9)),
        "ttfr_median_hr_ge_low_s": float(np.median(rec.loc[~low, "ttfr_s"])),
        "ttfr_p90_hr_ge_low_s": float(np.quantile(rec.loc[~low, "ttfr_s"], 0.9)),
        "ttfr_median_low_hr_s": float(np.median(rec.loc[low, "ttfr_s"])) if low.any() else np.nan,
        "ttfr_p95_hr_ge_low_s": float(np.quantile(rec.loc[~low, "ttfr_s"], 0.95)),
        "ttfr_p95_low_hr_s": float(np.quantile(rec.loc[low, "ttfr_s"], 0.95)) if low.any()
        else np.nan,
        "ttfr_max_s": float(rec["ttfr_s"].max()),
        "first_within_hr_ge_low": given.loc[given["reference_bpm"] >= cfg["targets"]["low_hr_bpm"],
                                            "first_within"].mean(),
        "first_within_low_hr": given.loc[given["reference_bpm"] < cfg["targets"]["low_hr_bpm"],
                                         "first_within"].mean(),
    }
    out.update({f"acc_at_{t:g}s": accuracy_at(cfg, rec, t) for t in cfg["eval"]["curve_times_s"]})
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


def strata(cfg: dict, rec: pd.DataFrame, meta: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """First-reading and steady-state accuracy, coverage and median TTFR per stratum."""
    joined = rec.merge(meta[["output_filename", *columns]], on="output_filename")
    rows = []
    for col in columns:
        for value, grp in joined.groupby(col):
            given = grp[np.isfinite(grp["ttfr_s"])]
            rows.append({"stratum": col, "value": value, "n": len(grp),
                         "coverage": len(given) / len(grp),
                         "first_within": given["first_within"].mean(),
                         "steady_within": given["steady_within"].mean(),
                         "half_double": given["first_half_double"].mean(),
                         "ttfr_median_s": float(np.median(grp["ttfr_s"]))})
    return pd.DataFrame(rows)


def write_run(cfg: dict, rec: pd.DataFrame, summary: dict, tag: str, split: str) -> Path:
    """Write config, per-recording results, summary and run record under runs_dir."""
    run_id = f"{datetime.now():%Y%m%d_%H%M%S}_{cfg['name']}_{split}{'_' + tag if tag else ''}"
    out = cfg["paths"]["runs_dir"] / run_id
    out.mkdir(parents=True)
    plain_cfg = {**cfg, "paths": {k: str(v) for k, v in cfg["paths"].items()}}
    (out / "config.yml").write_text(yaml.safe_dump(plain_cfg, sort_keys=False))
    rec.drop(columns=["readings"]).to_csv(out / "per_record.csv", index=False)
    rec[["output_filename", "readings"]].to_json(out / "readings.json", orient="records")
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    info = {"run_id": run_id, "variant": cfg["name"], "split": split, "tag": tag,
            "n_records": len(rec), "manifest": str(cfg["paths"]["manifest"]),
            "split_manifest": str(cfg["paths"]["split_manifest"]),
            "resources": estimator.resources(cfg), "git": "not under version control"}
    (out / "run_info.json").write_text(json.dumps(info, indent=1, default=float))
    return out
