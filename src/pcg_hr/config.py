"""Config loading, variant merging and preflight checks."""

import copy
import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config.yml"


def deep_merge(base: dict, override: dict) -> dict:
    """Return base with override merged in recursively; neither input is modified."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load(path: str | Path = DEFAULT_CONFIG, variant: str | None = None,
         overrides: dict | None = None) -> dict:
    """Load the YAML config, apply a named variant and explicit overrides, resolve paths."""
    with open(path) as f:
        cfg = yaml.safe_load(f)
    variants = cfg.pop("variants", {})
    if variant is not None:
        cfg = deep_merge(cfg, variants[variant])
    if overrides:
        cfg = deep_merge(cfg, overrides)
    cfg["name"] = variant or "final"
    cfg["paths"] = {k: PROJECT_ROOT / v for k, v in cfg["paths"].items()}
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    """Raise on inconsistent stream and front-end sizes."""
    fs, block = cfg["stream"]["fs_hz"], cfg["stream"]["block_samples"]
    rate = cfg["frontend"]["env_rate_hz"]
    if fs % rate or block % (fs // rate):
        raise ValueError(f"env_rate_hz={rate} must divide fs_hz and the block size")


def preflight(cfg: dict, need_splits: bool = True) -> None:
    """Check inputs exist and outputs are writable; log one line per check, raise once."""
    checks = [("manifest", cfg["paths"]["manifest"].is_file()),
              ("wav_dir", cfg["paths"]["wav_dir"].is_dir())]
    if need_splits:
        checks.append(("split_manifest", cfg["paths"]["split_manifest"].is_file()))
    for key in ("runs_dir", "models_dir", "cache_dir"):
        out = cfg["paths"][key]
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".write_probe"
        try:
            probe.touch()
            probe.unlink()
            checks.append((key, True))
        except OSError:
            checks.append((key, False))
    for name, ok in checks:
        log.info("preflight %-15s %s", name, "ok" if ok else "FAIL")
    failures = [name for name, ok in checks if not ok]
    if failures:
        raise RuntimeError(f"preflight failed: {', '.join(failures)}")
