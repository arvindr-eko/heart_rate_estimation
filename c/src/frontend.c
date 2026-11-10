/* Block A, front end (src/device/frontend.py): band-pass filter bank and log energy.
 * Input: int16 samples scaled by 1 / 32768 (exact). Per band HR_N_SECTIONS biquads, direct
 * form II transposed in the operation order of scipy sosfilt (a0 = 1); states start in steady
 * state for the first sample so a DC offset does not ring through. Output per band: log of the
 * mean energy over each HR_DECIMATION samples.
 * The section loop computes what CMSIS-DSP arm_biquad_cascade_df2T_f32 computes. */
#include <math.h>

#include "hr.h"

/* Zero filter states; set from the first sample of the first block. */
void hr_frontend_init(hr_frontend_t *fe)
{
    for (int b = 0; b < HR_N_BANDS; b++)
        for (int k = 0; k < HR_N_SECTIONS; k++)
            fe->z[b][k][0] = fe->z[b][k][1] = 0.0f;
    fe->started = 0;
}

#define PCM_SCALE (1.0f / 32768.0f)

/* Filter one block; log mean energy per band and envelope interval. */
void hr_frontend_process(hr_frontend_t *fe, const int16_t pcm[HR_BLOCK_SAMPLES],
                         float env[HR_N_BANDS][HR_ENV_PER_BLOCK])
{
    /* steady state for the level of the first sample */
    if (!fe->started) {
        for (int b = 0; b < HR_N_BANDS; b++)
            for (int k = 0; k < HR_N_SECTIONS; k++)
                for (int j = 0; j < 2; j++)
                    fe->z[b][k][j] = hr_zi_step[(b * HR_N_SECTIONS + k) * 2 + j]
                                     * ((float)pcm[0] * PCM_SCALE);
        fe->started = 1;
    }
    for (int b = 0; b < HR_N_BANDS; b++) {
        for (int e = 0; e < HR_ENV_PER_BLOCK; e++) {
            float energy = 0.0f;
            for (int i = 0; i < HR_DECIMATION; i++) {
                float v = (float)pcm[e * HR_DECIMATION + i] * PCM_SCALE;
                for (int k = 0; k < HR_N_SECTIONS; k++) {
                    const float *c = &hr_sos[(b * HR_N_SECTIONS + k) * 6];
                    float *z = fe->z[b][k];
                    float y = c[0] * v + z[0];
                    z[0] = c[1] * v - c[4] * y + z[1];
                    z[1] = c[2] * v - c[5] * y;
                    v = y;
                }
                energy += v * v;
            }
            env[b][e] = logf(energy / (float)HR_DECIMATION + HR_LOG_FLOOR);
        }
    }
}
