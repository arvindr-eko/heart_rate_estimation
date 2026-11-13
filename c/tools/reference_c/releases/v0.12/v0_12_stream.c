/* combo13_stream.c -- RAM-reduced streaming-front-end variant of the combo13 pipeline.
 *
 * GOAL: cut RAM from combo13_opt's 761KB (88% of which is six 8-second, native-rate/32000-sample
 * buffers needed by the whole-window batch artifact-removal design) down to Daniel's 50-70KB
 * PSoC6 target. See combo13_stream_internal.h for the full design rationale.
 *
 * WHAT CHANGED vs. combo13_opt.c (this is a genuine algorithm redesign, not a memory trick):
 *   1. remove_transients/declick_mult/detect_spike_runs' "sort/percentile over the whole 8s
 *      window" replaced with a streaming EMA-of-per-chunk-percentile estimator (StreamBaseline),
 *      updated continuously, plus detect/extend/interpolate over a SMALL chunk+carry window
 *      instead of the whole history.
 *   2. The two parallel decimated chains (with/without artifact-removal) consolidated into ONE
 *      -- onset detection and the r_adaptive preview now read from the SAME artifact-removed
 *      decimated signal as the main estimate (Daniel's explicit choice, 2026-08-04).
 *   3. STREAM_VARIANT (compile-time) controls WHERE detection happens relative to decimation,
 *      and at what decimated rate -- this file exists specifically to let Daniel compare:
 *        1 = detect at native rate (4kHz), decimate to 500Hz after cleaning
 *        2 = decimate to 500Hz first, detect at 500Hz
 *        3 = decimate to ~666.7Hz first, detect at that rate
 *
 * NOT YET DONE: full corpus retuning of the streaming-specific constants (chunk size, EMA decay,
 * rescaled pad_samples) -- these are reasoned first-pass values, validated only at the level of
 * "does this behave sanely," not corpus-tuned the way combo13's original constants were. Treat
 * this file as an exploratory comparison, per Daniel's own framing ("try both and see what the
 * performance looks like"), not a drop-in replacement for combo13_opt.c.
 */
#include "v0_12_stream.h"
#include <math.h>
#include <string.h>
#include <stdlib.h>
#ifdef STREAM_DEBUG
#include <stdio.h>
#endif

#define FS_IN 4000.0f
#define TICK_S 0.25f
#define FRAME_DS FRAME_DS_V
#define LO_LAG LO_LAG_V
#define HI_LAG HI_LAG_V
#define S_LEN S_LEN_V
/* LAG_POW retuned for variant 4 only (2026-08-06, dedicated sweep -- see ROBUST_TRIM_FRAC's own
 * comment below for the full sweep/combo methodology). 0.1->0.2 was part of the best-performing
 * combo found; variants 1/2/3 keep the original, already-validated 0.1 untouched.
 * RE-SWEPT 2026-08-09 (override hook added), REJECTED, after SPIKE_DETECT_MULT changed the
 * artifact-removal chain feeding the autocorrelation this constant operates on. 1-D sweep on a
 * 45-file sample showed a clean-looking octave-error win at 0.25-0.30 (37->21-24) with correct
 * roughly flat -- but a full 314-file corpus check at LAG_POW=0.28 told a different story: correct
 * 16,014->15,879 (-135, a REAL loss), octave_errors 119->102 (-17, genuinely better), other
 * 930->889 (-41, better), no_display +193. A real octave-error-vs-overall-accuracy TRADEOFF, not a
 * clean win -- exactly the "60-file/45-file sample misleads" pattern this project's own history
 * already flags for this specific constant (see ROBUST_TRIM_FRAC's comment above from the original
 * 2026-08-06 variant-4 tuning pass). NOT PROMOTED, left at 0.2. Override hook kept for future
 * re-sweep. */
#if STREAM_VARIANT == 4
#ifndef LAG_POW_OVERRIDE
#define LAG_POW_OVERRIDE 0.2f
#endif
#define LAG_POW LAG_POW_OVERRIDE
#else
#define LAG_POW 0.1f
#endif
/* AGREEMENT SIGNAL (revision 0.4, 2026-08-08): flux is now the ONLY agreement/combine-weight
 * signal -- tilt (the native-rate/FFT pitch-tilt trace this project inherited from combo13_opt)
 * has been fully removed, struct fields and all, after confirming by direct grep that its result
 * was consumed nowhere except the (dead, since revision 0.2) tilt branch of what used to be the
 * AGREEMENT_SIGNAL_FLUX toggle, plus debug capture the visualizer no longer reads. See project
 * memory (project_pcg_embedded_c) for the full removal writeup and the resulting RAM/clock-cycle
 * numbers. */


/* FLUX tuning knobs (2026-08-08, optimization sweep) -- deliberately kept as a SEPARATE set of
 * macros from tilt's own now-removed TILT_AC_ROBUST/LAGPOW/DETREND (even though defaults matched)
 * so a flux sweep could vary these independently without touching tilt's already-validated config.
 * Sweep findings (full 314-file corpus, own-accuracy AND real final-output validated for each
 * before trusting it -- see project memory project_pcg_embedded_c for full detail):
 *   - ROBUST/LAGPOW/DETREND structure: plain autocorrelation (all 3 off) scored best on flux's own
 *     accuracy (77.9% vs the shipped 73.3%) and even improved the final output's correct+octave
 *     counts, but at a real cost to "other"/no_display coverage -- Daniel's explicit call was to
 *     keep LAGPOW+DETREND (matches the structure already promoted for tilt) rather than trade
 *     caution for coverage. Left at ROBUST=0/LAGPOW=1/DETREND=1, unchanged from 0.2.
 *   - FLUX_CLIP: monotonic own-accuracy improvement up to ~6.0-8.0, but did NOT survive contact
 *     with the real final output (correct flat, "other" got WORSE) -- classic own-accuracy-isn't-
 *     the-real-metric result, NOT promoted. Left at 4.5 (matches FEAT_CLIP).
 *   - FLUX_RECTIFY_MODE: PROMOTED to full-wave (1) in the same 2026-08-08 session, revision 0.3 --
 *     see this macro's own comment below for the full story. */
#ifndef FLUX_CLIP
#define FLUX_CLIP 4.5f          /* matches FEAT_CLIP -- tried 6.0/8.0, own-accuracy improved but the
                                   real final output didn't, so left unchanged (see block comment) */
#endif
/* FLUX_RECTIFY_MODE (added 2026-08-08, PROMOTED to full-wave the same day -- revision 0.3): 0 =
 * half-wave (max(0,diff), matches flux_algorithm.py exactly -- the classic spectral-flux onset-
 * detection convention, only counts energy RISES). 1 (NOW THE DEFAULT) = full-wave (fabsf(diff)) --
 * symmetric to both S1-rise AND S2-rise transitions instead of only rises. Full 314-file corpus
 * own-accuracy: 73.3%->80.1% (+6.8pp), octave errors 565->469. Real final-output validation:
 * correct 14667->14810 (+143), octave_errors 159->168 (+9), other 888->929 (+41), no_display
 * 10369->10176 (-193) -- a real coverage/caution tradeoff in the aggregate, same shape as the
 * ROBUST/LAGPOW/DETREND structure question above, BUT unlike that one this was promoted: spot-
 * checked against both real disconnect/reconnect recordings and found a genuine, concrete fix, not
 * just a statistical tradeoff -- full-wave ELIMINATES the pre-existing spurious 160bpm lock on
 * daniel_recovery_test.f32 (t=25.5-27.75s under half-wave) entirely, and reaches the correct
 * ~52bpm reading ~9.5s earlier on daniel_offchest_test.f32 with zero wrong intermediate values.
 * Promoted per Daniel's explicit "yes, promote it." */
#ifndef FLUX_RECTIFY_MODE
#define FLUX_RECTIFY_MODE 1
#endif
#ifndef FLUX_AC_ROBUST
#define FLUX_AC_ROBUST 0
#endif
#ifndef FLUX_AC_LAGPOW
#define FLUX_AC_LAGPOW 1
#endif
#ifndef FLUX_AC_DETREND
#define FLUX_AC_DETREND 1
#endif

/* Decimated sample rate actually produced by DEC_STRIDE, used for FFS/pad-sample rescaling. */
#define DEC_HZ (FS_IN / (float)DEC_STRIDE)
#define FFS (DEC_HZ / (float)FRAME_DS)

/* combo13's tuned scalar constants -- unchanged from combo13_opt where dimensionless.
 * SIGNAL_ONSET_THRESH/ONSET_DEBOUNCE_TICKS are NOT swept below (2026-08-09) -- confirmed dead for
 * production output per the "onset retired from event-timing" comment further down this file
 * (sig_was_onset_ok/last_onset_el are computed but no longer read by anything that affects
 * pushed_valid/bpm; kept only because pcg_visualizer's B1/C0/B2 debug panels still read them).
 * The other 5 (SPIKE_DETECT_MULT/TRANSIENT_LIMIT_MULT/ARTIFACT_DETECT_MULT/ARTIFACT_EXTEND_MULT/
 * ARTIFACT_BASELINE_PCTL) ARE live and, per Daniel's "any other ideas to increase accuracy?", were
 * swept for the first time since inheriting them unchanged from combo13_opt -- never independently
 * re-validated for the CURRENT streaming/decimated/int16-quantized pipeline, same situation
 * PEAK_MEAN_DIFF_THRESH/TOP_PCT turned out to be in on 2026-08-08 (that sweep found a real ~7pp
 * gain). SPIKE_DETECT_MULT: 1-D sweep on a 45-file sample found a clean plateau at 14-16 (20.0's
 * neighbors above 18 revert to baseline-like behavior -- a sharp step, not sample noise), promoted
 * to full 314-file corpus check: correct 15,971->16,014 (+43), octave_errors 164->119 (-27.4%!),
 * other 975->930 (-4.6%), no_display 8,973->9,020 (+47, a small and acceptable cost given correct
 * ALSO rose). BYTE-IDENTICAL on both real recordings (the improvement lands on other corpus files
 * entirely, zero regression risk on the two witnessed real-world scenarios). TRANSIENT_LIMIT_MULT/
 * ARTIFACT_DETECT_MULT/ARTIFACT_EXTEND_MULT/ARTIFACT_BASELINE_PCTL: swept too, all showed only
 * sample-level noise that did NOT survive full-corpus re-check (e.g. TRANSIENT_LIMIT_MULT=6.0
 * looked promising on 45 files, correct 15,971->15,967 i.e. flat-to-slightly-worse at full scale --
 * the exact "60-file/45-file sample misleads" pattern this project has hit before with TENT) --
 * NOT PROMOTED, left at their original combo13_opt-inherited values. Override hooks kept for all 5
 * for any future re-sweep. **Promoted SPIKE_DETECT_MULT 20.0->15.0.** */
#ifndef SPIKE_DETECT_MULT_OVERRIDE
#define SPIKE_DETECT_MULT_OVERRIDE 15.0f
#endif
#ifndef TRANSIENT_LIMIT_MULT_OVERRIDE
#define TRANSIENT_LIMIT_MULT_OVERRIDE 4.29f
#endif
#ifndef ARTIFACT_DETECT_MULT_OVERRIDE
#define ARTIFACT_DETECT_MULT_OVERRIDE 16.96f
#endif
#ifndef ARTIFACT_EXTEND_MULT_OVERRIDE
#define ARTIFACT_EXTEND_MULT_OVERRIDE 8.02f
#endif
#ifndef ARTIFACT_BASELINE_PCTL_OVERRIDE
#define ARTIFACT_BASELINE_PCTL_OVERRIDE 0.4231f
#endif
static const float SIGNAL_ONSET_THRESH = 0.00021f;
static const int   ONSET_DEBOUNCE_TICKS = 4;
static const float SPIKE_DETECT_MULT = SPIKE_DETECT_MULT_OVERRIDE;
static const float TRANSIENT_LIMIT_MULT = TRANSIENT_LIMIT_MULT_OVERRIDE;
static const float ARTIFACT_DETECT_MULT = ARTIFACT_DETECT_MULT_OVERRIDE;
static const float ARTIFACT_EXTEND_MULT = ARTIFACT_EXTEND_MULT_OVERRIDE;
static const float ARTIFACT_BASELINE_PCTL = ARTIFACT_BASELINE_PCTL_OVERRIDE;
/* ROBUST_TRIM_FRAC, LAG_POW, TEAL_JUMP_RATIO, TEAL_JUMP_CONFIRM_TICKS, and
 * HARMONIC_VETO_LO_FRAC/HI_FRAC (below) are retuned for variant 4 only (2026-08-06), per Daniel's
 * explicit request to see whether variant 4's real accuracy regression (see
 * combo13_stream_internal.h's SCRATCH_SHARED_LEN-adjacent comment for the variant's own history)
 * could be tuned back. Started from a dedicated 50-combination one-at-a-time sweep
 * (variant4_sweep_results.csv) plus a combined test (variant4_combo_results.csv) -- a first
 * "everything" combination (6 constants, including TEAL_PUSH_HALF_WIDTH=29 above and
 * WEIGHTED_MED_TRAILING_N=12 below) looked like a clean win on the SAME 60-file sample used
 * throughout this project (correct +21, octave errors -23, other -110). It was NOT: a full
 * 314-file leave-one-out re-check (variant4_leave_one_out_results.csv) showed that combination
 * actually cost 178 fewer correct answers at full scale, undetected by the smaller sample.
 * Reverting TEAL_PUSH_HALF_WIDTH and WEIGHTED_MED_TRAILING_N (both looked negligible-to-negative
 * at full scale once the other 4 constants were active) and retuning the harmonic-veto window to
 * a MIDDLE-ground width (1.93/2.07, between the too-aggressive 1.95/2.05 tried first and the
 * original 1.9239938526174543/2.081876428164688) found a 4-constant combination
 * (variant4_combo_round2_results.csv, "B") that genuinely beats variant 2 on the FULL 314-file
 * corpus: correct 14353->14358 (+5), octave errors 195->166 (-29), other 945->877 (-68) -- while
 * keeping variant 4's RAM/clock advantages intact. Standing lesson for this whole file: a 60-file
 * sample can materially misjudge a change; re-validate anything non-trivial at full corpus scale
 * before trusting it, especially for a metric (correct count) sensitive to which specific files
 * happen to be well- or poorly-represented in a small sample. Variants 1/2/3 keep every one of
 * their original, already-validated values untouched -- this must never change what the shipped
 * design does. */
#if STREAM_VARIANT == 4
/* RE-SWEPT 2026-08-09 (override hook added), PROMOTED 0.012->0.015, per Daniel's "any other ideas
 * to increase accuracy" follow-up -- SPIKE_DETECT_MULT changed 20.0->15.0 the same week, altering
 * the artifact-removal chain feeding the autocorrelation this constant trims, making it worth
 * re-checking rather than assuming the original variant-4 tuning still holds. 1-D sweep on a
 * 45-file sample found a clean plateau at 0.013-0.017 (octave_errors 37->25-30, correct flat-to-
 * improved) -- promoted to full 314-file corpus: correct 16,014->16,082 (+68), octave_errors
 * 119->114 (-5), other 930->933 (~flat), no_display -66 -- a genuine multi-metric win, not the
 * "sample misleads" trap LAG_POW (swept the same day, see its own comment above) turned out to be.
 * Diffed tick-by-tick against both real recordings: differences confined to diagnostic-only fields
 * (harmonic_confirmed/raw_cand) on already-non-displayed ticks, plus one already-correct displayed
 * value shifting by 0.6bpm (53.3->52.7bpm on daniel_offchest_test.f32, well within tolerance) --
 * neither file's own documented first-display timing/value or spurious-lock self-correction
 * changed. **Promoted ROBUST_TRIM_FRAC 0.012->0.015.** */
#ifndef ROBUST_TRIM_FRAC_OVERRIDE
#define ROBUST_TRIM_FRAC_OVERRIDE 0.015f
#endif
static const float ROBUST_TRIM_FRAC = ROBUST_TRIM_FRAC_OVERRIDE;
#else
static const float ROBUST_TRIM_FRAC = 0.0079f;
#endif
static const float FEAT_CLIP = 4.5f;
static const float SINGLE_WIN_MIN = 4.0f;
static const float SINGLE_WIN_MAX = 8.0f;
/* TEAL_PUSH_HALF_WIDTH is a detrending half-width in LAG-BIN units (S_LEN=HI_LAG_V-LO_LAG_V wide)
 * -- 36 was tuned against variant 1/2's S_LEN=120 (36/120=30% of the range). Variant 4 (2026-08-06,
 * RAM/clock exploration) shrank S_LEN to 96 (24-120); a proportionally-rescaled 29 was tried first
 * (36*96/120=28.8) and looked like a win on a 60-file corpus sample, but a full 314-file leave-
 * one-out re-check (once combined with variant 4's OTHER retuned constants below) showed 29 was a
 * net LOSS at full scale (+39 correct recovered by reverting to 36, for only a small octave-error
 * cost) -- reverted. Left at 36 for every variant, same value as always. Lesson for this whole
 * file: a 60-file sample materially misjudged this one; see ROBUST_TRIM_FRAC's own comment below
 * for the fuller story and the full-corpus numbers this was decided from. */
static const int   TEAL_PUSH_HALF_WIDTH = 36;
#ifndef CONS_WAIT_S_OVERRIDE
#define CONS_WAIT_S_OVERRIDE 6.0f
#endif
static const float CONS_WAIT_S = CONS_WAIT_S_OVERRIDE; /* raised from 4.0f (2026-08-04) as part of the startup-transient fix -- see COMBO13_OPTIMIZATIONS.md; override hook added 2026-08-09 for the first-reading-accuracy sweep, see TEAL_BPM_DELAY_SLOPE's own comment below */
static const float CONS_WAIT_EXTRA_S_PER_BPM = 0.020f;
/* EARLY RELEASE (2026-08-05, time-to-first-display attempt; retuned more aggressive 2026-08-06 at
 * Daniel's request) -- allows suppress_circle's flat-wait component to be bypassed by up to
 * EARLY_RELEASE_MAX_SAVE_S seconds, but ONLY once the growing-window candidate has agreed with
 * the independent agreement signal (flux, since revision 0.2; tilt before that and before its
 * removal in revision 0.4) for EARLY_RELEASE_MIN_TICKS consecutive ticks, capped at
 * EARLY_RELEASE_MAX_SAVE_S seconds of savings off whatever CONS_WAIT_S/frozen_wait_s already
 * computed for this event. Original values (5 ticks/1.5s) were a deliberately modest first pass;
 * swept 3 tiers against the full corpus (5/1.5 baseline, 3/3.0, 2/5.0) using a tick-level 60-file
 * bucketed check -- 3/3.0 stayed at ZERO new half/double/triple octave errors while cutting mean/
 * median time-to-first-display (7.04s->6.90s / 6.25s->6.00s full-corpus); 2/5.0 looked BETTER on
 * every per-file and first-display metric but introduced 2 new triple-rate errors in that same
 * check -- rejected 2026-08-06, shipped 3/3.0 instead.
 * RE-SWEPT 2026-08-07 (same day as the tilt autocorrelation promotion just above/below in this
 * file) and PROMOTED, since tilt is the exact signal this whole mechanism trusts and it's now
 * measurably less noisy: re-tested 2/3.0, 2/4.0, 2/5.0, 3/3.0, 3/4.0, 3/5.0 on the FULL 314-file
 * corpus (stronger than the original 60-file check) -- 2/5.0, the SAME combo rejected 08-06, now
 * shows ZERO new octave errors (164, identical to 3/3.0's own baseline) while correct rose
 * 14359->14607 and first-display classification improved on BOTH counts (correct 278->282, wrong
 * 18->17 out of 314 files) -- the prior rejection's own failure mode is gone because the signal it
 * depended on (tilt) genuinely got better, not because the risk went away on its own. Median
 * time-to-first-display: 6.00s->5.75s.
 * RE-SWEPT AGAIN 2026-08-08, same day as tilt's full removal (revision 0.4) -- 2/5.0 was tuned
 * entirely around TILT's own noise characteristics (08-07), before flux/AGREEMENT_SIGNAL_FLUX
 * existed at all (added later that same day). Full 314-file corpus grid over MIN_TICKS 1/2/3 x
 * MAX_SAVE_S 4.0/5.0/6.0 found MIN_TICKS=1 dominates on every metric simultaneously vs the shipped
 * 2/5.0 (correct 14810->15032, octave_errors 168->170 [+2, within noise], other 929->944, no_display
 * 10176->9937, median time-to-first-display 5.75s->5.50s) -- MAX_SAVE_S barely matters in this
 * range (checked 4.0/5.0/6.0/8.0/10.0; plateaus at 6.0, degrades slightly past 10.0 by trading
 * no_display for wrong "other" answers). PROMOTED to 1/6.0. Both real recordings
 * (daniel_recovery_test.f32/daniel_offchest_test.f32) diffed tick-by-tick against the prior 2/5.0
 * build -- every differing line was a diagnostic-only field (harmonic_confirmed/raw_cand); the
 * actual displayed bpm value and first-display timing were IDENTICAL on both files, including the
 * pre-existing documented spurious 160bpm lock on daniel_recovery_test.f32 (unchanged, not made
 * worse). Values below remain overridable via -D for future re-sweeps; this does NOT touch
 * CONS_WAIT_S itself or the post-wait confirmation ramp (required_teal_delay_ticks), both left
 * exactly as validated. */
#ifndef EARLY_RELEASE_MIN_TICKS_OVERRIDE
#define EARLY_RELEASE_MIN_TICKS_OVERRIDE 1
#endif
#ifndef EARLY_RELEASE_MAX_SAVE_S_OVERRIDE
#define EARLY_RELEASE_MAX_SAVE_S_OVERRIDE 6.0f
#endif
static const int   EARLY_RELEASE_MIN_TICKS = EARLY_RELEASE_MIN_TICKS_OVERRIDE;
static const float EARLY_RELEASE_MAX_SAVE_S = EARLY_RELEASE_MAX_SAVE_S_OVERRIDE;
/* ADAPTIVE FIRST-READING STRICTNESS (2026-08-09), per Daniel's "what do you think about adapting
 * some of these constants at the beginning of a file and after a flatline or spike... get the best
 * of both worlds if these constants were able to adapt to the situation" -- follow-up to the
 * first-reading-accuracy sweep above/below, which found real gains but at a global cost (stricter
 * confirmation delayed/dropped ticks EVERYWHERE, not just the one moment Daniel actually cares
 * about: a session's TRUE first-ever displayed value). st->ever_displayed (already existed, set
 * once true, never reset -- see its own established use in the flatline-reset bootstrap-exemption
 * logic above) already distinguishes exactly this: false only until the very first successful
 * display of the whole session; true for every later event (flatline recovery, spike-triggered
 * reset) for the rest of that session. These _FIRST variants apply ONLY while ever_displayed is
 * still false; every later event keeps using the plain (already fast-tuned) constants above,
 * completely unaffected. Defaults equal the plain values (a true no-op until deliberately set
 * stricter) -- see first_reading_sweep.py for the sweep and full-corpus numbers. */
#ifndef EARLY_RELEASE_MIN_TICKS_FIRST_OVERRIDE
#define EARLY_RELEASE_MIN_TICKS_FIRST_OVERRIDE EARLY_RELEASE_MIN_TICKS_OVERRIDE
#endif
#ifndef EARLY_RELEASE_MAX_SAVE_S_FIRST_OVERRIDE
#define EARLY_RELEASE_MAX_SAVE_S_FIRST_OVERRIDE EARLY_RELEASE_MAX_SAVE_S_OVERRIDE
#endif
static const int   EARLY_RELEASE_MIN_TICKS_FIRST = EARLY_RELEASE_MIN_TICKS_FIRST_OVERRIDE;
static const float EARLY_RELEASE_MAX_SAVE_S_FIRST = EARLY_RELEASE_MAX_SAVE_S_FIRST_OVERRIDE;
/* =12 for variant 4 was tried as part of the first sweep combo and reverted -- see
 * ROBUST_TRIM_FRAC's comment above for the full story; negligible at full-corpus scale once the
 * other constants were active, not worth the extra state. Same value (8) for every variant. */
