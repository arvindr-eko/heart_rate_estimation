"""HMM over log-spaced HR states with anytime release.

Input per frame: an emission log-likelihood per state (NaN = no evidence). Output: posterior,
HR estimate (posterior mean inside the confidence window) and confidence (posterior mass within
release_tol_rel of the maximum a posteriori state).
"""

from dataclasses import dataclass

import numpy as np

from hr import periodicity


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
    hr = periodicity.hr_grid(cfg)
    step = np.log1p(trk["grid_step_rel"])
    sigma_states = trk["sigma_rel_per_sqrt_s"] * np.sqrt(frame_s) / step
    half = max(1, int(np.ceil(3 * sigma_states)))
    offsets = np.arange(-half, half + 1)
    kernel = np.exp(-0.5 * (offsets / max(sigma_states, 1e-6)) ** 2)
    return TrackerState(
        hr=hr, posterior=np.full(hr.size, 1.0 / hr.size), kernel=kernel / kernel.sum(),
        octave_shift=int(round(np.log(2) / step)),
        conf_halfwidth=int(round(np.log1p(trk["release_tol_rel"]) / step)), cfg=trk)


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
    """One forward step; states without evidence keep their prior weight."""
    prior = predict(state)
    log_e = np.nan_to_num(log_emission, nan=0.0)
    if np.isnan(log_emission).all():
        state.posterior = prior
        return
    observed = ~np.isnan(log_emission)
    log_e[~observed] = log_e[observed].mean()
    post = prior * np.exp(log_e - log_e.max())
    state.posterior = post / post.sum()


def estimate(state: TrackerState) -> tuple[float, float]:
    """HR (posterior mean near the MAP state) and confidence (mass in that window)."""
    i = int(np.argmax(state.posterior))
    lo, hi = max(0, i - state.conf_halfwidth), min(state.hr.size, i + state.conf_halfwidth + 1)
    mass = state.posterior[lo:hi]
    conf = float(mass.sum())
    log_hr = np.log(state.hr[lo:hi])
    return float(np.exp((mass * log_hr).sum() / max(conf, 1e-30))), conf


def resources(cfg: dict, frames_per_s: float) -> dict:
    """Estimated MAC/s and RAM (32-bit posterior) of the tracker."""
    n = periodicity.hr_grid(cfg).size
    return {"macs_per_s": n * 20 * frames_per_s, "ram_bytes": 2 * 4 * n, "const_bytes": 64}
