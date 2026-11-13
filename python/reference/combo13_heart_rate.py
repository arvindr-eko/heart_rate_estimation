#!/usr/bin/env python3
"""
Combo 13 PCG Heart-Rate Estimator -- single-file, self-contained export.

This is the complete, real-time heart-rate detection pipeline as validated 2026-07-31
(HARMONIC_VETO_PARAM_SWEEP_2026-07-31.txt), consolidated out of the pcg_abab project's several
interdependent modules (pcg_algorithm.py, current_pipeline_algorithm.py, pitch_family_algorithm.py,
weighted_rolling_median_check.py, detrend_algorithm.py, compressor_algorithm.py) into one file with
NO project-internal dependencies -- only numpy and scipy are required.

"Combo 13" is the winning result of a 600-combo fine-tuning sweep of the harmonic-confirmation veto
(added 2026-07-31) run against the general validated baseline (bare shipped DSP/combine, tilt
agreement signal, compressor off). It beat the shipped veto defaults on every axis at once:
    correct%              67.9% -> 69.4%
    no-display rate        24.6% -> 22.2%
    first-display latency  5.04s -> 4.57s avg
via: harmonic_veto_height_frac 0.15->0.059, harmonic_veto_hi_frac 2.05->2.082,
     harmonic_veto_lo_frac 1.95->1.924, harmonic_veto_hi_ext_mult 2.10->2.181,
     teal_jump_confirm_ticks 4->3, teal_bpm_delay_slope 0.0538->0.00239.
Every other parameter is the general shipped baseline (agreement_signal='tilt', compressor off) --
NOT the separate combo 292/96 "deliberate CornerCases overfit" configuration also explored this
project, which scores much worse on the general corpus.

PIPELINE (per 250ms tick, over an 8s trailing raw-audio buffer):
  1. Onset/flatline gating + spike detection -- timestamps events, drives everything below.
  2. removeTransients(): multi-pass detect->extend->interpolate artifact excision.
  3. Declick: whole-window robust-sigma clip.
  4. Decimate to 500Hz.
  5. 3-band filterbank (30-50/50-80/80-130Hz) -> per-band envelope -> per-band z-score+winsorize
     -> summed envelope.
  6. Robust (trimmed-mean-of-products) autocorrelation over an adaptive 4-8s growing window.
  7. Detrend the autocorrelation curve, pick its tallest local-maximum peak -> raw BPM candidate.
  8. HARMONIC-CONFIRMATION VETO (2026-07-31): reject the candidate unless a genuine 2nd-harmonic
     peak is also visible in an extended-lag autocorrelation -- a strong general confidence/garbage
     filter (does NOT disambiguate octaves, see this project's own validation writeup).
  9. Jump-confirmation guard + bpm-scaled settling-delay guard.
 10. An independent pitch-family "tilt" trace (bandpass -> framing -> FFT -> autocorrelation) provides
     a corroborating-agreement bonus.
 11. Weighted-median combine over a trailing window of accepted pushes -> the displayed BPM.

USAGE

    Batch (simplest): give it a full recording, get back a BPM time series.

        from combo13_heart_rate import estimate_heart_rate
        result = estimate_heart_rate(audio, fs=4000)
        for row in result:
            print(row['t'], row['bpm'])   # row['bpm'] is None until the pipeline trusts a reading

    Streaming (for live audio):

        est = Combo13HeartRateEstimator(fs=4000)
        while True:
            est.push(next_chunk_of_samples)      # any chunk size; call tick() once per ~250ms
            r = est.tick()
            print(r['t'], r['bpm'])

    Command line:

        python combo13_heart_rate.py recording.wav
"""
import numpy as np
from scipy.signal import lfilter, butter

# ============================================================================================
# CONSTANTS (pcg_algorithm.py)
# ============================================================================================
FS_IN = 4000.0          # expected input sample rate (Hz) -- overridden per-call by the real fs
TICK_S = 0.25           # nominal tick period (s); caller decides when to call tick()

# anti-alias + decimate 4000Hz-domain-rate -> 500Hz (4th-order Butterworth lowpass, integer stride 8)
AA_B = [0.0005891316955801649, 0.0023565267823206595, 0.0035347901734809895,
        0.0023565267823206595, 0.0005891316955801649]
AA_A = [1, -3.09905041635851, 3.6812793328016165, -1.9759888358976645, 0.40318602658384056]
AA_DECIM = 8

# 3 fixed band-pass filters at 500Hz (4th-order Butterworth) spanning S1/S2 heart-sound content.
BANDS = [
    ([0.013359200027856502, 0, -0.026718400055713003, 0, 0.013359200027856502],
     [1, -3.2216975944168245, 4.259742833119654, -2.6933188730452877, 0.700896781188403]),      # 30-50Hz
    ([0.02785976611713605, 0, -0.0557195322342721, 0, 0.02785976611713605],
     [1, -2.4220309055301232, 2.962756451291472, -1.8462867837112926, 0.5869195080611904]),      # 50-80Hz
    ([0.06745527388907191, 0, -0.13491054777814382, 0, 0.06745527388907191],
     [1, -0.8218517544917526, 1.3177346226477535, -0.5147610573338093, 0.41280159809618866]),    # 80-130Hz
]
ENV_A = 0.7975628241529755    # single-pole envelope smoothing coefficient
FRAME_DS = 5                  # 500Hz -> 100Hz feature/frame rate
FFS = 100.0                   # feature frame rate (Hz)
LO_LAG, HI_LAG = 30, 150      # autocorrelation lag range in frames -> 40-200bpm
LAG_POW = 0.1                 # lag-power reweighting exponent
FEAT_CLIP = 4.5                # winsorize clip, in z-score units (constructor default)

DECAY_ADAPTIVE_TAU_MIN, DECAY_ADAPTIVE_TAU_MAX = 1.0, 8.0


