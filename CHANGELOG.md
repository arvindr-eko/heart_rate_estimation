# Changelog

Semantic versioning, `MAJOR.MINOR.PATCH` in `pyproject.toml` (the single source; also in every
run's `run_info.json`, in the C code as `hr_version()` and in the README's version table):
MAJOR for a change of the C API or of the output contract (what a reading is and when it
comes), MINOR for a model or algorithm change that changes readings, PATCH for a change with
identical readings (fixes, tools, documentation). Each entry names the model, the runs and the
key test results; the full documentation of a version is `docs/` at its tag.

## Releasing a version
1. Change the code or the model; set `version` in `pyproject.toml` by the rules above; pin a
   changed dependency in `requirements.txt`; a new released model gets its line in `.gitignore`
   (`models/`).
2. Evaluate: `hr-estimation evaluate --split val`, then once per agreed candidate
   `--split test` and `--split test --stress`, each with `--tag vMAJOR.MINOR.PATCH`.
3. When readings changed, update the golden readings (`output/golden/`) from the test run; run
   `pytest` (includes the C port against the Python reference).
4. Regenerate the README's version table and key results, the tables of `docs/` and
   `docs/figures/` from the val, test and stress runs of step 2.
5. Add an entry below with the version, date, model, what changed and the key numbers; update
   the known limitations in the README.
6. Commit, tag `vMAJOR.MINOR.PATCH`, push the branch and the tag
   (`git push origin main vMAJOR.MINOR.PATCH`).

## 1.5.0rc1 (2026-11-12), model hr_v10c, beat detector trained against finger taps (release candidate, device test pending)
From the 1.4.0rc1 device test: finger taps still gave readings. With taps more varied than the
training's tap model (ring 20-250 Hz, decay 5-80 ms, with or without a click and a press pulse,
40-160 per min), 81 % of 300 rhythmic-tap clips gave a reading with 1.4.0rc1 (low-pitched rings
and press pulses about 95 %); the noise evaluation's taps (20 %) covered only the narrow model.
Same front end, network layout, readout and C code; new weights. C API unchanged.
- Taps (`augment.tap_shape`, `tap`, `noise`): one tap shape per clip over the ranges above,
  rhythmic taps at a steady 40-160 per min with 0-8 % jitter, single taps (a pair would mimic S1
  and S2).
