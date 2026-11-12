# Data, labels and training

The recordings behind the model, how they are split, how the reference heart rate is built from
the ECG, how the training data is augmented, and how the models are trained. Version and model:
[README](../README.md). Tables between the generated markers come from the configs and the data
manifest; aggregates only.

## Contents
1. Data snapshot
2. Splits: training, validation, test
3. Reference labels (ECG)
4. Augmentation
5. Training
6. Data handling

## 1. Data snapshot
Production recordings of the Eko DUO3: PCG at 4000 Hz, 16-bit, and three ECG leads at 500 Hz,
recorded together; the first 17 s of each are used (`data.max_duration_s`). Each recording
carries the production ECG heart rate of the EFAST service. A sweep tool selected them with the
settings below: up to `per_bin` recordings per 5 BPM bin between `min_bpm` and `max_bpm`, PCG
signal quality at least `min_pcg_sq` and agreement between the ECG and PCG heart rates within
`max_ecg_pcg_diff` BPM. The snapshot therefore holds mostly clean recordings, and the results
are those of clean recordings; an evaluation on an unfiltered snapshot is open.

<!-- begin:snapshot -->
| Snapshot setting | Value | Meaning |
|---|---|---|
| run_name | `sq5_sweep_core500_20261008` | snapshot name |
| target | `3400` | recordings requested |
| min_bpm | `30.0` | lowest label HR sampled (BPM) |
| max_bpm | `200.0` | highest label HR sampled |
| bin_width | `5.0` | HR bin width for stratified sampling (BPM) |
| per_bin | `100` | recordings per bin, at most |
| min_pcg_sq | `0.7` | lowest production PCG signal quality |
| max_ecg_pcg_diff | `5.0` | largest difference of ECG and PCG HR (BPM) |
| days | `365` | look-back window (days) |
| label_source | `heart_rate` | production label used |
| lead | `2` | ECG lead of the production label |
| s3_prefix | `s3://eko-algorithms/data/coral_hr/` | storage |
| started_at | `2026-10-08T18:31:28` | time of the pull |
<!-- end:snapshot -->

## 2. Splits: training, validation, test
- Split by clinician account (`user_id`), so no account contributes to two splits: recordings of
  one clinician share devices, patients and technique. `patient_id` is missing in most rows and
  cannot serve as the group key.
- Shares 50 / 20 / 30 % of the recordings, as 10 parts of 10 % from a grouped stratified k-fold
  over 5 BPM bins of the reference HR, so each split holds a similar mix of rates. Created once
  by `hr-estimation prepare` and kept (`output/splits/split_v1.csv`).
- Training: fits the network and the scorer, with augmented copies. Validation: tunes the
  settings and fits the readout calibration. Test: evaluated once per agreed candidate.

<!-- begin:splits -->
| Split | Recordings | Clinician accounts | Trusted ECG | Beats not heard | AF | Reference HR, median (5-95 %) | < 50 BPM | 50-99 | 100-149 | ≥ 150 | Device |
|---|---|---|---|---|---|---|---|---|---|---|---|
| train | 1366 | 593 | 1178 | 10 | 34 | 106 (47-167) | 104 | 521 | 530 | 211 | DUO3 1366 |
| val | 544 | 238 | 468 | 3 | 14 | 106 (48-169) | 40 | 213 | 201 | 90 | DUO3 544 |
| test | 825 | 342 | 708 | 5 | 18 | 106 (48-170) | 62 | 319 | 300 | 144 | DUO3 825 |
<!-- end:splits -->

Trusted ECG: consensus R-peaks valid on at least two leads (§3); these recordings are graded
against the ECG, except those whose beats are not heard. Beats not heard: ECG HR at least 1.5x
the EFAST HR, i.e. ECG beats without heart sounds; reported apart. AF: EFAST rhythm class
atrial fibrillation. Reference HR: the consensus ECG HR where trusted, else the EFAST ECG HR.

