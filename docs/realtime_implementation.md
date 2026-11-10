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
| `readout.py` | F | `assess`, `first_due`, `schedule`, `lost` per frame | posterior, HR, gates, CV → reading or none; loss of the heartbeat | times of the first and the latest reading, frames without heartbeat |

`estimator.process` runs A, B and the per-sample update of C on every block; every fifth block
also `scorer_input` (C: 13 activation features, 3 S1-band features, flag), D, E, the two gate
values (`periodicity`, `activation_sd`), R and F. After the first reading, frames without
heartbeat (`readout.heart_absent`) give no new reading, and `lost_after_s` (2 s) of them end the
measurement (`readout.lost`, then `estimator.restart`): readings stop until the next first
reading (§4.5).

## 2. Tools (`src/training/`)
Everything around the device code: data, ECG labels, training, evaluation and tools; runs on a
workstation or SageMaker, never on the stethoscope. One command, `hr-estimation`
(`training/cli.py`), one module call per step, run inside the project environment
([README](../README.md) §2). Data, labels, augmentation and training:
[data.md](data.md).

| Step | Command | Modules | Writes |
|---|---|---|---|
| 1. Split and labels | `hr-estimation prepare` | `data` (split by clinician account, once), `labels` (ECG R-peak consensus) | `output/splits/split_v1.csv`, `output/labels/ecg_labels.csv` |
| 2. Train | `hr-estimation train --out NAME` | `train`, `augment`; scorer inputs streamed through `device` | `output/models/NAME_tcn.npz`, `_scorer.npz`, `_calibration.npz` |
| 3. Select the model | `weights: NAME` in `configs/device.yml`, or `--weights NAME` per command | | |
| 4. Tune on validation | `hr-estimation evaluate --split val` | `evaluate` (every recording streamed through `device.estimator`) | `output/runs/<time>_NAME_val/` |
| 5. Test, once per candidate | `hr-estimation evaluate --split test`, then with `--stress` | `evaluate`, `augment` (stress conditions) | `output/runs/<time>_NAME_test[_stress]/` |
| 6. Other snapshot | `hr-estimation evaluate --data <dir>` | `labels.external`, `evaluate` | `output/runs/<time>_NAME_ext_<dir>/` |
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
| `data.py` | Manifest, grouped split by clinician account, WAV loading, parallel map over recordings |
| `labels.py` | ECG R-peaks (five NeuroKit2 detectors, three leads), reference HR per reading, other snapshots |
| `augment.py` | Synthetic PCG, noise at a set SNR, time warping and speed-up (training and stress) |
| `train.py` | Network (PyTorch, exported to numpy), scorer, readout calibration |
| `evaluate.py` | Streaming evaluation, grading per group, targets, stress protocol, figures, run folder |
| `c_model.py` | C sizes, coefficients and weights from the config and the model files |
| `c_port.py` | Synthetic test signals; every block's output of the Python reference and of the C port (through ctypes) and their comparison; self-test cases (`write_selftest`) |
| `cli.py` | The `hr-estimation` command |

## 3. C port (`c/`)
C99 implementation of `src/device` for the PSoC 6: float32, no dynamic memory, no recursion, all
state in one caller-owned `hr_state_t`. One C file per Python module. Sizes, coefficients and
weights are generated from `configs/device.yml` and the model files (`hr-estimation c-model` →
`c/build/generated/`: `hr_model.h`, `hr_model.c` and the self-test cases `hr_selftest_cases.h`;
not in git, they hold the weights).

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
| `hr_process_block(&s, pcm, &reading)` | every 50 ms, 200 int16 samples | 1 when a reading is due: the first when confident (or at `latest_s`), then 1 per s while a heartbeat is heard; restarts the measurement by itself when the heartbeat is lost (§4.5) |
| `hr_get_reading(&s, &reading)` | any time | latest reading; 0 before the first and after a restart |
| `hr_get_frame_info(&s, &info)` | any time | estimate (also before the first reading), gate values, rhythm, probabilities, phase (warm-up, searching, reading), heart present |
| `hr_set_settings(&s, &settings)`, `hr_get_settings` | any time | the three runtime settings, validated; used from the next frame |
| `hr_reset(&s)` | on request: new patient or site, gap in the input | new measurement, same settings |
| `hr_version`, `hr_model_id`, `hr_get_posterior`, `hr_state_size` | any time | software version; model name and weight checksums; posterior over the 232 rates; state size |

