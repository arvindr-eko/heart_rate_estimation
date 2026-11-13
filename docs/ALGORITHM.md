# PCG Heart-Rate Detector — How It Works

**Goal:** estimate heart rate (BPM) from a phonocardiogram (heart-sound) audio stream, robustly enough for an embedded device, and *know when not to trust the answer*.

The core idea: heart rate is a **rhythm/periodicity** problem (how often the "lub-dub" repeats), not a thing to regress directly. The whole pipeline turns the audio into a clean "beat-energy over time" signal, measures its repetition period several independent ways, and reports the rate they agree on — plus a confidence that lets it decline when the signal is ambiguous.

---

## 1. Signal pipeline (per analysis window)

```
audio → band-pass filter → envelope(s) → period estimators → consensus → confidence gate → BPM or "no reading"
```

### a. Band-pass filter (20–150 Hz, Butterworth order 4)
Isolates the frequency band where heart-sound energy lives. Below 20 Hz is body/baseline rumble and motion; above 150 Hz is mostly noise (we measured that on this device almost all energy sits below ~80 Hz). This step removes everything that isn't a heartbeat.

### b. Envelope extraction
A heartbeat is a series of short "thumps" (S1, then S2). The **envelope** is the outline of that energy over time — it converts the oscillating sound into a series of bumps we can find a rhythm in. We use the magnitude of the Hilbert transform, then smooth it.

**Smoothing is the single most important knob**, and it has two *competing* jobs:
- Smooth *enough* to merge S1 and S2 (the "lub" and "dub") into one bump per beat — otherwise the period-finder locks onto the S1→S2 gap and reports **double** the true rate.
- But *not so much* that it blurs neighbouring beats together — otherwise at high heart rates it reports **half** (or a third) of the true rate.

Because no single smoothing serves both, we compute the envelope at **several smoothing widths** and let the consensus (step d) sort it out.

### c. Period estimators (three independent methods)
Each runs on the envelope and returns a candidate BPM (or abstains):
- **Autocorrelation** — slides the envelope against itself; the lag of the strongest match is the beat period. (Primary method.)
- **Harmonic Product Spectrum (HPS)** — finds the dominant rhythm frequency in the envelope's spectrum.
- **Peak-interval** — detects beat peaks directly and takes the median spacing.

### d. Consensus (the reported rate)
We pool **12 estimates** — the three methods across the baseline envelopes, plus autocorrelation on five "sharp" envelopes of differing smoothing. The estimates are clustered (values within ~6% are grouped); the **largest cluster's mean is the reported BPM**. Different methods/scales fail in different ways, so the value they converge on is the trustworthy one. Including the sharp-envelope votes is what fixes high heart rates (they stay correct where the others subharmonic).

### e. Confidence gate
**Confidence = the fraction of the 12 estimates that landed in the winning cluster** (the agreement ratio). If agreement ≥ a threshold, report the rate; otherwise output **"no reading."** Rationale: a clean periodic signal drives most estimators to the same answer; a noisy/ambiguous one splits them.

---

### Worked example — one recording through the pipeline

The figure below traces a real **~69 BPM** recording through each stage. The **raw audio** (top) is a noisy oscillation; the **band-pass** isolates the S1/S2 bursts; the **6 ms "sharp" envelope** resolves the two heart sounds (S1 and S2 marked) within each beat; and the **150 ms envelope** deliberately merges S1+S2 into a single bump per beat — the smoothing trade-off described above, shown directly.

![Pipeline stages: raw -> band-pass -> envelopes for one ~69 BPM recording.](fig_pipeline.png)

Feeding the 30 ms envelope into the autocorrelation, the strongest periodic match lands at **69 BPM, matching the ECG ground truth.** Note the smaller peak near 35 BPM — that is the half-rate subharmonic the consensus and confidence gate exist to avoid.

![Autocorrelation of the envelope; the dominant peak is the heart rate.](fig_autocorr.png)

#### Octave-error case — the failure mode

Here a real **~73 BPM** recording has S1 and S2 of *similar loudness*, so the envelope looks like a near-uniform train of pulses at the **S1→S2 spacing**. In the autocorrelation the true 73 BPM produces only a weak peak, while the dominant peak sits near **160 BPM** (roughly double) — so the detector reports **161 BPM**. This is the irreducible octave ambiguity: the wrong rate isn't noise, it's a genuinely *stronger* periodicity in the signal, which is why no self-consistency gate can catch it.

![Octave-error pipeline stages (truth 73, detector 161).](fig_pipeline_octave.png)

