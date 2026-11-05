# pcg_hr: heart rate from heart sounds on the stethoscope MCU

Streaming heart rate (HR) estimation from the phonocardiogram (PCG, heart-sound audio, 4000 Hz,
16-bit) for the PSoC 6 Cortex-M4F within 30 MIPS, 15 KB RAM and 5 KB flash. A small causal
temporal convolutional network (TCN) marks heartbeats; autocorrelation and spectral features
measure their spacing; a small scorer and a hidden Markov model (HMM) tracker pick the HR; a
deadline rule decides when to show it: by 3 s normally, by 6 s when the heart may be slower than
50 BPM, always by 8 s. Python reference implementation; every block has fixed-size state and
maps to one C module (`c/include/hr_api.h`).

## Contents
1. Quick start
2. Usage example
3. Signal flow
4. Data
5. Results
6. Integration on the PSoC 6
7. Cost model
8. Tests
9. Layout, outputs and naming

## 1. Quick start
Environment (run once):

```bash
conda create -n hr_estimation python=3.11
conda activate hr_estimation
pip install -e .
```

Run (from the repository root; recordings, ECG files and models under `data/` and `output/`):

```bash
cd heart_rate_estimation
pcg-hr split                              # grouped train / val / test split
pcg-hr relabel                            # answer key from the three ECG leads
pcg-hr train-net --augment                # TCN (block B)
pcg-hr train-scorer --augment --features-name train_aug   # scorer (block D)
pcg-hr eval --split test                  # final candidate on the held-out split
pcg-hr --variant fast eval --split test   # fast setting (answer cutoff 2.5 s)
pcg-hr eval --split test --stress         # synthetic noise, 4 types x 4 SNRs
pcg-hr profile                            # PSoC 6 cost model
pytest                                    # unit, pipeline, streaming, regression, budget
```

## 2. Usage example
```
$ pcg-hr eval --split test
15:34:45 final: first within 0.967, half/double 0.000, median answer 3.00 s (HR >= 50 BPM),
         targets 8/8 -> 20261105_153445_final_test_final

$ pcg-hr profile
 "mips": 15.9777696,
 "peak_block_ms": 25.196842666666665,
 "ram_bytes": 12848,
 "flash_bytes": 4538,
 "within_budget": true
```

`first within`: share of first readings within ±5 BPM or ±10 % of the reference HR
(ANSI/AAMI EC13 tolerance). `median answer`: median time to the first reading.

## 3. Signal flow
Key shapes: `PCG 4000 Hz, 200-sample blocks ─▶ band log energies (4, 100 Hz) ─▶ activation
(100 Hz) ─▶ features (205 states x 24) every 0.25 s ─▶ log-likelihood (205) ─▶ posterior (205)
─▶ HR (BPM), 1 reading per s`

```
                   ┌──────────────────────────────────┐
                   │ A  Front end                     │
                   │    4 band-passes, log energy     │
                   │    out: (4, 5) per 50 ms block   │
                   └─────────────────┬────────────────┘
                                     ▼
                   ┌──────────────────────────────────┐
                   │ B  Causal TCN, 913 weights       │
                   │    0.63 s receptive field        │
                   │    out: activation, 100 Hz       │
                   └─────────────────┬────────────────┘
                  ┌──────────────────┴────────────────────┐
                  ▼                                       ▼
┌──────────────────────────────────┐    ┌──────────────────────────────────┐
│ C1 Features of activation        │    │ C2 Features of band-mean env     │
│    ACF/NCCF/YIN/DFT, <= 4 s      │    │    (from A), same 12 features    │
│    out: (205 states, 12)         │    │    out: (205 states, 12)         │
└─────────────────┬────────────────┘    └─────────────────┬────────────────┘
                  └──────────────────┬────────────────────┘
                                     ▼
                   ┌──────────────────────────────────┐
                   │ D  Scorer MLP, 417 weights       │
                   │    24 -> 16 -> 1 per state       │
                   │    out: log-softmax (205)        │
                   └─────────────────┬────────────────┘
                                     ▼
                   ┌──────────────────────────────────┐
                   │ E  HMM tracker                   │
                   │    205 states, 30-228 BPM        │
                   │    out: posterior, every 0.25 s  │
                   └─────────────────┬────────────────┘
                                     ▼
                   ┌──────────────────────────────────┐
                   │ F  Deadline release              │
                   │    3 s; 6 s if slow; 8 s max     │
                   │    out: HR (BPM), then 1 per s   │
                   └──────────────────────────────────┘
```

