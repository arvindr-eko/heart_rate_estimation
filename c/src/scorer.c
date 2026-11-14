/* Block D, scorer (src/device/scorer.py): one MLP (HR_N_IN -> HR_HIDDEN ReLU -> 1) scores every
 * HR state from its own inputs; the log-softmax over the states is the tracker's evidence. The
 * input standardisation of the reference is folded into the first layer by the generator
 * (hr_sc_w1 = w1 / std, hr_sc_b1 = b1 - (mean / std) @ w1). */
#include <math.h>

#include "hr_blocks.h"

/* Score of one state from its inputs (standardisation folded into the weights). */
float hr_scorer_logit(const float in[HR_N_IN])
{
    float hidden[HR_HIDDEN];
    for (int k = 0; k < HR_HIDDEN; k++)
        hidden[k] = 0.0f;
    for (int j = 0; j < HR_N_IN; j++) {
        const float x = in[j];
        const float *row = &hr_sc_w1[j * HR_HIDDEN];
        for (int k = 0; k < HR_HIDDEN; k++)
            hidden[k] += x * row[k];
    }
    float out = 0.0f;
    for (int k = 0; k < HR_HIDDEN; k++) {
        const float h = hidden[k] + hr_sc_b1[k];
        if (h > 0.0f)
            out += h * hr_sc_w2[k];
    }
    return out + hr_sc_b2[0];
}

/* In-place log-softmax. */
void hr_log_softmax(float *z, uint16_t n)
{
    float top = z[0];
    for (uint16_t i = 1; i < n; i++)
        top = z[i] > top ? z[i] : top;
    float sum = 0.0f;
    for (uint16_t i = 0; i < n; i++) {
        z[i] -= top;
        sum += expf(z[i]);
    }
    const float lse = logf(sum);
    for (uint16_t i = 0; i < n; i++)
        z[i] -= lse;
}