static const int   WEIGHTED_MED_TRAILING_N = 8;
/* AGREEMENT_BPM_TOL/AGREE_BONUS (override hooks added 2026-08-08 for a re-sweep) -- consumed by
 * teal_size_factor()'s combine-weight bonus. Inherited unchanged through every agreement-signal
 * swap this project has made (tilt -> flux, revision 0.2) and every retune of the signal ITSELF
 * (flux's own autocorrelation structure/clip/rectification, revisions 0.2/0.3) -- never
 * independently re-validated for flux's specific noise characteristics until now.
 * SWEPT 2026-08-08 (same day as the EARLY_RELEASE re-sweep above): 1-D sweeps on the full 314-file
 * corpus, one constant held at its shipped value while the other varied. AGREEMENT_BPM_TOL:
 * already near-optimal at 11.10 -- 8.5/11.10/14.0 all scored within noise of each other, 6.0 was
 * clearly worse, 20.0 added new octave errors (too loose a tolerance starts accepting bad
 * agreements) -- left UNCHANGED. AGREE_BONUS: a real, monotonic trend -- octave_errors dropped
 * 168->163->162->161 as bonus went 2.70->4.5->6.5->8.0, with correct flat-to-slightly-improved and
 * zero cost to other/no_display/time-to-first-display -- plateaus past 6.5. PROMOTED to 6.5.
 * Combined with the EARLY_RELEASE re-sweep above and validated together (not just individually):
 * full corpus correct 14810->15053, octave_errors 168->164, other 929->943, no_display
 * 10176->9923, median time-to-first-display 5.75s->5.50s -- every one of the 4 metrics Daniel asked
 * to optimize (accuracy/harmonic-count/no-display-rate/speed-to-first-number) moved in the right
 * direction simultaneously, with "other" (+14, +1.5%) the only real cost. Both real recordings
 * diffed tick-by-tick against the pre-sweep build: recovery test unchanged entirely; offchest test
 * showed a benign 0.5bpm shift (52.2->52.7) on 2 already-correct ticks, no new wrong value, same
 * first-display timing on both files. See project memory (project_pcg_embedded_c) for the full
 * sweep methodology and numbers. */
#ifndef AGREEMENT_BPM_TOL_OVERRIDE
#define AGREEMENT_BPM_TOL_OVERRIDE 11.10f
#endif
#ifndef AGREE_BONUS_OVERRIDE
#define AGREE_BONUS_OVERRIDE 6.5f
#endif
static const float AGREEMENT_BPM_TOL = AGREEMENT_BPM_TOL_OVERRIDE;
static const float AGREE_BONUS = AGREE_BONUS_OVERRIDE;
/* TENT_* (override hooks added 2026-08-08) -- bpm_favor_center()'s piecewise-linear "tent" shape,
 * peaking at TENT_PEAK_BPM/TENT_PEAK_H and sloping down to TENT_LO_H at 40bpm / TENT_HI_H at
 * 200bpm, multiplied into every candidate's combine weight alongside AGREE_BONUS. No tuning
 * history in this file at all before this. SWEPT 2026-08-08 (same day as AGREE_BONUS/
 * AGREEMENT_BPM_TOL): 1-D sweeps on a 60-file sample showed small trends (TENT_PEAK_BPM~60.0,
 * TENT_HI_H~1.0 looked marginally better; TENT_PEAK_H/TENT_LO_H showed no signal at all) -- but
 * NONE of it survived a full 314-file corpus check (TENT_PEAK_BPM=60.0 actually scored
 * correct=15052/octave=165 vs the shipped 55.9's 15053/164, i.e. flat-to-slightly-worse). Classic
 * 60-file-sample-misleads result -- NOT PROMOTED, left at the original inherited values. Override
 * hooks kept for any future re-sweep. */
#ifndef TENT_PEAK_BPM_OVERRIDE
#define TENT_PEAK_BPM_OVERRIDE 55.9f
#endif
#ifndef TENT_PEAK_H_OVERRIDE
#define TENT_PEAK_H_OVERRIDE 2.29f
#endif
#ifndef TENT_LO_H_OVERRIDE
#define TENT_LO_H_OVERRIDE 0.56f
#endif
#ifndef TENT_HI_H_OVERRIDE
#define TENT_HI_H_OVERRIDE 0.81f
#endif
static const float TENT_PEAK_BPM = TENT_PEAK_BPM_OVERRIDE;
static const float TENT_PEAK_H = TENT_PEAK_H_OVERRIDE;
static const float TENT_LO_H = TENT_LO_H_OVERRIDE;
static const float TENT_HI_H = TENT_HI_H_OVERRIDE;
/* variant-4-only retune -- see ROBUST_TRIM_FRAC's comment above for the full sweep methodology. */
#if STREAM_VARIANT == 4
static const float TEAL_JUMP_RATIO = 1.2f;
static const int   TEAL_JUMP_CONFIRM_TICKS = 4;
#else
static const float TEAL_JUMP_RATIO = 1.4f;
static const int   TEAL_JUMP_CONFIRM_TICKS = 3;
#endif
/* RATIO-AWARE JUMP CONFIRMATION (2026-08-09), per Daniel's "any other ideas to detect locking on
 * to harmonics or subharmonics?" -- PROTOTYPED AND REJECTED, with a clear mechanistic explanation,
 * not just a bad number. A genuine physiological rate change essentially never lands precisely on
 * 2.0x, 0.5x, 3.0x, or 1/3x of the prior accepted rate -- those exact ratios are specifically the
 * octave/subharmonic-confusion signature, so the idea was to require TEAL_JUMP_HARMONIC_CONFIRM_
 * TICKS consecutive ticks (more than the base TEAL_JUMP_CONFIRM_TICKS) before accepting a jump
 * whose ratio sits within TEAL_JUMP_HARMONIC_TOL of one of those 4 values.
 * FATAL FLAW, discovered via full-corpus sweep then confirmed mechanistically: a jump ratio alone
 * cannot tell "escaping a wrong harmonic to reach the true fundamental" apart from "locking onto a
 * wrong harmonic away from the true fundamental" -- both look IDENTICAL from the ratio's own
 * perspective (the ratio between a fundamental and its harmonic is the same magnitude regardless
 * of which direction you're moving). Full 314-file corpus swept 8/12/16 extra ticks: octave_errors
 * got WORSE as scrutiny increased (114->128->133->135), not better -- the opposite of the goal.
 * Confirmed directly on daniel_recovery_test.f32's own well-documented 160bpm->54.5bpm self-
 * correction (ratio 160/54.5=2.94, close enough to 3.0 to classify as harmonic): at 8 extra ticks
 * the correction is delayed a full second (t=27.50s->28.50s); at 16 extra ticks the correction
 * NEVER HAPPENS within the recording at all, permanently converting a previously-correct self-
 * correction into a stuck-wrong answer. Extra scrutiny on ANY harmonic-ratio jump equally penalizes
 * the CORRECTIVE jumps this project has documented and relied on, not just the bad initial locks --
 * a symmetric penalty can't help with an asymmetric problem. Defaults to TEAL_JUMP_HARMONIC_CONFIRM_
 * TICKS == TEAL_JUMP_CONFIRM_TICKS (a true, algebraically-guaranteed no-op -- when the two confirm-
 * tick constants are equal, is_harmonic_jump's value cannot change teal_jump_required_ticks) so
 * this stays inert in the shipped default; no RAM/clock cost to gate behind an opt-in flag since
 * the classification itself is a handful of cheap float comparisons only run when is_jump is
 * already true. A future attempt would need some way to infer DIRECTION (which side is more
 * likely correct), not just ratio proximity to a harmonic -- untried, and not obviously solvable
 * with information already available at this point in the pipeline. */
#ifndef TEAL_JUMP_HARMONIC_TOL_OVERRIDE
#define TEAL_JUMP_HARMONIC_TOL_OVERRIDE 0.05f
#endif
#ifndef TEAL_JUMP_HARMONIC_CONFIRM_TICKS_OVERRIDE
#define TEAL_JUMP_HARMONIC_CONFIRM_TICKS_OVERRIDE TEAL_JUMP_CONFIRM_TICKS
#endif
static const float TEAL_JUMP_HARMONIC_TOL = TEAL_JUMP_HARMONIC_TOL_OVERRIDE;
static const int   TEAL_JUMP_HARMONIC_CONFIRM_TICKS = TEAL_JUMP_HARMONIC_CONFIRM_TICKS_OVERRIDE;
/* CURVE_GATE_THRESH/STREAK_LIMIT (2026-08-09) -- swept 0.0/0.2/0.35/0.5 x streak 1/2/4 on a 45-file
 * sample, REJECTED. **CORRECTION (2026-08-09, same day)**: the original sweep numbers backing this
 * rejection were measured with a real bug present -- win_teal.Sw was only populated under
 * COMBO13_EXPOSE_DEBUG, so this sweep (built WITHOUT that flag) read win_teal.Sw as UNINITIALIZED
 * STACK MEMORY, not the real curve. Re-verified after fixing that (out.Sw's memcpy is now
 * unconditional, see estimate_detrend_from_mw()'s own comment): with REAL data, STREAK_LIMIT=4 is
 * nearly a no-op (correct 16,014->16,013, other 930->917, essentially flat) rather than the
 * dramatic regression originally reported; STREAK_LIMIT=1 shows a real but modest tradeoff (correct
 * 16,014->15,881 [-0.8%], other 930->762 [-18%], no_display +301) -- fewer garbage answers, but
 * also fewer correct ones, not a clean win either way. The REJECTION VERDICT still stands (no
 * configuration tried is a clear win, consistent with the independent, always-valid
 * curvecorr_validate_ticks.json finding that even the strictest real curve_corr band is still
 * 63.3% correct -- a hard cutoff on this signal was never going to have great precision) but the
 * originally-reported magnitude of the "cost" was wrong, a real lesson in this project's own
 * history about the CONSEQUENCES of not initializing what you read, not just about the correctness
 * of C code as text. Left as `_OVERRIDE` hooks purely so CURVE_GATE_ENABLE builds can keep
 * experimenting, not because these particular values are trustworthy defaults. */
#ifndef CURVE_GATE_THRESH_OVERRIDE
#define CURVE_GATE_THRESH_OVERRIDE 0.35f
#endif
#ifndef CURVE_GATE_STREAK_LIMIT_OVERRIDE
#define CURVE_GATE_STREAK_LIMIT_OVERRIDE 1
#endif
#ifdef CURVE_GATE_ENABLE
static const float CURVE_GATE_THRESH = CURVE_GATE_THRESH_OVERRIDE;
static const int   CURVE_GATE_STREAK_LIMIT = CURVE_GATE_STREAK_LIMIT_OVERRIDE;
#endif
/* CURVE_WEIGHT_LO/HI (2026-08-09) -- the soft-multiplier version of the curve-correlation idea,
 * see teal_size_factor()'s own comment for the exact formula. Defaults to 1.0/1.0 (a true no-op,
 * multiplier always exactly 1.0 regardless of curve_corr) so the computation-and-wiring itself can
 * be verified as a no-op before sweeping any real LO/HI values via explicit -D overrides -- same
 * discipline as every other constant swept this session (verify no-op first, then sweep). */
#ifndef CURVE_WEIGHT_LO_OVERRIDE
#define CURVE_WEIGHT_LO_OVERRIDE 1.0f
#endif
#ifndef CURVE_WEIGHT_HI_OVERRIDE
#define CURVE_WEIGHT_HI_OVERRIDE 1.0f
#endif
#ifdef CURVE_WEIGHT_ENABLE
static const float CURVE_WEIGHT_LO = CURVE_WEIGHT_LO_OVERRIDE;
static const float CURVE_WEIGHT_HI = CURVE_WEIGHT_HI_OVERRIDE;
#endif
/* TEMPLATE_MATCH_ENABLE / TEMPLATE_WEIGHT_ENABLE (2026-08-09) -- per Daniel's "library of known
 * autocorrelation morphologies, give bonus for peaks that correlate closely with the morphology
 * expected at that BPM." Investigation (autocorr_morphology_*.py, see project memory) found a real
 * 3-landmark structure (fundamental, 0.5x subharmonic, ~2/3x S1/S2 sub-beat bump) consistent
 * 50-190bpm in the real+selfrecorded corpus, and that a full-curve Pearson correlation against a
 * per-bpm-bucket template beats a simple single-point companion-peak-height threshold in the one
 * zone both were validated against in Python (claimed bpm [40,70): template TP=100%/FP=4.00% @
 * th=0.30 vs. simple-threshold's best zone 91.3%/12.11%, see
 * autocorr_template_library_prototype.py's genuine train/holdout-file-split validation). Templates
 * baked from the FULL 314-file corpus's 'correct' raw-candidate ticks
 * (autocorr_build_final_templates.py + gen_template_header.py), NOT just the 60%-train split used
 * to validate the method -- normal practice once the approach itself is validated. ONLY valid for
 * STREAM_VARIANT=4's fixed lag grid (ac_lo=24, S_LEN=96) -- see autocorr_templates.h.
 *
 * TEMPLATE_MATCH_ENABLE (hard veto, TESTED AND REJECTED IN THIS FORM, 2026-08-09 same day): setting
 * raw_cand_valid=false below TEMPLATE_MATCH_THRESH. First version picked only the single nearest
 * 10bpm bucket -- full-corpus validation found a real bucket-boundary cliff (a candidate at
 * 65.75bpm, true rate 65.2bpm, scored ~0.05-0.15 against the 60bpm template but ~0.35-0.44 against
 * the 70bpm template one bucket over; confirmed via independent Python recompute from the same
 * saved templates, not a coding bug). Fixed by taking the max correlation across the nearest TWO
 * buckets (best_template_corr() below). Even after that fix, full 314-file corpus tally (DEFAULT
 * vs. TEMPLATE_MATCH_ENABLE, displayed-bpm classification) was a net regression: correct
 * 15396->15168 (-228), no_display 9776->10174 (+398), other 823->720 (-103), half 65->21 (-44),
 * third 23->0 (-23). Directionally correct (kills nearly all 'third' locks, most 'half' locks, some
 * 'other' garbage) but costs more correct ticks than it saves -- likely cascading state effects: a
 * hard per-tick veto doesn't just cost that tick, it can reset jump-confirmation/delay-tick streak
 * counters that LATER, otherwise-good ticks in the same file depend on to ever get confirmed, a
 * bigger blast radius than the offline per-tick correlation metric could predict. REJECTED as a hard
 * veto; kept gated off for reference, same as CURVE_GATE_ENABLE above.
 *
 * TEMPLATE_WEIGHT_ENABLE (soft multiplier, per Daniel's "soften to a weight, not a veto" after
 * seeing the above): applies template correlation as a soft factor in teal_size_factor() instead,
 * same mechanism CURVE_WEIGHT_ENABLE already uses for its own (rejected) curve-stability signal --
 * see curve_corr_log's struct comment for why that one didn't move the needle (AGREE_BONUS's 6.5x
 * swing dominates a modest multiplier on top). Worth testing independently since this is a
 * DIFFERENT signal (shape-vs-known-morphology, not tick-to-tick stability) -- not yet swept/
 * validated in C, defaults to LO=HI=1.0 (true no-op) exactly like CURVE_WEIGHT_ENABLE's own
 * discipline: verify no-op first, then sweep. */
#if (defined(TEMPLATE_MATCH_ENABLE) || defined(TEMPLATE_WEIGHT_ENABLE) || defined(TEMPLATE_MATCH_GATED_ENABLE) || defined(TEMPLATE_MATCH_GATED_V2_ENABLE)) && STREAM_VARIANT == 4
#include "autocorr_templates.h"
#endif
#ifndef TEMPLATE_MATCH_THRESH_OVERRIDE
#define TEMPLATE_MATCH_THRESH_OVERRIDE 0.30f
#endif
#ifndef TEMPLATE_MATCH_MIN_BPM_OVERRIDE
#define TEMPLATE_MATCH_MIN_BPM_OVERRIDE 40.0f
#endif
#ifndef TEMPLATE_MATCH_MAX_BPM_OVERRIDE
#define TEMPLATE_MATCH_MAX_BPM_OVERRIDE 90.0f
#endif
/* PROMOTED 0.1/2.0 (2026-08-09, same day) -- full 314-file corpus sweep (default 15396 correct/823
 * other/23 third/9776 no_display): LO=0.3/HI=1.5 -> correct+6/other-2; LO=0.1/HI=1.0 (downweight-
 * only) -> correct+9/other-5; LO=0.1/HI=2.0 -> correct+23/other-19; LO=0.02/HI=3.0 -> correct+28/
 * other-24 (diminishing returns setting in, and HI=3.0 starts to approach AGREE_BONUS's own 6.5x
 * scale, untested territory). third dropped 23->19 in EVERY config tried, apparently a structural
 * ceiling independent of LO/HI. Zero no_display cost at every config tried -- unlike
 * TEMPLATE_MATCH_ENABLE's hard veto, the soft weight never removes a candidate from jump-
 * confirmation/delay-tick state-tracking, avoiding that mechanism's cascading-regression failure
 * mode entirely. 0.1/2.0 chosen as a good gain without pushing into the more extreme, less-tested
 * range. Both real recordings reverified: daniel_offchest_test.f32 byte-identical; daniel_
 * recovery_test.f32 one tick shifted 54.5->53.9bpm (both correct, no display gained/lost). */
#ifndef TEMPLATE_WEIGHT_LO_OVERRIDE
#define TEMPLATE_WEIGHT_LO_OVERRIDE 0.1f
#endif
#ifndef TEMPLATE_WEIGHT_HI_OVERRIDE
#define TEMPLATE_WEIGHT_HI_OVERRIDE 2.0f
#endif
#if defined(TEMPLATE_MATCH_ENABLE) || defined(TEMPLATE_WEIGHT_ENABLE)
static const float TEMPLATE_MATCH_MIN_BPM = TEMPLATE_MATCH_MIN_BPM_OVERRIDE;
static const float TEMPLATE_MATCH_MAX_BPM = TEMPLATE_MATCH_MAX_BPM_OVERRIDE;
#endif
#ifdef TEMPLATE_MATCH_ENABLE
static const float TEMPLATE_MATCH_THRESH = TEMPLATE_MATCH_THRESH_OVERRIDE;
#endif
#ifdef TEMPLATE_WEIGHT_ENABLE
static const float TEMPLATE_WEIGHT_LO = TEMPLATE_WEIGHT_LO_OVERRIDE;
static const float TEMPLATE_WEIGHT_HI = TEMPLATE_WEIGHT_HI_OVERRIDE;
#endif
#if defined(TEMPLATE_MATCH_ENABLE) || defined(TEMPLATE_WEIGHT_ENABLE)
/* Pearson correlation between a candidate's own raw autocorrelation curve and a specific bpm
 * template -- same manual mean/covariance/variance-loop pattern as curve_gate_corr/curve_weight_corr
 * above, generalized into a helper since this one is called against a table lookup rather than a
 * single snapshot-over-time comparison. */
/* tmpl is int8-QUANTIZED (2026-08-10, TEMPLATE_MATCH_QUANT_SCALE in autocorr_templates.h) -- dequant
 * happens inline per-element, same loop that was already touching every element anyway, so this is
 * free (one widen+multiply, no extra pass over the array). Pearson correlation is scale/shift-
 * invariant, so int8's ~0.8%-of-full-scale quantization step changes the correlation result by
 * noise far below the real signal's own tick-to-tick variation -- see project memory for the
 * full-corpus validation this was checked against before shipping. */
static float template_match_corr(const float *curve, const int8_t *tmpl, int n) {
    const float dq = 1.0f / TEMPLATE_MATCH_QUANT_SCALE;
    float mean_a = 0.0f, mean_b = 0.0f;
    for (int i = 0; i < n; i++) { mean_a += curve[i]; mean_b += (float)tmpl[i] * dq; }
    mean_a /= (float)n; mean_b /= (float)n;
    float cov = 0.0f, var_a = 0.0f, var_b = 0.0f;
    for (int i = 0; i < n; i++) {
        float da = curve[i] - mean_a, db = (float)tmpl[i] * dq - mean_b;
        cov += da * db; var_a += da * da; var_b += db * db;
    }
    float denom = sqrtf(var_a * var_b);
    return (denom > 1e-9f) ? (cov / denom) : 0.0f;
}
#endif
#if (defined(TEMPLATE_MATCH_ENABLE) || defined(TEMPLATE_WEIGHT_ENABLE)) && STREAM_VARIANT == 4
/* Nearest-TWO-buckets, max correlation -- see the TEMPLATE_MATCH_ENABLE comment above for why
 * (bucket-boundary cliff found via full-corpus validation). Shared by both the (rejected) hard-veto
 * and the soft-weight mechanisms below. Only valid for STREAM_VARIANT=4's fixed lag grid, same as
 * the baked template arrays themselves (autocorr_templates.h). */
static float best_template_corr(const float *curve, float bpm, int *out_bucket_bpm) {
    int best_i = -1, second_i = -1;
    float best_d = 1e30f, second_d = 1e30f;
    for (int i = 0; i < TEMPLATE_MATCH_N_BUCKETS; i++) {
        float d = fabsf(bpm - kTemplateBpm[i]);
        if (d < best_d) { second_d = best_d; second_i = best_i; best_d = d; best_i = i; }
        else if (d < second_d) { second_d = d; second_i = i; }
    }
    float corr = template_match_corr(curve, kTemplateCurve[best_i], S_LEN);
    int used = best_i;
    if (second_i >= 0) {
        float corr2 = template_match_corr(curve, kTemplateCurve[second_i], S_LEN);
        if (corr2 > corr) { corr = corr2; used = second_i; }
    }
    if (out_bucket_bpm) *out_bucket_bpm = (int)kTemplateBpm[used];
    return corr;
}
#endif
/* TEMPLATE_MATCH_GATED_ENABLE (2026-08-10) -- ported from the pcg_revisualizer JS exploration tool
 * after Daniel's full tick-by-tick corpus validation there. Architecturally DIFFERENT from
 * TEMPLATE_MATCH_ENABLE/TEMPLATE_WEIGHT_ENABLE above (both of which only ever look at the SINGLE
 * candidate win_teal.bpm already picked) -- this instead looks at the top-3 tallest local maxima
 * of win_teal.Sw itself (same peak-gathering convention as estimate_detrend_from_mw()'s own
 * cand[]/ncand), scores EACH against its own nearest-bpm-bucket templates, and only overrides the
 * default (tallest-peak) choice if a smaller peak's score clearly beats it.
 * Motivation (Daniel's "weight certain regions more heavily... the harmonic regions and the S1-S2
 * period" then "how could we optimize this against our corpus?"): a plain whole-curve correlation
 * dilutes a missing expected bump (e.g. the S1-S2 ~0.667x region) across the WHOLE curve's variance
 * -- region-weighting (Gaussian bumps in RATIO space at 0.5/2/(1/3)/3/0.667x, relative to the
 * candidate's OWN lag) and correlating BOTH win_r.Sw (raw) against RAW templates AND win_teal.Sw
 * (detrended, isolates bump/dip structure) against DETRENDED templates -- averaged -- beat every
 * other candidate tried in a genuine train/holdout-file-split corpus comparison
 * (embedded_c/optimize_template_match.py): best on the dominant 'half' harmonic direction (Youden
 * J=0.819) and strong on 'double' (J=0.554, roughly 3.5x the unweighted status quo's J=0.15).
 * GATED OVERRIDE (per Daniel's "yes, try that" after a full tick-by-tick scatter, 21065 real ticks,
 * showed the UNCONDITIONAL "highest score wins" rule scored WORSE than trusting the tallest peak
 * every time: 78.4% vs 86.0% correct -- concentrated in the 40-100bpm range, where 2x/3x harmonic
 * lag positions still fit inside the fixed grid and gave the picker wrong-but-plausible
 * alternatives; above ~100bpm no such trap exists, which is exactly why unconditional picking was
 * already a clean win there). embedded_c/revisualizer_margin_sweep_analyze.py swept the override
 * margin from 0 (unconditional) to 1.0 (never override, = pure tallest-peak baseline) against a
 * cached extraction of all 3 peaks' scores per tick: **margin=0.3 PROMOTED** -- 87.0% correct,
 * 0.7% double+triple, Pareto-dominant over BOTH extremes (beats unconditional's 78.4% AND
 * never-override's 86.0%), not merely a compromise between them.
 * NOTE: these are TICK-LEVEL raw-candidate numbers (before the weighted-median combine/jump-
 * confirmation pipeline), not directly comparable to this project's own DISPLAYED-bpm corpus
 * tallies elsewhere -- see the full-corpus displayed-bpm validation this mechanism's own promotion
 * comment (search this file for the revision this was promoted in) for the fair comparison.
 * Needs BOTH win_r.Sw and win_teal.Sw simultaneously (already computed, unconditionally, every
 * tick) -- this is why the mechanism lives here rather than folding into TEMPLATE_WEIGHT_ENABLE's
 * own single-candidate soft-multiplier integration point. */
