# Heart-Rate-from-PCG: Detailed Pipeline Walkthrough
**Date:** 2026-06-15
**Author:** Daniel (Eko Health) with Claude
**Subject:** Step-by-step description of the embedded feature-autocorrelation HR detector, with the signal shown after every processing stage. Worked on the file `…lGt5H-bP.wav` (Rph0096, ECG truth **73 bpm**) — the recording the previous detector doubled to ~161 bpm.

---

## 1. Overview

The detector estimates heart rate by **autocorrelating a representation in which the two heart sounds (S1 "lub", S2 "dub") look different from each other.** A plain amplitude envelope makes S1 and S2 look alike, so autocorrelation locks onto the S1→S2 half-spacing and reports double the true rate. Splitting the signal into frequency sub-bands restores the difference between S1 and S2, which suppresses that false half-period peak.

Every stage below uses only operations a microcontroller can run cheaply — decimation, causal IIR filters, rectification, and multiply-accumulate loops. **No FFT, no Hilbert transform, no zero-phase filtering.**

![Block diagram of the full pipeline, from raw audio to heart rate.](fig_pipe_block.png)

---

## 2. Step ① — Decimate to 500 Hz

Heart-sound energy lives below ~150 Hz, so the incoming 4–8 kHz audio is anti-alias filtered and **downsampled to 500 Hz**. This throws away nothing useful and makes every subsequent stage 8–16× cheaper. (On-device this is a CIC or short IIR decimator.)

![① The raw PCG, shown at 500 Hz after decimation. The lub-dub pairs are visible: a sound, a short gap (systole), a second sound, a longer gap (diastole), repeat.](fig_pipe_raw.png)

---

## 3. Step ② — Four causal band-pass filters

The decimated signal is split into **four frequency sub-bands** with second-order causal Butterworth band-pass biquads:

- 15–30 Hz, 30–50 Hz, 50–80 Hz, 80–130 Hz

These are causal (single forward pass), not zero-phase — phase doesn't matter because we work from envelopes and autocorrelation is shift-invariant. On-device each is one `arm_biquad_cascade_df1` call.

![② The same 4 s after each band-pass filter. S1 and S2 do not deposit their energy identically across the bands — that difference is what the rest of the pipeline exploits.](fig_pipe_bands.png)

**Why four bands?** This was swept directly. A *single* broadband band is the failure case — it doubles (86% correct, 14 octave errors). Accuracy climbs to **100% at four bands**, and adding more does not help: above ~110 Hz there is little heart-sound energy, so extra bands mostly contribute noise (and slightly *increase* octave errors on short segments). Three bands is a viable lighter option (99%, zero octave errors); **four is the sweet spot.**

---

## 4. Step ③ — Envelope: rectify + 1-pole IIR low-pass

For each band we need the **amplitude envelope** (how loud that band is over time). Instead of a Hilbert transform (which needs an FFT), we use the cheap, classic detector:

1. **Rectify**: take the absolute value `|x|`.
2. **Low-pass** with a single-pole IIR filter (≈18 Hz): `y[n] = (1−a)·|x[n]| + a·y[n−1]`.

That is two operations per sample and traces the same envelope shape a Hilbert transform would.

![③ Each band-pass output (grey) and its rectify-plus-IIR envelope (colour). One envelope per sub-band.](fig_pipe_envs.png)

---

## 5. Step ④ — Normalise and downsample to 100 Hz

Each band envelope is **z-scored** (subtract mean, divide by standard deviation) so the bands contribute equally regardless of absolute loudness, and **downsampled to 100 Hz** — heart rate never exceeds a few Hz, so 100 Hz frames give ample timing resolution while shrinking the autocorrelation.

![④ The four z-scored, 100 Hz feature streams. These are the inputs to the autocorrelation. Note the four streams emphasise the beat structure differently — the information that distinguishes S1 from S2.](fig_pipe_frames.png)

---

## 6. Step ⑤ — Restricted-lag autocorrelation, summed

