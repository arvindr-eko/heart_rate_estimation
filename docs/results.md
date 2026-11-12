# Results

Evaluation of the current version: test and validation results per group, targets, time to
converge, accuracy over time, first-reading error, strata, stress tests, settings trade-off and
corner cases. Every table and figure below is generated from the runs named here;
aggregates only. The C port's agreement with the Python reference and
the resources: [realtime_implementation.md](realtime_implementation.md).

<!-- begin:results_header -->
| Version | Model | Generated | Commit | Runs |
|---|---|---|---|---|
| 1.4.0rc1 | hr_v9c | 2026-11-11 | db171e4 | val `20261111_200954_hr_v9c_val_v1.4.0rc1`, test `20261111_201036_hr_v9c_test_v1.4.0rc1`, stress `20261111_201137_hr_v9c_test_v1.4.0rc1_stress` |
<!-- end:results_header -->

## Contents
1. How the results are produced
2. Test results per group
3. Targets
4. Time to converge
5. Accuracy over time
6. First-reading error
7. Strata
8. Validation
9. Stress test
10. Settings trade-off
11. Corner cases

## 1. How the results are produced
- Every recording streams through the device code in 50 ms blocks, as on the device; no
  whole-recording shortcut.
- Each reading is compared with the ECG heart rate over the same seconds ([data.md](data.md)
  §3). Recordings without a trusted ECG and recordings whose beats are not heard are reported
  apart.
- Groups: regular rhythm at 50-200 BPM, slow (below 50 BPM), irregular (ECG beat-interval
  variation at least 5 % or AF), all; AF alone as a subset of irregular.
- Metrics: time to the first reading; first reading within ±3 BPM and within ±10 BPM; reading on
  show within ±3 BPM at given times (none yet counts as not within); time to converge (from that
  reading on, every reading to the end of the 17 s recording within ±3 BPM); half/double
  readings (within 15 % of 0.5x or 2x the reference); mean absolute error, bias and 95 % limits
  of agreement over all readings.
- Targets: pass/fail per group, agreed before the final test run (§3).

## 2. Test results per group
<!-- begin:test -->
| Metric | regular, 50-200 BPM | slow, < 50 BPM | irregular or AF | all | AF only |
|---|---|---|---|---|---|
| recordings | 438 | 30 | 235 | 703 | 15 |
| first reading, median / 90 % by | 3 s / 4.25 s | 4.5 s / 6.5 s | 4 s / 6.5 s | 3 s / 5.5 s | 4.75 s / 10.5 s |
| first reading within ±3 BPM | 89.0 % | 100.0 % | 57.0 % | 78.8 % | 33.3 % |
| first reading within ±10 BPM | 99.5 % | 100.0 % | 78.7 % | 92.6 % | 60.0 % |
| within ±3 BPM at 4 / 6 / 9 s | 82.2 % / 98.2 % / 99.1 % | 43.3 % / 90.0 % / 100.0 % | 37.9 % / 66.4 % / 79.6 % | 65.7 % / 87.2 % / 92.6 % | 20.0 % / 13.3 % / 26.7 % |
| converged, median / 90 % by | 3 s / 5 s | 4.5 s / 6.5 s | 8.5 s / not reached | 4 s / 14.25 s | not reached / not reached |
| half/double readings | 0.1 % | 0.0 % | 0.8 % | 0.3 % | 4.1 % |
| mean absolute error; 95 % limits of agreement | 0.62; ±4.7 BPM | 0.37; ±1.0 BPM | 3.03; ±15.6 BPM | 1.36; ±9.5 BPM | 8.09; ±31.3 BPM |
| bias (mean signed error) | -0.27 BPM | +0.01 BPM | -0.08 BPM | -0.20 BPM | -0.23 BPM |

Recordings without a trusted ECG (117): first reading within the EC13 tolerance of the production
ECG HR (EFAST) in 97.4 %.
<!-- end:test -->

