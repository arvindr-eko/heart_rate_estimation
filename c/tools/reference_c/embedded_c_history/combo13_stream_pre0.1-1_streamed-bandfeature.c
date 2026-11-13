/* HISTORICAL SNAPSHOT (renamed 2026-08-08, versioning rebase) -- this is the RAM/clock "Pass 4"
 * checkpoint (streamed band-feature extraction, STATE_SIZE_BYTES=26,784), superseded by every
 * later pre-0.1 checkpoint and ultimately by revision 0.1 (combo13_stream.c). Kept for reference
 * only -- do not build firmware from this file. See project memory (project_pcg_embedded_c) for
 * the full history "plan of record" used to informally track before this rebase. */

/* combo13_stream.c -- RAM-reduced streaming-front-end variant of the combo13 pipeline.
 *
 * GOAL: cut RAM from combo13_opt's 761KB (88% of which is six 8-second, native-rate/32000-sample
 * buffers needed by the whole-window batch artifact-removal design) down to Daniel's 50-70KB
 * PSoC6 target. See combo13_stream_internal.h for the full design rationale.
 *
 * WHAT CHANGED vs. combo13_opt.c (this is a genuine algorithm redesign, not a memory trick):
 *   1. remove_transients/declick_mult/detect_spike_runs' "sort/percentile over the whole 8s
 *      window" replaced with a streaming EMA-of-per-chunk-percentile estimator (StreamBaseline),
 *      updated continuously, plus detect/extend/interpolate over a SMALL chunk+carry window
 *      instead of the whole history.
 *   2. The two parallel decimated chains (with/without artifact-removal) consolidated into ONE
 *      -- onset detection and the r_adaptive preview now read from the SAME artifact-removed
 *      decimated signal as the main estimate (Daniel's explicit choice, 2026-08-04).
 *   3. STREAM_VARIANT (compile-time) controls WHERE detection happens relative to decimation,
 *      and at what decimated rate -- this file exists specifically to let Daniel compare:
 *        1 = detect at native rate (4kHz), decimate to 500Hz after cleaning
 *        2 = decimate to 500Hz first, detect at 500Hz
 *        3 = decimate to ~666.7Hz first, detect at that rate
 *
 * NOT YET DONE: full corpus retuning of the streaming-specific constants (chunk size, EMA decay,
 * rescaled pad_samples) -- these are reasoned first-pass values, validated only at the level of
 * "does this behave sanely," not corpus-tuned the way combo13's original constants were. Treat
 * this file as an exploratory comparison, per Daniel's own framing ("try both and see what the
 * performance looks like"), not a drop-in replacement for combo13_opt.c.
 */
#include "combo13_stream.h"
#include <math.h>
#include <string.h>
#include <stdlib.h>
#ifdef STREAM_DEBUG
#include <stdio.h>
#endif

#define FS_IN 4000.0f
#define TICK_S 0.25f
#define FRAME_DS FRAME_DS_V
#define LO_LAG LO_LAG_V
#define HI_LAG HI_LAG_V
#define S_LEN S_LEN_V
#define LAG_POW 0.1f
#define FFT_N 128
#define TILT_AC_LO 35
#define TILT_AC_HI 176
#define TILT_AC_LEN (TILT_AC_HI - TILT_AC_LO)

/* Decimated sample rate actually produced by DEC_STRIDE, used for FFS/pad-sample rescaling. */
#define DEC_HZ (FS_IN / (float)DEC_STRIDE)
#define FFS (DEC_HZ / (float)FRAME_DS)

/* combo13's tuned scalar constants -- unchanged from combo13_opt where dimensionless. */
static const float SIGNAL_ONSET_THRESH = 0.00021f;
static const int   ONSET_DEBOUNCE_TICKS = 4;
static const float SPIKE_DETECT_MULT = 20.0f;
static const float TRANSIENT_LIMIT_MULT = 4.29f;
static const float ARTIFACT_DETECT_MULT = 16.96f;
static const float ARTIFACT_EXTEND_MULT = 8.02f;
static const float ARTIFACT_BASELINE_PCTL = 0.4231f;
static const float ROBUST_TRIM_FRAC = 0.0079f;
static const float FEAT_CLIP = 4.5f;
static const float SINGLE_WIN_MIN = 4.0f;
static const float SINGLE_WIN_MAX = 8.0f;
static const int   TEAL_PUSH_HALF_WIDTH = 36;
static const float CONS_WAIT_S = 6.0f; /* raised from 4.0f (2026-08-04) as part of the startup-transient fix -- see COMBO13_OPTIMIZATIONS.md */
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
/* Extra required agreement ticks tapering off over the RAMP_DELAY_TAPER_S seconds right after the
 * frozen wait lifts -- combo13_opt doesn't need this (exact whole-window recompute every tick),
 * but corpus data localized combo13_stream's extra garbage-lock regression to roughly this window
 * (5-20s post-onset), where the streaming baseline/decimation state is still settling. */
static const float RAMP_DELAY_TAPER_S = 15.0f;
static const float RAMP_DELAY_EXTRA_TICKS = 4.0f;
static const float TILT_SPLIT_HZ = 45.06f;
static const float PITCH_FFT_LO_HZ = 14.27f;
static const float PITCH_FFT_HI_HZ = 226.82f;
static const float PITCH_GATE_FRAC = 0.0658f;

/* peak-mean flatline detector -- same design/scale-rescale reasoning as combo13_opt's 2026-08-04
 * fix (see that file for the full derivation): JS's 0.003 threshold operates on pcgRing's
 * normalized [-1,1] scale; this port operates on raw wav-scale, rescaled by *32768. */
static const float PEAK_MEAN_TOP_PCT = 0.10f;
static const float PEAK_MEAN_DIFF_THRESH = 0.003f * 32768.0f;
static const float PEAK_MEAN_SUSTAIN_S = 1.0f;
/* minimum is_flatline duration to count as a genuine disconnect/reconnect (vs. a brief few-second
 * physiological dip) for purposes of the flatline-clear-event fix below */
static const float FLATLINE_FULL_RESET_MIN_S = 1.5f;

