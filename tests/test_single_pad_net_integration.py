"""Integration tests for the single_pad_net DRC rule using real boards.

These tests load the canonical board fixtures (boards/0X-...) and verify
that:

- Board 04 (STM32 dev board, **with** STM32F103C8T6 MCU now placed) does
  not produce any single-pad-net violations.  Historical context: before
  issue #2531 the board generator emitted a stub PCB without the MCU
  footprint, leaving SWDIO/SWCLK/SWO/NRST as ghost nets; this test now
  serves as a regression guard that the MCU placement keeps those signals
  multi-pad.
- Boards 01, 02, 03 also do not report any single-pad-net violations
  (regression guard against the rule firing on legitimate designs).
  Board 05 is intentionally excluded -- its STM32G4 MCU is a placeholder
  in the schematic generator, so the gate-driver and SWD nets legitimately
  trip the rule (a real finding, not a false positive).
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
    """Regression: with the STM32F103C8T6 MCU placed, board 04 should not
    report any single-pad-net violations.  The four SWD signals
    (SWDIO/SWCLK/SWO/NRST) that historically formed ghost nets now
    terminate on PA13/PA14/PB3/NRST of U2.
    """

    def test_check_all_no_single_pad_errors(self):
        """DRCChecker.check_all() emits 0 single_pad_net errors on board 04."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(str(_BOARD_04_PCB))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_all()

        single_pad_violations = [v for v in results.violations if v.rule_id == "single_pad_net"]
        assert len(single_pad_violations) == 0, (
            f"Board 04 (post-#2531) should have 0 single_pad_net errors, "
            f"got {len(single_pad_violations)}: "
            f"{[v.message for v in single_pad_violations]}"
        )

    def test_check_single_pad_nets_in_isolation(self):
        """check_single_pad_nets() returns 0 errors on board 04 (post-#2531)."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(str(_BOARD_04_PCB))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_single_pad_nets()

        assert len(results.violations) == 0, (
            f"Board 04 (post-#2531) unexpectedly produced "
            f"{len(results.violations)} single_pad_net violations: "
            f"{[v.message for v in results.violations]}"
        )

    def test_swd_nets_have_two_pads(self):
        """Each SWD signal connects MCU U2 -> SWD header J1 (two pads each).

        This is the constructive flip-side of test_check_single_pad_nets:
        not just absent of errors, but explicitly multi-pad.
        """
        from collections import defaultdict

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(_BOARD_04_PCB))
        net_pad_counts: dict[str, int] = defaultdict(int)
        net_refs: dict[str, set[str]] = defaultdict(set)
        for fp in pcb.footprints:
            for pad in fp.pads:
                if pad.net_name:
                    net_pad_counts[pad.net_name] += 1
                    net_refs[pad.net_name].add(fp.reference or fp.name)

        for net in ("SWDIO", "SWCLK", "SWO", "NRST"):
            assert net_pad_counts[net] >= 2, (
                f"Net {net} should connect both U2 (MCU) and J1 (SWD header), "
                f"got {net_pad_counts[net]} pads on {net_refs[net]}"
            )
            # Both U2 and J1 should appear on each SWD signal.
            assert "U2" in net_refs[net], f"{net} missing MCU U2 connection"
            assert "J1" in net_refs[net], f"{net} missing SWD header J1 connection"


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

    Board 04 (STM32 devboard) is now also single-pad-net clean -- see
    ``TestBoard04SwdSignals`` for its dedicated regression coverage.

    Board 05 (BLDC motor controller) is intentionally excluded -- its
    schematic uses a placeholder for the STM32G4 MCU, so the gate-driver
    nets, current-sense returns, Hall sensor inputs, and SWD signals all
    have only one pad on the PCB.  This is exactly the design defect
    the rule is meant to catch, and would be a legitimate finding (not
    a false positive) if the test fired against board 05.
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
