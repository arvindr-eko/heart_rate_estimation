"""Config loading, variant merging and preflight checks."""

import copy
import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def deep_merge(base: dict, override: dict) -> dict:
    """Return base with override merged in recursively; neither input is modified."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load(path: str | Path = PROJECT_ROOT / "config.yml", variant: str | None = None,
         overrides: dict | None = None) -> dict:
    """Load the YAML config, apply a named variant and explicit overrides, resolve paths."""
    with open(path) as f:
        cfg = yaml.safe_load(f)
    variants = cfg.pop("variants", {})
    if variant is not None:
        cfg = deep_merge(cfg, variants[variant])
    if overrides:
        cfg = deep_merge(cfg, overrides)
    cfg["name"] = variant or "base"
    cfg["paths"] = {k: PROJECT_ROOT / v for k, v in cfg["paths"].items()}
    validate(cfg)
    return cfg


def variant_names(path: str | Path = PROJECT_ROOT / "config.yml") -> list[str]:
    """Names of the variants defined in the config."""
    with open(path) as f:
        return list(yaml.safe_load(f).get("variants", {}))


def validate(cfg: dict) -> None:
    """Raise on inconsistent stream and front-end sizes."""
    fs = cfg["stream"]["fs_hz"]
    block = cfg["stream"]["block_samples"]
    for rate_key in ("env_rate_hz", "wave_rate_hz"):
        rate = cfg["frontend"][rate_key]
        if fs % rate or block % (fs // rate):
            raise ValueError(f"{rate_key}={rate} must divide fs and the block size")


def preflight(cfg: dict, need_splits: bool = True) -> None:
    """Check inputs exist and outputs are writable; log one line per check, raise once."""
    failures = []
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
        if not ok:
            failures.append(name)
    if failures:
        raise RuntimeError(f"preflight failed: {', '.join(failures)}")
