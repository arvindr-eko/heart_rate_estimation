"""Config loading (configs/device.yml and configs/training.yml), path resolution, validation and
preflight checks."""

import logging
from importlib import metadata
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILES = (PROJECT_ROOT / "configs" / "device.yml", PROJECT_ROOT / "configs" / "training.yml")
PACKAGE = "heart-rate-estimation"


def version() -> str:
    """Software version (pyproject.toml, semantic versioning); 'unknown' when not installed."""
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        return "unknown"


def load() -> dict:
    """Load and merge the YAML configs (device settings, training settings); paths resolved
    against the project root. A section may appear in one file only."""
    cfg = {}
    for path in CONFIG_FILES:
        with open(path) as f:
            part = yaml.safe_load(f)
        clash = set(cfg) & set(part)
        if clash:
            raise ValueError(f"{Path(path).name}: sections also in another config: {sorted(clash)}")
        cfg.update(part)
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
        checks += [("split_manifest", cfg["paths"]["split_manifest"].is_file()),
                   ("labels", cfg["paths"]["labels"].is_file()),
                   ("ecg_dir", cfg["paths"]["ecg_dir"].is_dir())]
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
