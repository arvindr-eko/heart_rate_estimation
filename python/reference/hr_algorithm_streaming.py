"""
Streaming / embedded-faithful heart-rate tracker.

Same algorithm as hr_algorithm.py (4 band envelopes -> summed biased autocorrelation),
but with the demo's windowing baked in for real-time use on an embedded device:

  * filters run CONTINUOUSLY  - IIR state is carried across blocks, exactly like
    on-device biquads; we never re-filter old samples (so no per-tick edge transient).
  * the 100 Hz band envelopes feed FIXED-LENGTH ring buffers (one per band).
  * the buffer length caps the window at WIN_SEC seconds: it grows from the first
    sample up to 8 s, then slides (oldest frame evicted) - never grows past 8 s.
  * heart_rate() runs the restricted-lag autocorrelation over whatever is currently
    in the buffers; call it as often as you like (e.g. every 250 ms).

Bounded resources (the point of the cap): 4 x 800 float ring buffer = ~12.8 KB (f32)
plus a handful of filter states. Per estimate ~ 4 bands x 120 lags x <=800 = ~0.4 M MAC;
at 4 calls/s ~ 1.5 M MAC/s. No FFT, no Hilbert, fully causal.
"""
import numpy as np
from collections import deque
from scipy.signal import butter, lfilter, decimate, resample_poly, find_peaks
from scipy.io import wavfile

DEC_FS   = 500                      # tracker runs on a 500 Hz stream
FRAME_FS = 100                      # envelope frame rate for the autocorrelation
STEP     = DEC_FS // FRAME_FS       # 5  : 500 Hz envelope -> 100 Hz frames
LAGPOW   = 0.25                     # gentle "prefer shorter lag" bias
BANDS    = [(15, 30), (30, 50), (50, 80), (80, 130)]
WIN_SEC  = 8.0                                  # <-- window cap
WIN_FRAMES = int(WIN_SEC * FRAME_FS)            # 800 frames = 8 s  (ring-buffer length)
MIN_FRAMES = int(2.0 * FRAME_FS)                # need >=2 s buffered before a reading
LO = int(0.30 * FRAME_FS)                       # lag search range = 40..200 bpm
HI = int(1.5  * FRAME_FS)


class _IIR:
    """Stateful causal IIR: carries filter memory across blocks (== on-device biquad state)."""
    def __init__(self, b, a):
        self.b, self.a = b, a
        self.zi = np.zeros(max(len(a), len(b)) - 1)
    def process(self, x):
        y, self.zi = lfilter(self.b, self.a, x, zi=self.zi)
        return y