- Network training (`training.net`): `noise_rows` 1,500 to 2,000 with taps drawn three times as
  often (`noise_kinds`); `synth_rows` 600 synthetic hearts (`train._synthetic_example`: S1 and S2
  at 30-220 BPM, S2 0.3-1.2 x S1, interval CV 0-0.1, a murmur in 30 %, 5-30 dB SNR, device
  channel; target: the teacher's output without the added noise), so a pair at the systolic
  interval marks a heart and a single tap does not. The TCN input cache key now covers
  `synth_rows` and `train.py`. hr_v10c is trained by `hr-estimation train --out hr_v10c` and
  `train --presence-only` (reproducible from the repository, unlike hr_v9c).
- Taps, share of clips with any reading, 1.4.0rc1 to 1.5.0rc1: 300 broad rhythmic-tap clips
  (separate seeds) 0.81 to 0.33; noise evaluation rhythmic taps 0.80 to 0.30, single taps 0.15
  to 0.05. Other no-heart clips: speech 0.10 to 0.15, voice 0 to 0.10, knocks 0 to 0.05,
  rubbing 0.05 to 0.10, music 0 to 0.05, handling 0.20 to 0.15, alarms 0.05 both.
- Faint hearts (synthetic, white noise; in-distribution for hr_v10c, which trains on the same
  generator): median first reading at +5, 0, −3, −6 dB SNR 4.0, 4.75, 5.0, 8.0 s to 3.0, 3.5,
  3.75, 4.0 s, all within ±3 BPM.
- Placement (validation): after quiet sound median 4.00 to 3.50 s, after handling 5.00 to
  4.50 s, after room noise 3.25 to 3.38 s; fresh start 90th percentile 5.10 to 4.55 s.
- Test split against 1.4.0rc1: regular median time to first reading 3.0 to 2.75 s (90th
  percentile 4.25 to 4.0 s), first within ±3 BPM 0.890 to 0.886, within ±3 BPM at 4 s 0.822 to
  0.881, MAE 1.36 to 1.34 BPM, slow median 4.5 to 4.0 s, irregular first within ±10 BPM 0.787 to
  0.762, irregular coverage 1.000 to 0.987. Stress: 90th percentile 5.5 to 4.75 s, within ±3 BPM
  at 4 s 0.731 to 0.786, reading by 10 s 0.937 to 0.950, MAE 1.90 to 1.94 BPM. Validation
  disagrees on MAE (1.69 to 1.79 BPM).
- C port: unchanged code, state 10,020 bytes and stack as 1.4.0rc1; Python and C identical on
  the synthetic cases and the test recordings. Golden readings from the test run.
- Experiments (validation, noise evaluation, the 300 broad tap clips and synthetic faint hearts):
  - hr_v10 (broad taps with 30 % double taps, no synthetic hearts): taps 0.20, broad clips 0.24,
    test MAE 1.30 BPM, but synthetic hearts read late or not at all (7 corner cases failed) and
    stress irregular coverage 0.943; hr_v10b (single taps only): more corner cases failed;
    hr_v10d (200 synthetic hearts): rhythmic taps 0.55;
  - `idle_activation` 0.3: no faint-heart difference with hr_v10 or hr_v10c, placement after room
    noise slower; kept 0.4. Presence 0.5 or confirmation 0.75 s on hr_v10: no gain;
  - beat picking on hr_v9c: `rhythm.refractory_rel` 0.75 median 3.0 to 2.75 s (first within
    ±3 BPM −0.004), `decision.regular_max_cv` 0.15 or 0.20 median 2.75 or 2.5 s (−0.008 or
    −0.028, irregular −0.021); not adopted (on hr_v10 the refractory cost 0.023).
- Known limitations: irregular hearts slightly worse (above); speech and voice give a reading in
  15 % and 10 % of held-out clips; rhythmic taps still in 30 %; the synthetic corner cases are no
  longer independent of the network's training data.

## 1.4.0rc1 (2026-11-11), model hr_v9c, faster first reading with the input running (release candidate, device-tested)
From the 1.3.0rc1 device test: no readings on clicks or speech, but the first reading after 6-7 s
on the screen. Firmware bench (coral-fw, placement after 10 s of off-chest sound with the
estimator running, as on the device): first reading 4.80 s after the placement (1.2.0rc1:
2.55 s; fresh start 4.05 s for both). The network outputs about 0 on off-chest sound, so at the
placement the 4 s analysis window held no beats and the presence gate stayed below 0.05 for 3 s.
Same model; C API unchanged.
- Idle restart (`estimator.py`, `estimator.c`): before the first reading, after `idle_s` (2 s)
  with the beat activation below `idle_activation` (0.4), the analysis (features, tracker,
  readout, measurement clock) restarts every frame until the activation rises; front end and
  network keep running. A placement then starts the analysis as a fresh start does.
- Earlier slow/irregular reading (`readout.first_due`, `hr_first_due`): `slow_irregular_after_s`
  4.0 to 3.0 s, with a probability within ±10 BPM of at least
  `decision.slow_irregular_early_confidence` (0.97) until `slow_irregular_relaxed_s` (4 s), 0.9
  from then on as before. The setting remains the earliest slow/irregular first reading.
- `hr-estimation noise-eval`: placement cases (10 s of quiet sound at 0.05x, room noise at 1x,
  handling at 2x or speech at 1x the recording SD before the recording; and fresh starts);
  `noise-eval --export DIR` writes them as int16 PCM with the Python readings for a firmware
  bench (patient audio: delete after use).
- Placement (validation, the tuning split; 60 recordings of any rhythm), 1.3.0rc1 to 1.4.0rc1:
  after room noise median 4.75 to 3.25 s, first reading by 4 s 0.08 to 0.63; after quiet sound
  4.75 to 4.00 s, 0.15 to 0.63; 90th percentile after room noise 6.03 to 5.83 s, after quiet
  sound 6.05 to 6.55 s; after handling (5.0 s) or speech (4.25 s) unchanged; fresh start median
  4.0 to 3.25 s.
- No-heart clips, share with any reading: rubbing 0.20 to 0.05, handling 0.45 to 0.20, rhythmic
  taps 0.30 to 0.20, synthetic taps 0.10 to 0, voice 0.05 to 0, music 0.05 to 0; speech 0.10 and
  alarms 0.05 unchanged.
- Test split against 1.3.0rc1 (fresh starts): regular median time to first reading 3.0 s and
  90th percentile 4.25 s unchanged, first within ±3 BPM 0.893 to 0.890, MAE 1.36 BPM both,
  irregular first within ±10 BPM 0.783 to 0.787. Stress: median first reading 4.0 to 3.0 s,
  MAE 1.90 BPM and reading by 10 s 0.937 both.
- C port: state 10,020 bytes and stack unchanged (the idle counter uses padding); code and
  constants 17,371 to 17,529 bytes. Python and C: identical reading times on the synthetic cases
  and the 825 test recordings (one reading 0.47 BPM apart, as in 1.3.0rc1). Golden readings
  regenerated from the test run (1.3.0rc1 copies kept as `*_v1.3.0rc1.json`).
- Documentation: README, `docs/` tables, figures and tutorial regenerated from the 1.4.0rc1
  runs; integration guide §4.4-4.5 (show a reading when it is returned; continuous input).
- Experiments (validation and noise-eval; runs `*_hr_v9c_*_sia*`, `*_er*`, `*_st*`, `*_ia*`,
  `*_lj*`, `*_noidle`):
  - slow/irregular rule: from 3.0 s at 0.9 (median 3.75 to 3.0 s, first within ±3 BPM 0.898 to
    0.874), 3.0 s at 0.95 or 0.97 alone (90th percentile 4.25 to 5.0-5.5 s), 2.5 s at 0.95
    (median 2.5 s, 0.878); chosen: 3.0 s at 0.97 plus 4 s at 0.9 (median 3.0 s, 0.890);
  - idle restart variants: full restart including front end and network (placement median
    4.0-4.25 s), analysis only (3.25-4.0 s, chosen), restarting the running mean too (worse);
    `idle_activation` 0.3 against 0.4 (0.4: placement median after room noise 0.25 s sooner,
    one clip fewer with a reading for rubbing, alarms and handling);
  - not adopted: restarting the input normalisation on a level jump at the placement (placement
    90th percentile 6.6 to 5.1 s, but readings on rhythmic taps 0.20 to 0.45); a shorter
    confirmation at presence 0.9 (at most 0.25 s sooner, MAE +0.04 BPM).
- Known limitations (code review and measurements):
  - very faint hearts (synthetic, −6 dB SNR): first reading median 8.0 s, 6.5 s without the idle
    restart (all within ±3 BPM); none at −3 dB or more. `idle_activation` 0.3 removes the delay;
    0.4 kept as device-tested;
  - many regular hearts start on the slow/irregular path: the picked-beat interval CV on the
    hr_v9c activation exceeds 0.10 (synthetic 72 BPM: until 3 s, while `p_fine` is 0.85 at
    2 s); better beat picking would give them the regular path sooner;
  - placement after quiet sound: 90th percentile 6.55 s (level step into the running mean);
  - voicing: in the fourth 10 ms interval of each block, lags up to 10 ms pair forward and longer
    ones backward (the same in Python, C and training; to be fixed with the next retraining);
    the lag-segment energy is recomputed per lag (a running update would save about a third of
    the voicing work).
- Device test (2026-11-11, firmware `h14`): first reading sooner than with h13, but later than
  offline; finger taps can still give readings. Tag `v1.4.0rc1`; not released.

## 1.3.0rc1 (2026-11-11), model hr_v9c, beat detector trained against other sounds (release candidate, not released)
No beat activation on speech, knocks and other sounds near the device, from the 1.2.0rc1 device
test. C API unchanged.
- Front end (block A, `frontend.py`, `frontend.c`): 8 network inputs instead of 4. Bands
  400-800 and 800-1600 Hz added as ln(E band / E 25-400 Hz); voicing: highest NCCF peak over lags
  2.5-12 ms of the 100-1600 Hz signal, per 10 ms within the current block (no added state);
  impulsiveness: ln(peak 1 ms energy / mean energy). Running-mean removal on the 4 heart bands
  only.
- Network hr_v9c (block B, same layout): initialised from hr_v8; trained with the hr_v8 output on
  the recording without added noise as target (squared error x 100, 30 epochs) and output 0 on
  1,500 clips of non-heart sound: 9 synthetic kinds and recorded speech, voice, knocks, rubbing,
  alarms and music (LibriSpeech, FSD50K CC0 and CC BY, `hr-estimation prepare --negatives`,
  [docs/data.md](docs/data.md) §4.1). Scorer, calibration and presence gate retrained on it.
- Readout: first reading at presence 0.6 (was 0.5) for 1.0 s (was 0.5 s).
- `hr-estimation noise-eval`: recorded sounds of speakers and uploaders not used in training
  (20 clips of 20 s per kind), lift-offs, swaps to another heart, heart plus sound. Global
  `--set SECTION.KEY=VALUE` on every command.
- No-heart clips, share with any reading, 1.2.0rc1 to 1.3.0rc1: recorded speech 0.40 to 0.10,
  voice 0.10 to 0.05, knocks and impacts 0.20 to 0, rubbing 0.30 to 0.20, alarms 0.10 to 0.05,
  music 0.30 to 0.05; synthetic taps 0.80 to 0.10, rhythmic taps 1.00 to 0.30, handling 0.85 to
  0.45, speech 1.00 to 0, friction 0.10 to 0. Lift-off: measurement ended in 59 of 60 per kind,
  median 3.0-3.5 s; error of the first reading after a return with rubbing, 90th percentile 34.5
  to 2.1 BPM. Swap to another heart: adapted in 45 of 45, median 4.5 to 4.25 s (90th percentile
  8.25 to 8.5 s).
- Test split against 1.2.0rc1: regular median time to first reading 2.75 to 3.0 s (90th
  percentile 4.0 to 4.25 s), first within ±3 BPM 0.888 to 0.893, accurate at 4 s 0.858 to 0.822,
  MAE 1.27 to 1.36 BPM, irregular coverage 0.996 to 1.000, reading by 10 s 0.997 to 0.990.
  Stress: median first reading 3.75 to 4.0 s (90th percentile 4.5 to 5.5 s), reading by 10 s
  0.977 to 0.937, MAE 1.62 to 1.90 BPM.
- C port: state 9,956 to 10,020 bytes (budget 10,240); stack of the deepest call path 672 bytes,
  libm included (budget 1,024); code and constants 16,394 to 17,371 bytes; host time per
  ordinary block 6.7 µs (1.1.0) to 12.0 µs, frame block unchanged (bundle self-test, same
  computer). 825 test recordings: identical reading times; 1 of 11,336 readings differs by more
  than 0.1 BPM (0.47 BPM, a tie in the estimate); recording tolerance 0.2 to 0.5 BPM.
- Tests: corner-case first reading by 5 s (was 4 s; 55 BPM synthetic: 4.5 s); synthetic speech
  gives no reading (was a known limitation).
- Experiments (validation and held-out non-heart clips):
  - distillation weight 10, 40 epochs (hr_v9b): activation on held-out speech, knocks and
    rubbing 0.24-0.34 on average, readings on them as with 1.2.0rc1; weight 100, 30 epochs
    (hr_v9c): 0.07-0.17, hearts 0.53; 16 channels instead of 8: no gain;
  - presence threshold 0.5-0.7 and confirmation 0.5-1.0 s: 0.6 and 1.0 s chosen (speech 0.25
    to 0.10, first within ±3 BPM 0.878 to 0.898, median first reading +0.5 s); 0.7 lowered
    irregular coverage;
  - no tracker update while no heartbeat is heard: validation MAE 1.78 to 1.82 BPM, not
    adopted.
- Not met: test MAE above 1.2.0rc1; fewer readings under stress; no-heart readings on rubbing,
  handling and rhythmic taps; adaptation to a new rate unchanged. hr_v9c was trained by
  `train.fit_net` from cached inputs with the settings now in `configs/training.yml`; retrain
  with `hr-estimation train` before a release.
- Device test (2026-11-11, firmware `h13` side by side with `h12` = 1.2.0rc1): no readings on
  clicks or speech; first reading on the screen after 6-7 s (h12: 4-5 s). Not released; no tag.

## 1.2.0rc1 (2026-11-11), model hr_v8, presence gate (release candidate, not released)
Readings only with a heart sound, and a fast stop after lift-off, from field reports of readings
on finger taps, handling and the device on a table.
- Block Q (`quality.py`, `quality.c`): ln(E 200-400 Hz / E 25-100 Hz), energy averages over 0.5 s.
- Recent heartbeat (`features.recent_periodicity`): correlation of the last 1.5 s of the
  activation with itself one period earlier; below 0.2 after the first reading: no heartbeat.
- Learned heart-present gate (`readout.presence`, `<weights>_presence.npz`, `hr-estimation train
  --presence-only`): MLP, 16 hidden units, over periodicity, recent heartbeat, activation SD,
  agreement, posterior masses, high-band ratio and its product with the periodicity; trained on
  training-split heart frames, with systolic murmurs added to every second recording, against
  no-heart clips (taps, rhythmic taps, handling, scratch, friction, speech, breath, room noise,
  hum) and lift-offs, all through the measured device channel (`augment.channel`). First
  reading at probability 0.5 for 0.5 s; after it, below 0.3: no heartbeat.
- Test split against 1.1.0: regular median time to first reading 2.5 to 2.75 s (90th
  percentile 4.0 s both), first within ±3 BPM 0.877 to 0.888, accurate at 4 s 0.900 to 0.858,
  MAE 1.36 to 1.27 BPM, irregular coverage 1.000 to 0.996. Stress: median first reading 2.75 to
  3.75 s, reading by 10 s 0.998 to 0.977, MAE 2.13 to 1.62 BPM.
- No-heart clips (validation harness, 10 x 20 s per kind, channel-matched): readings 60 to 21
  (taps), 83 to 42 (handling), 24 to 4 (friction), 22 to 0 (breath), 7 to 0 (room noise), 6 to 0
  (scratch); rhythmic taps and speech nearby unchanged (known limitation). Lift-off spliced into
  80 validation recordings: measurement ended for 79-80 of 80 per noise kind, median 2.8-3.8 s
  after the lift-off (1.1.0: 5.2 s in silence, often not within 8 s with noise).
- Golden readings regenerated from the test run (1.1.0 copies kept as `*_v1.1.0.json`).
- Device test (2026-11-11, firmware `h12` side by side with `h8` = 1.1.0): fewer readings on
  finger taps; still readings at random times while speaking near the device and on knocks and
  taps on the table. Not released; no tag.

## 1.1.0 (2026-11-10), model hr_v8
Memory of the C port within the firmware's limits: state 10 KB, stack 1 KB.
- Cortex-M4 build: state 13,836 to 9,940 bytes (budget 10,240); stack of the deepest call path
  504 bytes, libm included (budget 1,024); code and constants 13,705 to 14,453 bytes without
  libm (flash budget 5,120, exceeded).
- Per-frame ACF and NCCF stored as int16 codes (step 1/32767, saturated at ±1), in the Python
  reference and in C; this changes readings slightly. Kept in float32: the window samples
  (as codes, last-bit differences of the network output between implementations become code
  steps that stay in the window for 4 s), YIN (the scorer is steep in it) and the ACF used for
  the peak positions.
- C only, the same arithmetic as before: YIN kept only at each state's period, in the score
  buffer until the state is scored; the ACF at a state's period interpolated when needed; the
  ACF peaks found while the states are scored, from the lag sums, instead of a stored list; the
  window's per-sample update in one pass over the lags. Host time per call (3 paired runs,
  60 s input): ordinary block 5.0 to 3.8 µs, frame block 22.5 to 20.5 µs.
