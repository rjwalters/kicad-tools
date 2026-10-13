"""Overlapping pad copper is one conductor in strict mode (Issue #6435).

An exposed pad (EP) built from an F.Cu rect, a B.Cu rect and ``*.Cu``
thermal-via pads -- all sharing one pad number -- must read as one conductor,
because the PTH pads physically overlap both rects.  Before the fix strict mode
gave each occurrence its own node (#5151) but never bonded overlapping pads,
so the surface rects were stranded from the plane the thermal vias reach.

The bond is geometric: pad numbers are never consulted, and two pads that
overlap in XY but share no copper layer stay separate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import NetStatusAnalyzer

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal)
    (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
"""


def _ep_board(*, thermal_vias: bool = True, zone: bool = True) -> str:
    """U1 has an F.Cu + B.Cu EP rect (pad 9) and two ``*.Cu`` thermal vias.

    J1.1 is a through-hole GND pad elsewhere that reaches only the In1.Cu fill.
    """
    vias = (
        """
    (pad "9" thru_hole circle (at -0.5 0) (size 0.5 0.5) (drill 0.2)
      (layers "*.Cu") (net 1 "GND"))
    (pad "9" thru_hole circle (at 0.5 0) (size 0.5 0.5) (drill 0.2)
      (layers "*.Cu") (net 1 "GND"))"""
        if thermal_vias
        else ""
    )
    fill = (
        """
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (uuid "z1")
    (connect_pads yes (clearance 0.2)) (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 15 15) (xy 35 15) (xy 35 25) (xy 15 25)))
    (filled_polygon (layer "In1.Cu")
      (pts (xy 15 15) (xy 35 15) (xy 35 25) (xy 15 25))))"""
        if zone
        else ""
    )
    return (
        _HEADER
        + f"""
  (footprint "Pkg:EP_ThermalVias" (layer "F.Cu") (uuid "fpU1") (at 20 20)
    (property "Reference" "U1" (at 0 -3 0) (layer "F.SilkS") (uuid "refU1"))
    (pad "9" smd rect (at 0 0) (size 2 2) (layers "F.Cu" "F.Mask") (net 1 "GND"))
    (pad "9" smd rect (at 0 0) (size 1.8 1.8) (layers "B.Cu") (net 1 "GND")){vias})
  (footprint "Conn:Pin" (layer "F.Cu") (uuid "fpJ1") (at 30 20)
    (property "Reference" "J1" (at 0 -2 0) (layer "F.SilkS") (uuid "refJ1"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1)
      (layers "*.Cu" "*.Mask") (net 1 "GND"))){fill}
)
"""
    )


def _gnd(tmp_path: Path, text: str, *, strict: bool = True):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(text)
    status = NetStatusAnalyzer(path, strict=strict).analyze().get_net("GND")
    assert status is not None
    return status


def test_ep_rects_bond_through_thermal_vias_to_inner_fill(tmp_path):
    status = _gnd(tmp_path, _ep_board())
    assert status.total_pads == 5
    assert status.island_count == 1
    assert status.unconnected_pads == []
    assert status.status == "complete"


def test_ep_without_fill_is_one_conductor_but_far_pad_open(tmp_path):
    """The EP pieces bond to each other; J1 has no copper path to them."""
    status = _gnd(tmp_path, _ep_board(zone=False))
    assert status.island_count == 2
    assert [p.full_name for p in status.unconnected_pads] == ["J1.1"]


def test_ep_rects_without_thermal_vias_stay_separate(tmp_path):
    """F.Cu and B.Cu rects overlap in XY only; no via joins them."""
    status = _gnd(tmp_path, _ep_board(thermal_vias=False, zone=False))
    # U1.9 F.Cu, U1.9 B.Cu, J1.1: nothing joins any of them.
    assert status.island_count == 3


@pytest.mark.parametrize("same_number", [True, False])
def test_overlapping_pads_on_different_layers_do_not_bond(tmp_path, same_number):
    second = "1" if same_number else "2"
    text = (
        _HEADER
        + f"""
  (footprint "Test:Stack" (layer "F.Cu") (uuid "fpA") (at 10 10)
    (property "Reference" "J1" (at 0 -2 0) (layer "F.SilkS") (uuid "refA"))
    (duplicate_pad_numbers_are_jumpers no)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
    (pad "{second}" smd rect (at 0.2 0) (size 1 1) (layers "B.Cu") (net 1 "GND")))
)
"""
    )
    assert _gnd(tmp_path, text).island_count == 2


def test_overlapping_pads_on_same_layer_bond_regardless_of_number(tmp_path):
    text = (
        _HEADER
        + """
  (footprint "Test:Stack" (layer "F.Cu") (uuid "fpA") (at 10 10)
    (property "Reference" "J1" (at 0 -2 0) (layer "F.SilkS") (uuid "refA"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.8 0) (size 1 1) (layers "F.Cu") (net 1 "GND")))
)
"""
    )
    assert _gnd(tmp_path, text).island_count == 1


def test_legacy_mode_unchanged(tmp_path):
    """Legacy mode keys by REF.PAD and already merged the EP pieces."""
    status = _gnd(tmp_path, _ep_board(), strict=False)
    assert status.island_count == 1
