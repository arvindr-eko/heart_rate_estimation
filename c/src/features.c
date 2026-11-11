/* Block C, periodicity features (src/device/features.py).
 * Per sample: the window's ring buffer and lag sums S[k] = sum of x_i x_(i-k) over the pairs in
 * the window; the oldest sample's pairs are removed once the window is full, then the new
 * sample's pairs added (2 MAC per lag). Per frame, one pass over the lags of the mean-removed
 * window gives the ACF (normalised to lag 0), NCCF (normalised by the overlap energies) and YIN
 * (1 - cumulative-mean normalised difference). The head and tail sums of the window are running
 * sums here (prefix-sum arrays in the reference): same values up to float32 rounding. Per state
 * the 13 activation features, 3 features of the 25-50 Hz band and their validity flag, read at
 * the state's period by linear interpolation; NaN of the reference is 0 here. The per-frame ACF
 * and NCCF are stored as int16 codes at HR_LAG_SCALE; YIN only at the states' periods. */
#include <math.h>

#include "hr.h"

#if (HR_S1_DECIMATION & (HR_S1_DECIMATION - 1)) != 0
#error "S1-band lags are derived exactly from hr_lag only for a power-of-2 decimation"
#endif

/* int16 code of a lag-function value: value x HR_LAG_SCALE, saturated to ±HR_CODE_MAX, rounded
 * half away from zero (the float-to-int conversion truncates). */
static int16_t encode(float value)
{
    float v = value * HR_LAG_SCALE;
    if (v > (float)HR_CODE_MAX)
        v = (float)HR_CODE_MAX;
    else if (v < -(float)HR_CODE_MAX)
        v = -(float)HR_CODE_MAX;
    return (int16_t)(v < 0.0f ? v - 0.5f : v + 0.5f);
}

/* Value of a lag-function code. */
static float decode(int16_t code)
{
    return (float)code * HR_LAG_STEP;
}

/* Samples in the window: the count, at most the capacity. */
uint16_t hr_window_size(uint16_t cap, uint32_t count)
{
    return count < cap ? (uint16_t)count : cap;
}

/* Add one sample: per lag, remove the oldest sample's pair when full, then add the new
 * sample's pair, in one pass (the reference's two passes, same operations per lag). */
void hr_window_push(float *hist, float *sums, uint16_t cap, uint32_t *count, float x)
{
    const uint16_t n = hr_window_size(cap, *count);
    const uint16_t slot = (uint16_t)(*count % cap);         /* the oldest sample when full */
    uint16_t back = slot;                                   /* sample k steps before x */
    if (n == cap) {
        const float w0 = hist[slot];
        uint16_t ahead = slot;                              /* sample k steps after w0 */
        sums[0] = (sums[0] - w0 * w0) + x * x;
        for (uint16_t k = 1; k < cap; k++) {
            if (++ahead == cap)
                ahead = 0;
            back = back == 0 ? cap - 1 : back - 1;
            sums[k] = (sums[k] - w0 * hist[ahead]) + x * hist[back];
        }
    } else {
        sums[0] += x * x;
        for (uint16_t k = 1; k <= n; k++) {
            back = back == 0 ? cap - 1 : back - 1;
            sums[k] += x * hist[back];
        }
    }
    hist[slot] = x;
    (*count)++;
}

/* Integer part and fraction of a fractional lag; 0 outside 1 .. n - 1 (frac is 0 at n - 1). */
static int lag_split(uint16_t n, float lag, uint16_t *i, float *frac)
{
    if (!(lag >= 1.0f && lag <= (float)(n - 1)))
        return 0;
    *i = (uint16_t)lag;
    *frac = lag - (float)*i;
    return 1;
}

/* YIN at the period of each state whose interpolation ends at lag k, from state down (lags
 * rising); yin_prev and yin_k: YIN at lags k - 1 and k. Same arithmetic as hr_lag_interp.
 * Returns the next state. */
static int yin_at_states(float *yin_t, uint16_t n, uint16_t k, float yin_prev, float yin_k,
                         int state)
{
    for (; state >= 0; state--) {
        uint16_t i;
        float frac;
        if (!lag_split(n, hr_lag[state], &i, &frac) || (frac == 0.0f ? i : i + 1) != k)
            break;
        yin_t[state] = frac == 0.0f ? yin_k : (yin_k - yin_prev) * frac + yin_prev;
    }
    return state;
}

