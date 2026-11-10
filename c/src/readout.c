/* Block F, readout (src/device/readout.py): when the first reading is due. Gates: not before
 * HR_MIN_BLOCKS, and not while the periodicity is below HR_MIN_PERIODICITY or the activation
 * SD below HR_MIN_ACTIVATION_SD (no heartbeats). Path: slow/irregular when the estimate is below
 * HR_SLOW_BPM or the picked-beat CV is at least HR_REGULAR_MAX_CV. Regular: due when the frame
 * and the tracker agree and the probability within ±3 BPM reaches the confidence setting;
 * slow/irregular: from slow_irregular_after_s at HR_SLOW_IRREGULAR_CONFIDENCE within ±10 BPM;
 * forced at latest_s when the mass within ±HR_CONF_ROUGH_BPM reaches HR_FORCED_MIN_MASS.
 * No heartbeat in a frame: periodicity below HR_LOST_PERIODICITY (lower than the reading gate,
 * so irregular rhythms are kept) or activation SD below HR_MIN_ACTIVATION_SD. After the first
 * reading, readings pause in such frames, and HR_LOST_FRAMES of them in a row end the
 * measurement (estimator: restart).
 * Times are compared in blocks, exact for thresholds on the block grid. */
#include <math.h>

#include "hr.h"

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

/* 1 when the first reading is due at this frame. */
int hr_first_due(const hr_state_t *s, const hr_frame_info_t *f, float rough_mass, int agree)
{
    if (isnan(f->estimate_bpm) || s->n_blocks < HR_MIN_BLOCKS
        || f->periodicity < HR_MIN_PERIODICITY || f->activation_sd < HR_MIN_ACTIVATION_SD)
        return 0;
    if (s->n_blocks >= s->latest_blocks)
        return rough_mass >= HR_FORCED_MIN_MASS;
    if (f->path == HR_PATH_REGULAR)
        return agree && f->p_fine >= s->settings.confidence;
    return s->n_blocks >= s->slow_irregular_blocks && f->p_rough >= HR_SLOW_IRREGULAR_CONFIDENCE;
}

/* 1 when the frame has no heartbeat. */
int hr_heart_absent(const hr_frame_info_t *f)
{
    return f->periodicity < HR_LOST_PERIODICITY || f->activation_sd < HR_MIN_ACTIVATION_SD;
}

/* Count a frame after the first reading; 1 once HR_LOST_FRAMES in a row had no heartbeat. */
int hr_lost(hr_state_t *s, int absent)
{
    s->absent_frames = absent ? (uint16_t)(s->absent_frames + 1) : 0;
    return s->absent_frames >= HR_LOST_FRAMES;
}
