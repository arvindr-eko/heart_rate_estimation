/* combo13_stream_internal.h -- struct layout for the RAM-reduced streaming front-end variants.
 *
 * REPLACES the whole-8-second-window batch design (remove_transients/declick_mult/
 * detect_spike_runs operating on a persistent 32000-sample raw buffer, ~672KB/88% of
 * combo13_opt's RAM) with a genuinely streaming front-end: a compact EMA-based percentile
 * estimator updated per small chunk, plus local (not whole-window) detect/extend/interpolate.
 * See COMBO13_OPTIMIZATIONS.md's RAM-reduction section for the full design rationale and the
 * 3-variant comparison this exists to run.
 *
 * Also consolidates the two parallel decimated chains (with/without artifact-removal) into ONE
 * -- onset detection and the r_adaptive preview now read from the SAME artifact-removed signal
 * as the main estimate, per Daniel's explicit choice (2026-08-04).
 *
 * RAM design (SUPERSEDED 2026-08-05, RAM/clock pass 4 -- see band_filt_z/band_env_ring below):
 * originally, the persistent decimated history (`dec_hist`) was a RING buffer, and every
 * downstream consumer that needed a contiguous window (feature extraction, broad_env) gathered
 * FRESH from the ring into ONE shared scratch buffer (`scratch_work`) each time, filtering/
 * enveloping IN PLACE within it every tick. Pass 4 replaced both: a persistent per-band filter/
 * envelope runs ONCE per new decimated sample as it arrives (not re-run from scratch per tick per
 * consumer), caching its output directly at frame rate -- `dec_hist` itself became unnecessary
 * once nothing needed the raw decimated rate anymore, so it's gone too, along with `scratch_work`.
 *

 * STREAM_VARIANT must be defined at compile time:
 *   1 = detect artifacts/spikes at NATIVE rate (4kHz), decimate to 500Hz afterward
 *   2 = decimate to 500Hz FIRST, detect at 500Hz
 *   3 = decimate to ~666.7Hz (stride 6) FIRST, detect at that rate
 */
#ifndef COMBO13_STREAM_INTERNAL_H
#define COMBO13_STREAM_INTERNAL_H

/* Revision number for this algorithm + its matching pcg_visualizer deployment (2026-08-08).
 * Bump this whenever combo13_stream.c/this file's algorithm behavior or memory footprint changes
 * -- see c13stream_version() in combo13_stream.c and project memory (project_pcg_embedded_c) for
 * what each revision corresponds to. Retires "plan of record" as this project's informal label
 * for "the current state" going forward.
 * 0.1 -> 0.2 (2026-08-08, same day): AGREEMENT_SIGNAL_FLUX promoted to default -- see that macro's
 * own comment in combo13_stream.c for the full corpus numbers behind the promotion.
 * 0.2 -> 0.3 (2026-08-08, same day): FLUX_RECTIFY_MODE promoted to full-wave -- see that macro's
 * own comment in combo13_stream.c for the full corpus numbers + real-recording fix behind it.
 * 0.3 -> 0.4 (2026-08-08, same day): tilt (the native-rate/FFT pitch-tilt trace this project
 * inherited unchanged from combo13_opt) removed entirely -- struct fields, FFT subsystem,
 * constants, and both wasm debug getters -- after direct grep confirmed its result was consumed
 * nowhere in the shipped build (dead since the 0.2 AGREEMENT_SIGNAL_FLUX promotion). Pure dead-code
 * elimination: full 314-file corpus and both real recordings (daniel_recovery_test.f32/
 * daniel_offchest_test.f32, tick-by-tick) are IDENTICAL to 0.3's own numbers.
 * STATE_SIZE_BYTES 17,052 -> 11,076 (-5,976B, -35.0%) -- see project memory
 * (project_pcg_embedded_c) for the full removal writeup.
 * 0.4 -> 0.5 (2026-08-08, same day): re-swept EARLY_RELEASE_MIN_TICKS/MAX_SAVE_S (2/5.0 -> 1/6.0,
 * tuned entirely around tilt before flux existed) and AGREE_BONUS (2.70 -> 6.5, never independently
 * validated for flux at all) -- both promoted together after full 314-file corpus + both real
 * recordings confirmed a simultaneous win on accuracy/octave-errors/no-display-rate/
 * time-to-first-display, with only a small "other" cost. See EARLY_RELEASE's and AGREE_BONUS's own
 * comments in combo13_stream.c, and project memory (project_pcg_embedded_c), for full numbers.
 * 0.5 -> 0.6 (2026-08-08, same day): another optimization run's second phase, the TENT shape and
 * flatline detector. TENT_PEAK_BPM/PEAK_H/LO_H/HI_H swept and found already near-optimal -- a promising
 * 60-file signal did NOT survive full-corpus scale, NOT PROMOTED. PEAK_MEAN_DIFF_THRESH (0.003 ->
 * 0.002) + PEAK_MEAN_TOP_PCT (0.10 -> 0.05) promoted together after finding they were genuinely
 * miscalibrated for this design -- full corpus correct +918/no_display -950/octave flat, with
 * byte-identical output to the pre-sweep build on both real recordings (a more aggressive pairing
 * was found and rejected first, after it introduced a narrow interaction effect that delayed
 * self-correction out of a pre-existing spurious lock by 1.5s on one real recording).
 * PEAK_MEAN_SUSTAIN_S was swept too and showed a genuine, uncapped corpus-accuracy-vs-real-
 * device-off-detection-speed tradeoff -- presented to Daniel directly rather than auto-promoted;
 * his call was to leave it unchanged at 1.0s. See combo13_stream.c's own comments and project
 * memory (project_pcg_embedded_c) for full sweep numbers. */
/* 0.7->0.8 (2026-08-09): bundles this round's "try all three" investigation -- ROBUST_TRIM_FRAC
 * re-swept and promoted 0.012->0.015 (variant 4), LAG_POW re-swept and rejected (see its own
 * comment in combo13_stream.c), ratio-aware jump confirmation prototyped and rejected (inert by
 * default), cross-signal harmonic-ratio check investigated and rejected on full-corpus evidence
 * (see teal_size_factor()'s own comment in combo13_stream.c). No new mechanisms shipped active by
 * default beyond ROBUST_TRIM_FRAC's promotion. */
/* 0.8->0.9 (2026-08-09): first-reading-accuracy optimization, per Daniel's "optimize for the
 * accuracy of the first displayed reading" then "adapting some of these constants at the beginning
 * of a file and after a flatline or spike... best of both worlds." Promoted RAMP_DELAY_EXTRA_TICKS_
 * FIRST 4.0->6.0 (applies only to a session's true first-ever reading, via the pre-existing
 * st->ever_displayed flag -- every later flatline/spike reconnect keeps the original fast timing
 * untouched). Full corpus: first-reading correct 287/314->293/314 (91.4%->93.3%), correct(all
 * ticks) 16082->15495, octave_errors 114->88 (better), no_display 8954->9663. A deliberate value
 * tradeoff, not a clean win -- see combo13_stream.c's own comment and project memory for the full
 * Pareto frontier this was chosen from. */
/* 0.9 SAME-DAY ADDENDUM (2026-08-09, source-tree sync only, no revision bump), per Daniel's
 * follow-up "how much would it take to actually delay/prevent daniel_recovery_test.f32's own
 * 160bpm bad first reading" then "push slope to 0.03 and put it into V0.9." Promoted
 * TEAL_BPM_DELAY_SLOPE_FIRST 0.00239->0.03 (same !ever_displayed-only scoping) -- fixes that exact
 * real-recording case (first reading 160bpm->53.9bpm, correct) for a further first-reading correct
 * 293/314->294/314 on top of the tradeoff above. See combo13_stream.c's own comment for the full
 * threshold-finding and Pareto numbers. */
