"""Command line: splits, evaluation runs and run comparison.

    python scripts/hr_cli.py split
    python scripts/hr_cli.py eval --variants trk_nccf,acf_w3 --split val
    python scripts/hr_cli.py eval --variants trk_nccf --split test --stress
    python scripts/hr_cli.py train-salience --features-name v1 --out sal_v1
    python scripts/hr_cli.py compare --runs-glob '*_val'
"""

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # project root on the path

from hr import (augment, config, evaluate, gate, gate_train, io, net,  # noqa: E402
                net_train, periodicity, plots, reference, salience_train, synth)

log = logging.getLogger("hr_cli")


def parse_overrides(items: list[str]) -> dict:
    """Turn ['a.b=1', 'c=x'] into a nested dict with YAML-typed values."""
    out: dict = {}
    for item in items or []:
        key, value = item.split("=", 1)
        node = out
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = yaml.safe_load(value)
    return out


def cmd_split(args) -> None:
    """Create the split manifest."""
    cfg = config.load()
    config.preflight(cfg, need_splits=False)
    io.make_splits(cfg)


def cmd_eval(args) -> None:
    """Evaluate variants on a split (optionally the stress set); write one run per variant."""
    for name in args.variants.split(","):
        cfg = config.load(variant=None if name == "base" else name,
                          overrides=parse_overrides(args.set))
        if args.name:
            cfg["name"] = args.name
        config.preflight(cfg)
        df = io.load_split(cfg, args.split)
        if args.limit:
            df = df.sample(n=min(args.limit, len(df)), random_state=cfg["data"]["seed"])
        stress = None
        if args.stress:
            df = df.sample(n=min(cfg["stress"]["n_records"], len(df)),
                           random_state=cfg["stress"]["seed"])
            files, stress = synth.stress_tasks(df["output_filename"].tolist(), cfg)
            df = df.set_index("output_filename").loc[files].reset_index()
        rec = evaluate.run_records(cfg, df.reset_index(drop=True), stress)
        summary = evaluate.summarize(cfg, rec)
        summary["targets_passed"] = int(sum(evaluate.check_targets(cfg, summary).values()))
        out = evaluate.write_run(cfg, rec, summary, args.tag or ("stress" if args.stress else ""),
                                 args.split)
        if args.stress:
            rec["noise"] = [s[0] for s in rec["stress"]]
            rec["snr_db"] = [s[1] for s in rec["stress"]]
            by = rec.groupby(["noise", "snr_db"]).apply(
                lambda g: pd.Series(evaluate.summarize(cfg, g)), include_groups=False)
            by.to_csv(out / "stress_by_condition.csv")
        log.info("%s: first_within %.3f steady_within %.3f coverage %.3f ttfr_median %.2f s "
                 "half_double %.3f -> %s", cfg["name"], summary["first_within"],
                 summary["steady_within"], summary["coverage"], summary["ttfr_median_s"],
                 summary["first_half_double"], out.name)


def settings_hash(settings: dict, cfg: dict | None = None) -> str:
    """Short hash of a config section (and of the corrected-label file when cfg is given);
    names caches so changed settings or labels never reuse them."""
    key = json.dumps(settings, sort_keys=True)
    labels = cfg["paths"].get("reference_labels") if cfg else None
    if labels is not None and labels.is_file():
        key += hashlib.sha1(labels.read_bytes()).hexdigest()
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def training_rows(cfg: dict, split: str, with_augment: bool):
    """Training rows of a split: real recordings, plus augmented copies when requested."""
    df = io.load_split(cfg, split)
    beats = reference.build_beats(cfg, df) if with_augment else None
    rows = augment.plan(cfg, df, beats)
    log.info("training rows: %d (%d augmented)", len(rows), rows["spec"].notna().sum())
    return rows


def cmd_train_salience(args) -> None:
    """Collect features on a split and fit the salience MLP; cache features by name."""
    cfg = config.load(variant=args.variant or None, overrides=parse_overrides(args.set))
    config.preflight(cfg)
    cache = cfg["paths"]["cache_dir"] / f"salience_feats_{args.features_name}.npz"
    if cache.is_file():
        data = dict(np.load(cache))
    else:
        collect_cfg = config.deep_merge(cfg, {"salience": {"enabled": False}})
        data = salience_train.collect(collect_cfg, training_rows(cfg, args.split, args.augment))
        np.savez(cache, **data)
    weights = salience_train.fit(cfg, data)
    salience_train.save(weights, cfg["paths"]["models_dir"] / f"{args.out}.npz")
    log.info("salience model saved: %s.npz", args.out)


