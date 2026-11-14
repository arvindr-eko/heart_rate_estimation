/* Block A, front end (src/device/frontend.py): band-pass filter bank, log energy and
 * sound-source features.
 * Input: int16 samples scaled by 1 / 32768 (exact). Per band HR_N_SECTIONS biquads, direct
 * form II transposed in the operation order of scipy sosfilt (a0 = 1); states start in steady
 * state for the first sample so a DC offset does not ring through. Output per envelope interval
 * (HR_DECIMATION samples), HR_FE_OUT values: log mean energy of the HR_HEART_BANDS heart bands;
 * ln(E band / E heart bands) of the bands above; with HR_SOURCE_FEATURES, voicing and
 * impulsiveness of the source signal (sum of the band outputs from band HR_SOURCE_FROM up),
 * which is kept for the block in the caller's work buffer.
 * The section loop computes what CMSIS-DSP arm_biquad_cascade_df2T_f32 computes. */
#include <math.h>

#include "hr_blocks.h"

/* Zero filter states; set from the first sample of the first block. */
void hr_frontend_init(hr_frontend_t *fe)
{
    for (int b = 0; b < HR_N_BANDS; b++)
        for (int k = 0; k < HR_N_SECTIONS; k++)
            fe->z[b][k][0] = fe->z[b][k][1] = 0.0f;
    fe->started = 0;
}

#define PCM_SCALE (1.0f / 32768.0f)

#if HR_SOURCE_FEATURES
/* Highest NCCF at a local maximum over the voicing lags of the interval starting at s (lag
 * pairs forward where the block holds them, backward otherwise); 0 without one. */
static float voicing(const float x[HR_BLOCK_SAMPLES], int s)
{
    float ea = 0.0f;
    for (int i = 0; i < HR_DECIMATION; i++)
        ea += x[s + i] * x[s + i];
    float best = 0.0f, n2 = 0.0f, n1 = 0.0f;
    for (int lag = HR_VOICING_LAG_MIN; lag <= HR_VOICING_LAG_MAX; lag++) {
        const int b0 = (s + HR_DECIMATION + lag <= HR_BLOCK_SAMPLES) ? s + lag : s - lag;
        float eb = 0.0f, num = 0.0f;
        for (int i = 0; i < HR_DECIMATION; i++) {
            eb += x[b0 + i] * x[b0 + i];
            num += x[s + i] * x[b0 + i];
        }
        const float energy = ea * eb;
        const float n = energy > HR_NCCF_FLOOR_SQ ? num / sqrtf(energy) : 0.0f;
        /* lag - 1 is a local maximum */
        if (lag >= HR_VOICING_LAG_MIN + 2 && n1 > n2 && n1 >= n && n1 > best)
            best = n1;
        n2 = n1;
        n1 = n;
    }
    return best;
}

/* ln(highest mean energy over HR_IMPULSE_WINDOW samples / mean energy) of the interval at s. */
static float impulsiveness(const float x[HR_BLOCK_SAMPLES], int s)
{
    const int n_win = HR_DECIMATION / HR_IMPULSE_WINDOW;
    float peak = 0.0f, total = 0.0f;
    for (int w = 0; w < n_win; w++) {
        float e = 0.0f;
        for (int i = 0; i < HR_IMPULSE_WINDOW; i++) {
            const float v = x[s + w * HR_IMPULSE_WINDOW + i];
            e += v * v;
        }
        e /= (float)HR_IMPULSE_WINDOW;
        if (e > peak)
            peak = e;
        total += e;
    }
    return logf(peak + HR_LOG_FLOOR) - logf(total / (float)n_win + HR_LOG_FLOOR);
}
#endif

/* Filter one block; the front-end values per envelope interval (file comment). */
void hr_frontend_process(hr_frontend_t *fe, const int16_t pcm[HR_BLOCK_SAMPLES],
                         float work[HR_BLOCK_SAMPLES], float out[HR_FE_OUT][HR_ENV_PER_BLOCK])
{
    float energy[HR_N_BANDS][HR_ENV_PER_BLOCK];
    /* steady state for the level of the first sample */
    if (!fe->started) {
        for (int b = 0; b < HR_N_BANDS; b++)
            for (int k = 0; k < HR_N_SECTIONS; k++)
                for (int j = 0; j < 2; j++)
                    fe->z[b][k][j] = hr_zi_step[(b * HR_N_SECTIONS + k) * 2 + j]
                                     * ((float)pcm[0] * PCM_SCALE);
        fe->started = 1;
    }
    for (int i = 0; i < HR_BLOCK_SAMPLES; i++)
        work[i] = 0.0f;
    /* band-pass filters: mean energy per interval, source signal */
    for (int b = 0; b < HR_N_BANDS; b++) {
        for (int e = 0; e < HR_ENV_PER_BLOCK; e++) {
            float sum = 0.0f;
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
                sum += v * v;
                if (b >= HR_SOURCE_FROM)
                    work[e * HR_DECIMATION + i] += v;
            }
            energy[b][e] = sum / (float)HR_DECIMATION;
        }
    }
    /* heart bands: log energy; bands above: log ratio to the heart-band energy */
    for (int e = 0; e < HR_ENV_PER_BLOCK; e++) {
        float heart = 0.0f;
        for (int b = 0; b < HR_HEART_BANDS; b++) {
            out[b][e] = logf(energy[b][e] + HR_LOG_FLOOR);
            heart += energy[b][e];
        }
        for (int b = HR_HEART_BANDS; b < HR_N_BANDS; b++)
            out[b][e] = logf(energy[b][e] + HR_LOG_FLOOR) - logf(heart + HR_LOG_FLOOR);
#if HR_SOURCE_FEATURES
        out[HR_N_BANDS][e] = voicing(work, e * HR_DECIMATION);
        out[HR_N_BANDS + 1][e] = impulsiveness(work, e * HR_DECIMATION);
#endif
    }
}
