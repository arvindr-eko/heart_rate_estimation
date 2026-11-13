/* combo13.c -- Phase A: faithful C port of combo13_heart_rate.py.
 *
 * GOAL OF THIS FILE: establish a CORRECTNESS baseline in C, translated as directly as possible
 * from the validated Python reference, so it can be diffed tick-by-tick against Python before
 * any optimization is trusted. It is deliberately NOT yet embedded-appropriate:
 *   - every DSP stage recomputes from scratch over the whole trailing window every tick
 *     (matches Python's own behavior exactly -- Python's `iir()`/`lfilter()` calls use zero
 *     initial state every call, there is no persistent filter state across ticks there either)
 *   - RAM usage is large (see the accompanying report) because of this recompute-from-scratch
 *     style requiring several full-length scratch buffers at once
 * Optimizations (streaming filter state, incremental artifact removal, no per-lag sort, ring-
 * buffered tilt frames) are applied AFTER this baseline is validated, each one diffed against
 * this file's output before being trusted, per this project's own established practice.
 *
 * Fixed configuration: reproduces exactly the "combo 13" validated pipeline. fs is fixed at
 * 4000 Hz (all filter coefficients below are pre-designed for that rate). The dynamic-range
 * compressor is omitted (combo13 ships with it OFF -- see combo13.h).
 */
#include "combo13_opt.h"
#include <math.h>
#include <string.h>
#include <stdlib.h>

/* ---- profiling (see combo13.h) ---- */
static Combo13OptProfile g_prof;
void c13opt_profile_reset(void) { memset(&g_prof, 0, sizeof(g_prof)); }
Combo13OptProfile c13opt_profile_get(void) { return g_prof; }

static unsigned long ceil_log2(unsigned long n) {
    unsigned long r = 0, v = 1;
    while (v < n) { v <<= 1; r++; }
    return r ? r : 1;
}
#ifdef COMBO13_PROFILE
#define PROF_SORT(n) do { g_prof.sort_calls++; g_prof.sort_nlogn += (unsigned long)(n) * ceil_log2((unsigned long)(n)); } while (0)
#define PROF_SELECT(n) do { g_prof.select_n += (unsigned long)(n); } while (0)
#define PROF_AC(n) do { g_prof.autocorr_macs += (unsigned long)(n); } while (0)
#define PROF_FILT(n) do { g_prof.filter_taps += (unsigned long)(n); } while (0)
#define PROF_FFT() do { g_prof.fft_calls++; } while (0)
#else
#define PROF_SORT(n) ((void)0)
#define PROF_SELECT(n) ((void)0)
#define PROF_AC(n) ((void)0)
#define PROF_FILT(n) ((void)0)
#define PROF_FFT() ((void)0)
#endif

/* ============================================================================================
 * FIXED CONFIGURATION (combo13's validated defaults -- see combo13_heart_rate.py provenance)
 * ============================================================================================ */
#define FS_IN 4000.0f
#define TICK_S 0.25f

/* RAW_MAX, DEC_MAX, FEAT_MAX, S_LEN, EXT_HI_MAX, EXT_S_LEN, NLOG, TILT_FRAME_LEN, TILT_HOP,
 * TILT_MAX_FRAMES all come from combo13_internal.h (shared with the struct definition there). */
#define AA_DECIM 8
#define FRAME_DS 5
#define FFS 100.0f
#define LO_LAG 30
#define HI_LAG 150
#define LAG_POW 0.1f
#define FFT_N 128                /* next_pow2(max(TILT_FRAME_LEN,32)) */
#define TILT_AC_LO 35            /* fixed given fixed fs/hop: round((fs/hop)*60/200) */
#define TILT_AC_HI 176           /* round((fs/hop)*60/40) */
#define TILT_AC_LEN (TILT_AC_HI - TILT_AC_LO)          /* 141 */

/* combo13's tuned scalar constants */
static const float SIGNAL_ONSET_THRESH = 0.00021f;
static const int   ONSET_DEBOUNCE_TICKS = 4;
static const float SPIKE_DETECT_MULT = 20.0f;
static const float TRANSIENT_LIMIT_MULT = 4.29f;
static const float ARTIFACT_DETECT_MULT = 16.96f;
static const float ARTIFACT_EXTEND_MULT = 8.02f;
static const float ARTIFACT_BASELINE_PCTL = 0.4231f;
static const int   ARTIFACT_PASSES = 5;
static const int   ARTIFACT_PAD_SAMPLES = 36;
static const float ROBUST_TRIM_FRAC = 0.0079f;
static const float FEAT_CLIP = 4.5f;
static const float SINGLE_WIN_MIN = 4.0f;
static const float SINGLE_WIN_MAX = 8.0f;
static const int   TEAL_PUSH_HALF_WIDTH = 36;
static const float CONS_WAIT_S = 4.0f;
static const float CONS_WAIT_EXTRA_S_PER_BPM = 0.020f;
static const int   WEIGHTED_MED_TRAILING_N = 8;
static const float AGREEMENT_BPM_TOL = 11.10f;
static const float AGREE_BONUS = 2.70f;
static const float TENT_PEAK_BPM = 55.9f;
static const float TENT_PEAK_H = 2.29f;
static const float TENT_LO_H = 0.56f;
static const float TENT_HI_H = 0.81f;
static const float TEAL_JUMP_RATIO = 1.4f;
static const int   TEAL_JUMP_CONFIRM_TICKS = 3;
static const float TEAL_BPM_DELAY_SLOPE = 0.002388504108629796f;
static const float TILT_SPLIT_HZ = 45.06f;
static const float PITCH_FFT_LO_HZ = 14.27f;
static const float PITCH_FFT_HI_HZ = 226.82f;
static const float PITCH_GATE_FRAC = 0.0658f;

/* FIX (2026-08-04): peak-mean flatline detector, ported from pcg_visualizer/index.html's CURRENT
 * is_flatline mechanism (see the struct comment on peak_mean_below_since_el for the full story).
 * PEAK_MEAN_DIFF_THRESH_NORM=0.003 is the JS value AS-IS -- but the JS pipeline computes broadEnv
 * over samples normalized to [-1,1] (pcgRing divides by 32768), while this port (like the rest of
 * combo13_heart_rate.py's validated constants) operates on RAW un-normalized wav-scale samples --
 * confirmed empirically (real corpus RMS ~1219 on a typical file, not ~0.037) rather than assumed.
 * PEAK_MEAN_DIFF_THRESH is that same decision boundary rescaled by *32768 to match, since broadEnv
 * is a purely linear (unnormalized) computation -- multiplying the input by a constant multiplies
 * its output by the same constant, so the threshold must scale the same way to preserve the
 * identical decision. */
static const float PEAK_MEAN_TOP_PCT = 0.10f;
static const float PEAK_MEAN_DIFF_THRESH = 0.003f * 32768.0f; /* JS value 0.003, rescaled -- see comment above */
static const float PEAK_MEAN_SUSTAIN_S = 1.0f;

static const bool  USE_HARMONIC_VETO = true;
static const float HARMONIC_VETO_HI_EXT_MULT = 2.1813774614710146f;
static const float HARMONIC_VETO_HEIGHT_FRAC = 0.05895565949526575f;
static const float HARMONIC_VETO_LO_FRAC = 1.9239938526174543f;
static const float HARMONIC_VETO_HI_FRAC = 2.081876428164688f;

#define DECAY_ADAPTIVE_TAU_MIN 1.0f
#define DECAY_ADAPTIVE_TAU_MAX 8.0f

static const float NEG_INF = -1e30f;   /* sentinel standing in for Python's -inf/None timestamps */

/* ============================================================================================
 * FIXED FILTER COEFFICIENTS (pre-designed for fs=4000Hz -- see combo13_heart_rate.py)
 * ============================================================================================ */
static const float AA_B[5] = {0.0005891316955801649f, 0.0023565267823206595f, 0.0035347901734809895f,
                               0.0023565267823206595f, 0.0005891316955801649f};
