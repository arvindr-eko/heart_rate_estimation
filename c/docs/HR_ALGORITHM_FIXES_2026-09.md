# Coral HR Algorithm Firmware Fixes — 2026-08-31 → 2026-09-01

**Scope:** `coral-fw`, branch `feature/dbeitel/fw-985-integrate_new_heart_rate_algorithm`
**Status:** Applied and hardware-verified on a local build (`C:\cfw`, dev-key signed). **Not yet
merged upstream** — this document is meant to support that review.
**Files touched:** `source/heartrate/combo13_stream.c`, `source/heartrate/combo13_stream_internal.h`,
`source/heart_rate.c`, `source/heart_rate.h`, `source/audio_io_task.c`, `source/eko_app/screens.c`

---

## Summary

Three independent, real bugs were found and fixed, in this order:

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | Slower time-to-first-HR-value than the PC/WASM reference | Chunk-processing moved from `push()` to `tick()`, with a buffer sized to silently discard audio under scheduling jitter | Reverted to the reference kit's push()-triggered chunk processing |
| 2 | Still slower than the PC reference even after fix #1 | A second, legacy median filter (pre-dating combo13) stacked its own multi-reading warm-up on top of combo13's already-complete settling logic | Removed the legacy filter; combo13's own `bpm_valid` is now trusted directly |
| 3 | Audible audio popping every 2-3s (worse than the ~15-20s baseline) | `c13stream_push()`'s real DSP cost was running inline on the highest-priority, real-time audio task | Moved `c13stream_push()` to the low-priority HR task via a stream buffer; `c13stream_tick()` was already correctly on that task |

A fourth, purely cosmetic change is also included: the power-off screen's version string now reads
`0.6.4hr` instead of `0.6.4`, to mark this build lineage (see the last section).

---

## Bug #1: Time-to-first-HR regression (chunk-processing location)

### Symptom

Displayed HR value on real hardware took noticeably longer to appear than on the PC WASM reference
build (`pcg_visualizer`), running the same combo13 revision 0.14 against the same live audio.

### Root cause

The firmware's port of `combo13_stream.c` moved the chunk-processing step
(`process_and_emit_chunk()` — artifact detect/extend/interpolate, spike detection, baseline
updates, 3-band envelope filtering) out of `c13stream_push()`, where the validated reference kit
(`c13_core_kit_v0.14`) calls it the instant a 68-decimated-sample chunk fills, and into
`c13stream_tick()` instead — processing whatever had accumulated once per tick (every 0.5s).

To make room for a full tick's worth of backlog (~167 decimated samples), `DEC_CHUNK_N` was
tripled from 68 to 204. This left only ~22% margin before `c13stream_push()`'s own overflow guard:

```c
if (st->dec_chunk_len == DEC_CHUNK_N) {
  DEBUG_PRINTF("Chunk overflow\n");
  // process_and_emit_chunk(st);   <- dead code, never actually ran
  st->dec_chunk_len = 0;           <- silently discards the whole buffered chunk
}
```

Under real-world FreeRTOS scheduling (BLE + audio + accelerometer + HR all contending for CPU, and
`AudioHRTask` woken via a non-counting `xTaskNotify(..., eNoAction)`), the low-priority HR task
could run late relative to real time. When it did, this guard silently threw away real audio —
`dec_sample_count` (which gates how much history the algorithm considers available) fell behind
real elapsed time, meaning more real seconds were needed to accumulate the same usable analysis
window. This was not part of the documented `TICK_S` 0.25→0.5s coordination requirement — it was an
unrelated restructuring introduced during firmware integration.

### Fix

