/* combo13_wasm.c -- thin browser/WASM-friendly wrapper around combo13_opt (the validated,
 * optimized C port). Avoids returning the Combo13OptTick struct by value across the WASM/JS
 * boundary (awkward ABI) -- instead writes results into a small caller-provided float buffer
 * that JS reads directly out of WASM linear memory.
 *
 * Single static instance (no malloc) -- matches the embedded-firmware convention the rest of
 * this port follows; the visualizer only ever needs one.
 */
#include "combo13_opt.h"

static Combo13OptState g_state;

/* out[0]=t, out[1]=bpm, out[2]=bpm_valid(0/1), out[3]=is_flatline(0/1),
 * out[4]=raw_candidate_bpm, out[5]=raw_candidate_valid(0/1), out[6]=harmonic_confirmed(-1/0/1) */
#define COMBO13_WASM_OUT_N 7

void combo13_wasm_init(void) {
    c13opt_init(&g_state);
}

void combo13_wasm_push(float *samples, int n) {
    c13opt_push(&g_state, samples, n);
}

void combo13_wasm_tick(float *out) {
    Combo13OptTick r = c13opt_tick(&g_state);
    out[0] = r.t;
    out[1] = r.bpm;
    out[2] = r.bpm_valid ? 1.0f : 0.0f;
    out[3] = r.is_flatline ? 1.0f : 0.0f;
    out[4] = r.raw_candidate_bpm;
    out[5] = r.raw_candidate_valid ? 1.0f : 0.0f;
    out[6] = (float)r.harmonic_confirmed;
}

unsigned long combo13_wasm_state_size(void) {
    return c13opt_state_size();
}
