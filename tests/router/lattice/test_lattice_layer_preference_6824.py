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


# --- coupled pairs -------------------------------------------------------
# A pair's pads are SMD on F.Cu, so the planar coupled attempt only searches
# F.Cu and succeeds there; without the preferred-gateway-first attempt the
# class preference would never be consulted.


def _pair_route(preferred: list[int] | None):
    from kicad_tools.router.lattice.coupled import CoupledConnection

    def pad(x: float, y: float, net: int, name: str, ref: str) -> Pad:
        return Pad(
            x=x, y=y, width=0.5, height=0.5, net=net, net_name=name,
            layer=Layer.F_CU, ref=ref, pin="1",
        )  # fmt: skip

    p_a, n_a = pad(3.0, 5.0, 1, "D+", "J1"), pad(3.0, 6.0, 2, "D-", "J1")
    p_b, n_b = pad(17.0, 5.0, 1, "D+", "U1"), pad(17.0, 6.0, 2, "D-", "U1")
    nc = NetClassRouting(
        name="HS",
        trace_width=0.2,
        clearance=0.15,
        intra_pair_clearance=0.1,
        coupled_routing=True,
        preferred_layers=preferred,
    )
    pf = LatticePathfinder(
        _OUTLINE, [p_a, n_a, p_b, n_b], _RULES, LayerStack.four_layer_all_signal()
    )
    pc = CoupledConnection(
        key=("pair", 1, 2),
        pair_name="D",
        pad_p_a=p_a,
        pad_n_a=n_a,
        pad_p_b=p_b,
        pad_n_b=n_b,
        net_class=nc,
        pitch=nc.trace_width + nc.effective_intra_pair_clearance(),
    )
    routes, _ = pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True)
    return pf, pc, routes


def test_pair_with_unreachable_planar_preference_couples_on_it():
    pf, pc, routes = _pair_route([2])
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_gateway_log
    assert "In2.Cu" in pf.pair_gateways[pc.key]
    for pol in ("P", "N"):
        layers = {s.layer for s in routes[(pc.key, pol)].segments}
        assert Layer.IN2_CU in layers
        # The body is on the preferred layer: it carries the long run.
        body = sum(
            abs(s.x2 - s.x1) + abs(s.y2 - s.y1)
            for s in routes[(pc.key, pol)].segments
            if s.layer == Layer.IN2_CU
        )
        assert body > 8.0


def test_pair_without_preference_stays_planar_on_the_pad_layer():
    for preferred in (None, []):
        pf, pc, routes = _pair_route(preferred)
        assert pf.pair_outcomes[pc.key] == "coupled"
        assert pc.key not in pf.pair_gateways
        for pol in ("P", "N"):
            assert not routes[(pc.key, pol)].vias
            assert {s.layer for s in routes[(pc.key, pol)].segments} == {Layer.F_CU}


def test_pair_preferring_its_pad_layer_stays_planar():
    pf, pc, routes = _pair_route([0])
    assert pf.pair_outcomes[pc.key] == "coupled"
    assert pc.key not in pf.pair_gateways
