"""Tests for kicad_tools.optim.fom_electrical.

Issue #3186 -- electrical FOM soft terms.
"""

from __future__ import annotations

import pytest

from kicad_tools.optim.fom_electrical import (
    _looks_like_capacitor,
    _looks_like_ic,
    _looks_like_power_net,
    _point_segment_distance,
    _segment_segment_distance,
    decoupling_proximity,
    diff_pair_clearance_margin,
    match_group_skew,
    weighted_via_count,
)
from kicad_tools.optim.fom_features import BoardFeatures, PadFeature
from kicad_tools.schema.pcb import PCB, Segment, Via


def _pad(x, y, net=1, name="N1", ref="R1", pin="1"):
    return PadFeature(
        x=x,
        y=y,
        net_number=net,
        net_name=name,
        reference=ref,
        pad_number=pin,
        layers=("F.Cu",),
        pad_type="smd",
    )


def _via(x, y, net=1, via_type=None):
    return Via(
        position=(x, y),
        size=0.6,
        drill=0.3,
        layers=["F.Cu", "B.Cu"],
        net_number=net,
        via_type=via_type,
    )


def _seg(x1, y1, x2, y2, net=1, layer="F.Cu", width=0.2):
    return Segment(
        start=(x1, y1),
        end=(x2, y2),
        width=width,
        layer=layer,
        net_number=net,
    )


def _empty_pcb() -> PCB:
    return PCB.create(width=100, height=100)


# --------------------------------------------------------------------
# weighted_via_count
# --------------------------------------------------------------------


def test_weighted_via_count_empty():
    f = BoardFeatures()
    assert weighted_via_count(f) == 0.0


def test_weighted_via_count_standard_via_is_1():
    f = BoardFeatures()
    f.vias_by_net = {1: [_via(0, 0, via_type=None)]}
    assert weighted_via_count(f) == 1.0


def test_weighted_via_count_micro_is_3():
    f = BoardFeatures()
    f.vias_by_net = {1: [_via(0, 0, via_type="micro")]}
    assert weighted_via_count(f) == 3.0


def test_weighted_via_count_blind_is_5():
    f = BoardFeatures()
    f.vias_by_net = {1: [_via(0, 0, via_type="blind")]}
    assert weighted_via_count(f) == 5.0


def test_weighted_via_count_buried_is_8():
    f = BoardFeatures()
    f.vias_by_net = {1: [_via(0, 0, via_type="buried")]}
    assert weighted_via_count(f) == 8.0


def test_weighted_via_count_unknown_via_type_defaults_to_standard():
    f = BoardFeatures()
    f.vias_by_net = {1: [_via(0, 0, via_type="weird-future")]}
    assert weighted_via_count(f) == 1.0


def test_weighted_via_count_aggregates_across_nets():
    f = BoardFeatures()
    f.vias_by_net = {
        1: [_via(0, 0), _via(1, 0)],
        2: [_via(0, 5, via_type="micro")],
    }
    assert weighted_via_count(f) == 1.0 + 1.0 + 3.0


# --------------------------------------------------------------------
# match_group_skew
# --------------------------------------------------------------------


def test_match_group_skew_no_groups_returns_zero():
    pcb = _empty_pcb()
    # No net classes, no declared groups.
    assert match_group_skew(pcb) == 0.0


# --------------------------------------------------------------------
# diff_pair_clearance_margin
# --------------------------------------------------------------------


def test_diff_pair_clearance_margin_no_pairs():
    pcb = _empty_pcb()
    f = BoardFeatures()
    assert diff_pair_clearance_margin(f, pcb) == 0.0


def test_diff_pair_clearance_margin_returns_zero_without_diffpair_nets():
    pcb = _empty_pcb()
    f = BoardFeatures()
    f.net_names = {1: "VCC", 2: "GND"}
    assert diff_pair_clearance_margin(f, pcb) == 0.0


def test_segment_segment_distance_disjoint():
    # Two parallel segments 5 mm apart.
    d = _segment_segment_distance((0, 0), (10, 0), (0, 5), (10, 5))
    assert d == pytest.approx(5.0)


def test_segment_segment_distance_intersecting():
    # Two crossing segments.
    d = _segment_segment_distance((0, 0), (10, 10), (0, 10), (10, 0))
    # They cross, so min distance = 0.
    assert d == pytest.approx(0.0, abs=1e-9)


def test_point_segment_distance_endpoint():
    # Point coincides with endpoint.
    assert _point_segment_distance((0, 0), (0, 0), (5, 0)) == 0.0


def test_point_segment_distance_perpendicular():
    # Point at perpendicular distance 3.
    assert _point_segment_distance((5, 3), (0, 0), (10, 0)) == pytest.approx(3.0)


def test_point_segment_distance_beyond_endpoint():
    # Point beyond the segment.
    assert _point_segment_distance((-1, 0), (0, 0), (5, 0)) == pytest.approx(1.0)


# --------------------------------------------------------------------
# decoupling_proximity helpers
# --------------------------------------------------------------------


def test_looks_like_power_net():
    assert _looks_like_power_net("VCC")
    assert _looks_like_power_net("VDD")
    assert _looks_like_power_net("+3V3")
    assert _looks_like_power_net("+5V")
    assert _looks_like_power_net("VBUS")
    assert not _looks_like_power_net("GND")
    assert not _looks_like_power_net("DATA")
    assert not _looks_like_power_net("")


def test_looks_like_capacitor():
    assert _looks_like_capacitor("C1")
    assert _looks_like_capacitor("C42")
    assert not _looks_like_capacitor("CLK")  # no digit after C
    assert not _looks_like_capacitor("R1")
    assert not _looks_like_capacitor("")


