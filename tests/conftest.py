"""Shared fixtures: the final config, and a marker for tests that need the local data."""

import pytest

from training import config


@pytest.fixture(scope="session")
def cfg():
    """Final-candidate config."""
    return config.load()


def golden_file(cfg: dict):
    """Frozen test readings of the configured model."""
    return cfg["paths"]["golden_dir"] / f"{cfg['weights']}_test_readings.json"


def needs_data(cfg: dict) -> bool:
    """True when the recordings, the split and the golden readings are available locally."""
    return (cfg["paths"]["wav_dir"].is_dir() and cfg["paths"]["split_manifest"].is_file()
            and golden_file(cfg).is_file())
