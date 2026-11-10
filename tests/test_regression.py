"""Golden regression: the estimator reproduces the frozen test readings of the final model.

Needs the local recordings and output/golden; skipped otherwise. Readings are compared to
within 1e-6 (JSON export of the frozen run).
"""

import json

import numpy as np
import pytest

from device import estimator
from training import data
from tests.conftest import golden_file, needs_data


def test_golden_readings(cfg):
    """Identical readings on the first 40 test recordings."""
    if not needs_data(cfg):
        pytest.skip("local data or golden readings not available")
    golden = json.loads(golden_file(cfg).read_text())
    for row in golden[:40]:
        new, _ = estimator.run(cfg, data.load_wav(cfg, row["output_filename"]))
        assert len(new) == len(row["readings"])
        for got, want in zip(new, row["readings"]):
            assert np.allclose(got, want, rtol=0, atol=1e-6)
