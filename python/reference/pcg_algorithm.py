#!/usr/bin/env python3
"""
PCG (phonocardiogram) heart-rate estimator -- single-file consolidation of the shipped
"Rolling Median" algorithm from pcg_visualizer/index.html, as validated by rolling_buffer_sim.py
against the 269-file real corpus and 96-file synthetic set (state as of 2026-07-17).

This file contains ONLY the currently-shipped path. Every experimental variant explored during
development (taper, no-band-split, 10Hz-gap bands, 250Hz/300Hz internal rate, [3.5,5.5,7.5]s
window spacing, AGC/log-compression) was tried, corpus-validated, and rejected or left as a
non-default comparison -- none of that is here. See ALGORITHM_REPORT.md for the full history.

Pipeline (per 250ms tick, over an 8s trailing raw-audio buffer):
  1. Spike detection on the RAW window (pre-declick) -- feeds event timing only.
  2. Declick: whole-window transient clip at 4x robust scale.
  3. Decimate 4000Hz -> 500Hz (Butterworth anti-alias + integer stride).
  4. Broadband envelope (3 bands, unnormalized) -> signal-level -> onset debounce.
  5. Event timing (last onset or spike) drives an adaptive-decay "preview" estimate, used ONLY
     to size the buffer's post-event wait timer (never displayed directly).
  6. Three band-limited envelopes (30-50/50-80/80-130Hz), each independently z-scored and
     winsorized to +-4.5, summed into ONE signal, ONE autocorrelation -- repeated at 4/6/8s
     trailing windows.
  7. Peak-pick with lag-power reweighting; confidence = ratio of the top peak to the next
     *distinct* peak (>=8 lags away).
  8. Rolling buffer: event-triggered clear, bpm-scaled frozen wait, progressive window
     inclusion (3s at wait-end, growing to 8s), spread (MAD<=12bpm) + confidence (>=1.05) gate,
     11-slot sample-and-hold buffer (one sample per 2s), median display.

Usage:
  est = PCGHeartRateEstimator(fs=4000)
  while <new audio available>:
      est.push(new_samples)          # any chunk size
      if <250ms of new audio has accumulated>:
          result = est.tick()
          print(result['t'], result['shown'], result['state'])

Dependencies: numpy, scipy (signal.lfilter for the IIR filters). A bare-metal embedded port
would replace lfilter with a direct-form-II biquad loop -- kept as scipy here since the goal is
a correctness/feasibility test, not a from-scratch C port.
"""
import numpy as np
from scipy.signal import lfilter

# ---------------- tunable constants (all values are the CURRENTLY SHIPPED ones) ----------------
FS_IN = 4000.0            # expected input sample rate (Hz)
WIN_S = 8.0               # trailing raw-audio buffer length (s)
TICK_S = 0.25             # nominal tick period (s) -- caller decides when to call tick()

# anti-alias + decimate 4000Hz -> 500Hz (4th-order Butterworth lowpass, integer stride 8)
AA_B = [0.0005891316955801649, 0.0023565267823206595, 0.0035347901734809895,
        0.0023565267823206595, 0.0005891316955801649]
AA_A = [1, -3.09905041635851, 3.6812793328016165, -1.9759888358976645, 0.40318602658384056]
AA_DECIM = 8
TRANSIENT_LIMIT_MULT = 4  # declick(): clip to this many robust-sigma of the whole window

# 3 bandpass filters at 500Hz (4th-order Butterworth), the 15-30Hz band this project also
# explored is NOT here -- corpus-validated worse, dropped 2026-07-14.
BANDS = [
    ([0.013359200027856502, 0, -0.026718400055713003, 0, 0.013359200027856502],
     [1, -3.2216975944168245, 4.259742833119654, -2.6933188730452877, 0.700896781188403]),      # 30-50Hz
    ([0.02785976611713605, 0, -0.0557195322342721, 0, 0.02785976611713605],
     [1, -2.4220309055301232, 2.962756451291472, -1.8462867837112926, 0.5869195080611904]),      # 50-80Hz
    ([0.06745527388907191, 0, -0.13491054777814382, 0, 0.06745527388907191],
     [1, -0.8218517544917526, 1.3177346226477535, -0.5147610573338093, 0.41280159809618866]),    # 80-130Hz
]
ENV_A = 0.7975628241529755   # single-pole envelope smoothing coefficient
FRAME_DS = 5                 # 500Hz -> 100Hz feature/frame rate
FFS = 100.0                  # feature frame rate (Hz)
LO_LAG, HI_LAG = 30, 150     # autocorrelation lag range in frames -> 40-200bpm
LAG_POW = 0.1                # lag-power reweighting exponent
FEAT_CLIP = 4.5              # winsorize clip, in z-score units

