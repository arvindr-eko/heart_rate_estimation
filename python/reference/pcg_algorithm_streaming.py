#!/usr/bin/env python3
"""
STREAMING variant of pcg_algorithm.py's PCGHeartRateEstimator ("Option 1" from the embedded
porting-strategy decision, 2026-07-17) -- built for a head-to-head corpus/synthetic comparison
against the exact-recompute reference (pcg_algorithm.py) before anything is trusted on hardware.

Same shipped math throughout (autocorrelation, lag-power peak-pick/confidence, rolling-buffer
state machine) -- ONLY the DSP front end changes, replacing "recompute declick/decimate/bandpass/
envelope from scratch over the trailing window every tick" with continuous-state filtering that
processes each new TICK_S-worth of raw audio exactly once:

  - Anti-alias decimate (4000->500Hz) and the 3 bandpass + 3 envelope filters carry scipy.signal
    lfilter state (zi/zf) across ticks instead of being re-run from a zero initial condition over
    whatever window length is being analyzed. This removes the repeated per-window cold-start
    filter transient the exact version has at the start of every 4/6/8s slice it re-filters each
    tick -- arguably better (no artificial transient contribution to a window's z-score stats),
    but a real, unvalidated-until-now behavioral difference. This is DEVIATION 1.
  - declick() becomes causal: instead of clipping to 4x the CURRENT window's whole-window robust
    median (which needs the whole window in hand), it clips to 4x a running EMA-based robust-scale
    estimate that updates from its own CLIPPED output (same design as gridfit_sanity.py's
    declick_local(), explored 2026-07-15 for a different reason -- ringing after an impulse -- and
    reused here verbatim because it's already a considered, documented mechanism in this codebase).
    This is DEVIATION 2.
  - Everything downstream of the per-band 100Hz envelope stream (z-score/winsorize a window slice,
    sum bands, autocorrelate, lag-power peak-pick, confidence, rolling buffer) is UNCHANGED shipped
    math, just fed from a maintained 100Hz ring buffer per band instead of a freshly re-filtered
    slice. Spike detection is also left UNCHANGED (still a real whole-window median recompute, over
    a maintained 8s raw ring) -- it was not one of the two named streaming deviations, so its
    behavior is preserved exactly rather than quietly approximated too.

Assumes fs=4000Hz and TICK_S=0.25s (1000 raw samples/tick) so every decimation stage divides
evenly with zero cross-tick phase bookkeeping: 1000/AA_DECIM(8)=125 500Hz samples/tick,
125/FRAME_DS(5)=25 100Hz feature frames/tick/band. Both hold for every file in the real corpus
(confirmed 4000Hz-only elsewhere in this project) and the synthetic set.

NOT optimized for speed in this Python form (the causal declick loop in particular is a plain
per-sample Python loop) -- this is a correctness study to decide whether the streaming design's
two deviations are safe to ship, not a benchmark of the eventual C port's actual speed.

Usage: python pcg_algorithm_streaming.py <wav_path>
       python pcg_algorithm_streaming.py --compare <wav_path>   # side-by-side vs exact reference
"""
import collections
import numpy as np
from scipy.signal import lfilter

import pcg_algorithm as EXACT
from pcg_algorithm import (FS_IN, WIN_S, TICK_S, AA_B, AA_A, AA_DECIM, TRANSIENT_LIMIT_MULT,
                            BANDS, ENV_A, FRAME_DS, FFS, LO_LAG, HI_LAG, LAG_POW, FEAT_CLIP,
                            MULTIWIN_S, SIGNAL_ONSET_THRESH, ONSET_OFFSET_DEBOUNCE_TICKS,
                            SPIKE_DETECT_MULT, DECAY_ADAPTIVE_TAU_MIN, DECAY_ADAPTIVE_TAU_MAX,
                            CONS_SAMPLE_S, CONS_BUFFER_N, STABILITY_MAD_THRESH, CONF_MIN_THRESH,
                            CONS_WAIT_S, CONS_WAIT_EXTRA_S_PER_BPM,
                            add_autocorr, add_autocorr_decay, estimate_from_S, detect_spike_runs,
                            median, window_spread_mad)

# causal-declick constants, ported verbatim from gridfit_sanity.py's declick_local() (2026-07-15)
DECLICK_TAU_S = 1.5
DECLICK_FLOOR_MULT = 0.1


def zscore_clip(arr, clip=FEAT_CLIP):
    """Same tail as feat_norm_clip: mean/std z-score + winsorize -- just applied to an
    already-computed envelope array (a ring slice) instead of recomputing bandpass+envelope."""
    arr = np.asarray(arr, dtype=np.float64)
    mean = arr.mean()
    sd = arr.std() or 1.0
    return np.clip((arr - mean) / sd, -clip, clip)


