"""Block Q, signal quality: the spectral balance of the input, from the front end's band log
energies (10 ms). Heart sounds lie below 200 Hz; finger taps, scratching, friction, speech and
breath carry relatively more 200-400 Hz energy, also through the device channel; a murmur does
too, but in step with the heartbeat (the presence gate weighs this against the periodicity).

Per envelope sample: exponential averages (time constant hf_tau_s) of the 25-100 Hz and the
200-400 Hz band energies, started at the first sample. Per frame (readout): `hf`, the natural log
of their ratio. Energy-weighted, so loud sounds dominate and the ratio follows a change of sound
within about hf_tau_s, with no level tracking. State: 2 floats and a flag; float32, as on the
device.
"""

from dataclasses import dataclass

import numpy as np


@dataclass
class QualityState:
    """Averaged 25-100 Hz and 200-400 Hz band energies (None before the first sample)."""
    low: np.float32 | None = None
    high: np.float32 | None = None


def update(state: QualityState, cfg: dict, env: np.ndarray) -> None:
    """One envelope sample (band log energies): update the band energy averages."""
    f32 = np.float32
    e = np.exp(env.astype(np.float32))
    low, high = f32(e[0] + e[1]), f32(e[3])
    if state.low is None:
        state.low, state.high = low, high
        return
    alpha = f32(1.0 / (cfg["quality"]["hf_tau_s"] * cfg["frontend"]["env_rate_hz"]))
    state.low = f32(state.low + alpha * f32(low - state.low))
    state.high = f32(state.high + alpha * f32(high - state.high))


def high_band(state: QualityState) -> float:
    """ln(200-400 Hz / 25-100 Hz averaged energy); -inf before the first sample."""
    if state.low is None:
        return -np.inf
    return float(np.log(max(float(state.high), 1e-30)) - np.log(max(float(state.low), 1e-30)))