| Block | Module | What it does |
|---|---|---|
| A | `frontend.py` | 4 Butterworth band-passes (25-50, 50-100, 100-200, 200-400 Hz); log of the mean energy per 10 ms (4000 / 100 = 40 samples). |
| B | `net.py` | Causal TCN: 5 layers, kernel 3, dilations 1-16, 8 channels; receptive field 1 + 2 x 31 = 63 frames = 0.63 s; 913 weights. Input: band energies minus a 1 s running mean. Output: activation per 10 ms, high at each beat. Trained so the autocorrelation of its output peaks at the true beat period (no beat labels). |
| C | `features.py` | Window grows from 1 s to 4 s. For each HR state with period T: ACF at T, T/2, 2T and a flag for 2T fitting; NCCF and YIN at T; DFT at f, 2f, f/2; peak flag; ACF relative to its maximum; log(window / T). Computed on the activation and on the band-mean envelope: 24 values per state. |
| D | `scorer.py` | MLP 24 → 16 → 1 with the same 417 weights for every state; log-softmax over the 205 states. HR is not an input. |
| E | `tracker.py` | Update every 0.25 s; random walk 4 % per √s; jump to double or half with probability 0.002 per update; evidence = 0.35 x scorer log-probability. |
| F | `release.py` | First reading when the scorer's probability within ±5 % of its best state agrees with the tracker and reaches 0.97 before the cutoff, then 0.9 falling to 0 at 8 s. Cutoff 3 s, or 6 s while the estimate or ≥ 30 % of the tracker probability is below 50 BPM. Then one reading per second. |

`estimator.py` composes A-F (`init`, `process(block)`); HR states: 205, log-spaced 1 % apart,
30-228 BPM (`grid:` in `config.yml`).

## 4. Data
Snapshot `sq5_sweep_core500_20261008`: 2735 DUO3 recordings, PCG 4000 Hz, 17 s (longer ones
truncated); raw ECG (three leads, 500 Hz) for the answer key only. Filtered at extraction on PCG
quality ≥ 0.7 and ECG-PCG HR agreement ≤ 5 BPM: a clean set. Patient-derived; never committed.

| Split | Recordings | Users | HR median | < 50 BPM | ≥ 140 BPM | Atrial fibrillation | Use |
|---|---|---|---|---|---|---|---|
| train | 1366 | 593 | 106 | 121 | 331 | 34 | training (+ 5464 augmented copies) |
| val | 544 | 238 | 106 | 48 | 135 | 14 | every design and threshold choice |
| test | 825 | 342 | 106 | 68 | 213 | 18 | held out; evaluated once per candidate |

- Splits grouped by `user_id` (no clinician account in two splits), stratified by 5 BPM bin.
- Answer key (`output/labels/reference_v2.csv`): EFAST ECG HR; replaced by the ECG beat count on
  14 recordings where all three leads count at least 1.5x the EFAST HR (EFAST counting one beat
  per group of irregular beats: train 5, val 2, test 7). These form a separate "unheard beats"
  group, reported apart from the main metrics (`sep_*`).
- Augmentation (train only, 4 copies per recording): time warping at ECG R-peaks of 841
  recordings with regular beats (only diastole stretched; 2352 copies, a third aimed at 30-50
  BPM), rhythm jitter 3-15 % (613), noise white / mains hum / breath at 0-25 dB SNR (4359;
  friction held out for testing), gain ±12 dB, spectral tilt (2755).

