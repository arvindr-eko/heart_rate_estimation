/* combo13_stream.c -- combo13 streaming heart-rate front end, revision 0.16.
 *
 * Signal path, raw audio to displayed bpm (see README.md for the full picture):
 *   1. Streaming decimation (4000Hz -> 333.333Hz) + chunked artifact removal/declick/spike detection.
 *   2. A persistent 3-band envelope filter bank, updated per decimated sample, feeding every
 *      downstream feature extractor from a small ring buffer instead of a raw sample history.
 *   3. Autocorrelation of the summed, normalized envelope over a growing analysis window, detrended
 *      to suppress the window's own slow trend and pick a candidate periodicity.
 *   4. Morphology-template scoring re-ranks that candidate's local maxima against known per-bpm
 *      autocorrelation shapes, so a smaller-but-more-plausible peak can beat a taller harmonic.
 *   5. A harmonic veto looks for corroborating structure near 2x the candidate's own lag and drops
 *      the candidate if none is found.
 *   6. A settling wait, jump-confirmation, and bpm-dependent confirmation delay gate how quickly a
 *      new candidate is trusted after an event (signal onset, spike, flatline recovery).
 *   7. A weighted-median combine over the trailing accepted candidates produces the displayed bpm,
 *      weighting each by a bpm-plausibility shape, agreement with an independent envelope-flux
 *      estimate, and its own morphology-template score.
 *
 * *** Ticks run at TICK_S=0.5s of elapsed audio (revision 0.14; was 0.25s through revision 0.13) ***
 * -- hr_push_data() may be called with any number of samples at any cadence in between, but
 * hr_calculate() must be called once per TICK_S seconds of pushed audio. Query c13stream_tick_s()
 * rather than hardcoding either value -- see hr.h and INTEGRATION_NOTES.md. This is a
 * BREAKING change in calling cadence from every prior revision and requires a firmware-side update.
 */

#include "hr.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "autocorr_templates.h"
#include "autocorr_templates_detrended.h"
#include "combo13_stream_internal.h"
#include "debug_eko.h"

/* ================================================================================================
 * CONSTANTS
 * ================================================================================================ */
#define FS_IN  4000.0f
#define TICK_S 0.5f
#define DEC_HZ (FS_IN / (float)DEC_STRIDE) /* decimated sample rate: 333.333Hz */
#define FFS    (DEC_HZ / (float)FRAME_DS)  /* envelope frame rate: 66.667Hz */

/* Autocorrelation lag weighting exponent: S[lag] is divided by lag^LAG_POW before peak-picking, a
 * mild bias against very short lags (which have more independent product terms and so tend to look
 * artificially strong). */
static const float LAG_POW = 0.2f;

static const float FLUX_CLIP = 4.5f; /* z-score clip for the envelope-flux feature, matches FEAT_CLIP */

/* -- onset debounce (see hr_calculate() for why this no longer gates event timing) -- */
static const float SIGNAL_ONSET_THRESH = 0.00021f;
static const int ONSET_DEBOUNCE_TICKS = 2; /* 1.0s intended debounce duration at TICK_S=0.5s */

/* -- streaming artifact removal / declick / spike detection -- all three operate relative to a
 * streaming baseline (median or a robust percentile) rather than a fixed absolute level, so they
 * scale with the signal's own amplitude. -- */
static const float SPIKE_DETECT_MULT = 15.0f;
static const float TRANSIENT_LIMIT_MULT = 4.29f;
static const float ARTIFACT_DETECT_MULT = 16.96f;
static const float ARTIFACT_EXTEND_MULT = 8.02f;
static const float ARTIFACT_BASELINE_PCTL = 0.4231f;
static const int ARTIFACT_PAD_SAMPLES = 3; /* ~9ms at 333.333Hz: widens an excised artifact
                                               region so interpolation doesn't leave a boundary
                                               click */
#define BASELINE_TAU_S 1.0f                /* EMA time constant for both streaming baselines */

/* -- main-candidate autocorrelation -- */
static const float ROBUST_TRIM_FRAC = 0.015f; /* trimmed-mean fraction discarded from each tail
                                                  when averaging lagged products, for robustness
                                                  to a minority of contaminated samples */
static const float FEAT_CLIP = 4.5f;          /* z-score clip applied to each normalized band */
static const float SINGLE_WIN_MIN = 4.0f;     /* analysis window bounds, seconds */
static const float SINGLE_WIN_MAX = 8.0f;
static const int TEAL_PUSH_HALF_WIDTH = 36; /* detrending moving-average half-width, lag bins */

/* -- settling wait after an event (signal onset, spike, flatline recovery) -- */
static const float CONS_WAIT_S = 6.0f;
static const float CONS_WAIT_EXTRA_S_PER_BPM = 0.020f; /* extra wait scaled by how far the
                                                            adaptive-tau preview sits from 60bpm */
/* Early release: once the raw candidate has agreed with the independent flux estimate for
 * EARLY_RELEASE_MIN_TICKS consecutive ticks, up to EARLY_RELEASE_MAX_SAVE_S of the settling wait
 * can be skipped. The _FIRST variants apply only to a session's true first-ever display (see
 * ever_displayed) -- every later reconnect uses the plain values. */
static const int EARLY_RELEASE_MIN_TICKS = 1;
static const float EARLY_RELEASE_MAX_SAVE_S = 6.0f;
static const int EARLY_RELEASE_MIN_TICKS_FIRST = 1;
static const float EARLY_RELEASE_MAX_SAVE_S_FIRST = 6.0f;

/* -- combine stage -- */
/* enum, not `static const int`: in C the latter is not an integer constant expression, which
 * quietly turns every array sized by it into a (bounded) VLA -- flagged by -Werror=vla and the
 * cause of the `dynamic,bounded` markers in -fstack-usage output. Behavior-identical. */
enum {
  WEIGHTED_MED_TRAILING_N = 4
}; /* trailing pushed candidates the combine looks at --
       2.0s intended trailing window at TICK_S=0.5s */
static const float AGREEMENT_BPM_TOL = 11.10f; /* max |flux - candidate| still counted as agreement */
static const float AGREE_BONUS = 6.5f;         /* combine-weight multiplier when they agree */
/* bpm_favor_center()'s piecewise-linear "tent" shape: peaks at TENT_PEAK_BPM/TENT_PEAK_H, sloping
 * down to TENT_LO_H at 40bpm and TENT_HI_H at 200bpm -- a mild prior toward physiologically typical
 * resting heart rates, multiplied into every candidate's combine weight. */
static const float TENT_PEAK_BPM = 55.9f;
static const float TENT_PEAK_H = 2.29f;
static const float TENT_LO_H = 0.56f;
static const float TENT_HI_H = 0.81f;

/* -- jump confirmation: a candidate that jumps far from the last accepted value must repeat for
 * several ticks before being trusted, guarding against a single noisy tick knocking the display to
 * a wrong octave/harmonic. -- */
static const float TEAL_JUMP_RATIO = 1.2f;
static const int TEAL_JUMP_CONFIRM_TICKS = 2; /* 1.0s intended confirm duration at TICK_S=0.5s */
/* A jump whose ratio lands within tolerance of an exact harmonic (2x/0.5x/3x/(1/3)x) would in
 * principle deserve extra scrutiny (that ratio is the classic octave-confusion signature) -- but a
 * ratio alone can't distinguish "escaping a wrong harmonic toward the true rate" from "locking onto
 * a wrong harmonic away from it," so the harmonic-confirm requirement is kept equal to the plain
 * one (a deliberate no-op) rather than penalizing corrective jumps as often as bad ones. */
static const float TEAL_JUMP_HARMONIC_TOL = 0.05f;
static const int TEAL_JUMP_HARMONIC_CONFIRM_TICKS = TEAL_JUMP_CONFIRM_TICKS;

/* -- bpm-dependent confirmation delay (required_teal_delay_ticks) -- a candidate must repeat near
 * its own value for a number of ticks that grows slightly with bpm (faster rates get one wrong tick
 * more easily) and tapers down over RAMP_DELAY_TAPER_S after a settling wait lifts. The _FIRST
 * variants apply only to a session's true first-ever display, same scoping as EARLY_RELEASE.
 * TEAL_BPM_DELAY_SLOPE(_FIRST)/RAMP_DELAY_EXTRA_TICKS(_FIRST) are all expressed in extra-ticks-per-
 * bpm-over-60 or extra-ticks, so each is halved from its TICK_S=0.25s value to hold its real-world
 * duration in seconds constant now that TICK_S=0.5s. RAMP_DELAY_TAPER_S(_FIRST) are already in
 * seconds and are unchanged. -- */
static const float TEAL_BPM_DELAY_SLOPE = 0.001194252054314898f;
static const float TEAL_BPM_DELAY_SLOPE_FIRST = 0.015f;
static const float RAMP_DELAY_TAPER_S = 15.0f;
static const float RAMP_DELAY_EXTRA_TICKS = 2.0f;
static const float RAMP_DELAY_TAPER_S_FIRST = 15.0f;
static const float RAMP_DELAY_EXTRA_TICKS_FIRST = 3.0f;

/* -- morphology template matching (see MORPHOLOGY TEMPLATE MATCHING section below) -- */
static const float TEMPLATE_MATCH_MIN_BPM = 40.0f;
static const float TEMPLATE_MATCH_MAX_BPM = 90.0f;
static const float TEMPLATE_WEIGHT_LO = 0.1f; /* combine-weight multiplier at template corr 0.30 */
static const float TEMPLATE_WEIGHT_HI = 2.0f; /* combine-weight multiplier at template corr 0.70+ */
enum { TEMPLATE_MATCH_GATED_V2_N_PEAKS = 6 }; /* enum, not static const int: see WEIGHTED_MED_TRAILING_N */
static const float TEMPLATE_MATCH_OVERRIDE_MARGIN_V2 = 0.3f;
static const float TEMPLATE_MATCH_CONFIDENCE_FLOOR_V2 = 0.30f;
static const float TEMPLATE_UNSCORED_BELOW_BPM = 40.0f;
static const float TEMPLATE_UNSCORED_SCORE = 0.30f; /* == TEMPLATE_MATCH_CONFIDENCE_FLOOR_V2 */

/* -- flatline (device-off) detection -- gated primarily on real disconnect/reconnect timing since
 * this decides whether the device shows a number or "--", not just a corpus accuracy metric. -- */
static const float PEAK_MEAN_TOP_PCT = 0.05f;
static const float PEAK_MEAN_DIFF_THRESH = (0.002f * 32768.0f); /* int16-scale, see broad_env */
static const float PEAK_MEAN_SUSTAIN_S = 1.0f;
static const float FLATLINE_FULL_RESET_MIN_S = 1.5f; /* minimum flatline duration counted as a
                                                          genuine disconnect vs. a brief dip */

/* -- harmonic veto: requires corroborating autocorrelation structure near HARMONIC_VETO_LO_FRAC..
 * HARMONIC_VETO_HI_FRAC times the candidate's own lag (roughly 2x, i.e. half the candidate's bpm)
 * before confirming it. Converts a would-be wrong-bpm display into a blank one rather than a
 * confidently wrong number -- a materially better failure mode for a medical display. -- */
static const float HARMONIC_VETO_HI_EXT_MULT = 2.1813774614710146f;
static const float HARMONIC_VETO_HEIGHT_FRAC = 0.05895565949526575f;
static const float HARMONIC_VETO_LO_FRAC = 1.93f;
static const float HARMONIC_VETO_HI_FRAC = 2.07f;

