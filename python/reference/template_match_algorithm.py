#!/usr/bin/env python3
"""
S1/S2 template-match HR estimator -- faithful Python port of the experimental method built and
iterated on live in pcg_visualizer/index.html (panels 4/4a/4b, 2026-07-18 session): instead of
picking the tallest autocorrelation peak, sweep a fixed grid of candidate heart rates, build a
synthetic S1+S2 pulse-pair template at each candidate's implied systole duration (from
expected_systole_frames -- systole shortens only mildly with heart rate, diastole does almost all
the shortening), and score each by the AVERAGE of the real combined signal sampled at every
predicted mark time (searched over every phase offset). This replaces an earlier cosine-similarity
version (also tried live, also rejected) that had a structural bias favoring denser/faster
candidates -- see the "REBUILT 2026-07-18" comments in index.html's templateMatchScore for the
derivation. Averaging per-mark instead of sum-then-normalize removes that bias.

This module ONLY swaps out the multi-window bpm ESTIMATOR (estimate_sumenv -> here) -- every other
part of the shipped pipeline (declick, decimate, onset/flatline gating, adaptive-decay wait-timer
sizing, rolling-buffer subsample/median, spread+confidence stability gate) is reused UNCHANGED from
pcg_algorithm.py, so a tick-by-tick comparison against the shipped baseline isolates the effect of
this one change rather than conflating it with any other pipeline difference.

Usage (mirrors pcg_algorithm.py):
  est = TemplateMatchEstimator(fs=4000)
  while <new audio available>:
      est.push(new_samples)
      if <250ms of new audio has accumulated>:
          result = est.tick()
"""
import numpy as np
from scipy.ndimage import maximum_filter1d
import pcg_algorithm as P

# ---------------- candidate grid + systole model (identical to index.html's constants) ----------------
TEMPLATE_BPM_LO, TEMPLATE_BPM_HI, TEMPLATE_BPM_STEP = 40, 190, 5
TEMPLATE_BPMS = list(range(TEMPLATE_BPM_LO, TEMPLATE_BPM_HI + 1, TEMPLATE_BPM_STEP))  # 31 candidates

# REBUILT 2026-07-18: was a near-constant-with-floor approximation (350ms at 60bpm, shrinking only
# 0.8ms/bpm) -- caught live to be up to 140ms wrong at high rates against this project's OWN
# synthetic corpus (synth_pcg.py's systole_frac=0.333, a fixed FRACTION of the cycle). Switched to
# match that fraction directly, since that's the one dataset we have ground truth against; the real
# corpus's true relationship is probably somewhere between this and the old approximation.
SYSTOLE_FRAC = 0.333
SYSTOLE_MS_MIN, SYSTOLE_MS_MAX = 60.0, 500.0

MARK_TOL_FRAMES = 3   # frames (~30ms @ FFS=100Hz) -- local-max tolerance per predicted mark

# SHRINKAGE_K (added 2026-07-18): caught live on a 175bpm synthetic file locking to 45-50bpm instead
# -- a sparse (slow) candidate only needs a HANDFUL of marks to get lucky against a fast, densely-
# peaked true signal, while the true (many-marked) candidate has to keep dozens of marks honest.
# Laplace/empirical-Bayes fix: blend SHRINKAGE_K "virtual marks" at the signal's own overall mean
# into each candidate's average before scoring -- barely moves a real candidate's large-count
# average, pulls a sparse candidate's average hard toward baseline unless genuinely elevated.
SHRINKAGE_K = 8.0

# HARMONIC GUARD (added 2026-07-18): an exact-integer-multiple candidate (2x/3x/4x) is structurally
# hard to reject from S1 timing alone -- its S1 marks are a genuine subset of real S1 positions, not
# a lucky sample, so neither fix above catches it. Confirmed live: a 195bpm file's own candidate
# scored LOWER than its exact 1/3-subharmonic (65bpm) even with both fixes in place. Mirrors the
# shipped tripling guard's spirit: after the grid argmax, also score the winner's 2x/3x/4x multiples
# directly (reaching beyond the 190bpm grid top, e.g. to 195/200bpm) and switch to whichever
# actually scores highest.
HARMONIC_GUARD_MULTIPLES = (2, 3, 4)
HARMONIC_GUARD_MAX_BPM = 200.0


def expected_systole_frames(bpm):
    cycle_ms = 60000.0 / bpm
    ms = max(SYSTOLE_MS_MIN, min(SYSTOLE_MS_MAX, SYSTOLE_FRAC * cycle_ms))
    return ms / 10.0   # 10ms/frame @ P.FFS=100Hz