/* 0.10 (2026-08-09, same day): TEMPLATE_WEIGHT_ENABLE promoted into the shipped build (LO=0.1/
 * HI=2.0), per Daniel's "investigate the morphology of the autocorrelation... library of known
 * morphologies... try implementing this in C" -> "soften to a weight, not a veto." A real behavior
 * change (correct+23/other-19 on the full 314-file corpus, zero no_display cost), hence the bump
 * per this project's own versioning convention -- see project memory (project_pcg_embedded_c) for
 * the full sweep, the rejected TEMPLATE_MATCH_ENABLE hard-veto attempt that preceded it, and the
 * bucket-boundary bug found and fixed along the way.
 * CORRECTED (2026-08-09, same day): minor versions keep incrementing past single digits (0.9 ->
 * 0.10 -> 0.11...), they do NOT roll over to 1.0 -- Daniel's explicit correction. This retires the
 * plain-`atof()`-as-float WASM version encoding below, since "0.10" and "0.1" atof to the identical
 * float value (0.1) -- exactly the ambiguity the original 2026-08-08 comment on
 * combo13_wasm_version() flagged as a future revisit ("e.g. 1.12"). See that function's own updated
 * comment in combo13_stream.c for the new major*1000+minor encoding.
 * 0.10 -> 0.11 (2026-08-10): TEMPLATE_MATCH_GATED_V2_ENABLE promoted into the shipped build, per
 * Daniel's "push it to the visualizer as v0.11" after corpus-validating the mechanism (see that
 * macro's own comment in combo13_stream.c). Replaces ONLY win_teal's raw-candidate selection (top-6
 * region-weighted-both-curve-scored peaks instead of the plain tallest-detrended-peak) -- every
 * downstream mechanism (settling freeze, harmonic veto, jump-confirm, bpm-delay, weighted-median
 * combine) is untouched. Full 314-file corpus, DISPLAYED bpm: correct 59.07%->59.67%, half 65->0
 * (eliminated), third 19->0 (eliminated), other 816->810, no_display 9776->9708 -- every axis
 * improved simultaneously. Ships WITHOUT the tent-shaped selection bias an earlier attempt tried
 * (that attempt REGRESSED to 57.74% correct -- v0.10 already applies the same tent once, downstream,
 * in the combine stage, and applying it twice over-pulled high-bpm 'doubled' files toward their own
 * 1/3-subharmonic; kept as the opt-in, off-by-default TEMPLATE_MATCH_GATED_V2_TENT_ENABLE for
 * reference). Zero RAM cost beyond TEMPLATE_WEIGHT_ENABLE's own existing footprint
 * (STATE_SIZE_BYTES unchanged at 11,204). See project memory (project_pcg_embedded_c) for the full
 * regression-then-fix story. */
/* 0.12 (2026-08-10): RAM/CLOCK PASS, per Daniel's "everything is tight on my MCU... reduce clock
 * cycles, RAM and flash" + "any other clock cycle optimizations?" -- five changes, all corpus-
 * validated byte-identical DISPLAYED-bpm classification (314/314 files unchanged) against 0.11.
 * CORRECTION (2026-08-10, same day, caught during the pcg_visualizer redeploy): "byte-identical"
 * above is true at the classification level (correct/half/double/third/other/no_display bucket)
 * but NOT literally true at the exact-bpm-value level -- a finer-grained 410-file scatter check
 * found int8 template quantization (#1 below) occasionally flips a near-tied TEMPLATE_MATCH_GATED_V2
 * candidate-selection or harmonic-veto decision from ~0.8%-of-full-scale quantization noise: 2/410
 * files show a single-tick shift of <1bpm (one case includes a harmonic_confirmed True->False flip,
 * not just display rounding), root-caused to change #1 alone via the intermediate quant8-only build
 * (topN/Gaussian-LUT/lag_pow_recip/geometric-expf changes #2-5 are not implicated). Neither affected
 * tick changes its correct/incorrect classification -- same class of already-accepted float-precision
 * tie-sensitivity as this algorithm's documented float32-vs-float64 lroundf/lag-bin edge cases, not a
 * new failure mode. Five changes below, all corpus-validated at the classification level:
 *   1. kTemplateCurve/kTemplateCurveDetrended quantized float32->int8 (flash 22,272B->5,568B, 4x).
 *   2. find_topN_peaks() rewritten to a single-pass running top-N insertion (was: collect every
 *      local max into a 96-int/384B stack array, then O(n^2) selection-sort the whole thing).
 *   3. build_region_weights()'s up-to-2,880 expf()/tick replaced with a shared 201-entry Gaussian
 *      lookup table (kGaussLUT, linear-interpolated).
 *   4. powf((float)l, LAG_POW) -- recomputed from scratch across all S_LEN lags at 3 call sites
 *      every tick, despite being a pure function of compile-time constants -- replaced with
 *      st->lag_pow_recip[S_LEN], computed once in c13stream_init(). STATE_SIZE_BYTES grows
 *      11,204->11,588 (+384B) as a result -- the only RAM cost in this whole pass.
 *   5. The adaptive-tau decay window's per-sample expf() (3 bands/tick, up to FEAT_MAX_V calls
 *      each) replaced with 1 expf() + a running multiply by a per-call constant ratio (exact
 *      geometric sequence, since tau_frames is fixed within one call).
 * Also investigated and explicitly NOT changed: harmonic veto (re-tested for redundancy now that
 * both template mechanisms exist -- still earns its keep, converts would-be wrong-bpm-displayed
 * ticks into blanks rather than correct ones, kept per Daniel's own call); incremental/recursive
 * autocorrelation (the largest remaining unclaimed cost, ~570K MACs/tick -- confirmed blocked on a
 * real normalization problem, a prior attempt at a prerequisite fix already regressed the corpus,
 * not re-attempted without a fresh angle). See project memory (project_pcg_embedded_c) for the
 * full history. */
/* 0.13 (2026-08-11): TICK_S 0.25f->0.5f, per Daniel's "test some other ways to reduce clock cycle
 * and RAM usage... increasing the timing between steps" -- the backend (autocorrelation, template
 * matching, harmonic veto, combine) now runs half as often per unit wall-time. Every tick-COUNTED
 * downstream constant rescaled to preserve its real-world duration in seconds -- see combo13_stream.c's
 * own TICK_S comment and each rescaled constant's inline comment for the exact arithmetic. Full
 * 314-file corpus, DISPLAYED bpm: correct 59.67%->60.19% (classification-neutral-to-slightly-
 * better), median first-display-latency shift 0.00s (mean +0.11s). One new minor self-correcting
 * artifact (3/26,083 ticks, one file, a transient "double" before settling correct) -- disclosed,
 * not swept under the rug. STATE_SIZE_BYTES unchanged (11,588) -- this is a clock-cycle-only
 * change, no buffer is sized by tick rate. A separate DEC_STRIDE (further-downsampling) investigation
 * was explored alongside this one -- see project memory for that story and why it isn't part of
 * this revision. */