static const float AA_A[5] = {1.0f, -3.09905041635851f, 3.6812793328016165f, -1.9759888358976645f,
                               0.40318602658384056f};

static const float BAND_B[3][5] = {
    {0.013359200027856502f, 0, -0.026718400055713003f, 0, 0.013359200027856502f},
    {0.02785976611713605f, 0, -0.0557195322342721f, 0, 0.02785976611713605f},
    {0.06745527388907191f, 0, -0.13491054777814382f, 0, 0.06745527388907191f},
};
static const float BAND_A[3][5] = {
    {1.0f, -3.2216975944168245f, 4.259742833119654f, -2.6933188730452877f, 0.700896781188403f},
    {1.0f, -2.4220309055301232f, 2.962756451291472f, -1.8462867837112926f, 0.5869195080611904f},
    {1.0f, -0.8218517544917526f, 1.3177346226477535f, -0.5147610573338093f, 0.41280159809618866f},
};

/* pitch_bp_lo_hz=17.18, pitch_bp_hi_hz=175.17, fs=4000 -- scipy.signal.butter(2,...,'band') */
static const float PITCH_BP_B[5] = {0.013051383589961553f, 0, -0.026102767179923107f, 0, 0.013051383589961553f};
static const float PITCH_BP_A[5] = {1.0f, -3.6382064558549789f, 4.9826990589367766f, -3.0484777138479626f,
                                     0.70403179011858719f};

static const float ENV_A = 0.7975628241529755f;

/* struct Combo13OptState itself is defined in combo13_internal.h (included via combo13.h) so the
 * public header exposes a complete type -- see that file for why. */

unsigned long c13opt_state_size(void) { return (unsigned long)sizeof(struct Combo13OptState); }

/* ============================================================================================
 * DSP PRIMITIVES -- portable reference implementations. On the real PSoC6 CM4F target, these
 * are the functions to replace with CMSIS-DSP calls (arm_biquad_cascade_df2T_f32 for iir4,
 * arm_rfft_fast_f32 for fft128, arm_correlate_f32 for the autocorrelation inner loops).
 * ============================================================================================ */

/* 4th-order IIR, transposed Direct Form II, zero initial state (matches scipy.signal.lfilter's
 * default zi=0 -- Phase A recomputes from scratch every tick, no state carried across calls). */
static void iir4(const float b[5], const float a[5], const float *x, float *y, int n) {
    float z0 = 0, z1 = 0, z2 = 0, z3 = 0;
    PROF_FILT((unsigned long)n * 9); /* 9 MACs/sample: 5 b-taps + 4 a-taps */
    for (int i = 0; i < n; i++) {
        float xi = x[i];
        float yi = b[0] * xi + z0;
        z0 = b[1] * xi - a[1] * yi + z1;
        z1 = b[2] * xi - a[2] * yi + z2;
        z2 = b[3] * xi - a[3] * yi + z3;
        z3 = b[4] * xi - a[4] * yi;
        y[i] = yi;
    }
}

/* single-sample transposed-DF-II step with EXPLICIT, CALLER-OWNED state -- for the streaming
 * pitch-bandpass filter (task 7), whose state persists across push() calls rather than being
 * reset to zero every tick like every other filter in this file. */
static float iir4_step(const float b[5], const float a[5], float x, float z[4]) {
    PROF_FILT(9);
    float y = b[0] * x + z[0];
    z[0] = b[1] * x - a[1] * y + z[1];
    z[1] = b[2] * x - a[2] * y + z[2];
    z[2] = b[3] * x - a[3] * y + z[3];
    z[3] = b[4] * x - a[4] * y;
    return y;
}

/* single-pole envelope filter: y[n] = (1-A)*|x[n]| + A*y[n-1], y[-1]=0 */
static void env_singlepole(const float *x, float *y, int n) {
    float z = 0;
    PROF_FILT((unsigned long)n * 2);
    for (int i = 0; i < n; i++) {
        float v = fabsf(x[i]);
        float yi = (1.0f - ENV_A) * v + ENV_A * z;
        y[i] = yi;
        z = yi;
    }
}

static int decimate_stride8(const float *raw, int n, float *out) {
    /* AA-filter then keep every 8th sample starting at 0, matching y[:m*8:8] */
    static float tmp[RAW_MAX];
    if (n <= 0) return 0;
    iir4(AA_B, AA_A, raw, tmp, n);
    int m = n / AA_DECIM;
    for (int i = 0; i < m; i++) out[i] = tmp[i * AA_DECIM];
    return m;
}

/* OPTIMIZATION (task 4): quickselect replaces full-array sort wherever only a SINGLE order
 * statistic (a percentile or the median) is actually needed -- mathematically exact (produces
 * the identical value a full sort+index would), just O(n) average case instead of O(n log n),
 * with none of the corpus-revalidation risk a behavior-changing optimization would carry. */
static void swapf(float *a, float *b) { float t = *a; *a = *b; *b = t; }

static int median3_idx(const float *a, int i, int j, int k) {
    float ai = a[i], aj = a[j], ak = a[k];
    if ((ai <= aj && aj <= ak) || (ak <= aj && aj <= ai)) return j;
    if ((aj <= ai && ai <= ak) || (ak <= ai && ai <= aj)) return i;
    return k;
}

static int partitionf(float *a, int lo, int hi, int p) {
    float pivot = a[p];
    swapf(&a[p], &a[hi]);
    int store = lo;
    for (int i = lo; i < hi; i++) {
        if (a[i] < pivot) { swapf(&a[i], &a[store]); store++; }
    }
    swapf(&a[store], &a[hi]);
    return store;
}

/* Range-scoped: selects the k-th smallest (absolute index k) considering only a[lo0..hi0].
 * Needed (not just a convenience) when finding a SECOND order statistic after a first one has
 * already been placed elsewhere in the same array: restricting lo0/hi0 to the first call's own
 * partition boundary guarantees the second call never touches (and can't disturb) indices
 * outside that range -- the same idiom as calling std::nth_element twice, the second time
 * scoped to start at the first pivot, to get an exact trimmed range with two O(n) passes
 * instead of one O(n log n) sort. */
static void quickselectf_range(float *a, int lo0, int hi0, int k) {
    int lo = lo0, hi = hi0;
    while (lo < hi) {
        int mid = lo + (hi - lo) / 2;
        int p = median3_idx(a, lo, mid, hi);
        p = partitionf(a, lo, hi, p);
        if (k == p) return;
        else if (k < p) hi = p - 1;
        else lo = p + 1;
    }
}

/* After this call, a[k] holds the k-th smallest element (0-indexed); a is partially reordered
 * around it (same contract as C++'s std::nth_element). */
static void quickselectf(float *a, int n, int k) { quickselectf_range(a, 0, n - 1, k); }

/* np.median equivalent via quickselect: for even n, the (n/2-1)-th order statistic is exactly
 * max(a[0..n/2-1]) once a[n/2] has been selected -- partitioning guarantees everything left of
 * the pivot is <= it, so no second quickselect call is needed for the lower-middle element. */
static float robust_median(float *a, int n) {
    int mid = n / 2;
    quickselectf(a, n, mid);
    if (n % 2) return a[mid];
    float lower_max = a[0];
    for (int i = 1; i < mid; i++) if (a[i] > lower_max) lower_max = a[i];
    return 0.5f * (lower_max + a[mid]);
}

/* declick_mult: whole-array clip to `mult` robust-sigma (median-based) */
static void declick_mult(const float *x, int n, float mult, float *out, float *sort_scratch) {
    if (n <= 0) return;
    for (int i = 0; i < n; i++) sort_scratch[i] = fabsf(x[i]);
    PROF_SELECT(n);
    float med = robust_median(sort_scratch, n);
    float lim = mult * ((med * 1.4826f) != 0.0f ? med * 1.4826f : 1e-6f);
    for (int i = 0; i < n; i++) {
        float v = x[i];
        out[i] = v > lim ? lim : (v < -lim ? -lim : v);
    }
}

/* ============================================================================================
 * ARTIFACT REMOVAL
 * ============================================================================================ */
