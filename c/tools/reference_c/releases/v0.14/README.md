# Combo13 streaming heart-rate estimator — revision 0.14

Embedded-C port of the combo13 PCG (phonocardiogram) heart-rate algorithm for
PSoC6/Cortex-M4F, 400Hz-family decimation streaming front-end
(`STREAM_VARIANT=4`). **This is the current shipped revision.**

## What changed in this revision (0.12 → 0.14)

Two real, deliberate behavior changes, each individually corpus-validated and
approved before being combined. **Neither is a no-op refactor — both trade a
small amount of accuracy for a real reduction in clock cycles and/or RAM.**
If you need the exact pre-change baseline for comparison, that's revision
0.12 (`Desktop\pcg_abab\v0.12\`).

### 0.12 → 0.13: `TICK_S` 0.25s → 0.5s

The backend (autocorrelation, morphology-template scoring, harmonic veto,
weighted-median combine) now runs every 0.5 seconds of audio instead of every
0.25 — half as often per unit wall-time, a direct clock-cycle reduction in
the single most expensive part of the algorithm. Every constant that was
counted in *ticks* rather than seconds (`ONSET_DEBOUNCE_TICKS`,
`TEAL_JUMP_CONFIRM_TICKS`, `WEIGHTED_MED_TRAILING_N`,
`RAMP_DELAY_EXTRA_TICKS(_FIRST)`, `TEAL_BPM_DELAY_SLOPE(_FIRST)`) was
rescaled to preserve its original real-world duration in seconds.

**A new public API function was added specifically to make this safe to
test**: `float c13stream_tick_s(void)` returns the actual compiled `TICK_S`.
Any test harness or integration should query this rather than hardcoding a
duplicate assumption about the tick rate — a native test driver in this
project's own history hardcoded a 0.25s chunk size regardless of the
compiled `TICK_S`, which silently fed audio at the wrong rate and corrupted
every timestamp when testing a non-default build. `test_driver_stream.c` in
this folder already uses the accessor correctly; if you write your own
integration or test harness, do the same.

Full 314-file corpus, DISPLAYED bpm: correct 59.67% → 60.19% (+0.51pp,
genuinely slightly better, not identical), no_display 37.22% → 36.78%,
octave-error rate 0.00% → 0.02% (3/26,083 ticks, one file, a transient
"double" reading that self-corrects). Median first-display-latency shift:
0.00s. `STATE_SIZE_BYTES` unchanged (11,588 — this is a clock-cycle-only
change, no buffer is sized by tick rate).

### 0.13 → 0.14: `DEC_STRIDE` 10 → 12 (decimated rate 400Hz → 333.33Hz)

The front-end now decimates the native 4000Hz audio stream down to
333.33Hz instead of 400Hz before any analysis runs. This shrinks every
buffer sized in decimated-sample or frame terms, and reduces the
autocorrelation's own multiply-accumulate count (fewer samples represent the
same multi-second analysis window) — the single largest clock-cycle cost in
the whole algorithm. The anti-alias filter and the 3-band (30-50/50-80/
80-130Hz) envelope filterbank were both re-derived for the new rate and
verified bit-for-bit reproducible against the existing (unchanged)
`DEC_STRIDE=6/8` configurations before being trusted for the new one. The
morphology-template library (`autocorr_templates.h`/
`autocorr_templates_detrended.h`) was fully **re-baked** from the same
314-file corpus at the new lag grid — not reused or reinterpolated from the
400Hz templates.

Full 314-file corpus, DISPLAYED bpm (vs. 0.13's own baseline): correct
60.19% → 59.84% (-0.35pp), no_display +26/13,061 ticks, one new minor
failure mode (8/13,061 ticks, concentrated in one already-marginal file, a
`half`-rate misclassification that didn't exist at the finer 400Hz grid).
**Re-baking the templates did not meaningfully recover the accuracy cost of
the coarser grid** — an earlier no-template exploration found essentially
the same -0.39pp cost, meaning template matching lifts both grids about
equally rather than compensating for resolution loss. This is disclosed
honestly, not smoothed over: the RAM/clock win is real, and the accuracy
cost is real, small, and permanent.

`STATE_SIZE_BYTES`: 11,588 → **9,884 bytes (-14.7%)**. Autocorrelation
multiply-accumulate count: **-30.6%**.

### Combined result (0.12 → 0.14)

| | 0.12 | 0.14 |
|---|---|---|
| correct | 59.67% | 59.67%¹ |
| `STATE_SIZE_BYTES` | 11,588 | **9,884 (-14.7%)** |
| Backend invocations/sec | 4 | **2 (-50%)** |
| Autocorrelation MACs/invocation | baseline | **-30.6%** |

¹ Net accuracy is a coincidence of rounding, not a true wash — 0.13's real
+0.51pp gain and 0.14's real -0.35pp cost happen to land close together.
Treat this as "materially the same accuracy, meaningfully cheaper to run,"
not "provably neutral."

## Files (all required — no dependencies beyond the standard library)

Source files carry this folder's own revision number in their filename
(this project's standing convention for handoff folders specifically — the
live working copy elsewhere keeps plain, unversioned names):

- `v0_14_stream.h` — public API, include this from your firmware code.
- `v0_14_stream_internal.h` — state struct layout, included by the .c file only.
- `v0_14_stream.c` — implementation.
- `autocorr_templates.h` / `autocorr_templates_detrended.h` — baked per-bpm-bucket
  morphology template tables (raw-curve and detrended-curve versions, int8
  quantized, baked for THIS revision's 333.33Hz lag grid — do not mix with
  another revision's copies of these files). Required by the build flags
  below.
- `combo13_stream_wasm.c` — thin WASM ABI wrapper (for reference only), only
  needed if you're rebuilding for a browser/WebAssembly target.
- `test_driver_stream.c` — minimal native (non-WASM) command-line test
  driver, reads a raw float32 PCM file and prints one line per tick (tick
  interval determined automatically from the compiled `TICK_S`, currently
  0.5s). Useful for a quick standalone sanity build.

## Build

Compile `v0_14_stream.c` with:

```
-DSTREAM_VARIANT=4 -DTEMPLATE_WEIGHT_ENABLE -DTEMPLATE_MATCH_GATED_V2_ENABLE
```

All three flags are required for this revision's shipped behavior. Standard
C99, no platform-specific intrinsics, no dynamic allocation.

## Memory

Static/stack state size: **9,884 bytes** (one `struct Combo13StreamState`,
returned by `c13stream_state_size()` at runtime for confirmation — no heap
allocation anywhere in this file, confirmed via direct source inspection).
Comfortably fits inside a 50KB heap budget with room to spare if you choose
to `malloc()` it instead of the recommended `static` allocation.

## Public API (`v0_14_stream.h`)

```c
unsigned long c13stream_state_size(void);
const char   *c13stream_version(void);   // "0.14"
float         c13stream_tick_s(void);    // 0.5f -- query this, don't hardcode it
void c13stream_init(Combo13StreamState *st);
void c13stream_push(Combo13StreamState *st, const float *samples, int n);
Combo13StreamTick c13stream_tick(Combo13StreamState *st);
```

- **`c13stream_init`** — zero-initializes state. Call once at startup (or on a full reset).
- **`c13stream_push`** — feed new audio samples as they arrive, in order, any chunk size.
  Samples are **raw, un-normalized PCM-scale floats** (i.e. the same scale as your ADC's
  native amplitude, e.g. int16-range values cast to float — do **not** pre-normalize to
  ±1.0, every tuned threshold in this port assumes raw scale).
- **`c13stream_tick`** — call once every `c13stream_tick_s()` seconds of audio pushed
  (currently 0.5s — the algorithm's internal timing assumes this cadence). **Query this
  value at runtime rather than hardcoding 0.5** — see the `TICK_S` section above for why
  a hardcoded assumption is a real, previously-hit bug risk, not a hypothetical one.
  Returns one `Combo13StreamTick` result per call.

```c
typedef struct {
    float t;                    // elapsed seconds since c13stream_init
    float bpm;                  // heart rate estimate, valid only if bpm_valid
    bool  bpm_valid;             // false while '--' should be displayed (no reading yet)
    bool  is_flatline;           // true = no signal / device off-body
    float raw_candidate_bpm;     // pre-confirmation candidate (diagnostic use)
    bool  raw_candidate_valid;
    int   harmonic_confirmed;    // 1=confirmed, 0=unconfirmed, -1=no opinion yet
} Combo13StreamTick;
```

Only `bpm` + `bpm_valid` + `is_flatline` are needed for a basic integration — treat
`bpm_valid==false` (or `is_flatline==true`) as "show no reading," not as an error.

## Minimal integration loop

```c
static Combo13StreamState st;   // ~9.9KB — put this in a section you control, not the stack
c13stream_init(&st);
float tick_period_s = c13stream_tick_s();   // 0.5s -- query it, don't assume it

// each time you have ~tick_period_s worth of new audio samples:
c13stream_push(&st, new_samples, n);
Combo13StreamTick r = c13stream_tick(&st);
if (r.bpm_valid && !r.is_flatline) {
    // display r.bpm
} else {
    // display "--"
}
```

## Status

Corpus-validated (full 314-file real+self-recorded corpus, displayed-bpm
classification, numbers above) and compiled/sanity-checked as a genuinely
standalone build of this exact handoff folder (not just the live working
copy) against both real recordings before being written up here. **Not yet
verified on real PSoC6 hardware or against a live BLE-connected device** —
that is the recommended next checkpoint before fully trusting this revision
on-device. Recommend a bring-up test against a known audio capture first,
same as every prior revision.

## Revision history

- **0.10 / 0.11**: template-matching mechanisms promoted — see
  `Desktop\pcg_abab\v0.11\`'s own README for that story.
- **0.12**: RAM/clock/flash optimization pass (int8 template quantization,
  peak-finder rewrite, Gaussian LUT, precomputed lag-power table, geometric
  decay-window recurrence) — see `Desktop\pcg_abab\v0.12\`'s own README.
  Genuinely behavior-preserving (byte-identical classification, though not
  exact-value-identical — see that folder's README for the one honest
  correction on that point).
- **0.13**: `TICK_S` 0.25s→0.5s — real, small, net-positive accuracy change.
- **0.14** (this revision): `DEC_STRIDE` 10→12 with re-baked templates —
  real, small, net-negative accuracy change, traded for a real RAM/clock win.

See the parent project's own change history for the full revision-by-revision
detail behind every prior version.
