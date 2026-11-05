"""Top-level streaming estimator: front end -> periodicity -> emission -> tracker -> readings.

Call init once, then process per input block. process returns a reading (time_s, hr_bpm, conf)
on output slots, with hr_bpm NaN when the reading is withheld, and None otherwise.
"""

from dataclasses import dataclass, field

import numpy as np

from hr import beats, frontend, gate, net, periodicity, salience, tracker


@dataclass
class EstimatorState:
    """Module states, timing counters and release status."""
    cfg: dict
    fe: frontend.FrontendState
    per: periodicity.PeriodicityState
    trk: tracker.TrackerState | None
    hr_grid: np.ndarray
    sal: salience.SalienceModel | None = None
    nn: net.NetState | None = None
    per_env: periodicity.PeriodicityState | None = None
    beat: beats.BeatState | None = None
    basis: np.ndarray | None = None
    net_logp: np.ndarray | None = None
    gate_model: gate.GateModel | None = None
    gate_feats: np.ndarray | None = None
    hr_hist: list = field(default_factory=list)
    unscored_share: float = 1.0
    n_blocks: int = 0
    released_at: float | None = None
    last_output: float | None = None
    trace: list = field(default_factory=list)
    keep_trace: bool = False


def frame_seconds(cfg: dict) -> float:
    """Tracker frame length in seconds."""
    s = cfg["stream"]
    return s["block_samples"] * s["frame_blocks"] / s["fs_hz"]


def init(cfg: dict, keep_trace: bool = False) -> EstimatorState:
    """Create all module states for one recording."""
    nn_state = net.init(cfg) if cfg["net"]["enabled"] else None
    fe = frontend.init(cfg, bands_hz=cfg["net"]["bands_hz"] if nn_state else None)
    channels = (1 if nn_state or cfg["periodicity"]["input"] == "waveform"
                else len(cfg["frontend"]["bands_hz"]))
    trk = tracker.init(cfg, frame_seconds(cfg)) if cfg["tracker"]["enabled"] else None
    sal = None
    if cfg["salience"]["enabled"]:
        sal = salience.load(cfg["paths"]["models_dir"] / cfg["salience"]["weights"])
    basis = None
    if nn_state is not None and cfg["net"]["head"] == "hr":
        basis = net.hr_basis(periodicity.hr_grid(cfg), cfg["net"]["hr_basis"])
    per_env = periodicity.init(cfg, 1) if nn_state and cfg["salience"]["dual_input"] else None
    gate_model = None
    if cfg["tracker"]["release_mode"] == "deadline" and cfg["release"]["confidence"] == "gate":
        gate_model = gate.load(cfg["paths"]["models_dir"] / cfg["gate"]["weights"])
    beat = beats.init(cfg) if nn_state and cfg["beats"]["enabled"] else None
    return EstimatorState(cfg=cfg, fe=fe, per=periodicity.init(cfg, channels), trk=trk,
                          hr_grid=periodicity.hr_grid(cfg), sal=sal, nn=nn_state, basis=basis,
                          per_env=per_env, gate_model=gate_model, beat=beat,
                          keep_trace=keep_trace)


def salience_features(state: EstimatorState) -> np.ndarray | None:
    """Per-state features of the periodicity input, plus those of the band-mean envelope when
    dual_input is set and the beat-interval features when beats are enabled; None before
    min_window_s."""
    feats = periodicity.features(state.per)
    if feats is None:
        return None
    parts = [feats]
    if state.per_env is not None:
        parts.append(periodicity.features(state.per_env))
    if state.beat is not None:
        parts.append(beats.features(state.beat, state.hr_grid))
    return np.hstack(parts) if len(parts) > 1 else feats


def frame_scores(state: EstimatorState) -> tuple[np.ndarray, float, float]:
    """Per-state emission for this frame, plus the frame's own HR and confidence.

    With the salience model the frame confidence is its softmax mass within release_tol_rel of
    the arg-max state (calibrated on training data); without it the frame confidence is NaN.
    """
    min_frames = state.cfg["periodicity"]["min_window_s"] * state.per.rate_hz
    if state.basis is not None:
        if state.net_logp is None or state.per.history.shape[1] < min_frames:
            return np.full(state.hr_grid.shape, np.nan), np.nan, 0.0
        log_p = state.net_logp
    elif state.sal is None:
        return state.cfg["tracker"]["obs_beta"] * periodicity.compute(state.per), np.nan, np.nan
    else:
        feats = salience_features(state)
        if feats is None:
            return np.full(state.hr_grid.shape, np.nan), np.nan, 0.0
        log_p = salience.log_emission(state.sal, feats)
    i = int(np.argmax(log_p))
    hw = state.trk.conf_halfwidth if state.trk else 5
    conf = float(np.exp(log_p[max(0, i - hw):i + hw + 1]).sum())
    return state.cfg["salience"]["emission_beta"] * log_p, float(state.hr_grid[i]), conf