static void interp_over_mask(float *dst, const float *src, const bool *mask, int n) {
    int i = 0;
    while (i < n) {
        if (!mask[i]) { i++; continue; }
        int j = i;
        while (j < n && mask[j]) j++;
        float start_val = (i > 0) ? src[i - 1] : (j < n ? src[j] : src[n - 1]);
        float end_val = (j < n) ? src[j] : (i > 0 ? src[i - 1] : src[0]);
        int length = j - i;
        for (int k = 0; k < length; k++) {
            float t = (float)(k + 1) / (float)(length + 1);
            dst[i + k] = start_val + (end_val - start_val) * t;
        }
        i = j;
    }
}

/* Multi-pass detect->extend->interpolate artifact excision. Writes result into `out` (length n).
 * Uses st's scratch buffers (absx/work/sorted/grown), all sized RAW_MAX. */
static void remove_transients(struct Combo13OptState *st, const float *x, int n,
                               float detect_mult, float extend_mult, int pad_samples,
                               float pctl, int passes, float *out) {
    if (n <= 0) return;
    float *absx = st->scratch_absx;
    float *work = st->scratch_work;
    float *sorted = st->scratch_sorted;
    bool *grown = st->scratch_grown;

    for (int i = 0; i < n; i++) { absx[i] = fabsf(x[i]); grown[i] = false; }
    memcpy(work, x, n * sizeof(float));

    for (int pass = 0; pass < passes; pass++) {
        for (int i = 0; i < n; i++) sorted[i] = fabsf(work[i]);
        int idx = (int)(n * pctl);
        if (idx > n - 1) idx = n - 1;
        PROF_SELECT(n);
        quickselectf(sorted, n, idx);
        float baseline = sorted[idx] != 0.0f ? sorted[idx] : 1e-9f;
        float scale = (baseline * 1.4826f) != 0.0f ? baseline * 1.4826f : 1e-6f;
        float lim = detect_mult * scale;
        float extend_lim = extend_mult * scale;

        bool found_new = false;
        int i = 0;
        while (i < n) {
            if (grown[i] || absx[i] <= lim) { i++; continue; }
            int lo = i;
            while (lo > 0 && absx[lo - 1] > extend_lim) lo--;
            int hi = i;
            while (hi < n - 1 && absx[hi + 1] > extend_lim) hi++;
            for (int k = lo; k <= hi; k++) {
                if (!grown[k]) { grown[k] = true; found_new = true; }
            }
            i = hi + 1;
        }
        if (!found_new) break;
        memcpy(work, x, n * sizeof(float));
        interp_over_mask(work, x, grown, n);
    }

    /* pad_samples dilation on top of `grown`, then final interpolation into `out` */
    bool any_grown = false;
    for (int i = 0; i < n; i++) if (grown[i]) { any_grown = true; break; }
    if (any_grown && pad_samples > 0) {
        /* dilate using a copy so we don't compound growth while iterating; grown itself is the
         * source, we build padded directly on top of grown (safe since we only ever OR bits on,
         * and reading grown[center] doesn't depend on padding written for other centers -- same
         * as Python's `padded=grown.copy()` then writing into padded only). */
        static bool padded[RAW_MAX];
        memcpy(padded, grown, n * sizeof(bool));
        for (int center = 0; center < n; center++) {
            if (!grown[center]) continue;
            int lo = center - pad_samples; if (lo < 0) lo = 0;
            int hi = center + pad_samples + 1; if (hi > n) hi = n;
            for (int k = lo; k < hi; k++) padded[k] = true;
        }
        memcpy(out, x, n * sizeof(float));
        interp_over_mask(out, x, padded, n);
    } else {
        memcpy(out, x, n * sizeof(float));
        interp_over_mask(out, x, grown, n);
    }
}

/* Robust median-based spike detector -- returns run count, fills out_idx with each run's peak
 * sample index (caller-provided buffer, capacity `cap`). */
static int detect_spike_runs(const float *x, int n, float mult, int *out_idx, int cap,
                              float *sort_scratch) {
    if (n <= 0) return 0;
    for (int i = 0; i < n; i++) sort_scratch[i] = fabsf(x[i]);
    PROF_SELECT(n);
    float med = robust_median(sort_scratch, n);
    float lim = mult * ((med * 1.4826f) != 0.0f ? med * 1.4826f : 1e-6f);

    int count = 0;
    int i = 0;
    while (i < n) {
        if (fabsf(x[i]) <= lim) { i++; continue; }
        int j = i;
        float best_v = -1.0f; int best_idx = i;
        while (j < n && fabsf(x[j]) > lim) {
            float av = fabsf(x[j]);
            if (av > best_v) { best_v = av; best_idx = j; }
            j++;
        }
        if (count < cap) out_idx[count++] = best_idx;
        i = j;
    }
    return count;
}

/* ============================================================================================
 * FEATURE EXTRACTION (band filter -> envelope -> normalize/clip, at the 100Hz frame rate)
 * ============================================================================================ */
static int feat_norm_clip(struct Combo13OptState *st, const float *seg500, int n500, int band_idx,
                           float clip, float *out) {
    float *filt = st->scratch_filt;
    float *env = st->scratch_env;
    iir4(BAND_B[band_idx], BAND_A[band_idx], seg500, filt, n500);
    env_singlepole(filt, env, n500);
    int m = n500 / FRAME_DS;
    float sum = 0.0f;
    for (int i = 0; i < m; i++) { out[i] = env[i * FRAME_DS]; sum += out[i]; }
    if (m == 0) return 0;
    float mean = sum / (float)m;
    float var = 0.0f;
    for (int i = 0; i < m; i++) { float d = out[i] - mean; var += d * d; }
    float sd = sqrtf(var / (float)m);
    if (sd == 0.0f) sd = 1.0f;
    for (int i = 0; i < m; i++) {
        float z = (out[i] - mean) / sd;
        out[i] = z > clip ? clip : (z < -clip ? -clip : z);
    }
    return m;
}

static int sum_env_norm(struct Combo13OptState *st, const float *seg500, int n500, float clip,
                         float *out) {
    int m = feat_norm_clip(st, seg500, n500, 0, clip, st->scratch_feat_a);
    feat_norm_clip(st, seg500, n500, 1, clip, st->scratch_feat_b);
    feat_norm_clip(st, seg500, n500, 2, clip, st->scratch_feat_c);
    for (int i = 0; i < m; i++)
        out[i] = st->scratch_feat_a[i] + st->scratch_feat_b[i] + st->scratch_feat_c[i];
    return m;
}

/* unnormalized summed envelope at the 100Hz frame rate -- signal-level (onset/offset) only */
static int broad_env(struct Combo13OptState *st, const float *seg500, int n500, float *out) {
    float *filt = st->scratch_filt;
    float *env = st->scratch_env;
    int m = n500 / FRAME_DS;
    for (int i = 0; i < m; i++) out[i] = 0.0f;
    for (int b = 0; b < 3; b++) {
        iir4(BAND_B[b], BAND_A[b], seg500, filt, n500);
        env_singlepole(filt, env, n500);
        for (int i = 0; i < m; i++) out[i] += env[i * FRAME_DS];
    }
    return m;
}

/* ============================================================================================
 * AUTOCORRELATION (trimmed-mean-of-products -- Phase A keeps the full sort; task 5 removes it)
 * ============================================================================================ */
/* OPTIMIZATION (task 5), REVISED: first attempt dropped the trim entirely (relying on the
 * upstream FEAT_CLIP winsorize as the only outlier defense) -- corpus comparison against Phase A
 * (60 files, 6142 ticks) showed that was NOT accuracy-neutral: correct% -0.48pp and combined
 * half+double octave errors 109->125 (+15%). A second trim, per lag, on top of already-clipped
 * data turned out NOT to be redundant after all. Reverted to the exact trimmed mean, but computed
 * via quickselect instead of a full sort: the trimmed mean only needs the SUM of the middle
 * (1-2*trim_frac) band, which doesn't require that band to be internally ordered -- two
 * quickselect calls (at the trim and n-1-trim order statistics) partition exactly that band into
 * place in O(n) each, vs. O(n log n) for a full sort, for the mathematically IDENTICAL result
 * (bit-exact vs. Phase A, confirmed below). This keeps the full compute win with zero accuracy
 * risk, rather than trading accuracy for speed. */
