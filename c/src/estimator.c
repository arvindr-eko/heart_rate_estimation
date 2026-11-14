/* Streaming estimator (src/device/estimator.py): blocks A-F and R behind one call per input
 * block, and the public API of hr.h. Per block: band energies (A), activation (B) and the
 * windows' per-sample updates (C). Every HR_FRAME_BLOCKS blocks: lag functions and features
 * (C), scores (D), tracker (E), gates, rhythm (R) and readout (F). After the first reading,
 * readings pause in frames without heartbeat, and the measurement restarts when the heartbeat
 * is lost (hr_lost). Before the first reading, after HR_IDLE_SAMPLES activation samples below
 * HR_IDLE_ACTIVATION, the analysis (blocks C-F, clock) restarts every frame until the
 * activation rises; front end, quality and network keep running. */
#include <math.h>

#include "hr_blocks.h"

#define BLOCK_ROUNDING 1e-4f        /* tolerance of the seconds-to-blocks conversion */
#define MAX_SETTING_S 3600.0f       /* longest accepted time setting */

/* Number of blocks at or after which a time in seconds is reached (thresholds on the block
 * grid are exact). */
static uint32_t blocks_of(float seconds)
{
    return (uint32_t)ceilf(seconds * ((float)HR_FS_HZ / (float)HR_BLOCK_SAMPLES) - BLOCK_ROUNDING);
}

/* Clear blocks C-F, the clock and the readout; front end, quality and network are kept. */
static void clear_analysis(hr_state_t *s)
{
    for (int i = 0; i < HR_WIN; i++)
        s->act.hist[i] = s->act.sums[i] = 0.0f;
    for (int i = 0; i < HR_S1_WIN; i++)
        s->s1.hist[i] = s->s1.sums[i] = 0.0f;
    s->act.count = s->s1.count = 0;
    s->s1_n = 0;
    hr_tracker_init(s->posterior);
    s->n_blocks = s->first_block = s->last_block = 0;
    s->absent_frames = s->present_frames = 0;
    s->has_reading = 0;
    s->info.t_s = 0.0f;
    s->info.estimate_bpm = s->info.frame_bpm = NAN;
    s->info.interval_cv = s->info.beat_bpm = NAN;
    s->info.periodicity = s->info.activation_sd = s->info.p_fine = s->info.p_rough = 0.0f;
    s->info.path = HR_PATH_REGULAR;
    s->info.phase = HR_PHASE_WARMUP;
    s->info.heart_present = 0;
#ifdef HR_DEBUG
    s->dbg_has_features = 0;
#endif
}

/* Clear every block state, the readout and the display; settings and queued input are kept. */
static void clear(hr_state_t *s)
{
    hr_frontend_init(&s->fe);
    hr_quality_init(&s->q);
    hr_net_init(&s->nn);
    clear_analysis(s);
    s->shown_bpm = s->pending_bpm = HR_DISPLAY_NONE;
}

/* Runtime settings of configs/device.yml. */
void hr_default_settings(hr_settings_t *settings)
{
    settings->confidence = HR_DEFAULT_CONFIDENCE;
    settings->slow_irregular_after_s = HR_DEFAULT_SLOW_IRREGULAR_AFTER_S;
    settings->slow_irregular_confidence = HR_DEFAULT_SLOW_IRREGULAR_CONFIDENCE;
    settings->latest_s = HR_DEFAULT_LATEST_S;
}

/* Validate and store the runtime settings and their block thresholds. */
hr_result_t hr_set_settings(hr_state_t *s, const hr_settings_t *settings)
{
    if (s == 0 || settings == 0)
        return HR_ERR_ARG;
    const float slow = settings->slow_irregular_after_s, latest = settings->latest_s;
    const float slow_conf = settings->slow_irregular_confidence;
    if (!(settings->confidence > 0.0f && settings->confidence <= 1.0f)
        || !(slow_conf > 0.0f && slow_conf <= 1.0f)
        || !(slow >= 0.0f && slow <= MAX_SETTING_S) || !(latest >= 0.0f && latest <= MAX_SETTING_S))
        return HR_ERR_ARG;
    s->settings = *settings;
    s->slow_irregular_blocks = blocks_of(settings->slow_irregular_after_s);
    s->latest_blocks = blocks_of(settings->latest_s);
    return HR_OK;
}

