"""Integration tests for the single_pad_net DRC rule using real boards.

These tests load the canonical board fixtures (boards/0X-...) and verify
that:

- Board 04 (STM32 dev board, no MCU) reports exactly 4 single-pad-net
  errors for SWDIO, SWCLK, SWO, NRST -- the four SWD signals that have
  no MCU footprint to terminate on.
- Boards 01, 02, 03 do not report any single-pad-net violations
  (regression guard against the rule firing on legitimate designs).
  Board 05 is intentionally excluded -- its STM32G4 MCU is a placeholder
  in the schematic generator, so the gate-driver and SWD nets legitimately
  trip the rule (a real finding, not a false positive).
- The DRC checker's ``check_all()`` plumbing routes the violations
  through to ``error_count`` and ``DRCStatus.blocking_count``, which
  in turn drives ``AuditVerdict.NOT_READY`` -- exactly the behaviour
  the issue asks for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Repo root: tests/ is at the top of the repo, so parent of this file is
# the tests dir, and parent of that is the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent


# Board 04 stores its PCB under output/stm32_devboard.kicad_pcb (the
# subdirectory name uses underscores, not dashes).
_BOARD_04_PCB = _REPO_ROOT / "boards" / "04-stm32-devboard" / "output" / "stm32_devboard.kicad_pcb"


@pytest.mark.skipif(
    not _BOARD_04_PCB.exists(),
    reason=(
        "boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb not generated; "
        "run 'uv run python boards/04-stm32-devboard/generate_design.py' first"
    ),
)
class TestBoard04SwdSignals:
    """Board 04 (no MCU) should report exactly 4 single-pad SWD nets."""

    def test_check_all_includes_single_pad_errors(self):
        """DRCChecker.check_all() emits >=4 single_pad_net errors on board 04."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(str(_BOARD_04_PCB))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_all()

        single_pad_violations = [v for v in results.violations if v.rule_id == "single_pad_net"]
        assert len(single_pad_violations) == 4

        # All four are errors (not warnings).
        for v in single_pad_violations:
            assert v.severity == "error"

        flagged_nets = {v.nets[0] for v in single_pad_violations}
        assert flagged_nets == {"SWDIO", "SWCLK", "SWO", "NRST"}

        # All four pads should be on J1 (the SWD header).
        flagged_refs = {v.items[0].split("-")[0] for v in single_pad_violations}
        assert flagged_refs == {"J1"}

        # The total error count should be at least 4 (other rules may
        # also fire on this board, hence >=, not ==).
        assert results.error_count >= 4

    def test_check_single_pad_nets_in_isolation(self):
        """check_single_pad_nets() returns exactly 4 errors on board 04."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(str(_BOARD_04_PCB))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_single_pad_nets()

        assert len(results.violations) == 4
        assert all(v.rule_id == "single_pad_net" for v in results.violations)
        assert all(v.severity == "error" for v in results.violations)

    def test_audit_verdict_not_ready(self):
        """A board with single-pad signal nets fails the audit gate.

        This test exercises the same code path as ``Auditor.audit()``
        without requiring the rest of the project structure (project
        file, schematic, etc.).  We hand-build a DRCStatus from the
        same ``_check_drc`` method the auditor uses internally and
        verify the verdict mapping wires it up to NOT_READY.
        """
        from kicad_tools.audit.auditor import (
            AuditResult,
            AuditVerdict,
            ManufacturingAudit,
        )
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(_BOARD_04_PCB))
        # ManufacturingAudit needs a path argument for project resolution
        # but we only need its _check_drc helper for this assertion.
        auditor = ManufacturingAudit(_BOARD_04_PCB, manufacturer="jlcpcb", layers=2)
        drc_status = auditor._check_drc(pcb)

        # The single_pad_net rule should drive blocking_count >= 4.
        assert drc_status.blocking_count >= 4
        # Verify the rule fires through the DRC plumbing.
        assert drc_status.violations_by_type.get("single_pad_net", 0) == 4

        result = AuditResult()
        result.drc = drc_status
        # Verdict is NOT_READY because drc.blocking_count > 0.
        assert result.verdict == AuditVerdict.NOT_READY


_REGRESSION_BOARDS = [
    (
        "01-voltage-divider",
        _REPO_ROOT / "boards" / "01-voltage-divider" / "output" / "voltage_divider.kicad_pcb",
    ),
    (
        "02-charlieplex-led",
        _REPO_ROOT / "boards" / "02-charlieplex-led" / "output" / "charlieplex_3x3.kicad_pcb",
    ),
    (
        "03-usb-joystick",
        _REPO_ROOT / "boards" / "03-usb-joystick" / "output" / "usb_joystick.kicad_pcb",
    ),
]


class TestRegressionBoards:
    """Boards 01, 02, 03 (fully populated) should NOT trip the rule.

    These are the canonical "good design" reference boards.  Any
    single_pad_net violation here would indicate a false positive in
    the rule logic.

    Board 05 (BLDC motor controller) is intentionally excluded -- its
    schematic uses a placeholder for the STM32G4 MCU, so the gate-driver
    nets, current-sense returns, Hall sensor inputs, and SWD signals all
    have only one pad on the PCB.  This is exactly the design defect
    the rule is meant to catch, and would be a legitimate finding (not
    a false positive) if the test fired against board 05.  Board 04
    (also missing its MCU but otherwise simpler) is the canonical
    "expected to fail" fixture covered by ``TestBoard04SwdSignals``.
    """

    @pytest.mark.parametrize(
        "name,pcb_path",
        _REGRESSION_BOARDS,
        ids=[name for name, _ in _REGRESSION_BOARDS],
    )
    def test_no_single_pad_violations(self, name: str, pcb_path: Path):
        """Reference boards do not produce single_pad_net errors."""
        if not pcb_path.exists():
            pytest.skip(
                f"Board fixture {name} not generated at {pcb_path}; "
                f"run 'uv run python boards/{name}/generate_design.py' first"
            )

        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(str(pcb_path))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_single_pad_nets()

        assert len(results.violations) == 0, (
            f"Board {name} unexpectedly produced "
            f"{len(results.violations)} single_pad_net violations: "
            f"{[v.message for v in results.violations]}"
        )
