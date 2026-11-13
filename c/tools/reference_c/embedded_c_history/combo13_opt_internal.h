/* combo13_internal.h -- struct layout + fixed buffer-sizing constants shared between combo13.h
 * (public API) and combo13.c (implementation). Split out so combo13.h can expose a COMPLETE
 * struct definition (required for embedded callers to statically allocate an instance with no
 * malloc) without cluttering the public header with implementation-only DSP constants.
 *
 * All sizing constants are plain integer literals (not derived via float arithmetic at the
 * preprocessor/array-bound level) deliberately -- an earlier draft computed e.g. RAW_MAX as
 * `(int)(8.0f * 4000.0f)` and clang accepted it only via a non-standard "VLA folded to constant"
 * extension. Plain integer literals are portable to any C99+ toolchain including the ARM target.
 */
#ifndef COMBO13_OPT_INTERNAL_H
#define COMBO13_OPT_INTERNAL_H

#include <stdbool.h>

#define RAW_MAX 32000            /* 8.0s window * 4000Hz */
#define DEC_MAX 4000              /* RAW_MAX / AA_DECIM(8) -- 500Hz domain */
#define FEAT_MAX 800               /* DEC_MAX / FRAME_DS(5) -- 100Hz feature-frame domain */
#define S_LEN 120                  /* HI_LAG(150) - LO_LAG(30) */
#define EXT_HI_MAX 340
#define EXT_S_LEN 310               /* EXT_HI_MAX - LO_LAG(30) */
#define NLOG 50                     /* matches Python's log_cap */
#define TILT_FRAME_LEN 80
#define TILT_HOP 34
/* 1200 (not the tighter 940 margin Phase A used) -- the streaming tilt ring (task 7) must hold
 * AT LEAST the max window's frame count (939) without wrapping onto still-needed entries within
 * a single tick's read; 940 would leave a 1-frame margin, too tight to be a safety net rather
 * than an exact fit. RAM cost is trivial (4 bytes/entry).
 * #undef first: combo13_internal.h (Phase A) may already be in scope in a translation unit that
 * links both variants together (e.g. compare_driver.c) with a different value for its own,
 * independent struct -- avoids a same-name/different-value macro-redefined warning. */
#ifdef TILT_MAX_FRAMES
#undef TILT_MAX_FRAMES
#endif
#define TILT_MAX_FRAMES 1200

typedef struct { float val; bool valid; float conf; bool conf_valid; } Combo13OptLogEntry;
typedef struct { float val; bool valid; } Combo13OptTiltLogEntry;

struct Combo13OptState {
    float raw_buf[RAW_MAX];
    int   raw_len;

    float el;

    bool  sig_was_onset_ok;
    int   onset_off_streak;
    int   onset_on_streak;
    float onset_candidate_el;
    float last_onset_el;     /* sentinel (very negative) = None */
    float last_spike_el;     /* sentinel (very negative) = None */
    float last_logged_spike_t;

    float prev_last_event_el;
    float frozen_wait_s;

    Combo13OptLogEntry cons_buf_log[NLOG];
    Combo13OptTiltLogEntry tilt_log[NLOG];
    int   log_head;   /* next write index (circular) */
    int   log_count;  /* number of valid entries written so far, saturates at NLOG */

    float last_accepted_teal_bpm;
    bool  last_accepted_teal_bpm_valid;
    int   teal_jump_streak;

    float bpm_delay_anchor_bpm;
    bool  bpm_delay_anchor_valid;
    int   bpm_delay_streak;

    /* ---- FIX (2026-08-04): peak-mean flatline detector -- ported from pcg_visualizer/index.html's
     * CURRENT is_flatline mechanism (panel 1f, switched 2026-07-24), which combo13_heart_rate.py's
     * own onset-debounce is_flatline was never updated to match (a known, previously-documented gap
     * -- see [[project_pcg_visualizer]] memory, "current Python pipeline's flatline gate does not
     * match the true current shipped mechanism"). Found via live BLE testing: the old onset-debounce
     * mechanism this port originally used almost never returns to flatline off-chest, while the
     * visualizer's own JS readout (using peak-mean) correctly does on the same stream. NEG_INF
     * sentinel = "not currently below threshold" (mirrors last_onset_el's convention above). */
    float peak_mean_below_since_el;

    /* ---- OPTIMIZATION (task 7): streaming tilt-trace state -- replaces re-filtering the whole
     * raw window through the pitch bandpass and re-running ~500+ FFTs every tick. The pitch
     * bandpass filter's input is raw samples directly (NOT remove_transients' adaptively
     * re-thresholded output), so unlike the rest of the pipeline its filtered value at a given
     * sample is stable once computed -- a genuine streaming/incremental target, not just a
     * memoization hack. See COMBO13_OPTIMIZATIONS.md for the causality caveat this introduces
     * (a real streaming device can't know a future loud section's RMS the way Python's
     * whole-window-at-once gate computation implicitly can) and the corpus validation of it. */
    float pitch_bp_z[4];               /* continuous pitch-bandpass filter state, NEVER reset */
    float kumar_recent[TILT_FRAME_LEN]; /* circular buffer of the most recent filtered samples */
    int   kumar_recent_head;
    int   kumar_recent_count;
    long  total_filtered;               /* total pitch-bandpass samples produced, monotonic */
    long  next_frame_idx;                /* next tilt-trace frame index to compute */
    float tilt_ring[TILT_MAX_FRAMES];    /* carry-forward-applied tilt value per frame index */
    float running_max_rms;               /* monotonic historical max frame RMS, for the gate */
    float last_t_carry;                   /* sample-and-hold value for low-RMS frames */

    /* ---- scratch buffers (reused every tick; sized for the worst case) ---- */
    float scratch_absx[RAW_MAX];
    float scratch_work[RAW_MAX];
    float scratch_sorted[RAW_MAX];
    bool  scratch_grown[RAW_MAX];
    /* OPTIMIZATION (task 8, RAM): raw_clean/raw_ar/raw_clean_ar (three separate RAW_MAX buffers
     * in Phase A) are used in a strict sequential pipeline within one tick -- each is fully
     * consumed by the next stage before the next one is written, never needed simultaneously.
     * One shared buffer saves 2*128000=256000 bytes. See the c13opt_tick() call sites (task 8
     * comment there) for the exact non-overlapping sequence this relies on. */
    float scratch_raw_stage[RAW_MAX];
    float scratch_seg_clean[DEC_MAX];
    float scratch_seg_clean_ar[DEC_MAX];
    float scratch_filt[DEC_MAX];
    float scratch_env[DEC_MAX];
    float scratch_feat_a[FEAT_MAX];
    float scratch_feat_b[FEAT_MAX];
    float scratch_feat_c[FEAT_MAX];
    float scratch_sumenv[FEAT_MAX];
    float scratch_tilts[TILT_MAX_FRAMES];  /* gather buffer for the windowed tilt-ring read */
    float scratch_Sext[EXT_S_LEN];
};

#endif /* COMBO13_OPT_INTERNAL_H */