The API in use, with the display states: §4.4.

Host tool on a synthetic 72 BPM signal (raw int16 PCM; times from the start of the file; real
output):

```
$ c/build/hr_run hr72.raw
version 1.0.0, model hr_v8 61a88ea2280e, state 13836 bytes, 10.0 s of input
t 2.25 s  71.3 BPM  confidence 0.89  first
...
t 9.25 s  71.2 BPM  confidence 0.98
```

Implementation choices, all equivalent to the Python code up to float32 rounding:
- delay lines and windows are ring buffers, so nothing is shifted per sample;
- the lag functions take one pass over the lags with running head and tail sums (the Python
  code uses prefix-sum arrays, which would need 3.2 KB more RAM);
- the scorer's input standardisation is folded into its first layer (no divisions);
- per-frame buffers (lag functions, scores, tracker prior, picked beats) share one scratch area;
- times are compared in blocks, exact for thresholds on the 50 ms grid.

## 4. Integration into the firmware
The firmware passes blocks of 200 PCG samples to `hr_process_block` and shows what comes back.
The estimator decides when a reading is ready, updates it every second while a heartbeat is
heard, and ends the measurement by itself when the heartbeat is lost: the firmware needs no
contact detection.

### 4.1 Files and build
Bundle: `cd c && make model && make dist` → `c/build/hr_c_<version>.tar.gz` with the files
below, a `README.md` (version, model, commit, build date, link to this guide, self-test command)
and `SHA256SUMS`. It holds synthetic test signals only, no recorded data. `make dist` runs the
self-test first.

| File | Role |
|---|---|
| `include/hr.h` | API; the only header the firmware includes |
| `include/hr_blocks.h` | state layout and block functions, included by `hr.h` (fields private) |
| `src/*.c` (8 files) | the estimator |
| `generated/hr_model.h`, `generated/hr_model.c` | sizes, coefficients and weights of this version |
| `tools/hr_selftest.c`, `generated/hr_selftest_cases.h` | self-test (§4.7); test firmware only |
| `tools/hr_run.c` | host tool: a raw PCM file through the C port; readings, time per call |

Product build: compile `src/*.c` and `generated/hr_model.c` with the include paths `include/`
and `generated/`; link the C math library (`expf`, `logf`, `floorf`, `ceilf`). Flags of the
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
- State: one `hr_state_t` per measurement, owned by the caller and static (never on a task
  stack); size in §6, or `hr_state_size()` at run time. The library has no heap and no global
  variables, so two states in two tasks are independent.
- Stack: §6 gives `hr_process_block` with its deepest callee, without libm; 1 KB for the calls
  leaves room for libm (step 3 of §4.7 measures it).
- Flash: code and constants in §6. The constants are float32 weights and coefficients; at 8 bits
  they would take about a quarter of their size (derived, not built).
- Timing: one call every 50 ms (200 samples at 4000 Hz). Every fifth call (a frame block, every
  0.25 s) also computes the features, scores, tracker and readout and takes several times longer
  than the other four (front end and network). Host times in §6; Cortex-M4 cycles from the
  self-test (§4.7).
- Context: one task owns the state and makes every call for it; calls for one state are not
  re-entrant. Not in the audio interrupt: the interrupt or DMA callback hands each full buffer
  to the task (double buffering).