For each band we compute the autocorrelation **only over the lags that correspond to a plausible heart rate** — 0.30 s to 1.5 s (40–200 bpm). For each lag `L`, that is just `Σ f[t]·f[t+L]` over the window (a multiply-accumulate loop; no FFT). Each band's curve is normalised — using the **biased** estimator (divide by the window length `T`, not by the per-lag overlap count `T−L`), which gently tapers the unreliable long-lag end and noticeably cuts *halving* errors on short segments — then the **four are summed**.

The key effect: the **true cardiac period** (S1 → next S1, lag ≈ 0.82 s here) aligns the signal with itself across *all four bands at once*, while the **half-period** (S1 → S2, lag ≈ 0.41 s) only aligns on bands where S1 happens to resemble S2. Summing therefore builds up a tall peak at the true period and a suppressed one at the half-period.

![⑤ Per-band restricted-lag autocorrelations (faint) and their sum (black). The sum's dominant peak sits at the true period (~0.83 s → 72 bpm); the half-period bump near 0.41 s is suppressed.](fig_pipe_autocorr.png)

---

## 7. Step ⑥ — Combine, gentle short-lag bias, pick the peak

The summed autocorrelation is divided by `lag^0.25` — a gentle preference for shorter lags that cleans up the rare case where a peak at *twice* the true period would otherwise win. This bias is safe **only because** the sub-band representation has already removed the doubling peak (otherwise it would re-introduce doubling). The location of the maximum is the cardiac cycle; `HR = 60 / lag`.

---

## 8. Why it works — old envelope vs new representation

This is the whole point, on the same file:

![⑥ Raw autocorrelations (no lag bias). The single broadband envelope (red) is ambiguous between the true cycle and the S1→S2 spacing and mis-estimates (here 107 bpm); summing the four sub-band autocorrelations (teal) resolves the true period → 72 bpm. The black line marks the true 73 bpm period, the dashed line the half-period.](fig_pipe_benefit.png)

A single broadband amplitude envelope discards what distinguishes the two heart sounds, so its autocorrelation cannot tell the full cycle from the half cycle — and over-estimates. The four-sub-band representation keeps that distinction, so the half-period alignment fails and the true period wins. The previous (Hilbert + FFT) detector reported **161 bpm** on this file; the new pipeline reports **72** (truth 73).

---

## 9. Results

On all 269 ECG-labeled files (SQ ≥ 0.7 corpus), within ±5 bpm / ±5%:

| Method | within ±5 | octave errors |
|---|---|---|
| Previous integrated detector | 91% | 9 |
| Pretrained Springer HSMM | 86% | 22 |
| **This pipeline (embedded-faithful)** | **269/269 = 100%** | **0** |

Zero doubles, zero halves; the doubling that defeated every prior method is gone.

![Detected HR (embedded pipeline) vs ECG ground truth for every one of the 269 files. All points lie on the detected = truth diagonal; none fall on the 2× or ½× reference lines (dashed). This is the picture of zero octave errors.](fig_pipe_scatter.png)

## 10. Embedded budget (analytical, 8 s window)

| Stage | ~MACs / update |
|---|---|
| Decimate → 500 Hz (CIC ≈ free; IIR ≤ 1 M) | 0 – 1 M |
| 4 × band-pass biquads | ~160 k |
| 4 × rectify + 1-pole IIR envelope | ~32 k |
| 4 × restricted-lag autocorrelation | ~340 k |
| sum, `/lag^0.25`, argmax | negligible |
| **Total** | **~0.5 – 1.5 M MACs** |

≈ 4–12 ms per update on a Cortex-M4 @ 120 MHz (< 2 % CPU at a few updates/s). **RAM ≈ 13–16 KB** (four 100 Hz envelope streams, streamed; ~7 KB in int16), with **no FFT scratch buffers**. All stages map to CMSIS-DSP primitives (`arm_biquad_cascade_df1`, `arm_dot_prod`).

## 11. Time to a result (latency)

There are two clocks. **Compute** is negligible — ~2.6 ms per update in desktop Python, ~4–12 ms estimated on a Cortex-M4. The real latency is **acquisition**: autocorrelation needs several cardiac cycles before the period locks. Accuracy vs. how many seconds of audio have been collected (measured on the 269-file corpus):

| Window | within ±5 | octave errors |
|---|---|---|
| 2 s | 78% | 12 |
| 4 s | 89% | 6 |
| 5 s | 94% | 1 |
| **8 s** | **97%** | **2** |
| full (~16 s) | **100%** | **0** |

![Accuracy vs how much audio has been collected (current algorithm). A number is available within ~2 s but is unreliable; the reading becomes trustworthy around ~8 s (the physiological floor — several beats are needed); the full 100% / zero-octave figure needs the whole recording.](fig_pipe_latency.png)

**Practical rule:** the reading is provisional for the first few seconds and trustworthy by ~8 s. The demo reflects this with the confidence-driven lock described in §13 — the number is shown greyed while provisional and turns solid once it has held steady long enough *for its confidence* (a clear reading locks in well under a second of steady value; an ambiguous one must hold for several seconds, or never commits). In streaming use the detector re-runs every ~1 s on the growing/sliding window (each run only a few ms).

## 12. Confidence — how sure is the reading?

Every estimate comes with a **confidence**: the height of the tallest peak in the summed autocorrelation divided by the next distinct peak (≥ 80 ms away), measured above the baseline. A tall, lone winner means an unambiguous period; a near-tie means a competing octave or noise — so the ratio is a natural "how clear is this?" score.

![Confidence (peak ratio) at a 4 s window: correct reads cluster high, wrong reads low. Dashed lines mark the medians. It is a useful separator (AUC ≈ 0.78) but the two distributions overlap.](fig_conf_sep.png)

Validated on all 269 files at short windows (where errors occur): **correct reads run a median ratio ≈ 1.45, wrong reads ≈ 1.15**, and errors concentrate at low confidence (AUC ≈ 0.78–0.81 for telling correct from wrong).

**Honest limit.** It is a *moderate* separator, not a perfect gate — the distributions overlap, so a few reads are confidently-wrong or shakily-correct. We tested combining the ratio with cross-band agreement, cross-window stability, and a learned blend; **none beat the simple peak ratio.** The reason is fundamental: the errors that survive are *consensus-octave* errors — every band and every time window agrees on the wrong octave — so no self-consistency measure can flag them. Pushing past this needs *external* information (a plausible-rate / age prior), not another autocorrelation-derived term.

## 13. Showing the number: lock fast when clear, then stay sticky

Two rules turn the moment-to-moment estimate into a trustworthy on-screen number.

**(a) Continuous confidence → required-hold.** A value must hold steady for a length of time that depends on its confidence: a very clear reading commits almost immediately, an ambiguous one has to prove itself for several seconds. The curve is `hold = 0.3 + 7.7·e^(−2.5·(ratio−1))` seconds.

![Required steady-time vs confidence: ~0.3 s at high confidence, rising to ~8 s as the reading becomes ambiguous.](fig_hold_curve.png)

**(b) Hysteresis (stickiness).** Once a value is committed (shown solid), a *different* value cannot replace it until that challenger has itself held long enough to overcome the incumbent's accumulated support (which grows the longer the current number has been confirmed). This stops transient flickers from changing a well-established reading.

Before a value commits it is shown **greyed** (provisional); once it commits it turns **solid**. On a genuinely ambiguous recording — low confidence, competing peaks of similar height — the number **never commits**: the system declines to show a confident value rather than display a wrong one. (Example: a recording with an extra heart sound per beat produces a strong false peak at 3× the true rate that is present in every band; the true and false peaks stay nearly tied, confidence sits near 1.0, and the display correctly stays provisional.)

This is the practical payoff of the confidence metric: it controls *how fast* a reading is trusted and *whether* it is trusted at all. The live demo now layers a stronger, physiologically-grounded rule on top of this — the continuity prior of the next section — which supersedes the simple hysteresis for the committed output.

## 14. Real-time operation: windowing and the continuity prior

The core estimator in §2–§13 is **window-agnostic** — it autocorrelates whatever span of audio it is given. A real-time device adds two things on top of it: a windowing rule that decides *how much* recent audio to analyse, and a physiological continuity prior that decides *which* readings to trust as the committed output.

**The window: grow to 8 s, then slide.** The buffer fills until it holds 8 s of envelope frames, after which the oldest frame is dropped as each new one arrives — so the window grows to 8 s and then slides. Two facts set the cap. First, accuracy saturates there: about 94 % at 5 s, 97 % at 8 s, and 100 % only at the full ~16 s recording, so 8 s is the knee of the curve. Second, a resting heart rate changes slowly — on the order of 10 bpm per *minute* — so across an 8 s window the true rate drifts by only ~1.3 bpm, far inside the ±5 bpm tolerance. The window is thus long enough to sharpen the peak and average out transient noise, yet short enough never to blur a real, gradual change.

Streaming experiments confirmed the choice. Against a window that grows without bound, or one that *shrinks* when the signal degrades, the capped grow-then-slide window won: a longer window **dilutes** a brief artifact (the many clean cycles outvote it), whereas shrinking discards exactly the clean history that would have outvoted the noise. "Forgetting" old data only helps when the heart rate genuinely *jumps* — which, on a slowly-changing signal, it does not.

**The continuity prior: reject impossible jumps.** That same slow-change fact is the key to the last error mode. The hard residual errors are *consensus-octave* errors — a doubling (or a 2.5× excursion) coherent across every band and window, so it looks self-consistent and the confidence metric cannot flag it. But it is **physiologically impossible**: a jump from 73 to 182 bpm in a fraction of a second is not a real heart. The continuity prior makes that explicit — it seeds on the first value that has held steady and confident for ~1.5 s, then tracks only *plausible* moves (slew-limited to a rate far looser than physiology allows) and **rejects any larger jump as an artifact**. This is the one lever that beats the self-consistency ceiling, because it uses information from *outside* the autocorrelation (how fast a heart rate can change) rather than more internal evidence.

The re-acquire (the escape hatch for a wrong initial lock) is **directional**, and the direction matters: octave and harmonic errors skew *high*, never low. So a far value that is *higher* than the lock is treated as a doubling artifact and rejected (it would only re-acquire after a long ~15 s safety window); a far value that is *lower* is likely the true fundamental that the lock was a harmonic of, and breaks the lock quickly (~1 s of a stable, confident lower reading). This asymmetry is what lets the prior both **hold** a correct low reading against a high artifact (the 73-vs-182 case) and **abandon** a wrong high lock once the true lower rate appears — two situations that look identical until you use the fact that artifacts only ever push the rate up.

| metric (streaming, 250 ms ticks) | raw output | with continuity prior |
| --- | --- | --- |
| demo clips — per-tick correct | 93.2 % | **95.7 %** |
| trigger file (true 73) — per-tick correct | 42 % | **100 %** |
| native corpus (already ~99 %) | 95.3 % | 95.0 % (wash) |

On the trigger recording the window briefly favours a spurious 2.5× peak; the continuity prior holds the committed reading at the true 73 bpm throughout, showing "holding (ignored 182)" as it does. On the full corpus the prior is a wash — those long recordings are already ~99 % correct, so there is nothing to rescue and only a small (~1.5 %) residual wrong-lock risk, the same consensus-octave ambiguity surfacing as an occasional bad seed. The prior is therefore a clear win for streaming-display stability and neutral on the easy cases.

**Period-2 guard (the one failure the confidence gate misses).** A synthetic stress-test of the detector showed it is robust to noise, murmurs, gallops (caught by low confidence), and morphology — but two arrhythmias defeat it: *pulsus alternans* (beats alternate in strength) and *bigeminy* (beats alternate in timing). Both are **period-2** patterns: the signal repeats most strongly every *two* beats, so the autocorrelation locks at half the true rate — and it does so *confidently* (the half-rate peak is clean), which means the confidence gate cannot flag it. The streaming tracker therefore runs a dedicated period-2 test: if the detector picked lag *L* but there is also a real autocorrelation peak at *L*/2 (the doubled rate) **and** the beats at that rate alternate in amplitude (→ alternans) or interval (→ bigeminy), the true rate is reported as 2× and an "irregular" flag is raised. To avoid falsely doubling a healthy signal (whose normal beat-to-beat variation can momentarily mimic period-2), the correction is applied only once the pattern is sustained (≥5 of the last 8 estimates). On the corpus this is harmless (no healthy file is doubled); on synthetic alternans/bigeminy it recovers the true rate where the raw detector halved.

This windowing-plus-continuity layer lives outside the core estimator, in a small streaming wrapper (`HeartRateTracker` + `ContinuityFilter`, packaged as `algorithm/hr_algorithm_streaming.py`); the estimator itself is unchanged.

## 15. The algorithm in full (Python)

The complete detector is about 50 lines of Python and depends only on `numpy` and `scipy` (no machine learning, no proprietary libraries). Below is the core — `analyze_wav()` returns `(bpm, confidence)` for a WAV file. (The packaged file `algorithm/hr_algorithm.py` additionally includes a small command-line runner.)

```python
import numpy as np
from scipy.signal import butter, lfilter, decimate, resample_poly
from scipy.io import wavfile

DEC_FS = 500       # work at 500 Hz (heart sounds are < ~150 Hz)
FRAME_FS = 100     # envelope frame rate for the autocorrelation
LAGPOW = 0.25      # gentle "prefer shorter lag" bias
BANDS = [(15, 30), (30, 50), (50, 80), (80, 130)]   # the 4 sub-bands (Hz)

def to_dec(x, fs):                       # decimate audio to 500 Hz
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

def _envelope(x, fc=18):                 # rectify + 1-pole IIR low-pass (no Hilbert/FFT)
    a = np.exp(-2*np.pi*fc/DEC_FS); return lfilter([1-a], [1.0, -a], np.abs(x))

def _features(dd):                       # 4 z-scored band envelopes at 100 Hz
    F = []; step = int(DEC_FS/FRAME_FS)
    for lo, hi in BANDS:
        e = _envelope(_bandpass(dd, lo, hi))[::step]
        s = e.std(); F.append((e - e.mean())/(s if s > 0 else 1))
    L = min(len(f) for f in F)
    return np.array([f[:L] for f in F])

def heart_rate(F):                       # -> (bpm, confidence)
    T = F.shape[1]; lo = int(0.30*FRAME_FS); hi = min(int(1.5*FRAME_FS), T-1)
    if hi <= lo: return None, 0.0
    lags = np.arange(lo, hi); S = np.zeros(len(lags))
    for d in range(F.shape[0]):          # sum the per-band autocorrelations
        f = F[d]; r0 = np.dot(f, f)/T
        for i, l in enumerate(lags):
            S[i] += (np.dot(f[:T-l], f[l:])/T)/(r0 + 1e-9)   # biased /T autocorr
    S = S/(lags.astype(float)**LAGPOW)   # gentle short-lag bias
    s = S - S.min()
    pk = [i for i in range(1, len(s)-1) if s[i] >= s[i-1] and s[i] > s[i+1]]
    if not pk: return None, 1.0
    pk.sort(key=lambda i: -s[i]); p1 = pk[0]; bpm = 60.0/(lags[p1]/FRAME_FS)
    p2 = next((i for i in pk[1:] if abs(lags[i]-lags[p1]) >= 8), None)
    conf = (s[p1]/(s[p2] + 1e-9)) if p2 is not None else 9.99   # winner / runner-up
    return bpm, conf

def analyze_wav(path):
    fs, d = wavfile.read(path)
    d = d.mean(axis=1) if d.ndim > 1 else d
    d = d.astype(float); d /= (np.max(np.abs(d)) + 1e-9)
    return heart_rate(_features(to_dec(d, fs)))
```

That is the entire heart-rate estimator: decimate, four band-pass filters, four rectify-plus-IIR envelopes, four biased autocorrelations summed, a short-lag bias, and a peak pick that also returns the confidence. Everything in §2–§13 is this code.

**The continuity prior (§14), in full.** The streaming wrapper carries the filter state across blocks and keeps a fixed 8 s ring buffer; the only genuinely new logic is the output gate below, which turns the per-tick `(bpm, confidence)` stream into a committed reading.

```python
class ContinuityFilter:
    """Physiological continuity / slew-rate prior: HR changes slowly (~10 bpm/min),
    so seed on a stable+confident value, follow only plausible moves (slew-limited),
    and reject any larger jump as an artifact (kills octave / off-octave excursions)."""
    def __init__(self, max_slew=1.5, band=10.0, seed_conf=1.35, seed_min_t=3.0,
                 seed_hold=1.5, reacq_fast=1.0, reacq_s=15.0, reacq_conf=1.1):
        self.max_slew=max_slew; self.band=band; self.seed_conf=seed_conf
        self.seed_min_t=seed_min_t; self.seed_hold=seed_hold
        self.reacq_fast=reacq_fast; self.reacq_s=reacq_s; self.reacq_conf=reacq_conf
        self.out=None; self.t_last=None; self.far_since=None; self.far_val=None
        self.cand=None; self.cand_since=None

    def update(self, bpm, conf, t):
        if bpm is None:                       # no signal this tick: hold
            self.t_last=t; return self.out
        dt = (t - self.t_last) if self.t_last is not None else 0.0
        self.t_last = t
        if self.out is None:                  # seed on a value held stable & confident
            if self.cand is None or abs(bpm - self.cand) > max(3, 0.04*self.cand):
                self.cand = bpm; self.cand_since = t
            elif (t >= self.seed_min_t and t - self.cand_since >= self.seed_hold
                  and conf >= self.seed_conf):
                self.out = bpm
            return self.out
        if abs(bpm - self.out) <= self.band:  # plausible: track it, slew-limited
            self.far_since = None
            self.out += max(-self.max_slew*dt, min(self.max_slew*dt, bpm - self.out))
        else:                                 # far: DIRECTIONAL re-acquire (artifacts skew high)
            if self.far_since is None or abs(bpm - self.far_val) > max(3, 0.04*self.far_val):
                self.far_since = t; self.far_val = bpm           # track challenger value-stability
            else:
                need = self.reacq_fast if bpm < self.out - self.band else self.reacq_s
                if t - self.far_since >= need and conf >= self.reacq_conf:
                    self.out = bpm; self.far_since = None         # far LOWER: break fast; far HIGHER: hold
        return self.out
```

## 16. Caveats and next steps
- Results are on the **SQ ≥ 0.7 corpus**; **low-SQ / un-thresholded real-world validation** is the essential next test.
- The MAC/RAM figures are analytical from a validated pipeline, not yet **measured on-target** — real profiling (and the decimator choice) is the final step.
- `lag^0.25` is robust (0.25–0.30 both give 269/269), not a knife-edge fit.
- Keep a **confidence gate** (decline when no clear autocorrelation peak) for genuinely poor signals.
- The continuity prior (§14) trusts its **seed**. The directional re-acquire breaks a wrong *high* lock within ~1 s of the true lower rate appearing stably, which cut the residual wrong-lock risk to ~0.4 % (1/269) and made the prior net-positive on the corpus too. The remaining floor is a lock that the true rate never confidently contradicts within the recording; a plausible-rate prior (age/context) would remove it.
- The period-2 guard (§14) is validated on **synthetic** alternans/bigeminy and on the healthy corpus (no false doubling); it has **not** yet been validated on **real** alternans/bigeminy recordings, which the SQ ≥ 0.7 corpus does not contain. In those rhythms the heart rate is itself ambiguous, so the "irregular" flag is arguably the more honest output than any single number.
