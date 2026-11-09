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
4. Device reference (`src/device/`)
5. Training and evaluation (`src/training/`)
6. C port (`c/`)
7. Settings
8. Data, evaluation and results
9. Resources
10. Layout and outputs
11. Development history

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
hr-estimation prepare                         # split (created once) and ECG consensus labels
hr-estimation train --out hr_v8               # network (B), scorer (D), readout calibration (F)
hr-estimation evaluate --split val            # tuning split
hr-estimation evaluate --split test           # held-out split
hr-estimation evaluate --split test --stress  # noise and extreme-rate stress set
hr-estimation evaluate --data <snapshot dir>  # another snapshot (e.g. unfiltered), evaluation only
hr-estimation vectors                         # per-block reference vectors for the C port
hr-estimation c-model                         # C model files (sizes, coefficients, weights)
hr-estimation profile                         # PSoC 6 cost model
pytest                                        # unit, streaming, corner-case, golden, budget tests
```

## 2. Usage example
```
$ hr-estimation evaluate --split test --tag final
03:06:09 FAIL regular_common.first_within_fine: value 0.877, target 0.92
03:06:09 targets passed 9/12 -> 20261108_030609_hr_v8_test_final

$ hr-estimation profile
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
  noise) can still give one (§8).

## 4. Device reference (`src/device/`)
The algorithm the stethoscope runs, as the Python reference for the C port. Each module is one
block: a state dataclass, `init` and a per-call function. State is float32 with sizes fixed at
`init`; weights are loaded at `init`; no other file access. `device` never imports `training`
(tested).

Usage, here on a synthetic 72 BPM signal (real output):

```python
from device import estimator
from training import augment, config

cfg = config.load()                                     # configs/device.yml + training.yml
audio, _ = augment.synthetic_pcg(72, duration_s=10.0)   # or a recording: float, 4000 Hz, [-1, 1]
state = estimator.init(cfg)                             # all block states; again after contact loss
block = cfg["stream"]["block_samples"]                  # 200 samples = 50 ms
for start in range(0, audio.size - block + 1, block):
    reading = estimator.process(state, audio[start:start + block])
    if reading is not None:                             # (t_s, bpm, confidence)
        print("t %.2f s  %.1f BPM  confidence %.2f" % reading)
```
```
t 2.00 s  71.8 BPM  confidence 0.84
t 3.00 s  71.3 BPM  confidence 0.96
...
t 10.00 s  72.8 BPM  confidence 0.98
```

| Module | Block | Called | In → out | State |
|---|---|---|---|---|
| `estimator.py` | all | `init(cfg)`; `process(state, block)` every 50 ms; `run(cfg, audio)` for a whole recording | 200 samples → reading `(t_s, bpm, confidence)` or `None` | all block states, block counter |
| `frontend.py` | A | `process(state, block)` per block | 200 samples → (4, 5) log energies | 4 bands x 2 biquads x 2 values |
| `net.py` | B | `normalise(state, frame)`, `step(state, x)` per 10 ms | 4 log energies → activation in [0, 1] | running mean (4), one delay line per layer |
| `features.py` | C | `push(state, x)` per sample; `compute(state)` per frame; `hr_grid(cfg)` | activation or 25-50 Hz band → (232, 13) per frame | ring buffer and lag sums: 400 (activation), 100 (S1 band) |
| `scorer.py` | D | `log_emission(model, feats)` per frame | (232, 17) → log-likelihood (232) | weights only |
| `tracker.py` | E | `update(state, log_emission)`, `estimate(state)` per frame | (232) → posterior (232), HR | posterior (232) |
| `rhythm.py` | R | `analyse(window, rate_hz, hr, cfg)` per frame | 4 s of activation → interval CV, beat-count HR | none |
| `readout.py` | F | `assess`, `first_due`, `schedule` per frame | posterior, HR, gates, CV → reading or none | times of the first and the latest reading |

`estimator.process` runs A, B and the per-sample update of C on every block; every fifth block
also `scorer_input` (C: 13 activation features, 3 S1-band features, flag), D, E, the two gate
values (`periodicity`, `activation_sd`), R and F.

## 5. Training and evaluation (`src/training/`)
Everything around the device code: data, ECG labels, training, evaluation and tools; runs on a
workstation or SageMaker, never on the stethoscope. One command, `hr-estimation`
(`training/cli.py`), one module call per step.

