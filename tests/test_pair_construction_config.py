"""Non-finite environment overrides must not disable construction deadlines."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_corridor_reserve_rejects_nonfinite_environment_override(value):
    env = os.environ.copy()
    env["KCT_CONSTRUCTION_CORRIDOR_WALL_RESERVE"] = value
    result = subprocess.run(
        [sys.executable, "-c", "import kicad_tools.router.pair_construction"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "KCT_CONSTRUCTION_CORRIDOR_WALL_RESERVE must be finite" in result.stderr
