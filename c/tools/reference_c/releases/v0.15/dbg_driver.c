#include <stdio.h>
#include <stdlib.h>
#include "combo13_stream.h"
#include "combo13_stream_internal.h"
int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "rb"); fseek(f,0,SEEK_END); long n = ftell(f)/4; fseek(f,0,SEEK_SET);
    float *d = malloc(n*4); fread(d,4,n,f); fclose(f);
    double t0 = argc>2 ? atof(argv[2]) : 0, t1 = argc>3 ? atof(argv[3]) : 1e9;
    static Combo13StreamState st; c13stream_init(&st);
    int chunk = (int)((double)c13stream_tick_s()*4000.0+0.5);
    for (long i=0;i<n;i+=chunk){ int c=(i+chunk<=n)?chunk:(int)(n-i); c13stream_push(&st,d+i,c);
      Combo13StreamTick r=c13stream_tick(&st);
        if (r.t>=t0 && r.t<=t1){
        printf("t=%.1f bpm=%.1f v=%d raw=%.1f rv=%d harm=%d |", r.t, r.bpm, r.bpm_valid, r.raw_candidate_bpm, r.raw_candidate_valid, r.harmonic_confirmed);
        if(st.dbg_v2_valid) for(int k=0;k<st.dbg_v2_n_peaks;k++) printf(" %.1f(%.2f)%s", st.dbg_v2_peak_bpm[k], st.dbg_v2_peak_score[k], k==st.dbg_v2_winner_idx?"*":"");
        printf("\n"); } } }
    return 0; }