## 5. Results
Test split, main group (818 recordings; 7 unheard-beat recordings reported apart):

| | Default (cutoff 3.0 s) | Fast (cutoff 2.5 s) |
|---|---|---|
| First reading within tolerance | **96.7 %** | 96.5 % |
| HR ≥ 50 / < 50 BPM | 96.7 / 97.1 % | 96.7 / 94.1 % |
| Half/double errors | 0.0 % | 0.1 % |
| Steady state (median of all readings) | 99.6 % | 99.6 % |
| Bland-Altman 95 % limits (steady state) | ±2.6 BPM | ±2.7 BPM |
| Clean ECG windows vs ECG HR over the same 4 s | 472 / 472 | 410 / 410 |
| Median answer HR ≥ 50 (95th percentile); HR < 50; latest | 3.0 s (4.5 s); 6.0 s; 7.75 s | 2.5 s (4.4 s); 6.0 s; 7.75 s |
| Unheard-beat group: reads the heard rate / the ECG count | 5 / 2 | 5 / 2 |
| Targets met | 8 of 8 | 8 of 8 |

First reading within tolerance under synthetic noise (test, 198 recordings per condition,
default setting; SNR measured in 25-400 Hz):

| Noise | 20 dB | 10 dB | 5 dB | 0 dB |
|---|---|---|---|---|
| White | 98.0 % | 97.5 % | 97.0 % | 96.5 % |
| Friction (not in training) | 97.5 % | 98.0 % | 97.0 % | 97.0 % |
| Mains hum | 97.0 % | 97.0 % | 94.9 % | 94.4 % |
| Breath | 97.5 % | 98.0 % | 96.5 % | 97.5 % |

Median answer under noise 3.0-3.25 s (HR ≥ 50 BPM). Validation (544): 95.0 % first within, 8 of
8 targets. Study details and the methods tried: `hr_notes/hr_estimation_final_report.md` (not in
the repository).

## 6. Integration on the PSoC 6
- **API** (`c/include/hr_api.h`, mirrored by `estimator.py`): `hr_init(state, params)` at good
  contact, `hr_process_block(state, pcm[200], out)` every 50 ms with int16 PCG (scaled
  pcm / 32768), `hr_reset(state)` on contact loss. `out`: status (measuring / reading), BPM,
  confidence, time since start, new-reading flag. Runtime parameters `hr_params_t` carry the
  `release:` knobs (cutoffs 3 / 6 / 8 s, 50 BPM boundary, thresholds; fast setting: cutoff
  2.5 s, early confidence 0.95).
- **Timing** (cost model, float32, 150 MHz): about 0.4 ms per call, and about 25 ms on every 5th
  call, which carries features, scorer and tracker. Run the call in a task that tolerates a
  25 ms burst per 250 ms, or split the frame work over the 5 calls.
- **Memory**: all state in a caller-provided `hr_state_t` (12.8 KB float32, 10.3 KB fixed
  point); no allocation. Feature memory layout: per input the 4 s history, one autocorrelation
  array and the per-state DFT magnitude at f; other features computed per state on the fly.
- **Checklist**: PCG path delivers 4000 Hz int16 in 200-sample blocks (or a buffer adapter);
  start on good contact (time to first reading is measured from `hr_init`); reset on contact
  loss; number format (float32 or fixed point) decided after the C prototype; golden vectors
  (`pcg-hr vectors`) checked stage by stage: band energies, activation, tracker output per
  frame, readings.

## 7. Cost model
`pcg-hr profile` (`cost_model.py`) counts the operations of every block per second at the worst
case (full 4 s window), maps them to Cortex-M4F cycles (`profile:` in `config.yml`: fused
multiply-add 3 cycles in float32, dual 16-bit multiply-accumulate 0.5 cycles in fixed point,
divide and square root 14, exp / log 40 as an approximation, overhead x1.3), and sizes every
buffer.

