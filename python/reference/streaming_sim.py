#!/usr/bin/env python3
"""
True sliding-window streaming simulation with a STABILITY gate.
At each hop the detector runs on the last W seconds; it only REPORTS once:
  (a) >= T_min seconds of audio seen (warm-up floor),
  (b) agreement confidence >= TAU, and
  (c) the estimate is STABLE: last K window-estimates span <= STAB bpm (no octave jumps).
Measures: time-to-first-report, accuracy of that first report, % files that ever report,
steady-state coverage (% of post-onset hops that report), steady-state accuracy.
Compares stability-gate ON vs OFF to show its effect on warm-up errors.
"""
import os, glob, sys
import numpy as np, pandas as pd
from scipy.signal import hilbert
import eval_harness as EH

W=8.0; HOP=1.0; T_MIN=5.0; TAU=0.50; K=3; STAB=10.0   # window, hop, warm-up, conf, #hops, bpm spread

def est(data, fs):
    filt = EH.bandpass(data, 20, 150, fs)
    e_sm, e_lg, e_sh = EH.envelopes(filt, fs)
    pool = [EH.bpm_peaks(e_sm, fs)]
    for env in (e_sm, e_lg, e_sh):
        pool.append(EH.bpm_autocorr(env, fs)); pool.append(EH.bpm_hps(env, fs))
    for ms in (8,16,32,64,110):
        pool.append(EH._ac_tallest(EH.smooth(np.abs(hilbert(filt)), ms, fs), fs))
    vals = np.array([v for v in pool if v is not None and np.isfinite(v)])
    if len(vals)==0: return None, 0.0
    bm, bc = float(vals[0]), 0
    for v in vals:
        cl = vals[np.abs(vals-v) <= np.maximum(5.0,0.06*v)]
        if len(cl) > bc: bc, bm = len(cl), float(cl.mean())
    return bm, bc/len(vals)

lab = EH.load_labels()
files=[]
for sub in sorted(os.listdir(EH.ROOT)):
    sd=os.path.join(EH.ROOT,sub)
    if not os.path.isdir(sd): continue
    for w in sorted(glob.glob(os.path.join(sd,"*.wav"))):
        L=lab.get(EH.wkey(w))
        if L is not None and np.isfinite(L["ecg"]): files.append((w,float(L["ecg"])))

# compute per-hop history ONCE per file (the expensive part), then sweep gate params cheaply
HISTS=[]   # (gt, [(t,rate,conf),...])
durs=[]
for w,gt in files:
    fs,data=EH.wavfile.read(w); data=data.mean(axis=1) if data.ndim>1 else data
    data=data.astype(np.float64); data/=np.max(np.abs(data))+1e-10
    dur=len(data)/fs; durs.append(dur)
    hist=[]; t=HOP
    while t<=dur+1e-6:
        s=max(0,int((t-W)*fs)); e=int(t*fs); seg=data[s:e]
        if len(seg)>=int(1.8*fs):
            r,c=est(seg,fs); hist.append((t,r,c))
        t+=HOP
    HISTS.append((gt,hist))

def evaluate(t_min, use_stab):
    rows=[]
    for gt,hist in HISTS:
        first=None
        for i,(t,r,c) in enumerate(hist):
            if r is None or t<t_min or c<TAU: continue
            if use_stab:
                last=[hist[j][1] for j in range(max(0,i-K+1),i+1) if hist[j][1] is not None]
                if len(last)<K or (max(last)-min(last))>STAB: continue
            first=(i,t,r); break
        if first is None: rows.append(dict(ttfr=None,ferr=None,cov=0.0,sacc=None)); continue
        i0,t0,r0=first; rep=[]; nrep=ntot=0
        for i,(t,r,c) in enumerate(hist):
            if i<i0: continue
            ntot+=1; ok=(r is not None and t>=t_min and c>=TAU)
            if ok and use_stab:
                last=[hist[j][1] for j in range(max(0,i-K+1),i+1) if hist[j][1] is not None]
                ok=(len(last)>=K and (max(last)-min(last))<=STAB)
            if ok: nrep+=1; rep.append(r)
        rows.append(dict(ttfr=t0,ferr=abs(r0-gt),cov=nrep/max(1,ntot),
                         sacc=(np.mean([abs(x-gt)<=5 for x in rep])*100 if rep else None)))
    d=pd.DataFrame(rows); ever=d["ttfr"].notna()
    return dict(reports=ever.mean()*100,
                ttfr_med=d.loc[ever,"ttfr"].median(), ttfr_90=d.loc[ever,"ttfr"].quantile(0.9),
                first5=(d.loc[ever,"ferr"]<=5).mean()*100, first_mae=d.loc[ever,"ferr"].mean(),
                cov=d.loc[ever,"cov"].median()*100, sacc=d.loc[ever,"sacc"].dropna().mean())

durs=np.array(durs)
print(f"Files: {len(files)}   durations: min={durs.min():.1f}s median={np.median(durs):.1f}s max={durs.max():.1f}s")
print(f"Fixed: window={W}s hop={HOP}s tau={TAU} stability=last {K} hops within {STAB}bpm (ON)\n")
print(f"{'warm-up':>8} {'%report':>8} {'TTFR med':>9} {'TTFR 90%':>9} {'first±5':>8} {'firstMAE':>9} {'steadyCov':>10} {'steadyAcc':>10}")
for tmin in [4,5,6,7,8,10,12]:
    r=evaluate(tmin, True)
    print(f"{tmin:>7}s {r['reports']:>7.0f}% {r['ttfr_med']:>8.1f}s {r['ttfr_90']:>8.1f}s {r['first5']:>7.0f}% {r['first_mae']:>9.1f} {r['cov']:>9.0f}% {r['sacc']:>9.0f}%")
