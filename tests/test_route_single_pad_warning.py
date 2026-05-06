"""Test that ``kct route`` emits a top-of-output warning for single-pad nets."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BOARD_04_PCB = _REPO_ROOT / "boards" / "04-stm32-devboard" / "output" / "stm32_devboard.kicad_pcb"


@pytest.mark.skipif(
    not _BOARD_04_PCB.exists(),
    reason=(
        "boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb not generated; "
        "run 'uv run python boards/04-stm32-devboard/generate_design.py' first"
    ),
)
class TestRouteSinglePadWarning:
    """`kct route` should warn about SWDIO/SWCLK/SWO/NRST on board 04."""

    def test_warning_block_naming_swd_signals(self):
        """The route command surfaces the four floating SWD signals."""
        # Use --dry-run so we don't actually route or write output.  The
        # banner is printed before any routing work begins, so we do not
        # need to wait for a full route.
        env = os.environ.copy()
        # Avoid stale-pipx noise polluting stdout in CI environments.
        env["KCT_NO_DEV_WARN"] = "1"

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "route",
                str(_BOARD_04_PCB),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=str(_REPO_ROOT),
        )

        # We don't care whether routing itself succeeded -- only that
        # the banner printed.  Combine streams since output ordering
        # varies between platforms.
        combined = result.stdout + result.stderr

        assert "single-pad signal net" in combined, (
            f"Expected single-pad warning in route output:\n{combined[:4000]}"
        )
        # All four SWD signals named.
        for net in ("SWDIO", "SWCLK", "SWO", "NRST"):
            assert net in combined, f"Expected '{net}' in route output:\n{combined[:4000]}"
        # The banner points users at the check command.
        assert "kct check" in combined and "single_pad_net" in combined
