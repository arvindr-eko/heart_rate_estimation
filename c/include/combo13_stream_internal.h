/* combo13_stream_internal.h -- state layout and fixed sizing for the combo13 streaming heart-rate
 * front end, revision 0.16.
 *
 * This header fixes the front end's shape at compile time: decimated sample rate, frame rate, and
 * autocorrelation lag range. There is only one shipped configuration (what upstream called
 * "STREAM_VARIANT=4" -- decimate 4000Hz raw audio to 333.333Hz, extract envelope frames at
 * 66.667Hz, search lags 20-146 frames, i.e. 30-200 BPM as of 0.15). Earlier variants explored native-rate
 * detection and other decimation ratios; none of that survives here since only one configuration
 * ever shipped.
 */
#ifndef COMBO13_STREAM_INTERNAL_H
#define COMBO13_STREAM_INTERNAL_H

#include <stdbool.h>
#include <stdint.h>

/* ================================================================================================
 * REVISION 0.14 -- two real changes since the kit's 0.12 baseline. Both are corpus-validated and
 * intentional; see INTEGRATION_NOTES.md at the top of this kit for the full writeup.
 *
 *   *** REQUIRES FIRMWARE-SIDE CHANGE ***  TICK_S (combo13_stream.c) is now 0.5s, not 0.25s.
 *   hr_calculate() must be called once per 500ms of pushed audio, not 250ms -- query
 *   c13stream_tick_s() at startup rather than hardcoding either value (see combo13_stream.h).
 *   This is a BREAKING change from every prior revision's calling cadence and must be coordinated
 *   with the firmware adapter (c13_hr.c, not in this kit) before this drop is integrated.
 *
 *   2. DEC_STRIDE 10->12 (decimated rate 400Hz->333.333Hz), S_LEN 96->80 lag bins, morphology
 *      templates re-baked for the new lag grid (autocorr_templates.h/autocorr_templates_detrended.h).
 *      STATE_SIZE_BYTES drops accordingly (see hr_state_size()). A small, disclosed
 *      accuracy-for-resources tradeoff (-0.35pp on the 314-file corpus vs. 0.12), not a bug.
 * ================================================================================================ */
/* ================================================================================================
 * REVISION 0.16 (2026-10-12) -- ported from the pcg_abab dev tree (v0.15 + v0.16).
 *
 *   0.15: LOWER BPM BOUND 40 -> 30. The lag grid grows on the slow end only: lags 20..145 (S_LEN 80 ->
 *         126, bins 0..79 still mean lags 20..99 exactly). Everything that existed in 0.14 (flux
 *         agreement, r_adaptive preview, V2 top-6 peak scoring, template scoring, early release) still
 *         looks ONLY at the "fast zone" (bins [0,FAST_LEN), >= ~40 BPM) with the same detrend context, so
 *         a >=40 BPM result is bit-identical to 0.14 unless the slow-zone override below fires.
 *         A slow-zone (30-40 BPM) peak replaces the fast-zone pick only when its detrended height is
 *         >= SLOW_RATIO_MIN x the tallest fast-zone peak's.
 *   0.16: (a) SLOW_RATIO_MIN 1.5 -> 1.35 (retuned on the 2,213-file train split of the 3,049-file combined
 *         corpus), (b) morphology templates re-baked from that train split: 34 buckets (35 = one merged
 *         30-40 BPM bucket, then every 5 BPM 40..200) instead of 28 buckets 45..190.
 *
 *   STATE SIZE grows (S_LEN 80 -> 126, EXT_S_LEN 207 -> 283): the adapter already mallocs
 *   hr_state_size(), so nothing else to change. TICK_S (0.5s) and the public API are unchanged.
 * ================================================================================================ */
#define COMBO13_VERSION "0.16"

/* -- front-end rate/sizing constants -- */
#define FRAME_DS      5   /* decimated samples per envelope frame -> 333.333/5 = 66.667Hz */
#define LO_LAG        20  /* shortest lag searched, in frames (200 BPM) */
#define MAX_VALID_LAG 134 /* slowest ACCEPTABLE candidate, in frames (30 BPM = 133.3, rounded up) */
#define HI_LAG                                                                                            \
  (MAX_VALID_LAG + 12)          /* margin bins past the 30 BPM limit so a true 30 BPM peak is an interior \
                                 * local max (peak finders need a right-hand neighbor) and the detrend    \
                                 * has context; those margin bins are never chosen as a candidate */