static void add_autocorr_robust(struct Combo13OptState *st, float *S, const float *f, int T,
                                 int lo, int hi, float trim_frac) {
    if (T <= hi) return;
    float r0 = 0.0f;
    for (int i = 0; i < T; i++) r0 += f[i] * f[i];
    r0 /= (float)T;
    float *prod = st->scratch_sorted;
    for (int l = lo; l < hi; l++) {
        int n = T - l;
        for (int i = 0; i < n; i++) prod[i] = f[i] * f[i + l];
        PROF_AC(n);
        int trim = (int)(n * trim_frac);
        int keep = n - 2 * trim;
        float mean_val = 0.0f;
        if (keep > 0) {
            PROF_SELECT(n);
            quickselectf(prod, n, trim);
            /* range-scoped, starting at `trim` (the first call's own pivot boundary) -- NOT a
             * fresh quickselectf(prod,n,...) call, which would re-partition the whole array and
             * could disturb the already-correct prod[trim] set by the line above. */
            if (keep > 1) { PROF_SELECT(n); quickselectf_range(prod, trim, n - 1, n - 1 - trim); }
            float sum = 0.0f;
            for (int i = trim; i < n - trim; i++) sum += prod[i];
            mean_val = sum / (float)keep;
        }
        S[l - lo] += mean_val / (r0 + 1e-9f);
    }
}

/* plain (non-robust, non-clipped) autocorrelation, mean-centered input -- used only for the
 * tilt trace's own autocorrelation, which was never trimmed-mean in the Python reference either */
static void add_autocorr_plain(float *S, const float *f, int T, int lo, int hi) {
    if (T <= hi) return;
    float r0 = 0.0f;
    for (int i = 0; i < T; i++) r0 += f[i] * f[i];
    r0 /= (float)T;
    for (int l = lo; l < hi; l++) {
        int n = T - l;
        float s = 0.0f;
        for (int i = 0; i < n; i++) s += f[i] * f[i + l];
        PROF_AC(n);
        S[l - lo] += (s / (float)T) / (r0 + 1e-9f);
    }
}

typedef struct {
    bool valid;
    float bpm;
    float conf;
    float Sw[S_LEN];
    int lo;
} EstimateResult;

static EstimateResult estimate_sumenv_robust(struct Combo13OptState *st, const float *dec, int T500,
                                              float trim_frac, float feat_clip) {
    EstimateResult r; r.valid = false;
    if (T500 < 3 * 500) return r;
    float S[S_LEN] = {0};
    int m = sum_env_norm(st, dec, T500, feat_clip, st->scratch_sumenv);
    add_autocorr_robust(st, S, st->scratch_sumenv, m, LO_LAG, HI_LAG, trim_frac);

    float Sw[S_LEN];
    for (int l = LO_LAG; l < HI_LAG; l++) Sw[l - LO_LAG] = S[l - LO_LAG] / powf((float)l, LAG_POW);
    float swmin = Sw[0];
    for (int i = 1; i < S_LEN; i++) if (Sw[i] < swmin) swmin = Sw[i];
    for (int i = 0; i < S_LEN; i++) Sw[i] -= swmin;

    int pk[S_LEN]; int npk = 0;
    for (int i = 1; i < S_LEN - 1; i++)
        if (Sw[i] >= Sw[i - 1] && Sw[i] > Sw[i + 1]) pk[npk++] = i;

    memcpy(r.Sw, Sw, sizeof(Sw));
    r.lo = LO_LAG;
    if (npk == 0) {
        r.bpm = 60.0f / ((float)LO_LAG / FFS);
        r.conf = 1.0f;
        r.valid = true;
        return r;
    }
    /* selection sort by -Sw[i] descending (npk is small, <=~60, fine unsorted-input cost) */
    for (int i = 0; i < npk; i++) {
        int best = i;
        for (int j = i + 1; j < npk; j++) if (Sw[pk[j]] > Sw[pk[best]]) best = j;
        int tmp = pk[i]; pk[i] = pk[best]; pk[best] = tmp;
    }
    int p1 = pk[0];
    int bl = LO_LAG + p1;
    int p2 = -1;
    for (int k = 1; k < npk; k++) if (abs(pk[k] - p1) >= 8) { p2 = pk[k]; break; }
    if (p2 < 0 && npk > 1) p2 = pk[1];
    float conf = (p2 < 0) ? 1.2f : Sw[p1] / (Sw[p2] + 1e-9f);
    r.bpm = 60.0f / ((float)bl / FFS);
    r.conf = conf;
    r.valid = true;
    return r;
}

static float moving_average_at(const float *csum, int n, int i, int half_width) {
    int lo = i - half_width; if (lo < 0) lo = 0;
    int hi = i + half_width; if (hi > n - 1) hi = n - 1;
    return (csum[hi + 1] - csum[lo]) / (float)(hi - lo + 1);
}

static EstimateResult estimate_detrend_from_mw(const EstimateResult *win_r, int half_width) {
    EstimateResult out; out.valid = false;
    if (!win_r->valid) return out;
    int n = S_LEN;
    float csum[S_LEN + 1]; csum[0] = 0.0f;
    for (int i = 0; i < n; i++) csum[i + 1] = csum[i] + win_r->Sw[i];
    float det[S_LEN];
    for (int i = 0; i < n; i++) det[i] = win_r->Sw[i] - moving_average_at(csum, n, i, half_width);

    int cand[S_LEN]; int ncand = 0;
    for (int i = 1; i < n - 1; i++)
        if (det[i] >= det[i - 1] && det[i] > det[i + 1]) cand[ncand++] = i;
    if (ncand == 0) return out;
    for (int i = 0; i < ncand; i++) {
        int best = i;
        for (int j = i + 1; j < ncand; j++) if (det[cand[j]] > det[cand[best]]) best = j;
        int tmp = cand[i]; cand[i] = cand[best]; cand[best] = tmp;
    }
    int p1 = cand[0];
    int p2 = -1;
    for (int k = 1; k < ncand; k++) if (abs(cand[k] - p1) >= 8) { p2 = cand[k]; break; }
    float v1 = det[p1]; if (v1 < 1e-6f) v1 = 1e-6f;
    float conf;
    if (p2 < 0) conf = 1.2f;
    else {
        float v2 = det[p2]; if (v2 < 1e-6f) v2 = 1e-6f;
        conf = v1 / (v2 + 1e-9f);
    }
    out.bpm = 60.0f / (((float)win_r->lo + p1) / FFS);
    out.conf = conf;
    out.valid = true;
    return out;
}

/* ============================================================================================
 * HARMONIC-CONFIRMATION VETO
 * ============================================================================================ */
/* OPTIMIZATION (task 6): takes the ALREADY-COMPUTED sum_env_norm feature array as an input
 * instead of recomputing it. By the time this is called in c13opt_tick(), estimate_sumenv_robust
 * has already run sum_env_norm on this EXACT segment (same seg_clean_ar tail, same win_nsamp,
 * same FEAT_CLIP) for the main estimate -- Phase A recomputed the identical 3-band filter+
 * envelope+normalize work a second time here, purely redundant (the two call sites always use
 * the same window; nothing between them changes). Exact-equivalence, not corpus-revalidated:
 * this is literally the same numbers, just not computed twice. */
