/* combo13_stream_wasm_debug.c -- DEBUG/VISUALIZATION-ONLY WASM wrapper (2026-08-07), compiled with
 * -DCOMBO13_EXPOSE_DEBUG so combo13_stream_internal.h's dbg_* struct fields exist and
 * combo13_stream.c's COMBO13_EXPOSE_DEBUG-gated snapshot lines actually run.
 *
 * This is a SEPARATE build target from combo13_stream_wasm.c (the production wrapper) -- the
 * production combo13_wasm.js is built WITHOUT this flag and is completely untouched by anything
 * in this file. Both wrap the exact same, unmodified combo13_stream.c/combo13_stream_internal.h
 * source; only the compile flag differs, same pattern already used for STREAM_VARIANT itself.
 *
 * Exposes internal signals purely for pcg_visualizer's Combo13-internals panels (C0-C5, added
 * 2026-08-07; A2-A4, added later the same day for "Part A" -- see combo13_stream.c's own
 * COMBO13_EXPOSE_DEBUG-gated write sites for exactly where each is captured):
 *   1. band-summed envelope (scratch_sumenv) -- the actual signal the main autocorrelation runs on
 *   2. main autocorrelation curve (dbg_autocorr_raw) + the detrended peak pick (dbg_win_teal_bpm)
 *   3. flux's own trace + autocorrelation (dbg_flux_trace/dbg_flux_autocorr, added 2026-08-08),
 *      the sole agreement/combine-weight signal since revision 0.2 -- tilt (the native-rate/FFT
 *      pitch trace this project inherited from combo13_opt) was fully removed, struct fields and
 *      all, in revision 0.4 (2026-08-08, same day) after confirming its result was consumed
 *      nowhere in the shipped build
 *   4. the flatline detector's own decision metric (dbg_peak_minus_mean)
 *   5. onset state (sig_was_onset_ok/last_onset_el -- already-unconditional core algorithm state)
 *   6. Part A: the streaming-decimation output pre-clean (dbg_decpre_trace) and the same signal
 *      post-artifact-removal/declick (dbg_decpost_trace) -- each a persistent ring (written
 *      continuously from c13stream_push()) gathered into a linear trailing-window snapshot once
 *      per tick. (A2, tilt's own native-rate bandpass-input ring, was removed with tilt itself,
 *      revision 0.4, 2026-08-08.)
 *
 * Array signals are exposed as pointers into WASM linear memory (same convention as
 * combo13_wasm_tick's out buffer) -- JS reads them directly via HEAPF32.subarray(ptr/4, ptr/4+len),
 * with lengths obtained ONCE at init via the _max()/_ffs() getters below rather than ever
 * hardcoding a C compile-time constant in JS.
 */
#include "combo13_stream.h"

#ifndef COMBO13_EXPOSE_DEBUG
#error "combo13_stream_wasm_debug.c must be compiled with -DCOMBO13_EXPOSE_DEBUG"
#endif

/* TEMPLATE COMPARISON PANEL (2026-10-01), per Daniel's request to visually compare the live
 * autocorrelation against the actual template it's scored against, in a new panel below the
 * existing Peak Picker. autocorr_templates.h (kTemplateBpm, TEMPLATE_MATCH_N_BUCKETS/N_CURVE/
 * QUANT_SCALE) must come first -- autocorr_templates_detrended.h's array relies on those same
 * macros already being defined, same include order combo13_stream.c itself uses. This is a
 * SEPARATE translation unit from combo13_stream.c (which also includes these two headers,
 * guarded behind TEMPLATE_MATCH_GATED_V2_ENABLE/STREAM_VARIANT==4) -- no include guards exist in
 * either header, but that's fine here since each is only included once in this file. kTemplateCurve
 * (the RAW-curve table, unused here) comes along for free from autocorr_templates.h; harmless in
 * this debug-only WASM build (flash footprint isn't a concern off-device). */
#include "autocorr_templates.h"
#include "autocorr_templates_detrended.h"

static Combo13StreamState g_state;
/* Dequantized once at init so JS never has to deal with int8 scaling -- same "expose plain floats"
 * convention as every other debug array in this file. */
static float g_templateCurveF[TEMPLATE_MATCH_N_BUCKETS * TEMPLATE_MATCH_N_CURVE];

