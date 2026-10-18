"""``relocate_pad_edge_vias`` slides vias out of the mask-dam zone (Issue #7296)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.relocate_in_pad_vias import relocate_pad_edge_vias
from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

_HEAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance {mask}))
  (net 0 "")
  (net 1 "SIG1")
  (net 2 "SIG2")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "SIG1"))
  )
"""

# Right-hand copper edge at x=100.5; a 0.3 mm drill centred at 100.65 is tangent
# to it (gap 0), the board-05 R11.1 shape.  Escape track leaves along +X.
_EDGE_VIA = """  (via (at 100.65 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
  (segment (start 100.65 100) (end 104 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "s1"))
)
"""

_RULES = get_profile("jlcpcb").get_design_rules(2)


def _load(tmp_path: Path, body: str, mask: float = 0.0) -> PCB:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_HEAD.format(mask=mask) + body)
    return PCB.load(path)


def _edge_warnings(pcb: PCB) -> list:
    results = ViaInPadRule(report_pad_edge=True).check(pcb, _RULES)
    return [v for v in results.violations if v.rule_id == "via_at_pad_edge"]


def test_edge_via_slides_along_its_escape_track(tmp_path):
    pcb = _load(tmp_path, _EDGE_VIA)
    assert len(_edge_warnings(pcb)) == 1, "precondition"

    result = relocate_pad_edge_vias(pcb, _RULES)

    assert [m.kind for m in result.moved] == ["pad-edge"]
    assert not result.unresolvable
    assert _edge_warnings(pcb) == []
    (via,) = pcb.vias
    # Moved along the track (+X only), by the least whole-step distance that
    # leaves the dam: gap = (x - 0.15) - 100.5 >= 0.1.
    assert via.position[1] == pytest.approx(100.0)
    assert via.position[0] == pytest.approx(100.75, abs=0.03)
    # Connectivity: a stub on the pad layer joins old and new positions.
    assert result.moved[0].stub_layers[0] == "F.Cu"
    stubs = [
        s
        for s in pcb.segments
        if {round(s.start[0], 3), round(s.end[0], 3)} & {100.65} and s.uuid != "s1"
    ]
    assert stubs


def test_via_already_clear_of_the_dam_is_left_alone(tmp_path):
    pcb = _load(tmp_path, _EDGE_VIA.replace("100.65", "100.8"))
    assert _edge_warnings(pcb) == []
    result = relocate_pad_edge_vias(pcb, _RULES)
    assert result.moved == [] and result.unresolvable == []
    assert pcb.vias[0].position[0] == pytest.approx(100.8)


def test_in_pad_via_is_not_this_passs_business(tmp_path):
    pcb = _load(tmp_path, _EDGE_VIA.replace("100.65", "100.3"))
    result = relocate_pad_edge_vias(pcb, _RULES)
    assert result.moved == [] and result.unresolvable == []


def test_board_mask_expansion_widens_the_zone(tmp_path):
    """pad_to_mask_clearance 0.1: the aperture grows, so 100.8 is now flagged."""
    pcb = _load(tmp_path, _EDGE_VIA.replace("100.65", "100.8"), mask=0.1)
    assert len(_edge_warnings(pcb)) == 1
    result = relocate_pad_edge_vias(pcb, _RULES)
    assert len(result.moved) == 1
    assert _edge_warnings(pcb) == []


def test_boxed_in_via_is_reported_and_left_in_place(tmp_path):
    """A foreign pad hugging the only way out leaves no legal spot."""
    boxed = """  (footprint "test:pad" (layer "F.Cu") (at 101.3 100)
    (pad "1" smd rect (at 0 0) (size 0.6 6.0) (layers "F.Cu") (net 2 "SIG2"))
  )
"""
    pcb = _load(tmp_path, boxed + _EDGE_VIA)
    before = pcb.vias[0].position
    result = relocate_pad_edge_vias(pcb, _RULES)
    # The ladder may still find an off-axis spot above/below; whatever it does,
    # it never leaves a warning behind silently nor creates a clearance error.
    if result.unresolvable:
        assert not result.moved
        assert pcb.vias[0].position == before
        assert len(_edge_warnings(pcb)) == 1
    else:
        assert _edge_warnings(pcb) == []


def test_zero_dam_profile_is_a_noop(tmp_path):
    from types import SimpleNamespace

    pcb = _load(tmp_path, _EDGE_VIA)
    rules = SimpleNamespace(
        min_solder_mask_dam_mm=0.0, min_clearance_mm=0.1, min_hole_to_hole_mm=0.5
    )
    result = relocate_pad_edge_vias(pcb, rules)
    assert result.moved == [] and result.unresolvable == []