/* Current runtime settings. */
void hr_get_settings(const hr_state_t *s, hr_settings_t *settings)
{
    *settings = s->settings;
}

/* Settings (NULL: defaults), then all state cleared. */
hr_result_t hr_init(hr_state_t *s, const hr_settings_t *settings)
{
    hr_settings_t defaults;
    if (s == 0)
        return HR_ERR_ARG;
    hr_default_settings(&defaults);
    if (hr_set_settings(s, settings ? settings : &defaults) != HR_OK)
        return HR_ERR_ARG;
    clear(s);
    s->quiet_samples = 0;
    s->blocks_written = s->blocks_read = s->blocks_done = 0;
    s->ring_fill = 0;
    s->gap_pending = 0;
    return HR_OK;
}

/* New measurement with the same settings. */
void hr_reset(hr_state_t *s)
{
    clear(s);
    s->quiet_samples = 0;
}

/* Standard deviation of the activation window (two passes). */
static float activation_sd(const hr_window_t *act)
{
    const uint16_t n = hr_window_size(HR_WIN, act->count);
    if (n == 0)
        return 0.0f;
    /* the window in ring storage order: a rotation of oldest-first once full */
    float sum = 0.0f;
    for (uint16_t i = 0; i < n; i++)
        sum += act->hist[i];
    const float mean = sum / (float)n;
    float ss = 0.0f;
    for (uint16_t i = 0; i < n; i++) {
        const float d = act->hist[i] - mean;
        ss += d * d;
    }
    return sqrtf(ss / (float)n);
}

/* State nearest to hr on the log grid (first on ties). */
static int nearest_state(float hr)
{
    const int guess = (int)floorf((logf(hr) - HR_GRID_LOG_MIN) / HR_GRID_LOG_STEP);
    int best = -1;
    float best_gap = 0.0f;
    for (int i = guess - 1; i <= guess + 2; i++) {
        if (i < 0 || i >= HR_N_STATES)
            continue;
        const float gap = fabsf(HR_LAG_TO_BPM / hr_lag[i] - hr);
        if (best < 0 || gap < best_gap) {
            best = i;
            best_gap = gap;
        }
    }
    return best < 0 ? 0 : best;
}

/* Blocks C (per frame) and D: evidence in scratch.score; frame's best HR; 0 before the
 * minimum window. */
static int frame_evidence(hr_state_t *s, float *frame_bpm)
{
    hr_scratch_t *sc = &s->scratch;
    const uint16_t n = hr_window_size(HR_WIN, s->act.count);
    *frame_bpm = NAN;
    if (n < HR_WIN_MIN)
        return 0;
    float mu;
    hr_lag_functions(s->act.hist, s->act.sums, HR_WIN, s->act.count, sc->acf, sc->u.nccf,
                     HR_N_NCCF, sc->score, &mu);
    uint16_t n_s1 = hr_window_size(HR_S1_WIN, s->s1.count);
    if (n_s1 >= HR_S1_WIN_MIN)
        hr_lag_functions(s->s1.hist, s->s1.sums, HR_S1_WIN, s->s1.count, sc->s1_acf, 0, 0, 0, 0);
    else
        n_s1 = 0;
    /* maximum of the ACF at the states' periods */
    float acf_max = 0.0f;
    for (int i = 0; i < HR_N_STATES; i++) {
        const float v = hr_acf_t(sc->acf, n, (uint16_t)i);
        acf_max = (i == 0 || v > acf_max) ? v : acf_max;
    }
    /* features and score per state, rates rising (lags falling) for the peak cursor */
    hr_peak_cursor_t pc;
    hr_peak_cursor_init(&pc, &s->act, mu);
    for (int i = 0; i < HR_N_STATES; i++) {
        float in[HR_N_IN];
        hr_state_features(sc, n, n_s1, acf_max, (uint16_t)i, &pc, in);
        sc->score[i] = hr_scorer_logit(in);
#ifdef HR_DEBUG
        for (int j = 0; j < HR_N_IN; j++)
            s->dbg_feats[i][j] = in[j];
#endif
    }
    hr_log_softmax(sc->score, HR_N_STATES);
    int best = 0;
    for (int i = 1; i < HR_N_STATES; i++)
        if (sc->score[i] > sc->score[best])
            best = i;
    *frame_bpm = HR_LAG_TO_BPM / hr_lag[best];
    /* evidence: log-likelihood weighted by HR_EMISSION_BETA */
    for (int i = 0; i < HR_N_STATES; i++) {
#ifdef HR_DEBUG
        s->dbg_log_emission[i] = sc->score[i];
#endif
        sc->score[i] *= HR_EMISSION_BETA;
    }
    return 1;
}