#define DECAY_ADAPTIVE_TAU_MIN 1.0f
#define DECAY_ADAPTIVE_TAU_MAX 8.0f
static const float NEG_INF = -1e30f;

/* ------------------------------------------------------------------------------------------------
 * Fixed filter coefficients for the shipped 333.333Hz decimated rate (revision 0.14; DEC_STRIDE
 * 10->12, was 400Hz through revision 0.13): anti-alias lowpass (4000Hz -> 333.333Hz, cutoff
 * 146.667Hz, normalized to the NATIVE Nyquist since this filter runs at native rate one sample at a
 * time, before the stride-keep -- see streaming_decimate()) and a 3-band bandpass filterbank
 * (30-50/50-80/80-130Hz, normalized to the DECIMATED rate), both 4th-order, generated via
 * scipy.signal.butter() and confirmed stable (max pole magnitude 0.916, inside the unit circle).
 * ------------------------------------------------------------------------------------------------ */
static const float AA_B[5] = {0.00013253330021777536f, 0.00053013320087110145f, 0.00079519980130665223f,
                              0.00053013320087110145f, 0.00013253330021777536f};
static const float AA_A[5] = {1.0f, -3.3986115487748272f, 4.370134786925246f, -2.516248182601176f,
                              0.54684547725424182f};
static const float BAND_B[3][5] = {
    {0.027859766117136038f, 0, -0.055719532234272076f, 0, 0.027859766117136038f},
    {0.056448462260736423f, 0, -0.11289692452147285f, 0, 0.056448462260736423f},
    {0.13110643991662591f, 0, -0.26221287983325181f, 0, 0.13110643991662591f},
};
static const float BAND_A[3][5] = {
    {1.0f, -2.5792009483134946f, 3.1620434520162002f, -1.9660957308736584f, 0.58691950806118998f},
    {1.0f, -1.1374767806559709f, 1.5575095992873316f, -0.74977252963221197f, 0.45044543005604087f},
    {1.0f, 1.2247706907067881f, 1.1491130623077006f, 0.57598015013947657f, 0.27221493792500717f},
};
static const float ENV_A = 0.7975628241529755f; /* single-pole envelope follower decay */

/* ================================================================================================
 * DSP PRIMITIVES
 * ================================================================================================ */

/* Direct-form-II transposed biquad-cascade (order 4) step -- safe to run in place on the same
 * buffer it reads from, which every caller in this file relies on. */
static inline float iir4_step(const float b[5], const float a[5], float x, float z[4])
{
  float y = b[0] * x + z[0];
  z[0] = b[1] * x - a[1] * y + z[1];
  z[1] = b[2] * x - a[2] * y + z[2];
  z[2] = b[3] * x - a[3] * y + z[3];
  z[3] = b[4] * x - a[4] * y;
  return y;
}

static inline void swapf(float *a, float *b)
{
  float t = *a;
  *a = *b;
  *b = t;
}

static inline int median3_idx(const float *a, int i, int j, int k)
{
  float ai = a[i];
  float aj = a[j];
  float ak = a[k];
  if ((ai <= aj && aj <= ak) || (ak <= aj && aj <= ai)) {
    return j;
  }
  if ((aj <= ai && ai <= ak) || (ak <= ai && ai <= aj)) {
    return i;
  }
  return k;
}

/* Median-of-three quickselect: partitions `a[lo0..hi0]` in place until index `k` holds the value
 * that would be there in sorted order. Used throughout this file for exact percentiles/medians/
 * trimmed means over small chunks, instead of a full sort. */
static inline void quickselectf_range(float *a, int lo0, int hi0, int k)
{
  int lo = lo0;
  int hi = hi0;
  while (lo < hi) {
    int mid = lo + (hi - lo) / 2;
    float pivot = a[median3_idx(a, lo, mid, hi)];
    /* Three-way (Dutch national flag) partition: [lo,lt) < pivot, [lt,gt] == pivot, (gt,hi] > pivot.
     * A two-way partition degenerates to O(n^2) when many values are equal, which is exactly what a
     * silent/clipped signal produces (all-zero or all-clipped feature products) -- on the target that
     * turned an ordinary tick into a multi-second one and starved the watchdog. */
    int lt = lo;
    int i = lo;
    int gt = hi;
    while (i <= gt) {
      float v = a[i];
      if (v < pivot) {
        swapf(&a[lt], &a[i]);
        lt++;
        i++;
      } else if (v > pivot) {
        swapf(&a[i], &a[gt]);
        gt--;
      } else {
        i++;
      }
    }
    if (k < lt) {
      hi = lt - 1;
    } else if (k > gt) {
      lo = gt + 1;
    } else {
      return;
    }
  }
}

static inline void quickselectf(float *a, int n, int k)
{
  quickselectf_range(a, 0, n - 1, k);
}

/* ================================================================================================
 * STREAMING BASELINE & ARTIFACT REMOVAL -- a compact replacement for "sort/scan the whole analysis
 * window every tick": a chunk-sized EMA-of-percentile baseline, plus detect/extend/interpolate over
 * a small [carry+chunk] buffer using that already-established baseline for its threshold.
 * ================================================================================================ */
static inline void baseline_init(StreamBaseline_t *sb)
{
  sb->ema = 0.0f;
  sb->initialized = false;
  sb->warm_sum = 0.0f;
  sb->warm_dur = 0.0f;
}

#define BASELINE_WARMUP_S 1.0f

/* Computes the exact percentile of |chunk| (via quickselect), then EMA-blends it into the
 * persistent estimate. `scratch` must hold >= n floats. `alpha` is passed in (rather than derived
 * here from chunk_duration_s/BASELINE_TAU_S) since both baselines that share a chunk need the exact
 * same value -- computed once by the caller instead of twice. */
static float baseline_update(StreamBaseline_t *sb, const float *chunk, int n, float pctl, float chunk_duration_s,
                             float alpha, float *scratch)
{
  for (int i = 0; i < n; i++) {
    scratch[i] = fabsf(chunk[i]);
  }
  int idx = (int)(n * pctl);
  if (idx > n - 1) {
    idx = n - 1;
  }
  if (idx < 0) {
    idx = 0;
  }
  quickselectf(scratch, n, idx);
  float chunk_val = scratch[idx];
  if (!sb->initialized) {
    /* For the first BASELINE_WARMUP_S seconds, use a duration-weighted running mean instead of
       seeding the EMA from a single ~100ms chunk's percentile, which is noisy enough on its own
       to leave the artifact/decline threshold under-converged right after signal onset. */
    sb->warm_sum += chunk_val * chunk_duration_s;
    sb->warm_dur += chunk_duration_s;
    sb->ema = sb->warm_sum / sb->warm_dur;
    if (sb->warm_dur >= BASELINE_WARMUP_S) {
      sb->initialized = true;
    }
  } else {
    sb->ema = alpha * sb->ema + (1.0f - alpha) * chunk_val;
  }
  return sb->ema;
}