class StreamingPCGHeartRateEstimator:
    """Streaming ("Option 1") port. See module docstring for exactly what changed vs.
    pcg_algorithm.PCGHeartRateEstimator and why."""

    def __init__(self, fs=FS_IN):
        if fs != FS_IN:
            raise ValueError("streaming port assumes 4000Hz input, matching the real corpus/synthetic set")
        self.fs = fs
        self.el = 0.0
        self.chunk_raw = int(round(TICK_S * fs))
        assert self.chunk_raw % AA_DECIM == 0
        assert (self.chunk_raw // AA_DECIM) % FRAME_DS == 0

        # DEVIATION 2: causal declick state
        self.declick_env = None
        self.declick_floor = None
        self.declick_alpha = 1.0 / (fs * DECLICK_TAU_S)

        # DEVIATION 1: continuous filter state (never reset across ticks)
        self.zi_aa = np.zeros(max(len(AA_A), len(AA_B)) - 1)
        self.band_zi_bp = [np.zeros(max(len(a), len(b)) - 1) for b, a in BANDS]
        self.band_zi_env = [np.zeros(1) for _ in BANDS]

        ring_len = int(round(WIN_S * FFS))  # 800 frames = 8s at 100Hz
        self.band_ring = [collections.deque(maxlen=ring_len) for _ in BANDS]

        # spike detection: UNCHANGED whole-window median, over a maintained 8s raw ring (not one
        # of the two accepted deviations -- kept behaviorally identical to the exact version)
        self.raw_ring = collections.deque(maxlen=int(round(WIN_S * fs)))

        self.sig_was_onset_ok = False
        self.onset_off_streak = 0
        self.last_onset_el = None
        self.last_spike_el = None
        self.last_logged_spike_t = -np.inf

        self.cons_buf = []
        self.cons_last_sample_el = None
        self.prev_last_event_el = -np.inf
        self.frozen_wait_s = CONS_WAIT_S

        self._pending_raw = np.zeros(0, dtype=np.float64)

    def push(self, samples):
        self._pending_raw = np.concatenate([self._pending_raw, np.asarray(samples, dtype=np.float64)])

    def _causal_declick(self, x):
        n = len(x)
        out = np.empty(n)
        if self.declick_env is None:
            seed = (np.median(np.abs(x)) * 1.4826) or 1e-6
            self.declick_env = seed
            self.declick_floor = DECLICK_FLOOR_MULT * seed
        env = self.declick_env
        floor = self.declick_floor
        alpha = self.declick_alpha
        mult = TRANSIENT_LIMIT_MULT
        for i in range(n):
            lim = mult * max(env, floor)
            v = x[i]
            c = lim if v > lim else (-lim if v < -lim else v)
            out[i] = c
            env = (1 - alpha) * env + alpha * abs(c)
        self.declick_env = env
        return out

    def tick(self):
        el = self.el
        chunk = self._pending_raw[:self.chunk_raw]
        self._pending_raw = self._pending_raw[self.chunk_raw:]

        if len(chunk) < self.chunk_raw:
            self.el += TICK_S
            return dict(t=el, shown=median(self.cons_buf), state="flatline", sig_level=0.0, multi_wins=[])

        # 1. spike detection (unchanged whole-window median, maintained raw ring)
        self.raw_ring.extend(chunk.tolist())
        raw_win = np.asarray(self.raw_ring)
        spike_runs = detect_spike_runs(raw_win, SPIKE_DETECT_MULT)
        win_start_el = (el + TICK_S) - len(raw_win) / self.fs
        spike_abs_times = [win_start_el + i / self.fs for i in spike_runs]
        if spike_abs_times:
            max_t = max(spike_abs_times)
            if max_t > self.last_logged_spike_t + 0.01:
                self.last_spike_el = max_t
                self.last_logged_spike_t = max_t

        # 2. causal declick (DEVIATION 2)
        raw_clean = self._causal_declick(chunk)

        # 3. continuous AA filter + stride decimate (DEVIATION 1, state carried across ticks)
        y, self.zi_aa = lfilter(AA_B, AA_A, raw_clean, zi=self.zi_aa)
        seg_new = y[::AA_DECIM]  # 125 new 500Hz samples this tick

        # per-band: continuous bandpass + envelope (DEVIATION 1), frame-decimate into rings
        for b in range(len(BANDS)):
            yb, self.band_zi_bp[b] = lfilter(BANDS[b][0], BANDS[b][1], seg_new, zi=self.band_zi_bp[b])
            eb, self.band_zi_env[b] = lfilter([1 - ENV_A], [1, -ENV_A], np.abs(yb), zi=self.band_zi_env[b])
            frames_new = eb[::FRAME_DS]  # 25 new 100Hz frames this tick
            self.band_ring[b].extend(frames_new.tolist())

        ring_len_avail = len(self.band_ring[0])

        # 4. signal level: sum of last 1.5s of the (unnormalized) per-band envelope rings
        n_sig = min(ring_len_avail, round(1.5 * FFS))
        if n_sig > 0:
            summed = np.zeros(n_sig)
            for b in range(len(BANDS)):
                summed += np.asarray(self.band_ring[b])[-n_sig:]
            sig_level = float(np.sqrt(np.mean(summed ** 2)))
        else:
            sig_level = 0.0
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

        # 5. adaptive-decay preview estimate (per-band, age-weighted), fed from the FULL ring
        r_adaptive_bpm = None
        if ring_len_avail >= 3 * FFS:
            adaptive_tau = min(DECAY_ADAPTIVE_TAU_MAX, max(DECAY_ADAPTIVE_TAU_MIN, since_event))
            tau_frames = adaptive_tau * FFS
            S = np.zeros(HI_LAG - LO_LAG)
            for b in range(len(BANDS)):
                f = zscore_clip(np.asarray(self.band_ring[b]))
                add_autocorr_decay(S, f, LO_LAG, HI_LAG, tau_frames)
            r_adaptive = estimate_from_S(S, LAG_POW)
            r_adaptive_bpm = r_adaptive["bpm"] if r_adaptive else None

        # 6-7. multi-window sum-envelope bpm estimate, sliced from the maintained rings
        multi_wins = []
        for s in MULTIWIN_S:
            n_samp = int(round(s * FFS))
            if ring_len_avail >= n_samp:
                total = None
                for b in range(len(BANDS)):
                    f = zscore_clip(np.asarray(self.band_ring[b])[-n_samp:])
                    total = f.copy() if total is None else total + f
                S = np.zeros(HI_LAG - LO_LAG)
                add_autocorr(S, total, LO_LAG, HI_LAG)
                r = estimate_from_S(S, LAG_POW)
            else:
                r = None
            multi_wins.append(dict(sec=s, bpm=(r["bpm"] if r else None), conf=(r["conf"] if r else None)))

        # 8. rolling buffer state machine -- IDENTICAL to the exact version
        if last_event_el != self.prev_last_event_el:
            self.cons_buf = []
            self.cons_last_sample_el = None
            self.prev_last_event_el = last_event_el
            self.frozen_wait_s = CONS_WAIT_S + (
                CONS_WAIT_EXTRA_S_PER_BPM * abs(r_adaptive_bpm - 60) if r_adaptive_bpm is not None else 0.0)

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
            included = [mw for mw in multi_wins if mw["sec"] <= max_win and mw["bpm"] is not None]
            vals = [mw["bpm"] for mw in included]
            confs = [mw["conf"] for mw in included]
            min_conf = min(confs) if confs else None
            spread = window_spread_mad(vals)
            spread_bad = spread is not None and spread > STABILITY_MAD_THRESH
            conf_bad = min_conf is not None and min_conf < CONF_MIN_THRESH
            unstable = spread_bad or conf_bad
            if vals and not unstable:
                if self.cons_last_sample_el is None or el - self.cons_last_sample_el >= CONS_SAMPLE_S:
                    self.cons_last_sample_el = el
                    self.cons_buf.append(median(vals))
                    if len(self.cons_buf) > CONS_BUFFER_N:
                        self.cons_buf.pop(0)
            shown = median(self.cons_buf)
            state = "unstable-skip" if unstable else "active"

        self.el += TICK_S
        return dict(t=el, shown=shown, state=state, sig_level=sig_level, multi_wins=multi_wins)


if __name__ == "__main__":
    import sys
    from scipy.io import wavfile

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    args = sys.argv[1:]
    compare = False
    if args[0] == "--compare":
        compare = True
        args = args[1:]

    fs, data = wavfile.read(args[0])
    data = data.mean(axis=1) if data.ndim > 1 else data
    data = data.astype(np.float64)

    chunk = int(TICK_S * fs)
    est_stream = StreamingPCGHeartRateEstimator(fs=fs)
    est_exact = EXACT.PCGHeartRateEstimator(fs=fs) if compare else None

    for i in range(0, len(data), chunk):
        est_stream.push(data[i:i + chunk])
        r = est_stream.tick()
        shown_s = f"{r['shown']:.1f}" if r["shown"] is not None else "--"
        if compare:
            est_exact.push(data[i:i + chunk])
            r2 = est_exact.tick()
            shown_s2 = f"{r2['shown']:.1f}" if r2["shown"] is not None else "--"
            print(f"t={r['t']:6.2f}s  streaming={shown_s:>7} ({r['state']:>13})   "
                  f"exact={shown_s2:>7} ({r2['state']:>13})")
        else:
            print(f"t={r['t']:6.2f}s  shown={shown_s:>7}  ({r['state']})")