| Block | float32: cycles/s | fixed: cycles/s | RAM float32 | Flash |
|---|---|---|---|---|
| A front end | 0.71 M | 0.14 M | 80 B | 160 B |
| B TCN | 0.37 M | 0.08 M | 2.0 KB | 913 B |
| C features | 13.3 M | 4.7 M | 8.0 KB | 0 |
| D scorer | 1.47 M | 0.30 M | 1.0 KB | 465 B |
| E tracker | 0.11 M | 0.06 M | 1.7 KB | 0 |
| Total | **16.0 M (of 30)** | **5.3 M** | **12.8 KB (of 15)** | **4.5 KB incl. 3 KB code estimate (of 5)** |

Estimates, not measurements: next levels are instruction counts of the compiled C code in an
emulator, then the cycle counter on a PSoC 6 kit. The DFT at 3 multiples x 205 states x 400
samples per input dominates.

## 8. Tests
`pytest` (about 12 s): unit checks on hand-written inputs (tolerance, octave errors, separate
group, windowed ECG HR, release threshold, tracker, time warping), synthetic PCG end to end
(value and answer-time window per HR), numpy TCN equal to PyTorch, golden regression on 40 test
recordings against `output/golden/` (skipped without local data), cost model within budget.

## 9. Layout, outputs and naming
```
pyproject.toml, requirements.txt, config.yml
src/pcg_hr/      frontend, net, features, scorer, tracker, release, estimator   (device path)
                 data, reference, augment, synth, train_net, train_scorer       (training)
                 evaluate, plots, cost_model, cli                               (evaluation)
tests/           pytest suite
c/include/       hr_api.h (device API draft)
notebooks/       hr_results.ipynb (thin driver, outputs cleared)
data/, output/   local only, never committed
```

| Output | Content |
|---|---|
| `output/splits/split_v1.csv` | `output_filename`, `user_id`, `reference_bpm`, `hr_bin`, `fold` (0-9), `split` (train 0-4, val 5-6, test 7-9) |
| `output/labels/reference_v2.csv` | answer key: `efast_ecg_bpm`, `ecg_rpeak_bpm` (median of three leads), `leads_agree`, `label_source` (`efast` / `ecg_rpeaks`), `reference_bpm` |
| `output/models/hr_tcn.npz`, `hr_scorer.npz` | final weights (float32): `conv<i>_w` (out, in, 3), `conv<i>_b`, `out_w`, `out_b`; scorer `mean`, `std`, `w1` (24, 16), `b1`, `w2`, `b2` |
| `output/runs/<YYYYMMDD>_<HHMMSS>_<variant>_<split>[_<tag>][_stress]/` | `config.yml` (resolved), `per_record.csv`, `readings.json` (`[time_s, bpm, confidence]` per reading), `summary.json`, `run_info.json` (inputs, cost-model totals), `stress_by_condition.csv` (stress runs) |
| `output/golden/` | frozen final readings for the regression test |
| `output/vectors/<case>.npz` | synthetic golden vectors: `pcm` (int16), `band_log_energy`, `activation`, `trace` (per frame: time, HR, confidence, frame HR, frame confidence), `readings` |
| `output/cache/` | R-peak times per ECG lead (`rpeaks_<lead>.json`) and training caches; caches are named by a hash of the augmentation settings and the answer key, so changed inputs never reuse them |

`summary.json` keys: `first_within`, `first_within_hr_ge_low` / `_low_hr` (split at 50 BPM),
`first_half_double`, `steady_within`, `steady_mae`, `steady_bias`, `steady_loa` (1.96 x SD),
`ttfr_median_hr_ge_low_s`, `ttfr_p95_hr_ge_low_s`, `ttfr_median_low_hr_s`, `ttfr_max_s`,
`acc_at_<t>s` (share of recordings with a correct reading by t), `clean_window_n` /
`clean_window_first_within`, `sep_n` / `sep_first_within`, `targets_passed`.
