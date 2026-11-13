/* combo13_stream_internal.h -- state layout and fixed sizing for the combo13 streaming heart-rate
 * front end, revision 0.12.
 *
 * This header fixes the front end's shape at compile time: decimated sample rate, frame rate, and
 * autocorrelation lag range. There is only one shipped configuration (what upstream called
 * "STREAM_VARIANT=4" -- decimate 4000Hz raw audio to 400Hz, extract envelope frames at 80Hz, search
 * lags 24-120 frames, i.e. 40-200 BPM). Earlier variants explored native-rate detection and other
 * decimation ratios; none of that survives here since only one configuration ever shipped.
 */
#ifndef COMBO13_STREAM_INTERNAL_H
#define COMBO13_STREAM_INTERNAL_H

#include <stdbool.h>
#include <stdint.h>

#define COMBO13_VERSION "0.12"

/* -- front-end rate/sizing constants -- */
#define FRAME_DS      5          /* decimated samples per envelope frame -> 400/5 = 80Hz frame rate */
#define LO_LAG        24         /* shortest lag searched, in frames (200 BPM) */
#define HI_LAG        120        /* longest lag searched, in frames (40 BPM) */
#define S_LEN         (HI_LAG - LO_LAG)          /* width of the autocorrelation lag axis */
#define DEC_STRIDE    10         /* 4000Hz / 10 = 400Hz decimated rate */
#define DEC_MAX       3200       /* 8s of history at 400Hz -- the largest analysis window ever used */
#define FEAT_MAX      (DEC_MAX / FRAME_DS + 4)   /* longest envelope-frame history (+ small margin) */

/* Harmonic veto searches an extended lag range above the candidate's own lag (up to ~2.18x) to look
 * for corroborating structure at roughly twice the candidate rate. EXT_HI_MAX is that search
 * ceiling: ceil(2.1814 * HI_LAG) + 2, rounded up with a small margin. */
#define EXT_HI_MAX    270
#define EXT_S_LEN     (EXT_HI_MAX - LO_LAG)

#define NLOG          16         /* trailing-candidate log depth; only the most recent
                                     WEIGHTED_MED_TRAILING_N=8 entries are ever read, the rest is
                                     margin for ticks where no candidate was pushed */

/* Streaming front-end chunk/carry sizing: samples are cleaned (artifact removal + declick) in
 * ~100ms chunks with a fixed carry region from the previous chunk, so a spike or dropout near a
 * chunk boundary still has enough context on both sides to be detected and interpolated cleanly. */
#define DEC_CHUNK_N   68
#define DEC_CARRY_N   48

#define SCRATCH_SHARED_LEN FEAT_MAX

typedef struct { float val; bool valid; float conf; bool conf_valid; } StreamLogEntry;
typedef struct { float val; bool valid; } StreamAgreeLogEntry;

/* Streaming replacement for "sort the whole analysis window for a percentile": each chunk's own
 * exact percentile (via quickselect) is EMA-blended into a persistent estimate, so the baseline
 * tracks slowly-changing signal level in O(1) state instead of re-deriving it from a large buffer
 * every tick. For the first BASELINE_WARMUP_S seconds `ema` is instead a duration-weighted running
 * mean -- seeding the EMA directly from one noisy ~100ms chunk left the artifact/declick threshold
 * under-converged for several seconds after signal onset. */
typedef struct {
    float ema;
    bool  initialized;
    float warm_sum;
    float warm_dur;
} StreamBaseline;

struct Combo13StreamState {
    float el;                    /* elapsed time, seconds, since c13stream_init */

    /* -- onset debounce (kept for completeness; no longer gates any timing decision below since
     * SIGNAL_ONSET_THRESH only ever rejects true digital silence -- see c13stream_tick()) -- */
    bool  sig_was_onset_ok;
    int   onset_off_streak;
    int   onset_on_streak;
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
    bool  was_flatline;
    /* True once a bpm has ever been displayed this session -- distinguishes a genuine reconnect
     * from the initial cold-start bootstrap (also a long flatline period, but one that needs no
     * extra settling beyond the ordinary first-contact wait). */
    bool  ever_displayed;