/* 0.14 (2026-08-11): STREAM_VARIANT==4's own DEC_STRIDE 10->12 (400Hz->333.333Hz decimated rate),
 * the DEC_STRIDE side of the RAM/clock exploration mentioned in the 0.13 note above, now folded in
 * WITH template matching ported to the new lag grid (the exploration itself only tested the
 * no-template architecture, since the baked templates are fixed to the OLD 400Hz grid -- see
 * autocorr_templates.h/autocorr_templates_detrended.h, both re-baked from the same 314-file corpus
 * at the new geometry: ac_lo=20, S_LEN=80, ffs=66.667Hz, same 40-200bpm range preserved via
 * lo_lag=0.3*ffs/hi_lag=1.5*ffs). LO_LAG_V/HI_LAG_V 24/120 -> 20/100, DEC_MAX_V 3200->2672,
 * EXT_HI_MAX_V 270->227 (see each constant's own #define site below for the exact rescaling
 * arithmetic), AA/bandpass filter coefficients (combo13_stream.c) independently re-derived via
 * scipy.signal.butter and re-verified bit-for-bit against the DEC_STRIDE==8/6/10 coefficients
 * already in this file before being trusted for the new rate (a real bug was found and fixed along
 * the way -- see gen_filters_fixed.py's own docstring: the exploration's own gen_filters.py
 * normalized the anti-alias filter's cutoff to the DECIMATED Nyquist, which is dec_hz-independent
 * and does NOT reproduce the already-shipped DEC_STRIDE==8/6/10 AA coefficients; the AA filter
 * actually runs at NATIVE rate, so its Wn must be normalized to the NATIVE Nyquist instead -- fixed
 * before generating the new DEC_STRIDE==12 coefficients, which is a genuine bit-for-bit match to
 * what the exploration folder ultimately shipped despite the intermediate script bug). See
 * combo13_stream.c's own filter-coefficient block for the full per-band pole-stability check.
 * Full 314-file corpus, DISPLAYED bpm, STREAM_VARIANT=4 + TEMPLATE_WEIGHT_ENABLE +
 * TEMPLATE_MATCH_GATED_V2_ENABLE -- see this file's own change history / project memory for the
 * exact tally recorded at promotion time. STATE_SIZE_BYTES and the autocorrelation MAC-count
 * reduction are recorded at promotion time as well -- both buffers scale with S_LEN_V (96->80,
 * 0.833x) and DEC_MAX_V (3200->2672, 0.835x) respectively. */
/* 0.15 (2026-10-06): LOWER BPM BOUND 40 -> 30. STREAM_VARIANT==4's lag grid extended on the slow end
 * only: HI_LAG_V 100 -> 134 (lag = 60*FFS/bpm, FFS=66.667Hz -> 30bpm = 133.3 frames, rounded UP to 134
 * so the grid covers 30.0bpm; bin 133 = 30.1bpm, last bin 29.85bpm is excluded because the grid is
 * [LO_LAG, HI_LAG) i.e. last bin = lag 133). LO_LAG_V (200bpm ceiling) unchanged. S_LEN_V 80 -> 113
 * (the new bins are appended AFTER the old ones, so bins 0..79 still mean lags 20..99 exactly).
 * EXT_HI_MAX_V 227 -> 303 (harmonic veto's 2.1814x extended-lag ceiling: ceil(2.18138*133)+2 = 293,
 * +~3% margin, same convention as every prior revision). The baked morphology templates are
 * UNCHANGED (still 80 bins, lags 20..99, nearest bucket 45bpm) -- template scoring is restricted to
 * those first 80 bins (TEMPLATE_MATCH_N_CURVE), and peaks slower than the lowest template-backed rate
 * score as "unscored" -- see combo13_stream.c's combined_template_score(). No template exists for
 * 30-40bpm because the 314-file corpus contains no ground-truth readings below ~40bpm to bake from. */
#define COMBO13_VERSION "0.15"

#include <stdbool.h>
#include <stdint.h>

#if !defined(STREAM_VARIANT)
#error "define STREAM_VARIANT to 1, 2, or 3"
#endif

#if STREAM_VARIANT == 1
  #define DETECT_AT_NATIVE 1
  #define DEC_STRIDE 8
  #define DEC_MAX_V 4000        /* 8s @ 500Hz */
  #define FRAME_DS_V 5
  #define LO_LAG_V 30
  #define HI_LAG_V 150
#elif STREAM_VARIANT == 2
  #define DETECT_AT_NATIVE 0
  #define DEC_STRIDE 8
  #define DEC_MAX_V 4000        /* 8s @ 500Hz */
  #define FRAME_DS_V 5
  #define LO_LAG_V 30
  #define HI_LAG_V 150
#elif STREAM_VARIANT == 3
  #define DETECT_AT_NATIVE 0
  #define DEC_STRIDE 6           /* nominal rate 4000/6 = 666.667Hz */
  #define DEC_MAX_V 5340        /* 8s @ 666.667Hz + margin */
  #define FRAME_DS_V 7           /* FFS = 666.667/7 = 95.24Hz */
  #define LO_LAG_V 29
  #define HI_LAG_V 143
#elif STREAM_VARIANT == 4
  /* 2026-08-06: RAM/clock exploration at Daniel's request -- decimate further to 400Hz (still well
   * clear of the ~300Hz Nyquist floor for this algorithm's ~150Hz highest band of interest), with
   * FRAME_DS held at 5 (unlike variants 2/3, which each hold their own frame rate near ~95-100Hz)
   * so FFS drops proportionally too: 400/5=80Hz vs the shipped variant's 500/5=100Hz, an 0.8x
   * ratio. LO_LAG/HI_LAG retuned by that exact 0.8x ratio (30->24, 150->120) to preserve the SAME
   * 40-200bpm range at the new frame rate. This is a genuine DSP-resolution change (not just a
   * buffer/bookkeeping one like the same-day RAM consolidation) -- autocorrelation cost scales
   * with frames*lag_range, both ~0.8x here, so ~0.8^2=0.64x the MACs (a real ~36% cut to the
   * single largest per-tick cost), at the cost of coarser time resolution for periodicity
   * discrimination. Corpus/real-recording-validated and shipped as revision 0.1 onward at 400Hz.
   *
   * 0.14 (2026-08-11): one step further, per the DEC_STRIDE RAM/clock investigation -- decimate to
   * 4000/12=333.333Hz instead of 400Hz. FRAME_DS_V held at 5 again, so FFS=333.333/5=66.667Hz
   * (0.8333x this variant's own prior 80Hz). LO_LAG/HI_LAG rescaled by the SAME lag=60*FFS/bpm
   * relationship used originally: lo_lag=0.3*FFS=20 (200bpm ceiling), hi_lag=1.5*FFS=100 (40bpm
   * floor) -- both exact integers. DEC_MAX_V=ceil(8s*333.333Hz)+margin=2667+5=2672. Autocorrelation
   * cost scales with frames*lag_range, both ~0.833x this variant's prior 400Hz numbers, so
   * ~0.833^2=0.694x the MACs relative to the 400Hz baseline this variant already shipped (a further
   * ~30.6% cut). Templates re-baked for this exact geometry (autocorr_templates.h/
   * autocorr_templates_detrended.h) -- see this file's own COMBO13_VERSION="0.14" comment above for
   * the full story and validation tally. */
  #define DETECT_AT_NATIVE 0
  #define DEC_STRIDE 12          /* 4000/12 = 333.333Hz */
  #define DEC_MAX_V 2672        /* ceil(8s @ 333.333Hz) + ~5 margin */
  #define FRAME_DS_V 5           /* FFS = 333.333/5 = 66.667Hz */
  #define LO_LAG_V 20
  #define MAX_VALID_LAG_V 134    /* 0.15: slowest ACCEPTABLE candidate = 30bpm (lag 133.3 -> 134); was 100 (40bpm) in 0.14 */
  #define HI_LAG_V (MAX_VALID_LAG_V + 12)  /* margin bins past the 30bpm limit so a true 30bpm peak is an interior local max
                                              (peak finders need a right-hand neighbor) and the detrend/peak-finders see context */
