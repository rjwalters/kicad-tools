"""Diff-pair coupled routing on the lattice engine (issue #4270, P3).

Unit coverage for the fat-agent centerline + geometric offset emission:

* offset octilinearity property (segment directions preserved -> #3907);
* exact-pitch emission on the coupled body;
* polarity-side selection (P leg lands on the P pads' side);
* decline paths: blocked fat envelope, polarity twist, no planar path --
  every decline carries a reason and emits NOTHING (never split, #3906);
* engagement gating (``coupled_routing=False`` classes stay single-ended).

The board-06 witness (>= coupled floor, intra-pair validator, DRC) lives in
``test_coupled_board06.py`` -- it negotiates the full 26-net board and is
runtime-heavy.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.lattice.coupled import (
    CoupledConnection,
    assign_polarity,
    choose_pair_endpoints,
    merge_collinear_points,
    offset_polyline,
)
from kicad_tools.router.lattice.geometry import dist, seg_seg_dist
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

_RULES = DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb")

_HS = NetClassRouting(
    name="HS",
    trace_width=0.2,
    clearance=0.15,
    intra_pair_clearance=0.1,
    coupled_routing=True,
)
_PITCH = _HS.trace_width + _HS.effective_intra_pair_clearance()  # 0.3


def _pad(
    x: float,
    y: float,
    net: int,
    net_name: str,
    *,
    ref: str,
    width: float = 0.5,
    height: float = 0.5,
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _pair_board(
    extra_pads: list[Pad] | None = None,
    *,
    swap_far_end: bool = False,
    outline: list[tuple[float, float]] | None = None,
) -> tuple[LatticePathfinder, CoupledConnection]:
    """A 30x12 two-layer board with one horizontal diff pair (nets 1/2)."""
    outline = outline or [(0.0, 0.0), (30.0, 0.0), (30.0, 12.0), (0.0, 12.0)]
    p_a = _pad(3.0, 5.0, 1, "D+", ref="J1")
    n_a = _pad(3.0, 6.0, 2, "D-", ref="J1")
    by, ny = (6.0, 5.0) if swap_far_end else (5.0, 6.0)
    p_b = _pad(27.0, by, 1, "D+", ref="U1")
    n_b = _pad(27.0, ny, 2, "D-", ref="U1")
    pads = [p_a, n_a, p_b, n_b] + list(extra_pads or [])
    pf = LatticePathfinder(outline, pads, _RULES, LayerStack.two_layer())
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


def _is_octilinear(a: tuple[float, float], b: tuple[float, float]) -> bool:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if math.hypot(dx, dy) <= 1e-9:
        return True
    ang = math.degrees(math.atan2(dy, dx)) % 45.0
    return min(ang, 45.0 - ang) < 1e-6


# ---------------------------------------------------------------------------
# Offset geometry (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "polyline",
    [
        [(0.0, 0.0), (10.0, 0.0)],
        [(0.0, 0.0), (10.0, 0.0), (15.0, 5.0)],
        [(0.0, 0.0), (5.0, 5.0), (10.0, 5.0), (10.0, 10.0), (6.0, 14.0)],
        [(0.0, 0.0), (4.0, 0.0), (8.0, 4.0), (8.0, 9.0), (12.0, 13.0), (20.0, 13.0)],
    ],
)
@pytest.mark.parametrize("offset", [0.15, -0.15, 0.1375])
def test_offset_preserves_octilinear_directions(polyline, offset) -> None:
    """#3907 by construction: every offset segment keeps its source direction."""
    out = offset_polyline(polyline, offset)
    assert out is not None
    assert len(out) == len(polyline)
    for (a, b), (c, d) in zip(
        zip(polyline, polyline[1:], strict=False), zip(out, out[1:], strict=False), strict=True
    ):
        assert _is_octilinear(c, d)
        # Same direction as the source segment (unit-vector match).
        la, lo = dist(a, b), dist(c, d)
        assert lo > 0
        assert math.isclose((b[0] - a[0]) / la, (d[0] - c[0]) / lo, abs_tol=1e-9)
        assert math.isclose((b[1] - a[1]) / la, (d[1] - c[1]) / lo, abs_tol=1e-9)