MULTIWIN_S = (4, 6, 8)       # multi-window family (seconds) feeding the rolling buffer

SIGNAL_ONSET_THRESH = 0.005
ONSET_OFFSET_DEBOUNCE_TICKS = 4
SPIKE_DETECT_MULT = 20

DECAY_ADAPTIVE_TAU_MIN, DECAY_ADAPTIVE_TAU_MAX = 1.0, 8.0

CONS_SAMPLE_S, CONS_BUFFER_N = 2.0, 11
STABILITY_MAD_THRESH = 12.0
CONF_MIN_THRESH = 1.05     # shipped value; a sweep found 1.10 a plausible tightening but it was
                            # never confirmed for shipping, so this stays at the validated default
CONS_WAIT_S, CONS_WAIT_EXTRA_S_PER_BPM = 4.0, 0.020


# ==================== stateless DSP ====================

def iir(b, a, x):
    return lfilter(b, a, np.asarray(x, dtype=np.float64))


def declick(x):
    if len(x) == 0:
        return x
    med = np.median(np.abs(x))
    lim = TRANSIENT_LIMIT_MULT * ((med * 1.4826) or 1e-6)
    return np.clip(x, -lim, lim)


def decimate(x):
    y = iir(AA_B, AA_A, x)
    m = len(y) // AA_DECIM
    return y[:m * AA_DECIM:AA_DECIM]


def env_singlepole(y):
    # p[n] = (1-a)*|y[n]| + a*p[n-1], exactly lfilter([1-a],[1,-a],|y|)
    return lfilter([1 - ENV_A], [1, -ENV_A], np.abs(y))


def feat_norm_clip(seg_500, band_idx, clip=FEAT_CLIP):
    y = iir(BANDS[band_idx][0], BANDS[band_idx][1], seg_500)
    e = env_singlepole(y)
    m = len(e) // FRAME_DS
    f = e[:m * FRAME_DS:FRAME_DS].copy()
    mean = f.mean()
    sd = f.std() or 1.0
    z = (f - mean) / sd
    return np.clip(z, -clip, clip)


def sum_env_norm(seg_500, clip=FEAT_CLIP):
    total = None
    for b in range(len(BANDS)):
        f = feat_norm_clip(seg_500, b, clip)
        total = f.copy() if total is None else total + f
    return total


def broad_env(seg_500):
    """Unnormalized summed envelope across the 3 bands, at the 100Hz frame rate -- used only for
    signal-level (onset/offset) detection, never fed into the bpm autocorrelation."""
    env = None
    for b in range(len(BANDS)):
        y = iir(BANDS[b][0], BANDS[b][1], seg_500)
        e = env_singlepole(y)
        m = len(e) // FRAME_DS
        ds = e[:m * FRAME_DS:FRAME_DS]
        env = ds.copy() if env is None else env + ds
    return env


def add_autocorr(S, f, lo, hi):
    T = len(f)
    if T <= hi:
        return
    r0 = (f * f).mean()
    for l in range(lo, hi):
        s = np.dot(f[:T - l], f[l:])
        S[l - lo] += (s / T) / (r0 + 1e-9)


def add_autocorr_decay(S, f, lo, hi, tau_frames):
    """Age-weighted autocorrelation: each product pair is weighted by how OLD its earlier sample
    is (exponential decay, time constant tau_frames) -- used only for the adaptive preview
    estimate that sizes the buffer's post-event wait timer."""
    T = len(f)
    if T <= hi:
        return
    idx = np.arange(T)
    w = np.exp(-(T - 1 - idx) / tau_frames)
    r0 = np.sum(w * f * f)
    r0w = np.sum(w)
    r0 = r0 / (r0w if r0w != 0 else 1.0)
    for l in range(lo, hi):
        s = np.sum(w[:T - l] * f[:T - l] * f[l:])
        sw = np.sum(w[:T - l])
        S[l - lo] += (s / (sw if sw != 0 else 1.0)) / (r0 + 1e-9)