## 3. Targets
<!-- begin:targets -->
| Group | Metric | Value | Target | Result |
|---|---|---|---|---|
| regular, 50-200 BPM | first reading, median | 3 s | 3 s | pass |
| regular, 50-200 BPM | first reading, 90 % by | 4.25 s | 4 s | **fail** |
| regular, 50-200 BPM | first reading within ±3 BPM | 89.0 % | 92.0 % | **fail** |
| regular, 50-200 BPM | within ±3 BPM at 4 s | 82.2 % | 92.0 % | **fail** |
| slow, < 50 BPM | first reading, median | 4.5 s | 5 s | pass |
| slow, < 50 BPM | first reading within ±10 BPM | 100.0 % | 95.0 % | pass |
| slow, < 50 BPM | within ±3 BPM at 9 s | 100.0 % | 90.0 % | pass |
| irregular or AF | first reading, median | 4 s | 6 s | pass |
| irregular or AF | first reading within ±10 BPM | 78.7 % | 90.0 % | **fail** |
| irregular or AF | within ±3 BPM at 9 s | 79.6 % | 60.0 % | pass |
| all | reading by 10 s | 99.0 % | 99.0 % | pass |
| all | half/double readings | 0.3 % | 0.5 % | pass |
| very fast | first reading within ±10 BPM | 73.6 % | 90.0 % | **fail** |
| very fast | half/double readings | 6.5 % | 1.0 % | **fail** |
<!-- end:targets -->

## 4. Time to converge
Converged: from that reading on, every reading to the end of the recording is within ±3 BPM.
Left panel: share of recordings with a first reading by each time (thin) and with a first reading
within ±3 BPM of the ECG (thick); the gap between the two is the share whose first reading is off
by more than 3 BPM. Right: share converged.

<!-- begin:convergence -->
| Group | Recordings | First reading, median | Converged: median / 75 % / 90 % by | Converged by 4 / 6 / 9 / 12 s | Not converged * |
|---|---|---|---|---|---|
| regular, 50-200 BPM | 438 | 3 s | 3 s / 4 s / 5 s | 80.6 % / 96.1 % / 97.0 % / 98.4 % | 0.7 % |
| slow, < 50 BPM | 30 | 4.5 s | 4.5 s / 5.75 s / 6.5 s | 43.3 % / 90.0 % / 100.0 % / 100.0 % | 0.0 % |
| irregular or AF | 235 | 4 s | 8.5 s / 15 s / not reached | 15.7 % / 34.9 % / 51.1 % / 61.3 % | 15.3 % |
| all | 703 | 3 s | 4 s / 6 s / 14.25 s | 57.3 % / 75.4 % / 81.8 % / 86.1 % | 5.5 % |
| AF only | 15 | 4.75 s | not reached / not reached / not reached | 0.0 % / 0.0 % / 0.0 % / 0.0 % | 60.0 % |

\* last reading of the 17 s recording outside ±3 BPM.

![First reading and convergence over time](figures/convergence.png)
<!-- end:convergence -->

## 5. Accuracy over time
<!-- begin:accuracy_over_time -->
| Group | 2 s | 3 s | 4 s | 5 s | 6 s | 8 s | 9 s | 10 s |
|---|---|---|---|---|---|---|---|---|
| regular, 50-200 BPM | 11.9 % | 57.3 % | 82.2 % | 94.7 % | 98.2 % | 98.9 % | 99.1 % | 98.6 % |
| slow, < 50 BPM | 0.0 % | 0.0 % | 43.3 % | 60.0 % | 90.0 % | 100.0 % | 100.0 % | 100.0 % |
| irregular or AF | 4.7 % | 17.9 % | 37.9 % | 52.8 % | 66.4 % | 80.4 % | 79.6 % | 80.4 % |
| all | 9.0 % | 41.7 % | 65.7 % | 79.2 % | 87.2 % | 92.7 % | 92.6 % | 92.6 % |

![Accuracy over time](figures/accuracy_over_time.png)
<!-- end:accuracy_over_time -->

## 6. First-reading error
Absolute difference between the first reading and the ECG heart rate over the same seconds.