/* ACF (all lags) and NCCF (first n_nccf lags) of the window as codes; YIN at each state's
 * period (yin_t, HR_N_STATES; 0 outside the window), NULL: none; the window mean (NaN for a
 * flat window), NULL: none. Returns n. */
uint16_t hr_lag_functions(const float *hist, const float *sums, uint16_t cap, uint32_t count,
                          int16_t *acf, int16_t *nccf, uint16_t n_nccf, float *yin_t,
                          float *mean)
{
    const uint16_t n = hr_window_size(cap, count);
    const uint16_t start = (uint16_t)((count - n) % cap);
    if (n == 0)
        return 0;
    if (n_nccf > n)
        n_nccf = n;
    const uint16_t n_yin = yin_t ? (n < HR_N_YIN ? n : HR_N_YIN) : 0;
    const uint16_t n_energy = n_nccf > n_yin ? n_nccf : n_yin;
    int state = HR_N_STATES - 1;                                /* next state for yin_t */
    /* window mean and totals (oldest first) */
    float total = 0.0f, total_sq = 0.0f;
    uint16_t idx = start;
    for (uint16_t i = 0; i < n; i++) {
        const float v = hist[idx];
        total += v;
        total_sq += v * v;
        if (++idx == cap)
            idx = 0;
    }
    const float mu = total / (float)n;
    /* flat window (e.g. silence): no periodicity; two-pass variance, exact for constant input */
    float ss = 0.0f;
    idx = start;
    for (uint16_t i = 0; i < n; i++) {
        const float d = hist[idx] - mu;
        ss += d * d;
        if (++idx == cap)
            idx = 0;
    }
    const int flat = ss / (float)n <= HR_VAR_FLOOR;
    if (mean)
        *mean = flat ? NAN : mu;
    if (flat) {
        for (uint16_t j = 0; j < n; j++)
            acf[j] = 0;
        for (uint16_t j = 0; j < n_nccf; j++)
            nccf[j] = 0;
    }
    float head = total, tail = total, q_head = total_sq, q_tail = total_sq, cum = 0.0f;
    float r0 = 1.0f, yin_prev = 0.0f;                           /* r0 set at k = 0 */
    uint16_t i_first = start;                                   /* sample k */
    uint16_t i_last = (uint16_t)((start + n - 1) % cap);        /* sample n - 1 - k */
    for (uint16_t k = 0; k < n && !flat; k++) {
        const float pairs = (float)(n - k);
        const float r = sums[k] - mu * (head + tail) + pairs * mu * mu;
        if (k == 0)
            r0 = r > HR_EPS ? r : HR_EPS;
        acf[k] = encode(r / r0);
        if (k < n_energy) {
            const float pmm = pairs * mu * mu;
            const float e_head = q_head - 2.0f * mu * head + pmm;
            const float e_tail = q_tail - 2.0f * mu * tail + pmm;
            if (k < n_nccf) {
                const float e = e_head * e_tail;
                nccf[k] = encode(r / sqrtf(e > HR_EPS ? e : HR_EPS));
            }
            if (k < n_yin) {
                const float diff = (e_head + e_tail - 2.0f * r) / pairs;
                float yin_k = 0.0f;
                if (k > 0) {
                    cum += diff;
                    yin_k = 1.0f - diff * (float)k / (cum > HR_EPS ? cum : HR_EPS);
                }
                state = yin_at_states(yin_t, n, k, yin_prev, yin_k, state);
                yin_prev = yin_k;
            }
        }
        /* head and tail sums of the overlap at lag k + 1 */
        const float w_last = hist[i_last], w_first = hist[i_first];
        head -= w_last;
        q_head -= w_last * w_last;
        tail -= w_first;
        q_tail -= w_first * w_first;
        i_last = i_last == 0 ? cap - 1 : i_last - 1;
        if (++i_first == cap)
            i_first = 0;
    }
    /* states outside the window (and every state of a flat window) */
    for (; yin_t && state >= 0; state--)
        yin_t[state] = 0.0f;
    return n;
}

/* Sample i of the cursor's window, oldest first. */
static float cursor_sample(const hr_peak_cursor_t *pc, uint16_t i)
{
    const uint16_t idx = (uint16_t)(pc->start + i);
    return pc->win->hist[idx >= HR_WIN ? idx - HR_WIN : idx];
}

