"""Block E, tracker: forward HMM over log-spaced HR states.

Input per frame (0.25 s): log-likelihood per state (232). Each frame the posterior drifts (random
walk, sigma_rel_per_sqrt_s), may jump to half or double (octave_prob) or anywhere (floor_prob),
and is multiplied by the evidence. Output: HR estimate, the posterior mean (in log HR) over the
states within estimate_tol_rel of the most likely state. State: posterior (232 float32).
"""

from dataclasses import dataclass

import numpy as np

from device import features


@dataclass
class TrackerState:
    """Posterior over HR states and fixed transition parameters."""
    hr: np.ndarray
    posterior: np.ndarray
    kernel: np.ndarray
    octave_shift: int
    halfwidth: int              # estimate window, states either side of the most likely
    cfg: dict


def init(cfg: dict, frame_s: float) -> TrackerState:
    """Uniform posterior; random-walk kernel for one frame; octave shift in states."""
    trk = cfg["tracker"]
    hr = features.hr_grid(cfg)
    step = np.log1p(cfg["grid"]["step_rel"])
    sigma_states = trk["sigma_rel_per_sqrt_s"] * np.sqrt(frame_s) / step
    half = max(1, int(np.ceil(3 * sigma_states)))
    offsets = np.arange(-half, half + 1)
    kernel = np.exp(-0.5 * (offsets / max(sigma_states, 1e-6)) ** 2)
    return TrackerState(
        hr=hr.astype(np.float32), posterior=np.full(hr.size, 1.0 / hr.size, np.float32),
        kernel=(kernel / kernel.sum()).astype(np.float32),
        octave_shift=int(round(np.log(2) / step)),
        halfwidth=int(round(np.log1p(trk["estimate_tol_rel"]) / step)), cfg=trk)


def predict(state: TrackerState) -> np.ndarray:
    """Prior for the next frame: random walk, octave jumps and a uniform floor."""
    post = state.posterior
    drift = np.convolve(post, state.kernel, mode="same")
    shift = state.octave_shift
    octave = np.zeros_like(post)
    octave[shift:] += 0.5 * post[:-shift]
    octave[:-shift] += 0.5 * post[shift:]
    p_oct, p_floor = np.float32(state.cfg["octave_prob"]), np.float32(state.cfg["floor_prob"])
    prior = (1 - p_oct - p_floor) * drift + p_oct * octave + p_floor / post.size
    return (prior / prior.sum()).astype(np.float32)


def update(state: TrackerState, log_emission: np.ndarray) -> None:
    """One forward step; without evidence (all NaN) the posterior becomes the prior."""
    prior = predict(state)
    if np.isnan(log_emission).all():
        state.posterior = prior
        return
    post = prior * np.exp((log_emission - log_emission.max()).astype(np.float32))
    state.posterior = post / post.sum()


def estimate(state: TrackerState) -> float:
    """HR: posterior mean (in log HR) over the states within halfwidth of the most likely."""
    i = int(np.argmax(state.posterior))
    lo, hi = max(0, i - state.halfwidth), min(state.hr.size, i + state.halfwidth + 1)
    mass = state.posterior[lo:hi]
    return float(np.exp((mass * np.log(state.hr[lo:hi])).sum() / max(float(mass.sum()), 1e-30)))