static const bool  USE_HARMONIC_VETO = true;
static const float HARMONIC_VETO_HI_EXT_MULT = 2.1813774614710146f;
static const float HARMONIC_VETO_HEIGHT_FRAC = 0.05895565949526575f;
static const float HARMONIC_VETO_LO_FRAC = 1.9239938526174543f;
static const float HARMONIC_VETO_HI_FRAC = 2.081876428164688f;

#define DECAY_ADAPTIVE_TAU_MIN 1.0f
#define DECAY_ADAPTIVE_TAU_MAX 8.0f
static const float NEG_INF = -1e30f;

/* -- ARTIFACT_PAD_SAMPLES was tuned at NATIVE rate (36 samples @ 4000Hz = 9ms). The padding's
 * job (widen the excised region so interpolation doesn't leave a residual click right at the
 * boundary) is a TIME-domain concept, so it's rescaled to preserve ~9ms at whatever rate
 * detection actually runs at, rather than reusing "36 samples" unchanged at a different rate
 * (which would mean a wildly different padding DURATION). Reasoned, not yet corpus-retuned. */
#if DETECT_AT_NATIVE
static const int ARTIFACT_PAD_SAMPLES_V = 36;                 /* native rate: unchanged */
#elif DEC_STRIDE == 8
static const int ARTIFACT_PAD_SAMPLES_V = 5;   /* round(36 * 500/4000) */
#elif DEC_STRIDE == 6
static const int ARTIFACT_PAD_SAMPLES_V = 6;   /* round(36 * 666.667/4000) */
#endif

/* -- streaming baseline EMA decay: chosen so the effective averaging window approximates a few
 * seconds (roughly half of the original 8s whole-window design), independent of chunk rate --
 * alpha = exp(-chunk_duration_s / tau), tau=4s. Reasoned starting point, not corpus-tuned. */
#define BASELINE_TAU_S 1.0f

/* ============================================================================================
 * FIXED FILTER COEFFICIENTS
 * ============================================================================================ */
#if DEC_STRIDE == 8
/* 4000Hz -> 500Hz, cutoff 220Hz (0.88 * new Nyquist 250Hz) -- unchanged from combo13_opt. */
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
#elif DEC_STRIDE == 6
/* 4000Hz -> 666.67Hz, cutoff 293.33Hz (same 0.88 ratio to the new Nyquist 333.33Hz), and the
 * 3-band filterbank redesigned for fs=666.67Hz (same 30-50/50-80/80-130Hz absolute bands). */
static const float AA_B[5] = {0.001647044374329831f, 0.006588177497319324f, 0.009882266245978986f,
                               0.006588177497319324f, 0.001647044374329831f};
static const float AA_A[5] = {1.0f, -2.8007313099532984f, 3.070799253199432f, -1.5398998142263016f,
                               0.29618458096944505f};
static const float BAND_B[3][5] = {
    {0.007820208033497195f, 0, -0.01564041606699439f, 0, 0.007820208033497195f},
    {0.01658193166930305f, 0, -0.0331638633386061f, 0, 0.01658193166930305f},
    {0.04125353724172028f, 0, -0.08250707448344057f, 0, 0.04125353724172028f},
};
static const float BAND_A[3][5] = {
    {1.0f, -3.48793980138064f, 4.788095777659303f, -3.050876980518939f, 0.7660066009432641f},
    {1.0f, -2.978431944281721f, 3.840435522149019f, -2.4337686546267747f, 0.6704579059531741f},
    {1.0f, -1.890905580595881f, 2.261674923723877f, -1.3420711824184908f, 0.5139818942196758f},
};
#else
#error "unhandled DEC_STRIDE"
#endif

static const float PITCH_BP_B[5] = {0.013051383589961553f, 0, -0.026102767179923107f, 0, 0.013051383589961553f};
static const float PITCH_BP_A[5] = {1.0f, -3.6382064558549789f, 4.9826990589367766f, -3.0484777138479626f,
                                     0.70403179011858719f};
static const float ENV_A = 0.7975628241529755f;

unsigned long c13stream_state_size(void) { return (unsigned long)sizeof(struct Combo13StreamState); }

/* ============================================================================================
 * DSP PRIMITIVES (same designs as combo13_opt.c -- iir4/env_singlepole are both safe in-place,
 * used that way throughout this file specifically to avoid needing separate filt/env buffers)
 * ============================================================================================ */
static float iir4_step(const float b[5], const float a[5], float x, float z[4]) {
    float y = b[0] * x + z[0];
    z[0] = b[1] * x - a[1] * y + z[1];
    z[1] = b[2] * x - a[2] * y + z[2];
    z[2] = b[3] * x - a[3] * y + z[3];
    z[3] = b[4] * x - a[4] * y;
    return y;
}

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
    for (int i = lo; i < hi; i++) if (a[i] < pivot) { swapf(&a[i], &a[store]); store++; }
    swapf(&a[store], &a[hi]);
    return store;
}
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
static void quickselectf(float *a, int n, int k) { quickselectf_range(a, 0, n - 1, k); }

/* ============================================================================================
 * STREAMING BASELINE (replaces "sort the whole 8s window for a percentile")
 * ============================================================================================ */
static void baseline_init(StreamBaseline *sb) {
    sb->ema = 0.0f; sb->initialized = false; sb->warm_sum = 0.0f; sb->warm_dur = 0.0f;
}

#define BASELINE_WARMUP_S 1.0f

/* Computes the EXACT percentile of |chunk| (via quickselect, chunk-sized scratch only -- no
 * whole-window sort), then EMA-blends it into the persistent estimate. `scratch` must be >= n.
 * For the first BASELINE_WARMUP_S seconds, `ema` is instead a duration-weighted running mean of
 * every chunk seen so far -- a single ~100ms chunk's percentile is noisy enough on its own that
 * seeding the EMA directly from it (the original design) left the artifact/decline threshold
 * under-converged for several seconds after onset; corpus data showed this window (roughly 5-20s
 * into a recording) is where combo13_stream's garbage-value rate exceeds combo13_opt's exact
 * whole-window reference. */