#ifndef TEMPLATE_MATCH_OVERRIDE_MARGIN_OVERRIDE
#define TEMPLATE_MATCH_OVERRIDE_MARGIN_OVERRIDE 0.3f
#endif
#if (defined(TEMPLATE_MATCH_GATED_ENABLE) || defined(TEMPLATE_MATCH_GATED_V2_ENABLE)) && STREAM_VARIANT == 4
#include "autocorr_templates_detrended.h"
static const float TEMPLATE_MATCH_OVERRIDE_MARGIN = TEMPLATE_MATCH_OVERRIDE_MARGIN_OVERRIDE;

typedef struct { float ratio, sigma, boost; } RegionWeight;
static const RegionWeight kRegionWeights[5] = {
    {0.5f, 0.05f, 3.0f},          /* octave harmonic: candidate too fast */
    {2.0f, 0.08f, 3.0f},          /* octave harmonic: candidate too slow */
    {1.0f / 3.0f, 0.03f, 2.0f},   /* 3rd-harmonic companion, weaker evidence */
    {3.0f, 0.12f, 2.0f},          /* 3rd-harmonic companion, weaker evidence */
    {0.667f, 0.08f, 2.5f},        /* S1-S2 sub-beat bump, autocorr_morphology_by_decade.py */
};
/* RAM/CLOCK PASS (2026-08-10), per Daniel's "everything is tight on my MCU... reduce clock cycles" --
 * unmodified build_region_weights() called expf() up to 5*S_LEN=480 times PER INVOCATION, and this
 * runs once per candidate peak (up to TEMPLATE_MATCH_GATED_V2_N_PEAKS=6/tick) -- up to 2,880 expf()
 * calls/tick, each a soft-float transcendental-function library call on a Cortex-M4F (no hardware
 * exp). All 5 bumps share the exact same functional FORM (a standard unit Gaussian exp(-0.5*d*d));
 * only their target ratio/sigma/boost differ. That means one small shared LUT over the Gaussian's
 * OWN standardized input d (not over ratio) covers every bump, at every sigma, exactly -- a step in
 * d of DSTEP is a step of DSTEP*sigma in ratio space, so the table's effective ratio-space resolution
 * automatically scales down for the narrowest bump (sigma=0.03) instead of needing a separate table
 * per bump. Table is symmetric (exp(-0.5*d*d) is even in d) so only d in [0,DMAX] is stored; beyond
 * DMAX=4 the true value is <=3.4e-4 of the peak, indistinguishable from 0 at this algorithm's working
 * precision, so it's just clamped to 0 rather than stored. Generated via:
 *   python -c "import numpy as np; d=np.linspace(0,4.0,201); print(np.exp(-0.5*d*d))"
 * Linear interpolation between adjacent entries keeps the lookup smooth (no zeroth-order-hold steps
 * that would show up as spurious steps in the resulting template-match score). */
#define GAUSS_LUT_N 201
#define GAUSS_LUT_DMAX 4.0f
static const float kGaussLUT[GAUSS_LUT_N] = {
    1.00000000f, 0.99980002f, 0.99920032f, 0.99820162f, 0.99680511f, 0.99501248f, 0.99282586f, 0.99024786f, 0.98728157f, 0.98393051f, 0.98019867f, 0.97609047f, 0.97161077f, 0.96676484f, 0.96155838f, 0.95599748f, 0.95008863f, 0.94383870f, 0.93725490f, 0.93034481f, 0.92311635f, 0.91557774f, 0.90773754f, 0.89960455f, 0.89118789f, 0.88249690f, 0.87354119f, 0.86433055f, 0.85487502f, 0.84518478f, 0.83527021f, 0.82514182f, 0.81481026f, 0.80428628f, 0.79358073f, 0.78270454f, 0.77166867f, 0.76048416f, 0.74916202f, 0.73771331f, 0.72614904f, 0.71448020f, 0.70271772f, 0.69087249f, 0.67895529f, 0.66697681f, 0.65494763f, 0.64287820f, 0.63077882f, 0.61865965f, 0.60653066f, 0.59440166f, 0.58228224f, 0.57018181f, 0.55810956f, 0.54607443f, 0.53408515f, 0.52215020f, 0.51027780f, 0.49847592f, 0.48675226f, 0.47511424f, 0.46356902f, 0.45212346f, 0.44078414f, 0.42955736f, 0.41844911f, 0.40746510f, 0.39661073f, 0.38589113f, 0.37531110f, 0.36487516f, 0.35458755f, 0.34445218f, 0.33447271f, 0.32465247f, 0.31499453f, 0.30550168f, 0.29617642f, 0.28702097f, 0.27803730f, 0.26922710f, 0.26059182f, 0.25213264f, 0.24385049f, 0.23574608f, 0.22781987f, 0.22007211f, 0.21250282f, 0.20511182f, 0.19789870f, 0.19086288f, 0.18400359f, 0.17731987f, 0.17081059f, 0.16447446f, 0.15831002f, 0.15231569f, 0.14648972f, 0.14083025f, 0.13533528f, 0.13000271f, 0.12483031f, 0.11981577f, 0.11495667f, 0.11025053f, 0.10569476f, 0.10128672f, 0.09702370f, 0.09290294f, 0.08892162f, 0.08507688f, 0.08136582f, 0.07778552f, 0.07433302f, 0.07100535f, 0.06779953f, 0.06471256f, 0.06174144f, 0.05888317f, 0.05613476f, 0.05349324f, 0.05095563f, 0.04851899f, 0.04618039f, 0.04393693f, 0.04178575f, 0.03972399f, 0.03774886f, 0.03585759f, 0.03404745f, 0.03231577f, 0.03065989f, 0.02907723f, 0.02756523f, 0.02612141f, 0.02474331f, 0.02342855f, 0.02217477f, 0.02097970f, 0.01984109f, 0.01875678f, 0.01772463f, 0.01674258f, 0.01580862f, 0.01492079f, 0.01407718f, 0.01327597f, 0.01251534f, 0.01179358f, 0.01110900f, 0.01045997f, 0.00984492f, 0.00926233f, 0.00871073f, 0.00818870f, 0.00769488f, 0.00722795f, 0.00678664f, 0.00636972f, 0.00597602f, 0.00560442f, 0.00525382f, 0.00492318f, 0.00461151f, 0.00431784f, 0.00404125f, 0.00378087f, 0.00353586f, 0.00330539f, 0.00308872f, 0.00288509f, 0.00269381f, 0.00251420f, 0.00234563f, 0.00218749f, 0.00203920f, 0.00190019f, 0.00176996f, 0.00164799f, 0.00153381f, 0.00142697f, 0.00132705f, 0.00123362f, 0.00114632f, 0.00106477f, 0.00098862f, 0.00091755f, 0.00085125f, 0.00078943f, 0.00073180f, 0.00067811f, 0.00062811f, 0.00058156f, 0.00053824f, 0.00049796f, 0.00046050f, 0.00042569f, 0.00039335f, 0.00036333f, 0.00033546f
};
static float fast_unit_gauss(float d) {
    float ad = fabsf(d);
    if (ad >= GAUSS_LUT_DMAX) return 0.0f;
    float pos = ad * ((float)(GAUSS_LUT_N - 1) / GAUSS_LUT_DMAX);
    int idx = (int)pos;
    if (idx >= GAUSS_LUT_N - 1) return kGaussLUT[GAUSS_LUT_N - 1];
    float frac = pos - (float)idx;
    return kGaussLUT[idx] + frac * (kGaussLUT[idx + 1] - kGaussLUT[idx]);
}
static void build_region_weights(float bpm, float *w) {
    float peak_lag = 60.0f * FFS / bpm;
    for (int i = 0; i < S_LEN; i++) {
        int lag = LO_LAG + i;
        float ratio = peak_lag / (float)lag;
        float wv = 1.0f;
        for (int k = 0; k < 5; k++) {
            float d = (ratio - kRegionWeights[k].ratio) / kRegionWeights[k].sigma;
            wv += kRegionWeights[k].boost * fast_unit_gauss(d);
        }
        w[i] = wv;
    }
}
/* Weighted-Pearson generalization of template_match_corr() above -- degenerates to it when every
 * weight is equal. tmpl is int8-quantized, same as template_match_corr() -- see that function's own
 * comment for the dequant rationale. */
static float weighted_template_match_corr(const float *curve, const int8_t *tmpl, const float *w, int n) {
    const float dq = 1.0f / TEMPLATE_MATCH_QUANT_SCALE;
    float sw = 0.0f, mean_a = 0.0f, mean_b = 0.0f;
    for (int i = 0; i < n; i++) { sw += w[i]; mean_a += w[i] * curve[i]; mean_b += w[i] * ((float)tmpl[i] * dq); }
    if (sw < 1e-9f) return 0.0f;
    mean_a /= sw; mean_b /= sw;
    float cov = 0.0f, var_a = 0.0f, var_b = 0.0f;
    for (int i = 0; i < n; i++) {
        float da = curve[i] - mean_a, db = (float)tmpl[i] * dq - mean_b;
        cov += w[i] * da * db; var_a += w[i] * da * da; var_b += w[i] * db * db;
    }
    float denom = sqrtf(var_a * var_b);
    return (denom > 1e-9f) ? (cov / denom) : 0.0f;
}
/* Nearest-TWO-buckets, max WEIGHTED correlation -- same bucket-boundary-cliff fix as
 * best_template_corr() above, generalized over either template table (RAW or DETRENDED, both
 * share the same kTemplateBpm bucket list/ordering). */
static float best_weighted_template_corr(const float *curve, const int8_t templates[][TEMPLATE_MATCH_N_CURVE],
                                           float bpm, const float *w) {
    int best_i = -1, second_i = -1;
    float best_d = 1e30f, second_d = 1e30f;
    for (int i = 0; i < TEMPLATE_MATCH_N_BUCKETS; i++) {
        float d = fabsf(bpm - kTemplateBpm[i]);
        if (d < best_d) { second_d = best_d; second_i = best_i; best_d = d; best_i = i; }
        else if (d < second_d) { second_d = d; second_i = i; }
    }
    float corr = weighted_template_match_corr(curve, templates[best_i], w, S_LEN);
    if (second_i >= 0) {
        float corr2 = weighted_template_match_corr(curve, templates[second_i], w, S_LEN);
        if (corr2 > corr) corr = corr2;
    }
    return corr;
}
/* Region-weighted RAW-vs-RAW score AVERAGED with region-weighted DETRENDED-vs-DETRENDED score --
 * the promoted combined_mean from optimize_template_match.py. */
static float combined_template_score(const float *raw_curve, const float *det_curve, float bpm) {
    float weights[S_LEN];
    build_region_weights(bpm, weights);
    float raw_score = best_weighted_template_corr(raw_curve, kTemplateCurve, bpm, weights);
    float det_score = best_weighted_template_corr(det_curve, kTemplateCurveDetrended, bpm, weights);
    return 0.5f * (raw_score + det_score);
}
/* Top-3 tallest local maxima of a detrended S_LEN-length curve -- SAME peak-gathering convention
 * as estimate_detrend_from_mw()'s own cand[]/ncand (curve[i]>=curve[i-1] && curve[i]>curve[i+1]),
 * sorted tallest-first (bins[0] is exactly the bin win_teal.bpm itself was already picked from).
 * Returns the count found (0-3). */
static int find_top3_peaks(const float *curve, int *out_bins) {
    int cand[S_LEN]; int ncand = 0;
    for (int i = 1; i < S_LEN - 1; i++)
        if (curve[i] >= curve[i - 1] && curve[i] > curve[i + 1]) cand[ncand++] = i;
    for (int i = 0; i < ncand; i++) {
        int best = i;
        for (int j = i + 1; j < ncand; j++) if (curve[cand[j]] > curve[cand[best]]) best = j;
        int tmp = cand[i]; cand[i] = cand[best]; cand[best] = tmp;
    }
    int n = ncand < 3 ? ncand : 3;
    for (int i = 0; i < n; i++) out_bins[i] = cand[i];
    return n;
}
#endif
/* FIRST-READING ACCURACY SWEEP (2026-08-09), per Daniel's "optimization sweep... for the accuracy
 * of the first displayed reading... might come at the expense of more missing displays or other
 * regressions." Override hooks added to TEAL_BPM_DELAY_SLOPE/RAMP_DELAY_TAPER_S/RAMP_DELAY_EXTRA_
 * TICKS -- required_teal_delay_ticks()'s own 3 constants, which (unlike EARLY_RELEASE_MIN_TICKS/
 * MAX_SAVE_S just above) have never been independently corpus-swept since this file's creation;
 * the EARLY_RELEASE comment above explicitly scoped its own 2026-08-08 re-sweep to NOT touch these
 * ("this does NOT touch CONS_WAIT_S itself or the post-wait confirmation ramp... both left exactly
 * as validated"). See first_reading_sweep.py for the sweep itself and its full-corpus numbers. */
#ifndef TEAL_BPM_DELAY_SLOPE_OVERRIDE
#define TEAL_BPM_DELAY_SLOPE_OVERRIDE 0.002388504108629796f
#endif
static const float TEAL_BPM_DELAY_SLOPE = TEAL_BPM_DELAY_SLOPE_OVERRIDE;
/* _FIRST variant (2026-08-09) -- same !st->ever_displayed-only scoping as the RAMP_DELAY_*_FIRST
 * constants above (see EARLY_RELEASE_MIN_TICKS_FIRST's comment for the full rationale). Investigated
 * per Daniel's "how much would it take to actually delay/prevent daniel_recovery_test.f32's own
 * 160bpm bad first reading, and what would it cost."
 * PROMOTED 0.00239->0.03 (2026-08-09, same day as revision 0.9 itself -- source-tree sync only, no
 * revision bump, per this project's own established practice for a same-day refinement of a round
 * already shipped that day). Threshold effect found empirically: the fix engages somewhere between
 * 0.020 (still shows the wrong 160bpm) and 0.021 (correctly shows ~53.9bpm) on
 * daniel_recovery_test.f32's own documented bad first reading -- 0.03 gives comfortable margin
 * above that edge. Full 314-file corpus on top of the already-shipped revision-0.9 baseline:
 * first-reading correct 293/314->294/314, correct(all ticks) 15495->15396 (-99), no_display
 * 9663->9776 (+113) -- 0.05 reaches 295/314 for roughly double the cost, 0.10 reaches the SAME
 * 295/314 as 0.05 for much MORE cost (dominated, not worth it) -- 0.03 chosen as the cheapest step
 * that still fixes the specific, previously-flagged real-recording case. Both real recordings
 * reverified: daniel_recovery_test.f32's first reading is now 53.9bpm (correct, was 160bpm),
 * delayed only 2.25s (25.50s->27.75s); daniel_offchest_test.f32 unaffected in value (its true rate
 * is near 60bpm, this mechanism only adds delay above that) aside from a similarly small shift.
 * Neither file's final/tail value changed. */
#ifndef TEAL_BPM_DELAY_SLOPE_FIRST_OVERRIDE
#define TEAL_BPM_DELAY_SLOPE_FIRST_OVERRIDE 0.03f
#endif
static const float TEAL_BPM_DELAY_SLOPE_FIRST = TEAL_BPM_DELAY_SLOPE_FIRST_OVERRIDE;
/* Extra required agreement ticks tapering off over the RAMP_DELAY_TAPER_S seconds right after the
 * frozen wait lifts -- combo13_opt doesn't need this (exact whole-window recompute every tick),
 * but corpus data localized combo13_stream's extra garbage-lock regression to roughly this window
 * (5-20s post-onset), where the streaming baseline/decimation state is still settling. */
#ifndef RAMP_DELAY_TAPER_S_OVERRIDE
#define RAMP_DELAY_TAPER_S_OVERRIDE 15.0f
#endif
#ifndef RAMP_DELAY_EXTRA_TICKS_OVERRIDE
#define RAMP_DELAY_EXTRA_TICKS_OVERRIDE 4.0f
#endif
static const float RAMP_DELAY_TAPER_S = RAMP_DELAY_TAPER_S_OVERRIDE;
static const float RAMP_DELAY_EXTRA_TICKS = RAMP_DELAY_EXTRA_TICKS_OVERRIDE;
/* _FIRST variants -- applied only while !st->ever_displayed (this session's true first-ever
 * reading), see EARLY_RELEASE_MIN_TICKS_FIRST's own comment above for the full rationale. Defaults
 * equal the plain values (no-op until deliberately set stricter).
 * RAMP_DELAY_EXTRA_TICKS_FIRST PROMOTED 4.0->6.0 (2026-08-09, revision 0.9), per Daniel's "push
 * 293/314 (moderate) to the visualizer" -- the "moderate" tier of the first-reading-accuracy sweep
 * (see first_reading_sweep.py). Applying this ONLY to the true first-ever reading (not every later
 * flatline/spike reconnect within the same session) full-corpus: first-reading correct 287/314->
 * 293/314 (91.4%->93.3%), at correct(all ticks) 16082->15495 (-587), octave_errors 114->88 (-26,
 * genuinely better), no_display 8954->9663 (+709). Strictly better than changing the plain
 * (non-adaptive) RAMP_DELAY_EXTRA_TICKS globally to the same value 4.0->6.0, which reached the
 * SAME 293/314 but cost more (correct 15425, no_display 9767) -- confirmed the adaptive scoping
 * recovers real ticks (+70 correct/-104 no_display here) for free by leaving every later
 * reconnect's confirmation speed untouched. Both real recordings diffed: identical first-display
 * and final tail value to the pre-promotion build (including daniel_recovery_test.f32's own
 * documented 160->54.5bpm self-correction, unaffected). A real, deliberate value tradeoff (not a
 * clean win) -- Daniel's explicit call after reviewing the full Pareto frontier (289/314 cheaper,
 * 296/314 available at a much steeper cost via this same constant pushed to 16). */
#ifndef RAMP_DELAY_TAPER_S_FIRST_OVERRIDE
#define RAMP_DELAY_TAPER_S_FIRST_OVERRIDE RAMP_DELAY_TAPER_S_OVERRIDE
#endif
#ifndef RAMP_DELAY_EXTRA_TICKS_FIRST_OVERRIDE
#define RAMP_DELAY_EXTRA_TICKS_FIRST_OVERRIDE 6.0f
#endif
static const float RAMP_DELAY_TAPER_S_FIRST = RAMP_DELAY_TAPER_S_FIRST_OVERRIDE;
static const float RAMP_DELAY_EXTRA_TICKS_FIRST = RAMP_DELAY_EXTRA_TICKS_FIRST_OVERRIDE;

/* peak-mean flatline detector -- was a straight design/scale-rescale port of combo13_opt's
 * 2026-08-04 fix: JS's 0.003 threshold operates on pcgRing's normalized [-1,1] scale, rescaled here
 * by *32768, but never independently corpus-tuned for THIS streaming design. SAFETY-SENSITIVE:
 * gates whether the device shows a number or "--", so promotion here is gated primarily on the 2
 * real disconnect/reconnect recordings' actual detection timing, not just corpus no_display count
 * (a looser threshold can look like a corpus win by suppressing legitimate "--" while actually
 * failing to catch real device-offs, which the classify()-based corpus score can't detect).
 * SWEPT 2026-08-08: PEAK_MEAN_DIFF_THRESH and PEAK_MEAN_TOP_PCT turned out to be genuinely
 * miscalibrated for this design -- lowering either alone improved the full 314-file corpus
 * substantially (correct +477 to +1054, no_display -480 to -1103, octave_errors completely FLAT)
 * with literally ZERO change to either real recording's actual disconnect-detection timing across
 * the whole safe range tested (true silence crosses even a much stricter threshold easily, so a
 * tighter bar doesn't delay catching a REAL device-off). BUT found a real, narrow INTERACTION
 * effect when combining an aggressive threshold (0.001) with a lower TOP_PCT (0.05): together they
 * delayed daniel_recovery_test.f32's self-correction out of its pre-existing documented spurious
 * 160bpm lock by 5 extra ticks (1.5s) -- neither change alone caused this, only that specific
 * combination did (likely via a small shift in flatline_clear_el cascading into the post-event
 * confirmation ramp timing). PROMOTED THRESH 0.003->0.002 + TOP_PCT 0.10->0.05 together (not the
 * more aggressive 0.001 pairing) after confirming this exact combination produces BYTE-IDENTICAL
 * output to the pre-sweep build on both real recordings, while still capturing most of the corpus
 * win (correct +918, no_display -950). PEAK_MEAN_SUSTAIN_S was ALSO swept and showed a genuine,
 * uncapped tradeoff (higher = better corpus numbers but proportionally slower real device-off
 * detection, e.g. 1.5s -> +0.5s detection delay both real files) -- presented to Daniel directly
 * since this is a real value judgment, not a clean win; his call was to leave it at the original
 * 1.0s, prioritizing device-off responsiveness. See project memory (project_pcg_embedded_c) for
 * full sweep numbers. */
#ifndef PEAK_MEAN_TOP_PCT_OVERRIDE
#define PEAK_MEAN_TOP_PCT_OVERRIDE 0.05f
#endif
#ifndef PEAK_MEAN_DIFF_THRESH_OVERRIDE
#define PEAK_MEAN_DIFF_THRESH_OVERRIDE (0.002f * 32768.0f)
#endif
#ifndef PEAK_MEAN_SUSTAIN_S_OVERRIDE
#define PEAK_MEAN_SUSTAIN_S_OVERRIDE 1.0f
#endif
static const float PEAK_MEAN_TOP_PCT = PEAK_MEAN_TOP_PCT_OVERRIDE;
static const float PEAK_MEAN_DIFF_THRESH = PEAK_MEAN_DIFF_THRESH_OVERRIDE;
static const float PEAK_MEAN_SUSTAIN_S = PEAK_MEAN_SUSTAIN_S_OVERRIDE;
/* minimum is_flatline duration to count as a genuine disconnect/reconnect (vs. a brief few-second
 * physiological dip) for purposes of the flatline-clear-event fix below */
static const float FLATLINE_FULL_RESET_MIN_S = 1.5f;