#else
  #error "STREAM_VARIANT must be 1, 2, 3, or 4"
#endif

#define S_LEN_V (HI_LAG_V - LO_LAG_V)
/* peak scans run over bins [1, PEAK_SCAN_END) -- i.e. a peak at lag MAX_VALID_LAG_V (the 30bpm limit) is the last
 * eligible one; the margin bins beyond it are autocorrelated (for detrending context + as the peak's right-hand
 * neighbor) but can never BE the chosen candidate. Variants 1-3 keep the full-width scan (unchanged). */
#if STREAM_VARIANT == 4
#ifdef PEAK_SCAN_END_OVERRIDE
#define PEAK_SCAN_END PEAK_SCAN_END_OVERRIDE
#else
#define PEAK_SCAN_END (MAX_VALID_LAG_V - LO_LAG_V + 1)
#endif
/* the 0.14 (40bpm-floor) "fast zone": lags [LO_LAG_V, FAST_HI_LAG_V) = 20..99 = bins [0,FAST_LEN_V).
 * Everything that existed in 0.14 (flux agreement, r_adaptive preview, the V2 top-6 peak scoring, the
 * early-release agreement check, template scoring) still looks ONLY at this zone, with the SAME detrend
 * context as 0.14, so for any signal whose winning candidate is >= 40bpm the output is bit-identical to
 * 0.14 unless the new slow-zone override (below) fires. */
#if STREAM_VARIANT == 4
#define FAST_HI_LAG_V 100
#else
#define FAST_HI_LAG_V HI_LAG_V
#endif
#define FAST_LEN_V (FAST_HI_LAG_V - LO_LAG_V)
/* SLOW-ZONE OVERRIDE (0.15): bins [FAST_LEN_V-1, PEAK_SCAN_END) = lags 99..134 = ~40.4bpm down to 30bpm. A slow-zone
 * peak replaces the fast-zone candidate only when its detrended height is >= SLOW_RATIO_MIN x the tallest fast-zone
 * peak's -- a 2T/3T/4T autocorrelation echo of a genuine 60-160bpm rhythm is almost always LOWER than the true
 * fundamental, so requiring dominance keeps those echoes from being mistaken for a slow heart rate. */
#ifndef SLOW_RATIO_MIN_OVERRIDE
#define SLOW_RATIO_MIN_OVERRIDE 1.5f
#endif
#else
#define PEAK_SCAN_END (S_LEN_V - 1)
#endif
#define FEAT_MAX_V (DEC_MAX_V / FRAME_DS_V + 4)
/* CURVE_CORR_GAP_TICKS (2026-08-09) -- how many ticks apart the 2 curves compared by the
 * autocorrelation curve correlation prototype are. Override hook added specifically because the
 * first cut (GAP=1, comparing consecutive ticks) turned out to be dominated by window overlap, not
 * genuine (in)stability -- see combo13_stream.c's own comment at the write site for the full
 * investigation (Daniel's "that doesn't seem strict enough. It is almost always at 1" catch). */
/* TICK_S=0.5s REVISION (2026-08-11): 16 ticks * 0.25s = 4.0s intended gap -> 8 ticks at TICK_S=0.5s
 * to preserve that real-world duration. Inert in the shipped production build regardless -- only
 * read by CURVE_GATE_ENABLE/CURVE_WEIGHT_ENABLE (both opt-in, undefined here) and the
 * COMBO13_EXPOSE_DEBUG-gated curve_ring prototype -- rescaled anyway for correctness. */
#ifndef CURVE_CORR_GAP_TICKS_OVERRIDE
#define CURVE_CORR_GAP_TICKS_OVERRIDE 8
#endif
#define CURVE_CORR_GAP_TICKS CURVE_CORR_GAP_TICKS_OVERRIDE
/* RAM PASS 7 (2026-08-07): scratch_Sext[EXT_S_LEN_V] (combo13_stream.c's check_harmonic_confirmed)
 * sizes off EXT_HI_MAX_V, but the true ceiling it ever needs is
 * hi_ext = ceil(HARMONIC_VETO_HI_EXT_MULT * cand_lag) + 2, cand_lag <= HI_LAG_V -- this was never
 * re-derived per variant, just shared at 350 (variant 1-3's own ceiling, HI_LAG_V=150:
 * ceil(2.1814*150)+2=330, +~6% margin). Variant 4's own HI_LAG_V=120 caps hi_ext at
 * ceil(2.1814*120)+2=264 -- 270 keeps a comparable ~2.3% margin without shipping ~80 unused
 * floats. Variants 1-3 keep the original shared value untouched (already-validated paths, no
 * reason to touch them for a variant-4-only optimization).
 * 0.14 (2026-08-11): variant 4's own HI_LAG_V dropped 120->100 (DEC_STRIDE 10->12) -- hi_ext ceiling
 * becomes ceil(2.1813774614710146*100)+2=221; 227 keeps a comparable ~2.7% margin. */
#if STREAM_VARIANT == 4
  #define EXT_HI_MAX_V 303   /* 0.15: was 227 for the 40bpm floor; 2.18138*134+2=295 -> +~3% margin (veto looks past the 30bpm limit only) */
#else
  #define EXT_HI_MAX_V 350
#endif
#define EXT_S_LEN_V (EXT_HI_MAX_V - LO_LAG_V)
#define NLOG_V 16                /* only WEIGHTED_MED_TRAILING_N=8 ever needed; margin for gaps */

/* -- streaming front-end chunk/carry sizing -- */
#define NATIVE_CHUNK_N 400        /* 100ms @ 4000Hz -- variant 1's detection granularity */
#define NATIVE_CARRY_N 256         /* boundary context: pad_samples + extend-search margin */
#define DEC_CHUNK_N 68              /* ~100ms @ up to 667Hz -- variants 2/3's detection granularity */
#define DEC_CARRY_N 48               /* boundary context at decimated rate */

/* SHARED transient scratch (2026-08-06, RAM consolidation) -- sized to the largest single need
 * among the buffers it replaces (scratch_feat_a/b -- see the field's own comment in the struct
 * below for why they can share one region, and why scratch_Sext deliberately can't join them).
 * Used to also cover the tilt trace's own gather buffer (TILT_MAX_FRAMES, which dominated this
 * max for every variant) until tilt's removal, revision 0.4, 2026-08-08 -- now just FEAT_MAX_V. */
#define SCRATCH_SHARED_LEN FEAT_MAX_V

typedef struct { float val; bool valid; float conf; bool conf_valid; } StreamLogEntry;
typedef struct { float val; bool valid; } StreamAgreeLogEntry;

/* Compact streaming percentile/scale tracker: EMA of a per-chunk EXACT percentile (via
 * quickselect on the small chunk itself), replacing "sort the whole 8s window" with O(1) state.
 * `warm_sum`/`warm_dur` accumulate a duration-weighted running mean over the first
 * BASELINE_WARMUP_S seconds before `ema` starts being trusted as a real EMA seed -- avoids
 * seeding the whole downstream artifact/decline threshold off a single noisy ~100ms chunk. */
typedef struct {
    float ema;
    bool  initialized;
    float warm_sum;
    float warm_dur;
} StreamBaseline;

struct Combo13StreamState {
    float el;

