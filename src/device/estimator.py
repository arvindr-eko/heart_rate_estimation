"""Streaming estimator: blocks A-F and R behind one call per input block, as on the device.

Usage: state = init(cfg); then reading = process(state, block) for each block of block_samples
PCG samples (float, full scale 1.0). Per block: band energies (A), beat activation (B) and the
feature block's per-sample update (C). Every frame_blocks blocks: features (C), scorer (D),
tracker (E), rhythm (R) and readout (F). A reading is (t_s, bpm, confidence), returned on output
slots only (module readout); None otherwise. After the first reading, readings pause while a
frame has no heartbeat (readout.heart_absent; the reading on show stays), and after
decision.lost_after_s without heartbeat the measurement restarts by itself (restart): readings
stop until the next first reading. restart (or init) also starts a new measurement on request.
"""

from dataclasses import dataclass, field

import numpy as np

from device import features, frontend, net, readout, rhythm, scorer, tracker


@dataclass
class EstimatorState:
    """Block states, the scorer model, the calibration and the block counter."""
    cfg: dict
    fe: frontend.FrontendState
    nn: net.NetState
    feat: features.FeatureState
    feat_s1: features.FeatureState          # 25-50 Hz band (S1), decimated by s1_decimation
    model: scorer.ScorerModel | None
    cal: readout.Calibration
    trk: tracker.TrackerState
    rd: readout.ReadoutState
    grid: np.ndarray
    n_blocks: int = 0
    trace: list = field(default_factory=list)
    keep_trace: bool = False
    s1_acc: list = field(default_factory=list)  # S1-band values awaiting decimation


def frame_seconds(cfg: dict) -> float:
    """Tracker frame length in seconds."""
    s = cfg["stream"]
    return s["block_samples"] * s["frame_blocks"] / s["fs_hz"]


def _blocks(cfg: dict, weights: dict | None = None) -> dict:
    """Fresh state of every block (network weights reused when given)."""
    ft = cfg["features"]
    return {"fe": frontend.init(cfg), "nn": net.init(cfg, weights), "feat": features.init(cfg),
            "feat_s1": features.init(cfg, ft["s1_window_s"],
                                     cfg["frontend"]["env_rate_hz"] / ft["s1_decimation"]),
            "trk": tracker.init(cfg, frame_seconds(cfg)), "rd": readout.ReadoutState()}


def init(cfg: dict, keep_trace: bool = False, with_scorer: bool = True) -> EstimatorState:
    """All block states for one measurement; without the scorer when collecting its training
    inputs."""
    model = scorer.load(scorer.weights_path(cfg)) if with_scorer else None
    return EstimatorState(cfg=cfg, model=model, cal=readout.load_calibration(cfg),
                          grid=features.hr_grid(cfg), keep_trace=keep_trace, **_blocks(cfg))


def restart(state: EstimatorState) -> None:
    """New measurement: every block state cleared; scorer, calibration and trace kept."""
    for name, value in _blocks(state.cfg, state.nn.weights).items():
        setattr(state, name, value)
    state.n_blocks = 0
    state.s1_acc = []


def feed(state: EstimatorState, block: np.ndarray) -> None:
    """Blocks A-B for one input block, and the per-sample update of block C: the activation,
    and the normalised 25-50 Hz band (S1) averaged over s1_decimation envelope samples."""
    env = frontend.process(state.fe, block)
    for i in range(env.shape[1]):
        x = net.normalise(state.nn, env[:, i])
        features.push(state.feat, np.atleast_1d(net.step(state.nn, x)))
        state.s1_acc.append(x[0])
        if len(state.s1_acc) == state.cfg["features"]["s1_decimation"]:
            features.push(state.feat_s1, np.array([np.mean(state.s1_acc)], np.float32))
            state.s1_acc.clear()
    state.n_blocks += 1