# ============================================================================================
# LOW-LEVEL DSP (pcg_algorithm.py)
# ============================================================================================

def iir(b, a, x):
    return lfilter(b, a, np.asarray(x, dtype=np.float64))


def decimate(x):
    y = iir(AA_B, AA_A, x)
    m = len(y) // AA_DECIM
    return y[:m * AA_DECIM:AA_DECIM]


def env_singlepole(y):
    # p[n] = (1-a)*|y[n]| + a*p[n-1], exactly lfilter([1-a],[1,-a],|y|)
    return lfilter([1 - ENV_A], [1, -ENV_A], np.abs(y))


def feat_norm_clip(seg_500, band_idx, clip=FEAT_CLIP):
    y = iir(BANDS[band_idx][0], BANDS[band_idx][1], seg_500)
    e = env_singlepole(y)
    m = len(e) // FRAME_DS
    f = e[:m * FRAME_DS:FRAME_DS].copy()
    mean = f.mean()
    sd = f.std() or 1.0
    z = (f - mean) / sd
    return np.clip(z, -clip, clip)


def sum_env_norm(seg_500, clip=FEAT_CLIP):
    total = None
    for b in range(len(BANDS)):
        f = feat_norm_clip(seg_500, b, clip)
        total = f.copy() if total is None else total + f
    return total


def broad_env(seg_500):
    """Unnormalized summed envelope across the 3 bands, at the 100Hz frame rate -- used only for
    signal-level (onset/offset) detection, never fed into the bpm autocorrelation."""
    env = None
    for b in range(len(BANDS)):
        y = iir(BANDS[b][0], BANDS[b][1], seg_500)
        e = env_singlepole(y)
        m = len(e) // FRAME_DS
        ds = e[:m * FRAME_DS:FRAME_DS]
        env = ds.copy() if env is None else env + ds
    return env


def add_autocorr(S, f, lo, hi):
    T = len(f)
    if T <= hi:
        return
    r0 = (f * f).mean()
    for l in range(lo, hi):
        s = np.dot(f[:T - l], f[l:])
        S[l - lo] += (s / T) / (r0 + 1e-9)


def add_autocorr_decay(S, f, lo, hi, tau_frames):
    """Age-weighted autocorrelation: each product pair is weighted by how OLD its earlier sample
    is (exponential decay, time constant tau_frames) -- feeds only the adaptive preview estimate
    that sizes the buffer's post-event wait timer, never the displayed BPM."""
    T = len(f)
    if T <= hi:
        return
    idx = np.arange(T)
    w = np.exp(-(T - 1 - idx) / tau_frames)
    r0 = np.sum(w * f * f)
    r0w = np.sum(w)
    r0 = r0 / (r0w if r0w != 0 else 1.0)
    for l in range(lo, hi):
        s = np.sum(w[:T - l] * f[:T - l] * f[l:])
        sw = np.sum(w[:T - l])
        S[l - lo] += (s / (sw if sw != 0 else 1.0)) / (r0 + 1e-9)


def estimate_from_S(S, lag_pow=LAG_POW):
    if S is None:
        return None
    n = len(S)
    Sw = np.empty(n)
    for l in range(LO_LAG, HI_LAG):
        Sw[l - LO_LAG] = S[l - LO_LAG] / (l ** lag_pow)
    Sw -= Sw.min()
    pk = [i for i in range(1, n - 1) if Sw[i] >= Sw[i - 1] and Sw[i] > Sw[i + 1]]
    if not pk:
        return dict(bpm=60.0 / (LO_LAG / FFS), conf=1.0, lag=LO_LAG)
    pk.sort(key=lambda i: -Sw[i])
    p1 = pk[0]
    bl = LO_LAG + p1
    p2 = -1
    for k in range(1, len(pk)):
        if abs(pk[k] - p1) >= 8:
            p2 = pk[k]
            break
    if p2 < 0 and len(pk) > 1:
        p2 = pk[1]
    conf = 1.2 if p2 < 0 else Sw[p1] / (Sw[p2] + 1e-9)
    return dict(bpm=60.0 / (bl / FFS), conf=conf, lag=bl)


def estimate_decay(seg_500, tau_s):
    """Adaptive-decay preview estimator: per-band autocorrelation (age-weighted, NOT sum-envelope
    first), summed across bands. Used ONLY to size the rolling window's post-event wait timer,
    never displayed."""
    T500 = len(seg_500)
    if T500 < 3 * 500:
        return None
    n = HI_LAG - LO_LAG
    S = np.zeros(n)
    tau_frames = tau_s * FFS
    for b in range(len(BANDS)):
        f = feat_norm_clip(seg_500, b)
        add_autocorr_decay(S, f, LO_LAG, HI_LAG, tau_frames)
    return estimate_from_S(S)


def detect_spike_runs(x, mult):
    """Robust median-based spike detector: flags contiguous runs of samples far above the
    window's own robust scale, one marker per run at that run's peak-amplitude sample."""
    if len(x) == 0:
        return []
    absx = np.abs(x)
    med = np.median(absx)
    lim = mult * ((med * 1.4826) or 1e-6)
    mask = absx > lim
    if not mask.any():
        return []
    idx = np.flatnonzero(mask)
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[0, breaks + 1]
    ends = np.r_[breaks, len(idx) - 1]
    runs = []
    for s, e in zip(starts, ends):
        seg_idx = idx[s:e + 1]
        runs.append(int(seg_idx[np.argmax(absx[seg_idx])]))
    return runs