/* REDUNDANCY RE-TESTED (2026-08-10), per Daniel's "everything is tight on my MCU... reduce clock
 * cycles, RAM and flash" -- with BOTH TEMPLATE_WEIGHT_ENABLE and TEMPLATE_MATCH_GATED_V2_ENABLE now
 * shipped (neither existed when this veto's own leave-one-out ablation originally found it was the
 * single biggest lever), worth re-checking whether it still earns its keep: costs scratch_Sext
 * (~984B, ~8.8% of STATE_SIZE_BYTES) plus its own separate extended-range autocorrelation pass.
 * Full 314-file corpus, disabled vs. current v0.11 (both template mechanisms + all three RAM/clock
 * optimizations above): 33/314 files changed. correct -13 (15565->15552), but the real story is
 * no_display -318 (9708->9390) traded almost 1:1 against other +326 (810->1136) -- i.e. disabling it
 * mostly converts blank-screen ticks into WRONG-NUMBER-DISPLAYED ticks, not into correct ones. NOT
 * redundant -- keep it. For a medical display, a blank reading is a materially better failure mode
 * than a confidently wrong one, so this is worth its RAM/clock cost even though correct-rate alone
 * looks like a wash. (Test harness: combo13_stream_noveto.c, a throwaway copy with this flag flipped
 * to false, deleted after this comparison -- not a shippable option, just flip this line back to
 * false and rebuild if this tradeoff ever needs revisiting.) */
static const bool  USE_HARMONIC_VETO = true;
static const float HARMONIC_VETO_HI_EXT_MULT = 2.1813774614710146f;
static const float HARMONIC_VETO_HEIGHT_FRAC = 0.05895565949526575f;
/* variant-4-only retune, a middle-ground narrowing of the harmonic-veto window -- see
 * ROBUST_TRIM_FRAC's comment above for the full sweep methodology (this specific window width is
 * the single biggest lever found: too narrow, 1.95/2.05, recovered good octave/other numbers but
 * cost too much correct-count at full-corpus scale; 1.93/2.07 keeps most of the benefit at a much
 * smaller correct-count cost). */
#if STREAM_VARIANT == 4
static const float HARMONIC_VETO_LO_FRAC = 1.93f;
static const float HARMONIC_VETO_HI_FRAC = 2.07f;
#else
static const float HARMONIC_VETO_LO_FRAC = 1.9239938526174543f;
static const float HARMONIC_VETO_HI_FRAC = 2.081876428164688f;
#endif

#define DECAY_ADAPTIVE_TAU_MIN 1.0f
#define DECAY_ADAPTIVE_TAU_MAX 8.0f
static const float NEG_INF = -1e30f;

/* -- ARTIFACT_PAD_SAMPLES was tuned at NATIVE rate (36 samples @ 4000Hz = 9ms). The padding's
 * job (widen the excised region so interpolation doesn't leave a residual click right at the
 * boundary) is a TIME-domain concept, so it's rescaled to preserve ~9ms at whatever rate
 * detection actually runs at, rather than reusing "36 samples" unchanged at a different rate
 * (which would mean a wildly different padding DURATION). Reasoned, not yet corpus-retuned. */
#if DETECT_AT_NATIVE
static const int ARTIFACT_PAD_SAMPLES_V = 36;                 /* native rate: unchanged */
#elif DEC_STRIDE == 8
static const int ARTIFACT_PAD_SAMPLES_V = 5;   /* round(36 * 500/4000) */
#elif DEC_STRIDE == 6
static const int ARTIFACT_PAD_SAMPLES_V = 6;   /* round(36 * 666.667/4000) */
#elif DEC_STRIDE == 10
static const int ARTIFACT_PAD_SAMPLES_V = 4;   /* round(36 * 400/4000) */
#endif

/* -- streaming baseline EMA decay: chosen so the effective averaging window approximates a few
 * seconds (roughly half of the original 8s whole-window design), independent of chunk rate --
 * alpha = exp(-chunk_duration_s / tau), tau=4s. Reasoned starting point, not corpus-tuned. */
#define BASELINE_TAU_S 1.0f

/* ============================================================================================
 * FIXED FILTER COEFFICIENTS
 * ============================================================================================ */
#if DEC_STRIDE == 8
/* 4000Hz -> 500Hz, cutoff 220Hz (0.88 * new Nyquist 250Hz) -- unchanged from combo13_opt. */
static const float AA_B[5] = {0.0005891316955801649f, 0.0023565267823206595f, 0.0035347901734809895f,
                               0.0023565267823206595f, 0.0005891316955801649f};
static const float AA_A[5] = {1.0f, -3.09905041635851f, 3.6812793328016165f, -1.9759888358976645f,
                               0.40318602658384056f};
static const float BAND_B[3][5] = {
    {0.013359200027856502f, 0, -0.026718400055713003f, 0, 0.013359200027856502f},
    {0.02785976611713605f, 0, -0.0557195322342721f, 0, 0.02785976611713605f},
    {0.06745527388907191f, 0, -0.13491054777814382f, 0, 0.06745527388907191f},
};
static const float BAND_A[3][5] = {
    {1.0f, -3.2216975944168245f, 4.259742833119654f, -2.6933188730452877f, 0.700896781188403f},
    {1.0f, -2.4220309055301232f, 2.962756451291472f, -1.8462867837112926f, 0.5869195080611904f},
    {1.0f, -0.8218517544917526f, 1.3177346226477535f, -0.5147610573338093f, 0.41280159809618866f},
};
#elif DEC_STRIDE == 6
/* 4000Hz -> 666.67Hz, cutoff 293.33Hz (same 0.88 ratio to the new Nyquist 333.33Hz), and the
 * 3-band filterbank redesigned for fs=666.67Hz (same 30-50/50-80/80-130Hz absolute bands). */
static const float AA_B[5] = {0.001647044374329831f, 0.006588177497319324f, 0.009882266245978986f,
                               0.006588177497319324f, 0.001647044374329831f};
static const float AA_A[5] = {1.0f, -2.8007313099532984f, 3.070799253199432f, -1.5398998142263016f,
                               0.29618458096944505f};
static const float BAND_B[3][5] = {
    {0.007820208033497195f, 0, -0.01564041606699439f, 0, 0.007820208033497195f},
    {0.01658193166930305f, 0, -0.0331638633386061f, 0, 0.01658193166930305f},
    {0.04125353724172028f, 0, -0.08250707448344057f, 0, 0.04125353724172028f},
};
static const float BAND_A[3][5] = {
    {1.0f, -3.48793980138064f, 4.788095777659303f, -3.050876980518939f, 0.7660066009432641f},
    {1.0f, -2.978431944281721f, 3.840435522149019f, -2.4337686546267747f, 0.6704579059531741f},
    {1.0f, -1.890905580595881f, 2.261674923723877f, -1.3420711824184908f, 0.5139818942196758f},
};
#elif DEC_STRIDE == 10
/* 4000Hz -> 400Hz, cutoff 176Hz (same 0.88 ratio to the new Nyquist 200Hz), and the 3-band
 * filterbank redesigned for fs=400Hz (same 30-50/50-80/80-130Hz absolute bands). Generated via
 * scipy.signal.butter(4,...)/butter(2,...,btype='band') -- same exact formula reverse-verified
 * bit-for-bit against both the DEC_STRIDE==8 and DEC_STRIDE==6 coefficients above before being
 * trusted for this new rate (2026-08-06, RAM/clock exploration). */
static const float AA_B[5] = {0.00026065246340829931f, 0.0010426098536331972f, 0.001563914780449796f,
                               0.0010426098536331972f, 0.00026065246340829931f};
static const float AA_A[5] = {1.0f, -3.2786580631627373f, 4.084910893974536f, -2.2863387454274871f,
                               0.48425635403022088f};
static const float BAND_B[3][5] = {
    {0.020083365564211239f, 0, -0.040166731128422478f, 0, 0.020083365564211239f},
    {0.041253537241720296f, 0, -0.082507074483440593f, 0, 0.041253537241720296f},
    {0.097631072937817531f, 0, -0.19526214587563506f, 0, 0.097631072937817531f},
};
static const float BAND_A[3][5] = {
    {1.0f, -2.9168352223974185f, 3.7095751328615272f, -2.329296241137329f, 0.64135153805756329f},
    {1.0f, -1.7995526223795089f, 2.1756162427190548f, -1.277233374592974f, 0.51398189421967588f},
    {1.0f, 0.24991368265800784f, 0.95922458745521311f, 0.13668233164758969f, 0.33333333333333343f},
};
#else
#error "unhandled DEC_STRIDE"
#endif

static const float ENV_A = 0.7975628241529755f;

unsigned long c13stream_state_size(void) { return (unsigned long)sizeof(struct Combo13StreamState); }

/* COMBO13_VERSION (2026-08-08): revision number for this algorithm + its matching pcg_visualizer
 * deployment, bumped whenever combo13_stream.c/combo13_stream_internal.h's algorithm behavior or
 * memory footprint changes. 0.1 = the state as of RAM/clock pass 7 (STREAM_VARIANT=4,
 * STATE_SIZE_BYTES=17,052) -- see project memory (project_pcg_embedded_c) for the full history
 * this number now supersedes ("plan of record" is retired as an informal label going forward). */
const char *c13stream_version(void) { return COMBO13_VERSION; }

/* ============================================================================================
 * DSP PRIMITIVES (same designs as combo13_opt.c -- iir4/env_singlepole are both safe in-place,
 * used that way throughout this file specifically to avoid needing separate filt/env buffers)
 * ============================================================================================ */
static float iir4_step(const float b[5], const float a[5], float x, float z[4]) {
    float y = b[0] * x + z[0];
    z[0] = b[1] * x - a[1] * y + z[1];
    z[1] = b[2] * x - a[2] * y + z[2];
    z[2] = b[3] * x - a[3] * y + z[3];
    z[3] = b[4] * x - a[4] * y;
    return y;
}

static void swapf(float *a, float *b) { float t = *a; *a = *b; *b = t; }
static int median3_idx(const float *a, int i, int j, int k) {
    float ai = a[i], aj = a[j], ak = a[k];
    if ((ai <= aj && aj <= ak) || (ak <= aj && aj <= ai)) return j;
    if ((aj <= ai && ai <= ak) || (ak <= ai && ai <= aj)) return i;
    return k;
}
static int partitionf(float *a, int lo, int hi, int p) {
    float pivot = a[p];
    swapf(&a[p], &a[hi]);
    int store = lo;
    for (int i = lo; i < hi; i++) if (a[i] < pivot) { swapf(&a[i], &a[store]); store++; }
    swapf(&a[store], &a[hi]);
    return store;
}
static void quickselectf_range(float *a, int lo0, int hi0, int k) {
    int lo = lo0, hi = hi0;
    while (lo < hi) {
        int mid = lo + (hi - lo) / 2;
        int p = median3_idx(a, lo, mid, hi);
        p = partitionf(a, lo, hi, p);
        if (k == p) return;
        else if (k < p) hi = p - 1;
        else lo = p + 1;
    }
}
static void quickselectf(float *a, int n, int k) { quickselectf_range(a, 0, n - 1, k); }

/* ============================================================================================
 * STREAMING BASELINE (replaces "sort the whole 8s window for a percentile")
 * ============================================================================================ */
static void baseline_init(StreamBaseline *sb) {
    sb->ema = 0.0f; sb->initialized = false; sb->warm_sum = 0.0f; sb->warm_dur = 0.0f;
}

#define BASELINE_WARMUP_S 1.0f

/* Computes the EXACT percentile of |chunk| (via quickselect, chunk-sized scratch only -- no
 * whole-window sort), then EMA-blends it into the persistent estimate. `scratch` must be >= n.
 * For the first BASELINE_WARMUP_S seconds, `ema` is instead a duration-weighted running mean of
 * every chunk seen so far -- a single ~100ms chunk's percentile is noisy enough on its own that
 * seeding the EMA directly from it (the original design) left the artifact/decline threshold
 * under-converged for several seconds after onset; corpus data showed this window (roughly 5-20s
 * into a recording) is where combo13_stream's garbage-value rate exceeds combo13_opt's exact
 * whole-window reference. */
/* RAM/CLOCK PASS 7 (2026-08-07): `alpha` used to be recomputed via expf() INSIDE this function,
 * once per call -- but both call sites below always pass the SAME chunk_duration_s for a given
 * chunk (art_baseline and med_baseline update from the identical chunk), so it was really the
 * same expf() call done twice per chunk for no reason, the same class of redundant-recompute
 * waste already caught once elsewhere in this project's history (a per-band alpha recomputed 3x
 * for the same value). Hoisted to the caller, computed once per chunk, passed in -- bit-exact,
 * since it's the identical value either way. */
static float baseline_update(StreamBaseline *sb, const float *chunk, int n, float pctl,
                              float chunk_duration_s, float alpha, float *scratch) {
    for (int i = 0; i < n; i++) scratch[i] = fabsf(chunk[i]);
    int idx = (int)(n * pctl); if (idx > n - 1) idx = n - 1; if (idx < 0) idx = 0;
    quickselectf(scratch, n, idx);
    float chunk_val = scratch[idx];
    if (!sb->initialized) {
        sb->warm_sum += chunk_val * chunk_duration_s;
        sb->warm_dur += chunk_duration_s;
        sb->ema = sb->warm_sum / sb->warm_dur;
        if (sb->warm_dur >= BASELINE_WARMUP_S) sb->initialized = true;
    } else {
        sb->ema = alpha * sb->ema + (1.0f - alpha) * chunk_val;
    }
    return sb->ema;
}

static void interp_over_mask(float *dst, const float *src, const bool *mask, int n) {
    int i = 0;
    while (i < n) {
        if (!mask[i]) { i++; continue; }
        int j = i;
        while (j < n && mask[j]) j++;
        float start_val = (i > 0) ? src[i - 1] : (j < n ? src[j] : src[n - 1]);
        float end_val = (j < n) ? src[j] : (i > 0 ? src[i - 1] : src[0]);
        int length = j - i;
        for (int k = 0; k < length; k++) {
            float t = (float)(k + 1) / (float)(length + 1);
            dst[i + k] = start_val + (end_val - start_val) * t;
        }
        i = j;
    }
}

/* Single-pass detect->extend->interpolate over a SMALL [carry+chunk] buffer, using the
 * ALREADY-ESTABLISHED (pre-this-chunk) streaming baseline for the threshold, rather than
 * re-deriving it from scratch each time (Phase A's 5-pass design existed to keep the baseline
 * uncontaminated by a spike it was itself about to detect -- here the EMA already reflects many
 * PRIOR clean chunks, so a single detection pass against a stable historical estimate serves the
 * same purpose without needing multiple whole-window passes). Modifies `buf` in place. */
static void stream_detect_extend_interp(float *buf, int n, float scale, float detect_mult,
                                          float extend_mult, int pad_samples,
                                          bool *scratch_grown, float *scratch_absx) {
    float lim = detect_mult * scale;
    float extend_lim = extend_mult * scale;
    for (int i = 0; i < n; i++) { scratch_absx[i] = fabsf(buf[i]); scratch_grown[i] = false; }

    bool any = false;
    int i = 0;
    while (i < n) {
        if (scratch_grown[i] || scratch_absx[i] <= lim) { i++; continue; }
        int lo = i;
        while (lo > 0 && scratch_absx[lo - 1] > extend_lim) lo--;
        int hi = i;
        while (hi < n - 1 && scratch_absx[hi + 1] > extend_lim) hi++;
        for (int k = lo; k <= hi; k++) if (!scratch_grown[k]) { scratch_grown[k] = true; any = true; }
        i = hi + 1;
    }
    if (!any) return;

    if (pad_samples > 0) {
        static bool padded_static[NATIVE_CARRY_N + NATIVE_CHUNK_N > DEC_CARRY_N + DEC_CHUNK_N
                                   ? NATIVE_CARRY_N + NATIVE_CHUNK_N : DEC_CARRY_N + DEC_CHUNK_N];
        bool *padded = padded_static;
        memcpy(padded, scratch_grown, n * sizeof(bool));
        for (int center = 0; center < n; center++) {
            if (!scratch_grown[center]) continue;
            int lo = center - pad_samples; if (lo < 0) lo = 0;
            int hi = center + pad_samples + 1; if (hi > n) hi = n;
            for (int k = lo; k < hi; k++) padded[k] = true;
        }
        float orig[NATIVE_CARRY_N + NATIVE_CHUNK_N > DEC_CARRY_N + DEC_CHUNK_N
                    ? NATIVE_CARRY_N + NATIVE_CHUNK_N : DEC_CARRY_N + DEC_CHUNK_N];
        memcpy(orig, buf, n * sizeof(float));
        interp_over_mask(buf, orig, padded, n);
    } else {
        float orig[NATIVE_CARRY_N + NATIVE_CHUNK_N > DEC_CARRY_N + DEC_CHUNK_N
                    ? NATIVE_CARRY_N + NATIVE_CHUNK_N : DEC_CARRY_N + DEC_CHUNK_N];
        memcpy(orig, buf, n * sizeof(float));
        interp_over_mask(buf, orig, scratch_grown, n);
    }
}

/* declick clip (median-based), in place -- same math as combo13_opt's declick_mult but using the
 * streaming median estimate instead of a whole-window median. */
static void declick_clip(float *buf, int n, float scale, float mult) {
    float lim = mult * scale;
    for (int i = 0; i < n; i++) {
        float v = buf[i];
        buf[i] = v > lim ? lim : (v < -lim ? -lim : v);
    }
}

/* ============================================================================================
 * PERSISTENT PER-BAND FEATURE STREAM (2026-08-05, RAM/clock pass 4) -- replaces the old raw-
 * decimated-history ring (dec_hist) + per-tick from-scratch gather/filter/envelope (scratch_work)
 * entirely. See combo13_stream_internal.h's comment on band_filt_z/band_env_ring for the design.
 * ============================================================================================ */
static void stream_dec_samples(struct Combo13StreamState *st, const float *samples, int n) {
    for (int i = 0; i < n; i++) {
        if (st->dec_sample_count < DEC_MAX_V) st->dec_sample_count++;

        float env[3];
        for (int b = 0; b < 3; b++) {
            float filtered = iir4_step(BAND_B[b], BAND_A[b], samples[i], st->band_filt_z[b]);
            float e = (1.0f - ENV_A) * fabsf(filtered) + ENV_A * st->band_env_z[b];
            st->band_env_z[b] = e;
            env[b] = e;
        }
        st->band_frame_phase++;
        if (st->band_frame_phase >= FRAME_DS) {
            st->band_frame_phase = 0;
            for (int b = 0; b < 3; b++) {
                float ec = env[b] < -32768.0f ? -32768.0f : (env[b] > 32767.0f ? 32767.0f : env[b]);
                st->band_env_ring[b][st->band_env_ring_head] = (int16_t)lroundf(ec);
            }
            st->band_env_ring_head = (st->band_env_ring_head + 1) % FEAT_MAX_V;
            if (st->band_env_ring_len < FEAT_MAX_V) st->band_env_ring_len++;
        }
    }
}
static void band_env_gather_trailing(const struct Combo13StreamState *st, int band_idx, int m, float *out) {
    int start = ((st->band_env_ring_head - m) % FEAT_MAX_V + FEAT_MAX_V) % FEAT_MAX_V;
    for (int i = 0; i < m; i++) out[i] = (float)st->band_env_ring[band_idx][(start + i) % FEAT_MAX_V];
}

/* ============================================================================================
 * FEATURE EXTRACTION -- gathers trailing frames straight from the persistent per-band envelope
 * rings (stream_dec_samples keeps these current, one push per FRAME_DS decimated samples). No
 * raw filtering happens here anymore (2026-08-05, RAM/clock pass 4) -- see
 * combo13_stream_internal.h's comment on band_filt_z/band_env_ring for the design.
 * ============================================================================================ */
static int feat_norm_clip(struct Combo13StreamState *st, int win_n, int band_idx, float clip, float *out) {
    int m = win_n / FRAME_DS;
    band_env_gather_trailing(st, band_idx, m, out);
    float sum = 0.0f;
    for (int i = 0; i < m; i++) sum += out[i];
    if (m == 0) return 0;
    float mean = sum / (float)m;
    float var = 0.0f;
    for (int i = 0; i < m; i++) { float d = out[i] - mean; var += d * d; }
    float sd = sqrtf(var / (float)m);
    if (sd == 0.0f) sd = 1.0f;
    for (int i = 0; i < m; i++) {
        float z = (out[i] - mean) / sd;
        out[i] = z > clip ? clip : (z < -clip ? -clip : z);
    }
    return m;
}

static int sum_env_norm(struct Combo13StreamState *st, int win_n, float clip, float *out) {
    /* accumulates bands 2, 0, 1 into `out` one at a time (2026-08-06: was band2->out direct,
     * band0->scratch_feat_a, band1->scratch_feat_b, then out[i]+=a[i]+b[i] -- restructured to
     * never need two scratch bands alive at once, so both can share ONE buffer, scratch_shared,
     * reused for band0 then again for band1). Same 3 additions per element either way. */
    feat_norm_clip(st, win_n, 2, clip, out);   /* band 2 written directly into the caller's out[] */
    int m = feat_norm_clip(st, win_n, 0, clip, st->scratch_shared);
    for (int i = 0; i < m; i++) out[i] += st->scratch_shared[i];
    feat_norm_clip(st, win_n, 1, clip, st->scratch_shared);
    for (int i = 0; i < m; i++) out[i] += st->scratch_shared[i];
    return m;
}

/* unnormalized summed envelope -- signal-level (onset) and peak-mean (flatline) only */
static int broad_env(struct Combo13StreamState *st, int win_n, float *out) {
    int m = win_n / FRAME_DS;
    band_env_gather_trailing(st, 0, m, out);
    float tmp[FEAT_MAX_V];
    for (int b = 1; b < 3; b++) {
        band_env_gather_trailing(st, b, m, tmp);
        for (int i = 0; i < m; i++) out[i] += tmp[i];
    }
    return m;
}

/* ENVELOPE FLUX (2026-08-08, AGREEMENT_SIGNAL_FLUX experiment) -- faithful port of this project's
 * already-validated `envelope_flux()`/`envelope_flux_param()` (pcg_abab/flux_algorithm.py,
 * teal_flux_algorithm.py): rectified first-difference of the RAW (unnormalized) 3-band-summed
 * envelope, z-scored+clipped. Reuses broad_env() (already computed elsewhere for onset/flatline)
 * rather than adding any new per-sample state -- band_env_ring already streams everything this
 * needs. Rectification+diff done in a single in-place forward pass (tracks the previous RAW sample
 * in a local rather than needing a second buffer); out[0]'s diff is defined as 0, matching Python's
 * `np.diff(env, prepend=env[0])`. Clips to FEAT_CLIP, same convention as every z-scored feature in
 * this file (matches flux_algorithm.py's own `FLUX_CLIP = P.FEAT_CLIP` comment). */
static int envelope_flux(struct Combo13StreamState *st, int win_n, float *out) {
    int m = broad_env(st, win_n, out);
    if (m == 0) return 0;
    float prev = out[0];
    out[0] = 0.0f;
    for (int i = 1; i < m; i++) {
        float cur = out[i];
        float d = cur - prev;
#if FLUX_RECTIFY_MODE
        out[i] = fabsf(d);
#else
        out[i] = d > 0.0f ? d : 0.0f;
#endif
        prev = cur;
    }
    float sum = 0.0f;
    for (int i = 0; i < m; i++) sum += out[i];
    float mean = sum / (float)m;
    float var = 0.0f;
    for (int i = 0; i < m; i++) { float d = out[i] - mean; var += d * d; }
    float sd = sqrtf(var / (float)m);
    if (sd == 0.0f) sd = 1.0f;
    for (int i = 0; i < m; i++) {
        float z = (out[i] - mean) / sd;
        out[i] = z > FLUX_CLIP ? FLUX_CLIP : (z < -FLUX_CLIP ? -FLUX_CLIP : z);
    }
    return m;
}

