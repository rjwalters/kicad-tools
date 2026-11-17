"""``relocate_pad_edge_vias(same_net_pad_gap_mm=...)`` clears same-net slits (Issue #8979).

Board 02's grid route seats escape vias whose annulus sits just outside the
via's own SMD pad: a same-net ``physical_copper_gap`` slit ``kct check --mfr``
grades as an error since #8938.  The opt-in band moves such a via clear of the
band; with the band off the pass is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.relocate_in_pad_vias import _same_net_slit_ok, relocate_pad_edge_vias
from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.physical_gap import check_physical_copper_gap

BAND = 0.15
_RULES = get_profile("jlcpcb").get_design_rules(2)

# A 1.0 x 1.0 mm SIG1 pad, right copper edge at x=100.5, and a d0.3/s0.6 via
# whose annulus is TANGENT to that edge (centre x=100.8).  Its drill edge sits
# 0.15 mm from the aperture, clear of the mask dam, so only the slit band flags
# it.  The escape track leaves along +X.
_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd {shape} (at 0 0) (size 1.0 1.0) (layers "F.Cu"){rratio} (net 1 "SIG1"))
  )
  (via (at {x} 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
  (segment (start {x} 100) (end 104 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "s1"))
)
"""


def _load(tmp_path: Path, x: float, shape: str = "rect") -> PCB:
    rratio = " (roundrect_rratio 0.25)" if shape == "roundrect" else ""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_BOARD.format(x=x, shape=shape, rratio=rratio))
    return PCB.load(path)


def _slits(pcb: PCB) -> list:
    pcb.save(pcb.path)
    results = check_physical_copper_gap(PCB.load(pcb.path), BAND)
    return [v for v in results.violations if v.rule_id == "physical_copper_gap"]


def test_tangent_via_is_a_slit_and_the_band_moves_it_clear(tmp_path):
    pcb = _load(tmp_path, 100.8)
    assert _slits(pcb), "precondition: a tangent annulus leaves a same-net slit"

    result = relocate_pad_edge_vias(pcb, _RULES, same_net_pad_gap_mm=BAND)

    assert len(result.moved) == 1 and not result.unresolvable
    (via,) = pcb.vias
    # Along its escape track, to the first whole step with gap >= band.
    assert via.position[1] == pytest.approx(100.0)
    assert via.position[0] - 0.3 - 100.5 >= BAND - 1e-6
    assert _slits(pcb) == []


def test_band_off_leaves_the_tangent_via_alone(tmp_path):
    """Control: without the band the pass is the pre-#8979 mask-dam pass."""
    pcb = _load(tmp_path, 100.8)
    result = relocate_pad_edge_vias(pcb, _RULES)
    assert result.moved == [] and result.unresolvable == []
    assert pcb.vias[0].position[0] == pytest.approx(100.8)


def test_via_clear_of_the_band_is_left_alone(tmp_path):
    pcb = _load(tmp_path, 101.0)
    assert _slits(pcb) == []
    result = relocate_pad_edge_vias(pcb, _RULES, same_net_pad_gap_mm=BAND)
    assert result.moved == [] and result.unresolvable == []


def test_roundrect_corner_is_measured_on_the_true_outline(tmp_path):
    """A via touching a roundrect's bbox corner is not touching its copper."""
    pcb = _load(tmp_path, 100.0, shape="roundrect")
    from kicad_tools.validate.rules.clearance import _pad_polygon

    fp = pcb.footprints[0]
    polygon = _pad_polygon(fp.pads[0], fp)
    # Bbox corner (100.5, 100.5); a seat diagonally tangent to that corner.
    off = 0.3 / 2**0.5
    assert not _same_net_slit_ok(100.5 + off, 100.5 + off, 0.3, [polygon], BAND)


def test_deep_overlap_is_one_copper_union(tmp_path):
    pcb = _load(tmp_path, 100.0)
    fp = pcb.footprints[0]
    from kicad_tools.validate.rules.clearance import _pad_polygon

    polygon = _pad_polygon(fp.pads[0], fp)
    # Annulus 0.15 mm into the pad edge (centre outside the pad).
    assert _same_net_slit_ok(100.65, 100.0, 0.3, [polygon], BAND)
    # A 0.03 mm overlap is rejected conservatively: the checker finds no slit
    # on this straight edge with the track covering the join, but a tangent
    # seat (0.0408 mm slit, above) is only 0.03 mm away from it.
    assert not _same_net_slit_ok(100.77, 100.0, 0.3, [polygon], BAND)