/* One lag down the walk: the overlap gains sample n - k at the head and k - 1 at the tail;
 * then r_up, r_k = r[k + 1], r[k] at the new k (lag_functions' formula for r). */
static void cursor_down(hr_peak_cursor_t *pc)
{
    const float mu = pc->mu;
    pc->head += cursor_sample(pc, (uint16_t)(pc->n - pc->k));
    pc->tail += cursor_sample(pc, (uint16_t)(pc->k - 1));
    pc->k--;
    pc->r_up = pc->r_k;
    pc->r_k = pc->win->sums[pc->k] - mu * (pc->head + pc->tail) + (float)(pc->n - pc->k) * mu * mu;
}

/* Next local maximum of r below lag k + 1, refined by parabolic interpolation; -1 when none. */
static float next_peak(hr_peak_cursor_t *pc)
{
    while (pc->k >= 1) {
        const float c = pc->r_up;
        cursor_down(pc);
        const float a = pc->r_k, b = pc->r_up;
        if (b > a && b >= c) {
            const float curvature = a - 2.0f * b + c;
            return (float)(pc->k + 1) + (curvature < 0.0f ? 0.5f * (a - c) / curvature : 0.0f);
        }
    }
    return -1.0f;
}

/* Cursor before the first state: no peak above, the highest peak below lag n - 1; no peaks
 * for a flat window (mu NaN, hr_lag_functions). The walk starts at lag n (empty overlap). */
void hr_peak_cursor_init(hr_peak_cursor_t *pc, const hr_window_t *win, float mu)
{
    pc->win = win;
    pc->mu = mu;
    pc->n = hr_window_size(HR_WIN, win->count);
    pc->start = (uint16_t)((win->count - pc->n) % HR_WIN);
    pc->below = pc->above = -1.0f;
    pc->head = pc->tail = pc->r_k = 0.0f;
    pc->k = 0;
    if (isnan(mu) || pc->n < 3)
        return;
    pc->k = (int16_t)pc->n;
    cursor_down(pc);
    cursor_down(pc);
    pc->below = next_peak(pc);
}

/* Lag function (codes) at a fractional lag, linear interpolation; 0 and *valid = 0 outside
 * 1 .. n - 1. */
float hr_lag_interp(const int16_t *r, uint16_t n, float lag, int *valid)
{
    uint16_t i;
    float frac;
    *valid = lag_split(n, lag, &i, &frac);
    if (!*valid)
        return 0.0f;
    const float r_i = decode(r[i]);
    return frac == 0.0f ? r_i : (decode(r[i + 1]) - r_i) * frac + r_i;
}

/* Heartbeats in the latest samples (features.recent_periodicity): highest normalised
 * correlation of the last stretch of the activation window with itself one period earlier, over
 * lags within ±HR_RECENT_TOL of period (samples); the stretch is HR_RECENT_N samples, at least one
 * period plus HR_RECENT_OVERLAP. 0 with too few samples or a flat overlap. */
float hr_recent_periodicity(const hr_window_t *act, float period)
{
    const int size = (int)hr_window_size(HR_WIN, act->count);
    const int lo_raw = (int)floorf(period * (1.0f - HR_RECENT_TOL));
    const int lo = lo_raw > 1 ? lo_raw : 1;
    const int hi = (int)ceilf(period * (1.0f + HR_RECENT_TOL));
    int n = HR_RECENT_N > hi + HR_RECENT_OVERLAP ? HR_RECENT_N : hi + HR_RECENT_OVERLAP;
    n = n < size ? n : size;
    const uint32_t first = act->count - (uint32_t)n;   /* push index of the stretch's first */
    float best = 0.0f;
    for (int lag = lo; lag <= hi && lag <= n - HR_RECENT_OVERLAP; lag++) {
        const int m = n - lag;
        float mean_a = 0.0f, mean_b = 0.0f;
        for (int i = 0; i < m; i++) {
            mean_a += act->hist[(first + (uint32_t)(lag + i)) % HR_WIN];
            mean_b += act->hist[(first + (uint32_t)i) % HR_WIN];
        }
        mean_a /= (float)m;
        mean_b /= (float)m;
        float ab = 0.0f, aa = 0.0f, bb = 0.0f;
        for (int i = 0; i < m; i++) {
            const float a = act->hist[(first + (uint32_t)(lag + i)) % HR_WIN] - mean_a;
            const float b = act->hist[(first + (uint32_t)i) % HR_WIN] - mean_b;
            ab += a * b;
            aa += a * a;
            bb += b * b;
        }
        if ((aa < bb ? aa : bb) > HR_VAR_FLOOR * (float)m) {
            const float r = ab / sqrtf(aa * bb);
            best = r > best ? r : best;
        }
    }
    return best;
}