/* ============================================================================================
 * AUTOCORRELATION (trimmed-mean-of-products, via quickselect -- same design as combo13_opt's
 * task-5 optimization, `prod` scratch reuses scratch_shared since it's already free by the time
 * this runs: sum_env_norm's own use of scratch_shared is fully consumed into scratch_sumenv
 * before add_autocorr_robust is ever called.)
 * ============================================================================================ */
static void add_autocorr_robust(struct Combo13StreamState *st, float *S, const float *f, int T,
                                 int lo, int hi, float trim_frac) {
    if (T <= hi) return;
    float r0 = 0.0f;
    for (int i = 0; i < T; i++) r0 += f[i] * f[i];
    r0 /= (float)T;
    float *prod = st->scratch_shared;
    for (int l = lo; l < hi; l++) {
        int n = T - l;
        for (int i = 0; i < n; i++) prod[i] = f[i] * f[i + l];
        int trim = (int)(n * trim_frac);
        int keep = n - 2 * trim;
        float mean_val = 0.0f;
        if (keep > 0) {
            quickselectf(prod, n, trim);
            if (keep > 1) quickselectf_range(prod, trim, n - 1, n - 1 - trim);
            float sum = 0.0f;
            for (int i = trim; i < n - trim; i++) sum += prod[i];
            mean_val = sum / (float)keep;
        }
        S[l - lo] += mean_val / (r0 + 1e-9f);
    }
}

static void add_autocorr_plain(float *S, const float *f, int T, int lo, int hi) {
    if (T <= hi) return;
    float r0 = 0.0f;
    for (int i = 0; i < T; i++) r0 += f[i] * f[i];
    r0 /= (float)T;
    for (int l = lo; l < hi; l++) {
        int n = T - l;
        float s = 0.0f;
        for (int i = 0; i < n; i++) s += f[i] * f[i + l];
        S[l - lo] += (s / (float)T) / (r0 + 1e-9f);
    }
}

typedef struct {
    bool valid;
    float bpm;
    float conf;
    float Sw[S_LEN];
    int lo;
} EstimateResult;

#if defined(TEMPLATE_MATCH_GATED_ENABLE) && STREAM_VARIANT == 4
/* The gated pick itself: default to the tallest peak (bins[0], = win_teal.bpm's own choice) unless
 * a smaller peak's combined_template_score() beats it by more than TEMPLATE_MATCH_OVERRIDE_MARGIN.
 * Returns false (leave raw_cand_bpm untouched) if win_teal has no local maxima at all -- same
 * degenerate case estimate_detrend_from_mw() itself already guards (ncand==0 there). Placed here
 * (after EstimateResult's own typedef, not up with this mechanism's other helpers above) since it's
 * the only piece of this mechanism that needs the type itself. */
static bool template_match_gated_pick(const EstimateResult *win_r, const EstimateResult *win_teal,
                                        float *out_bpm) {
    if (!win_r->valid || !win_teal->valid) return false;
    int bins[3];
    int npk = find_top3_peaks(win_teal->Sw, bins);
    if (npk == 0) return false;

    float best_bpm = 60.0f * FFS / (float)(LO_LAG + bins[0]);
    float best_score = combined_template_score(win_r->Sw, win_teal->Sw, best_bpm);
    for (int i = 1; i < npk; i++) {
        float cand_bpm = 60.0f * FFS / (float)(LO_LAG + bins[i]);
        float cand_score = combined_template_score(win_r->Sw, win_teal->Sw, cand_bpm);
        if (cand_score > best_score + TEMPLATE_MATCH_OVERRIDE_MARGIN && cand_score > best_score) {
            best_score = cand_score; best_bpm = cand_bpm;
        }
    }
    *out_bpm = best_bpm;
    return true;
}
#endif

/* TEMPLATE_MATCH_GATED_V2_ENABLE (2026-08-10) -- per Daniel's "can you make an experimental
 * algorithm that integrates [the re-visualizer's upgraded candidate selection] into v0.10?" after
 * this project's own re-visualizer exploration found its raw per-tick candidate (top-6 peaks + the
 * SAME region-weighted-both-curve template score above) measurably better than v0.10's own raw
 * candidate (win_teal's plain tallest-detrended-peak pick) BEFORE any smoothing is applied. Rather
 * than treating the re-visualizer as a replacement algorithm (its own corpus numbers, while better
 * on raw tick accuracy, have never been checked for DISPLAY STABILITY/jitter the way this project's
 * real UX-tuning always has been -- see project memory), this ONLY swaps what feeds raw_cand_bpm.
 * Every downstream mechanism -- settling freeze (suppress_circle/frozen_wait_s), harmonic veto
 * (check_harmonic_confirmed), jump confirmation (teal_jump_streak), bpm-dependent delay
 * (required_teal_delay_ticks), and the weighted-median combine (teal_size_factor/weighted_median) --
 * runs EXACTLY as before, completely untouched. Reuses the SAME region-weight/template
 * infrastructure as TEMPLATE_MATCH_GATED_ENABLE above (RegionWeight/kRegionWeights/
 * build_region_weights/weighted_template_match_corr/best_weighted_template_corr/
 * combined_template_score are all shared, unmodified).
 * FULL CORPUS VALIDATION STORY (DISPLAYED-bpm, 314 files, 26083 ticks, v011_corpus_validate.py) --
 * v0.10 baseline: 59.07% correct, 65 half, 19 third, 816 other, 9776 no_display.
 *   ATTEMPT 1 (top-6 peaks + region score + a tent-shaped selection bias, ported directly from the
 *   re-visualizer's own promoted design, floor tuned to that tent-adjusted scale at 0.70): **59.07%
 *   -> 57.74% correct, a REGRESSION**, driven by third +315 (19->334), half +27, double +29,
 *   concentrated almost entirely in high-bpm 'doubled'-category files (true rate 160-190bpm).
 *   Root cause, confirmed by ablation (below): v0.10 ALREADY applies the exact same tent
 *   (bpm_favor_center()) once, downstream, in the weighted-median combine (teal_size_factor()) --
 *   applying it a SECOND time here, at selection, doubles up the pull toward ~56bpm strongly enough
 *   to catch files whose true rate's 1/3-subharmonic lands almost exactly in that same favored zone
 *   (165-190bpm / 3 = 55-63bpm) -- exactly the failure mode observed. A real, informative negative
 *   result: a mechanism validated in the re-visualizer (which has NO other bpm-plausibility bias
 *   anywhere in its pipeline) doesn't transplant unchanged into a host that already has one.
 *   ATTEMPT 2 / PROMOTED DEFAULT (same top-6 peaks + region score, tent bias REMOVED, floor rescaled
 *   to 0.30 to match the plain combined_template_score()'s own scale -- see V1's own floor-equivalent
 *   reasoning above): **59.07% -> 59.67% correct, a REAL, CLEAN WIN** -- half 65->0 (eliminated),
 *   third 19->0 (eliminated), other 816->810, no_display 9776->9708 -- every axis improved
 *   simultaneously, nothing traded away. Unlike TEMPLATE_MATCH_GATED_ENABLE (V1, top-3, no tent, no
 *   floor), which was found to be a WASH at the displayed-bpm level (v0.10's own downstream
 *   smoothing absorbed most of V1's tick-level gain) -- the wider top-6 search plus the confidence
 *   floor together survive v0.10's smoothing stack and produce a genuine improvement, once the
 *   redundant tent multiplier is removed. RAM cost: ZERO beyond TEMPLATE_WEIGHT_ENABLE's own existing
 *   +128B (confirmed via STATE_SIZE_BYTES, identical with and without this macro) -- all V2 state is
 *   local/stack, and the template tables are already-shared static const data.
 * TEMPLATE_MATCH_GATED_V2_TENT_ENABLE is kept as an OPT-IN ablation flag to reproduce attempt 1's
 * rejected tent-biased behavior for reference (same discipline as TEMPLATE_MATCH_ENABLE's rejected
 * hard veto, SYMMETRIC_ENABLE, etc. elsewhere in this file) -- default (undefined) is the winning
 * no-tent configuration. Both real recordings re-verified against the default config:
 * daniel_recovery_test.f32/daniel_offchest_test.f32 tail bpm unchanged (54.5/matches baseline).
 * STATUS: PROMOTED (2026-08-10, revision 0.11) per Daniel's "push it to the visualizer as v0.11" --
 * deployed to pcg_visualizer's shipped WASM build (both production combo13_wasm.js and the debug
 * build). This macro is now part of the standard shipped compile-flag set, same convention as
 * TEMPLATE_WEIGHT_ENABLE (an explicit -D flag at build time, not unconditionally on in source). */
#ifndef TEMPLATE_MATCH_GATED_V2_N_PEAKS_OVERRIDE
#define TEMPLATE_MATCH_GATED_V2_N_PEAKS_OVERRIDE 6
#endif
#ifndef TEMPLATE_MATCH_OVERRIDE_MARGIN_V2_OVERRIDE
#define TEMPLATE_MATCH_OVERRIDE_MARGIN_V2_OVERRIDE 0.3f
#endif
#ifndef TEMPLATE_MATCH_CONFIDENCE_FLOOR_V2_OVERRIDE
/* 0.30 matches the plain combined_template_score() scale (same floor-equivalent reasoning as V1's
 * own margin above); TEMPLATE_MATCH_GATED_V2_TENT_ENABLE rescales to 0.70 (the re-visualizer's own
 * tent-adjusted-scale value) since multiplying by bpm_favor_center() shifts the score's whole range --
 * same "these thresholds aren't on the same scale" reasoning the JS re-visualizer itself documents
 * for its selectionScore vs combined columns. */
#ifdef TEMPLATE_MATCH_GATED_V2_TENT_ENABLE
#define TEMPLATE_MATCH_CONFIDENCE_FLOOR_V2_OVERRIDE 0.70f
#else
#define TEMPLATE_MATCH_CONFIDENCE_FLOOR_V2_OVERRIDE 0.30f
#endif
#endif
#if defined(TEMPLATE_MATCH_GATED_V2_ENABLE) && STREAM_VARIANT == 4
static const int   TEMPLATE_MATCH_GATED_V2_N_PEAKS = TEMPLATE_MATCH_GATED_V2_N_PEAKS_OVERRIDE;
static const float TEMPLATE_MATCH_OVERRIDE_MARGIN_V2 = TEMPLATE_MATCH_OVERRIDE_MARGIN_V2_OVERRIDE;
static const float TEMPLATE_MATCH_CONFIDENCE_FLOOR_V2 = TEMPLATE_MATCH_CONFIDENCE_FLOOR_V2_OVERRIDE;
#ifdef TEMPLATE_MATCH_GATED_V2_TENT_ENABLE
static float bpm_favor_center(float bpm); /* forward decl -- defined below, in the COMBINE STAGE
                                              HELPERS section; same forward-reference situation V1's
                                              own template_match_gated_pick is already in relative to
                                              EstimateResult, just one function further down. Only
                                              needed for this rejected-but-kept-for-reference ablation
                                              path -- the promoted default never calls it here. */
#endif
/* Generalization of find_top3_peaks() above to an arbitrary cap (kept as a SEPARATE function rather
 * than parameterizing find_top3_peaks itself, so V1 stays byte-for-byte untouched for reference). */
/* RAM/CLOCK PASS (2026-08-10), per Daniel's "everything is tight on my MCU... reduce clock cycles,
 * RAM and flash." Original version collected EVERY local maximum into a full S_LEN(96)-int stack
 * array (corpus-wide max observed: 19 candidates in one tick's curve, but the buffer was sized for
 * the worst theoretical case) then selection-sorted the whole thing -- O(S_LEN) stack + O(ncand^2)
 * compares just to keep the top n_max=6. This is a single-pass running-top-N insertion instead:
 * O(n_max)=6-sized stack arrays, O(n_max) work per newly-found local max (worst case, usually less)
 * -- same total peak-finding cost class, but the large scratch array is gone entirely, and the
 * typical-case compare count drops sharply once a curve has more than a handful of local maxima.
 * TIE-BREAKING PRESERVED EXACTLY: the original selection sort used strict '>' when updating its
 * running `best`, so an equal-height LATER-scanned candidate never displaces an EARLIER one --
 * ties keep ascending-bin-index (lower lag) order. This version scans in the same ascending-bin
 * order and also uses strict '>' for both the insertion-shift and the full-list admission check, so
 * a same-height later candidate can never shift past, or evict, an equal-height earlier one --
 * bit-identical output to the original for every tie case, not just the common no-tie case. */
static int find_topN_peaks(const float *curve, int *out_bins, int n_max) {
    float top_height[TEMPLATE_MATCH_GATED_V2_N_PEAKS_OVERRIDE];
    int count = 0;
    for (int i = 1; i < S_LEN - 1; i++) {
        if (!(curve[i] >= curve[i - 1] && curve[i] > curve[i + 1])) continue;
        float h = curve[i];
        if (count < n_max) {
            int pos = count;
            while (pos > 0 && h > top_height[pos - 1]) {
                top_height[pos] = top_height[pos - 1]; out_bins[pos] = out_bins[pos - 1]; pos--;
            }
            top_height[pos] = h; out_bins[pos] = i;
            count++;
        } else if (h > top_height[n_max - 1]) {
            int pos = n_max - 1;
            while (pos > 0 && h > top_height[pos - 1]) {
                top_height[pos] = top_height[pos - 1]; out_bins[pos] = out_bins[pos - 1]; pos--;
            }
            top_height[pos] = h; out_bins[pos] = i;
        }
    }
    return count;
}
#ifdef TEMPLATE_MATCH_GATED_V2_TENT_ENABLE
#define TEMPLATE_MATCH_GATED_V2_TENT(bpm) bpm_favor_center(bpm)
#else
#define TEMPLATE_MATCH_GATED_V2_TENT(bpm) (1.0f)
#endif
/* The V2 gated pick: same default-to-tallest-unless-clearly-beaten override rule as V1, but scoring
 * top-6 peaks, and a confidence floor that can reject the candidate outright. *out_reject is set
 * true ONLY when peaks were found but the winner still isn't good enough (mirrors the re-
 * visualizer's confidence-floor semantics); left false when there was nothing to look at (npk==0),
 * matching V1's own "leave raw_cand_bpm untouched" convention for that degenerate case -- the caller
 * distinguishes these via the return value AND *out_reject together. */
static bool template_match_gated_pick_v2(struct Combo13StreamState *st,
                                           const EstimateResult *win_r, const EstimateResult *win_teal,
                                           float *out_bpm, bool *out_reject) {
    *out_reject = false;
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_v2_valid = 0; st->dbg_v2_n_peaks = 0; st->dbg_v2_winner_idx = -1;
#endif
    if (!win_r->valid || !win_teal->valid) return false;
    int bins[S_LEN];
    int npk = find_topN_peaks(win_teal->Sw, bins, TEMPLATE_MATCH_GATED_V2_N_PEAKS);
    if (npk == 0) return false;

    float peak_bpm[TEMPLATE_MATCH_GATED_V2_N_PEAKS_OVERRIDE];
    float peak_score[TEMPLATE_MATCH_GATED_V2_N_PEAKS_OVERRIDE];
    int best_idx = 0;
    peak_bpm[0] = 60.0f * FFS / (float)(LO_LAG + bins[0]);
    peak_score[0] = combined_template_score(win_r->Sw, win_teal->Sw, peak_bpm[0]) * TEMPLATE_MATCH_GATED_V2_TENT(peak_bpm[0]);
    for (int i = 1; i < npk; i++) {
        peak_bpm[i] = 60.0f * FFS / (float)(LO_LAG + bins[i]);
        peak_score[i] = combined_template_score(win_r->Sw, win_teal->Sw, peak_bpm[i]) * TEMPLATE_MATCH_GATED_V2_TENT(peak_bpm[i]);
        if (peak_score[i] > peak_score[best_idx] + TEMPLATE_MATCH_OVERRIDE_MARGIN_V2 && peak_score[i] > peak_score[best_idx]) {
            best_idx = i;
        }
    }
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_v2_valid = 1;
    st->dbg_v2_n_peaks = npk;
    for (int i = 0; i < npk && i < COMBO13_DBG_V2_MAX_PEAKS; i++) {
        st->dbg_v2_peak_bpm[i] = peak_bpm[i];
        st->dbg_v2_peak_score[i] = peak_score[i];
    }
#endif
    if (peak_score[best_idx] < TEMPLATE_MATCH_CONFIDENCE_FLOOR_V2) { *out_reject = true; return false; }
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_v2_winner_idx = best_idx;
#endif
    *out_bpm = peak_bpm[best_idx];
    return true;
}
#endif

static EstimateResult estimate_sumenv_robust(struct Combo13StreamState *st, int win_n,
                                              float trim_frac, float feat_clip) {
    EstimateResult r; r.valid = false;
    if (win_n < 3 * (int)DEC_HZ) return r;
    float S[S_LEN] = {0};
    int m = sum_env_norm(st, win_n, feat_clip, st->scratch_sumenv);
    add_autocorr_robust(st, S, st->scratch_sumenv, m, LO_LAG, HI_LAG, trim_frac);

    float Sw[S_LEN];
    for (int l = LO_LAG; l < HI_LAG; l++) Sw[l - LO_LAG] = S[l - LO_LAG] * st->lag_pow_recip[l - LO_LAG];
    float swmin = Sw[0];
    for (int i = 1; i < S_LEN; i++) if (Sw[i] < swmin) swmin = Sw[i];
    for (int i = 0; i < S_LEN; i++) Sw[i] -= swmin;

    int pk[S_LEN]; int npk = 0;
    for (int i = 1; i < S_LEN - 1; i++)
        if (Sw[i] >= Sw[i - 1] && Sw[i] > Sw[i + 1]) pk[npk++] = i;

    memcpy(r.Sw, Sw, sizeof(Sw));
    r.lo = LO_LAG;
    if (npk == 0) {
        r.bpm = 60.0f / ((float)LO_LAG / FFS);
        r.conf = 1.0f;
        r.valid = true;
        return r;
    }
    for (int i = 0; i < npk; i++) {
        int best = i;
        for (int j = i + 1; j < npk; j++) if (Sw[pk[j]] > Sw[pk[best]]) best = j;
        int tmp = pk[i]; pk[i] = pk[best]; pk[best] = tmp;
    }
    int p1 = pk[0];
    int bl = LO_LAG + p1;
    int p2 = -1;
    for (int k = 1; k < npk; k++) if (abs(pk[k] - p1) >= 8) { p2 = pk[k]; break; }
    if (p2 < 0 && npk > 1) p2 = pk[1];
    float conf = (p2 < 0) ? 1.2f : Sw[p1] / (Sw[p2] + 1e-9f);
    r.bpm = 60.0f / ((float)bl / FFS);
    r.conf = conf;
    r.valid = true;
    return r;
}

static float moving_average_at(const float *csum, int n, int i, int half_width) {
    int lo = i - half_width; if (lo < 0) lo = 0;
    int hi = i + half_width; if (hi > n - 1) hi = n - 1;
    return (csum[hi + 1] - csum[lo]) / (float)(hi - lo + 1);
}

static EstimateResult estimate_detrend_from_mw(const EstimateResult *win_r, int half_width) {
    EstimateResult out; out.valid = false;
    if (!win_r->valid) return out;
    int n = S_LEN;
    float csum[S_LEN + 1]; csum[0] = 0.0f;
    for (int i = 0; i < n; i++) csum[i + 1] = csum[i] + win_r->Sw[i];
    float det[S_LEN];
    for (int i = 0; i < n; i++) det[i] = win_r->Sw[i] - moving_average_at(csum, n, i, half_width);
    /* Stash the actual detrended curve into out.Sw (2026-08-07, per Daniel's "just plot the real
     * curve" request) -- out.Sw was originally unused by this function's own PRODUCTION callers
     * (only .bpm/.conf/.valid were read there), so this started as a debug-only addition, gated
     * behind COMBO13_EXPOSE_DEBUG so the production build paid zero cost for a copy it never read.
     * UNGATED 2026-08-09: this is no longer true -- the curve_gate and curve_weight production
     * consumers below now read win_teal.Sw directly. A REAL BUG was found and fixed here: the
     * (rejected) CURVE_GATE_ENABLE hard-veto prototype's entire corpus sweep was built WITHOUT
     * -DCOMBO13_EXPOSE_DEBUG, meaning out.Sw was never written and win_teal.Sw was read as
     * UNINITIALIZED STACK MEMORY, not the real curve, for that whole investigation. The rejection
     * verdict itself is independently supported by the valid debug-WASM-derived corpus analysis
     * (curvecorr_validate_ticks.json: even the strictest real curve_corr band is still 63.3%
     * correct) -- but the specific C-implementation sweep numbers from that investigation were
     * measuring noise, not the real mechanism, and were re-verified after this fix (see project
     * memory) before trusting the new curve_weight_* soft-multiplier work that follows. */
    memcpy(out.Sw, det, sizeof(det));

    int cand[S_LEN]; int ncand = 0;
    for (int i = 1; i < n - 1; i++)
        if (det[i] >= det[i - 1] && det[i] > det[i + 1]) cand[ncand++] = i;
    if (ncand == 0) return out;
    for (int i = 0; i < ncand; i++) {
        int best = i;
        for (int j = i + 1; j < ncand; j++) if (det[cand[j]] > det[cand[best]]) best = j;
        int tmp = cand[i]; cand[i] = cand[best]; cand[best] = tmp;
    }
    int p1 = cand[0];
    int p2 = -1;
    for (int k = 1; k < ncand; k++) if (abs(cand[k] - p1) >= 8) { p2 = cand[k]; break; }
    float v1 = det[p1]; if (v1 < 1e-6f) v1 = 1e-6f;
    float conf;
    if (p2 < 0) conf = 1.2f;
    else {
        float v2 = det[p2]; if (v2 < 1e-6f) v2 = 1e-6f;
        conf = v1 / (v2 + 1e-9f);
    }
    out.bpm = 60.0f / (((float)win_r->lo + p1) / FFS);
    out.conf = conf;
    out.valid = true;
    return out;
}

/* ============================================================================================
 * HARMONIC-CONFIRMATION VETO (unchanged design from combo13_opt's task-6 de-duplication)
 * ============================================================================================ */
