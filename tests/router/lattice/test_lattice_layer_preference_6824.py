"""Lattice honours ``NetClassRouting.preferred_layers`` as a soft bias (#6824).

Two through-hole pads on a 4-layer all-signal stack have equal-length
planar routes on every layer.  A class preferring a layer lands on it; a
class with no preference keeps the historical choice.
"""

from __future__ import annotations

from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

_RULES = DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb")
_OUTLINE = [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)]


def _pad(x: float, ref: str) -> Pad:
    return Pad(
        x=x,
        y=5.0,
        width=1.2,
        height=1.2,
        net=1,
        net_name="SIG",
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
        through_hole=True,
        drill=0.6,
    )


def _route_layers(net_class: NetClassRouting) -> set[str]:
    a, b = _pad(3.0, "J1"), _pad(17.0, "U1")
    pf = LatticePathfinder(_OUTLINE, [a, b], _RULES, LayerStack.four_layer_all_signal())
    route = pf.route(a, b, net_class=net_class)
    assert route is not None
    return {seg.layer.value for seg in route.segments}


def test_preferred_layer_wins_over_equal_alternatives():
    nc = NetClassRouting(name="P", trace_width=0.2, clearance=0.15, preferred_layers=[2])
    assert _route_layers(nc) == {Layer.IN2_CU.value}


def test_other_preferred_layer_is_followed():
    nc = NetClassRouting(name="P", trace_width=0.2, clearance=0.15, preferred_layers=[1])
    assert _route_layers(nc) == {Layer.IN1_CU.value}


def test_no_preference_is_unchanged():
    base = NetClassRouting(name="N", trace_width=0.2, clearance=0.15)
    empty = NetClassRouting(name="N", trace_width=0.2, clearance=0.15, preferred_layers=[])
    assert _route_layers(base) == _route_layers(empty)


def test_unreachable_preference_is_dropped():
    base = NetClassRouting(name="N", trace_width=0.2, clearance=0.15)
    off = NetClassRouting(name="N", trace_width=0.2, clearance=0.15, preferred_layers=[9])
    assert _route_layers(base) == _route_layers(off)


def _reserved_plane_rules(strict: bool = False) -> DesignRules:
    return DesignRules(
        strict_layers=strict,
        trace_width=0.2,
        trace_clearance=0.15,
        manufacturer="jlcpcb",
        allowed_layers=["F.Cu", "B.Cu"],
    )


def _route_layers_reserved(net_class: NetClassRouting, strict: bool = False) -> set[str]:
    a, b = _pad(3.0, "J1"), _pad(17.0, "U1")
    pf = LatticePathfinder(
        _OUTLINE, [a, b], _reserved_plane_rules(strict), LayerStack.four_layer_all_signal()
    )
    route = pf.route(a, b, net_class=net_class)
    assert route is not None
    return {seg.layer.value for seg in route.segments}


def test_authored_preference_lifts_plane_reservation_for_that_net():
    nc = NetClassRouting(name="P", trace_width=0.2, clearance=0.15, preferred_layers=[2])
    assert Layer.IN2_CU.value in _route_layers_reserved(nc)


def test_plane_reservation_still_blocks_nets_without_that_preference():
    nc = NetClassRouting(name="N", trace_width=0.2, clearance=0.15)
    assert not {Layer.IN1_CU.value, Layer.IN2_CU.value} & _route_layers_reserved(nc)


def test_class_avoid_layers_still_blocks_a_preferred_reserved_layer():
    nc = NetClassRouting(
        name="P", trace_width=0.2, clearance=0.15, preferred_layers=[2], avoid_layers=[2]
    )
    assert Layer.IN2_CU.value not in _route_layers_reserved(nc, strict=True)