#define S_LEN (HI_LAG - LO_LAG) /* width of the autocorrelation lag axis (126) */
/* Peak scans over the full-width curve cover bins [1, PEAK_SCAN_END) -- a peak at lag MAX_VALID_LAG is the last
 * eligible one. */
#define PEAK_SCAN_END (MAX_VALID_LAG - LO_LAG + 1)
/* The 0.14 "fast zone": lags [LO_LAG, FAST_HI_LAG) = 20..99 = bins [0, FAST_LEN), i.e. >= ~40 BPM. */
#define FAST_HI_LAG 100
#define FAST_LEN    (FAST_HI_LAG - LO_LAG)
/* Slow-zone override (bins [FAST_LEN-1, PEAK_SCAN_END) = lags 99..134 = ~40.4 down to 30 BPM): a slow peak
 * replaces the fast-zone candidate only if its detrended height >= SLOW_RATIO_MIN x the tallest fast peak's
 * (a 2T/3T/4T autocorrelation echo of a genuine 60-160 BPM rhythm is almost always LOWER than the true
 * fundamental, so requiring dominance keeps those echoes from reading as a slow heart rate). */
#define SLOW_RATIO_MIN 1.35f
#define DEC_STRIDE     12   /* 4000Hz / 12 = 333.333Hz decimated rate */
#define DEC_MAX        2672 /* 8s of history at 333.333Hz -- the largest analysis window ever used */
#define FEAT_MAX       (DEC_MAX / FRAME_DS + 4) /* longest envelope-frame history (+ small margin) */

/* Harmonic veto searches an extended lag range above the candidate's own lag (up to ~2.18x) to look
 * for corroborating structure at roughly twice the candidate rate. EXT_HI_MAX is that search
 * ceiling: ceil(2.1814 * MAX_VALID_LAG) + 2 = 295, rounded up with a ~3% margin (0.15: was 227). */
#define EXT_HI_MAX 303
#define EXT_S_LEN  (EXT_HI_MAX - LO_LAG)

/* trailing-candidate log depth; only the most recent
 * WEIGHTED_MED_TRAILING_N=4 entries are ever read, the rest is
 * margin for ticks where no candidate was pushed */
#define NLOG 16

/* Streaming front-end chunk/carry sizing: samples are cleaned (artifact removal + declick) in
 * ~500ms chunks with a fixed carry region from the previous chunk, so a spike or dropout near a
 * chunk boundary still has enough context on both sides to be detected and interpolated cleanly. */
#define DEC_CHUNK_N 68
#define DEC_CARRY_N 48

/* carry + chunk, the buffer size every artifact-removal pass operates on */
#define CHUNK_TOTAL_N (DEC_CARRY_N + (3 * DEC_CHUNK_N))

#define SCRATCH_SHARED_LEN FEAT_MAX

typedef struct EstimateResult {
  bool valid;
  float bpm;
  float conf; /* tallest-peak / next-distinct-peak height ratio -- a prominence measure */
  float Sw[S_LEN];
  int lo;
  /* 0.15 slow-zone (30-40 BPM) candidate, filled in by estimate_detrend_from_mw only */
  bool slow_valid;
  float slow_bpm;
  float slow_ratio;
} EstimateResult_t;

typedef struct StreamLogEntry {
  float val;
  bool valid;
  float conf;
  bool conf_valid;
} StreamLogEntry_t;

typedef struct StreamAgreeLogEntry {
  float val;
  bool valid;
} StreamAgreeLogEntry_t;

/* Streaming replacement for "sort the whole analysis window for a percentile": each chunk's own
 * exact percentile (via quickselect) is EMA-blended into a persistent estimate, so the baseline
 * tracks slowly-changing signal level in O(1) state instead of re-deriving it from a large buffer
 * every tick. For the first BASELINE_WARMUP_S seconds `ema` is instead a duration-weighted running
 * mean -- seeding the EMA directly from one noisy ~100ms chunk left the artifact/declick threshold
 * under-converged for several seconds after signal onset. */