/* Activation ACF at a state's period; 0 outside the window (NaN of the reference). */
float hr_acf_t(const int16_t *acf, uint16_t n, uint16_t state)
{
    int valid;
    const float v = hr_lag_interp(acf, n, hr_lag[state], &valid);
    return valid ? v : 0.0f;
}

/* Relative offset from lag to the nearest ACF peak, clipped to ±HR_PEAK_OFFSET_MAX; a tie goes
 * to the peak above, as in the reference. Lags must come in falling order. Peaks are at least
 * one lag apart, so the two around lag are the reference's searchsorted pair. */
static float peak_offset(hr_peak_cursor_t *pc, float lag)
{
    while (pc->below >= lag) {
        pc->above = pc->below;
        pc->below = next_peak(pc);
    }
    if (pc->below < 0.0f && pc->above < 0.0f)
        return HR_PEAK_OFFSET_MAX;
    const int take_below = pc->above < 0.0f || (pc->below >= 0.0f
                                                && fabsf(pc->below - lag) < fabsf(pc->above - lag));
    const float nearest = take_below ? pc->below : pc->above;
    const float offset = (nearest - lag) / lag;
    if (offset > HR_PEAK_OFFSET_MAX)
        return HR_PEAK_OFFSET_MAX;
    return offset < -HR_PEAK_OFFSET_MAX ? -HR_PEAK_OFFSET_MAX : offset;
}

/* Scorer inputs of one state (features.FEATURE_NAMES, S1 features, flag); YIN from
 * sc->score[state] (hr_lag_functions). */
void hr_state_features(const hr_scratch_t *sc, uint16_t n, uint16_t n_s1, float acf_t_max,
                       uint16_t state, hr_peak_cursor_t *pc, float in[HR_N_IN])
{
    const float lag = hr_lag[state];
    const float overlap_limit = (float)n - HR_MIN_OVERLAP;    /* NCCF needs this much overlap */
    const float acf_t = hr_acf_t(sc->acf, n, state);
    int valid, valid_2t, valid_3t;
    /* activation features, features.FEATURE_NAMES order */
    in[0] = acf_t;
    in[1] = hr_lag_interp(sc->acf, n, lag * 0.5f, &valid);
    in[2] = hr_lag_interp(sc->acf, n, lag * 2.0f, &valid_2t);
    in[3] = (float)valid_2t;
    in[4] = lag <= overlap_limit ? hr_lag_interp(sc->u.nccf, n, lag, &valid)
                                 : 0.0f;
    in[5] = sc->score[state];
    /* local ACF peak against the states two steps away, circular as in the reference */
    in[6] = (acf_t >= hr_acf_t(sc->acf, n, (uint16_t)((state + HR_N_STATES - 2) % HR_N_STATES))
             && acf_t >= hr_acf_t(sc->acf, n, (uint16_t)((state + 2) % HR_N_STATES))) ? 1.0f
                                                                                    : 0.0f;
    in[7] = acf_t - acf_t_max;
    in[8] = logf((float)n / lag);
    in[9] = hr_lag_interp(sc->acf, n, lag * 3.0f, &valid_3t);
    in[10] = (float)valid_3t;
    in[11] = peak_offset(pc, lag);
    in[12] = 2.0f * lag <= overlap_limit
                 ? hr_lag_interp(sc->u.nccf, n, 2.0f * lag, &valid) : 0.0f;
    /* 25-50 Hz band, candidate rates up to s1_max_bpm only; validity flag */
    const int s1_state = state <= HR_S1_LAST_STATE;
    const float lag_s1 = lag * (1.0f / (float)HR_S1_DECIMATION);
    in[13] = in[14] = in[15] = 0.0f;
    if (s1_state && n_s1 > 0) {
        in[13] = hr_lag_interp(sc->s1_acf, n_s1, lag_s1, &valid);
        in[14] = hr_lag_interp(sc->s1_acf, n_s1, lag_s1 * 0.5f, &valid);
        in[15] = hr_lag_interp(sc->s1_acf, n_s1, lag_s1 * 2.0f, &valid);
    }
    in[16] = (float)s1_state;
}