/* out[0]=t, [1]=bpm, [2]=bpm_valid, [3]=is_flatline, [4]=raw_candidate_bpm, [5]=raw_candidate_valid,
 * [6]=harmonic_confirmed, [7]=dbg_autocorr_valid, [8]=dbg_autocorr_lo, [9]=dbg_win_teal_valid,
 * [10]=dbg_win_teal_bpm, [11]=dbg_band_env_n, [12]/[13]/[14]=RETIRED (were dbg_tilt_trace_len/
 * dbg_tilt_autocorr_valid/dbg_tilt_bpm, tilt's own C4 panel data -- tilt removed entirely in
 * revision 0.4, 2026-08-08; left as always-0.0f rather than renumbering everything downstream),
 * [15]=dbg_peak_mean_valid, [16]=dbg_peak_minus_mean, [17]=sig_was_onset_ok,
 * [18]=last_onset_el (seconds since init, or a very negative sentinel if no onset has ever fired --
 * see combo13_stream.c's NEG_INF=-1e30f; JS treats any negative value as "none yet"). Onset fields
 * are ALREADY plain, unconditional Combo13StreamState members (not behind COMBO13_EXPOSE_DEBUG --
 * they're core algorithm state, same as is_flatline), so exposing them needed zero new C
 * instrumentation, just 2 more lines here.
 * [19]=RETIRED (was dbg_tiltbp_trace_len, A2's own tilt-bandpass input trace -- A2 removed with
 * tilt, revision 0.4). [20]=dbg_decpre_trace_len (A3), [21]=dbg_decpost_trace_len (A4) -- each
 * grows from 0 up to its own max as history accumulates, exactly like dbg_band_env_n above; the
 * actual array data is read via the pointer+max getters below.
 * [22]=dbg_sig_level (B1), [23]=dbg_sig_level_valid, [24]=onset_on_streak, [25]=onset_off_streak
 * (both already-plain unconditional state, like sig_was_onset_ok/last_onset_el above), [26]=
 * last_spike_el, [27]=flatline_clear_el (also already-plain; JS recomputes last_event_el/
 * since_event from these + last_onset_el(out[18])/t(out[0]) using the IDENTICAL formula
 * combo13_stream.c itself uses, rather than duplicating that storage here -- B2), [28]=
 * dbg_adaptive_tau, [29]=dbg_r_adaptive_bpm, [30]=dbg_r_adaptive_valid (B3).
 * [31]=dbg_suppress_circle, [32]=dbg_early_release_ok, [33]=dbg_jump_unconfirmed,
 * [34]=dbg_bpm_delay_unconfirmed, [35]=dbg_pushed_valid (Gate Candidate, 2026-08-07) -- the guard
 * chain downstream of raw_candidate_valid/harmonic_confirmed (already out[5]/out[6] above) that
 * decides whether THIS tick's candidate actually reaches cons_buf_log.
 * [36]=dbg_frozen_wait_s (B3, 2026-08-07) -- the actual wait-duration consequence of
 * adaptive_tau/r_adaptive_bpm (out[28]/out[29]), snapshotted every tick so its currently-held value
 * is always visible, not just at the moment it's reset.
 * [37]=dbg_early_agree_now (Gate Candidate, 2026-08-07) -- THIS tick's raw agreement-signal-vs-teal
 * agreement (agree_valid/agree_bpm in combo13_stream.c -- flux, the sole agreement signal since
 * revision 0.2), before early_release_ok's (out[32]) own streak-gating. harmonic_confirmed is
 * already out[6] -- no new field needed for that lane.
 * [38]=dbg_flux_valid, [39]=dbg_flux_bpm (2026-08-08, added when the diagram was updated to show
 * flux's own real steps instead of tilt's) -- flux's own autocorrelation result, straight from the
 * flux agreement-signal block, not the combine.
 * [40]=dbg_flux_autocorr_valid -- whether dbg_flux_autocorr (pointer getter below) holds real data
 * this tick (same convention as out[7] for the main curve).
 * [41]=dbg_flux_trace_len -- how much of dbg_flux_trace (pointer getter below) is populated this
 * tick (same convention as out[11]'s dbg_band_env_n).
 * CONFIDENCE METRIC PROTOTYPE (2026-08-08): [42]=dbg_conf_valid, [43]=dbg_conf (win_teal's own
 * peak-height-ratio prominence measure for THIS tick's pushed candidate, previously computed every
 * tick but write-only -- stored into cons_buf_log[].conf and never read back anywhere until now).
 * [44]=dbg_agree_margin_valid, [45]=dbg_agree_margin (raw |flux_bpm - raw_candidate_bpm| distance
 * when both independently valid, finer-grained than the binary AGREE_BONUS gate). [46]=
 * dbg_trailing_spread, [47]=dbg_trailing_count (max-min spread and item count of the
 * weighted-median's own trailing candidate window -- a stability proxy). See
 * combo13_stream_internal.h's own comment on these fields for the full design rationale.
 * AUTOCORRELATION CURVE CORRELATION PROTOTYPE (2026-08-09): [48]=dbg_curve_corr_valid,
 * [49]=dbg_curve_corr -- Pearson correlation between THIS tick's win_teal detrended curve and the
 * SAME curve one tick ago (1.0=identical shape, 0=unrelated, negative=inverted). See
 * combo13_stream.c's own comment at the write site for the full design rationale. */
