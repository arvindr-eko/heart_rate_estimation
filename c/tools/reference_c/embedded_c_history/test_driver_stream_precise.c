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
    /* Chunk size tracks the ACTUAL compiled TICK_S (via c13stream_tick_s()) rather than a
     * hardcoded/caller-supplied value -- a caller-supplied tick length can silently desync from
     * whatever TICK_S the linked build was actually compiled with, corrupting every timestamp. */
    int chunk = (int)((double)c13stream_tick_s() * fs + 0.5);

    static Combo13StreamState st;
    c13stream_init(&st);

    for (long i = 0; i < n; i += chunk) {
        int this_chunk = (int)((i + chunk <= n) ? chunk : (n - i));
        c13stream_push(&st, data + i, this_chunk);
        Combo13StreamTick r = c13stream_tick(&st);
        char bpm_s[16];
        if (r.bpm_valid) snprintf(bpm_s, sizeof(bpm_s), "%.6f", r.bpm);
        else snprintf(bpm_s, sizeof(bpm_s), "--");
        printf("t=%6.2fs  bpm=%6s  flatline=%s  harmonic_confirmed=%s  raw_cand=%s\n",
               r.t, bpm_s, r.is_flatline ? "True" : "False",
               r.harmonic_confirmed < 0 ? "None" : (r.harmonic_confirmed ? "True" : "False"),
               r.raw_candidate_valid ? "yes" : "--");
    }
    free(data);
    printf("STATE_SIZE_BYTES=%lu\n", c13stream_state_size());
    printf("COMBO13_VERSION=%s\n", c13stream_version());
    return 0;
}
