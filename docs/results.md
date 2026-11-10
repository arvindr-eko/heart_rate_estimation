# Results

Evaluation of the current version: test and validation results per group, targets, time to
converge, accuracy over time, first-reading error, strata, stress tests, settings trade-off and
corner cases. Every table and figure below is generated from the runs named here;
aggregates only. The C port's agreement with the Python reference and
the resources: [realtime_implementation.md](realtime_implementation.md).

<!-- begin:results_header -->
| Version | Model | Generated | Commit | Runs |
|---|---|---|---|---|
| 1.0.0 | hr_v8 | 2026-11-09 | 77e69ad | val `20261109_170159_hr_v8_val_v1.0.0`, test `20261109_170212_hr_v8_test_v1.0.0`, stress `20261109_171914_hr_v8_test_v1.0.0_stress` |
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
| first reading, median / 90 % by | 2.5 s / 4 s | 4 s / 6.25 s | 4 s / 5.25 s | 3.5 s / 4.25 s | 4 s / 7.75 s |
| first reading within ±3 BPM | 87.7 % | 100.0 % | 51.1 % | 76.0 % | 46.7 % |
| first reading within ±10 BPM | 99.5 % | 100.0 % | 76.2 % | 91.7 % | 66.7 % |
| within ±3 BPM at 4 / 6 / 9 s | 90.0 % / 99.3 % / 98.9 % | 56.7 % / 90.0 % / 100.0 % | 41.7 % / 72.3 % / 81.3 % | 72.4 % / 89.9 % / 93.0 % | 26.7 % / 13.3 % / 40.0 % |
| converged, median / 90 % by | 3 s / 4.25 s | 4 s / 6.25 s | 10 s / not reached | 4 s / 15 s | 16.75 s / not reached |
| half/double readings | 0.0 % | 0.0 % | 0.6 % | 0.2 % | 0.0 % |
| mean absolute error; 95 % limits of agreement | 0.57; ±2.8 BPM | 0.38; ±1.1 BPM | 3.04; ±13.9 BPM | 1.36; ±8.2 BPM | 5.67; ±13.6 BPM |
| bias (mean signed error) | -0.19 BPM | -0.01 BPM | -0.11 BPM | -0.16 BPM | +3.61 BPM |

Recordings without a trusted ECG (117): first reading within the EC13 tolerance of the production
ECG HR (EFAST) in 95.7 %.
<!-- end:test -->

## 3. Targets
<!-- begin:targets -->
| Group | Metric | Value | Target | Result |
|---|---|---|---|---|
| regular, 50-200 BPM | first reading, median | 2.5 s | 3 s | pass |
| regular, 50-200 BPM | first reading, 90 % by | 4 s | 4 s | pass |
| regular, 50-200 BPM | first reading within ±3 BPM | 87.7 % | 92.0 % | **fail** |
| regular, 50-200 BPM | within ±3 BPM at 4 s | 90.0 % | 92.0 % | **fail** |
| slow, < 50 BPM | first reading, median | 4 s | 5 s | pass |
| slow, < 50 BPM | first reading within ±10 BPM | 100.0 % | 95.0 % | pass |
| slow, < 50 BPM | within ±3 BPM at 9 s | 100.0 % | 90.0 % | pass |
| irregular or AF | first reading, median | 4 s | 6 s | pass |
| irregular or AF | first reading within ±10 BPM | 76.2 % | 90.0 % | **fail** |
| irregular or AF | within ±3 BPM at 9 s | 81.3 % | 60.0 % | pass |
| all | reading by 10 s | 99.9 % | 99.0 % | pass |
| all | half/double readings | 0.2 % | 0.5 % | pass |
| very fast | first reading within ±10 BPM | 81.9 % | 90.0 % | **fail** |
| very fast | half/double readings | 7.2 % | 1.0 % | **fail** |
<!-- end:targets -->

## 4. Time to converge
Converged: from that reading on, every reading to the end of the recording is within ±3 BPM.
Left panel: share of recordings with a first reading by each time; right: share converged.