def frame_output(state: EstimatorState, t: float) -> tuple[float, float, float, float]:
    """(tracker or fixed-window HR, its confidence, frame HR, frame confidence)."""
    scores, frame_hr, frame_conf = frame_scores(state)
    state.unscored_share = float(np.isnan(scores).mean())
    if state.trk is None:
        if t + 1e-9 < state.cfg["tracker"]["fixed_window_s"] or np.isnan(scores).all():
            return np.nan, 0.0, frame_hr, frame_conf
        return float(state.hr_grid[np.nanargmax(scores)]), 1.0, frame_hr, frame_conf
    tracker.update(state.trk, scores)
    hr, conf = tracker.estimate(state.trk)
    return hr, conf, frame_hr, frame_conf


def ready(state: EstimatorState, hr: float, conf: float, frame_hr: float, frame_conf: float,
          t: float) -> bool:
    """Release rule: posterior mass, frame confidence, or both with agreement.

    The frame-confidence threshold is piecewise linear in time (release_frame_conf as
    [[t_s, conf], ...]) so hard recordings still get a reading, later.
    """
    trk = state.cfg["tracker"]
    mode = trk["release_mode"]
    if mode == "deadline":
        return deadline_ready(state, hr, frame_hr, frame_conf, t)
    if t + 1e-9 < trk["min_time_s"] or np.isnan(hr):
        return False
    if mode == "posterior":
        return conf >= trk["release_conf"]
    times, confs = zip(*trk["release_frame_conf"])
    needed = float(np.interp(t, times, confs))
    agree = abs(np.log(hr / frame_hr)) <= np.log1p(trk["release_tol_rel"])
    if mode == "frame":
        return frame_conf >= needed and agree
    return frame_conf >= needed and conf >= trk["release_conf"] and agree


def deadline_threshold(rel: dict, t: float, slow: bool) -> float:
    """Confidence needed at time t: c_early before the cutoff, then linear from c_cutoff to 0 at
    max_s. The cutoff is low_hr_s when the heart may be slow, default_s otherwise."""
    cutoff = rel["low_hr_s"] if slow else rel["default_s"]
    if t + 1e-9 < cutoff:
        return rel["c_early"]
    span = max(rel["max_s"] - cutoff, 1e-9)
    return rel["c_cutoff"] * max(0.0, (rel["max_s"] - t) / span)


def deadline_ready(state: EstimatorState, hr: float, frame_hr: float, frame_conf: float,
                   t: float) -> bool:
    """Deadline release: always by max_s; earlier once the window holds min_periods periods of
    the estimate and the confidence meets the time- and HR-dependent threshold.

    Confidence is the salience frame confidence with tracker agreement (confidence = frame), or
    the learned gate probability (confidence = gate). The heart counts as possibly slow when the
    estimate or posterior mass of low_mass_min lies below low_hr_bpm.
    """
    rel = state.cfg["release"]
    if np.isnan(hr) or t + 1e-9 < rel["min_s"]:
        return False
    if t + 1e-9 >= rel["max_s"]:
        return True
    if min(t, state.cfg["periodicity"]["window_s"]) * hr / 60.0 < rel["min_periods"]:
        return False
    low_mass = float(state.trk.posterior[state.trk.hr < rel["low_hr_bpm"]].sum())
    needed = deadline_threshold(rel, t, hr < rel["low_hr_bpm"] or low_mass >= rel["low_mass_min"])
    if rel["confidence"] == "gate":
        return state.gate_feats is not None and gate.predict(state.gate_model,
                                                              state.gate_feats) >= needed
    agree = abs(np.log(hr / frame_hr)) <= np.log1p(state.cfg["tracker"]["release_tol_rel"])
    return bool(agree and frame_conf >= needed)


def update_gate_features(state: EstimatorState, hr: float, conf: float, frame_hr: float,
                         frame_conf: float, t: float) -> None:
    """Frame statistics for the gate (needs the tracker); keeps the last second of estimates."""
    state.hr_hist = (state.hr_hist + [hr])[-5:]
    if state.trk is None or np.isnan(hr):
        state.gate_feats = None
        return
    trk = state.trk
    state.gate_feats = gate.features(
        trk.posterior, trk.hr, trk.conf_halfwidth, trk.octave_shift, hr, conf, frame_hr,
        frame_conf, state.unscored_share, t,
        state.hr_hist[0] if len(state.hr_hist) == 5 else np.nan,
        state.cfg["periodicity"]["window_s"], state.cfg["release"]["low_hr_bpm"])


