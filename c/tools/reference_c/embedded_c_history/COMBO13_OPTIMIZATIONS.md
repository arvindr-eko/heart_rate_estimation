# combo13 embedded port: optimization report

Goal: port `combo13_heart_rate.py` (the validated "combo 13" PCG heart-rate pipeline) to C for
PSoC6 (Cortex-M4F), reducing clock-cycle and RAM usage to something that fits an embedded target,
while confirming the result still performs equivalently to the validated Python reference.

## Method

1. **Phase A**: a direct, function-by-function C translation of `combo13_heart_rate.py` with NO
   algorithmic changes -- same recompute-the-whole-window-from-scratch-every-tick structure
   Python uses, just in C/float32. This established a trustworthy correctness baseline.
   Validated **bit-for-bit identical to the Python reference** across 100 real corpus files
   (2026-08-04 session), spanning regular/pediatric/`_2spedup` sped-up subsets.
2. Every optimization after that was diffed against Phase A (`compare_driver.c`), not re-derived
   from scratch -- isolating exactly one change at a time, per this project's own established
   practice (see `[[project_pcg_cnn]]`/`[[project_pcg_visualizer]]` memory: "verify a regression
   claim by isolating the ONE changed variable").
3. Two different validation bars were used depending on whether a change was expected to alter
   any output value:
   - **Exact-equivalence** (mathematically the identical computation, just cheaper) -- checked by
     requiring **zero** tick mismatches across a 40-file random sample.
   - **Behavior-changing** (a genuine algorithmic change, e.g. batch->streaming) -- checked at the
     **classification level** (correct/half/double/third/triple/no_display against ground truth,
     this project's own standing tick-by-tick convention), comparing aggregate tallies between
     Phase A and the optimized version across a 60-file sample (regular/`_2spedup`/selfrecorded,
     20 each) rather than requiring exact per-tick agreement.

## Optimizations applied

| # | Change | Class | Result |
|---|--------|-------|--------|
| 4 | `remove_transients`/`declick_mult`/`detect_spike_runs`: replaced full sort (`qsort`, O(n log n)) with **quickselect** (O(n) average) for the single order statistic each actually needs | Exact-equivalence | 0/40 files mismatched |
| 5 | `add_autocorr_robust`'s trimmed mean: first tried dropping the trim entirely (relying on the upstream FEAT_CLIP z-score clip) -- **REJECTED**, cost ~0.5pp correct-tick rate and ~15% more octave errors on a 60-file check. Reverted to the exact trimmed mean, computed via **two range-scoped quickselect calls** (same idiom as `std::nth_element` twice) instead of a sort | Exact-equivalence | 0/40 files mismatched |
| 6 | `sum_env_norm` was computed TWICE per tick on the identical segment (once for the main estimate, once inside the harmonic-veto check) -- de-duplicated to compute once, reuse the cached feature array for both | Exact-equivalence | 0/40 files mismatched |
| 7 | Tilt-trace (pitch-family agreement signal): was recomputing ~500-560 FFTs/tick over the WHOLE 8s window every tick. Replaced with a **streaming pitch-bandpass filter + frame ring buffer**, computing each frame's FFT-based tilt feature ONCE, the moment it becomes available, cached thereafter | Behavior-changing (causality: a real streaming device can't use a "whole window including future frames" RMS gate the way Python's batch-per-tick computation implicitly can) | correct 3941->3953 (+12), half+double 109->107 (-2), no_display unchanged, across 6142 ticks -- net neutral to slightly positive |
| 8 | RAM: `scratch_raw_clean`/`scratch_raw_ar`/`scratch_raw_clean_ar` (3 separate 128KB buffers) shared into ONE buffer, since they're used in a strict sequential pipeline (each fully consumed before the next is written) within a tick; one `declick_mult` call is intentionally in-place (safe -- pure elementwise clip, median computed into separate scratch first) | Exact-equivalence | 0/40 files mismatched; also removed the now-dead whole-buffer `scratch_kumar_bp` (128KB), superseded by task 7's streaming ring |

## Before / after (steady-state, per tick, real recording)

| Metric | Phase A | Optimized | Change |
|---|---|---|---|
| Sort calls (O(n log n) each) | 286 | 0 | eliminated |
| Select calls (O(n) each, replaced the sorts) | 0 | ~522,000 total elements | new, but O(n) not O(n log n), and quickselect avoids qsort's function-pointer-indirection overhead per comparison |
| Autocorrelation multiply-accumulates | 570,333 | 570,333 | unchanged (inherent to the algorithm; see "what's NOT yet optimized" below) |
| Filter multiply-accumulates | 1,392,000 | 981,000 | -30% |
| FFT calls (128-pt) | ~524 | ~15 | -97% |
| Static RAM | 1,142,836 B (~1.09 MB) | 761,276 B (~744 KB) | -33% |

## What's NOT yet optimized (documented, not silently skipped)

- **The AA/decimate filter still re-runs over the whole raw window from scratch, twice per tick**
  (once for the non-artifact-removed chain feeding onset detection, once for the
  artifact-removed chain feeding the main estimate) -- this is now the single largest remaining
  "redundant recompute" cost (~576,000 of the 981,000 filter MACs/tick). The tilt trace's
  streaming technique (task 7) could apply here too, but the artifact-removed chain's input
  depends on `remove_transients`'s adaptively-re-thresholded output, which is NOT stable
  sample-to-sample the way raw audio is -- a genuine further behavior-changing optimization,
  same risk class as task 7, not yet attempted.
- **`remove_transients`/`declick_mult`/`detect_spike_runs` still require the full 8s raw window
  resident in RAM** (they're whole-window outlier/threshold detectors by design) -- further RAM
  reduction below ~760KB would require making these incremental too (e.g. a rolling/decaying
  percentile estimate), which changes behavior and needs the same corpus validation as task 7.
- **Autocorrelation MACs (570,333/tick) are unchanged** -- inherent to computing the main
  estimate + harmonic-veto check over the required lag range; the lag range is narrower than the
  window length, so a direct multiply-accumulate loop is already cheaper than an FFT-based
  autocorrelation here, not a missed optimization.

## Post-deployment fix: flatline detection (2026-08-04)

After wiring the WASM comparison panel into `pcg_visualizer`, live BLE testing found the panel
**never went `--` off-chest** while the visualizer's own JS readout correctly did. Root cause:
`combo13_heart_rate.py`'s `is_flatline` uses an older onset-debounce mechanism
(`sig_was_onset_ok`/`SIGNAL_ONSET_THRESH`), ported faithfully -- but the JS pipeline switched to a
different, newer mechanism back on 2026-07-24 (panel 1f's peak-mean detector,
`PEAK_MEAN_DIFF_THRESH`), and the Python/C reference was never updated to match (a known,
previously-documented gap -- see `[[project_pcg_visualizer]]` memory). The old mechanism is
evidently much less sensitive to genuine off-chest/no-signal conditions in live use.

**Fix**: ported the peak-mean detector into `combo13_opt.c` (NOT Phase A, which stays a frozen
match to the Python file as-is): `broadEnv()` over the same trailing `win_nsamp` segment already
used for the main estimate, top-10%-mean minus overall-mean as the decision variable, sustained
>=1s below threshold before declaring flatline (recovery is immediate). The one non-trivial part:
`PEAK_MEAN_DIFF_THRESH=0.003` in the JS file is calibrated against `pcgRing`'s normalized `[-1,1]`
sample scale, while this port's constants are all validated against raw un-normalized wav-scale
values (confirmed empirically: real corpus RMS ~1219, not ~0.037) -- rescaled the threshold by
`*32768` to match, since `broadEnv` is a purely linear computation (verified this rescaling
directly against real corpus data, not just derived in theory, before trusting it). The "top N
largest values, averaged" step uses the SAME quickselect trick as the trimmed-mean optimization
(partition at the boundary index, sum the unordered top partition -- no sort needed).

**Validated two ways**: (1) a synthetic loud-quiet-loud test (6s real audio + 6s injected noise-
floor silence + 6s real audio) -- flatline correctly triggers ~4s after the gap begins and clears
~4s after real audio resumes (the delay is inherent: the window needs to fill with the new
condition before the sustained-threshold check can fire, matching the JS mechanism's own
`win_nsamp`-gated dependency). (2) 60-file corpus classification check: `no_display` 1349->1640
(+291, the expected cost -- this mechanism has a ~4-5s startup bootstrap before it can say
ANYTHING, same as the JS panel's own current behavior, since neither can evaluate before
`win_nsamp` samples exist), `correct` 3941->3675 (-266, tracking the no_display increase almost
1:1 -- meaning displayed-tick error rates did NOT increase), half/double/third/triple flat or
slightly improved. **Net: trades a startup delay (already present in the shipped JS mechanism)
for correctly detecting genuine off-chest silence mid-stream, with no new wrong answers.**

## Post-deployment fix: buffer not cleared on flatline (2026-08-04)

Daniel asked directly whether the weighted-median combine buffer (`cons_buf_log`) gets cleared
when the device goes `--` -- "that was an earlier requirement." Checked the actual JS source
directly rather than relying on memory: `pcg_visualizer/index.html` added exactly this on
2026-07-28 (`if(isFlatline){for(let i=0;i<consBufLog.length;i++)consBufLog[i].val=null;}`), but
`combo13_heart_rate.py` (dated 2026-07-31, the file this whole port is based on) never carried it
forward -- confirmed by grepping that file's own source, not assumed. Without it, the trailing
weighted-median gather can walk backward PAST a flatline stretch (skipping only the invalid/
never-pushed entries logged during the gap itself) and reach a stale PRE-flatline value still
sitting `valid=true` in the ring buffer, resurrecting an old rate the instant signal resumes
instead of starting fresh.

**Fix**: while `is_flatline` holds, every entry currently in `cons_buf_log` is invalidated each
tick (ring structure/position untouched, only the validity flags) -- exact semantic match to the
JS version, translated to this port's valid-flag representation instead of a null sentinel.

**Validated**: re-ran the same synthetic loud-quiet-loud test used for the flatline-detection fix
-- confirmed the OLD behavior would have shown the stale `69.8` immediately once flatline cleared
at t=16.75s (the pre-gap value was still `valid=true` and within the ring's 50-slot depth); the
FIXED version correctly stays `--` through the rest of the test file, since not enough new real
pushes have accumulated yet. 60-file corpus check: **tallies came back byte-identical to the
flatline-fix-only checkpoint** (`no_display`=1640, `correct`=3675, all others unchanged) -- a true
no-op on real files, exactly as expected, since real recordings essentially never re-enter
flatline mid-stream after the initial startup window (where there's nothing valid to clear yet
anyway).

## RAM reduction: streaming front end (2026-08-04)

Daniel's ask: PSoC6's CM4 RAM region is 279KB total, ~131KB already committed to `.bss`, and only
~110KB fixed heap is available for audio processing (roughly half of that realistically available
to the heart-rate algorithm) -- `combo13_opt`'s 761,276B (~744KB) static footprint didn't fit by
more than an order of magnitude. Target: 50-70KB.

**Root cause of the 744KB**: 88% of it was six 8-second/32,000-sample-at-4kHz native-rate buffers
(`raw_buf` plus five `scratch_*` arrays), because every optimization up through task 8 kept
Phase A's original design of recomputing everything from the whole resident window, every tick --
RAM-sharing (task 8) reduced the buffer *count* but not the *size* of each one.

**Design**: replace whole-window batch recompute with a genuinely streaming front end --
continuous per-chunk (0.25s) processing instead of a full 8s re-scan each tick:
- **EMA baseline tracking** (`StreamBaseline`, tau=1.0s) replaces the exact whole-window percentile
  recompute used for the artifact/median baselines -- an approximation, but corpus-validated to be
  close enough (see below).
- **Streaming decimation** (continuous AA filter + stride-keep phase counter) replaces re-running
  the decimate filter over the whole raw window every tick (the single largest documented
  remaining cost in the "what's NOT yet optimized" section above -- this closes that gap).
- **A persistent ring buffer** (`dec_hist`) holds only the decimated signal history, replacing the
  full native-rate window.
- **Consolidated to a single decimated chain** feeding onset detection, `r_adaptive`, and the main
  estimate alike (Phase A/`combo13_opt` ran two chains -- artifact-removed and non-artifact-removed
  -- at native rate; this collapses to one, at whichever rate the variant below uses).
- Everything else (quickselect, the streaming tilt-trace, the harmonic veto, the peak-mean flatline
  detector, the cons_buf_log clear-on-flatline fix) carried over unchanged from `combo13_opt`.

**Three variants were built and corpus-validated** to answer Daniel's specific question ("does
detection need to be at 500Hz? could it be ~650Hz instead?"):

| Variant | Detection rate | RAM | Reduction | Correct% (60-file) | half+double (vs REF 107) | "other" (vs REF 637) |
|---|---|---|---|---|---|---|
| V1 | native, 4000Hz | 56,024 B | 13.6x | 59.14% (-0.70pp) | 123 (+16) | 661 (+24, +3.8%) |
| **V2 (shipped)** | decimated, 500Hz (stride 8) | 53,864 B | 14.1x | 60.44% (+0.60pp) | 113 (+6) | 732 (+95, +14.9%) |
| V3 | decimated, ~666.7Hz (stride 6) | 64,132 B | 11.9x | 59.68% (-0.16pp) | 108 (+1) | 750 (+113, +17.7%) |

All three land in the 50-70KB target. REF (`combo13_opt`, 761,276B) baseline on the same 60-file
sample: correct 3675/6142 (59.84%), half+double 107, other 637.

The real trade-off is which error type each variant absorbs: V2 has the smallest footprint and
even edges out REF on raw correct%, but its "other" (garbage-value) misclassifications rise 14.9%.
V3 keeps the half/double octave-error rate closest to REF of the three (+1 vs +16 for V1), at the
cost of the largest footprint and biggest "other" increase. **Decision: V2 (500Hz/stride-8)
shipped** -- smallest RAM, best raw accuracy; the "other"-bucket increase was judged an acceptable
trade for a 14x reduction, and 500Hz was confirmed sufficient (~650Hz/V3 was evaluated per
Daniel's question and does NOT clearly outperform 500Hz on the metric that matters most, correct%,
despite the closer octave-error match).

A synthetic-gap-file investigation during V1 development surfaced a subtlety worth recording:
`combo13_opt`'s flatline detection on a 6-second synthetic gap only worked as an emergent SIDE
EFFECT of its whole-window, fresh-every-tick median recompute (the quiet noise floor spuriously
exceeded a still-stale pre-transition spike threshold, incidentally shrinking `win_nsamp` below 6s)
-- not by design. The streaming EMA-based baseline in `combo13_stream` doesn't have this side
channel, so it correctly does NOT flag flatline on a 6s gap (mathematically an 8s window can never
be 100% inside a <8s gap) but DOES correctly flag it on a realistic 15s gap (`synth_gap_long.f32`),
with a sensible ~8.5s reaction lag. Confirmed no bug -- the 6s test was measuring a REF quirk, not
a stream regression.

**Deployed to `pcg_visualizer`**: `combo13_stream_wasm.c` (built with `-DSTREAM_VARIANT=2`) exports
the SAME symbol names as `combo13_wasm.c` (`combo13_wasm_init/push/tick/state_size`), so it drops
in as `pcg_visualizer/combo13_wasm.js` with zero changes to the page's JS -- only the algorithm
running underneath changed. Smoke-tested in Node against `synth_gap_long.f32` post-build (state
size confirmed 53,864B, locks onto ~69bpm on real audio, correctly flags flatline through the
gap) before deploying. The old `combo13_opt`-based build is kept at
`embedded_c/combo13_wasm_opt_backup.js` for rollback. **Browser-unverified** as of this writing (js
wiring itself, not the corpus validation).

## Target-specific notes for the real PSoC6 CM4F port

- All DSP primitives (`iir4`, `fft128`, the autocorrelation inner loops) are written as portable
  reference C, isolated in clearly-marked sections specifically so they can be swapped for
  CMSIS-DSP equivalents (`arm_biquad_cascade_df2T_f32`, `arm_rfft_fast_f32`, a dot-product-based
  correlate) on the real target -- CM4F's hardware FPU and CMSIS-DSP's hand-tuned assembly should
  beat this reference code further, for free, without changing any algorithm logic.
- The dynamic-range compressor (part of the general combo13 config surface) is omitted entirely,
  since combo13's validated configuration ships with it OFF (`use_compressor=False`) -- dead code
  for this configuration, not a missing feature.
- Fixed configuration only: no runtime parameter tuning/sliders like the Python reference or the
  pcg_visualizer JS tool have -- this port reproduces exactly ONE validated pipeline.

## Files

- `combo13.h`/`combo13_internal.h`/`combo13.c` -- Phase A, the frozen correctness oracle. Do not
  optimize further; this is what everything else is diffed against.
- `combo13_opt.h`/`combo13_opt_internal.h`/`combo13_opt.c` -- the optimized version, described
  above.
- `compare_driver.c` -- links both, runs them tick-by-tick side by side over the same audio,
  reports per-tick divergence.
- `corpus_compare.py` -- classification-level (correct/half/double/...) comparison against ground
  truth, for validating behavior-changing optimizations.
- `test_driver.c`/`test_driver_opt.c` -- standalone CLI drivers (`node test_driver.js file.f32 fs`),
  print the same fields as `combo13_heart_rate.py`'s own CLI for direct diffing. Compile with
  `-DCOMBO13_PROFILE` for the operation-count instrumentation used in this report.
- `sample_list.txt` -- the 40-file random sample (seed 1234567) used for exact-equivalence checks.
- `combo13_stream.h`/`combo13_stream_internal.h`/`combo13_stream.c` -- the RAM-reduced streaming
  front end, parameterized by `-DSTREAM_VARIANT=1/2/3` (native/500Hz/~667Hz detection rate). V2 is
  the shipped default.
- `test_driver_stream.c` -- standalone CLI driver, same shape as `test_driver_opt.c`.
- `compare_driver_stream.c` -- links `combo13_opt.c` + `combo13_stream.c`, tick-by-tick CSV output
  for `corpus_compare_stream.py`/`corpus_compare_stream2.py`/`corpus_compare_stream3.py`
  (per-variant copies, each with an isolated temp-file path to allow safe concurrent runs).
- `c_stream_corpus_report.py` -- full-410-file-corpus per-file mean/median HR report for a given
  variant (`python c_stream_corpus_report.py 1|2|3`), same convention as `c_corpus_report.py`.
- `combo13_stream_wasm.c` -- WASM wrapper for `combo13_stream`, same exported symbol names as
  `combo13_wasm.c` so it drops straight into `pcg_visualizer/combo13_wasm.js`.
- `combo13_wasm_opt_backup.js` -- the pre-switch `combo13_opt`-based WASM build, kept for rollback.
