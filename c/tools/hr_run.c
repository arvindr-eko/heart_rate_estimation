/* Host tool: run the estimator over a raw PCM file (int16, little endian, HR_FS_HZ, mono),
 * print the readings (time from the start of the file; "first" marks the first reading of a
 * measurement), and time every call (ordinary blocks and frame blocks separately).
 * Usage: hr_run FILE [REPEAT]; REPEAT runs the file several times for stable timings, printing
 * the readings of the first run only. Input beyond MAX_INPUT_S is not read. */
#define _POSIX_C_SOURCE 199309L
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#include "hr_blocks.h"

#define MAX_INPUT_S 600             /* longest input read, s */

typedef struct {
    double total_us, max_us;
    unsigned long calls;
} timing_t;

/* Monotonic time in microseconds. */
static double now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1e6 + ts.tv_nsec * 1e-3;
}

/* Add one call's duration to a timing summary. */
static void add(timing_t *t, double us)
{
    t->total_us += us;
    t->max_us = us > t->max_us ? us : t->max_us;
    t->calls++;
}

/* Read the PCM file, run the estimator REPEAT times, print readings and timings. */
int main(int argc, char **argv)
{
    static hr_state_t state;
    static int16_t pcm[HR_FS_HZ * MAX_INPUT_S];
    if (argc < 2) {
        fprintf(stderr, "usage: %s FILE [REPEAT]\n", argv[0]);
        return 2;
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f) {
        perror(argv[1]);
        return 1;
    }
    const size_t n = fread(pcm, sizeof(int16_t), sizeof(pcm) / sizeof(pcm[0]), f);
    const int longer = fgetc(f) != EOF;
    fclose(f);
    if (longer)
        fprintf(stderr, "%s: only the first %d s are read\n", argv[1], MAX_INPUT_S);
    const int repeat = argc > 2 ? atoi(argv[2]) : 1;
    timing_t ordinary = {0, 0, 0}, frame = {0, 0, 0};
    printf("version %s, model %s, state %u bytes, %.1f s of input\n", hr_version(),
           hr_model_id(), hr_state_size(), (double)n / HR_FS_HZ);
    for (int r = 0; r < repeat; r++) {
        hr_init(&state, NULL);
        for (size_t start = 0; start + HR_BLOCK_SAMPLES <= n; start += HR_BLOCK_SAMPLES) {
            hr_reading_t reading;
            const double t0 = now_us();
            const int due = hr_process_block(&state, &pcm[start], &reading);
            const double us = now_us() - t0;
            add(state.n_blocks % HR_FRAME_BLOCKS ? &ordinary : &frame, us);
            if (due && r == 0)
                printf("t %.2f s  %.1f BPM  confidence %.2f%s\n",
                       (double)(start + HR_BLOCK_SAMPLES) / HR_FS_HZ, (double)reading.bpm,
                       (double)reading.confidence, reading.first ? "  first" : "");
        }
    }
    printf("ordinary blocks: %lu calls, mean %.1f us, max %.1f us\n", ordinary.calls,
           ordinary.total_us / (ordinary.calls ? ordinary.calls : 1), ordinary.max_us);
    printf("frame blocks:    %lu calls, mean %.1f us, max %.1f us\n", frame.calls,
           frame.total_us / (frame.calls ? frame.calls : 1), frame.max_us);
    return 0;
}
