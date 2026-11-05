"""Command line `pcg-hr`: data preparation, training, evaluation, profiling, test vectors.

    pcg-hr split                          grouped train / val / test split
    pcg-hr relabel                        answer key from the ECG leads
    pcg-hr train-net --augment            TCN (block B)
    pcg-hr train-scorer --augment         scorer (block D)
    pcg-hr eval --split test [--stress] [--variant fast]
    pcg-hr figures --runs label=glob,... --out dir
    pcg-hr profile [--format fixed]       PSoC 6 cost model
    pcg-hr vectors --out dir              synthetic golden vectors for the C port
"""

import argparse
import hashlib
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from pcg_hr import (augment, config, cost_model, data, estimator, evaluate, frontend, net,
                    plots, reference, synth, train_net, train_scorer)

log = logging.getLogger("pcg_hr")


def settings_hash(cfg: dict) -> str:
    """Short hash of the augmentation settings and the answer-key file; names caches so changed
    settings or labels never reuse them."""
    key = json.dumps(cfg["augment"], sort_keys=True)
    labels = cfg["paths"]["reference_labels"]
    if labels.is_file():
        key += hashlib.sha1(labels.read_bytes()).hexdigest()
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def training_rows(cfg: dict, with_augment: bool) -> pd.DataFrame:
    """Training-split rows: real recordings, plus augmented copies when requested."""
    df = data.load_split(cfg, "train")
    beats = reference.build_beats(cfg, df) if with_augment else None
    rows = augment.plan(cfg, df, beats)
    log.info("training rows: %d (%d augmented)", len(rows), rows["spec"].notna().sum())
    return rows


def cmd_split(args, cfg: dict) -> None:
    """Create the split manifest."""
    config.preflight(cfg, need_splits=False)
    data.make_splits(cfg)


def cmd_relabel(args, cfg: dict) -> None:
    """Write the answer key: ECG beat count where all leads agree and count >= relabel_min_ratio
    x the EFAST HR; EFAST otherwise."""
    config.preflight(cfg)
    no_labels = config.deep_merge(cfg, {})
    no_labels["paths"]["reference_labels"] = Path("/nonexistent")
    df = data.load_split(no_labels)
    labels = reference.relabel(cfg, df)
    out = cfg["paths"]["reference_labels"]
    out.parent.mkdir(parents=True, exist_ok=True)
    labels.to_csv(out, index=False)
    log.info("answer key: %d of %d recordings use the ECG beat count",
             int((labels["label_source"] == "ecg_rpeaks").sum()), len(labels))


def cmd_train_net(args, cfg: dict) -> None:
    """Build (or load) the training set, train the TCN, export, check streaming equivalence."""
    config.preflight(cfg)
    tag = f"_aug{settings_hash(cfg)}" if args.augment else ""
    cache = cfg["paths"]["cache_dir"] / f"net_data_train{tag}.npz"
    if cache.is_file():
        dataset = dict(np.load(cache))
    else:
        dataset = train_net.build_dataset(cfg, training_rows(cfg, args.augment))
        np.savez(cache, **dataset)
    model = train_net.fit(cfg, dataset)
    weights = train_net.export(model)
    np.savez(cfg["paths"]["models_dir"] / f"{args.out}.npz", **weights)
    x = dataset["x"][0][:, :400]
    streamed = net.process(net.init(cfg, weights), x)[0]
    diff = float(np.abs(streamed - train_net.torch_activation(model, cfg, x)).max())
    log.info("saved %s.npz: %d weights; streaming vs torch max difference %.1e", args.out,
             sum(v.size for v in weights.values()), diff)


def cmd_train_scorer(args, cfg: dict) -> None:
    """Collect scorer inputs with the current TCN (cached by name) and fit the scorer."""
    config.preflight(cfg)
    cache = cfg["paths"]["cache_dir"] / f"scorer_feats_{args.features_name}.npz"
    if cache.is_file():
        dataset = dict(np.load(cache))
    else:
        dataset = train_scorer.collect(cfg, training_rows(cfg, args.augment))
        np.savez(cache, **dataset)
    train_scorer.save(train_scorer.fit(cfg, dataset),
                      cfg["paths"]["models_dir"] / f"{args.out}.npz")
    log.info("saved %s.npz", args.out)


def cmd_eval(args, cfg: dict) -> None:
    """Evaluate on a split (optionally the stress set); write the run; log the key results."""
    config.preflight(cfg)
    df = data.load_split(cfg, args.split)
    stress = None
    if args.stress:
        df = df.sample(n=min(cfg["stress"]["n_records"], len(df)),
                       random_state=cfg["stress"]["seed"])
        files, stress = synth.stress_tasks(df["output_filename"].tolist(), cfg)
        df = df.set_index("output_filename").loc[files].reset_index()
    rec = evaluate.run_records(cfg, df.reset_index(drop=True), stress)
    summary = evaluate.summarize(cfg, rec)
    summary["targets_passed"] = int(sum(evaluate.check_targets(cfg, summary).values()))
    if cfg["paths"]["ecg_dir"].is_dir() and not args.stress:
        summary.update(evaluate.windowed_scores(cfg, rec, reference.build_beats(cfg, df)))
    tag = "_".join(x for x in (args.tag, "stress" if args.stress else "") if x)
    out = evaluate.write_run(cfg, rec, summary, tag, args.split)
    if args.stress:
        rec["noise"] = [s[0] for s in rec["stress"]]
        rec["snr_db"] = [s[1] for s in rec["stress"]]
        by = rec.groupby(["noise", "snr_db"]).apply(
            lambda g: pd.Series(evaluate.summarize(cfg, g)), include_groups=False)
        by.to_csv(out / "stress_by_condition.csv")
    log.info("%s: first within %.3f, half/double %.3f, median answer %.2f s (HR >= %d BPM), "
             "targets %d/8 -> %s", cfg["name"], summary["first_within"],
             summary["first_half_double"], summary["ttfr_median_hr_ge_low_s"],
             cfg["targets"]["low_hr_bpm"], summary["targets_passed"], out.name)


