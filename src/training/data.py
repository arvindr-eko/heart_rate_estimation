"""Recordings: manifest with labels, grouped stratified splits, audio loading, the real
non-heart sounds (negatives), and the parallel map used by every training and evaluation step."""

import hashlib
import json
import logging
import multiprocessing as mp
import time
from math import gcd
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import soundfile as sf
from scipy import signal
from sklearn.model_selection import StratifiedGroupKFold

log = logging.getLogger(__name__)

SPLITS = ("train", "val", "test")
SPLIT_COLUMNS = ["output_filename", "user_id", "reference_bpm", "hr_bin", "fold", "split"]

_WORKER: dict = {}


def load_manifest(cfg: dict) -> pd.DataFrame:
    """Recordings with status ok and an EFAST ECG HR (efast_bpm). With the label table
    (paths.labels) present, adds its columns; reference_bpm is the consensus ECG HR where the
    ECG is trusted, the EFAST ECG HR otherwise (label_source ecg / efast). hr_bin: 5 BPM bin of
    reference_bpm."""
    df = pd.read_csv(cfg["paths"]["manifest"])
    df = df[df["status"] == "ok"].copy()
    df["efast_bpm"] = pd.to_numeric(df[cfg["data"]["reference_column"]], errors="coerce")
    df = df.dropna(subset=["efast_bpm"])
    df["reference_bpm"], df["label_source"] = df["efast_bpm"], "efast"
    table = cfg["paths"].get("labels")
    if table is not None and table.is_file():
        df = df.merge(pd.read_csv(table), on="output_filename", how="left")
        df["ecg_trusted"] = df["ecg_trusted"].fillna(False).astype(bool)
        df["unheard"] = df["unheard"].fillna(False).astype(bool)
        df.loc[df["ecg_trusted"], "reference_bpm"] = df.loc[df["ecg_trusted"], "ecg_hr_bpm"]
        df.loc[df["ecg_trusted"], "label_source"] = "ecg"
    width = cfg["split"]["stratify_bin_bpm"]
    df["hr_bin"] = (df["reference_bpm"] // width * width).astype(int)
    return df.reset_index(drop=True)


def n_parts(shares: list[float]) -> int:
    """Smallest number of equal parts that represents every share exactly (10 for 50/20/30)."""
    for n in range(1, 101):
        if all(abs(s * n - round(s * n)) < 1e-9 for s in shares):
            return n
    raise ValueError(f"split shares {shares} need more than 100 parts")


def make_splits(cfg: dict) -> pd.DataFrame:
    """Split by clinician account into train / val / test with the configured shares, each
    with a similar mix of HR bins; write the split manifest.

    Recordings are dealt into equal parts (StratifiedGroupKFold); consecutive parts form the
    splits, e.g. 10 parts of 10 % for shares 0.5 / 0.2 / 0.3. The part number is kept as `fold`.
    HR bins come from the EFAST reference, so ECG label corrections never move recordings
    between splits.
    """
    sp = cfg["split"]
    shares = [sp[name] for name in SPLITS]
    parts = n_parts(shares)
    df = load_manifest({**cfg, "paths": {**cfg["paths"], "labels": None}})
    groups = df[sp["group_column"]].astype(str)
    # Merge sparse HR bins so every stratum has a member for each part
    counts = df["hr_bin"].value_counts()
    strata = df["hr_bin"].where(df["hr_bin"].map(counts) >= parts, -1)
    kfold = StratifiedGroupKFold(n_splits=parts, shuffle=True, random_state=cfg["data"]["seed"])
    df["fold"] = -1
    for fold, (_, idx) in enumerate(kfold.split(df, strata, groups)):
        df.loc[idx, "fold"] = fold
    bounds = np.cumsum([round(s * parts) for s in shares])
    df["split"] = [SPLITS[int(np.searchsorted(bounds, f, side="right"))] for f in df["fold"]]
    out = cfg["paths"]["split_manifest"]
    out.parent.mkdir(parents=True, exist_ok=True)
    df[SPLIT_COLUMNS].to_csv(out, index=False)
    log.info("splits: %s", df["split"].value_counts().to_dict())
    return df


def load_split(cfg: dict, split: str) -> pd.DataFrame:
    """Manifest rows of one split (train, val, test)."""
    splits = pd.read_csv(cfg["paths"]["split_manifest"])
    df = load_manifest(cfg).merge(splits[["output_filename", "fold", "split"]],
                                  on="output_filename")
    return df[df["split"] == split].reset_index(drop=True)


def load_wav(cfg: dict, filename: str) -> np.ndarray:
    """PCG as float32 in [-1, 1], truncated to max_duration_s; checks the sample rate."""
    audio, fs = sf.read(cfg["paths"]["wav_dir"] / filename, dtype="float32")
    if fs != cfg["stream"]["fs_hz"]:
        raise ValueError(f"{filename}: sample rate {fs} != {cfg['stream']['fs_hz']}")
    if audio.ndim > 1:
        audio = audio[:, 0]
    return audio[: int(cfg["data"]["max_duration_s"] * fs)]


def _to_device_rate(path: Path, fs_out: int) -> np.ndarray:
    """Mono float32 audio of a file, resampled (polyphase) to fs_out."""
    x, fs = sf.read(path, dtype="float32", always_2d=True)
    g = gcd(int(fs), fs_out)
    return signal.resample_poly(x.mean(axis=1), fs_out // g, int(fs) // g).astype(np.float32)


def _eval_group(group: str, share: float, seed: int) -> bool:
    """Whether a source group (speaker, uploader) is held out for evaluation: a seeded hash
    below share."""
    h = hashlib.sha1(f"{seed}:{group}".encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF < share


def _librispeech_rows(cfg: dict) -> list:
    """Utterances of the LibriSpeech subsets present (dev-clean, test-clean), at most
    negatives.speech_per_speaker per speaker; kind speech; split by speaker (eval_share)."""
    ng, rows = cfg["negatives"], []
    root = cfg["paths"]["negatives_raw"] / "librispeech" / "LibriSpeech"
    for subset in ("dev-clean", "test-clean"):
        if not (root / subset).is_dir():
            continue
        for speaker in sorted((root / subset).iterdir()):
            group = f"spk{speaker.name}"
            split = "eval" if _eval_group(group, ng["eval_share"], ng["seed"]) else "train"
            rows += [{"source": f, "kind": "speech", "corpus": f"librispeech/{subset}",
                      "licence": "CC BY 4.0", "group": group, "split": split,
                      "attribution": "LibriSpeech, Panayotov et al. 2015"}
                     for f in sorted(speaker.rglob("*.flac"))[: ng["speech_per_speaker"]]]
    return rows


def _fsd50k_rows(cfg: dict) -> list:
    """FSD50K evaluation-set clips with a CC0 or CC-BY licence whose labels name a kind of
    negatives.fsd50k_kinds (first kind in config order; clips with an excluded label dropped);
    split by uploader. Clips whose audio is not extracted are skipped (logged)."""
    ng, rows, missing = cfg["negatives"], [], 0
    root = cfg["paths"]["negatives_raw"] / "fsd50k"
    truth = pd.read_csv(root / "FSD50K.ground_truth" / "eval.csv")
    info = json.loads((root / "FSD50K.metadata" / "eval_clips_info_FSD50K.json").read_text())
    for fname, labels in zip(truth["fname"].astype(str), truth["labels"]):
        names = set(labels.split(","))
        meta = info[fname]
        licence = meta["license"]
        if names & set(ng["fsd50k_exclude"]) or not any(k in licence for k in ng["licences"]):
            continue
        kind = next((k for k, wanted in ng["fsd50k_kinds"].items() if names & set(wanted)), None)
        source = root / "FSD50K.eval_audio" / f"{fname}.wav"
        if kind is None or not source.is_file():
            missing += kind is not None
            continue
        rows.append({"source": source, "kind": kind,
                     "corpus": "fsd50k/eval", "licence": licence, "group": meta["uploader"],
                     "split": "eval" if _eval_group(meta["uploader"], ng["eval_share"],
                                                    ng["seed"]) else "train",
                     "attribution": f"{meta['title']} by {meta['uploader']} (Freesound {fname})"})
    log.info("FSD50K: %d clips kept, %d wanted clips without extracted audio", len(rows), missing)
    return rows


def _write_negative(cfg: dict, row: dict) -> dict:
    """One negative at the device rate as 16-bit WAV (peak 0.9) under the manifest's folder;
    the manifest row without the source path."""
    fs = cfg["stream"]["fs_hz"]
    x = _to_device_rate(row["source"], fs)
    rel = f"clips/{row['corpus'].replace('/', '_')}/{row['source'].stem}.wav"
    out = cfg["paths"]["negatives"].parent / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out, x / max(float(np.abs(x).max()), 1e-9) * 0.9, fs, subtype="PCM_16")
    return {**{k: v for k, v in row.items() if k != "source"}, "file": rel,
            "duration_s": round(x.size / fs, 2)}


def prepare_negatives(cfg: dict) -> pd.DataFrame:
    """Manifest of real non-heart sounds (paths.negatives): LibriSpeech speech and FSD50K
    clips (licences in negatives.licences), resampled to the device rate; training and
    evaluation splits disjoint by speaker and uploader."""
    rows = _librispeech_rows(cfg) + _fsd50k_rows(cfg)
    df = pd.DataFrame(map_records(cfg, _write_negative, rows, "negatives"))
    df.to_csv(cfg["paths"]["negatives"], index=False)
    for (kind, split), grp in df.groupby(["kind", "split"]):
        log.info("negatives %s %s: %d clips, %.0f min, %d groups", kind, split, len(grp),
                 grp["duration_s"].sum() / 60, grp["group"].nunique())
    return df


def _worker_init(cfg: dict, fn: Callable) -> None:
    """Store the config and the task function in a worker process."""
    _WORKER["cfg"], _WORKER["fn"] = cfg, fn


def _worker_call(task):
    """Run the stored task function on one task."""
    return _WORKER["fn"](_WORKER["cfg"], task)


def map_records(cfg: dict, fn: Callable, tasks: list, what: str, chunksize: int = 4) -> list:
    """fn(cfg, task) for every task on `workers` spawned processes, results in task order;
    logs progress about every 10 %. fn must be a module-level function."""
    results, start = [], time.time()
    step = max(1, len(tasks) // 10)
    with mp.get_context("spawn").Pool(cfg["workers"], _worker_init, (cfg, fn)) as pool:
        for i, result in enumerate(pool.imap(_worker_call, tasks, chunksize), 1):
            results.append(result)
            if i % step == 0 or i == len(tasks):
                elapsed = time.time() - start
                log.info("%s: %d/%d, %.0f s elapsed, %.0f s left", what, i, len(tasks),
                         elapsed, elapsed / i * (len(tasks) - i))
    return results