| Step | Command | Modules | Writes |
|---|---|---|---|
| 1. Split and labels | `hr-estimation prepare` | `data` (split by clinician account, once), `labels` (ECG R-peak consensus) | `output/splits/split_v1.csv`, `output/labels/ecg_labels.csv` |
| 2. Train | `hr-estimation train --out NAME` | `train`, `augment`; scorer inputs streamed through `device` | `output/models/NAME_tcn.npz`, `_scorer.npz`, `_calibration.npz` |
| 3. Select the model | `weights: NAME` in `configs/device.yml`, or `--weights NAME` per command | | |
| 4. Tune on validation | `hr-estimation evaluate --split val` | `evaluate` (every recording streamed through `device.estimator`) | `output/runs/<time>_NAME_val/` |
| 5. Test, once per candidate | `hr-estimation evaluate --split test`, then with `--stress` | `evaluate`, `augment` (stress conditions) | `output/runs/<time>_NAME_test[_stress]/` |
| 6. Other snapshot | `hr-estimation evaluate --data <dir>` | `labels.external`, `evaluate` | `output/runs/<time>_NAME_ext_<dir>/` |
| 7. Golden readings | copy the test run's `readings.json` to `output/golden/NAME_test_readings.json` | read by `tests/test_regression.py` | |
| 8. C port data | `hr-estimation vectors`, `hr-estimation c-model` | `vectors`, `c_model` | `output/vectors/*.npz`, `c/build/generated/` |
| 9. Budget | `hr-estimation profile` | `cost_model` | printed table and totals |

What a change needs:
- `readout` settings: nothing; evaluate on validation for the trade-off.
- `decision`, `tracker`, `rhythm`, `scorer.emission_beta`: `train --calibrate-only` (the
  calibration depends on the posterior), then evaluate on validation.
- S1-band or scorer settings: `train --scorer-only --weights BASE --out NEW` (keeps the network of
  `BASE`).
- `frontend`, `net`, `features`, `grid`: `train --out NEW` (network and scorer depend on them).

| Module | Role |
|---|---|
| `config.py` | Loads and merges the two configs, resolves paths, preflight checks |
| `data.py` | Manifest, grouped split by clinician account, WAV loading, parallel map over recordings |
| `labels.py` | ECG R-peaks (five NeuroKit2 detectors, three leads), reference HR per reading, other snapshots |
| `augment.py` | Synthetic PCG, noise at a set SNR, time warping and speed-up (training and stress) |
| `train.py` | Network (PyTorch, exported to numpy), scorer, readout calibration |
| `evaluate.py` | Streaming evaluation, grading per bucket, targets, stress protocol, figures, run folder |
| `cost_model.py` | PSoC 6 estimate of cycles, RAM and flash per block |
| `vectors.py` | Per-block reference vectors for the C port |
| `c_model.py` | C sizes, coefficients and weights from the config and the model files |
| `cli.py` | The `hr-estimation` command |

## 6. C port (`c/`)
C99 implementation of `src/device` for the PSoC 6: float32, no dynamic memory, no recursion, all
state in one caller-owned `hr_state_t` (13,836 bytes). One C file per Python module. Sizes,
coefficients and weights are generated from `configs/device.yml` and the model files
(`hr-estimation c-model` → `c/build/generated/hr_model.h` and `.c`; not in git, they hold the
weights).

| Python (`src/device/`) | C (`c/src/`) |
|---|---|
| `frontend.py` | `frontend.c` |
| `net.py` | `net.c` |
| `features.py` | `features.c` |
| `scorer.py` | `scorer.c` |
| `tracker.py` | `tracker.c` |
| `rhythm.py` | `rhythm.c` |
| `readout.py` | `readout.c` |
| `estimator.py` | `estimator.c`, the API of `c/include/hr.h` |

Build and test:

```bash
cd c
make model                     # generated sizes, coefficients, weights (hr-estimation c-model)
make                           # build/libhr_debug.* (tests), build/hr_run (host tool)
make m4                        # Cortex-M4 objects: code size per module, stack per function
cd .. && pytest tests/test_c_port.py
```

| Call | When | Does |
|---|---|---|
| `hr_init(&s, settings)` | good contact | clears all state; `NULL` settings: the defaults |
| `hr_process_block(&s, pcm, &reading)` | every 50 ms, 200 int16 samples | 1 when a reading is due (the first when confident, then 1 per s) |
| `hr_get_reading(&s, &reading)` | any time | latest reading; 0 before the first |
| `hr_get_frame_info(&s, &info)` | any time | estimate (also before the first reading), gate values, rhythm, probabilities, phase (warm-up, searching, reading) |
| `hr_set_settings(&s, &settings)`, `hr_get_settings` | any time | the three runtime settings, validated; used from the next frame |
| `hr_reset(&s)` | contact loss | new measurement, same settings |
| `hr_get_posterior`, `hr_model_id`, `hr_state_size` | any time | posterior over the 232 rates; model name and checksums; state size |