def weighted_median(vals, weights):
    pairs = sorted(zip(vals, weights), key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        return float(np.median(vals))
    cum = 0.0
    half = total / 2.0
    for v, w in pairs:
        cum += w
        if cum >= half:
            return float(v)
    return float(pairs[-1][0])


# ============================================================================================
# ARTIFACT REMOVAL (current_pipeline_algorithm.py)
# ============================================================================================

def declick_mult(x, mult):
    """Whole-window clip to `mult` robust-sigma (median-based, not mean/std)."""
    if len(x) == 0:
        return x
    x = np.asarray(x, dtype=np.float64)
    med = np.median(np.abs(x))
    lim = mult * ((med * 1.4826) or 1e-6)
    return np.clip(x, -lim, lim)


def _interp_over_mask(dst, src, mask):
    n = len(mask)
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < n and mask[j]:
            j += 1
        start_val = src[i - 1] if i > 0 else (src[j] if j < n else src[n - 1])
        end_val = src[j] if j < n else (src[i - 1] if i > 0 else src[0])
        length = j - i
        t = (np.arange(length) + 1) / (length + 1)
        dst[i:j] = start_val + (end_val - start_val) * t
        i = j


def remove_transients(x, detect_mult, extend_mult, pad_samples, pctl, passes):
    """Multi-pass detect -> extend -> interpolate artifact excision. Flags samples far above a
    robust percentile-based baseline, grows the flagged region outward while neighbors still
    exceed a looser threshold, then interpolates linearly across the excised region. Re-detects
    against the progressively cleaner baseline for `passes` iterations, then pads the final mask
    by `pad_samples` before one last interpolation."""
    n = len(x)
    if n == 0:
        return np.zeros(0)
    x = np.asarray(x, dtype=np.float64)
    absx = np.abs(x)
    grown = np.zeros(n, dtype=bool)
    work = x.copy()
    for _ in range(passes):
        work_abs = np.abs(work)
        sorted_abs = np.sort(work_abs)
        idx = min(int(n * pctl), n - 1)
        baseline = sorted_abs[idx] or 1e-9
        scale = (baseline * 1.4826) or 1e-6
        lim = detect_mult * scale
        extend_lim = extend_mult * scale

        found_new = False
        i = 0
        while i < n:
            if grown[i] or absx[i] <= lim:
                i += 1
                continue
            lo = i
            while lo > 0 and absx[lo - 1] > extend_lim:
                lo -= 1
            hi = i
            while hi < n - 1 and absx[hi + 1] > extend_lim:
                hi += 1
            for k in range(lo, hi + 1):
                if not grown[k]:
                    grown[k] = True
                    found_new = True
            i = hi + 1
        if not found_new:
            break
        work = x.copy()
        _interp_over_mask(work, x, grown)

    padded = grown.copy()
    if padded.any() and pad_samples > 0:
        # simple fixed-width dilation without a scipy.ndimage dependency
        idxs = np.flatnonzero(grown)
        for center in idxs:
            lo = max(0, center - pad_samples)
            hi = min(n, center + pad_samples + 1)
            padded[lo:hi] = True
    out = x.copy()
    _interp_over_mask(out, x, padded)
    return out


# ============================================================================================
# ROBUST AUTOCORRELATION + DETRENDING (current_pipeline_algorithm.py / detrend_algorithm.py)
# ============================================================================================

def add_autocorr_robust(S, f, lo, hi, trim_frac):
    """Trimmed-mean-of-products autocorrelation: at each lag, sorts the per-index product terms
    and averages only the middle (1-2*trim_frac) fraction -- down-weights a single contaminated
    instant at exactly the lags it would otherwise distort, with no explicit spike detection."""
    T = len(f)
    if T <= hi:
        return
    r0 = float(np.mean(f * f))
    for l in range(lo, hi):
        n = T - l
        prod = f[:n] * f[l:l + n]
        sorted_prod = np.sort(prod)
        trim = int(n * trim_frac)
        if n - 2 * trim > 0:
            mean_val = sorted_prod[trim:n - trim].mean()
        else:
            mean_val = 0.0
        S[l - lo] += mean_val / (r0 + 1e-9)


def estimate_sumenv_robust(dec, trim_frac, feat_clip=FEAT_CLIP):
    T500 = len(dec)
    if T500 < 3 * 500:
        return None
    n = HI_LAG - LO_LAG
    S = np.zeros(n)
    add_autocorr_robust(S, sum_env_norm(dec, clip=feat_clip), LO_LAG, HI_LAG, trim_frac)
    Sw = np.empty(n)
    for l in range(LO_LAG, HI_LAG):
        Sw[l - LO_LAG] = S[l - LO_LAG] / (l ** LAG_POW)
    Sw = Sw - Sw.min()
    pk = [i for i in range(1, n - 1) if Sw[i] >= Sw[i - 1] and Sw[i] > Sw[i + 1]]
    if not pk:
        return dict(bpm=60.0 / (LO_LAG / FFS), conf=1.0, Sw=Sw, lo=LO_LAG)
    pk.sort(key=lambda i: -Sw[i])
    p1 = pk[0]
    bl = LO_LAG + p1
    p2 = -1
    for k in pk[1:]:
        if abs(k - p1) >= 8:
            p2 = k
            break
    if p2 < 0 and len(pk) > 1:
        p2 = pk[1]
    conf = 1.2 if p2 < 0 else Sw[p1] / (Sw[p2] + 1e-9)
    return dict(bpm=60.0 / (bl / FFS), conf=conf, Sw=Sw, lo=LO_LAG)


def moving_average(arr, half_width):
    """Box-filter moving average with edge-clamping (average over [i-h, i+h] clipped to array
    bounds, not a symmetric-padded/wrapped average)."""
    n = len(arr)
    csum = np.concatenate([[0.0], np.cumsum(arr)])
    out = np.empty(n)
    for i in range(n):
        lo = max(0, i - half_width)
        hi = min(n - 1, i + half_width)
        out[i] = (csum[hi + 1] - csum[lo]) / (hi - lo + 1)
    return out


def detrend_sw(Sw, half_width):
    return Sw - moving_average(Sw, half_width)


def find_all_local_maxima(Sw):
    return [i for i in range(1, len(Sw) - 1) if Sw[i] >= Sw[i - 1] and Sw[i] > Sw[i + 1]]


def pick_top_with_conf(candidate_idxs, value_of, lo):
    if not candidate_idxs:
        return None
    sorted_idxs = sorted(candidate_idxs, key=lambda i: -value_of(i))
    p1 = sorted_idxs[0]
    p2 = -1
    for k in sorted_idxs[1:]:
        if abs(k - p1) >= 8:
            p2 = k
            break
    v1 = max(1e-6, value_of(p1))
    v2 = max(1e-6, value_of(p2)) if p2 >= 0 else None
    conf = 1.2 if p2 < 0 else v1 / (v2 + 1e-9)
    return dict(bpm=60.0 / ((lo + p1) / FFS), conf=conf)


def estimate_detrend_from_mw(r, half_width):
    """Detrends an ALREADY-COMPUTED Sw (no new autocorrelation) and re-picks its peak from the
    detrended curve -- sharpens the true periodicity peak against a slow-moving baseline trend."""
    if r is None or 'Sw' not in r:
        return None
    Sw = r['Sw']
    det = detrend_sw(Sw, half_width)
    candidates = find_all_local_maxima(det)
    return pick_top_with_conf(candidates, lambda i: det[i], r['lo'])


# ============================================================================================
# HARMONIC-CONFIRMATION VETO (2026-07-31, fine-tuned as "combo 13")
# ============================================================================================
# Mirrors a legacy whole-signal algorithm's own hard 2nd-harmonic requirement, applied as a per-tick
# trustworthiness check on THIS pipeline's own candidate: extends the robust autocorrelation's lag
# range out to ~2.18x the candidate's own lag (the shipped Sw curve only spans LO_LAG..HI_LAG =
# 40-200bpm, too narrow to see a slow candidate's own 2nd harmonic) and checks for a genuine
# local-max peak near 2x that lag. Does NOT discriminate WHICH octave is correct (half+double error
# rate barely differs between confirmed/unconfirmed ticks) but IS a strong general confidence/
# garbage filter (unconfirmed ticks are ~59% noise vs. ~5% when confirmed). Wired in as a VETO: an
# unconfirmed candidate is suppressed to None before either guard below ever sees it.
#
# These 4 values are COMBO 13's fine-tuned result (HARMONIC_VETO_PARAM_SWEEP_2026-07-31.txt),
# beating the original shipped defaults (0.15/1.95/2.05/2.10) on accuracy, no-display rate, AND
# latency simultaneously.
HARMONIC_VETO_HI_EXT_MULT = 2.1813774614710146
HARMONIC_VETO_HEIGHT_FRAC = 0.05895565949526575
HARMONIC_VETO_LO_FRAC = 1.9239938526174543
HARMONIC_VETO_HI_FRAC = 2.081876428164688


def check_harmonic_confirmed(seg_clean_ar, win_nsamp, cand_bpm, feat_clip, robust_trim_frac,
                              hi_ext_mult=HARMONIC_VETO_HI_EXT_MULT, height_frac=HARMONIC_VETO_HEIGHT_FRAC,
                              lo_frac=HARMONIC_VETO_LO_FRAC, hi_frac=HARMONIC_VETO_HI_FRAC):
    """True/False/None (None = window too short to even look -- treated as "no opinion", NOT a veto
    trigger, since the caller's own suppress_circle already handles "not enough data yet")."""
    if cand_bpm is None or len(seg_clean_ar) < win_nsamp:
        return None
    cand_lag = 6000.0 / cand_bpm  # frames at FFS=100Hz (60*FFS/bpm)
    hi_ext = int(np.ceil(hi_ext_mult * cand_lag)) + 2
    if hi_ext <= LO_LAG + 2:
        return False  # candidate lag already so long 2x runs off any reasonable window
    seg = seg_clean_ar[-win_nsamp:]
    f = sum_env_norm(seg, clip=feat_clip)
    n_ext = hi_ext - LO_LAG
    if len(f) <= hi_ext:
        return False  # window too short to even look
    S_ext = np.zeros(n_ext)
    add_autocorr_robust(S_ext, f, LO_LAG, hi_ext, robust_trim_frac)
    target_lo, target_hi = lo_frac * cand_lag, hi_frac * cand_lag
    thresh = height_frac * (S_ext.max() if len(S_ext) else 0)
    for i in range(1, n_ext - 1):
        lag = LO_LAG + i
        if target_lo < lag < target_hi and S_ext[i] >= S_ext[i - 1] and S_ext[i] > S_ext[i + 1] and S_ext[i] > thresh:
            return True
    return False


# ============================================================================================
# INDEPENDENT AGREEMENT SIGNAL: pitch-family "tilt" trace (pitch_family_algorithm.py)
# ============================================================================================

_BP_CACHE = {}


def kumar_pitch_bandpass(raw, fs, bp_lo_hz, bp_hi_hz):
    key = (fs, bp_lo_hz, bp_hi_hz)
    if key not in _BP_CACHE:
        b, a = butter(2, [bp_lo_hz / (fs / 2), bp_hi_hz / (fs / 2)], btype='band')
        _BP_CACHE[key] = (b, a)
    b, a = _BP_CACHE[key]
    return lfilter(b, a, raw)


def pitch_feature_for_segment(seg, fs, fft_lo_hz, fft_hi_hz, tilt_split_hz):
    n = len(seg)
    if n < 4:
        return None
    n_fft = 1
    while n_fft < n:
        n_fft *= 2
    n_fft = max(n_fft, 32)
    hann = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / (n - 1)) if n > 1 else np.ones(n)
    windowed = np.zeros(n_fft)
    windowed[:n] = seg * hann
    spec = np.fft.fft(windowed)
    n_bins = n_fft // 2 + 1
    mag = np.abs(spec[:n_bins])
    freq_res = fs / n_fft
    lo_bin = max(1, round(fft_lo_hz / freq_res))
    hi_bin = min(n_bins - 1, round(min(fs / 2 * 0.99, fft_hi_hz) / freq_res))
    if hi_bin <= lo_bin:
        return None
    ks = np.arange(lo_bin, hi_bin + 1)
    band_mag = mag[lo_bin:hi_bin + 1]
    split_bin = round(tilt_split_hz / freq_res)
    lo_mask = ks < split_bin
    lo_e = float((band_mag[lo_mask] ** 2).sum())
    hi_e = float((band_mag[~lo_mask] ** 2).sum())
    tilt = float(np.log((hi_e + 1e-9) / (lo_e + 1e-9)))
    return dict(tilt=tilt)


