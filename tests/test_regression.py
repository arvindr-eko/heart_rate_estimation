"""Golden regression: the estimator reproduces the frozen final-candidate test readings.

Needs the local recordings and output/golden; skipped otherwise. Golden values are rounded to
10 decimals (JSON export of the frozen run).
"""

import json

import pytest

from pcg_hr import data, estimator
from tests.conftest import needs_data


def test_golden_readings(cfg):
    """Identical readings on the first 40 test recordings."""
    if not needs_data(cfg["paths"]):
        pytest.skip("local data or golden readings not available")
    golden = json.loads((cfg["paths"]["golden_dir"] / "final_default_test_readings.json")
                        .read_text())
    for row in golden[:40]:
        new, _ = estimator.run(cfg, data.load_wav(cfg, row["output_filename"]))
        assert len(new) == len(row["readings"])
        for got, want in zip(new, row["readings"]):
            assert [round(v, 10) for v in got] == want