- Readings against 1.0.0: other reading times in 1 of 544 validation recordings, 0 of 825 test
  recordings and 0 of 4,736 stress cases; matched readings within 2.3e-4 BPM (99th percentile),
  largest 0.26 BPM (test) and 2.6 BPM (one stress reading). Every target keeps its result: 9 of
  14 (12 test, 2 stress); mean absolute error 1.3604 to 1.3605 BPM (test).
- C port against the Python reference: identical reading times on all 825 test recordings, BPM
  within 6.5e-3 (99th percentile), largest 0.104. The int16 codes of the two implementations can
  be one step apart, so the tolerances are wider: log-likelihoods 1e-3 to 2e-3, readings of the
  30 min session 1e-3 to 2e-3 BPM, recordings and the self-test 0.1 to 0.2 BPM and 0.02 to 0.05
  confidence.
- Budgets in `training/c_port.py` (`BUDGET`) instead of `configs/training.yml`; the stack is
  checked from the call graph and the frames of the Cortex-M4 build (`c_port.m4_stack`, with
  32 bytes for each libm call), both in `tests/test_c_port.py`.
- Runs: `20261110_150811_hr_v8_val_v1.1.0`, `20261110_150919_hr_v8_test_v1.1.0`,
  `20261110_151009_hr_v8_test_v1.1.0_stress`; validation stress
  `20261110_150905_hr_v8_val_v1.1.0_stress` against `20261110_142225_hr_v8_val_v1.0.0_stress`;
  golden readings from the test run.