    bool  sig_was_onset_ok;
    int   onset_off_streak;
    int   onset_on_streak;
    float onset_candidate_el;
    float last_onset_el;
    float last_spike_el;
    float last_logged_spike_t;
    float flatline_clear_el;   /* timestamp is_flatline last transitioned true->false AFTER
                                  persisting >= FLATLINE_FULL_RESET_MIN_S -- a first-class event
                                  source, same as last_onset_el/last_spike_el, so recovering from a
                                  genuine disconnect gets the identical wait/ramp treatment as first
                                  contact regardless of the separate onset-debounce detector's
                                  state. Brief sub-threshold flatline blips don't set this. */
    float flatline_became_true_el;
    bool  was_flatline;
    bool  ever_displayed;      /* true once out.bpm_valid has ever been true -- distinguishes a
                                  genuine reconnect from the initial cold-start bootstrap, which
                                  is also a (long) flatline period but already fully covered by
                                  the ordinary first-contact wait/ramp */

    float prev_last_event_el;
    float frozen_wait_s;

    /* RAM/CLOCK PASS (2026-08-10), per Daniel's "any other clock cycle optimizations?" -- caches
     * 1.0f/powf((float)(LO_LAG_V+i), LAG_POW) for every lag bin, computed once in c13stream_init()
     * instead of via powf() inside the per-lag loop every single call. LO_LAG_V/HI_LAG_V/LAG_POW are
     * all fixed for the lifetime of a build (compile-time constants), so this value never changes
     * after init -- populated here (in the struct, computed at init time) rather than baked as a
     * flash const table so it automatically tracks whichever STREAM_VARIANT/LAG_POW combination is
     * actually compiled, with no separate per-variant generated header to keep in sync. See
     * combo13_stream.c's 3 lag_pow_recip call sites (previously each ran `S[i] / powf(l, LAG_POW)`
     * across all S_LEN_V lags) for the actual savings. */
    float lag_pow_recip[S_LEN_V];

    /* -- time-to-first-display optimization (2026-08-05, EARLY RELEASE attempt) -- counts
     * consecutive ticks the growing-window candidate (win_teal, computed every tick regardless
     * of the wait) agrees with the INDEPENDENT agreement signal (flux since revision 0.2; tilt's
     * native-rate trace before that and before its removal in revision 0.4), using the same
     * tolerance (AGREEMENT_BPM_TOL) the final weighted-median stage already uses for its own
     * agreement bonus. Deliberately requires agreement from a SECOND, differently-derived signal
     * rather than self-consistency alone -- a harmonic lock in the growing window can itself stay
     * self-consistent for several seconds (empirically confirmed elsewhere in this project's
     * history), so self-consistency alone is not sufficient evidence to release early. Reset on
     * any is_new_event/is_flatline, same as every other event-driven counter in this struct. */
    int early_release_streak;

    StreamLogEntry cons_buf_log[NLOG_V];
    StreamAgreeLogEntry agree_log[NLOG_V];
    /* CURVE-CORRELATION COMBINE WEIGHT (2026-08-09), PROTOTYPED AND REJECTED, per Daniel's "any
     * other ideas to increase accuracy?" -- distinct from (and independent of) the curve_gate_*
     * hard-veto prototype above (also rejected). Applied curve_corr as a SOFT multiplier on the
     * combine weight instead (teal_size_factor(), same mechanism AGREE_BONUS already uses) --
     * curve_corr is a decent RANKING signal (AUC 0.78) but a bad CUTOFF signal, so a soft weight
     * seemed like the natural next thing to try instead of another hard threshold. Swept
     * CURVE_WEIGHT_LO/HI across 6 ranges (symmetric +/-20% to +/-70% around 1.0, plus 2
     * downweight-only variants that never boost above baseline) on the full 314-file corpus: every
     * single configuration came back flat-to-slightly-WORSE than not having it at all (best case
     * 16,014->16,013 correct, essentially noise; worst case tried 16,014->16,007 correct AND
     * octave_errors 119->124, a real regression) -- no configuration, symmetric or asymmetric, wide
     * or narrow, showed a genuine improvement. Plausible mechanism: teal_size_factor()'s weight is
     * already dominated by AGREE_BONUS's much larger swing (6.5x when agreeing vs 1x when not) --
     * a weighted median is fairly insensitive to a modest smooth reweighting layered on top of an
     * already-dominant strong signal, so even a real ranking signal doesn't move the needle much
     * applied this way. curve_corr_log stores each tick's value alongside cons_buf_log/agree_log
     * (same shape as agree_log) so the trailing weighted-median gather can weight each HISTORICAL
     * entry by the curve stability measured AT THAT TICK -- the plumbing itself is correct and was
     * verified as an exact no-op at CURVE_WEIGHT_LO=HI=1.0 (byte-identical tick output on both real
     * recordings, only STATE_SIZE_BYTES changed by the expected ~528B) before any real range was
     * swept. OPT-IN ONLY via CURVE_WEIGHT_ENABLE (undefined by default, zero cost in the real
     * shipped build), for the same reason as curve_gate_* above: a genuinely tested idea that
     * didn't pan out, kept for anyone who wants to try a different combine mechanism later (e.g.
     * feeding curve_corr into weighted_median()'s tie-breaking directly, rather than a smooth
     * multiplier on top of an already-dominant AGREE_BONUS). */
#ifdef CURVE_WEIGHT_ENABLE
    StreamAgreeLogEntry curve_corr_log[NLOG_V];
    float curve_weight_prev[S_LEN_V];
    int   curve_weight_prev_valid;
    int   curve_weight_snap_counter;
    int   curve_weight_corr_valid;
    float curve_weight_corr;
#endif
    /* TEMPLATE_WEIGHT_ENABLE (2026-08-09) -- same shape/role as curve_corr_log above, but logs each
     * PUSHED tick's morphology-template correlation (best_template_corr() in combo13_stream.c)
     * instead of tick-to-tick curve stability, for teal_size_factor()'s own soft multiplier. See
     * TEMPLATE_MATCH_ENABLE/TEMPLATE_WEIGHT_ENABLE's shared comment in combo13_stream.c for the
     * full rationale (this is the soft-weight follow-up to a hard-veto version that was tested and
     * rejected on full-corpus evidence). */
#ifdef TEMPLATE_WEIGHT_ENABLE
    StreamAgreeLogEntry template_corr_log[NLOG_V];
#endif
    int   log_head;
    int   log_count;

    float last_accepted_teal_bpm;
    bool  last_accepted_teal_bpm_valid;
    int   teal_jump_streak;

    float bpm_delay_anchor_bpm;
    bool  bpm_delay_anchor_valid;
    int   bpm_delay_streak;

    /* CURVE-CORRELATION QUALITY GATE (2026-08-09), PROTOTYPED AND REJECTED as a hard veto, per
     * Daniel's "how would you propose adding this into the main algorithm... could it be used to
     * improve our algorithm further?" curve_corr is corpus-validated as the single strongest
     * individual predictor of correct-vs-wrong of any raw signal available (AUC 0.78, see project
     * memory / comboConfidence()'s own comment in pcg_visualizer, where it WAS successfully folded
     * in as a soft blended term). But a strong RANKING signal is not automatically a good CUTOFF
     * signal: directly checked accuracy of ticks below several candidate hard thresholds against
     * the same 314-file/17,076-tick corpus -- even in the STRICTEST band (curve_corr < 0.35, the
     * default swept below), those ticks are still 63.3% correct. A hard veto at ANY threshold this
     * project tried (0.0/0.2/0.35/0.5) discards roughly 2 correct answers for every 1 truly-wrong
     * one it catches, confirmed via a full corpus sweep (STREAK_LIMIT 1/2/4, requiring 1-4
     * consecutive 4s snapshot periods below threshold before vetoing): correct answers dropped
     * 2724->1253-1988 on a 45-file sample while octave_errors barely moved (54->21-57) and "other"
     * errors dropped but nowhere near enough to justify the correct-answer cost (322->70-131).
     * A real implementation bug was found and fixed along the way (curve_gate_low_streak was
     * incrementing once per TICK instead of once per 4s SNAPSHOT EVENT, since curve_gate_corr is
     * held constant for all 16 ticks between snapshots -- this let one bad window blow past any
     * STREAK_LIMIT within itself) -- fixing it improved the picture but did not change the
     * fundamental conclusion. Kept here, OPT-IN ONLY via CURVE_GATE_ENABLE (undefined by default,
     * zero RAM/clock cost in the real shipped build) for anyone who wants to re-experiment --
     * e.g. with a softer weight-reduction instead of a hard veto, which this project has NOT yet
     * tried and which the underlying AUC=0.78 signal strength suggests could work better, given it
     * already improved the confidence composite as a blended (not gating) term. */
#ifdef CURVE_GATE_ENABLE
    float curve_gate_prev[S_LEN_V];
    int   curve_gate_prev_valid;
    int   curve_gate_snap_counter;
    int   curve_gate_corr_valid;
    float curve_gate_corr;
    int   curve_gate_low_streak;
#endif