def cmd_train_net(args) -> None:
    """Build (or load) the pseudo-labelled dataset, train the network, export, verify streaming."""
    cfg = config.load(variant=args.variant or None, overrides=parse_overrides(args.set))
    config.preflight(cfg)
    tag = f"_aug{settings_hash(cfg['augment'], cfg)}" if args.augment else ""
    cache = cfg["paths"]["cache_dir"] / f"net_data_{args.split}{tag}.npz"
    if cache.is_file():
        data = dict(np.load(cache))
    else:
        data = net_train.build_dataset(cfg, training_rows(cfg, args.split, args.augment))
        np.savez(cache, **data)
    model = net_train.fit(cfg, data)
    weights = net_train.export(model, cfg)
    path = cfg["paths"]["models_dir"] / f"{args.out}.npz"
    np.savez(path, **weights)
    # Streaming numpy inference must equal the whole-sequence torch output
    x = data["x"][0][:, :400]
    basis = (net.hr_basis(periodicity.hr_grid(cfg), cfg["net"]["hr_basis"])
             if cfg["net"]["head"] == "hr" else None)
    streamed = net.process(net.init(cfg, weights), x, basis)
    max_diff = float(np.abs(streamed - net_train.torch_activation(model, cfg, x)).max())
    log.info("net saved: %s.npz, %d weights, streaming vs torch max diff %.2e", args.out,
             sum(v.size for v in weights.values()), max_diff)


def cmd_train_gate(args) -> None:
    """Collect per-frame traces on a split, fit the gate, print calibration on a check split."""
    cfg = config.load(variant=args.variant, overrides=parse_overrides(args.set))
    config.preflight(cfg)
    collect_cfg = config.deep_merge(cfg, {"release": {"confidence": "frame"}})
    data = gate_train.collect(collect_cfg, augment.plan(cfg, io.load_split(cfg, args.split)))
    weights = gate_train.fit(cfg, data)
    path = cfg["paths"]["models_dir"] / f"{args.out}.npz"
    gate_train.save(weights, path)
    model = gate.load(path)
    check_rows = augment.plan(cfg, io.load_split(cfg, args.check_split))
    check = gate_train.collect(collect_cfg, check_rows)
    log.info("gate saved: %s.npz; calibration on %s:\n%s", args.out, args.check_split,
             gate_train.calibration(model, check, gate.predict).round(3).to_string())


def subset_rows(data: dict, keep: np.ndarray) -> dict:
    """Rows of a network dataset selected by a boolean mask."""
    return {k: v[keep] for k, v in data.items()}


def cmd_train_crossfit_gate(args) -> None:
    """Cross-fitted release gate: per training fold, network and scorer trained on the other
    folds (with augmentation), traces collected on the held-out fold; the gate is fitted on all
    out-of-fold traces, so its inputs come from models that never saw those recordings."""
    cfg = config.load(variant=args.variant)
    config.preflight(cfg)
    base = config.deep_merge(cfg, {"release": {"confidence": "frame"}})
    df = io.load_split(cfg, "train")
    rows = augment.plan(cfg, df, reference.build_beats(cfg, df))
    rows["fold"] = rows["output_filename"].map(dict(zip(df["output_filename"], df["fold"])))
    tag = settings_hash(cfg["augment"], cfg)
    cache = cfg["paths"]["cache_dir"] / f"net_data_train_aug{tag}.npz"
    if cache.is_file():
        net_data = dict(np.load(cache))
    else:
        net_data = net_train.build_dataset(cfg, rows)
        np.savez(cache, **net_data)
    row_fold = rows.set_index("key").loc[net_data["names"], "fold"].to_numpy()
    traces = []
    models = cfg["paths"]["models_dir"]
    for fold in sorted(rows["fold"].unique()):
        log.info("cross-fit fold %d: network", fold)
        model = net_train.fit(cfg, subset_rows(net_data, row_fold != fold))
        net_name = f"{args.out}_net_f{fold}.npz"
        np.savez(models / net_name, **net_train.export(model, cfg))
        fold_cfg = config.deep_merge(base, {"net": {"weights": net_name}})
        log.info("cross-fit fold %d: scorer", fold)
        collect_cfg = config.deep_merge(fold_cfg, {"salience": {"enabled": False}})
        feats = salience_train.collect(collect_cfg, rows[rows["fold"] != fold])
        sal_name = f"{args.out}_sal_f{fold}.npz"
        salience_train.save(salience_train.fit(fold_cfg, feats), models / sal_name)
        del feats
        log.info("cross-fit fold %d: traces", fold)
        fold_cfg = config.deep_merge(fold_cfg, {"salience": {"weights": sal_name}})
        traces.append(gate_train.collect(fold_cfg, rows[rows["fold"] == fold]))
    data = {k: np.concatenate([tr[k] for tr in traces]) for k in traces[0]}
    gate_train.save(gate_train.fit(cfg, data), models / f"{args.out}.npz")
    val = gate_train.collect(base, augment.plan(cfg, io.load_split(cfg, "val")))
    log.info("gate saved: %s.npz from %d frames; calibration on val:\n%s", args.out,
             len(data["y"]), gate_train.calibration(gate.load(models / f"{args.out}.npz"), val,
                                                   gate.predict).round(3).to_string())


