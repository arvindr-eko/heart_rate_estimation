# Real-time implementation: Python reference and C port

How the algorithm runs in real time: block by block in the Python reference (`src/device/`),
which defines it, and in the C port for the stethoscope (`c/`); the tools around them
(`src/training/`); how to integrate the C port into the firmware and check it on the target;
how the C port is verified against the Python reference, with its results; and the measured
resources against the MCU budget. How the algorithm works: [tutorial.md](tutorial.md). Version:
[README](../README.md). Tables between the generated markers come from the configs, the C build
and the release's test run (README version table).

## Contents
1. Python reference (`src/device/`)
2. Tools (`src/training/`)
3. C port (`c/`)
4. Integration into the firmware
5. Verification of the C port
6. Resources
7. Parameters

## 1. Python reference (`src/device/`)
The algorithm the stethoscope runs, as the reference for the C port. Each module is one block:
a state dataclass, `init` and a per-call function. State is float32 with sizes fixed at `init`;
weights are loaded at `init`; no other file access. `device` never imports `training` (tested).

```python
from device import estimator
from training import augment, config

cfg = config.load()                                     # configs/device.yml + training.yml
audio, _ = augment.synthetic_pcg(72, duration_s=10.0)   # or a recording: float, 4000 Hz, [-1, 1]
state = estimator.init(cfg)                             # all block states; init: new measurement
block = cfg["stream"]["block_samples"]                  # 200 samples = 50 ms
for start in range(0, audio.size - block + 1, block):
    reading = estimator.process(state, audio[start:start + block])
    if reading is not None:                             # (t_s, bpm, confidence)
        print("t %.2f s  %.1f BPM  confidence %.2f" % reading)
```
```
t 3.75 s  71.7 BPM  confidence 0.97
t 4.75 s  72.0 BPM  confidence 0.98
...
t 9.75 s  72.8 BPM  confidence 0.98
```

| Module | Block | Called | In → out | State |
|---|---|---|---|---|
| `estimator.py` | all | `init(cfg)`; `process(state, block)` every 50 ms; `run(cfg, audio)` for a whole recording | 200 samples → reading `(t_s, bpm, confidence)` or `None` | all block states, block counter |
| `frontend.py` | A | `process(state, block)` per block | 200 samples → (8, 5): 4 heart-band log energies, 2 upper-band ratios, voicing, impulsiveness | 6 bands x 2 biquads x 2 values |
| `quality.py` | Q | `update(state, cfg, frame)` per 10 ms | band values → 200-400 Hz / 25-100 Hz energy ratio | two energy averages |
| `net.py` | B | `normalise(state, frame)`, `step(state, x)` per 10 ms | 8 front-end values → activation in [0, 1] | running mean (4 heart bands), one delay line per layer |
| `features.py` | C | `push(state, x)` per sample; `compute(state)` per frame; `hr_grid(cfg)` | activation or 25-50 Hz band → (232, 13) per frame | ring buffer and lag sums: 400 (activation), 100 (S1 band) |
| `scorer.py` | D | `log_emission(model, feats)` per frame | (232, 17) → log-likelihood (232) | weights only |
| `tracker.py` | E | `update(state, log_emission)`, `estimate(state)` per frame | (232) → posterior (232), HR | posterior (232) |
| `rhythm.py` | R | `analyse(window, rate_hz, hr, cfg)` per frame | 4 s of activation → interval CV, beat-count HR | none |
| `readout.py` | F | `assess`, `presence`, `first_due`, `schedule`, `lost` per frame | posterior, HR, gates, presence, CV → reading or none; loss of the heartbeat | times of the first and the latest reading, frames with and without heartbeat |

`estimator.process` runs A, B and the per-sample update of C on every block; every fifth block
also `scorer_input` (C: 13 activation features, 3 S1-band features, flag), D, E, the two gate
values (`periodicity`, `activation_sd`), R and F. After the first reading, frames without
heartbeat (`readout.heart_absent`) give no new reading, and `lost_after_s` (2 s) of them end the
measurement (`readout.lost`, then `estimator.restart`): readings stop until the next first
reading (§4.5). Before the first reading, `idle_s` (2 s) with the activation below
`idle_activation` restart the analysis every frame (`estimator.restart_analysis`; front end
and network keep running).

## 2. Tools (`src/training/`)
Everything around the device code: data, ECG labels, training, evaluation and tools; runs on a
workstation or SageMaker, never on the stethoscope. One command, `hr-estimation`
(`training/cli.py`), one module call per step, run inside the project environment
([README](../README.md) §2). Data, labels, augmentation and training:
[data.md](data.md).