def template_match_score(combined, cmax, L, tau):
    """Per-mark SHRUNK-average match score for one candidate period L (frames) + systole offset
    tau (frames), searched over every phase offset, returns the BEST phase's score. `cmax` is the
    caller-precomputed local-max envelope of `combined` (maximum_filter1d, width
    2*MARK_TOL_FRAMES+1) -- shared across all candidates/phases in one call to
    estimate_template_match so it's computed once per tick, not once per candidate."""
    N = len(combined)
    Li = int(round(L))
    if Li < 4 or Li > N:
        return 0.0
    scale = combined.max()
    if scale <= 0:
        return 0.0
    baseline = combined.mean()
    tau_r = int(round(tau))
    base = np.arange(0, N, Li)
    best = 0.0
    for phi in range(Li):
        idx1 = base + phi
        idx1 = idx1[idx1 < N]
        if len(idx1) == 0:
            continue
        idx2 = idx1 + tau_r
        idx2 = idx2[idx2 < N]
        vals = cmax[idx1] if len(idx2) == 0 else np.concatenate([cmax[idx1], cmax[idx2]])
        shrunk = (vals.sum() + SHRINKAGE_K * baseline) / (len(vals) + SHRINKAGE_K)
        score = shrunk / scale
        if score > best:
            best = score
    return float(best)


def _score_at_bpm(combined, cmax, bpm):
    return template_match_score(combined, cmax, 60.0 * P.FFS / bpm, expected_systole_frames(bpm))


def apply_harmonic_guard(bpm, score, combined, cmax):
    best_bpm, best_score = bpm, score
    for m in HARMONIC_GUARD_MULTIPLES:
        cand = bpm * m
        if cand > HARMONIC_GUARD_MAX_BPM:
            break
        s = _score_at_bpm(combined, cmax, cand)
        if s > best_score:
            best_score, best_bpm = s, cand
    return best_bpm, best_score


def estimate_template_match(seg_500):
    """S1/S2 template-match bpm estimate: sweep TEMPLATE_BPMS, score each via
    template_match_score against sum_env_norm(seg_500), argmax + harmonic guard, then a confidence
    (top score / next DISTINCT-candidate score, >=8bpm apart -- same p1/p2 convention
    estimate_from_S uses)."""
    T500 = len(seg_500)
    if T500 < 3 * 500:
        return None
    combined = P.sum_env_norm(seg_500)
    cmax = maximum_filter1d(combined, size=2 * MARK_TOL_FRAMES + 1, mode="nearest")
    scores = np.array([_score_at_bpm(combined, cmax, bpm) for bpm in TEMPLATE_BPMS])
    order = np.argsort(-scores)
    p1 = int(order[0])
    bpm1 = TEMPLATE_BPMS[p1]
    p2 = -1
    for k in order[1:]:
        if abs(TEMPLATE_BPMS[int(k)] - bpm1) >= 8:
            p2 = int(k)
            break
    conf = 1.2 if p2 < 0 else float(scores[p1] / (scores[p2] + 1e-9))
    guarded_bpm, guarded_score = apply_harmonic_guard(bpm1, scores[p1], combined, cmax)
    return dict(bpm=float(guarded_bpm), conf=conf)