## 1.0.0 (2026-11-09), model hr_v8
First versioned release.
- Python device reference (`src/device/`): band-pass front end, causal TCN, periodicity
  features of the beat signal and of the 25-50 Hz (S1) band, per-state scorer, HMM tracker,
  rhythm check, readout with a regular and a slow/irregular path, three runtime settings, and
  gates that keep silence and mains hum from giving a reading.
- Contact handling: after the first reading, frames without heartbeat (activation periodicity
  at the estimate below 0.15, or activation SD below 0.03) give no new reading, and 2 s of them
  end the measurement, which restarts by itself; the firmware needs no contact detection.
  Synthetic lift-offs into silence, white or breath noise or hum: the last reading 3 s after the
  lift-off (median); the largest error of a reading shown after it 12 BPM (22 BPM when readings
  went on during the 2 s); friction noise can keep readings going. Loss level 0.15 chosen on
  validation over 0.1: lift-offs into white noise detected in 10 of 10 cases instead of 7,
  measurements ended early in 8 instead of 6 of 544 recordings. Against the same model without
  contact handling: readings changed in 41 of 544 validation and 50 of 825 test recordings,
  every target keeps its result, mean absolute error 2.13 to 1.96 BPM (validation) and 1.40 to
  1.36 BPM (test), with atrial fibrillation 12.9 to 9.2 and 6.0 to 5.7 BPM.
