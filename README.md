# Heart rate from heart sounds on the stethoscope MCU

Streaming heart rate (HR) estimation from the phonocardiogram (PCG, heart-sound audio, 4000 Hz,
16-bit) for the PSoC 6 Cortex-M4F, within 30 MIPS, 15 KB RAM and 5 KB flash. A small causal
network marks heartbeats, periodicity features measure their spacing, a small scorer and a
hidden Markov model (HMM) tracker pick the rate, and a readout decides when to show it: an
accurate first reading at 2.5-4 s for regular rhythms, a later first reading (from 4 s) for slow
or irregular hearts that then converges, one reading per second, and three runtime settings.
Python reference implementation: the device code has fixed-size float32 state and maps one
module to one C file.

## Contents
1. Quick start
2. Usage example
3. Signal flow
4. Settings
5. Data, evaluation and results
6. Resources
7. Layout and outputs
8. Development history

## 1. Quick start
Environment (run once):

```bash
conda create -n hr_estimation python=3.11
conda activate hr_estimation
pip install -e .
```

Run (recordings under `data/`, models and results under `output/`, both outside git):

```bash
cd heart_rate_estimation
pcg-hr prepare                         # split (created once) and ECG consensus labels
pcg-hr train --out hr_v8               # network (B), scorer (D), readout calibration (F)
pcg-hr evaluate --split val            # tuning split
pcg-hr evaluate --split test           # held-out split
pcg-hr evaluate --split test --stress  # noise and extreme-rate stress set
pcg-hr evaluate --data <snapshot dir>  # another snapshot (e.g. unfiltered), evaluation only
pcg-hr vectors                         # per-block reference vectors for the C port
pcg-hr profile                         # PSoC 6 cost model
pytest                                 # unit, streaming, corner-case, regression, budget tests
```

## 2. Usage example
```
$ pcg-hr evaluate --split test --tag final
03:06:09 FAIL regular_common.first_within_fine: value 0.877, target 0.92
03:06:09 targets passed 9/12 -> 20261108_030609_hr_v8_test_final

$ pcg-hr profile
 "mips": 3.3134296,
 "ram_bytes": 14720,
 "within_budget": true
```

`regular_common.first_within_fine`: share of first readings within ±3 BPM of the ECG heart rate
over the same seconds, for regular rhythms at 50-200 BPM. `mips`: million cycles per second.

## 3. Signal flow
Key shapes: `PCG 4000 Hz, 200-sample blocks ─▶ band log energies (4, 100 Hz) ─▶ activation
(100 Hz) ─▶ features (232 states x 17) every 0.25 s ─▶ log-likelihood (232) ─▶ posterior (232)
─▶ HR (BPM): first reading when confident, then 1 per s`

```
┌──────────────────────────────────────┐
│ A  Front end                         │
│    4 band-passes, log energy         │
│    out: (4 bands, 5) per block       │
└───────────────────┬──────────────────┘
                    │ 4 log energies minus running mean
                    ├──────────────────────────────────────────┐ 25-50 Hz band
                    ▼                                          ▼
┌──────────────────────────────────────┐     ┌──────────────────────────────────┐
│ B  Causal TCN                        │     │ C  S1 band                       │
│    913 weights, 0.63 s field         │     │    averaged to 50 Hz, last 2 s   │
│    out: activation (100 Hz)          │     │    out: lag sums (100 lags)      │
└───────────────────┬──────────────────┘     └─────────────────┬────────────────┘
                    ▼                                          │
┌──────────────────────────────────────┐                       │
│ C  Periodicity features              │◀──────────────────────┘
│    lag sums, last 4 s (400 lags)     │
│    out: (232 states, 17) / 0.25 s    │
└───────────────────┬──────────────────┘
                    ▼
┌──────────────────────────────────────┐
│ D  Scorer MLP                        │
│    17 -> 16 -> 1 per state           │
│    out: log-likelihood (232)         │
└───────────────────┬──────────────────┘
                    ▼
┌──────────────────────────────────────┐     ┌──────────────────────────────────┐
│ E  HMM tracker                       │────▶│ R  Rhythm                        │
│    232 states, 30-299 BPM            │     │    beats in the last 4 s of B    │
│    out: posterior (232), HR          │     │    out: beat-interval CV         │
└───────────────────┬──────────────────┘     └─────────────────┬────────────────┘
                    ▼                                          │
┌──────────────────────────────────────┐                       │
│ F  Readout                           │◀──────────────────────┘
│    gates; regular or slow/irr. path  │
│    out: reading (BPM) or none        │
└──────────────────────────────────────┘
```