def test_offset_legs_sit_at_exact_pitch() -> None:
    """+/- pitch/2 offsets are exactly pitch apart along the whole body."""
    center = [(0.0, 0.0), (8.0, 0.0), (12.0, 4.0), (20.0, 4.0)]
    plus = offset_polyline(center, +_PITCH / 2.0)
    minus = offset_polyline(center, -_PITCH / 2.0)
    assert plus is not None and minus is not None
    for (a, b), (c, d) in zip(
        zip(plus, plus[1:], strict=False), zip(minus, minus[1:], strict=False), strict=True
    ):
        assert seg_seg_dist(a, b, c, d) == pytest.approx(_PITCH, abs=1e-9)


def test_offset_declines_on_u_turn_and_degenerate() -> None:
    assert offset_polyline([(0.0, 0.0), (5.0, 0.0), (0.0, 0.0)], 0.15) is None  # U-turn
    assert offset_polyline([(0.0, 0.0), (0.0, 0.0)], 0.15) is None  # zero-length
    assert offset_polyline([(0.0, 0.0)], 0.15) is None  # single point
    # Tight inside turn shorter than the miter -> reversed segment -> None.
    assert offset_polyline([(0.0, 0.0), (0.05, 0.0), (0.05, 0.05), (0.0, 0.05)], 0.3) is None


def test_merge_collinear_points_fuses_straight_runs() -> None:
    pts = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 1.0), (3.0, 1.0), (4.0, 2.0), (5.0, 2.0)]
    assert merge_collinear_points(pts) == [(0.0, 0.0), (2.0, 0.0), (4.0, 2.0), (5.0, 2.0)]


def test_assign_polarity_maps_legs_by_pad_side_and_detects_twist() -> None:
    center = [(0.0, 5.5), (30.0, 5.5)]
    plus = offset_polyline(center, +0.15)  # left of +x travel = y+ side
    minus = offset_polyline(center, -0.15)
    assert plus is not None and minus is not None
    p_a = _pad(0.0, 6.0, 1, "D+", ref="A")
    n_a = _pad(0.0, 5.0, 2, "D-", ref="A")
    p_b = _pad(30.0, 6.0, 1, "D+", ref="B")
    n_b = _pad(30.0, 5.0, 2, "D-", ref="B")
    assigned = assign_polarity(plus, minus, p_a, n_a, p_b, n_b)
    assert assigned is not None
    leg_p, _leg_n = assigned
    assert leg_p == plus  # P pads sit on the +offset side at both ends
    # Swap P/N at end B -> twist -> None.
    assert assign_polarity(plus, minus, p_a, n_a, n_b, p_b) is None


def test_choose_pair_endpoints_picks_farthest_couples() -> None:
    ps = [
        _pad(0.0, 0.0, 1, "D+", ref="A"),
        _pad(0.1, 2.0, 1, "D+", ref="B"),
        _pad(20.0, 0.0, 1, "D+", ref="C"),
    ]
    ns = [
        _pad(0.0, 1.0, 2, "D-", ref="A"),
        _pad(0.1, 3.0, 2, "D-", ref="B"),
        _pad(20.0, 1.0, 2, "D-", ref="C"),
    ]
    chosen = choose_pair_endpoints(ps, ns)
    assert chosen is not None
    (ip_a, in_a), (ip_b, in_b) = chosen
    picked = {ip_a, ip_b}
    assert 2 in picked, "the far couple must be an endpoint"
    assert in_a == ip_a and in_b == ip_b, "couples pair nearest P/N"
    # Degenerate: a single couple cannot form a main run.
    assert choose_pair_endpoints(ps[:1], ns[:1]) is None
    assert choose_pair_endpoints([], ns) is None


# ---------------------------------------------------------------------------
# Coupled negotiation on a synthetic board
# ---------------------------------------------------------------------------