/* V2 PEAK-PICKER VISUALIZATION (2026-08-10): [50]=dbg_v2_valid, [51]=dbg_v2_n_peaks,
 * [52]=dbg_v2_winner_idx (-1 if peaks were found but the confidence floor rejected all of them),
 * [53..58]=dbg_v2_peak_bpm[0..5], [59..64]=dbg_v2_peak_score[0..5] -- see
 * combo13_stream_internal.h's own comment on these fields for the full design rationale
 * (TEMPLATE_MATCH_GATED_V2_ENABLE's internal candidate list every tick). */
#define COMBO13_DBG_OUT_N 65

void combo13_wasm_init(void) {
    c13stream_init(&g_state);
    for (int i = 0; i < TEMPLATE_MATCH_N_BUCKETS * TEMPLATE_MATCH_N_CURVE; i++) {
        g_templateCurveF[i] = ((const int8_t *)kTemplateCurveDetrended)[i] / TEMPLATE_MATCH_QUANT_SCALE;
    }
}

void combo13_wasm_push(float *samples, int n) {
    c13stream_push(&g_state, samples, n);
}

void combo13_wasm_tick(float *out) {
    Combo13StreamTick r = c13stream_tick(&g_state);
    out[0] = r.t;
    out[1] = r.bpm;
    out[2] = r.bpm_valid ? 1.0f : 0.0f;
    out[3] = r.is_flatline ? 1.0f : 0.0f;
    out[4] = r.raw_candidate_bpm;
    out[5] = r.raw_candidate_valid ? 1.0f : 0.0f;
    out[6] = (float)r.harmonic_confirmed;
    out[7] = g_state.dbg_autocorr_valid ? 1.0f : 0.0f;
    out[8] = (float)g_state.dbg_autocorr_lo;
    out[9] = g_state.dbg_win_teal_valid ? 1.0f : 0.0f;
    out[10] = g_state.dbg_win_teal_bpm;
    out[11] = (float)g_state.dbg_band_env_n;
    out[12] = 0.0f; /* retired: was dbg_tilt_trace_len, tilt removed revision 0.4 */
    out[13] = 0.0f; /* retired: was dbg_tilt_autocorr_valid, tilt removed revision 0.4 */
    out[14] = 0.0f; /* retired: was dbg_tilt_bpm, tilt removed revision 0.4 */
    out[15] = g_state.dbg_peak_mean_valid ? 1.0f : 0.0f;
    out[16] = g_state.dbg_peak_minus_mean;
    out[17] = g_state.sig_was_onset_ok ? 1.0f : 0.0f;
    out[18] = g_state.last_onset_el;
    out[19] = 0.0f; /* retired: was dbg_tiltbp_trace_len (A2), tilt removed revision 0.4 */
    out[20] = (float)g_state.dbg_decpre_trace_len;
    out[21] = (float)g_state.dbg_decpost_trace_len;
    out[22] = g_state.dbg_sig_level;
    out[23] = g_state.dbg_sig_level_valid ? 1.0f : 0.0f;
    out[24] = (float)g_state.onset_on_streak;
    out[25] = (float)g_state.onset_off_streak;
    out[26] = g_state.last_spike_el;
    out[27] = g_state.flatline_clear_el;
    out[28] = g_state.dbg_adaptive_tau;
    out[29] = g_state.dbg_r_adaptive_bpm;
    out[30] = g_state.dbg_r_adaptive_valid ? 1.0f : 0.0f;
    out[31] = (float)g_state.dbg_suppress_circle;
    out[32] = (float)g_state.dbg_early_release_ok;
    out[33] = (float)g_state.dbg_jump_unconfirmed;
    out[34] = (float)g_state.dbg_bpm_delay_unconfirmed;
    out[35] = (float)g_state.dbg_pushed_valid;
    out[36] = g_state.dbg_frozen_wait_s;
    out[37] = (float)g_state.dbg_early_agree_now;
    out[38] = g_state.dbg_flux_valid ? 1.0f : 0.0f;
    out[39] = g_state.dbg_flux_bpm;
    out[40] = g_state.dbg_flux_autocorr_valid ? 1.0f : 0.0f;
    out[41] = (float)g_state.dbg_flux_trace_len;
    /* CONFIDENCE METRIC PROTOTYPE (2026-08-08) -- see combo13_stream_internal.h's own comment on
     * these 6 dbg_* fields for what each represents. */
    out[42] = g_state.dbg_conf_valid ? 1.0f : 0.0f;
    out[43] = g_state.dbg_conf;
    out[44] = g_state.dbg_agree_margin_valid ? 1.0f : 0.0f;
    out[45] = g_state.dbg_agree_margin;
    out[46] = g_state.dbg_trailing_spread;
    out[47] = (float)g_state.dbg_trailing_count;
    /* AUTOCORRELATION CURVE CORRELATION PROTOTYPE (2026-08-09) -- see combo13_stream_internal.h's
     * own comment on dbg_curve_corr for what this represents (Pearson correlation of win_teal's
     * detrended curve against the same curve one tick ago). */
    out[48] = g_state.dbg_curve_corr_valid ? 1.0f : 0.0f;
    out[49] = g_state.dbg_curve_corr;
    /* V2 PEAK-PICKER VISUALIZATION (2026-08-10) -- see COMBO13_DBG_OUT_N's own comment above for
     * the full layout. Peak slots beyond dbg_v2_n_peaks are left at whatever combo13_stream.c last
     * wrote there (stale from an earlier tick with more peaks) -- JS only ever reads the first
     * dbg_v2_n_peaks entries, same convention as every other variable-length debug array here. */
    out[50] = (float)g_state.dbg_v2_valid;
    out[51] = (float)g_state.dbg_v2_n_peaks;
    out[52] = (float)g_state.dbg_v2_winner_idx;
    for (int i = 0; i < COMBO13_DBG_V2_MAX_PEAKS; i++) {
        out[53 + i] = g_state.dbg_v2_peak_bpm[i];
        out[59 + i] = g_state.dbg_v2_peak_score[i];
    }
}

