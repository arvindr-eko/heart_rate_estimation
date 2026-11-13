/* HISTORICAL SNAPSHOT (renamed 2026-08-08) -- pairs with combo13_stream_pre0.1-2_early-release.c.
 * Superseded; do not build firmware from this file. See that file's own header note and project
 * memory (project_pcg_embedded_c) for detail. */

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
#else
  #error "STREAM_VARIANT must be 1, 2, or 3"
#endif

#define S_LEN_V (HI_LAG_V - LO_LAG_V)
#define FEAT_MAX_V (DEC_MAX_V / FRAME_DS_V + 4)
#define EXT_HI_MAX_V 350
#define EXT_S_LEN_V (EXT_HI_MAX_V - LO_LAG_V)
#define NLOG_V 16                /* only WEIGHTED_MED_TRAILING_N=8 ever needed; margin for gaps */

/* -- streaming front-end chunk/carry sizing -- */
#define NATIVE_CHUNK_N 400        /* 100ms @ 4000Hz -- variant 1's detection granularity */
#define NATIVE_CARRY_N 256         /* boundary context: pad_samples + extend-search margin */
#define DEC_CHUNK_N 68              /* ~100ms @ up to 667Hz -- variants 2/3's detection granularity */
#define DEC_CARRY_N 48               /* boundary context at decimated rate */

/* -- tilt trace (task 7, unaffected by this restructuring -- still fed directly from raw at
 * NATIVE rate always, independent of which variant/decimated-rate is chosen downstream) -- */
#define TILT_FRAME_LEN 80
#define TILT_HOP 34
#define TILT_MAX_FRAMES 1200        /* (8s native / hop) + margin -- same sizing as combo13_opt */

/* SHARED transient scratch (2026-08-06, RAM consolidation) -- sized to the largest single need
 * among the buffers it replaces (scratch_feat_a/b, scratch_tilts -- see the field's own comment
 * in the struct below for why they can share one region, and why scratch_Sext deliberately can't
 * join them). */
#define SCRATCH_SHARED_LEN (FEAT_MAX_V > TILT_MAX_FRAMES ? FEAT_MAX_V : TILT_MAX_FRAMES)

typedef struct { float val; bool valid; float conf; bool conf_valid; } StreamLogEntry;
typedef struct { float val; bool valid; } StreamTiltLogEntry;

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

    /* -- time-to-first-display optimization (2026-08-05, EARLY RELEASE attempt) -- counts
     * consecutive ticks the growing-window candidate (win_teal, computed every tick regardless
     * of the wait) agrees with the INDEPENDENT native-rate tilt trace, using the same tolerance
     * (AGREEMENT_BPM_TOL) the final weighted-median stage already uses for its own agreement
     * bonus. Deliberately requires agreement from a SECOND, differently-derived signal rather than
     * self-consistency alone -- a harmonic lock in the growing window can itself stay self-
     * consistent for several seconds (empirically confirmed elsewhere in this project's history),
     * so self-consistency alone is not sufficient evidence to release early. Reset on any
     * is_new_event/is_flatline, same as every other event-driven counter in this struct. */
    int early_release_streak;

    StreamLogEntry cons_buf_log[NLOG_V];
    StreamTiltLogEntry tilt_log[NLOG_V];
    int   log_head;
    int   log_count;

    float last_accepted_teal_bpm;
    bool  last_accepted_teal_bpm_valid;
    int   teal_jump_streak;

    float bpm_delay_anchor_bpm;
    bool  bpm_delay_anchor_valid;
    int   bpm_delay_streak;

    float peak_mean_below_since_el;

    /* -- streaming tilt-trace state (unchanged design from combo13_opt, task 7) -- */
    float pitch_bp_z[4];
    float kumar_recent[TILT_FRAME_LEN];
    int   kumar_recent_head;
    int   kumar_recent_count;
    long  total_filtered;
    long  next_frame_idx;
    float tilt_ring[TILT_MAX_FRAMES];
    float running_max_rms;
    float last_t_carry;

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
     * local stack array the old scratch_tilts-based version also needed). Each use is fully
     * consumed before the next begins, so one buffer sized to the largest need (the tilt trace's
     * own TILT_MAX_FRAMES) replaces all 3, saving ~6.4KB net.
     * scratch_Sext[EXT_S_LEN_V] deliberately does NOT join this pool, despite looking like a
     * plausible 4th candidate: check_harmonic_confirmed uses it as an OUTER accumulator across a
     * whole call to add_autocorr_robust, which itself uses scratch_shared as its OWN internal
     * `prod` scratch during that exact same call -- the two would alias and corrupt each other if
     * merged. Caught by tracing actual concurrent liveness, not assumed. */
    float scratch_shared[SCRATCH_SHARED_LEN];
    float scratch_Sext[EXT_S_LEN_V];
};

#endif /* COMBO13_STREAM_INTERNAL_H */
