# combo13 streaming heart-rate front end (revision 0.12, lean)

A compact C implementation of the combo13 heart-rate-from-PCG algorithm, designed to run on a
resource-constrained MCU (originally targeted at PSoC6/Cortex-M4F). It consumes a raw PCG
(phonocardiogram) audio stream and produces a smoothed beats-per-minute estimate roughly 4 times a
second, along with a flatline (device-off) flag and internal diagnostic fields.

This is a structural/textual cleanup of the shipped algorithm's source (same revision, same
behavior) with all dead code paths and development history removed. It is validated bit-exact
against the original source: 0 differing ticks across a 314-file corpus (26,083 ticks) plus two
real recordings, at full float precision. See the bottom of this file for how that was verified.

## Signal path

Audio arrives via `c13stream_push()` at its native rate (this build is tuned for 4000Hz) in
whatever chunk sizes are convenient for the caller. Internally:

1. **Streaming decimation.** A continuous anti-alias IIR filter runs sample-by-sample; every 10th
   filtered sample is kept, producing a 400Hz decimated stream. Filter state carries across calls
   indefinitely, so results don't depend on how samples happen to be chunked.

2. **Chunked artifact removal.** Once 68 decimated samples have accumulated (~170ms), that chunk
   plus a 48-sample carry region from the previous chunk is cleaned in place:
   - **Spike detection** flags the loudest sample in any run that exceeds ~15x the streaming median
     baseline, purely for event timing (it doesn't touch the signal).
   - **Artifact detect/extend/interpolate** finds samples exceeding a robust-percentile baseline,
     extends the flagged region outward while still elevated, pads it slightly, and linearly
     interpolates across it from its untouched neighbors.
   - **Declick** hard-clips anything still exceeding a looser multiple of the median baseline.
   - Both streaming baselines (the artifact detector's own robust percentile, and the shared
     median) are then updated from the *raw* pre-cleaning chunk, not the cleaned output, and
     always from the baseline as it stood *before* this chunk -- so the threshold used to clean a
     chunk is never itself contaminated by what it's about to detect.

3. **Per-band envelope extraction.** The cleaned decimated stream feeds a persistent 3-band
   bandpass filterbank (30-50Hz, 50-80Hz, 80-130Hz) with a single-pole envelope follower on each
   band, updated once per decimated sample. Every 5th sample (80Hz frame rate), the current
   envelope values are pushed into a small ring buffer per band. Every feature extractor downstream
   just reads trailing frames out of these rings -- there is no raw re-filtering per tick.

4. **Autocorrelation candidate.** Each tick, the 3 bands are z-scored, clipped, and summed over a
   4-8 second growing analysis window (the window grows after each event, up to a cap, giving the
   estimate more data to work with the longer the signal has been stable). A trimmed-mean
   autocorrelation is computed over lags corresponding to 40-200bpm, weighted to mildly discount
   very short lags, then detrended (a local moving average subtracted off) to suppress the curve's
   own slow trend so a real periodicity peak stands out.

5. **Morphology-template re-ranking.** Rather than always taking the single tallest detrended peak,
   the top 6 local maxima are scored against known autocorrelation-curve shapes for their
   corresponding bpm (baked from the corpus's own correct readings, region-weighted toward where a
   fundamental's harmonics and S1-S2 sub-beat structure should appear). A smaller peak can win if
   its score clearly beats the tallest one's; the whole candidate is dropped if even the winner's
   score misses a confidence floor.

6. **Harmonic veto.** The chosen candidate is checked for corroborating autocorrelation structure
   near twice its own lag (i.e. near half its bpm) -- if the true fundamental is actually there
   instead, this shows up as a competing peak and the candidate is vetoed (dropped for that tick,
   not replaced) rather than displayed with false confidence.

7. **Settling, jump, and delay guards.** After any event (first signal onset, a detected spike, or
   recovery from a flatline period), a settling wait suppresses the display while the front end's
   internal state catches up -- unless an independently-derived flux estimate (see below) agrees
   with the candidate for a few consecutive ticks, in which case part of the wait can be skipped.
   Once past the wait, a candidate that jumps far from the last accepted value must repeat for
   several ticks before being trusted (guarding against a single bad tick knocking the display to a
   wrong octave), and a bpm-dependent confirmation delay applies similarly.

8. **Weighted-median combine.** The trailing 8 accepted candidates are combined via a weighted
   median, each weighted by a bpm-plausibility shape (mild prior toward typical resting rates),
   boosted when an independently-derived envelope-flux estimate agrees with it, and scaled by its
   own morphology-template correlation. This is the value returned as `bpm`.

The **flux** signal mentioned in steps 7-8 is a second, independently-derived rate estimate: a
rectified first-difference ("onset strength") of the raw summed envelope, autocorrelated the same
way as the main candidate. It never drives the display on its own -- it's a cross-check the settling
and combine stages use to decide how much to trust the primary candidate.

The **flatline detector** runs in parallel using a short (~2 second) window: if the gap between the
top 5% loudest frames and the mean stays below a threshold for a full second, the signal is treated
as silence (device off/disconnected) and the display blanks rather than showing a stale or spurious
number.

## Public API

```c
#include "combo13_stream.h"

Combo13StreamState st;      // or malloc'd -- see c13stream_state_size()
c13stream_init(&st);

// as audio arrives, in any chunk size/cadence:
c13stream_push(&st, samples, n_samples);

// every 0.25s of elapsed audio:
Combo13StreamTick tick = c13stream_tick(&st);
if (tick.bpm_valid) {
    display(tick.bpm);
} else if (tick.is_flatline) {
    display_dashes();
} // else: still settling/suppressed -- keep showing the last value or dashes, caller's choice
```

- `c13stream_init(st)` -- zero-initializes state and precomputes fixed per-lag constants. Call once.
- `c13stream_push(st, samples, n)` -- feeds raw audio samples (this build expects 4000Hz). Safe to
  call with any chunk size, any number of times, between ticks.
- `c13stream_tick(st)` -- advances the internal clock by `TICK_S` (0.25s) and returns the current
  estimate. Call at a steady 4Hz cadence relative to how much audio has actually been pushed (the
  test drivers push exactly 0.25s of samples between each tick).
- `c13stream_state_size()` -- returns `sizeof(Combo13StreamState)` in bytes, for allocating state
  without needing the struct's internal layout at the call site.
- `c13stream_version()` -- returns `"0.12"`, the algorithm revision string.

`Combo13StreamTick` fields:

| field                 | meaning                                                              |
|-----------------------|-----------------------------------------------------------------------|
| `t`                   | elapsed time, seconds, at this tick                                  |
| `bpm`                 | displayed estimate (weighted-median combine output)                  |
| `bpm_valid`           | false while suppressed (settling/jump/delay guard) or flatline       |
| `is_flatline`         | true when no real signal is present                                  |
| `raw_candidate_bpm`   | this tick's own raw estimate, before combine-stage smoothing          |
| `raw_candidate_valid` | false when the raw estimate was vetoed or not yet available          |
| `harmonic_confirmed`  | 1 = confirmed, 0 = vetoed, -1 = not evaluated this tick               |

There is no dynamic allocation anywhere in this file -- `Combo13StreamState` is a plain struct the
caller owns (stack, static, or malloc'd once).

## Build

This is a fixed single configuration (equivalent to what the original multi-variant source called
`STREAM_VARIANT=4`): 4000Hz input, decimated to 400Hz, 80Hz envelope frame rate, 40-200bpm search
range. No compile-time flags are required.

**Native:**
```sh
gcc -O2 -o test_driver_stream test_driver_stream.c combo13_stream.c -lm
./test_driver_stream <raw_f32_pcm_file> <sample_rate_hz>
```

**WebAssembly (Emscripten), for browser/Node tooling:**
```sh
emcc -O2 -sNODERAWFS=1 -o test_driver_stream_csv.js test_driver_stream_csv.c combo13_stream.c
node test_driver_stream_csv.js <raw_f32_pcm_file> <sample_rate_hz>
```
`test_driver_stream_csv.c` prints one CSV row per tick at full float precision (used for the
tick-by-tick validation below); `test_driver_stream.c` prints a human-readable line per tick.

NODERAWFS builds embed a reference to their own `.wasm` filename -- always compile directly under
the target filename; renaming a built `.js`/`.wasm` pair afterward will break it.

Input files are raw interleaved-free float32 PCM (no header) -- e.g. `numpy.ndarray.tofile()` on a
mono `float32` array, or `ffmpeg -f f32le`.

## Memory and flash footprint

- **RAM:** `c13stream_state_size()` returns **11,588 bytes** -- the entire `Combo13StreamState`,
  no dynamic allocation anywhere. Identical to the original (unrefactored) revision 0.12 build, as
  expected for a pure refactor.
- **Flash (proxy measurement only):** compiled `.wasm` size is essentially unchanged from the
  original source (~54KB at `-Os`, ~66KB at `-O2`, both within ~0.1% of the pre-refactor build) --
  an optimizing compiler already dead-strips unreferenced functions and unreachable branches, so a
  source-level removal of the same dead code doesn't move the compiled output much. The real,
  measurable win from this refactor is source size and complexity (see below), not compiled size
  under an optimizing toolchain. This machine has no ARM/Cortex-M4F cross-compiler, so real target
  flash size would need to be confirmed with the actual embedded toolchain -- the wasm figures above
  are Emscripten's own optimizer, a reasonable proxy for "does dead code actually get removed," not
  a stand-in for ARM code density.
- **Source size:** `combo13_stream.c` went from 2,759 lines / 122 preprocessor conditional branches
  to 1,371 lines / 0 conditional branches. `combo13_stream_internal.h` went from 673 to 162 lines.

## Validation

`validate_lean_refactor.py` (in this directory) drives both this build and the original
`embedded_c/combo13_stream.c` build through identical input and diffs every `Combo13StreamTick`
field, tick-by-tick, at full float precision:

```sh
python validate_lean_refactor.py real     # daniel_recovery_test.f32 + daniel_offchest_test.f32
python validate_lean_refactor.py subset 8 # every 8th corpus file
python validate_lean_refactor.py full     # the full 314-file real+selfrecorded corpus
```

Last run: **0 mismatches** across both real recordings and the full 314-file/26,083-tick corpus.
`c13stream_state_size()` returns 11,588 and `c13stream_version()` returns `"0.12"` in both builds.
