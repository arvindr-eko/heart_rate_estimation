# Combo13 streaming heart-rate estimator — revision 0.12

Embedded-C port of the combo13 PCG (phonocardiogram) heart-rate algorithm for
PSoC6/Cortex-M4F, 400Hz-decimation streaming front-end (`STREAM_VARIANT=4`).
**This is the current shipped revision** — deployed live in `pcg_visualizer`
and validated against the full corpus described below.

## What changed in this revision (0.11 → 0.12)

A RAM/clock/flash optimization pass, effectively no behavior change. Every
one of the five edits below was corpus-validated **byte-identical
DISPLAYED-bpm classification** against 0.11 (314/314 files unchanged at the
correct/half/double/third/other/no_display bucket level) before being kept.

**Correction, caught during the pcg_visualizer redeploy of this same
revision**: "byte-identical" is true at the classification level above, but
not literally true at the exact-bpm-value level. A finer-grained 410-file
scatter check found that int8 template quantization (change #1 below)
occasionally flips a near-tied `TEMPLATE_MATCH_GATED_V2` candidate-selection
or harmonic-veto decision, from ~0.8%-of-full-scale quantization noise: 2 of
410 files show a single-tick shift of less than 1bpm (one case includes a
`harmonic_confirmed` True→False flip, not just display rounding). Root-caused
to change #1 alone via an intermediate quant8-only build — changes #2–5 are
not implicated. Neither affected tick changes its correct/incorrect
classification outcome. This is the same class of already-accepted
float-precision tie-sensitivity as this algorithm's documented
float32-vs-float64 `lroundf`/autocorrelation-lag-bin edge cases, not a new
failure mode — but it means "exact same heart-rate readings on every file" is
not quite accurate; "same classification outcome on every file" is the
verified claim.

1. **Morphology templates quantized float32 → int8.** `kTemplateCurve` /
   `kTemplateCurveDetrended` (used by `TEMPLATE_MATCH_GATED_V2_ENABLE`'s
   candidate scoring) dropped from 22,272 bytes to 5,568 bytes of flash (4x).
   Values are dequantized inline (one multiply per point) inside the
   correlation loop that was already accumulating there — no added per-element
   cost. Data range checked directly before quantizing (raw curves in
   `[0.0043, 1.0]`, detrended in `[-0.5086, 1.0]`), both comfortably fit a
   shared int8 scale.
2. **`find_topN_peaks()` rewritten** from "collect every local maximum into a
   96-int (384-byte) stack array, then O(n²) selection-sort the whole thing"
   to a single-pass running top-N insertion (O(n_max)=6-sized stack arrays).
   Tie-breaking preserved exactly (same ascending-lag ordering for equal-height
   candidates as the original selection sort).
3. **`build_region_weights()`'s per-tick `expf()` calls replaced with a
   shared Gaussian lookup table.** The unmodified function called `expf()` up
   to 5×96=480 times per invocation, and this runs once per candidate peak (up
   to 6/tick) — up to 2,880 `expf()` calls/tick. All 5 region bumps share the
   same functional form (a unit Gaussian), so one 201-entry lookup table
   (linear-interpolated, symmetric, flash-resident) covers every bump at every
   bump's own sigma.
4. **`powf((float)l, LAG_POW)` precomputed.** Three separate call sites
   recomputed this from scratch across all 96 lags, every tick, despite it
   being a pure function of compile-time constants (`LO_LAG`, `HI_LAG`,
   `LAG_POW` never change after compile time). Replaced with
   `st->lag_pow_recip[S_LEN]`, computed once in `c13stream_init()`. This is
   the **only RAM cost in this revision**: `STATE_SIZE_BYTES` grows from
   11,204 to 11,588 (+384 bytes, +3.4%).
5. **Adaptive-tau decay window's per-sample `expf()` replaced with a
   geometric recurrence.** 3 bands/tick each built an exponential decay window
   via one `expf()` call per sample (up to `FEAT_MAX_V` calls/band). Since the
   decay constant is fixed within one call, the weights form an exact
   geometric sequence — reduced to 1 `expf()` call (for the sequence's
   endpoint) + 1 `expf()` call (for the constant ratio) + `m-1` multiplies.

Also investigated and explicitly **not** changed:
- **Harmonic veto** — re-tested for redundancy now that both
  `TEMPLATE_WEIGHT_ENABLE` and `TEMPLATE_MATCH_GATED_V2_ENABLE` exist.
  Disabling it changes 33/314 files: correct rate barely moves (-13 ticks),
  but it mostly converts `no_display` ticks into **wrong-bpm-displayed**
  ticks (-318 no_display, +326 other), not into correct ones. Kept — a blank
  reading is a materially safer failure mode than a confidently wrong one.
- **Incremental/recursive autocorrelation** — the largest remaining unclaimed
  clock-cycle cost (~570,000 multiply-accumulates/tick in
  `add_autocorr_robust()`). Confirmed blocked on a real normalization
  problem: both its own `r0` normalizer and the upstream per-band z-score are
  whole-window batch statistics recomputed every tick, so an unchanged raw
  sample's *normalized* value still shifts as the window slides — breaking
  the "just add the new cross-terms, subtract the old ones" assumption
  incremental autocorrelation needs. A prior attempt (2026-08-05) to fix this
  by making normalization causal instead was reverted (2026-08-08) after it
  regressed the corpus (a "garbage-lock" collapse to 100% no-display in the
  first 0–10s of every file). Not re-attempted without a fresh angle.

### Results (full 314-file real+self-recorded corpus, displayed bpm)

| | revision 0.11 | revision 0.12 |
|---|---|---|
| correct | 59.67% | **59.67% (identical)** |
| half/third-rate locks | 0 | **0 (identical)** |
| other/garbage | 810 | **810 (identical)** |
| no-display | 9,708 | **9,708 (identical)** |
| `STATE_SIZE_BYTES` | 11,204 | **11,588 (+384B, +3.4%)** |
| Template flash footprint | 22,272B (float32) | **5,568B (int8, -75%)** |
| `expf()`/`powf()` calls in hot loops | ~2,880 + 288 + up to ~600/tick | **~4 (+ multiplies)/tick** |

Zero files changed classification at any step of this pass (each of the 5
changes was validated individually, 0/314 changed every time). The only cost
is the +384B RAM for the lag-power table; everything else is a pure win.
"Identical" in this table means identical classification-bucket totals, not
exact-bpm-value identity on every tick — see the correction above.

## Files (all required — no dependencies beyond the standard library)

Source files carry this folder's own revision number in their filename (this
project's standing convention for handoff folders specifically — the live
working copy elsewhere keeps plain, unversioned names):