typedef struct StreamBaseline {
  float ema;
  bool initialized;
  float warm_sum;
  float warm_dur;
} StreamBaseline_t;

typedef struct PushScratch {
  /* process_and_emit_chunk */
  bool grown[CHUNK_TOTAL_N];
  float absx[CHUNK_TOTAL_N];
  float raw_chunk[CHUNK_TOTAL_N];
  /* stream_detect_extend_interp */
  bool padded[CHUNK_TOTAL_N];
  float original[CHUNK_TOTAL_N];
} PushScratch_t;

typedef struct AdaptivePreview {
  float S[S_LEN];
  float Sw[S_LEN];
} AdaptivePreview_t;

typedef struct SumEnvRobust {
  float S[S_LEN];
  int pk[S_LEN];
} SumEnvRobust_t;

typedef struct DetrendFromMw {
  float csum[S_LEN + 1];
  int cand[S_LEN];
} DetrendFromMw_t;

typedef struct FluxAgreement {
  union {
    float S[S_LEN];
    float det_buf[S_LEN];
  };
  float Sw_buf[S_LEN];
  float dcsum[S_LEN + 1];
} FluxAgreement_t;

typedef struct TickScratch {
  union {
    /* r_adaptive preview */
    AdaptivePreview_t prv;
    /* estimate_sumenv_robust */
    SumEnvRobust_t sum;
    /* estimate_sumenv_robust */
    DetrendFromMw_t det;
    /* Flux agreement signal */
    FluxAgreement_t flx;
    /* Morphology-template gated peak selection */
    float weights[S_LEN];
  };
  /* estimate_sumenv_robust */
  EstimateResult_t win_r;
  /* estimate_detrend_from_mw */
  EstimateResult_t win_teal;
} TickScratch_t;

typedef union StackScratch {
  PushScratch_t push;
  TickScratch_t tick;
} StackScratch_t;