def cmd_compare(args) -> None:
    """Table of summaries and resource estimates for runs matching a glob."""
    cfg = config.load()
    rows = []
    for run in sorted(cfg["paths"]["runs_dir"].glob(args.runs_glob)):
        if not (run / "summary.json").is_file():
            continue
        summary = json.loads((run / "summary.json").read_text())
        info = json.loads((run / "run_info.json").read_text())
        rows.append({"run": run.name, "variant": info["variant"], **summary,
                     **{f"res_{k}": v for k, v in info["resources"].items()}})
    table = pd.DataFrame(rows)
    out = Path(args.out) if args.out else None
    if out:
        table.to_csv(out, index=False)
    cols = ["variant", "first_within", "steady_within", "first_half_double", "coverage",
            "ttfr_median_s", "ttfr_p90_s", "acc_at_2s", "acc_at_3s", "res_mips", "res_ram_bytes"]
    print(table[[c for c in cols if c in table]].to_string(index=False, float_format="%.3f"))


def run_table(cfg: dict, runs_glob: str) -> pd.DataFrame:
    """Summaries and resource estimates of all runs matching the glob."""
    rows = []
    for run in sorted(cfg["paths"]["runs_dir"].glob(runs_glob)):
        if (run / "summary.json").is_file():
            info = json.loads((run / "run_info.json").read_text())
            rows.append({"run": run.name, "variant": info["variant"],
                         **json.loads((run / "summary.json").read_text()),
                         **{f"res_{k}": v for k, v in info["resources"].items()}})
    return pd.DataFrame(rows)


def cmd_select(args) -> None:
    """Best run per family (variant-name prefix): max first-reading accuracy subject to the
    coverage and TTFR targets; families with no feasible run show their best-coverage run."""
    cfg = config.load()
    tg = cfg["targets"]
    table = run_table(cfg, args.runs_glob)
    table["family"] = table["variant"].str.rsplit("_", n=args.strip, expand=True)[0]
    table["feasible"] = ((table["coverage"] >= tg["coverage_min"])
                         & (table["ttfr_median_hr_ge_low_s"] <= tg["ttfr_median_s"])
                         & (table["ttfr_p95_hr_ge_low_s"] <= tg["ttfr_p95_s"])
                         & (table["ttfr_max_s"] <= tg["ttfr_max_s"]))
    best = (table.sort_values(["feasible", "first_within"], ascending=False)
            .groupby("family").head(1).sort_values("first_within", ascending=False))
    cols = ["variant", "feasible", "first_within", "first_within_hr_ge_low",
            "first_within_low_hr", "steady_within", "first_half_double", "coverage",
            "ttfr_median_hr_ge_low_s", "ttfr_p95_hr_ge_low_s", "ttfr_median_low_hr_s",
            "ttfr_max_s", "res_mips", "res_ram_bytes"]
    print(best[cols].to_string(index=False, float_format="%.3f"))


def cmd_figures(args) -> None:
    """Report figures and a per-stratum table for runs given as label=run_glob pairs."""
    cfg = config.load()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    runs = {}
    for item in args.runs.split(","):
        label, pattern = item.split("=", 1)
        runs[label] = plots.load_run(sorted(cfg["paths"]["runs_dir"].glob(pattern))[-1])
    plots.accuracy_vs_time(cfg, runs, out / "accuracy_vs_time.png")
    plots.ttfr_cdf(runs, out / "ttfr_cdf.png")
    plots.band_accuracy(runs, out / "band_accuracy.png")
    meta = io.load_split(cfg)
    meta["hr_band"] = pd.cut(meta["reference_bpm"], [0, 40, 60, 100, 140, 170, 250])
    first = next(iter(runs.values()))
    evaluate.strata(cfg, first, meta, ["hr_band", "firmware_revision", "mains_filter",
                                       "platform"] if "platform" in meta else
                    ["hr_band", "firmware_revision", "mains_filter"]).to_csv(
        out / "strata_first_run.csv", index=False)
    log.info("figures written for %d runs", len(runs))


