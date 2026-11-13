#!/usr/bin/env python3
"""
Corpus/synthetic validation of pcg_algorithm_streaming.py (Option 1, the streaming port) against
pcg_algorithm.py (the exact, corpus-validated reference). Classifies each file with BOTH
estimators using the same tail-median convention as rolling_buffer_sim.py, reports the streaming
port's own corpus/synthetic counts, and separately reports how often the two estimators' final
displayed value actually differs on the same file (a direct measure of how much the two
deviations -- continuous filter state, causal declick -- actually move real-world results).
"""
import glob, os, sys, warnings
import numpy as np
from scipy.io import wavfile
import eval_harness as EH
import pcg_algorithm as EXACT
from pcg_algorithm_streaming import StreamingPCGHeartRateEstimator
warnings.filterwarnings("ignore")


def run_file(path, EstimatorCls, fs_expected=4000):
    fs, data = wavfile.read(path)
    data = data.mean(axis=1) if data.ndim > 1 else data
    data = data.astype(np.float64)
    if fs != fs_expected:
        return None
    est = EstimatorCls(fs=fs)
    chunk = int(EXACT.TICK_S * fs)
    rows = []
    for i in range(0, len(data), chunk):
        est.push(data[i:i + chunk])
        rows.append(est.tick())
    tv = [r['shown'] for r in rows[-14:] if r['shown'] is not None]
    return EXACT.median(tv) if tv else None


def classify(shown, gt):
    return ('declined' if shown is None else EH.classify(shown, gt)), shown


def run_real_corpus():
    lab = EH.load_labels()
    files = []
    for sub in sorted(os.listdir(EH.ROOT)):
        sd = os.path.join(EH.ROOT, sub)
        if not os.path.isdir(sd):
            continue
        for w in sorted(glob.glob(os.path.join(sd, "*.wav"))):
            k = EH.wkey(w)
            L = lab.get(k)
            if L is None or not np.isfinite(L["ecg"]):
                continue
            files.append((w, float(L["ecg"])))
    print(f"{len(files)} real-corpus files")
    import collections
    counts_stream = collections.Counter()
    counts_exact = collections.Counter()
    disagreements = []
    for i, (w, gt) in enumerate(files):
        shown_s = run_file(w, StreamingPCGHeartRateEstimator)
        shown_e = run_file(w, EXACT.PCGHeartRateEstimator)
        kind_s, _ = classify(shown_s, gt)
        kind_e, _ = classify(shown_e, gt)
        counts_stream[kind_s] += 1
        counts_exact[kind_e] += 1
        same = (shown_s == shown_e) or (shown_s is not None and shown_e is not None and abs(shown_s - shown_e) < 0.05)
        if not same:
            disagreements.append((os.path.basename(w), gt, shown_e, kind_e, shown_s, kind_s))
        if (i + 1) % 25 == 0:
            print(f"...{i + 1}/{len(files)}  (disagreements so far: {len(disagreements)})")
    print("REAL CORPUS -- streaming:", dict(counts_stream))
    print("REAL CORPUS -- exact:    ", dict(counts_exact))
    print(f"\n{len(disagreements)} files where streaming and exact disagree on final displayed value:")
    for base, gt, se, ke, ss, ks in disagreements:
        print(f"  {base:75s} gt={gt:7.2f}  exact={se} ({ke})   streaming={ss} ({ks})")


def run_synthetic():
    import synth_characterize as SC
    import re
    files = sorted(glob.glob(os.path.join(SC.OUTDIR, "*.wav")))
    print(f"{len(files)} synthetic files")
    fname_re = re.compile(r"hr(\d+)_w([\d.]+)_j([\d.]+)_s(\d+)\.wav")
    import collections
    counts_stream = collections.Counter()
    counts_exact = collections.Counter()
    disagreements = []
    for i, path in enumerate(files):
        base = os.path.basename(path)
        m = fname_re.match(base)
        true_hr = float(m.group(1))
        shown_s = run_file(path, StreamingPCGHeartRateEstimator)
        shown_e = run_file(path, EXACT.PCGHeartRateEstimator)
        kind_s, _ = classify(shown_s, true_hr)
        kind_e, _ = classify(shown_e, true_hr)
        counts_stream[kind_s] += 1
        counts_exact[kind_e] += 1
        same = (shown_s == shown_e) or (shown_s is not None and shown_e is not None and abs(shown_s - shown_e) < 0.05)
        if not same:
            disagreements.append((base, true_hr, shown_e, kind_e, shown_s, kind_s))
        if (i + 1) % 20 == 0:
            print(f"...{i + 1}/{len(files)}  (disagreements so far: {len(disagreements)})")
    print("SYNTHETIC -- streaming:", dict(counts_stream))
    print("SYNTHETIC -- exact:    ", dict(counts_exact))
    print(f"\n{len(disagreements)} files where streaming and exact disagree on final displayed value:")
    for base, gt, se, ke, ss, ks in disagreements:
        print(f"  {base:40s} gt={gt:7.2f}  exact={se} ({ke})   streaming={ss} ({ks})")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--synth":
        run_synthetic()
    else:
        run_real_corpus()