def cmd_figures(args, cfg: dict) -> None:
    """Report figures and a per-stratum table for runs given as label=run_glob pairs; main
    group only (recordings labelled with the ECG beat count are reported separately)."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    runs = {}
    for item in args.runs.split(","):
        label, pattern = item.split("=", 1)
        rec = plots.load_run(sorted(cfg["paths"]["runs_dir"].glob(pattern))[-1])
        if "label_source" in rec:
            rec = rec[rec["label_source"] != "ecg_rpeaks"].reset_index(drop=True)
        runs[label] = rec
    plots.accuracy_vs_time(cfg, runs, out / "accuracy_vs_time.png")
    plots.ttfr_cdf(runs, out / "ttfr_cdf.png")
    plots.band_accuracy(runs, out / "band_accuracy.png")
    meta = data.load_split(cfg)
    meta["hr_band"] = pd.cut(meta["reference_bpm"], [0, 50, 60, 100, 140, 250])
    evaluate.strata(next(iter(runs.values())), meta,
                    ["hr_band", "firmware_revision", "mains_filter"]).to_csv(
        out / "strata_first_run.csv", index=False)
    log.info("figures written for %d runs", len(runs))


def cmd_profile(args, cfg: dict) -> None:
    """Print the cost model per module and the totals against the budget."""
    if args.format:
        cfg = config.deep_merge(cfg, {"profile": {"number_format": args.format}})
    table = cost_model.profile(cfg)
    print(table.drop(columns=["ram_detail", "per_frame"]).round(0).to_string(index=False))
    print(json.dumps(cost_model.totals(table, cfg), indent=1, default=float))


VECTOR_CASES = [("hr72_clean", 72, {}), ("hr45_clean", 45, {}), ("hr150_clean", 150, {}),
                ("hr110_s2_equal", 110, {"s2_ratio": 1.0}),
                ("hr72_irregular", 72, {"interval_cv": 0.15})]


def cmd_vectors(args, cfg: dict) -> None:
    """Synthetic golden vectors per case (.npz): int16 input (peak 16000), band log energies
    (block A), activation (block B), per-frame tracker HR, confidence, frame HR and frame
    confidence (blocks C-E), and readings (block F), for stage-by-stage checks of the C port."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fs, block = cfg["stream"]["fs_hz"], cfg["stream"]["block_samples"]
    for name, hr, kw in VECTOR_CASES:
        audio, _ = synth.pcg(hr, duration_s=10.0, fs=fs, seed=hr, **kw)
        audio = synth.add_noise(audio, fs, "white", 25.0, hr)
        pcm = np.round(audio / np.abs(audio).max() * 16000).astype(np.int16)
        x = pcm.astype(np.float32) / 32768.0
        state = estimator.init(cfg, keep_trace=True)
        fe, nn = frontend.init(cfg), net.init(cfg)
        env, act, readings = [], [], []
        for start in range(0, x.size - block + 1, block):
            env.append(frontend.process(fe, x[start:start + block]))
            act.append(net.process(nn, env[-1]))
            reading = estimator.process(state, x[start:start + block])
            if reading is not None:
                readings.append(reading)
        trace = np.array(state.trace)
        np.savez(out / f"{name}.npz", pcm=pcm, band_log_energy=np.hstack(env),
                 activation=np.hstack(act)[0], trace=trace, readings=np.array(readings),
                 true_bpm=hr, input_scale=1.0 / 32768.0)
        log.info("vector %s: %d frames, first reading %.1f BPM at %.2f s", name, len(trace),
                 readings[0][1], readings[0][0])


def main() -> None:
    """Parse arguments and dispatch."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--variant", default=None, help="config variant, e.g. fast")
    sub = parser.add_subparsers(required=True)
    sub.add_parser("split").set_defaults(func=cmd_split)
    sub.add_parser("relabel").set_defaults(func=cmd_relabel)
    tn = sub.add_parser("train-net")
    tn.add_argument("--augment", action="store_true")
    tn.add_argument("--out", default="hr_tcn_new")
    tn.set_defaults(func=cmd_train_net)
    ts = sub.add_parser("train-scorer")
    ts.add_argument("--augment", action="store_true")
    ts.add_argument("--features-name", required=True)
    ts.add_argument("--out", default="hr_scorer_new")
    ts.set_defaults(func=cmd_train_scorer)
    ev = sub.add_parser("eval")
    ev.add_argument("--split", default="val")
    ev.add_argument("--stress", action="store_true")
    ev.add_argument("--tag", default="")
    ev.set_defaults(func=cmd_eval)
    fg = sub.add_parser("figures")
    fg.add_argument("--runs", required=True, help="label=run_glob,label=run_glob,...")
    fg.add_argument("--out", required=True)
    fg.set_defaults(func=cmd_figures)
    pr = sub.add_parser("profile")
    pr.add_argument("--format", choices=["float32", "fixed"], default=None)
    pr.set_defaults(func=cmd_profile)
    vc = sub.add_parser("vectors")
    vc.add_argument("--out", required=True)
    vc.set_defaults(func=cmd_vectors)
    args = parser.parse_args()
    args.func(args, config.load(variant=args.variant))


if __name__ == "__main__":
    main()
