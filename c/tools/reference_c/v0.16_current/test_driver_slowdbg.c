#include <stdio.h>
#include <stdlib.h>
#include "combo13_stream.h"
#include "combo13_stream_internal.h"
int main(int argc,char**argv){FILE*f=fopen(argv[1],"rb");fseek(f,0,SEEK_END);long nb=ftell(f);fseek(f,0,SEEK_SET);long n=nb/4;float*d=malloc(nb);fread(d,4,n,f);fclose(f);
 double fs=atof(argv[2]);int chunk=(int)((double)c13stream_tick_s()*fs+0.5);static Combo13StreamState st;c13stream_init(&st);
 printf("t,bpm,valid,raw,rawvalid,slow_valid,slow_bpm,slow_ratio,slow_active,harm\n");
 for(long i=0;i<n;i+=chunk){int c=(int)((i+chunk<=n)?chunk:(n-i));c13stream_push(&st,d+i,c);Combo13StreamTick r=c13stream_tick(&st);
 printf("%.1f,%.1f,%d,%.1f,%d,%d,%.1f,%.2f,%d,%d\n",r.t,r.bpm,r.bpm_valid,r.raw_candidate_bpm,r.raw_candidate_valid,st.dbg_slow_valid,st.dbg_slow_bpm,st.dbg_slow_ratio,st.slow_override_active,r.harmonic_confirmed);}
 return 0;}
