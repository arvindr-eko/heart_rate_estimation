/* combo13.h -- C port of combo13_heart_rate.py, Phase A (faithful, pre-optimization baseline).
 *
 * Fixed-configuration port: this reproduces exactly ONE validated pipeline configuration
 * ("combo 13", see combo13_heart_rate.py's own docstring for provenance) -- there is no
 * runtime parameterization/sliders like the Python reference or the pcg_visualizer JS have.
 * Sample rate is fixed at 4000 Hz (matches the precomputed filter coefficients below, all of
 * which were designed for fs=4000 -- changing fs would require redesigning every coefficient
 * table, not just a constructor argument).
 *
 * Scope reduction vs. the Python reference: the dynamic-range compressor is OMITTED. combo13's
 * validated config ships with use_compressor=False, so it is dead code for this configuration;
 * dropping it here saves embedded footprint. If a future combo needs it, port
 * dynamic_range_compress() from combo13_heart_rate.py.
 *
 * DSP primitives (iir_apply, fft128, autocorrelation loops) are written as portable C so this
 * builds identically for native/WASM testing and for the real ARM target. On the real PSoC6
 * CM4F target, swap these for CMSIS-DSP equivalents (arm_biquad_cascade_df2T_f32,
 * arm_rfft_fast_f32, arm_correlate_f32) once Phase A is validated -- the call sites are
 * isolated in combo13.c's "DSP PRIMITIVES" section specifically so that swap is localized.
 */
#ifndef COMBO13_OPT_H
#define COMBO13_OPT_H

#include <stdbool.h>
#include "combo13_opt_internal.h"   /* defines struct Combo13OptState -- see that file for why it's
                                 * a complete type here: embedded callers must be able to
                                 * statically allocate an instance (no malloc), which requires
                                 * knowing its size at the call site, not just an opaque handle. */

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    float t;                  /* elapsed seconds since c13opt_init(), start of this tick */
    float bpm;                 /* displayed heart rate; only meaningful if bpm_valid */
    bool  bpm_valid;
    bool  is_flatline;
    float raw_candidate_bpm;   /* pre-guard candidate; diagnostic */
    bool  raw_candidate_valid;
    int   harmonic_confirmed;  /* 1=true, 0=false, -1=no-opinion (None in the Python ref) */
} Combo13OptTick;

typedef struct Combo13OptState Combo13OptState;

/* Returns the byte size of Combo13OptState -- useful for reporting/asserting RAM use at build time. */
unsigned long c13opt_state_size(void);

void c13opt_init(Combo13OptState *st);

/* Append newly-arrived raw audio samples (any chunk size, any number of calls between ticks).
 * Internally kept as a rolling 8-second buffer at 4000 Hz, matching the Python reference. */
void c13opt_push(Combo13OptState *st, const float *samples, int n);

/* Call once per ~250ms (TICK_S) of pushed audio. */
Combo13OptTick c13opt_tick(Combo13OptState *st);

/* ---- Optional operation-count profiling (compile with -DCOMBO13_PROFILE to enable; zero-cost
 * no-op otherwise). Hardware/compiler-independent counters used to compare optimization passes
 * against each other -- not a substitute for on-target cycle measurement, but a fair proxy since
 * the dominant costs here are sorts (n log n) and multiply-accumulates (autocorr/filter/FFT). */
typedef struct {
    unsigned long sort_calls;
    unsigned long sort_nlogn;        /* sum of n*ceil(log2(n)) across all sort calls */
    unsigned long select_n;           /* sum of n across all quickselect calls (O(n) avg each) */
    unsigned long autocorr_macs;      /* multiply-accumulates in all autocorrelation inner loops */
    unsigned long filter_taps;         /* multiply-accumulates in IIR/envelope filtering */
    unsigned long fft_calls;
} Combo13OptProfile;

void c13opt_profile_reset(void);
Combo13OptProfile c13opt_profile_get(void);

#ifdef __cplusplus
}
#endif

#endif /* COMBO13_OPT_H */