/* Frame work: tracker, gates, rhythm, readout; 1 when a reading is due. On loss, the state is
 * cleared and the frame info kept with phase HR_PHASE_WARMUP. */
static int frame(hr_state_t *s, hr_reading_t *out)
{
    hr_scratch_t *sc = &s->scratch;
    hr_frame_info_t *f = &s->info;
    float frame_bpm;
    const int have = frame_evidence(s, &frame_bpm);
#ifdef HR_DEBUG
    s->dbg_has_features = (uint8_t)have;
#endif
    hr_tracker_update(s->posterior, sc->u.prior, have ? sc->score : 0);
    const float hr = hr_tracker_estimate(s->posterior);
    f->t_s = (float)(s->n_blocks * HR_BLOCK_SAMPLES) / (float)HR_FS_HZ;
    f->estimate_bpm = hr;
    f->frame_bpm = frame_bpm;
    f->periodicity = have ? hr_acf_t(sc->acf, hr_window_size(HR_WIN, s->act.count),
                                     (uint16_t)nearest_state(hr))
                          : 0.0f;
    f->activation_sd = activation_sd(&s->act);
    hr_rhythm_analyse(&s->act, hr, sc->u.rhythm.maxima, sc->u.rhythm.beats, &f->interval_cv,
                      &f->beat_bpm);
    /* F: path, probabilities, agreement, first-reading rule, schedule */
    const float fine = hr_mass_within(s->posterior, hr, HR_CONF_FINE_BPM);
    const float rough = hr_mass_within(s->posterior, hr, HR_CONF_ROUGH_BPM);
    const int slow_irregular = hr < HR_SLOW_BPM
                               || (!isnan(f->interval_cv) && f->interval_cv >= HR_REGULAR_MAX_CV);
    f->path = slow_irregular ? HR_PATH_SLOW_IRREGULAR : HR_PATH_REGULAR;
    f->p_fine = hr_probability(hr_cal_fine, fine, f->periodicity);
    f->p_rough = hr_probability(hr_cal_rough, rough, f->periodicity);
    const int agree = !isnan(frame_bpm) && fabsf(logf(hr / frame_bpm)) <= HR_EST_TOL_LOG;
    /* presence: block Q, recent heartbeat, learned gate */
    const float recent = isnan(hr) ? 0.0f : hr_recent_periodicity(&s->act, HR_LAG_TO_BPM / hr);
    const float hf = hr_quality_high_band(&s->q);
    const int clean = hf <= HR_HF_MAX, noisy = hf > HR_HF_LOST;
    const float p_present = hr_presence(f, recent, hf, agree, rough, fine);
    f->heart_present = (uint8_t)hr_heart_present(f, recent, clean, p_present);
    const int absent = hr_heart_absent(f, recent, noisy, p_present);
    if (s->has_reading && hr_lost(s, absent)) {
        const hr_frame_info_t ended = *f;
        clear(s);
        s->info = ended;
        s->info.phase = HR_PHASE_WARMUP;
        return 0;
    }
    s->present_frames = f->heart_present ? (uint16_t)(s->present_frames + 1) : 0;
    f->phase = have ? HR_PHASE_SEARCHING : HR_PHASE_WARMUP;
    int emit;
    if (!s->has_reading) {
        emit = hr_first_due(s, f, rough, agree);
        if (emit) {
            s->has_reading = 1;
            s->first_block = s->n_blocks;
        }
    } else {
        emit = !absent && s->n_blocks >= s->last_block + HR_OUTPUT_BLOCKS;
    }
    if (!s->has_reading && s->quiet_samples >= HR_IDLE_SAMPLES) {
        /* no heartbeat yet: the analysis starts again */
        const hr_frame_info_t ended = *f;
        clear_analysis(s);
        s->info = ended;
        s->info.phase = HR_PHASE_WARMUP;
        return 0;
    }
    if (s->has_reading)
        f->phase = HR_PHASE_READING;
    if (!emit)
        return 0;
    s->last_block = s->n_blocks;
    s->last.t_s = f->t_s;
    s->last.bpm = hr;
    s->last.confidence = f->path == HR_PATH_REGULAR ? f->p_fine : f->p_rough;
    s->last.path = f->path;
    s->last.first = (uint8_t)(s->first_block == s->n_blocks);
    s->last.bpm_valid = s->last.raw_candidate_valid = true;
    s->last.is_flatline = false;
    s->last.raw_candidate_bpm = hr;
    s->last.harmonic_confirmed = -1;
    if (out)
        *out = s->last;
    return 1;
}

