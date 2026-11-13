/* compare_driver_stream.c -- runs combo13_opt (the validated, RAM-heavy optimized reference) and
 * combo13_stream (the RAM-reduced streaming redesign, compiled with a specific -DSTREAM_VARIANT)
 * side by side, tick-by-tick, printing CSV for corpus_compare_stream.py to classify against
 * ground truth. Same pattern as compare_driver.c, one level further out. */
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include "combo13_opt.h"
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

    static Combo13OptState ref;
    static Combo13StreamState opt;
    c13opt_init(&ref);
    c13stream_init(&opt);

    for (long i = 0; i < n; i += chunk) {
        int this_chunk = (int)((i + chunk <= n) ? chunk : (n - i));
        c13opt_push(&ref, data + i, this_chunk);
        c13stream_push(&opt, data + i, this_chunk);
        Combo13OptTick r = c13opt_tick(&ref);
        Combo13StreamTick o = c13stream_tick(&opt);
        printf("%.2f,%d,%.4f,%d,%.4f\n", r.t,
               r.bpm_valid ? 1 : 0, r.bpm_valid ? r.bpm : 0.0f,
               o.bpm_valid ? 1 : 0, o.bpm_valid ? o.bpm : 0.0f);
    }
    free(data);
    return 0;
}
