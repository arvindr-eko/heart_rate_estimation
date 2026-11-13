# Combo13 streaming heart-rate estimator — revision 0.11

Embedded-C port of the combo13 PCG (phonocardiogram) heart-rate algorithm for
PSoC6/Cortex-M4F, 400Hz-decimation streaming front-end (`STREAM_VARIANT=4`).
**This is the current shipped revision** — deployed live in `pcg_visualizer`
and validated against the full corpus described below.

## What changed in this revision (0.10 → 0.11)

`TEMPLATE_MATCH_GATED_V2_ENABLE`: replaces ONLY the raw-candidate selection
step. Previously the algorithm picked the single tallest local maximum of the
detrended autocorrelation curve as its candidate bpm. Revision 0.11 instead
looks at the top 6 tallest local maxima, scores each one by correlating both
the raw and detrended autocorrelation curves against a library of known
per-bpm-bucket morphology templates (29 buckets, 5bpm resolution, baked from
a 314-file real+self-recorded corpus), and picks the tallest peak *unless* a
shorter peak's template-match score clearly beats it (a "gated override" —
picking on score alone, unconditionally, was tried and found worse than
always trusting height). A peak also needs a minimum score to be accepted at
all, regardless of height.

Every downstream mechanism — the settling freeze after a signal disruption,
harmonic veto, jump confirmation, bpm-dependent confirmation delay, and the
weighted-median combine that produces the final displayed number — is
**completely untouched**. Only the raw candidate feeding into that pipeline
changed.

An earlier attempt also biased the candidate scores toward a "typical"
resting heart rate (a tent-shaped multiplier peaking around 56bpm). That
version **regressed** full-corpus accuracy, because the existing combine
stage already applies the same bias later — applying it twice pulled
high-bpm recordings toward their own 1/3-subharmonic. That bias was removed
from the promoted default; the rejected version is preserved as an opt-in
build flag (`TEMPLATE_MATCH_GATED_V2_TENT_ENABLE`) for reference, not for use.

### Results (full 314-file real+self-recorded corpus, displayed bpm)

| | revision 0.10 | revision 0.11 |
|---|---|---|
| correct | 59.07% | **59.67%** |
| half-rate locks | 65 | **0** |
| third-rate locks | 19 | **0** |
| other/garbage | 816 | **810** |
| no-display | 9,776 | **9,708** |
| `STATE_SIZE_BYTES` | 11,204 | 11,204 (unchanged) |

Every axis improved simultaneously; nothing was traded away. RAM cost is
zero beyond what the prior revision already used.

## Files (all required — no dependencies beyond the standard library)

Source files carry this folder's own revision number in their filename (this
project's standing convention for handoff folders specifically — the live
working copy elsewhere keeps plain, unversioned names):

- `v0_11_stream.h` — public API, include this from your firmware code.
- `v0_11_stream_internal.h` — state struct layout, included by the .c file only.
- `v0_11_stream.c` — implementation.
- `autocorr_templates.h` / `autocorr_templates_detrended.h` — baked per-bpm-bucket
  morphology template tables (raw-curve and detrended-curve versions), NOT
  renamed per-revision (only the 3 core files above are). Required by the
  build flags below — the algorithm will not compile without them once
  `TEMPLATE_WEIGHT_ENABLE` or `TEMPLATE_MATCH_GATED_V2_ENABLE` is defined.
- `combo13_stream_wasm.c` — thin WASM ABI wrapper (same one used to build the
  live browser panel), only needed if you're rebuilding for a browser/
  WebAssembly target rather than a real embedded target.
- `test_driver_stream.c` — minimal native (non-WASM) command-line test driver,
  reads a raw float32 PCM file and prints one line per 250ms tick. Useful for
  a quick standalone sanity build.

## Build

Compile `v0_11_stream.c` with:

```
-DSTREAM_VARIANT=4 -DTEMPLATE_WEIGHT_ENABLE -DTEMPLATE_MATCH_GATED_V2_ENABLE
```

All three flags are required for this revision's shipped behavior. Standard
C99, no platform-specific intrinsics, no dynamic allocation.

(`STREAM_VARIANT=1/2/3` select earlier, superseded front-end designs — not
used by this revision. `TEMPLATE_MATCH_GATED_V2_TENT_ENABLE` reproduces a
REJECTED variant for reference only — do not define it in a normal build.)

## Memory

Static/stack state size: **11,204 bytes** (one `struct Combo13StreamState`,
returned by `c13stream_state_size()` at runtime for confirmation — no heap
allocation anywhere in this file).

## Public API (`v0_11_stream.h`)

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
  internal timing assumes this cadence — `TICK_S=0.25f` in `v0_11_stream.c`). Returns
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
static Combo13StreamState st;   // ~11.2KB — put this in a section you control, not the stack
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
known-good values). **Not yet verified on real PSoC6 hardware or against a
live BLE-connected device** — that is the recommended next checkpoint before
fully trusting this revision on-device. Recommend a bring-up test against a
known audio capture first, same as every prior revision.

## Revision history

- **0.10**: `TEMPLATE_WEIGHT_ENABLE` — soft morphology-template multiplier
  applied to the combine-stage weighting only (candidate selection itself
  unchanged from prior revisions).
- **0.11** (this revision): `TEMPLATE_MATCH_GATED_V2_ENABLE` — upgrades the
  raw-candidate selection itself, as described above.

See the parent project's own change history for the full revision-by-revision
detail behind every prior version.