    float prev_last_event_el;
    float frozen_wait_s;

    /* 1.0f/powf(LO_LAG+i, LAG_POW) for every lag bin, precomputed once in c13stream_init() since
     * LAG_POW is a fixed compile-time constant -- avoids a powf() call per lag bin, per candidate,
     * every tick (the autocorrelation weighting is applied at 3 call sites). */
    float lag_pow_recip[S_LEN];

    /* Consecutive ticks the raw candidate has agreed with the independent agreement signal (flux),
     * within AGREEMENT_BPM_TOL -- lets the settling wait after an event be cut short once two
     * independently-derived estimates agree, rather than always waiting the full CONS_WAIT_S.
     * Reset on any new event or flatline. */
    int early_release_streak;

    StreamLogEntry       cons_buf_log[NLOG];      /* trailing pushed candidates + their confidence */
    StreamAgreeLogEntry  agree_log[NLOG];          /* trailing flux agreement values, same indices */
    StreamAgreeLogEntry  template_corr_log[NLOG];  /* trailing morphology-template correlations */
    int   log_head;
    int   log_count;

    float last_accepted_teal_bpm;
    bool  last_accepted_teal_bpm_valid;
    int   teal_jump_streak;

    float bpm_delay_anchor_bpm;
    bool  bpm_delay_anchor_valid;
    int   bpm_delay_streak;

    float peak_mean_below_since_el;   /* flatline detector's own debounce timer */

    /* -- streaming decimation (continuous anti-alias filter + stride-keep) -- */
    float dec_aa_z[4];
    int   dec_stride_phase;

    /* -- streaming artifact removal (chunk + carry buffer at the decimated rate) -- */
    float dec_chunk_buf[DEC_CARRY_N + DEC_CHUNK_N];
    int   dec_chunk_len;
    StreamBaseline art_baseline;   /* robust-percentile tracker feeding artifact detect/extend */
    StreamBaseline med_baseline;   /* median tracker feeding declick + spike detection */
    /* Wall-clock time of the start of the next not-yet-processed chunk, on the same timeline `el`
     * advances -- used to timestamp detected spikes at chunk granularity. */
    float chunk_time_cursor;

    /* Count of decimated samples seen so far (saturating at DEC_MAX) -- used by every "is there
     * enough history yet" gate (window sizing, harmonic-veto readiness), independent of how much
     * of that history is still resident in the per-band envelope rings below. */
    int   dec_sample_count;

    /* Persistent per-band bandpass filter + envelope state, updated once per new decimated sample
     * as it arrives. Every FRAME_DS-th sample, each band's current envelope value is pushed into
     * band_env_ring at frame rate -- every downstream feature extractor just gathers trailing
     * frames from these rings, with no raw filtering repeated per tick. */
    float   band_filt_z[3][4];
    float   band_env_z[3];
    int     band_frame_phase;
    int16_t band_env_ring[3][FEAT_MAX];
    int     band_env_ring_len;
    int     band_env_ring_head;

    /* Persists across the whole tick (written by the summed-envelope feature extractor, read much
     * later by the harmonic veto), so it can't share memory with the general scratch pool below. */
    float scratch_sumenv[FEAT_MAX];

    /* General-purpose scratch, reused strictly sequentially within one c13stream_tick() call by
     * whichever stage currently needs a FEAT_MAX-sized workspace (band gathering, autocorrelation
     * products, flux trace, flatline peak-mean scan). Each use fully consumes its result before the
     * next stage starts, so one shared buffer replaces what would otherwise be several same-sized
     * buffers live at once. */
    float scratch_shared[SCRATCH_SHARED_LEN];

    /* Extended-range autocorrelation workspace for the harmonic veto -- kept separate from
     * scratch_shared because the veto uses it as an outer accumulator across a call that itself
     * uses scratch_shared internally; sharing them would alias and corrupt both. */
    float scratch_Sext[EXT_S_LEN];
};

#endif /* COMBO13_STREAM_INTERNAL_H */
