/* Host driver for the FIRMWARE API (int16 push in 8-sample pieces, process_chunk/update_chunk/tick once per
 * tick period, heap-allocated state) -- mirrors audio_io_task.c's call pattern. Same CSV columns as the dev
 * test_driver_stream_csv.c so outputs can be diffed directly. Input: raw float32 file (int16-valued samples). */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include "combo13_stream.h"

int main(int argc, char **argv)
{
  if (argc < 3) { fprintf(stderr, "usage: %s <raw_f32_pcm_file> <fs>\n", argv[0]); return 1; }
  FILE *f = fopen(argv[1], "rb");
  if (!f) { fprintf(stderr, "cannot open %s\n", argv[1]); return 1; }
  fseek(f, 0, SEEK_END); long nbytes = ftell(f); fseek(f, 0, SEEK_SET);
  long n = nbytes / (long)sizeof(float);
  float *data = (float *)malloc(n * sizeof(float));
  if (fread(data, sizeof(float), n, f) != (size_t)n) { return 2; }
  fclose(f);

  double fs = atof(argv[2]);
  int chunk = (int)((double)c13stream_tick_s() * fs + 0.5);

  Combo13StreamState_t *st = (Combo13StreamState_t *)malloc(c13stream_state_size());
  c13stream_init(st);
  fprintf(stderr, "STATE_SIZE_BYTES=%u\nCOMBO13_VERSION=%s\n", (unsigned)c13stream_state_size(), c13stream_version());

  printf("t,bpm,bpm_valid,is_flatline,raw_bpm,raw_valid,harm\n");
  for (long i = 0; i < n; i += chunk) {
    int this_chunk = (int)((i + chunk <= n) ? chunk : (n - i));
    int16_t buf[8];
    for (int j = 0; j < this_chunk; j += 8) {
      int k = 0;
      for (; k < 8 && j + k < this_chunk; k++) {
        buf[k] = (int16_t)data[i + j + k];
      }
      c13stream_push(st, buf, k);
    }
    Combo13StreamTick_t r;
    c13stream_process_chunk(st);
    c13stream_update_chunk(st);
    c13stream_tick(st, &r);
    printf("%.4f,%.6f,%d,%d,%.6f,%d,%d\n", r.t, r.bpm, r.bpm_valid, r.is_flatline, r.raw_candidate_bpm,
           r.raw_candidate_valid, r.harmonic_confirmed);
  }
  free(data);
  free(st);
  return 0;
}