/* Returns 1=confirmed, 0=rejected, -1=no-opinion (window too short / candidate lag out of range) */
static int check_harmonic_confirmed(struct Combo13OptState *st, int seg_len, int win_nsamp,
                                     bool cand_valid, float cand_bpm,
                                     const float *precomputed_feat, int precomputed_m,
                                     float robust_trim_frac) {
    if (!cand_valid || seg_len < win_nsamp) return -1;
    float cand_lag = 6000.0f / cand_bpm;
    int hi_ext = (int)ceilf(HARMONIC_VETO_HI_EXT_MULT * cand_lag) + 2;
    if (hi_ext <= LO_LAG + 2) return 0;
    /* Defensive clamp not present in the Python reference: given cand_bpm can only arise from a
     * peak in the S_LEN=120 array (lag in [LO_LAG+1, LO_LAG+118] = [31,148]), hi_ext is bounded
     * by construction to ~325 at HARMONIC_VETO_HI_EXT_MULT=2.18 -- this clamp is a never-taken
     * safety net against buffer overrun, not a behavior change for any reachable input. */
    if (hi_ext > EXT_HI_MAX) hi_ext = EXT_HI_MAX;

    int m = precomputed_m;
    int n_ext = hi_ext - LO_LAG;
    if (m <= hi_ext) return 0;

    float *S_ext = st->scratch_Sext;
    for (int i = 0; i < n_ext; i++) S_ext[i] = 0.0f;
    add_autocorr_robust(st, S_ext, precomputed_feat, m, LO_LAG, hi_ext, robust_trim_frac);

    float target_lo = HARMONIC_VETO_LO_FRAC * cand_lag;
    float target_hi = HARMONIC_VETO_HI_FRAC * cand_lag;
    float smax = 0.0f;
    for (int i = 0; i < n_ext; i++) if (S_ext[i] > smax) smax = S_ext[i];
    float thresh = HARMONIC_VETO_HEIGHT_FRAC * smax;

    for (int i = 1; i < n_ext - 1; i++) {
        float lag = (float)(LO_LAG + i);
        if (lag > target_lo && lag < target_hi &&
            S_ext[i] >= S_ext[i - 1] && S_ext[i] > S_ext[i + 1] && S_ext[i] > thresh)
            return 1;
    }
    return 0;
}

/* ============================================================================================
 * PITCH-FAMILY "TILT" TRACE (independent agreement signal)
 * ============================================================================================ */
typedef struct { float re, im; } cplx;

static cplx fft_tw[FFT_N / 2];
static bool fft_tw_ready = false;

static void fft_init_twiddles(void) {
    for (int k = 0; k < FFT_N / 2; k++) {
        float ang = -2.0f * (float)M_PI * (float)k / (float)FFT_N;
        fft_tw[k].re = cosf(ang);
        fft_tw[k].im = sinf(ang);
    }
    fft_tw_ready = true;
}

/* in-place iterative radix-2 DIT FFT, N=FFT_N (power of 2) */
static void fft128(cplx *a) {
    if (!fft_tw_ready) fft_init_twiddles();
    PROF_FFT();
    int n = FFT_N;
    for (int i = 1, j = 0; i < n; i++) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) { cplx t = a[i]; a[i] = a[j]; a[j] = t; }
    }
    for (int len = 2; len <= n; len <<= 1) {
        int half = len >> 1;
        int step = n / len;
        for (int i = 0; i < n; i += len) {
            for (int k = 0; k < half; k++) {
                cplx w = fft_tw[k * step];
                cplx u = a[i + k];
                cplx v;
                v.re = a[i + k + half].re * w.re - a[i + k + half].im * w.im;
                v.im = a[i + k + half].re * w.im + a[i + k + half].im * w.re;
                a[i + k].re = u.re + v.re;
                a[i + k].im = u.im + v.im;
                a[i + k + half].re = u.re - v.re;
                a[i + k + half].im = u.im - v.im;
            }
        }
    }
}

/* Hann-windowed, zero-padded FFT magnitude -> log(hi-band energy / lo-band energy) tilt feature.
 * Fixed-parameter version of pitch_feature_for_segment (frame_len=80 hardcoded elsewhere). */
static float pitch_tilt_feature(const float *seg /* len TILT_FRAME_LEN */) {
    cplx buf[FFT_N];
    int n = TILT_FRAME_LEN;
    for (int i = 0; i < n; i++) {
        float hann = (n > 1) ? (0.5f - 0.5f * cosf(2.0f * (float)M_PI * (float)i / (float)(n - 1))) : 1.0f;
        buf[i].re = seg[i] * hann;
        buf[i].im = 0.0f;
    }
    for (int i = n; i < FFT_N; i++) { buf[i].re = 0.0f; buf[i].im = 0.0f; }
    fft128(buf);

    int n_bins = FFT_N / 2 + 1;
    float freq_res = FS_IN / (float)FFT_N;
    int lo_bin = (int)lroundf(PITCH_FFT_LO_HZ / freq_res); if (lo_bin < 1) lo_bin = 1;
    float hi_hz_capped = PITCH_FFT_HI_HZ < (FS_IN / 2.0f * 0.99f) ? PITCH_FFT_HI_HZ : (FS_IN / 2.0f * 0.99f);
    int hi_bin = (int)lroundf(hi_hz_capped / freq_res);
    if (hi_bin > n_bins - 1) hi_bin = n_bins - 1;
    if (hi_bin <= lo_bin) return 0.0f;
    int split_bin = (int)lroundf(TILT_SPLIT_HZ / freq_res);

    float lo_e = 0.0f, hi_e = 0.0f;
    for (int k = lo_bin; k <= hi_bin; k++) {
        float mag = sqrtf(buf[k].re * buf[k].re + buf[k].im * buf[k].im);
        if (k < split_bin) lo_e += mag * mag; else hi_e += mag * mag;
    }
    return logf((hi_e + 1e-9f) / (lo_e + 1e-9f));
}

/* OPTIMIZATION (task 7): streaming replacement for continuous_pitch_trace. Called from
 * c13opt_push() on every NEWLY ARRIVED raw sample (not from tick()) -- runs it through the
 * continuous-state pitch bandpass, maintains a small ring of just the last TILT_FRAME_LEN
 * filtered samples, and computes+caches a frame's tilt feature (with the sample-and-hold gate
 * logic already baked in) the moment enough new samples exist for it. This replaces ~500+
 * FFTs/tick over the whole window with ~29 FFTs/tick (one per newly-available frame).
 *
 * CAVEAT (accept­ed, corpus-validated -- see COMBO13_OPTIMIZATIONS.md): the RMS gate's threshold
 * uses a MONOTONIC historical max, not Python's per-tick max-over-the-whole-current-window.
 * Python's version is non-causal (an early frame's gate is computed using a max that includes
 * LATER frames in the same window, since it computes rms[] for the whole window before taking
 * the max) -- impossible for genuine real-time hardware, which can only know the past. This is
 * an inherent, necessary consequence of real streaming operation, not an implementation shortcut. */
static void streaming_tilt_push_sample(struct Combo13OptState *st, float raw_sample) {
    float y = iir4_step(PITCH_BP_B, PITCH_BP_A, raw_sample, st->pitch_bp_z);
    st->kumar_recent[st->kumar_recent_head] = y;
    st->kumar_recent_head = (st->kumar_recent_head + 1) % TILT_FRAME_LEN;
    if (st->kumar_recent_count < TILT_FRAME_LEN) st->kumar_recent_count++;
    st->total_filtered++;

    while (st->kumar_recent_count >= TILT_FRAME_LEN &&
           st->total_filtered >= (long)st->next_frame_idx * TILT_HOP + TILT_FRAME_LEN) {
        float frame[TILT_FRAME_LEN];
        int start = st->kumar_recent_head; /* oldest sample, since the ring is exactly full */
        for (int k = 0; k < TILT_FRAME_LEN; k++) frame[k] = st->kumar_recent[(start + k) % TILT_FRAME_LEN];

        float sumsq = 0.0f;
        for (int k = 0; k < TILT_FRAME_LEN; k++) sumsq += frame[k] * frame[k];
        float rms = sqrtf(sumsq / (float)TILT_FRAME_LEN);
        if (rms > st->running_max_rms) st->running_max_rms = rms;
        float gate = st->running_max_rms * PITCH_GATE_FRAC;
        if (rms >= gate) st->last_t_carry = pitch_tilt_feature(frame);

        st->tilt_ring[st->next_frame_idx % TILT_MAX_FRAMES] = st->last_t_carry;
        st->next_frame_idx++;
    }
}