    float peak_mean_below_since_el;

    /* -- streaming decimation state (variants 2/3 decimate BEFORE detection; variant 1 decimates
     * AFTER its native-rate detection stage cleans the signal) -- */
    float dec_aa_z[4];             /* continuous AA-filter state */
    int   dec_stride_phase;         /* samples produced by the filter since the last kept sample */

    /* -- streaming artifact-removal state (the core RAM-reduction mechanism) -- operates at
     * native rate for variant 1, at the decimated rate for variants 2/3. */
#if DETECT_AT_NATIVE
    float native_chunk_buf[NATIVE_CARRY_N + NATIVE_CHUNK_N];
    int   native_chunk_len;         /* how much of the buffer (from NATIVE_CARRY_N onward) is filled */
#else
    float dec_chunk_buf[DEC_CARRY_N + DEC_CHUNK_N];
    int   dec_chunk_len;
#endif
    StreamBaseline art_baseline;     /* ARTIFACT_BASELINE_PCTL-percentile tracker, for remove_transients */
    StreamBaseline med_baseline;      /* median tracker, shared by declick_mult + detect_spike_runs */
    /* wall-clock time (seconds since c13stream_init) of the START of the next not-yet-processed
     * chunk's contents -- incremented by chunk_duration_s each time a chunk completes, used to
     * timestamp detected spikes (which happen at chunk-processing granularity, not tick
     * granularity) onto the same `el` timeline c13stream_tick() advances. */
    float chunk_time_cursor;

    /* -- decimated-sample COUNTER only (2026-08-05, RAM/clock pass 4: the actual raw-decimated-
     * rate history buffer this used to back, `dec_hist[DEC_MAX_V]`, is GONE -- see band_filt_z/
     * band_env_ring below for why it's no longer needed). Every gating check elsewhere in this
     * file that asks "how many decimated samples have we seen so far" (sig_win sizing, the 3s/
     * hist_len>=win_nsamp readiness checks, check_harmonic_confirmed's bound) still needs this
     * count in RAW-SAMPLE units even though no raw samples are stored anymore -- so it stays a
     * plain saturating counter, incremented once per appended sample, same saturation cap
     * (DEC_MAX_V) and semantics as before. Renamed from dec_hist_len since "dec_hist" no longer
     * exists as a buffer. */
    int   dec_sample_count;

    /* -- persistent per-band bandpass filter + envelope state (2026-08-05, RAM/clock pass 4),
     * REPLACING scratch_work's per-tick from-scratch gather+filter+envelope entirely, and (since
     * that was dec_hist's ONLY reason to exist) letting the whole raw-decimated-history buffer go
     * too. Updated exactly once per NEW decimated sample as it's appended (see stream_dec_samples)
     * -- same technique already validated for the tilt-trace bandpass (task 7): band_filt_z/
     * band_env_z are float, carried forward continuously (never reset), computed from the exact
     * unquantized previous value each step. Every FRAME_DS-th sample (a fixed, absolute-stream-
     * position phase -- NOT relative to whatever window a given tick happens to be using, a
     * genuine behavior change from the old per-tick-relative phase, same class of accepted
     * causality difference as the tilt-trace's own streaming redesign), the 3 bands' current
     * envelope values are pushed into band_env_ring. feat_norm_clip/broad_env/the r_adaptive
     * preview all just gather trailing frames from this ring now -- no raw filtering left in any
     * of them. Eliminates the redundant per-tick full-window recompute (this same 3-band filter+
     * envelope was being redone from scratch up to 3x/tick across sum_env_norm/broad_env/
     * r_adaptive-preview) AND ~16KB of storage (dec_hist's 8KB + scratch_work's 8KB), replaced by
     * 3 rings of FEAT_MAX_V int16 frames (~4.8KB) plus a few dozen bytes of filter/envelope state. */
    float band_filt_z[3][4];
    float band_env_z[3];
    int   band_frame_phase;      /* raw decimated samples accumulated since the last frame push */
    int16_t band_env_ring[3][FEAT_MAX_V];
    int   band_env_ring_len;      /* frames pushed so far, saturates at FEAT_MAX_V */
    int   band_env_ring_head;      /* next write position (circular once full) */