| Block | Module | What it does |
|---|---|---|
| A | `device/frontend.py` | 4 Butterworth band-passes (25-50, 50-100, 100-200, 200-400 Hz), 2 biquads each, started in steady state for the first sample; log of the mean energy per 10 ms. |
| B | `device/net.py` | Causal temporal convolutional network (TCN): 5 layers, kernel 3, dilations 1-16, 8 channels, 0.63 s receptive field; output near 1 at each beat. |
| C | `device/features.py` | Lag sums updated per sample over the last 4 s of activation and the last 2 s of the 25-50 Hz band (S1, at 50 Hz); per frame, autocorrelation (ACF), normalised cross-correlation (NCCF) and YIN; 13 + 3 features per candidate rate, plus a flag; the S1-band features apply to candidates up to 250 BPM. |
| D | `device/scorer.py` | Same 339-parameter MLP for every candidate rate; log-softmax over the 232 rates (30-299 BPM). |
| E | `device/tracker.py` | Every 0.25 s: random walk 8 % per √s, half/double jump 0.002, evidence weight 0.35. |
| R | `device/rhythm.py` | Beats picked from the activation (S2 suppressed using the tracker's period); beat-interval variation and beat-count rate. |
| F | `device/readout.py` | Path per frame: slow (estimate below 50 BPM) or irregular (picked-beat variation ≥ 10 %) versus regular. Probability of the reading being right from the posterior mass and the periodicity (logistic, fitted on validation). Regular: first reading at `confidence`. Slow or irregular: from `slow_irregular_after_s` at probability 0.9 within ±10 BPM. No reading without heartbeats: activation ACF at the estimate's period below 0.2, or activation SD over the window below 0.03 (silence, mains hum). Forced at `latest_s`. |

### How it works, step by step
One call per 50 ms block of 200 audio samples (4000 Hz); a frame is 5 blocks (0.25 s).

| Step | In | What happens | Out |
|---|---|---|---|
| A front end | 200 samples | 4 band-pass filters (25-50, 50-100, 100-200, 200-400 Hz); mean energy per 10 ms; log | 4 bands x 5 values |
| B beat network | 4 log energies per 10 ms | subtract a 1 s running mean (gain drops out); 5-layer causal network that sees the last 0.63 s | 1 value per 10 ms in [0, 1], high at each beat (the activation) |
| C features (per sample) | each activation value; the 25-50 Hz band averaged to 50 Hz | ring buffers of the last 4 s (400 values) and 2 s (100 values); lag sums: how much each signal matches itself shifted by k samples, updated per sample | running sums only |
| C features (per frame) | the lag sums | for each of 232 candidate rates (period T = 6000 / BPM samples): 13 measures from the activation (similarity at T, T/2, 2T, 3T; normalised versions; exact peak position; periods in the window) and 3 from the S1 band (similarity at T, T/2, 2T; used up to 250 BPM, where S1 and S2 are still apart) plus a flag | 232 x 17 numbers |
| D scorer | 232 x 17 | the same tiny network scores every candidate; softmax over candidates | 232 probabilities for this frame |
| E tracker | previous belief + this frame | belief drifts slightly (rate changes up to about 8 % per √s), may jump to half or double with probability 0.002, then is multiplied by the frame's probabilities | 232 probabilities; estimate = weighted mean around the peak |
| R rhythm | the last 4 s of activation, the estimate | picks the beats (peaks at least 0.6 periods apart) and measures how evenly they come | beat-interval variation |
| F readout | estimate, belief, periodicity, activation SD, rhythm | decides when to show the first reading, then shows the estimate every second | reading (BPM, confidence) or nothing |

**Half or double rate.** Each beat has two sounds (S1, S2), so a signal can look periodic at
half the true period. Four safeguards: the features compare every candidate period T with T/2,
2T and 3T, so the true period stands out from its multiples; the 25-50 Hz band, where S1
dominates, repeats only at the true period even when S2 is as loud as S1; the tracker allows a
jump to half or double only with low probability, so one misleading frame cannot flip the
reading; and the first reading needs the frame's own best rate and the tracker to agree within
5 %.

**Regular, irregular, slow and fast hearts.**
- Regular rhythm at a common rate: first reading as soon as the probability of being within
  ±3 BPM reaches `confidence` (typically 2.5-3 s), then one per second.
- Slow (estimate below 50 BPM: few beats per second) or irregular (picked beats vary by 10 % or
  more, such as atrial fibrillation or extra beats): first reading not before
  `slow_irregular_after_s` (4 s), when the probability of being within ±10 BPM reaches 0.9; the
  following readings converge (within ±3 BPM for most recordings by 9 s).
- Fast (up to 299 BPM): many beats per second, same path as regular rhythm.
- No heart sound: silence and mains hum alone give no reading (the activation is flat or has no
  periodicity). Noise that the network takes for beats (friction bursts, some white or breath
  noise) can still give one (§5).

## 4. Settings
Runtime settings (`configs/device.yml`, part 1; no retraining):

| Setting | Effect | Default |
|---|---|---|
| `confidence` | Regular hearts: probability the first reading must have of being within ±3 BPM. Lower: sooner and less accurate. | 0.7 |
| `slow_irregular_after_s` | Slow or irregular hearts: earliest first reading. | 4.0 s |
| `latest_s` | With a heart sound present, a reading by this time. | 8.0 s |

Effect of `confidence` (validation, regular rhythms at 50-200 BPM): 0.6 → first reading 2.75 s
median, 84.3 % within ±3 BPM; 0.65 → 3.0 s, 85.0 %; 0.7 (default) → 3.0 s, 85.4 %. Internal
constants (rate range, slow limit, regularity limit, certainty of the slow/irregular path,
heart-sound gates) sit in part 2 (`decision`) and need re-validation when changed.

## 5. Data, evaluation and results
- Data: snapshot `sq5_sweep_core500_20261008`, 2735 DUO3 recordings (17 s PCG, three ECG leads
  at 500 Hz), selected on PCG quality ≥ 0.7 and ECG-PCG agreement ≤ 5 BPM. Split by clinician
  account: 1366 train, 544 validation, 825 test. Patient-derived; never committed.
- Reference: R-peaks by consensus of five NeuroKit2 detectors on three leads, gaps filled from a
  single clean lead (`training/labels.py`); every reading is graded against the ECG HR over the
  same seconds. Training adds 4 augmented copies per recording (time warping, speed-up to
  200-300 BPM, rhythm jitter, noise, gain, tilt).
- Buckets (from the ECG): regular (beat-interval variation < 5 %) at 50-200 BPM, slow (< 50 BPM),
  irregular (≥ 5 % or atrial fibrillation), very fast (200-300 BPM, sped-up recordings only).

Test (run `20261108_030609_hr_v8_test_final`; very fast from stress run
`20261108_032254_hr_v8_test_final_stress`):

| Bucket | Recordings | First reading, median | First reading accuracy | Later | Targets |
|---|---|---|---|---|---|
| regular, 50-200 BPM | 438 | 2.5 s (90 % by 4.0 s) | 87.7 % within ±3 BPM, 99.5 % within ±10 | 90.0 % within ±3 BPM at 4 s, 98.9 % at 9 s | 2 of 4 |
| slow | 30 | 4.0 s | 100 % within ±10 BPM | 100 % within ±3 BPM at 9 s | 3 of 3 |
| irregular or AF | 235 | 4.0 s | 76.2 % within ±10 BPM | 80.4 % within ±3 BPM at 9 s | 2 of 3 |
| very fast (sped-up recordings) | 40 per rate | | 52.5-100 % within ±10 BPM | half/double 7.5 % | 0 of 2 |
| all | 703 | 3.5 s | | 99.9 % with a reading by 10 s; half/double 0.2 % | 2 of 2 |

Missed targets: regular first reading (92 %) and 4 s reading (92 %) within ±3 BPM, irregular first
reading within ±10 BPM (90 %), very fast (sped-up recordings, where speeding up also moves S1 out
of the 25-50 Hz band; synthetic heart sounds at 170-290 BPM give no half/double readings).
Corner cases (synthetic: gain, DC offset, 33-220 BPM, loud S2, murmur, irregular beats, start-up
noise, contact loss, mains hum with a heart; no reading for silence or hum alone) all pass. Noise
alone still gives a reading in some cases (25 synthetic 17 s inputs per type: friction 25, breath
13, white 5): a known limitation, tested as an expected failure. Unfiltered recordings:
`pcg-hr evaluate --data <snapshot folder>` (pending a database pull).

## 6. Resources
`pcg-hr profile` (planned C code, Cortex-M4F cycles, float32, 150 MHz): 3.3 M cycles/s of the 30 M
budget; busiest 50 ms block 3.6 ms; RAM 14,720 of 15,360 bytes; flash 4,436 of 5,120 bytes (with
a 3,000-byte code estimate). Every call runs blocks A, B and the per-sample part of C (0.5 ms);
every fifth call adds the frame work of C, D, E, R and F. Estimates until measured on the target.
Reference vectors for the C port (`pcg-hr vectors`, `output/vectors/`): every block's inputs and
outputs on 8 synthetic cases (including silence and hum alone) and 3 test recordings.

## 7. Layout and outputs
```
configs/device.yml          what the stethoscope runs: user settings, algorithm constants
configs/training.yml        data, split, labels, augmentation, training, evaluation, targets
src/device/                 on-device algorithm (blocks A-F and R, estimator); ports to C 1:1
src/training/               data, labels, augment, train, evaluate, cost_model, config, cli
                            (never on the device)
tests/                      pytest suite
data/                       recordings (not in git)
output/                     splits, labels, models, runs, caches, golden readings (not in git)
```
A run folder `output/runs/<time>_<model>_<split>[_<tag>]/` holds `config.yml`,
`per_record.csv`, `readings.json`, `summary.json` (metrics per bucket and group), `targets.json`
(pass/fail), `run_info.json` (git commit, weight checksums, cost), figures and `strata.csv`;
stress runs add `stress_by_condition.csv`.

## 8. Development history
This branch holds the selected candidate only. The other approaches evaluated during development
(autocorrelation and GCC-PHAT front ends, end-to-end networks, alternative release rules and
feature sets) are kept on branch `arvind/prototype`.