static int check_harmonic_confirmed(struct Combo13StreamState *st, int hist_len, int win_nsamp,
                                     bool cand_valid, float cand_bpm,
                                     const float *precomputed_feat, int precomputed_m,
                                     float robust_trim_frac) {
    if (!cand_valid || hist_len < win_nsamp) return -1;
    float cand_lag = 60.0f * FFS / cand_bpm;
    int hi_ext = (int)ceilf(HARMONIC_VETO_HI_EXT_MULT * cand_lag) + 2;
    if (hi_ext <= LO_LAG + 2) return 0;
    if (hi_ext > EXT_HI_MAX_V) hi_ext = EXT_HI_MAX_V;

    int m = precomputed_m;
    int n_ext = hi_ext - LO_LAG;
    if (m <= hi_ext) return 0;

    float *S_ext = st->scratch_Sext;
    for (int i = 0; i < n_ext; i++) S_ext[i] = 0.0f;
    add_autocorr_robust(st, S_ext, precomputed_feat, m, LO_LAG, hi_ext, robust_trim_frac);

    float target_lo = HARMONIC_VETO_LO_FRAC * cand_lag;
    float target_hi = HARMONIC_VETO_HI_FRAC * cand_lag;
    float smax = 0.0f;
    for (int i = 0; i < n_ext; i++) if (S_ext[i] > smax) smax = S_ext[i];
    float thresh = HARMONIC_VETO_HEIGHT_FRAC * smax;

    bool confirmed = false;
    for (int i = 1; i < n_ext - 1; i++) {
        float lag = (float)(LO_LAG + i);
        if (lag > target_lo && lag < target_hi &&
            S_ext[i] >= S_ext[i - 1] && S_ext[i] > S_ext[i + 1] && S_ext[i] > thresh) {
            confirmed = true;
            break;
        }
    }
    if (!confirmed) return 0;

#ifdef HARMONIC_VETO_SYMMETRIC_ENABLE
    /* SYMMETRIC HALF-LAG CHECK (2026-08-09), per Daniel's "any other ideas to increase accuracy?" --
     * PROTOTYPED, INSTRUMENTED, AND REJECTED. The check above only ever guards against the
     * candidate being too FAST (looks for corroborating structure at ~2x the candidate's own lag).
     * There is no symmetric check for the candidate being too SLOW (locked onto a sub-harmonic,
     * true peak at ~0.5x the candidate's lag) -- tried adding one: if S_ext (the raw, undetrended
     * extended autocorrelation already computed above) shows a TALLER local-max peak at ~half the
     * candidate's lag than AT the candidate's own lag, veto. Reused HARMONIC_VETO_LO_FRAC/HI_FRAC
     * reciprocally for a symmetric search window -- no new tunable constants needed.
     * INSTRUMENTED WITH COUNTERS BEFORE TRUSTING THE ZERO-EFFECT CORPUS RESULT (temporary driver,
     * not shipped): across the full 314-file/18,361-evaluation corpus, the check WAS reached every
     * time as designed (cand_idx in valid range 18,361/18,361), and a peak DID exist somewhere in
     * the half-lag search window fairly often (3,025/18,361, ~16.5%) -- but that peak was taller
     * than the candidate's OWN value in the same raw curve only 3 TIMES ever, nowhere near enough
     * to move any corpus metric (confirmed: classify() tallies were byte-identical with and without
     * this check enabled). Root cause understood, not just observed: detrending (which produced the
     * candidate) removes a slow common-mode baseline trend, it does not typically reshuffle which
     * lag has the tallest LOCAL peak -- so the candidate's own raw-curve value is almost always
     * already close to locally dominant, leaving essentially no room for a genuinely taller
     * competing peak nearby. A real, informative negative result (this project's existing candidate
     * selection is already robust against this specific failure mode, not that the check was buggy
     * or untested) -- kept OPT-IN ONLY via HARMONIC_VETO_SYMMETRIC_ENABLE (undefined by default,
     * zero cost) for anyone who wants to try a different comparison basis in the future. */
    int cand_idx = (int)lroundf(cand_lag) - LO_LAG;
    if (cand_idx >= 1 && cand_idx < n_ext - 1) {
        float cand_val = S_ext[cand_idx];
        float target_lo2 = cand_lag / HARMONIC_VETO_HI_FRAC;
        float target_hi2 = cand_lag / HARMONIC_VETO_LO_FRAC;
        for (int i = 1; i < n_ext - 1; i++) {
            float lag = (float)(LO_LAG + i);
            if (lag > target_lo2 && lag < target_hi2 &&
                S_ext[i] >= S_ext[i - 1] && S_ext[i] > S_ext[i + 1] && S_ext[i] > cand_val)
                return 0; // a taller competing peak at ~half the lag -- distrust the candidate
        }
    }
#endif

    return 1;
}

static int peak_idx_tallest_local_max(const float *Sw, int n) {
    if (n < 3) return 0;
    int best = 0; float bv = -1e30f;
    for (int i = 1; i < n - 1; i++)
        if (Sw[i] >= Sw[i - 1] && Sw[i] > Sw[i + 1] && Sw[i] > bv) { bv = Sw[i]; best = i; }
    return best;
}

/* ============================================================================================
 * COMBINE STAGE HELPERS (unchanged from combo13_opt)
 * ============================================================================================ */
static float bpm_favor_center(float bpm) {
    if (bpm <= TENT_PEAK_BPM) {
        float t = (bpm - 40.0f) / (TENT_PEAK_BPM - 40.0f);
        if (t < 0) t = 0; if (t > 1) t = 1;
        return TENT_LO_H + t * (TENT_PEAK_H - TENT_LO_H);
    }
    float t = (bpm - TENT_PEAK_BPM) / (200.0f - TENT_PEAK_BPM);
    if (t < 0) t = 0; if (t > 1) t = 1;
    return TENT_PEAK_H + t * (TENT_HI_H - TENT_PEAK_H);
}
/* CROSS-SIGNAL HARMONIC-RATIO CHECK (2026-08-09), idea #1 of Daniel's "any other ideas to detect
 * locking on to harmonics or subharmonics?" follow-up -- INVESTIGATED AND REJECTED before writing
 * any mechanism, on corpus-wide evidence, not just the anecdote that motivated it.
 * Hypothesis: autocorrelation (this file's raw candidate, from the detrended curve) is classically
 * prone to locking onto SUBHARMONICS (too slow) while FFT/spectral methods (flux, the agreement
 * signal in agree_bpm above) are classically prone to locking onto HARMONICS (too fast) -- a
 * directional asymmetry that, if real, could tell teal_size_factor() which side to trust whenever
 * the candidate and flux disagree by ~2x/0.5x/3x/(1/3)x, extending the existing agree_bpm check
 * beyond a direct-match tolerance.
 * Motivating anecdote: daniel_recovery_test.f32 at t=25.50-26.25s -- raw candidate stuck at
 * 160bpm (wrong) while flux sat at 53.3-56.5bpm (correct, ~54.5bpm true), an almost-exact 3x
 * mismatch, a full second before the candidate itself corrected and the display followed.
 * Full 314-file real+selfrecorded corpus (18,366 candidate+flux-valid ticks; harmonic_check_
 * extract.py/harmonic_check_analyze.py, since deleted): ticks where candidate/flux disagreed by a
 * harmonic ratio (tol 5%) = 469 (2.6% of all ticks). Of those 469: candidate was the correct one
 * 342 times (73%), flux was correct only 97 times (21%), neither 30 times -- the OPPOSITE of a
 * coin-flip that would justify a voting/trust rule, and the opposite direction of the single
 * anecdote above. Splitting by magnitude instead of by signal identity (higher-rate side correct
 * 241 vs lower-rate side correct 198) is close to a coin flip too -- no exploitable "always trust
 * the higher/lower one" shortcut either.
 * The anecdote wasn't wrong, just unrepresentative: restricting to the 92 disagreement ticks where
 * the DISPLAYED value was actually wrong (the only ticks where intervening could help), flux was
 * the correct side 65 times (71%) vs candidate 5 times (5%) -- but a real mechanism only has
 * (candidate, flux, their ratio) to work with, not foreknowledge of whether the current display
 * happens to be wrong, so it cannot distinguish this favorable 92-tick subset from the other 377
 * (80%) disagreement ticks where the display was already correct via the candidate. Any rule that
 * shifts weight toward flux whenever a harmonic-ratio disagreement is detected fires on all 469
 * ticks alike, and would break far more already-correct candidate answers (342) than it would fix
 * already-wrong ones (97). NOT PURSUED -- no mechanism added, agree_bpm's existing direct-match
 * check (AGREEMENT_BPM_TOL/AGREE_BONUS above) is left as the only cross-signal agreement logic. */
static float teal_size_factor(float bpm, bool agree_valid, float agree_bpm,
                               bool curve_corr_valid, float curve_corr,
                               bool tmpl_corr_valid, float tmpl_corr) {
    bool agrees = agree_valid && fabsf(agree_bpm - bpm) <= AGREEMENT_BPM_TOL;
    float factor = bpm_favor_center(bpm);
    if (agrees) factor *= AGREE_BONUS;
    /* CURVE-CORRELATION SOFT MULTIPLIER (2026-08-09) -- PROTOTYPED AND REJECTED, see
     * curve_corr_log's own struct comment in combo13_stream_internal.h for the full corpus
     * evidence (every LO/HI range tried came back flat-to-slightly-worse). OPT-IN ONLY via
     * CURVE_WEIGHT_ENABLE, zero cost in the real shipped build. */
#ifdef CURVE_WEIGHT_ENABLE
    float cw_sub = curve_corr_valid ? (curve_corr - 0.65f) / 0.33f : 0.5f;
    if (cw_sub < 0.0f) cw_sub = 0.0f; if (cw_sub > 1.0f) cw_sub = 1.0f;
    factor *= CURVE_WEIGHT_LO + cw_sub * (CURVE_WEIGHT_HI - CURVE_WEIGHT_LO);
#else
    (void)curve_corr_valid; (void)curve_corr;
#endif
    /* MORPHOLOGY-TEMPLATE SOFT MULTIPLIER (2026-08-09), see TEMPLATE_WEIGHT_ENABLE's own comment
     * above for why this exists as a separate attempt from TEMPLATE_MATCH_ENABLE's rejected hard
     * veto. Pivot/span (0.30/0.40) chosen so corr=0.30 (the hard-veto's own validated threshold)
     * maps to sub=0 and corr=0.70 (roughly the mean for genuinely correct ticks, per
     * autocorr_template_library_prototype.py) maps to sub=1 -- a reasonable starting point for a
     * sweep, not itself validated. OPT-IN ONLY via TEMPLATE_WEIGHT_ENABLE, zero cost in the real
     * shipped build. */
#ifdef TEMPLATE_WEIGHT_ENABLE
    float tw_sub = tmpl_corr_valid ? (tmpl_corr - 0.30f) / 0.40f : 0.5f;
    if (tw_sub < 0.0f) tw_sub = 0.0f; if (tw_sub > 1.0f) tw_sub = 1.0f;
    factor *= TEMPLATE_WEIGHT_LO + tw_sub * (TEMPLATE_WEIGHT_HI - TEMPLATE_WEIGHT_LO);
#else
    (void)tmpl_corr_valid; (void)tmpl_corr;
#endif
    return factor;
}
static float weighted_median(const float *vals, const float *weights, int count) {
    int idx[WEIGHTED_MED_TRAILING_N];
    for (int i = 0; i < count; i++) idx[i] = i;
    for (int i = 0; i < count; i++) {
        int best = i;
        for (int j = i + 1; j < count; j++) if (vals[idx[j]] < vals[idx[best]]) best = j;
        int tmp = idx[i]; idx[i] = idx[best]; idx[best] = tmp;
    }
    float total = 0.0f;
    for (int i = 0; i < count; i++) total += weights[i];
    if (total <= 0.0f) {
        if (count % 2) return vals[idx[count / 2]];
        return 0.5f * (vals[idx[count / 2 - 1]] + vals[idx[count / 2]]);
    }
    float half = total / 2.0f;
    float cum = 0.0f;
    for (int i = 0; i < count; i++) {
        cum += weights[idx[i]];
        if (cum >= half) return vals[idx[i]];
    }
    return vals[idx[count - 1]];
}
static int required_teal_delay_ticks(float bpm, float time_since_wait_end, bool is_first) {
    float taper_s = is_first ? RAMP_DELAY_TAPER_S_FIRST : RAMP_DELAY_TAPER_S;
    float extra_ticks = is_first ? RAMP_DELAY_EXTRA_TICKS_FIRST : RAMP_DELAY_EXTRA_TICKS;
    float slope = is_first ? TEAL_BPM_DELAY_SLOPE_FIRST : TEAL_BPM_DELAY_SLOPE;
    float over = bpm - 60.0f; if (over < 0) over = 0;
    int t = (int)ceilf(1.0f + slope * over);
    if (time_since_wait_end < taper_s) {
        float frac = (taper_s - time_since_wait_end) / taper_s;
        t += (int)ceilf(extra_ticks * frac);
    }
    return t < 1 ? 1 : t;
}

/* ============================================================================================
 * STREAMING FRONT-END: chunked artifact-removal + declick + spike-eventing, then streaming
 * decimation. Shared logic for whichever rate detection runs at (native for variant 1,
 * decimated for variants 2/3) via generic buffer/size parameters -- only ONE of the two call
 * sites in c13stream_push() below is compiled per variant, per DETECT_AT_NATIVE.
 * ============================================================================================ */
#define _MAXA (NATIVE_CARRY_N + NATIVE_CHUNK_N)
#define _MAXB (DEC_CARRY_N + DEC_CHUNK_N)
#define MAX_CHUNK_TOTAL (_MAXA > _MAXB ? _MAXA : _MAXB)

/* out_dec_cb: callback-style emission -- appends the newly-cleaned chunk into whatever the
 * caller needs (dec_hist directly for variants 2/3, or the streaming AA-decimate for variant 1). */
typedef void (*ChunkEmitFn)(struct Combo13StreamState *st, const float *cleaned_chunk, int chunk_n);

static void process_and_emit_chunk(struct Combo13StreamState *st, float *buf, int carry_n, int chunk_n,
                                     float rate_hz, int pad_samples, ChunkEmitFn emit) {
    int total = carry_n + chunk_n;
    bool scratch_grown[MAX_CHUNK_TOTAL];
    float scratch_absx[MAX_CHUNK_TOTAL];
    float raw_chunk[MAX_CHUNK_TOTAL];
    memcpy(raw_chunk, buf + carry_n, chunk_n * sizeof(float));
#ifdef COMBO13_EXPOSE_DEBUG
    /* SPIKE-DETECT MASK (2026-08-07): debug-only, see combo13_stream_internal.h's comment on
     * dbg_decpost_spike_ring. Marks every sample in a detected run, not just the loudest one --
     * matches what the scan below actually considers "part of the spike," not just its peak. */
    bool scratch_spike[MAX_CHUNK_TOTAL];
    memset(scratch_spike, 0, sizeof(scratch_spike));
#endif

    /* spike detection (event-timing only, does not modify the signal) -- uses the median
     * baseline as it stood BEFORE this chunk (consistent with the artifact detector's own use
     * of the pre-chunk baseline), scanning just the NEW portion (buf[carry_n..total)).
     * st->chunk_time_cursor is the wall-clock time of buf[carry_n] (the start of the new
     * portion) on the SAME timeline c13stream_tick()'s `el` advances -- each detected run's
     * peak sample converts to absolute time via that cursor plus its offset into the chunk. */
    if (st->med_baseline.initialized) {
        float med_scale = st->med_baseline.ema * 1.4826f;
        if (med_scale == 0.0f) med_scale = 1e-6f;
        float lim = SPIKE_DETECT_MULT * med_scale;
        float max_t = NEG_INF;
        bool any_spike = false;
        int i = carry_n;
        while (i < total) {
            if (fabsf(buf[i]) <= lim) { i++; continue; }
            int j = i; float best_v = -1.0f; int best_local = i;
            while (j < total && fabsf(buf[j]) > lim) {
                float av = fabsf(buf[j]);
                if (av > best_v) { best_v = av; best_local = j; }
                j++;
            }
            float spike_t = st->chunk_time_cursor + (float)(best_local - carry_n) / rate_hz;
            if (spike_t > max_t) max_t = spike_t;
            any_spike = true;
#ifdef COMBO13_EXPOSE_DEBUG
            for (int k = i; k < j; k++) scratch_spike[k] = true;
#endif
            i = j;
        }
        if (any_spike && max_t > st->last_logged_spike_t + 0.01f) {
            st->last_spike_el = max_t;
            st->last_logged_spike_t = max_t;
        }
    }

    /* -- artifact detect/extend/interpolate, using the PRE-chunk baseline (established from many
     * prior chunks, not re-derived from this potentially-contaminated one) -- */
    float art_scale = 1e-6f;
#ifdef COMBO13_EXPOSE_DEBUG
    /* Captured HERE, before baseline_update() below can flip .initialized from false to true for
     * the first time -- reading st->art_baseline.initialized any later would wrongly imply
     * stream_detect_extend_interp() ran this chunk on the very first chunk that actually
     * initializes the baseline. Same reasoning for med_scale_for_debug below (med_baseline.ema
     * itself, not just the flag, changes inside baseline_update()). */
    bool dbg_art_detect_ran = st->art_baseline.initialized;
    float dbg_declick_lim = 0.0f;
#endif
    if (st->art_baseline.initialized) {
        art_scale = st->art_baseline.ema * 1.4826f;
        if (art_scale == 0.0f) art_scale = 1e-6f;
        stream_detect_extend_interp(buf, total, art_scale, ARTIFACT_DETECT_MULT, ARTIFACT_EXTEND_MULT,
                                      pad_samples, scratch_grown, scratch_absx);
    }

    /* -- declick clip, using the median baseline (established pre-chunk) -- */
    if (st->med_baseline.initialized) {
        float med_scale = st->med_baseline.ema * 1.4826f;
        if (med_scale == 0.0f) med_scale = 1e-6f;
        declick_clip(buf, total, med_scale, TRANSIENT_LIMIT_MULT);
#ifdef COMBO13_EXPOSE_DEBUG
        dbg_declick_lim = TRANSIENT_LIMIT_MULT * med_scale;
#endif
    }

    /* -- update both baselines from the RAW (pre-cleaning) chunk, not the artifact-removed/
     * declicked output -- both percentiles used here (0.4231, 0.5/median) are already robust to
     * a MINORITY of contaminated samples within one ~100ms chunk, the same robustness
     * combo13_opt's whole-window percentile relies on (it recomputes from the real current raw
     * window every tick, never from previously-censored output). Updating from the cleaned chunk
     * instead was a real bug: during a sustained quiet stretch both baselines decay toward the
     * quiet amplitude, and when real signal resumes, every sample looks like a giant artifact
     * relative to that collapsed threshold -- gets suppressed -- and the baseline then only ever
     * sees the suppressed near-zero result, permanently locking out real signal. Found via a
     * loud-quiet-loud recovery test that no prior corpus/synthetic-gap test had exercised. */
    float chunk_duration_s = (float)chunk_n / rate_hz;
    float baseline_alpha = expf(-chunk_duration_s / BASELINE_TAU_S);
    baseline_update(&st->art_baseline, raw_chunk, chunk_n, ARTIFACT_BASELINE_PCTL, chunk_duration_s, baseline_alpha, scratch_absx);
    baseline_update(&st->med_baseline, raw_chunk, chunk_n, 0.5f, chunk_duration_s, baseline_alpha, scratch_absx);

#ifdef COMBO13_EXPOSE_DEBUG
    /* A4 (2026-08-07): the SAME chunk, AFTER artifact-removal+declick above -- this is what
     * actually reaches stream_dec_samples/the band-envelope pipeline. See
     * combo13_stream_internal.h's own comment on dbg_decpost_ring. Only defined for the
     * DETECT_AT_NATIVE==0 variants this project actually ships/visualizes (2/3/4) -- DEC_MAX_V
     * sizing assumes the decimated rate, not native, so this call site would need its own native-
     * rate ring if variant 1 (DETECT_AT_NATIVE==1) is ever revisited for this panel.
     * OVERLAY FLAGS (2026-08-07, per Daniel's request to see WHERE artifact-removal/declick each
     * fire): scratch_grown[] is stream_detect_extend_interp's own per-sample "was interpolated"
     * mask (still valid here, nothing else in this function reuses that buffer before this point)
     * -- only meaningful if the artifact detector actually ran THIS chunk (dbg_art_detect_ran,
     * captured BEFORE baseline_update() above could flip .initialized true for the first time),
     * else scratch_grown holds a stale/unrelated mask from whenever it last ran and must be
     * ignored. declick's own mask isn't tracked by declick_clip itself (would need a signature
     * change) -- reconstructed instead from dbg_declick_lim (the exact limit declick_clip used,
     * captured at the same call site before med_baseline.ema could change) by checking whether
     * the post-clip sample landed exactly on it. */
#if !DETECT_AT_NATIVE
    for (int _i = 0; _i < chunk_n; _i++) {
        int _pos = carry_n + _i;
        float _v = buf[_pos];
        st->dbg_decpost_ring[st->dbg_decpost_total % DEC_MAX_V] = _v;
        st->dbg_decpost_artifact_ring[st->dbg_decpost_total % DEC_MAX_V] =
            (dbg_art_detect_ran && scratch_grown[_pos]) ? 1.0f : 0.0f;
        st->dbg_decpost_declick_ring[st->dbg_decpost_total % DEC_MAX_V] =
            (dbg_declick_lim > 0.0f && fabsf(_v) >= dbg_declick_lim - 1e-6f) ? 1.0f : 0.0f;
        st->dbg_decpost_spike_ring[st->dbg_decpost_total % DEC_MAX_V] = scratch_spike[_pos] ? 1.0f : 0.0f;
        st->dbg_decpost_total++;
    }
#endif
#endif
    /* -- emit the cleaned new chunk, then shift the tail into the carry region -- */
    emit(st, buf + carry_n, chunk_n);
    memmove(buf, buf + chunk_n, carry_n * sizeof(float));
    st->chunk_time_cursor += chunk_duration_s;
}

/* ============================================================================================
 * STREAMING DECIMATION (continuous AA filter + stride-keep, same technique already validated
 * for the pitch-bandpass filter in task 7 -- carries state across calls, never resets)
 * ============================================================================================ */
static int streaming_decimate(struct Combo13StreamState *st, const float *samples, int n, float *out) {
    int m = 0;
    for (int i = 0; i < n; i++) {
        float y = iir4_step(AA_B, AA_A, samples[i], st->dec_aa_z);
        if (st->dec_stride_phase == 0) {
            out[m++] = y;
#ifdef COMBO13_EXPOSE_DEBUG
            /* A3 (2026-08-07): the decimated-rate signal at the exact moment it's produced, BEFORE
             * any chunk-based artifact removal/declick -- see combo13_stream_internal.h's own
             * comment on dbg_decpre_ring. */
            st->dbg_decpre_ring[st->dbg_decpre_total % DEC_MAX_V] = y;
            st->dbg_decpre_total++;
#endif
        }
        st->dec_stride_phase = (st->dec_stride_phase + 1) % DEC_STRIDE;
    }
    return m;
}

#if DETECT_AT_NATIVE
static void emit_native_cleaned_chunk(struct Combo13StreamState *st, const float *cleaned_chunk, int chunk_n) {
    float dec_out[NATIVE_CHUNK_N];
    int m = streaming_decimate(st, cleaned_chunk, chunk_n, dec_out);
    stream_dec_samples(st, dec_out, m);
}
#else
static void emit_dec_cleaned_chunk(struct Combo13StreamState *st, const float *cleaned_chunk, int chunk_n) {
    stream_dec_samples(st, cleaned_chunk, chunk_n);
}
#endif

/* ============================================================================================
 * PUBLIC API
 * ============================================================================================ */
void c13stream_init(Combo13StreamState *st) {
    memset(st, 0, sizeof(*st));
    st->last_onset_el = NEG_INF;
    /* last_spike_el/flatline_clear_el anchored to 0.0f, not NEG_INF (2026-08-07, onset retirement):
     * with onset no longer in last_event_el's max() below, one of these two has to supply a real
     * t=0 reference at true cold start or since_event=el-NEG_INF pins win_sec at SINGLE_WIN_MAX from
     * tick 1 (corpus-confirmed regression, see the comment at last_event_el's computation). Onset
     * used to provide this by accident (its ~1s debounce firing near real stream start); anchoring
     * here directly is more precise (t=0 exactly, not ~0.75-1s in) and no longer depends on onset. */
    st->last_spike_el = 0.0f;
    st->last_logged_spike_t = NEG_INF;
    st->flatline_clear_el = 0.0f;
    st->was_flatline = false;
    st->prev_last_event_el = NEG_INF;
    st->peak_mean_below_since_el = NEG_INF;
    st->frozen_wait_s = CONS_WAIT_S;
    for (int i = 0; i < S_LEN; i++) st->lag_pow_recip[i] = 1.0f / powf((float)(LO_LAG + i), LAG_POW);
    baseline_init(&st->art_baseline);
    baseline_init(&st->med_baseline);
    for (int i = 0; i < NLOG_V; i++) { st->cons_buf_log[i].valid = false; st->agree_log[i].valid = false; }
}