### 4.3 Input
- Mono PCG, int16, 4000 Hz, through the same signal chain as the app recordings the model was
  trained and tested on (microphone path, filters, sample rate). The level does not matter
  (−40 dB with a DC offset: the same readings within 0.5 BPM, [results.md](results.md) §11);
  another filter setting or sample rate needs re-validation.
- Blocks of exactly `HR_BLOCK_SAMPLES` (200) samples, in order, without gaps or overlap. A
  driver with another buffer size feeds the accumulator of the example in §4.4.
- After a gap in the input (dropped samples, a paused stream) or a change of the signal chain:
  a new measurement (`hr_input_gap` in the example).

### 4.4 Output and display
`hr_process_block` returns 1 when a reading is due; `hr_get_frame_info` gives the phase of the
measurement. The latest reading is shown only in phase `HR_PHASE_READING`:

| Phase | When | Display |
|---|---|---|
| `HR_PHASE_WARMUP` | first second of a measurement (no features yet); also the frame that ended a measurement | measuring |
| `HR_PHASE_SEARCHING` | estimate updated every 0.25 s; first reading not yet due | measuring |
| `HR_PHASE_READING` | from the first reading on; a new reading every second while a heartbeat is heard, otherwise the latest one stays | the latest reading |

The estimator in a firmware task (`show_bpm` and `show_measuring` stand for the display code):

```c
#include <string.h>
#include "hr.h"

void show_bpm(float bpm);
void show_measuring(void);

static hr_state_t hr;                                     /* caller-owned, static */
static int16_t block[HR_BLOCK_SAMPLES];
static uint32_t fill;

/* Power-up or new patient: new measurement, default settings. */
void hr_start(void)
{
    fill = 0u;
    hr_init(&hr, NULL);
}

/* Gap in the input or change of the signal chain: new measurement. */
void hr_input_gap(void)
{
    fill = 0u;
    hr_reset(&hr);
}

/* One block of 200 samples (50 ms): estimator, then display. */
static void on_block(const int16_t pcm[HR_BLOCK_SAMPLES])
{
    hr_reading_t r;
    hr_frame_info_t f;
    if (hr_process_block(&hr, pcm, &r))
        show_bpm(r.bpm);                                  /* whole BPM on the display */
    hr_get_frame_info(&hr, &f);
    if (f.phase != HR_PHASE_READING)
        show_measuring();                                 /* no reading, or after a restart */
}

/* Samples from the audio driver, any count per call, in order. */
void hr_push_samples(const int16_t *x, uint32_t n)
{
    while (n > 0u) {
        uint32_t take = HR_BLOCK_SAMPLES - fill;
        take = take < n ? take : n;
        memcpy(&block[fill], x, take * sizeof *x);
        fill += take;
        x += take;
        n -= take;
        if (fill == HR_BLOCK_SAMPLES) {
            fill = 0u;
            on_block(block);
        }
    }
}
```

| Reading field | Meaning |
|---|---|
| `bpm` | heart rate (float); rounded for display |
| `confidence` | calibrated probability that the reading is within ±3 BPM (regular path) or ±10 BPM (slow or irregular path) |
| `path` | `HR_PATH_REGULAR`, or `HR_PATH_SLOW_IRREGULAR`: estimate below 50 BPM or irregular beat intervals |
| `first` | 1 for the first reading of a measurement |
| `t_s` | seconds since the measurement started |

Time to the first reading per rhythm: README key results and [results.md](results.md).

### 4.5 Contact handling
- No reading without heart sounds: silence and mains hum alone give none (tested); noise alone
  (friction, some breath and white noise) can still give one (known limitation,
  [results.md](results.md) §11).
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
- `heart_present` (frame info) is 1 while a frame passes the first-reading gates (periodicity
  ≥ 0.2, activation SD ≥ 0.03), e.g. for a weak-signal hint.

