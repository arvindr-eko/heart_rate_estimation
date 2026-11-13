/* TEMPORARY investigation driver (2026-08-09) -- dumps the raw (pre-detrend) and detrended
 * autocorrelation curves per tick, plus enough context (bpm/valid/raw-candidate) to let the
 * analysis script filter to only the ticks that are genuinely reading the target heart rate.
 * Per Daniel's "investigate if there is a morphology of the autocorrelation of different heart
 * rates... start by looking at files in the 60bpm range... generate a mean autocorrelation."
 * Reuses the SAME wrapper functions combo13_stream_wasm_debug.c already exposes to the browser
 * debug panel (combo13_wasm_dbg_autocorr_ptr() etc.) rather than touching internal struct fields
 * directly -- native build here, WASM build in the browser, same interface either way.
 * Not meant to ship. */
#include <stdio.h>
#include <stdlib.h>
#include "combo13_stream.h"

#ifndef COMBO13_EXPOSE_DEBUG
#error "must be compiled with -DCOMBO13_EXPOSE_DEBUG"
#endif

extern void combo13_wasm_init(void);
extern void combo13_wasm_push(float *samples, int n);
extern void combo13_wasm_tick(float *out);
extern float *combo13_wasm_dbg_autocorr_ptr(void);
extern float *combo13_wasm_dbg_win_teal_curve_ptr(void);
extern int combo13_wasm_dbg_autocorr_max(void);
extern float combo13_wasm_dbg_ffs(void);

#define OUT_N 50

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s <raw_f32_pcm_file> <fs>\n", argv[0]); return 1; }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { fprintf(stderr, "cannot open %s\n", argv[1]); return 1; }
    fseek(f, 0, SEEK_END); long nbytes = ftell(f); fseek(f, 0, SEEK_SET);
    long n = nbytes / (long)sizeof(float);
    float *data = (float *)malloc(n * sizeof(float));
    fread(data, sizeof(float), n, f);
    fclose(f);

    double fs = atof(argv[2]);
    int chunk = (int)(0.25 * fs + 0.5);

    combo13_wasm_init();
    float out[OUT_N];
    int ac_max = combo13_wasm_dbg_autocorr_max();
    float ffs = combo13_wasm_dbg_ffs();
    float *ac_raw = combo13_wasm_dbg_autocorr_ptr();
    float *win_teal_curve = combo13_wasm_dbg_win_teal_curve_ptr();

    fprintf(stderr, "ac_max=%d ffs=%.4f\n", ac_max, ffs);

    for (long i = 0; i < n; i += chunk) {
        int this_chunk = (int)((i + chunk <= n) ? chunk : (n - i));
        combo13_wasm_push(data + i, this_chunk);
        combo13_wasm_tick(out);
        float t = out[0];
        int bpm_valid = (int)out[2];
        float bpm = out[1];
        int raw_valid = (int)out[5];
        float raw_bpm = out[4];
        int ac_valid = (int)out[7];
        int ac_lo = (int)out[8];
        int wt_valid = (int)out[9];

        printf("%.2f,%d,%.4f,%d,%.4f,%d,%d,%d", t, bpm_valid, bpm, raw_valid, raw_bpm, ac_valid, ac_lo, wt_valid);
        if (ac_valid) {
            for (int k = 0; k < ac_max; k++) printf(",%.6g", ac_raw[k]);
        } else {
            for (int k = 0; k < ac_max; k++) printf(",");
        }
        if (wt_valid) {
            for (int k = 0; k < ac_max; k++) printf(",%.6g", win_teal_curve[k]);
        } else {
            for (int k = 0; k < ac_max; k++) printf(",");
        }
        printf("\n");
    }
    free(data);
    return 0;
}