![Octave-error autocorrelation: the true-rate peak is weak; the S1->S2 peak dominates.](fig_autocorr_octave.png)

#### High heart rate (~176 BPM)

At high rates the beats crowd together. Note how the **150 ms envelope smears adjacent beats into one another** (why over-smoothing fails at high HR), while the sharp envelope keeps them separate. The autocorrelation's dominant peak lands cleanly on **176 BPM (detector correct)** — and because twice that (352) exceeds the 220 BPM physiological cap, the double-rate trap simply *cannot* occur here. That is exactly why the high-rate band is reliable.

![High-HR pipeline stages (truth 176, detector 176).](fig_pipeline_highhr.png)

![High-HR autocorrelation: clean dominant peak at 176 BPM.](fig_autocorr_highhr.png)

---

## 2. Streaming layer (real-time)

The detector runs on a **sliding window** of recent audio, recomputed each **hop**. Two extra rules govern *when* it speaks:
- **Warm-up floor** — don't report until enough audio has accumulated (too few beats → confidently wrong).
- **Stability gate** — only report once the last few window-estimates agree (no octave jumps), so transient warm-up values are suppressed.

---

## 3. Parameters — values, how set, and why

| Parameter | Value | How chosen | Why |
|---|---|---|---|
| Band-pass low cut | **20 Hz** | domain + measured spectra | below = baseline/motion rumble |
| Band-pass high cut | **150 Hz** | domain + measured spectra (energy <80 Hz) | above = noise, ~no cardiac energy |
| Filter order | **4** | standard | sharp roll-off, numerically stable |
| HR search range | **30–220 BPM** | physiological limits | bounds the autocorr lag search **and** auto-rejects out-of-range octave artifacts — this is *why* high-rate doubling self-resolves (2× a fast rate exceeds 220) |
| Baseline envelope smoothing | **30 / 150 ms + Shannon** | inherited, then analyzed | 30 ms keeps S1/S2 separate, 150 ms merges them, Shannon-energy emphasizes thumps |
| Multiscale smoothing set | **8 / 16 / 32 / 64 / 110 ms** | spans the regime | light widths are correct at high HR, heavy at low HR; consensus picks the agreed rate |
| Cluster tolerance | **±6 % (min ±5 BPM)** | chosen | groups within-octave estimates; relative tolerance scales with HR |
| Confidence threshold τ | **0.50** | **swept** (coverage/accuracy curve) | knee point: declines only ~2 % of clips, 92 % accurate on reported, 83 % of declines are real errors |
| Sliding window W | **8 s** | **swept** (streaming sim) | enough beats for a stable autocorr; matches the accuracy knee |
| Hop | **1 s** | chosen | update cadence; compute is ~0.1 s so this is comfortable |
| Warm-up floor | **8 s** | **swept** (latency↔accuracy) | knee: first value ~89 % accurate; 5–6 s trades down to ~84–87 % |
| Stability window K | **3 hops (~3 s)** | chosen | enough to confirm the estimate has settled |
| Stability spread | **≤10 BPM** | chosen | catches octave jumps (±50 %+) while tolerating natural beat-to-beat variation |

**Measured/swept (data-driven):** the HR range's role, the smoothing scales, τ, W, and the warm-up floor — these were chosen from accuracy curves on a 269-recording labelled corpus.
**Domain-standard:** band-pass cuts, filter order.
**Reasonable heuristics (not exhaustively tuned):** cluster tolerance, stability K and spread.

---

## 4. Performance (on the 269-clip, signal-quality≥0.7 corpus)

- **Offline accuracy:** 91 % within ±5 BPM, MAE 6.6 — beats the shipping product (CORAL HR 82 %, efast-PCG 85 %).
- **Confidence gate:** at τ=0.5, declines ~2 % for 92 % accuracy on what it reports; tighten τ to trade coverage for accuracy.
- **Streaming:** first trustworthy reading at ~8 s (89 %), then continuous updates at ~90 %; reports ~100 % of the time on clean signal.

## 5. Known limits

- **Octave floor (~8–10 %):** some clean recordings genuinely look like their own double or half (equal-loudness S1≈S2 with equal spacing). The information isn't in the signal — confirmed via spectral, morphology, and nine estimator experiments — so no estimator or self-consistency gate fixes these without external info (ECG, age, second recording). The confidence gate cannot catch them because the whole pool agrees on the wrong octave.
- **Corpus is good-quality (SQ≥0.7):** real-world steady-state coverage will be lower and driven by signal quality (the gate correctly goes silent during motion/poor contact). Needs validation on un-thresholded data.
- **Timing measured on PC** (~0.1 s/update); on-device profiling still required.