Reverted to the reference kit's design: `process_and_emit_chunk()` fires inside `c13stream_push()`
the moment a fixed 68-sample chunk completes, independent of tick cadence. `DEC_CHUNK_N` reverted
to 68. The silent-discard overflow branch is gone (chunks can no longer back up past capacity in
the first place, since they're drained as they complete rather than batched per tick).

**Files:** `combo13_stream_internal.h` (`DEC_CHUNK_N` 204→68), `combo13_stream.c`
(`process_and_emit_chunk()` signature/call site, `c13stream_push()`, `c13stream_tick()`).

### Verification

- Diffed the firmware's `combo13_stream.c`/`.h` against the trusted `c13_core_kit_v0.14` reference
  kit to find the divergence.
- After the fix, `grep`-confirmed `DEC_CHUNK_N 68` and the restored `process_and_emit_chunk(st)`
  call site were present in the actual compiled build tree (not just edited-and-assumed).
- Daniel flashed the resulting `.cyacd2` and confirmed on real hardware: "that works really well" —
  time-to-first-HR now matches the PC reference.

---

## Bug #2: Legacy median filter double-stacking a warm-up

### Symptom

Even after fix #1, the on-device displayed HR value still lagged the PC visualizer noticeably,
despite both processing the same live audio via the same combo13 algorithm in parallel (PC over the
BLE-relayed audio stream, device natively — so if anything the device has a data-timing advantage).

### Root cause

`heart_rate.c` — a file that predates combo13, ported from the old on-chip AWE HR algorithm (per its
own header comments) — implements a second median filter sitting between combo13's output and the
display/BLE path:

```c
if (kRetCodeOk == read_ret_code) {
  HeartRate_UpdateFilterState(hr_value);   // <- feeds the legacy filter
}
...
heart_rate_reported_1hz = HeartRate_GetFilteredValue();   // <- what's actually displayed/sent
```

This filter required 4-10 mutually-agreeing readings (bpm-range dependent, each 0.5s apart) before
ever reporting a value — on top of combo13's own internal settling wait, jump confirmation,
bpm-dependent confirmation delay, and weighted-median combine, which already do this job. The two
mechanisms were stacked, each adding its own several-second warm-up.

A second, related gap found in the same code path: `audio_io_task.c` was feeding `heart_rate.bpm`
into this filter **unconditionally**, never checking combo13's own `bpm_valid`/`is_flatline` flags.
The legacy filter's agreement-count was effectively the *only* thing preventing garbage/settling-
phase values from being reported — another reason it couldn't simply be deleted without also fixing
what feeds it.

### Fix

`heart_rate.c` was reduced to a direct passthrough — stores the latest reported value, returns it
(or the CLI test-override value via `HeartRate_OverrideHrSet`, preserved exactly as before). The
circular buffer, insertion sort, percent-diff/neighbor-agreement threshold logic, and the
range-dependent minimum-reading-count logic were all removed.

`audio_io_task.c` now gates the value on combo13's own validity flags before it's ever stored:

```c
hr_value = (heart_rate.bpm_valid && !heart_rate.is_flatline) ? (int32_t)(heart_rate.bpm + 0.5) : -1;
```

`-1` is the same "invalid" sentinel already used throughout this codebase; it was confirmed (by
reading `DisplayBpmUpdate()` and `DisplayStatusBarUpdate()` directly) to correctly flow through the
existing out-of-range "--" display convention (`kMinDisplayHR`/`kMaxDisplayHR` = 30/200) without
requiring any change to the display code itself.

**Files:** `heart_rate.c` (rewritten), `heart_rate.h` (doc comments updated to match), `audio_io_task.c`
(the `hr_value` computation in `ConfigureNextI2SOutputTransfer()`).

### Verification

- Read every call site of `heart_rate.c`'s public API (`screens.c` ×2, `cli_commands.c`'s test
  override, `sensor_output.c`'s noise-gate reset) before removing anything, to confirm none would
  break.
- Confirmed the `-1` sentinel's existing propagation path by reading the actual display functions
  rather than assuming.
- Rebuilt clean (zero new warnings), confirmed the new logic present in the compiled tree.

---

## Bug #3: Audio popping every 2-3 seconds

### Symptom

After the above fixes, Daniel reported new audible popping in the audio output, more frequent than
the baseline popping present on other builds (every 2-3s vs. ~15-20s).

### Root cause

`AudioHRTask` (runs `c13stream_tick()` — autocorrelation, template matching, harmonic veto) is
deliberately set to the *lowest* FreeRTOS priority (`kTasksPriorityAudioHr =
CY_RTOS_PRIORITY_BELOWNORMAL`, in `task_priorities_m4.h`), correctly below the real-time audio path
(`AudioIOTask` = ABOVENORMAL, the AWE high-priority pump = NORMAL).

However, per the bug #1 fix, `c13stream_push()` — which now runs the real DSP cost of
`process_and_emit_chunk()` inline — was being called directly from `AudioIOTask` itself, i.e. on
the *highest* priority, directly inside the same hot path that services the I2S/PDM DMA buffers.
Correctly fixing bug #1 traded one problem for another: the chunk-processing location was now
algorithmically correct, but colocated with hard-real-time audio work it hadn't been running
alongside before.

### Diagnosis method

Two sequential A/B hardware tests, each built as its own `.cyacd2` and confirmed compiled-in
(via `grep`/binary timestamp, not just visual inspection of source) before Daniel flashed anything:

1. **combo13 fully disabled** (no init/malloc, no push, no tick, display forced to a constant 0) —
   confirmed: **popping gone.** This proved combo13 was involved, without yet identifying which
   half of it.
2. **`c13stream_push()` moved to the low-priority task, `c13stream_tick()` left untouched** —
   `AudioIOTask` was changed to do a cheap `xStreamBufferSend()` of raw samples into a new stream
   buffer (reusing this file's existing playback-stream-buffer pattern), and `AudioHRTask` was
   changed to drain that buffer and call `c13stream_push()` itself, immediately before
   `c13stream_tick()`. Confirmed: **popping gone, same as test 1.** This isolates the cause
   precisely to `process_and_emit_chunk()`'s priority placement, not combo13's total CPU/RAM
   footprint — and `combo13_stream.c`/`.h` were untouched by this test, keeping the validated
   algorithm kit byte-identical to the reference.

### Fix (made permanent after both tests confirmed the hypothesis)

```c
// AudioIOTask's hot path (was: c13stream_push(heart_state, audioBufferForIPCM4toM0, 8) inline):
xStreamBufferSend(hr_audio_stream_buffer_handle, audioBufferForIPCM4toM0, 8 * sizeof(int16_t), 0);

// AudioHRTask (added before the existing c13stream_tick() call):
while ((received = xStreamBufferReceive(hr_audio_stream_buffer_handle, hr_drain_buf,
                                        sizeof(hr_drain_buf), 0)) > 0) {
  c13stream_push(heart_state, hr_drain_buf, (int)(received / sizeof(int16_t)));
}
c13stream_tick(heart_state, &heart_rate);
```

The stream buffer is sized for ~1s of backlog (4000 samples) against a 0.5s drain cadence — a large
safety margin, not a tightly-fit buffer like the one that caused bug #1's data loss. Overflow (if
it ever happened) is counted (`hr_audio_stream_buffer_overflow_count`), never silently dropped.

Both diagnostic flags used during testing (`HR_ALGORITHM_DISABLED_FOR_POP_TEST`,
`HR_DEFER_PUSH_TO_LOW_PRIORITY_TASK`) were removed once test 2 confirmed the fix — the
push-deferral-via-stream-buffer is now the sole, unconditional architecture, and the old
"call `c13stream_push()` inline from `AudioIOTask`" code path no longer exists at all.

**Files:** `audio_io_task.c` (new stream buffer declaration + creation, the `AudioIOTask` hot-path
call site, `AudioHRTask`'s body).

### Verification

- Confirmed task priority assignments by reading `task_priorities_m4.h` directly rather than
  assuming.
- Both A/B builds confirmed compiled-in (flag values + relevant symbols present in the built `.elf`)
  before either was handed to Daniel to flash.
- Final permanent build rebuilt clean after removing the flags; confirmed zero warnings and the
  `hr_audio_stream_buffer` symbols present in the final `.elf`.
- Daniel confirmed on real hardware: pops gone in both A/B tests, and the final permanent build
  behaves the same as test 2.

---

## Cosmetic change: power-off version string

`DisplayTwistOff()` in `screens.c` (shown when the device powers off via its twist switch — the
main manual power-off path, confirmed via its call site in `app_task.c`'s `AppStateOffEnter`,
`kSystemPowerOffReasonTwistOff` case) now displays `0.6.4hr` instead of `0.6.4`, to mark this build
lineage. Two other places in `screens.c` build a similar bare version string
(`DisplaySplashScreen`'s boot-screen display, gated behind `NO_IDLE_BUILD || DEBUG_UART_ENABLED`
and off by default; and dead/commented-out code in `DisplayPowerOffConfirmStart`) were left
untouched — neither is the string actually observed on-device. `common.h` already had an
`HR_TEST_BUILD` flag with a similar-purpose `"h"` version modifier, but it was deliberately not
reused here since that same flag also changes the BLE HR reporting rate from 1Hz to 2Hz elsewhere
in `audio_io_task.c` — an unrelated side effect this change didn't intend to introduce.

---

## What's NOT done yet

- **None of this has gone through the firmware team's normal PR/review process.** Everything above
  was applied directly to a local zip export on a development machine (`C:\cfw`), not a tracked
  branch or pull request.
- **Every `.cyacd2` built so far is signed with the repo's checked-in non-production dev keys**
  (`coral/Keys.cylib` — the same keys CI's "combined" stage uses on every branch/PR), for
  bench-testing only. None of this has been through the real production pipeline (production keys,
  QA) required before it ships.
- The git-describe string embedded in these builds reads `"local-build-snapshot-dirty"` (no real
  git history was available from a zip export) — an intentional marker so these builds can't be
  mistaken for an official release.

**Next step:** get all four changes above reviewed and merged into
`feature/dbeitel/fw-985-integrate_new_heart_rate_algorithm` (or wherever they should land) through
the normal process, then rebuilt through the real CI/production pipeline.