def test_pair_routes_coupled_at_exact_pitch_with_correct_polarity() -> None:
    pf, pc = _pair_board()
    routes, stats = pf.route_netset([], coupled=[pc])
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_outcomes
    assert stats.routed == 1 and stats.total == 1
    assert stats.lattice_builds == 1
    route_p = routes[(pc.key, "P")]
    route_n = routes[(pc.key, "N")]
    assert route_p.net == 1 and route_n.net == 2
    assert not route_p.vias and not route_n.vias  # planar v1
    # Legs terminate on their exact pads (polarity-correct attach).
    ends_p = {(s.x1, s.y1) for s in route_p.segments} | {(s.x2, s.y2) for s in route_p.segments}
    assert (pc.pad_p_a.x, pc.pad_p_a.y) in ends_p
    assert (pc.pad_p_b.x, pc.pad_p_b.y) in ends_p
    # The coupled body sits at exactly the pitch: the straight mid-corridor
    # segments of the two legs are pitch apart.
    min_sep = min(
        seg_seg_dist((sp.x1, sp.y1), (sp.x2, sp.y2), (sn.x1, sn.y1), (sn.x2, sn.y2))
        for sp in route_p.segments
        for sn in route_n.segments
    )
    assert min_sep == pytest.approx(_PITCH, abs=1e-4)
    # Emitted at the CLASS trace width (the pitch math depends on it).
    assert all(s.width == pytest.approx(_HS.trace_width) for s in route_p.segments)
    # Intra-pair clearance validator (engine-agnostic reuse) finds nothing.
    from kicad_tools.router.diffpair_routing import find_intra_pair_clearance_violations

    violation = find_intra_pair_clearance_violations(
        route_p, route_n, _HS.effective_intra_pair_clearance(), pair_name="D"
    )
    assert violation is None


def test_pair_declines_when_fat_envelope_is_blocked() -> None:
    # A wall slot 0.8 mm wide: passable for one 0.2/0.15 trace (band 0.3 mm)
    # but NOT for the 0.3-pitch fat agent (band 0.0 mm).  The pair must
    # decline with a reason and emit NOTHING -- never split into uncoupled
    # legs (#4270 v1 semantics).
    walls = [
        _pad(15.0, 9.15, 9, "WALL", ref="WT", width=0.5, height=6.5),
        _pad(15.0, 1.85, 9, "WALL", ref="WB", width=0.5, height=6.5),
    ]
    pf, pc = _pair_board(walls)
    routes, stats = pf.route_netset([], coupled=[pc])
    assert pf.pair_outcomes[pc.key] != "coupled"
    assert pf.failure_reasons[pc.key] == pf.pair_outcomes[pc.key]
    assert routes == {}
    assert stats.routed == 0 and stats.total == 1
    # The same corridor IS single-ended routable: the two nets as ordinary
    # connections complete (proves the decline was the fat envelope, not
    # the corridor).
    pf2, pc2 = _pair_board(walls)
    singles = [
        ((1, 0), pc2.pad_p_a, pc2.pad_p_b, None),
        ((2, 0), pc2.pad_n_a, pc2.pad_n_b, None),
    ]
    routes2, stats2 = pf2.route_netset(singles)
    assert stats2.routed == 2, pf2.failure_reasons


def test_pair_declines_on_polarity_twist_when_no_hook_room() -> None:
    # P/N swapped at the far end.  Full-height walls RIGHT behind both end
    # pad columns (grown keep-outs overlapping the pads' own) leave no hook
    # corridor to approach from behind, so the twist cannot be resolved ->
    # honest decline, nothing emitted.
    walls = [
        _pad(1.8, 6.0, 9, "WALL", ref="WA", width=0.4, height=12.0),
        _pad(28.2, 6.0, 9, "WALL", ref="WB", width=0.4, height=12.0),
    ]
    pf, pc = _pair_board(walls, swap_far_end=True)
    routes, _stats = pf.route_netset([], coupled=[pc])
    assert routes == {}
    assert pf.pair_outcomes[pc.key] != "coupled"


def test_pair_declines_with_no_planar_path() -> None:
    # A full-height wall between the ends on BOTH layers (the wall net has
    # a through-hole-like blocking twin on B.Cu) -> no planar corridor at
    # all; v1 pair agents are via-free so the pair declines.
    wall_f = _pad(15.0, 6.0, 9, "WALL", ref="WF", width=0.6, height=13.0)
    wall_b = Pad(
        x=15.0,
        y=6.0,
        width=0.6,
        height=13.0,
        net=9,
        net_name="WALL",
        layer=Layer.B_CU,
        ref="WB",
        pin="1",
    )
    pf, pc = _pair_board([wall_f, wall_b])
    routes, _stats = pf.route_netset([], coupled=[pc])
    assert routes == {}
    assert pf.pair_outcomes[pc.key] != "coupled"
    assert pc.key in pf.failure_reasons


