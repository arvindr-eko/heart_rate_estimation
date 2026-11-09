"""Reference vectors for the C port: per-block inputs and outputs of the Python device code on
synthetic signals and a few test recordings, one .npz per case in paths.vectors_dir.

Per case: pcm (int16 input), band_log_energy (4, envelope samples), activation (envelope
samples), per frame t_s, features (frames, 232, 17; NaN before the first window),
log_emission (frames, 232), posterior (frames, 232), hr, frame_hr, periodicity, activation_sd,
cv (picked beats), beat_hr, p_fine, p_rough, regular_path, and the readings (t_s, bpm,
confidence).
"""

import logging

import numpy as np

from device import estimator, frontend, net, scorer
from training import augment, data

log = logging.getLogger(__name__)

SYNTHETIC = [("hr72", 72, {}), ("hr45_slow", 45, {}), ("hr150", 150, {}), ("hr250_fast", 250, {}),
             ("hr110_loud_s2", 110, {"s2_ratio": 1.3}),
             ("hr80_irregular", 80, {"interval_cv": 0.10}), ("silence", 0, {}),
             ("hum_only", 0, {"noise": "hum"})]   # periodic ripple, no beats: activation-SD gate
RECORDINGS = 3                       # first test recordings with a trusted ECG
DURATION_S = 10.0
PEAK = 16000                         # int16 peak of the synthetic cases


def record(cfg: dict, pcm: np.ndarray) -> dict:
    """Stream int16 PCM through the estimator, recording every block's and frame's outputs."""
    x = pcm.astype(np.float32) / 32768.0
    state = estimator.init(cfg, keep_trace=True)
    fe, nn = frontend.init(cfg), net.init(cfg)
    block, n_grid = cfg["stream"]["block_samples"], state.grid.size
    n_in = state.model.w1.shape[0]
    env, act, feats, loglik, post, readings = [], [], [], [], [], []
    for start in range(0, x.size - block + 1, block):
        piece = x[start:start + block]
        band = frontend.process(fe, piece)
        env.append(band)
        act.append(net.process(nn, band)[0])
        reading = estimator.process(state, piece)
        if reading is not None:
            readings.append(reading)
        if state.n_blocks % cfg["stream"]["frame_blocks"] == 0:
            f = estimator.scorer_input(state)
            feats.append(np.full((n_grid, n_in), np.nan, np.float32) if f is None else f)
            loglik.append(np.full(n_grid, np.nan) if f is None
                          else scorer.log_emission(state.model, f))
            post.append(state.trk.posterior.copy())
    trace = state.trace
    return {"pcm": pcm, "band_log_energy": np.hstack(env), "activation": np.concatenate(act),
            "t_s": np.array([f["t"] for f in trace]), "features": np.stack(feats),
            "log_emission": np.stack(loglik), "posterior": np.stack(post),
            **{k: np.array([f[k] for f in trace], dtype=float) for k in (
                "hr", "frame_hr", "periodicity", "activation_sd", "cv", "beat_hr", "p_fine",
                "p_rough")},
            "regular_path": np.array([f["path"] == "regular" for f in trace]),
            "readings": np.array(readings, dtype=float).reshape(-1, 3)}


def synthetic(cfg: dict) -> list[tuple[str, np.ndarray, float]]:
    """(name, int16 PCM, true BPM) of the synthetic cases."""
    fs = cfg["stream"]["fs_hz"]
    n = int(DURATION_S * fs)
    cases = []
    for name, hr, kw in SYNTHETIC:
        if hr:
            audio, _ = augment.synthetic_pcg(hr, duration_s=DURATION_S, fs=fs, seed=hr, **kw)
            audio = augment.add_noise(audio, fs, "white", 25.0, hr)
            pcm = np.round(audio / np.abs(audio).max() * PEAK).astype(np.int16)
        elif kw.get("noise"):
            x = augment.noise(kw["noise"], n, fs, np.random.default_rng(0))
            pcm = np.round(x / np.abs(x).max() * PEAK).astype(np.int16)
        else:
            pcm = np.zeros(n, np.int16)
        cases.append((name, pcm, float(hr)))
    return cases


def to_pcm(audio: np.ndarray) -> np.ndarray:
    """int16 PCM of a recording loaded as float in [-1, 1] (exact for 16-bit WAVs)."""
    return np.round(np.clip(audio, -1.0, 32767 / 32768) * 32768).astype(np.int16)


def recordings(cfg: dict, count: int = RECORDINGS) -> list[tuple[str, np.ndarray, float]]:
    """(name, int16 PCM, ECG reference BPM) of the first test recordings with a trusted ECG."""
    test = data.load_split(cfg, "test")
    test = test[test["ecg_trusted"] & ~test["unheard"]].head(count)
    return [(f"recording_{i}", to_pcm(data.load_wav(cfg, f)), float(ref))
            for i, (f, ref) in enumerate(zip(test["output_filename"], test["reference_bpm"]))]


def write(cfg: dict) -> None:
    """Write the synthetic and recording cases."""
    out = cfg["paths"]["vectors_dir"]
    out.mkdir(parents=True, exist_ok=True)
    cases = synthetic(cfg) + recordings(cfg)
    for name, pcm, bpm in cases:
        np.savez(out / f"{name}.npz", **record(cfg, pcm), true_bpm=bpm)
    log.info("vectors: %d synthetic and %d recording cases in %s", len(SYNTHETIC),
             len(cases) - len(SYNTHETIC), out.name)