unsigned long combo13_wasm_state_size(void) {
    return c13stream_state_size();
}

/* Same COMBO13_VERSION major*1000+minor encoding as the production wrapper (combo13_stream_wasm.c)
 * -- see that file's comment for why, and for the 2026-08-09 fix history. */
#include <stdio.h>
float combo13_wasm_version(void) {
    int major = 0, minor = 0;
    sscanf(c13stream_version(), "%d.%d", &major, &minor);
    return (float)(major * 1000 + minor);
}

/* Seconds of audio JS must push per c13stream_tick() call -- see combo13_stream_wasm.c's (the
 * production wrapper) identical comment for the full rationale; same accessor mirrored here so
 * the debug build stays in lockstep. */
float combo13_wasm_tick_s(void) { return c13stream_tick_s(); }

/* -- debug array pointers (into g_state's own persistent fields, no extra copying) -- */
float *combo13_wasm_dbg_band_env_ptr(void)     { return g_state.scratch_sumenv; }
/* Per-band RAW envelope (2026-08-07) -- same validity/length as dbg_band_env_n (out[11]) and same
 * max length as scratch_sumenv (FEAT_MAX_V, combo13_wasm_dbg_band_env_max() below), no new getter
 * needed for either. Band order/colors match the legacy JS pipeline's own convention: 0=30-50Hz,
 * 1=50-80Hz, 2=80-130Hz. */