class HeartRateTracker:
    """Feed 500 Hz audio blocks with update(); read (bpm, confidence) with heart_rate()."""
    def __init__(self):
        self.bp  = [_IIR(*butter(2, [lo/(DEC_FS/2), hi/(DEC_FS/2)], 'band')) for lo, hi in BANDS]
        a = np.exp(-2*np.pi*18/DEC_FS)                       # 18 Hz 1-pole envelope smoother
        self.env = [_IIR([1 - a], [1.0, -a]) for _ in BANDS]
        self.buf = [deque(maxlen=WIN_FRAMES) for _ in BANDS]  # fixed-length -> 8 s cap
        self.n500 = 0                                         # samples seen, for downsample phase
        self._p2_recent = deque(maxlen=8); self._p2_bpm = None; self._p2_kind = None   # period-2 confirm window
        self.p2_pending = False                                        # period-2 detected this tick (pre-confirm)

    def update(self, x500):
        """Push a block of 500 Hz samples through the (stateful) front-end into the ring buffers."""
        x500 = np.asarray(x500, dtype=float)
        if x500.size == 0:
            return
        start = (-self.n500) % STEP          # first local index aligned to the global 500->100 grid
        for b in range(4):
            e = self.env[b].process(np.abs(self.bp[b].process(x500)))   # band-pass -> rectify -> smooth
            self.buf[b].extend(e[start::STEP])                          # decimate to 100 Hz, append
        self.n500 += x500.size               # one shared phase counter (bands are synchronous)

    def heart_rate(self):
        """(bpm, confidence, flag) from the current <=8 s window, or (None, 0.0, None) until 2 s.
        `flag` is None, 'alternans', or 'bigeminy'; when set, bpm is CORRECTED to the true rate
        (the half-rate the autocorrelation would otherwise lock onto in a period-2 rhythm)."""
        T = len(self.buf[0])
        if T < MIN_FRAMES:
            return None, 0.0, None
        hi = min(HI, T - 1)
        if hi <= LO:
            return None, 0.0, None
        lags = np.arange(LO, hi); S = np.zeros(len(lags)); bands = []
        for b in range(4):
            f = np.fromiter(self.buf[b], dtype=float, count=T); bands.append(f)
            sd = f.std(); fz = (f - f.mean()) / (sd if sd > 0 else 1)    # z-score over the window
            r0 = np.dot(fz, fz) / T
            for i, l in enumerate(lags):
                S[i] += (np.dot(fz[:T-l], fz[l:]) / T) / (r0 + 1e-9)     # biased /T autocorr
        S = S / (lags.astype(float)**LAGPOW)                            # gentle short-lag bias
        s = S - S.min()
        pk = [i for i in range(1, len(s)-1) if s[i] >= s[i-1] and s[i] > s[i+1]]
        if not pk:
            return None, 1.0, None
        pk.sort(key=lambda i: -s[i]); p1 = pk[0]; bpm = 60.0 / (lags[p1] / FRAME_FS)
        p2 = next((i for i in pk[1:] if abs(lags[i] - lags[p1]) >= 8), None)
        conf = (s[p1] / (s[p2] + 1e-9)) if p2 is not None else 9.99      # winner / runner-up
        # period-2 guard: only CORRECT once the rhythm is sustained over a recent window
        # (>=5 of last 8 ticks). Real signals fluctuate and throw sporadic 1-2 tick hits that
        # would falsely DOUBLE a correct reading; genuine alternans/bigeminy persists.
        p2flag, p2bpm = self._period2(s, lags, p1, bands, bpm)
        self.p2_pending = p2flag is not None
        self._p2_recent.append(1 if p2flag else 0)
        if p2flag: self._p2_bpm = p2bpm; self._p2_kind = p2flag
        if sum(self._p2_recent) >= 5:
            return self._p2_bpm, conf, self._p2_kind
        return bpm, conf, None

    def _period2(self, s, lags, p1, bands, bpm):
        """Detect a period-2 rhythm (pulsus alternans / bigeminy) that the autocorrelation
        would HALVE. If found, return (true_rate=2x, flag); else (bpm, None). The confidence
        gate is blind to this (the half-rate is internally self-consistent and confident)."""
        L = lags[p1]; h = L / 2.0
        if h < LO + 1:                                  # doubled rate would exceed 200 bpm -> not a halving
            return None, bpm
        def pkv(tgt):
            i = int(round(tgt - LO)); w = s[max(0, i-3):i+4]; return float(w.max()) if len(w) else 0.0
        ratio = pkv(h) / (pkv(L) + 1e-9)                # is there a real autocorr peak at the doubled rate?
        env = np.clip(np.mean(bands, axis=0), 0, None)  # broadband envelope (raw band frames)
        peaks, _ = find_peaks(env, distance=max(3, int(0.6*h)))
        if len(peaks) < 4:
            return None, bpm
        amps = env[peaks]; ints = np.diff(peaks)
        alt_amp = abs(amps[::2].mean() - amps[1::2].mean()) / (amps.mean() + 1e-9)   # pulsus alternans
        alt_int = abs(ints[::2].mean() - ints[1::2].mean()) / (ints.mean() + 1e-9)   # bigeminy (timing)
        true_bpm = 60.0 / (h / FRAME_FS)
        if ratio >= 0.50 and alt_amp >= 0.30: return 'alternans', true_bpm
        if ratio >= 0.20 and alt_int >= 0.30: return 'bigeminy', true_bpm
        return None, bpm


