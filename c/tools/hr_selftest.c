/* Self-test of the C port on the target or a host: runs the synthetic cases of
 * hr_selftest_cases.h (generated with the model files, hr-estimation c-model) through the public
 * API and checks every reading against the Python reference: same input block, BPM within
 * HR_SELFTEST_BPM_TOL, confidence within HR_SELFTEST_CONFIDENCE_TOL, first-reading flag; no
 * reading without a heart sound. Checks the API contract (argument checks, settings kept on
 * error, reset) and reports the time per call of ordinary and frame blocks.
 * Platform hooks, defined when compiling: HR_SELFTEST_PRINTF (default printf);
 * HR_SELFTEST_TICKS() with HR_SELFTEST_TICK_UNIT, a 32-bit up-counting timer (default: the DWT
 * cycle counter on Cortex-M3/M4/M7/M33, the monotonic clock in ns on a POSIX host);
 * HR_SELFTEST_CPU_HZ, the core clock, with a cycle timer: CPU load and frame-block time.
 * Output uses integer formats only (no float printf support needed). On the target, call
 * hr_selftest() from a task with at least 1 KB of free stack (0: pass); HR_SELFTEST_MAIN adds
 * main() for host builds. */
#if !defined(__arm__) && !defined(_POSIX_C_SOURCE)
#define _POSIX_C_SOURCE 199309L     /* host: clock_gettime */
#endif
#include <math.h>
#include <stdint.h>
#ifndef HR_SELFTEST_PRINTF
#include <stdio.h>
#define HR_SELFTEST_PRINTF printf
#endif
#if !defined(__arm__)
#include <time.h>
#endif

#include "hr_blocks.h"
#include "hr_selftest_cases.h"

#if defined(HR_SELFTEST_TICKS)
/* Platform timer: nothing to start. */
static void ticks_start(void)
{
}
#elif defined(__ARM_ARCH_7EM__) || defined(__ARM_ARCH_7M__) || defined(__ARM_ARCH_8M_MAIN__)
#define DEMCR (*(volatile uint32_t *)0xE000EDFCu)       /* debug exception and monitor control */
#define DWT_CTRL (*(volatile uint32_t *)0xE0001000u)
#define DWT_CYCCNT (*(volatile uint32_t *)0xE0001004u)
#define HR_SELFTEST_TICKS() DWT_CYCCNT
#define HR_SELFTEST_TICK_UNIT "cycles"
/* Enable the DWT cycle counter (DEMCR.TRCENA, DWT_CTRL.CYCCNTENA). */
static void ticks_start(void)
{
    DEMCR |= 1u << 24;
    DWT_CYCCNT = 0u;
    DWT_CTRL |= 1u;
}
#elif defined(__arm__)
#error "no cycle counter known for this core: define HR_SELFTEST_TICKS() and HR_SELFTEST_TICK_UNIT"
#else
#define HR_SELFTEST_TICKS() host_ns()
#define HR_SELFTEST_TICK_UNIT "ns"
/* Monotonic clock in ns modulo 2^32: differences up to 4.29 s are exact. */
static uint32_t host_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)((uint64_t)ts.tv_sec * 1000000000u + (uint64_t)ts.tv_nsec);
}

/* Host clock: nothing to start. */
static void ticks_start(void)
{
}
#endif

typedef struct {
    uint64_t total;
    uint32_t max, calls;
} timing_t;

/* Add one call's duration in ticks. */
static void add(timing_t *t, uint32_t ticks)
{
    t->total += ticks;
    t->max = ticks > t->max ? ticks : t->max;
    t->calls++;
}

/* Mean ticks per call. */
static unsigned long mean(const timing_t *t)
{
    return (unsigned long)(t->calls ? t->total / t->calls : 0u);
}

/* Value x100 rounded to an integer, for printing without float formats. */
static long x100(float x)
{
    return (long)(100.0f * x + (x < 0.0f ? -0.5f : 0.5f));
}

/* One reading against the reference; prints and returns 1 on a mismatch. */
static int check_reading(const char *name, uint16_t k, uint32_t block, const hr_reading_t *r,
                         const hr_selftest_reading_t *e)
{
    if (block == e->block && fabsf(r->bpm - e->bpm) <= HR_SELFTEST_BPM_TOL
        && fabsf(r->confidence - e->confidence) <= HR_SELFTEST_CONFIDENCE_TOL
        && r->first == e->first)
        return 0;
    HR_SELFTEST_PRINTF("  %s reading %u: block %lu (expected %lu), BPM x100 %ld (%ld), "
                       "confidence x100 %ld (%ld), first %u (%u)\n", name, (unsigned)k,
                       (unsigned long)block, (unsigned long)e->block, x100(r->bpm),
                       x100(e->bpm), x100(r->confidence), x100(e->confidence),
                       (unsigned)r->first, (unsigned)e->first);
    return 1;
}

