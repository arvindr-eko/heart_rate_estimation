"""Evaluation on the streaming path, as on the device: run the estimator over recordings, grade
every reading against the ECG over the same seconds, summarise per bucket, check the
pass/fail targets, and write a run folder with figures.

Reference of a reading at time t: consensus ECG HR over [t - window_s, t], widened to at least
min_intervals beat intervals (labels.window_hr). Stress cases that change the rate carry their
own beat times.

Groups per recording; metrics and targets use `graded` only:
  graded   trusted ECG, beats heard
  unheard  trusted ECG HR >= unheard_ratio x the EFAST ECG HR (grouped beats, many not heard)
  other    ECG not trusted: first reading against the EFAST ECG HR only (EC13)
Buckets of the graded group, from the ECG (eval.buckets), each with its own targets:
  irregular       beat-interval CV >= regular_max_cv, or atrial fibrillation
  slow            regular, reference HR below slow_bpm
  regular_common  regular, slow_bpm to fast_bpm
  very_fast       regular, above fast_bpm (stress cases only)
`all` pools the graded group; `af` reports atrial fibrillation on its own.
"""

import hashlib
import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from matplotlib.figure import Figure

from training import config
from device import estimator, net, readout, scorer
from training import augment, c_port, data, labels

log = logging.getLogger(__name__)

BUCKETS = ("regular_common", "slow", "irregular", "very_fast")
BUCKET_COLORS = {"regular_common": "#2a78d6", "slow": "#eb6834", "irregular": "#1baf7a",
                 "very_fast": "#eda100"}  # categorical slots 1-4
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e3de"
OTHER_KEYS = ("n", "coverage", "ttfr_median_s", "first_ec13")  # metrics without an ECG reference
STRATA = ["hardware_type", "firmware_revision", "mains_filter", "rhythm", "hr_band_10",
          "efast_pcg_murmur_classification"]
STRESS = {                                    # stress protocol (hr-estimation evaluate --stress)
    "seed": 7,
    "noise_records": 200,                     # random recordings x noise types x SNRs; every
    "noise_types": ["white", "friction", "hum", "breath"],  # slow recording gets the same set
    "snr_db": [20, 10, 5, 0],
    "rate_targets_bpm": [32, 38, 44, 170, 190, 210, 230, 250, 280, 300],
    "rate_records": 40,                       # regular-beat recordings per rate target,
    "rate_snr_db": 10,                        # clean and with white noise at this SNR
    "slow_source_bpm": [55, 95],              # slow targets: diastole stretched from this range
    "rate_factor_max": 2.0,                   # fast targets: sped up by at most this factor
}


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


def interval_cv(beats_s: np.ndarray) -> float:
    """Beat-interval coefficient of variation (SD / mean); NaN with fewer than 3 intervals."""
    rr = np.diff(beats_s)
    return float(rr.std() / rr.mean()) if rr.size >= 3 else np.nan


def bucket_of(cfg: dict, hr: float, cv: float, af: bool) -> str:
    """Evaluation bucket of a graded recording (module docstring); an unknown CV counts as
    irregular."""
    bk = cfg["eval"]["buckets"]
    if af or not cv < bk["regular_max_cv"]:
        return "irregular"
    return "slow" if hr < bk["slow_bpm"] else "very_fast" if hr > bk["fast_bpm"] else (
        "regular_common")


# Stress cases

