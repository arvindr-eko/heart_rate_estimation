/* Block R, rhythm (src/device/rhythm.py): beats picked from the activation window and their
 * regularity. Beats: local maxima of at least HR_PEAK_MIN_REL x the window maximum, taken from
 * the highest (ties: earliest first) and kept at least HR_REFRACTORY_REL x the estimate's
 * period apart, which suppresses S2. Output: interval CV (SD / mean) and beat-count rate
 * 60 x (n - 1) / (last - first beat); NaN with fewer than HR_MIN_INTERVALS intervals. */
#include <math.h>

#include "hr_blocks.h"

/* Sample i of the window (oldest first) starting at ring index start. */
static float at(const float *hist, uint16_t start, uint16_t i)
{
    const uint16_t idx = (uint16_t)(start + i);
    return hist[idx >= HR_WIN ? idx - HR_WIN : idx];
}

/* Picked-beat interval CV and beat-count rate of the activation window. */
void hr_rhythm_analyse(const hr_window_t *act, float hr, uint16_t *maxima, uint16_t *beats,
                       float *cv, float *beat_bpm)
{
    *cv = *beat_bpm = NAN;
    if (isnan(hr))
        return;
    const uint16_t n = hr_window_size(HR_WIN, act->count);
    const float period = HR_LAG_TO_BPM / hr;
    if (n < 3 || !isfinite(period))
        return;
    const float *h = act->hist;
    const uint16_t start = (uint16_t)((act->count - n) % HR_WIN);
    float top = at(h, start, 0);
    for (uint16_t i = 1; i < n; i++) {
        const float v = at(h, start, i);
        top = v > top ? v : top;
    }
    /* local maxima above the threshold, then sorted by height (stable) */
    const float threshold = HR_PEAK_MIN_REL * top;
    uint16_t m = 0;
    for (uint16_t k = 1; k + 1 < n; k++) {
        const float a = at(h, start, k - 1), b = at(h, start, k), d = at(h, start, k + 1);
        if (b > a && b >= d && b >= threshold)
            maxima[m++] = k;
    }
    for (uint16_t i = 1; i < m; i++) {
        const uint16_t key = maxima[i];
        const float v = at(h, start, key);
        uint16_t j = i;
        while (j > 0 && at(h, start, maxima[j - 1]) < v) {
            maxima[j] = maxima[j - 1];
            j--;
        }
        maxima[j] = key;
    }
    /* greedy selection with the refractory distance */
    const float refractory = HR_REFRACTORY_REL * period;
    uint16_t nb = 0;
    for (uint16_t i = 0; i < m; i++) {
        int keep = 1;
        for (uint16_t j = 0; j < nb && keep; j++) {
            const int gap = (int)maxima[i] - (int)beats[j];
            keep = (float)(gap < 0 ? -gap : gap) >= refractory;
        }
        if (keep)
            beats[nb++] = maxima[i];
    }
    if (nb < HR_MIN_INTERVALS + 1)
        return;
    for (uint16_t i = 1; i < nb; i++) {
        const uint16_t key = beats[i];
        uint16_t j = i;
        while (j > 0 && beats[j - 1] > key) {
            beats[j] = beats[j - 1];
            j--;
        }
        beats[j] = key;
    }
    /* interval statistics */
    const uint16_t n_rr = nb - 1;
    const float span = (float)(beats[nb - 1] - beats[0]);
    const float mean = span / (float)n_rr;
    float ss = 0.0f;
    for (uint16_t i = 0; i < n_rr; i++) {
        const float dev = (float)(beats[i + 1] - beats[i]) - mean;
        ss += dev * dev;
    }
    *cv = sqrtf(ss / (float)n_rr) / mean;
    *beat_bpm = HR_LAG_TO_BPM * (float)n_rr / span;
}