/* Run one case from a fresh state, timing every call; number of failed checks. */
static int run_case(hr_state_t *s, const hr_selftest_case_t *c, timing_t *ordinary,
                    timing_t *frame)
{
    static const int16_t zeros[HR_BLOCK_SAMPLES] = {0};
    const uint32_t n_blocks = c->n_samples / HR_BLOCK_SAMPLES;
    uint16_t k = 0;
    int errors = hr_init(s, 0) != HR_OK;
    for (uint32_t b = 0; b < n_blocks; b++) {
        const int16_t *pcm = c->pcm ? &c->pcm[b * HR_BLOCK_SAMPLES] : zeros;
        hr_reading_t r;
        const uint32_t t0 = HR_SELFTEST_TICKS();
        const int due = hr_process_block(s, pcm, &r);
        add((b + 1u) % HR_FRAME_BLOCKS ? ordinary : frame, HR_SELFTEST_TICKS() - t0);
        if (!due)
            continue;
        if (k < c->n_readings)
            errors += check_reading(c->name, k, b + 1u, &r, &c->readings[k]);
        k++;
    }
    if (k != c->n_readings) {
        HR_SELFTEST_PRINTF("  %s: %u readings, expected %u\n", c->name, (unsigned)k,
                           (unsigned)c->n_readings);
        errors++;
    }
    return errors;
}

/* API contract on the state after a case with readings: latest reading kept, reset ends the
 * measurement, argument checks, settings kept on error. */
static int check_api(hr_state_t *s)
{
    hr_settings_t settings, kept;
    hr_reading_t r;
    hr_frame_info_t info;
    int errors = hr_get_reading(s, &r) != 1;
    errors += hr_init(0, 0) != HR_ERR_ARG;
    hr_reset(s);
    hr_get_frame_info(s, &info);
    errors += hr_get_reading(s, &r) != 0 || info.phase != HR_PHASE_WARMUP;
    hr_get_settings(s, &settings);
    settings.confidence = 0.0f;
    errors += hr_set_settings(s, &settings) != HR_ERR_ARG;
    hr_get_settings(s, &kept);
    errors += kept.confidence != HR_DEFAULT_CONFIDENCE;
    settings = kept;
    settings.slow_irregular_confidence = 1.5f;
    errors += hr_set_settings(s, &settings) != HR_ERR_ARG;
    return errors;
}

/* Run every case and the API checks; print the results and the timing; 0 when all pass. */
int hr_selftest(void)
{
    static hr_state_t state;
    timing_t ordinary = {0u, 0u, 0u}, frame = {0u, 0u, 0u};
    int failed = 0, api = -1;
    ticks_start();
    HR_SELFTEST_PRINTF("hr_selftest: version %s, model %s, state %lu bytes, input ring %lu\n",
                       hr_version(), hr_model_id(), (unsigned long)hr_state_size(),
                       (unsigned long)sizeof(state.ring));
    for (int i = 0; i < HR_SELFTEST_N_CASES; i++) {
        const hr_selftest_case_t *c = &hr_selftest_cases[i];
        const int errors = run_case(&state, c, &ordinary, &frame);
        HR_SELFTEST_PRINTF("  %-14s %2u readings  %s\n", c->name, (unsigned)c->n_readings,
                           errors ? "FAIL" : "PASS");
        failed += errors != 0;
        if (api < 0 && c->n_readings)
            api = check_api(&state);
    }
    api = api != 0;                          /* no case with readings: fail */
    HR_SELFTEST_PRINTF("  %-14s              %s\n", "api", api ? "FAIL" : "PASS");
    failed += api != 0;
    HR_SELFTEST_PRINTF("  ordinary block: mean %lu, max %lu %s (%lu calls)\n", mean(&ordinary),
                       (unsigned long)ordinary.max, HR_SELFTEST_TICK_UNIT,
                       (unsigned long)ordinary.calls);
    HR_SELFTEST_PRINTF("  frame block:    mean %lu, max %lu %s (%lu calls)\n", mean(&frame),
                       (unsigned long)frame.max, HR_SELFTEST_TICK_UNIT,
                       (unsigned long)frame.calls);
    if (ordinary.total + frame.total == 0u)
        HR_SELFTEST_PRINTF("  timer: no ticks counted (cycle counter not running)\n");
#ifdef HR_SELFTEST_CPU_HZ
    {
        /* load: mean cycles per block over the cycles of one block period, x10000 */
        const uint64_t per_block = (uint64_t)HR_SELFTEST_CPU_HZ * HR_BLOCK_SAMPLES / HR_FS_HZ;
        const uint64_t calls = (uint64_t)ordinary.calls + frame.calls;
        const uint64_t load = calls ? (ordinary.total + frame.total) * 10000u / calls / per_block
                                    : 0u;
        const uint64_t frame_us = (uint64_t)frame.max * 1000000u / HR_SELFTEST_CPU_HZ;
        HR_SELFTEST_PRINTF("  CPU load %lu.%02lu %% at %lu Hz; largest frame block %lu us\n",
                           (unsigned long)(load / 100u), (unsigned long)(load % 100u),
                           (unsigned long)HR_SELFTEST_CPU_HZ, (unsigned long)frame_us);
    }
#endif
    HR_SELFTEST_PRINTF("hr_selftest: %s\n", failed ? "FAIL" : "PASS");
    return failed;
}

#ifdef HR_SELFTEST_MAIN
/* Host entry point. */
int main(void)
{
    return hr_selftest() ? 1 : 0;
}
#endif
