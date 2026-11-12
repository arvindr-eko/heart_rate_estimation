/* combo13_stream.h -- public API for the combo13 streaming heart-rate front end.
 *
 * Call sequence: c13stream_init() once, then repeatedly c13stream_push() with newly-arrived raw
 * audio samples followed by c13stream_tick() every TICK_S seconds (0.5s as of revision 0.14 --
 * see c13stream_tick_s() below, and INTEGRATION_NOTES.md at the top of this kit) of elapsed audio
 * to get the current bpm estimate. See README.md for the full signal path and usage example.
 */
#ifndef COMBO13_STREAM_H
#define COMBO13_STREAM_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct Combo13StreamTick {
  float t;                  /* elapsed time, seconds, at this tick */
  float bpm;                /* displayed bpm estimate (smoothed/combined) */
  bool bpm_valid;           /* false while suppressed (settling, jump/delay guard) or flatline */
  bool is_flatline;         /* true when no real signal is present (device off/disconnected) */
  float raw_candidate_bpm;  /* this tick's own raw estimate, before combine-stage smoothing */
  bool raw_candidate_valid; /* false when the raw estimate was vetoed or not yet available */
  int harmonic_confirmed;   /* 1=confirmed, 0=vetoed, -1=not evaluated this tick */
} Combo13StreamTick_t;

// Forward declaration
typedef struct Combo13StreamState Combo13StreamState_t;

uint32_t c13stream_state_size(void);
const char *c13stream_version(void); /* e.g. "0.14" -- see COMBO13_VERSION in combo13_stream_internal.h */
/* Seconds of audio expected between c13stream_tick() calls (TICK_S in combo13_stream.c, 0.5s as of
 * revision 0.14) -- query this instead of hardcoding a caller-side copy of the value, so a driver's
 * (or the firmware adapter's) chunk/call cadence can never silently desync from the compiled tick
 * rate. This function exists specifically because a hardcoded 0.25s assumption has already caused
 * two real bugs in this project (a native test driver and a browser deployment) when TICK_S itself
 * later changed -- see INTEGRATION_NOTES.md. */
float c13stream_tick_s(void);
void c13stream_init(Combo13StreamState_t *st);
bool c13stream_push(Combo13StreamState_t *st, const int16_t *samples, int n);
int c13stream_process_chunk(Combo13StreamState_t *st);
int c13stream_update_chunk(Combo13StreamState_t *st);
Combo13StreamTick_t *c13stream_tick(Combo13StreamState_t *st, Combo13StreamTick_t *out);

#ifdef __cplusplus
}
#endif

#endif /* COMBO13_STREAM_H */
