# Heart rate from heart sounds on the stethoscope MCU

Streaming heart rate (HR) estimation from the phonocardiogram (PCG, heart-sound audio, 4000 Hz,
16-bit) for the PSoC 6 Cortex-M4F. A small causal network marks heartbeats, periodicity features
measure their spacing, a small scorer and a hidden Markov model (HMM) tracker pick the rate, and
a readout decides when to show it: an accurate first reading at 2.5-4 s for regular rhythms, a
later first reading (from 3 s) for slow or irregular hearts that then converges, one reading per
second after that, and four runtime settings. A Python reference (`src/device/`) defines the
algorithm; the C port (`c/`) is verified against it and has the four firmware calls shared with
the combo13 algorithm.

<!-- begin:version -->
| Version | Model | Generated | Commit | Runs |
|---|---|---|---|---|
| 2.0.0 | hr_v10c | 2026-11-13 | bbf88ed | val `20261113_161950_hr_v10c_val_v2.0.0`, test `20261113_162005_hr_v10c_test_v2.0.0`, stress `20261113_162107_hr_v10c_test_v2.0.0_stress` |

Key results, test split (703 recordings with a trusted ECG reference); all results in
[docs/results.md](docs/results.md):

| Result | Value |
|---|---|
| regular, 50-200 BPM: first reading, median / 90 % by | 2.75 s / 4 s |
| regular: first reading within ±3 BPM; reading within ±3 BPM at 4 s | 88.6 %; 88.1 % |
| regular: converged (every later reading within ±3 BPM), median | 3 s |
| slow, < 50 BPM: first reading, median; within ±10 BPM | 4 s; 100.0 % |
| irregular or AF: first reading within ±10 BPM; within ±3 BPM at 9 s | 76.2 %; 77.0 % |
| all: half/double readings; with a reading by 10 s | 0.3 %; 98.3 % |
| targets met (test and stress) | 8 of 14 |
| C port: test recordings with the Python reading times | 825 of 825 |
| C port, Cortex-M4: RAM of the state, plus the input ring of the firmware calls; stack, libm included; flash of code and constants without libm | 10,068 bytes (budget 10,240, within) + 4,800 bytes; 752 bytes (budget 1,024, within); 18,343 bytes (budget 5,120, exceeded) |
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
- Input: int16 PCG at 4000 Hz, processed in blocks of 200 samples (50 ms). Output: nothing, or
  a reading (time, BPM, confidence): the first one when the estimate is likely right, then one
  per second while a heartbeat is heard. The firmware calls (`hr_push_data` in the audio
  context; `hr_process_chunk`, `hr_update_chunk`, `hr_calculate` in a task) give the value to
  show: whole BPM, smoothed, single outliers hidden.
- Regular rhythms at common rates: first reading after 2.75-4 s, aimed at ±3 BPM. Slow
  (< 50 BPM) or irregular hearts: first reading from 3 s within ±10 BPM, then converging.
  Only heart sounds give readings: the network is trained to stay silent on speech, knocks,
  taps, rubbing and other sounds, and a learned presence gate must hold for 1 s before the first
  reading. After the first reading, frames without heartbeat give no new reading, and 2 s of
  them (stethoscope lifted) end the measurement, which then restarts by itself. The input may
  run before the stethoscope is placed: while no heartbeat is heard the analysis restarts, so a
  placement starts like a fresh measurement. The firmware needs no contact detection.
- Trained parts: a causal network (1,009 weights), a per-rate scorer (339 parameters) and a
  presence gate (MLP, 16 hidden units); the rest is fixed signal processing. float32 (the
  per-frame ACF and NCCF as int16 codes), fixed-size state of 10.1 KB plus a 4.7 KB input
  ring, no look-ahead.
- Reference: the ECG of the same recording (R-peak consensus of five detectors on three leads),
  never an algorithm input.