void c13stream_push(Combo13StreamState *st, const float *samples, int n) {
    for (int i = 0; i < n; i++) {
#if DETECT_AT_NATIVE
        st->native_chunk_buf[NATIVE_CARRY_N + st->native_chunk_len] = samples[i];
        st->native_chunk_len++;
        if (st->native_chunk_len == NATIVE_CHUNK_N) {
            process_and_emit_chunk(st, st->native_chunk_buf, NATIVE_CARRY_N, NATIVE_CHUNK_N,
                                     FS_IN, ARTIFACT_PAD_SAMPLES_V, emit_native_cleaned_chunk);
            st->native_chunk_len = 0;
        }
#else
        float dec_sample;
        int got = streaming_decimate(st, &samples[i], 1, &dec_sample);
        if (got) {
            st->dec_chunk_buf[DEC_CARRY_N + st->dec_chunk_len] = dec_sample;
            st->dec_chunk_len++;
            if (st->dec_chunk_len == DEC_CHUNK_N) {
                process_and_emit_chunk(st, st->dec_chunk_buf, DEC_CARRY_N, DEC_CHUNK_N,
                                         DEC_HZ, ARTIFACT_PAD_SAMPLES_V, emit_dec_cleaned_chunk);
                st->dec_chunk_len = 0;
            }
        }
#endif
    }
}

static void log_push(Combo13StreamState *st, bool val_valid, float val, bool conf_valid, float conf,
                      bool agree_valid, float agree, bool curve_corr_valid, float curve_corr,
                      bool tmpl_corr_valid, float tmpl_corr) {
    int i = st->log_head;
    st->cons_buf_log[i].valid = val_valid;
    st->cons_buf_log[i].val = val;
    st->cons_buf_log[i].conf_valid = conf_valid;
    st->cons_buf_log[i].conf = conf;
    st->agree_log[i].valid = agree_valid;
    st->agree_log[i].val = agree;
#ifdef CURVE_WEIGHT_ENABLE
    st->curve_corr_log[i].valid = curve_corr_valid;
    st->curve_corr_log[i].val = curve_corr;
#else
    (void)curve_corr_valid; (void)curve_corr;
#endif
#ifdef TEMPLATE_WEIGHT_ENABLE
    st->template_corr_log[i].valid = tmpl_corr_valid;
    st->template_corr_log[i].val = tmpl_corr;
#else
    (void)tmpl_corr_valid; (void)tmpl_corr;
#endif
    st->log_head = (i + 1) % NLOG_V;
    if (st->log_count < NLOG_V) st->log_count++;
}