def cmd_relabel(args) -> None:
    """Write corrected labels: ECG R-peak HR where all leads agree and EFAST is out of tolerance."""
    cfg = config.load(overrides={"paths": {"reference_labels": "output/labels/none.csv"}})
    config.preflight(cfg)
    df = io.load_split(cfg)
    labels = reference.relabel(cfg, df)
    out = config.PROJECT_ROOT / "output/labels/reference_v2.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    labels.to_csv(out, index=False)
    changed = labels[labels["label_source"] == "ecg_rpeaks"].merge(df[["output_filename", "split"]])
    log.info("relabelled %d of %d recordings (leads agree on %d); by split %s", len(changed),
             len(labels), int(labels["leads_agree"].sum()),
             changed["split"].value_counts().to_dict())


def cmd_regrade(args) -> None:
    """Score existing runs against the windowed ECG reference; write summary_win.json each."""
    cfg = config.load()
    config.preflight(cfg)
    meta = io.load_split(cfg)
    beats = reference.build_beats(cfg, meta)
    efast = dict(zip(meta["output_filename"], meta["reference_bpm"]))
    rows = []
    for run in sorted(cfg["paths"]["runs_dir"].glob(args.runs_glob)):
        run_cfg = config.deep_merge(cfg, {"targets": yaml.safe_load(
            (run / "config.yml").read_text())["targets"]})
        rec = evaluate.add_windowed_scores(run_cfg, plots.load_run(run), beats, efast)
        win = evaluate.windowed_summary(run_cfg, rec, beats)
        (run / "summary_win.json").write_text(json.dumps(win, indent=1))
        summary = json.loads((run / "summary.json").read_text())
        rows.append({"run": run.name, "first_within": summary["first_within"], **win})
    cols = ["run", "first_within", "win_n", "win_first_within", "win_first_within_hr_ge_low",
            "win_first_within_low_hr", "win_readings_within", "win_acc_at_3s", "win_acc_at_4s",
            "win_acc_at_6s", "win_acc_at_8s"]
    print(pd.DataFrame(rows)[cols].to_string(index=False, float_format="%.3f"))


def main() -> None:
    """Parse arguments and dispatch."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)
    sub.add_parser("split").set_defaults(func=cmd_split)
    ev = sub.add_parser("eval")
    ev.add_argument("--variants", required=True)
    ev.add_argument("--split", default="val")
    ev.add_argument("--stress", action="store_true")
    ev.add_argument("--limit", type=int, default=0)
    ev.add_argument("--set", nargs="*", help="overrides, e.g. tracker.obs_beta=4")
    ev.add_argument("--name", default="", help="run name replacing the variant name")
    ev.add_argument("--tag", default="")
    ev.set_defaults(func=cmd_eval)
    trn = sub.add_parser("train-salience")
    trn.add_argument("--variant", default="")
    trn.add_argument("--split", default="train")
    trn.add_argument("--features-name", required=True)
    trn.add_argument("--out", required=True)
    trn.add_argument("--set", nargs="*")
    trn.add_argument("--augment", action="store_true")
    trn.set_defaults(func=cmd_train_salience)
    tnn = sub.add_parser("train-net")
    tnn.add_argument("--variant", default="")
    tnn.add_argument("--split", default="train")
    tnn.add_argument("--out", required=True)
    tnn.add_argument("--set", nargs="*")
    tnn.add_argument("--augment", action="store_true")
    tnn.set_defaults(func=cmd_train_net)
    fig = sub.add_parser("figures")
    fig.add_argument("--runs", required=True, help="label=run_glob,label=run_glob,...")
    fig.add_argument("--out", required=True)
    fig.set_defaults(func=cmd_figures)
    sub.add_parser("relabel").set_defaults(func=cmd_relabel)
    rg = sub.add_parser("regrade")
    rg.add_argument("--runs-glob", required=True)
    rg.set_defaults(func=cmd_regrade)
    sel = sub.add_parser("select")
    sel.add_argument("--runs-glob", default="*_tune")
    sel.add_argument("--strip", type=int, default=2, help="name tokens removed to get the family")
    sel.set_defaults(func=cmd_select)
    tg = sub.add_parser("train-gate")
    tg.add_argument("--variant", required=True)
    tg.add_argument("--split", default="val_a")
    tg.add_argument("--check-split", default="val_b")
    tg.add_argument("--out", required=True)
    tg.add_argument("--set", nargs="*")
    tg.set_defaults(func=cmd_train_gate)
    cf = sub.add_parser("train-crossfit-gate")
    cf.add_argument("--variant", required=True)
    cf.add_argument("--out", required=True)
    cf.set_defaults(func=cmd_train_crossfit_gate)
    cmp = sub.add_parser("compare")
    cmp.add_argument("--runs-glob", default="*")
    cmp.add_argument("--out", default="")
    cmp.set_defaults(func=cmd_compare)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