<!-- begin:convergence -->
| Group | Recordings | First reading, median | Converged: median / 75 % / 90 % by | Converged by 4 / 6 / 9 / 12 s | Not converged * |
|---|---|---|---|---|---|
| regular, 50-200 BPM | 438 | 2.5 s | 3 s / 4 s / 4.25 s | 88.8 % / 97.9 % / 98.4 % / 99.3 % | 0.2 % |
| slow, < 50 BPM | 30 | 4 s | 4 s / 4.75 s / 6.25 s | 56.7 % / 90.0 % / 100.0 % / 100.0 % | 0.0 % |
| irregular or AF | 235 | 4 s | 10 s / 15.75 s / not reached | 13.2 % / 33.2 % / 48.5 % / 59.1 % | 12.8 % |
| all | 703 | 3.5 s | 4 s / 6 s / 15 s | 62.2 % / 76.0 % / 81.8 % / 85.9 % | 4.4 % |
| AF only | 15 | 4 s | 16.75 s / not reached / not reached | 0.0 % / 0.0 % / 0.0 % / 0.0 % | 46.7 % |

\* last reading of the 17 s recording outside ±3 BPM.

![First reading and convergence over time](figures/convergence.png)
<!-- end:convergence -->

## 5. Accuracy over time
<!-- begin:accuracy_over_time -->
| Group | 2 s | 3 s | 4 s | 5 s | 6 s | 8 s | 9 s | 10 s |
|---|---|---|---|---|---|---|---|---|
| regular, 50-200 BPM | 32.9 % | 51.6 % | 90.0 % | 97.5 % | 99.3 % | 99.5 % | 98.9 % | 99.1 % |
| slow, < 50 BPM | 3.3 % | 3.3 % | 56.7 % | 86.7 % | 90.0 % | 100.0 % | 100.0 % | 100.0 % |
| irregular or AF | 11.1 % | 18.3 % | 41.7 % | 59.6 % | 72.3 % | 81.7 % | 81.3 % | 82.1 % |
| all | 24.3 % | 38.4 % | 72.4 % | 84.4 % | 89.9 % | 93.6 % | 93.0 % | 93.5 % |

![Accuracy over time](figures/accuracy_over_time.png)
<!-- end:accuracy_over_time -->

## 6. First-reading error
Absolute difference between the first reading and the ECG heart rate over the same seconds.

<!-- begin:first_error -->
| Group | First readings | Median |error| (BPM) | 90th percentile | 95th percentile | Largest |
|---|---|---|---|---|---|
| regular, 50-200 BPM | 438 | 0.96 | 3.51 | 4.89 | 12.7 |
| slow, < 50 BPM | 30 | 0.41 | 1.31 | 1.94 | 2.5 |
| irregular or AF | 235 | 2.91 | 18.73 | 29.12 | 76.7 |
| all | 703 | 1.16 | 7.94 | 13.50 | 76.7 |

![First-reading error](figures/first_error.png)
<!-- end:first_error -->

## 7. Strata
Test recordings with a trusted ECG, by device, firmware, mains filter setting, rhythm class and
10 BPM band of the reference HR; small strata show their count.