- Near-flat activation windows (SD below 0.01, e.g. after seconds of digital silence) give no
  periodicity: the mean removal from the lag sums cancelled there, the periodicity left
  [-1, 1], and C and Python readings differed.
- Tools (`src/training/`, command `hr-estimation`): ECG consensus labels, augmentation,
  training, streaming evaluation per group with pass/fail targets, stress test, C model files
  and self-test cases, comparison of the C port with the reference. The planning cost model of
  the development (3.3 M cycles/s estimated against 30 M) is replaced by measurements: sizes and
  stack of the Cortex-M4 build, cycles from the self-test on the target.
- C port (`c/`): one C file per device module, C99, float32, caller-owned state; API for
  processing, queries, runtime settings and reset; heart-present flag and the automatic
  restart. For the firmware: self-test against the reference readings of synthetic cases, with
  cycles per call (`make selftest`, host and target), release bundle with checksums (`make
  dist`), integration guide (`docs/realtime_implementation.md` §4).
- Model files of hr_v8 in `models/` (network, scorer, readout calibration; 10 KB).
- Installation with pip: `requirements.txt` pins the packages; every run records its checksum.
- Test results (703 recordings with a trusted ECG): 9 of 14 targets (12 test, 2 stress).
  Regular rhythm at 50-200 BPM: first reading 2.5 s median, 87.7 % within ±3 BPM (target
  92 %), converged 3.0 s median. Irregular or AF: first reading 76.2 % within ±10 BPM (target
  90 %). All: 0.2 % half/double readings.
- C port against the Python reference: identical reading times on all 825 test recordings;
  30 min session: identical reading times, no restart, no drift of the lag sums. Cortex-M4
  build: state 13,836 bytes (RAM budget 15,360); code and constants 13,705 bytes without libm
  (flash budget 5,120, exceeded). Not yet run on the target.
- Runs: `20261109_170159_hr_v8_val_v1.0.0`, `20261109_170212_hr_v8_test_v1.0.0`,
  `20261109_171914_hr_v8_test_v1.0.0_stress`, evaluated in a new pip environment
  (requirements `cadc20337fe7`) at `77e69ad` with this release's changes; golden readings from
  the test run.
- Commits: `65b3509` (redesign), `c588927` (command `hr-estimation`), `928dc19` (flat-window
  test), `77e69ad` (C port), and the release commit (versioning, documentation, contact
  handling, firmware kit).

Earlier development (models `hr_v1` to `hr_v7` and the other methods tried) is on branch
`arvind/prototype` and not versioned.