def estimate_from_S(S, lag_pow=LAG_POW):
    if S is None:
        return None
    n = len(S)
    Sw = np.empty(n)
    for l in range(LO_LAG, HI_LAG):
        Sw[l - LO_LAG] = S[l - LO_LAG] / (l ** lag_pow)
    Sw -= Sw.min()
    pk = [i for i in range(1, n - 1) if Sw[i] >= Sw[i - 1] and Sw[i] > Sw[i + 1]]
    if not pk:
        return dict(bpm=60.0 / (LO_LAG / FFS), conf=1.0, lag=LO_LAG)
    pk.sort(key=lambda i: -Sw[i])
    p1 = pk[0]
    bl = LO_LAG + p1
    p2 = -1
    for k in range(1, len(pk)):
        if abs(pk[k] - p1) >= 8:
            p2 = pk[k]
            break
    if p2 < 0 and len(pk) > 1:
        p2 = pk[1]
    conf = 1.2 if p2 < 0 else Sw[p1] / (Sw[p2] + 1e-9)
    # lag (in frames, at FFS=100Hz) of the winning peak -- exposed as a diagnostic field. (A cycle-
    # count self-consistency check built from this, "does the window's own candidate imply enough
    # repetitions of itself," was tried and REJECTED 2026-07-18: it's backwards-biased, since a fast
    # candidate always implies more cycles-in-window than a slow one regardless of which is correct.)
    return dict(bpm=60.0 / (bl / FFS), conf=conf, lag=bl)


def estimate_sumenv(seg_500):
    """The shipped multi-window bpm estimator: 3 independently normalized/clipped envelopes
    summed, ONE autocorrelation."""
    T500 = len(seg_500)
    if T500 < 3 * 500:
        return None
    n = HI_LAG - LO_LAG
    S = np.zeros(n)
    add_autocorr(S, sum_env_norm(seg_500), LO_LAG, HI_LAG)
    return estimate_from_S(S)


def estimate_decay(seg_500, tau_s):
    """Adaptive-decay preview estimator: per-band autocorrelation (age-weighted, NOT sum-envelope
    first), summed across bands. Used only to size the rolling buffer's wait timer."""
    T500 = len(seg_500)
    if T500 < 3 * 500:
        return None
    n = HI_LAG - LO_LAG
    S = np.zeros(n)
    tau_frames = tau_s * FFS
    for b in range(len(BANDS)):
        f = feat_norm_clip(seg_500, b)
        add_autocorr_decay(S, f, LO_LAG, HI_LAG, tau_frames)
    return estimate_from_S(S)


def detect_spike_runs(x, mult=SPIKE_DETECT_MULT):
    """Robust median-based spike detector: flags contiguous runs of samples far above the
    window's own robust scale, one marker per run at that run's peak-amplitude sample."""
    if len(x) == 0:
        return []
    absx = np.abs(x)
    med = np.median(absx)
    lim = mult * ((med * 1.4826) or 1e-6)
    mask = absx > lim
    if not mask.any():
        return []
    idx = np.flatnonzero(mask)
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[0, breaks + 1]
    ends = np.r_[breaks, len(idx) - 1]
    runs = []
    for s, e in zip(starts, ends):
        seg_idx = idx[s:e + 1]
        runs.append(int(seg_idx[np.argmax(absx[seg_idx])]))
    return runs


def median(vals):
    v = sorted(x for x in vals if x is not None and np.isfinite(x))
    if not v:
        return None
    n = len(v)
    mid = n // 2
    return v[mid] if n % 2 else (v[mid - 1] + v[mid]) / 2.0


def window_spread_mad(vals):
    if len(vals) < 2:
        return None
    v = np.array(vals, dtype=float)
    med = np.median(v)
    mad = np.median(np.abs(v - med))
    return mad * 1.4826


