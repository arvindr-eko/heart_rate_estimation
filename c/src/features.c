/* Block C, periodicity features (src/device/features.py).
 * Per sample: the window's ring buffer and lag sums S[k] = sum of x_i x_(i-k) over the pairs in
 * the window; the oldest sample's pairs are removed once the window is full, then the new
 * sample's pairs added (2 MAC per lag). Per frame, one pass over the lags of the mean-removed
 * window gives the ACF (normalised to lag 0), NCCF (normalised by the overlap energies) and YIN
 * (1 - cumulative-mean normalised difference). The head and tail sums of the window are running
 * sums here (prefix-sum arrays in the reference): same values up to float32 rounding. Per state
 * the 13 activation features, 3 features of the 25-50 Hz band and their validity flag, read at
 * the state's period by linear interpolation; NaN of the reference is 0 here. */
#include <math.h>

#include "hr.h"

#if (HR_S1_DECIMATION & (HR_S1_DECIMATION - 1)) != 0
#error "S1-band lags are derived exactly from hr_lag only for a power-of-2 decimation"
#endif

/* Samples in the window: the count, at most the capacity. */
uint16_t hr_window_size(uint16_t cap, uint32_t count)
{
    return count < cap ? (uint16_t)count : cap;
}

/* Add one sample: remove the oldest sample's lag pairs when full, add the new ones. */
void hr_window_push(float *hist, float *sums, uint16_t cap, uint32_t *count, float x)
{
    uint16_t n = hr_window_size(cap, *count);
    /* full window: remove the oldest sample's pairs */
    if (n == cap) {
        const uint16_t oldest = (uint16_t)(*count % cap);
        const float w0 = hist[oldest];
        uint16_t idx = oldest;
        for (uint16_t k = 0; k < cap; k++) {
            sums[k] -= w0 * hist[idx];
            if (++idx == cap)
                idx = 0;
        }
        n = cap - 1;
    }
    /* add the new sample's pairs: lag 0 with itself, lag k with the sample k steps back */
    sums[0] += x * x;
    uint16_t idx = (uint16_t)((*count + cap - 1) % cap);
    for (uint16_t k = 1; k <= n; k++) {
        sums[k] += x * hist[idx];
        idx = idx == 0 ? cap - 1 : idx - 1;
    }
    hist[*count % cap] = x;
    (*count)++;
}

