"""Shared fixtures: the final config, and a marker for tests that need the local data."""

import pytest

from training import config


@pytest.fixture(scope="session")
def cfg():
    """Final-candidate config."""
    return config.load()


def needs_data(cfg_paths: dict) -> bool:
    """True when the recordings, labels and golden readings are available locally."""
    return all(cfg_paths[k].exists() for k in ("wav_dir", "split_manifest", "golden_dir"))