<!-- begin:contact -->
Synthetic: 8 s of a 72 BPM heart, 10 s of silence or noise at the level of the heart sounds
(stethoscope lifted), then the heart again; 10 noise draws per kind of noise.

| Gap | Measurement ended in the gap | Last reading after the lift-off, median / largest | Largest error of the readings after the lift-off (BPM) | Cases with readings without the heart | First reading after the return, median | Largest error of that reading (BPM) |
|---|---|---|---|---|---|---|
| silence | 1 of 1 | 3 s / 3 s | 0.3 | 0 | 3.25 s | 1.2 |
| white | 10 of 10 | 3 s / 6.25 s | 12.3 | 0 | 2 s | 0.8 |
| breath | 10 of 10 | 3 s / 4.25 s | 10.1 | 0 | 2.25 s | 0.8 |
| friction | 7 of 10 | 3 s / 5 s | 4.3 | 3 | 0.75 s | 29.1 |
| hum | 10 of 10 | 3 s / 3 s | 1.8 | 0 | 2 s | 0.6 |
<!-- end:contact -->

Readings without the heart come from noise passing for heartbeats (friction); they can go on
after the heart returns, so in those cases the first reading after the return can be wrong.

Host tool on the lift-off case of the C comparison (heart sounds 0-10 s, white noise 10-18 s,
heart sounds again from 18 s; real output):

```
$ c/build/hr_run hr72_lift_off.raw
t 2.25 s  71.3 BPM  confidence 0.89  first
...
t 12.25 s  72.4 BPM  confidence 0.96
t 20.75 s  71.3 BPM  confidence 0.93  first
```

### 4.6 Settings and logging
- `hr_set_settings`: the three runtime settings ([README](../README.md) §4), checked
  (`HR_ERR_ARG`, settings unchanged, when out of range) and used from the next frame;
  `hr_get_settings` reads them back.
- Log `hr_version()` and `hr_model_id()` (model name, weight checksums) once at start, so field
  data maps to a release; per reading `t_s`, `bpm`, `confidence`, `path`, `first`; for
  debugging, the frame info every 0.25 s.

### 4.7 Validation and profiling on the target
| Step | How | Pass |
|---|---|---|
| 1. Host build | repository: `cd c && make selftest`; bundle: `cc -std=c99 -O2 -ffp-contract=off -Iinclude -Igenerated -DHR_SELFTEST_MAIN tools/hr_selftest.c src/*.c generated/hr_model.c -lm -o hr_selftest && ./hr_selftest` | `hr_selftest: PASS` |
| 2. Self-test on the target | `tools/hr_selftest.c` in a test firmware with the product compiler and flags; `HR_SELFTEST_PRINTF` set to the console print function, `HR_SELFTEST_CPU_HZ` to the core clock; `hr_selftest()` called from the task that will run the estimator | PASS for every case; cycles per call and CPU load printed |
| 3. Stack | RTOS stack high-water mark of that task after step 2 | within the task's stack, with margin |
| 4. Time per block | largest frame-block time of step 2, with the task's other work | within the 50 ms block period |
| 5. Input chain | record the blocks passed to `hr_process_block` in one session, and the app recording of the same session | the same signal (rate, level, filters) up to a time shift |
| 6. Target against host | replay the recorded blocks on a computer: `c/build/hr_run FILE` | the readings the device showed, in the same order, BPM within 0.1 (`hr_run` counts time from the start of the file, `t_s` from the start of each measurement) |

The self-test runs the cases of `hr_selftest_cases.h` through the public API: a synthetic 45
BPM heart (10 s), silence, and a 72 BPM heart with a lift-off (the lift-off case of §5: 28 s;
the first reading, the pause and the restart). Every reading must come at the same block as in
the Python reference, with the same first-reading flag, BPM within 0.1 and confidence within
0.02 (the tolerance of the test recordings, §5; on the host the differences stay below 0.005);
then the API checks (argument checks, reset, settings kept on error). The signals take 304 KB of
flash, in the test firmware only. Time per call: DWT cycle counter on Cortex-M3/M4/M7/M33, or
`HR_SELFTEST_TICKS()`. Output on a host computer (on the target: cycles):

