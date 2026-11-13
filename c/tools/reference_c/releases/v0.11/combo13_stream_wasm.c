/* combo13_stream_wasm.c -- thin browser/WASM-friendly wrapper around combo13_stream (the
 * RAM-reduced streaming front-end, compiled with -DSTREAM_VARIANT=2 for the visualizer deploy).
 * Same exported symbol names as combo13_wasm.c (combo13_wasm_init/push/tick/state_size) so this
 * can be dropped in as a straight replacement for pcg_visualizer/combo13_wasm.js with zero
 * changes to the page's JS -- only the algorithm running underneath changes.
 *
 * Avoids returning the Combo13StreamTick struct by value across the WASM/JS boundary (awkward
 * ABI) -- instead writes results into a small caller-provided float buffer that JS reads
 * directly out of WASM linear memory.
 *
 * Single static instance (no malloc) -- matches the embedded-firmware convention the rest of
 * this port follows; the visualizer only ever needs one.
 */
#include "v0_11_stream.h"

static Combo13StreamState g_state;

/* out[0]=t, out[1]=bpm, out[2]=bpm_valid(0/1), out[3]=is_flatline(0/1),
 * out[4]=raw_candidate_bpm, out[5]=raw_candidate_valid(0/1), out[6]=harmonic_confirmed(-1/0/1) */
#define COMBO13_WASM_OUT_N 7

void combo13_wasm_init(void) {
    c13stream_init(&g_state);
}

void combo13_wasm_push(float *samples, int n) {
    c13stream_push(&g_state, samples, n);
}

void combo13_wasm_tick(float *out) {
    Combo13StreamTick r = c13stream_tick(&g_state);
    out[0] = r.t;
    out[1] = r.bpm;
    out[2] = r.bpm_valid ? 1.0f : 0.0f;
    out[3] = r.is_flatline ? 1.0f : 0.0f;
    out[4] = r.raw_candidate_bpm;
    out[5] = r.raw_candidate_valid ? 1.0f : 0.0f;
    out[6] = (float)r.harmonic_confirmed;
}

unsigned long combo13_wasm_state_size(void) {
    return c13stream_state_size();
}

/* Returns COMBO13_VERSION ("0.1", "0.9", "0.10", ...) encoded as major*1000+minor (e.g. "0.9" ->
 * 9.0f, "0.10" -> 10.0f) rather than marshaling a C string across the WASM boundary. REPLACES the
 * original plain-atof()-as-float encoding (2026-08-08): that scheme broke the instant a minor
 * version reached two digits, since atof("0.10") and atof("0.1") are the identical float 0.1f --
 * exactly the ambiguity the original comment flagged as a future revisit ("e.g. 1.12"), triggered
 * for real on 2026-08-09 when 0.9 -> 0.10 (Daniel's explicit correction: minor versions keep
 * incrementing past single digits, they do NOT roll over to 1.0). JS side decodes via
 * `major=Math.floor(v/1000); minor=Math.round(v-major*1000)`. */
#include <stdio.h>
float combo13_wasm_version(void) {
    int major = 0, minor = 0;
    sscanf(c13stream_version(), "%d.%d", &major, &minor);
    return (float)(major * 1000 + minor);
}