# ==================== stability-weighted voting (EXPERIMENTAL, 2026-07-18) ====================
# Motivated by a corpus-confirmed failure mode: the 4s window is ~45x more likely to disagree with
# truth than the 8s window specifically at slow (<65bpm) true rates, because too few beat cycles
# fit in a 4s window there (~3.7 at 55bpm) for the true fundamental peak to reliably dominate a
# real, recurring competing peak near the S1-S2 (intra-beat) interval. Two earlier per-tick designs
# (a "does this window's own candidate imply enough cycles" self-consistency check; a peak-ratio-
# confidence-based tie-break) were tried and REJECTED after checking them against real tick data --
# both were either backwards-biased (systematically favoring fast candidates over slow ones,
# regardless of correctness) or relied on confidence, which was shown to be noisy enough on a single
# tick to rank a WRONG window above a RIGHT one regularly, even in an easy (non-adversarial) file.
#
# This design instead weights each window by ITS OWN recent output STABILITY (not its output value,
# and not confidence): in the slow-rate failure mode specifically, the 4s window's wrong answers are
# genuinely scattered tick-to-tick (thin statistics -> different competing peaks win by chance from
# one tick to the next), not locked onto one stable wrong value -- confirmed directly against real
# corpus data before implementing this (see conversation this was built from). This is DELIBERATELY
# scoped to exclude the one real-corpus case (a `_2spedup` artifact file) where a wrong answer was
# itself highly stable -- that file is being treated as an accepted, already-known-confound edge
# case, not something this mechanism is trying to solve.
STABILITY_HISTORY_N = 6      # ticks of per-window bpm history kept for the stability score
STABILITY_SCALE = 6.0        # sensitivity of stability to relative scatter -- swept, not derived
STABILITY_FLOOR = 0.15       # a window's weight never drops to exactly zero

# NOTE (2026-07-18): weighted_voting (above) was corpus-validated and REJECTED -- under the
# correct tick-by-tick metric (every tick an operator could see, not just the final tail value),
# it increased wrong-number-displayed percentage in every measured slice (e.g. real corpus 2.7%->
# 3.2%, synthetic 0-65bpm bucket 0.0%->2.0%) with zero reduction in no-display time anywhere. Left
# in the file, disabled by default, only as a documented negative result -- do not enable it.

# DROP-4S-STEADY-STATE + TRIPLING GUARD (2026-07-18): the mechanism that replaced weighted_voting
# as the leading approach, per Daniel's proposal. Two parts:
#  1. The 4s window is excluded from the ongoing multi-window vote once longer windows are
#     available (max_win>=6) -- NOT because of any per-tick signal (three per-tick signals were
#     tried and rejected: self-consistency cycle-count, ratio/confidence tie-break, stability
#     weighting), but as a simple, deterministic, time-based rule: stop asking the window that's
#     corpus-confirmed unreliable at slow rates once something more reliable is available. The 4s
#     window is STILL used during the brief initial ramp-up (max_win<6) so response time right
#     after a buffer clear isn't regressed.
#  2. TRIPLING GUARD: even though 4s is excluded from the normal vote, it's still computed every
#     tick, and specifically checked against the ORIGINAL shortwinCheck() failure direction this
#     project inherited from hr_live -- if the 6s/8s-based vote is reliably (over several
#     consecutive ticks, not one noisy tick) close to 3x what the 4s window is independently
#     reporting, the long windows have likely locked onto a resonance and 4s is likely right.
#     Deliberately narrower than the (REJECTED) generic ratio/confidence tie-break: it only fires
#     in the one specific, historically-validated direction (long windows too fast by 3x), not a
#     symmetric "any suspicious ratio" rule -- which was rejected specifically because that pattern
#     is directionally ambiguous (it looks identical whether the majority or the minority window is
#     correct). Requiring RELIABILITY (several consecutive ticks) rather than a single-tick match
#     is what makes this safe where the single-tick ratio/confidence tie-break wasn't.
TRIPLING_RATIO_TOL = 0.12     # tolerance band (fraction) for "close to 1/3"
TRIPLING_CONFIRM_TICKS = 5    # consecutive ticks the 1/3 relationship must hold before trusting it