| Step | Command | Modules | Writes |
|---|---|---|---|
| 1. Split and labels | `hr-estimation prepare` | `data` (split by clinician account, once), `labels` (ECG R-peak consensus) | `output/splits/split_v1.csv`, `output/labels/ecg_labels.csv` |
| 1b. Non-heart sounds | `hr-estimation prepare --negatives` | `data` (LibriSpeech, FSD50K; [data.md](data.md) §4.1) | `data/negatives/manifest.csv`, `clips/` |
| 2. Train | `hr-estimation train --out NAME` | `train`, `augment`; scorer inputs streamed through `device` | `models/NAME_tcn.npz`, `_scorer.npz`, `_calibration.npz`, `_presence.npz` |
| 3. Select the model | `weights: NAME` in `configs/device.yml`, or `--weights NAME` per command | | |
| 4. Tune on validation | `hr-estimation evaluate --split val` | `evaluate` (every recording streamed through `device.estimator`) | `output/runs/<time>_NAME_val/` |
| 5. Test, once per candidate | `hr-estimation evaluate --split test`, then with `--stress` | `evaluate`, `augment` (stress conditions) | `output/runs/<time>_NAME_test[_stress]/` |
| 6. Other snapshot | `hr-estimation evaluate --data <dir>` | `labels.external`, `evaluate` | `output/runs/<time>_NAME_ext_<dir>/` |
| 6b. Non-heart sounds | `hr-estimation noise-eval`; `--export DIR` writes the placement cases for a firmware bench | `evaluate`, `augment` (evaluation part of the negatives) | `output/runs/<time>_NAME_noise/summary.json` |
| 7. Golden readings | copy the test run's `readings.json` to `output/golden/NAME_test_readings.json` | read by `tests/test_regression.py` | |
| 8. C port files | `hr-estimation c-model` (or `make model` in `c/`) | `c_model`, `c_port` (self-test cases) | `c/build/generated/`: model files, self-test cases |

What a change needs:
- `readout` settings: nothing; evaluate on validation for the trade-off.
- `decision`, `tracker`, `rhythm`, `scorer.emission_beta`: `train --calibrate-only` (the
  calibration depends on the posterior), then evaluate on validation.
- S1-band or scorer settings: `train --scorer-only --weights BASE --out NEW` (keeps the network
  of `BASE`).
- `frontend`, `net`, `features`, `grid`: `train --out NEW` (network and scorer depend on them).

| Module | Role |
|---|---|
| `config.py` | Loads and merges the two configs, resolves paths, preflight checks, software version |
| `data.py` | Manifest, grouped split by clinician account, WAV loading, parallel map over recordings, non-heart sound manifest |
| `labels.py` | ECG R-peaks (five NeuroKit2 detectors, three leads), reference HR per reading, other snapshots |
| `augment.py` | Synthetic PCG, noise at a set SNR (synthetic and recorded), device channel, time warping and speed-up (training and stress), no-heart clips and lift-offs |
| `train.py` | Network (PyTorch, exported to numpy; teacher distillation, non-heart clips), scorer, readout calibration, presence gate |
| `evaluate.py` | Streaming evaluation, grading per group, targets, stress protocol, figures, run folder; non-heart, lift-off, swap, overlap and placement evaluation |
| `c_model.py` | C sizes, coefficients and weights from the config and the model files |
| `c_port.py` | Synthetic test signals; every block's output of the Python reference and of the C port (through ctypes) and their comparison; the firmware calls against `hr_process_block` and the reference display rule (`compare_stream`); self-test cases (`write_selftest`) |
| `cli.py` | The `hr-estimation` command |

## 3. C port (`c/`)
C99 implementation of `src/device` for the PSoC 6: float32, no dynamic memory, no recursion, all
state in one caller-owned `hr_state_t`. One C file per Python module, plus `stream.c` for the
firmware calls. Sizes, coefficients and weights are generated from `configs/device.yml` and the
model files (`hr-estimation c-model`): `c/generated/hr_model.h` and `hr_model.c` (35 KB,
committed, so firmware builds the port without Python; regenerated by every C test run, so
`git status` shows when they no longer match the config and models) and the self-test cases
`c/build/generated/hr_selftest_cases.h` (not in git).

| Python (`src/device/`) | C (`c/src/`) |
|---|---|
| `frontend.py` | `frontend.c` |
| `quality.py` | `quality.c` |
| `net.py` | `net.c` |
| `features.py` | `features.c` |
| `scorer.py` | `scorer.c` |
| `tracker.py` | `tracker.c` |
| `rhythm.py` | `rhythm.c` |
| `readout.py` | `readout.c` |
| `estimator.py` | `estimator.c`, the API of `c/include/hr.h` |
| `readout.py` (`display_step`) | `stream.c`: input ring and display value of the firmware calls |

Build and test:

```bash
cd c
make model                     # generated model files, self-test cases (hr-estimation c-model)
make                           # build/libhr_debug.* (tests), build/hr_run (host tool)
make selftest                  # build/hr_selftest on this computer: PASS or FAIL (§4.7)
make m4                        # Cortex-M4 objects: code size per module, state size, stack
make dist                      # bundle for the firmware: build/hr_c_<version>.tar.gz (§4.1)
cd .. && pytest tests/test_c_port.py
```