<!-- begin:first_error -->
| Group | First readings | Median |error| (BPM) | 90th percentile | 95th percentile | Largest |
|---|---|---|---|---|---|
| regular, 50-200 BPM | 438 | 0.85 | 3.13 | 4.19 | 55.3 |
| slow, < 50 BPM | 30 | 0.36 | 1.13 | 1.29 | 1.6 |
| irregular or AF | 235 | 2.01 | 19.19 | 24.78 | 91.0 |
| all | 703 | 1.02 | 6.80 | 12.78 | 91.0 |

![First-reading error](figures/first_error.png)
<!-- end:first_error -->

## 7. Strata
Test recordings with a trusted ECG, by device, firmware, mains filter setting, rhythm class and
10 BPM band of the reference HR; small strata show their count.

<!-- begin:strata -->
| Stratum | Value | Recordings | First within ±3 BPM | First within ±10 BPM | First reading, median | Within ±3 BPM at 9 s |
|---|---|---|---|---|---|---|
| device | DUO3 | 703 | 78.8 % | 92.6 % | 3 s | 92.6 % |
| firmware | 1.0.11 | 218 | 76.1 % | 90.8 % | 3 s | 93.1 % |
| firmware | 1.0.12 | 1 | 100.0 % | 100.0 % | 6 s | 100.0 % |
| firmware | 1.0.4 | 6 | 66.7 % | 100.0 % | 2.5 s | 100.0 % |
| firmware | 1.0.7 | 3 | 100.0 % | 100.0 % | 2.5 s | 100.0 % |
| firmware | 1.0.8 | 27 | 85.2 % | 85.2 % | 3 s | 92.6 % |
| firmware | 1.0.9 | 1 | 100.0 % | 100.0 % | 4 s | 100.0 % |
| firmware | 1.1.1 | 104 | 82.7 % | 98.1 % | 2.5 s | 98.1 % |
| firmware | 1.1.2 | 342 | 78.7 % | 92.4 % | 3.75 s | 90.6 % |
| firmware | 1.2.0 | 1 | 100.0 % | 100.0 % | 10.5 s | 0.0 % |
| mains filter (Hz) | 50 | 22 | 81.8 % | 90.9 % | 3.25 s | 86.4 % |
| mains filter (Hz) | 60 | 681 | 78.7 % | 92.7 % | 3 s | 92.8 % |
| rhythm (EFAST class) | Atrial Fibrillation | 15 | 33.3 % | 60.0 % | 4.75 s | 26.7 % |
| rhythm (EFAST class) | Normal Sinus Rhythm | 174 | 78.2 % | 93.1 % | 3.25 s | 96.6 % |
| rhythm (EFAST class) | Sinus Bradycardia | 66 | 84.8 % | 89.4 % | 4.5 s | 92.4 % |
| rhythm (EFAST class) | Sinus Tachycardia | 257 | 80.2 % | 95.3 % | 2.75 s | 98.8 % |
| rhythm (EFAST class) | Unspecified Rhythm | 191 | 79.1 % | 92.1 % | 3.25 s | 85.9 % |
| reference HR | 30-39 BPM | 4 | 100.0 % | 100.0 % | 6 s | 100.0 % |
| reference HR | 40-49 BPM | 42 | 78.6 % | 83.3 % | 5 s | 90.5 % |
| reference HR | 50-59 BPM | 43 | 79.1 % | 88.4 % | 4.25 s | 81.4 % |
| reference HR | 60-69 BPM | 64 | 76.6 % | 90.6 % | 4 s | 92.2 % |
| reference HR | 70-79 BPM | 51 | 80.4 % | 92.2 % | 3.5 s | 94.1 % |
| reference HR | 80-89 BPM | 52 | 82.7 % | 94.2 % | 3.5 s | 90.4 % |
| reference HR | 90-99 BPM | 60 | 75.0 % | 91.7 % | 3 s | 95.0 % |
| reference HR | 100-109 BPM | 51 | 84.3 % | 94.1 % | 3.75 s | 90.2 % |
| reference HR | 110-119 BPM | 53 | 77.4 % | 94.3 % | 3 s | 96.2 % |
| reference HR | 120-129 BPM | 54 | 77.8 % | 96.3 % | 2.5 s | 90.7 % |
| reference HR | 130-139 BPM | 49 | 63.3 % | 83.7 % | 3 s | 89.8 % |
| reference HR | 140-149 BPM | 52 | 76.9 % | 94.2 % | 2.75 s | 90.4 % |
| reference HR | 150-159 BPM | 56 | 83.9 % | 98.2 % | 2.5 s | 98.2 % |
| reference HR | 160-169 BPM | 34 | 82.4 % | 100.0 % | 2.5 s | 97.1 % |
| reference HR | 170-179 BPM | 22 | 86.4 % | 95.5 % | 2.75 s | 100.0 % |
| reference HR | 180-189 BPM | 11 | 81.8 % | 90.9 % | 3 s | 100.0 % |
| reference HR | 190-199 BPM | 5 | 100.0 % | 100.0 % | 3 s | 100.0 % |
| efast_pcg_murmur_classification | No Structural Murmur | 591 | 78.2 % | 92.0 % | 3.25 s | 92.6 % |
| efast_pcg_murmur_classification | Structural Murmur | 112 | 82.1 % | 95.5 % | 3 s | 92.9 % |
<!-- end:strata -->

