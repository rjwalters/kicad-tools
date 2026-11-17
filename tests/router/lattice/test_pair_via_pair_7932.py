"""Mid-trunk via pairs for lattice diff pairs (issue #7932, epic #6441 P3).

The coupled search may change layer in the middle of a trunk with a P/N via
pair placed as ONE move, offered only as a deferred third retry after the
planar attempt and the fan-via gateway retry (#6444) both declined.

Synthetic boards only (the four-board bench lives in
``scripts/research/diffpair_bench.py``).  The via-pair fixture: end A is a
pair of F.Cu SMD pads, end B a pair of through-hole pads.  A full-height F.Cu
wall bisects the route, so no planar run exists; a B.Cu-only block covers
end A's whole neighbourhood, so no fan-via gateway at end A is legal (and a
B.Cu body cannot start there).  The only coupled route is F.Cu out of end A,
one via pair past the B.Cu block, B.Cu into end B.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.pathfinder import (
    LatticePathfinder,
    _RouteResult,
    _ViaPairSpec,
)
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pairplan.via_pair import (
    ViaPair,
    jog_into_via,
    jog_out_of_via,
    via_pair_jog,
    via_pair_sites,
)
from kicad_tools.router.primitives import Pad, Route
from kicad_tools.router.rules import DesignRules, NetClassRouting

_RULES = DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb")
_HS = NetClassRouting(
    name="HS",
    trace_width=0.2,
    clearance=0.15,
    intra_pair_clearance=0.1,
    coupled_routing=True,
)
_PITCH = _HS.trace_width + _HS.effective_intra_pair_clearance()
_OUTLINE = [(0.0, 0.0), (30.0, 0.0), (30.0, 12.0), (0.0, 12.0)]


def _pad(
    x: float,
    y: float,
    net: int,
    name: str,
    ref: str,
    *,
    layer: Layer = Layer.F_CU,
    width: float = 0.5,
    height: float = 0.5,
    tht: bool = False,
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=name,
        layer=layer,
        ref=ref,
        pin="1",
        through_hole=tht,
        drill=0.3 if tht else 0.0,
    )


def _pair(pads: list[Pad]) -> tuple[LatticePathfinder, CoupledConnection]:
    p_a, n_a, p_b, n_b = pads[:4]
    pf = LatticePathfinder(_OUTLINE, pads, _RULES, LayerStack.two_layer())
    pc = CoupledConnection(
        key=("pair", 1, 2),
        pair_name="D",
        pad_p_a=p_a,
        pad_n_a=n_a,
        pad_p_b=p_b,
        pad_n_b=n_b,
        net_class=_HS,
        pitch=_PITCH,
    )
    return pf, pc


def _via_pair_board() -> tuple[LatticePathfinder, CoupledConnection]:
    """Planar and gateway both decline; one mid-trunk via pair couples it."""
    pads = [
        _pad(3.0, 5.0, 1, "D+", "J1"),
        _pad(3.0, 6.0, 2, "D-", "J1"),
        _pad(27.0, 5.0, 1, "D+", "U1", width=0.6, height=0.6, tht=True),
        _pad(27.0, 6.2, 2, "D-", "U1", width=0.6, height=0.6, tht=True),
        # Full-height F.Cu wall between the ends: no planar corridor.
        _pad(15.0, 6.0, 9, "WALL", "WF", width=0.6, height=13.0),
        # B.Cu-only block over end A's neighbourhood: no fan via fits there.
        _pad(4.5, 6.0, 10, "BLOCK", "WB", layer=Layer.B_CU, width=9.0, height=13.0),
    ]
    return _pair(pads)


def _planar_board() -> tuple[LatticePathfinder, CoupledConnection]:
    pads = [
        _pad(3.0, 5.0, 1, "D+", "J1"),
        _pad(3.0, 6.0, 2, "D-", "J1"),
        _pad(27.0, 5.0, 1, "D+", "U1"),
        _pad(27.0, 6.0, 2, "D-", "U1"),
    ]
    return _pair(pads)


def _gateway_board() -> tuple[LatticePathfinder, CoupledConnection]:
    """The #6444 walled pair: both ends SMD on F.Cu, couples through gateways."""
    pads = [
        _pad(3.0, 5.0, 1, "D+", "J1"),
        _pad(3.0, 6.0, 2, "D-", "J1"),
        _pad(27.0, 5.0, 1, "D+", "U1"),
        _pad(27.0, 6.0, 2, "D-", "U1"),
        _pad(15.0, 6.0, 9, "WALL", "WF", width=0.6, height=13.0),
    ]
    return _pair(pads)


