# Fixing Octave Errors with Feature-Shaped Autocorrelation
**Date:** 2026-06-14
**Author:** Daniel (Eko Health) with Claude
**Subject:** A new HR-from-PCG method that eliminates rate doubling by autocorrelating a *feature-shaped* representation instead of a plain amplitude envelope.

---

## 1. The problem we set out to fix

The feature-based HR detector was strong (~91% within ±5 bpm on the SQ≥0.7 corpus) but its residual failures were almost all **octave errors** — reporting **2× the true rate**. The mechanism: a heartbeat is two sounds, "lub" (S1) and "dub" (S2). Detect the rate by autocorrelating the signal's amplitude envelope, and the doubling falls out of the math, as shown below.

**Worked example (the file that triggered this investigation):** `unfiltered_iOS-v5_3_1__20230926-141043_lGt5H-bP.wav` (identifier Rph0096), ECG ground truth **73 bpm**. The old detector reported **161 bpm** (2.2×). The new method reports **72 bpm**.

![Raw PCG waveform, first 4 seconds. You can see the lub-dub pairs: a sound, a shorter gap, a second sound, then a longer gap to the next pair.](fig_ex_raw.png)

---

## 2. The old envelope, and why it doubles

**How the old envelope is built:**
1. Band-pass filter the audio to 20–150 Hz (the heart-sound band).
2. Take the **Hilbert analytic envelope** — `|hilbert(x)|` — which traces the instantaneous amplitude of the signal.
3. Smooth it with a ~30 ms moving average to get one clean **broadband amplitude envelope**.

This produces a single curve with one bump per sound. The trouble is visible immediately:

![Old envelope (red) over the rectified raw signal (grey). S1 and S2 produce near-identical bumps — the envelope has discarded everything that distinguishes a "lub" from a "dub."](fig_ex_envelope.png)

**Why that causes doubling:** autocorrelation finds the lag at which the signal best matches a shifted copy of itself. Because S1 and S2 look alike, shifting by the **half-cycle** (sliding S1 onto S2) produces a strong match — so the autocorrelation has a tall peak at the S1→S2 spacing. The detector picks that peak and reports double the true rate. The envelope made the true cycle and the half-cycle look equally good, and the half-cycle won.

---

## 3. The new representation: shape the envelope *before* autocorrelating

The fix (Daniel's hypothesis): don't collapse the beat into one amplitude curve. Build a representation in which **S1 and S2 are different**, so the half-cycle shift no longer aligns.

**How the new representation is built** — a *stack* of envelopes, not one:
- **Four frequency sub-band envelopes** (15–30, 30–50, 50–80, 80–130 Hz): band-pass into each sub-band, then Hilbert-envelope + 15 ms smooth. This captures the **spectral shape** of each sound — S1 and S2 distribute their energy across these bands differently.
- **An onset-strength (attack) envelope**: the positive part of the time-derivative of a *sharp* (5 ms) broadband envelope. This emphasizes **how abruptly each sound starts** — S1 has a sharper attack than S2.
- **A broadband amplitude envelope** (down-weighted), kept for robustness.

Each feature is z-scored (zero mean, unit variance) so they contribute comparably.

![A few of the stacked feature envelopes for the same 4 s. Unlike the single old envelope, S1 and S2 differ across the sub-bands and especially in the onset (attack) channel.](fig_ex_features.png)

We then **autocorrelate each feature dimension separately and sum** the results (onset-strength weighted ×2, broadband amplitude ×0.3). The logic: the **true cycle** (S1 → next S1) aligns across *every* dimension at once, while the **half-cycle** (S1 → S2) only aligns on the dimensions where S1 happens to resemble S2. Summing therefore tilts the winner toward the true period and suppresses the half-cycle peak.

---

## 4. The benefit, in one figure

Here is the same file's autocorrelation, computed both ways on the same lag axis:

![Old broadband-envelope autocorrelation (red) has its dominant peak at the half-period (0.37 s ≈ 161 bpm, the S1→S2 spacing) and picks it → doubling. The new feature autocorrelation (teal) suppresses that half-period peak and peaks at the true period (0.83 s ≈ 72 bpm). The black line marks the true 73 bpm period; the dashed grey line marks the half-period.](fig_ex_autocorr.png)

- **Old envelope:** tallest peak at **lag 0.37 s → 161 bpm** (doubled).
- **New representation:** half-period peak collapsed; tallest peak at **lag 0.83 s → 72 bpm** (correct, matches ECG 73).

That is the entire benefit in one picture: the doubling peak is *structurally removed* by giving the autocorrelation a representation in which the two heart sounds are no longer interchangeable.

---

## 5. One more step: a now-safe short-lag bias

With doubling eliminated, the only residual errors flipped to a few **halvings** (the autocorr occasionally favored a peak at 2× the true period). Because the representation now suppresses the *double* peak, we can safely re-introduce a gentle "prefer the shorter lag" bias (divide the autocorrelation by `lag^0.2`) — the very heuristic that used to *cause* doubling is now safe, and it cleans up the halvings without bringing doubling back.

| `lag^p` bias | correct | double | half | other |
|---|---|---|---|---|
| 0.0 | 97% | 0 | 5 | 3 |
| 0.1 | 99% | 0 | 1 | 2 |
| **0.2** | **99.6%** | **0** | **0** | **1** |
| 0.3 | 99% | 0 | 0 | 2 |

Doubles stay at **zero across the entire sweep** — confirming the two failure modes are now decoupled.

---

## 6. Results

On all 269 ECG-labeled files (SQ ≥ 0.7 corpus), same ±5 bpm / ±5% metric throughout:

| Method | within ±5 | octave errors (double+half) |
|---|---|---|
| Integrated detector (previous best) | 91% | 9 |
| Pretrained Springer HSMM | 86% | 22 |
| **Feature autocorrelation + short-lag bias** | **99.6%** | **0** |
| Human ear (tapping, same files) | 99% | — |

The single remaining error in 269 files is a 7% high near-miss at high rate (179→192 bpm), not an octave error. The normal-rate doubling that defeated every prior method (onset-grouping, label-free and pretrained HSMM) is gone.

---

## 7. Caveats and next steps

- **Signal quality:** this is the SQ ≥ 0.7 corpus (clean signals). The essential next validation is **un-thresholded / low-SQ real-world data**, where performance will be lower.
- **Parameter robustness:** the `lag^0.2` bias was chosen on this corpus, but the result is stable across 0.15–0.30 (all 99–100%, zero octave), and the no-tuning equal-weight variant is also 97% — so it is not a fragile fit. A **patient-level split** should still confirm generalization.
- **High rate:** the lone miss and the slightly weaker 120–160 band suggest the sub-band filters / 250 Hz feature rate need attention above ~160 bpm.
- **On-device:** the method is more compute than a single envelope (six sub-band Hilbert envelopes + onset + per-dimension autocorrelations) but remains O(N log N) FFTs — **on-target (CMSIS-DSP) profiling is required** before claiming embeddability.
- **Integration:** fold this in as the periodicity engine (replacing or augmenting the integrated detector's autocorrelation component), keeping the confidence gate for genuinely poor signals.