## 8. Validation
Validation split, the same settings; used for tuning, so its results are not an independent
estimate.

<!-- begin:validation -->
| Group | Recordings | First reading, median | First within ±3 BPM | First within ±10 BPM | Within ±3 BPM at 4 s | At 9 s | Half/double |
|---|---|---|---|---|---|---|---|
| regular, 50-200 BPM | 254 | 3 s | 89.0 % | 99.2 % | 81.5 % | 98.8 % | 0.3 % |
| slow, < 50 BPM | 20 | 5 s | 100.0 % | 100.0 % | 25.0 % | 100.0 % | 0.0 % |
| irregular or AF | 191 | 4 s | 58.1 % | 85.3 % | 40.8 % | 76.4 % | 0.7 % |
| all | 465 | 3.25 s | 76.8 % | 93.5 % | 62.4 % | 89.7 % | 0.5 % |

Targets met on validation: 7 of 12.
<!-- end:validation -->

## 9. Stress test
Test recordings with added noise or a changed rate (`hr-estimation evaluate --split test
--stress`): 200 recordings x 4 noise types (white, friction, hum, breath) x SNR 20, 10, 5 and
0 dB; the slow recordings under the same noise; regular recordings re-timed to 32, 38 and
44 BPM (diastole stretched) and 170-300 BPM (sped up, at most 2x), clean and with white noise
at 10 dB. Sped-up recordings also shift S1 out of the 25-50 Hz band, so they understate the
S1-band features at very fast rates.

<!-- begin:stress -->
| Family | Conditions | Recordings per condition | First within ±10 BPM | Within ±3 BPM at 9 s | Half/double |
|---|---|---|---|---|---|
| noise: white, friction, hum, breath at 0-20 dB SNR | 16 | 166 | 81.9 % - 94.0 % | 73.5 % - 94.0 % | 0.0 % - 0.3 % |
| slow recordings under the same noise | 16 | 46 | 78.3 % - 91.3 % | 69.6 % - 89.1 % | 0.0 % - 0.7 % |
| slowed to 32-44 BPM (clean, white noise 10 dB) | 6 | 40 | 95.0 % - 100.0 % | 97.5 % - 100.0 % | 0.0 % - 1.5 % |
| sped up to 170-230 BPM | 8 | 40 | 87.5 % - 100.0 % | 80.0 % - 97.5 % | 0.0 % - 3.0 % |
| sped up to 250 BPM | 2 | 40 | 67.5 % - 80.0 % | 57.5 % - 75.0 % | 3.0 % - 9.2 % |
| sped up to 280-300 BPM | 4 | 40 | 35.0 % - 70.0 % | 7.5 % - 72.5 % | 10.3 % - 29.8 % |

<details><summary>All conditions</summary>

