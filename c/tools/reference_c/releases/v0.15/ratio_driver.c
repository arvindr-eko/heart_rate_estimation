#include <stdio.h>
#include <stdlib.h>
#include "combo13_stream.h"
#include "combo13_stream_internal.h"
/* usage: ratio_driver file fs -> per tick: t,slow_valid,slow_bpm,slow_ratio,bpm,bpm_valid,override */
int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "rb"); fseek(f,0,SEEK_END); long n = ftell(f)/4; fseek(f,0,SEEK_SET);
    float *d = malloc(n*4); fread(d,4,n,f); fclose(f);
    double fs = atof(argv[2]); int chunk = (int)((double)c13stream_tick_s()*fs+0.5);
    static Combo13StreamState st; c13stream_init(&st);
    printf("t,slow_valid,slow_bpm,slow_ratio,bpm,bpm_valid,override,raw_valid\n");
    for (long i=0;i<n;i+=chunk){ int c=(i+chunk<=n)?chunk:(int)(n-i); c13stream_push(&st,d+i,c);
        Combo13StreamTick r=c13stream_tick(&st);
        printf("%.2f,%d,%.2f,%.3f,%.2f,%d,%d,%d\n", r.t, st.dbg_slow_valid, st.dbg_slow_bpm, st.dbg_slow_ratio, r.bpm, r.bpm_valid, st.slow_override_active, r.raw_candidate_valid); }
    return 0; }