static int peak_idx_tallest_local_max(const float *Sw, int n) {
    if (n < 3) return 0;
    int best = 0; float bv = -1e30f;
    for (int i = 1; i < n - 1; i++) {
        if (Sw[i] >= Sw[i - 1] && Sw[i] > Sw[i + 1] && Sw[i] > bv) { bv = Sw[i]; best = i; }
    }
    return best;
}

/* ============================================================================================
 * COMBINE STAGE HELPERS
 * ============================================================================================ */
static float bpm_favor_center(float bpm) {
    if (bpm <= TENT_PEAK_BPM) {
        float t = (bpm - 40.0f) / (TENT_PEAK_BPM - 40.0f);
        if (t < 0) t = 0; if (t > 1) t = 1;
        return TENT_LO_H + t * (TENT_PEAK_H - TENT_LO_H);
    }
    float t = (bpm - TENT_PEAK_BPM) / (200.0f - TENT_PEAK_BPM);
    if (t < 0) t = 0; if (t > 1) t = 1;
    return TENT_PEAK_H + t * (TENT_HI_H - TENT_PEAK_H);
}

static float teal_size_factor(float bpm, bool tilt_valid, float tilt_bpm) {
    bool agrees = tilt_valid && fabsf(tilt_bpm - bpm) <= AGREEMENT_BPM_TOL;
    float factor = bpm_favor_center(bpm);
    if (agrees) factor *= AGREE_BONUS;
    return factor;
}

static float weighted_median(const float *vals, const float *weights, int count) {
    /* selection-sort by value (count <= WEIGHTED_MED_TRAILING_N=8, trivially small) */
    int idx[WEIGHTED_MED_TRAILING_N];
    for (int i = 0; i < count; i++) idx[i] = i;
    for (int i = 0; i < count; i++) {
        int best = i;
        for (int j = i + 1; j < count; j++) if (vals[idx[j]] < vals[idx[best]]) best = j;
        int tmp = idx[i]; idx[i] = idx[best]; idx[best] = tmp;
    }
    float total = 0.0f;
    for (int i = 0; i < count; i++) total += weights[i];
    if (total <= 0.0f) {
        /* plain median fallback, matching np.median for the (rare/never-hit) zero-weight case */
        if (count % 2) return vals[idx[count / 2]];
        return 0.5f * (vals[idx[count / 2 - 1]] + vals[idx[count / 2]]);
    }
    float half = total / 2.0f;
    float cum = 0.0f;
    for (int i = 0; i < count; i++) {
        cum += weights[idx[i]];
        if (cum >= half) return vals[idx[i]];
    }
    return vals[idx[count - 1]];
}

static int required_teal_delay_ticks(float bpm) {
    float over = bpm - 60.0f; if (over < 0) over = 0;
    int t = (int)ceilf(1.0f + TEAL_BPM_DELAY_SLOPE * over);
    return t < 1 ? 1 : t;
}

/* ============================================================================================
 * PUBLIC API
 * ============================================================================================ */
void c13opt_init(Combo13OptState *st) {
    memset(st, 0, sizeof(*st));
    st->last_onset_el = NEG_INF;
    st->last_spike_el = NEG_INF;
    st->last_logged_spike_t = NEG_INF;
    st->prev_last_event_el = NEG_INF;
    st->peak_mean_below_since_el = NEG_INF;
    st->frozen_wait_s = CONS_WAIT_S;
    st->log_head = 0;
    st->log_count = 0;
    for (int i = 0; i < NLOG; i++) { st->cons_buf_log[i].valid = false; st->tilt_log[i].valid = false; }
}

void c13opt_push(Combo13OptState *st, const float *samples, int n) {
    if (n <= 0) return;

    /* streaming tilt-trace update (task 7) -- must see EVERY sample in order, independent of
     * how the raw_buf sliding window below handles chunking/trimming. */
    for (int i = 0; i < n; i++) streaming_tilt_push_sample(st, samples[i]);

    if (n >= RAW_MAX) {
        memcpy(st->raw_buf, samples + (n - RAW_MAX), RAW_MAX * sizeof(float));
        st->raw_len = RAW_MAX;
        return;
    }
    int total = st->raw_len + n;
    if (total <= RAW_MAX) {
        memcpy(st->raw_buf + st->raw_len, samples, n * sizeof(float));
        st->raw_len = total;
    } else {
        int drop = total - RAW_MAX;
        int keep_old = st->raw_len - drop;
        if (keep_old > 0) memmove(st->raw_buf, st->raw_buf + drop, keep_old * sizeof(float));
        else keep_old = 0;
        memcpy(st->raw_buf + keep_old, samples, n * sizeof(float));
        st->raw_len = RAW_MAX;
    }
}

/* push a new (val,conf) pair into the circular log; returns nothing, purely for bookkeeping */
static void log_push(Combo13OptState *st, bool val_valid, float val, bool conf_valid, float conf,
                      bool tilt_valid, float tilt) {
    int i = st->log_head;
    st->cons_buf_log[i].valid = val_valid;
    st->cons_buf_log[i].val = val;
    st->cons_buf_log[i].conf_valid = conf_valid;
    st->cons_buf_log[i].conf = conf;
    st->tilt_log[i].valid = tilt_valid;
    st->tilt_log[i].val = tilt;
    st->log_head = (i + 1) % NLOG;
    if (st->log_count < NLOG) st->log_count++;
}