```c
#include "hr.h"

static hr_state_t hr;                                     /* caller-owned, 13.8 KB */

void on_contact(void) { hr_init(&hr, NULL); }             /* settings of configs/device.yml */

void on_audio_block(const int16_t pcm[HR_BLOCK_SAMPLES])  /* every 50 ms */
{
    hr_reading_t r;
    if (hr_process_block(&hr, pcm, &r))
        show(r.bpm, r.confidence);
}
```

Usage example (host tool on a synthetic 72 BPM signal, raw int16 PCM; real output):

```
$ c/build/hr_run hr72.raw
model hr_v8 61a88ea2280e, state 13836 bytes, 10.0 s of input
t 2.25 s  71.3 BPM  confidence 0.89
...
t 9.25 s  71.2 BPM  confidence 0.98
```

Agreement with the Python reference (`tests/test_c_port.py`):
- 6 synthetic heartbeat cases: every block's output within float32 rounding (largest absolute
  differences: band log energy 6.6e-4, features 3.5e-4, posterior 1.8e-5); same paths and
  readings.
- No heart sound (silence, hum alone): same front end, activation and activation SD, no reading
  in either. Features of a near-flat window are rounding noise in both and are not compared.
- All 825 test recordings: identical reading times (11,980 readings); BPM within 0.002 for 99 %
  of the recordings and at most 0.084, where a binary feature (local ACF peak) flips on a tie.

| Measure | Value | Budget |
|---|---|---|
| RAM: state (`hr_state_t`; 6,752 bytes of it per-frame scratch) | 13,836 bytes | |
| RAM: stack, deepest call chain (`hr_process_block` 296 + `hr_net_step` 144 + libm) | about 470 bytes | |
| RAM: total | about 14.3 KB | 15,360 bytes |
| Flash: code, Cortex-M4, `-Os` | 7,239 bytes | |
| Flash: constants (float32 coefficients and weights) | 6,188 bytes | |
| Flash: total, without libm (`expf`, `logf`, `floorf`, `ceilf`) | 13,427 bytes | 5,120 bytes, exceeded |
| Time per call on the host (Apple arm64, `-O2`) | 5.2 µs (ordinary block), 23.6 µs (frame block) | |
| Cortex-M4 cycles | not measured (needs the target; cost model 3.3 M cycles/s) | 30 M cycles/s |

On the target, cycles per call come from the DWT cycle counter around `hr_process_block`.

## 7. Settings
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

## 8. Data, evaluation and results
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
`hr-estimation evaluate --data <snapshot folder>` (pending a database pull).

## 9. Resources
Planning estimate, `hr-estimation profile` (Cortex-M4F cycles, float32, 150 MHz): 3.3 M cycles/s
of the 30 M budget; busiest 50 ms block 3.6 ms; RAM 14,720 bytes; flash 4,436 bytes, assuming
8-bit weights and 3,000 bytes of code. Every call runs blocks A, B and the per-sample part of C
(0.5 ms); every fifth call adds the frame work of C, D, E, R and F.

Measured on the C build (§6): RAM 13,836 bytes of state plus about 470 bytes of stack, within
the 15,360-byte budget; flash 13,427 bytes without libm (7,239 bytes of code, 6,188 bytes of
float32 constants), over the 5,120-byte budget. Cycles stay an estimate until measured on the
target. Reference vectors for the C port (`hr-estimation vectors`, `output/vectors/`): every
block's inputs and outputs on 8 synthetic cases (including silence and hum alone) and 3 test
recordings.

## 10. Layout and outputs
```
configs/device.yml          what the stethoscope runs: user settings, algorithm constants
configs/training.yml        data, split, labels, augmentation, training, evaluation, targets
src/device/                 on-device algorithm (blocks A-F and R, estimator); ports to C 1:1
src/training/               config, data, labels, augment, train, evaluate, cost_model, vectors,
                            c_model, cli
c/                          C port: include/ (API), src/ (one file per device module), tools/,
                            Makefile; build/ (generated model files, binaries) not in git
                            (never on the device)
tests/                      pytest suite
data/                       recordings (not in git)
output/                     splits, labels, models, runs, caches, golden readings (not in git)
```
A run folder `output/runs/<time>_<model>_<split>[_<tag>]/` holds `config.yml`,
`per_record.csv`, `readings.json`, `summary.json` (metrics per bucket and group), `targets.json`
(pass/fail), `run_info.json` (git commit, weight checksums, cost), figures and `strata.csv`;
stress runs add `stress_by_condition.csv`.

## 11. Development history
`main` holds the selected candidate only. The other approaches evaluated during development
(autocorrelation and GCC-PHAT front ends, end-to-end networks, alternative release rules and
feature sets) are kept on branch `arvind/prototype`.
