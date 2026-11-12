/* Block E, tracker (src/device/tracker.py): forward HMM over the log-spaced HR states.
 * Prediction per frame: random walk (hr_kernel, HR_KERNEL_TAPS taps), jump to double or half
 * (HR_OCTAVE_SHIFT states, HR_OCTAVE_PROB) and a uniform floor (HR_FLOOR_PROB); update: prior
 * times exp(evidence - max), normalised. Estimate: posterior mean in log HR over the states
 * within HR_EST_HALFWIDTH of the most likely state. */
#include <math.h>

#include "hr_blocks.h"

#define MASS_FLOOR 1e-30f           /* estimate denominator floor, as the reference */

/* Uniform posterior. */
void hr_tracker_init(float posterior[HR_N_STATES])
{
    for (int i = 0; i < HR_N_STATES; i++)
        posterior[i] = 1.0f / (float)HR_N_STATES;
}

/* Predict into prior, then multiply by exp(evidence - max) and normalise. */
void hr_tracker_update(float posterior[HR_N_STATES], float prior[HR_N_STATES],
                       const float *evidence)
{
    const int half = (HR_KERNEL_TAPS - 1) / 2;
    const float keep = 1.0f - HR_OCTAVE_PROB - HR_FLOOR_PROB;
    const float floor_share = HR_FLOOR_PROB / (float)HR_N_STATES;
    /* predict */
    float total = 0.0f;
    for (int i = 0; i < HR_N_STATES; i++) {
        const int lo = i - half < 0 ? 0 : i - half;
        const int hi = i + half > HR_N_STATES - 1 ? HR_N_STATES - 1 : i + half;
        float drift = 0.0f;
        for (int j = lo; j <= hi; j++)
            drift += posterior[j] * hr_kernel[i + half - j];
        float octave = 0.0f;
        if (i >= HR_OCTAVE_SHIFT)
            octave += 0.5f * posterior[i - HR_OCTAVE_SHIFT];
        if (i + HR_OCTAVE_SHIFT < HR_N_STATES)
            octave += 0.5f * posterior[i + HR_OCTAVE_SHIFT];
        prior[i] = keep * drift + HR_OCTAVE_PROB * octave + floor_share;
        total += prior[i];
    }
    for (int i = 0; i < HR_N_STATES; i++)
        prior[i] = prior[i] / total;
    /* update; without evidence the posterior is the prior */
    if (evidence == 0) {
        for (int i = 0; i < HR_N_STATES; i++)
            posterior[i] = prior[i];
        return;
    }
    float top = evidence[0];
    for (int i = 1; i < HR_N_STATES; i++)
        top = evidence[i] > top ? evidence[i] : top;
    total = 0.0f;
    for (int i = 0; i < HR_N_STATES; i++) {
        posterior[i] = prior[i] * expf(evidence[i] - top);
        total += posterior[i];
    }
    for (int i = 0; i < HR_N_STATES; i++)
        posterior[i] = posterior[i] / total;
}

/* Posterior mean in log HR around the most likely state. */
float hr_tracker_estimate(const float posterior[HR_N_STATES])
{
    int best = 0;
    for (int i = 1; i < HR_N_STATES; i++)
        if (posterior[i] > posterior[best])
            best = i;
    const int lo = best - HR_EST_HALFWIDTH < 0 ? 0 : best - HR_EST_HALFWIDTH;
    const int hi = best + HR_EST_HALFWIDTH + 1 > HR_N_STATES ? HR_N_STATES
                                                             : best + HR_EST_HALFWIDTH + 1;
    float weighted = 0.0f, mass = 0.0f;
    for (int i = lo; i < hi; i++) {
        weighted += posterior[i] * (HR_GRID_LOG_MIN + HR_GRID_LOG_STEP * (float)i);
        mass += posterior[i];
    }
    return expf(weighted / (mass > MASS_FLOOR ? mass : MASS_FLOOR));
}
