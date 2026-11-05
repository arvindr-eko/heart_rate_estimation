"""Manifest, answer key, grouped stratified splits and WAV loading."""

import logging

import numpy as np
import pandas as pd
import soundfile as sf
from sklearn.model_selection import StratifiedGroupKFold

log = logging.getLogger(__name__)

SPLIT_COLUMNS = ["output_filename", "user_id", "reference_bpm", "hr_bin", "fold", "split"]


def load_manifest(cfg: dict) -> pd.DataFrame:
    """Recordings with status ok and a reference HR. reference_bpm is the EFAST ECG HR, replaced
    by the answer-key file (paths.reference_labels) when it exists; adds label_source, hr_bin."""
    df = pd.read_csv(cfg["paths"]["manifest"])
    df = df[df["status"] == "ok"].copy()
    df["reference_bpm"] = pd.to_numeric(df[cfg["data"]["reference_column"]], errors="coerce")
    df["label_source"] = "efast"
    labels = cfg["paths"].get("reference_labels")
    if labels is not None and labels.is_file():
        corrected = pd.read_csv(labels).set_index("output_filename")
        df["label_source"] = df["output_filename"].map(corrected["label_source"]).fillna("efast")
        df["reference_bpm"] = df["output_filename"].map(corrected["reference_bpm"]).fillna(
            df["reference_bpm"])
    df = df.dropna(subset=["reference_bpm"])
    width = cfg["data"]["stratify_bin_bpm"]
    df["hr_bin"] = (df["reference_bpm"] // width * width).astype(int)
    return df.reset_index(drop=True)


def make_splits(cfg: dict) -> pd.DataFrame:
    """Assign grouped, HR-stratified folds and named splits; write the split manifest."""
    df = load_manifest(cfg)
    groups = df[cfg["data"]["group_column"]].astype(str)
    # Merge sparse HR bins so every stratum has enough members for the fold count
    counts = df["hr_bin"].value_counts()
    strata = df["hr_bin"].where(df["hr_bin"].map(counts) >= cfg["data"]["n_folds"], -1)
    kfold = StratifiedGroupKFold(n_splits=cfg["data"]["n_folds"], shuffle=True,
                                 random_state=cfg["data"]["seed"])
    df["fold"] = -1
    for fold, (_, idx) in enumerate(kfold.split(df, strata, groups)):
        df.loc[idx, "fold"] = fold
    fold_to_split = {f: name for name, folds in cfg["data"]["folds"].items() for f in folds}
    df["split"] = df["fold"].map(fold_to_split)
    out = cfg["paths"]["split_manifest"]
    out.parent.mkdir(parents=True, exist_ok=True)
    df[SPLIT_COLUMNS].to_csv(out, index=False)
    log.info("splits: %s", df["split"].value_counts().to_dict())
    return df


def load_split(cfg: dict, split: str | None = None) -> pd.DataFrame:
    """Manifest rows of one split (train, val, test); all rows when split is None."""
    splits = pd.read_csv(cfg["paths"]["split_manifest"])
    df = load_manifest(cfg).merge(splits[["output_filename", "fold", "split"]],
                                  on="output_filename")
    if split is not None:
        df = df[df["split"] == split]
    return df.reset_index(drop=True)


def load_wav(cfg: dict, filename: str) -> np.ndarray:
    """PCG as float32 in [-1, 1], truncated to max_duration_s; checks the sample rate."""
    audio, fs = sf.read(cfg["paths"]["wav_dir"] / filename, dtype="float32")
    if fs != cfg["stream"]["fs_hz"]:
        raise ValueError(f"{filename}: sample rate {fs} != {cfg['stream']['fs_hz']}")
    if audio.ndim > 1:
        audio = audio[:, 0]
    return audio[: int(cfg["data"]["max_duration_s"] * fs)]