float *combo13_wasm_dbg_band0_env_ptr(void) { return g_state.dbg_band0_env; }
float *combo13_wasm_dbg_band1_env_ptr(void) { return g_state.dbg_band1_env; }
float *combo13_wasm_dbg_band2_env_ptr(void) { return g_state.dbg_band2_env; }
float *combo13_wasm_dbg_autocorr_ptr(void)     { return g_state.dbg_autocorr_raw; }
/* The ACTUAL detrended curve win_teal.bpm's peak is picked from (2026-08-07 fix) -- same
 * S_LEN_V size/indexing as dbg_autocorr_raw above, valid whenever dbg_win_teal_valid (out[9]) is. */
float *combo13_wasm_dbg_win_teal_curve_ptr(void) { return g_state.dbg_win_teal_curve; }
/* Flux's own trace + autocorrelation curve (2026-08-08) -- flux shares the main candidate's own
 * lag range/frame rate (LO_LAG/HI_LAG/FFS), so these reuse combo13_wasm_dbg_band_env_max()/
 * dbg_autocorr_max()/dbg_ffs()/out[8]'s dbg_autocorr_lo for length/conversion -- no new max-length
 * or rate getters needed, only the 2 pointers below. */
float *combo13_wasm_dbg_flux_trace_ptr(void)    { return g_state.dbg_flux_trace; }
float *combo13_wasm_dbg_flux_autocorr_ptr(void) { return g_state.dbg_flux_autocorr; }
/* Part A (2026-08-07) -- already-linearized trailing-window snapshots, see combo13_stream_internal.h.
 * (A2, tilt's own bandpass-input trace, was removed with tilt itself, revision 0.4, 2026-08-08.) */
float *combo13_wasm_dbg_decpre_ptr(void)  { return g_state.dbg_decpre_trace; }   /* A3 */
float *combo13_wasm_dbg_decpost_ptr(void) { return g_state.dbg_decpost_trace; }  /* A4 */
/* A4 overlay flags (2026-08-07) -- 1.0f/0.0f per sample, SAME length/indexing as
 * dbg_decpost_trace above (no separate _max getter needed, reuse combo13_wasm_dbg_dec_max()). */
float *combo13_wasm_dbg_decpost_artifact_ptr(void) { return g_state.dbg_decpost_artifact_trace; }
float *combo13_wasm_dbg_decpost_declick_ptr(void)  { return g_state.dbg_decpost_declick_trace; }
/* Spike-detect mask (2026-08-07) -- same length/indexing as the two above, reuses
 * combo13_wasm_dbg_dec_max(). */
float *combo13_wasm_dbg_decpost_spike_ptr(void)    { return g_state.dbg_decpost_spike_trace; }

/* -- max lengths for the arrays above, read once at JS init time -- never hardcode these in JS -- */
int combo13_wasm_dbg_band_env_max(void)      { return FEAT_MAX_V; }
int combo13_wasm_dbg_autocorr_max(void)      { return S_LEN_V; }
int combo13_wasm_dbg_dec_max(void)     { return DEC_MAX_V; }            /* A3/A4 (same size) */

/* TEMPLATE COMPARISON PANEL (2026-10-01) -- baked per-bpm-bucket DETRENDED templates the peak
 * picker's template-match score compares each candidate against (same table
 * TEMPLATE_MATCH_GATED_V2_ENABLE's combined_template_score() uses on the win_teal/detrended side,
 * see combo13_stream.c). Flat row-major [bucket*TEMPLATE_MATCH_N_CURVE + bin], already dequantized
 * (see g_templateCurveF above) -- JS reads it the same way as every other HEAPF32 debug array,
 * using the bucket-count/curve-length getters below rather than hardcoding 28/80. Static tables,
 * not per-tick state -- no out[] slot needed, same convention as the other pointer+getter pairs on
 * this page. kTemplateBpm's AC_LO/N_CURVE already match the live curve's own LO_LAG_V/S_LEN_V grid
 * for STREAM_VARIANT==4 (confirmed: both 20/80) -- JS should still read dbg_template_ac_lo() rather
 * than assume that, in case a future STREAM_VARIANT or lag-grid change ever decouples them. */
