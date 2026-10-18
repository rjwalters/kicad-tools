"""``silkscreen_text_thickness``: visible silk text thinner than the fab floor (#7228)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.silkscreen import (  # noqa: E402
    check_all_silkscreen,
    check_silkscreen_text_thickness,
)

BOARD02 = (
    Path(__file__).resolve().parent.parent
    / "boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb"
)


@pytest.fixture
def rules():
    return get_profile("jlcpcb").get_design_rules(layers=2)


def _ref(pcb: PCB, reference: str):
    footprint = next(fp for fp in pcb.footprints if fp.reference == reference)
    return next(t for t in footprint.texts if t.text_type == "reference")


def test_unmutated_board_is_clean(rules):
    """0.15 mm text is AT the floor and must not trigger."""
    pcb = PCB.load(BOARD02)
    assert _ref(pcb, "C2").font_thickness == pytest.approx(rules.min_silkscreen_width_mm)
    assert check_silkscreen_text_thickness(pcb, rules).violations == []


def test_thin_reference_is_flagged(rules):
    pcb = PCB.load(BOARD02)
    _ref(pcb, "C2").font_thickness = 0.08
    found = check_silkscreen_text_thickness(pcb, rules).violations
    assert [(v.rule_id, v.severity, v.items) for v in found] == [
        ("silkscreen_text_thickness", "warning", ("C2 (reference)",))
    ]
    assert found[0].actual_value == pytest.approx(0.08)
    assert found[0].required_value == pytest.approx(rules.min_silkscreen_width_mm)
    assert any(v.rule_id == "silkscreen_text_thickness" for v in check_all_silkscreen(pcb, rules))


def test_hidden_thin_text_is_skipped(rules):
    pcb = PCB.load(BOARD02)
    text = _ref(pcb, "C2")
    text.font_thickness = 0.08
    text.hidden = True
    assert check_silkscreen_text_thickness(pcb, rules).violations == []