class ContinuityFilter:
    """Physiological continuity / slew-rate prior on the OUTPUT stream.

    HR changes slowly (~10 BPM/min = 0.167 BPM/s), so:
      * seed on the first confident reading,
      * follow only PLAUSIBLE moves (within `band` BPM of the held value), slew-limited,
      * REJECT any larger jump as an artifact (this kills octave/off-octave excursions -
        a value 1.6x-2.5x the held one cannot be real on a slow-changing signal),
      * re-acquire (re-seed) only if a far, confident, NON-jumpy value persists for
        `reacq_s` - the escape hatch in case the initial lock was itself wrong.
    Returns the committed BPM (or None until seeded)."""
    def __init__(self, max_slew=1.5, band=10.0, seed_conf=1.35, seed_conf_hi=1.10,
                 seed_hr_lo=80.0, seed_hr_hi=150.0,
                 seed_min_t=3.0, seed_hold=1.5, reacq_fast=1.0, reacq_s=15.0, reacq_conf=1.1):
        self.max_slew=max_slew; self.band=band; self.seed_conf=seed_conf
        self.seed_conf_hi=seed_conf_hi                            # peak-ratio declines with HR, so the
        self.seed_hr_lo=seed_hr_lo; self.seed_hr_hi=seed_hr_hi    # seed bar ramps DOWN with HR (not a step)
        self.seed_min_t=seed_min_t; self.seed_hold=seed_hold
        self.reacq_fast=reacq_fast; self.reacq_s=reacq_s; self.reacq_conf=reacq_conf
        self.out=None; self.t_last=None; self.far_since=None; self.far_val=None
        self.cand=None; self.cand_since=None

    def _seed_bar(self, bpm):
        """Confidence required to seed, ramped down with HR (high rates have lower peak-ratio)."""
        if bpm <= self.seed_hr_lo: return self.seed_conf
        if bpm >= self.seed_hr_hi: return self.seed_conf_hi
        fr = (bpm - self.seed_hr_lo) / (self.seed_hr_hi - self.seed_hr_lo)
        return self.seed_conf + fr * (self.seed_conf_hi - self.seed_conf)

    def update(self, bpm, conf, t, flag=None, hold=False):
        if bpm is None:                       # no signal this tick: hold
            self.t_last=t; return self.out
        dt = (t - self.t_last) if self.t_last is not None else 0.0
        self.t_last = t
        if self.out is None:                  # not locked: seed on a value held stable & confident
            if hold:                          # period-2 pending -> don't seed the (likely halved) raw value
                self.cand = None; return None
            # track value-stability continuously (a transient low-conf tick must NOT reset it)
            if self.cand is None or abs(bpm - self.cand) > max(3, 0.04*self.cand):
                self.cand = bpm; self.cand_since = t                # new candidate value
            elif (t >= self.seed_min_t and t - self.cand_since >= self.seed_hold
                  and conf >= self._seed_bar(bpm)):
                self.out = bpm                                      # held stable long enough & confident -> lock
            return self.out
        if abs(bpm - self.out) <= self.band:  # plausible: track it, slew-limited
            self.far_since = None
            step = max(-self.max_slew*dt, min(self.max_slew*dt, bpm - self.out))
            self.out += step
        else:                                 # far from committed: directional re-acquire
            # artifacts skew HIGH (doublings/harmonics). A far LOWER value is likely the true
            # fundamental the lock was a harmonic of -> break the lock fast. A far HIGHER value
            # is a doubling artifact -> reject (only the slow safety re-acquire applies).
            if self.far_since is None or abs(bpm - self.far_val) > max(3, 0.04*self.far_val):
                self.far_since = t; self.far_val = bpm              # track challenger value-stability
            else:
                # a flagged period-2 CORRECTION is trusted -> accept fast in either direction;
                # otherwise direction matters: far-lower=true fundamental (fast), far-higher=artifact (slow)
                need = self.reacq_fast if (flag is not None or bpm < self.out - self.band) else self.reacq_s
                if t - self.far_since >= need and conf >= self.reacq_conf:
                    self.out = bpm; self.far_since = None            # stable long enough & confident -> re-acquire
        return self.out


# ---- offline test harness (the device's ADC/DSP supplies the 500 Hz stream in hardware) ----
def _to_dec(x, fs):
    if fs == DEC_FS: return x.astype(float)
    if fs % DEC_FS == 0:
        q = fs // DEC_FS; y = x.astype(float)
        while q > 1:
            f = next((k for k in (8, 5, 4, 2) if q % k == 0), 2)
            y = decimate(y, f, ftype='iir'); q //= f
        return y
    return resample_poly(x.astype(float), DEC_FS, fs)

def analyze_wav_streaming(path, block_ms=250):
    """Simulate streaming: feed the file to the tracker in blocks, returning a (t, bpm, conf) log."""
    fs, d = wavfile.read(path)
    d = d.mean(axis=1) if d.ndim > 1 else d
    d = d.astype(float); d /= (np.max(np.abs(d)) + 1e-9)
    dec = _to_dec(d, fs)                       # front-end decimation to 500 Hz (done on-device in HW)
    trk = HeartRateTracker(); cf = ContinuityFilter()
    blk = max(1, int(DEC_FS * block_ms / 1000))
    log = []
    for i in range(0, len(dec), blk):
        trk.update(dec[i:i + blk])
        bpm, conf, flag = trk.heart_rate()
        t = (i + blk) / DEC_FS
        hold = trk.p2_pending and flag is None  # period-2 detected but not yet confirmed -> don't seed
        out = cf.update(bpm, conf, t, flag, hold)   # continuity-limited committed output
        log.append((t, bpm, conf, out, flag))
    return log

if __name__ == "__main__":
    import sys
    for t, bpm, conf, out, flag in analyze_wav_streaming(sys.argv[1]):
        raw = "--" if bpm is None else f"{bpm:5.1f} bpm  conf {conf:.2f}x" + (f"  [{flag}]" if flag else "")
        com = "--" if out is None else f"{out:5.1f}"
        print(f"{t:5.2f}s  raw {raw:24s}  -> committed {com}")
