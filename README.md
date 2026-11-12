# Heart rate from heart sounds on the stethoscope MCU

Streaming heart rate (HR) estimation from the phonocardiogram (PCG, heart-sound audio, 4000 Hz,
16-bit) for the PSoC 6 Cortex-M4F. A small causal network marks heartbeats, periodicity features
measure their spacing, a small scorer and a hidden Markov model (HMM) tracker pick the rate, and
a readout decides when to show it: an accurate first reading at 2.5-4 s for regular rhythms, a
later first reading (from 4 s) for slow or irregular hearts that then converges, one reading per
second after that, and three runtime settings. A Python reference (`src/device/`) defines the
algorithm; the C port (`c/`) is verified against it.

<!-- begin:version -->
| Version | Model | Generated | Commit | Runs |
|---|---|---|---|---|
| 1.5.0rc2 | hr_v10c | 2026-11-12 | 2f814e0 | val `20261112_112328_hr_v10c_val_v1.5.0rc2`, test `20261112_112408_hr_v10c_test_v1.5.0rc2`, stress `20261112_112506_hr_v10c_test_v1.5.0rc2_stress` |

Key results, test split (703 recordings with a trusted ECG reference); all results in
[docs/results.md](docs/results.md):

| Result | Value |
|---|---|
| regular, 50-200 BPM: first reading, median / 90 % by | 2.75 s / 4.25 s |
| regular: first reading within ±3 BPM; reading within ±3 BPM at 4 s | 88.8 %; 79.2 % |
| regular: converged (every later reading within ±3 BPM), median | 3 s |
| slow, < 50 BPM: first reading, median; within ±10 BPM | 5.25 s; 100.0 % |
| irregular or AF: first reading within ±10 BPM; within ±3 BPM at 9 s | 77.0 %; 77.0 % |
| all: half/double readings; with a reading by 10 s | 0.2 %; 97.9 % |
| targets met (test and stress) | 6 of 14 |
| C port: test recordings with the Python reading times | 825 of 825 |
| C port, Cortex-M4: RAM of the state; stack, libm included; flash of code and constants without libm | 10,020 bytes (budget 10,240, within); 672 bytes (budget 1,024, within); 17,518 bytes (budget 5,120, exceeded) |
<!-- end:version -->

## Documents
| Document | Contents |
|---|---|
| [docs/tutorial.md](docs/tutorial.md) | How the algorithm works: signal-flow diagram with a legend and the data shape on every wire, time units and windows, every block (input, output, shape walk, example figure; TCN architecture, activation and S1-band windows), one heart end to end |
| [docs/data.md](docs/data.md) | Data snapshot; training, validation and test splits; ECG reference labels; augmentation; training |
| [docs/results.md](docs/results.md) | Results of this version: per group, targets, time to converge, accuracy over time, first-reading error, strata, validation, stress test, settings trade-off, corner cases |
| [docs/realtime_implementation.md](docs/realtime_implementation.md) | Real-time implementation, Python reference and C port: modules, tools, C API; integration into the firmware (files and build, memory and timing, input, display states, contact handling, validation and profiling on the target); how the C port is verified against the Python reference, with its results; resources against the MCU budget; parameters |
| [CHANGELOG.md](CHANGELOG.md) | Versions: what changed, model, key results; how to release a version |

## Contents
1. Key details
2. Quick start
3. Usage example
4. Settings
5. Layout and outputs
6. Versions

## 1. Key details
- Input: 200 int16 PCG samples every 50 ms. Output: nothing, or a reading (time, BPM,
  confidence): the first one when the estimate is likely right, then one per second while a
  heartbeat is heard.
- Regular rhythms at common rates: first reading after 2.75-4.25 s, aimed at ±3 BPM. Slow
  (< 50 BPM) or irregular hearts: first reading from 3 s within ±10 BPM, then converging.
  Only heart sounds give readings: the network is trained to stay silent on speech, knocks,
  taps, rubbing and other sounds, and a learned presence gate must hold for 1 s before the first
  reading. After the first reading, frames without heartbeat give no new reading, and 2 s of
  them (stethoscope lifted) end the measurement, which then restarts by itself. The input may
  run before the stethoscope is placed: while no heartbeat is heard the analysis restarts, so a
  placement starts like a fresh measurement. The firmware needs no contact detection.
- Trained parts: a causal network (1,009 weights), a per-rate scorer (339 parameters) and a
  presence gate (MLP, 16 hidden units); the rest is fixed signal processing. float32 (the
  per-frame ACF and NCCF as int16 codes), fixed-size state of 10.0 KB, no look-ahead.
- Reference: the ECG of the same recording (R-peak consensus of five detectors on three leads),
  never an algorithm input.
- Known limitations of this version: the regular group's first reading misses its ±3 BPM target
  (92 %), irregular rhythms and very fast (sped-up) hearts miss theirs; some non-heart sounds
  still give readings (held-out clips: speech 15 %, voice 10 %, rhythmic finger taps 30 %); the
  data is filtered on signal quality (unfiltered evaluation open); the C port exceeds the 5 KB
  flash budget. Details and the device tests: [CHANGELOG.md](CHANGELOG.md).

## 2. Quick start
Environment (run once; Python 3.11):