float *combo13_wasm_dbg_template_curve_ptr(void) { return g_templateCurveF; }
float *combo13_wasm_dbg_template_bpm_ptr(void)    { return (float *)kTemplateBpm; }
int   combo13_wasm_dbg_template_n_buckets(void)   { return TEMPLATE_MATCH_N_BUCKETS; }
int   combo13_wasm_dbg_template_n_curve(void)      { return TEMPLATE_MATCH_N_CURVE; }
int   combo13_wasm_dbg_template_ac_lo(void)        { return TEMPLATE_MATCH_AC_LO; }

/* flatline detector's own decision threshold, same raw-amplitude scale as dbg_peak_minus_mean --
 * see combo13_stream.h/.c's c13stream_dbg_peak_mean_thresh() for why this needs a real accessor
 * instead of a duplicated literal. */
float combo13_wasm_dbg_peak_mean_thresh(void) { return c13stream_dbg_peak_mean_thresh(); }
/* B1: onset-ON threshold (SIGNAL_ONSET_THRESH); the onset-OFF threshold is exactly half of this
 * (`signal_offset_thresh = SIGNAL_ONSET_THRESH * 0.5f` in combo13_stream.c) -- JS computes that
 * directly from this one value rather than needing a second accessor for a trivially-derived one. */
float combo13_wasm_dbg_signal_onset_thresh(void) { return c13stream_dbg_signal_onset_thresh(); }
/* Weighted-median combine panel (2026-08-07) -- tent-weighting shape + agreement bonus, so JS
 * can compute each pushed candidate's own real combine weight (bpm_favor_center * agree_bonus). */
float combo13_wasm_dbg_tent_peak_bpm(void)     { return c13stream_dbg_tent_peak_bpm(); }
float combo13_wasm_dbg_tent_peak_h(void)       { return c13stream_dbg_tent_peak_h(); }
float combo13_wasm_dbg_tent_lo_h(void)         { return c13stream_dbg_tent_lo_h(); }
float combo13_wasm_dbg_tent_hi_h(void)         { return c13stream_dbg_tent_hi_h(); }
float combo13_wasm_dbg_agreement_bpm_tol(void) { return c13stream_dbg_agreement_bpm_tol(); }
float combo13_wasm_dbg_agree_bonus(void)       { return c13stream_dbg_agree_bonus(); }
/* Gate Candidate's early-release streak (2026-08-07) -- so JS can recompute the real streak
 * threshold instead of hardcoding a duplicate copy. */
int   combo13_wasm_dbg_early_release_min_ticks(void)  { return c13stream_dbg_early_release_min_ticks(); }
float combo13_wasm_dbg_early_release_max_save_s(void) { return c13stream_dbg_early_release_max_save_s(); }

/* -- rates needed to convert bin index -> bpm in JS, also read once at init -- FS_IN=4000.0f
 * matches combo13_stream.c's own #define FS_IN exactly (the fixed native audio rate this whole
 * port assumes, not a per-variant tunable); DEC_STRIDE/FRAME_DS_V come straight from
 * combo13_stream_internal.h, the same header combo13_stream.c itself builds FFS/DEC_HZ from -- so
 * this can't silently drift from the real algorithm's own rate even if DEC_STRIDE is retuned. */
#define COMBO13_DBG_FS_IN 4000.0f
float combo13_wasm_dbg_ffs(void)       { return (COMBO13_DBG_FS_IN / (float)DEC_STRIDE) / (float)FRAME_DS_V; }
/* Part A (2026-08-07): native rate (A1's own sample rate) and decimated rate (A3/A4's), so JS can
 * convert a trailing-window sample count into elapsed seconds without ever hardcoding either.
 * (A2, tilt's own native-rate ring, was removed with tilt itself, revision 0.4, 2026-08-08.) */
float combo13_wasm_dbg_native_rate(void) { return COMBO13_DBG_FS_IN; }
float combo13_wasm_dbg_dec_rate(void)    { return COMBO13_DBG_FS_IN / (float)DEC_STRIDE; }
