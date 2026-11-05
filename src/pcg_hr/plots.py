"""Report figures from evaluation runs: accuracy vs time, TTFR distribution, HR-band accuracy."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # file output only; no display backend
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from pcg_hr import evaluate  # noqa: E402

SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e3de"


def load_run(run_dir: Path) -> pd.DataFrame:
    """Per-record results with their readings list."""
    rec = pd.read_csv(run_dir / "per_record.csv")
    readings = json.loads((run_dir / "readings.json").read_text())
    rec["readings"] = [[tuple(np.nan if v is None else v for v in x) for x in r["readings"]]
                       for r in readings]
    return rec


def style(ax, title: str, xlabel: str, ylabel: str) -> None:
    """Recessive grid and axes, primary-ink title."""
    ax.set_title(title, loc="left", color=INK, fontsize=11)
    ax.set_xlabel(xlabel, color=MUTED)
    ax.set_ylabel(ylabel, color=MUTED)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=MUTED)


def accuracy_vs_time(cfg: dict, runs: dict, out: Path) -> None:
    """Share of recordings whose latest reading is within tolerance, vs time; one line per run."""
    times = np.arange(1.0, 8.01, 0.25)
    fig, ax = plt.subplots(figsize=(7, 4))
    for color, (label, rec) in zip(SERIES_COLORS, runs.items()):
        ax.plot(times, [100 * evaluate.accuracy_at(cfg, rec, t) for t in times], color=color,
                linewidth=2, label=label)
    ax.axvline(3.0, color=MUTED, linewidth=1, linestyle="--")
    style(ax, "Correct reading available by time t", "time since start (s)",
          "recordings with a correct reading (%)")
    ax.set_ylim(0, 100)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def ttfr_cdf(runs: dict, out: Path) -> None:
    """Cumulative share of recordings with a first reading by time t."""
    fig, ax = plt.subplots(figsize=(7, 4))
    for color, (label, rec) in zip(SERIES_COLORS, runs.items()):
        ttfr = np.sort(rec["ttfr_s"].to_numpy())
        ax.step(ttfr, 100 * np.arange(1, ttfr.size + 1) / ttfr.size, where="post", color=color,
                linewidth=2, label=label)
    ax.axvline(3.0, color=MUTED, linewidth=1, linestyle="--")
    style(ax, "Time to first reading", "time since start (s)", "recordings with a reading (%)")
    ax.set_xlim(0, 8)
    ax.set_ylim(0, 100)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def band_accuracy(runs: dict, out: Path, edges: tuple = (0, 50, 70, 100, 130, 160, 250)) -> None:
    """First-reading accuracy per reference HR band; grouped bars, one colour per run."""
    labels = [f"{lo}-{hi}" for lo, hi in zip(edges[:-1], edges[1:])]
    fig, ax = plt.subplots(figsize=(7, 4))
    width = 0.8 / len(runs)
    for k, (color, (label, rec)) in enumerate(zip(SERIES_COLORS, runs.items())):
        band = pd.cut(rec["reference_bpm"], edges, labels=labels)
        acc = rec.groupby(band, observed=False)["first_within"].mean() * 100
        ax.bar(np.arange(len(labels)) + (k - (len(runs) - 1) / 2) * width, acc.to_numpy(),
               width * 0.9, color=color, label=label)
    ax.set_xticks(np.arange(len(labels)), labels)
    style(ax, "First-reading accuracy by reference HR", "reference HR band (BPM)",
          "first reading within tolerance (%)")
    ax.set_ylim(0, 100)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
