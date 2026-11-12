/* Block states and functions of the estimator, one group per Python module of src/device
 * (internal; included by hr.h so that callers can allocate hr_state_t statically). */
#ifndef HR_BLOCKS_H
#define HR_BLOCKS_H

#include <stdint.h>

#include "hr_model.h"

/* Q signal quality (quality.py): averaged 25-100 Hz and 200-400 Hz band energies. */
typedef struct {
    float low, high;
    uint8_t set;                    /* a sample has been seen */
} hr_quality_t;

/* A front end (frontend.py): direct form II transposed states per band and section. */
typedef struct {
    float z[HR_N_BANDS][HR_N_SECTIONS][2];
    uint8_t started;                /* states set from the first sample */
} hr_frontend_t;

/* B causal TCN (net.py): running mean of the heart-band inputs and one ring delay line per
 * layer. */
typedef struct {
    float mean[HR_HEART_BANDS];
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
 * picked beats (R); never live across frames. ACF and NCCF as int16 codes at HR_LAG_SCALE. */
typedef struct {
    int16_t acf[HR_WIN];            /* activation ACF, lags 0 .. n - 1 */
    union {
        int16_t nccf[HR_N_NCCF];
        float prior[HR_N_STATES];
        struct {
            uint16_t maxima[HR_MAX_PEAKS];
            uint16_t beats[HR_MAX_PEAKS];
        } rhythm;
    } u;
    int16_t s1_acf[HR_S1_WIN];      /* 25-50 Hz band ACF */
    float score[HR_N_STATES];       /* YIN at each state's period, then per state (once its
                                       features are read) the scorer logit, then the tracker
                                       evidence */
} hr_scratch_t;

/* ACF peaks around the current state's lag, searched from the longest lag down as the states
 * rise (lags fall), on the activation's mean-removed lag sums r[k] rebuilt from the window on
 * the way down (full precision; peak positions do not depend on the ACF's normalisation). */
typedef struct {
    const hr_window_t *win;
    float mu;                       /* window mean */
    float head, tail;               /* sums of the first and the last n - k samples */
    float r_k, r_up;                /* r[k], r[k + 1]; walk at lag k */
    float below;                    /* largest peak position below the lag; < 0: none */
    float above;                    /* smallest peak position at or above the lag; < 0: none */
    uint16_t n, start;              /* window size, ring index of the oldest sample */
    int16_t k;
} hr_peak_cursor_t;

typedef struct {
    hr_frontend_t fe;
    hr_quality_t q;
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
    uint16_t present_frames;        /* frames in a row with a heart present */
    uint16_t quiet_samples;         /* activation samples in a row below HR_IDLE_ACTIVATION
                                       (saturated; kept across restarts) */
    uint8_t s1_n;
    uint8_t has_reading;
    hr_scratch_t scratch;
#ifdef HR_DEBUG
    float dbg_env[HR_FE_OUT][HR_ENV_PER_BLOCK];
    float dbg_act[HR_ENV_PER_BLOCK];
    float dbg_feats[HR_N_STATES][HR_N_IN];
    float dbg_log_emission[HR_N_STATES];
    uint8_t dbg_has_features;
#endif
} hr_state_t;

/* Q: per envelope sample; the high-band ratio per frame. */
void hr_quality_init(hr_quality_t *q);
void hr_quality_update(hr_quality_t *q, const float env[HR_FE_OUT]);
float hr_quality_high_band(const hr_quality_t *q);

/* A: filter one block of int16 samples (full scale 32768); the front-end values per envelope
 * interval; work: HR_BLOCK_SAMPLES floats of scratch (the source signal). */
void hr_frontend_init(hr_frontend_t *fe);
void hr_frontend_process(hr_frontend_t *fe, const int16_t pcm[HR_BLOCK_SAMPLES],
                         float work[HR_BLOCK_SAMPLES], float out[HR_FE_OUT][HR_ENV_PER_BLOCK]);

/* B: subtract the running mean of the heart bands; one 10 ms frame through the TCN,
 * activation in [0, 1]. */
void hr_net_init(hr_net_t *nn);
void hr_net_normalise(hr_net_t *nn, const float in[HR_NET_IN], float out[HR_NET_IN]);
float hr_net_step(hr_net_t *nn, const float x[HR_NET_IN]);

/* C: push one sample (ring buffer, lag sums); lag functions of the window; features. */
void hr_window_push(float *hist, float *sums, uint16_t cap, uint32_t *count, float x);
uint16_t hr_window_size(uint16_t cap, uint32_t count);
uint16_t hr_lag_functions(const float *hist, const float *sums, uint16_t cap, uint32_t count,
                          int16_t *acf, int16_t *nccf, uint16_t n_nccf, float *yin_t,
                          float *mean);
float hr_lag_interp(const int16_t *r, uint16_t n, float lag, int *valid);
float hr_acf_t(const int16_t *acf, uint16_t n, uint16_t state);
float hr_recent_periodicity(const hr_window_t *act, float period);
void hr_peak_cursor_init(hr_peak_cursor_t *pc, const hr_window_t *win, float mu);
void hr_state_features(const hr_scratch_t *sc, uint16_t n, uint16_t n_s1, float acf_t_max,
                       uint16_t state, hr_peak_cursor_t *pc, float in[HR_N_IN]);

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
float hr_presence(const hr_frame_info_t *f, float recent, float hf, int agree, float rough_mass,
                  float fine_mass);
int hr_heart_present(const hr_frame_info_t *f, float recent, int clean, float p_present);
int hr_first_due(const hr_state_t *s, const hr_frame_info_t *f, float rough_mass, int agree);
int hr_heart_absent(const hr_frame_info_t *f, float recent, int noisy, float p_present);
int hr_lost(hr_state_t *s, int absent);

#endif