def schedule(state: EstimatorState, hr: float, conf: float, frame_hr: float, frame_conf: float,
             t: float):
    """Release logic and output slots; returns a reading or None."""
    trk = state.cfg["tracker"]
    period = state.cfg["stream"]["output_period_s"]
    if state.released_at is None:
        if not ready(state, hr, conf, frame_hr, frame_conf, t):
            return None
        state.released_at = state.last_output = t
        return (t, hr, conf)
    if t + 1e-9 < state.last_output + period:
        return None
    state.last_output = t
    return (t, hr if conf >= trk["hold_conf"] else np.nan, conf)


def feed(state: EstimatorState, block: np.ndarray) -> None:
    """Front end (and network) for one block; push the periodicity input."""
    env, wave = frontend.process(state.fe, block)
    if state.basis is not None:
        state.net_logp = net.process(state.nn, env, state.basis)[:, -1]
        periodicity.push(state.per, np.zeros((1, env.shape[1])))  # time base only
    elif state.nn is not None:
        activation = net.process(state.nn, env)
        periodicity.push(state.per, activation)
        if state.beat is not None:
            beats.push(state.beat, activation)
        if state.per_env is not None:
            periodicity.push(state.per_env, env.mean(axis=0, keepdims=True))
    elif state.cfg["periodicity"]["input"] == "waveform":
        periodicity.push(state.per, wave[None, :])
    else:
        periodicity.push(state.per, env)
    state.n_blocks += 1


def process(state: EstimatorState, block: np.ndarray):
    """Consume one input block; return a reading on output slots, else None."""
    feed(state, block)
    if state.n_blocks % state.cfg["stream"]["frame_blocks"]:
        return None
    t = state.n_blocks * state.cfg["stream"]["block_samples"] / state.cfg["stream"]["fs_hz"]
    hr, conf, frame_hr, frame_conf = frame_output(state, t)
    if state.gate_model is not None or state.keep_trace:
        update_gate_features(state, hr, conf, frame_hr, frame_conf, t)
    if state.keep_trace:
        state.trace.append((t, hr, conf, frame_hr, frame_conf, state.gate_feats))
    return schedule(state, hr, conf, frame_hr, frame_conf, t)


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


def resources(cfg: dict) -> dict:
    """Summed module resource estimates; MIPS assumes 1 MAC per instruction plus 50 % overhead."""
    frames_per_s = 1.0 / frame_seconds(cfg)
    channels = len(cfg["frontend"]["bands_hz"])
    if cfg["salience"]["enabled"]:
        per = periodicity.feature_resources(cfg, frames_per_s)
        if cfg["net"]["enabled"] and cfg["salience"]["dual_input"]:
            n = int(round(cfg["periodicity"]["window_s"] * periodicity.input_rate(cfg)))
            per = {"macs_per_s": 2 * per["macs_per_s"], "const_bytes": 0,
                   "ram_bytes": per["ram_bytes"] + 2 * n + 8 * n}
    else:
        per = periodicity.resources(cfg, 1 if cfg["net"]["enabled"] else channels, frames_per_s)
    if cfg["net"]["enabled"]:
        weights = net.load_weights(cfg["paths"]["models_dir"] / cfg["net"]["weights"])
        parts = [frontend.resources(cfg, len(cfg["net"]["bands_hz"])), net.resources(cfg, weights)]
        if cfg["net"]["head"] == "activation":
            parts.append(per)
    else:
        parts = [frontend.resources(cfg), per]
    if cfg["tracker"]["enabled"]:
        parts.append(tracker.resources(cfg, frames_per_s))
    if cfg["salience"]["enabled"]:
        model = salience.load(cfg["paths"]["models_dir"] / cfg["salience"]["weights"])
        parts.append(salience.resources(model, periodicity.hr_grid(cfg).size, frames_per_s))
    if cfg["tracker"]["release_mode"] == "deadline" and cfg["release"]["confidence"] == "gate":
        parts.append(gate.resources(gate.load(cfg["paths"]["models_dir"] / cfg["gate"]["weights"]),
                                    frames_per_s))
    total = {k: sum(p[k] for p in parts) for k in parts[0]}
    total["mips"] = 1.5 * total["macs_per_s"] / 1e6
    return total
