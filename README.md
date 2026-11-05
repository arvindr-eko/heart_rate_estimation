# Heart rate estimation from PCG for the stethoscope MCU

Streaming heart rate (HR) estimation from the phonocardiogram (PCG, heart-sound audio, 4000 Hz)
for a microcontroller (MCU) budget of 30 MIPS, 15 KB RAM and 5 KB flash. Python prototype;
every module processes fixed-size blocks with fixed-size state and maps to one C module.
Three candidate families are implemented and compared: pure signal processing (DSP), a hybrid
(causal TCN + DSP periodicity features + learned scorer + HMM tracker) and end-to-end ML.
The finalist, `hybrid_aug`, is trained on real and augmented recordings (time warping, rhythm
jitter, noise, gain, tilt); it answers at 3 s by default, waits up to 6 s when the heart may be
slower than 50 BPM or the estimate is uncertain, and always answers by 8 s. Accuracy is graded
against the ECG heart rate (lead 2, `_ECG_1`); ECG is never an algorithm input.

## Contents
1. Quick start
2. Usage example
3. Signal flow
4. Modules
5. Configuration and variants
6. Data and outputs
7. Results summary

## 1. Quick start
Environment (run once):

```bash
conda create -n hr_estimation python=3.11
conda activate hr_estimation
pip install -r requirements.txt
```

Run (from the repository root):

```bash
cd heart_rate_estimation
python scripts/hr_cli.py split                                    # grouped train/val/test split
python scripts/hr_cli.py relabel                                  # ECG-count labels (data/.../ecg)
python scripts/hr_cli.py train-net --variant hybrid_aug --out net_tcn8_aug --augment \
    --set salience.enabled=false tracker.release_mode=frame
python scripts/hr_cli.py train-salience --variant hybrid_aug --features-name dual_aug_train \
    --out sal_dual16_aug --augment --set tracker.release_mode=frame
python scripts/hr_cli.py eval --variants hybrid_aug,dsp_final,ml_final --split test
python scripts/hr_cli.py eval --variants hybrid_aug --split test --stress
python scripts/hr_cli.py regrade --runs-glob '*_hybrid_aug_test'      # ECG same-window grading
python scripts/hr_cli.py select --runs-glob '*_tune'                # best run per family
```

## 2. Usage example
```
$ python scripts/hr_cli.py eval --variants hybrid_aug --split test
10:53:31 hybrid_aug: first_within 0.965 steady_within 0.995 coverage 1.000 ttfr_median 3.00 s
         half_double 0.002 -> 20261105_105331_hybrid_aug_test
```

`first_within`: share of first readings within ±5 BPM or ±10 % (whichever is greater) of
the ECG reference HR. `ttfr_median`: median time to first reading from recording start.

## 3. Signal flow
Key shapes: `PCG 4000 Hz, 200-sample blocks ─▶ band energies (4, 100 Hz) ─▶ activation (100 Hz)
─▶ features (205, 24) every 0.25 s ─▶ log-likelihood (205) ─▶ posterior (205) ─▶ HR or none, 1/s`

```
                   ┌──────────────────────────────────┐
                   │ A  Front end (§ frontend.py)     │
                   │    4 band-pass bands, log energy │
                   │    out: (4, 5) per 50 ms block   │
                   └─────────────────┬────────────────┘
                                     ▼
                   ┌──────────────────────────────────┐
                   │ B  Causal TCN (§ net.py)         │
                   │    913 weights, 0.63 s field     │
                   │    out: activation (1, 5), 100 Hz│
                   └─────────────────┬────────────────┘
                  ┌──────────────────┴────────────────────┐
                  ▼                                       ▼
┌──────────────────────────────────┐    ┌──────────────────────────────────┐
│ C1 Features of activation        │    │ C2 Features of band-mean env     │
│    ACF/NCCF/YIN/DFT, 4 s window  │    │    (from A), same 12 features    │
│    out: (205 states, 12)         │    │    out: (205 states, 12)         │
└─────────────────┬────────────────┘    └─────────────────┬────────────────┘
                  └──────────────────┬────────────────────┘
                                     ▼
                   ┌──────────────────────────────────┐
                   │ D  Salience MLP (§ salience.py)  │
                   │    24 -> 16 -> 1, shared weights │
                   │    out: log-softmax (205)        │
                   └─────────────────┬────────────────┘
                                     ▼
                   ┌──────────────────────────────────┐
                   │ E  HMM tracker (§ tracker.py)    │
                   │    205 states, 30-230 BPM        │
                   │    out: posterior (205), 0.25 s  │
                   └─────────────────┬────────────────┘
                                     ▼
                   ┌──────────────────────────────────┐
                   │ F  Release + output              │
                   │    frame-confidence schedule     │
                   │    out: HR (BPM) or none, 1/s    │
                   └──────────────────────────────────┘
```

| Block | Function |
|---|---|
| A | Butterworth band-pass per band (25-50, 50-100, 100-200, 200-400 Hz), mean energy per 10 ms, log. |
| B | Causal dilated TCN (dilations 1-16, kernel 3, 8 channels) on running-mean-normalised log energies; trained so the ACF of its output peaks at the true beat period (no beat labels). |
| C | Per HR state (period T): ACF at T, T/2, 2T; NCCF and YIN at T; DFT magnitude at f, 2f, f/2; peak flag; relative ACF; periods in window. Window grows to 4 s. |
| D | MLP shared across states; log-softmax over states is the frame emission. HR is not an input. |
| E | Forward HMM over log-spaced states (1 % apart): random walk, octave-jump prior, uniform floor. |
| F | Deadline release (`release:` in `config.yml`): first reading when the frame confidence (softmax mass within ±5 % of the best state) meets a threshold and agrees with the tracker; threshold 0.97 before the cutoff, then linear from 0.9 to 0 at 8 s. Cutoff 3 s, or 6 s while the estimate or ≥ 30 % of the posterior lies below 50 BPM. Then one reading per second, always shown. |

