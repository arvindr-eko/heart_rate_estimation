#!/usr/bin/env python3
"""
PCG heart-rate evaluation harness.
- Joins all 269 wavs to ground truth (efast_ecg_hr_value).
- Scores an estimator by HR band, and SEPARATELY counts half- vs double-rate
  (and third/triple) errors so we never fix one direction while breaking the other.
- Benchmarks vs CORAL HR (shipping product) and efast_pcg_hr_value.
- Deterministic patient-level train/dev split (40% dev) so discriminator work
  can be developed on TRAIN and reported honestly on the held-out DEV.

Estimators are pluggable. Baseline = faithful port of pcg_hr_analyzer.py consensus.
Usage: python eval_harness.py [baseline]    (more added later)
"""
import os, glob, sys, hashlib
import numpy as np, pandas as pd
from scipy.io import wavfile
from scipy.signal import butter, filtfilt, hilbert, find_peaks
from scipy.ndimage import uniform_filter1d
import warnings; warnings.filterwarnings("ignore")

ROOT=r"C:\Users\daniel\Desktop\AudioFiles\coral_hr_dataset_with_sq_thresholding_0.7-20260612T222237Z-3-001\coral_hr_dataset_with_sq_thresholding_0.7"
TOL=5.0; BPM_LO,BPM_HI,HPS_LO=30.0,220.0,40.0
BANDS=[(0,60),(60,90),(90,120),(120,160),(160,400)]

# ----- shared DSP -----
def smooth(x,ms,fs): return uniform_filter1d(x.astype(np.float64),size=max(1,int(ms*fs/1000)),mode="reflect")
def bandpass(x,lo,hi,fs,o=4):
    nyq=fs/2.0; b,a=butter(o,[lo/nyq,min(hi/nyq,0.99)],btype="band"); return filtfilt(b,a,x.astype(np.float64))
def fft_autocorr(x):
    e=x-x.mean(); N=len(e); fsz=1<<(2*N-1).bit_length()
    F=np.fft.rfft(e,n=fsz); c=np.fft.irfft(F*np.conj(F))[:N]; return c/(c[0]+1e-10)

