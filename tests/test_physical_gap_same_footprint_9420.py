"""Same-net pads of one footprint are not gap errors (Issue #9420).

A stock library footprint (``RF_Module:ESP32-C3-WROOM-02``) carries a ground pad
array whose pads sit 0.19 mm apart. The consumer cannot edit it, so those pairs
are listed at ``info``; every other pairing keeps its verdict.
"""

from __future__ import annotations

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.physical_gap import (
    FabGapPolicy,
    GapBand,
    check_physical_copper_gap,
    check_physical_copper_gap_fab_view,
)

GAP = 0.19  # mm between the two pads' facing edges


def _pad(number, x, net, net_name):
    return (
        f'(pad "{number}" smd rect (at {x} 0) (size 1 1) (layers "F.Cu") (net {net} "{net_name}"))'
    )


def _footprint(ref, uuid, pads, at=(0, 0)):
    return (
        f'(footprint "Lib:Part" (layer "F.Cu") (at {at[0]} {at[1]}) (uuid "{uuid}") '
        f'(property "Reference" "{ref}") {" ".join(pads)})'
    )


def _pcb(*footprints):
    return PCB(
        parse_string(
            """(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (net 0 "") (net 1 "GND") (net 2 "VCC")
      """
            + " ".join(footprints)
            + ")"
        )
    )


def _severities(results):
    return sorted(v.severity for v in results.violations if v.rule_id == "physical_copper_gap")


def _fab_policy():
    band = GapBand(danger_mm=0.25, warning_mm=None)
    return FabGapPolicy(pad=band, trace=band)


def _one_footprint(net_b=1, name_b="GND"):
    step = 1 + GAP
    return _pcb(_footprint("U1", "fp-1", [_pad(1, 0, 1, "GND"), _pad(2, step, net_b, name_b)]))


def _two_footprints():
    step = 1 + GAP
    return _pcb(
        _footprint("U1", "fp-1", [_pad(1, 0, 1, "GND")]),
        _footprint("U2", "fp-2", [_pad(1, step, 1, "GND")]),
    )


@pytest.mark.parametrize("check", ["native", "fab"])
def test_same_net_pads_of_one_footprint_are_info(check):
    pcb = _one_footprint()
    results = (
        check_physical_copper_gap(pcb, 0.2)
        if check == "native"
        else check_physical_copper_gap_fab_view(pcb, _fab_policy())
    )
    assert _severities(results) == ["info"]
    assert not results.errors and not results.warnings


@pytest.mark.parametrize("check", ["native", "fab"])
def test_same_net_pads_of_different_footprints_still_error(check):
    pcb = _two_footprints()
    results = (
        check_physical_copper_gap(pcb, 0.2)
        if check == "native"
        else check_physical_copper_gap_fab_view(pcb, _fab_policy())
    )
    assert _severities(results) == ["error"]


@pytest.mark.parametrize("check", ["native", "fab"])
def test_different_net_pads_of_one_footprint_still_error(check):
    pcb = _one_footprint(net_b=2, name_b="VCC")
    results = (
        check_physical_copper_gap(pcb, 0.2)
        if check == "native"
        else check_physical_copper_gap_fab_view(pcb, _fab_policy())
    )
    assert _severities(results) == ["error"]


def test_unnetted_pads_of_one_footprint_are_not_exempt():
    pcb = _pcb(_footprint("U1", "fp-1", [_pad(1, 0, 0, ""), _pad(2, 1 + GAP, 0, "")]))
    assert _severities(check_physical_copper_gap(pcb, 0.2)) == ["error"]


def test_same_net_track_beside_footprint_pad_still_errors():
    """Routed copper is not a land pattern: pad-to-track keeps its verdict."""
    # Pad right edge x=0.5; a 0.2 mm track centred at x=0.79 starts 0.19 mm away.
    track = '(segment (start 0.79 -1) (end 0.79 1) (width 0.2) (layer "F.Cu") (net 1) (uuid "t1"))'
    pcb = PCB(
        parse_string(
            """(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (net 0 "") (net 1 "GND")
      """
            + _footprint("U1", "fp-1", [_pad(1, 0, 1, "GND")])
            + track
            + ")"
        )
    )
    assert _severities(check_physical_copper_gap(pcb, 0.2)) == ["error"]