static float baseline_update(StreamBaseline *sb, const float *chunk, int n, float pctl,
                              float chunk_duration_s, float *scratch) {
    for (int i = 0; i < n; i++) scratch[i] = fabsf(chunk[i]);
    int idx = (int)(n * pctl); if (idx > n - 1) idx = n - 1; if (idx < 0) idx = 0;
    quickselectf(scratch, n, idx);
    float chunk_val = scratch[idx];
    if (!sb->initialized) {
        sb->warm_sum += chunk_val * chunk_duration_s;
        sb->warm_dur += chunk_duration_s;
        sb->ema = sb->warm_sum / sb->warm_dur;
        if (sb->warm_dur >= BASELINE_WARMUP_S) sb->initialized = true;
    } else {
        float alpha = expf(-chunk_duration_s / BASELINE_TAU_S);
        sb->ema = alpha * sb->ema + (1.0f - alpha) * chunk_val;
    }
    return sb->ema;
}

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

/* Single-pass detect->extend->interpolate over a SMALL [carry+chunk] buffer, using the
 * ALREADY-ESTABLISHED (pre-this-chunk) streaming baseline for the threshold, rather than
 * re-deriving it from scratch each time (Phase A's 5-pass design existed to keep the baseline
 * uncontaminated by a spike it was itself about to detect -- here the EMA already reflects many
 * PRIOR clean chunks, so a single detection pass against a stable historical estimate serves the
 * same purpose without needing multiple whole-window passes). Modifies `buf` in place. */
static void stream_detect_extend_interp(float *buf, int n, float scale, float detect_mult,
                                          float extend_mult, int pad_samples,
                                          bool *scratch_grown, float *scratch_absx) {
    float lim = detect_mult * scale;
    float extend_lim = extend_mult * scale;
    for (int i = 0; i < n; i++) { scratch_absx[i] = fabsf(buf[i]); scratch_grown[i] = false; }

    bool any = false;
    int i = 0;
    while (i < n) {
        if (scratch_grown[i] || scratch_absx[i] <= lim) { i++; continue; }
        int lo = i;
        while (lo > 0 && scratch_absx[lo - 1] > extend_lim) lo--;
        int hi = i;
        while (hi < n - 1 && scratch_absx[hi + 1] > extend_lim) hi++;
        for (int k = lo; k <= hi; k++) if (!scratch_grown[k]) { scratch_grown[k] = true; any = true; }
        i = hi + 1;
    }
    if (!any) return;

    if (pad_samples > 0) {
        static bool padded_static[NATIVE_CARRY_N + NATIVE_CHUNK_N > DEC_CARRY_N + DEC_CHUNK_N
                                   ? NATIVE_CARRY_N + NATIVE_CHUNK_N : DEC_CARRY_N + DEC_CHUNK_N];
        bool *padded = padded_static;
        memcpy(padded, scratch_grown, n * sizeof(bool));
        for (int center = 0; center < n; center++) {
            if (!scratch_grown[center]) continue;
            int lo = center - pad_samples; if (lo < 0) lo = 0;
            int hi = center + pad_samples + 1; if (hi > n) hi = n;
            for (int k = lo; k < hi; k++) padded[k] = true;
        }
        float orig[NATIVE_CARRY_N + NATIVE_CHUNK_N > DEC_CARRY_N + DEC_CHUNK_N
                    ? NATIVE_CARRY_N + NATIVE_CHUNK_N : DEC_CARRY_N + DEC_CHUNK_N];
        memcpy(orig, buf, n * sizeof(float));
        interp_over_mask(buf, orig, padded, n);
    } else {
        float orig[NATIVE_CARRY_N + NATIVE_CHUNK_N > DEC_CARRY_N + DEC_CHUNK_N
                    ? NATIVE_CARRY_N + NATIVE_CHUNK_N : DEC_CARRY_N + DEC_CHUNK_N];
        memcpy(orig, buf, n * sizeof(float));
        interp_over_mask(buf, orig, scratch_grown, n);
    }
}

/* declick clip (median-based), in place -- same math as combo13_opt's declick_mult but using the
 * streaming median estimate instead of a whole-window median. */
static void declick_clip(float *buf, int n, float scale, float mult) {
    float lim = mult * scale;
    for (int i = 0; i < n; i++) {
        float v = buf[i];
        buf[i] = v > lim ? lim : (v < -lim ? -lim : v);
    }
}

/* ============================================================================================
 * PERSISTENT PER-BAND FEATURE STREAM (2026-08-05, RAM/clock pass 4) -- replaces the old raw-
 * decimated-history ring (dec_hist) + per-tick from-scratch gather/filter/envelope (scratch_work)
 * entirely. See combo13_stream_internal.h's comment on band_filt_z/band_env_ring for the design.
 * ============================================================================================ */
static void stream_dec_samples(struct Combo13StreamState *st, const float *samples, int n) {
    for (int i = 0; i < n; i++) {
        if (st->dec_sample_count < DEC_MAX_V) st->dec_sample_count++;

        float env[3];
        for (int b = 0; b < 3; b++) {
            float filtered = iir4_step(BAND_B[b], BAND_A[b], samples[i], st->band_filt_z[b]);
            float e = (1.0f - ENV_A) * fabsf(filtered) + ENV_A * st->band_env_z[b];
            st->band_env_z[b] = e;
            env[b] = e;
        }
        st->band_frame_phase++;
        if (st->band_frame_phase >= FRAME_DS) {
            st->band_frame_phase = 0;
            for (int b = 0; b < 3; b++) {
                float ec = env[b] < -32768.0f ? -32768.0f : (env[b] > 32767.0f ? 32767.0f : env[b]);
                st->band_env_ring[b][st->band_env_ring_head] = (int16_t)lroundf(ec);
            }
            st->band_env_ring_head = (st->band_env_ring_head + 1) % FEAT_MAX_V;
            if (st->band_env_ring_len < FEAT_MAX_V) st->band_env_ring_len++;
        }
    }
}
static void band_env_gather_trailing(const struct Combo13StreamState *st, int band_idx, int m, float *out) {
    int start = ((st->band_env_ring_head - m) % FEAT_MAX_V + FEAT_MAX_V) % FEAT_MAX_V;
    for (int i = 0; i < m; i++) out[i] = (float)st->band_env_ring[band_idx][(start + i) % FEAT_MAX_V];
}