def _signature(routes: dict) -> list:
    out = []
    for key in sorted(routes, key=repr):
        r = routes[key]
        out.append(
            (
                repr(key),
                [(s.x1, s.y1, s.x2, s.y2, s.width, s.layer) for s in r.segments],
                [(v.x, v.y) for v in r.vias],
            )
        )
    return out


def _route(pf: LatticePathfinder, pc: CoupledConnection, **kw: object) -> dict:
    routes, _stats = pf.route_netset([], coupled=[pc], pair_gateways=True, **kw)
    return routes


# -- the move ---------------------------------------------------------------


def test_pair_couples_through_exactly_one_mid_trunk_via_pair() -> None:
    pf, pc = _via_pair_board()
    routes = _route(pf, pc)
    # Planar and gateway both declined first ...
    assert "pair-gateway-no-candidates" in pf.pair_gateway_log[pc.key]
    assert pc.key not in pf.pair_gateways
    # ... and the third retry coupled it through ONE via pair.
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_via_pair_log
    note = pf.pair_via_pair_coupled[pc.key]
    assert note.startswith("coupled through 1 via pair(s) (F.Cu->B.Cu at ")
    assert "vias P/N=1/1" in note
    route_p, route_n = routes[(pc.key, "P")], routes[(pc.key, "N")]
    assert len(route_p.vias) == len(route_n.vias) == 1
    vp, vn = route_p.vias[0], route_n.vias[0]
    # The two vias straddle one centre, at the via-pair pitch.
    via_gap = max(_RULES.via_diameter + _HS.clearance, _RULES.via_drill + _RULES.min_hole_to_hole)
    assert math.hypot(vp.x - vn.x, vp.y - vn.y) == pytest.approx(max(via_gap, _PITCH), abs=1e-6)
    for route in (route_p, route_n):
        layers = {s.layer for s in route.segments}
        assert layers == {Layer.F_CU, Layer.B_CU}
        for s in route.segments:
            # Nothing on F.Cu crosses the wall; B.Cu stays clear of the block.
            if s.layer == Layer.F_CU:
                assert max(s.x1, s.x2) < 15.0 - 0.3
            else:
                assert min(s.x1, s.x2) > 9.0
    # Coupled at the pair pitch on each layer (never closer); away from the
    # via jogs the long body runs are exactly one pitch apart.
    for layer in (Layer.F_CU, Layer.B_CU):
        segs_p = [s for s in route_p.segments if s.layer == layer]
        segs_n = [s for s in route_n.segments if s.layer == layer]
        for s1 in segs_p:
            for s2 in segs_n:
                d = seg_seg_dist((s1.x1, s1.y1), (s1.x2, s1.y2), (s2.x1, s2.y1), (s2.x2, s2.y2))
                assert d >= _PITCH - 1e-4
        body_p = max(segs_p, key=lambda s: math.hypot(s.x2 - s.x1, s.y2 - s.y1))
        body_n = max(segs_n, key=lambda s: math.hypot(s.x2 - s.x1, s.y2 - s.y1))
        assert abs(body_p.y1 - body_n.y1) == pytest.approx(_PITCH, abs=1e-3)