def scorer_input(state: EstimatorState) -> np.ndarray | None:
    """Block C: activation features, then the S1-band features (features.S1_FEATURES) and a
    flag, both zero for candidate rates above s1_max_bpm (S1 and S2 merge at very high rates, so
    the band says nothing there); None before min_window_s."""
    act = features.compute(state.feat)
    if act is None:
        return act
    cols = [features.FEATURE_NAMES.index(n) for n in features.S1_FEATURES]
    s1 = features.compute(state.feat_s1)
    s1 = np.zeros((act.shape[0], len(cols)), np.float32) if s1 is None else s1[:, cols]
    valid = (state.grid <= state.cfg["features"]["s1_max_bpm"]).astype(np.float32)
    return np.hstack([act, s1 * valid[:, None], valid[:, None]])


def frame_evidence(state: EstimatorState) -> tuple[np.ndarray, float, np.ndarray | None]:
    """Blocks C-D: tracker evidence per state, the frame's own best HR and the features; NaN
    evidence and no features before min_window_s."""
    feats = scorer_input(state)
    if feats is None:
        return np.full(state.grid.shape, np.nan), np.nan, None
    log_p = scorer.log_emission(state.model, feats)
    return (state.cfg["scorer"]["emission_beta"] * log_p, float(state.grid[int(np.argmax(log_p))]),
            feats)


def periodicity(state: EstimatorState, feats: np.ndarray | None, hr: float) -> float:
    """ACF of the activation at the period of the HR estimate (0 without features): near 0 for
    silence or noise, high for regular heartbeats."""
    if feats is None or np.isnan(hr):
        return 0.0
    return float(feats[int(np.argmin(np.abs(state.grid - hr))), features.ACF_T])


def activation_sd(state: EstimatorState) -> float:
    """Standard deviation of the activation over the features window: near 0 for silence and
    steady sounds such as mains hum, whose tiny ripple the scale-free periodicity still scores
    high; 0.04 or more with heartbeats, also under heavy noise."""
    w = features.window(state.feat)
    return float(np.std(w)) if w.size else 0.0


def process(state: EstimatorState, block: np.ndarray) -> tuple | None:
    """Consume one input block; return a reading (t_s, bpm, confidence) on output slots."""
    feed(state, block)
    if state.n_blocks % state.cfg["stream"]["frame_blocks"]:
        return None
    cfg = state.cfg
    t = state.n_blocks * cfg["stream"]["block_samples"] / cfg["stream"]["fs_hz"]
    evidence, frame_hr, feats = frame_evidence(state)
    tracker.update(state.trk, evidence)
    hr = tracker.estimate(state.trk)
    acf = periodicity(state, feats, hr)
    sd = activation_sd(state)
    cv, beat_hr = rhythm.analyse(features.window(state.feat), state.feat.rate_hz, hr, cfg["rhythm"])
    a = readout.assess(cfg, state.cal, state.trk.posterior, state.grid, hr, frame_hr, cv, acf)
    present = readout.heart_present(cfg, acf, sd)
    if state.keep_trace:
        state.trace.append({"t": t, "hr": hr, "frame_hr": frame_hr, "periodicity": acf,
                            "activation_sd": sd, "cv": cv, "beat_hr": beat_hr,
                            "heart_present": present, **a})
    absent = readout.heart_absent(cfg, acf, sd)
    if readout.lost(state.rd, cfg, absent, frame_seconds(cfg)):
        restart(state)
        return None
    if absent and state.rd.first_at is not None:   # no update without heartbeat
        return None
    due = readout.first_due(cfg, a, acf, sd, hr, t)
    return readout.schedule(state.rd, cfg, due, hr, readout.confidence_of(a), t)


def run(cfg: dict, audio: np.ndarray, keep_trace: bool = False,
        state: EstimatorState | None = None) -> tuple[list, list]:
    """Stream a whole recording block by block, from a new state (keep_trace) or the given one,
    which the caller inspects afterwards; return (readings, frame trace). Reading times count
    from the start of the recording; the trace's t counts from the start of the measurement,
    which a restart resets (frame i ends at input time (i + 1) x frame_seconds)."""
    if state is None:
        state = init(cfg, keep_trace)
    block, fs = cfg["stream"]["block_samples"], cfg["stream"]["fs_hz"]
    readings = []
    for i, start in enumerate(range(0, audio.size - block + 1, block)):
        reading = process(state, audio[start:start + block])
        if reading is not None:
            readings.append(((i + 1) * block / fs, *reading[1:]))
    return readings, state.trace
