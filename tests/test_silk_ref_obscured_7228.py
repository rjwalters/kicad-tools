"""``silk_ref_obscured`` and off-board ``silk_edge_clearance`` text (#7228)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.silkscreen import (  # noqa: E402
    check_silk_edge_clearance,
    check_silk_ref_obscured,
)

ROOT = Path(__file__).resolve().parent.parent
BOARD02 = ROOT / "boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb"
BOARD03 = ROOT / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"


@pytest.fixture
def rules():
    return get_profile("jlcpcb").get_design_rules(layers=2)


def _ref(pcb: PCB, reference: str):
    footprint = next(fp for fp in pcb.footprints if fp.reference == reference)
    return next(t for t in footprint.texts if t.text_type == "reference")


def test_unmutated_boards_have_no_obscured_ref(rules):
    for path in (BOARD02, BOARD03):
        assert check_silk_ref_obscured(PCB.load(path), rules).violations == [], path.name


def test_hidden_reference_is_flagged(rules):
    pcb = PCB.load(BOARD02)
    _ref(pcb, "D1").hidden = True
    found = check_silk_ref_obscured(pcb, rules).violations
    assert [(v.rule_id, v.severity, v.items) for v in found] == [
        ("silk_ref_obscured", "warning", ("D1 (reference)", "hidden"))
    ]


def test_reference_inside_own_body_is_flagged(rules):
    pcb = PCB.load(BOARD02)
    _ref(pcb, "U1").position = (3.81, 3.81)  # DIP-8 body centre
    found = check_silk_ref_obscured(pcb, rules).violations
    assert [v.items for v in found] == [("U1 (reference)", "courtyard")]


def test_reference_off_board_is_flagged_as_outside(rules):
    pcb = PCB.load(BOARD02)
    _ref(pcb, "D2").position = (0.0, -15.0)  # D2 is at y=8: 7 mm above the top edge
    found = check_silk_edge_clearance(pcb, rules).violations
    assert [(v.items, v.actual_value) for v in found] == [(("D2 (reference)", "Edge.Cuts"), None)]
    assert "outside" in found[0].message


def test_overhanging_footprint_outline_is_not_flagged_outside(rules):
    """Board 03's J1 body strokes overhang the edge by design (#6494)."""
    assert check_silk_edge_clearance(PCB.load(BOARD03), rules).violations == []