def test_polarity_twist_resolves_via_hook_when_room_exists() -> None:
    # Same twist, but with open board behind the far end: the parity-
    # filtered stub search may approach from behind and un-twist.  Either
    # outcome is legal copper; if it couples, polarity must be correct.
    pf, pc = _pair_board(swap_far_end=True)
    routes, _stats = pf.route_netset([], coupled=[pc])
    if pf.pair_outcomes[pc.key] == "coupled":
        route_p = routes[(pc.key, "P")]
        ends_p = {(s.x1, s.y1) for s in route_p.segments} | {(s.x2, s.y2) for s in route_p.segments}
        assert (pc.pad_p_a.x, pc.pad_p_a.y) in ends_p
        assert (pc.pad_p_b.x, pc.pad_p_b.y) in ends_p
        from kicad_tools.router.diffpair_routing import find_intra_pair_clearance_violations

        violation = find_intra_pair_clearance_violations(
            route_p, routes[(pc.key, "N")], _HS.effective_intra_pair_clearance(), pair_name="D"
        )
        assert violation is None
    else:
        assert routes == {}


def test_coupled_pair_blocks_other_nets_at_full_copper_gap() -> None:
    # A single-ended net negotiated AFTER the pair must respect both legs.
    pf, pc = _pair_board(
        [
            _pad(3.0, 2.0, 7, "S", ref="S1"),
            _pad(27.0, 9.0, 7, "S", ref="S2"),
        ]
    )
    conns = [((7, 0), pf.pads[4], pf.pads[5], None)]
    routes, stats = pf.route_netset(conns, coupled=[pc])
    assert pf.pair_outcomes[pc.key] == "coupled"
    assert stats.routed == 2
    copper_gap = _RULES.trace_width + _RULES.trace_clearance
    single = routes[(7, 0)]
    for leg_key in ((pc.key, "P"), (pc.key, "N")):
        leg = routes[leg_key]
        for s1 in single.segments:
            for s2 in leg.segments:
                if s1.layer != s2.layer:
                    continue
                d = seg_seg_dist((s1.x1, s1.y1), (s1.x2, s1.y2), (s2.x1, s2.y1), (s2.x2, s2.y2))
                assert d >= copper_gap - 1e-6


# ---------------------------------------------------------------------------
# Engagement gating through the Autorouter hook (core.py)
# ---------------------------------------------------------------------------


def _autorouter(net_class_map: dict) -> object:
    from kicad_tools.router.core import Autorouter

    router = Autorouter(30, 12, strategy="lattice", net_class_map=net_class_map)
    pads = [
        _pad(3.0, 5.0, 1, "D+", ref="J1"),
        _pad(3.0, 6.0, 2, "D-", ref="J1"),
        _pad(27.0, 5.0, 1, "D+", ref="U1"),
        _pad(27.0, 6.0, 2, "D-", ref="U1"),
    ]
    for i, pad in enumerate(pads):
        key = (pad.ref, f"{i}")
        router.pads[key] = pad
        router.nets.setdefault(pad.net, []).append(key)
        router.net_names[pad.net] = pad.net_name
    return router


def test_core_engages_coupled_for_opted_in_class() -> None:
    router = _autorouter({"D+": _HS, "D-": _HS})
    routes = router.route_net(1)
    assert router._lattice_pair_outcomes == {"D": "coupled"}
    assert routes and all(r.net == 1 for r in routes)
    routes_n = router.route_net(2)
    assert routes_n and all(r.net == 2 for r in routes_n)
    assert router._lattice_pathfinder.lattice_builds == 1


def test_core_stays_single_ended_without_coupled_routing_opt_in() -> None:
    nc = NetClassRouting(
        name="HS_OFF",
        trace_width=0.2,
        clearance=0.15,
        intra_pair_clearance=0.1,
        coupled_routing=False,
    )
    router = _autorouter({"D+": nc, "D-": nc})
    routes = router.route_net(1)
    assert router._lattice_pair_outcomes == {}
    assert routes, "single-ended fallback must still route the net"
    # No coupled keys in the negotiated cache: both nets served as singles.
    assert router._lattice_negotiation_stats.total == 2
