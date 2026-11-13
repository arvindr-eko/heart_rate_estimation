"""Build + evaluate combo13 driver variants over a file list. Run from a shell that has sourced
emsdk's my_env.sh and exported EM_CONFIG (emcc/node on PATH).

Displayed-bpm convention = test_driver_stream_csv.c columns: t,bpm,bpm_valid,is_flatline,raw_bpm,raw_valid,harm.
'correct' = within +-5bpm OR +-5% (project-wide convention, eval_harness.classify)."""
import os, sys, csv, subprocess, pickle, hashlib
from multiprocessing import Pool
import numpy as np
from scipy.io import wavfile

R = r"C:\Users\daniel\Desktop\pcg_abab\retune_2026-10"
WAVS = r"C:\Users\daniel\Desktop\ExpandedCorpus\combined_20261012\wavs"
NODE = r"C:\Users\daniel\emsdk\node\22.16.0_64bit\bin\node.exe"
BASEFLAGS = ["-O2", "-DSTREAM_VARIANT=4", "-DTEMPLATE_WEIGHT_ENABLE", "-DTEMPLATE_MATCH_GATED_V2_ENABLE", "-sNODERAWFS=1"]
CACHE_DIR = os.path.join(R, "evalcache")
os.makedirs(CACHE_DIR, exist_ok=True)


def build(srcdir, name, defs=(), outdir=None):
    outdir = outdir or os.path.join(srcdir, "drivers")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, name + ".js")
    cmd = ["emcc"] + BASEFLAGS + [f"-D{d}" for d in defs] + ["-I", srcdir, "-o", out,
           os.path.join(srcdir, "test_driver_stream_csv.c"), os.path.join(srcdir, "combo13_stream.c")]
    r = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-1500:])
    return out


def classify(bpm, gt):
    if bpm is None:
        return "no_display"
    ratio = bpm / gt
    if abs(bpm - gt) <= 5 or abs(ratio - 1) <= 0.05: return "correct"
    if abs(ratio - 0.5) <= 0.05: return "half"
    if abs(ratio - 2.0) <= 0.05: return "double"
    if abs(ratio - 1 / 3) <= 0.05: return "third"
    if abs(ratio - 3.0) <= 0.05: return "triple"
    return "other"


def _run(a):
    driver, fname = a
    fs, d = wavfile.read(os.path.join(WAVS, fname))
    if d.ndim > 1: d = d.mean(axis=1)
    tmp = os.path.join(CACHE_DIR, f"_t{os.getpid()}.f32")
    d.astype(np.float32).tofile(tmp)
    try:
        out = subprocess.run([NODE, driver, tmp, str(fs)], capture_output=True, text=True).stdout
    finally:
        os.remove(tmp)
    t, b, flat = [], [], []
    for line in out.splitlines()[1:]:
        p = line.split(",")
        if len(p) < 7: continue
        t.append(float(p[0])); b.append(float(p[1]) if int(p[2]) else np.nan); flat.append(int(p[3]))
    return fname, np.array(t), np.array(b), np.array(flat)


def run(driver, files, tag, pool=8, force=False):
    """Returns {file: (t, bpm(nan=not displayed), flatline)}; cached by tag+file-list hash."""
    h = hashlib.md5(("|".join(sorted(files)) + driver + str(os.path.getmtime(driver))).encode()).hexdigest()[:10]
    cp = os.path.join(CACHE_DIR, f"{tag}_{h}.pkl")
    if os.path.exists(cp) and not force:
        return pickle.load(open(cp, "rb"))
    res = {}
    with Pool(pool) as p:
        for f, t, b, fl in p.imap_unordered(_run, [(driver, f) for f in files], chunksize=4):
            res[f] = (t, b, fl)
    pickle.dump(res, open(cp, "wb"), protocol=4)
    return res


def load_split():
    return list(csv.DictReader(open(os.path.join(R, "split.csv"), encoding="utf-8")))