def window_stability(history):
    """Bounded/saturating stability score in (STABILITY_FLOOR, 1.0], from a window's own recent
    bpm outputs -- 1.0 when they tightly agree with each other, decaying toward STABILITY_FLOOR as
    they scatter. MAD-based (robust to one wild reading), normalized by the window's own recent
    median bpm so the same few-bpm jitter counts for less at a fast rate than at a slow one.
    Returns 1.0 (full trust) when there isn't enough history yet -- consistent with this project's
    preference for not penalizing absence of evidence, only penalizing evidence of scatter."""
    if len(history) < 2:
        return 1.0
    arr = np.asarray(history, dtype=np.float64)
    med = np.median(arr)
    mad = np.median(np.abs(arr - med)) * 1.4826
    rel_mad = mad / max(med, 1.0)
    stability = 1.0 / (1.0 + STABILITY_SCALE * rel_mad)
    return max(STABILITY_FLOOR, stability)


def weighted_mean(vals, weights):
    w = np.asarray(weights, dtype=np.float64)
    v = np.asarray(vals, dtype=np.float64)
    return float(np.sum(v * w) / np.sum(w))


def weighted_spread(vals, weights):
    """Weighted analog of window_spread_mad: a weighted-median absolute deviation, scaled to match
    a standard deviation for Gaussian-ish data (1.4826), same convention as the unweighted version."""
    if len(vals) < 2:
        return None
    v = np.asarray(vals, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    wmed = weighted_median(vals, weights)
    dev = np.abs(v - wmed)
    wmad = weighted_median(dev.tolist(), weights.tolist() if hasattr(weights, "tolist") else weights)
    return wmad * 1.4826


def weighted_median(vals, weights):
    pairs = sorted(zip(vals, weights), key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        return float(np.median(vals))
    cum = 0.0
    half = total / 2.0
    for v, w in pairs:
        cum += w
        if cum >= half:
            return float(v)
    return float(pairs[-1][0])


# ==================== streaming estimator ====================

class PCGHeartRateEstimator:
    """Stateful, tick-driven port of the shipped Rolling Median algorithm. Caller pushes raw
    audio samples as they arrive and calls tick() once per TICK_S (250ms) of new audio; tick()
    returns the current display value and internal state for that instant.

    NOTE: each tick recomputes declick/decimate/bandpass/envelope/autocorrelation from scratch
    over the trailing 8s buffer (exactly what was corpus-validated) -- no incremental/sliding-
    window optimization has been done. That is the single biggest remaining embedded-cost lever
    (see ALGORITHM_REPORT.md section 4) and was NOT attempted here.

    weighted_voting (EXPERIMENTAL, 2026-07-18, default False = exactly today's shipped behavior):
    replaces the plain median/MAD-spread used to form the multi-window consensus with a version
    weighted by each window's own recent output STABILITY (see window_stability() above). Motivated
    by the same corpus-confirmed failure mode as the (REJECTED, see git history / conversation)
    min_cycles_in_window attempt -- the 4s window disagrees with truth ~45x more often than 8s
    specifically at slow (<65bpm) true rates, because too few beat cycles fit in 4s there for the
    true fundamental to reliably dominate a real, recurring competing peak near the S1-S2 interval.
    Two earlier per-tick designs were tried and rejected after checking them against real tick data:
    a self-consistency check on each window's own candidate lag (backwards-biased -- systematically
    favored fast candidates over slow ones regardless of correctness) and a confidence-based
    tie-break (confidence alone was shown to rank a WRONG window above a RIGHT one regularly, even
    in a non-adversarial file). This design instead weights by STABILITY specifically because, in
    the slow-rate failure mode, the 4s window's wrong answers are genuinely scattered tick-to-tick
    (thin statistics -> a different competing peak can win by chance each tick) rather than locked
    onto one stable wrong value -- confirmed directly against real corpus data before implementing.
    Deliberately scoped to exclude the one real-corpus case found where a WRONG answer was itself
    highly stable (a `_2spedup` artifact file) -- that's being treated as an accepted, already-known
    confound, not something this mechanism is trying to solve. REJECTED, see note above -- do not
    enable.

    drop_4s_steady_state (EXPERIMENTAL, 2026-07-18, default False = today's shipped behavior):
    excludes the 4s window from the ongoing vote once max_win>=6, but retains a narrow, direction-
    specific tripling guard using 4s. See the module-level comment above for the full reasoning."""

    def __init__(self, fs=FS_IN, weighted_voting=False, drop_4s_steady_state=False, multiwin_s=None):
        self.fs = fs
        self.weighted_voting = weighted_voting
        self.drop_4s_steady_state = drop_4s_steady_state
        # multiwin_s override (EXPERIMENTAL, 2026-07-18): lets a caller test an alternate window
        # family (e.g. (5,6,8) instead of the shipped (4,6,8)) without touching any of the drop-4s/
        # weighted-voting logic above -- defaults to MULTIWIN_S, i.e. exactly today's shipped family.
        self.multiwin_s = tuple(multiwin_s) if multiwin_s is not None else MULTIWIN_S
        self._raw = np.zeros(0, dtype=np.float64)
        self.el = 0.0  # elapsed time (s), advances by TICK_S per tick() call

        self.sig_was_onset_ok = False
        self.onset_off_streak = 0
        self.last_onset_el = None
        self.last_spike_el = None
        self.last_logged_spike_t = -np.inf

        self.cons_buf = []
        self.cons_last_sample_el = None
        self.prev_last_event_el = -np.inf
        self.frozen_wait_s = CONS_WAIT_S
        self.window_history = {s: [] for s in self.multiwin_s}  # per-window recent bpm, for stability
        self.tripling_streak = 0

    def push(self, samples):
        self._raw = np.concatenate([self._raw, np.asarray(samples, dtype=np.float64)])
        max_len = int(WIN_S * self.fs)
        if len(self._raw) > max_len:
            self._raw = self._raw[-max_len:]

    def tick(self):
        el = self.el
        raw = self._raw

        # 1. spike detection on the RAW (pre-declick) window -- event timing only.
        spike_runs = detect_spike_runs(raw) if len(raw) else []
        win_start_el = el - len(raw) / self.fs
        spike_abs_times = [win_start_el + i / self.fs for i in spike_runs]
        if spike_abs_times:
            max_t = max(spike_abs_times)
            if max_t > self.last_logged_spike_t + 0.01:
                self.last_spike_el = max_t
                self.last_logged_spike_t = max_t

        # 2-3. declick + decimate to 500Hz
        raw_clean = declick(raw) if len(raw) else raw
        seg_clean = decimate(raw_clean) if len(raw_clean) else np.array([])

        # 4. signal level (last 1.5s of broadband envelope) -> onset debounce
        sig_level = 0.0
        if len(seg_clean):
            env = broad_env(seg_clean)
            n = min(len(env), round(1.5 * FFS))
            sig_level = float(np.sqrt(np.mean(env[-n:] ** 2))) if n > 0 else 0.0
        is_flatline = sig_level < SIGNAL_ONSET_THRESH

        above = sig_level >= SIGNAL_ONSET_THRESH
        if above:
            self.onset_off_streak = 0
            if not self.sig_was_onset_ok:
                self.last_onset_el = el
                self.sig_was_onset_ok = True
        else:
            self.onset_off_streak += 1
            if self.onset_off_streak >= ONSET_OFFSET_DEBOUNCE_TICKS:
                self.sig_was_onset_ok = False

        last_event_el = max(self.last_onset_el if self.last_onset_el is not None else -np.inf,
                             self.last_spike_el if self.last_spike_el is not None else -np.inf)
        since_event = el - last_event_el

        # 5. adaptive-decay preview estimate -> sizes the post-event wait timer only
        adaptive_tau = min(DECAY_ADAPTIVE_TAU_MAX, max(DECAY_ADAPTIVE_TAU_MIN, since_event))
        r_adaptive = estimate_decay(seg_clean, adaptive_tau) if len(seg_clean) else None
        r_adaptive_bpm = r_adaptive["bpm"] if r_adaptive else None

        # 6-7. multi-window sum-envelope bpm estimate
        multi_wins = []
        for s in self.multiwin_s:
            n_samp = round(s * 500)
            r = estimate_sumenv(seg_clean[-n_samp:]) if len(seg_clean) >= n_samp else None
            multi_wins.append(dict(sec=s, bpm=(r["bpm"] if r else None), conf=(r["conf"] if r else None)))

        # per-window bpm history, for the stability-weighted voting option (see window_stability())
        for mw in multi_wins:
            if mw["bpm"] is not None:
                hist = self.window_history[mw["sec"]]
                hist.append(mw["bpm"])
                if len(hist) > STABILITY_HISTORY_N:
                    hist.pop(0)

        # 8. rolling buffer state machine
        if last_event_el != self.prev_last_event_el:
            self.cons_buf = []
            self.cons_last_sample_el = None
            self.prev_last_event_el = last_event_el
            self.frozen_wait_s = CONS_WAIT_S + (
                CONS_WAIT_EXTRA_S_PER_BPM * abs(r_adaptive_bpm - 60) if r_adaptive_bpm is not None else 0.0)

        tripling_corrected = False
        if is_flatline:
            self.cons_buf = []
            self.cons_last_sample_el = None
            shown = None
            state = "flatline"
        elif since_event < self.frozen_wait_s:
            shown = median(self.cons_buf)
            state = "waiting"
        else:
            max_win = min(8, 3 + int(np.floor(since_event - self.frozen_wait_s)))
            drop_4s = self.drop_4s_steady_state and max_win >= 6
            vote_wins = [mw for mw in multi_wins if mw["sec"] >= 6] if drop_4s else multi_wins
            included = [mw for mw in vote_wins if mw["sec"] <= max_win and mw["bpm"] is not None]
            vals = [mw["bpm"] for mw in included]
            confs = [mw["conf"] for mw in included]
            min_conf = min(confs) if confs else None
            if self.weighted_voting and included:
                weights = [window_stability(self.window_history[mw["sec"]]) for mw in included]
                spread = weighted_spread(vals, weights)
                push_val = weighted_mean(vals, weights)
            else:
                weights = None
                spread = window_spread_mad(vals)
                push_val = median(vals)
            spread_bad = spread is not None and spread > STABILITY_MAD_THRESH
            conf_bad = min_conf is not None and min_conf < CONF_MIN_THRESH
            unstable = spread_bad or conf_bad

            # TRIPLING GUARD -- only relevant once 4s has been excluded from the normal vote above.
            if drop_4s:
                mw4 = next((mw for mw in multi_wins if mw["sec"] == 4), None)
                bpm_4s = mw4["bpm"] if mw4 else None
                conf_4s = mw4["conf"] if mw4 else None
                if (bpm_4s is not None and conf_4s is not None and conf_4s >= CONF_MIN_THRESH
                        and push_val is not None
                        and abs(bpm_4s - push_val / 3.0) <= TRIPLING_RATIO_TOL * (push_val / 3.0)):
                    self.tripling_streak += 1
                else:
                    self.tripling_streak = 0
                if self.tripling_streak >= TRIPLING_CONFIRM_TICKS:
                    tripling_corrected = True
                    push_val = bpm_4s
                    unstable = False
            else:
                self.tripling_streak = 0

            if vals and not unstable:
                if self.cons_last_sample_el is None or el - self.cons_last_sample_el >= CONS_SAMPLE_S:
                    self.cons_last_sample_el = el
                    self.cons_buf.append(push_val)
                    if len(self.cons_buf) > CONS_BUFFER_N:
                        self.cons_buf.pop(0)
            shown = median(self.cons_buf)
            state = "unstable-skip" if unstable else "active"

        self.el += TICK_S
        return dict(t=el, shown=shown, state=state, sig_level=sig_level, multi_wins=multi_wins,
                    tripling_corrected=tripling_corrected)


if __name__ == "__main__":
    import sys
    from scipy.io import wavfile

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    fs, data = wavfile.read(sys.argv[1])
    data = data.mean(axis=1) if data.ndim > 1 else data
    data = data.astype(np.float64)

    est = PCGHeartRateEstimator(fs=fs)
    chunk = int(TICK_S * fs)
    for i in range(0, len(data), chunk):
        est.push(data[i:i + chunk])
        r = est.tick()
        shown_s = f"{r['shown']:.1f}" if r["shown"] is not None else "--"
        print(f"t={r['t']:6.2f}s  shown={shown_s:>7}  ({r['state']})")
