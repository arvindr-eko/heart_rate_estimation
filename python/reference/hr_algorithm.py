#!/usr/bin/env python3
"""
Heart-rate from a phonocardiogram (PCG / heart-sound) recording, via feature-shaped
autocorrelation. Standalone -- depends only on numpy + scipy.

The recording is split into 4 frequency sub-bands; an amplitude envelope of each band is
autocorrelated; the four autocorrelations are summed. Because the two heart sounds (S1
"lub" and S2 "dub") differ across the sub-bands, the half-cycle (S1->S2) peak is
suppressed and the detector locks onto the TRUE cardiac period instead of doubling it.
A confidence value (tallest peak / next peak) indicates how clear the reading is.

Usage:
    python hr_algorithm.py <file.wav>      # one file
    python hr_algorithm.py <folder>        # every .wav in a folder
    python hr_algorithm.py                 # defaults to ./audio
"""
import sys, os, glob
import numpy as np
from scipy.signal import butter, lfilter, decimate, resample_poly
from scipy.io import wavfile

DEC_FS = 500       # work at 500 Hz (heart sounds are < ~150 Hz)
FRAME_FS = 100     # envelope frame rate for the autocorrelation
LAGPOW = 0.25      # gentle "prefer shorter lag" bias (kills residual halving; safe here)
BANDS = [(15, 30), (30, 50), (50, 80), (80, 130)]   # the 4 sub-bands (Hz)

def to_dec(x, fs):
    """Decimate the audio to 500 Hz (integer factors; resample otherwise)."""
    if fs == DEC_FS: return x.astype(float)
    if fs % DEC_FS == 0:
        q = fs // DEC_FS; y = x.astype(float)
        while q > 1:
            f = next((k for k in (8, 5, 4, 2) if q % k == 0), 2)
            y = decimate(y, f, ftype='iir'); q //= f
        return y
    return resample_poly(x.astype(float), DEC_FS, fs)

def _bandpass(x, lo, hi):
    b, a = butter(2, [lo/(DEC_FS/2), hi/(DEC_FS/2)], 'band'); return lfilter(b, a, x)

def _envelope(x, fc=18):
    """Amplitude envelope = rectify + 1-pole IIR low-pass (no Hilbert/FFT needed)."""
    a = np.exp(-2*np.pi*fc/DEC_FS); return lfilter([1-a], [1.0, -a], np.abs(x))

def _features(dd):
    F = []; step = int(DEC_FS/FRAME_FS)
    for lo, hi in BANDS:
        e = _envelope(_bandpass(dd, lo, hi))[::step]
        s = e.std(); F.append((e - e.mean())/(s if s > 0 else 1))   # z-score each band
    L = min(len(f) for f in F)
    return np.array([f[:L] for f in F])

def heart_rate(F):
    """Return (bpm, confidence) from the 4 z-scored band envelopes."""
    T = F.shape[1]; lo = int(0.30*FRAME_FS); hi = min(int(1.5*FRAME_FS), T-1)   # 40-200 bpm
    if hi <= lo: return None, 0.0
    lags = np.arange(lo, hi); S = np.zeros(len(lags))
    for d in range(F.shape[0]):
        f = F[d]; r0 = np.dot(f, f)/T
        for i, l in enumerate(lags):
            S[i] += (np.dot(f[:T-l], f[l:])/T)/(r0 + 1e-9)           # restricted-lag autocorr (biased /T: tapers unreliable long lags)
    S = S/(lags.astype(float)**LAGPOW)
    s = S - S.min()
    pk = [i for i in range(1, len(s)-1) if s[i] >= s[i-1] and s[i] > s[i+1]]
    if not pk: return None, 1.0
    pk.sort(key=lambda i: -s[i]); p1 = pk[0]; bpm = 60.0/(lags[p1]/FRAME_FS)
    p2 = next((i for i in pk[1:] if abs(lags[i]-lags[p1]) >= 8), None)
    conf = (s[p1]/(s[p2] + 1e-9)) if p2 is not None else 9.99         # confidence = winner / runner-up
    return bpm, conf

def analyze_wav(path):
    fs, d = wavfile.read(path)
    d = d.mean(axis=1) if d.ndim > 1 else d
    d = d.astype(float); d /= (np.max(np.abs(d)) + 1e-9)
    return heart_rate(_features(to_dec(d, fs)))

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "audio"
    files = [arg] if arg.lower().endswith(".wav") else sorted(glob.glob(os.path.join(arg, "*.wav")))
    if not files:
        print("No .wav files found at:", arg); sys.exit(1)
    print(f"{'file':<36}{'HR (bpm)':>9}{'confidence':>13}")
    print("-"*58)
    for f in files:
        bpm, conf = analyze_wav(f)
        print(f"{os.path.basename(f):<36}{('%.0f'%bpm) if bpm else 'n/a':>9}{conf:>11.2f}x")