<!-- begin:split_settings -->
| Setting | Value | Meaning |
|---|---|---|
| reference_column | efast_ecg_heart_rate | EFAST ECG HR; the consensus ECG HR replaces it where the ECG is trusted (labels) |
| max_duration_s | 17.0 | longer recordings truncated |
| seed | 20261031 | split and training |
| train | 0.5 | share of recordings: training |
| val | 0.2 | validation (tuning, calibration) |
| test | 0.3 | test (once per agreed candidate) |
| group_column | user_id | one account never in two splits |
| stratify_bin_bpm | 5 | each split gets a similar mix of 5 BPM bins |
<!-- end:split_settings -->

## 3. Reference labels (ECG)
The ECG is the answer key, never an algorithm input.
- R-peaks per lead by five published detectors of NeuroKit2 (`labels.detectors`); a beat of a
  lead needs agreement of at least 3 detectors within 75 ms, and a beat of the recording at
  least 2 leads. Gaps longer than 1.6x the median interval are filled with beats found on one
  lead (beats missed where the other leads were noisy).
- Trusted: at least 2 leads match the recording's beats (F1 ≥ 0.9), at least 4 beats, every
  interval between 0.25 and 2.2 s (27-240 BPM).
- Reference of a reading at time t: the ECG HR over the last 4 s before t, widened to hold at
  least 3 beat intervals, from the beat count over the span (the same definition as the
  algorithm's beat-count rate).
- Groups in the results: regular (ECG beat-interval CV below 5 %) at 50-200 BPM, slow (below
  50 BPM), irregular (CV at least 5 % or AF), very fast (200-300 BPM, sped-up recordings of the
  stress test only).

<!-- begin:labels -->
| Setting | Value | Meaning |
|---|---|---|
| leads | [_ECG, _ECG_1, _ECG_2] | ECG file suffixes: leads 1-3 |
| detectors | [neurokit, pantompkins1985, hamilton2002, elgendi2010, kalidas2017] | NeuroKit2 |
| match_tol_s | 0.075 | detections of one beat agree within this |
| min_detectors | 3 | a beat of a lead: found by this many detectors |
| min_leads | 2 | a beat of the recording: found on this many leads |
| gap_ratio | 1.6 | longer gaps filled with beats found on one lead |
| lead_f1_min | 0.9 | trusted: min_leads leads match the recording's beats |
| min_beats | 4 | trusted: at least this many beats ... |
| rr_range_s | [0.25, 2.2] | ... with every R-R interval in this range (27-240 BPM) |
| window_s | 4.0 | reference of a reading: ECG HR over the last 4 s, |
| min_intervals | 3 | widened to hold at least 3 beat intervals |
| unheard_ratio | 1.5 | ECG HR >= 1.5x EFAST: beats not heard (own group) |
<!-- end:labels -->

## 4. Augmentation
Training split only; validation and test recordings are never augmented. Each training
recording is used as recorded plus `copies` augmented copies, each drawn independently:
1. Rate change, only for recordings with regular ECG beats (`warp_rr_cv_max`,
   `warp_rr_dev_max`):
   - speed-up (probability `p_fast`): the whole signal sped up toward a rate in
     `fast_target_bpm`, at most `speed_max` times faster. Covers very fast hearts, which the
     data lacks; speeding up also raises the sound frequencies.
   - otherwise time warp (probability `p_warp`): each diastole stretched or compressed toward a
     target rate, log-uniform in `target_bpm` (`slow_target_bpm` for a share `p_slow` of the
     warps), while S1, systole and S2 keep their shape (`keep_s`, `keep_frac`). Covers slow and
     fast rates with realistic heart sounds. With probability `p_jitter` the warp also varies
     every beat interval (CV in `jitter_cv`), imitating irregular rhythm.
2. Gain: every copy, uniform in `gain_db`.
3. Spectral tilt (probability `p_tilt`): a mix of the signal (share `tilt_alpha`) and its
   first-order low-pass at `tilt_cutoff_hz`, which attenuates the high frequencies, imitating
   different chest pieces and contact.
4. Noise (probability `p_noise`): one of `noise_types` (white, mains hum with harmonics, breath
   bursts, recorded non-heart sounds of the training part, §4.1) at an SNR in `snr_db`. Friction
   noise is kept out of training for the stress test.

The reference HR and the beat times of a copy follow the warp or the speed-up, so its targets
stay exact.

<!-- begin:augmentation -->
| Setting | Value | Meaning |
|---|---|---|
| copies | 4 | augmented copies per training recording (0: none) |
| seed | 11 | random draws of the copies |
| warp_rr_cv_max | 0.10 | time warp only regular ECG beats: interval CV max |
| warp_rr_dev_max | 0.25 | and largest interval deviation from the median |
| p_fast | 0.15 | sped up (whole signal) to fast_target_bpm |
| fast_target_bpm | [200, 300] | log-uniform target HR of a speed-up |
| speed_max | 2.0 | at most this speed-up factor |
| p_warp | 0.7 | otherwise time-warped with this probability |
| p_slow | 0.35 | share of warps aimed at slow hearts |
| target_bpm | [30, 220] | log-uniform target HR |
| slow_target_bpm | [30, 50] | target HR of the slow warps |
| hr_factor_range | [0.45, 1.8] | target / original HR |
| keep_s | 0.5 | unwarped part after each R-peak (S1, systole, S2) |
| keep_frac | 0.65 | at most this share of the cycle |
| min_diastole_s | 0.08 | shortest warped diastole |
| p_jitter | 0.25 | share of warps with per-beat jitter |
| jitter_cv | [0.03, 0.15] | per-beat interval variation (irregular rhythm) |
| p_noise | 0.8 | share of copies with added noise |
| noise_types | [white, hum, breath, "real:speech", "real:voice", "real:impact", "real:rub", "real:alarm", "real:music"] | friction held out for the stress test; real: recorded sounds of the negatives manifest |
| eq_db | 6.0 | real sounds: random EQ ±dB at each channel point |
| snr_db | [0, 25] | noise level, uniform |
| gain_db | [-12, 12] | gain of every copy, uniform |
| p_tilt | 0.5 | share of copies with spectral tilt |
| tilt_alpha | [0.3, 1.0] | share of the original signal in the tilt mix |
| tilt_cutoff_hz | 150 | first-order low-pass of the tilt |
| channel_hz | [0, 50, 75, 100, 150, 200, 300, 400, 500, 700, 1000, 1500, 2000] | device channel: |
| channel_db | [0, 0, -8, -11, -17, -20, -23, -25, -28, -31, -34, -36, -37] | synthetic noise as recorded; median background spectrum of 200 val recordings (30 % quietest 0.1 s), flat below 50 Hz |
<!-- end:augmentation -->

### 4.1 Recorded non-heart sounds
Speech, voice, knocks and other sounds near the device, used as noise in training (network,
presence gate) and, from a separate part, in `hr-estimation noise-eval`. Prepared by
`hr-estimation prepare --negatives` (section `negatives` of `configs/training.yml`): resampled
to 4000 Hz mono, 16-bit, peak 0.9, in `data/negatives/` (not in git), listed with kind, corpus,
licence, group, split and attribution in `data/negatives/manifest.csv`. In use, each clip passes
through the measured device channel (`augment.channel`) with a random equaliser of ±`eq_db`.

| Corpus | Licence | Clips | Kinds |
|---|---|---|---|
| LibriSpeech `dev-clean` (Panayotov et al., ICASSP 2015; openslr.org/12) | CC BY 4.0 | 240 (40 speakers x `speech_per_speaker` 6) | speech |
| FSD50K evaluation set (Fonseca et al., IEEE/ACM TASLP 2022; Zenodo 4060432) | CC0 1,780; CC BY 3.0/4.0 1,292 | 3,072 | impact, voice, rub, alarm, music |

- Kinds from the FSD50K labels (`fsd50k_kinds`, first match wins): impact (knocks, taps, thuds,
  doors, footsteps, typing, clock ticks, drums), voice (speech in rooms, conversation, singing,
  laughter, cough, breathing), rub (scratching, crumpling, tearing, zippers, writing), alarm
  (alarms, bells, phones, horns), music. Purr excluded (`fsd50k_exclude`): periodic like a heart.
- Licences: only CC0 and CC BY (`licences`); the author of every CC BY clip is in the manifest's
  `attribution` column. Clips are not redistributed.
- Split: training and evaluation parts disjoint by speaker (LibriSpeech) and by Freesound
  uploader (FSD50K), seeded hash, `eval_share` 0.3 to evaluation.

## 5. Training
`hr-estimation train --out NAME` trains the three parts of a model and writes
`models/NAME_tcn.npz`, `NAME_scorer.npz` and `NAME_calibration.npz` (released models are tracked
in git, others ignored).
- Network (block B): the autocorrelation of its output over a random 2-4 s window must peak at
  the target period, the ECG HR over that window; no beat-by-beat labels are needed. Since
  hr_v9 also: the output must match the `teacher` network (hr_v8) on the recording without
  added noise (squared error x `distill_weight`), and stay at 0 on `noise_rows` clips of
  non-heart sound only (`noise_kinds`, §4.1; finger taps drawn more often, one tap shape per
  clip: ring 20-250 Hz, decay 5-80 ms, with or without a click and a press pulse, 40-160 per
  min). Since hr_v10c also: `synth_rows` synthetic hearts (S1 and S2 at 30-220 BPM through the
  device channel) with the teacher's output as target, so that a pair of sounds at the systolic
  interval marks a heartbeat and a single tap does not. Initialised from the teacher, zero
  weights for the added inputs. Trained in PyTorch, exported to numpy and checked against the
  streaming implementation.
- Scorer (block D): inputs from streaming the training recordings and their copies through
  blocks A-C of the device code; target: the states within 3 % of the ECG HR over the frame's
  window. Frames every 0.25 s up to 5 s, then one per second.
- Readout calibration (block F): two logistic fits (regular, slow/irregular) on validation
  frames, from the posterior mass and the periodicity.
- Variants: `--scorer-only --weights BASE` keeps the network of BASE and retrains the scorer and
  the calibration; `--calibrate-only` refits the calibration only. The current model's history
  is in [CHANGELOG.md](../CHANGELOG.md).

<!-- begin:training -->
| Setting | Value | Meaning |
|---|---|---|
| net.periodicity_window_s | [2.0, 4.0] | loss window drawn per batch |
| net.crop_s | 4.0 | length of each training example |
| net.batch | 32 | examples per batch |
| net.epochs | 30 | passes over the training set |
| net.lr | 0.003 | Adam learning rate |
| net.teacher | hr_v8 | target activation: this model's TCN on the recording |
| net.distill_weight | 100.0 | without added noise (MSE, x this weight), 0 on |
| net.synth_rows | 600 | synthetic hearts (S1 and S2 at 30-220 BPM) |
| net.noise_rows | 2000 | noise-only rows (max_duration_s each, noise_kinds) |
| net.noise_kinds | [taps, rhythmic_taps, handling, scratch, friction, speech, breath, white, hum, "real:speech", "real:voice", "real:impact", "real:rub", "real:alarm", "real:music", taps, rhythmic_taps, rhythmic_taps, rhythmic_taps] | kinds of the noise-only rows (target 0), in turn; taps listed more than once: drawn more often |
| scorer.positive_tol_rel | 0.03 | target: states within 3 % of the target HR |
| scorer.dense_until_s | 5.0 | training frames every 0.25 s up to here, |
| scorer.sparse_step_s | 1.0 | then one per second |
| scorer.epochs | 12 | passes over the training frames |
| scorer.batch_frames | 256 | frames per batch |
| scorer.lr | 0.003 | Adam learning rate |
| scorer.weight_decay | 0.0001 | Adam weight decay |
<!-- end:training -->

## 6. Data handling
Recordings, manifests, label tables, models and per-recording results are patient-derived or
large: they live in `data/` and `output/` (not in git) and on S3 (snapshot prefix in §1). These
documents hold aggregates only: counts, shares, percentiles and curves, no recording
identifiers, waveforms or per-recording values.