<!-- begin:strata -->
| Stratum | Value | Recordings | First within ±3 BPM | First within ±10 BPM | First reading, median | Within ±3 BPM at 9 s |
|---|---|---|---|---|---|---|
| device | DUO3 | 703 | 76.0 % | 91.7 % | 3.5 s | 93.0 % |
| firmware | 1.0.11 | 218 | 75.2 % | 90.4 % | 3.75 s | 93.1 % |
| firmware | 1.0.12 | 1 | 100.0 % | 100.0 % | 2.5 s | 100.0 % |
| firmware | 1.0.4 | 6 | 83.3 % | 100.0 % | 4 s | 83.3 % |
| firmware | 1.0.7 | 3 | 100.0 % | 100.0 % | 4 s | 66.7 % |
| firmware | 1.0.8 | 27 | 81.5 % | 88.9 % | 4 s | 88.9 % |
| firmware | 1.0.9 | 1 | 100.0 % | 100.0 % | 4 s | 100.0 % |
| firmware | 1.1.1 | 104 | 76.0 % | 96.2 % | 2 s | 98.1 % |
| firmware | 1.1.2 | 342 | 75.7 % | 91.5 % | 3.75 s | 92.1 % |
| firmware | 1.2.0 | 1 | 0.0 % | 0.0 % | 5 s | 100.0 % |
| mains filter (Hz) | 50 | 22 | 68.2 % | 86.4 % | 2.5 s | 90.9 % |
| mains filter (Hz) | 60 | 681 | 76.2 % | 91.9 % | 3.5 s | 93.1 % |
| rhythm (EFAST class) | Atrial Fibrillation | 15 | 46.7 % | 66.7 % | 4 s | 40.0 % |
| rhythm (EFAST class) | Normal Sinus Rhythm | 174 | 75.3 % | 92.0 % | 2.75 s | 97.1 % |
| rhythm (EFAST class) | Sinus Bradycardia | 66 | 78.8 % | 86.4 % | 4 s | 93.9 % |
| rhythm (EFAST class) | Sinus Tachycardia | 257 | 79.0 % | 94.2 % | 2.25 s | 98.1 % |
| rhythm (EFAST class) | Unspecified Rhythm | 191 | 73.8 % | 92.1 % | 4 s | 86.4 % |
| reference HR | 30-39 BPM | 4 | 100.0 % | 100.0 % | 5 s | 100.0 % |
| reference HR | 40-49 BPM | 42 | 73.8 % | 78.6 % | 4.25 s | 92.9 % |
| reference HR | 50-59 BPM | 43 | 74.4 % | 90.7 % | 4 s | 86.0 % |
| reference HR | 60-69 BPM | 64 | 73.4 % | 85.9 % | 4 s | 93.8 % |
| reference HR | 70-79 BPM | 51 | 76.5 % | 92.2 % | 2.75 s | 92.2 % |
| reference HR | 80-89 BPM | 52 | 78.8 % | 96.2 % | 3.25 s | 92.3 % |
| reference HR | 90-99 BPM | 60 | 71.7 % | 90.0 % | 2.25 s | 98.3 % |
| reference HR | 100-109 BPM | 51 | 88.2 % | 96.1 % | 4 s | 92.2 % |
| reference HR | 110-119 BPM | 53 | 75.5 % | 86.8 % | 2.5 s | 92.5 % |
| reference HR | 120-129 BPM | 54 | 74.1 % | 94.4 % | 2 s | 90.7 % |
| reference HR | 130-139 BPM | 49 | 69.4 % | 85.7 % | 3.75 s | 91.8 % |
| reference HR | 140-149 BPM | 52 | 73.1 % | 96.2 % | 2 s | 88.5 % |
| reference HR | 150-159 BPM | 56 | 75.0 % | 98.2 % | 1.75 s | 96.4 % |
| reference HR | 160-169 BPM | 34 | 85.3 % | 97.1 % | 1.75 s | 97.1 % |
| reference HR | 170-179 BPM | 22 | 72.7 % | 95.5 % | 2.5 s | 100.0 % |
| reference HR | 180-189 BPM | 11 | 81.8 % | 100.0 % | 4 s | 100.0 % |
| reference HR | 190-199 BPM | 5 | 80.0 % | 100.0 % | 4 s | 80.0 % |
<!-- end:strata -->

## 8. Validation
Validation split, the same settings; used for tuning, so its results are not an independent
estimate.

<!-- begin:validation -->
| Group | Recordings | First reading, median | First within ±3 BPM | First within ±10 BPM | Within ±3 BPM at 4 s | At 9 s | Half/double |
|---|---|---|---|---|---|---|---|
| regular, 50-200 BPM | 254 | 3 s | 85.4 % | 98.8 % | 88.2 % | 99.2 % | 0.3 % |
| slow, < 50 BPM | 20 | 4.25 s | 100.0 % | 100.0 % | 50.0 % | 100.0 % | 0.0 % |
| irregular or AF | 191 | 4 s | 51.8 % | 80.1 % | 44.5 % | 77.0 % | 1.1 % |
| all | 465 | 4 s | 72.3 % | 91.2 % | 68.6 % | 90.1 % | 0.6 % |

