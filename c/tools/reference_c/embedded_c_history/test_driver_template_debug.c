/* TEMPORARY diagnostic (2026-08-09): dumps per-tick raw_cand_bpm + template_match_corr to sanity
 * check whether the TEMPLATE_MATCH_ENABLE full-corpus regression is a real correlation-based
 * rejection or an implementation bug (e.g. bad indexing/alignment). Not meant to ship. */
#include <stdio.h>
#include <stdlib.h>
#include "combo13_stream.h"
#include "combo13_stream_internal.h"

#ifndef COMBO13_EXPOSE_DEBUG
#error "must be compiled with -DCOMBO13_EXPOSE_DEBUG"
#endif
#ifndef TEMPLATE_MATCH_ENABLE
#error "must be compiled with -DTEMPLATE_MATCH_ENABLE"
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
        printf("t=%.2f bpm=%s raw_cand=%s corr_valid=%d corr=%.4f bucket=%d\n",
               r.t, r.bpm_valid ? "yes" : "--", r.raw_candidate_valid ? "yes" : "--",
               st.dbg_template_match_corr_valid, st.dbg_template_match_corr, st.dbg_template_match_bucket);
    }
    free(data);
    return 0;
}