    float scratch_sumenv[FEAT_MAX_V];   /* NOT part of the shared pool below -- its own lifetime
                                            spans from sum_env_norm's write through
                                            check_harmonic_confirmed's read much later in the same
                                            tick, overlapping several of the shared buffer's own
                                            sequential uses in between, so it can't share memory
                                            with them. */
    /* SHARED transient scratch (2026-08-06, RAM consolidation -- was 3 separate buffers:
     * scratch_feat_a[FEAT_MAX_V], scratch_feat_b[FEAT_MAX_V], scratch_tilts[TILT_MAX_FRAMES],
     * totaling ~11.2KB). Every one of those was used STRICTLY SEQUENTIALLY within a single
     * c13stream_tick() call -- confirmed by tracing the call order: sig_level's broad_env ->
     * r_adaptive preview's per-band feat_norm_clip -> sum_env_norm's band0 then band1 (accumulated
     * into the caller's `out` one band at a time, never needing two bands' scratch alive at once)
     * -> add_autocorr_robust's `prod` -> the peak-mean flatline check's broad_env -> the
     * tilt-trace's gather-then-mean-subtract (done in place here now, eliminating a same-sized
     * local stack array the old scratch_tilts-based version also needed -- tilt itself, and its
     * gather buffer's sizing requirement, were removed entirely in revision 0.4, 2026-08-08).
     * Each use is fully consumed before the next begins, so one buffer sized to the largest need
     * replaces all 3, saving ~6.4KB net at the time.
     * scratch_Sext[EXT_S_LEN_V] deliberately does NOT join this pool, despite looking like a
     * plausible 4th candidate: check_harmonic_confirmed uses it as an OUTER accumulator across a
     * whole call to add_autocorr_robust, which itself uses scratch_shared as its OWN internal
     * `prod` scratch during that exact same call -- the two would alias and corrupt each other if
     * merged. Caught by tracing actual concurrent liveness, not assumed. */
    float scratch_shared[SCRATCH_SHARED_LEN];
#ifdef COMBO13_EXPOSE_DEBUG
    int   dbg_slow_valid; float dbg_slow_bpm; float dbg_slow_ratio;   /* 0.15 slow-zone candidate (every tick win_teal is valid) */
#endif
    int   slow_override_active;   /* 0.15: 1 on a tick where the 30-40bpm slow-zone override chose this tick's candidate (diagnostic) */
    float scratch_Sext[EXT_S_LEN_V];

#ifdef COMBO13_EXPOSE_DEBUG
    /* VISUALIZATION-ONLY INSTRUMENTATION (2026-08-07), OFF by default -- adds read-only snapshots of
     * intermediate signals purely for an external tool to render (pcg_visualizer's Combo13-internals
     * panels). Nothing in c13stream_tick()'s own algorithm ever READS these fields back, so they
     * cannot influence any real bpm/flatline/harmonic decision -- confirmed by construction (grep
     * combo13_stream.c for COMBO13_EXPOSE_DEBUG: every touched line is inside one of these guards,
     * either writing here or reading scratch_sumenv, which is unconditionally live already). See
     * combo13_stream.c's own c13stream_tick() for exactly where each snapshot is taken -- always
     * immediately after the source value is computed, before anything downstream can overwrite the
     * scratch memory it came from. Adds ~5KB to STATE_SIZE_BYTES only when this macro is defined --
     * the production build (pcg_visualizer/combo13_wasm.js) never defines it, so this whole block
     * compiles out entirely there; a separate combo13_wasm_debug.js build defines it. */
    float dbg_autocorr_raw[S_LEN_V];   /* win_r.Sw -- the weighted/min-subtracted main autocorrelation
                                           curve, BEFORE the further detrend step below. Kept for
                                           reference/comparison; C2's panel plots dbg_win_teal_curve
                                           instead (2026-08-07 fix, see that field's own comment --
                                           this comment used to WRONGLY claim they were the same). */
    int   dbg_autocorr_valid;
    int   dbg_autocorr_lo;              /* LO_LAG_V -- bin i corresponds to lag (LO_LAG_V+i) */
    float dbg_win_teal_bpm;             /* the detrended peak pick BEFORE any downstream suppression/
                                            veto/jump-guard, so the curve's own peak marker always
                                            matches what's actually drawn even on ticks where the
                                            already-public raw_candidate_bpm is null */
    int   dbg_win_teal_valid;
    /* dbg_win_teal_curve (2026-08-07, per Daniel's "just plot the real curve" request): the ACTUAL
     * detrended curve `estimate_detrend_from_mw()` picks dbg_win_teal_bpm's peak from -- win_r.Sw
     * above with a local moving-average (+-TEAL_PUSH_HALF_WIDTH lag-bins) subtracted off. Until
     * this fix, C2's panel plotted dbg_autocorr_raw (the PRE-detrend curve) while marking a peak
     * dot picked from THIS curve instead -- a real, confirmed mismatch (diverged on ~24% of ticks
     * in a real-recording spot-check, sometimes by 100+bpm) that made the dot look randomly
     * misplaced. Same S_LEN_V size/indexing as dbg_autocorr_raw -- no separate max-length getter. */
    float dbg_win_teal_curve[S_LEN_V];
    /* V2 PEAK-PICKER VISUALIZATION (2026-08-10), per Daniel's "add a panel that lets us visualize
     * the peak picker to the pcg visualizer" -- exposes TEMPLATE_MATCH_GATED_V2_ENABLE's own
     * internal candidate list every tick (see that macro's comment in combo13_stream.c for the
     * mechanism itself): up to COMBO13_DBG_V2_MAX_PEAKS local maxima of dbg_win_teal_curve above,
     * each with its own template-match score, plus which one (if any) actually won the gated
     * override. dbg_v2_valid=0 means either V2 didn't run this tick (win_r/win_teal invalid) or
     * found zero local maxima -- dbg_v2_n_peaks/peak arrays/winner_idx are only meaningful when
     * this is 1. dbg_v2_winner_idx=-1 means peaks WERE found but the confidence floor rejected all
     * of them (distinct from "V2 didn't run at all"). COMBO13_DBG_V2_MAX_PEAKS is a plain literal,
     * not a reference to TEMPLATE_MATCH_GATED_V2_N_PEAKS_OVERRIDE (defined later, in
     * combo13_stream.c, using this header's own constants) -- keep the two in sync by hand if
     * either ever changes; only affects this debug-only array sizing, never real algorithm
     * behavior. */
#define COMBO13_DBG_V2_MAX_PEAKS 6
    int   dbg_v2_valid;
    int   dbg_v2_n_peaks;
    int   dbg_v2_winner_idx;
    float dbg_v2_peak_bpm[COMBO13_DBG_V2_MAX_PEAKS];
    float dbg_v2_peak_score[COMBO13_DBG_V2_MAX_PEAKS];
    /* AUTOCORRELATION CURVE CORRELATION PROTOTYPE (2026-08-09) -- see combo13_stream.c's own
     * comment at the write site for the full rationale, and CURVE_CORR_GAP_TICKS's own comment
     * above for why this is a ring buffer of CURVE_CORR_GAP_TICKS curves (not just 1 -- comparing
     * ONLY 1 tick apart made the metric dominated by the growing/sliding window's own >=96% sample
     * overlap at that gap, not genuine curve-shape (in)stability). curve_ring/curve_ring_head/
     * curve_ring_count hold the last CURVE_CORR_GAP_TICKS ticks' win_teal.Sw curves; each new tick
     * correlates against the OLDEST entry (exactly CURVE_CORR_GAP_TICKS ticks old) before
     * overwriting it. dbg_curve_corr/dbg_curve_corr_valid hold the result. All prototype-only, no
     * use elsewhere in the algorithm yet. */
    float curve_ring[CURVE_CORR_GAP_TICKS][S_LEN_V];
    int   curve_ring_head;
    int   curve_ring_count;
    float dbg_curve_corr;
    int   dbg_curve_corr_valid;
    int   dbg_band_env_n;               /* valid length of scratch_sumenv this tick (win_nsamp/FRAME_DS)
                                            -- scratch_sumenv itself already persists as a real struct
                                            field above, no separate copy needed */
    /* Per-band RAW envelope (2026-08-07, per Daniel's request to see the 3 individual bands that
     * sum into scratch_sumenv/C1) -- unlike scratch_sumenv, no persistent per-band-only field
     * already exists (sum_env_norm's per-band gather is transient, immediately summed and
     * discarded), so this needs 3 new snapshots, gathered via the ALREADY-existing
     * band_env_gather_trailing() (a pure read of the persistent band_env_ring, no new ring
     * needed). Same window length/validity as dbg_band_env_n above, for direct visual
     * correspondence with C1. RAW/unnormalized scale (same units as band_env_ring itself) --
     * NOT the z-scored+clipped version sum_env_norm actually sums. */
    float dbg_band0_env[FEAT_MAX_V];
    float dbg_band1_env[FEAT_MAX_V];
    float dbg_band2_env[FEAT_MAX_V];
    int   dbg_flux_valid;   /* own-accuracy sweep methodology applied to flux (2026-08-08) --
                                straight from the flux agreement-signal block, not the combine. */
    float dbg_flux_bpm;
    /* Live-panel instrumentation for flux (2026-08-08, added when the visualizer's diagram was
     * updated to show flux's own real steps instead of tilt's, now that flux drives the actual
     * agreement decision -- and tilt's own equivalent dbg_tilt_trace/dbg_tilt_autocorr fields were
     * removed entirely along with tilt itself, revision 0.4, same day). Sized to FEAT_MAX_V/S_LEN
     * (flux's own trace/curve lengths -- flux runs at the decimated envelope's frame rate and lag
     * range, same as the main candidate). dbg_flux_autocorr captures the ACTUAL post-lagpow/
     * detrend curve flux_bpm's peak is picked from -- same "plot the real curve" discipline C2/C4
     * needed real bug fixes to get right. */
    float dbg_flux_trace[FEAT_MAX_V];
    int   dbg_flux_trace_len;
    float dbg_flux_autocorr[S_LEN_V];
    int   dbg_flux_autocorr_valid;
    float dbg_peak_minus_mean;          /* the flatline detector's own decision metric */
    int   dbg_peak_mean_valid;

