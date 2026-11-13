# Heart-rate algorithm update: combo13 0.14 -> 0.16 (30-200 BPM)

Base: `coral-fw` branch `feature/dbeitel/fw-985-integrate_new_heart_rate_algorithm` (the zip received 2026-10-12).
Scope of the 0.16 port: 4 files, all in `coral/modus/coral/source/heartrate/`. No change to `combo13_stream.h`, `heart_rate.c/.h`, or the tick cadence (`TICK_S` stays 0.5 s).
**Current total scope (all updates below): 5 files** - `combo13_stream.c`, `combo13_stream_internal.h`, `autocorr_templates.h`, `autocorr_templates_detrended.h` (in `heartrate/`) and `audio_io_task.c` (in `fw/`). Start with `HR_0.16_REBOOT_AND_DROPOUT_REPORT.docx`.

## Option A - drop in (simplest)
Copy the 4 files from `heartrate/` in this folder over the same-named files in your tree:
`combo13_stream.c`, `combo13_stream_internal.h`, `autocorr_templates.h`, `autocorr_templates_detrended.h`.
They are based on your tree's 0.14 files (originals kept in `original_0.14/` so you can diff).

## Option B - review/apply as patches
`combo13_stream.c.patch` and `combo13_stream_internal.h.patch` are unified diffs against your 0.14 files
(`git apply` from the repo root after fixing the paths, or `patch -p1`). The two template headers are regenerated
tables; replace them wholesale (they are not hand-editable).

## What the change does
1. **Lag grid extended to 30 BPM** (internal.h): `HI_LAG` 100 -> 146, `S_LEN` 80 -> 126, `EXT_HI_MAX` 227 -> 303; new `MAX_VALID_LAG`, `PEAK_SCAN_END`, `FAST_HI_LAG`/`FAST_LEN` (the old 0.14 range, lags 20..99).
2. **Fast zone stays 0.14-identical** (.c): r_adaptive preview, flux agreement, `estimate_sumenv_robust` peak scan, `find_topN_peaks` and template matching all still use only `FAST_LEN`/`FAST_HI_LAG` / `TEMPLATE_MATCH_N_CURVE` (80 bins).
3. **Slow-zone override** (.c): `estimate_detrend_from_mw()` also computes a 30-40 BPM candidate (`EstimateResult_t` gained `slow_valid/slow_bpm/slow_ratio`); in `c13stream_tick()` it replaces the fast pick only if `slow_ratio >= SLOW_RATIO_MIN` (1.35). Harmonic veto, jump and delay guards still apply to it.
4. **Retuned templates** (2 headers): 34 buckets (one merged 30-40 BPM bucket, then every 5 BPM 40..200) built from the train split of a 3,049-file corpus. `SLOW_RATIO_MIN` 1.5 -> 1.35.
5. `build_region_weights()` fills only 80 entries (only those are read); output-identical, less CPU.

## Impact you need to know about
| | 0.14 | 0.16 |
|---|---|---|
| `c13stream_state_size()` (malloc'd in audio_io_task.c) | 16,112 B | 16,600 B |
| `c13stream_tick` stack frame | 192 B | 200 B |
| `.text` of combo13_stream.o | 17,595 B | 19,371 B |

CPU on the PSoC6 is **not measured**; the slow path adds one extra full-width detrend per tick. Please check the HR task's worst-case time against the 0.5 s tick and the 4 KB stack.
Make sure the heap can supply the extra ~0.5 KB (`malloc` failure leaves HR disabled, per `audio_io_task.c`).

## How to verify
- Build: the 4 files compile with 0 warnings under `-Wall -Wextra -Wshadow -Werror=double-promotion -Werror=vla` (arm-gcc 14.2, cortex-m4 hard-float). A full `build-application.ps1 -Config release` build succeeded with this change.
- `Heart rate algorithm version:` debug print at start-up should read **0.16**.
- Host regression test: `host_test/drv_main.c` drives the firmware API (8-sample int16 `c13stream_push`, then `process_chunk`/`update_chunk`/`tick` every 0.5 s) and prints CSV; `host_test/debug_eko.h` is a one-macro stub. Build with
  `emcc -O2 -sNODERAWFS=1 -I <heartrate dir> -I host_test -o drv.js host_test/drv_main.c <heartrate dir>/combo13_stream.c`, run `node drv.js <raw_float32_file> 4000`.
- Expected on a bench recording of 30-40 BPM: a stable reading appears after the usual ~6 s settling; at >= 40 BPM behaviour matches 0.14 except where the new templates change the pick.

## Test results (host, held-out 836 files, firmware-API flow)
Correct ticks 59.1% -> 60.2%; wrong 8.0% -> 7.8%; files never accurate 3.7% -> 1.4%; 30-40 BPM (17 files) correct ticks 0% -> 40%.
With the new templates + override disabled, output is bit-identical to your 0.14 on all 836 files (grid extension is non-perturbing).
Known minor differences on the two real recordings: `daniel_recovery_test` 1 tick, `daniel_offchest_test` 6 ticks (one doubled 105 BPM raw candidate at 23.5 s and ~1.5 s later display; final values equal).

## Caveats
- Not flashed / not hardware-tested. Local build used the repo's dev keys; not a release image.
- Labels are ECG-derived, DUO3 hardware only; only 17 slow (30-40 BPM) test files, so that result is indicative.
- Time to first accurate HR is unchanged (~6 s, set by `CONS_WAIT_S`).
- Dev-tree numbers (TTFR median 6.5 s) differ from firmware-flow numbers (6.0 s) because the firmware's chunking differs from the dev tree (pre-existing).

## UPDATE 2026-10-15 - two more fixes found on hardware (apply both)
- `audio_io_task.c.patch` / `fw/audio_io_task.c`: 20 ms `vTaskDelay` after each HR task iteration (random watchdog reboots when paired over BLE).
- `heartrate/combo13_stream.c` now also contains a three-way-partition `quickselectf_range()` (old two-way partition was O(n^2) on duplicate-heavy data, giving 2-5 s HR ticks and HR dropouts). Output is bit-identical on 836 files.
- Full write-up, evidence and open questions: `HR_0.16_REBOOT_AND_DROPOUT_REPORT.md` / `.docx`.
- Updated repo zip: `Downloads\coral-fw-...-hr0.16_qsfix.zip`.

## UPDATE 2026-10-19 - parabolic peak interpolation (display-only)
- `heartrate/combo13_stream.c` (`template_match_gated_pick_v2()`, about lines 1158-1176) now refines the winning peak with a 3-point parabola and reports `60*FFS/(lag+d)` instead of `60*FFS/lag`. Scoring and peak selection still use integer bins, so which peak wins is unchanged; only the reported BPM is finer. This removes most of the integer-lag quantization at high BPM.
- Cost: about +116 bytes of code, one float division per tick. Compiles with 0 warnings under the flags above.
- Device sweep (400 clips, same device): pass within +-5 BPM unchanged (358/400 both), pass within +-3 BPM 83.8% -> 86.3%, MAE 3.01 -> 2.74 BPM. See the report, section 6.
- `combo13_stream.c.patch` was regenerated against `original_0.14/` and includes the quickselect fix and the interpolation. `COMBO13_VERSION` is still "0.16"; bump it if you want the interpolation build identifiable.
