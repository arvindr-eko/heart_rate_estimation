"""Recordings: manifest with labels, grouped stratified splits, audio loading, and the parallel
map used by every training and evaluation step."""

import logging
import multiprocessing as mp
import time
from typing import Callable

import numpy as np
import pandas as pd
import soundfile as sf
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