| Condition | Recordings | First within ±10 BPM | Within ±3 BPM at 9 s | Half/double |
|---|---|---|---|---|
| noise_breath_0dB | 166 | 93.4 % | 88.0 % | 0.2 % |
| noise_breath_10dB | 166 | 91.6 % | 88.0 % | 0.0 % |
| noise_breath_20dB | 166 | 91.0 % | 90.4 % | 0.3 % |
| noise_breath_5dB | 166 | 89.8 % | 89.2 % | 0.1 % |
| noise_friction_0dB | 166 | 92.2 % | 89.8 % | 0.1 % |
| noise_friction_10dB | 166 | 92.8 % | 92.2 % | 0.0 % |
| noise_friction_20dB | 166 | 93.4 % | 92.2 % | 0.1 % |
| noise_friction_5dB | 166 | 94.0 % | 92.8 % | 0.1 % |
| noise_hum_0dB | 166 | 81.9 % | 73.5 % | 0.2 % |
| noise_hum_10dB | 166 | 91.0 % | 90.4 % | 0.0 % |
| noise_hum_20dB | 166 | 93.4 % | 94.0 % | 0.1 % |
| noise_hum_5dB | 166 | 91.6 % | 84.9 % | 0.1 % |
| noise_white_0dB | 166 | 89.8 % | 89.2 % | 0.1 % |
| noise_white_10dB | 166 | 92.8 % | 88.6 % | 0.1 % |
| noise_white_20dB | 166 | 92.8 % | 89.8 % | 0.1 % |
| noise_white_5dB | 166 | 91.6 % | 90.4 % | 0.1 % |
| rate_clean_170bpm | 40 | 100.0 % | 97.5 % | 0.0 % |
| rate_clean_190bpm | 40 | 100.0 % | 97.5 % | 1.3 % |
| rate_clean_210bpm | 40 | 97.5 % | 95.0 % | 0.0 % |
| rate_clean_230bpm | 40 | 100.0 % | 97.5 % | 0.0 % |
| rate_clean_250bpm | 40 | 80.0 % | 75.0 % | 3.0 % |
| rate_clean_280bpm | 40 | 70.0 % | 72.5 % | 10.3 % |
| rate_clean_300bpm | 40 | 40.0 % | 12.5 % | 29.8 % |
| rate_clean_32bpm | 40 | 95.0 % | 100.0 % | 1.5 % |
| rate_clean_38bpm | 40 | 97.5 % | 100.0 % | 0.4 % |
| rate_clean_44bpm | 40 | 97.5 % | 100.0 % | 0.6 % |
| rate_white_10dB_170bpm | 40 | 87.5 % | 80.0 % | 0.0 % |
| rate_white_10dB_190bpm | 40 | 90.0 % | 82.5 % | 3.0 % |
| rate_white_10dB_210bpm | 40 | 90.0 % | 85.0 % | 0.0 % |
| rate_white_10dB_230bpm | 40 | 92.5 % | 92.5 % | 2.0 % |
| rate_white_10dB_250bpm | 40 | 67.5 % | 57.5 % | 9.2 % |
| rate_white_10dB_280bpm | 40 | 35.0 % | 37.5 % | 16.5 % |
| rate_white_10dB_300bpm | 40 | 50.0 % | 7.5 % | 22.9 % |
| rate_white_10dB_32bpm | 40 | 97.5 % | 97.5 % | 0.2 % |
| rate_white_10dB_38bpm | 40 | 100.0 % | 100.0 % | 0.0 % |
| rate_white_10dB_44bpm | 40 | 97.5 % | 100.0 % | 0.4 % |
| slow_breath_0dB | 46 | 84.8 % | 69.6 % | 0.0 % |
| slow_breath_10dB | 46 | 91.3 % | 73.9 % | 0.0 % |
| slow_breath_20dB | 46 | 87.0 % | 73.9 % | 0.0 % |
| slow_breath_5dB | 46 | 89.1 % | 82.6 % | 0.0 % |
| slow_friction_0dB | 46 | 84.8 % | 84.8 % | 0.0 % |
| slow_friction_10dB | 46 | 82.6 % | 87.0 % | 0.0 % |
| slow_friction_20dB | 46 | 87.0 % | 87.0 % | 0.0 % |
| slow_friction_5dB | 46 | 84.8 % | 84.8 % | 0.0 % |
| slow_hum_0dB | 46 | 87.0 % | 71.7 % | 0.0 % |
| slow_hum_10dB | 46 | 82.6 % | 89.1 % | 0.0 % |
| slow_hum_20dB | 46 | 84.8 % | 87.0 % | 0.0 % |
| slow_hum_5dB | 46 | 78.3 % | 78.3 % | 0.7 % |
| slow_white_0dB | 46 | 82.6 % | 87.0 % | 0.6 % |
| slow_white_10dB | 46 | 84.8 % | 89.1 % | 0.4 % |
| slow_white_20dB | 46 | 82.6 % | 69.6 % | 0.0 % |
| slow_white_5dB | 46 | 80.4 % | 87.0 % | 0.5 % |