typedef struct hr_state {
  float el; /* elapsed time, seconds, since hr_init */

  /* -- onset debounce (kept for completeness; no longer gates any timing decision below since
   * SIGNAL_ONSET_THRESH only ever rejects true digital silence -- see hr_calculate()) -- */
  bool sig_was_onset_ok;
  int onset_off_streak;
  int onset_on_streak;
  float onset_candidate_el;
  float last_onset_el;

  float last_spike_el;
  float last_logged_spike_t;
  /* Timestamp is_flatline last transitioned true->false after persisting at least
   * FLATLINE_FULL_RESET_MIN_S -- a genuine disconnect/reconnect event, treated identically to a
   * spike event for the purposes of the settling-wait/ramp timer below. Brief sub-threshold
   * flatline blips don't set this. */
  float flatline_clear_el;
  float flatline_became_true_el;
  bool was_flatline;
  /* True once a bpm has ever been displayed this session -- distinguishes a genuine reconnect
   * from the initial cold-start bootstrap (also a long flatline period, but one that needs no
   * extra settling beyond the ordinary first-contact wait). */
  bool ever_displayed;

  float prev_last_event_el;
  float frozen_wait_s;

  /* 1.0f/powf(LO_LAG+i, LAG_POW) for every lag bin, precomputed once in hr_init() since
   * LAG_POW is a fixed compile-time constant -- avoids a powf() call per lag bin, per candidate,
   * every tick (the autocorrelation weighting is applied at 3 call sites). */
  float lag_pow_recip[S_LEN];

  /* Consecutive ticks the raw candidate has agreed with the independent agreement signal (flux),
   * within AGREEMENT_BPM_TOL -- lets the settling wait after an event be cut short once two
   * independently-derived estimates agree, rather than always waiting the full CONS_WAIT_S.
   * Reset on any new event or flatline. */
  int early_release_streak;

  StreamLogEntry_t cons_buf_log[NLOG];           /* trailing pushed candidates + their confidence */
  StreamAgreeLogEntry_t agree_log[NLOG];         /* trailing flux agreement values, same indices */
  StreamAgreeLogEntry_t template_corr_log[NLOG]; /* trailing morphology-template correlations */
  int log_head;
  int log_count;

  float last_accepted_teal_bpm;
  bool last_accepted_teal_bpm_valid;
  int teal_jump_streak;

  float bpm_delay_anchor_bpm;
  bool bpm_delay_anchor_valid;
  int bpm_delay_streak;

  float peak_mean_below_since_el; /* flatline detector's own debounce timer */

  /* -- streaming decimation (continuous anti-alias filter + stride-keep) -- */
  float dec_aa_z[4];
  int dec_stride_phase;

  /* -- streaming artifact removal (chunk + carry buffer at the decimated rate) -- */
  float dec_chunk_buf[CHUNK_TOTAL_N];
  int dec_chunk_len;
  int cur_chunk_len;
  StreamBaseline_t art_baseline; /* robust-percentile tracker feeding artifact detect/extend */
  StreamBaseline_t med_baseline; /* median tracker feeding declick + spike detection */
  /* Wall-clock time of the start of the next not-yet-processed chunk, on the same timeline `el`
   * advances -- used to timestamp detected spikes at chunk granularity. */
  float chunk_time_cursor;

  /* Count of decimated samples seen so far (saturating at DEC_MAX) -- used by every "is there
   * enough history yet" gate (window sizing, harmonic-veto readiness), independent of how much
   * of that history is still resident in the per-band envelope rings below. */
  int dec_sample_count;

  /* Persistent per-band bandpass filter + envelope state, updated once per new decimated sample
   * as it arrives. Every FRAME_DS-th sample, each band's current envelope value is pushed into
   * band_env_ring at frame rate -- every downstream feature extractor just gathers trailing
   * frames from these rings, with no raw filtering repeated per tick. */
  float band_filt_z[3][4];
  float band_env_z[3];
  int band_frame_phase;
  int16_t band_env_ring[3][FEAT_MAX];
  int band_env_ring_len;
  int band_env_ring_head;

  /* Persists across the whole tick (written by the summed-envelope feature extractor, read much
   * later by the harmonic veto), so it can't share memory with the general scratch pool below. */
  float scratch_sumenv[FEAT_MAX];

  /* General-purpose scratch, reused strictly sequentially within one hr_calculate() call by
   * whichever stage currently needs a FEAT_MAX-sized workspace (band gathering, autocorrelation
   * products, flux trace, flatline peak-mean scan). Each use fully consumes its result before the
   * next stage starts, so one shared buffer replaces what would otherwise be several same-sized
   * buffers live at once. */
  float scratch_shared[SCRATCH_SHARED_LEN];

  /* Extended-range autocorrelation workspace for the harmonic veto -- kept separate from
   * scratch_shared because the veto uses it as an outer accumulator across a call that itself
   * uses scratch_shared internally; sharing them would alias and corrupt both. */
  float scratch_Sext[EXT_S_LEN];

  /* IN-TREE STACK FIX (coral-fw, re-applied to every revision since 0.6 -- first made against the
   * 0.6/17KB drop in commit 5ab280ab after its first flash bootlooped, then re-applied to 0.7 in
   * 96dcdef4, and to 0.12 in this kit): broad_env()'s `tmp[FEAT_MAX]` and the r_adaptive preview's
   * `w[FEAT_MAX]` are 2,576B each -- fine as stack locals on an 8MB x86 stack, fatal on the
   * firmware's 4KB heart-rate task stack. Both now point here instead. They are never
   * simultaneously live (broad_env is not called while the r_adaptive weight buffer is in use),
   * and neither can join scratch_shared, which each uses CONCURRENTLY (broad_env gathers band b
   * into tmp while accumulating into its caller's out == scratch_shared; `w` weights the very
   * features feat_norm_clip just left in scratch_shared) -- liveness re-traced against v0.14's own
   * call graph (broad_env's 3 call sites -- sig-level, peak-mean/flatline, envelope_flux -- and
   * the r_adaptive preview all still pass/use st->scratch_shared exactly as in 0.12, and the two
   * buffers' call sites remain non-overlapping, sequential blocks within hr_calculate(); this
   * proof was re-verified against v0.14's structure, not copied blind).
   * NOT in the upstream project -- sync before the next revision drop. */
  float scratch_feat[FEAT_MAX];

  StackScratch_t stack;

} hr_state_t;

#endif /* COMBO13_STREAM_INTERNAL_H */
