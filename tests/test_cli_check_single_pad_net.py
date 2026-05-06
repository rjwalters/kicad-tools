"""CLI tests for `kct check --only single_pad_net`.

The fixture used here is board 05 (BLDC motor controller), which still
emits a stub PCB without its STM32G4 MCU and therefore generates the
single-pad-net errors the rule is designed to catch.  Board 04 used to
serve this purpose but was fixed by issue #2531 (MCU placed) -- see
``test_single_pad_net_integration.py`` for the regression guard there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BOARD_05_PCB = (
    _REPO_ROOT / "boards" / "05-bldc-motor-controller" / "output" / "bldc_controller.kicad_pcb"
)


@pytest.mark.skipif(
    not _BOARD_05_PCB.exists(),
    reason=(
        "boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb not generated; "
        "run 'uv run python boards/05-bldc-motor-controller/design.py' first"
    ),
)
class TestCheckSinglePadNetCli:
    """End-to-end CLI tests for the new rule on a stub-MCU board."""

    def test_only_single_pad_net_reports_errors(self, capsys):
        """`kct check --only single_pad_net` exits 2 with errors."""
        from kicad_tools.cli.check_cmd import main

        rc = main([str(_BOARD_05_PCB), "--only", "single_pad_net"])
        assert rc == 2  # Errors found.

        captured = capsys.readouterr()
        # Some single-pad-net signal should be flagged on the stub board.
        assert "single_pad_net" in captured.out
        # The output should list at least one offending net.
        assert "Net '" in captured.out

    def test_skip_single_pad_net_excludes_rule(self, capsys):
        """`kct check --skip single_pad_net` does not include this rule_id."""
        from kicad_tools.cli.check_cmd import main

        # We don't care about the exit code (other rules may fire on
        # this board); only that the single_pad_net rule is excluded.
        main([str(_BOARD_05_PCB), "--skip", "single_pad_net"])

        captured = capsys.readouterr()
        assert "single_pad_net" not in captured.out

    def test_json_output_resolves_type(self, capsys):
        """JSON output round-trips rule_id -> type without 'unknown'."""
        from kicad_tools.cli.check_cmd import main

        rc = main(
            [
                str(_BOARD_05_PCB),
                "--only",
                "single_pad_net",
                "--format",
                "json",
            ]
        )
        assert rc == 2

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert data["summary"]["errors"] >= 1
        assert data["summary"]["passed"] is False

        violations = data["violations"]
        assert len(violations) >= 1
        for v in violations:
            assert v["rule_id"] == "single_pad_net"
            # Critical: must NOT resolve to 'unknown' -- verifies the
            # ViolationType enum + alias entry are wired correctly.
            assert v["type"] == "single_pad_net"
            assert v["severity"] == "error"