def test_looks_like_ic():
    assert _looks_like_ic("U1")
    assert _looks_like_ic("U42")
    assert not _looks_like_ic("UART")
    assert not _looks_like_ic("R1")


# --------------------------------------------------------------------
# decoupling_proximity
# --------------------------------------------------------------------


def test_decoupling_proximity_empty():
    f = BoardFeatures()
    assert decoupling_proximity(f) == 0.0


def test_decoupling_proximity_no_ic_no_cost():
    # Power net with only caps, no ICs -> 0 contribution.
    f = BoardFeatures()
    f.net_names = {1: "VCC"}
    f.nets_to_pads = {1: [_pad(0, 0, ref="C1"), _pad(10, 0, ref="C2")]}
    assert decoupling_proximity(f) == 0.0


def test_decoupling_proximity_no_caps_no_cost():
    # Power net with only ICs, no caps -> 0 contribution.
    f = BoardFeatures()
    f.net_names = {1: "VCC"}
    f.nets_to_pads = {1: [_pad(0, 0, ref="U1"), _pad(10, 0, ref="U2")]}
    assert decoupling_proximity(f) == 0.0


def test_decoupling_proximity_co_located():
    # IC pin and cap pin at the same point -> 0 distance.
    f = BoardFeatures()
    f.net_names = {1: "VCC"}
    f.nets_to_pads = {1: [_pad(5, 5, ref="U1"), _pad(5, 5, ref="C1")]}
    assert decoupling_proximity(f) == pytest.approx(0.0)


def test_decoupling_proximity_separated():
    # IC pin at (0,0); cap pin at (3,4) -> distance 5.
    f = BoardFeatures()
    f.net_names = {1: "VCC"}
    f.nets_to_pads = {1: [_pad(0, 0, ref="U1"), _pad(3, 4, ref="C1")]}
    assert decoupling_proximity(f) == pytest.approx(5.0)


def test_decoupling_proximity_picks_nearest_cap():
    # IC at (0,0); caps at (1,0) and (10,0). Should pick distance 1.
    f = BoardFeatures()
    f.net_names = {1: "VCC"}
    f.nets_to_pads = {1: [_pad(0, 0, ref="U1"), _pad(1, 0, ref="C1"), _pad(10, 0, ref="C2")]}
    assert decoupling_proximity(f) == pytest.approx(1.0)


def test_decoupling_proximity_skips_non_power_nets():
    # Non-power net is ignored.
    f = BoardFeatures()
    f.net_names = {1: "DATA"}
    f.nets_to_pads = {1: [_pad(0, 0, ref="U1"), _pad(10, 0, ref="C1")]}
    assert decoupling_proximity(f) == 0.0


def test_diff_pair_clearance_margin_with_pair_no_segments():
    pcb = _empty_pcb()
    f = BoardFeatures()
    # Set up a diff pair via net names: USB_P / USB_N
    f.net_names = {1: "USB_P", 2: "USB_N"}
    # No segments routed -> 0 contribution.
    assert diff_pair_clearance_margin(f, pcb) == 0.0


def test_diff_pair_clearance_margin_pair_with_segments_close():
    pcb = _empty_pcb()
    f = BoardFeatures()
    f.net_names = {1: "USB_P", 2: "USB_N"}
    # Routes 0.1 mm apart -> 0.1 mm clearance < 0.2 target -> 0.1 shortfall.
    f.segments_by_net = {
        1: [_seg(0, 0, 10, 0, net=1)],
        2: [_seg(0, 0.1, 10, 0.1, net=2)],
    }
    shortfall = diff_pair_clearance_margin(f, pcb, target_clearance_mm=0.2)
    assert shortfall == pytest.approx(0.1)


def test_diff_pair_clearance_margin_pair_with_segments_far():
    pcb = _empty_pcb()
    f = BoardFeatures()
    f.net_names = {1: "USB_P", 2: "USB_N"}
    # Routes 1.0 mm apart -> well over the 0.2 target -> 0.
    f.segments_by_net = {
        1: [_seg(0, 0, 10, 0, net=1)],
        2: [_seg(0, 1.0, 10, 1.0, net=2)],
    }
    assert diff_pair_clearance_margin(f, pcb, target_clearance_mm=0.2) == pytest.approx(0.0)


def test_diff_pair_clearance_margin_segments_on_different_layers_no_count():
    pcb = _empty_pcb()
    f = BoardFeatures()
    f.net_names = {1: "USB_P", 2: "USB_N"}
    # P on F.Cu, N on B.Cu -> intra-pair clearance check skipped.
    f.segments_by_net = {
        1: [_seg(0, 0, 10, 0, net=1, layer="F.Cu")],
        2: [_seg(0, 0.1, 10, 0.1, net=2, layer="B.Cu")],
    }
    assert diff_pair_clearance_margin(f, pcb, target_clearance_mm=0.2) == pytest.approx(0.0)


def test_decoupling_proximity_aggregates_multiple_ics():
    # Two ICs, two caps.  Each IC matches its nearest cap.
    f = BoardFeatures()
    f.net_names = {1: "VCC"}
    f.nets_to_pads = {
        1: [
            _pad(0, 0, ref="U1"),
            _pad(20, 0, ref="U2"),
            _pad(1, 0, ref="C1"),  # cap near U1
            _pad(21, 0, ref="C2"),  # cap near U2
        ]
    }
    # U1->C1 = 1, U2->C2 = 1, total = 2.
    assert decoupling_proximity(f) == pytest.approx(2.0)
