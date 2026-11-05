"""Cost model stays within the PSoC 6 budget in both number formats."""

import pytest

from pcg_hr import config, cost_model


@pytest.mark.parametrize("fmt", ["float32", "fixed"])
def test_within_budget(fmt):
    """MIPS, RAM and flash (with the code estimate) within budget; busiest block < 50 ms."""
    cfg = config.load(overrides={"profile": {"number_format": fmt}})
    totals = cost_model.totals(cost_model.profile(cfg), cfg)
    assert totals["within_budget"] and totals["peak_block_share"] < 1.0
