/* combo13_stream.h -- public API for the RAM-reduced streaming-front-end variants.
 * Compile with -DSTREAM_VARIANT=1, 2, 3, or 4 (see combo13_stream_internal.h).
 * Current plan of record (revision 0.1, see COMBO13_VERSION) is STREAM_VARIANT=4. */
#ifndef COMBO13_STREAM_H
#define COMBO13_STREAM_H

#include <stdbool.h>
#include "v0_11_stream_internal.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    float t;
    float bpm;
    bool  bpm_valid;
    bool  is_flatline;
    float raw_candidate_bpm;
    bool  raw_candidate_valid;
    int   harmonic_confirmed;  /* 1=true, 0=false, -1=no-opinion */
} Combo13StreamTick;

typedef struct Combo13StreamState Combo13StreamState;

unsigned long c13stream_state_size(void);
const char *c13stream_version(void);  /* e.g. "0.1" -- see COMBO13_VERSION in combo13_stream_internal.h */
void c13stream_init(Combo13StreamState *st);
void c13stream_push(Combo13StreamState *st, const float *samples, int n);
Combo13StreamTick c13stream_tick(Combo13StreamState *st);

#ifdef COMBO13_EXPOSE_DEBUG
/* Visualization-only accessor (2026-08-07) -- PEAK_MEAN_DIFF_THRESH is `static const` (internal
 * linkage) inside combo13_stream.c, so a separate debug WASM wrapper .c file can't reference it by
 * name directly; this is the single source of truth a wrapper calls instead of hardcoding a
 * duplicate copy of the constant. */
float c13stream_dbg_peak_mean_thresh(void);
/* Same rationale, for B1's onset/signal-level panel (SIGNAL_ONSET_THRESH). */
float c13stream_dbg_signal_onset_thresh(void);
/* Same rationale, for the Weighted-median combine panel (2026-08-07) -- the tent-weighting shape
 * (bpm_favor_center) and agreement bonus (teal_size_factor) constants, so JS can recompute
 * each pushed candidate's own real combine weight instead of hardcoding a duplicate copy. */
float c13stream_dbg_tent_peak_bpm(void);
float c13stream_dbg_tent_peak_h(void);
float c13stream_dbg_tent_lo_h(void);
float c13stream_dbg_tent_hi_h(void);
float c13stream_dbg_agreement_bpm_tol(void);
float c13stream_dbg_agree_bonus(void);
/* Same rationale, for the Gate Candidate panel's early-release streak (2026-08-07, per Daniel's
 * "how is tilt-agrees connected" follow-up) -- lets JS recompute the real streak-threshold check
 * instead of hardcoding a duplicate copy of EARLY_RELEASE_MIN_TICKS/EARLY_RELEASE_MAX_SAVE_S. */
int   c13stream_dbg_early_release_min_ticks(void);
float c13stream_dbg_early_release_max_save_s(void);
#endif

#ifdef __cplusplus
}
#endif

#endif /* COMBO13_STREAM_H */