/* Blocks A-C for one input block; the frame work every HR_FRAME_BLOCKS blocks. */
int hr_process_block(hr_state_t *s, const int16_t *pcm, hr_reading_t *out)
{
    float env[HR_FE_OUT][HR_ENV_PER_BLOCK];
    /* scratch.score is free between frames: the front end's source signal */
#if HR_N_STATES < HR_BLOCK_SAMPLES
#error "scratch.score is too small for the front end's work buffer"
#endif
    hr_frontend_process(&s->fe, pcm, s->scratch.score, env);
    for (int e = 0; e < HR_ENV_PER_BLOCK; e++) {
        float band[HR_NET_IN], x[HR_NET_IN];
        for (int b = 0; b < HR_NET_IN; b++)
            band[b] = env[b][e];
        hr_quality_update(&s->q, band);
        hr_net_normalise(&s->nn, band, x);
        const float a = hr_net_step(&s->nn, x);
        if (a >= HR_IDLE_ACTIVATION)
            s->quiet_samples = 0;
        else if (s->quiet_samples < UINT16_MAX)
            s->quiet_samples++;
        hr_window_push(s->act.hist, s->act.sums, HR_WIN, &s->act.count, a);
        /* 25-50 Hz band (normalised) averaged over HR_S1_DECIMATION envelope samples */
        s->s1_pending[s->s1_n++] = x[0];
        if (s->s1_n == HR_S1_DECIMATION) {
            float sum = 0.0f;
            for (int j = 0; j < HR_S1_DECIMATION; j++)
                sum += s->s1_pending[j];
            hr_window_push(s->s1.hist, s->s1.sums, HR_S1_WIN, &s->s1.count,
                           sum / (float)HR_S1_DECIMATION);
            s->s1_n = 0;
        }
#ifdef HR_DEBUG
        s->dbg_act[e] = a;
#endif
    }
#ifdef HR_DEBUG
    for (int b = 0; b < HR_FE_OUT; b++)
        for (int e = 0; e < HR_ENV_PER_BLOCK; e++)
            s->dbg_env[b][e] = env[b][e];
#endif
    s->n_blocks++;
    if (s->n_blocks % HR_FRAME_BLOCKS)
        return 0;
    return frame(s, out);
}

/* Latest reading; 0 before the first. */
int hr_get_reading(const hr_state_t *s, hr_reading_t *out)
{
    if (!s->has_reading)
        return 0;
    *out = s->last;
    return 1;
}

/* Copy of the latest frame's values. */
void hr_get_frame_info(const hr_state_t *s, hr_frame_info_t *info)
{
    *info = s->info;
}

/* Posterior over the HR states. */
const float *hr_get_posterior(const hr_state_t *s)
{
    return s->posterior;
}

/* Software version. */
const char *hr_version(void)
{
    return HR_VERSION;
}

/* Model name and weight checksums. */
const char *hr_model_id(void)
{
    return HR_MODEL_ID;
}

/* Size of hr_state_t in bytes. */
uint32_t hr_state_size(void)
{
    return (uint32_t)sizeof(hr_state_t);
}

#ifdef HR_DEBUG
const float *hr_debug_band_energy(const hr_state_t *s) { return &s->dbg_env[0][0]; }
const float *hr_debug_activation(const hr_state_t *s) { return s->dbg_act; }
const float *hr_debug_features(const hr_state_t *s) { return &s->dbg_feats[0][0]; }
const float *hr_debug_log_emission(const hr_state_t *s) { return s->dbg_log_emission; }
int hr_debug_has_features(const hr_state_t *s) { return s->dbg_has_features; }
#endif