/* ============================================================================================
 * FEATURE EXTRACTION -- gathers trailing frames straight from the persistent per-band envelope
 * rings (stream_dec_samples keeps these current, one push per FRAME_DS decimated samples). No
 * raw filtering happens here anymore (2026-08-05, RAM/clock pass 4) -- see
 * combo13_stream_internal.h's comment on band_filt_z/band_env_ring for the design.
 * ============================================================================================ */
static int feat_norm_clip(struct Combo13StreamState *st, int win_n, int band_idx, float clip, float *out) {
    int m = win_n / FRAME_DS;
    band_env_gather_trailing(st, band_idx, m, out);
    float sum = 0.0f;
    for (int i = 0; i < m; i++) sum += out[i];
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

static int sum_env_norm(struct Combo13StreamState *st, int win_n, float clip, float *out) {
    int m = feat_norm_clip(st, win_n, 0, clip, st->scratch_feat_a);
    feat_norm_clip(st, win_n, 1, clip, st->scratch_feat_b);
    feat_norm_clip(st, win_n, 2, clip, out);   /* band 2 written directly into the caller's out[] */
    for (int i = 0; i < m; i++) out[i] += st->scratch_feat_a[i] + st->scratch_feat_b[i];
    return m;
}

/* unnormalized summed envelope -- signal-level (onset) and peak-mean (flatline) only */
static int broad_env(struct Combo13StreamState *st, int win_n, float *out) {
    int m = win_n / FRAME_DS;
    band_env_gather_trailing(st, 0, m, out);
    float tmp[FEAT_MAX_V];
    for (int b = 1; b < 3; b++) {
        band_env_gather_trailing(st, b, m, tmp);
        for (int i = 0; i < m; i++) out[i] += tmp[i];
    }
    return m;
}

/* ============================================================================================
 * AUTOCORRELATION (trimmed-mean-of-products, via quickselect -- same design as combo13_opt's
 * task-5 optimization, `prod` scratch reuses scratch_feat_a since it's already free by the time
 * this runs: sum_env_norm's own use of scratch_feat_a is fully consumed into scratch_sumenv
 * before add_autocorr_robust is ever called.)
 * ============================================================================================ */
static void add_autocorr_robust(struct Combo13StreamState *st, float *S, const float *f, int T,
                                 int lo, int hi, float trim_frac) {
    if (T <= hi) return;
    float r0 = 0.0f;
    for (int i = 0; i < T; i++) r0 += f[i] * f[i];
    r0 /= (float)T;
    float *prod = st->scratch_feat_a;
    for (int l = lo; l < hi; l++) {
        int n = T - l;
        for (int i = 0; i < n; i++) prod[i] = f[i] * f[i + l];
        int trim = (int)(n * trim_frac);
        int keep = n - 2 * trim;
        float mean_val = 0.0f;
        if (keep > 0) {
            quickselectf(prod, n, trim);
            if (keep > 1) quickselectf_range(prod, trim, n - 1, n - 1 - trim);
            float sum = 0.0f;
            for (int i = trim; i < n - trim; i++) sum += prod[i];
            mean_val = sum / (float)keep;
        }
        S[l - lo] += mean_val / (r0 + 1e-9f);
    }
}

static void add_autocorr_plain(float *S, const float *f, int T, int lo, int hi) {
    if (T <= hi) return;
    float r0 = 0.0f;
    for (int i = 0; i < T; i++) r0 += f[i] * f[i];
    r0 /= (float)T;
    for (int l = lo; l < hi; l++) {
        int n = T - l;
        float s = 0.0f;
        for (int i = 0; i < n; i++) s += f[i] * f[i + l];
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

static EstimateResult estimate_sumenv_robust(struct Combo13StreamState *st, int win_n,
                                              float trim_frac, float feat_clip) {
    EstimateResult r; r.valid = false;
    if (win_n < 3 * (int)DEC_HZ) return r;
    float S[S_LEN] = {0};
    int m = sum_env_norm(st, win_n, feat_clip, st->scratch_sumenv);
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
 * HARMONIC-CONFIRMATION VETO (unchanged design from combo13_opt's task-6 de-duplication)
 * ============================================================================================ */
static int check_harmonic_confirmed(struct Combo13StreamState *st, int hist_len, int win_nsamp,
                                     bool cand_valid, float cand_bpm,
                                     const float *precomputed_feat, int precomputed_m,
                                     float robust_trim_frac) {
    if (!cand_valid || hist_len < win_nsamp) return -1;
    float cand_lag = 60.0f * FFS / cand_bpm;
    int hi_ext = (int)ceilf(HARMONIC_VETO_HI_EXT_MULT * cand_lag) + 2;
    if (hi_ext <= LO_LAG + 2) return 0;
    if (hi_ext > EXT_HI_MAX_V) hi_ext = EXT_HI_MAX_V;

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
 * PITCH-FAMILY "TILT" TRACE -- UNCHANGED from combo13_opt's task-7 streaming design: fed
 * directly from raw NATIVE-rate samples, entirely independent of this file's decimation-rate/
 * detection-rate variant, so it's ported verbatim.
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
static void fft128(cplx *a) {
    if (!fft_tw_ready) fft_init_twiddles();
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
static float pitch_tilt_feature(const float *seg) {
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
static void streaming_tilt_push_sample(struct Combo13StreamState *st, float raw_sample) {
    float y = iir4_step(PITCH_BP_B, PITCH_BP_A, raw_sample, st->pitch_bp_z);
    st->kumar_recent[st->kumar_recent_head] = y;
    st->kumar_recent_head = (st->kumar_recent_head + 1) % TILT_FRAME_LEN;
    if (st->kumar_recent_count < TILT_FRAME_LEN) st->kumar_recent_count++;
    st->total_filtered++;
    while (st->kumar_recent_count >= TILT_FRAME_LEN &&
           st->total_filtered >= (long)st->next_frame_idx * TILT_HOP + TILT_FRAME_LEN) {
        float frame[TILT_FRAME_LEN];
        int start = st->kumar_recent_head;
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
    for (int i = 1; i < n - 1; i++)
        if (Sw[i] >= Sw[i - 1] && Sw[i] > Sw[i + 1] && Sw[i] > bv) { bv = Sw[i]; best = i; }
    return best;
}

/* ============================================================================================
 * COMBINE STAGE HELPERS (unchanged from combo13_opt)
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
static int required_teal_delay_ticks(float bpm, float time_since_wait_end) {
    float over = bpm - 60.0f; if (over < 0) over = 0;
    int t = (int)ceilf(1.0f + TEAL_BPM_DELAY_SLOPE * over);
    if (time_since_wait_end < RAMP_DELAY_TAPER_S) {
        float frac = (RAMP_DELAY_TAPER_S - time_since_wait_end) / RAMP_DELAY_TAPER_S;
        t += (int)ceilf(RAMP_DELAY_EXTRA_TICKS * frac);
    }
    return t < 1 ? 1 : t;
}

/* ============================================================================================
 * STREAMING FRONT-END: chunked artifact-removal + declick + spike-eventing, then streaming
 * decimation. Shared logic for whichever rate detection runs at (native for variant 1,
 * decimated for variants 2/3) via generic buffer/size parameters -- only ONE of the two call
 * sites in c13stream_push() below is compiled per variant, per DETECT_AT_NATIVE.
 * ============================================================================================ */
#define _MAXA (NATIVE_CARRY_N + NATIVE_CHUNK_N)
#define _MAXB (DEC_CARRY_N + DEC_CHUNK_N)
#define MAX_CHUNK_TOTAL (_MAXA > _MAXB ? _MAXA : _MAXB)

/* out_dec_cb: callback-style emission -- appends the newly-cleaned chunk into whatever the
 * caller needs (dec_hist directly for variants 2/3, or the streaming AA-decimate for variant 1). */
typedef void (*ChunkEmitFn)(struct Combo13StreamState *st, const float *cleaned_chunk, int chunk_n);

static void process_and_emit_chunk(struct Combo13StreamState *st, float *buf, int carry_n, int chunk_n,
                                     float rate_hz, int pad_samples, ChunkEmitFn emit) {
    int total = carry_n + chunk_n;
    bool scratch_grown[MAX_CHUNK_TOTAL];
    float scratch_absx[MAX_CHUNK_TOTAL];
    float raw_chunk[MAX_CHUNK_TOTAL];
    memcpy(raw_chunk, buf + carry_n, chunk_n * sizeof(float));

    /* spike detection (event-timing only, does not modify the signal) -- uses the median
     * baseline as it stood BEFORE this chunk (consistent with the artifact detector's own use
     * of the pre-chunk baseline), scanning just the NEW portion (buf[carry_n..total)).
     * st->chunk_time_cursor is the wall-clock time of buf[carry_n] (the start of the new
     * portion) on the SAME timeline c13stream_tick()'s `el` advances -- each detected run's
     * peak sample converts to absolute time via that cursor plus its offset into the chunk. */
    if (st->med_baseline.initialized) {
        float med_scale = st->med_baseline.ema * 1.4826f;
        if (med_scale == 0.0f) med_scale = 1e-6f;
        float lim = SPIKE_DETECT_MULT * med_scale;
        float max_t = NEG_INF;
        bool any_spike = false;
        int i = carry_n;
        while (i < total) {
            if (fabsf(buf[i]) <= lim) { i++; continue; }
            int j = i; float best_v = -1.0f; int best_local = i;
            while (j < total && fabsf(buf[j]) > lim) {
                float av = fabsf(buf[j]);
                if (av > best_v) { best_v = av; best_local = j; }
                j++;
            }
            float spike_t = st->chunk_time_cursor + (float)(best_local - carry_n) / rate_hz;
            if (spike_t > max_t) max_t = spike_t;
            any_spike = true;
            i = j;
        }
        if (any_spike && max_t > st->last_logged_spike_t + 0.01f) {
            st->last_spike_el = max_t;
            st->last_logged_spike_t = max_t;
        }
    }

    /* -- artifact detect/extend/interpolate, using the PRE-chunk baseline (established from many
     * prior chunks, not re-derived from this potentially-contaminated one) -- */
    float art_scale = 1e-6f;
    if (st->art_baseline.initialized) {
        art_scale = st->art_baseline.ema * 1.4826f;
        if (art_scale == 0.0f) art_scale = 1e-6f;
        stream_detect_extend_interp(buf, total, art_scale, ARTIFACT_DETECT_MULT, ARTIFACT_EXTEND_MULT,
                                      pad_samples, scratch_grown, scratch_absx);
    }

    /* -- declick clip, using the median baseline (established pre-chunk) -- */
    if (st->med_baseline.initialized) {
        float med_scale = st->med_baseline.ema * 1.4826f;
        if (med_scale == 0.0f) med_scale = 1e-6f;
        declick_clip(buf, total, med_scale, TRANSIENT_LIMIT_MULT);
    }

    /* -- update both baselines from the RAW (pre-cleaning) chunk, not the artifact-removed/
     * declicked output -- both percentiles used here (0.4231, 0.5/median) are already robust to
     * a MINORITY of contaminated samples within one ~100ms chunk, the same robustness
     * combo13_opt's whole-window percentile relies on (it recomputes from the real current raw
     * window every tick, never from previously-censored output). Updating from the cleaned chunk
     * instead was a real bug: during a sustained quiet stretch both baselines decay toward the
     * quiet amplitude, and when real signal resumes, every sample looks like a giant artifact
     * relative to that collapsed threshold -- gets suppressed -- and the baseline then only ever
     * sees the suppressed near-zero result, permanently locking out real signal. Found via a
     * loud-quiet-loud recovery test that no prior corpus/synthetic-gap test had exercised. */
    float chunk_duration_s = (float)chunk_n / rate_hz;
    baseline_update(&st->art_baseline, raw_chunk, chunk_n, ARTIFACT_BASELINE_PCTL, chunk_duration_s, scratch_absx);
    baseline_update(&st->med_baseline, raw_chunk, chunk_n, 0.5f, chunk_duration_s, scratch_absx);

    /* -- emit the cleaned new chunk, then shift the tail into the carry region -- */
    emit(st, buf + carry_n, chunk_n);
    memmove(buf, buf + chunk_n, carry_n * sizeof(float));
    st->chunk_time_cursor += chunk_duration_s;
}

/* ============================================================================================
 * STREAMING DECIMATION (continuous AA filter + stride-keep, same technique already validated
 * for the pitch-bandpass filter in task 7 -- carries state across calls, never resets)
 * ============================================================================================ */
static int streaming_decimate(struct Combo13StreamState *st, const float *samples, int n, float *out) {
    int m = 0;
    for (int i = 0; i < n; i++) {
        float y = iir4_step(AA_B, AA_A, samples[i], st->dec_aa_z);
        if (st->dec_stride_phase == 0) out[m++] = y;
        st->dec_stride_phase = (st->dec_stride_phase + 1) % DEC_STRIDE;
    }
    return m;
}

#if DETECT_AT_NATIVE
static void emit_native_cleaned_chunk(struct Combo13StreamState *st, const float *cleaned_chunk, int chunk_n) {
    float dec_out[NATIVE_CHUNK_N];
    int m = streaming_decimate(st, cleaned_chunk, chunk_n, dec_out);
    stream_dec_samples(st, dec_out, m);
}
#else
static void emit_dec_cleaned_chunk(struct Combo13StreamState *st, const float *cleaned_chunk, int chunk_n) {
    stream_dec_samples(st, cleaned_chunk, chunk_n);
}
#endif

/* ============================================================================================
 * PUBLIC API
 * ============================================================================================ */
void c13stream_init(Combo13StreamState *st) {
    memset(st, 0, sizeof(*st));
    st->last_onset_el = NEG_INF;
    st->last_spike_el = NEG_INF;
    st->last_logged_spike_t = NEG_INF;
    st->flatline_clear_el = NEG_INF;
    st->was_flatline = false;
    st->prev_last_event_el = NEG_INF;
    st->peak_mean_below_since_el = NEG_INF;
    st->frozen_wait_s = CONS_WAIT_S;
    baseline_init(&st->art_baseline);
    baseline_init(&st->med_baseline);
    for (int i = 0; i < NLOG_V; i++) { st->cons_buf_log[i].valid = false; st->tilt_log[i].valid = false; }
}

void c13stream_push(Combo13StreamState *st, const float *samples, int n) {
    for (int i = 0; i < n; i++) {
        streaming_tilt_push_sample(st, samples[i]);
#if DETECT_AT_NATIVE
        st->native_chunk_buf[NATIVE_CARRY_N + st->native_chunk_len] = samples[i];
        st->native_chunk_len++;
        if (st->native_chunk_len == NATIVE_CHUNK_N) {
            process_and_emit_chunk(st, st->native_chunk_buf, NATIVE_CARRY_N, NATIVE_CHUNK_N,
                                     FS_IN, ARTIFACT_PAD_SAMPLES_V, emit_native_cleaned_chunk);
            st->native_chunk_len = 0;
        }
#else
        float dec_sample;
        int got = streaming_decimate(st, &samples[i], 1, &dec_sample);
        if (got) {
            st->dec_chunk_buf[DEC_CARRY_N + st->dec_chunk_len] = dec_sample;
            st->dec_chunk_len++;
            if (st->dec_chunk_len == DEC_CHUNK_N) {
                process_and_emit_chunk(st, st->dec_chunk_buf, DEC_CARRY_N, DEC_CHUNK_N,
                                         DEC_HZ, ARTIFACT_PAD_SAMPLES_V, emit_dec_cleaned_chunk);
                st->dec_chunk_len = 0;
            }
        }
#endif
    }
}

static void log_push(Combo13StreamState *st, bool val_valid, float val, bool conf_valid, float conf,
                      bool tilt_valid, float tilt) {
    int i = st->log_head;
    st->cons_buf_log[i].valid = val_valid;
    st->cons_buf_log[i].val = val;
    st->cons_buf_log[i].conf_valid = conf_valid;
    st->cons_buf_log[i].conf = conf;
    st->tilt_log[i].valid = tilt_valid;
    st->tilt_log[i].val = tilt;
    st->log_head = (i + 1) % NLOG_V;
    if (st->log_count < NLOG_V) st->log_count++;
}

Combo13StreamTick c13stream_tick(Combo13StreamState *st) {
    Combo13StreamTick out; memset(&out, 0, sizeof(out));
    float el = st->el;
    out.t = el;

    int hist_len = st->dec_sample_count;

    /* -- onset/sig_level: broad_env over a SHORT recent window (not the whole ring) -- only the
     * trailing ~1.5s RMS is ever used, so gathering/filtering the full 8s history here (as
     * combo13_opt's non-consolidated chain did) would be pure waste. Reasoned simplification,
     * not yet corpus-validated in isolation. */
    float sig_level = 0.0f;
    if (hist_len > 0) {
        int sig_win = (int)(2.0f * DEC_HZ); if (sig_win > hist_len) sig_win = hist_len;
        int m = broad_env(st, sig_win, st->scratch_feat_a);
        int want = (int)lroundf(1.5f * FFS); int n_trail = m; if (want < n_trail) n_trail = want;
        if (n_trail > 0) {
            float sumsq = 0.0f;
            for (int i = m - n_trail; i < m; i++) sumsq += st->scratch_feat_a[i] * st->scratch_feat_a[i];
            sig_level = sqrtf(sumsq / (float)n_trail);
        }
    }

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

    float last_event_el = st->last_onset_el > st->last_spike_el ? st->last_onset_el : st->last_spike_el;
    if (st->flatline_clear_el > last_event_el) last_event_el = st->flatline_clear_el;
    float since_event = el - last_event_el;

    float adaptive_tau = since_event;
    if (adaptive_tau < DECAY_ADAPTIVE_TAU_MIN) adaptive_tau = DECAY_ADAPTIVE_TAU_MIN;
    if (adaptive_tau > DECAY_ADAPTIVE_TAU_MAX) adaptive_tau = DECAY_ADAPTIVE_TAU_MAX;

    /* r_adaptive preview -- sizes the wait timer only, never displayed. Uses whatever history is
     * available so far (up to the full ring), same intent as combo13_opt's own version. */
    bool r_adaptive_valid = false; float r_adaptive_bpm = 0.0f;
    if (hist_len >= 3 * (int)DEC_HZ) {
        float S[S_LEN] = {0};
        float tau_frames = adaptive_tau * FFS;
        for (int b = 0; b < 3; b++) {
            /* identical math to feat_norm_clip(st, hist_len, b, FEAT_CLIP, ...) -- called directly
             * now that it no longer needs a scratch_work gather+filter+envelope pass first
             * (2026-08-05, RAM/clock pass 4), removing what used to be a hand-duplicated copy of
             * that function's body. */
            int m = feat_norm_clip(st, hist_len, b, FEAT_CLIP, st->scratch_feat_a);
            float w[FEAT_MAX_V];
            for (int i = 0; i < m; i++) w[i] = expf(-(float)(m - 1 - i) / tau_frames);
            float r0 = 0.0f, r0w = 0.0f;
            for (int i = 0; i < m; i++) { r0 += w[i] * st->scratch_feat_a[i] * st->scratch_feat_a[i]; r0w += w[i]; }
            r0 = r0 / (r0w != 0.0f ? r0w : 1.0f);
            if (m > HI_LAG) {
                for (int l = LO_LAG; l < HI_LAG; l++) {
                    int nn = m - l;
                    float s = 0.0f, sw = 0.0f;
                    for (int i = 0; i < nn; i++) { s += w[i] * st->scratch_feat_a[i] * st->scratch_feat_a[i + l]; sw += w[i]; }
                    S[l - LO_LAG] += (s / (sw != 0.0f ? sw : 1.0f)) / (r0 + 1e-9f);
                }
            }
        }
        float Sw[S_LEN];
        for (int l = LO_LAG; l < HI_LAG; l++) Sw[l - LO_LAG] = S[l - LO_LAG] / powf((float)l, LAG_POW);
        float swmin = Sw[0]; for (int i = 1; i < S_LEN; i++) if (Sw[i] < swmin) swmin = Sw[i];
        for (int i = 0; i < S_LEN; i++) Sw[i] -= swmin;
        int p1 = -1; float bv = -1e30f;
        for (int i = 1; i < S_LEN - 1; i++)
            if (Sw[i] >= Sw[i - 1] && Sw[i] > Sw[i + 1] && Sw[i] > bv) { bv = Sw[i]; p1 = i; }
        r_adaptive_bpm = (p1 < 0) ? 60.0f / ((float)LO_LAG / FFS) : 60.0f / (((float)LO_LAG + p1) / FFS);
        r_adaptive_valid = true;
    }

    float win_sec = SINGLE_WIN_MIN + (since_event - st->frozen_wait_s > 0 ? since_event - st->frozen_wait_s : 0.0f);
    if (win_sec < SINGLE_WIN_MIN) win_sec = SINGLE_WIN_MIN;
    if (win_sec > SINGLE_WIN_MAX) win_sec = SINGLE_WIN_MAX;
    int win_nsamp = (int)lroundf(win_sec * DEC_HZ);
#ifdef STREAM_DEBUG
    fprintf(stderr, "DBG t=%.2f win_nsamp=%d since_event=%.2f frozen_wait_s=%.2f last_onset_el=%.2f last_spike_el=%.2f med_ema=%.3f art_ema=%.3f\n",
            el, win_nsamp, since_event, st->frozen_wait_s, st->last_onset_el, st->last_spike_el,
            st->med_baseline.ema, st->art_baseline.ema);
#endif

    EstimateResult win_r; win_r.valid = false;
    if (hist_len >= win_nsamp && win_nsamp > 0) {
        win_r = estimate_sumenv_robust(st, win_nsamp, ROBUST_TRIM_FRAC, FEAT_CLIP);
    }
    EstimateResult win_teal = estimate_detrend_from_mw(&win_r, TEAL_PUSH_HALF_WIDTH);

    /* peak-mean flatline detector -- same design as combo13_opt's 2026-08-04 fix */
    bool peak_mean_valid = false;
    float peak_minus_mean = 0.0f;
    if (hist_len >= win_nsamp && win_nsamp > 0) {
        int m = broad_env(st, win_nsamp, st->scratch_feat_b);
        if (m > 0) {
            float sum = 0.0f;
            for (int i = 0; i < m; i++) sum += st->scratch_feat_b[i];
            float mean_abs = sum / (float)m;
            int top_n = (int)lroundf((float)m * PEAK_MEAN_TOP_PCT); if (top_n < 1) top_n = 1;
            int boundary = m - top_n;
            quickselectf(st->scratch_feat_b, m, boundary);
            float peak_sum = 0.0f;
            for (int i = boundary; i < m; i++) peak_sum += st->scratch_feat_b[i];
            float peak_level = peak_sum / (float)top_n;
            peak_minus_mean = peak_level - mean_abs;
            peak_mean_valid = true;
#ifdef STREAM_DEBUG
            fprintf(stderr, "DBG t=%.2f sig_level=%.6f hist_len=%d win_nsamp=%d peak_minus_mean=%.4f thresh=%.4f\n",
                    el, sig_level, hist_len, win_nsamp, peak_minus_mean, PEAK_MEAN_DIFF_THRESH);
#endif
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

    if (is_flatline) {
        for (int i = 0; i < NLOG_V; i++) st->cons_buf_log[i].valid = false;
    }
    /* is_flatline clearing has no sustain requirement (unlike setting it) -- a single tick's
     * good-looking peak-mean reading ends it, and brief few-second dips (breathing, momentary
     * weak contact) trip is_flatline fairly often even in otherwise-normal recordings -- corpus-
     * confirmed: treating EVERY such blip as a fresh first-contact-equivalent event was a severe
     * regression (5-10s bucket correct% collapsed to 0). Only flatline episodes that persisted at
     * least FLATLINE_FULL_RESET_MIN_S are treated as a genuine disconnect/reconnect (this is the
     * scenario a physical device removal actually produces -- many seconds, not one bad tick), and
     * only those fold into last_event_el for the full since_event/frozen_wait_s/ramp reset; shorter
     * blips still clear cons_buf_log above but don't force the expensive full reset. One-tick lag
     * vs. the since_event/win_sec computed earlier this tick, same as onset/spike events already
     * accept. */
    if (is_flatline && !st->was_flatline) st->flatline_became_true_el = el;
    if (st->was_flatline && !is_flatline) {
        float flatline_dur = el - st->flatline_became_true_el;
        /* st->ever_displayed guards against the initial cold-start bootstrap: every stream begins
         * flatline (no history yet) for several seconds by design, already fully covered by the
         * ordinary first-contact wait/ramp -- without this guard that bootstrap's own end looked
         * exactly like a "genuine reconnect" and chained a SECOND full wait right after the first,
         * corpus-confirmed as a severe regression (0-10s bucket collapsed to 100% no-display).
         * 2026-08-05: ever_displayed alone isn't enough -- if establishing contact takes so long
         * that it spans MULTIPLE flatline episodes before the first-ever valid display (poor/
         * intermittent contact, not a clean single bootstrap), ever_displayed stays false through
         * all of them, so a later episode's clear -- happening well after the original wait window
         * (`since_event`/`frozen_wait_s`, computed earlier this tick from the stale first event) has
         * already expired -- got the SAME bootstrap exemption as the true cold start, leaving zero
         * active wait/ramp protection by the time real contact finally lands (real recording from
         * Daniel: a ~150-158bpm harmonic lock displayed completely unguarded on true first contact,
         * true rate ~52bpm). Fixed by also treating it as fresh once the original wait has already
         * elapsed (`since_event>=frozen_wait_s`), regardless of ever_displayed -- this still exempts
         * the ordinary bootstrap (whose own end is normally well inside frozen_wait_s on real files)
         * while catching this slow-contact case. */
        bool past_original_wait = since_event >= st->frozen_wait_s;
        if ((st->ever_displayed || past_original_wait) && flatline_dur >= FLATLINE_FULL_RESET_MIN_S) st->flatline_clear_el = el;
    }
    st->was_flatline = is_flatline;

    bool is_new_event = (last_event_el != st->prev_last_event_el);
    if (is_new_event) {
        st->prev_last_event_el = last_event_el;
        st->frozen_wait_s = CONS_WAIT_S + (r_adaptive_valid ? CONS_WAIT_EXTRA_S_PER_BPM * fabsf(r_adaptive_bpm - 60.0f) : 0.0f);
    }

    /* tilt trace read (unchanged from combo13_opt's task-7 design, native-rate-based throughout) */
    bool tilt_valid = false; float tilt_bpm = 0.0f;
    {
        long n_frames = st->next_frame_idx;
        if (n_frames > TILT_MAX_FRAMES) n_frames = TILT_MAX_FRAMES;
        if (n_frames > TILT_AC_HI) {
            long first_idx = st->next_frame_idx - n_frames;
            float mean = 0.0f;
            for (long i = 0; i < n_frames; i++) { st->scratch_tilts[i] = st->tilt_ring[(first_idx + i) % TILT_MAX_FRAMES]; mean += st->scratch_tilts[i]; }
            mean /= (float)n_frames;
            float c[TILT_MAX_FRAMES];
            for (long i = 0; i < n_frames; i++) c[i] = st->scratch_tilts[i] - mean;
            float S[TILT_AC_LEN] = {0};
            add_autocorr_plain(S, c, (int)n_frames, TILT_AC_LO, TILT_AC_HI);
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
        int win_m = win_nsamp / FRAME_DS;
        harmonic_confirmed = check_harmonic_confirmed(st, hist_len, win_nsamp, raw_cand_valid, raw_cand_bpm,
                                                        st->scratch_sumenv, win_m, ROBUST_TRIM_FRAC);
        if (harmonic_confirmed == 0) raw_cand_valid = false;
    }

    bool is_jump = false;
    if (raw_cand_valid && st->last_accepted_teal_bpm_valid) {
        float ratio = raw_cand_bpm / st->last_accepted_teal_bpm;
        is_jump = (ratio >= TEAL_JUMP_RATIO) || (ratio <= 1.0f / TEAL_JUMP_RATIO);
    }
    if (suppress_circle) st->teal_jump_streak = 0;
    else if (is_jump) st->teal_jump_streak++;
    else st->teal_jump_streak = 0;
    bool jump_unconfirmed = is_jump && (st->teal_jump_streak < TEAL_JUMP_CONFIRM_TICKS);

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
    float time_since_wait_end = since_event - st->frozen_wait_s;
    bool bpm_delay_unconfirmed = raw_cand_valid &&
        (st->bpm_delay_streak < required_teal_delay_ticks(raw_cand_bpm, time_since_wait_end));

    bool pushed_valid = raw_cand_valid && !suppress_circle && !jump_unconfirmed && !bpm_delay_unconfirmed;
    float pushed_bpm = pushed_valid ? raw_cand_bpm : 0.0f;
    if (pushed_valid) { st->last_accepted_teal_bpm = pushed_bpm; st->last_accepted_teal_bpm_valid = true; }

    bool pushed_conf_valid = pushed_valid && win_teal.valid;
    float pushed_conf = pushed_conf_valid ? win_teal.conf : 0.0f;

    log_push(st, pushed_valid, pushed_bpm, pushed_conf_valid, pushed_conf,
              (!suppress_circle) && tilt_valid, tilt_bpm);

    float vals[WEIGHTED_MED_TRAILING_N], weights[WEIGHTED_MED_TRAILING_N];
    int count = 0;
    int avail = st->log_count;
    for (int back = 1; back <= avail && count < WEIGHTED_MED_TRAILING_N; back++) {
        int idx = (st->log_head - back + NLOG_V) % NLOG_V;
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

    if (out.bpm_valid) st->ever_displayed = true;

    st->el += TICK_S;
    return out;
}
