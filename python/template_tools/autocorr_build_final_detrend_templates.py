"""Build the FINAL detrended-curve templates (2026-08-10), companion to
autocorr_build_final_templates.py's raw-curve templates -- needed for the promoted combined_mean
score (0.5*(raw_weighted + det_weighted)), per Daniel's "yes promote this" after the corpus
comparison in optimize_template_match.py showed the combined score beats either alone.

Same extraction/bucketing as the raw-template builder: 'correct' raw-candidate ticks from the FULL
314-file corpus, bucketed by ground truth (nearest 5bpm -- widened from 10bpm 2026-08-10, per
Daniel's "the templates we are using are only every 10bpm... if we added those other templates we
might get better matching"). Uses win_teal_curve (the DETRENDED curve autocorr_dump.js already
emits, columns 8+96 onward) instead of the raw curve, normalized by each tick's own max ABSOLUTE
value (not max, since the detrended curve can go negative) before averaging.
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
# See autocorr_build_final_templates.py's own comment on this constant -- must match the compiled
# combo13_stream.c's S_LEN_V exactly. 80 as of revision 0.14's DEC_STRIDE=12 rescale (2026-08-11).
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
    tmp_f32 = os.path.join(HERE, f"_dtmpl_tmp_{os.getpid()}_{abs(hash(ident)) % 100000}.f32")
    data.astype(np.float32).tofile(tmp_f32)
    try:
        out = subprocess.run([NODE, DRIVER, tmp_f32, str(fs_r)], capture_output=True, text=True, cwd=HERE)
    finally:
        os.remove(tmp_f32)

    bucket = round(gt / BUCKET_WIDTH) * BUCKET_WIDTH
    curves = []
    for line in out.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) != 8 + 2 * N_CURVE:
            continue
        t, bpm_valid, bpm, raw_valid, raw_bpm, ac_valid, this_ac_lo, wt_valid = parts[:8]
        raw_valid, wt_valid = int(raw_valid), int(wt_valid)
        if not (raw_valid and wt_valid):
            continue
        if classify(float(raw_bpm), gt) != "correct":
            continue
        det_vals = parts[8 + N_CURVE:8 + 2 * N_CURVE]
        curve = np.array([float(v) if v else np.nan for v in det_vals])
        if np.any(np.isnan(curve)):
            continue
        absmax = np.nanmax(np.abs(curve))
        if absmax <= 0:
            continue
        curves.append(curve / absmax)
    return bucket, curves


if __name__ == "__main__":
    files = CS.load_all_files(include_synth=False)
    print(f"scoring {len(files)} files (full corpus, final DETRENDED templates)...", file=sys.stderr)

    by_bucket = defaultdict(list)
    with Pool(16) as pool:
        for i, (bucket, curves) in enumerate(pool.imap_unordered(run_file, files)):
            by_bucket[bucket].extend(curves)
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(files)} files done", file=sys.stderr)

    kept_buckets = {}
    for b in sorted(by_bucket):
        n = len(by_bucket[b])
        status = "KEPT" if n >= MIN_TICKS_PER_BUCKET else "DROPPED (too few ticks)"
        print(f"  bucket {b:>4}bpm: {n:>5} ticks -- {status}", file=sys.stderr)
        if n >= MIN_TICKS_PER_BUCKET:
            kept_buckets[b] = np.mean(by_bucket[b], axis=0)

    np.savez(os.path.join(HERE, "autocorr_final_detrend_templates.npz"),
             buckets=np.array(sorted(kept_buckets.keys())),
             **{f"template_{b}": v for b, v in kept_buckets.items()})
    print(f"\nwrote autocorr_final_detrend_templates.npz with {len(kept_buckets)} buckets: {sorted(kept_buckets.keys())}",
          file=sys.stderr)