static inline void interp_over_mask(float *dst, const float *src, const bool *mask, int n)
{
  int i = 0;
  while (i < n) {
    if (!mask[i]) {
      i++;
      continue;
    }
    int j = i;
    while (j < n && mask[j]) {
      j++;
    }
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

/* Detect samples exceeding `detect_mult*scale`, extend the flagged run outward while still above
 * `extend_mult*scale`, optionally pad by `pad_samples` on each side, then linearly interpolate the
 * whole flagged region from its untouched neighbors. Modifies `buf` in place. Uses the
 * ALREADY-ESTABLISHED baseline (many chunks old) for its threshold rather than deriving one from
 * this chunk, so the threshold isn't itself contaminated by the artifact it's trying to detect. */
static void stream_detect_extend_interp(float *buf, int n, float scale, float detect_mult, float extend_mult,
                                        int pad_samples, bool *scratch_grown, float *scratch_absx,
                                        bool padded[CHUNK_TOTAL_N], float orig[CHUNK_TOTAL_N])
{
  float lim = detect_mult * scale;
  float extend_lim = extend_mult * scale;
  for (int i = 0; i < n; i++) {
    scratch_absx[i] = fabsf(buf[i]);
    scratch_grown[i] = false;
  }

  bool any = false;
  int i = 0;
  while (i < n) {
    if (scratch_grown[i] || scratch_absx[i] <= lim) {
      i++;
      continue;
    }
    int lo = i;
    while (lo > 0 && scratch_absx[lo - 1] > extend_lim) {
      lo--;
    }
    int hi = i;
    while (hi < n - 1 && scratch_absx[hi + 1] > extend_lim) {
      hi++;
    }
    for (int k = lo; k <= hi; k++) {
      if (!scratch_grown[k]) {
        scratch_grown[k] = true;
        any = true;
      }
    }
    i = hi + 1;
  }
  if (!any) {
    return;
  }

  if (pad_samples > 0) {
    memcpy(padded, scratch_grown, n * sizeof(bool));
    for (int center = 0; center < n; center++) {
      if (!scratch_grown[center]) {
        continue;
      }
      int lo = center - pad_samples;
      if (lo < 0) {
        lo = 0;
      }
      int hi = center + pad_samples + 1;
      if (hi > n) {
        hi = n;
      }
      for (int k = lo; k < hi; k++) {
        padded[k] = true;
      }
    }
    memcpy(orig, buf, n * sizeof(float));
    interp_over_mask(buf, orig, padded, n);
  } else {
    memcpy(orig, buf, n * sizeof(float));
    interp_over_mask(buf, orig, scratch_grown, n);
  }
}

/* Declick clip (hard-limit to +-mult*scale), in place, using the streaming median baseline. */
static inline void declick_clip(float *buf, int n, float scale, float mult)
{
  float lim = mult * scale;
  for (int i = 0; i < n; i++) {
    float v = buf[i];
    buf[i] = v > lim ? lim : (v < -lim ? -lim : v);
  }
}

/* ================================================================================================
 * BAND FEATURE EXTRACTION -- a persistent 3-band bandpass filter + envelope runs once per new
 * decimated sample as it arrives; every FRAME_DS-th sample, the current envelope values are pushed
 * into a ring buffer at frame rate. Every feature extractor below just gathers trailing frames from
 * these rings -- no raw filtering is repeated per tick.
 * ================================================================================================ */
static void stream_dec_samples(hr_state_t *st, const float *samples, int n)
{
  for (int i = 0; i < n; i++) {
    if (st->dec_sample_count < DEC_MAX) {
      st->dec_sample_count++;
    }

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
      st->band_env_ring_head = (st->band_env_ring_head + 1) % FEAT_MAX;
      if (st->band_env_ring_len < FEAT_MAX) {
        st->band_env_ring_len++;
      }
    }
  }
}

static inline void band_env_gather_trailing(const hr_state_t *st, int band_idx, int m, float *out)
{
  int start = ((st->band_env_ring_head - m) % FEAT_MAX + FEAT_MAX) % FEAT_MAX;
  for (int i = 0; i < m; i++) {
    out[i] = (float)st->band_env_ring[band_idx][(start + i) % FEAT_MAX];
  }
}

/* Gathers the trailing `win_n/FRAME_DS` envelope frames for one band, z-scores and clips them. */
static int feat_norm_clip(hr_state_t *st, int win_n, int band_idx, float clip, float *out)
{
  int m = win_n / FRAME_DS;
  band_env_gather_trailing(st, band_idx, m, out);
  if (m == 0) {
    return 0;
  }
  float sum = 0.0f;
  for (int i = 0; i < m; i++) {
    sum += out[i];
  }
  float mean = sum / (float)m;
  float var = 0.0f;
  for (int i = 0; i < m; i++) {
    float d = out[i] - mean;
    var += d * d;
  }
  float sd = sqrtf(var / (float)m);
  if (sd == 0.0f) {
    sd = 1.0f;
  }
  for (int i = 0; i < m; i++) {
    float z = (out[i] - mean) / sd;
    out[i] = z > clip ? clip : (z < -clip ? -clip : z);
  }
  return m;
}

/* Sum of the 3 bands' z-scored+clipped envelopes -- the feature the main candidate's autocorrelation
 * runs on. Accumulates one band at a time into the caller's `out[]` so only one FEAT_MAX-sized
 * scratch buffer is ever needed for the other two bands, not two live at once. */
static int sum_env_norm(hr_state_t *st, int win_n, float clip, float *out)
{
  feat_norm_clip(st, win_n, 2, clip, out);
  int m = feat_norm_clip(st, win_n, 0, clip, st->scratch_shared);
  for (int i = 0; i < m; i++) {
    out[i] += st->scratch_shared[i];
  }
  feat_norm_clip(st, win_n, 1, clip, st->scratch_shared);
  for (int i = 0; i < m; i++) {
    out[i] += st->scratch_shared[i];
  }
  return m;
}

/* Unnormalized summed envelope -- used by the onset/signal-level check and the flatline detector,
 * both of which care about absolute signal level, not a z-scored shape. */
static int broad_env(hr_state_t *st, int win_n, float *out)
{
  int m = win_n / FRAME_DS;
  band_env_gather_trailing(st, 0, m, out);
  /* in-tree stack fix: was `float tmp[FEAT_MAX]` (2,576B stack local) -- see scratch_feat's
   * comment in combo13_stream_internal.h */
  float *tmp = st->scratch_feat;
  for (int b = 1; b < 3; b++) {
    band_env_gather_trailing(st, b, m, tmp);
    for (int i = 0; i < m; i++) {
      out[i] += tmp[i];
    }
  }
  return m;
}

/* Envelope flux: rectified first difference of the raw summed envelope, z-scored+clipped. An
 * independent second signal (derived from onset-strength rather than periodicity of level) that
 * the settling and combine stages check the main candidate against. out[0]'s diff is defined as 0. */
static int envelope_flux(hr_state_t *st, int win_n, float *out)
{
  int m = broad_env(st, win_n, out);
  if (m == 0) {
    return 0;
  }
  float prev = out[0];
  out[0] = 0.0f;
  for (int i = 1; i < m; i++) {
    float cur = out[i];
    float d = cur - prev;
    out[i] = fabsf(d); /* full-wave rectified: symmetric to both S1-rise and S2-rise energy */
    prev = cur;
  }
  float sum = 0.0f;
  for (int i = 0; i < m; i++) {
    sum += out[i];
  }
  float mean = sum / (float)m;
  float var = 0.0f;
  for (int i = 0; i < m; i++) {
    float d = out[i] - mean;
    var += d * d;
  }
  float sd = sqrtf(var / (float)m);
  if (sd == 0.0f) {
    sd = 1.0f;
  }
  for (int i = 0; i < m; i++) {
    float z = (out[i] - mean) / sd;
    out[i] = z > FLUX_CLIP ? FLUX_CLIP : (z < -FLUX_CLIP ? -FLUX_CLIP : z);
  }
  return m;
}

/* ================================================================================================
 * AUTOCORRELATION
 * ================================================================================================ */

/* Trimmed-mean-of-products autocorrelation: for each lag, discards the top/bottom `trim_frac` of
 * the per-sample products before averaging, for robustness to a minority of contaminated samples
 * that survived the earlier artifact removal. `S` is accumulated into (not overwritten), so
 * multiple calls can build up a shared curve; `st->scratch_shared` is reused as scratch here since
 * whatever called this has already fully consumed its own use of it by this point. */
static void add_autocorr_robust(hr_state_t *st, float *S, const float *f, int T, int lo, int hi,
                                float trim_frac)
{
  if (T <= hi) {
    return;
  }
  float r0 = 0.0f;
  for (int i = 0; i < T; i++) {
    r0 += f[i] * f[i];
  }
  r0 /= (float)T;
  float *prod = st->scratch_shared;
  for (int l = lo; l < hi; l++) {
    int n = T - l;
    for (int i = 0; i < n; i++) {
      prod[i] = f[i] * f[i + l];
    }
    int trim = (int)(n * trim_frac);
    int keep = n - 2 * trim;
    float mean_val = 0.0f;
    if (keep > 0) {
      quickselectf(prod, n, trim);
      if (keep > 1) {
        quickselectf_range(prod, trim, n - 1, n - 1 - trim);
      }
      float sum = 0.0f;
      for (int i = trim; i < n - trim; i++) {
        sum += prod[i];
      }
      mean_val = sum / (float)keep;
    }
    S[l - lo] += mean_val / (r0 + 1e-9f);
  }
}

/* Plain (untrimmed) autocorrelation -- used for the flux signal, where robustness to contamination
 * matters less than keeping flux computationally cheap and simple as a second opinion. */
static inline void add_autocorr_plain(float *S, const float *f, int T, int lo, int hi)
{
  if (T <= hi) {
    return;
  }
  float r0 = 0.0f;
  for (int i = 0; i < T; i++) {
    r0 += f[i] * f[i];
  }
  r0 /= (float)T;
  for (int l = lo; l < hi; l++) {
    int n = T - l;
    float s = 0.0f;
    for (int i = 0; i < n; i++) {
      s += f[i] * f[i + l];
    }
    S[l - lo] += (s / (float)T) / (r0 + 1e-9f);
  }
}

/* Main-candidate estimate: autocorrelates the summed normalized envelope over `win_n` decimated
 * samples, applies the lag^-LAG_POW weighting, min-subtracts (so the curve floors at 0), and picks
 * the tallest peak. Returns invalid if there isn't yet enough history for a 3-second minimum window. */
static EstimateResult_t *estimate_sumenv_robust(hr_state_t *st, int win_n, float trim_frac, float feat_clip,
                                                EstimateResult_t *r)
{
  r->valid = false;
  if (win_n < 3 * (int)DEC_HZ) {
    return r;
  }
  float *S = st->stack.tick.sum.S;
  memset(S, 0x0, S_LEN * sizeof(float));
  int m = sum_env_norm(st, win_n, feat_clip, st->scratch_sumenv);
  add_autocorr_robust(st, S, st->scratch_sumenv, m, LO_LAG, HI_LAG, trim_frac);

  for (int l = LO_LAG; l < HI_LAG; l++) {
    r->Sw[l - LO_LAG] = S[l - LO_LAG] * st->lag_pow_recip[l - LO_LAG];
  }
  float swmin = r->Sw[0];
  for (int i = 1; i < S_LEN; i++) {
    if (r->Sw[i] < swmin) {
      swmin = r->Sw[i];
    }
  }
  for (int i = 0; i < S_LEN; i++) {
    r->Sw[i] -= swmin;
  }

  int *pk = st->stack.tick.sum.pk;
  int npk = 0;
  for (int i = 1; i < FAST_LEN - 1; i++) {
    if (r->Sw[i] >= r->Sw[i - 1] && r->Sw[i] > r->Sw[i + 1]) {
      pk[npk++] = i;
    }
  }

  r->lo = LO_LAG;
  if (npk == 0) {
    r->bpm = 60.0f / ((float)LO_LAG / FFS);
    r->conf = 1.0f;
    r->valid = true;
    return r;
  }
  for (int i = 0; i < npk; i++) {
    int best = i;
    for (int j = i + 1; j < npk; j++) {
      if (r->Sw[pk[j]] > r->Sw[pk[best]]) {
        best = j;
      }
    }
    int tmp = pk[i];
    pk[i] = pk[best];
    pk[best] = tmp;
  }
  int p1 = pk[0];
  int bl = LO_LAG + p1;
  int p2 = -1;
  for (int k = 1; k < npk; k++) {
    if (abs(pk[k] - p1) >= 8) {
      p2 = pk[k];
      break;
    }
  }
  if (p2 < 0 && npk > 1) {
    p2 = pk[1];
  }
  float conf = (p2 < 0) ? 1.2f : r->Sw[p1] / (r->Sw[p2] + 1e-9f);
  r->bpm = 60.0f / ((float)bl / FFS);
  r->conf = conf;
  r->valid = true;
  return r;
}

static inline float moving_average_at(const float *csum, int n, int i, int half_width)
{
  int lo = i - half_width;
  if (lo < 0) {
    lo = 0;
  }
  int hi = i + half_width;
  if (hi > n - 1) {
    hi = n - 1;
  }
  return (csum[hi + 1] - csum[lo]) / (float)(hi - lo + 1);
}

/* Detrends win_r's curve by subtracting a local moving average (+-half_width lag bins), which
 * suppresses the curve's own slow trend so a genuine periodicity peak stands out from a broad
 * hump. Re-picks the tallest peak of the detrended curve as the candidate bpm. */
static EstimateResult_t *estimate_detrend_from_mw(hr_state_t *st, const EstimateResult_t *win_r,
                                                  int half_width, EstimateResult_t *out)
{
  out->valid = false;
  if (!win_r->valid) {
    return out;
  }
  float *csum = st->stack.tick.det.csum;
  const int n = S_LEN;
  const int nf = FAST_LEN;
  out->slow_valid = false;
  out->slow_bpm = 0.0f;
  out->slow_ratio = 0.0f;
  csum[0] = 0.0f;
  for (int i = 0; i < n; i++) {
    csum[i + 1] = csum[i] + win_r->Sw[i];
  }
  /* 0.15: first the FULL-width detrend (window clamps at n) -- used only to size/locate the slow-zone
   * (30-40 BPM) candidate against the tallest fast peak on the SAME curve. */
  for (int i = 0; i < n; i++) {
    out->Sw[i] = win_r->Sw[i] - moving_average_at(csum, n, i, half_width);
  }
  {
    float h_fast = 0.0f;
    bool any_fast = false;
    for (int i = 1; i < nf - 1; i++) {
      if (out->Sw[i] >= out->Sw[i - 1] && out->Sw[i] > out->Sw[i + 1] && (!any_fast || out->Sw[i] > h_fast)) {
        h_fast = out->Sw[i];
        any_fast = true;
      }
    }
    int ps = -1;
    float h_slow = 0.0f;
    for (int i = nf - 1; i < PEAK_SCAN_END; i++) {
      if (out->Sw[i] >= out->Sw[i - 1] && out->Sw[i] > out->Sw[i + 1] && (ps < 0 || out->Sw[i] > h_slow)) {
        h_slow = out->Sw[i];
        ps = i;
      }
    }
    if (ps >= 0 && h_slow > 1e-6f) {
      float denom = (any_fast && h_fast > 1e-6f) ? h_fast : 1e-6f;
      out->slow_valid = true;
      out->slow_bpm = 60.0f / (((float)win_r->lo + (float)ps) / FFS);
      out->slow_ratio = h_slow / denom;
    }
  }
  /* The FAST zone (bins [0,nf)) is then re-detrended over its OWN nf-wide window, exactly as in 0.14
   * (moving_average_at clamps at nf; csum's first nf+1 entries are the same prefix sums), so its curve --
   * and every pick made from it -- is bit-identical to 0.14. Bins >= nf keep the full-width values. */
  for (int i = 0; i < nf; i++) {
    out->Sw[i] = win_r->Sw[i] - moving_average_at(csum, nf, i, half_width);
  }

  int *cand = st->stack.tick.det.cand;
  int ncand = 0;
  for (int i = 1; i < nf - 1; i++) {
    if (out->Sw[i] >= out->Sw[i - 1] && out->Sw[i] > out->Sw[i + 1]) {
      cand[ncand++] = i;
    }
  }
  if (ncand == 0) {
    return out;
  }
  for (int i = 0; i < ncand; i++) {
    int best = i;
    for (int j = i + 1; j < ncand; j++) {
      if (out->Sw[cand[j]] > out->Sw[cand[best]]) {
        best = j;
      }
    }
    int tmp = cand[i];
    cand[i] = cand[best];
    cand[best] = tmp;
  }
  int p1 = cand[0];
  int p2 = -1;
  for (int k = 1; k < ncand; k++) {
    if (abs(cand[k] - p1) >= 8) {
      p2 = cand[k];
      break;
    }
  }
  float v1 = out->Sw[p1];
  if (v1 < 1e-6f) {
    v1 = 1e-6f;
  }
  float conf;
  if (p2 < 0) {
    conf = 1.2f;
  } else {
    float v2 = out->Sw[p2];
    if (v2 < 1e-6f) {
      v2 = 1e-6f;
    }
    conf = v1 / (v2 + 1e-9f);
  }
  out->bpm = 60.0f / (((float)win_r->lo + p1) / FFS);
  out->conf = conf;
  out->valid = true;
  return out;
}

/* Tallest local maximum of an S_LEN-length curve -- used to pick the flux peak (flux has no
 * template-matching or detrending step of its own beyond what's applied to its curve directly). */
static inline int peak_idx_tallest_local_max(const float *Sw, int n)
{
  if (n < 3) {
    return 0;
  }
  int best = 0;
  float bv = -1e30f;
  for (int i = 1; i < n - 1; i++) {
    if (Sw[i] >= Sw[i - 1] && Sw[i] > Sw[i + 1] && Sw[i] > bv) {
      bv = Sw[i];
      best = i;
    }
  }
  return best;
}

/* ================================================================================================
 * MORPHOLOGY TEMPLATE MATCHING -- known autocorrelation-curve shapes, baked from the corpus's own
 * 'correct' ticks and bucketed by ground-truth bpm, used two ways below: (a) to re-rank raw
 * candidate peaks by how closely each one's curve resembles the shape expected at that bpm (RAW
 * CANDIDATE SELECTION), and (b) as a soft weight on the combine stage's trust in a pushed candidate
 * (teal_size_factor(), in SMOOTHING / WEIGHTED-MEDIAN COMBINE below). kTemplateBpm/kTemplateCurve/
 * kTemplateCurveDetrended come from autocorr_templates.h/autocorr_templates_detrended.h (baked
 * lookup tables, int8-quantized, re-baked for this revision's 333.333Hz/S_LEN=80 lag grid -- see
 * those files' own headers for how they were generated).
 * ================================================================================================ */

/* Pearson correlation between a candidate curve and one int8-quantized template. Correlation is
 * scale/shift-invariant, so int8's ~0.8%-of-full-scale quantization noise is far below the curve's
 * own tick-to-tick variation -- quantizing the (otherwise sizeable) template tables 4x smaller costs
 * no meaningful precision here. */
static float template_match_corr(const float *curve, const int8_t *tmpl, int n)
{
  const float dq = 1.0f / TEMPLATE_MATCH_QUANT_SCALE;
  float mean_a = 0.0f;
  float mean_b = 0.0f;
  for (int i = 0; i < n; i++) {
    mean_a += curve[i];
    mean_b += (float)tmpl[i] * dq;
  }
  mean_a /= (float)n;
  mean_b /= (float)n;
  float cov = 0.0f;
  float var_a = 0.0f;
  float var_b = 0.0f;
  for (int i = 0; i < n; i++) {
    float da = curve[i] - mean_a;
    float db = (float)tmpl[i] * dq - mean_b;
    cov += da * db;
    var_a += da * da;
    var_b += db * db;
  }
  float denom = sqrtf(var_a * var_b);
  return (denom > 1e-9f) ? (cov / denom) : 0.0f;
}

/* Scores against the nearest TWO bpm buckets and takes the max, rather than only the single
 * nearest one -- a candidate sitting near a bucket boundary can score much higher against the
 * neighboring bucket's template than its own nominally-nearest one. */
static float best_template_corr(const float *curve, float bpm, int *out_bucket_bpm)
{
  int best_i = -1;
  int second_i = -1;
  float best_d = 1e30f;
  float second_d = 1e30f;
  for (int i = 0; i < TEMPLATE_MATCH_N_BUCKETS; i++) {
    float d = fabsf(bpm - kTemplateBpm[i]);
    if (d < best_d) {
      second_d = best_d;
      second_i = best_i;
      best_d = d;
      best_i = i;
    } else if (d < second_d) {
      second_d = d;
      second_i = i;
    }
  }
  float corr = template_match_corr(curve, kTemplateCurve[best_i], TEMPLATE_MATCH_N_CURVE);
  int used = best_i;
  if (second_i >= 0) {
    float corr2 = template_match_corr(curve, kTemplateCurve[second_i], TEMPLATE_MATCH_N_CURVE);
    if (corr2 > corr) {
      corr = corr2;
      used = second_i;
    }
  }
  if (out_bucket_bpm) {
    *out_bucket_bpm = (int)kTemplateBpm[used];
  }
  return corr;
}

/* Region-weighted scoring: a plain whole-curve correlation dilutes a missing expected bump (e.g.
 * the S1-S2 sub-beat) across the curve's whole variance. Instead, 5 Gaussian bumps in RATIO space
 * (relative to the candidate's own lag) up-weight the regions where a real fundamental's harmonics/
 * sub-beat structure should show up, before correlating. */
typedef struct RegionWeight {
  float ratio, sigma, boost;
} RegionWeight_t;
static const RegionWeight_t kRegionWeights[5] = {
    {0.5f, 0.05f, 3.0f},        /* octave harmonic: candidate too fast */
    {2.0f, 0.08f, 3.0f},        /* octave harmonic: candidate too slow */
    {1.0f / 3.0f, 0.03f, 2.0f}, /* 3rd-harmonic companion, weaker evidence */
    {3.0f, 0.12f, 2.0f},        /* 3rd-harmonic companion, weaker evidence */
    {0.667f, 0.08f, 2.5f},      /* S1-S2 sub-beat bump */
};

/* Lookup table for the standard unit Gaussian exp(-0.5*d*d), d in [0,4] (beyond 4 the true value is
 * <=3.4e-4 of the peak, indistinguishable from 0 here) -- all 5 region bumps share this one table
 * (only their target ratio/sigma/boost differ), avoiding a soft-float expf() call per bump per lag
 * bin, of which there can be thousands per tick (up to 6 candidate peaks x S_LEN lag bins x 5
 * bumps). Linearly interpolated between entries so the resulting score has no zeroth-order-hold
 * steps. Generated via: python -c "import numpy as np; d=np.linspace(0,4.0,201); print(np.exp(-0.5*d*d))" */
#define GAUSS_LUT_N    201
#define GAUSS_LUT_DMAX 4.0f
static const float kGaussLUT[GAUSS_LUT_N] = {
    1.00000000f, 0.99980002f, 0.99920032f, 0.99820162f, 0.99680511f, 0.99501248f, 0.99282586f, 0.99024786f, 0.98728157f,
    0.98393051f, 0.98019867f, 0.97609047f, 0.97161077f, 0.96676484f, 0.96155838f, 0.95599748f, 0.95008863f, 0.94383870f,
    0.93725490f, 0.93034481f, 0.92311635f, 0.91557774f, 0.90773754f, 0.89960455f, 0.89118789f, 0.88249690f, 0.87354119f,
    0.86433055f, 0.85487502f, 0.84518478f, 0.83527021f, 0.82514182f, 0.81481026f, 0.80428628f, 0.79358073f, 0.78270454f,
    0.77166867f, 0.76048416f, 0.74916202f, 0.73771331f, 0.72614904f, 0.71448020f, 0.70271772f, 0.69087249f, 0.67895529f,
    0.66697681f, 0.65494763f, 0.64287820f, 0.63077882f, 0.61865965f, 0.60653066f, 0.59440166f, 0.58228224f, 0.57018181f,
    0.55810956f, 0.54607443f, 0.53408515f, 0.52215020f, 0.51027780f, 0.49847592f, 0.48675226f, 0.47511424f, 0.46356902f,
    0.45212346f, 0.44078414f, 0.42955736f, 0.41844911f, 0.40746510f, 0.39661073f, 0.38589113f, 0.37531110f, 0.36487516f,
    0.35458755f, 0.34445218f, 0.33447271f, 0.32465247f, 0.31499453f, 0.30550168f, 0.29617642f, 0.28702097f, 0.27803730f,
    0.26922710f, 0.26059182f, 0.25213264f, 0.24385049f, 0.23574608f, 0.22781987f, 0.22007211f, 0.21250282f, 0.20511182f,
    0.19789870f, 0.19086288f, 0.18400359f, 0.17731987f, 0.17081059f, 0.16447446f, 0.15831002f, 0.15231569f, 0.14648972f,
    0.14083025f, 0.13533528f, 0.13000271f, 0.12483031f, 0.11981577f, 0.11495667f, 0.11025053f, 0.10569476f, 0.10128672f,
    0.09702370f, 0.09290294f, 0.08892162f, 0.08507688f, 0.08136582f, 0.07778552f, 0.07433302f, 0.07100535f, 0.06779953f,
    0.06471256f, 0.06174144f, 0.05888317f, 0.05613476f, 0.05349324f, 0.05095563f, 0.04851899f, 0.04618039f, 0.04393693f,
    0.04178575f, 0.03972399f, 0.03774886f, 0.03585759f, 0.03404745f, 0.03231577f, 0.03065989f, 0.02907723f, 0.02756523f,
    0.02612141f, 0.02474331f, 0.02342855f, 0.02217477f, 0.02097970f, 0.01984109f, 0.01875678f, 0.01772463f, 0.01674258f,
    0.01580862f, 0.01492079f, 0.01407718f, 0.01327597f, 0.01251534f, 0.01179358f, 0.01110900f, 0.01045997f, 0.00984492f,
    0.00926233f, 0.00871073f, 0.00818870f, 0.00769488f, 0.00722795f, 0.00678664f, 0.00636972f, 0.00597602f, 0.00560442f,
    0.00525382f, 0.00492318f, 0.00461151f, 0.00431784f, 0.00404125f, 0.00378087f, 0.00353586f, 0.00330539f, 0.00308872f,
    0.00288509f, 0.00269381f, 0.00251420f, 0.00234563f, 0.00218749f, 0.00203920f, 0.00190019f, 0.00176996f, 0.00164799f,
    0.00153381f, 0.00142697f, 0.00132705f, 0.00123362f, 0.00114632f, 0.00106477f, 0.00098862f, 0.00091755f, 0.00085125f,
    0.00078943f, 0.00073180f, 0.00067811f, 0.00062811f, 0.00058156f, 0.00053824f, 0.00049796f, 0.00046050f, 0.00042569f,
    0.00039335f, 0.00036333f, 0.00033546f};

static inline float fast_unit_gauss(float d)
{
  float ad = fabsf(d);
  if (ad >= GAUSS_LUT_DMAX) {
    return 0.0f;
  }
  float pos = ad * ((float)(GAUSS_LUT_N - 1) / GAUSS_LUT_DMAX);
  int idx = (int)pos;
  if (idx >= GAUSS_LUT_N - 1) {
    return kGaussLUT[GAUSS_LUT_N - 1];
  }
  float frac = pos - (float)idx;
  return kGaussLUT[idx] + frac * (kGaussLUT[idx + 1] - kGaussLUT[idx]);
}

static inline void build_region_weights(float bpm, float *w)
{
  float peak_lag = 60.0f * FFS / bpm;
  for (int i = 0; i < TEMPLATE_MATCH_N_CURVE; i++) { /* only the template-width prefix is ever read */
    int lag = LO_LAG + i;
    float ratio = peak_lag / (float)lag;
    float wv = 1.0f;
    for (int k = 0; k < 5; k++) {
      float d = (ratio - kRegionWeights[k].ratio) / kRegionWeights[k].sigma;
      wv += kRegionWeights[k].boost * fast_unit_gauss(d);
    }
    w[i] = wv;
  }
}

/* Weighted-Pearson generalization of template_match_corr() -- degenerates to it when every weight
 * is equal. */
static float weighted_template_match_corr(const float *curve, const int8_t *tmpl, const float *w, int n)
{
  const float dq = 1.0f / TEMPLATE_MATCH_QUANT_SCALE;
  float sw = 0.0f;
  float mean_a = 0.0f;
  float mean_b = 0.0f;
  for (int i = 0; i < n; i++) {
    sw += w[i];
    mean_a += w[i] * curve[i];
    mean_b += w[i] * ((float)tmpl[i] * dq);
  }
  if (sw < 1e-9f) {
    return 0.0f;
  }
  mean_a /= sw;
  mean_b /= sw;
  float cov = 0.0f;
  float var_a = 0.0f;
  float var_b = 0.0f;
  for (int i = 0; i < n; i++) {
    float da = curve[i] - mean_a;
    float db = (float)tmpl[i] * dq - mean_b;
    cov += w[i] * da * db;
    var_a += w[i] * da * da;
    var_b += w[i] * db * db;
  }
  float denom = sqrtf(var_a * var_b);
  return (denom > 1e-9f) ? (cov / denom) : 0.0f;
}

static float best_weighted_template_corr(const float *curve, const int8_t templates[][TEMPLATE_MATCH_N_CURVE],
                                         float bpm, const float *w)
{
  int best_i = -1;
  int second_i = -1;
  float best_d = 1e30f;
  float second_d = 1e30f;
  for (int i = 0; i < TEMPLATE_MATCH_N_BUCKETS; i++) {
    float d = fabsf(bpm - kTemplateBpm[i]);
    if (d < best_d) {
      second_d = best_d;
      second_i = best_i;
      best_d = d;
      best_i = i;
    } else if (d < second_d) {
      second_d = d;
      second_i = i;
    }
  }
  float corr = weighted_template_match_corr(curve, templates[best_i], w, TEMPLATE_MATCH_N_CURVE);
  if (second_i >= 0) {
    float corr2 = weighted_template_match_corr(curve, templates[second_i], w, TEMPLATE_MATCH_N_CURVE);
    if (corr2 > corr) {
      corr = corr2;
    }
  }
  return corr;
}

/* Region-weighted RAW-vs-RAW score averaged with region-weighted DETRENDED-vs-DETRENDED score --
 * scores a candidate bpm against both the pre-detrend curve (raw shape) and the detrended curve
 * (isolates bump/dip structure), each against its own matching template set. */
static float combined_template_score(const float *raw_curve, const float *det_curve, float bpm, float *weights)
{
  /* 0.15: a peak below TEMPLATE_UNSCORED_BELOW_BPM has no template (and the 80-bin templates only span lags
   * 20..99 anyway) -- it would get exactly the confidence floor. Currently unreachable in practice because
   * the V2 pick only sees fast-zone peaks (>= 40 BPM); kept for fidelity with the dev tree. */
  if (bpm < TEMPLATE_UNSCORED_BELOW_BPM) {
    return TEMPLATE_UNSCORED_SCORE;
  }
  build_region_weights(bpm, weights);
  float raw_score = best_weighted_template_corr(raw_curve, kTemplateCurve, bpm, weights);
  float det_score = best_weighted_template_corr(det_curve, kTemplateCurveDetrended, bpm, weights);
  return 0.5f * (raw_score + det_score);
}

/* ================================================================================================
 * RAW CANDIDATE SELECTION -- rather than always taking win_teal's single tallest detrended peak,
 * scores its top few local maxima against the morphology templates above and lets a smaller,
 * more-plausible peak override the tallest one.
 * ================================================================================================ */

/* Single-pass running top-N insertion: keeps only the N tallest local maxima seen so far, in
 * descending-height order, rather than collecting every local maximum before sorting. Ties keep
 * ascending-bin-index (lower lag) order, since both the shift-insertion and full-list admission
 * checks use strict '>'. */
static int find_topN_peaks(const float *curve, int *out_bins, int n_max)
{
  float top_height[TEMPLATE_MATCH_GATED_V2_N_PEAKS];
  int count = 0;
  for (int i = 1; i < FAST_LEN - 1; i++) {
    if (!(curve[i] >= curve[i - 1] && curve[i] > curve[i + 1])) {
      continue;
    }
    float h = curve[i];
    if (count < n_max) {
      int pos = count;
      while (pos > 0 && h > top_height[pos - 1]) {
        top_height[pos] = top_height[pos - 1];
        out_bins[pos] = out_bins[pos - 1];
        pos--;
      }
      top_height[pos] = h;
      out_bins[pos] = i;
      count++;
    } else if (h > top_height[n_max - 1]) {
      int pos = n_max - 1;
      while (pos > 0 && h > top_height[pos - 1]) {
        top_height[pos] = top_height[pos - 1];
        out_bins[pos] = out_bins[pos - 1];
        pos--;
      }
      top_height[pos] = h;
      out_bins[pos] = i;
    }
  }
  return count;
}

/* Scores the top TEMPLATE_MATCH_GATED_V2_N_PEAKS local maxima of win_teal's detrended curve, and
 * overrides the default tallest-peak pick (bins[0]) only when a smaller peak's score clearly beats
 * it (by more than the override margin). Returns false (candidate untouched) if there are no local
 * maxima at all. Sets *out_reject when peaks exist but even the winning one misses the confidence
 * floor -- distinct from "nothing to look at," since the caller treats these two cases differently
 * (leave the existing candidate alone vs. discard it outright). */
static bool template_match_gated_pick_v2(const EstimateResult_t *win_r, const EstimateResult_t *win_teal,
                                         float *out_bpm, bool *out_reject, float weights[S_LEN])
{
  *out_reject = false;
  if (!win_r->valid || !win_teal->valid) {
    return false;
  }
  int bins[TEMPLATE_MATCH_GATED_V2_N_PEAKS];
  int npk = find_topN_peaks(win_teal->Sw, bins, TEMPLATE_MATCH_GATED_V2_N_PEAKS);
  if (npk == 0) {
    return false;
  }

  float peak_bpm[TEMPLATE_MATCH_GATED_V2_N_PEAKS];
  float peak_score[TEMPLATE_MATCH_GATED_V2_N_PEAKS];
  int best_idx = 0;
  peak_bpm[0] = 60.0f * FFS / (float)(LO_LAG + bins[0]);
  peak_score[0] = combined_template_score(win_r->Sw, win_teal->Sw, peak_bpm[0], weights);
  for (int i = 1; i < npk; i++) {
    peak_bpm[i] = 60.0f * FFS / (float)(LO_LAG + bins[i]);
    peak_score[i] = combined_template_score(win_r->Sw, win_teal->Sw, peak_bpm[i], weights);
    if (peak_score[i] > peak_score[best_idx] + TEMPLATE_MATCH_OVERRIDE_MARGIN_V2 &&
        peak_score[i] > peak_score[best_idx]) {
      best_idx = i;
    }
  }
  if (peak_score[best_idx] < TEMPLATE_MATCH_CONFIDENCE_FLOOR_V2) {
    *out_reject = true;
    return false;
  }
  /* Parabolic refinement of the winning peak on win_teal->Sw. Display-only: scoring above stays on
   * integer bins. Removes most of the lag-quantization sawtooth (bpm = 60*FFS/lag, step ~bpm^2/4000). */
  {
    int b = bins[best_idx];
    float lag = (float)(LO_LAG + b);
    if (b >= 1 && b + 1 < S_LEN) {
      float ym = win_teal->Sw[b - 1];
      float y0 = win_teal->Sw[b];
      float yp = win_teal->Sw[b + 1];
      float den = ym - 2.0f * y0 + yp;
      if (den < -1e-12f) {
        float d = 0.5f * (ym - yp) / den;
        if (d > -0.5f && d < 0.5f) {
          lag += d;
        }
      }
    }
    *out_bpm = 60.0f * FFS / lag;
  }
  return true;
}

/* ================================================================================================
 * HARMONIC VETO
 * ================================================================================================ */

/* Looks for corroborating autocorrelation structure at roughly HARMONIC_VETO_LO_FRAC..HI_FRAC times
 * the candidate's own lag (i.e. near half the candidate's bpm) -- if the true fundamental sits there
 * instead, this peak should still show up as a local max in the raw (undetrended) curve, extended
 * out to hi_ext. Returns 1 (confirmed), 0 (vetoed), or -1 (not evaluated: invalid input or not
 * enough history). */
static int check_harmonic_confirmed(hr_state_t *st, int hist_len, int win_nsamp, bool cand_valid,
                                    float cand_bpm, const float *precomputed_feat, int precomputed_m,
                                    float robust_trim_frac)
{
  if (!cand_valid || hist_len < win_nsamp) {
    return -1;
  }
  float cand_lag = 60.0f * FFS / cand_bpm;
  int hi_ext = (int)ceilf(HARMONIC_VETO_HI_EXT_MULT * cand_lag) + 2;
  if (hi_ext <= LO_LAG + 2) {
    return 0;
  }
  if (hi_ext > EXT_HI_MAX) {
    hi_ext = EXT_HI_MAX;
  }

  int m = precomputed_m;
  int n_ext = hi_ext - LO_LAG;
  if (m <= hi_ext) {
    return 0;
  }

  float *S_ext = st->scratch_Sext;
  for (int i = 0; i < n_ext; i++) {
    S_ext[i] = 0.0f;
  }
  add_autocorr_robust(st, S_ext, precomputed_feat, m, LO_LAG, hi_ext, robust_trim_frac);

  float target_lo = HARMONIC_VETO_LO_FRAC * cand_lag;
  float target_hi = HARMONIC_VETO_HI_FRAC * cand_lag;
  float smax = 0.0f;
  for (int i = 0; i < n_ext; i++) {
    if (S_ext[i] > smax) {
      smax = S_ext[i];
    }
  }
  float thresh = HARMONIC_VETO_HEIGHT_FRAC * smax;

  bool confirmed = false;
  for (int i = 1; i < n_ext - 1; i++) {
    float lag = (float)(LO_LAG + i);
    if (lag > target_lo && lag < target_hi && S_ext[i] >= S_ext[i - 1] && S_ext[i] > S_ext[i + 1] &&
        S_ext[i] > thresh) {
      confirmed = true;
      break;
    }
  }
  return confirmed ? 1 : 0;
}

/* ================================================================================================
 * SMOOTHING / WEIGHTED-MEDIAN COMBINE
 * ================================================================================================ */

static inline float bpm_favor_center(float bpm)
{
  if (bpm <= TENT_PEAK_BPM) {
    float t = (bpm - 40.0f) / (TENT_PEAK_BPM - 40.0f);
    if (t < 0) {
      t = 0;
    }
    if (t > 1) {
      t = 1;
    }
    return TENT_LO_H + t * (TENT_PEAK_H - TENT_LO_H);
  }
  float t = (bpm - TENT_PEAK_BPM) / (200.0f - TENT_PEAK_BPM);
  if (t < 0) {
    t = 0;
  }
  if (t > 1) {
    t = 1;
  }
  return TENT_PEAK_H + t * (TENT_HI_H - TENT_PEAK_H);
}

/* Combine weight for one historical candidate: a bpm-plausibility tent, times an agreement bonus if
 * flux agrees within tolerance, times a soft multiplier from the candidate's own morphology-template
 * correlation (pivot/span chosen so corr=0.30, the confidence-floor threshold, maps to weight
 * TEMPLATE_WEIGHT_LO and corr=0.70 maps to TEMPLATE_WEIGHT_HI). */
static inline float teal_size_factor(float bpm, bool agree_valid, float agree_bpm, bool tmpl_corr_valid,
                                     float tmpl_corr)
{
  bool agrees = agree_valid && fabsf(agree_bpm - bpm) <= AGREEMENT_BPM_TOL;
  float factor = bpm_favor_center(bpm);
  if (agrees) {
    factor *= AGREE_BONUS;
  }
  float tw_sub = tmpl_corr_valid ? (tmpl_corr - 0.30f) / 0.40f : 0.5f;
  if (tw_sub < 0.0f) {
    tw_sub = 0.0f;
  }
  if (tw_sub > 1.0f) {
    tw_sub = 1.0f;
  }
  factor *= TEMPLATE_WEIGHT_LO + tw_sub * (TEMPLATE_WEIGHT_HI - TEMPLATE_WEIGHT_LO);
  return factor;
}

/* Weighted median over up to WEIGHTED_MED_TRAILING_N (vals[i], weights[i]) pairs: sorts by value,
 * then walks the cumulative weight until it crosses half the total. Falls back to an unweighted
 * median if every weight is <= 0 (shouldn't happen in practice, since bpm_favor_center() is always
 * positive, but keeps the function well-defined regardless). */
static inline float weighted_median(const float *vals, const float *weights, int count)
{
  int idx[WEIGHTED_MED_TRAILING_N];

  for (int i = 0; i < count; i++) {
    idx[i] = i;
  }
  for (int i = 0; i < count; i++) {
    int best = i;
    for (int j = i + 1; j < count; j++) {
      if (vals[idx[j]] < vals[idx[best]]) {
        best = j;
      }
    }
    int tmp = idx[i];
    idx[i] = idx[best];
    idx[best] = tmp;
  }
  float total = 0.0f;
  for (int i = 0; i < count; i++) {
    total += weights[i];
  }
  if (total <= 0.0f) {
    if (count % 2) {
      return vals[idx[count / 2]];
    }
    return 0.5f * (vals[idx[count / 2 - 1]] + vals[idx[count / 2]]);
  }
  float half = total / 2.0f;
  float cum = 0.0f;
  for (int i = 0; i < count; i++) {
    cum += weights[idx[i]];
    if (cum >= half) {
      return vals[idx[i]];
    }
  }
  return vals[idx[count - 1]];
}

/* How many consecutive ticks a candidate must repeat near its own value before being pushed, once
 * a settling wait has lifted -- grows slightly with bpm, and tapers down over RAMP_DELAY_TAPER_S
 * seconds after the wait ends (state is still settling right after an event). */
static inline int required_teal_delay_ticks(float bpm, float time_since_wait_end, bool is_first)
{
  float taper_s = is_first ? RAMP_DELAY_TAPER_S_FIRST : RAMP_DELAY_TAPER_S;
  float extra_ticks = is_first ? RAMP_DELAY_EXTRA_TICKS_FIRST : RAMP_DELAY_EXTRA_TICKS;
  float slope = is_first ? TEAL_BPM_DELAY_SLOPE_FIRST : TEAL_BPM_DELAY_SLOPE;
  float over = bpm - 60.0f;
  if (over < 0) {
    over = 0;
  }
  int t = (int)ceilf(1.0f + slope * over);
  if (time_since_wait_end < taper_s) {
    float frac = (taper_s - time_since_wait_end) / taper_s;
    t += (int)ceilf(extra_ticks * frac);
  }
  return t < 1 ? 1 : t;
}

/* ================================================================================================
 * STREAMING FRONT END -- chunked artifact-removal/declick/spike-eventing at the decimated rate,
 * then streaming decimation from the raw incoming samples.
 * ================================================================================================ */

/* Cleans one [carry+chunk] buffer in place (spike-event detection, artifact detect/extend/
 * interpolate, declick clip, both baselines updated from the raw pre-cleaning chunk), then feeds
 * the newly-cleaned chunk into the per-band envelope pipeline and shifts the tail into the carry
 * region for next time. Operates on st->dec_chunk_buf; called once per full chunk accumulated by
 * hr_push_data(). */
static void process_and_emit_chunk(hr_state_t *st, int samples)
{
  float *buf = st->dec_chunk_buf;
  int total = DEC_CARRY_N + samples;
  bool *scratch_grown = st->stack.push.grown;
  float *scratch_absx = st->stack.push.absx;
  float *raw_chunk = st->stack.push.raw_chunk;
  memcpy(raw_chunk, buf + DEC_CARRY_N, samples * sizeof(float));

  /* Spike detection is event-timing only -- it never modifies the signal, just records the
   * timestamp of the loudest sample in each run exceeding the median-baseline threshold, using
   * the baseline as it stood BEFORE this chunk (consistent with the artifact detector below). */
  if (st->med_baseline.initialized) {
    float med_scale = st->med_baseline.ema * 1.4826f;
    if (med_scale == 0.0f) {
      med_scale = 1e-6f;
    }
    float lim = SPIKE_DETECT_MULT * med_scale;
    float max_t = NEG_INF;
    bool any_spike = false;
    int i = DEC_CARRY_N;
    while (i < total) {
      if (fabsf(buf[i]) <= lim) {
        i++;
        continue;
      }
      int j = i;
      float best_v = -1.0f;
      int best_local = i;
      while (j < total && fabsf(buf[j]) > lim) {
        float av = fabsf(buf[j]);
        if (av > best_v) {
          best_v = av;
          best_local = j;
        }
        j++;
      }
      float spike_t = st->chunk_time_cursor + (float)(best_local - DEC_CARRY_N) / DEC_HZ;
      if (spike_t > max_t) {
        max_t = spike_t;
      }
      any_spike = true;
      i = j;
    }
    if (any_spike && max_t > st->last_logged_spike_t + 0.01f) {
      st->last_spike_el = max_t;
      st->last_logged_spike_t = max_t;
    }
  }

  /* Artifact detect/extend/interpolate, using the pre-chunk baseline (established from many
   * prior chunks, not re-derived from this potentially-contaminated one). */
  if (st->art_baseline.initialized) {
    float art_scale = st->art_baseline.ema * 1.4826f;
    if (art_scale == 0.0f) {
      art_scale = 1e-6f;
    }
    stream_detect_extend_interp(buf, total, art_scale, ARTIFACT_DETECT_MULT, ARTIFACT_EXTEND_MULT, ARTIFACT_PAD_SAMPLES,
                                scratch_grown, scratch_absx, st->stack.push.padded, st->stack.push.original);
  }

  /* Declick clip, using the median baseline (also established pre-chunk). */
  if (st->med_baseline.initialized) {
    float med_scale = st->med_baseline.ema * 1.4826f;
    if (med_scale == 0.0f) {
      med_scale = 1e-6f;
    }
    declick_clip(buf, total, med_scale, TRANSIENT_LIMIT_MULT);
  }

  /* Both baselines are updated from the RAW (pre-cleaning) chunk, not the artifact-removed/
   * declicked output -- updating from the cleaned chunk would let a sustained quiet stretch
   * collapse both baselines toward near-zero, and then real signal resuming would look like a
   * giant artifact relative to that collapsed threshold and get suppressed, permanently locking
   * out real signal. */
  float chunk_duration_s = (float)samples / DEC_HZ;
  float baseline_alpha = expf(-chunk_duration_s / BASELINE_TAU_S);
  baseline_update(&st->art_baseline, raw_chunk, samples, ARTIFACT_BASELINE_PCTL, chunk_duration_s, baseline_alpha,
                  scratch_absx);
  baseline_update(&st->med_baseline, raw_chunk, samples, 0.5f, chunk_duration_s, baseline_alpha, scratch_absx);

  stream_dec_samples(st, buf + DEC_CARRY_N, samples);
  st->chunk_time_cursor += chunk_duration_s;
}

/* Continuous anti-alias filter + stride-keep -- carries filter state across calls, never resets, so
 * it can be fed one sample or a whole buffer at a time with identical results. */
static int streaming_decimate(hr_state_t *st, const float *samples, int n, float *out)
{
  int m = 0;
  for (int i = 0; i < n; i++) {
    float y = iir4_step(AA_B, AA_A, samples[i], st->dec_aa_z);
    if (st->dec_stride_phase == 0) {
      out[m++] = y;
    }
    st->dec_stride_phase = (st->dec_stride_phase + 1) % DEC_STRIDE;
  }
  return m;
}

/* ================================================================================================
 * PUBLIC API
 * ================================================================================================ */

uint32_t hr_state_size(void)
{
  return (uint32_t)sizeof(hr_state_t);
}

const char *hr_version(void)
{
  return COMBO13_VERSION;
}

const char *hr_model_id(void)
{
  return "combo13 stream";
}

void hr_default_settings(hr_settings_t *settings)
{
}

hr_result_t hr_init(hr_state_t *st, const hr_settings_t *settings)
{
  if (NULL == st) {
    return HR_ERR_ARG;
  }

  memset(st, 0, sizeof(*st));
  st->last_onset_el = NEG_INF;
  /* last_spike_el/flatline_clear_el anchor to 0.0f (not NEG_INF) so since_event has a sane t=0
   * reference at true cold start, before any real spike/flatline event has ever fired. */
  st->last_spike_el = 0.0f;
  st->last_logged_spike_t = NEG_INF;
  st->flatline_clear_el = 0.0f;
  st->was_flatline = false;
  st->prev_last_event_el = NEG_INF;
  st->peak_mean_below_since_el = NEG_INF;
  st->frozen_wait_s = CONS_WAIT_S;
  for (int i = 0; i < S_LEN; i++) {
    st->lag_pow_recip[i] = 1.0f / powf((float)(LO_LAG + i), LAG_POW);
  }
  baseline_init(&st->art_baseline);
  baseline_init(&st->med_baseline);
  for (int i = 0; i < NLOG; i++) {
    st->cons_buf_log[i].valid = false;
    st->agree_log[i].valid = false;
  }

  return HR_OK;
}

#define PUSH_DECIMATE_SAMPLES 1

bool hr_push_data(hr_state_t *st, const int16_t *samples, int n)
{
  bool result = false;
  int chunk_len = st->dec_chunk_len;

  if (NULL == st || NULL == samples) {
    return false;
  }

  for (int i = 0; i < n; i++) {
    float input_sample = (float)samples[i];
    float dec_sample;
    int got = streaming_decimate(st, &input_sample, PUSH_DECIMATE_SAMPLES, &dec_sample);
    if (got) {
      st->dec_chunk_buf[DEC_CARRY_N + chunk_len] = dec_sample;
      chunk_len++;
      if (chunk_len >= CHUNK_TOTAL_N - DEC_CARRY_N) {
        DEBUG_PRINTF("Heart rate chunk overflow\n");
        chunk_len--;
        break;
      } else if (chunk_len >= DEC_CHUNK_N) {
        result = true;
      }
    }
  }

  st->dec_chunk_len = chunk_len;

  return result;
}

int hr_process_chunk(hr_state_t *st)
{
  if (NULL == st) {
    return 0;
  }

  st->cur_chunk_len = st->dec_chunk_len;
  process_and_emit_chunk(st, st->cur_chunk_len);

  return st->dec_chunk_len;
}

int hr_update_chunk(hr_state_t *st)
{
  if (NULL == st) {
    return 0;
  }

  memmove(st->dec_chunk_buf, st->dec_chunk_buf + st->cur_chunk_len,
          (DEC_CARRY_N + st->dec_chunk_len - st->cur_chunk_len) * sizeof(float));
  st->dec_chunk_len -= st->cur_chunk_len;
  st->cur_chunk_len = 0;

  return st->dec_chunk_len;
}

/* Appends one tick's outcome to the trailing candidate log (a fixed-depth ring), read back by the
 * weighted-median combine in hr_calculate() below. */
static void log_push(hr_state_t *st, bool val_valid, float val, bool conf_valid, float conf, bool agree_valid,
                     float agree, bool tmpl_corr_valid, float tmpl_corr)
{
  int i = st->log_head;
  st->cons_buf_log[i].valid = val_valid;
  st->cons_buf_log[i].val = val;
  st->cons_buf_log[i].conf_valid = conf_valid;
  st->cons_buf_log[i].conf = conf;
  st->agree_log[i].valid = agree_valid;
  st->agree_log[i].val = agree;
  st->template_corr_log[i].valid = tmpl_corr_valid;
  st->template_corr_log[i].val = tmpl_corr;
  st->log_head = (i + 1) % NLOG;
  if (st->log_count < NLOG) {
    st->log_count++;
  }
}

int hr_calculate(hr_state_t *st, hr_reading_t *out)
{
  float el = st->el;

  if (NULL == st) {
    return HR_ERR_ARG;
  }

  if (NULL != out) {
    memset(out, 0, sizeof(*out));
    out->t_s = el;
  }

  int hist_len = st->dec_sample_count;

  /* Onset/signal-level: short trailing-RMS window (not the whole history) -- only the trailing
   * ~1.5s RMS is ever used. sig_win is reused below by the flatline check, which needs the exact
   * same short window (see that block's comment). */
  float sig_level = 0.0f;
  int sig_win = 0;
  if (hist_len > 0) {
    sig_win = (int)(2.0f * DEC_HZ);
    if (sig_win > hist_len) {
      sig_win = hist_len;
    }
    int m = broad_env(st, sig_win, st->scratch_shared);
    int want = (int)lroundf(1.5f * FFS);
    int n_trail = m;
    if (want < n_trail) {
      n_trail = want;
    }
    if (n_trail > 0) {
      float sumsq = 0.0f;
      for (int i = m - n_trail; i < m; i++) {
        sumsq += st->scratch_shared[i] * st->scratch_shared[i];
      }
      sig_level = sqrtf(sumsq / (float)n_trail);
    }
  }

  float signal_offset_thresh = SIGNAL_ONSET_THRESH * 0.5f;
  if (st->sig_was_onset_ok) {
    st->onset_on_streak = 0;
    if (sig_level < signal_offset_thresh) {
      st->onset_off_streak++;
      if (st->onset_off_streak >= ONSET_DEBOUNCE_TICKS) {
        st->sig_was_onset_ok = false;
      }
    } else {
      st->onset_off_streak = 0;
    }
  } else {
    st->onset_off_streak = 0;
    if (sig_level >= SIGNAL_ONSET_THRESH) {
      if (st->onset_on_streak == 0) {
        st->onset_candidate_el = el;
      }
      st->onset_on_streak++;
      if (st->onset_on_streak >= ONSET_DEBOUNCE_TICKS) {
        st->last_onset_el = st->onset_candidate_el;
        st->sig_was_onset_ok = true;
        st->onset_on_streak = 0;
      }
    } else {
      st->onset_on_streak = 0;
    }
  }

  /* Event timeline: only spikes and genuine flatline-clear events (below) anchor since_event/
   * frozen_wait_s -- onset (above) is tracked but SIGNAL_ONSET_THRESH is calibrated to reject only
   * true digital silence, so it practically never toggles off once real contact is made, and so
   * never contributes a fresh event here. */
  float last_event_el = st->last_spike_el;
  if (st->flatline_clear_el > last_event_el) {
    last_event_el = st->flatline_clear_el;
  }
  float since_event = el - last_event_el;

  float adaptive_tau = since_event;
  if (adaptive_tau < DECAY_ADAPTIVE_TAU_MIN) {
    adaptive_tau = DECAY_ADAPTIVE_TAU_MIN;
  }
  if (adaptive_tau > DECAY_ADAPTIVE_TAU_MAX) {
    adaptive_tau = DECAY_ADAPTIVE_TAU_MAX;
  }

  /* r_adaptive preview: sizes the settling-wait timer only (frozen_wait_s below), never itself
   * displayed. Uses an adaptive-decay-weighted autocorrelation over all available history so far,
   * favoring recent samples more heavily the longer it's been since the last event. */
  bool r_adaptive_valid = false;
  float r_adaptive_bpm = 0.0f;
  if (hist_len >= 3 * (int)DEC_HZ) {
    float *S = st->stack.tick.prv.S;
    memset(S, 0x0, S_LEN * sizeof(float));
    float tau_frames = adaptive_tau * FFS;
    for (int b = 0; b < 3; b++) {
      int m = feat_norm_clip(st, hist_len, b, FEAT_CLIP, st->scratch_shared);
      /* tau_frames is fixed across this whole loop, so the exponential decay weights form an
         exact geometric sequence -- computed via one expf() for the endpoint, one for the
         common ratio, and m-1 multiplies, instead of one expf() per frame. */
      /* in-tree stack fix: was `float w[FEAT_MAX]` (2,576B stack local) -- see
       * scratch_feat's comment in combo13_stream_internal.h */
      float *w = st->scratch_feat;
      if (m > 0) {
        float ratio = expf(1.0f / tau_frames);
        w[0] = expf(-(float)(m - 1) / tau_frames);
        for (int i = 1; i < m; i++) {
          w[i] = w[i - 1] * ratio;
        }
      }
      float r0 = 0.0f;
      float r0w = 0.0f;
      for (int i = 0; i < m; i++) {
        r0 += w[i] * st->scratch_shared[i] * st->scratch_shared[i];
        r0w += w[i];
      }
      r0 = r0 / (r0w != 0.0f ? r0w : 1.0f);
      if (m > FAST_HI_LAG) {
        for (int l = LO_LAG; l < FAST_HI_LAG; l++) {
          int nn = m - l;
          float s = 0.0f;
          float sw = 0.0f;
          for (int i = 0; i < nn; i++) {
            s += w[i] * st->scratch_shared[i] * st->scratch_shared[i + l];
            sw += w[i];
          }
          S[l - LO_LAG] += (s / (sw != 0.0f ? sw : 1.0f)) / (r0 + 1e-9f);
        }
      }
    }
    float *Sw = st->stack.tick.prv.Sw;
    for (int l = LO_LAG; l < FAST_HI_LAG; l++) {
      Sw[l - LO_LAG] = S[l - LO_LAG] * st->lag_pow_recip[l - LO_LAG];
    }
    float swmin = Sw[0];
    for (int i = 1; i < FAST_LEN; i++) {
      if (Sw[i] < swmin) {
        swmin = Sw[i];
      }
    }
    for (int i = 0; i < FAST_LEN; i++) {
      Sw[i] -= swmin;
    }
    int p1 = -1;
    float bv = -1e30f;
    for (int i = 1; i < FAST_LEN - 1; i++) {
      if (Sw[i] >= Sw[i - 1] && Sw[i] > Sw[i + 1] && Sw[i] > bv) {
        bv = Sw[i];
        p1 = i;
      }
    }
    r_adaptive_bpm = (p1 < 0) ? 60.0f / ((float)LO_LAG / FFS) : 60.0f / (((float)LO_LAG + p1) / FFS);
    r_adaptive_valid = true;
  }

  float win_sec = SINGLE_WIN_MIN + (since_event - st->frozen_wait_s > 0 ? since_event - st->frozen_wait_s : 0.0f);
  if (win_sec < SINGLE_WIN_MIN) {
    win_sec = SINGLE_WIN_MIN;
  }
  if (win_sec > SINGLE_WIN_MAX) {
    win_sec = SINGLE_WIN_MAX;
  }
  int win_nsamp = (int)lroundf(win_sec * DEC_HZ);

  EstimateResult_t *win_r = &st->stack.tick.win_r;
  win_r->valid = false;
  if (hist_len >= win_nsamp && win_nsamp > 0) {
    estimate_sumenv_robust(st, win_nsamp, ROBUST_TRIM_FRAC, FEAT_CLIP, win_r);
  }
  EstimateResult_t *win_teal = &st->stack.tick.win_teal;
  win_teal->valid = false;
  estimate_detrend_from_mw(st, win_r, TEAL_PUSH_HALF_WIDTH, win_teal);

  /* Flatline (device-off) detector: reuses sig_win, the SAME short window sig_level uses above,
   * rather than the full (up to 8s) analysis window. After a real disconnect, an 8s window's own
   * top-10%-mean stays anchored to the still-resident pre-removal loud stretch for several
   * seconds while the window's overall mean falls -- a blind spot where a spurious candidate
   * could lock in with nothing to gate it. The short window closes that gap. */
  bool peak_mean_valid = false;
  float peak_minus_mean = 0.0f;
  if (sig_win > 0) {
    int m = broad_env(st, sig_win, st->scratch_shared);
    if (m > 0) {
      float sum = 0.0f;
      for (int i = 0; i < m; i++) {
        sum += st->scratch_shared[i];
      }
      float mean_abs = sum / (float)m;
      int top_n = (int)lroundf((float)m * PEAK_MEAN_TOP_PCT);
      if (top_n < 1) {
        top_n = 1;
      }
      int boundary = m - top_n;
      quickselectf(st->scratch_shared, m, boundary);
      float peak_sum = 0.0f;
      for (int i = boundary; i < m; i++) {
        peak_sum += st->scratch_shared[i];
      }
      float peak_level = peak_sum / (float)top_n;
      peak_minus_mean = peak_level - mean_abs;
      peak_mean_valid = true;
    }
  }
  bool peak_mean_below_now = (!peak_mean_valid) || (peak_minus_mean < PEAK_MEAN_DIFF_THRESH);
  if (peak_mean_below_now) {
    if (st->peak_mean_below_since_el == NEG_INF) {
      st->peak_mean_below_since_el = el;
    }
  } else {
    st->peak_mean_below_since_el = NEG_INF;
  }
  bool is_flatline =
      (st->peak_mean_below_since_el != NEG_INF) && (el - st->peak_mean_below_since_el) >= PEAK_MEAN_SUSTAIN_S;

  if (is_flatline) {
    for (int i = 0; i < NLOG; i++) {
      st->cons_buf_log[i].valid = false;
    }
  }
  /* Clearing is_flatline has no sustain requirement -- a single good-looking tick ends it, since
   * brief few-second dips (breathing, momentary weak contact) are common even in otherwise-normal
   * recordings and shouldn't each be treated as a fresh disconnect/reconnect. Only an episode that
   * persisted at least FLATLINE_FULL_RESET_MIN_S counts as a genuine reconnect for event-timing
   * purposes (below); shorter blips still clear cons_buf_log above but skip the full settling reset. */
  if (is_flatline && !st->was_flatline) {
    st->flatline_became_true_el = el;
  }
  if (st->was_flatline && !is_flatline) {
    float flatline_dur = el - st->flatline_became_true_el;
    /* ever_displayed exempts the initial cold-start bootstrap (every stream begins flatline for
     * several seconds by design) from a second full settling wait stacked on the first. If
     * establishing contact spans multiple flatline episodes before the first-ever valid display,
     * the original wait window will already have elapsed by the time a later episode clears --
     * since_event>=frozen_wait_s catches that case too, so a slow first contact still gets full
     * settling protection even though ever_displayed is still false. */
    bool past_original_wait = since_event >= st->frozen_wait_s;
    if ((st->ever_displayed || past_original_wait) && flatline_dur >= FLATLINE_FULL_RESET_MIN_S) {
      st->flatline_clear_el = el;
    }
  }
  st->was_flatline = is_flatline;

  bool is_new_event = (last_event_el != st->prev_last_event_el);
  if (is_new_event) {
    st->prev_last_event_el = last_event_el;
    st->frozen_wait_s =
        CONS_WAIT_S + (r_adaptive_valid ? CONS_WAIT_EXTRA_S_PER_BPM * fabsf(r_adaptive_bpm - 60.0f) : 0.0f);
  }

  /* Flux agreement signal: rectified first-difference of the raw summed envelope, z-scored+
   * clipped, autocorrelated over the same lag range as the main candidate -- an independently-
   * derived second opinion the settling and combine stages check the main candidate against. */
  bool flux_valid = false;
  float flux_bpm = 0.0f;
  if (win_r->valid) {
    int m = envelope_flux(st, win_nsamp, st->scratch_shared);
    if (m > FAST_HI_LAG) {
      float *S = st->stack.tick.flx.S;
      memset(S, 0x0, S_LEN * sizeof(float));
      add_autocorr_plain(S, st->scratch_shared, m, LO_LAG, FAST_HI_LAG);
      float *Sw_buf = st->stack.tick.flx.Sw_buf;
      for (int l = LO_LAG; l < FAST_HI_LAG; l++) {
        Sw_buf[l - LO_LAG] = S[l - LO_LAG] * st->lag_pow_recip[l - LO_LAG];
      }
      float *det_buf = st->stack.tick.flx.det_buf;
      float *dcsum = st->stack.tick.flx.dcsum;
      dcsum[0] = 0.0f;
      for (int i = 0; i < FAST_LEN; i++) {
        dcsum[i + 1] = dcsum[i] + Sw_buf[i];
      }
      for (int i = 0; i < FAST_LEN; i++) {
        det_buf[i] = Sw_buf[i] - moving_average_at(dcsum, FAST_LEN, i, TEAL_PUSH_HALF_WIDTH);
      }
      int pk = peak_idx_tallest_local_max(det_buf, FAST_LEN);
      flux_bpm = 60.0f * FFS / (float)(LO_LAG + pk);
      flux_valid = true;
    }
  }
  bool agree_valid = flux_valid;
  float agree_bpm = flux_bpm;

  /* Early-release streak: consecutive ticks the raw candidate has agreed with flux, letting the
   * settling wait be cut short below once two independently-derived estimates concur, rather than
   * always waiting the full frozen_wait_s. Reset on any new event or flatline. */
  bool early_agree_now = win_teal->valid && agree_valid && fabsf(agree_bpm - win_teal->bpm) <= AGREEMENT_BPM_TOL;
  if (is_flatline || is_new_event) {
    st->early_release_streak = 0;
  } else if (early_agree_now) {
    st->early_release_streak++;
  } else {
    st->early_release_streak = 0;
  }
  int early_release_min_ticks = st->ever_displayed ? EARLY_RELEASE_MIN_TICKS : EARLY_RELEASE_MIN_TICKS_FIRST;
  float early_release_max_save_s = st->ever_displayed ? EARLY_RELEASE_MAX_SAVE_S : EARLY_RELEASE_MAX_SAVE_S_FIRST;
  bool early_release_ok = (st->early_release_streak >= early_release_min_ticks) &&
                          (since_event >= st->frozen_wait_s - early_release_max_save_s);

  bool suppress_circle = is_flatline || is_new_event || (since_event < st->frozen_wait_s && !early_release_ok);
  bool raw_cand_valid = (!suppress_circle) && win_teal->valid;
  float raw_cand_bpm = raw_cand_valid ? win_teal->bpm : 0.0f;

  /* Morphology-template gated peak selection -- runs before the harmonic veto/jump guards below,
   * so everything downstream (including the veto itself) treats the possibly-overridden or
   * possibly-rejected result as the real candidate for this tick. */
  if (raw_cand_valid) {
    float gated_bpm;
    bool reject;
    float *weights = st->stack.tick.weights;
    if (template_match_gated_pick_v2(win_r, win_teal, &gated_bpm, &reject, weights)) {
      raw_cand_bpm = gated_bpm;
    } else if (reject) {
      raw_cand_valid = false;
    }
  }

  /* 0.15 SLOW-ZONE OVERRIDE (30-40 BPM): applies only when a slow-zone peak dominates the tallest fast-zone
   * peak by SLOW_RATIO_MIN. Replaces the fast-zone pick (including a V2 confidence-floor rejection); the
   * harmonic veto / jump / delay guards below still apply to it like any other candidate. */
  if (!suppress_circle && win_teal->valid && win_teal->slow_valid && win_teal->slow_ratio >= SLOW_RATIO_MIN) {
    raw_cand_bpm = win_teal->slow_bpm;
    raw_cand_valid = true;
  }

  int win_m = win_nsamp / FRAME_DS;
  int harmonic_confirmed = check_harmonic_confirmed(st, hist_len, win_nsamp, raw_cand_valid, raw_cand_bpm,
                                                    st->scratch_sumenv, win_m, ROBUST_TRIM_FRAC);
  if (harmonic_confirmed == 0) {
    raw_cand_valid = false;
  }

  bool is_jump = false;
  bool is_harmonic_jump = false;
  if (raw_cand_valid && st->last_accepted_teal_bpm_valid) {
    float ratio = raw_cand_bpm / st->last_accepted_teal_bpm;
    is_jump = (ratio >= TEAL_JUMP_RATIO) || (ratio <= 1.0f / TEAL_JUMP_RATIO);
    if (is_jump) {
      static const float kHarmonicRatios[4] = {2.0f, 0.5f, 3.0f, 1.0f / 3.0f};
      for (int hi = 0; hi < 4; hi++) {
        if (fabsf(ratio / kHarmonicRatios[hi] - 1.0f) <= TEAL_JUMP_HARMONIC_TOL) {
          is_harmonic_jump = true;
          break;
        }
      }
    }
  }
  if (suppress_circle) {
    st->teal_jump_streak = 0;
  } else if (is_jump) {
    st->teal_jump_streak++;
  } else {
    st->teal_jump_streak = 0;
  }
  int teal_jump_required_ticks = is_harmonic_jump ? TEAL_JUMP_HARMONIC_CONFIRM_TICKS : TEAL_JUMP_CONFIRM_TICKS;
  bool jump_unconfirmed = is_jump && (st->teal_jump_streak < teal_jump_required_ticks);

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
  bool bpm_delay_unconfirmed =
      raw_cand_valid &&
      (st->bpm_delay_streak < required_teal_delay_ticks(raw_cand_bpm, time_since_wait_end, !st->ever_displayed));

  bool pushed_valid = raw_cand_valid && !suppress_circle && !jump_unconfirmed && !bpm_delay_unconfirmed;
  float pushed_bpm = pushed_valid ? raw_cand_bpm : 0.0f;
  if (pushed_valid) {
    st->last_accepted_teal_bpm = pushed_bpm;
    st->last_accepted_teal_bpm_valid = true;
  }

  bool pushed_conf_valid = pushed_valid && win_teal->valid;
  float pushed_conf = pushed_conf_valid ? win_teal->conf : 0.0f;

  /* Morphology-template correlation for the PUSHED candidate -- feeds teal_size_factor()'s soft
   * combine weight below. Independent of the gated selection above; scored against win_r->Sw (the
   * raw, pre-detrend curve the templates were built from). */
  bool template_weight_valid = false;
  float template_weight_corr = 0.0f;
  if (pushed_valid && win_r->valid && pushed_bpm >= TEMPLATE_MATCH_MIN_BPM && pushed_bpm <= TEMPLATE_MATCH_MAX_BPM) {
    template_weight_corr = best_template_corr(win_r->Sw, pushed_bpm, NULL);
    template_weight_valid = true;
  }

  log_push(st, pushed_valid, pushed_bpm, pushed_conf_valid, pushed_conf, (!suppress_circle) && agree_valid, agree_bpm,
           template_weight_valid, template_weight_corr);

  /* Weighted-median combine over the trailing WEIGHTED_MED_TRAILING_N pushed candidates, each
   * weighted by teal_size_factor() -- smooths single-tick noise while still reacting promptly to
   * a genuine, sustained rate change. */
  float vals[WEIGHTED_MED_TRAILING_N], weights[WEIGHTED_MED_TRAILING_N];
  int count = 0;
  int avail = st->log_count;
  for (int back = 1; back <= avail && count < WEIGHTED_MED_TRAILING_N; back++) {
    int idx = (st->log_head - back + NLOG) % NLOG;
    if (!st->cons_buf_log[idx].valid) {
      continue;
    }
    float v = st->cons_buf_log[idx].val;
    bool av = st->agree_log[idx].valid;
    float aval = st->agree_log[idx].val;
    bool twv = st->template_corr_log[idx].valid;
    float twval = st->template_corr_log[idx].val;
    vals[count] = v;
    weights[count] = teal_size_factor(v, av, aval, twv, twval);
    count++;
  }
  bool weighted_med_valid = count > 0;
  float weighted_med = weighted_med_valid ? weighted_median(vals, weights, count) : 0.0f;

  if (NULL != out) {
    out->is_flatline = is_flatline;
    out->bpm_valid = (!is_flatline) && weighted_med_valid;
    out->bpm = out->bpm_valid ? weighted_med : 0.0f;
    out->raw_candidate_valid = raw_cand_valid;
    out->raw_candidate_bpm = raw_cand_valid ? raw_cand_bpm : 0.0f;
    out->harmonic_confirmed = harmonic_confirmed;
  }

  if ((!is_flatline) && weighted_med_valid) {
    st->ever_displayed = true;
  }

  st->el += TICK_S;
  return HR_OK;
}