Combo13StreamTick c13stream_tick(Combo13StreamState *st) {
    Combo13StreamTick out; memset(&out, 0, sizeof(out));
    float el = st->el;
    out.t = el;

    int hist_len = st->dec_sample_count;

#ifdef COMBO13_EXPOSE_DEBUG
    /* PART A PANELS (2026-08-07): gather each continuously-filled ring's trailing window into a
     * plain linear snapshot, once per tick -- see combo13_stream_internal.h's own comment on
     * dbg_decpre_ring/dbg_decpost_ring. Independent of everything else this tick computes, so
     * placed here, together, rather than scattered near whatever else happens to be nearby.
     * (A2's own tilt-bandpass ring was removed with tilt itself, revision 0.4, 2026-08-08.) */
    {
        long n = st->dbg_decpre_total; if (n > DEC_MAX_V) n = DEC_MAX_V;
        long first = st->dbg_decpre_total - n;
        for (long i = 0; i < n; i++) st->dbg_decpre_trace[i] = st->dbg_decpre_ring[(first + i) % DEC_MAX_V];
        st->dbg_decpre_trace_len = (int)n;
    }
    {
        long n = st->dbg_decpost_total; if (n > DEC_MAX_V) n = DEC_MAX_V;
        long first = st->dbg_decpost_total - n;
        for (long i = 0; i < n; i++) {
            long src = (first + i) % DEC_MAX_V;
            st->dbg_decpost_trace[i] = st->dbg_decpost_ring[src];
            /* A4 overlay flags (2026-08-07) -- gathered in lockstep with dbg_decpost_trace above,
             * same index/length, so JS never needs a second position mapping for these. */
            st->dbg_decpost_artifact_trace[i] = st->dbg_decpost_artifact_ring[src];
            st->dbg_decpost_declick_trace[i] = st->dbg_decpost_declick_ring[src];
            st->dbg_decpost_spike_trace[i] = st->dbg_decpost_spike_ring[src];
        }
        st->dbg_decpost_trace_len = (int)n;
    }
#endif

    /* -- onset/sig_level: broad_env over a SHORT recent window (not the whole ring) -- only the
     * trailing ~1.5s RMS is ever used, so gathering/filtering the full 8s history here (as
     * combo13_opt's non-consolidated chain did) would be pure waste. Reasoned simplification,
     * not yet corpus-validated in isolation. sig_win is hoisted out of this block (2026-08-06)
     * so the peak-mean flatline check below can reuse the exact same short window -- see that
     * block's comment for why. */
    float sig_level = 0.0f;
    int sig_win = 0;
    if (hist_len > 0) {
        sig_win = (int)(2.0f * DEC_HZ); if (sig_win > hist_len) sig_win = hist_len;
        int m = broad_env(st, sig_win, st->scratch_shared);
        int want = (int)lroundf(1.5f * FFS); int n_trail = m; if (want < n_trail) n_trail = want;
        if (n_trail > 0) {
            float sumsq = 0.0f;
            for (int i = m - n_trail; i < m; i++) sumsq += st->scratch_shared[i] * st->scratch_shared[i];
            sig_level = sqrtf(sumsq / (float)n_trail);
        }
    }
#ifdef COMBO13_EXPOSE_DEBUG
    /* B1 (2026-08-07): sig_level itself, for the onset/signal-level check panel -- valid whenever
     * hist_len>0 (the same condition gating the block above; sig_level defaults to 0.0f/meaningless
     * otherwise, matching the real algorithm's own treatment). */
    st->dbg_sig_level = sig_level;
    st->dbg_sig_level_valid = hist_len > 0;
#endif

    float signal_offset_thresh = SIGNAL_ONSET_THRESH * 0.5f;
    if (st->sig_was_onset_ok) {
        st->onset_on_streak = 0;
        if (sig_level < signal_offset_thresh) {
            st->onset_off_streak++;
            if (st->onset_off_streak >= ONSET_DEBOUNCE_TICKS) st->sig_was_onset_ok = false;
        } else st->onset_off_streak = 0;
    } else {
        st->onset_off_streak = 0;
        if (sig_level >= SIGNAL_ONSET_THRESH) {
            if (st->onset_on_streak == 0) st->onset_candidate_el = el;
            st->onset_on_streak++;
            if (st->onset_on_streak >= ONSET_DEBOUNCE_TICKS) {
                st->last_onset_el = st->onset_candidate_el;
                st->sig_was_onset_ok = true;
                st->onset_on_streak = 0;
            }
        } else st->onset_on_streak = 0;
    }

    /* Onset (sig_was_onset_ok/last_onset_el, computed just above) retired from event-timing
     * (2026-08-07): SIGNAL_ONSET_THRESH is calibrated to reject only true digital silence, far
     * below any real noise floor, so onset_off_streak effectively never fires on real hardware --
     * last_onset_el freezes near stream-start and never contributes a fresh RECONNECT event again.
     * It was silently dead weight for that purpose the entire streaming-port lifetime. Its OTHER,
     * accidental job -- anchoring since_event to a sane value at true cold start, before any real
     * spike/flatline event has ever fired -- is now covered directly by last_spike_el/
     * flatline_clear_el initializing to 0.0f instead of NEG_INF (c13stream_init above) rather than
     * depending on onset's incidental ~1s-debounce timing. Kept computing sig_was_onset_ok/
     * last_onset_el (unused here now) since pcg_visualizer's B1/C0/B2 debug panels still read them
     * to show the underlying gate's real (nearly-always-on) behavior. */
    float last_event_el = st->last_spike_el;
    if (st->flatline_clear_el > last_event_el) last_event_el = st->flatline_clear_el;
    float since_event = el - last_event_el;

    float adaptive_tau = since_event;
    if (adaptive_tau < DECAY_ADAPTIVE_TAU_MIN) adaptive_tau = DECAY_ADAPTIVE_TAU_MIN;
    if (adaptive_tau > DECAY_ADAPTIVE_TAU_MAX) adaptive_tau = DECAY_ADAPTIVE_TAU_MAX;
#ifdef COMBO13_EXPOSE_DEBUG
    /* B3 (2026-08-07): adaptive_tau is always defined (a plain clamp of since_event, no validity
     * flag needed) -- r_adaptive_bpm/valid below default to invalid until proven otherwise inside
     * the `if` block, same pattern as every other conditionally-computed debug signal here. */
    st->dbg_adaptive_tau = adaptive_tau;
    st->dbg_r_adaptive_valid = false;
    st->dbg_r_adaptive_bpm = 0.0f;
#endif

    /* r_adaptive preview -- sizes the wait timer only, never displayed. Uses whatever history is
     * available so far (up to the full ring), same intent as combo13_opt's own version. */
    bool r_adaptive_valid = false; float r_adaptive_bpm = 0.0f;
    if (hist_len >= 3 * (int)DEC_HZ) {
        float S[S_LEN] = {0};
        float tau_frames = adaptive_tau * FFS;
        for (int b = 0; b < 3; b++) {
            /* identical math to feat_norm_clip(st, hist_len, b, FEAT_CLIP, ...) -- called directly
             * now that it no longer needs a scratch_work gather+filter+envelope pass first
             * (2026-08-05, RAM/clock pass 4), removing what used to be a hand-duplicated copy of
             * that function's body. */
            int m = feat_norm_clip(st, hist_len, b, FEAT_CLIP, st->scratch_shared);
            /* RAM/CLOCK PASS (2026-08-10), per Daniel's "any other clock cycle optimizations?" --
             * was m separate expf() calls (up to FEAT_MAX_V per band, 3 bands/tick). tau_frames is
             * fixed across this whole loop (only varies tick-to-tick, not sample-to-sample), so the
             * weights form an exact geometric sequence: w[i+1]/w[i] = exp(-(m-1-(i+1))/tau_frames) /
             * exp(-(m-1-i)/tau_frames) = exp(1/tau_frames), a single constant ratio. Reduces to 2
             * expf() calls (endpoint + ratio) plus m-1 multiplies, down from m. Introduces ordinary
             * float rounding from the repeated multiply instead of m independent evaluations, but the
             * sequence is smoothly monotonic (no cancellation/blowup risk) -- corpus-validated
             * byte-identical DISPLAYED-bpm classification against the direct-expf() version (314
             * files, 0 changed) before trusting it. */
            float w[FEAT_MAX_V];
            if (m > 0) {
                float ratio = expf(1.0f / tau_frames);
                w[0] = expf(-(float)(m - 1) / tau_frames);
                for (int i = 1; i < m; i++) w[i] = w[i - 1] * ratio;
            }
            float r0 = 0.0f, r0w = 0.0f;
            for (int i = 0; i < m; i++) { r0 += w[i] * st->scratch_shared[i] * st->scratch_shared[i]; r0w += w[i]; }
            r0 = r0 / (r0w != 0.0f ? r0w : 1.0f);
            if (m > HI_LAG) {
                for (int l = LO_LAG; l < HI_LAG; l++) {
                    int nn = m - l;
                    float s = 0.0f, sw = 0.0f;
                    for (int i = 0; i < nn; i++) { s += w[i] * st->scratch_shared[i] * st->scratch_shared[i + l]; sw += w[i]; }
                    S[l - LO_LAG] += (s / (sw != 0.0f ? sw : 1.0f)) / (r0 + 1e-9f);
                }
            }
        }
        float Sw[S_LEN];
        for (int l = LO_LAG; l < HI_LAG; l++) Sw[l - LO_LAG] = S[l - LO_LAG] * st->lag_pow_recip[l - LO_LAG];
        float swmin = Sw[0]; for (int i = 1; i < S_LEN; i++) if (Sw[i] < swmin) swmin = Sw[i];
        for (int i = 0; i < S_LEN; i++) Sw[i] -= swmin;
        int p1 = -1; float bv = -1e30f;
        for (int i = 1; i < S_LEN - 1; i++)
            if (Sw[i] >= Sw[i - 1] && Sw[i] > Sw[i + 1] && Sw[i] > bv) { bv = Sw[i]; p1 = i; }
        r_adaptive_bpm = (p1 < 0) ? 60.0f / ((float)LO_LAG / FFS) : 60.0f / (((float)LO_LAG + p1) / FFS);
        r_adaptive_valid = true;
#ifdef COMBO13_EXPOSE_DEBUG
        st->dbg_r_adaptive_bpm = r_adaptive_bpm;
        st->dbg_r_adaptive_valid = true;
#endif
    }

    float win_sec = SINGLE_WIN_MIN + (since_event - st->frozen_wait_s > 0 ? since_event - st->frozen_wait_s : 0.0f);
    if (win_sec < SINGLE_WIN_MIN) win_sec = SINGLE_WIN_MIN;
    if (win_sec > SINGLE_WIN_MAX) win_sec = SINGLE_WIN_MAX;
    int win_nsamp = (int)lroundf(win_sec * DEC_HZ);
#ifdef STREAM_DEBUG
    fprintf(stderr, "DBG t=%.2f win_nsamp=%d since_event=%.2f frozen_wait_s=%.2f last_onset_el=%.2f last_spike_el=%.2f med_ema=%.3f art_ema=%.3f\n",
            el, win_nsamp, since_event, st->frozen_wait_s, st->last_onset_el, st->last_spike_el,
            st->med_baseline.ema, st->art_baseline.ema);
#endif

    EstimateResult win_r; win_r.valid = false;
    if (hist_len >= win_nsamp && win_nsamp > 0) {
        win_r = estimate_sumenv_robust(st, win_nsamp, ROBUST_TRIM_FRAC, FEAT_CLIP);
    }
    EstimateResult win_teal = estimate_detrend_from_mw(&win_r, TEAL_PUSH_HALF_WIDTH);

    /* CURVE-CORRELATION QUALITY GATE (2026-08-09) -- PROTOTYPED AND REJECTED as a hard veto, see
     * curve_gate_prev's own struct comment in combo13_stream_internal.h for the full corpus
     * evidence (even the strictest low-correlation band is still 63.3% correct -- a binary cutoff
     * on this signal discards far more good answers than bad ones, at any threshold tried).
     * OPT-IN ONLY via CURVE_GATE_ENABLE, zero cost in the real shipped build. Snapshot-and-compare
     * once every CURVE_CORR_GAP_TICKS ticks (4.0s): on the tick the counter rolls over, correlate
     * THIS tick's win_teal.Sw against the snapshot taken exactly CURVE_CORR_GAP_TICKS ticks ago,
     * then overwrite the snapshot with the current curve. Ticks in between a snapshot event just
     * carry forward the last-computed curve_gate_corr unchanged. */
#ifdef CURVE_GATE_ENABLE
    if (win_teal.valid) {
        st->curve_gate_snap_counter++;
        if (st->curve_gate_snap_counter >= CURVE_CORR_GAP_TICKS) {
            if (st->curve_gate_prev_valid) {
                float mean_a = 0.0f, mean_b = 0.0f;
                for (int i = 0; i < S_LEN; i++) { mean_a += win_teal.Sw[i]; mean_b += st->curve_gate_prev[i]; }
                mean_a /= (float)S_LEN; mean_b /= (float)S_LEN;
                float cov = 0.0f, var_a = 0.0f, var_b = 0.0f;
                for (int i = 0; i < S_LEN; i++) {
                    float da = win_teal.Sw[i] - mean_a, db = st->curve_gate_prev[i] - mean_b;
                    cov += da * db; var_a += da * da; var_b += db * db;
                }
                float denom = sqrtf(var_a * var_b);
                st->curve_gate_corr = (denom > 1e-9f) ? (cov / denom) : 0.0f;
                st->curve_gate_corr_valid = 1;
                /* Streak counts consecutive SNAPSHOT EVENTS (4s apart) below threshold, not ticks --
                 * incrementing per-tick here would let one bad 4s window blow past any STREAK_LIMIT
                 * within itself (curve_gate_corr is held constant for all 16 ticks until the next
                 * rollover), defeating the whole point of requiring SUSTAINED badness across multiple
                 * separate windows. Must live inside this rollover block, evaluated once per event. */
                if (st->curve_gate_corr < CURVE_GATE_THRESH) st->curve_gate_low_streak++;
                else st->curve_gate_low_streak = 0;
            }
            memcpy(st->curve_gate_prev, win_teal.Sw, sizeof(win_teal.Sw));
            st->curve_gate_prev_valid = 1;
            st->curve_gate_snap_counter = 0;
        }
    } else {
        st->curve_gate_prev_valid = 0;
        st->curve_gate_corr_valid = 0;
        st->curve_gate_snap_counter = 0;
        st->curve_gate_low_streak = 0;
    }
    bool curve_gate_veto = st->curve_gate_corr_valid && (st->curve_gate_low_streak >= CURVE_GATE_STREAK_LIMIT);
#else
    bool curve_gate_veto = false;
#endif

    /* CURVE-CORRELATION COMBINE WEIGHT (2026-08-09) -- PROTOTYPED AND REJECTED, see curve_corr_log's
     * own struct comment in combo13_stream_internal.h for the full corpus evidence. OPT-IN ONLY via
     * CURVE_WEIGHT_ENABLE, zero cost in the real shipped build. */
#ifdef CURVE_WEIGHT_ENABLE
    if (win_teal.valid) {
        st->curve_weight_snap_counter++;
        if (st->curve_weight_snap_counter >= CURVE_CORR_GAP_TICKS) {
            if (st->curve_weight_prev_valid) {
                float mean_a = 0.0f, mean_b = 0.0f;
                for (int i = 0; i < S_LEN; i++) { mean_a += win_teal.Sw[i]; mean_b += st->curve_weight_prev[i]; }
                mean_a /= (float)S_LEN; mean_b /= (float)S_LEN;
                float cov = 0.0f, var_a = 0.0f, var_b = 0.0f;
                for (int i = 0; i < S_LEN; i++) {
                    float da = win_teal.Sw[i] - mean_a, db = st->curve_weight_prev[i] - mean_b;
                    cov += da * db; var_a += da * da; var_b += db * db;
                }
                float denom = sqrtf(var_a * var_b);
                st->curve_weight_corr = (denom > 1e-9f) ? (cov / denom) : 0.0f;
                st->curve_weight_corr_valid = 1;
            }
            memcpy(st->curve_weight_prev, win_teal.Sw, sizeof(win_teal.Sw));
            st->curve_weight_prev_valid = 1;
            st->curve_weight_snap_counter = 0;
        }
    } else {
        st->curve_weight_prev_valid = 0;
        st->curve_weight_corr_valid = 0;
        st->curve_weight_snap_counter = 0;
    }
#endif
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_autocorr_valid = win_r.valid;
    if (win_r.valid) {
        memcpy(st->dbg_autocorr_raw, win_r.Sw, sizeof(win_r.Sw));
        st->dbg_autocorr_lo = win_r.lo;
    }
    st->dbg_win_teal_valid = win_teal.valid;
    st->dbg_win_teal_bpm = win_teal.valid ? win_teal.bpm : 0.0f;
    if (win_teal.valid) memcpy(st->dbg_win_teal_curve, win_teal.Sw, sizeof(win_teal.Sw));

    /* AUTOCORRELATION CURVE CORRELATION PROTOTYPE (2026-08-09), per Daniel's "does anything measure
     * the consistency of the autocorrelations frame to frame?" -- confirmed nothing did (win_r/
     * win_teal are fresh locals every tick, never persisted for cross-tick comparison). Pearson
     * correlation between THIS tick's win_teal.Sw (the ACTUAL post-detrend curve win_teal.bpm's
     * peak is picked from -- same "compare the real curve" discipline C2/C4 needed real bugs to get
     * right, not the pre-detrend win_r.Sw) and the curve from CURVE_CORR_GAP_TICKS ticks ago.
     * 1.0 = identical shape, 0 = unrelated, negative = inverted.
     * GAP=1 (comparing literally consecutive ticks) was the first cut shipped -- Daniel caught that
     * it read as "almost always at 1" in the live panel. Root-caused, not just re-tuned blind: the
     * main candidate's analysis window grows to SINGLE_WIN_MAX=8.0s and TICK_S=0.25s, so two
     * consecutive ticks' windows share (8.0-0.25)/8.0 = 96.9% of their samples once the window is at
     * steady state -- the metric was mostly measuring "the window barely moved," not genuine
     * curve-shape (in)stability, REGARDLESS of whether the true periodicity estimate was stable or
     * garbage. Confirmed empirically too: median correlation 0.97 across both real recordings,
     * 60-67% of ticks >=0.95. Widened the gap to CURVE_CORR_GAP_TICKS=16 (4.0s, ~50% fresh window
     * content between compared frames) after sweeping 1/4/8/16/32 -- see project memory
     * (project_pcg_embedded_c) for the full sweep numbers and why 16 was picked. Deliberately no
     * special-casing of flatline/event boundaries (a correlation spanning a real disconnect SHOULD
     * read as low -- that's the point of showing this standalone before deciding how to use it).
     * Invalid until CURVE_CORR_GAP_TICKS consecutive valid curves have accumulated in the ring
     * (cold start, right after a reset/gap, or window too short). */
    if (win_teal.valid && st->curve_ring_count >= CURVE_CORR_GAP_TICKS) {
        const float *old = st->curve_ring[st->curve_ring_head];
        float mean_a = 0.0f, mean_b = 0.0f;
        for (int i = 0; i < S_LEN; i++) { mean_a += win_teal.Sw[i]; mean_b += old[i]; }
        mean_a /= (float)S_LEN; mean_b /= (float)S_LEN;
        float cov = 0.0f, var_a = 0.0f, var_b = 0.0f;
        for (int i = 0; i < S_LEN; i++) {
            float da = win_teal.Sw[i] - mean_a, db = old[i] - mean_b;
            cov += da * db; var_a += da * da; var_b += db * db;
        }
        float denom = sqrtf(var_a * var_b);
        st->dbg_curve_corr = (denom > 1e-9f) ? (cov / denom) : 0.0f;
        st->dbg_curve_corr_valid = 1;
    } else {
        st->dbg_curve_corr_valid = 0;
        st->dbg_curve_corr = 0.0f;
    }
    if (win_teal.valid) {
        memcpy(st->curve_ring[st->curve_ring_head], win_teal.Sw, sizeof(win_teal.Sw));
        st->curve_ring_head = (st->curve_ring_head + 1) % CURVE_CORR_GAP_TICKS;
        if (st->curve_ring_count < CURVE_CORR_GAP_TICKS) st->curve_ring_count++;
    } else {
        st->curve_ring_count = 0;
        st->curve_ring_head = 0;
    }

    st->dbg_band_env_n = win_r.valid ? (win_nsamp / FRAME_DS) : 0;
    if (win_r.valid) {
        band_env_gather_trailing(st, 0, st->dbg_band_env_n, st->dbg_band0_env);
        band_env_gather_trailing(st, 1, st->dbg_band_env_n, st->dbg_band1_env);
        band_env_gather_trailing(st, 2, st->dbg_band_env_n, st->dbg_band2_env);
    }
#endif

    /* peak-mean flatline detector -- design otherwise unchanged from combo13_opt's 2026-08-04 fix,
     * but (2026-08-06) now computed over sig_win (the SAME short ~2s window sig_level itself uses,
     * hoisted above) instead of the shared win_nsamp (up to 8s). Root-caused via a real recording
     * (Daniel's "highcountbefore flatline" WAV): after an actual device removal, win_nsamp's top-
     * 10%-mean stays anchored to the still-resident PRE-removal loud stretch while the window's
     * overall mean falls as quiet samples dilute it -- so peak_minus_mean was observed to actually
     * INCREASE for several seconds post-removal (590->690 over ~6s in that recording) before
     * finally collapsing once the entire loud stretch slid out of the 8s window, only THEN
     * crossing PEAK_MEAN_DIFF_THRESH. That ~7s blind spot left win_teal (computed over the same
     * stale window) free to lock onto a spurious value with nothing to gate it. sig_level's own
     * onset detector already reacted almost instantly to the same removal (dropping well below
     * threshold within ~1-2 ticks) since it was already using a short window -- reusing that exact
     * window for peak-mean directly closes the gap without inventing a new tunable constant. */
    bool peak_mean_valid = false;
    float peak_minus_mean = 0.0f;
    if (sig_win > 0) {
        int m = broad_env(st, sig_win, st->scratch_shared);
        if (m > 0) {
            float sum = 0.0f;
            for (int i = 0; i < m; i++) sum += st->scratch_shared[i];
            float mean_abs = sum / (float)m;
            int top_n = (int)lroundf((float)m * PEAK_MEAN_TOP_PCT); if (top_n < 1) top_n = 1;
            int boundary = m - top_n;
            quickselectf(st->scratch_shared, m, boundary);
            float peak_sum = 0.0f;
            for (int i = boundary; i < m; i++) peak_sum += st->scratch_shared[i];
            float peak_level = peak_sum / (float)top_n;
            peak_minus_mean = peak_level - mean_abs;
            peak_mean_valid = true;
#ifdef STREAM_DEBUG
            fprintf(stderr, "DBG t=%.2f sig_level=%.6f hist_len=%d sig_win=%d peak_minus_mean=%.4f thresh=%.4f\n",
                    el, sig_level, hist_len, sig_win, peak_minus_mean, PEAK_MEAN_DIFF_THRESH);
#endif
        }
    }
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_peak_minus_mean = peak_minus_mean;
    st->dbg_peak_mean_valid = peak_mean_valid;
#endif
    bool peak_mean_below_now = (!peak_mean_valid) || (peak_minus_mean < PEAK_MEAN_DIFF_THRESH);
    if (peak_mean_below_now) {
        if (st->peak_mean_below_since_el == NEG_INF) st->peak_mean_below_since_el = el;
    } else {
        st->peak_mean_below_since_el = NEG_INF;
    }
    bool is_flatline = (st->peak_mean_below_since_el != NEG_INF) &&
                        (el - st->peak_mean_below_since_el) >= PEAK_MEAN_SUSTAIN_S;

    if (is_flatline) {
        for (int i = 0; i < NLOG_V; i++) st->cons_buf_log[i].valid = false;
    }
    /* is_flatline clearing has no sustain requirement (unlike setting it) -- a single tick's
     * good-looking peak-mean reading ends it, and brief few-second dips (breathing, momentary
     * weak contact) trip is_flatline fairly often even in otherwise-normal recordings -- corpus-
     * confirmed: treating EVERY such blip as a fresh first-contact-equivalent event was a severe
     * regression (5-10s bucket correct% collapsed to 0). Only flatline episodes that persisted at
     * least FLATLINE_FULL_RESET_MIN_S are treated as a genuine disconnect/reconnect (this is the
     * scenario a physical device removal actually produces -- many seconds, not one bad tick), and
     * only those fold into last_event_el for the full since_event/frozen_wait_s/ramp reset; shorter
     * blips still clear cons_buf_log above but don't force the expensive full reset. One-tick lag
     * vs. the since_event/win_sec computed earlier this tick, same as onset/spike events already
     * accept. */
    if (is_flatline && !st->was_flatline) st->flatline_became_true_el = el;
    if (st->was_flatline && !is_flatline) {
        float flatline_dur = el - st->flatline_became_true_el;
        /* st->ever_displayed guards against the initial cold-start bootstrap: every stream begins
         * flatline (no history yet) for several seconds by design, already fully covered by the
         * ordinary first-contact wait/ramp -- without this guard that bootstrap's own end looked
         * exactly like a "genuine reconnect" and chained a SECOND full wait right after the first,
         * corpus-confirmed as a severe regression (0-10s bucket collapsed to 100% no-display).
         * 2026-08-05: ever_displayed alone isn't enough -- if establishing contact takes so long
         * that it spans MULTIPLE flatline episodes before the first-ever valid display (poor/
         * intermittent contact, not a clean single bootstrap), ever_displayed stays false through
         * all of them, so a later episode's clear -- happening well after the original wait window
         * (`since_event`/`frozen_wait_s`, computed earlier this tick from the stale first event) has
         * already expired -- got the SAME bootstrap exemption as the true cold start, leaving zero
         * active wait/ramp protection by the time real contact finally lands (real recording from
         * Daniel: a ~150-158bpm harmonic lock displayed completely unguarded on true first contact,
         * true rate ~52bpm). Fixed by also treating it as fresh once the original wait has already
         * elapsed (`since_event>=frozen_wait_s`), regardless of ever_displayed -- this still exempts
         * the ordinary bootstrap (whose own end is normally well inside frozen_wait_s on real files)
         * while catching this slow-contact case. */
        bool past_original_wait = since_event >= st->frozen_wait_s;
        if ((st->ever_displayed || past_original_wait) && flatline_dur >= FLATLINE_FULL_RESET_MIN_S) st->flatline_clear_el = el;
    }
    st->was_flatline = is_flatline;

    bool is_new_event = (last_event_el != st->prev_last_event_el);
    if (is_new_event) {
        st->prev_last_event_el = last_event_el;
        st->frozen_wait_s = CONS_WAIT_S + (r_adaptive_valid ? CONS_WAIT_EXTRA_S_PER_BPM * fabsf(r_adaptive_bpm - 60.0f) : 0.0f);
    }

    /* FLUX agreement-signal computation (2026-08-08; sole agreement signal since tilt's removal,
     * revision 0.4, same day) -- uses the same autocorrelation range as the MAIN candidate,
     * LO_LAG/HI_LAG, since flux is derived from the decimated envelope at frame rate FFS. Only
     * computed when win_r's own window is already valid (same win_nsamp, reuses st->scratch_shared
     * which is free again by this point -- sum_env_norm fully consumes its own use of it before
     * returning). */
    bool flux_valid = false; float flux_bpm = 0.0f;
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_flux_autocorr_valid = 0;
    st->dbg_flux_trace_len = 0;
#endif
    if (win_r.valid) {
        int m = envelope_flux(st, win_nsamp, st->scratch_shared);
#ifdef COMBO13_EXPOSE_DEBUG
        /* Captured here, BEFORE either autocorrelation branch runs -- st->scratch_shared is still
         * the untouched flux trace at this point in both branches (the ROBUST branch below copies
         * it to flux_trace_copy before reusing st->scratch_shared as add_autocorr_robust's own
         * internal `prod` scratch, which would otherwise corrupt this capture if taken after). */
        if (m > 0) {
            int cap_m = m < FEAT_MAX_V ? m : FEAT_MAX_V;
            memcpy(st->dbg_flux_trace, st->scratch_shared, (size_t)cap_m * sizeof(float));
            st->dbg_flux_trace_len = cap_m;
        }
#endif
        if (m > HI_LAG) {
            float S[S_LEN] = {0};
#if FLUX_AC_ROBUST
            /* Can't autocorrelate st->scratch_shared in place under ROBUST -- add_autocorr_robust
             * reuses st->scratch_shared internally as its own `prod` scratch, which would alias and
             * corrupt the flux trace mid-computation. Copy first. */
            float flux_trace_copy[FEAT_MAX_V];
            memcpy(flux_trace_copy, st->scratch_shared, (size_t)m * sizeof(float));
            add_autocorr_robust(st, S, flux_trace_copy, m, LO_LAG, HI_LAG, ROBUST_TRIM_FRAC);
#else
            add_autocorr_plain(S, st->scratch_shared, m, LO_LAG, HI_LAG);
#endif
            float *curve = S;
#if FLUX_AC_LAGPOW
            float Sw_buf[S_LEN];
            for (int l = LO_LAG; l < HI_LAG; l++) Sw_buf[l - LO_LAG] = curve[l - LO_LAG] * st->lag_pow_recip[l - LO_LAG];
            curve = Sw_buf;
#endif
#if FLUX_AC_DETREND
            float det_buf[S_LEN];
            float dcsum[S_LEN + 1]; dcsum[0] = 0.0f;
            for (int i = 0; i < S_LEN; i++) dcsum[i + 1] = dcsum[i] + curve[i];
            for (int i = 0; i < S_LEN; i++) det_buf[i] = curve[i] - moving_average_at(dcsum, S_LEN, i, TEAL_PUSH_HALF_WIDTH);
            curve = det_buf;
#endif
            int pk = peak_idx_tallest_local_max(curve, S_LEN);
            flux_bpm = 60.0f * FFS / (float)(LO_LAG + pk);
            flux_valid = true;
#ifdef COMBO13_EXPOSE_DEBUG
            /* Captures `curve` (the ACTUAL post-lagpow/detrend curve flux_bpm's peak is picked
             * from), not the pre-transform `S` -- same class of fix C2/C4 needed real bugs to get
             * right; getting this wrong here would ship the exact same mismatch on day one. */
            memcpy(st->dbg_flux_autocorr, curve, sizeof(S));
            st->dbg_flux_autocorr_valid = 1;
#endif
        }
    }
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_flux_valid = flux_valid ? 1 : 0;
    st->dbg_flux_bpm = flux_bpm;
#endif
    bool agree_valid = flux_valid; float agree_bpm = flux_bpm;

    /* EARLY RELEASE streak (2026-08-05) -- see combo13_stream_internal.h's comment on
     * early_release_streak. Computed from win_teal/agree_valid, both already available every tick
     * regardless of suppression, so this adds no new per-tick computation. agree_valid/agree_bpm
     * is flux_valid/flux_bpm (the sole agreement signal since tilt's removal, revision 0.4,
     * 2026-08-08). */
    bool early_agree_now = win_teal.valid && agree_valid &&
                            fabsf(agree_bpm - win_teal.bpm) <= AGREEMENT_BPM_TOL;
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_early_agree_now = early_agree_now ? 1 : 0;
#endif
    if (is_flatline || is_new_event) st->early_release_streak = 0;
    else if (early_agree_now) st->early_release_streak++;
    else st->early_release_streak = 0;
    int   early_release_min_ticks = st->ever_displayed ? EARLY_RELEASE_MIN_TICKS : EARLY_RELEASE_MIN_TICKS_FIRST;
    float early_release_max_save_s = st->ever_displayed ? EARLY_RELEASE_MAX_SAVE_S : EARLY_RELEASE_MAX_SAVE_S_FIRST;
    bool early_release_ok = (st->early_release_streak >= early_release_min_ticks) &&
                             (since_event >= st->frozen_wait_s - early_release_max_save_s);

    bool suppress_circle = is_flatline || is_new_event ||
                            (since_event < st->frozen_wait_s && !early_release_ok);
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_suppress_circle = suppress_circle ? 1 : 0;
    st->dbg_early_release_ok = early_release_ok ? 1 : 0;
    st->dbg_frozen_wait_s = st->frozen_wait_s;
#endif
    bool raw_cand_valid = (!suppress_circle) && win_teal.valid;
    float raw_cand_bpm = raw_cand_valid ? win_teal.bpm : 0.0f;

#if defined(TEMPLATE_MATCH_GATED_ENABLE) && STREAM_VARIANT == 4
    /* Runs BEFORE harmonic_confirmed/jump-guards below, so everything downstream treats the
     * (possibly overridden) pick as the real candidate for this tick, same as any other raw_cand_bpm
     * value. See TEMPLATE_MATCH_GATED_ENABLE's own comment above for the full rationale/validation. */
    if (raw_cand_valid) {
        float gated_bpm;
        if (template_match_gated_pick(&win_r, &win_teal, &gated_bpm)) raw_cand_bpm = gated_bpm;
    }
#endif
#if defined(TEMPLATE_MATCH_GATED_V2_ENABLE) && STREAM_VARIANT == 4
    /* Same integration point as V1 above (runs BEFORE harmonic_confirmed/jump-guards, so everything
     * downstream -- including harmonic veto itself -- treats this as the real candidate) -- but V2
     * can also REJECT the candidate outright (raw_cand_valid=false) when even its own winning peak
     * fails the confidence floor, unlike V1 which only ever overrides or leaves the tallest-peak
     * default untouched. See TEMPLATE_MATCH_GATED_V2_ENABLE's own comment above for full rationale. */
    if (raw_cand_valid) {
        float gated_bpm_v2; bool reject_v2;
        if (template_match_gated_pick_v2(st, &win_r, &win_teal, &gated_bpm_v2, &reject_v2)) raw_cand_bpm = gated_bpm_v2;
        else if (reject_v2) raw_cand_valid = false;
    }
#ifdef COMBO13_EXPOSE_DEBUG
    else { st->dbg_v2_valid = 0; st->dbg_v2_n_peaks = 0; st->dbg_v2_winner_idx = -1; }
#endif
#endif

    int harmonic_confirmed = -1;
    if (USE_HARMONIC_VETO) {
        int win_m = win_nsamp / FRAME_DS;
        harmonic_confirmed = check_harmonic_confirmed(st, hist_len, win_nsamp, raw_cand_valid, raw_cand_bpm,
                                                        st->scratch_sumenv, win_m, ROBUST_TRIM_FRAC);
        if (harmonic_confirmed == 0) raw_cand_valid = false;
    }

#if defined(TEMPLATE_MATCH_ENABLE) && STREAM_VARIANT == 4
    /* Morphology-template HARD VETO -- REJECTED in this form, kept gated off for reference. See the
     * TEMPLATE_MATCH_ENABLE comment above for the full corpus numbers. Uses win_r.Sw (the RAW,
     * pre-detrend curve -- same one dbg_autocorr_raw copies) since that's what the templates were
     * built from, not win_teal.Sw (detrended). Gated to the validated rate range only. */
    if (raw_cand_valid && win_r.valid &&
        raw_cand_bpm >= TEMPLATE_MATCH_MIN_BPM && raw_cand_bpm <= TEMPLATE_MATCH_MAX_BPM) {
        int used_bucket = 0;
        float corr = best_template_corr(win_r.Sw, raw_cand_bpm, &used_bucket);
#ifdef COMBO13_EXPOSE_DEBUG
        st->dbg_template_match_corr = corr;
        st->dbg_template_match_corr_valid = 1;
        st->dbg_template_match_bucket = used_bucket;
#endif
        if (corr < TEMPLATE_MATCH_THRESH) raw_cand_valid = false;
    }
#ifdef COMBO13_EXPOSE_DEBUG
    else {
        st->dbg_template_match_corr_valid = 0;
    }
#endif
#endif

    bool is_jump = false;
    bool is_harmonic_jump = false;
    if (raw_cand_valid && st->last_accepted_teal_bpm_valid) {
        float ratio = raw_cand_bpm / st->last_accepted_teal_bpm;
        is_jump = (ratio >= TEAL_JUMP_RATIO) || (ratio <= 1.0f / TEAL_JUMP_RATIO);
        if (is_jump) {
            static const float kHarmonicRatios[4] = {2.0f, 0.5f, 3.0f, 1.0f / 3.0f};
            for (int hi = 0; hi < 4; hi++) {
                if (fabsf(ratio / kHarmonicRatios[hi] - 1.0f) <= TEAL_JUMP_HARMONIC_TOL) {
                    is_harmonic_jump = true;
                    break;
                }
            }
        }
    }
    if (suppress_circle) st->teal_jump_streak = 0;
    else if (is_jump) st->teal_jump_streak++;
    else st->teal_jump_streak = 0;
    int teal_jump_required_ticks = is_harmonic_jump ? TEAL_JUMP_HARMONIC_CONFIRM_TICKS : TEAL_JUMP_CONFIRM_TICKS;
    bool jump_unconfirmed = is_jump && (st->teal_jump_streak < teal_jump_required_ticks);
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_jump_unconfirmed = jump_unconfirmed ? 1 : 0;
#endif

    bool same_as_anchor = false;
    if (raw_cand_valid && st->bpm_delay_anchor_valid) {
        float ratio = raw_cand_bpm / st->bpm_delay_anchor_bpm;
        same_as_anchor = (ratio < TEAL_JUMP_RATIO) && (ratio > 1.0f / TEAL_JUMP_RATIO);
    }
    if (!raw_cand_valid) {
        st->bpm_delay_streak = 0;
        st->bpm_delay_anchor_valid = false;
    } else if (same_as_anchor) {
        st->bpm_delay_streak++;
    } else {
        st->bpm_delay_streak = 1;
        st->bpm_delay_anchor_bpm = raw_cand_bpm;
        st->bpm_delay_anchor_valid = true;
    }
    float time_since_wait_end = since_event - st->frozen_wait_s;
    bool bpm_delay_unconfirmed = raw_cand_valid &&
        (st->bpm_delay_streak < required_teal_delay_ticks(raw_cand_bpm, time_since_wait_end, !st->ever_displayed));
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_bpm_delay_unconfirmed = bpm_delay_unconfirmed ? 1 : 0;
#endif

    bool pushed_valid = raw_cand_valid && !suppress_circle && !jump_unconfirmed && !bpm_delay_unconfirmed && !curve_gate_veto;
    float pushed_bpm = pushed_valid ? raw_cand_bpm : 0.0f;
    if (pushed_valid) { st->last_accepted_teal_bpm = pushed_bpm; st->last_accepted_teal_bpm_valid = true; }
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_pushed_valid = pushed_valid ? 1 : 0;
#endif

    bool pushed_conf_valid = pushed_valid && win_teal.valid;
    float pushed_conf = pushed_conf_valid ? win_teal.conf : 0.0f;
#ifdef COMBO13_EXPOSE_DEBUG
    st->dbg_conf_valid = pushed_conf_valid ? 1 : 0;
    st->dbg_conf = pushed_conf;
    bool agree_margin_valid = raw_cand_valid && agree_valid;
    st->dbg_agree_margin_valid = agree_margin_valid ? 1 : 0;
    st->dbg_agree_margin = agree_margin_valid ? fabsf(agree_bpm - raw_cand_bpm) : 0.0f;
#endif

#if defined(TEMPLATE_WEIGHT_ENABLE) && STREAM_VARIANT == 4
    /* Computed off pushed_bpm (not raw_cand_bpm) since only pushed ticks are ever read back out of
     * template_corr_log -- see cons_buf_log[idx].valid gate in the gather loop below. Independent
     * of TEMPLATE_MATCH_ENABLE's own hard-veto gate (that one may already have vetoed this tick, in
     * which case pushed_valid is already false here and this is skipped anyway). */
    bool template_weight_valid = false;
    float template_weight_corr = 0.0f;
    if (pushed_valid && win_r.valid &&
        pushed_bpm >= TEMPLATE_MATCH_MIN_BPM && pushed_bpm <= TEMPLATE_MATCH_MAX_BPM) {
        template_weight_corr = best_template_corr(win_r.Sw, pushed_bpm, NULL);
        template_weight_valid = true;
    }
#endif

    log_push(st, pushed_valid, pushed_bpm, pushed_conf_valid, pushed_conf,
              (!suppress_circle) && agree_valid, agree_bpm,
#ifdef CURVE_WEIGHT_ENABLE
              st->curve_weight_corr_valid ? true : false, st->curve_weight_corr,
#else
              false, 0.0f,
#endif
#if defined(TEMPLATE_WEIGHT_ENABLE) && STREAM_VARIANT == 4
              template_weight_valid, template_weight_corr
#else
              false, 0.0f
#endif
              );

    float vals[WEIGHTED_MED_TRAILING_N], weights[WEIGHTED_MED_TRAILING_N];
    int count = 0;
    int avail = st->log_count;
    for (int back = 1; back <= avail && count < WEIGHTED_MED_TRAILING_N; back++) {
        int idx = (st->log_head - back + NLOG_V) % NLOG_V;
        if (!st->cons_buf_log[idx].valid) continue;
        float v = st->cons_buf_log[idx].val;
        bool av = st->agree_log[idx].valid;
        float aval = st->agree_log[idx].val;
#ifdef CURVE_WEIGHT_ENABLE
        bool cwv = st->curve_corr_log[idx].valid;
        float cwval = st->curve_corr_log[idx].val;
#else
        bool cwv = false; float cwval = 0.0f;
#endif
#ifdef TEMPLATE_WEIGHT_ENABLE
        bool twv = st->template_corr_log[idx].valid;
        float twval = st->template_corr_log[idx].val;
#else
        bool twv = false; float twval = 0.0f;
#endif
        vals[count] = v;
        weights[count] = teal_size_factor(v, av, aval, cwv, cwval, twv, twval);
        count++;
    }
    bool weighted_med_valid = count > 0;
    float weighted_med = weighted_med_valid ? weighted_median(vals, weights, count) : 0.0f;
#ifdef COMBO13_EXPOSE_DEBUG
    if (count > 0) {
        float vmin = vals[0], vmax = vals[0];
        for (int i = 1; i < count; i++) { if (vals[i] < vmin) vmin = vals[i]; if (vals[i] > vmax) vmax = vals[i]; }
        st->dbg_trailing_spread = vmax - vmin;
    } else {
        st->dbg_trailing_spread = 0.0f;
    }
    st->dbg_trailing_count = count;
#endif

    out.is_flatline = is_flatline;
    out.bpm_valid = (!is_flatline) && weighted_med_valid;
    out.bpm = out.bpm_valid ? weighted_med : 0.0f;
    out.raw_candidate_valid = raw_cand_valid;
    out.raw_candidate_bpm = raw_cand_valid ? raw_cand_bpm : 0.0f;
    out.harmonic_confirmed = harmonic_confirmed;

    if (out.bpm_valid) st->ever_displayed = true;

    st->el += TICK_S;
    return out;
}

#ifdef COMBO13_EXPOSE_DEBUG
/* See combo13_stream.h's own comment on this declaration. */
float c13stream_dbg_peak_mean_thresh(void) {
    return PEAK_MEAN_DIFF_THRESH;
}
/* B1 (2026-08-07): same rationale as c13stream_dbg_peak_mean_thresh() above -- SIGNAL_ONSET_THRESH
 * is `static const` (internal linkage), so a separate wrapper .c file can't reference it by name. */
float c13stream_dbg_signal_onset_thresh(void) {
    return SIGNAL_ONSET_THRESH;
}
/* Weighted-median combine panel (2026-08-07) -- see combo13_stream.h's own comment. */
float c13stream_dbg_tent_peak_bpm(void)      { return TENT_PEAK_BPM; }
float c13stream_dbg_tent_peak_h(void)        { return TENT_PEAK_H; }
float c13stream_dbg_tent_lo_h(void)          { return TENT_LO_H; }
float c13stream_dbg_tent_hi_h(void)          { return TENT_HI_H; }
float c13stream_dbg_agreement_bpm_tol(void)  { return AGREEMENT_BPM_TOL; }
float c13stream_dbg_agree_bonus(void)        { return AGREE_BONUS; }
/* Gate Candidate's early-release streak (2026-08-07) -- see combo13_stream.h's own comment. */
int   c13stream_dbg_early_release_min_ticks(void)  { return EARLY_RELEASE_MIN_TICKS; }
float c13stream_dbg_early_release_max_save_s(void) { return EARLY_RELEASE_MAX_SAVE_S; }
#endif