</details>
<!-- end:stress -->

## 10. Settings trade-off
Regular recordings of the validation split for several `confidence` settings (the other runtime
settings at their defaults): lower is sooner and less accurate.

<!-- begin:tradeoff -->
| confidence | First reading, median | 90 % by | First within ±3 BPM | Within ±3 BPM at 4 s |
|---|---|---|---|---|
| 0.6 | 3 s | 4.25 s | 88.6 % | 81.5 % |
| 0.65 | 3 s | 4.25 s | 88.6 % | 81.5 % |
| 0.7 (default) | 3 s | 4.25 s | 89.0 % | 81.5 % |
| 0.8 | 3 s | 4.5 s | 89.8 % | 81.5 % |
<!-- end:tradeoff -->

## 11. Corner cases
Synthetic PCG with a known rate (`tests/test_corner_cases.py`). Expected failures are known
limitations, kept as tests so that a fix shows up.

<!-- begin:corner_cases -->
| Check | Cases | Result |
|---|---|---|
| Normal range: first reading by 5 s, within ±fine_bpm. | 4 | 4 pass |
| Outside the normal range: first reading within ±rough_bpm, within ±fine_bpm by latest_s. | 6 | 6 pass |
| Silence: no reading. | 1 | 1 pass |
| Mains hum without heart sounds: no reading. | 5 | 5 pass |
| Noise without heart sounds (room, breath, scratching): no reading for any of 5 seeds. | 3 | 3 pass |
| Finger taps on the chest piece, handling, friction: at most one reading per 3 s of input over 5 seeds (the release gave about one per 2 s for taps and handling). | 3 | 3 pass |
| Rhythmic taps or speech without heart sounds: no reading for any of 5 seeds. | 2 | 1 expected failure, 1 pass |
| -40 dB gain with a DC offset: first reading within one frame of the original, every reading within 0.5 BPM (the DC step adds a band-pass transient at the start). | 1 | 1 pass |
| S2 as loud as or louder than S1: never half or double, within ±fine_bpm by latest_s. | 5 | 5 pass |
| Systolic murmur: within ±fine_bpm by latest_s. | 1 | 1 pass |
| Beat intervals varying by 10 %: first reading within ±rough_bpm of the mean rate. | 1 | 1 pass |
| Handling noise in the first 0.5 s: first reading within ±fine_bpm. | 1 | 1 pass |
| 1.5 s of silence mid-measurement (the measurement may end: contact loss is detected within about 2-3 s): a reading within ±fine_bpm is on show 4 s after the silence. | 1 | 1 pass |
| Stethoscope lifted for 10 s (silence, room noise or hum) and put back: readings stop within 4 s of the lift-off, none appear without the heart, and its return gives a new first reading within ±fine_bpm. | 3 | 3 pass |
| Estimator running on 10 s of off-chest sound (silence or room noise), then the heart: first reading no more than one frame after a fresh start's, within ±fine_bpm (the analysis restarts while no heartbeat is heard). | 2 | 2 pass |
| 50/60 Hz hum and harmonics: within ±fine_bpm by latest_s. | 2 | 2 pass |
| One reading per output period after the first; confidence in [0, 1]. | 1 | 1 pass |
<!-- end:corner_cases -->
