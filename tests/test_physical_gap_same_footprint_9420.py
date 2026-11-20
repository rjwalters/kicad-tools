"""Issue #9420: same-net pads of ONE footprint in the 2 oz pad mask-bridge check.

A stock module's ground pad array (``RF_Module:ESP32-C3-WROOM-02``) puts
circular thermal pads 0.19 mm apart on one net.  A consumer cannot edit that
land pattern, so the pair is listed at ``info``; every other pair keeps its
``error``.
"""

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.physical_gap import check_pad_mask_bridge

GAP = 0.19


def board(*items):
    return PCB(
        parse_string(
            """(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (net 0 "") (net 1 "GND") (net 2 "VCC")
    """
            + "\n".join(items)
            + ")"
        )
    )


def _pad(number, x, net, uuid):
    name = {1: "GND", 2: "VCC"}.get(net, "")
    return (
        f'(pad "{number}" smd circle (at {x} 0) (size 1 1) (layers "F.Cu") '
        f'(net {net} "{name}") (uuid "{uuid}"))'
    )


def footprint(ref, *pads, uuid=None):
    ident = f'(uuid "{uuid}")' if uuid else ""
    return (
        f'(footprint "RF_Module:ESP32-C3-WROOM-02" (layer "F.Cu") (at 0 0) {ident}\n'
        f'  (property "Reference" "{ref}")\n  ' + "\n  ".join(pads) + ")"
    )


def severities(pcb):
    return sorted(
        v.severity
        for v in check_pad_mask_bridge(pcb, 2.0).violations
        if v.rule_id == "physical_copper_gap"
    )


def test_same_net_pads_of_one_footprint_are_info_and_still_listed():
    pcb = board(footprint("U1", _pad(1, 0, 1, "a"), _pad(2, 1 + GAP, 1, "b")))
    found = check_pad_mask_bridge(pcb, 2.0).violations
    assert [v.severity for v in found] == ["info"]
    assert found[0].actual_value == pytest.approx(GAP, abs=1e-3)
    assert found[0].required_value == pytest.approx(0.2)
    assert found[0].items == ("a", "b")
    assert found[0].layer == "F.Cu"


def test_same_net_pads_of_different_footprints_still_error():
    pcb = board(
        footprint("U1", _pad(1, 0, 1, "a")),
        footprint("U2", _pad(1, 1 + GAP, 1, "b")),
    )
    assert severities(pcb) == ["error"]


def test_different_net_pads_of_one_footprint_still_error():
    pcb = board(footprint("U1", _pad(1, 0, 1, "a"), _pad(2, 1 + GAP, 2, "b")))
    assert severities(pcb) == ["error"]


def test_unnetted_pads_of_one_footprint_still_error():
    pcb = board(footprint("U1", _pad(1, 0, 0, "a"), _pad(2, 1 + GAP, 0, "b")))
    assert severities(pcb) == ["error"]


def test_two_footprints_sharing_reference_and_uuid_do_not_alias():
    pcb = board(
        footprint("U1", _pad(1, 0, 1, "a"), uuid="dup"),
        footprint("U1", _pad(1, 1 + GAP, 1, "b"), uuid="dup"),
    )
    assert severities(pcb) == ["error"]


def test_exempt_pair_below_threshold_is_not_hidden_when_far_apart():
    # Above the 0.20 mm limit nothing is reported at all (unchanged).
    pcb = board(footprint("U1", _pad(1, 0, 1, "a"), _pad(2, 1.25, 1, "b")))
    assert severities(pcb) == []
