"""Full 314-file corpus validation harness for the v0.14 re-tuning investigation (2026-08-12),
modeled directly on v011_corpus_validate.py's own methodology (DISPLAYED-bpm classification, the
shipped-relevant output post jump-confirmation/settling-delay/weighted-median combine -- not the
tick-level raw-candidate proxy). Compares a BASELINE driver build (v0.14 unmodified,
TEMPLATE_WEIGHT_ENABLE + TEMPLATE_MATCH_GATED_V2_ENABLE) against a CANDIDATE driver build (same
flags, one constant changed) across the full corpus, then separately sanity-checks both real
recordings (daniel_recovery_test.f32 / daniel_offchest_test.f32, both 5000Hz raw f32) for a tail-bpm
value change.

Usage:
    python v014_corpus_validate.py <baseline.js> <candidate.js> [label]
"""
import os, re, subprocess, sys
from multiprocessing import Pool
from collections import Counter
import numpy as np
from scipy.io import wavfile

sys.path.insert(0, r"C:\Users\daniel\Desktop\pcg_abab")
import corpus_sources as CS

HERE = os.path.dirname(os.path.abspath(__file__))
NODE = r"C:\Users\daniel\emsdk\node\22.16.0_64bit\bin\node.exe"
PAT = re.compile(r"bpm=\s*(\S+)\s+flatline=(\S+)")

BASELINE_BUILD = sys.argv[1]
CANDIDATE_BUILD = sys.argv[2]
LABEL = sys.argv[3] if len(sys.argv) > 3 else CANDIDATE_BUILD


def classify(bpm, gt):
    if bpm is None:
        return "no_display"
    ratio = bpm / gt
    if abs(bpm - gt) <= 5 or abs(ratio - 1) <= 0.05:
        return "correct"
    if abs(ratio - 0.5) <= 0.05:
        return "half"
    if abs(ratio - 2.0) <= 0.05:
        return "double"
    if abs(ratio - 1.0 / 3.0) <= 0.05:
        return "third"
    if abs(ratio - 3.0) <= 0.05:
        return "triple"
    return "other"


def run_one(driver, wav_path, fs, tmp_suffix):
    fs_r, data = wavfile.read(wav_path)
    if data.ndim > 1:
        data = data.mean(axis=1)
    tmp_f32 = os.path.join(HERE, f"_v014_{tmp_suffix}_{os.getpid()}.f32")
    data.astype(np.float32).tofile(tmp_f32)
    try:
        out = subprocess.run([NODE, driver, tmp_f32, str(fs_r)], capture_output=True, text=True, cwd=HERE)
    finally:
        os.remove(tmp_f32)
    bpms = []
    for line in out.stdout.splitlines():
        if not line.startswith("t="):
            continue
        m = PAT.search(line)
        if not m:
            continue
        bpm_s = m.group(1)
        bpms.append(float(bpm_s) if bpm_s != "--" else None)
    return bpms


def run_file(args):
    wav_path, gt, cat, ident = args
    fs_r, _ = wavfile.read(wav_path)
    base_bpms = run_one(os.path.join(HERE, BASELINE_BUILD), wav_path, fs_r, "base")
    cand_bpms = run_one(os.path.join(HERE, CANDIDATE_BUILD), wav_path, fs_r, "cand")
    base_cls = [classify(b, gt) for b in base_bpms]
    cand_cls = [classify(b, gt) for b in cand_bpms]
    return base_cls, cand_cls, gt, cat, ident


def run_real_file(fname, fs=5000.0):
    path = os.path.join(HERE, fname)
    out_b = subprocess.run([NODE, os.path.join(HERE, BASELINE_BUILD), path, str(fs)],
                            capture_output=True, text=True, cwd=HERE)
    out_c = subprocess.run([NODE, os.path.join(HERE, CANDIDATE_BUILD), path, str(fs)],
                            capture_output=True, text=True, cwd=HERE)
    def tail_bpm(out):
        bpms = []
        for line in out.stdout.splitlines():
            if not line.startswith("t="):
                continue
            m = PAT.search(line)
            if m:
                s = m.group(1)
                bpms.append(None if s == "--" else float(s))
        return bpms[-1] if bpms else None, len(bpms)
    tb, nb = tail_bpm(out_b)
    tc, nc = tail_bpm(out_c)
    return tb, nb, tc, nc


if __name__ == "__main__":
    files = CS.load_all_files(include_synth=False)
    print(f"validating {BASELINE_BUILD} (v0.14 baseline) vs {CANDIDATE_BUILD} ({LABEL}) across {len(files)} files...",
          file=sys.stderr)

    base_tally = Counter()
    cand_tally = Counter()
    changed_files = []
    with Pool(16) as pool:
        for i, (base_cls, cand_cls, gt, cat, ident) in enumerate(pool.imap_unordered(run_file, files)):
            base_tally.update(base_cls)
            cand_tally.update(cand_cls)
            if base_cls != cand_cls:
                changed_files.append((ident, gt, cat, Counter(base_cls), Counter(cand_cls)))
            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(files)} files done", file=sys.stderr)

    print(f"\n=== FULL CORPUS TALLY (314 files, DISPLAYED-bpm classification) -- {LABEL} ===")
    print("v0.14 baseline: ", dict(base_tally))
    print(f"{LABEL}:        ", dict(cand_tally))
    print("\ndelta (candidate - baseline):")
    for k in sorted(set(base_tally) | set(cand_tally)):
        d = cand_tally.get(k, 0) - base_tally.get(k, 0)
        print(f"  {k:>12}: {d:+d}")

    n_base_correct = base_tally.get("correct", 0)
    n_cand_correct = cand_tally.get("correct", 0)
    total = sum(base_tally.values())
    print(f"\ncorrect rate: baseline={100*n_base_correct/total:.2f}%  {LABEL}={100*n_cand_correct/total:.2f}%  (n={total} ticks)")

    print(f"\n{len(changed_files)}/{len(files)} files had at least one tick's classification change:")
    for ident, gt, cat, bc, cc in changed_files[:30]:
        print(f"  {ident} (gt={gt}, {cat}): baseline={dict(bc)} -> {LABEL}={dict(cc)}")

    print("\n=== REAL RECORDINGS SANITY CHECK ===")
    for fname in ["daniel_recovery_test.f32", "daniel_offchest_test.f32"]:
        tb, nb, tc, nc = run_real_file(fname)
        print(f"{fname}: baseline tail={tb} ({nb} ticks)  {LABEL} tail={tc} ({nc} ticks)")