```
$ ./hr_selftest
hr_selftest: version 1.0.0, model hr_v8 61a88ea2280e, state 13836 bytes
  hr45_slow       7 readings  PASS
  silence         0 readings  PASS
  hr72_lift_off  19 readings  PASS
  api                         PASS
hr_selftest: PASS
```

## 5. Verification of the C port
`tests/test_c_port.py` (through `training/c_port.py`) builds the debug library, streams the same
int16 PCM through both implementations and compares them:
- Synthetic heartbeat cases (`training/c_port.py`): every block's output within absolute
  float32 tolerances, the same path in every frame, the same readings. A binary feature (ACF
  local peak, validity of the 2T or 3T lag) may flip where its two compared values tie to
  rounding level; the log-likelihoods of those states are not compared.
- Synthetic cases without a heart sound (silence, hum alone): the front end, the activation, the
  activation SD and the readings (none). Features of a near-flat window are rounding noise in
  both implementations and are not compared.
- Synthetic lift-offs (`hr72_lift_off`: 10 s of heart sounds, 8 s of white noise at their
  level, 10 s of heart sounds; `hr72_silent_gap`: 10 s of digital zeros instead): compared as
  the cases without a heart sound (front end, activation, activation SD), plus the frame times
  (the restart resets them) and the readings, including the new first reading after the
  return: the same times, BPM within 0.1 and confidence within 0.02 (frames of the gap are
  rounding-sensitive in both implementations).
- All synthetic cases: the same heart-present flag in every frame.
- Every test recording: the C readings against the Python readings of the test run: the same
  reading times, BPM within 0.1 and confidence within 0.02.
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
| hr72 | 5.8e-04 | 5.5e-06 | 1.1e-04 | 2.8e-04 | 4.7e-06 | 6.9e-05 | 0 | 8 / 8 | yes | yes |
| hr45_slow | 5.5e-04 | 9.2e-06 | 1.2e-04 | 6.5e-04 | 3.2e-06 | 5.7e-05 | 0 | 7 / 7 | yes | yes |
| hr150 | 5.4e-04 | 8.6e-06 | 8.0e-05 | 1.4e-04 | 3.4e-06 | 2.1e-04 | 2 | 9 / 9 | yes | yes |
| hr250_fast | 5.2e-04 | 7.2e-06 | 9.5e-05 | 5.9e-04 | 1.8e-05 | 3.5e-04 | 0 | 7 / 7 | yes | yes |
| hr110_loud_s2 | 3.9e-04 | 8.2e-06 | 1.6e-04 | 1.4e-04 | 1.1e-05 | 4.1e-04 | 0 | 7 / 7 | yes | yes |
| hr80_irregular | 4.7e-04 | 7.4e-06 | 1.4e-04 | 4.1e-04 | 1.9e-06 | 9.2e-05 | 0 | 8 / 8 | yes | yes |
| silence | 0.0e+00 | 6.0e-08 | not compared | not compared | not compared | not compared | - | 0 / 0 | yes | yes |
| hum_only | 3.5e-05 | 1.8e-06 | not compared | not compared | not compared | not compared | - | 0 / 0 | yes | yes |
| hr72_lift_off | 5.8e-04 | 1.0e-05 | not compared | not compared | not compared | not compared | - | 19 / 19 | yes | yes |
| hr72_silent_gap | 5.8e-04 | 8.6e-06 | not compared | not compared | not compared | not compared | - | 19 / 19 | yes | yes |

| Measure | Value |
|---|---|
| test recordings with identical reading times | 825 of 825 |
| readings compared | 11897 |
| BPM difference, median / 99th percentile / largest | 1.2e-04 / 1.9e-03 / 0.084 |
| confidence difference, 99th percentile / largest | 3.4e-04 / 0.017 |

