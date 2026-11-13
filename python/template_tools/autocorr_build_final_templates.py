"""Build the FINAL per-bpm templates for the C implementation (2026-08-09), from the FULL corpus
(not the train/test split used in autocorr_template_library_prototype.py to validate the method --
once validated, the shipped template should use all available data, matching normal practice; the
train/test numbers already recorded in memory remain the evidence base for "this works").
Same extraction as before: win_r.Sw (raw, pre-detrend autocorrelation) for 'correct' raw-candidate
ticks, bucketed by ground truth into 10bpm buckets, mean per bucket -- but keeps the RAW (not
max-normalized) mean this time, since Pearson correlation is scale-invariant so it doesn't matter
for the C-side check, and skipping normalization here keeps this script simpler.

BUCKET_WIDTH 10->5 (2026-08-10), per Daniel's "the templates we are using are only every 10bpm...
if we added those other templates we might get better matching... a 95bpm peak is being compared
against 100bpm which might have a lower Pearson correlation than it would against a 95 template."
"""
import os, subprocess, sys
from multiprocessing import Pool
from collections import defaultdict
import numpy as np
from scipy.io import wavfile

sys.path.insert(0, r"C:\Users\daniel\Desktop\pcg_abab")
import corpus_sources as CS

HERE = os.path.dirname(os.path.abspath(__file__))
NODE = r"C:\Users\daniel\emsdk\node\22.16.0_64bit\bin\node.exe"
DRIVER = os.path.join(HERE, "autocorr_dump.js")
# N_CURVE = S_LEN_V = HI_LAG_V - LO_LAG_V for whatever geometry autocorr_dump.js was built with.
# 96 (24-120, 400Hz/DEC_STRIDE=10) through revision 0.13; 80 (20-100, 333.333Hz/DEC_STRIDE=12) as
# of revision 0.14's DEC_STRIDE rescale (2026-08-11) -- MUST match the compiled combo13_stream.c's
# own S_LEN_V exactly (autocorr_dump.js's own combo13_wasm_dbg_autocorr_max() reports it at
# runtime; this constant just needs to match by hand since the CSV column count is fixed at parse
# time here).
N_CURVE = 80
BUCKET_WIDTH = 5
MIN_TICKS_PER_BUCKET = 50


def classify(bpm, gt):
    if bpm is None:
        return "no_display"
    ratio = bpm / gt
    if abs(bpm - gt) <= 5 or abs(ratio - 1) <= 0.05:
        return "correct"
    return "other"


def run_file(args):
    wav_path, gt, cat, ident = args
    fs_r, data = wavfile.read(wav_path)
    if data.ndim > 1:
        data = data.mean(axis=1)
    tmp_f32 = os.path.join(HERE, f"_tmpl_tmp_{os.getpid()}_{abs(hash(ident))%100000}.f32")
    data.astype(np.float32).tofile(tmp_f32)
    try:
        out = subprocess.run([NODE, DRIVER, tmp_f32, str(fs_r)], capture_output=True, text=True, cwd=HERE)
    finally:
        os.remove(tmp_f32)

    bucket = round(gt / BUCKET_WIDTH) * BUCKET_WIDTH
    curves, ac_lo_seen = [], None
    for line in out.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) != 8 + 2 * N_CURVE:
            continue
        t, bpm_valid, bpm, raw_valid, raw_bpm, ac_valid, this_ac_lo, wt_valid = parts[:8]
        raw_valid, ac_valid = int(raw_valid), int(ac_valid)
        if not (raw_valid and ac_valid):
            continue
        if classify(float(raw_bpm), gt) != "correct":
            continue
        raw_vals = parts[8:8 + N_CURVE]
        curve = np.array([float(v) if v else np.nan for v in raw_vals])
        if np.any(np.isnan(curve)) or np.nanmax(curve) <= 0:
            continue
        curve = curve / np.nanmax(curve)  # normalize for averaging comparability; corr is scale-invariant anyway
        curves.append(curve)
        ac_lo_seen = int(this_ac_lo)
    return bucket, curves, ac_lo_seen


if __name__ == "__main__":
    files = CS.load_all_files(include_synth=False)
    print(f"scoring {len(files)} files (full corpus, final templates)...", file=sys.stderr)

    by_bucket = defaultdict(list)
    ac_los = set()
    with Pool(16) as pool:
        for i, (bucket, curves, ac_lo) in enumerate(pool.imap_unordered(run_file, files)):
            by_bucket[bucket].extend(curves)
            if ac_lo is not None:
                ac_los.add(ac_lo)
            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(files)} files done", file=sys.stderr)

    assert len(ac_los) <= 1, f"expected one shared ac_lo, got {ac_los}"
    ac_lo = ac_los.pop() if ac_los else None
    print(f"\nshared ac_lo={ac_lo}", file=sys.stderr)

    kept_buckets = {}
    for b in sorted(by_bucket):
        n = len(by_bucket[b])
        status = "KEPT" if n >= MIN_TICKS_PER_BUCKET else "DROPPED (too few ticks)"
        print(f"  bucket {b:>4}bpm: {n:>5} ticks -- {status}", file=sys.stderr)
        if n >= MIN_TICKS_PER_BUCKET:
            kept_buckets[b] = np.mean(by_bucket[b], axis=0)

    np.savez(os.path.join(HERE, "autocorr_final_templates.npz"),
             buckets=np.array(sorted(kept_buckets.keys())),
             ac_lo=ac_lo,
             **{f"template_{b}": v for b, v in kept_buckets.items()})
    print(f"\nwrote autocorr_final_templates.npz with {len(kept_buckets)} buckets: {sorted(kept_buckets.keys())}",
          file=sys.stderr)