def tune_subset(rows, every=3):
    """Deterministic ~1/3 of TRAIN, plus every train file in the <45 bpm bins (scarce)."""
    tr = sorted([r for r in rows if r["split"] == "train"], key=lambda r: (r["bin"], r["file"]))
    keep = []
    cnt = {}
    for r in tr:
        cnt[r["bin"]] = cnt.get(r["bin"], 0) + 1
        if r["bin"] in ("30-40", "40-45") or cnt[r["bin"]] % every == 0:
            keep.append(r)
    return keep


def file_stats(res, gt):
    t, b, _ = res
    cls = [classify(None if np.isnan(x) else x, gt) for x in b]
    disp = [i for i, c in enumerate(cls) if c != "no_display"]
    corr = [i for i, c in enumerate(cls) if c == "correct"]
    ttfd = t[disp[0]] if disp else None
    ttfr = t[corr[0]] if corr else None
    # settled: first tick from which the next 4 displayed ticks (>=2s) are all correct
    settled = None
    for i in corr:
        w = [c for c in cls[i:i + 4]]
        if len(w) == 4 and all(c == "correct" for c in w):
            settled = t[i]; break
    n = len(cls)
    return dict(cls=cls, ttfd=ttfd, ttfr=ttfr, settled=settled, n=n,
                first_disp_correct=(cls[disp[0]] == "correct") if disp else None)


def summarize(res, rows):
    """rows: list of split.csv rows (dict). Returns dict of aggregate stats."""
    S = []
    for r in rows:
        if r["file"] not in res: continue
        s = file_stats(res[r["file"]], float(r["bpm"])); s["bpm"] = float(r["bpm"]); s["file"] = r["file"]; S.append(s)
    allc = [c for s in S for c in s["cls"]]
    n = len(allc)
    def frac(k): return sum(c == k for c in allc) / n
    disp = [c for c in allc if c != "no_display"]
    out = dict(files=len(S), ticks=n,
               correct=frac("correct"), no_display=frac("no_display"),
               wrong_displayed=sum(c not in ("correct", "no_display") for c in allc) / n,
               acc_when_displayed=(sum(c == "correct" for c in disp) / len(disp)) if disp else 0.0,
               half=frac("half"), double=frac("double"), third=frac("third"), triple=frac("triple"), other=frac("other"))
    tt = [s["ttfr"] for s in S if s["ttfr"] is not None]
    st = [s["settled"] for s in S if s["settled"] is not None]
    td = [s["ttfd"] for s in S if s["ttfd"] is not None]
    out.update(never_correct=1 - len(tt) / len(S), ttfr_median=float(np.median(tt)) if tt else None,
               ttfr_mean=float(np.mean(tt)) if tt else None, ttfr_p90=float(np.percentile(tt, 90)) if tt else None,
               settled_median=float(np.median(st)) if st else None, never_settled=1 - len(st) / len(S),
               ttfd_median=float(np.median(td)) if td else None,
               first_display_correct=float(np.mean([s["first_disp_correct"] for s in S if s["first_disp_correct"] is not None])),
               never_displayed=sum(s["ttfd"] is None for s in S) / len(S))
    return out, S


def fmt(o):
    f = lambda x: "  -- " if x is None else f"{x:5.1f}"
    return (f"files={o['files']:4d} correct={o['correct']*100:5.1f}% nodisp={o['no_display']*100:5.1f}% wrong={o['wrong_displayed']*100:4.1f}% "
            f"(acc|disp {o['acc_when_displayed']*100:4.1f}%) h/d/3/x3/o={o['half']*100:.1f}/{o['double']*100:.1f}/{o['third']*100:.1f}/{o['triple']*100:.1f}/{o['other']*100:.1f} "
            f"TTFR med {f(o['ttfr_median'])} p90 {f(o['ttfr_p90'])} never {o['never_correct']*100:4.1f}% settled med {f(o['settled_median'])}")
