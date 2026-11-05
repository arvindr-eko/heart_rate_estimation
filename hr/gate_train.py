"""Training of the release gate: per-frame traces, MLP fit (scikit-learn), calibration table.

Training code only; not ported to the MCU.
"""

import logging
import multiprocessing as mp
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.neural_network import MLPClassifier

from hr import augment, estimator, evaluate, io

log = logging.getLogger(__name__)

_CFG: dict = {}


def _init(cfg: dict) -> None:
    """Store the config in the worker."""
    _CFG.clear()
    _CFG.update(cfg)


def record_trace(task: tuple) -> tuple:
    """Gate features and tracker HR for every frame up to collect_max_s of one (possibly
    augmented) training row."""
    key, filename, ref, spec, beats = task
    cfg = _CFG
    audio = io.load_wav(cfg, filename)
    if spec is not None:
        audio, ref = augment.apply(cfg, audio, beats, ref, spec)
    audio = audio[: int(cfg["gate"]["collect_max_s"] * cfg["stream"]["fs_hz"])]
    _, trace = estimator.run(cfg, audio, keep_trace=True)
    rows = [(t, hr, feats) for t, hr, _, _, _, feats in trace if feats is not None]
    if not rows:
        return key, np.zeros((0, 0), np.float32), np.zeros(0, bool), np.zeros(0)
    t, hr, feats = zip(*rows)
    ok = evaluate.is_within(cfg, np.array(hr), np.full(len(hr), ref))
    return key, np.stack(feats), ok, np.array(t)


def collect(cfg: dict, rows: pd.DataFrame) -> dict:
    """Features, correctness labels, frame times and row index per row of augment.plan."""
    tasks = list(zip(rows["key"], rows["output_filename"], rows["reference_bpm"], rows["spec"],
                     rows["beats"]))
    out = {"x": [], "y": [], "t": [], "rec": []}
    start = time.time()
    with mp.get_context("spawn").Pool(cfg["eval"]["workers"], _init, (cfg,)) as pool:
        for i, (_, x, y, t) in enumerate(pool.imap(record_trace, tasks, 4), 1):
            if len(y):
                out["x"].append(x)
                out["y"].append(y)
                out["t"].append(t)
                out["rec"].append(np.full(len(y), i - 1))
            if i % 100 == 0 or i == len(tasks):
                log.info("gate traces: %d/%d rows, %.0f s", i, len(tasks),
                         time.time() - start)
    return {k: np.concatenate(v) for k, v in out.items()}


def fit(cfg: dict, data: dict) -> dict:
    """Fit the gate MLP; return numpy weights."""
    g = cfg["gate"]
    mean, std = data["x"].mean(0), data["x"].std(0) + 1e-6
    clf = MLPClassifier(hidden_layer_sizes=(g["hidden"],), alpha=g["l2"], max_iter=g["max_iter"],
                        random_state=cfg["data"]["seed"])
    clf.fit((data["x"] - mean) / std, data["y"])
    return {"mean": mean.astype(np.float32), "std": std.astype(np.float32),
            "w1": clf.coefs_[0].astype(np.float32), "b1": clf.intercepts_[0].astype(np.float32),
            "w2": clf.coefs_[1][:, 0].astype(np.float32),
            "b2": np.float32(clf.intercepts_[1][0])}


def calibration(model, data: dict, predict) -> pd.DataFrame:
    """Predicted vs observed correctness in probability bins."""
    p = np.array([predict(model, x) for x in data["x"]])
    bins = pd.cut(p, [0, 0.5, 0.8, 0.9, 0.95, 0.98, 1.0], include_lowest=True)
    return (pd.DataFrame({"bin": bins, "p": p, "y": data["y"]}).groupby("bin", observed=True)
            .agg(n=("y", "size"), predicted=("p", "mean"), observed=("y", "mean")))


def save(weights: dict, path: Path) -> None:
    """Write weights as .npz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, **weights)