/* ACF (all lags), NCCF and YIN (first n_nccf, n_yin lags) of the window; returns n. */
uint16_t hr_lag_functions(const float *hist, const float *sums, uint16_t cap, uint32_t count,
                          float *acf, float *nccf, uint16_t n_nccf, float *yin, uint16_t n_yin)
{
    const uint16_t n = hr_window_size(cap, count);
    const uint16_t start = (uint16_t)((count - n) % cap);
    if (n == 0)
        return 0;
    if (n_nccf > n)
        n_nccf = n;
    if (n_yin > n)
        n_yin = n;
    const uint16_t n_energy = n_nccf > n_yin ? n_nccf : n_yin;
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
    if (ss / (float)n <= HR_VAR_FLOOR) {
        for (uint16_t j = 0; j < n; j++)
            acf[j] = 0.0f;
        for (uint16_t j = 0; j < n_nccf; j++)
            nccf[j] = 0.0f;
        for (uint16_t j = 0; j < n_yin; j++)
            yin[j] = 0.0f;
        return n;
    }
    float head = total, tail = total, q_head = total_sq, q_tail = total_sq, cum = 0.0f;
    uint16_t i_first = start;                                   /* sample k */
    uint16_t i_last = (uint16_t)((start + n - 1) % cap);        /* sample n - 1 - k */
    for (uint16_t k = 0; k < n; k++) {
        const float pairs = (float)(n - k);
        const float r = sums[k] - mu * (head + tail) + pairs * mu * mu;
        acf[k] = r;
        if (k < n_energy) {
            const float pmm = pairs * mu * mu;
            const float e_head = q_head - 2.0f * mu * head + pmm;
            const float e_tail = q_tail - 2.0f * mu * tail + pmm;
            if (k < n_nccf) {
                const float e = e_head * e_tail;
                nccf[k] = r / sqrtf(e > HR_EPS ? e : HR_EPS);
            }
            if (k < n_yin) {
                const float diff = (e_head + e_tail - 2.0f * r) / pairs;
                if (k == 0) {
                    yin[0] = 0.0f;
                } else {
                    cum += diff;
                    yin[k] = 1.0f - diff * (float)k / (cum > HR_EPS ? cum : HR_EPS);
                }
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
    const float r0 = acf[0] > HR_EPS ? acf[0] : HR_EPS;
    for (uint16_t k = 0; k < n; k++)
        acf[k] = acf[k] / r0;
    return n;
}

/* Local maxima of the ACF, refined by parabolic interpolation, ascending. */
uint16_t hr_acf_peaks(const float *acf, uint16_t n, float *peaks)
{
    uint16_t m = 0;
    for (uint16_t k = 1; k + 1 < n; k++) {
        const float a = acf[k - 1], b = acf[k], c = acf[k + 1];
        if (b > a && b >= c) {
            const float curvature = a - 2.0f * b + c;
            peaks[m++] = (float)k + (curvature < 0.0f ? 0.5f * (a - c) / curvature : 0.0f);
        }
    }
    return m;
}

/* Lag function at a fractional lag; 0 and *valid = 0 outside 1 .. n - 1. */
float hr_lag_interp(const float *r, uint16_t n, float lag, int *valid)
{
    if (!(lag >= 1.0f && lag <= (float)(n - 1))) {
        *valid = 0;
        return 0.0f;
    }
    *valid = 1;
    const uint16_t i = (uint16_t)lag;
    const float frac = lag - (float)i;
    if (i >= n - 1 || frac == 0.0f)
        return r[i];
    return (r[i + 1] - r[i]) * frac + r[i];
}

/* Relative offset from lag to the nearest ACF peak, clipped to ±HR_PEAK_OFFSET_MAX. */
static float peak_offset(const float *peaks, uint16_t m, float lag)
{
    if (m == 0)
        return HR_PEAK_OFFSET_MAX;
    uint16_t lo = 0, hi = m;                        /* peaks below lag (searchsorted, left) */
    while (lo < hi) {
        const uint16_t mid = (uint16_t)((lo + hi) / 2);
        if (peaks[mid] < lag)
            lo = mid + 1;
        else
            hi = mid;
    }
    const uint16_t j_max = m > 1 ? m - 1 : 1;
    const uint16_t j = lo < 1 ? 1 : (lo > j_max ? j_max : lo);
    const float left = peaks[j - 1], right = peaks[j < m - 1 ? j : m - 1];
    const float nearest = fabsf(left - lag) < fabsf(right - lag) ? left : right;
    const float offset = (nearest - lag) / lag;
    if (offset > HR_PEAK_OFFSET_MAX)
        return HR_PEAK_OFFSET_MAX;
    return offset < -HR_PEAK_OFFSET_MAX ? -HR_PEAK_OFFSET_MAX : offset;
}

/* Scorer inputs of one state (features.FEATURE_NAMES, S1 features, flag). */
void hr_state_features(const hr_scratch_t *sc, uint16_t n, uint16_t n_peaks, uint16_t n_s1,
                       float acf_t_max, uint16_t state, float in[HR_N_IN])
{
    const float lag = hr_lag[state];
    const float overlap_limit = (float)n - HR_MIN_OVERLAP;    /* NCCF needs this much overlap */
    const float acf_t = sc->acf_t[state];
    int valid, valid_2t, valid_3t;
    /* activation features, features.FEATURE_NAMES order */
    in[0] = acf_t;
    in[1] = hr_lag_interp(sc->acf, n, lag * 0.5f, &valid);
    in[2] = hr_lag_interp(sc->acf, n, lag * 2.0f, &valid_2t);
    in[3] = (float)valid_2t;
    in[4] = lag <= overlap_limit ? hr_lag_interp(sc->u.lag.nccf, n, lag, &valid) : 0.0f;
    in[5] = hr_lag_interp(sc->u.lag.yin, n, lag, &valid);
    /* local ACF peak against the states two steps away, circular as in the reference */
    in[6] = (acf_t >= sc->acf_t[(state + HR_N_STATES - 2) % HR_N_STATES]
             && acf_t >= sc->acf_t[(state + 2) % HR_N_STATES]) ? 1.0f : 0.0f;
    in[7] = acf_t - acf_t_max;
    in[8] = logf((float)n / lag);
    in[9] = hr_lag_interp(sc->acf, n, lag * 3.0f, &valid_3t);
    in[10] = (float)valid_3t;
    in[11] = peak_offset(sc->peaks, n_peaks, lag);
    in[12] = 2.0f * lag <= overlap_limit ? hr_lag_interp(sc->u.lag.nccf, n, 2.0f * lag, &valid)
                                         : 0.0f;
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