class TemplateMatchEstimator(P.PCGHeartRateEstimator):
    """Identical to P.PCGHeartRateEstimator except multi_wins is built from
    estimate_template_match() instead of estimate_sumenv() -- every other mechanism (onset/
    flatline gating, adaptive-decay wait sizing, rolling-buffer subsample/median, spread+
    confidence gate, tripling guard/weighted-voting options) is inherited unchanged. Overrides
    tick() by copy (not by decomposing the parent into hooks) -- matches this project's existing
    convention of duplicating estimator variants wholesale rather than threading a strategy
    pattern through the shipped class (see e.g. the DECAY_OLD_VARIANTS / contamination-strategy
    comments in pcg_visualizer/index.html for the precedent)."""

    def tick(self):
        el = self.el
        raw = self._raw

        spike_runs = P.detect_spike_runs(raw) if len(raw) else []
        win_start_el = el - len(raw) / self.fs
        spike_abs_times = [win_start_el + i / self.fs for i in spike_runs]
        if spike_abs_times:
            max_t = max(spike_abs_times)
            if max_t > self.last_logged_spike_t + 0.01:
                self.last_spike_el = max_t
                self.last_logged_spike_t = max_t

        raw_clean = P.declick(raw) if len(raw) else raw
        seg_clean = P.decimate(raw_clean) if len(raw_clean) else np.array([])

        sig_level = 0.0
        if len(seg_clean):
            env = P.broad_env(seg_clean)
            n = min(len(env), round(1.5 * P.FFS))
            sig_level = float(np.sqrt(np.mean(env[-n:] ** 2))) if n > 0 else 0.0
        is_flatline = sig_level < P.SIGNAL_ONSET_THRESH

        above = sig_level >= P.SIGNAL_ONSET_THRESH
        if above:
            self.onset_off_streak = 0
            if not self.sig_was_onset_ok:
                self.last_onset_el = el
                self.sig_was_onset_ok = True
        else:
            self.onset_off_streak += 1
            if self.onset_off_streak >= P.ONSET_OFFSET_DEBOUNCE_TICKS:
                self.sig_was_onset_ok = False

        last_event_el = max(self.last_onset_el if self.last_onset_el is not None else -np.inf,
                             self.last_spike_el if self.last_spike_el is not None else -np.inf)
        since_event = el - last_event_el

        adaptive_tau = min(P.DECAY_ADAPTIVE_TAU_MAX, max(P.DECAY_ADAPTIVE_TAU_MIN, since_event))
        r_adaptive = P.estimate_decay(seg_clean, adaptive_tau) if len(seg_clean) else None
        r_adaptive_bpm = r_adaptive["bpm"] if r_adaptive else None

        # ---- THE ONE CHANGE: template-match instead of sum-envelope autocorrelation ----
        multi_wins = []
        for s in self.multiwin_s:
            n_samp = round(s * 500)
            r = estimate_template_match(seg_clean[-n_samp:]) if len(seg_clean) >= n_samp else None
            multi_wins.append(dict(sec=s, bpm=(r["bpm"] if r else None), conf=(r["conf"] if r else None)))

        for mw in multi_wins:
            if mw["bpm"] is not None:
                hist = self.window_history[mw["sec"]]
                hist.append(mw["bpm"])
                if len(hist) > P.STABILITY_HISTORY_N:
                    hist.pop(0)

        if last_event_el != self.prev_last_event_el:
            self.cons_buf = []
            self.cons_last_sample_el = None
            self.prev_last_event_el = last_event_el
            self.frozen_wait_s = P.CONS_WAIT_S + (
                P.CONS_WAIT_EXTRA_S_PER_BPM * abs(r_adaptive_bpm - 60) if r_adaptive_bpm is not None else 0.0)

        tripling_corrected = False
        if is_flatline:
            self.cons_buf = []
            self.cons_last_sample_el = None
            shown = None
            state = "flatline"
        elif since_event < self.frozen_wait_s:
            shown = P.median(self.cons_buf)
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
                weights = [P.window_stability(self.window_history[mw["sec"]]) for mw in included]
                spread = P.weighted_spread(vals, weights)
                push_val = P.weighted_mean(vals, weights)
            else:
                spread = P.window_spread_mad(vals)
                push_val = P.median(vals)
            spread_bad = spread is not None and spread > P.STABILITY_MAD_THRESH
            conf_bad = min_conf is not None and min_conf < P.CONF_MIN_THRESH
            unstable = spread_bad or conf_bad

            if drop_4s:
                mw4 = next((mw for mw in multi_wins if mw["sec"] == 4), None)
                bpm_4s = mw4["bpm"] if mw4 else None
                conf_4s = mw4["conf"] if mw4 else None
                if (bpm_4s is not None and conf_4s is not None and conf_4s >= P.CONF_MIN_THRESH
                        and push_val is not None
                        and abs(bpm_4s - push_val / 3.0) <= P.TRIPLING_RATIO_TOL * (push_val / 3.0)):
                    self.tripling_streak += 1
                else:
                    self.tripling_streak = 0
                if self.tripling_streak >= P.TRIPLING_CONFIRM_TICKS:
                    tripling_corrected = True
                    push_val = bpm_4s
                    unstable = False
            else:
                self.tripling_streak = 0

            if vals and not unstable:
                if self.cons_last_sample_el is None or el - self.cons_last_sample_el >= P.CONS_SAMPLE_S:
                    self.cons_last_sample_el = el
                    self.cons_buf.append(push_val)
                    if len(self.cons_buf) > P.CONS_BUFFER_N:
                        self.cons_buf.pop(0)
            shown = P.median(self.cons_buf)
            state = "unstable-skip" if unstable else "active"

        self.el += P.TICK_S
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

    est = TemplateMatchEstimator(fs=fs)
    chunk = int(P.TICK_S * fs)
    for i in range(0, len(data), chunk):
        est.push(data[i:i + chunk])
        r = est.tick()
        shown_s = f"{r['shown']:.1f}" if r["shown"] is not None else "--"
        print(f"t={r['t']:6.2f}s  shown={shown_s:>7}  ({r['state']})")
