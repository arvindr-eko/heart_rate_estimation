/* combo13_stream.h -- public API for the combo13 streaming heart-rate front end.
 *
 * Call sequence: c13stream_init() once, then repeatedly c13stream_push() with newly-arrived raw
 * audio samples followed by c13stream_tick() every TICK_S seconds (0.25s) of elapsed audio to get
 * the current bpm estimate. See README.md for the full signal path and usage example.
 */
#ifndef COMBO13_STREAM_H
#define COMBO13_STREAM_H

#include <stdbool.h>
#include "combo13_stream_internal.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    float t;                    /* elapsed time, seconds, at this tick */
    float bpm;                  /* displayed bpm estimate (smoothed/combined) */
    bool  bpm_valid;            /* false while suppressed (settling, jump/delay guard) or flatline */
    bool  is_flatline;          /* true when no real signal is present (device off/disconnected) */
    float raw_candidate_bpm;    /* this tick's own raw estimate, before combine-stage smoothing */
    bool  raw_candidate_valid;  /* false when the raw estimate was vetoed or not yet available */
    int   harmonic_confirmed;   /* 1=confirmed, 0=vetoed, -1=not evaluated this tick */
} Combo13StreamTick;

typedef struct Combo13StreamState Combo13StreamState;

unsigned long c13stream_state_size(void);
const char *c13stream_version(void);   /* e.g. "0.12" -- see COMBO13_VERSION in combo13_stream_internal.h */
void c13stream_init(Combo13StreamState *st);
void c13stream_push(Combo13StreamState *st, const float *samples, int n);
Combo13StreamTick c13stream_tick(Combo13StreamState *st);

#ifdef __cplusplus
}
#endif

#endif /* COMBO13_STREAM_H */