def test_via_budget_too_low_declines_by_name_and_emits_nothing() -> None:
    pf, pc = _via_pair_board()
    pf.pair_gateway_limits[pc.key] = (None, 0)
    routes = _route(pf, pc)
    assert routes == {}
    assert pf.pair_outcomes[pc.key] != "coupled"
    assert pf.pair_via_pair_coupled == {}
    assert pf.pair_via_pair_log[pc.key] == (
        "via-pair declined (pair-via-pair-via-limit): needs 1 via/net > limit 0"
    )
    # The same budget spent on the pair's other chain segments counts too.
    pf2, pc2 = _via_pair_board()
    pf2.pair_gateway_limits[pc2.key] = (None, 2)
    pres, reason = pf2._route_pair_via_pair(
        pc2, committed=pf2._fresh_committed(), history={}, present=1.0, vias_used=2
    )
    assert pres is None
    assert reason == "pair-via-pair-via-limit"
    assert "+ 2 on other segments > limit 2" in pf2.pair_via_pair_log[pc2.key]


@pytest.mark.parametrize("board", [_planar_board, _gateway_board])
def test_pairs_that_couple_today_are_byte_identical_with_the_move(board) -> None:
    pf_off, pc_off = board()
    pf_off.pair_via_pairs = False
    routes_off = _route(pf_off, pc_off, uncoupled_fallback=True)
    pf_on, pc_on = board()
    routes_on = _route(pf_on, pc_on, uncoupled_fallback=True)
    assert pf_on.pair_outcomes[pc_on.key] == "coupled"
    # The move was never offered: the pair coupled before the third retry.
    assert pf_on.pair_via_pair_log == {}
    assert pf_on.pair_via_pair_coupled == {}
    assert _signature(routes_on) == _signature(routes_off)
    assert pf_on.pair_gateways == pf_off.pair_gateways


def test_p_stays_on_its_side_through_the_via_pair() -> None:
    pf, pc = _via_pair_board()
    routes = _route(pf, pc)
    route_p, route_n = routes[(pc.key, "P")], routes[(pc.key, "N")]
    vp, vn = route_p.vias[0], route_n.vias[0]
    centre = ((vp.x + vn.x) / 2.0, (vp.y + vn.y) / 2.0)
    # Travel through the via pair is perpendicular to the via axis, towards
    # end B (+x here).
    axis = (vp.x - vn.x, vp.y - vn.y)
    heading = (axis[1], -axis[0])
    if heading[0] < 0.0:
        heading = (-heading[0], -heading[1])

    def side(x: float, y: float) -> float:
        return heading[0] * (y - centre[1]) - heading[1] * (x - centre[0])

    p_side = math.copysign(1.0, side(vp.x, vp.y))
    # P's pads sit on the same side as its via at both ends.
    assert (
        math.copysign(1.0, side(pc.pad_p_a.x, pc.pad_p_a.y) - side(pc.pad_n_a.x, pc.pad_n_a.y))
        == p_side
    )

    def parallel(s) -> bool:
        return abs(heading[0] * (s.y2 - s.y1) - heading[1] * (s.x2 - s.x1)) < 1e-6

    # The straight approach (F.Cu) and exit (B.Cu) runs beside the via pair:
    # P on its via's side of the travel direction both before and after.
    for route, sign in ((route_p, p_side), (route_n, -p_side)):
        for layer in (Layer.F_CU, Layer.B_CU):
            near = [
                s
                for s in route.segments
                if s.layer == layer
                and parallel(s)
                and min(
                    math.hypot(s.x1 - centre[0], s.y1 - centre[1]),
                    math.hypot(s.x2 - centre[0], s.y2 - centre[1]),
                )
                < 3.0
            ]
            assert near, layer
            for s in near:
                mid = ((s.x1 + s.x2) / 2.0, (s.y1 + s.y2) / 2.0)
                assert math.copysign(1.0, side(*mid)) == sign, (layer, s)


def test_via_pair_routing_is_deterministic() -> None:
    sigs = []
    for _ in range(2):
        pf, pc = _via_pair_board()
        routes = _route(pf, pc)
        sigs.append((_signature(routes), dict(pf.pair_via_pair_log), dict(pf.pair_outcomes)))
    assert sigs[0] == sigs[1]
    assert sigs[0][0]


