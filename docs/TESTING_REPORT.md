# PCG Heart-Rate Perception Testing — Summary Report
**Period:** 2026-06-12 → 2026-06-13
**Author:** Daniel (Eko Health) with Claude
**Scope:** Perceptual and algorithmic testing conducted to understand the octave-error (rate doubling/halving) failure in the feature-based HR-from-PCG detector, using both synthetic ("simulated") heart sounds and the real labeled Coral corpus.

---

## 1. Why we did this

The integrated feature-based detector reaches ~91% within ±5 bpm on the SQ≥0.7 corpus (beating the shipping CORAL HR and efast_pcg references), but its residual failures are almost all **octave errors** — reporting 2× the true rate (locking onto the S1→S2 half-period) or, less often, ½×. A representative failure kicked this off:

| File | `...lGt5H-bP.wav` (identifier Rph0096) |
|---|---|
| ECG ground truth | **72.6 bpm**, Normal Sinus Rhythm |
| Integrated detector | **161.1 bpm** (≈2.2× — doubled) |
| Detector confidence | 0.67 (above gate → reported, not declined) |
| Octave-dispute signal | 0.00 (no internal disagreement to flag it) |
| Signal quality (SQ) | 0.47 (moderate) |

The detector is *confidently wrong*, and no self-consistency gate catches it. Meanwhile the human ear parses the "lub-dub" easily, even through motion artifact. The central question: **what is the ear using that the algorithm isn't — and is that information actually present in the recordings?**

---

## 2. Tools built (all in `C:\Users\daniel\Desktop\pcg_abab\`)

| Tool | Purpose |
|---|---|
| `heart_synth.html` | Interactive Web-Audio heart-sound synthesizer (sliders: rate, S1↔S2 gap, S1/S2 freq/attack/decay/level, noise, jitter) |
| `heart_quiz.html` | Tap experiment v1 (random parameters) — later superseded |
| `heart_quiz_iso.html` | Cue-isolation tap experiment (S1≡S2 identical; timing sweep + cue rescue) |
| `heart_quiz_real.html` | Cue-isolation on 60 real files (timing-kill / morphology-kill) — **invalidated by an event-detection bug** |
| `heart_quiz_baseline.html` | Tap-through of all 269 real files, unmodified (hearability baseline) |
| `generate_and_test.py`, `run_iso.py`, `tune_grouping.py` | Re-synthesize stimuli to WAV and run the detector for human-vs-machine comparison |
| `reverse_events.py` | Time-reversal listening manipulation |
| `analyze_*.py` | Result analysis / ECG-truth joins |

---

## 3. Part A — Simulated heart sounds

Sounds were synthesized as damped sinusoids (power-curve attack + exponential decay), which let us dial each candidate cue independently: rate, the systole/diastole gap split, S1 vs S2 pitch, attack, decay, loudness, plus noise and beat jitter.

### 3.1 Tap experiment v1 (random parameters, 60 trials)
**Result: 100% correct (mean octave error |log₂| = 0.037).**
Daniel tracked the true beat on every trial. The intended cue-vs-clarity correlations came out as **noise**, for a clear reason: with all parameters randomized *independently*, no trial was ever fully degenerate — every clip left at least one usable cue (timing asymmetry, pitch, loudness, or attack), and he used whichever was present.
**Finding: cue redundancy.** No single cue is necessary; this is exactly why a single-mechanism detector (autocorrelation) fails where the human doesn't.

### 3.2 Detector vs human on the same 60 stimuli
**Detector: 57/60 (95%), 3 doublings, MAE 5.0 bpm — vs human 100%.**
The 3 detector failures were all **equal-amplitude, near-symmetric-gap** cases at normal rates (e.g., 101→201, 51→112, 106→212). The human got them via the **pitch** difference — a channel the envelope-based detector is blind to. *Caveat noted at the time: that pitch cue is an artifact of the synthesizer; real Coral PCG has no reliable S1/S2 pitch difference.*

### 3.3 Cue-isolation experiment (S1 ≡ S2 acoustically identical, 30 trials)
This removed the synthetic pitch crutch and structured the trials.

**Block 1 — Timing sweep (only cue = gap asymmetry):** **Timing-grouping confirmed, cleanly.** With pitch/loudness/attack stripped, Daniel still locked the true beat while the systole/diastole gap stayed asymmetric, and only flipped to 2× as the gaps approached equal.

![Timing sweep: with S1 and S2 acoustically identical, the tapped rate tracks the true beat (ratio 1.0) while the systole/diastole gap is asymmetric and jumps to 2× only as the gap approaches symmetric. Error bars span the two reps; the boundary is bistable.](fig_timing_sweep.png)

**Block 2 — Cue rescue at the degenerate base (110 bpm, symmetric, identical):** a near **double dissociation** between human and machine:

| Re-added cue | Human | Detector |
|---|---|---|
| none (control) | 2× | 2× |
| **S2 quieter (−40%)** | **2× (no rescue)** | 1× (rescued) |
| S2 softer attack | 1× | 1× |
| S2 longer ring (decay) | 1× | 1× |
| S2 higher pitch | 1× | 1.5× (broke) |

This overturned a working assumption: **loudness contrast rescues the machine but not the human**, while temporal-envelope morphology (attack/decay) helps both, and pitch helps the human but breaks the detector.

![Cue rescue at the degenerate base. Ratio 1.0 = correct, 2.0 = doubled. Loudness contrast rescues the detector but not the human; attack and ring rescue both; pitch rescues the human but pushes the detector to a garbled 1.5×.](fig_cue_rescue.png)

### 3.4 Tuned grouping algorithm on all 90 synthetic files
We built an onset-based **timing + amplitude grouping** detector (detect events → if even/odd inter-onset intervals *or* peak heights alternate → 2 sounds/cycle → period = every-other-onset spacing; else uniform → period = inter-onset interval) and grid-searched it.

**Best: double+half = 4, vs 8 for the integrated detector** (correct 83/90).

![Octave errors (double + half) on the 90 synthetic files: the tuned timing-grouping detector halves them versus the integrated detector. The residual 4 are information-limited (identical sounds and symmetric gaps).](fig_octave_counts.png)
All 4 residual errors were the **information-limited** cases (identical sounds *and* symmetric gaps — a uniform pulse train where 2× is the only defensible reading). The grouping detector recovered every file that contained *any* usable cue, including the equal-amplitude cases that defeated the integrated detector.
**Takeaway: below 4 is unreachable by any period-picker — it needs a confidence gate (decline) or external info, not more tuning.**

---

## 4. Part B — Real Coral files

### 4.1 Time-reversal listening test (the trigger file)
Each detected event in the real Rph0096 file was reversed in place (±70 ms window, crossfaded). **Daniel still clearly heard the lub-dub.**
*Interpretation (corrected during the work):* time reversal preserves the magnitude spectrum and destroys only the attack/decay envelope shape. So this **ruled out attack/morphology as load-bearing** but did **not** isolate timing (spectrum, amplitude, and timing all survived).

### 4.2 Real-file cue-isolation experiment — INVALIDATED (lesson learned)
We built a 60-file experiment (20 original / 20 timing-killed / 20 morphology-killed). **The manipulated conditions were corrupted** by an event-detection bug: the onset detector over-detected ~2–3× too many events (murmur energy, S1/S2 sub-peaks, noise), so the timing-kill and morphology-kill clips became dense, fast click trains. Daniel tapped 256–412 bpm on them — that's 4–6× the true rate, i.e., a processing artifact, not perceptual doubling.

Two useful by-products:
- **The files are all sinus rhythm** (29 Normal / 19 Tachy / 12 Brady; 0 irregular). The "irregularity" Daniel heard was the broken audio, not the recordings.
- **Original (untouched) condition: 18/20 correct** — confirming the real audio itself is readable; only the manipulation was broken.
- **The bug is the same robust-onset-detection problem at the heart of the whole algorithm effort.** The morphology hypothesis remains *untested* pending a fixed manipulation.

### 4.3 Hearability baseline — all 269 files, unmodified
The decisive run: tap the true rate on every labeled file, or mark "no clear beat."

**Result: 266 / 269 correct = 99%.**

| Breakdown | Correct |
|---|---|
| Signal quality 0.4–0.7 | **100%** (n=68) |
| Signal quality 0.8–1.0 | 97–99% |
| Normal Sinus / Bradycardia / Tachycardia | 99% / 100% / 95% |
| Rate <60 / 60–90 / 120–160 | 100% |
| Rate 90–120 | 94% (1 double, 1 half) |
| Rate 160+ | 97% (1 no-beat) |

![Human tapped HR vs ECG ground truth across all 269 real files. 266 sit on the true-rate diagonal; the only misses are one doubled (104→212) and one halved (112→56), both at mid/high rate, plus one "no clear beat" at 179 bpm (not plotted).](fig_baseline_scatter.png)

![Hearability stays at 94–100% across every heart-rate band and every signal-quality band — including 100% at the lowest SQ (0.4–0.7), where the algorithm struggles most.](fig_baseline_bands.png)

**Key conclusion: the information needed to recover HR is present in essentially every recording.** Since a human extracts it from 266/269 — including low-SQ files — a file the algorithm misses is an **extraction failure, not an information limit.** With the detector at ~91% (SQ≥0.7) and the human at 99% (full set, including worse SQ), there is roughly **8 points of pure algorithmic headroom.** This also settles an earlier debate: it was an overstatement to call the S1/S2 cue "absent" on real PCG — for 99% of files it is demonstrably present and recoverable. (What our scalar features showed was only that *those specific features* — spectral centroid AUC≈0.50, attack/decay AUC 0.46–0.53 — don't capture it, not that it's gone.)

---

## 5. Where human hearing ran into limitations