def continuous_pitch_trace(x, fs, frame_ms, hop_frac, gate_frac, fft_lo_hz, fft_hi_hz, tilt_split_hz):
    frame_len = max(4, round(frame_ms / 1000 * fs))
    hop = max(1, round(frame_len * hop_frac))
    n = len(x)
    n_frames = max(0, (n - frame_len) // hop + 1)
    tilts = np.zeros(n_frames)
    rms = np.zeros(n_frames)
    for k in range(n_frames):
        base = k * hop
        seg = x[base:base + frame_len]
        rms[k] = np.sqrt(np.mean(seg * seg)) if len(seg) else 0.0
    max_rms = max(float(rms.max()) if n_frames else 1e-12, 1e-12)
    gate = max_rms * gate_frac
    last_t = 0.0
    for k in range(n_frames):
        base = k * hop
        if rms[k] >= gate:
            feat = pitch_feature_for_segment(x[base:base + frame_len], fs, fft_lo_hz, fft_hi_hz, tilt_split_hz)
            if feat is not None:
                last_t = feat['tilt']
        tilts[k] = last_t
    return dict(tilts=tilts, frame_len=frame_len, hop=hop)


def pitch_autocorr_lag_range(trace_rate):
    lo = max(1, round(trace_rate * 60 / 200))
    hi = max(lo + 2, round(trace_rate * 60 / 40))
    return lo, hi


def trace_autocorr(x, lo, hi):
    n = len(x)
    if n <= hi:
        return np.zeros(0)
    c = x - x.mean()
    S = np.zeros(hi - lo)
    add_autocorr(S, c, lo, hi)
    return S


def peak_idx(Sw):
    """Tallest local maximum (not just the global max)."""
    if len(Sw) < 3:
        return 0
    best, bv = 0, -np.inf
    for i in range(1, len(Sw) - 1):
        if Sw[i] >= Sw[i - 1] and Sw[i] > Sw[i + 1] and Sw[i] > bv:
            bv = Sw[i]
            best = i
    return best


# ============================================================================================
# OPTIONAL: dynamic-range compressor (compressor_algorithm.py) -- OFF by default for combo 13,
# kept available since it's a validated, cheap-to-include real feature (fixes strong recordings
# tripping the spike/artifact detectors on their own genuine S1/S2, with zero effect on quiet ones).
# ============================================================================================

def dynamic_range_compress(x, fs, threshold=0.08, ratio=8.0, attack_s=0.01, release_s=0.15,
                            knee_db=6.0, makeup_gain_db=0.0, eps=1e-9):
    n = len(x)
    if n == 0:
        return np.zeros(0)
    x = np.asarray(x, dtype=np.float64)
    absx = np.abs(x)
    threshold_db = 20.0 * np.log10(max(threshold, eps))
    a_att = float(np.exp(-1.0 / max(1.0, attack_s * fs)))
    a_rel = float(np.exp(-1.0 / max(1.0, release_s * fs)))
    makeup = 10.0 ** (makeup_gain_db / 20.0)
    half_knee = knee_db / 2.0

    env = 0.0
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        e = absx[i]
        a = a_att if e > env else a_rel
        env = a * env + (1.0 - a) * e
        level_db = 20.0 * np.log10(max(env, eps))

        over2 = 2.0 * (level_db - threshold_db)
        if knee_db <= 0 or over2 < -knee_db:
            gc_db = level_db
        elif abs(over2) <= knee_db:
            gc_db = level_db + (1.0 / ratio - 1.0) * (level_db - threshold_db + half_knee) ** 2 / (2.0 * knee_db)
        else:
            gc_db = threshold_db + (level_db - threshold_db) / ratio

        gain_reduction_db = gc_db - level_db
        gain = (10.0 ** (gain_reduction_db / 20.0)) * makeup
        out[i] = x[i] * gain
    return out


# ============================================================================================
# THE ESTIMATOR
# ============================================================================================

class Combo13HeartRateEstimator:
    """Stateful, tick-driven heart-rate estimator at combo 13's fine-tuned settings. Push raw audio
    samples as they arrive and call tick() once per TICK_S (250ms) of new audio.

    Every constructor argument defaults to combo 13's exact validated value -- a bare
    Combo13HeartRateEstimator(fs=...) reproduces the validated result exactly. Pass overrides only
    if you deliberately want to explore off this configuration."""

    def __init__(self, fs=FS_IN,
                 signal_onset_thresh=0.00021, onset_debounce_ticks=4,
                 spike_detect_mult=20.0,
                 transient_limit_mult=4.29,
                 artifact_detect_mult=16.96, artifact_extend_mult=8.02, artifact_baseline_pctl=0.4231,
                 artifact_passes=5, artifact_pad_samples=36,
                 robust_trim_frac=0.0079,
                 feat_clip=FEAT_CLIP,
                 single_win_min=4.0, single_win_max=8.0,
                 teal_push_half_width=36,
                 cons_wait_s=4.0, cons_wait_extra_s_per_bpm=0.020,
                 weighted_med_trailing_n=8,
                 agreement_bpm_tol=11.10, agree_bonus=2.70,
                 tent_peak_bpm=55.9, tent_peak_h=2.29, tent_lo_h=0.56, tent_hi_h=0.81,
                 teal_jump_ratio=1.4, teal_jump_confirm_ticks=3, teal_bpm_delay_slope=0.002388504108629796,
                 pitch_bp_lo_hz=17.18, pitch_bp_hi_hz=175.17,
                 pitch_frame_ms=20.02, pitch_hop_frac=0.422, pitch_gate_frac=0.0658,
                 pitch_fft_lo_hz=14.27, pitch_fft_hi_hz=226.82, tilt_split_hz=45.06,
                 use_compressor=False, compressor_threshold=0.08, compressor_ratio=8.0,
                 compressor_attack_s=0.01, compressor_release_s=0.15,
                 use_harmonic_veto=True,
                 harmonic_veto_hi_ext_mult=HARMONIC_VETO_HI_EXT_MULT,
                 harmonic_veto_height_frac=HARMONIC_VETO_HEIGHT_FRAC,
                 harmonic_veto_lo_frac=HARMONIC_VETO_LO_FRAC,
                 harmonic_veto_hi_frac=HARMONIC_VETO_HI_FRAC):
        self.fs = fs
        self.signal_onset_thresh = signal_onset_thresh
        self.signal_offset_thresh = signal_onset_thresh * 0.5
        self.onset_debounce_ticks = onset_debounce_ticks
        self.spike_detect_mult = spike_detect_mult
        self.transient_limit_mult = transient_limit_mult
        self.artifact_detect_mult = artifact_detect_mult
        self.artifact_extend_mult = artifact_extend_mult
        self.artifact_baseline_pctl = artifact_baseline_pctl
        self.artifact_passes = artifact_passes
        self.artifact_pad_samples = artifact_pad_samples
        self.robust_trim_frac = robust_trim_frac
        self.feat_clip = feat_clip
        self.single_win_min = single_win_min
        self.single_win_max = single_win_max
        self.teal_push_half_width = teal_push_half_width
        self.cons_wait_s = cons_wait_s
        self.cons_wait_extra_s_per_bpm = cons_wait_extra_s_per_bpm
        self.weighted_med_trailing_n = weighted_med_trailing_n
        self.agreement_bpm_tol = agreement_bpm_tol
        self.agree_bonus = agree_bonus
        self.tent_peak_bpm = tent_peak_bpm
        self.tent_peak_h = tent_peak_h
        self.tent_lo_h = tent_lo_h
        self.tent_hi_h = tent_hi_h
        self.teal_jump_ratio = teal_jump_ratio
        self.teal_jump_confirm_ticks = teal_jump_confirm_ticks
        self.teal_bpm_delay_slope = teal_bpm_delay_slope
        self.pitch_bp_lo_hz = pitch_bp_lo_hz
        self.pitch_bp_hi_hz = pitch_bp_hi_hz
        self.pitch_frame_ms = pitch_frame_ms
        self.pitch_hop_frac = pitch_hop_frac
        self.pitch_gate_frac = pitch_gate_frac
        self.pitch_fft_lo_hz = pitch_fft_lo_hz
        self.pitch_fft_hi_hz = pitch_fft_hi_hz
        self.tilt_split_hz = tilt_split_hz
        self.use_compressor = use_compressor
        self.compressor_threshold = compressor_threshold
        self.compressor_ratio = compressor_ratio
        self.compressor_attack_s = compressor_attack_s
        self.compressor_release_s = compressor_release_s
        self.use_harmonic_veto = use_harmonic_veto
        self.harmonic_veto_hi_ext_mult = harmonic_veto_hi_ext_mult
        self.harmonic_veto_height_frac = harmonic_veto_height_frac
        self.harmonic_veto_lo_frac = harmonic_veto_lo_frac
        self.harmonic_veto_hi_frac = harmonic_veto_hi_frac

        self._raw = np.zeros(0)
        self.el = 0.0
        self.sig_was_onset_ok = False
        self.onset_off_streak = 0
        self.onset_on_streak = 0
        self.onset_candidate_el = None
        self.last_onset_el = None
        self.last_spike_el = None
        self.last_logged_spike_t = -np.inf
        self.prev_last_event_el = -np.inf
        self.frozen_wait_s = cons_wait_s
        self.cons_buf_log = []      # list of dict(val, conf)
        self.tilt_log = []          # list of val-or-None, same index cadence as cons_buf_log
        self.last_accepted_teal_bpm = None
        self.teal_jump_streak = 0
        self.bpm_delay_anchor_bpm = None
        self.bpm_delay_streak = 0
        self.log_cap = 50

    def _required_teal_delay_ticks(self, bpm):
        return max(1, int(np.ceil(1 + self.teal_bpm_delay_slope * max(0.0, bpm - 60))))

    def push(self, samples):
        """Append newly-arrived raw audio samples (any chunk size). Internally kept as a rolling
        8-second buffer."""
        self._raw = np.concatenate([self._raw, np.asarray(samples, dtype=np.float64)])
        max_len = int(8.0 * self.fs)
        if len(self._raw) > max_len:
            self._raw = self._raw[-max_len:]

    def _bpm_favor_center(self, bpm):
        if bpm <= self.tent_peak_bpm:
            t = max(0.0, min(1.0, (bpm - 40) / (self.tent_peak_bpm - 40)))
            return self.tent_lo_h + t * (self.tent_peak_h - self.tent_lo_h)
        t = max(0.0, min(1.0, (bpm - self.tent_peak_bpm) / (200 - self.tent_peak_bpm)))
        return self.tent_peak_h + t * (self.tent_hi_h - self.tent_peak_h)

    def _teal_size_factor(self, bpm, tilt_bpm):
        agrees = tilt_bpm is not None and abs(tilt_bpm - bpm) <= self.agreement_bpm_tol
        factor = self._bpm_favor_center(bpm)
        if agrees:
            factor *= self.agree_bonus
        return factor

    def tick(self):
        """Call once per ~250ms of pushed audio. Returns a dict:
            t                  elapsed seconds since this estimator was created
            bpm                the displayed heart rate, or None if not yet trustworthy
            is_flatline        True if the current instant has no detected signal
            raw_candidate_bpm  the pre-guard candidate (diagnostic; None whenever suppressed)
            harmonic_confirmed True/False/None -- the harmonic-veto's verdict this tick
        """
        el = self.el
        raw = self._raw
        raw_for_detect = (dynamic_range_compress(raw, self.fs, threshold=self.compressor_threshold,
                                                  ratio=self.compressor_ratio,
                                                  attack_s=self.compressor_attack_s,
                                                  release_s=self.compressor_release_s)
                          if (self.use_compressor and len(raw)) else raw)

        raw_clean = declick_mult(raw, self.transient_limit_mult) if len(raw) else raw
        seg_clean = decimate(raw_clean) if len(raw_clean) else np.array([])

        sig_level = 0.0
        if len(seg_clean):
            env = broad_env(seg_clean)
            n = min(len(env), round(1.5 * FFS))
            sig_level = float(np.sqrt(np.mean(env[-n:] ** 2))) if n > 0 else 0.0

        # onset/flatline debounce + hysteresis
        if self.sig_was_onset_ok:
            self.onset_on_streak = 0
            if sig_level < self.signal_offset_thresh:
                self.onset_off_streak += 1
                if self.onset_off_streak >= self.onset_debounce_ticks:
                    self.sig_was_onset_ok = False
            else:
                self.onset_off_streak = 0
        else:
            self.onset_off_streak = 0
            if sig_level >= self.signal_onset_thresh:
                if self.onset_on_streak == 0:
                    self.onset_candidate_el = el
                self.onset_on_streak += 1
                if self.onset_on_streak >= self.onset_debounce_ticks:
                    self.last_onset_el = self.onset_candidate_el
                    self.sig_was_onset_ok = True
                    self.onset_on_streak = 0
            else:
                self.onset_on_streak = 0

        spike_runs = detect_spike_runs(raw_for_detect, mult=self.spike_detect_mult) if len(raw_for_detect) else []
        win_start_el = el - len(raw) / self.fs
        spike_abs_times = [win_start_el + i / self.fs for i in spike_runs]
        if spike_abs_times:
            max_t = max(spike_abs_times)
            if max_t > self.last_logged_spike_t + 0.01:
                self.last_spike_el = max_t
                self.last_logged_spike_t = max_t

        last_event_el = max(self.last_onset_el if self.last_onset_el is not None else -np.inf,
                             self.last_spike_el if self.last_spike_el is not None else -np.inf)
        since_event = el - last_event_el

        adaptive_tau = min(DECAY_ADAPTIVE_TAU_MAX, max(DECAY_ADAPTIVE_TAU_MIN, since_event))
        r_adaptive = estimate_decay(seg_clean, adaptive_tau) if len(seg_clean) else None
        r_adaptive_bpm = r_adaptive["bpm"] if r_adaptive else None

        win_sec = max(self.single_win_min, min(self.single_win_max,
                      self.single_win_min + max(0.0, since_event - self.frozen_wait_s)))
        win_nsamp = round(win_sec * 500)

        raw_ar = remove_transients(raw_for_detect, self.artifact_detect_mult, self.artifact_extend_mult,
                                    self.artifact_pad_samples, self.artifact_baseline_pctl,
                                    self.artifact_passes) if len(raw) else np.zeros(0)
        raw_clean_ar = declick_mult(raw_ar, self.transient_limit_mult) if len(raw_ar) else raw_ar
        seg_clean_ar = decimate(raw_clean_ar) if len(raw_clean_ar) else np.array([])
        win_r = (estimate_sumenv_robust(seg_clean_ar[-win_nsamp:], self.robust_trim_frac, self.feat_clip)
                 if len(seg_clean_ar) >= win_nsamp else None)
        win_teal = estimate_detrend_from_mw(win_r, self.teal_push_half_width) if win_r else None

        is_flatline = not self.sig_was_onset_ok

        is_new_event = last_event_el != self.prev_last_event_el
        if is_new_event:
            self.prev_last_event_el = last_event_el
            self.frozen_wait_s = self.cons_wait_s + (
                self.cons_wait_extra_s_per_bpm * abs(r_adaptive_bpm - 60) if r_adaptive_bpm is not None else 0.0)

        # independent pitch-family TILT trace -> agreement/corroboration signal
        tilt_bpm = None
        kumar_pitch_bp = (kumar_pitch_bandpass(raw, self.fs, self.pitch_bp_lo_hz, self.pitch_bp_hi_hz)
                           if len(raw) else np.zeros(0))
        if len(kumar_pitch_bp):
            trace = continuous_pitch_trace(kumar_pitch_bp, self.fs, self.pitch_frame_ms,
                                            self.pitch_hop_frac, self.pitch_gate_frac,
                                            self.pitch_fft_lo_hz, self.pitch_fft_hi_hz, self.tilt_split_hz)
            trace_rate = self.fs / trace["hop"] if trace["hop"] else 0
            if trace_rate:
                lo, hi = pitch_autocorr_lag_range(trace_rate)
                tilt_ac = trace_autocorr(trace["tilts"], lo, hi)
                if len(tilt_ac):
                    tilt_bpm = 60 * trace_rate / (lo + peak_idx(tilt_ac))
        agreement_bpm = tilt_bpm

        suppress_circle = is_flatline or is_new_event or since_event < self.frozen_wait_s
        self.tilt_log.append(None if suppress_circle else agreement_bpm)
        if len(self.tilt_log) > self.log_cap:
            self.tilt_log.pop(0)

        raw_candidate_bpm = None if suppress_circle else (win_teal["bpm"] if win_teal else None)

        # HARMONIC-CONFIRMATION VETO -- applied before either guard below ever sees the candidate.
        harmonic_confirmed = (check_harmonic_confirmed(seg_clean_ar, win_nsamp, raw_candidate_bpm,
                                                        self.feat_clip, self.robust_trim_frac,
                                                        hi_ext_mult=self.harmonic_veto_hi_ext_mult,
                                                        height_frac=self.harmonic_veto_height_frac,
                                                        lo_frac=self.harmonic_veto_lo_frac,
                                                        hi_frac=self.harmonic_veto_hi_frac)
                              if self.use_harmonic_veto else None)
        if harmonic_confirmed is False:
            raw_candidate_bpm = None

        # jump-confirmation guard: suppress a large jump vs. the last ACCEPTED push unless it
        # holds for teal_jump_confirm_ticks consecutive ticks.
        is_jump = False
        if raw_candidate_bpm is not None and self.last_accepted_teal_bpm is not None:
            jump_ratio = raw_candidate_bpm / self.last_accepted_teal_bpm
            is_jump = jump_ratio >= self.teal_jump_ratio or jump_ratio <= 1 / self.teal_jump_ratio
        if suppress_circle:
            self.teal_jump_streak = 0
        elif is_jump:
            self.teal_jump_streak += 1
        else:
            self.teal_jump_streak = 0
        jump_unconfirmed = is_jump and self.teal_jump_streak < self.teal_jump_confirm_ticks

        # bpm-scaled settling delay: tracks the candidate's OWN self-consistency; faster candidates
        # require more consecutive agreement before being trusted.
        same_as_delay_anchor = False
        if raw_candidate_bpm is not None and self.bpm_delay_anchor_bpm is not None:
            anchor_ratio = raw_candidate_bpm / self.bpm_delay_anchor_bpm
            same_as_delay_anchor = anchor_ratio < self.teal_jump_ratio and anchor_ratio > 1 / self.teal_jump_ratio
        if raw_candidate_bpm is None:
            self.bpm_delay_streak = 0
            self.bpm_delay_anchor_bpm = None
        elif same_as_delay_anchor:
            self.bpm_delay_streak += 1
        else:
            self.bpm_delay_streak = 1
            self.bpm_delay_anchor_bpm = raw_candidate_bpm
        bpm_delay_unconfirmed = (raw_candidate_bpm is not None
                                  and self.bpm_delay_streak < self._required_teal_delay_ticks(raw_candidate_bpm))

        pushed_bpm = None if (suppress_circle or jump_unconfirmed or bpm_delay_unconfirmed) else raw_candidate_bpm
        if pushed_bpm is not None:
            self.last_accepted_teal_bpm = pushed_bpm

        pushed_conf = win_teal["conf"] if (pushed_bpm is not None and win_teal) else None
        self.cons_buf_log.append(dict(val=pushed_bpm, conf=pushed_conf))
        if len(self.cons_buf_log) > self.log_cap:
            self.cons_buf_log.pop(0)

        # weighted-median combine over the trailing window of accepted pushes
        trailing_vals, trailing_weights = [], []
        idx = len(self.cons_buf_log) - 1
        count = 0
        while idx >= 0 and count < self.weighted_med_trailing_n:
            entry = self.cons_buf_log[idx]
            if entry["val"] is not None:
                t = self.tilt_log[idx] if idx < len(self.tilt_log) else None
                w = self._teal_size_factor(entry["val"], t)
                trailing_vals.append(entry["val"])
                trailing_weights.append(w)
                count += 1
            idx -= 1
        weighted_med = weighted_median(trailing_vals, trailing_weights) if trailing_vals else None

        bpm = None if is_flatline else weighted_med

        self.el += TICK_S
        return dict(t=el, bpm=bpm, is_flatline=is_flatline,
                    raw_candidate_bpm=raw_candidate_bpm, harmonic_confirmed=harmonic_confirmed)


# ============================================================================================
# CONVENIENCE: batch entry point
# ============================================================================================

def estimate_heart_rate(audio, fs, **estimator_overrides):
    """Runs the full combo-13 pipeline over a complete audio array and returns the BPM time series.

    Args:
        audio: 1-D array-like of raw audio samples (mono; if you have stereo, average the channels
               first). Any numeric dtype -- converted to float64 internally.
        fs:    sample rate of `audio`, in Hz.
        **estimator_overrides: any Combo13HeartRateEstimator constructor kwarg, to explore off the
               validated combo-13 defaults.

    Returns:
        A list of dicts, one per ~250ms tick, each with keys 't', 'bpm', 'is_flatline',
        'raw_candidate_bpm', 'harmonic_confirmed' -- see Combo13HeartRateEstimator.tick()'s own
        docstring. 'bpm' is None for any tick where the pipeline doesn't yet trust a reading
        (flatline, still settling after an event, or vetoed/unconfirmed candidate).
    """
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    est = Combo13HeartRateEstimator(fs=fs, **estimator_overrides)
    chunk = int(round(TICK_S * fs))
    results = []
    for i in range(0, len(audio), chunk):
        est.push(audio[i:i + chunk])
        results.append(est.tick())
    return results


if __name__ == "__main__":
    import sys
    from scipy.io import wavfile

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    fs, data = wavfile.read(sys.argv[1])
    data = data.mean(axis=1) if data.ndim > 1 else data
    data = data.astype(np.float64)

    for r in estimate_heart_rate(data, fs):
        bpm_s = f"{r['bpm']:.1f}" if r["bpm"] is not None else "--"
        print(f"t={r['t']:6.2f}s  bpm={bpm_s:>6}  "
              f"flatline={r['is_flatline']}  harmonic_confirmed={r['harmonic_confirmed']}")
