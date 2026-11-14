/* Block Q, signal quality (src/device/quality.py): spectral balance of the input. Per envelope
 * sample, exponential averages (HR_Q_HF_ALPHA) of the 25-100 Hz and the 200-400 Hz band energies,
 * started at the first sample; per frame the natural log of their ratio (NO_RATIO before the
 * first sample). */
#include <math.h>

#include "hr_blocks.h"

#define ENERGY_FLOOR 1e-30f
#define NO_RATIO     (-1e30f)  /* before the first sample (-inf in the reference); below every
                                  gate and the gate input's clipping range */

/* No sample seen. */
void hr_quality_init(hr_quality_t *q)
{
    q->low = q->high = 0.0f;
    q->set = 0;
}

/* One envelope sample (band log energies): update the band energy averages. */
void hr_quality_update(hr_quality_t *q, const float env[HR_FE_OUT])
{
    const float low = expf(env[0]) + expf(env[1]), high = expf(env[3]);
    if (!q->set) {
        q->low = low;
        q->high = high;
        q->set = 1;
        return;
    }
    q->low += HR_Q_HF_ALPHA * (low - q->low);
    q->high += HR_Q_HF_ALPHA * (high - q->high);
}

/* ln(200-400 Hz / 25-100 Hz averaged energy); NO_RATIO before the first sample. */
float hr_quality_high_band(const hr_quality_t *q)
{
    if (!q->set)
        return NO_RATIO;
    return logf(q->high > ENERGY_FLOOR ? q->high : ENERGY_FLOOR)
           - logf(q->low > ENERGY_FLOOR ? q->low : ENERGY_FLOOR);
}