Daniel's ear is the strongest "detector" we have, but it is not infinite. The boundaries we mapped:

1. **Fully degenerate synthetic case — the hard floor.** When S1 and S2 were acoustically *identical* **and** the gaps were *symmetric* (a uniform pulse train), Daniel doubled on 5 of 6 such trials, with 1 coin-flip (1× on one rep, 2× on the other of the same condition). This is genuinely information-limited: there is no signal feature separating rate R from 2R, and human and machine fail *together* here.

2. **Loudness-only contrast did not rescue him.** A loud-soft-loud-soft train at symmetric timing (S2 at −40%) still produced doubling — the human does not bind beats by amplitude alternation the way the envelope detector does (at least at this contrast level).

3. **Timing-symmetry threshold.** In the timing sweep, Daniel reliably tracked the true beat while the systole/diastole gap stayed asymmetric (sysFrac ≲ 0.34–0.42) and flipped to 2× as it approached symmetric (~0.42–0.50), with a **bistable boundary zone** where the same stimulus could be heard either way on different reps.

4. **Real-file misses (3 of 269), all at rate extremes:**
   - ecg **104 → tapped 212** (clean double, high SQ 0.9, Sinus Tachycardia) — `...Vf39tL3t`
   - ecg **112 → tapped 56** (half) — `...83ucYCxO`
   - ecg **179 → "no clear beat"** (very high rate) — `...jiSSSnTR`

5. **Very high rate (≥~180 bpm) is partly a *tapping* limit, not a perception limit.** Tapping faster than ~3 taps/second is near the motor ceiling, so the high-rate half/no-beat results conflate perception with the response method.

6. **Replay dependence (methodological).** Daniel could loop and replay each clip; a single-pass listen would likely lower the 99%. So the baseline proves the information *exists*, but the 99% figure benefits from repeated listening, motivation, and the forgiving median-of-taps scoring — it is **not** a directly fair bar for a single-pass causal algorithm.

7. **Artifact-driven "limit" (not a true one).** On the corrupted manipulation clips, Daniel tapped 4–6× the true rate — but that reflects broken audio (over-detected click trains), not a perceptual limit. Noted here only to keep the record honest.

**Pattern:** every genuine human miss falls in the **same band as the algorithm's** — mid-rate equal-amplitude doubling and high-rate ambiguity — just ~30× rarer. The failure *modes* are shared; the *rate* of failure is what differs.

---

## 6. What this means for the algorithm

- **Timing asymmetry is the operative, real-world cue.** It is the only cue that is both robustly used by the human (Section 3.3, Block 1) and reliably present on real Coral PCG (pitch and morphology are weak/absent there; amplitude helps the machine but not the human). It is available exactly where needed — at normal/brady rates, systole is clearly shorter than diastole.
- **The fix direction is a timing-grouping / interval-alternation period selector**, not another autocorrelation peak-pick heuristic. On clean synthetic data it already halves the octave errors (8→4) and the residual is provably information-limited.
- **The real engineering challenge is robust onset detection on noisy real PCG** — the exact thing that broke the real-file manipulation experiment. This is now clearly the crux.
- **There is ~8 points of headroom** between the detector (~91%) and the human ceiling (99%), and that headroom is extraction, not information.
- **The truly degenerate residual** (equal amplitude *and* symmetric timing, where even the human coin-flips) should be handled by a **confidence gate that declines**, not by a resolver.

---

## 7. Open questions / next steps

1. **Fix onset detection** (estimate coarse rate → constrain to ≤2 events/cycle; reject clips whose event count exceeds ~1.5× the ECG-expected) and **re-run the real-file cue-isolation** to finally test whether a subtle morphology cue exists on real PCG.
2. **Build and validate the timing-grouping detector on the real corpus**, targeting the human ceiling.
3. **Confidence gate** for the information-limited degenerate band.
4. (Optional) More reps on the synthetic loudness-rescue condition to firm up the "amplitude doesn't rescue the human" finding (currently n=2/condition).

## 8. Data-policy note
On raw audio, **268/269 files are hearable and 0 are irregular rhythms** — there is nothing to cull for hearability. The earlier instinct to drop "irregular" files was driven by the corrupted manipulation audio, not the recordings. **Recommendation: keep the whole dataset.**

---

## 9. Caveats (read before citing any number)
- Synthetic stimuli are clean damped sinusoids; **relative** effects (which manipulation breaks the percept) transfer, **absolute** thresholds do not.
- Several synthetic conditions had only **n=2 reps**; treat condition-level trends, not single points, as the result.
- The 99% human baseline benefits from replay, motivation, and forgiving tap scoring — it bounds what's *achievable*, not what a single-pass detector should be held to.
- The real-file manipulation experiment is **invalid** (over-detection); its tk/mk numbers must not be used.
- Cue-discriminability "absences" (pitch/morphology) reflect the limits of the *specific scalar features measured*, not proof the cue is physically absent.