def stress_cases(cfg: dict, df: pd.DataFrame, beats: dict) -> tuple[pd.DataFrame, list]:
    """Stress cases of a split, one row each: `noise` (noise_records random recordings) and
    `slow` (every graded recording below buckets.slow_bpm) under each noise type and SNR; `rate`
    (regular-beat recordings retimed to each rate target, clean and with white noise at
    rate_snr_db): diastole stretched from slow_source_bpm for slow targets, whole signal sped
    up by at most rate_factor_max for fast targets."""
    st = STRESS
    rng = np.random.default_rng(st["seed"])
    ok = (df["ecg_trusted"] & ~df["unheard"]).to_numpy()
    rows, cases, seed = [], [], st["seed"]
    noise_idx = rng.choice(len(df), size=min(st["noise_records"], len(df)), replace=False)
    slow_idx = np.flatnonzero(ok & (df["reference_bpm"] < cfg["eval"]["buckets"]["slow_bpm"]))
    for kind, idx in (("noise", noise_idx), ("slow", slow_idx)):
        for noise in st["noise_types"]:
            for snr in st["snr_db"]:
                for i in idx:
                    rows.append(i)
                    cases.append({"kind": kind, "noise": noise, "snr_db": float(snr),
                                  "seed": seed})
                    seed += 1
    regular = [i for i in np.flatnonzero(ok)
               if augment.regular(beats[df["output_filename"].iloc[i]]["beats"], cfg)]
    lo, hi = st["slow_source_bpm"]
    for target in st["rate_targets_bpm"]:
        ref = df["reference_bpm"].to_numpy()
        source = [i for i in regular if (lo <= ref[i] <= hi if target < lo
                                         else ref[i] < target <= ref[i] * st["rate_factor_max"])]
        for i in rng.permutation(source)[:st["rate_records"]]:
            for noise in (None, "white"):
                rows.append(i)
                cases.append({"kind": "rate", "target_bpm": float(target), "noise": noise,
                              "snr_db": st["rate_snr_db"] if noise else None, "seed": seed,
                              "beats": beats[df["output_filename"].iloc[i]]["beats"],
                              "ref_bpm": float(ref[i])})
                seed += 1
    return df.iloc[rows].reset_index(drop=True), cases


def case_name(case: dict | None) -> str:
    """Condition label of a stress case ('' for a plain recording)."""
    if case is None:
        return ""
    parts = [case["kind"], case.get("noise") or "clean"]
    if case.get("snr_db") is not None:
        parts.append(f"{case['snr_db']:g}dB")
    if case.get("target_bpm") is not None:
        parts.append(f"{case['target_bpm']:g}bpm")
    return "_".join(parts)


# Running and grading

def _run_one(cfg: dict, task: tuple) -> dict:
    """Stream one recording or stress case; its readings, and its beat times when retimed."""
    filename, case = task
    fs = cfg["stream"]["fs_hz"]
    audio = data.load_wav(cfg, filename)
    beats = None
    if case is not None and case.get("target_bpm") is not None:
        audio, beats = augment.retime(cfg, audio, case["beats"], case["ref_bpm"],
                                      case["target_bpm"])
    if case is not None and case.get("noise"):
        audio = augment.add_noise(audio, fs, case["noise"], case["snr_db"], case["seed"])
    readings, _ = estimator.run(cfg, audio)
    return {"readings": readings, "beats": beats}


def run_records(cfg: dict, df: pd.DataFrame, cases: list | None = None) -> pd.DataFrame:
    """Readings of every recording (or stress case) in df with the columns grading needs."""
    cases = cases or [None] * len(df)
    tasks = list(zip(df["output_filename"], cases))
    rec = pd.DataFrame(data.map_records(cfg, _run_one, tasks, "evaluate", chunksize=8))
    for col in ("output_filename", "reference_bpm", "efast_bpm", "ecg_trusted", "unheard"):
        rec[col] = df[col].to_numpy()
    rec["rhythm"] = df["efast_ecg_rhythm_classification"].to_numpy()
    rec["case"] = [case_name(c) for c in cases]
    return rec


def group_of(row, own_beats: bool) -> str:
    """Grading group of one recording (module docstring)."""
    if own_beats:
        return "graded"
    if row.unheard:
        return "unheard"
    return "graded" if row.ecg_trusted else "other"


