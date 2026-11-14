/* Block F, readout (src/device/readout.py): when the first reading is due. Gates: not before
 * HR_MIN_BLOCKS, and not while the periodicity is below HR_MIN_PERIODICITY or the activation
 * SD below HR_MIN_ACTIVATION_SD (no heartbeats). Path: slow/irregular when the estimate is below
 * HR_SLOW_BPM or the picked-beat CV is at least HR_REGULAR_MAX_CV. Regular: due when the frame
 * and the tracker agree and the probability within ±3 BPM reaches the confidence setting;
 * slow/irregular: from slow_irregular_after_s at HR_SLOW_IRREGULAR_EARLY_CONFIDENCE within
 * ±10 BPM, from HR_SLOW_IRREGULAR_RELAXED_BLOCKS on at the slow_irregular_confidence setting;
 * forced at latest_s when the mass within ±HR_CONF_ROUGH_BPM reaches HR_FORCED_MIN_MASS.
 * Heart present (first reading): signal clean (block Q), learned presence at least
 * HR_PRESENCE_MIN, and the gates above, for HR_CONFIRM_FRAMES frames. No heartbeat in a frame:
 * signal noisy, presence below HR_PRESENCE_LOST, periodicity below HR_LOST_PERIODICITY (lower
 * than the reading gate, so irregular rhythms are kept), activation SD below
 * HR_MIN_ACTIVATION_SD or recent heartbeat below HR_RECENT_LOST. After the first reading,
 * readings pause in such frames, and HR_LOST_FRAMES of them in a row end the measurement
 * (estimator: restart).
 * Times are compared in blocks, exact for thresholds on the block grid. */
#include <math.h>

#include "hr_blocks.h"

/* Posterior mass of the states within ±half_width_bpm of hr. */
float hr_mass_within(const float posterior[HR_N_STATES], float hr, float half_width_bpm)
{
    float mass = 0.0f;
    for (int i = 0; i < HR_N_STATES; i++)
        if (fabsf(HR_LAG_TO_BPM / hr_lag[i] - hr) <= half_width_bpm)
            mass += posterior[i];
    return mass;
}

/* Logistic probability from (intercept, mass, periodicity) coefficients. */
float hr_probability(const float weights[3], float mass, float periodicity)
{
    return 1.0f / (1.0f + expf(-(weights[0] + weights[1] * mass + weights[2] * periodicity)));
}

/* Learned heart-present probability (readout.presence): MLP, one ReLU hidden layer of
 * HR_PRESENCE_HIDDEN units, over periodicity, recent heartbeat, ln activation SD, agreement, the
 * posterior masses, the high-band ratio (clipped) and its product with the periodicity; 1 without
 * a gate. */
float hr_presence(const hr_frame_info_t *f, float recent, float hf, int agree, float rough_mass,
                  float fine_mass)
{
#if HR_PRESENCE_ENABLED
    const float sd = f->activation_sd > HR_PRESENCE_SD_FLOOR ? f->activation_sd
                                                             : HR_PRESENCE_SD_FLOOR;
    const float h = hf < HR_PRESENCE_HF_LOW ? HR_PRESENCE_HF_LOW
                    : (hf > HR_PRESENCE_HF_HIGH ? HR_PRESENCE_HF_HIGH : hf);
    const float x[HR_PRESENCE_N] = {f->periodicity, recent, logf(sd), (float)agree, rough_mass,
                                    fine_mass, h, h * f->periodicity};
    float z = 0.0f;
    for (int j = 0; j < HR_PRESENCE_HIDDEN; j++) {
        float a = 0.0f;
        for (int i = 0; i < HR_PRESENCE_N; i++)
            a += x[i] * hr_presence_w1[i * HR_PRESENCE_HIDDEN + j];
        a += hr_presence_b1[j];
        z += hr_presence_w2[j] * (a > 0.0f ? a : 0.0f);
    }
    return 1.0f / (1.0f + expf(-(HR_PRESENCE_B2 + z)));
#else
    (void)f, (void)recent, (void)hf, (void)agree, (void)rough_mass, (void)fine_mass;
    return 1.0f;
#endif
}

/* 1 while a heartbeat is heard: signal clean (block Q), presence, periodicity, activation SD and
 * recent heartbeat at or above their gates. */
int hr_heart_present(const hr_frame_info_t *f, float recent, int clean, float p_present)
{
    return clean && p_present >= HR_PRESENCE_MIN && f->periodicity >= HR_MIN_PERIODICITY
           && f->activation_sd >= HR_MIN_ACTIVATION_SD && recent >= HR_RECENT_MIN;
}

/* 1 when the first reading is due at this frame (heart present for HR_CONFIRM_FRAMES). */
int hr_first_due(const hr_state_t *s, const hr_frame_info_t *f, float rough_mass, int agree)
{
    if (isnan(f->estimate_bpm) || s->n_blocks < HR_MIN_BLOCKS || !f->heart_present
        || s->present_frames < HR_CONFIRM_FRAMES)
        return 0;
    if (s->n_blocks >= s->latest_blocks)
        return rough_mass >= HR_FORCED_MIN_MASS;
    if (f->path == HR_PATH_REGULAR)
        return agree && f->p_fine >= s->settings.confidence;
    if (s->n_blocks < s->slow_irregular_blocks)
        return 0;
    return f->p_rough >= (s->n_blocks < HR_SLOW_IRREGULAR_RELAXED_BLOCKS
                              ? HR_SLOW_IRREGULAR_EARLY_CONFIDENCE
                              : s->settings.slow_irregular_confidence);
}

/* 1 when the frame has no heartbeat: signal noisy (block Q), presence below HR_PRESENCE_LOST,
 * periodicity below HR_LOST_PERIODICITY, activation SD below HR_MIN_ACTIVATION_SD or recent
 * heartbeat below HR_RECENT_LOST. */
int hr_heart_absent(const hr_frame_info_t *f, float recent, int noisy, float p_present)
{
    return noisy || p_present < HR_PRESENCE_LOST || f->periodicity < HR_LOST_PERIODICITY
           || f->activation_sd < HR_MIN_ACTIVATION_SD || recent < HR_RECENT_LOST;
}

/* Count a frame after the first reading; 1 once HR_LOST_FRAMES in a row had no heartbeat. */
int hr_lost(hr_state_t *s, int absent)
{
    s->absent_frames = absent ? (uint16_t)(s->absent_frames + 1) : 0;
    return s->absent_frames >= HR_LOST_FRAMES;
}
