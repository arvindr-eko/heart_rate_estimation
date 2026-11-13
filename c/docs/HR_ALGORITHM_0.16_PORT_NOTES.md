# Combo13 heart-rate algorithm: 0.14 -> 0.16 port (2026-10-12)

Files changed, all under `coral/modus/coral/source/heartrate/`:
`combo13_stream.c`, `combo13_stream_internal.h`, `autocorr_templates.h`, `autocorr_templates_detrended.h`.
`combo13_stream.h`, `audio_io_task.c`, `heart_rate.c/.h` are untouched; the public API and `TICK_S` (0.5 s) are unchanged.

## What changed
- **0.15 - lower bound 40 -> 30 BPM.** Lag grid extended on the slow end (`HI_LAG` 100 -> 146, `S_LEN` 80 -> 126,
  `EXT_HI_MAX` 227 -> 303). Everything from 0.14 still looks only at the "fast zone" (bins 0..79, lags 20..99) with the same
  detrend context. A slow-zone (30-40 BPM) peak replaces the fast-zone pick only when its detrended height is
  >= `SLOW_RATIO_MIN` x the tallest fast peak. `estimate_detrend_from_mw()` now also produces that slow candidate
  (`EstimateResult_t` gained `slow_valid/slow_bpm/slow_ratio`).
- **0.16 - retune.** `SLOW_RATIO_MIN` 1.5 -> 1.35; morphology templates re-baked from the train split of the 3,049-file
  combined corpus (34 buckets: one merged 30-40 BPM bucket, then every 5 BPM 40..200; was 28 buckets 45..190).
- `build_region_weights()` now fills only the template-width prefix (`TEMPLATE_MATCH_N_CURVE` = 80) of the weights
  array, since only that prefix is ever read. Output-identical, saves ~46 Gaussian-LUT evaluations x 5 per scored peak.

## Resource impact (host measurement, arm-none-eabi-gcc 14.2 -O2 cortex-m4 hard-float)
| | 0.14 | 0.16 |
|---|---|---|
| `c13stream_state_size()` (heap, malloc'd by audio_io_task.c) | 16,112 B | 16,600 B (+488) |
| `c13stream_tick` stack frame | 192 B | 200 B |
| `combo13_stream.o` text | 17,595 B | 19,371 B (+1,776, includes +6 template rows x 2 tables) |

CPU on the PSoC6 is NOT measured. The slow-zone path adds one extra full-width detrend per tick.
Compiles with 0 warnings under `-Wall -Wextra -Wshadow -Werror=double-promotion -Werror=vla`.

## Validation (host build of this exact source, firmware call pattern: 8-sample int16 pushes, process/update/tick per 0.5 s)
- With the new templates and the slow override disabled, output is bit-identical to the original 0.14 firmware source on
  836/836 held-out test files (proves the grid extension does not perturb the >= 40 BPM path).
- Held-out test set (836 files, never used for templates or tuning), original 0.14 firmware vs this 0.16:
  correct ticks 59.1% -> 60.2%; wrong ticks 8.0% -> 7.8%; files never accurate 3.7% -> 1.4%;
  30-40 BPM (17 files): correct ticks 0% -> 40%.
- Not verified on hardware.
