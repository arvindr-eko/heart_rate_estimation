/* test_driver_tilt_variants.c (2026-08-07) -- prints tilt_bpm/tilt_valid per tick, straight from
 * g_state's own debug fields (compiled with -DCOMBO13_EXPOSE_DEBUG). Same file-reading/chunking
 * convention as test_driver_stream.c, just a different printf -- built once per TILT_AC_ROBUST/
 * LAGPOW/DETREND combination via -D flags, for the tilt-vs-ground-truth corpus sweep. */
#include <stdio.h>
#include <stdlib.h>
#include "combo13_stream.h"

#ifndef COMBO13_EXPOSE_DEBUG
#error "test_driver_tilt_variants.c must be compiled with -DCOMBO13_EXPOSE_DEBUG"
#endif

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

    static Combo13StreamState st;
    c13stream_init(&st);

    for (long i = 0; i < n; i += chunk) {
        int this_chunk = (int)((i + chunk <= n) ? chunk : (n - i));
        c13stream_push(&st, data + i, this_chunk);
        Combo13StreamTick r = c13stream_tick(&st);
        char tilt_s[16];
        if (st.dbg_tilt_autocorr_valid) snprintf(tilt_s, sizeof(tilt_s), "%.2f", st.dbg_tilt_bpm);
        else snprintf(tilt_s, sizeof(tilt_s), "--");
        printf("t=%6.2fs  tilt_bpm=%8s\n", r.t, tilt_s);
    }
    free(data);
    return 0;
}
