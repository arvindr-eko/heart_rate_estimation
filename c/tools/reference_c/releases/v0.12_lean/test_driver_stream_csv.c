/* test_driver_stream_csv.c -- full-precision CSV driver for tick-by-tick validation against the
 * shipped embedded_c/ build. Prints every Combo13StreamTick field at full float precision, one row
 * per tick.
 *
 * Build (NODERAWFS -- must compile directly under this target filename; renaming a built .js file
 * afterward breaks it, since NODERAWFS embeds a reference to its own .wasm filename):
 *   emcc -O2 -sNODERAWFS=1 -o test_driver_stream_csv.js test_driver_stream_csv.c combo13_stream.c
 *
 * Usage: node test_driver_stream_csv.js <raw_f32_pcm_file> <fs>
 */
#include <stdio.h>
#include <stdlib.h>
#include "combo13_stream.h"

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

    printf("t,bpm,bpm_valid,is_flatline,raw_bpm,raw_valid,harm\n");
    for (long i = 0; i < n; i += chunk) {
        int this_chunk = (int)((i + chunk <= n) ? chunk : (n - i));
        c13stream_push(&st, data + i, this_chunk);
        Combo13StreamTick r = c13stream_tick(&st);
        printf("%.4f,%.6f,%d,%d,%.6f,%d,%d\n", r.t, r.bpm, r.bpm_valid, r.is_flatline,
               r.raw_candidate_bpm, r.raw_candidate_valid, r.harmonic_confirmed);
    }
    free(data);
    fprintf(stderr, "STATE_SIZE_BYTES=%lu\n", c13stream_state_size());
    fprintf(stderr, "COMBO13_VERSION=%s\n", c13stream_version());
    return 0;
}