Long session: 30 min of a synthetic 72 BPM heart with white noise at 20 dB SNR, through both
implementations.

| Measure | Value |
|---|---|
| readings; C reading times identical | 1797; yes |
| largest difference C against Python: BPM / confidence | 3.7e-04 / 1.8e-05 |
| restarts of the measurement | 0 |
| mean absolute error against 72 BPM: first / last 5 min | 0.52 / 0.59 BPM |
| lag sums: largest difference from a float64 computation, relative to lag 0 | 2.3e-05 |
<!-- end:c_port -->

## 6. Resources
<!-- begin:resources -->
Measured on the C build (`make -C c m4`):

| Measure (Cortex-M4, -Os) | Bytes |
|---|---|
| code: estimator.c | 2315 |
| code: features.c | 1908 |
| code: frontend.c | 448 |
| code: net.c | 652 |
| code: readout.c | 434 |
| code: rhythm.c | 788 |
| code: scorer.c | 312 |
| code: tracker.c | 660 |
| code, total | 7517 |
| constants: float32 coefficients and weights (hr_model.c) | 6188 |
| flash, total without libm | 13705 |
| RAM: state (hr_state_t) | 13836 |
| RAM: stack, hr_process_block + deepest callee, without libm | 448 |

Budget (`configs/training.yml`): RAM 15360 bytes, flash 5120 bytes, CPU 30 M cycles/s on average
(measured on the target with the self-test). libm (expf, logf, floorf, ceilf) adds to flash when
linked.

Time per call on the host computer (`c/build/hr_run`, 60 s synthetic input x 10; host timing, not
Cortex-M4 cycles):

| Call (host, -O2) | Mean (µs) | Largest (µs) |
|---|---|---|
| ordinary block | 5.3 | 22.0 |
| frame block | 23.6 | 42.0 |
<!-- end:resources -->

## 7. Parameters
`configs/device.yml`; meanings are the config's own comments.

<!-- begin:device_parameters -->
| Section | Setting | Value | Meaning |
|---|---|---|---|
| readout | confidence | `0.7` | regular hearts: how sure before the first reading (probability of being within ±3 BPM, measured on validation); lower: sooner |
| readout | slow_irregular_after_s | `4.0` | slow or irregular hearts: first reading not before this time |
| readout | latest_s | `8.0` | heart sound present: a first reading at this time when the estimate is roughly certain (decision.forced_min_mass) |
| stream | fs_hz | `4000` | PCG sample rate |
| stream | block_samples | `200` | 50 ms per input block |
| stream | frame_blocks | `5` | tracker update every 5 blocks = 0.25 s |
| stream | output_period_s | `1.0` | one reading per second after the first |
| frontend | bands_hz | `[[25, 50], [50, 100], [100, 200], [200, 400]]` | band-pass edges |
| frontend | filter_order | `2` | Butterworth order per band edge |
| frontend | env_rate_hz | `100` | log energy every 10 ms |
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
| decision | slow_irregular_confidence | `0.9` | slow/irregular path: probability within ±10 BPM |
| decision | conf_fine_bpm | `4.0` | confidence of a regular reading: mass within ±4 BPM |
| decision | conf_rough_bpm | `10.0` | slow/irregular reading and forced reading: ±10 BPM |
| decision | forced_min_mass | `0.3` | forced reading at latest_s only above this mass |
| decision | min_periodicity | `0.2` | no reading below this ACF at the estimate's period |
| decision | min_activation_sd | `0.03` | no reading below this activation SD (silence, hum) |
| decision | lost_periodicity | `0.15` | after the first reading, a frame without heartbeat: ACF below this (lower than min_periodicity, so irregular rhythms stay) or SD below min_activation_sd |
| decision | lost_after_s | `2.0` | this long without heartbeat in a row: the measurement restarts by itself |
<!-- end:device_parameters -->