Targets met on validation: 8 of 12.
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
| noise: white, friction, hum, breath at 0-20 dB SNR | 16 | 166 | 89.2 % - 92.2 % | 91.0 % - 95.2 % | 0.1 % - 0.4 % |
| slow recordings under the same noise | 16 | 46 | 73.9 % - 84.8 % | 84.8 % - 95.7 % | 0.0 % - 0.8 % |
| slowed to 32-44 BPM (clean, white noise 10 dB) | 6 | 40 | 82.5 % - 95.0 % | 100.0 % - 100.0 % | 0.2 % - 2.5 % |
| sped up to 170-230 BPM | 8 | 40 | 95.0 % - 100.0 % | 95.0 % - 100.0 % | 0.0 % - 2.9 % |
| sped up to 250 BPM | 2 | 40 | 82.5 % - 85.0 % | 77.5 % - 82.5 % | 7.0 % - 7.7 % |
| sped up to 280-300 BPM | 4 | 40 | 52.5 % - 67.5 % | 32.5 % - 82.5 % | 11.8 % - 24.5 % |

<details><summary>All conditions</summary>

| Condition | Recordings | First within ±10 BPM | Within ±3 BPM at 9 s | Half/double |
|---|---|---|---|---|
| noise_breath_0dB | 166 | 89.8 % | 94.6 % | 0.2 % |
| noise_breath_10dB | 166 | 90.4 % | 92.2 % | 0.2 % |
| noise_breath_20dB | 166 | 92.2 % | 92.8 % | 0.1 % |
| noise_breath_5dB | 166 | 90.4 % | 93.4 % | 0.1 % |
| noise_friction_0dB | 166 | 89.8 % | 94.0 % | 0.1 % |
| noise_friction_10dB | 166 | 90.4 % | 92.8 % | 0.1 % |
| noise_friction_20dB | 166 | 90.4 % | 93.4 % | 0.1 % |
| noise_friction_5dB | 166 | 89.2 % | 94.0 % | 0.1 % |
| noise_hum_0dB | 166 | 91.0 % | 94.0 % | 0.4 % |
| noise_hum_10dB | 166 | 89.8 % | 92.2 % | 0.3 % |
| noise_hum_20dB | 166 | 92.2 % | 95.2 % | 0.1 % |
| noise_hum_5dB | 166 | 89.2 % | 91.0 % | 0.3 % |
| noise_white_0dB | 166 | 89.8 % | 93.4 % | 0.1 % |
| noise_white_10dB | 166 | 89.8 % | 92.2 % | 0.1 % |
| noise_white_20dB | 166 | 90.4 % | 94.0 % | 0.1 % |
| noise_white_5dB | 166 | 89.8 % | 93.4 % | 0.1 % |
| rate_clean_170bpm | 40 | 97.5 % | 95.0 % | 0.0 % |
| rate_clean_190bpm | 40 | 100.0 % | 100.0 % | 0.0 % |
| rate_clean_210bpm | 40 | 97.5 % | 97.5 % | 0.0 % |
| rate_clean_230bpm | 40 | 100.0 % | 97.5 % | 0.0 % |
| rate_clean_250bpm | 40 | 85.0 % | 77.5 % | 7.0 % |
| rate_clean_280bpm | 40 | 67.5 % | 82.5 % | 11.8 % |
| rate_clean_300bpm | 40 | 52.5 % | 32.5 % | 24.5 % |
| rate_clean_32bpm | 40 | 92.5 % | 100.0 % | 1.5 % |
| rate_clean_38bpm | 40 | 87.5 % | 100.0 % | 0.9 % |
| rate_clean_44bpm | 40 | 95.0 % | 100.0 % | 0.7 % |
| rate_white_10dB_170bpm | 40 | 97.5 % | 95.0 % | 0.0 % |
| rate_white_10dB_190bpm | 40 | 100.0 % | 95.0 % | 2.9 % |
| rate_white_10dB_210bpm | 40 | 97.5 % | 97.5 % | 0.0 % |
| rate_white_10dB_230bpm | 40 | 95.0 % | 95.0 % | 2.4 % |
| rate_white_10dB_250bpm | 40 | 82.5 % | 82.5 % | 7.7 % |
| rate_white_10dB_280bpm | 40 | 67.5 % | 77.5 % | 19.1 % |
| rate_white_10dB_300bpm | 40 | 60.0 % | 32.5 % | 24.1 % |
| rate_white_10dB_32bpm | 40 | 87.5 % | 100.0 % | 2.5 % |
| rate_white_10dB_38bpm | 40 | 82.5 % | 100.0 % | 1.1 % |
| rate_white_10dB_44bpm | 40 | 87.5 % | 100.0 % | 0.2 % |
| slow_breath_0dB | 46 | 82.6 % | 93.5 % | 0.2 % |
| slow_breath_10dB | 46 | 78.3 % | 93.5 % | 0.5 % |
| slow_breath_20dB | 46 | 82.6 % | 93.5 % | 0.2 % |
| slow_breath_5dB | 46 | 73.9 % | 93.5 % | 0.7 % |
| slow_friction_0dB | 46 | 84.8 % | 91.3 % | 0.0 % |
| slow_friction_10dB | 46 | 78.3 % | 93.5 % | 0.2 % |
| slow_friction_20dB | 46 | 82.6 % | 95.7 % | 0.2 % |
| slow_friction_5dB | 46 | 78.3 % | 89.1 % | 0.2 % |
| slow_hum_0dB | 46 | 84.8 % | 84.8 % | 0.6 % |
| slow_hum_10dB | 46 | 76.1 % | 95.7 % | 0.8 % |
| slow_hum_20dB | 46 | 80.4 % | 93.5 % | 0.2 % |
| slow_hum_5dB | 46 | 78.3 % | 93.5 % | 0.2 % |
| slow_white_0dB | 46 | 78.3 % | 89.1 % | 0.7 % |
| slow_white_10dB | 46 | 78.3 % | 95.7 % | 0.3 % |
| slow_white_20dB | 46 | 80.4 % | 95.7 % | 0.2 % |
| slow_white_5dB | 46 | 82.6 % | 95.7 % | 0.2 % |