| Call (`c/include/hr.h`) | When | Does |
|---|---|---|
| `hr_init(&s, settings)` | start: power-up, new patient | clears all state; `NULL` settings: the defaults |
| `hr_push_data(&s, pcm, n)` | audio context, every audio frame, under the audio lock | queues n samples; true when a 50 ms block is complete (wake the processing task); `pcm` NULL: n samples missing, new measurement after them |
| `hr_process_chunk(&s)` | processing task, no lock | runs the estimator on every complete block, in order |
| `hr_update_chunk(&s)` | processing task, under the audio lock | releases the processed blocks |
| `hr_calculate(&s, &reading)` | processing task, after `hr_update_chunk` | 1 and the shown value (whole BPM, display rule of §4.4) while a reading is shown; 0 otherwise |
| `hr_process_block(&s, pcm, &reading)` | instead of the four calls above, from one context: every 50 ms, 200 int16 samples | 1 when a reading is due: the first when confident (or at `latest_s`), then 1 per s while a heartbeat is heard; restarts the measurement by itself when the heartbeat is lost (§4.5) |
| `hr_get_reading(&s, &reading)` | any time | latest reading; 0 before the first and after a restart |
| `hr_get_frame_info(&s, &info)` | any time | estimate (also before the first reading), gate values, rhythm, probabilities, phase (warm-up, searching, reading), heart present |
| `hr_set_settings(&s, &settings)`, `hr_get_settings` | any time | the four runtime settings, validated; used from the next frame |
| `hr_reset(&s)` | on request: new patient or site | new measurement, same settings |
| `hr_version`, `hr_model_id`, `hr_get_posterior`, `hr_state_size` | any time | software version; model name and weight checksums; posterior over the 232 rates; state size |

The API is shared with the combo13 algorithm (same `hr.h` on its branch), so firmware can switch
between the two; the combo13 reading fields are filled as described in `hr.h`. The API in use,
with the display states: §4.4.

Host tool on a synthetic 72 BPM signal (raw int16 PCM; times from the start of the file; real
output):

```
$ c/build/hr_run hr72.raw
version 2.0.0, model hr_v10c 243cbe5b6b8b gate 865f4d, state 14868 bytes, 10.0 s of input
t 3.50 s  72.0 BPM  confidence 0.97  first
...
t 9.50 s  71.3 BPM  confidence 0.98
```

Implementation choices, all equivalent to the Python code up to float32 rounding:
- delay lines and windows are ring buffers, so nothing is shifted per sample;
- the lag functions take one pass over the lags with running head and tail sums (the Python
  code uses prefix-sum arrays, which would need 3.2 KB more RAM);