```bash
cd heart_rate_estimation
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .                                 # packages of requirements.txt, hr-estimation
```

Run in the activated environment (recordings under `data/`, models and results under `output/`,
both outside git):

```bash
cd heart_rate_estimation
hr-estimation prepare                            # split (created once) and ECG labels
hr-estimation prepare --negatives                # non-heart sound clips (docs/data.md §4.1)
hr-estimation train --out hr_v9                  # network, scorer, calibration, presence gate
hr-estimation evaluate --split val               # tuning split
hr-estimation evaluate --split test              # held-out split
hr-estimation evaluate --split test --stress     # noise and extreme-rate stress set
hr-estimation evaluate --data <snapshot dir>     # another snapshot, e.g. unfiltered
hr-estimation noise-eval                         # non-heart sounds, lift-offs, placements
hr-estimation c-model                            # C model files, self-test cases
pytest                                           # unit, streaming, corner, golden, C port
```

- C port: `cd c && make model && make && make selftest && make m4`; bundle for the firmware:
  `make dist` ([docs/realtime_implementation.md](docs/realtime_implementation.md) §3 and §4).
  Release procedure: [CHANGELOG.md](CHANGELOG.md).
- Packages: pinned in `requirements.txt`, which `pyproject.toml` reads. Linux without a GPU:
  `pip install torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu` before
  `pip install -e .` installs the CPU-only torch.

## 3. Usage example
```
$ hr-estimation evaluate --split test --tag v1.4.0rc1
20:10:36 FAIL regular_common.first_within_fine: value 0.89, target 0.92
20:10:36 targets passed 8/12 -> 20261111_201036_hr_v9c_test_v1.4.0rc1

$ cd c && make selftest
hr_selftest: version 1.4.0rc1, model hr_v9c a1eb297821a6 gate c70a8b, state 10020 bytes
  hr72_lift_off  16 readings  PASS
hr_selftest: PASS
```

`regular_common.first_within_fine`: share of first readings within ±3 BPM of the ECG heart rate
over the same seconds, for regular rhythms at 50-200 BPM. `make selftest`: the C port on
synthetic signals against the readings of the Python reference. The Python device code and the
C API in use:
[docs/realtime_implementation.md](docs/realtime_implementation.md) §1, §3 and §4.

## 4. Settings
Runtime settings (`configs/device.yml`, part 1; no retraining):

| Setting | Effect | Default |
|---|---|---|
| `confidence` | Regular hearts: probability the first reading must have of being within ±3 BPM. Lower: sooner and less accurate. | 0.7 |
| `slow_irregular_after_s` | Slow or irregular hearts: earliest first reading (probability within ±10 BPM of at least 0.97 until 4 s, then 0.95). | 3.0 s |
| `latest_s` | With a heart sound present: a first reading at this time when the estimate is roughly certain (posterior mass of at least `forced_min_mass` within ±10 BPM). | 8.0 s |

Their effect: [docs/results.md](docs/results.md) §10. Internal constants (rate range, slow
limit, regularity limit, certainty of the slow/irregular path, heart-sound gates) sit in part 2
(`decision`) and need re-validation when changed; all parameters with their meanings:
[docs/realtime_implementation.md](docs/realtime_implementation.md) §7.

## 5. Layout and outputs
```
configs/device.yml          what the stethoscope runs: user settings, algorithm constants
configs/training.yml        data, split, labels, augmentation, training, evaluation, targets
src/device/                 on-device algorithm (blocks A-F and R, estimator); ports to C 1:1
src/training/               data, labels, augmentation, training, evaluation, tools
                            (never on the device)
c/                          C port: include/ (API), src/ (one file per device module), tools/,
                            Makefile; build/ (generated model files, binaries) not in git
docs/                       tutorial, data, results, implementation; figures/ generated
tests/                      pytest suite
CHANGELOG.md                versions: what changed, model, key results
pyproject.toml              package and command; dependencies from requirements.txt
requirements.txt            pinned packages
models/                     model files of shipped versions: hr_v8 (1.0-1.2), hr_v9c (1.3-1.4),
                            hr_v10c (1.5) release candidates; network, scorer, calibration,
                            presence gate; other models stay out of git
data/                       recordings and non-heart sound clips (not in git)
output/                     splits, labels, runs, caches, golden readings (not in git)
```
A run folder `output/runs/<time>_<model>_<split>[_<tag>]/` holds `config.yml`,
`per_record.csv`, `readings.json`, `summary.json` (metrics per group), `targets.json`
(pass/fail) and `run_info.json` (software version, requirements checksum, git commit, model
file checksums); test and validation runs add figures and `strata.csv`, stress runs
`stress_by_condition.csv`.

## 6. Versions
Semantic versioning in `pyproject.toml`: MAJOR for a change of the C API or the output
contract, MINOR for a model or algorithm change that changes readings, PATCH for a change with
identical readings. The version is recorded in every run, in the C code (`hr_version()`) and in
the generated version table above. Releases, their key results and the release procedure:
[CHANGELOG.md](CHANGELOG.md). The approaches evaluated before 1.0.0 (autocorrelation and
GCC-PHAT front ends, end-to-end networks, alternative release rules and feature sets) are kept
on branch `arvind/prototype`.