Release knobs (`release:` section; runtime parameters in the C port):

| Knob | Default | Effect |
|---|---|---|
| `default_s` | 3.0 | answer cutoff when HR ≥ `low_hr_bpm` |
| `low_hr_s` | 6.0 | answer cutoff when the heart may be slower |
| `max_s` | 8.0 | every recording answered by this time |
| `low_hr_bpm` | 50 | slow-heart boundary |
| `low_mass_min` | 0.3 | posterior mass below `low_hr_bpm` that counts as possibly slow |
| `c_early`, `c_cutoff` | 0.97, 0.9 | confidence needed before / at the cutoff |
| `confidence` | frame | `frame` (scorer confidence + tracker agreement) or `gate` (learned P(correct)) |

## 4. Modules
| Module | Content | C port |
|---|---|---|
| `hr/frontend.py` | band-pass, notch, envelope, decimation | yes |
| `hr/net.py` | streaming TCN/GRU inference, HR-class head | yes |
| `hr/periodicity.py` | ACF, NCCF, YIN, GACF/PHAT, GCC segment, DFT, per-state features | yes |
| `hr/salience.py` | salience MLP inference | yes |
| `hr/gate.py` | optional learned release confidence (inference) | optional |
| `hr/augment.py` | training augmentation: beat-synchronous time warping, rhythm jitter, noise, gain, tilt | no |
| `hr/reference.py` | ECG answer key: R-peaks, trusted-reference rule, windowed HR, relabel | no |
| `hr/beats.py` | beat picking on the activation (tested, not used by the finalist) | no |
| `hr/tracker.py` | HMM over HR states, estimate and confidence | yes |
| `hr/estimator.py` | block-level pipeline, release rule, resource estimate | yes |
| `hr/net_train.py`, `hr/salience_train.py`, `hr/gate_train.py` | training (PyTorch, scikit-learn) | no |
| `hr/io.py`, `hr/synth.py`, `hr/evaluate.py`, `hr/plots.py` | data, synthetic PCG and noise, metrics, figures | no |
| `scripts/hr_cli.py` | split, train, eval, select, compare, figures | no |

## 5. Configuration and variants
`config.yml` holds all paths, parameters, targets and budgets. Variants are deep-merged over
the base; finalists: `hybrid_aug` (current), `hybrid_deadline` (same rule, no augmentation),
`hybrid_final` (previous release rule), `dsp_final`, `ml_final`. Training augmentation:
`augment:` section; ECG answer key: `reference:` section. Any value can be overridden per
run with `--set section.key=value`.

## 6. Data and outputs
- Input: `data/sq5_sweep_core500_20261008/` (2735 DUO3 recordings, 4000 Hz, `manifest.csv`;
  `ecg/`: raw ECG, three leads per recording, 500 Hz). Patient-derived; never committed.
- Reference HR: `efast_ecg_heart_rate`. Split: grouped by `user_id`, stratified by 5 BPM bin
  (train 1366, val 544, test 825); `val_a` / `val_b` are the two val folds (gate training /
  tuning).
- `output/runs/<run_id>/`: resolved config, per-record results, readings, summary, run record.
  `output/models/`: weights (`.npz`). `output/cache/`: feature caches. Not committed.

## 7. Results summary
Test split (825 recordings), evaluated once per finalist:

| Candidate | First reading within tolerance | Steady state | Half/double | Median TTFR | MIPS / RAM / weights (estimated) |
|---|---|---|---|---|---|
| Hybrid, augmented training (finalist, final answer key, 818 recordings) | 96.7 % | 99.6 % | 0.0 % | 3.0 s (HR < 50: 6-8 s) | 8.9 / 9.8 KB / 1.6 KB |
| Same, fast setting `hybrid_aug_fast` (cutoff 2.5 s) | 96.5 % | 99.6 % | 0.1 % | 2.5 s | 8.9 / 9.8 KB / 1.6 KB |
| Same, EFAST answer key (825 recordings) | 96.5 % | 99.5 % | 0.2 % | 3.0 s | 8.9 / 9.8 KB / 1.6 KB |
| Hybrid, deadline release, no augmentation | 95.9 % | 99.6 % | 0.5 % | 3.0 s (HR < 50: 6.0 s) | 8.9 / 9.8 KB / 1.6 KB |
| Hybrid, previous confidence release | 95.0 % | 99.5 % | 0.5 % | 2.75 s | 8.9 / 9.8 KB / 1.6 KB |
| End-to-end ML | 85.7 % | 91.9 % | 0.6 % | 2.50 s | 1.1 / 9.8 KB / 5.5 KB |
| DSP | 81.5 % | 94.3 % | 4.6 % | 2.00 s | 0.6 / 3.3 KB / 0.1 KB |
| ACF, fixed 3 s window | 78.9 % | 93.7 % | 6.4 % | 3.00 s | 0.4 / 1.4 KB / 0 |
| ACF, fixed 6 s window | 88.4 % | 93.6 % | 5.5 % | 6.00 s | 0.8 / 2.0 KB / 0 |

Final answer key: EFAST ECG HR, replaced by the ECG beat count on 14 recordings where all three
leads count at least 1.5x the EFAST HR (`output/labels/reference_v2.csv`); those form a separate
"unheard beats" group (7 in test), reported apart. On the 472 test recordings with a clean ECG
window before the first reading, the first reading is within tolerance of the same-window ECG
heart rate in 472 of 472 cases.
Resources are operation-count and buffer-size estimates from `estimator.resources`, not
target measurements. Full study: `hr_notes/hr_estimation_report.md`.
