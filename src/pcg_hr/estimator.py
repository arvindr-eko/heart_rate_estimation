"""Streaming estimator: the device-facing API composing blocks A-F.

Usage: state = init(cfg); for each block of block_samples PCG samples, reading = process(state,
block). A reading is (t_s, bpm, confidence), returned on output slots only (first reading per
the release rule, then every output_period_s); None otherwise. Mirrors c/include/hr_api.h.
"""

from dataclasses import dataclass, field

import numpy as np

from pcg_hr import features, frontend, net, release, scorer, tracker


@dataclass
class EstimatorState:
    """Module states, the scorer model and block counter."""
    cfg: dict
    fe: frontend.FrontendState
    nn: net.NetState
    feat_act: features.FeatureState      # features of the activation
    feat_env: features.FeatureState      # features of the band-mean envelope
    model: scorer.ScorerModel | None
    trk: tracker.TrackerState
    rel: release.ReleaseState
    grid: np.ndarray
    n_blocks: int = 0
    trace: list = field(default_factory=list)
    keep_trace: bool = False


def frame_seconds(cfg: dict) -> float:
    """Tracker frame length in seconds."""
    s = cfg["stream"]
    return s["block_samples"] * s["frame_blocks"] / s["fs_hz"]


def init(cfg: dict, keep_trace: bool = False, with_scorer: bool = True) -> EstimatorState:
    """Create all module states for one recording (call again on contact loss); without the
    scorer when collecting its training features."""
    model = (scorer.load(cfg["paths"]["models_dir"] / cfg["scorer"]["weights"]) if with_scorer
             else None)
    return EstimatorState(
        cfg=cfg, fe=frontend.init(cfg), nn=net.init(cfg), feat_act=features.init(cfg),
        feat_env=features.init(cfg), model=model,
        trk=tracker.init(cfg, frame_seconds(cfg)), rel=release.ReleaseState(),
        grid=features.hr_grid(cfg), keep_trace=keep_trace)


def feed(state: EstimatorState, block: np.ndarray) -> None:
    """Blocks A-B for one input block; push the activation and the band-mean envelope."""
    env = frontend.process(state.fe, block)
    features.push(state.feat_act, net.process(state.nn, env))
    features.push(state.feat_env, env.mean(axis=0, keepdims=True))
    state.n_blocks += 1


def scorer_input(state: EstimatorState) -> np.ndarray | None:
    """Block C: features of the activation and of the band-mean envelope, (n_states, 24);
    None before min_window_s."""
    act = features.compute(state.feat_act)
    if act is None:
        return None
    return np.hstack([act, features.compute(state.feat_env)])


def frame_evidence(state: EstimatorState) -> tuple[np.ndarray, float, float]:
    """Blocks C-D: tracker evidence per state, and the frame's own best HR and confidence
    (scorer probability within the tracker's confidence window of its best state)."""
    feats = scorer_input(state)
    if feats is None:
        return np.full(state.grid.shape, np.nan), np.nan, 0.0
    log_p = scorer.log_emission(state.model, feats)
    i = int(np.argmax(log_p))
    hw = state.trk.conf_halfwidth
    conf = float(np.exp(log_p[max(0, i - hw):i + hw + 1]).sum())
    return state.cfg["scorer"]["emission_beta"] * log_p, float(state.grid[i]), conf


def process(state: EstimatorState, block: np.ndarray) -> tuple | None:
    """Consume one input block; return a reading (t_s, bpm, confidence) on output slots."""
    feed(state, block)
    if state.n_blocks % state.cfg["stream"]["frame_blocks"]:
        return None
    t = state.n_blocks * state.cfg["stream"]["block_samples"] / state.cfg["stream"]["fs_hz"]
    evidence, frame_hr, frame_conf = frame_evidence(state)
    tracker.update(state.trk, evidence)
    hr, conf = tracker.estimate(state.trk)
    if state.keep_trace:
        state.trace.append((t, hr, conf, frame_hr, frame_conf))
    is_ready = release.ready(state.cfg, state.trk.posterior, state.grid, hr, frame_hr,
                             frame_conf, t)
    return release.schedule(state.rel, state.cfg, is_ready, hr, conf, t)


def run(cfg: dict, audio: np.ndarray, keep_trace: bool = False) -> tuple[list, list]:
    """Stream a whole recording block by block; return (readings, frame trace)."""
    state = init(cfg, keep_trace)
    block = cfg["stream"]["block_samples"]
    readings = []
    for start in range(0, audio.size - block + 1, block):
        reading = process(state, audio[start:start + block])
        if reading is not None:
            readings.append(reading)
    return readings, state.trace
