"""Clearance-safety gate for the drill-clearance repairer (issue #4408).

Before #4408 ``DrillClearanceRepairer._slide_via`` slid a via by the shortfall
without checking that the *new* position was itself legal -- so a slide could
resolve one hole-to-hole pair while crowding the neighbour on the other side (or
shorting a foreign net).  That is exactly why #4017 had to be relocated by hand.

These tests pin the new behaviour: when the active manufacturer's
:class:`DesignRules` are supplied, a slide that would introduce a NEW violation
is **declined** (the via is left in place and counted in ``skipped_unsafe``),
while the legacy unchecked-slide behaviour is preserved when no rules are given.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.core.types import Severity
from kicad_tools.drc.repair_drill_clearance import DrillClearanceRepairer
from kicad_tools.drc.violation import DRCViolation, Location, ViolationType
from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB

_SIZE = 0.3
_DRILL = 0.15


def _crowded_board(path: Path) -> None:
    """Three vias where sliding the middle one away from its neighbour crowds a
    third via on the far side.

    * via A (net A) at (5.0, 5.00)  -- the violation partner
    * via B (net B) at (5.0, 5.35)  -- the via that would be slid (gap 0.20 < 0.5)
    * via C (net C) at (5.0, 5.85)  -- sliding B north to clear A lands ~0.19 mm
                                       from C (a fresh hole-to-hole violation)

    Board is created un-centred so board-relative == file-absolute coordinates,
    keeping the DRC location arithmetic simple.
    """
    pcb = PCB.create(width=40.0, height=40.0, center=False)
    pcb.add_via(5.0, 5.00, size=_SIZE, drill=_DRILL, net="A")
    pcb.add_via(5.0, 5.35, size=_SIZE, drill=_DRILL, net="B")
    pcb.add_via(5.0, 5.85, size=_SIZE, drill=_DRILL, net="C")
    # A north-going escape on B's net gives the slide a direction toward C.
    pcb.add_trace((5.0, 5.35), (5.0, 6.0), width=0.15, layer="B.Cu", net="B")
    pcb.save(str(path))


def _hole_to_hole_violation() -> DRCViolation:
    """A hole-to-hole violation between via A and via B (0.20 mm < 0.50 mm).

    Location is nudged toward via A so the repairer's two-nearest selection
    picks (A, B) and slides B (the second-nearest), which owns the escape.
    """
    return DRCViolation(
        type=ViolationType.DRILL_CLEARANCE,
        type_str="drill_clearance",
        severity=Severity.ERROR,
        message="Hole-to-hole clearance 0.200mm < minimum 0.500mm",
        locations=[Location(x_mm=5.0, y_mm=5.05)],
        required_value_mm=0.5,
        actual_value_mm=0.2,
    )


def _via_b_position(pcb_path: Path) -> tuple[float, float]:
    pcb = PCB.load(str(pcb_path))
    via = min(pcb.vias, key=lambda v: abs(v.position[1] - 5.35))
    return via.position


def test_unsafe_slide_declined_with_design_rules(tmp_path: Path) -> None:
    """With design rules, a slide that would crowd via C is declined."""
    pcb_path = tmp_path / "crowded.kicad_pcb"
    _crowded_board(pcb_path)
    before = _via_b_position(pcb_path)

    rules = get_profile("jlcpcb-tier1").get_design_rules()
    repairer = DrillClearanceRepairer(pcb_path)
    result = repairer.repair([_hole_to_hole_violation()], design_rules=rules)

    assert result.repaired == 0, "the unsafe slide must NOT be applied"
    assert result.skipped_unsafe == 1
    # Nothing was written / moved.
    assert _via_b_position(pcb_path) == before


def test_legacy_slide_applied_without_design_rules(tmp_path: Path) -> None:
    """Backward compat: with no design rules the via is still slid (unchecked)."""
    pcb_path = tmp_path / "crowded.kicad_pcb"
    _crowded_board(pcb_path)
    before = _via_b_position(pcb_path)

    repairer = DrillClearanceRepairer(pcb_path)
    result = repairer.repair([_hole_to_hole_violation()])  # no design_rules

    assert result.repaired == 1
    assert result.skipped_unsafe == 0
    repairer.save(str(pcb_path))
    # The via moved (north, toward C) -- the legacy unchecked behaviour.
    assert _via_b_position(pcb_path) != before


def test_safe_slide_still_applied_with_design_rules(tmp_path: Path) -> None:
    """A slide into open space is applied even when design rules are supplied."""
    pcb_path = tmp_path / "open.kicad_pcb"
    # via A + via B violate; there is NO third via to crowd, so sliding B north
    # to clear A is clearance-safe and must be applied.
    pcb = PCB.create(width=40.0, height=40.0, center=False)
    pcb.add_via(5.0, 5.00, size=_SIZE, drill=_DRILL, net="A")
    pcb.add_via(5.0, 5.35, size=_SIZE, drill=_DRILL, net="B")
    pcb.add_trace((5.0, 5.35), (5.0, 7.0), width=0.15, layer="B.Cu", net="B")
    pcb.save(str(pcb_path))
    before = _via_b_position(pcb_path)

    rules = get_profile("jlcpcb-tier1").get_design_rules()
    repairer = DrillClearanceRepairer(pcb_path)
    result = repairer.repair([_hole_to_hole_violation()], design_rules=rules)

    assert result.repaired == 1
    assert result.skipped_unsafe == 0
    repairer.save(str(pcb_path))
    assert _via_b_position(pcb_path) != before
