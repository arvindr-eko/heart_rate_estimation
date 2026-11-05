"""Block E, tracker: forward HMM over log-spaced HR states.

Input per frame: log-likelihood per state. Output: HR estimate (posterior mean within conf_tol_rel
of the most likely state) and confidence (posterior mass in that window).
"""

from dataclasses import dataclass

import numpy as np

from pcg_hr import features


@dataclass
class TrackerState:
    """Posterior over HR states and fixed transition parameters."""
    hr: np.ndarray
    posterior: np.ndarray
    kernel: np.ndarray
    octave_shift: int
    conf_halfwidth: int
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
        hr=hr, posterior=np.full(hr.size, 1.0 / hr.size), kernel=kernel / kernel.sum(),
        octave_shift=int(round(np.log(2) / step)),
        conf_halfwidth=int(round(np.log1p(trk["conf_tol_rel"]) / step)), cfg=trk)


def predict(state: TrackerState) -> np.ndarray:
    """Prior for the next frame: random walk, octave jumps and a uniform floor."""
    post = state.posterior
    drift = np.convolve(post, state.kernel, mode="same")
    shift = state.octave_shift
    octave = np.zeros_like(post)
    octave[shift:] += 0.5 * post[:-shift]
    octave[:-shift] += 0.5 * post[shift:]
    p_oct, p_floor = state.cfg["octave_prob"], state.cfg["floor_prob"]
    prior = (1 - p_oct - p_floor) * drift + p_oct * octave + p_floor / post.size
    return prior / prior.sum()


def update(state: TrackerState, log_emission: np.ndarray) -> None:
    """One forward step; without evidence (all NaN) the posterior becomes the prior."""
    prior = predict(state)
    if np.isnan(log_emission).all():
        state.posterior = prior
        return
    post = prior * np.exp(log_emission - log_emission.max())
    state.posterior = post / post.sum()


def estimate(state: TrackerState) -> tuple[float, float]:
    """HR (posterior mean near the most likely state) and confidence (mass in that window)."""
    i = int(np.argmax(state.posterior))
    lo, hi = max(0, i - state.conf_halfwidth), min(state.hr.size, i + state.conf_halfwidth + 1)
    mass = state.posterior[lo:hi]
    conf = float(mass.sum())
    log_hr = np.log(state.hr[lo:hi])
    return float(np.exp((mass * log_hr).sum() / max(conf, 1e-30))), conf