    /* PART A PANELS (2026-08-07) -- A1 (raw unfiltered audio) needs no new state, it's just
     * pcgRing's own samples read straight in JS. A3/A4 below each pair a persistent RING
     * (written continuously from c13stream_push()'s own per-sample/per-chunk code) with a
     * LINEARIZED trailing-window SNAPSHOT (gathered once per tick) -- so JS never has to replicate
     * any circular-buffer index math itself, it just reads a plain contiguous array via HEAPF32
     * each tick. (A2, the tilt-bandpass input ring, was removed with tilt itself, revision 0.4,
     * 2026-08-08.) */
    float dbg_decpre_ring[DEC_MAX_V];    /* A3: decimated-rate signal straight out of
                                             streaming_decimate() (AA-filtered + stride-kept),
                                             BEFORE any chunk-based artifact removal/declick */
    long  dbg_decpre_total;
    float dbg_decpre_trace[DEC_MAX_V];
    int   dbg_decpre_trace_len;

    float dbg_decpost_ring[DEC_MAX_V];   /* A4: the SAME decimated-rate signal AFTER
                                             process_and_emit_chunk's artifact-removal+declick --
                                             this is what actually reaches stream_dec_samples/the
                                             band-envelope pipeline */
    long  dbg_decpost_total;
    float dbg_decpost_trace[DEC_MAX_V];
    int   dbg_decpost_trace_len;
    /* A4 OVERLAY FLAGS (2026-08-07): parallel to dbg_decpost_ring/_trace above, SAME indexing
     * (dbg_decpost_total/dbg_decpost_trace_len, pushed/gathered in lockstep, no separate counters
     * needed) -- 1.0f/0.0f marking which samples in that same output were touched by
     * stream_detect_extend_interp's artifact-interpolation vs. hard-clipped by declick_clip, so
     * the visualizer can overlay exactly where each intervention fired on the waveform. */
    float dbg_decpost_artifact_ring[DEC_MAX_V];
    float dbg_decpost_declick_ring[DEC_MAX_V];
    float dbg_decpost_artifact_trace[DEC_MAX_V];
    float dbg_decpost_declick_trace[DEC_MAX_V];
    /* SPIKE-DETECT MASK (2026-08-07): same parallel/lockstep convention as the artifact/declick
     * pair above -- 1.0f/0.0f marking which samples fell inside a detected spike run (the
     * SPIKE_DETECT_MULT=20x threshold scan that only records last_spike_el, never modifies the
     * signal). Populated from a NEW scratch_spike[] local in process_and_emit_chunk (spike
     * detection itself doesn't build a mask normally -- it only needs the single loudest sample's
     * timestamp -- so this is debug-only bookkeeping with zero production cost). */
    float dbg_decpost_spike_ring[DEC_MAX_V];
    float dbg_decpost_spike_trace[DEC_MAX_V];

    /* PART B PANELS (2026-08-07) -- all 3 plain scalars, no ring/snapshot pairing needed since
     * these are already exactly one value per tick (not a per-sample/per-chunk stream like Part A). */
    float dbg_sig_level;         /* B1: the onset detector's own signal-level metric */
    int   dbg_sig_level_valid;   /* false whenever hist_len==0 (cold start) */
    float dbg_adaptive_tau;      /* B3: the actual decay time-constant used this tick (a plain
                                     clamp of since_event -- B2 exposes the raw timestamps
                                     since_event itself is computed from; JS recomputes since_event
                                     with the identical formula rather than duplicating storage) */
    float dbg_r_adaptive_bpm;    /* B3: the adaptive-tau preview estimate itself */
    int   dbg_r_adaptive_valid;
    float dbg_frozen_wait_s;     /* B3 (2026-08-07): the actual consequence of adaptive_tau/
                                     r_adaptive_bpm above -- CONS_WAIT_S + extra-per-bpm-from-60,
                                     only re-set on a fresh event but snapshotted every tick so the
                                     trace shows its CURRENT held value continuously, not just at
                                     reset moments. Was previously invisible anywhere in this panel
                                     set despite being the one number the whole mechanism serves. */

    /* GATE CANDIDATE (2026-08-07), per Daniel's request to visualize the "early-release agreement
     * / harmonic veto / jump-confirm + settling-delay guards" step -- all 5 plain scalars, one
     * value per tick, same convention as the Part B fields above. raw_candidate_valid/bpm and
     * harmonic_confirmed are ALREADY unconditional fields on Combo13StreamTick itself (out[4-6]),
     * so only the guards downstream of those needed new storage: */
    int dbg_suppress_circle;        /* is_flatline || is_new_event || still-waiting-without-early-release */
    int dbg_early_release_ok;       /* context for the above -- was suppression bypassed via early agreement? */
    int dbg_early_agree_now;        /* THIS tick's raw agreement-signal-vs-teal agreement, before
                                        streak-gating (2026-08-07) -- early_release_ok above is the
                                        SUSTAINED/gated version of this; this is the instantaneous input to it. */
    int dbg_jump_unconfirmed;       /* candidate jumped >=20-40% from last ACCEPTED value, not yet sustained */
    int dbg_bpm_delay_unconfirmed;  /* candidate hasn't repeated near its own anchor long enough yet */
    int dbg_pushed_valid;           /* the actual outcome: did this tick's candidate reach cons_buf_log? */

    /* CONFIDENCE METRIC PROTOTYPE (2026-08-08), per Daniel's "how would you generate a confidence
     * metric... attempt that and prototype it." 3 raw component signals, captured at the moment
     * this tick's candidate is (or isn't) pushed -- the composite score itself is computed in JS
     * from these, not in C, so the formula can be iterated on without a rebuild while validating.
     * dbg_conf is win_teal.conf, ALREADY computed every tick for the pushed candidate (peak-height
     * ratio between the tallest autocorrelation peak and the next DISTINCT peak, >=8 lag bins away
     * -- a classic pitch-detection prominence/ambiguity measure) but previously write-only: stored
     * into cons_buf_log[].conf and never read back out anywhere. dbg_agree_margin is new -- the raw
     * |flux_bpm - raw_candidate_bpm| distance when both are independently valid (finer-grained than
     * the binary AGREE_BONUS gate teal_size_factor() already uses). dbg_trailing_spread is new --
     * the max-min spread of the weighted-median's own trailing candidate window, a stability proxy
     * independent of any single tick's own signal quality. */
    float dbg_conf;
    int   dbg_conf_valid;
    float dbg_agree_margin;
    int   dbg_agree_margin_valid;
    float dbg_trailing_spread;
    int   dbg_trailing_count;

    /* MORPHOLOGY TEMPLATE-MATCH PROTOTYPE (2026-08-09), gated behind TEMPLATE_MATCH_ENABLE (see that
     * macro's own comment in combo13_stream.c) -- Pearson correlation between this tick's raw
     * candidate curve (win_r.Sw) and the nearest-bpm-bucket template. Only written when
     * TEMPLATE_MATCH_ENABLE is ALSO defined (on top of COMBO13_EXPOSE_DEBUG); left at its
     * zero-initialized state (dbg_template_match_corr_valid=0) otherwise. */
    float dbg_template_match_corr;
    int   dbg_template_match_corr_valid;
    int   dbg_template_match_bucket;
#endif
};

#endif /* COMBO13_STREAM_INTERNAL_H */
