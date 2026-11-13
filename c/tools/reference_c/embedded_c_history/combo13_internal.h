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
#ifndef COMBO13_INTERNAL_H
#define COMBO13_INTERNAL_H

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
#define TILT_MAX_FRAMES 940          /* (RAW_MAX-TILT_FRAME_LEN)/TILT_HOP + 2 margin */

typedef struct { float val; bool valid; float conf; bool conf_valid; } Combo13LogEntry;
typedef struct { float val; bool valid; } Combo13TiltLogEntry;

struct Combo13State {
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

    Combo13LogEntry cons_buf_log[NLOG];
    Combo13TiltLogEntry tilt_log[NLOG];
    int   log_head;   /* next write index (circular) */
    int   log_count;  /* number of valid entries written so far, saturates at NLOG */

    float last_accepted_teal_bpm;
    bool  last_accepted_teal_bpm_valid;
    int   teal_jump_streak;

    float bpm_delay_anchor_bpm;
    bool  bpm_delay_anchor_valid;
    int   bpm_delay_streak;

    /* ---- scratch buffers (reused every tick; sized for the worst case) ---- */
    float scratch_absx[RAW_MAX];
    float scratch_work[RAW_MAX];
    float scratch_sorted[RAW_MAX];
    bool  scratch_grown[RAW_MAX];
    float scratch_raw_clean[RAW_MAX];
    float scratch_raw_ar[RAW_MAX];
    float scratch_raw_clean_ar[RAW_MAX];
    float scratch_seg_clean[DEC_MAX];
    float scratch_seg_clean_ar[DEC_MAX];
    float scratch_filt[DEC_MAX];
    float scratch_env[DEC_MAX];
    float scratch_feat_a[FEAT_MAX];
    float scratch_feat_b[FEAT_MAX];
    float scratch_feat_c[FEAT_MAX];
    float scratch_sumenv[FEAT_MAX];
    float scratch_kumar_bp[RAW_MAX];
    float scratch_tilt_rms[TILT_MAX_FRAMES];
    float scratch_tilts[TILT_MAX_FRAMES];
    float scratch_Sext[EXT_S_LEN];
};

#endif /* COMBO13_INTERNAL_H */