</details>
<!-- end:stress -->

## 10. Settings trade-off
Regular recordings of the validation split for several `confidence` settings (the other runtime
settings at their defaults): lower is sooner and less accurate.

<!-- begin:tradeoff -->
| confidence | First reading, median | 90 % by | First within ±3 BPM | Within ±3 BPM at 4 s |
|---|---|---|---|---|
| 0.6 | 2.75 s | 4 s | 84.3 % | 88.6 % |
| 0.65 | 3 s | 4 s | 85.0 % | 88.2 % |
| 0.7 (default) | 3 s | 4 s | 85.4 % | 88.2 % |
| 0.8 | 3.25 s | 4 s | 86.6 % | 87.8 % |
<!-- end:tradeoff -->

## 11. Corner cases
Synthetic PCG with a known rate (`tests/test_corner_cases.py`). Expected failures are known
limitations, kept as tests so that a fix shows up.

<!-- begin:corner_cases -->
| Check | Cases | Result |
|---|---|---|
| Normal range: first reading by 4 s, within ±fine_bpm. | 4 | 4 pass |
| Outside the normal range: first reading within ±rough_bpm, within ±fine_bpm by latest_s. | 6 | 6 pass |
| Silence: no reading. | 1 | 1 pass |
| Mains hum without heart sounds: no reading. | 5 | 5 pass |
| White, breath or friction noise without heart sounds: no reading for any of 5 seeds. | 3 | 3 expected failure |
| -40 dB gain with a DC offset: first reading within one frame of the original, every reading within 0.5 BPM (the DC step adds a band-pass transient at the start). | 1 | 1 pass |
| S2 as loud as or louder than S1: never half or double, within ±fine_bpm by latest_s. | 5 | 5 pass |
| Systolic murmur: within ±fine_bpm by latest_s. | 1 | 1 pass |
| Beat intervals varying by 10 %: first reading within ±rough_bpm of the mean rate. | 1 | 1 pass |
| Handling noise in the first 0.5 s: first reading within ±fine_bpm. | 1 | 1 pass |
| 1.5 s of silence mid-measurement: the measurement goes on (no restart) and the reading on show is within ±fine_bpm 4 s later. | 1 | 1 pass |
| Stethoscope lifted for 10 s (silence, room noise or hum) and put back: readings stop within 6 s of the lift-off, none appear without the heart, and its return gives a new first reading within ±fine_bpm. | 3 | 3 pass |
| 50/60 Hz hum and harmonics: within ±fine_bpm by latest_s. | 2 | 2 pass |
| One reading per output period after the first; confidence in [0, 1]. | 1 | 1 pass |
<!-- end:corner_cases -->
