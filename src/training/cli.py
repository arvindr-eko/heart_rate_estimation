"""Command line `pcg-hr`.

    pcg-hr prepare                               split (created once) and ECG label table
    pcg-hr train [--scorer-only] [--out NAME]    TCN, scorer, calibration -> models/NAME_*.npz
    pcg-hr train --calibrate-only [--weights NAME]  refit the readout calibration only
    pcg-hr evaluate [--split val|test] [--stress] [--weights NAME] [--tag TAG]
    pcg-hr evaluate --data SNAPSHOT_DIR       another snapshot (e.g. unfiltered), evaluation only
    pcg-hr vectors                            per-block reference vectors for the C port
    pcg-hr profile [--format float32|fixed] [--weights NAME]
"""

import argparse
import json
import logging
from pathlib import Path

from training import config, cost_model, data, evaluate, labels, train, vectors

log = logging.getLogger("pcg-hr")


def cmd_prepare(args: argparse.Namespace, cfg: dict) -> None:
    """Split manifest (created once, never overwritten) and the ECG label table."""
    config.preflight(cfg, need_splits=False)
    if cfg["paths"]["split_manifest"].is_file():
        log.info("split kept: %s", cfg["paths"]["split_manifest"].name)
    else:
        data.make_splits(cfg)
    labels.write_table(cfg)


def cmd_train(args: argparse.Namespace, cfg: dict) -> None:
    """Train and save NAME_tcn.npz, NAME_scorer.npz and NAME_calibration.npz, or refit the
    calibration of the configured model."""
    config.preflight(cfg)
    if args.calibrate_only:
        train.calibrate(cfg)
    else:
        train.run(cfg, args.out, args.scorer_only)


def cmd_evaluate(args: argparse.Namespace, cfg: dict) -> None:
    """Evaluate one split (or its stress set); log the targets that failed and the run folder."""
    config.preflight(cfg)
    if args.data:
        ext, df = labels.external(cfg, args.data)
        out, _, checks = evaluate.run(ext, f"ext_{Path(args.data).name}", args.stress, args.tag,
                                      df=df)
    else:
        out, _, checks = evaluate.run(cfg, args.split, args.stress, args.tag)
    failed = {k: v for k, v in checks.items() if not v["pass"]}
    for key, check in failed.items():
        log.info("FAIL %s: %s", key, ", ".join(f"{k} {v:.3g}" for k, v in check.items()
                                               if k in ("value", "target")))
    log.info("targets passed %d/%d -> %s", len(checks) - len(failed), len(checks), out.name)


def cmd_vectors(args: argparse.Namespace, cfg: dict) -> None:
    """Write the per-block reference vectors for the C port."""
    config.preflight(cfg)
    vectors.write(cfg)


def cmd_profile(args: argparse.Namespace, cfg: dict) -> None:
    """Print the cost model per block and the totals against the budget."""
    table = cost_model.profile(cfg, args.format or "float32")
    print(table.drop(columns=["ram_detail", "per_frame"]).round(0).to_string(index=False))
    print(json.dumps(cost_model.totals(table, cfg), indent=1, default=float))


def main() -> None:
    """Parse arguments and dispatch."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)
    sub.add_parser("prepare").set_defaults(func=cmd_prepare)
    tr = sub.add_parser("train")
    tr.add_argument("--scorer-only", action="store_true", help="keep the configured TCN")
    tr.add_argument("--out", default="new", help="model name of the new weights")
    tr.add_argument("--calibrate-only", action="store_true",
                    help="refit the readout calibration of the configured model on validation")
    tr.set_defaults(func=cmd_train)
    ev = sub.add_parser("evaluate")
    ev.add_argument("--split", default="val", choices=["train", "val", "test"])
    ev.add_argument("--stress", action="store_true", help="noise stress set of the split")
    ev.add_argument("--tag", default="", help="suffix of the run folder name")
    ev.add_argument("--data", default=None, help="snapshot folder (manifest.csv, wavs/, ecg/)")
    ev.set_defaults(func=cmd_evaluate)
    sub.add_parser("vectors").set_defaults(func=cmd_vectors)
    pr = sub.add_parser("profile")
    pr.add_argument("--format", choices=["float32", "fixed"], default=None)
    pr.set_defaults(func=cmd_profile)
    for p in (tr, ev, pr):
        p.add_argument("--weights", default=None, help="model name (default: config weights)")
    args = parser.parse_args()
    cfg = config.load()
    if getattr(args, "weights", None):
        cfg["weights"] = args.weights
    args.func(args, cfg)


if __name__ == "__main__":
    main()