- `v0_12_stream.h` — public API, include this from your firmware code.
- `v0_12_stream_internal.h` — state struct layout, included by the .c file only.
- `v0_12_stream.c` — implementation.
- `autocorr_templates.h` / `autocorr_templates_detrended.h` — baked per-bpm-bucket
  morphology template tables (raw-curve and detrended-curve versions, **int8
  quantized as of this revision**), NOT renamed per-revision (only the 3 core
  files above are). Required by the build flags below — the algorithm will
  not compile without them once `TEMPLATE_WEIGHT_ENABLE` or
  `TEMPLATE_MATCH_GATED_V2_ENABLE` is defined.
- `combo13_stream_wasm.c` — thin WASM ABI wrapper (same one used to build the
  live browser panel), only needed if you're rebuilding for a browser/
  WebAssembly target rather than a real embedded target.
- `test_driver_stream.c` — minimal native (non-WASM) command-line test driver,
  reads a raw float32 PCM file and prints one line per 250ms tick. Useful for
  a quick standalone sanity build.

## Build

Compile `v0_12_stream.c` with:

```
-DSTREAM_VARIANT=4 -DTEMPLATE_WEIGHT_ENABLE -DTEMPLATE_MATCH_GATED_V2_ENABLE
```

All three flags are required for this revision's shipped behavior. Standard
C99, no platform-specific intrinsics, no dynamic allocation.

(`STREAM_VARIANT=1/2/3` select earlier, superseded front-end designs — not
used by this revision. `TEMPLATE_MATCH_GATED_V2_TENT_ENABLE` reproduces a
REJECTED variant for reference only — do not define it in a normal build.)

## Memory

Static/stack state size: **11,588 bytes** (one `struct Combo13StreamState`,
returned by `c13stream_state_size()` at runtime for confirmation — no heap
allocation anywhere in this file; confirmed via direct source inspection, not
just absence of symptoms). If you choose to `malloc()` this struct instead of
declaring it `static`, it comfortably fits inside a 50KB heap budget with
~40KB to spare — but static allocation is recommended for a fixed-size,
single-instance, lifetime-of-the-device struct like this one.

## Public API (`v0_12_stream.h`)

```c
unsigned long c13stream_state_size(void);
void c13stream_init(Combo13StreamState *st);
void c13stream_push(Combo13StreamState *st, const float *samples, int n);
Combo13StreamTick c13stream_tick(Combo13StreamState *st);
```

- **`c13stream_init`** — zero-initializes state. Call once at startup (or on a full reset).
- **`c13stream_push`** — feed new audio samples as they arrive, in order, any chunk size.
  Samples are **raw, un-normalized PCM-scale floats** (i.e. the same scale as your ADC's
  native amplitude, e.g. int16-range values cast to float — do **not** pre-normalize to
  ±1.0, every tuned threshold in this port assumes raw scale).
- **`c13stream_tick`** — call once every **250ms** of audio pushed (the algorithm's
  internal timing assumes this cadence — `TICK_S=0.25f` in `v0_12_stream.c`). Returns
  one `Combo13StreamTick` result per call.

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
static Combo13StreamState st;   // ~11.6KB — put this in a section you control, not the stack
c13stream_init(&st);

// each time you have ~250ms worth of new audio samples:
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
classification) and browser-verified live via WebAssembly (headless Chrome,
zero console errors, both real captured test recordings unchanged from their
known-good values). Also compiled and sanity-checked as a **standalone
build of this exact handoff folder** (not just the live working copy) against
both real recordings before being written up here. **Not yet verified on real
PSoC6 hardware or against a live BLE-connected device** — that is the
recommended next checkpoint before fully trusting this revision on-device.
Recommend a bring-up test against a known audio capture first, same as every
prior revision.

## Revision history

- **0.10**: `TEMPLATE_WEIGHT_ENABLE` — soft morphology-template multiplier
  applied to the combine-stage weighting only (candidate selection itself
  unchanged from prior revisions).
- **0.11**: `TEMPLATE_MATCH_GATED_V2_ENABLE` — upgrades the raw-candidate
  selection itself (top-6-peak region-weighted template scoring + gated
  override).
- **0.12** (this revision): RAM/clock/flash optimization pass, no behavior
  change — see above.

See the parent project's own change history for the full revision-by-revision
detail behind every prior version.