- the window's per-sample update removes the oldest sample's pair and adds the new one in one
  pass over the lags (the same two operations per lag as the reference's two passes);
- YIN is kept only at the states' periods, interpolated during the lag pass and held in the
  score buffer until each state's features are read; the ACF at a state's period is
  interpolated when needed instead of stored per state;
- the ACF peaks are found while the states are scored (rates rising, lags falling), from the
  mean-removed lag sums rebuilt on the way down, instead of a stored list of peaks;
- the scorer's input standardisation is folded into its first layer (no divisions);
- per-frame buffers (ACF and NCCF as int16 codes, scores, tracker prior, picked beats) share
  one scratch area;
- times are compared in blocks, exact for thresholds on the 50 ms grid.

## 4. Integration into the firmware
The firmware pushes the PCG samples with `hr_push_data` and shows what `hr_calculate` returns.
The estimator decides when a reading is ready, updates it every second while a heartbeat is
heard, and ends the measurement by itself when the heartbeat is lost: the firmware needs no
contact detection. The display rule (whole BPM, smoothed, single outliers hidden) is part of
the library, so the firmware shows the value as it comes.

### 4.1 Files and build
Bundle: `cd c && make model && make dist` → `c/build/hr_c_<version>.tar.gz` with the files
below, a `README.md` (version, model, commit, build date, link to this guide, self-test command)
and `SHA256SUMS`. It holds synthetic test signals only, no recorded data. `make dist` runs the
self-test first.

| File | Role |
|---|---|
| `include/hr.h` | API; the only header the firmware needs |
| `include/hr_blocks.h` | state layout and block functions (fields private); included only to allocate the state statically |
| `src/*.c` (10 files) | the estimator and the firmware calls (`stream.c`) |
| `generated/hr_model.h`, `generated/hr_model.c` | sizes, coefficients and weights of this version |
| `tools/hr_selftest.c`, `generated/hr_selftest_cases.h` | self-test (§4.7); test firmware only |
| `tools/hr_run.c` | host tool: a raw PCM file through the C port; readings, time per call |

Product build: compile `src/*.c` and `generated/hr_model.c` with the include paths `include/`
and `generated/`; link the C math library (`expf`, `logf`, `floorf`, `ceilf`). The repository
can serve as a git submodule instead of the bundle: the same files under `c/` (`c/src`,
`c/include`, `c/generated`), with `c/tools` and `c/build` excluded from the build. Flags of the
verified build:

| Flag | Reason |
|---|---|
| `-std=c99` | C99 without compiler extensions |
| `-mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard` | float32 on the single-precision FPU; no double arithmetic in the code (checked with `-Wdouble-promotion`) |
| `-ffp-contract=off` | no fused multiply-add: the rounding of the comparisons in §5 and of the self-test |
| `-fno-math-errno` | `sqrtf` and `fabsf` as FPU instructions |
| `-Os` or `-O2` | sizes in §6 are for `-Os` |
| never `-ffast-math` | it removes the NaN tests; NaN marks undefined values (no estimate yet) |

Built so far with Apple clang on the host (`-Wall -Wextra -Werror -pedantic
-Wdouble-promotion`) and with clang for `thumbv7em-none-eabihf` (compile only, for sizes and
stack); not yet with the product toolchain or on the target (§4.7).

### 4.2 Memory and timing
- State: one `hr_state_t` per measurement, owned by the caller: static (with `hr_blocks.h`) or
  allocated once with `hr_state_size()` bytes, never on a task stack; size in §6. It holds the
  input ring of `firmware.ring_blocks` (12) blocks of 400 bytes, 0.6 s of input for a
  processing task woken at least every 0.5 s. The library has no heap and no global variables,
  so two states in two tasks are independent.
- Stack: §6 gives the deepest call path from `hr_process_chunk` (which calls
  `hr_process_block`), from the call graph and the
  frames of the Cortex-M4 build, with 32 bytes for each libm or compiler-runtime call (newlib's
  `expf` and `logf` for the Cortex-M4F at `-Os` take 16-32 bytes); budget 1 KB. Step 3 of §4.7
  measures it on the target, with the product's libm.
- Flash: code and constants in §6. The constants are float32 weights and coefficients; at 8 bits
  they would take about a quarter of their size (derived, not built).
- Timing: one call every 50 ms (200 samples at 4000 Hz). Every fifth call (a frame block, every
  0.25 s) also computes the features, scores, tracker and readout and takes several times longer
  than the other four (front end and network). Host times in §6; Cortex-M4 cycles from the
  self-test (§4.7).
- Context: `hr_push_data` runs in the audio context (task or callback) under a lock that
  `hr_update_chunk` also takes; every other call runs in one processing task, outside that
  lock except `hr_update_chunk`. The ring has one writer and one reader: `hr_push_data` writes
  only free blocks, `hr_process_chunk` reads only complete ones. Waking the processing task when
  `hr_push_data` returns true (each 50 ms block) shows each reading as soon as it is due; a
  task woken on a fixed tick adds up to one tick to the time to the first reading.
  `hr_process_block` instead: one task makes every call.

### 4.3 Input
- Mono PCG, int16, 4000 Hz, through the same signal chain as the app recordings the model was
  trained and tested on (microphone path, filters, sample rate). The level does not matter
  (−40 dB with a DC offset: the same readings within 0.5 BPM, [results.md](results.md) §11);
  another filter setting or sample rate needs re-validation.
- `hr_push_data`: any number of samples per call, in order, without gaps or overlap; the ring
  forms the blocks of `HR_BLOCK_SAMPLES` (200). `hr_process_block`: blocks of exactly 200.
- A gap in the input (an audio frame that could not be read: `hr_push_data(&s, NULL, n)`; or the
  ring full because the processing task fell 0.6 s behind) drops that input, and the next
  complete block starts a new measurement. After a change of the signal chain: `hr_reset`.

### 4.4 Output and display
`hr_calculate` returns 1 with the shown value while a reading is shown, 0 otherwise. The shown
value follows the readings of `hr_process_block` after every block (`stream.c`, reference
`readout.display_step`; config `firmware`):
- nothing outside the phase `HR_PHASE_READING` (`hr_get_frame_info`);
- a reading in whole BPM (half up, at most 255 for the 8-bit BLE field); the first one of a
  measurement as is, one within `display_near_bpm` (3) of the shown value as is;
- farther: the shown value moves halfway, at least `display_near_bpm`, at most
  `display_jump_bpm` (10) per reading; a reading more than `display_jump_bpm` away only once
  the next reading agrees with it within `display_jump_bpm`, so a single outlier is never shown.

| Phase | When | Display |
|---|---|---|
| `HR_PHASE_WARMUP` | first second of a measurement (no features yet); also the frame that ended a measurement | measuring |
| `HR_PHASE_SEARCHING` | estimate updated every 0.25 s; first reading not yet due | measuring |
| `HR_PHASE_READING` | from the first reading on; a new reading every second while a heartbeat is heard, otherwise the latest one stays | the shown value |

The estimator in the firmware (`show_bpm`, `show_measuring`, `wake_hr_task`, `audio_lock` and
`audio_unlock` stand for the firmware's own functions):

```c
#include "hr.h"
#include "hr_blocks.h"                                    /* static state */

void show_bpm(int bpm);
void show_measuring(void);
void wake_hr_task(void);                                  /* e.g. a task notification */
void audio_lock(void);
void audio_unlock(void);

static hr_state_t hr;                                     /* caller-owned, static */

/* Power-up or new patient: new measurement, default settings. */
void hr_start(void)
{
    hr_init(&hr, NULL);
}

/* Audio context, every audio frame (any number of samples, in order; NULL: frame lost). */
void on_audio(const int16_t *pcm, int n)
{
    audio_lock();
    const bool block_complete = hr_push_data(&hr, pcm, n);
    audio_unlock();
    if (block_complete)
        wake_hr_task();                                   /* every 50 ms */
}

/* Heart rate task, on every wake-up: estimator, then display. */
void hr_task_step(void)
{
    hr_reading_t r;
    hr_process_chunk(&hr);                                /* estimator work, no lock */
    audio_lock();
    hr_update_chunk(&hr);
    audio_unlock();
    if (hr_calculate(&hr, &r))
        show_bpm((int)r.bpm);                             /* whole BPM, display rule applied */
    else
        show_measuring();                                 /* no reading, or after a restart */
}
```

| Reading field | `hr_calculate` | `hr_process_block` |
|---|---|---|
| `bpm` | shown value, whole BPM; -1 when none | heart rate (float) |
| `bpm_valid` | 1 while a value is shown | 1 |
| `raw_candidate_bpm`, `raw_candidate_valid` | latest reading before the display rule; a reading was given in this measurement | the reading; 1 |
| `confidence` | of the latest reading | calibrated probability that the reading is within ±3 BPM (regular path) or ±10 BPM (slow or irregular path) |
| `path` | of the latest reading | `HR_PATH_REGULAR`, or `HR_PATH_SLOW_IRREGULAR`: estimate below 50 BPM or irregular beat intervals |
| `first` | of the latest reading | 1 for the first reading of a measurement |
| `t_s` | seconds since the measurement started, up to the latest processed block | seconds since the measurement started |
| `is_flatline`, `harmonic_confirmed` | 0, -1 (combo13 fields) | 0, -1 |

Time to the first reading per rhythm: README key results and [results.md](results.md).

### 4.5 Contact handling
- No reading without heart sounds: silence and mains hum alone give none (tested); the network
  is trained to stay silent on other sounds, and a learned presence gate must hold for 1 s
  before the first reading. Some sounds can still give one (held-out clips: speech 15 %,
  voice 10 %, rhythmic finger taps 30 %; `hr-estimation noise-eval`, CHANGELOG 1.5.0rc1).
- After the first reading, a frame without heartbeat has the periodicity of the activation at
  the estimate below `lost_periodicity` (0.15) or its standard deviation (SD) below
  `min_activation_sd` (0.03). Such frames give no new reading (the latest one stays on show).
  `lost_after_s` (2 s) of them in a row end the measurement: readings stop, `hr_get_reading`
  returns 0, the phase returns to `HR_PHASE_WARMUP`, and a new measurement starts by itself,
  with a new first reading (`first` = 1) once a heart is heard again. The loss level lies below
  the first-reading gate (0.2) so that irregular rhythms keep their readings; rarely, mostly
  with atrial fibrillation (AF), a measurement still restarts with the stethoscope on the chest
  (CHANGELOG 1.0.0).
- The analysis window (4 s) holds heartbeats for a while after a lift-off, so the last reading
  comes some seconds after it (table). Firmware that detects contact itself can call `hr_reset`
  when contact is lost to end the readings at once.
- Continuous input (since 1.4.0): the estimator may run before the stethoscope is placed. Before
  the first reading, after `idle_s` (2 s) with the beat activation below `idle_activation`
  (0.4), the analysis (features, tracker, readout, measurement clock) restarts every frame until
  the activation rises; front end and network keep running. A placement therefore starts the
  analysis as a fresh start does, without `hr_reset` or contact detection in the firmware.
  After a gap in the input the firmware calls restart the measurement themselves (§4.3).
  Placement test:
  `hr-estimation noise-eval` (placement section) and `noise-eval --export DIR` (int16 cases
  and Python readings for a firmware bench).
- `heart_present` (frame info) is 1 while a frame passes the first-reading gates (periodicity
  ≥ 0.2, activation SD ≥ 0.03), e.g. for a weak-signal hint.

<!-- begin:contact -->
Synthetic: 8 s of a 72 BPM heart, 10 s of silence or noise at the level of the heart sounds
(stethoscope lifted), then the heart again; 10 noise draws per kind of noise.

| Gap | Measurement ended in the gap | Last reading after the lift-off, median / largest | Largest error of the readings after the lift-off (BPM) | Cases with readings without the heart | First reading after the return, median | Largest error of that reading (BPM) |
|---|---|---|---|---|---|---|
| silence | 1 of 1 | 0.75 s / 0.75 s | 0.1 | 0 | 2.75 s | 0.5 |
| white | 10 of 10 | -0.25 s / -0.25 s | 0.0 | 0 | 2.25 s | 0.6 |
| breath | 10 of 10 | -0.25 s / -0.25 s | 0.0 | 0 | 2.875 s | 1.3 |
| friction | 10 of 10 | -0.25 s / 0.75 s | 0.1 | 0 | 4.25 s | 1.6 |
| hum | 10 of 10 | 0.75 s / 1.75 s | 0.6 | 0 | 2.5 s | 0.2 |
<!-- end:contact -->

Readings without the heart come from noise passing for heartbeats (friction); they can go on
after the heart returns, so in those cases the first reading after the return can be wrong.

Host tool on the lift-off case of the C comparison (heart sounds 0-10 s, white noise 10-18 s,
heart sounds again from 18 s; real output):

```
$ c/build/hr_run hr72_lift_off.raw
t 3.50 s  72.0 BPM  confidence 0.97  first
...
t 9.50 s  71.3 BPM  confidence 0.98
t 21.50 s  72.2 BPM  confidence 0.97  first
```

### 4.6 Settings and logging
- `hr_set_settings`: the four runtime settings ([README](../README.md) §4), checked
  (`HR_ERR_ARG`, settings unchanged, when out of range) and used from the next frame;
  `hr_get_settings` reads them back.
- Log `hr_version()` and `hr_model_id()` (model name, weight checksums) once at start, so field
  data maps to a release; per shown value `t_s`, `bpm`, `raw_candidate_bpm`, `confidence`,
  `path`, `first`; for debugging, the frame info every 0.25 s.

### 4.7 Validation and profiling on the target
| Step | How | Pass |
|---|---|---|
| 1. Host build | repository: `cd c && make selftest`; bundle: `cc -std=c99 -O2 -ffp-contract=off -Iinclude -Igenerated -DHR_SELFTEST_MAIN tools/hr_selftest.c src/*.c generated/hr_model.c -lm -o hr_selftest && ./hr_selftest` | `hr_selftest: PASS` |
| 2. Self-test on the target | `tools/hr_selftest.c` in a test firmware with the product compiler and flags; `HR_SELFTEST_PRINTF` set to the console print function, `HR_SELFTEST_CPU_HZ` to the core clock; `hr_selftest()` called from the task that will run the estimator | PASS for every case; cycles per call and CPU load printed |
| 3. Stack | RTOS stack high-water mark of that task after step 2 | within the task's stack, with margin |
| 4. Time per block | largest frame-block time of step 2, with the task's other work | within the 50 ms block period |
| 5. Input chain | record the samples passed to `hr_push_data` in one session, and the app recording of the same session | the same signal (rate, level, filters) up to a time shift |
| 6. Target against host | replay the recorded samples on a computer: `c/build/hr_run FILE` | the device's shown values equal the display rule (§4.4) applied to `hr_run`'s readings, in the same order (`hr_run` counts time from the start of the file, `t_s` from the start of each measurement) |

The self-test runs the cases of `hr_selftest_cases.h` through the public API: a synthetic 45
BPM heart (10 s), silence, and a 72 BPM heart with a lift-off (the lift-off case of §5: 28 s;
the first reading, the pause and the restart). Every reading must come at the same block as in
the Python reference, with the same first-reading flag, BPM within 0.2 and confidence within
0.05 (the tolerance of the test recordings, §5; on the host the differences stay below 0.005);
then the API checks (argument checks, reset, settings kept on error). The signals take 304 KB of
flash, in the test firmware only. Time per call: DWT cycle counter on Cortex-M3/M4/M7/M33, or
`HR_SELFTEST_TICKS()`. Output on a host computer (on the target: cycles):

```
$ ./hr_selftest
hr_selftest: version 2.0.0, model hr_v10c 243cbe5b6b8b gate 865f4d, state 14868 bytes, input ring 4800
  hr45_slow       7 readings  PASS
  silence         0 readings  PASS
  hr72_lift_off  14 readings  PASS
  api                         PASS
hr_selftest: PASS
```

## 5. Verification of the C port
`tests/test_c_port.py` (through `training/c_port.py`) builds the debug library, streams the same
int16 PCM through both implementations and compares them:
- Synthetic heartbeat cases (`training/c_port.py`): every block's output within absolute
  float32 tolerances, the same path in every frame, the same readings. The int16 ACF and NCCF
  codes of the two implementations can be one step apart where a value lies at a rounding
  boundary; that moves the log-likelihoods by up to about 1e-3 (tolerance 2e-3). A binary
  feature (ACF local peak, validity of the 2T or 3T lag) may flip where its two compared values
  tie to rounding level; the log-likelihoods of those states are not compared.
- Synthetic cases without a heart sound (silence, hum alone): the front end, the activation, the
  activation SD and the readings (none). Features of a near-flat window are rounding noise in
  both implementations and are not compared.
- Synthetic lift-offs (`hr72_lift_off`: 10 s of heart sounds, 8 s of white noise at their
  level, 10 s of heart sounds; `hr72_silent_gap`: 10 s of digital zeros instead): compared as
  the cases without a heart sound (front end, activation, activation SD), plus the frame times
  (the restart resets them) and the readings, including the new first reading after the
  return: the same times, BPM within 0.2 and confidence within 0.05 (frames of the gap are
  rounding-sensitive in both implementations).
- All synthetic cases: the same heart-present flag in every frame.
- Every test recording: the C readings against the Python readings of the test run: the same
  reading times, BPM within 0.2 and confidence within 0.05.
- Self-test (`make selftest`, §4.7): the C port through its public API against the Python
  readings of three synthetic cases.
- Long session (`c_port.long_session`, table below): 30 min through both implementations; the
  agreement, the accuracy at the start and at the end, and the drift of the lag sums, which are
  updated per sample.

The comparison found one bug in the C port (the frame's best rate taken after the evidence
weighting) and two weaknesses of the reference: the flat-window test on rounding noise, and
near-flat windows (activation SD below 0.01, e.g. after seconds of digital silence), where the
mean removal from the lag sums cancels and the periodicity left [-1, 1]; such windows now give
no periodicity. All are fixed in this version, in both implementations. On the target: the
self-test (§4.7).

<!-- begin:c_port -->
| Case | Band log energy | Activation | Features | Log-likelihood | Posterior | Estimate (BPM) | Binary-feature flips | Readings (Python / C) | Same times | Same heart-present flags |
|---|---|---|---|---|---|---|---|---|---|---|
| hr72 | 5.8e-04 | 1.2e-05 | 2.1e-04 | 4.1e-04 | 2.4e-05 | 7.6e-05 | 0 | 7 / 7 | yes | yes |
| hr45_slow | 5.5e-04 | 1.0e-05 | 2.3e-04 | 5.1e-04 | 1.9e-05 | 5.7e-05 | 0 | 7 / 7 | yes | yes |
| hr150 | 5.4e-04 | 1.1e-05 | 5.7e-05 | 6.9e-04 | 1.7e-05 | 2.1e-04 | 0 | 8 / 8 | yes | yes |
| hr250_fast | 5.2e-04 | 8.6e-06 | 2.1e-04 | 1.3e-03 | 4.6e-05 | 4.7e-04 | 1 | 7 / 7 | yes | yes |
| hr110_loud_s2 | 3.9e-04 | 1.0e-05 | 6.9e-05 | 6.6e-04 | 1.7e-05 | 1.6e-04 | 0 | 7 / 7 | yes | yes |
| hr80_irregular | 4.7e-04 | 9.1e-06 | 1.3e-04 | 4.7e-04 | 1.5e-05 | 1.7e-04 | 0 | 2 / 2 | yes | yes |
| silence | 0.0e+00 | 2.1e-07 | not compared | not compared | not compared | not compared | - | 0 / 0 | yes | yes |
| hum_only | 3.5e-05 | 4.2e-06 | not compared | not compared | not compared | not compared | - | 0 / 0 | yes | yes |
| hr72_lift_off | 5.8e-04 | 1.3e-05 | not compared | not compared | not compared | not compared | - | 14 / 14 | yes | yes |
| hr72_silent_gap | 5.8e-04 | 1.2e-05 | not compared | not compared | not compared | not compared | - | 16 / 16 | yes | yes |

| Measure | Value |
|---|---|
| test recordings with identical reading times | 825 of 825 |
| readings compared | 11415 |
| BPM difference, median / 99th percentile / largest | 1.3e-04 / 3.8e-03 / 0.378 |
| confidence difference, 99th percentile / largest | 4.3e-04 / 0.011 |

Long session: 30 min of a synthetic 72 BPM heart with white noise at 20 dB SNR, through both
implementations.

| Measure | Value |
|---|---|
| readings; C reading times identical | 1798; yes |
| largest difference C against Python: BPM / confidence | 2.1e-03 / 6.5e-06 |
| restarts of the measurement | 0 |
| mean absolute error against 72 BPM: first / last 5 min | 0.51 / 0.59 BPM |
| lag sums: largest difference from a float64 computation, relative to lag 0 | 1.3e-05 |
<!-- end:c_port -->

## 6. Resources
<!-- begin:resources -->
Measured on the C build (`make -C c m4`):

| Measure (Cortex-M4, -Os) | Bytes |
|---|---|
| code: estimator.c | 2601 |
| code: features.c | 3216 |
| code: frontend.c | 960 |
| code: net.c | 624 |
| code: quality.c | 252 |
| code: readout.c | 862 |
| code: rhythm.c | 788 |
| code: scorer.c | 312 |
| code: stream.c | 728 |
| code: tracker.c | 660 |
| code, total | 11003 |
| constants: float32 coefficients and weights (hr_model.c) | 7340 |
| flash, total without libm | 18343 |
| RAM: state without the input ring (hr_state_t) | 10068 |
| RAM: input ring (firmware calls) | 4800 |
| RAM: stack, deepest call path (hr_process_chunk → hr_process_block → hr_frontend_process → __aeabi_memclr4) | 752 |

Budget (`training/c_port.py`): state without the input ring 10240 bytes, stack 1024 bytes (each libm
or compiler-runtime call counted as 32 bytes, newlib's expf and logf measured), flash 5120 bytes,
CPU 30 M cycles/s on average (measured on the target with the self-test). libm (expf, logf, floorf,
ceilf) adds to flash when linked.

Time per call on the host computer (`c/build/hr_run`, 60 s synthetic input x 10; host timing, not
Cortex-M4 cycles):

| Call (host, -O2) | Mean (µs) | Largest (µs) |
|---|---|---|
| ordinary block | 9.9 | 50.0 |
| frame block | 29.5 | 56.0 |
<!-- end:resources -->

## 7. Parameters
`configs/device.yml`; meanings are the config's own comments.

<!-- begin:device_parameters -->
| Section | Setting | Value | Meaning |
|---|---|---|---|
| readout | confidence | `0.7` | regular hearts: how sure before the first reading (probability of being within ±3 BPM, measured on validation); lower: sooner |
| readout | slow_irregular_after_s | `3.0` | slow or irregular hearts: first reading not before this time (decision.slow_irregular_early_confidence until decision.slow_irregular_relaxed_s, then slow_irregular_confidence) |
| readout | slow_irregular_confidence | `0.9` | slow or irregular hearts: how sure before the first reading from decision.slow_irregular_relaxed_s (probability of being within ±10 BPM); higher: later, more accurate (1.5.0rc2: 0.95) |
| readout | latest_s | `8.0` | heart sound present: a first reading at this time when the estimate is roughly certain (decision.forced_min_mass) |
| stream | fs_hz | `4000` | PCG sample rate |
| stream | block_samples | `200` | 50 ms per input block |
| stream | frame_blocks | `5` | tracker update every 5 blocks = 0.25 s |
| stream | output_period_s | `1.0` | one reading per second after the first |
| frontend | bands_hz | `[[25, 50], [50, 100], [100, 200], [200, 400], [400, 800], [800, 1600]]` | band-passes |
| frontend | heart_bands | `4` | first bands (25-400 Hz): log energy, running mean removed; bands above: ln(E band / E heart bands) |
| frontend | filter_order | `2` | Butterworth order per band edge |
| frontend | env_rate_hz | `100` | one value per input every 10 ms |
| frontend | source_features | `true` | voicing and impulsiveness as TCN inputs |
| frontend | voicing_from_hz | `100` | source signal: sum of the bands from here up |
| frontend | voicing_lag_ms | `[2.5, 12.0]` | voicing: highest NCCF over these lags (83-400 Hz pitch) |
| frontend | impulse_window_ms | `1.0` | impulsiveness: ln(peak energy over this / mean), 10 ms |
| net | norm_tau_s | `1.0` | running-mean time constant of the input |
| net | channels | `8` | channels of every layer |
| net | kernel | `3` | taps per causal convolution |
| net | dilations | `[1, 2, 4, 8, 16]` | receptive field 63 frames = 0.63 s |
| features | window_s | `4.0` | longest analysis window |
| features | min_window_s | `1.0` | no features before this |
| features | min_overlap_s | `0.8` | NCCF valid only with this much overlap |
| features | s1_window_s | `2.0` | 25-50 Hz (S1) band: window of its features, |
| features | s1_decimation | `2` | stored at 100 / 2 = 50 Hz |
| features | s1_max_bpm | `250` | band features used for candidate rates up to this |
| grid | hr_min_bpm | `30` | lowest candidate rate |
| grid | hr_max_bpm | `300` | upper bound (highest state 298.8 BPM) |
| grid | step_rel | `0.01` | relative spacing of the states |
| scorer | emission_beta | `0.35` | weight of the scorer in the tracker |
| scorer | hidden | `16` | hidden units of the scorer MLP |
| tracker | sigma_rel_per_sqrt_s | `0.08` | HR drift, relative, per sqrt(second) |
| tracker | octave_prob | `0.002` | chance per update of a jump to double or half |
| tracker | floor_prob | `0.0005` | uniform re-initialisation per update |
| tracker | estimate_tol_rel | `0.05` | estimate window and frame agreement, ±5 % |
| rhythm | peak_min_rel | `0.4` | local maxima of at least this x the window maximum |
| rhythm | refractory_rel | `0.6` | beats at least this x the estimate's period apart |
| rhythm | min_intervals | `2` | interval CV and beat-count HR need this many |
| decision | min_s | `1.5` | no reading before this time |
| decision | slow_bpm | `50` | below: slow path |
| decision | regular_max_cv | `0.10` | picked-beat interval CV at or above: irregular path |
| decision | slow_irregular_early_confidence | `0.97` | slow/irregular path before slow_irregular_relaxed_s: |
| decision | slow_irregular_relaxed_s | `4.0` | this probability within ±10 BPM is needed |
| decision | conf_fine_bpm | `4.0` | confidence of a regular reading: mass within ±4 BPM |
| decision | conf_rough_bpm | `10.0` | slow/irregular reading and forced reading: ±10 BPM |
| decision | forced_min_mass | `0.3` | forced reading at latest_s only above this mass |
| decision | min_periodicity | `0.2` | no reading below this ACF at the estimate's period |
| decision | min_activation_sd | `0.03` | no reading below this activation SD (silence, hum) |
| decision | lost_periodicity | `0.15` | after the first reading, a frame without heartbeat: ACF below this (lower than min_periodicity, so irregular rhythms stay) or SD below min_activation_sd |
| decision | lost_after_s | `2.0` | this long without heartbeat in a row: the measurement restarts by itself |
| decision | idle_s | `2.0` | before the first reading: this long with the |
| decision | idle_activation | `0.4` | activation below this (no heartbeat), the analysis restarts every frame until it rises (front end and network keep running) |
| decision | recent_s | `1.5` | recent heartbeat: correlation of the last recent_s (at least one period + recent_overlap_s) |
| decision | recent_overlap_s | `0.5` | with itself one period earlier, |
| decision | recent_tol_rel | `0.15` | best lag within ±15 % of the estimate's period |
| decision | recent_min | `0.0` | heart present (first reading, updates) at or above |
| decision | recent_lost | `0.2` | after the first reading: below this, no heartbeat |
| decision | confirm_s | `1.0` | first reading only after this long of heart present |
| decision | presence_min | `0.6` | learned heart-present gate (readout.presence, <weights>_presence.npz): present at or above this |
| decision | presence_lost | `0.3` | after the first reading: below this, no heartbeat |
<!-- end:device_parameters -->