- Known limitations of this version: the regular group's first reading misses its ±3 BPM target
  (92 %), irregular rhythms and very fast (sped-up) hearts miss theirs; some non-heart sounds
  still give readings (held-out clips: speech 15 %, voice 10 %, rhythmic finger taps 30 %);
  regular hearts often start on the slow/irregular path (the beat picker alternates between the
  activation's two pulses per beat), which delays their first reading; all training recordings
  are from DUO3, while Coral feeds the estimator after its noise canceller; the data is filtered
  on signal quality (unfiltered evaluation open); the C port exceeds the 5 KB flash budget.
  Details and the device tests: [CHANGELOG.md](CHANGELOG.md).

### What changed from h14 to h15
Device builds: h14 = 1.4.0rc1; h15 = 2.0.0 with the default settings (the readings of
1.5.0rc1). Firmware variants of 2.0.0 separate the setting from the estimator's input: h15p
(input before the Coral noise canceller), h15ba (`slow_irregular_confidence` 0.95, the value of
1.5.0rc2) and h15bp (0.95, input before the canceller). Test split unless noted; details in
[CHANGELOG.md](CHANGELOG.md).
- Finger taps: the network trains on taps of many shapes and rates (ring 20-250 Hz, with and
  without a click and a press pulse, 40-160 per min) and on synthetic hearts with S1 and S2, so
  a pair of sounds marks a beat and a single tap does not. Rhythmic taps with a reading: 81 % to
  33 % of 300 varied tap clips.
- Faint hearts (synthetic, −6 dB SNR): median first reading 8.0 to 4.0 s.
- Speed (h15): regular first reading median 3.0 to 2.75 s, 90th percentile 4.25 to 4.0 s; slow
  hearts median 4.5 to 4.0 s.
- Accuracy (h15): MAE 1.36 to 1.34 BPM; regular first reading within ±3 BPM 0.890 to 0.886;
  irregular first reading within ±10 BPM 0.787 to 0.762. With `slow_irregular_confidence` 0.95:
  MAE 1.28 BPM, irregular 0.770, slow hearts later (median 5.25 s).
- C: the estimator code is unchanged. 2.0.0 adds the four firmware calls (`hr_push_data`,
  `hr_process_chunk`, `hr_update_chunk`, `hr_calculate`; the API shared with combo13), with the
  input ring and the display rule in the library, and the runtime setting
  `slow_irregular_confidence`; state 10,068 bytes plus a 4,800-byte input ring.

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
hr-estimation train --out hr_v10c                # network, scorer, readout calibration
hr-estimation train --presence-only --weights hr_v10c   # presence gate
hr-estimation evaluate --split val               # tuning split
hr-estimation evaluate --split test              # held-out split
hr-estimation evaluate --split test --stress     # noise and extreme-rate stress set
hr-estimation evaluate --data <snapshot dir>     # another snapshot, e.g. unfiltered
hr-estimation noise-eval                         # non-heart sounds, lift-offs, placements
hr-estimation c-model                            # C model files, self-test cases
pytest                                           # unit, streaming, corner, golden, C port
```

- C port: `cd c && make model && make && make selftest && make m4`; bundle for the firmware:
  `make dist`, or the repository as a submodule (`c/src`, `c/include` and the committed model
  files in `c/generated`; [docs/realtime_implementation.md](docs/realtime_implementation.md) §3
  and §4). Release procedure: [CHANGELOG.md](CHANGELOG.md).
- Packages: pinned in `requirements.txt`, which `pyproject.toml` reads. Linux without a GPU:
  `pip install torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu` before
  `pip install -e .` installs the CPU-only torch.

## 3. Usage example
```
$ hr-estimation evaluate --split test --tag v2.0.0
16:20:06 targets passed 8/12 -> 20261113_162005_hr_v10c_test_v2.0.0

$ cd c && make selftest
hr_selftest: version 2.0.0, model hr_v10c 243cbe5b6b8b gate 865f4d, state 14868 bytes, input ring 4800
  hr72_lift_off  14 readings  PASS
hr_selftest: PASS
```

`targets passed`: acceptance targets met on the test split (each target and its value:
[docs/results.md](docs/results.md)). `make selftest`: the C port on synthetic signals against
the readings of the Python reference. The Python device code and the C API in use:
[docs/realtime_implementation.md](docs/realtime_implementation.md) §1, §3 and §4.

## 4. Settings
Runtime settings (`configs/device.yml`, part 1; no retraining):

| Setting | Effect | Default |
|---|---|---|
| `confidence` | Regular hearts: probability the first reading must have of being within ±3 BPM. Lower: sooner and less accurate. | 0.7 |
| `slow_irregular_after_s` | Slow or irregular hearts: earliest first reading (probability within ±10 BPM of at least 0.97 until 4 s, then `slow_irregular_confidence`). | 3.0 s |
| `slow_irregular_confidence` | Slow or irregular hearts from 4 s: probability the first reading must have of being within ±10 BPM. Higher: later and more accurate (0.95, the value of 1.5.0rc2: test MAE 1.34 to 1.28 BPM, slow hearts median 4.0 to 5.25 s). | 0.9 |
| `latest_s` | With a heart sound present: a first reading at this time when the estimate is roughly certain (posterior mass of at least `forced_min_mass` within ±10 BPM). | 8.0 s |

In C: `hr_settings_t`, changed at run time with `hr_set_settings`. The firmware calls' display
rule and input ring (`firmware`: `display_near_bpm` 3, `display_jump_bpm` 10, `ring_blocks` 12)
are compiled into the C model files (`hr-estimation c-model`).

Their effect: [docs/results.md](docs/results.md) §10. Internal constants (rate range, slow
limit, regularity limit, early certainty of the slow/irregular path, heart-sound gates) sit in
part 2 (`decision`) and need re-validation when changed; all parameters with their meanings:
[docs/realtime_implementation.md](docs/realtime_implementation.md) §7.

## 5. Layout and outputs
```
configs/device.yml          what the stethoscope runs: user settings, algorithm constants
configs/training.yml        data, split, labels, augmentation, training, evaluation, targets
src/device/                 on-device algorithm (blocks A-F and R, estimator); ports to C 1:1
src/training/               data, labels, augmentation, training, evaluation, tools
                            (never on the device)
c/                          C port: include/ (API), src/ (one file per device module, stream.c:
                            firmware calls), generated/ (model files, committed), tools/,
                            Makefile; build/ (self-test cases, binaries) not in git
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
at tag `archive/prototype`.