def grade(cfg: dict, rec: pd.DataFrame, beats: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-recording and per-reading results. Every reading of a recording with a trusted ECG
    is compared with the ECG HR over the same seconds."""
    lc, ev = cfg["labels"], cfg["eval"]
    curve = np.asarray(ev["curve_times_s"], dtype=float)
    per_rec, per_read = [], []
    for i, r in enumerate(rec.itertuples(index=False)):
        own = r.beats is not None
        b = np.asarray(r.beats) if own else beats[r.output_filename]["beats"]
        group = group_of(r, own)
        ref_hr = labels.full_hr(b) if own else r.reference_bpm
        af = r.rhythm == cfg["eval"]["af_rhythm"]
        cv = interval_cv(b)
        t = np.array([x[0] for x in r.readings], dtype=float)
        bpm = np.array([x[1] for x in r.readings], dtype=float)
        ref = (np.full(t.size, np.nan) if group == "other" else
               np.array([labels.window_hr(b, ti, lc["window_s"], lc["min_intervals"]) for ti in t]))
        err = bpm - ref
        ok = np.abs(err) <= ev["fine_bpm"]
        bad = np.flatnonzero(~ok)
        start = bad[-1] + 1 if bad.size else 0
        shown = np.searchsorted(t, curve + 1e-9, side="right") - 1
        half_double = is_half_double(cfg, bpm, ref)
        per_rec.append({
            "rec": i, "output_filename": r.output_filename, "case": r.case, "group": group,
            "bucket": bucket_of(cfg, ref_hr, cv, af) if group == "graded" else group,
            "af": af, "rr_cv": cv, "reference_bpm": ref_hr, "efast_bpm": r.efast_bpm,
            "ttfr_s": t[0] if t.size else np.inf,
            "first_bpm": bpm[0] if t.size else np.nan,
            "first_ref_bpm": ref[0] if t.size else np.nan,
            "first_err": err[0] if t.size else np.nan,
            "first_ec13": bool(t.size and is_within(cfg, bpm[0], r.efast_bpm)),
            "converge_s": t[start] if start < t.size else np.inf,
            **{f"fine_at_{c:g}s": bool(j >= 0 and ok[j]) for c, j in zip(curve, shown)}})
        per_read += [{"rec": i, "t_s": ti, "bpm": hi, "ref_bpm": ri, "err": ei, "first": k == 0,
                      "within_fine": bool(oki), "half_double": bool(hdi)}
                     for k, (ti, hi, ri, ei, oki, hdi)
                     in enumerate(zip(t, bpm, ref, err, ok, half_double))]
    return pd.DataFrame(per_rec), pd.DataFrame(per_read)


# Summary and targets

def metrics(cfg: dict, recs: pd.DataFrame, reads: pd.DataFrame) -> dict:
    """Metrics of a set of recordings and their readings. Time quantiles take the next higher
    value (no interpolation; recordings without a reading count as infinite)."""
    ev = cfg["eval"]
    if recs.empty:
        return {"n": 0}
    ttfr = recs["ttfr_s"].to_numpy()
    err = reads["err"].dropna()
    later = reads[~reads["first"]]
    first_abs = recs["first_err"].abs()
    return {
        "n": len(recs),
        "coverage": float(np.isfinite(ttfr).mean()),
        "ttfr_median_s": float(np.quantile(ttfr, 0.5, method="higher")),
        "ttfr_p90_s": float(np.quantile(ttfr, 0.9, method="higher")),
        "ttfr_max_s": float(ttfr.max()),
        "reading_by_10s": float((ttfr <= 10.0 + 1e-9).mean()),
        "first_within_fine": float((first_abs <= ev["fine_bpm"]).mean()),
        "first_within_rough": float((first_abs <= ev["rough_bpm"]).mean()),
        "later_within_fine": float(later["within_fine"].mean()) if len(later) else np.nan,
        **{f"within_fine_at_{c:g}s": float(recs[f"fine_at_{c:g}s"].mean())
           for c in ev["curve_times_s"]},
        "converge_median_s": float(np.quantile(recs["converge_s"], 0.5, method="higher")),
        "converge_p90_s": float(np.quantile(recs["converge_s"], 0.9, method="higher")),
        "half_double_readings": float(reads["half_double"].mean()) if len(reads) else np.nan,
        "first_mae_bpm": float(first_abs.mean()),
        "bias_bpm": float(err.mean()), "mae_bpm": float(err.abs().mean()),
        "loa_bpm": float(1.96 * err.std()),
        "first_ec13": float(recs["first_ec13"].mean()),
    }


def summarize(cfg: dict, per_rec: pd.DataFrame, per_read: pd.DataFrame) -> dict:
    """Metrics per bucket of the graded group, for the whole graded group (`all`), atrial
    fibrillation (`af`) and the other groups, as flat '<set>.<metric>' keys."""
    graded = per_rec[per_rec["group"] == "graded"]
    sets = {b: graded[graded["bucket"] == b] for b in BUCKETS}
    sets.update({"all": graded, "af": graded[graded["af"]]})
    sets.update({g: per_rec[per_rec["group"] == g] for g in ("unheard", "other")})
    out = {}
    for name, recs in sets.items():
        values = metrics(cfg, recs, per_read[per_read["rec"].isin(recs["rec"])])
        if name == "other":
            values = {k: v for k, v in values.items() if k in OTHER_KEYS}
        out.update({f"{name}.{k}": v for k, v in values.items()})
    return out


def check_targets(cfg: dict, summary: dict, only: tuple | None = None) -> dict:
    """Pass/fail per target in cfg targets (buckets in `only` if given): time targets (_s) and
    half/double rates are upper bounds, the others lower bounds. Buckets without recordings are
    skipped; a missing value fails."""
    checks = {}
    for band, targets in cfg["targets"].items():
        if (only and band not in only) or not summary.get(f"{band}.n", 0):
            continue
        for name, target in targets.items():
            key = f"{band}.{name}"
            value = summary.get(key, np.nan)
            upper = name.endswith("_s") or name.startswith("half_double")
            checks[key] = {"value": value, "target": target, "upper_bound": upper,
                           "pass": bool(value <= target if upper else value >= target)}
    return checks


def stress_table(cfg: dict, per_rec: pd.DataFrame, per_read: pd.DataFrame) -> pd.DataFrame:
    """Key metrics per stress condition (graded recordings)."""
    rows = []
    for name, recs in per_rec[per_rec["group"] == "graded"].groupby("case"):
        m = metrics(cfg, recs, per_read[per_read["rec"].isin(recs["rec"])])
        rows.append({"condition": name, **{k: m[k] for k in (
            "n", "ttfr_median_s", "first_within_fine", "first_within_rough", "within_fine_at_4s",
            "within_fine_at_9s", "half_double_readings")}})
    return pd.DataFrame(rows)



# Output

def provenance(cfg: dict) -> dict:
    """Software version, checksum of requirements.txt, git commit (with an
    uncommitted-changes flag) and checksums of the model files (network, scorer, calibration,
    presence gate when fitted)."""
    def git(*args: str) -> str:
        """Output of a git command in the project root ('' outside a repository)."""
        return subprocess.run(["git", *args], cwd=config.PROJECT_ROOT, capture_output=True,
                              text=True).stdout.strip()

    files = [p for p in (net.weights_path(cfg), scorer.weights_path(cfg),
                         readout.calibration_path(cfg), readout.presence_path(cfg)) if p.is_file()]
    reqs = config.PROJECT_ROOT / "requirements.txt"
    return {"version": config.version(),
            "requirements_sha256": hashlib.sha256(reqs.read_bytes()).hexdigest()[:12],
            "git_commit": git("rev-parse", "--short", "HEAD"),
            "git_uncommitted_changes": bool(git("status", "--porcelain", "--untracked-files=no")),
            "weights_sha1": {p.name: hashlib.sha1(p.read_bytes()).hexdigest()[:12]
                             for p in files}}


def new_run(cfg: dict, split: str, tag: str) -> Path:
    """Run folder runs_dir/<time>_<weights>_<split>[_<tag>] with the config used."""
    run_id = f"{datetime.now():%Y%m%d_%H%M%S}_{cfg['weights']}_{split}{'_' + tag if tag else ''}"
    out = cfg["paths"]["runs_dir"] / run_id
    out.mkdir(parents=True)
    plain_cfg = {**cfg, "paths": {k: str(v) for k, v in cfg["paths"].items()}}
    (out / "config.yml").write_text(yaml.safe_dump(plain_cfg, sort_keys=False))
    return out


def write_run(cfg: dict, rec: pd.DataFrame, per_rec: pd.DataFrame, summary: dict, checks: dict,
              tag: str, split: str) -> Path:
    """Write config, per-recording results, readings, summary, targets and run record under
    runs_dir/<time>_<weights>_<split>[_<tag>]."""
    out = new_run(cfg, split, tag)
    run_id = out.name
    per_rec.to_csv(out / "per_record.csv", index=False)
    rec[["output_filename", "case", "readings"]].to_json(out / "readings.json", orient="records",
                                                         double_precision=15)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    (out / "targets.json").write_text(json.dumps(checks, indent=1))
    info = {"run_id": run_id, "weights": cfg["weights"], "split": split, "tag": tag,
            "n_records": len(rec), "manifest": str(cfg["paths"]["manifest"]),
            "split_manifest": str(cfg["paths"]["split_manifest"]),
            "labels": str(cfg["paths"]["labels"]), **provenance(cfg)}
    (out / "run_info.json").write_text(json.dumps(info, indent=1, default=float))
    return out


def _axes(title: str, xlabel: str, ylabel: str) -> tuple:
    """Figure with one axes: recessive grid and spines, left-aligned title."""
    fig = Figure(figsize=(7, 4), layout="tight")
    ax = fig.subplots()
    ax.set_title(title, loc="left", color=INK, fontsize=11)
    ax.set_xlabel(xlabel, color=MUTED)
    ax.set_ylabel(ylabel, color=MUTED)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=MUTED)
    return fig, ax


def figures(cfg: dict, per_rec: pd.DataFrame, per_read: pd.DataFrame, out: Path) -> None:
    """Graded group: share of recordings whose reading on show is within ±fine / ±rough BPM vs
    time, time to first reading, and first-reading error vs reference HR, per bucket (PNG)."""
    ev = cfg["eval"]
    graded = per_rec[per_rec["group"] == "graded"]
    reads = per_read[per_read["rec"].isin(graded["rec"])].sort_values("t_s")
    end_s = max(ev["curve_times_s"])
    times = np.arange(1.0, end_s + 0.01, 0.25)
    fig, ax = _axes("Reading on show within ±3 BPM (solid) and ±10 BPM (dashed) of the ECG",
                    "time since start (s)", "recordings (%)")
    for band in BUCKETS:
        recs = graded[graded["bucket"] == band]
        if recs.empty:
            continue
        band_reads = reads[reads["rec"].isin(recs["rec"])]
        fine, rough = [], []
        for t in times:
            shown = band_reads[band_reads["t_s"] <= t + 1e-9].groupby("rec").last()
            fine.append(100 * (shown["err"].abs() <= ev["fine_bpm"]).sum() / len(recs))
            rough.append(100 * (shown["err"].abs() <= ev["rough_bpm"]).sum() / len(recs))
        label = f"{band} (n={len(recs)})"
        ax.plot(times, fine, color=BUCKET_COLORS[band], linewidth=2, label=label)
        ax.plot(times, rough, color=BUCKET_COLORS[band], linewidth=1.5, linestyle="--")
    ax.set_ylim(0, 100)
    ax.legend(frameon=False, fontsize=8, loc="lower right", labelcolor=INK)
    fig.savefig(out / "accuracy_vs_time.png", dpi=150)
    fig, ax = _axes("Time to first reading", "time since start (s)", "recordings (%)")
    for band in BUCKETS:
        ttfr = np.sort(graded.loc[graded["bucket"] == band, "ttfr_s"].to_numpy())
        if ttfr.size:
            ax.step(ttfr, 100 * np.arange(1, ttfr.size + 1) / ttfr.size, where="post",
                    color=BUCKET_COLORS[band], linewidth=2, label=f"{band} (n={ttfr.size})")
    ax.set_xlim(0, end_s + 0.5)
    ax.set_ylim(0, 100)
    ax.legend(frameon=False, fontsize=8, loc="lower right", labelcolor=INK)
    fig.savefig(out / "ttfr_cdf.png", dpi=150)
    fig, ax = _axes("First reading minus ECG HR (errors beyond ±25 BPM drawn at the edge)",
                    "ECG HR over the same seconds (BPM)", "error (BPM)")
    for limit in (ev["fine_bpm"], ev["rough_bpm"]):
        for sign in (-1, 1):
            ax.axhline(sign * limit, color=MUTED, linewidth=0.8, linestyle="--")
    for band in BUCKETS:
        recs = graded[graded["bucket"] == band]
        ax.scatter(recs["first_ref_bpm"], recs["first_err"].clip(-25, 25), s=10,
                   color=BUCKET_COLORS[band], alpha=0.7, linewidths=0, label=band)
    ax.set_ylim(-26, 26)
    ax.legend(frameon=False, fontsize=8, loc="lower right", labelcolor=INK)
    fig.savefig(out / "first_error.png", dpi=150)


def strata(cfg: dict, per_rec: pd.DataFrame, meta: pd.DataFrame) -> pd.DataFrame:
    """Graded group per stratum (device, firmware, mains filter, rhythm, 10 BPM band): count,
    first reading within ±fine and ±rough, median time to first reading, within ±fine at 9 s."""
    ev = cfg["eval"]
    meta = meta.assign(rhythm=meta["efast_ecg_rhythm_classification"],
                       hr_band_10=(meta["reference_bpm"] // 10 * 10).astype(int))
    joined = per_rec[per_rec["group"] == "graded"].merge(
        meta[["output_filename", *STRATA]], on="output_filename")
    rows = []
    for col in STRATA:
        for value, grp in joined.groupby(col):
            rows.append({"stratum": col, "value": value, "n": len(grp),
                         "first_within_fine": (grp["first_err"].abs() <= ev["fine_bpm"]).mean(),
                         "first_within_rough": (grp["first_err"].abs() <= ev["rough_bpm"]).mean(),
                         "ttfr_median_s": float(np.quantile(grp["ttfr_s"], 0.5, method="higher")),
                         "within_fine_at_9s": grp["fine_at_9s"].mean()})
    return pd.DataFrame(rows)


def run(cfg: dict, split: str, stress: bool = False, tag: str = "",
        df: pd.DataFrame | None = None) -> tuple[Path, dict, dict]:
    """Evaluate one split (or the given recordings, e.g. another snapshot), or its stress set;
    write the run folder; return it, the summary and the target checks."""
    df = data.load_split(cfg, split) if df is None else df
    meta = df
    beats = labels.build_beats(cfg, df)
    cases = None
    if stress:
        df, cases = stress_cases(cfg, df, beats)
    rec = run_records(cfg, df, cases)
    per_rec, per_read = grade(cfg, rec, beats)
    summary = summarize(cfg, per_rec, per_read)
    checks = check_targets(cfg, summary, only=("very_fast",) if stress else None)
    run_tag = "_".join(x for x in (tag, "stress" if stress else "") if x)
    out = write_run(cfg, rec, per_rec, summary, checks, run_tag, split)
    if stress:
        stress_table(cfg, per_rec, per_read).to_csv(out / "stress_by_condition.csv", index=False)
    else:
        figures(cfg, per_rec, per_read, out)
        strata(cfg, per_rec, meta).to_csv(out / "strata.csv", index=False)
    return out, summary, checks


# No-heart evaluation (hr-estimation noise-eval): evaluation negatives only

def _noise_one(cfg: dict, task: tuple) -> dict:
    """Readings and restart times of one case: ("clip", kind, seed) a no-heart clip; ("lift",
    file, kind, seed) a lift-off spliced into a recording; ("swap", file, file) the first
    recording to swap_at_s, then the second (another heart rate); ("overlap", file, kind,
    snr_db, seed) a recording with the sound added throughout."""
    ne, fs = cfg["noise_eval"], cfg["stream"]["fs_hz"]
    if task[0] == "clip":
        audio = augment.no_heart_clip(cfg, task[1], int(ne["clip_s"] * fs),
                                      np.random.default_rng(task[2]), split="eval")
    elif task[0] == "lift":
        _, name, kind, seed = task
        audio = augment.lift_off(cfg, data.load_wav(cfg, name), ne["lift_at_s"], ne["off_s"],
                                 kind, ne["lift_kinds"][kind], np.random.default_rng(seed),
                                 split="eval")
    elif task[0] == "swap":
        first, second = (data.load_wav(cfg, name) for name in task[1:3])
        audio = np.concatenate([first[: int(ne["swap_at_s"] * fs)], second])
    else:
        _, name, kind, snr_db, seed = task
        audio = augment.add_noise(data.load_wav(cfg, name), fs, kind, snr_db, seed, cfg,
                                  split="eval")
    readings, trace = estimator.run(cfg, audio, keep_trace=True)
    return {"readings": readings, "restarts": c_port.restart_times(cfg, trace)}


def _lift_case(cfg: dict, res: dict, beats: np.ndarray) -> dict:
    """One lift-off: time from the lift-off to the end of the measurement (NaN: not within the
    gap), largest change of a reading before that end from the last reading before the
    lift-off, readings in the gap after it, time and error of the first reading after the
    return (ECG over the same seconds of the original recording)."""
    ne, lc = cfg["noise_eval"], cfg["labels"]
    at, back = ne["lift_at_s"], ne["lift_at_s"] + ne["off_s"]
    t = np.array([r[0] for r in res["readings"]]).reshape(-1)
    bpm = np.array([r[1] for r in res["readings"]]).reshape(-1)
    ends = [s for s in res["restarts"] if at < s <= back]
    clear = ends[0] if ends else np.nan
    before = bpm[t <= at]
    during = bpm[(t > at) & (t < (clear if ends else back))]
    after = np.flatnonzero(t > back)
    err = np.nan
    if after.size:
        ref = labels.window_hr(beats, t[after[0]] - ne["off_s"], lc["window_s"],
                               lc["min_intervals"])
        err = abs(bpm[after[0]] - ref)
    return {"clear_s": clear - at, "drift_bpm": (float(np.abs(during - before[-1]).max())
                                                 if before.size and during.size else 0.0),
            "ghost": int(((t >= clear) & (t < back)).sum()) if ends else 0,
            "back_s": t[after[0]] - back if after.size else np.nan, "back_err_bpm": err}


def _swap_case(cfg: dict, res: dict, beats_a: np.ndarray, beats_b: np.ndarray) -> dict:
    """One swap to another heart: time from the swap to the first reading within rough_bpm of
    the second heart's ECG (NaN: none), and readings after the swap off by more than rough_bpm
    from both hearts (neither the old value held nor the new one)."""
    ne, lc, ev = cfg["noise_eval"], cfg["labels"], cfg["eval"]
    at = ne["swap_at_s"]
    ref_a = labels.window_hr(beats_a, at, lc["window_s"], lc["min_intervals"])
    adapt, wrong = np.nan, 0
    for t, bpm, _ in res["readings"]:
        if t <= at:
            continue
        ref_b = labels.window_hr(beats_b, t - at, lc["window_s"], lc["min_intervals"])
        if abs(bpm - ref_b) <= ev["rough_bpm"]:
            adapt = t - at if np.isnan(adapt) else adapt
        elif abs(bpm - ref_a) > ev["rough_bpm"]:
            wrong += 1
    return {"adapt_s": adapt, "wrong": wrong}


def _overlap_case(cfg: dict, res: dict, beats: np.ndarray, duration_s: float) -> dict:
    """One recording with a sound added: readings per minute and readings off by more than
    rough_bpm from the ECG over the same seconds."""
    lc = cfg["labels"]
    errs = [abs(bpm - labels.window_hr(beats, t, lc["window_s"], lc["min_intervals"]))
            for t, bpm, _ in res["readings"]]
    return {"per_min": len(errs) / duration_s * 60,
            "off": int(sum(e > cfg["eval"]["rough_bpm"] for e in errs if np.isfinite(e)))}


def _q(values: list, q: float) -> float:
    """Quantile of the finite values (NaN without any)."""
    v = np.asarray(values, float)
    v = v[np.isfinite(v)]
    return float(np.quantile(v, q)) if v.size else np.nan


def noise_run(cfg: dict, tag: str = "") -> tuple[Path, dict]:
    """No-heart clips per kind, lift-offs, swaps to another heart and overlaps on validation
    recordings (trusted ECG, beats heard), all with evaluation negatives; write summary.json and
    run_info.json to a run folder; return it and the summary."""
    ne = cfg["noise_eval"]
    df = data.load_split(cfg, "val")
    df = df[df["ecg_trusted"] & ~df["unheard"]]
    df = df.sample(n=min(ne["records"], len(df)), random_state=ne["seed"])
    beats = labels.build_beats(cfg, df)
    names = list(df["output_filename"])
    seed = ne["seed"] * 1000
    tasks = [("clip", k, seed + j) for k in ne["kinds"] for j in range(ne["clips"])]
    tasks += [("lift", f, k, seed + j) for k in ne["lift_kinds"] for j, f in enumerate(names)]
    hr = dict(zip(df["output_filename"], df["reference_bpm"]))
    tasks += [("swap", a, b) for a, b in zip(names, names[1:] + names[:1])
              if abs(hr[a] - hr[b]) >= ne["swap_min_delta_bpm"]]
    tasks += [("overlap", f, k, snr, seed + j) for k in ne["overlap_kinds"]
              for snr in ne["overlap_snr_db"] for j, f in enumerate(names)]
    results = data.map_records(cfg, _noise_one, tasks, "noise-eval")
    summary: dict = {"clips": {}, "lift": {}, "swap": [], "overlap": {}}
    for task, res in zip(tasks, results):
        if task[0] == "clip":
            c = summary["clips"].setdefault(task[1], {"n": 0, "with_reading": 0, "readings": 0})
            c["n"] += 1
            c["with_reading"] += bool(res["readings"])
            c["readings"] += len(res["readings"])
        elif task[0] == "lift":
            summary["lift"].setdefault(task[2], []).append(
                _lift_case(cfg, res, beats[task[1]]["beats"]))
        elif task[0] == "swap":
            summary["swap"].append(_swap_case(cfg, res, beats[task[1]]["beats"],
                                              beats[task[2]]["beats"]))
        else:
            dur = len(data.load_wav(cfg, task[1])) / cfg["stream"]["fs_hz"]
            summary["overlap"].setdefault(f"{task[2]}@{task[3]:g}dB", []).append(
                _overlap_case(cfg, res, beats[task[1]]["beats"], dur))
    for c in summary["clips"].values():
        c["share_with_reading"] = c["with_reading"] / c["n"]
        c["per_min"] = c["readings"] / (c["n"] * ne["clip_s"] / 60)
    summary["lift"] = {k: {"n": len(v),
                           "cleared": int(np.isfinite([c["clear_s"] for c in v]).sum()),
                           "clear_median_s": _q([c["clear_s"] for c in v], 0.5),
                           "drift_p90_bpm": _q([c["drift_bpm"] for c in v], 0.9),
                           "ghost_cases": sum(c["ghost"] > 0 for c in v),
                           "back_median_s": _q([c["back_s"] for c in v], 0.5),
                           "back_err_p90_bpm": _q([c["back_err_bpm"] for c in v], 0.9)}
                       for k, v in summary["lift"].items()}
    sw = summary["swap"]
    summary["swap"] = {"n": len(sw), "adapted": int(np.isfinite([c["adapt_s"] for c in sw]).sum()),
                       "adapt_median_s": _q([c["adapt_s"] for c in sw], 0.5),
                       "adapt_p90_s": _q([c["adapt_s"] for c in sw], 0.9),
                       "wrong_readings": sum(c["wrong"] for c in sw)}
    summary["overlap"] = {k: {"n": len(v), "per_min": float(np.mean([c["per_min"] for c in v])),
                              "off_readings": sum(c["off"] for c in v),
                              "recordings_with_off": sum(c["off"] > 0 for c in v)}
                          for k, v in summary["overlap"].items()}
    out = new_run(cfg, "noise", tag)
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=float))
    info = {"run_id": out.name, "weights": cfg["weights"], "tag": tag, "n_cases": len(tasks),
            "negatives": str(cfg["paths"]["negatives"]), **provenance(cfg)}
    (out / "run_info.json").write_text(json.dumps(info, indent=1, default=float))
    return out, summary
