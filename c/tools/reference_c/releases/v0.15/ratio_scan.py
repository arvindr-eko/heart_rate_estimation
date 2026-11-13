import os, sys, subprocess, numpy as np, glob
from multiprocessing import Pool
from scipy.io import wavfile
sys.path.insert(0, r"C:\Users\daniel\Desktop\pcg_abab")
import corpus_sources as CS
HERE = os.path.dirname(os.path.abspath(__file__))
NODE = r"C:\Users\daniel\emsdk\node\22.16.0_64bit\bin\node.exe"
def scan(args):
    path, gt, cat, ident = args
    if path.endswith('.f32'):
        data = np.fromfile(path, dtype=np.float32); fs = 4000
    else:
        fs, d = wavfile.read(path)
        if d.ndim > 1: d = d.mean(axis=1)
        data = d.astype(np.float32)
    tmp = os.path.join(HERE, f"_rs_{os.getpid()}.f32"); data.tofile(tmp)
    try:
        out = subprocess.run([NODE, os.path.join(HERE, "ratio_driver.js"), tmp, str(fs)], capture_output=True, text=True, cwd=HERE).stdout.splitlines()[1:]
    finally: os.remove(tmp)
    rows = []
    for l in out:
        p = l.split(',')
        if int(p[1]): rows.append((float(p[0]), float(p[2]), float(p[3]), float(p[4]), int(p[5])))
    return ident, gt, cat, rows
if __name__ == "__main__":
    files = CS.load_all_files(include_synth=True)
    for f in sorted(glob.glob(os.path.join(HERE, "synth", "*.f32"))):
        g = int(os.path.basename(f)[2:5]); files.append((f, float(g), 'lowsynth', os.path.basename(f)[:-4]))
    res = []
    with Pool(16) as p:
        for r in p.imap_unordered(scan, files): res.append(r)
    import pickle; pickle.dump(res, open(os.path.join(HERE, "ratio_scan.pkl"), "wb"))
    print("done", len(res))