# ----- baseline estimators (ported from pcg_hr_analyzer.py) -----
def bpm_autocorr(env,fs):
    lo=max(2,int(fs*60/BPM_HI)); hi=min(len(env)//2,int(fs*60/BPM_LO))
    if lo>=hi: return None
    s=fft_autocorr(env)[lo:hi]; pk,pr=find_peaks(s,height=0.1)
    if len(pk)==0: best=int(np.argmax(s))+lo
    else:
        lags=pk+lo; best=lags[int(np.argmax(pr["peak_heights"]/(lags**0.25)))]
    bpm=60.0/(best/fs); return bpm if BPM_LO<=bpm<=BPM_HI else None
def bpm_hps(env,fs,nh=5):
    N=len(env); spec=np.abs(np.fft.rfft(env)); freqs=np.fft.rfftfreq(N,1.0/fs)
    i0=int(np.searchsorted(freqs,HPS_LO/60.0)); i1=int(np.searchsorted(freqs,BPM_HI/60.0))
    if i0>=i1: return None
    fr=freqs[i0:i1]; hps=spec[i0:i1].copy()
    for h in range(2,nh+1): hps*=np.interp(fr*h,freqs,spec)
    bpm=float(fr[int(np.argmax(hps))]*60.0); return bpm if BPM_LO<=bpm<=BPM_HI else None
def bpm_peaks(env,fs,min_dist_ms=270,thr_pct=60):
    md=int(min_dist_ms*fs/1000); thr=np.percentile(env,thr_pct)
    pks,_=find_peaks(env,distance=md,height=thr)
    if len(pks)<2: return None
    iv=np.diff(pks)/fs; v=iv[(iv>=60.0/BPM_HI)&(iv<=60.0/BPM_LO)]
    if len(v)==0: return None
    bpm=float(60.0/np.median(v)); return bpm if BPM_LO<=bpm<=BPM_HI else None
def best_cluster(arr,window=10.0):
    best_mean,best_count=float(np.median(arr)),0
    for e in arr:
        cl=arr[np.abs(arr-e)<window]
        if len(cl)>best_count: best_count,best_mean=len(cl),float(cl.mean())
    return best_mean
def envelopes(filt,fs):
    e_sm=smooth(np.abs(hilbert(filt)),30,fs); e_lg=smooth(np.abs(hilbert(filt)),150,fs)
    x=filt/(np.max(np.abs(filt))+1e-10); e_sh=smooth(-x**2*np.log(x**2+1e-10),50,fs)
    return e_sm,e_lg,e_sh

def est_baseline(data,fs):
    filt=bandpass(data,20,150,fs); e_sm,e_lg,e_sh=envelopes(filt,fs)
    est=[bpm_peaks(e_sm,fs)]
    for env in (e_sm,e_lg,e_sh):
        for fn in (bpm_autocorr,bpm_hps): est.append(fn(env,fs))
    arr=np.array([e for e in est if e is not None])
    return best_cluster(arr) if len(arr) else float("nan")

def ac_at(corr,lag):
    """autocorr height near a (float) lag, taking the local max within +-6% for robustness."""
    if lag<2 or lag>=len(corr)-1: return 0.0
    w=max(2,int(0.06*lag)); a,b=max(1,int(lag)-w),min(len(corr),int(lag)+w+1)
    return float(np.max(corr[a:b]))

def est_v2(data,fs):
    """Harmonic-comb fundamental finder: the true period P has autocorr support at
    P,2P,3P,...; subharmonics (4P etc) do not. Pick the candidate maximizing comb support,
    which collapses deep subharmonic / half-rate errors to the true fundamental."""
    filt=bandpass(data,20,150,fs)
    env=smooth(np.abs(hilbert(filt)),30,fs)
    corr=fft_autocorr(env)
    lo=max(2,int(fs*60/BPM_HI)); hi=min(len(corr)//2,int(fs*60/BPM_LO))
    s=corr[lo:hi]
    if s.max()<=0: return float("nan")
    pk,_=find_peaks(s,height=0.15*s.max(),distance=max(2,int(0.05*fs)))
    if len(pk)==0: return 60.0*fs/(int(np.argmax(s))+lo)
    lags=pk+lo
    cands=set()
    for L in lags:
        for div in (1,2,3,4):
            P=L/div
            if BPM_LO<=60.0*fs/P<=BPM_HI: cands.add(int(round(P)))
    W=[1.0,0.85,0.7,0.55,0.45]
    def comb(P): return sum(w*ac_at(corr,k*P) for k,w in enumerate(W,1))
    best=max(cands,key=comb)
    return 60.0*fs/best

def half_cycle_sim(env,P2):
    """Fold env at period P2, split the mean template into two halves, return their
    correlation. ~1 => the two halves are the same beat repeated (period is really P2/2);
    low => the halves are S1-part vs S2-part (P2 is the true full cardiac cycle)."""
    P2=int(round(P2))
    if P2<6 or P2>len(env)//3: return 1.0
    n=len(env)//P2; T=env[:n*P2].reshape(n,P2).mean(0)
    h=P2//2; a=T[:h]-T[:h].mean(); b=T[h:2*h]-T[h:2*h].mean()
    d=np.linalg.norm(a)*np.linalg.norm(b)
    return float(a@b/d) if d>0 else 1.0

TAU=0.55   # tuned on TRAIN: halves-correlation below this => double the period

def est_v3(data,fs):
    """v2 harmonic-comb fundamental, THEN an anti-double octave guard:
    if folding at 2P shows two DISSIMILAR halves (S1 vs S2), P was the S1->S2 half-period
    and the true cardiac period is 2P."""
    filt=bandpass(data,20,150,fs); env=smooth(np.abs(hilbert(filt)),30,fs)
    corr=fft_autocorr(env)
    lo=max(2,int(fs*60/BPM_HI)); hi=min(len(corr)//2,int(fs*60/BPM_LO))
    s=corr[lo:hi]
    if s.max()<=0: return float("nan")
    pk,_=find_peaks(s,height=0.15*s.max(),distance=max(2,int(0.05*fs)))
    if len(pk)==0: return 60.0*fs/(int(np.argmax(s))+lo)
    lags=pk+lo; cands=set()
    for L in lags:
        for div in (1,2,3,4):
            P=L/div
            if BPM_LO<=60.0*fs/P<=BPM_HI: cands.add(int(round(P)))
    W=[1.0,0.85,0.7,0.55,0.45]
    P=max(cands,key=lambda P:sum(w*ac_at(corr,k*P) for k,w in enumerate(W,1)))
    twoP=2*P
    if 60.0*fs/twoP>=BPM_LO and half_cycle_sim(env,twoP)<TAU:   # halves differ -> P was S1->S2 half
        P=twoP
    return 60.0*fs/P

def est_seg(data,fs):
    """Option B (lightweight, training-free S1/S2 segmentation):
    detect all peaks, use systole<diastole interval alternation to recover the S1->S1 period;
    for the degenerate equal-spacing case fall back to the same half-cycle structural test."""
    filt=bandpass(data,20,150,fs); env=smooth(np.abs(hilbert(filt)),30,fs); env/=env.max()+1e-10
    thr=np.percentile(env,55); pk,_=find_peaks(env,height=thr,distance=max(2,int(0.10*fs)))  # 100ms < shortest systole
    if len(pk)<4:   # too few beats -> autocorr fallback
        corr=fft_autocorr(env); lo=max(2,int(fs*60/BPM_HI)); hi=min(len(corr)//2,int(fs*60/BPM_LO))
        s=corr[lo:hi]; return 60.0*fs/(int(np.argmax(s))+lo) if s.max()>0 else float("nan")
    iv=np.diff(pk)/fs
    med=np.median(iv); shrt=np.median(iv[iv<med]) if (iv<med).any() else med
    lng=np.median(iv[iv>=med]); ratio=shrt/lng if lng>0 else 1.0
    if ratio<0.72:                       # clear systole/diastole alternation -> both S1&S2 seen
        period=shrt+lng
    else:                                # uniform intervals: only-S1, OR S1~=S2 degenerate
        base=med
        if 60.0/(2*base)>=BPM_LO and half_cycle_sim(env,int(round(2*base*fs)))<TAU:
            period=2*base                # halves differ -> equal-spaced S1/S2 -> true period is 2x
        else:
            period=base
    bpm=60.0/period
    return bpm if BPM_LO<=bpm<=BPM_HI else float("nan")

def _ac_tallest(env,fs):
    """tallest RAW autocorr peak (no lag weighting) — exposes octave behavior directly."""
    corr=fft_autocorr(env); lo=max(2,int(fs*60/BPM_HI)); hi=min(len(corr)//2,int(fs*60/BPM_LO))
    s=corr[lo:hi]
    if s.max()<=0: return float("nan")
    pk,pr=find_peaks(s,height=0.05*s.max())
    if len(pk)==0: return 60.0*fs/(int(np.argmax(s))+lo)
    lags=pk+lo; return 60.0*fs/lags[int(np.argmax(pr["peak_heights"]))]
def est_ac_smooth(data,fs):   # over-smoothed envelope (like current pipeline)
    return _ac_tallest(smooth(np.abs(hilbert(bandpass(data,20,150,fs))),30,fs),fs)
def est_ac_sharp(data,fs):    # morphology-preserving envelope (light 4ms)
    return _ac_tallest(smooth(np.abs(hilbert(bandpass(data,20,150,fs))),4,fs),fs)
def est_ac_sharp_wide(data,fs):   # sharp + wider band (keep S2/onset high-freq)
    return _ac_tallest(smooth(np.abs(hilbert(bandpass(data,20,min(300,fs/2*0.95),fs))),4,fs),fs)

def envelopes_sharp(filt,fs):
    e_sm=smooth(np.abs(hilbert(filt)),6,fs); e_md=smooth(np.abs(hilbert(filt)),15,fs)
    x=filt/(np.max(np.abs(filt))+1e-10); e_sh=smooth(-x**2*np.log(x**2+1e-10),20,fs)
    return e_sm,e_md,e_sh
def est_baseline_sharp(data,fs):
    """The winning consensus, but with morphology-preserving (sharp) envelopes 6/15/20ms
    instead of 30/150/50ms. Also scales the peak-picker min-distance with the autocorr
    period so it doesn't merge beats at high HR."""
    filt=bandpass(data,20,150,fs); e_sm,e_md,e_sh=envelopes_sharp(filt,fs)
    # adaptive peak min-distance from a coarse autocorr period estimate
    coarse=bpm_autocorr(e_sm,fs) or 75.0
    md_ms=max(120,min(420, 0.45*60000.0/coarse))   # ~45% of the cycle
    est=[bpm_peaks(e_sm,fs,min_dist_ms=md_ms)]
    for env in (e_sm,e_md,e_sh):
        for fn in (bpm_autocorr,bpm_hps): est.append(fn(env,fs))
    arr=np.array([e for e in est if e is not None])
    return best_cluster(arr) if len(arr) else float("nan")

def est_adaptive(data,fs):
    """Rate-adaptive envelope smoothing: a coarse rate sets the smoothing window to a
    fraction of the cycle -> heavy enough to merge S1+S2 at normal HR (no double-rate),
    light enough to keep beats separate at high HR (no smearing). Then consensus."""
    filt=bandpass(data,20,150,fs)
    coarse=bpm_autocorr(smooth(np.abs(hilbert(filt)),20,fs),fs) or 75.0
    cyc=60000.0/coarse
    sm=float(np.clip(0.33*cyc,14,150))          # merge S1-S2 (~1/3 cycle) but cap; small at high HR
    e_main=smooth(np.abs(hilbert(filt)),sm,fs)
    x=filt/(np.max(np.abs(filt))+1e-10); e_sh=smooth(-x**2*np.log(x**2+1e-10),min(sm,60),fs)
    md=float(np.clip(0.45*cyc,120,450))
    est=[bpm_peaks(e_main,fs,min_dist_ms=md)]
    for env in (e_main,e_sh):
        for fn in (bpm_autocorr,bpm_hps): est.append(fn(env,fs))
    arr=np.array([e for e in est if e is not None])
    return best_cluster(arr) if len(arr) else float("nan")

MS_SCALES=[5,9,15,24,40,65,105,170,270]   # extend past the systolic gap (~280ms) so S1-S2 merges at the heavy end
def est_multiscale(data,fs,pk_frac=0.40):
    """Cross-scale lag voting. A rate-cluster's SUPPORT = how many smoothing scales show
    it as a strong autocorr peak. The true fundamental is supported across the MOST scales
    (the S1->S2 half-period dies once smoothing exceeds systole; pure subharmonics are
    lower-rate). Pick max-support; tie-break toward the LOWER rate (avoids the light-only
    double, prefers the fundamental over its S1->S2 split)."""
    filt=bandpass(data,20,150,fs)
    lo=max(2,int(fs*60/BPM_HI)); hi=min(int(60*fs/BPM_LO),len(filt)//2)
    if hi<=lo: return float("nan")
    clusters=[]
    for si,ms in enumerate(MS_SCALES):
        env=smooth(np.abs(hilbert(filt)),ms,fs)
        c=fft_autocorr(env)
        if hi>=len(c): continue
        s=c[lo:hi]
        if s.max()<=0: continue
        pk,pr=find_peaks(s,height=pk_frac*s.max())
        for L,h in zip(pk+lo,pr["peak_heights"]):
            r=60.0*fs/L
            if not (BPM_LO<=r<=BPM_HI): continue
            m=None
            for cl in clusters:
                if abs(r-cl["rate"])/cl["rate"]<0.05: m=cl; break
            if m is None: clusters.append({"rate":r,"scales":{si},"h":h,"rs":[r]})
            else: m["scales"].add(si); m["h"]+=h; m["rs"].append(r); m["rate"]=np.mean(m["rs"])
    if not clusters: return float("nan")
    maxsup=max(len(cl["scales"]) for cl in clusters)
    top=[cl for cl in clusters if len(cl["scales"])>=maxsup-1]    # within 1 scale of the best support
    top.sort(key=lambda cl:np.mean(cl["rs"]))                     # tie-break toward LOWER rate
    return float(np.mean(top[0]["rs"]))

def est_abab(data,fs,asym_thr=0.20):
    """Age-free octave resolution via the lub-dub TIMING asymmetry (the original ABAB idea,
    made robust). Autocorr gives the dominant lag L; peaks are detected at spacing ~0.45*L
    (rate-aware -> no noise over-detection, unlike `seg`). If consecutive inter-peak
    intervals ALTERNATE (systole != diastole), L was the S1->S2 half-period and the true
    cardiac period is systole+diastole. If uniform, L is already the beat period."""
    filt=bandpass(data,20,150,fs)
    env=smooth(np.abs(hilbert(filt)),12,fs); env/=env.max()+1e-10
    c=fft_autocorr(env); lo=max(2,int(fs*60/BPM_HI)); hi=min(len(c)//2,int(fs*60/BPM_LO))
    if hi<=lo: return float("nan")
    s=c[lo:hi]
    if s.max()<=0: return float("nan")
    pk_ac,pr=find_peaks(s,height=0.10*s.max())
    if len(pk_ac)==0: return 60.0*fs/(int(np.argmax(s))+lo)
    L=(pk_ac+lo)[int(np.argmax(pr["peak_heights"]))]           # dominant autocorr lag (samples)
    dist=max(2,int(0.45*L))
    epk,_=find_peaks(env,distance=dist,height=np.percentile(env,50))
    if len(epk)>=6:
        iv=np.diff(epk).astype(float)
        m=np.median(iv)
        shrt=np.median(iv[iv<m]) if (iv<m).any() else m        # systole cluster
        lng=np.median(iv[iv>=m])                               # diastole cluster
        asym=(lng-shrt)/(m+1e-9)                               # phase-INDEPENDENT bimodality
        period=(shrt+lng) if asym>asym_thr else m              # two clusters -> S1+S2 split -> sum
        bpm=60.0*fs/period
        if BPM_LO<=bpm<=BPM_HI: return bpm
    return 60.0*fs/L                                            # fallback: dominant autocorr

def shannon_energy_lobes(x,fs,frame_ms=20,hop_frac=0.5,min_ms=30,max_ms=250,merge_ms=50,eps=1e-3,
                          local_min_frac=0.25,jitter_win_ms=50,jitter_thresh=0.15,
                          use_local_min=False,use_jitter=False):
    """Kumar et al. 2006 ICASSP, stage 1 (segmentation) + stage 2 (validation):
    20ms/50%-overlap Shannon energy (-x^2*log(x^2+eps), eps added for numerical safety,
    not in the original paper) on the peak-normalized signal; lobe boundaries = zero-
    crossings of the mean-subtracted energy curve; keep only lobes with duration in
    [30,250]ms (criterion I), merge lobes <50ms apart (criterion II). No wavelet
    transform: the caller's own bandpass stands in for the paper's 5-level FWT
    approximation coefficients as "a lowpassed, still-bipolar signal".

    use_local_min (criterion III, added 2026-07-20 per Daniel's request): if a
    lobe's OWN Shannon-energy curve dips to <25% of its own max somewhere in the
    interior (a second heart-sound split the zero-crossing pass missed because it
    didn't dip all the way to the mean), split there and keep ONLY the louder
    (higher-RMS, measured on the raw signal) half for further processing -- exactly
    the paper's own rule, including its "just keep the loudest piece" resolution
    rather than treating both halves as separate lobes.

    use_jitter (criterion IV, added 2026-07-20): the paper's own text is genuinely
    ambiguous about the exact accept/reject threshold on the jitter VALUE (eq. 5)
    vs. simply "periodicity is trackable at all" within a 50ms sliding window --
    implemented here as the more conservative reading: compute local maxima of the
    lobe's own short-time autocorrelation (50ms window), and if >=3 are trackable
    (enough to compute eq. 5's k-1/k/k+1 triples) AND the resulting jitter is BELOW
    jitter_thresh (i.e. genuinely regular/tonal spacing, not merely "some
    periodicity exists"), discard the lobe as more consistent with sustained noise
    (speech/swallowing) than a genuine broadband heart-sound transient. A lobe too
    short to find 3 peaks at all is kept (that's the normal case for a real click).

    Returns (lobes, frame_rate) where lobes is a list of [start_frame,end_frame]."""
    x=x/(np.max(np.abs(x))+1e-10)
    frame=max(2,int(frame_ms*fs/1000)); hop=max(1,int(frame*hop_frac))
    n=(len(x)-frame)//hop+1
    if n<2: return [],1.0
    es=np.empty(n)
    for i in range(n):
        seg=x[i*hop:i*hop+frame]; v2=seg*seg
        es[i]=-np.mean(v2*np.log(v2+eps))
    ez=es-es.mean()
    lobes=[]; in_l=False; start=0
    for i in range(n):
        if ez[i]>0 and not in_l: in_l=True; start=i
        elif ez[i]<=0 and in_l: in_l=False; lobes.append([start,i])
    if in_l: lobes.append([start,n])
    ffr=fs/hop
    valid=[[a,b] for a,b in lobes if min_ms<=(b-a)/ffr*1000<=max_ms]
    if not valid: return [],ffr
    merged=[valid[0]]
    for a,b in valid[1:]:
        pa,pb=merged[-1]
        if (a-pb)/ffr*1000<merge_ms: merged[-1][1]=b
        else: merged.append([a,b])

    if use_local_min:
        def rms_of(a2,b2):
            s0,s1=int(a2*hop),int(min(len(x),b2*hop+frame))
            seg=x[s0:s1]; return np.sqrt(np.mean(seg**2)) if len(seg) else 0.0
        resplit=[]
        for a,b in merged:
            seg_es=es[a:b]
            if b-a<3: resplit.append([a,b]); continue
            M=seg_es.max()
            mins=[i for i in range(1,len(seg_es)-1) if seg_es[i]<seg_es[i-1] and seg_es[i]<seg_es[i+1]]
            qualifying=[i for i in mins if seg_es[i]<local_min_frac*M]
            if not qualifying: resplit.append([a,b]); continue
            split_i=min(qualifying,key=lambda i:seg_es[i]); split=a+split_i
            left,right=(a,split),(split,b)
            resplit.append(list(left) if rms_of(*left)>=rms_of(*right) else list(right))
        merged=resplit

    if use_jitter:
        win=max(2,int(jitter_win_ms*fs/1000))
        final=[]
        for a,b in merged:
            s0,s1=int(a*hop),int(min(len(x),b*hop+frame))
            seg=x[s0:s1]
            if len(seg)<win*2: final.append([a,b]); continue
            c=seg-seg.mean(); ac=np.correlate(c,c,mode="full")[len(seg)-1:]
            ac=ac/(ac[0]+1e-12)
            pk,_=find_peaks(ac[1:win]); pk=pk+1
            if len(pk)<3: final.append([a,b]); continue
            Tp=pk.astype(float)
            num=sum(abs(2*Tp[k]-Tp[k-1]-Tp[k+1]) for k in range(1,len(Tp)-1))
            den=sum(Tp[1:-1])
            jitter=num/den if den>0 else 1.0
            if jitter<jitter_thresh: continue   # regular short-time periodicity -> tonal noise, discard
            final.append([a,b])
        merged=final

    return merged,ffr

def _kumar_bpm_from_lobes(lobes,ffr):
    if len(lobes)<4: return float("nan")
    starts=np.array([a for a,b in lobes],dtype=float)
    gaps=np.diff(starts)/ffr
    if len(gaps)<3: return float("nan")
    med=np.median(gaps)
    shrt=np.median(gaps[gaps<med]) if (gaps<med).any() else med
    lng=np.median(gaps[gaps>=med])
    ratio=shrt/lng if lng>0 else 1.0
    period=(shrt+lng) if ratio<0.72 else med
    bpm=60.0/period
    return bpm if BPM_LO<=bpm<=BPM_HI else float("nan")

def est_kumar_seg(data,fs):
    """Diastole>systole guard, built the way Kumar et al. 2006 attempted it: real
    per-beat SEGMENTATION (shannon_energy_lobes, not a simple envelope-threshold
    peak-picker like est_seg above) followed by the SAME physiological
    discrimination rule est_seg already uses (median-split lobe-start gaps into
    short/long; if they're clearly asymmetric it's a genuine systole/diastole
    alternation and the true period is their SUM, otherwise treat the gaps as
    already-full-cycle spacing) -- substituted for the paper's own MFCC+SOM timbre
    classifier, which needs per-run training data this single-window estimator
    doesn't have. Criteria I/II only (see shannon_energy_lobes) -- kept exactly as
    first tested, for a stable before/after comparison against est_kumar_seg_full."""
    filt=bandpass(data,20,150,fs)
    lobes,ffr=shannon_energy_lobes(filt,fs)
    return _kumar_bpm_from_lobes(lobes,ffr)

def est_kumar_seg_full(data,fs):
    """Same as est_kumar_seg, but with criteria III (local-minima resplitting) and
    IV (jitter-based noise rejection) also enabled -- the complete validation stage
    from the paper, added 2026-07-20 per Daniel's follow-up request to check
    whether est_kumar_seg's catastrophic corpus result was due to this project's
    deliberate simplification rather than the segmentation approach itself."""
    filt=bandpass(data,20,150,fs)
    lobes,ffr=shannon_energy_lobes(filt,fs,use_local_min=True,use_jitter=True)
    return _kumar_bpm_from_lobes(lobes,ffr)

ESTIMATORS={"baseline":est_baseline,"v2":est_v2,"v3":est_v3,"seg":est_seg,
            "ac_smooth":est_ac_smooth,"ac_sharp":est_ac_sharp,"ac_sharp_wide":est_ac_sharp_wide,
            "baseline_sharp":est_baseline_sharp,"adaptive":est_adaptive,"multiscale":est_multiscale,
            "abab":est_abab,"kumar_seg":est_kumar_seg,"kumar_seg_full":est_kumar_seg_full}

# ----- labels / join -----
def amkey(s):
    s=str(s)
    for p in ("filtered_","unfiltered_"):
        if s.startswith(p): s=s[len(p):]
    return s
def wkey(fn):
    s=os.path.splitext(os.path.basename(fn))[0]
    for p in ("filtered_","unfiltered_"):
        if s.startswith(p): s=s[len(p):]
    return s[:-8] if s.endswith("_2spedup") else s
def load_labels():
    lab={}
    for csv in ["coral_hr_validation_full_set_updated.csv","coral_hr_validation_moderate_sq_efast_hr_outputs.csv"]:
        df=pd.read_csv(os.path.join(ROOT,csv))
        for _,r in df.iterrows():
            lab[amkey(r["amazon_file_name"])]=dict(
                ecg=pd.to_numeric(r.get("efast_ecg_hr_value"),errors="coerce"),
                pcg=pd.to_numeric(r.get("efast_pcg_hr_value"),errors="coerce"),
                coral=pd.to_numeric(r.get("CORAL HR"),errors="coerce"),
                ident=str(r.get("identifier")))
    return lab
def in_dev(ident):
    return int(hashlib.md5(str(ident).encode()).hexdigest(),16)%100 < 40

def classify(det,gt):
    if not np.isfinite(det): return "nan"
    if abs(det-gt)<=TOL: return "correct"
    for name,mult in [("half",0.5),("double",2.0),("third",1/3.),("triple",3.0),("two-thirds",2/3.)]:
        if abs(det-gt*mult)<=max(TOL,0.06*gt*mult): return name
    return "other"

def run(est_name):
    est=ESTIMATORS[est_name]; lab=load_labels(); rows=[]
    for sub in sorted(os.listdir(ROOT)):
        sd=os.path.join(ROOT,sub)
        if not os.path.isdir(sd): continue
        for w in sorted(glob.glob(os.path.join(sd,"*.wav"))):
            k=wkey(w); L=lab.get(k)
            if L is None or not np.isfinite(L["ecg"]): continue
            fs,data=wavfile.read(w); data=data.mean(axis=1) if data.ndim>1 else data
            data=data.astype(np.float64); data/=np.max(np.abs(data))+1e-10
            det=est(data,fs); gt=float(L["ecg"])
            rows.append(dict(folder=sub,ident=L["ident"],gt=gt,det=det,
                             err=abs(det-gt),cls=classify(det,gt),
                             dev=in_dev(L["ident"]),coral=L["coral"],pcg=L["pcg"]))
    return pd.DataFrame(rows)

def summarize(df,title):
    n=len(df); w5=(df["err"]<=5).mean()*100; w3=(df["err"]<=3).mean()*100
    mae=df["err"].mean(); med=df["err"].median()
    vc=df["cls"].value_counts().to_dict()
    print(f"\n### {title}  (n={n})")
    print(f"  within5={w5:.0f}%  within3={w3:.0f}%  MAE={mae:.1f}  median={med:.1f}")
    print(f"  errors: half={vc.get('half',0)} double={vc.get('double',0)} third={vc.get('third',0)} "
          f"triple={vc.get('triple',0)} two-thirds={vc.get('two-thirds',0)} other={vc.get('other',0)} nan={vc.get('nan',0)}")
    print("  by HR band:")
    for lo,hi in BANDS:
        b=df[(df["gt"]>lo)&(df["gt"]<=hi)]
        if len(b)==0: continue
        print(f"    {lo:>3}-{hi:<3} n={len(b):3d}  within5={ (b['err']<=5).mean()*100:3.0f}%  MAE={b['err'].mean():5.1f}"
              f"  half={ (b['cls']=='half').sum()} double={ (b['cls']=='double').sum()} third={ (b['cls']=='third').sum()} other={ (b['cls']=='other').sum()}")

if __name__=="__main__":
    names=sys.argv[1:] or ["baseline"]
    for name in names:
        df=run(name)
        ndev=df["dev"].sum(); print(f"\n========== Estimator: {name}   files: {len(df)}   split train={len(df)-ndev} dev={ndev} (by patient) ==========")
        summarize(df,f"{name} - ALL")
        summarize(df[~df["dev"]],f"{name} - TRAIN")
        summarize(df[df["dev"]],f"{name} - DEV (held out)")
        df.to_csv(os.path.join(r"C:\Users\daniel\Desktop\pcg_abab",f"results_{name}.csv"),index=False)
    # benchmarks (same for all)
    df0=run(names[0])
    print("\n--- benchmarks (vs same GT) ---")
    for col in ["coral","pcg"]:
        c=df0.dropna(subset=[col])
        if len(c):
            e=(pd.to_numeric(c[col],errors="coerce")-c["gt"]).abs()
            print(f"  {col:6}: n={len(c):3d}  within5={(e<=5).mean()*100:3.0f}%  MAE={e.mean():.1f}")
