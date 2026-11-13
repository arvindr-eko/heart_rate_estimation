"""Rebuild the autocorr templates (raw + detrended) from the COMBINED 2026-10-12 corpus, 5-bpm buckets
30..200, using the v0.15 debug build (126-bin curve, correct 0.5s tick pacing).

Selection (per bucket b = round(gt/5)*5):
  A ("correct-raw", the OLD method, apples-to-apples with autocorr_final_*templates.npz):
      ticks where the raw candidate is valid and within +-5bpm / +-5% of ground truth.
  B ("gt-only"): every tick whose autocorr curve is valid, regardless of what the algorithm
      reported. Needed for slow buckets because 0.14/0.15 often lock a 3x harmonic there, so
      method A starves (and is biased toward easy ticks).
Primary template = A when it has >= MIN_TICKS ticks AND bucket >= 45bpm, else B (flagged).
Stored both as 80-bin (lags 20..99, the shipped C format, normalized by its own max) and the full
126-bin curve (lags 20..145).

Also caches every file's per-tick data (retune_cache.pkl) for the upcoming parameter scans.
"""
import os, sys, csv, subprocess, pickle
from multiprocessing import Pool
from collections import defaultdict
import numpy as np
from scipy.io import wavfile

HERE = os.path.dirname(os.path.abspath(__file__))
NODE = r"C:\Users\daniel\emsdk\node\22.16.0_64bit\bin\node.exe"
DRIVER = r"C:\Users\daniel\Desktop\pcg_abab\v0.15\tmpl\autocorr_dump_v015.js"
CORPUS = r"C:\Users\daniel\Desktop\ExpandedCorpus\combined_20261012"
NC = 126          # full curve bins (v0.15 S_LEN_V)
N80 = 80          # shipped template length
BW = 5
MIN_TICKS = 50
MIN_BPM_A = 45    # below this, method A is not trusted as primary


MERGE_BELOW = 40.0   # 2026-10-12: labels <40bpm pooled into ONE 10bpm-wide bucket, centered 35


def bucket_of(gt):
    return 35 if gt < MERGE_BELOW else int(round(gt / BW) * BW)


def cls(bpm, gt):
    if bpm is None or bpm <= 0:
        return "no_display"
    return "correct" if (abs(bpm - gt) <= 5 or abs(bpm / gt - 1) <= 0.05) else "other"


def run_file(a):
    fname, gt = a
    fs, d = wavfile.read(os.path.join(CORPUS, "wavs", fname))
    if d.ndim > 1:
        d = d.mean(axis=1)
    tmp = os.path.join(HERE, f"_tmp_{os.getpid()}.f32")
    d.astype(np.float32).tofile(tmp)
    try:
        out = subprocess.run([NODE, DRIVER, tmp, str(fs)], capture_output=True, text=True, cwd=HERE).stdout
    finally:
        os.remove(tmp)
    ticks = []
    for line in out.splitlines():
        p = line.strip().split(",")
        if len(p) != 8 + 2 * NC:
            continue
        t, bv, bpm, rv, rbpm, acv, lo, wtv = p[:8]
        raw = np.array([float(v) if v else np.nan for v in p[8:8 + NC]], dtype=np.float32)
        det = np.array([float(v) if v else np.nan for v in p[8 + NC:]], dtype=np.float32)
        ticks.append((float(t), int(bv), float(bpm), int(rv), float(rbpm), int(acv), int(wtv), raw, det))
    return fname, gt, ticks


def norm(c, n, absolute):
    c = c[:n]
    if np.any(np.isnan(c)):
        return None
    m = np.max(np.abs(c)) if absolute else np.max(c)
    return c / m if m > 0 else None


if __name__ == "__main__":
    man = list(csv.DictReader(open(os.path.join(CORPUS, "manifest.csv"), encoding="utf-8")))
    jobs = [(r["file"], float(r["bpm_label"])) for r in man]
    print(f"{len(jobs)} files", file=sys.stderr)
    cache = {}
    CP = os.path.join(HERE, "retune_cache.pkl")
    if os.environ.get("REUSE_CACHE") and os.path.exists(CP):
        cache = pickle.load(open(CP, "rb")); jobs = []
    with Pool(6) as pool:
        for i, (f, gt, ticks) in enumerate(pool.imap_unordered(run_file, jobs, chunksize=4)):
            cache[f] = (gt, ticks)
            if (i + 1) % 200 == 0:
                print(f"  {i+1}/{len(jobs)}", file=sys.stderr, flush=True)
    if jobs:
        pickle.dump(cache, open(CP, "wb"), protocol=4)

    OUTN = "new_templates.npz"
    if os.environ.get("TRAIN_ONLY"):
        keep = {r["file"] for r in csv.DictReader(open(os.path.join(HERE, "split.csv"), encoding="utf-8")) if r["split"] == "train"}
        cache = {f: v for f, v in cache.items() if f in keep}
        OUTN = "new_templates_train.npz"
        print(f"TRAIN_ONLY: {len(cache)} files", file=sys.stderr)
    acc = {k: defaultdict(list) for k in ("A_raw80", "A_raw126", "A_det80", "A_det126",
                                          "B_raw80", "B_raw126", "B_det80", "B_det126")}
    files_in = {"A": defaultdict(set), "B": defaultdict(set)}
    for f, (gt, ticks) in cache.items():
        b = bucket_of(gt)
        for (t, bv, bpm, rv, rbpm, acv, wtv, raw, det) in ticks:
            if acv:
                for tag, n in (("raw80", N80), ("raw126", NC)):
                    c = norm(raw, n, False)
                    if c is not None:
                        acc["B_" + tag][b].append(c)
                        if rv and cls(rbpm, gt) == "correct":
                            acc["A_" + tag][b].append(c)
                            files_in["A"][b].add(f)
                        files_in["B"][b].add(f)
            if wtv:
                for tag, n in (("det80", N80), ("det126", NC)):
                    c = norm(det, n, True)
                    if c is not None:
                        acc["B_" + tag][b].append(c)
                        if rv and cls(rbpm, gt) == "correct":
                            acc["A_" + tag][b].append(c)

    buckets = sorted({b for k in acc for b in acc[k]})
    save = {"buckets": np.array(buckets)}
    summary = []
    for b in buckets:
        nA = len(acc["A_raw80"][b]); nB = len(acc["B_raw80"][b])
        useA = nA >= MIN_TICKS and b >= MIN_BPM_A
        src = "A" if useA else ("B" if nB >= MIN_TICKS else None)
        summary.append((b, nA, nB, len(files_in["A"][b]), len(files_in["B"][b]), src))
        for k in acc:
            if acc[k][b]:
                save[f"{k}_{b}"] = np.mean(acc[k][b], axis=0)
        if src:
            for tag in ("raw80", "raw126", "det80", "det126"):
                if acc[f"{src}_{tag}"][b]:
                    save[f"template_{tag}_{b}"] = np.mean(acc[f"{src}_{tag}"][b], axis=0)
        save[f"source_{b}"] = np.array(src or "none")
    save["summary"] = np.array([(b, nA, nB, fA, fB) for b, nA, nB, fA, fB, s in summary])
    np.savez(os.path.join(HERE, OUTN), **save)
    print("bucket  ticksA ticksB filesA filesB primary")
    for b, nA, nB, fA, fB, s in summary:
        print(f"{b:>5} {nA:>7} {nB:>6} {fA:>6} {fB:>6}  {s}")
