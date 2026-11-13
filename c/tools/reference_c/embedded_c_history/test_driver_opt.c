/* test_driver.c -- reads a raw float32 PCM dump (produced by export_raw_f32.py) and feeds it
 * through the combo13 C port tick-by-tick, printing the same fields the Python CLI prints, for
 * direct diffing against `python combo13_heart_rate.py <wav>`. */
#include <stdio.h>
#include <stdlib.h>
#include "combo13_opt.h"

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s <raw_f32_pcm_file> <fs>\n", argv[0]);
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
    int chunk = (int)(0.25 * fs + 0.5);

    static Combo13OptState st;
    c13opt_init(&st);

    for (long i = 0; i < n; i += chunk) {
        int this_chunk = (int)((i + chunk <= n) ? chunk : (n - i));
        c13opt_profile_reset();
        c13opt_push(&st, data + i, this_chunk);
        Combo13OptTick r = c13opt_tick(&st);
        Combo13OptProfile p = c13opt_profile_get();
        fprintf(stderr, "PROF t=%.2f sort_calls=%lu sort_nlogn=%lu select_n=%lu autocorr_macs=%lu filter_taps=%lu fft_calls=%lu\n",
                r.t, p.sort_calls, p.sort_nlogn, p.select_n, p.autocorr_macs, p.filter_taps, p.fft_calls);
        char bpm_s[16];
        if (r.bpm_valid) snprintf(bpm_s, sizeof(bpm_s), "%.1f", r.bpm);
        else snprintf(bpm_s, sizeof(bpm_s), "--");
        printf("t=%6.2fs  bpm=%6s  flatline=%s  harmonic_confirmed=%s  raw_cand=%s\n",
               r.t, bpm_s, r.is_flatline ? "True" : "False",
               r.harmonic_confirmed < 0 ? "None" : (r.harmonic_confirmed ? "True" : "False"),
               r.raw_candidate_valid ? "yes" : "--");
    }
    free(data);
    printf("STATE_SIZE_BYTES=%lu\n", c13opt_state_size());
    return 0;
}