# -- the final check --------------------------------------------------------


def _spec() -> _ViaPairSpec:
    return _ViaPairSpec(
        layers=frozenset({0, 1}),
        via_pitch=0.85,
        reach=0.65,
        jog=via_pair_jog(_PITCH, 0.85),
        clearance=0.15,
    )


def _centerline_result(
    runs: list[tuple[int, list]], via_pairs: list[ViaPair], via_points: list | None = None
) -> _RouteResult:
    return _RouteResult(
        Route(net=1, net_name="D+"),
        [(layer, pts, [0.2] * (len(pts) - 1)) for layer, pts in runs],
        list(via_points or []),
        set(),
        via_pairs,
    )


def test_final_check_still_declines_a_single_ended_via_or_unequal_counts() -> None:
    pf, pc = _via_pair_board()
    committed = pf._fresh_committed()
    run_a = (0, [(4.0, 5.5), (12.0, 5.5)])
    run_b = (1, [(12.0, 5.5), (25.0, 5.5)])
    via_p, via_n = via_pair_sites((12.0, 5.5), (1.0, 0.0), 0.85, p_left=False)
    pair = ViaPair((12.0, 5.5), (1.0, 0.0), via_p, via_n, 0, 1)
    # A single-ended via on the centerline: not a via pair.
    single = _centerline_result([run_a, run_b], [pair], via_points=[(12.0, 5.5)])
    assert pf._finish_via_pair_legs(pc, single, committed, _spec()) == (None, "pair-not-planar")
    # Two runs but no via pair joining them (unequal P/N via counts).
    unpaired = _centerline_result([run_a, run_b], [])
    assert pf._finish_via_pair_legs(pc, unpaired, committed, _spec()) == (None, "pair-not-planar")
    # A via pair whose layers do not match the runs it joins.
    crossed = _centerline_result(
        [run_a, run_b], [ViaPair(pair.centre, pair.heading, via_p, via_n, 1, 0)]
    )
    assert pf._finish_via_pair_legs(pc, crossed, committed, _spec()) == (None, "pair-not-planar")
    # A via pair with P's via on N's side would swap polarity: declined.
    swapped = _centerline_result(
        [run_a, run_b], [ViaPair(pair.centre, pair.heading, via_n, via_p, 0, 1)]
    )
    assert pf._finish_via_pair_legs(pc, swapped, committed, _spec()) == (None, "pair-via-pair-side")


def test_jog_geometry_is_octilinear_and_symmetric() -> None:
    jog = via_pair_jog(_PITCH, 0.85)
    via_p, via_n = via_pair_sites((10.0, 5.0), (1.0, 0.0), 0.85, p_left=True)
    assert via_p == pytest.approx((10.0, 5.425))
    assert via_n == pytest.approx((10.0, 4.575))
    leg_p = jog_into_via([(5.0, 5.15), (10.0, 5.15)], via_p, (1.0, 0.0), jog)
    leg_n = jog_into_via([(5.0, 4.85), (10.0, 4.85)], via_n, (1.0, 0.0), jog)
    assert leg_p is not None and leg_n is not None
    (ax, ay), (bx, by) = leg_p[-2], leg_p[-1]
    assert abs(abs(bx - ax) - abs(by - ay)) < 1e-9  # a 45-degree jog

    # P and N jog by the same length: no skew from the move.
    def length(poly: list) -> float:
        return sum(math.dist(a, b) for a, b in zip(poly, poly[1:], strict=False))

    assert length(leg_p) == pytest.approx(length(leg_n))
    out = jog_out_of_via([(10.0, 5.15), (15.0, 5.15)], via_p, (1.0, 0.0), jog)
    assert out is not None and out[0] == via_p
    # A last segment too short to carry the jog is refused.
    assert jog_into_via([(9.9, 5.15), (10.0, 5.15)], via_p, (1.0, 0.0), jog) is None
