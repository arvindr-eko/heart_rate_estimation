/* Block states and functions of the estimator, one group per Python module of src/device
 * (internal; included by hr.h so that callers can allocate hr_state_t statically). */
#ifndef HR_BLOCKS_H
#define HR_BLOCKS_H

#include <stdint.h>

#include "hr_model.h"

/* A front end (frontend.py): direct form II transposed states per band and section. */
typedef struct {
    float z[HR_N_BANDS][HR_N_SECTIONS][2];
    uint8_t started;                /* states set from the first sample */
} hr_frontend_t;

/* B causal TCN (net.py): running input mean and one ring delay line per layer. */
typedef struct {
    float mean[HR_NET_IN];
    float line[HR_NET_LINE_TOTAL];  /* layer i: [2 x dilation slots][input channels] */
    uint16_t oldest[HR_NET_LAYERS]; /* slot of the oldest input per layer */
    uint8_t mean_set;
} hr_net_t;

/* C window of one input (features.py): ring buffer and lag sums. */
typedef struct {
    float hist[HR_WIN];
    float sums[HR_WIN];
    uint32_t count;                 /* samples pushed */
} hr_window_t;

typedef struct {
    float hist[HR_S1_WIN];
    float sums[HR_S1_WIN];
    uint32_t count;
} hr_s1_window_t;

/* Per-frame scratch: lag functions and scores for D, then the tracker prior (E), then the
 * picked beats (R); never live across frames. */
typedef struct {
    float acf[HR_WIN];              /* activation ACF, lags 0 .. n - 1 */
    union {
        struct {
            float nccf[HR_N_NCCF];
            float yin[HR_N_YIN];
        } lag;
        float prior[HR_N_STATES];
        struct {
            uint16_t maxima[HR_MAX_PEAKS];
            uint16_t beats[HR_MAX_PEAKS];
        } rhythm;
    } u;
    float peaks[HR_MAX_PEAKS];      /* ACF peak positions (parabolic), ascending */
    float s1_acf[HR_S1_WIN];        /* 25-50 Hz band ACF */
    float acf_t[HR_N_STATES];       /* activation ACF at each state's period (0 if invalid) */
    float score[HR_N_STATES];       /* scorer logits, then the tracker evidence */
} hr_scratch_t;

typedef struct {
    hr_frontend_t fe;
    hr_net_t nn;
    hr_window_t act;                /* activation, HR_WIN samples at HR_ENV_RATE_HZ */
    hr_s1_window_t s1;              /* 25-50 Hz band, decimated by HR_S1_DECIMATION */
    float s1_pending[HR_S1_DECIMATION];
    float posterior[HR_N_STATES];
    hr_settings_t settings;
    uint32_t slow_irregular_blocks; /* settings in blocks */
    uint32_t latest_blocks;
    uint32_t n_blocks;
    uint32_t first_block;           /* block of the first reading (valid if has_reading) */
    uint32_t last_block;            /* block of the latest reading */
    hr_reading_t last;
    hr_frame_info_t info;
    uint16_t absent_frames;         /* frames in a row without a heartbeat (after the first
                                       reading) */
    uint8_t s1_n;
    uint8_t has_reading;
    hr_scratch_t scratch;
#ifdef HR_DEBUG
    float dbg_env[HR_N_BANDS][HR_ENV_PER_BLOCK];
    float dbg_act[HR_ENV_PER_BLOCK];
    float dbg_feats[HR_N_STATES][HR_N_IN];
    float dbg_log_emission[HR_N_STATES];
    uint8_t dbg_has_features;
#endif
} hr_state_t;

/* A: filter one block of int16 samples (full scale 32768), log mean energy per band and
 * envelope interval. */
void hr_frontend_init(hr_frontend_t *fe);
void hr_frontend_process(hr_frontend_t *fe, const int16_t pcm[HR_BLOCK_SAMPLES],
                         float env[HR_N_BANDS][HR_ENV_PER_BLOCK]);

/* B: subtract the running mean; one 10 ms frame through the TCN, activation in [0, 1]. */
void hr_net_init(hr_net_t *nn);
void hr_net_normalise(hr_net_t *nn, const float in[HR_NET_IN], float out[HR_NET_IN]);
float hr_net_step(hr_net_t *nn, const float x[HR_NET_IN]);

/* C: push one sample (ring buffer, lag sums); lag functions of the window; features. */
void hr_window_push(float *hist, float *sums, uint16_t cap, uint32_t *count, float x);
uint16_t hr_window_size(uint16_t cap, uint32_t count);
uint16_t hr_lag_functions(const float *hist, const float *sums, uint16_t cap, uint32_t count,
                          float *acf, float *nccf, uint16_t n_nccf, float *yin, uint16_t n_yin);
uint16_t hr_acf_peaks(const float *acf, uint16_t n, float *peaks);
float hr_lag_interp(const float *r, uint16_t n, float lag, int *valid);
void hr_state_features(const hr_scratch_t *sc, uint16_t n, uint16_t n_peaks, uint16_t n_s1,
                       float acf_t_max, uint16_t state, float in[HR_N_IN]);

/* D: logit of one state from its inputs; log-softmax over the states. */
float hr_scorer_logit(const float in[HR_N_IN]);
void hr_log_softmax(float *z, uint16_t n);

/* E: forward step (evidence NULL: prediction only) and estimate. */
void hr_tracker_init(float posterior[HR_N_STATES]);
void hr_tracker_update(float posterior[HR_N_STATES], float prior[HR_N_STATES],
                       const float *evidence);
float hr_tracker_estimate(const float posterior[HR_N_STATES]);

/* R: picked-beat interval CV and beat-count rate of the activation window. */
void hr_rhythm_analyse(const hr_window_t *act, float hr, uint16_t *maxima, uint16_t *beats,
                       float *cv, float *beat_bpm);

/* F: posterior mass near hr, calibrated probability, path, first-reading rule, loss rule. */
float hr_mass_within(const float posterior[HR_N_STATES], float hr, float half_width_bpm);
float hr_probability(const float weights[3], float mass, float periodicity);
int hr_first_due(const hr_state_t *s, const hr_frame_info_t *f, float rough_mass, int agree);
int hr_heart_absent(const hr_frame_info_t *f);
int hr_lost(hr_state_t *s, int absent);

#endif
