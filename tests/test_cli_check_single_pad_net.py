"""CLI tests for `kct check --only single_pad_net` against board 04."""

from __future__ import annotations

import json
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
class TestCheckSinglePadNetCli:
    """End-to-end CLI tests for the new rule on board 04."""

    def test_only_single_pad_net_reports_four_errors(self, capsys):
        """`kct check --only single_pad_net` exits 2 with 4 errors."""
        from kicad_tools.cli.check_cmd import main

        rc = main([str(_BOARD_04_PCB), "--only", "single_pad_net"])
        assert rc == 2  # Errors found.

        captured = capsys.readouterr()
        # Each of the 4 SWD signals should appear in the output.
        for net_name in ("SWDIO", "SWCLK", "SWO", "NRST"):
            assert net_name in captured.out, f"Expected '{net_name}' in output:\n{captured.out}"
        # All four pads should be on J1.
        assert "J1" in captured.out

    def test_skip_single_pad_net_excludes_rule(self, capsys):
        """`kct check --skip single_pad_net` does not include this rule_id."""
        from kicad_tools.cli.check_cmd import main

        # We don't care about the exit code (other rules may fire on
        # this board); only that the single_pad_net rule is excluded.
        main([str(_BOARD_04_PCB), "--skip", "single_pad_net"])

        captured = capsys.readouterr()
        assert "single_pad_net" not in captured.out

    def test_json_output_resolves_type(self, capsys):
        """JSON output round-trips rule_id -> type without 'unknown'."""
        from kicad_tools.cli.check_cmd import main

        rc = main(
            [
                str(_BOARD_04_PCB),
                "--only",
                "single_pad_net",
                "--format",
                "json",
            ]
        )
        assert rc == 2

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert data["summary"]["errors"] == 4
        assert data["summary"]["passed"] is False

        violations = data["violations"]
        assert len(violations) == 4
        for v in violations:
            assert v["rule_id"] == "single_pad_net"
            # Critical: must NOT resolve to 'unknown' -- verifies the
            # ViolationType enum + alias entry are wired correctly.
            assert v["type"] == "single_pad_net"
            assert v["severity"] == "error"
