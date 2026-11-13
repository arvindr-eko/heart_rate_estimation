import sys, subprocess, glob, os, csv, io, numpy as np
drv = sys.argv[1]; files = sorted(glob.glob(r"C:\Users\daniel\Desktop\pcg_abab\v0.15\synth\*.f32"))
def run(f):
    out = subprocess.run(["node", drv, f, "4000"], capture_output=True, text=True).stdout
    rows = list(csv.DictReader(io.StringIO(out)))
    return rows
print("file                     gt  first_disp  n_disp/n  median   last   raw_median(valid)")
for f in files:
    gt = int(os.path.basename(f)[2:5])
    r = run(f)
    disp = [(float(x['t']), float(x['bpm'])) for x in r if x['bpm_valid']=='1']
    raw = [float(x['raw_bpm']) for x in r if x['raw_valid']=='1']
    fd = disp[0][0] if disp else float('nan')
    med = np.median([b for _,b in disp]) if disp else float('nan')
    last = disp[-1][1] if disp else float('nan')
    rm = np.median(raw) if raw else float('nan')
    print(f"{os.path.basename(f)[:-4]:24s} {gt:3d} {fd:8.1f} {len(disp):5d}/{len(r):3d} {med:7.1f} {last:7.1f} {rm:7.1f}")
