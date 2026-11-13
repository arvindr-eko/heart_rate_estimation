/* compare_driver.c -- runs combo13 (Phase A, frozen oracle) and combo13_opt (the version being
 * optimized) side-by-side over the same raw f32 PCM, tick-by-tick, and reports any divergence.
 * This is the harness every optimization pass (tasks 4-8) is checked against before being
 * trusted, matching this project's own established practice of diffing one changed variable
 * at a time against a known-good baseline rather than re-deriving trust from scratch. */
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include "combo13.h"
#include "combo13_opt.h"

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s <raw_f32_pcm_file> <fs> [tol_bpm]\n", argv[0]);
        return 1;
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { fprintf(stderr, "cannot open %s\n", argv[1]); return 1; }
    fseek(f, 0, SEEK_END);
    long nbytes = ftell(f);
    fseek(f, 0, SEEK_SET);
    long n = nbytes / (long)sizeof(float);
    float *data = (float *)malloc(n * sizeof(float));
    fread(data, sizeof(float), n, f);
    fclose(f);

    double fs = atof(argv[2]);
    float tol = (argc > 3) ? (float)atof(argv[3]) : 0.15f;
    int chunk = (int)(0.25 * fs + 0.5);

    static Combo13State ref;
    static Combo13OptState opt;
    c13_init(&ref);
    c13opt_init(&opt);

    int n_ticks = 0, n_mismatch = 0;
    for (long i = 0; i < n; i += chunk) {
        int this_chunk = (int)((i + chunk <= n) ? chunk : (n - i));
        c13_push(&ref, data + i, this_chunk);
        c13opt_push(&opt, data + i, this_chunk);
        Combo13Tick r = c13_tick(&ref);
        Combo13OptTick o = c13opt_tick(&opt);
        n_ticks++;

        bool bad = false;
        if (r.is_flatline != o.is_flatline) bad = true;
        if (r.harmonic_confirmed != o.harmonic_confirmed) bad = true;
        if (r.bpm_valid != o.bpm_valid) bad = true;
        else if (r.bpm_valid && fabsf(r.bpm - o.bpm) > tol) bad = true;

        if (bad) {
            n_mismatch++;
            if (n_mismatch <= 10) {
                char rb[16], ob[16];
                snprintf(rb, sizeof(rb), r.bpm_valid ? "%.2f" : "--", r.bpm);
                snprintf(ob, sizeof(ob), o.bpm_valid ? "%.2f" : "--", o.bpm);
                fprintf(stderr, "tick %3d (t=%.2f): ref bpm=%s flat=%d harm=%d  |  opt bpm=%s flat=%d harm=%d\n",
                        n_ticks - 1, r.t, rb, r.is_flatline, r.harmonic_confirmed,
                        ob, o.is_flatline, o.harmonic_confirmed);
            }
        }
        printf("%.2f,%d,%.4f,%d,%.4f\n", r.t,
               r.bpm_valid ? 1 : 0, r.bpm_valid ? r.bpm : 0.0f,
               o.bpm_valid ? 1 : 0, o.bpm_valid ? o.bpm : 0.0f);
    }
    fprintf(stderr, "%s: %d/%d ticks mismatched\n", argv[1], n_mismatch, n_ticks);
    free(data);
    return n_mismatch > 0 ? 1 : 0;
}