Combo13OptTick c13opt_tick(Combo13OptState *st) {
    Combo13OptTick out; memset(&out, 0, sizeof(out));
    float el = st->el;
    out.t = el;

    int raw_len = st->raw_len;
    const float *raw = st->raw_buf;

    /* -- non-artifact-removed chain: declick -> decimate -> broad_env (onset) + decay preview --
     * (task 8: shares scratch_raw_stage -- fully consumed by decimate_stride8 on the next line,
     * before the artifact-removed chain below ever writes that buffer again.) */
    int seg_clean_len = 0;
    if (raw_len > 0) {
        declick_mult(raw, raw_len, TRANSIENT_LIMIT_MULT, st->scratch_raw_stage, st->scratch_sorted);
        seg_clean_len = decimate_stride8(st->scratch_raw_stage, raw_len, st->scratch_seg_clean);
    }

    float sig_level = 0.0f;
    if (seg_clean_len > 0) {
        int m = broad_env(st, st->scratch_seg_clean, seg_clean_len, st->scratch_feat_a);
        int n_trail = m; int want = (int)lroundf(1.5f * FFS); if (want < n_trail) n_trail = want;
        if (n_trail > 0) {
            float sumsq = 0.0f;
            for (int i = m - n_trail; i < m; i++) sumsq += st->scratch_feat_a[i] * st->scratch_feat_a[i];
            sig_level = sqrtf(sumsq / (float)n_trail);
        }
    }

    /* -- onset/flatline debounce + hysteresis -- */
    float signal_offset_thresh = SIGNAL_ONSET_THRESH * 0.5f;
    if (st->sig_was_onset_ok) {
        st->onset_on_streak = 0;
        if (sig_level < signal_offset_thresh) {
            st->onset_off_streak++;
            if (st->onset_off_streak >= ONSET_DEBOUNCE_TICKS) st->sig_was_onset_ok = false;
        } else st->onset_off_streak = 0;
    } else {
        st->onset_off_streak = 0;
        if (sig_level >= SIGNAL_ONSET_THRESH) {
            if (st->onset_on_streak == 0) st->onset_candidate_el = el;
            st->onset_on_streak++;
            if (st->onset_on_streak >= ONSET_DEBOUNCE_TICKS) {
                st->last_onset_el = st->onset_candidate_el;
                st->sig_was_onset_ok = true;
                st->onset_on_streak = 0;
            }
        } else st->onset_on_streak = 0;
    }

    /* -- spike detection (on raw, since compressor is omitted raw_for_detect == raw) -- */
    int spike_idx[64];
    int n_spikes = (raw_len > 0)
        ? detect_spike_runs(raw, raw_len, SPIKE_DETECT_MULT, spike_idx, 64, st->scratch_sorted)
        : 0;
    float win_start_el = el - (float)raw_len / FS_IN;
    if (n_spikes > 0) {
        float max_t = NEG_INF;
        for (int i = 0; i < n_spikes; i++) {
            float t = win_start_el + (float)spike_idx[i] / FS_IN;
            if (t > max_t) max_t = t;
        }
        if (max_t > st->last_logged_spike_t + 0.01f) {
            st->last_spike_el = max_t;
            st->last_logged_spike_t = max_t;
        }
    }

    float last_event_el = st->last_onset_el > st->last_spike_el ? st->last_onset_el : st->last_spike_el;
    float since_event = el - last_event_el;

    float adaptive_tau = since_event;
    if (adaptive_tau < DECAY_ADAPTIVE_TAU_MIN) adaptive_tau = DECAY_ADAPTIVE_TAU_MIN;
    if (adaptive_tau > DECAY_ADAPTIVE_TAU_MAX) adaptive_tau = DECAY_ADAPTIVE_TAU_MAX;

    /* decay-weighted preview estimate (adaptive tau) -- sizes the wait timer only, never displayed.
     * NOTE: ported with the SAME age-weighted autocorrelation shape as combo13_heart_rate.py's
     * add_autocorr_decay/estimate_decay, but computed inline here rather than as a separate
     * named function (kept local since it's only used for this one scalar). */
    bool r_adaptive_valid = false; float r_adaptive_bpm = 0.0f;
    if (seg_clean_len >= 3 * 500) {
        float S[S_LEN] = {0};
        float tau_frames = adaptive_tau * FFS;
        for (int b = 0; b < 3; b++) {
            int m = feat_norm_clip(st, st->scratch_seg_clean, seg_clean_len, b, FEAT_CLIP, st->scratch_feat_a);
            /* age-weighted autocorr: weight by age of the OLDER sample in each product pair */
            float w[FEAT_MAX];
            for (int i = 0; i < m; i++) w[i] = expf(-(float)(m - 1 - i) / tau_frames);
            float r0 = 0.0f, r0w = 0.0f;
            for (int i = 0; i < m; i++) { r0 += w[i] * st->scratch_feat_a[i] * st->scratch_feat_a[i]; r0w += w[i]; }
            r0 = r0 / (r0w != 0.0f ? r0w : 1.0f);
            if (m > HI_LAG) {
                for (int l = LO_LAG; l < HI_LAG; l++) {
                    int nn = m - l;
                    float s = 0.0f, sw = 0.0f;
                    PROF_AC(nn);
                    for (int i = 0; i < nn; i++) { s += w[i] * st->scratch_feat_a[i] * st->scratch_feat_a[i + l]; sw += w[i]; }
                    S[l - LO_LAG] += (s / (sw != 0.0f ? sw : 1.0f)) / (r0 + 1e-9f);
                }
            }
        }
        float Sw[S_LEN];
        for (int l = LO_LAG; l < HI_LAG; l++) Sw[l - LO_LAG] = S[l - LO_LAG] / powf((float)l, LAG_POW);
        float swmin = Sw[0]; for (int i = 1; i < S_LEN; i++) if (Sw[i] < swmin) swmin = Sw[i];
        for (int i = 0; i < S_LEN; i++) Sw[i] -= swmin;
        /* pick tallest local max (matches estimate_from_S's sort-by-height, first entry) */
        int p1 = -1; float bv = -1e30f;
        for (int i = 1; i < S_LEN - 1; i++)
            if (Sw[i] >= Sw[i - 1] && Sw[i] > Sw[i + 1] && Sw[i] > bv) { bv = Sw[i]; p1 = i; }
        if (p1 < 0) {
            r_adaptive_bpm = 60.0f / ((float)LO_LAG / FFS);
        } else {
            r_adaptive_bpm = 60.0f / (((float)LO_LAG + p1) / FFS);
        }
        r_adaptive_valid = true;
    }

    float win_sec = SINGLE_WIN_MIN + (since_event - st->frozen_wait_s > 0 ? since_event - st->frozen_wait_s : 0.0f);
    if (win_sec < SINGLE_WIN_MIN) win_sec = SINGLE_WIN_MIN;
    if (win_sec > SINGLE_WIN_MAX) win_sec = SINGLE_WIN_MAX;
    int win_nsamp = (int)lroundf(win_sec * 500.0f);

    /* -- artifact-removed chain: remove_transients -> declick -> decimate --
     * (task 8: reuses the SAME scratch_raw_stage the non-artifact-removed chain above already
     * finished with. declick_mult(scratch_raw_stage, ..., scratch_raw_stage, ...) below is
     * intentionally in-place -- safe because declick_mult is a pure elementwise clip: each
     * out[i] depends only on x[i], and the median is computed into a separate scratch array
     * BEFORE the clip loop touches x/out, so there's no read-after-write hazard.) */
    int seg_clean_ar_len = 0;
    if (raw_len > 0) {
        remove_transients(st, raw, raw_len, ARTIFACT_DETECT_MULT, ARTIFACT_EXTEND_MULT,
                           ARTIFACT_PAD_SAMPLES, ARTIFACT_BASELINE_PCTL, ARTIFACT_PASSES,
                           st->scratch_raw_stage);
        declick_mult(st->scratch_raw_stage, raw_len, TRANSIENT_LIMIT_MULT, st->scratch_raw_stage,
                     st->scratch_sorted);
        seg_clean_ar_len = decimate_stride8(st->scratch_raw_stage, raw_len, st->scratch_seg_clean_ar);
    }

    EstimateResult win_r; win_r.valid = false;
    if (seg_clean_ar_len >= win_nsamp && win_nsamp > 0) {
        win_r = estimate_sumenv_robust(st, st->scratch_seg_clean_ar + (seg_clean_ar_len - win_nsamp),
                                        win_nsamp, ROBUST_TRIM_FRAC, FEAT_CLIP);
    }
    EstimateResult win_teal = estimate_detrend_from_mw(&win_r, TEAL_PUSH_HALF_WIDTH);

    /* PEAK-MEAN FLATLINE DETECTOR (2026-08-04 fix -- see the struct field comment on
     * peak_mean_below_since_el). Mirrors pcg_visualizer's tickBody() exactly: broadEnv() over the
     * SAME trailing win_nsamp segment of seg_clean_ar already used for the main estimate, top-10%
     * mean minus overall mean as the decision variable, sustained >=1s below threshold (falling
     * edge only -- recovery is immediate) before actually declaring flatline. `st->sig_was_onset_ok`
     * (computed earlier in this function) is UNCHANGED and still drives last_onset_el/event-timing
     * below -- only the is_flatline BOOLEAN switches to this newer mechanism, exactly matching the
     * visualizer's own "onset-debounce machinery still runs UNCHANGED for event-timing/wait-timer
     * purposes, unrelated job" comment. */
    bool peak_mean_valid = false;
    float peak_minus_mean = 0.0f;
    if (seg_clean_ar_len >= win_nsamp && win_nsamp > 0) {
        const float *window_slice = st->scratch_seg_clean_ar + (seg_clean_ar_len - win_nsamp);
        int m = broad_env(st, window_slice, win_nsamp, st->scratch_feat_b);
        if (m > 0) {
            float sum = 0.0f;
            for (int i = 0; i < m; i++) sum += st->scratch_feat_b[i];
            float mean_abs = sum / (float)m;

            int top_n = (int)lroundf((float)m * PEAK_MEAN_TOP_PCT);
            if (top_n < 1) top_n = 1;
            int boundary = m - top_n; /* quickselect this index -> [boundary,m) is exactly the top_n largest */
            quickselectf(st->scratch_feat_b, m, boundary);
            float peak_sum = 0.0f;
            for (int i = boundary; i < m; i++) peak_sum += st->scratch_feat_b[i];
            float peak_level = peak_sum / (float)top_n;

            peak_minus_mean = peak_level - mean_abs;
            peak_mean_valid = true;
        }
    }
    bool peak_mean_below_now = (!peak_mean_valid) || (peak_minus_mean < PEAK_MEAN_DIFF_THRESH);
    if (peak_mean_below_now) {
        if (st->peak_mean_below_since_el == NEG_INF) st->peak_mean_below_since_el = el;
    } else {
        st->peak_mean_below_since_el = NEG_INF;
    }
    bool is_flatline = (st->peak_mean_below_since_el != NEG_INF) &&
                        (el - st->peak_mean_below_since_el) >= PEAK_MEAN_SUSTAIN_S;

    /* FIX (2026-08-04): CLEAR PUSHES ON FLATLINE -- ported from pcg_visualizer/index.html's
     * `if(isFlatline){for(let i=0;i<consBufLog.length;i++)consBufLog[i].val=null;}` (added there
     * 2026-07-28). Missing here because combo13_heart_rate.py (dated 2026-07-31, the file this
     * whole port is based on) never carried this fix forward -- confirmed directly against that
     * file's own source, not assumed. Without it, once signal resumes after a genuine off-chest
     * gap, the trailing weighted-median gather below walks backward PAST the flatline stretch
     * (skipping only invalid/never-pushed entries) and can reach a stale PRE-flatline value,
     * resurrecting an old rate instead of starting fresh. While is_flatline holds, every entry
     * currently in cons_buf_log is invalidated (array structure/ring position untouched, only the
     * validity flags) -- idempotent after the first flatline tick, same as the JS version. */
    if (is_flatline) {
        for (int i = 0; i < NLOG; i++) st->cons_buf_log[i].valid = false;
    }

    bool is_new_event = (last_event_el != st->prev_last_event_el);
    if (is_new_event) {
        st->prev_last_event_el = last_event_el;
        st->frozen_wait_s = CONS_WAIT_S + (r_adaptive_valid ? CONS_WAIT_EXTRA_S_PER_BPM * fabsf(r_adaptive_bpm - 60.0f) : 0.0f);
    }

    /* -- independent pitch-family tilt trace (task 7: read from the streaming ring, computed
     * incrementally in c13opt_push, instead of recomputing the whole trace from scratch here) -- */
    bool tilt_valid = false; float tilt_bpm = 0.0f;
    if (raw_len > 0) {
        int n_frames = (raw_len - TILT_FRAME_LEN) / TILT_HOP + 1;
        if (n_frames < 0) n_frames = 0;
        if (n_frames > st->next_frame_idx) n_frames = (int)st->next_frame_idx;
        if (n_frames > TILT_MAX_FRAMES) n_frames = TILT_MAX_FRAMES;
        long first_idx = st->next_frame_idx - n_frames;
        for (int i = 0; i < n_frames; i++)
            st->scratch_tilts[i] = st->tilt_ring[(first_idx + i) % TILT_MAX_FRAMES];
        if (n_frames > TILT_AC_HI) {
            float mean = 0.0f;
            for (int i = 0; i < n_frames; i++) mean += st->scratch_tilts[i];
            mean /= (float)n_frames;
            float c[TILT_MAX_FRAMES];
            for (int i = 0; i < n_frames; i++) c[i] = st->scratch_tilts[i] - mean;
            float S[TILT_AC_LEN] = {0};
            add_autocorr_plain(S, c, n_frames, TILT_AC_LO, TILT_AC_HI);
            int pk = peak_idx_tallest_local_max(S, TILT_AC_LEN);
            float trace_rate = FS_IN / (float)TILT_HOP;
            tilt_bpm = 60.0f * trace_rate / (float)(TILT_AC_LO + pk);
            tilt_valid = true;
        }
    }

    bool suppress_circle = is_flatline || is_new_event || (since_event < st->frozen_wait_s);

    bool raw_cand_valid = (!suppress_circle) && win_teal.valid;
    float raw_cand_bpm = raw_cand_valid ? win_teal.bpm : 0.0f;

    int harmonic_confirmed = -1;
    if (USE_HARMONIC_VETO) {
        /* reuses st->scratch_sumenv, populated by win_r's estimate_sumenv_robust call above on
         * this EXACT same segment (task 6) -- nothing between the two calls touches it. */
        int win_m = win_nsamp / FRAME_DS;
        harmonic_confirmed = check_harmonic_confirmed(st, seg_clean_ar_len, win_nsamp,
                                                        raw_cand_valid, raw_cand_bpm,
                                                        st->scratch_sumenv, win_m, ROBUST_TRIM_FRAC);
        if (harmonic_confirmed == 0) raw_cand_valid = false;
    }

    /* jump-confirmation guard */
    bool is_jump = false;
    if (raw_cand_valid && st->last_accepted_teal_bpm_valid) {
        float ratio = raw_cand_bpm / st->last_accepted_teal_bpm;
        is_jump = (ratio >= TEAL_JUMP_RATIO) || (ratio <= 1.0f / TEAL_JUMP_RATIO);
    }
    if (suppress_circle) st->teal_jump_streak = 0;
    else if (is_jump) st->teal_jump_streak++;
    else st->teal_jump_streak = 0;
    bool jump_unconfirmed = is_jump && (st->teal_jump_streak < TEAL_JUMP_CONFIRM_TICKS);

    /* bpm-scaled settling delay */
    bool same_as_anchor = false;
    if (raw_cand_valid && st->bpm_delay_anchor_valid) {
        float ratio = raw_cand_bpm / st->bpm_delay_anchor_bpm;
        same_as_anchor = (ratio < TEAL_JUMP_RATIO) && (ratio > 1.0f / TEAL_JUMP_RATIO);
    }
    if (!raw_cand_valid) {
        st->bpm_delay_streak = 0;
        st->bpm_delay_anchor_valid = false;
    } else if (same_as_anchor) {
        st->bpm_delay_streak++;
    } else {
        st->bpm_delay_streak = 1;
        st->bpm_delay_anchor_bpm = raw_cand_bpm;
        st->bpm_delay_anchor_valid = true;
    }
    bool bpm_delay_unconfirmed = raw_cand_valid && (st->bpm_delay_streak < required_teal_delay_ticks(raw_cand_bpm));

    bool pushed_valid = raw_cand_valid && !suppress_circle && !jump_unconfirmed && !bpm_delay_unconfirmed;
    float pushed_bpm = pushed_valid ? raw_cand_bpm : 0.0f;
    if (pushed_valid) { st->last_accepted_teal_bpm = pushed_bpm; st->last_accepted_teal_bpm_valid = true; }

    bool pushed_conf_valid = pushed_valid && win_teal.valid;
    float pushed_conf = pushed_conf_valid ? win_teal.conf : 0.0f;

    log_push(st, pushed_valid, pushed_bpm, pushed_conf_valid, pushed_conf,
              (!suppress_circle) && tilt_valid, tilt_bpm);

    /* -- weighted-median combine over the trailing window of accepted pushes -- */
    float vals[WEIGHTED_MED_TRAILING_N], weights[WEIGHTED_MED_TRAILING_N];
    int count = 0;
    int avail = st->log_count;
    for (int back = 1; back <= avail && count < WEIGHTED_MED_TRAILING_N; back++) {
        int idx = (st->log_head - back + NLOG) % NLOG;
        if (!st->cons_buf_log[idx].valid) continue;
        float v = st->cons_buf_log[idx].val;
        bool tv = st->tilt_log[idx].valid;
        float tval = st->tilt_log[idx].val;
        vals[count] = v;
        weights[count] = teal_size_factor(v, tv, tval);
        count++;
    }
    bool weighted_med_valid = count > 0;
    float weighted_med = weighted_med_valid ? weighted_median(vals, weights, count) : 0.0f;

    out.is_flatline = is_flatline;
    out.bpm_valid = (!is_flatline) && weighted_med_valid;
    out.bpm = out.bpm_valid ? weighted_med : 0.0f;
    out.raw_candidate_valid = raw_cand_valid;
    out.raw_candidate_bpm = raw_cand_valid ? raw_cand_bpm : 0.0f;
    out.harmonic_confirmed = harmonic_confirmed;

    st->el += TICK_S;
    return out;
}
