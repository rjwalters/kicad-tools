"""Diff-pair coupled routing on the lattice engine (issue #4270, P3).

Unit coverage for the fat-agent centerline + geometric offset emission:

* offset octilinearity property (segment directions preserved -> #3907);
* exact-pitch emission on the coupled body;
* polarity-side selection (P leg lands on the P pads' side);
* decline paths: blocked fat envelope, polarity twist, no planar path --
  every decline carries a reason and, with the default
  ``uncoupled_fallback=False``, emits NOTHING (never split, #3906);
* decline reasons distinguished: ``pair-no-parity`` (no search ran) vs
  ``pair-no-path`` (an exhausted A*) (issue #6373);
* the #6373 uncoupled fallback: a declined pair's main run routed as two
  single-ended legs in the same pass, extras landing on connected copper;
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
    # but NOT for the 0.3-pitch fat agent (band 0.0 mm).  With the default
    # ``uncoupled_fallback=False`` the pair must decline with a reason and
    # emit NOTHING -- never split into uncoupled legs (#4270 v1 semantics,
    # still the pathfinder default; #6373 made the split opt-in).
    walls = _slot_walls()
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


def test_pair_declines_when_emitted_leg_violates_keepout() -> None:
    # A track-keepout rule area spanning the pair corridor on both layers
    # (#4605/#4672).  The fat centerline search deliberately skips the
    # keepout mask, so the coarse search succeeds -- the finish gate's
    # emitted-leg re-verification (probing every leg segment at trace_w/2)
    # must catch the violation and decline with the ``pair-leg-keepout``
    # attribution, emitting NOTHING (never split, #3906).
    from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask

    wall = KeepoutArea(
        polygon=((14.0, 0.0), (16.0, 0.0), (16.0, 12.0), (14.0, 12.0)),
        layers=frozenset({0, 1}),
        blocks_tracks=True,
        blocks_vias=True,
        only=None,
        exempt=frozenset(),
        name="pair-wall",
    )
    pf, pc = _pair_board()
    pf.set_keepouts(LatticeKeepoutMask([wall]))
    routes, stats = pf.route_netset([], coupled=[pc])
    assert pf.pair_outcomes[pc.key] == "pair-leg-keepout", pf.pair_outcomes
    assert pf.failure_reasons[pc.key] == "pair-leg-keepout"
    assert routes == {}
    assert stats.routed == 0 and stats.total == 1
    # Control: the identical board with no keepout couples cleanly (proves
    # the decline above is the keepout, not the corridor).
    pf2, pc2 = _pair_board()
    routes2, stats2 = pf2.route_netset([], coupled=[pc2])
    assert pf2.pair_outcomes[pc2.key] == "coupled"
    assert stats2.routed == 1 and routes2


def test_pair_declines_when_emitted_leg_violates_pairwise_pad_requirement() -> None:
    """Issue #4507: a coupled/fat leg must honour a mapped foreign PAD's HV
    pairwise requirement, not just the scalar floor.

    The fat centerline SEARCH deliberately skips pairwise widening (a real
    same-domain diff pair must not be over-constrained by a multi-mm HV
    keep-out on top of its own pitch) -- but the finish-gate's emitted-leg
    re-verification already re-covers foreign COMMITTED trace/via copper
    pairwise-aware (``CommittedCopper.seg_clear`` consults the installed
    projection).  Before this fix the same re-verification's PAD check
    (``pads_block_segment_grown``) was pure-scalar, so a mapped foreign pad
    close enough to violate the pairwise requirement but far enough to clear
    the ordinary DRU floor slipped through untouched -- precisely the T4
    softstart-rev-C residual's shape (a routed trace vs. a foreign pad, not
    caught by any predicate the coupled path consulted).
    """
    from kicad_tools.router.lattice.pairwise import LatticePairwise

    # The unobstructed pair's own body bulges to y in [6.25, 6.55] over the
    # flat mid-corridor (an artifact of the fat-agent stub search, pinned by
    # ``test_pair_routes_coupled_at_exact_pitch_with_correct_polarity``'s
    # sibling boards -- not something this test relies on staying put, since
    # both controls below re-derive the outcome from the SAME foreign pad).
    # Placed just above that body at y=8.0: its scalar keep-out edge (0.1
    # trace half + 0.25 agent radius baked into ``pad_rects`` + its own 0.5mm
    # half-height) sits at y=7.25 -- 0.70mm clear of the N leg at y=6.55, so
    # the pure-scalar check never blocks it.  Its pairwise keep-out grows by
    # ``own_half + 2.0mm requirement - agent_radius`` = 1.85mm further, to
    # y=5.4 -- past both legs, so the mapped 2.0mm requirement DOES block it.
    foreign = _pad(15.0, 8.0, 9, "HV_FOREIGN", ref="HV1", width=1.0, height=1.0)
    pf, pc = _pair_board([foreign])
    pf.set_pairwise(
        LatticePairwise(
            required_by_pair={(1, 9): 2.0, (2, 9): 2.0},
            zones=(),
            max_by_net={1: 2.0, 2: 2.0, 9: 2.0},
        )
    )
    routes, stats = pf.route_netset([], coupled=[pc])
    assert pf.pair_outcomes[pc.key] == "pair-leg-pairwise-blocked", pf.pair_outcomes
    assert pf.failure_reasons[pc.key] == "pair-leg-pairwise-blocked"
    assert routes == {}
    assert stats.routed == 0 and stats.total == 1
    # Control: the identical board with no pairwise projection installed
    # couples cleanly straight over the same foreign pad (proves the decline
    # above is the pairwise requirement, not the corridor or the pad itself).
    pf2, pc2 = _pair_board([foreign])
    routes2, stats2 = pf2.route_netset([], coupled=[pc2])
    assert pf2.pair_outcomes[pc2.key] == "coupled"
    assert stats2.routed == 1 and routes2


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
    # Issue #6373: stubs exist at both ends with a consistent parity, so the
    # search RAN and was exhausted -> the reason is exactly ``pair-no-path``.
    assert pf.pair_outcomes[pc.key] == "pair-no-path", pf.pair_outcomes


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


# ---------------------------------------------------------------------------
# Issue #6373: decline-reason split + uncoupled fallback
# ---------------------------------------------------------------------------


def _slot_walls() -> list[Pad]:
    """Two wall pads leaving a 0.8 mm slot: single-trace OK, fat pair not."""
    return [
        _pad(15.0, 9.15, 9, "WALL", ref="WT", width=0.5, height=6.5),
        _pad(15.0, 1.85, 9, "WALL", ref="WB", width=0.5, height=6.5),
    ]


def _route_touches(route, pad: Pad) -> bool:
    half_w, half_h = pad.width / 2.0 + 1e-6, pad.height / 2.0 + 1e-6
    for seg in route.segments:
        for x, y in ((seg.x1, seg.y1), (seg.x2, seg.y2)):
            if abs(x - pad.x) <= half_w and abs(y - pad.y) <= half_h:
                return True
    return False


def _net_connected(routes: list, pads: list[Pad]) -> bool:
    """Every pad lies in ONE component of the routes' segment graph."""
    parent: dict[object, object] = {}

    def find(x: object) -> object:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: object, b: object) -> None:
        parent[find(a)] = find(b)

    def pt(x: float, y: float) -> tuple[float, float]:
        return (round(x, 4), round(y, 4))

    for route in routes:
        for seg in route.segments:
            union(pt(seg.x1, seg.y1), pt(seg.x2, seg.y2))
    for i, pad in enumerate(pads):
        for route in routes:
            for seg in route.segments:
                for x, y in ((seg.x1, seg.y1), (seg.x2, seg.y2)):
                    if (
                        abs(x - pad.x) <= pad.width / 2.0 + 1e-6
                        and abs(y - pad.y) <= pad.height / 2.0 + 1e-6
                    ):
                        union(("pad", i), pt(x, y))
    roots = {find(("pad", i)) for i in range(len(pads))}
    return len(roots) == 1


def test_pair_no_parity_reason_when_no_consistent_parity(monkeypatch) -> None:
    # Force every A-end stub onto parity True and every B-end stub onto
    # parity False: no parity has stubs on BOTH sides, so no search may run
    # and the decline is the distinct ``pair-no-parity`` (#6373), never the
    # exhausted-A* ``pair-no-path``.
    from kicad_tools.router.lattice import coupled as coupled_mod

    pf, pc = _pair_board()
    monkeypatch.setattr(coupled_mod, "side_bit", lambda _d, pad_p, _pad_n: pad_p is pc.pad_p_a)
    searches: list[object] = []
    real_route_impl = pf._route_impl

    def counting_route_impl(*args, **kwargs):
        searches.append(kwargs.get("extra_clearance"))
        return real_route_impl(*args, **kwargs)

    monkeypatch.setattr(pf, "_route_impl", counting_route_impl)
    pf.build()
    result, reason = pf._route_pair_impl(
        pc, committed=pf._fresh_committed(), history={}, present=1.0
    )
    assert result is None
    assert reason == "pair-no-parity"
    assert searches == [], "no fat search may run without a consistent parity"


def test_uncoupled_fallback_routes_both_legs_when_fat_envelope_blocked() -> None:
    pf, pc = _pair_board(_slot_walls())
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    outcome = pf.pair_outcomes[pc.key]
    assert outcome.startswith("uncoupled-fallback:"), pf.pair_outcomes
    # The underlying coupled decline reason is carried in the outcome.
    assert outcome.split(":", 1)[1].startswith("pair-")
    assert set(routes) == {(pc.key, "P"), (pc.key, "N")}
    assert stats.routed == 2 and stats.total == 2, stats
    assert pf.failure_reasons == {}, pf.failure_reasons
    assert stats.converged
    # Each leg is its own net and lands on both of its own pads.
    route_p, route_n = routes[(pc.key, "P")], routes[(pc.key, "N")]
    assert route_p.net == 1 and route_n.net == 2
    assert _route_touches(route_p, pc.pad_p_a) and _route_touches(route_p, pc.pad_p_b)
    assert _route_touches(route_n, pc.pad_n_a) and _route_touches(route_n, pc.pad_n_b)
    # Never ships a short: legs keep the full copper gap from each other.
    copper_gap = _RULES.trace_width + _RULES.trace_clearance
    for s1 in route_p.segments:
        for s2 in route_n.segments:
            if s1.layer == s2.layer:
                d = seg_seg_dist((s1.x1, s1.y1), (s1.x2, s1.y2), (s2.x1, s2.y1), (s2.x2, s2.y2))
                assert d >= copper_gap - 1e-6


def test_uncoupled_fallback_reports_unroutable_leg_without_emitting_it() -> None:
    # Full-height wall on both layers: neither the pair NOR a single leg can
    # cross (legs are routed with vias allowed, but the wall spans both
    # layers).  Each failing leg is reported under its own key and nothing
    # partial is emitted.
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
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert pf.pair_outcomes[pc.key] == "uncoupled-fallback:pair-no-path"
    assert routes == {}
    assert set(pf.failure_reasons) == {(pc.key, "P"), (pc.key, "N")}
    assert stats.routed == 0 and stats.total == 2


def test_uncoupled_fallback_coupled_pair_unchanged() -> None:
    # A pair that couples must be byte-for-byte the same with the flag on.
    pf, pc = _pair_board()
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    pf0, pc0 = _pair_board()
    routes0, stats0 = pf0.route_netset([], coupled=[pc0])
    assert pf.pair_outcomes[pc.key] == "coupled"
    assert stats.routed == stats0.routed == 1 and stats.total == stats0.total == 1
    for key in ((pc.key, "P"), (pc.key, "N")):
        segs = [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in routes[key].segments]
        segs0 = [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in routes0[key].segments]
        assert segs == segs0


def test_uncoupled_fallback_recovers_coupled_in_later_pass(monkeypatch) -> None:
    # Regression (#6377 review): a pair that falls back on pass 1 because of
    # congestion and COUPLES on pass 2 must come out coupled.  Both passes
    # complete the pair (routed_items ties), so the best-pass tie-break must
    # rank the coupled pass above the earlier uncoupled-fallback pass.
    pf, pc = _pair_board()
    real_pair_impl = pf._route_pair_impl
    calls = {"n": 0}

    def decline_first_live_call(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, "pair-blocked-congestion"
        return real_pair_impl(*args, **kwargs)

    monkeypatch.setattr(pf, "_route_pair_impl", decline_first_live_call)
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert stats.iterations == 2, stats
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_outcomes
    assert stats.converged
    assert stats.routed == 1 and stats.total == 1, stats
    assert set(routes) == {(pc.key, "P"), (pc.key, "N")}
    assert pf.failure_reasons == {}, pf.failure_reasons


def test_uncoupled_fallback_extras_land_on_connected_copper() -> None:
    # 3 pads per net: the coupled main run J1 -> U1 is blocked by the slot,
    # and each net has an extra pad anchored (as the autorouter does) on the
    # FAR reserved endpoint.  Without the fallback the extras form islands
    # with the far endpoint; with it every pad of each net is one component.
    extra_p = _pad(27.0, 2.5, 1, "D+", ref="U2")
    extra_n = _pad(27.0, 8.5, 2, "D-", ref="U2")
    pf, pc = _pair_board(_slot_walls() + [extra_p, extra_n])
    singles = [
        ((1, 0), pc.pad_p_b, extra_p, _HS),
        ((2, 0), pc.pad_n_b, extra_n, _HS),
    ]

    routes0, stats0 = pf.route_netset(singles, coupled=[pc])
    assert stats0.routed == 2, "extras route; only the coupled main run is open"
    assert not _net_connected(
        [r for r in routes0.values() if r.net == 1], [pc.pad_p_a, pc.pad_p_b, extra_p]
    )

    pf2, pc2 = _pair_board(_slot_walls() + [extra_p, extra_n])
    singles2 = [
        ((1, 0), pc2.pad_p_b, extra_p, _HS),
        ((2, 0), pc2.pad_n_b, extra_n, _HS),
    ]
    routes, stats = pf2.route_netset(singles2, coupled=[pc2], uncoupled_fallback=True)
    assert pf2.pair_outcomes[pc2.key].startswith("uncoupled-fallback:")
    assert stats.routed == stats.total == 4, (stats, pf2.failure_reasons)
    for net, pads in (
        (1, [pc2.pad_p_a, pc2.pad_p_b, extra_p]),
        (2, [pc2.pad_n_a, pc2.pad_n_b, extra_n]),
    ):
        assert _net_connected([r for r in routes.values() if r.net == net], pads), net


def test_core_uses_uncoupled_fallback_for_blocked_pair(capsys) -> None:
    # Through the Autorouter (core.py): the lattice engine always enables
    # the fallback, so a blocked pair is reported as a diff-pair WARNING and
    # both nets still get copper (issue #6373).
    router = _autorouter({"D+": _HS, "D-": _HS})
    for i, wall in enumerate(_slot_walls()):
        # One single-pad net per wall: an obstacle, never itself routed.
        wall = Pad(
            x=wall.x,
            y=wall.y,
            width=wall.width,
            height=wall.height,
            net=20 + i,
            net_name=f"WALL{i}",
            layer=Layer.F_CU,
            ref=wall.ref,
            pin="1",
        )
        key = (wall.ref, "1")
        router.pads[key] = wall
        router.nets.setdefault(wall.net, []).append(key)
        router.net_names[wall.net] = wall.net_name
    routes_p = router.route_net(1)
    outcome = router._lattice_pair_outcomes["D"]
    assert outcome.startswith("uncoupled-fallback:"), router._lattice_pair_outcomes
    assert routes_p and all(r.net == 1 for r in routes_p)
    routes_n = router.route_net(2)
    assert routes_n and all(r.net == 2 for r in routes_n)
    out = capsys.readouterr().out
    assert "WARNING: diff pair D routed uncoupled" in out
    stats = router._lattice_negotiation_stats
    assert stats.routed == stats.total == 2


# ---------------------------------------------------------------------------
# Issue #6378: chain-ordered endpoints through a pass-through part
# ---------------------------------------------------------------------------

# A SOT-23-6-style flow-through ESD part (board 03 U2), centred at (15, 6)
# with the signal flowing along +x: the entry column carries D+ / GND / D-
# (pins 3/2/1), the exit column D+ / VBUS / D- (pins 4/5/6).  P and N are
# 1.9 mm apart with a FOREIGN pad between them on both sides, so each
# column's P/N midpoint lands on that foreign pad.
_SOT_W, _SOT_H = 1.06, 0.6
_SOT_IN_X, _SOT_OUT_X = 13.86, 16.14


def _chain_pads() -> dict[str, Pad]:
    def sot(x: float, y: float, net: int, name: str, pin: str) -> Pad:
        return Pad(
            x=x,
            y=y,
            width=_SOT_W,
            height=_SOT_H,
            net=net,
            net_name=name,
            layer=Layer.F_CU,
            ref="U2",
            pin=pin,
        )

    return {
        "J1.P": _pad(3.0, 5.5, 1, "D+", ref="J1"),
        "J1.N": _pad(3.0, 6.5, 2, "D-", ref="J1"),
        "U2.3": sot(_SOT_IN_X, 5.05, 1, "D+", "3"),
        "U2.2": sot(_SOT_IN_X, 6.0, 9, "GND", "2"),
        "U2.1": sot(_SOT_IN_X, 6.95, 2, "D-", "1"),
        "U2.4": sot(_SOT_OUT_X, 5.05, 1, "D+", "4"),
        "U2.5": sot(_SOT_OUT_X, 6.0, 10, "VBUS", "5"),
        "U2.6": sot(_SOT_OUT_X, 6.95, 2, "D-", "6"),
        "U1.P": _pad(27.0, 5.5, 1, "D+", ref="U1"),
        "U1.N": _pad(27.0, 6.5, 2, "D-", ref="U1"),
    }


def test_choose_pair_chain_goes_through_pass_through_part() -> None:
    from kicad_tools.router.lattice.coupled import choose_pair_chain

    pads = _chain_pads()
    ps = [pads["J1.P"], pads["U2.3"], pads["U2.4"], pads["U1.P"]]
    ns = [pads["J1.N"], pads["U2.1"], pads["U2.6"], pads["U1.N"]]
    chain = choose_pair_chain(ps, ns)
    assert chain is not None
    # J1 -> U2 entry, U2 exit -> U1 -- NOT the J1 -> U1 trunk across U2.
    assert chain.segments == [((0, 0), (1, 1)), ((2, 2), (3, 3))]
    assert chain.hops == [((1, 1), (2, 2))]
    assert choose_pair_endpoints(ps, ns) == ((0, 0), (3, 3)), "the ends are unchanged"


def test_choose_pair_chain_is_the_trunk_without_interior_parts() -> None:
    from kicad_tools.router.lattice.coupled import choose_pair_chain

    # Two couples: exactly today's single farthest-apart trunk.
    ps = [_pad(3.0, 5.0, 1, "D+", ref="J1"), _pad(27.0, 5.0, 1, "D+", ref="U1")]
    ns = [_pad(3.0, 6.0, 2, "D-", ref="J1"), _pad(27.0, 6.0, 2, "D-", ref="U1")]
    chain = choose_pair_chain(ps, ns)
    assert chain is not None
    assert chain.segments == [choose_pair_endpoints(ps, ns)] and chain.hops == []
    # USB-C dual row: the second row sits on the END part (J1), so it is an
    # extra, not an interior part -- still the single trunk (board 06 USB2_D).
    ps3 = ps + [_pad(4.35, 5.0, 1, "D+", ref="J1")]
    ns3 = ns + [_pad(4.35, 6.0, 2, "D-", ref="J1")]
    chain3 = choose_pair_chain(ps3, ns3)
    assert chain3 is not None
    assert chain3.segments == [choose_pair_endpoints(ps3, ns3)] and chain3.hops == []
    assert choose_pair_chain(ps[:1], ns[:1]) is None


def test_relocate_pair_anchor_moves_blocked_midpoint_off_the_part() -> None:
    from kicad_tools.router.lattice.coupled import relocate_pair_anchor
    from kicad_tools.router.lattice.history import PhysicalHistory

    pads = _chain_pads()
    order = list(pads.values())
    pf = LatticePathfinder(
        [(0.0, 0.0), (30.0, 0.0), (30.0, 12.0), (0.0, 12.0)], order, _RULES, LayerStack.two_layer()
    )
    obstacles = pf.obstacles
    half = _PITCH / 2.0

    def exempt_for(*names: str) -> frozenset[int]:
        return frozenset(order.index(pads[n]) for n in names)

    seg_pads = ("U2.4", "U2.6", "U1.P", "U1.N")
    anchor = relocate_pair_anchor(
        obstacles,
        pads["U2.4"],
        pads["U2.6"],
        (27.0, 6.0),
        half_pitch=half,
        layers=(0,),
        exempt=exempt_for(*seg_pads),
        pair_nets={1, 2},
    )
    assert anchor is not None
    vbus = obstacles.pad_rects[order.index(pads["U2.5"])]
    assert anchor[0] > vbus[2] + half, "anchor clears the VBUS keep-out grown by pitch/2"
    assert anchor[1] == pytest.approx(6.0), "slides perpendicular to the P-N axis"
    assert anchor[0] - _SOT_OUT_X < 3.0, "computed shift, not a runaway"

    # Keepout-aware (board 03: U2.5's #6401 VBUS via reservation sits just
    # outside the part, where the anchor would otherwise land): the anchor
    # moves past a track-blocking area and the doglegs avoid it.
    from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask

    vx = anchor[0]
    via_site = KeepoutArea(
        polygon=((vx - 0.4, 5.6), (vx + 0.4, 5.6), (vx + 0.4, 6.4), (vx - 0.4, 6.4)),
        layers=frozenset({0, 1}),
        blocks_tracks=True,
        blocks_vias=True,
    )
    mask = LatticeKeepoutMask([via_site])
    moved = relocate_pair_anchor(
        obstacles,
        pads["U2.4"],
        pads["U2.6"],
        (27.0, 6.0),
        half_pitch=half,
        layers=(0,),
        exempt=exempt_for(*seg_pads),
        pair_nets={1, 2},
        keepouts=mask,
        trace_half=_HS.trace_width / 2.0,
    )
    assert moved is not None and moved[0] > vx + 0.4 + half
    assert not mask.segment_blocked(moved, moved, 0, 1, half + _HS.trace_width / 2.0)

    # An unblocked midpoint keeps anchor=None.
    assert (
        relocate_pair_anchor(
            obstacles,
            pads["U1.P"],
            pads["U1.N"],
            (15.0, 6.0),
            half_pitch=half,
            layers=(0,),
            exempt=exempt_for(*seg_pads),
            pair_nets={1, 2},
        )
        is None
    )

    # The relocated segment routes COUPLED on the otherwise empty board; the
    # un-relocated one cannot leave the part (pair-escape-a).
    pc = CoupledConnection(
        key=("pair", 1, 2, 1),
        pair_name="D",
        pad_p_a=pads["U2.4"],
        pad_n_a=pads["U2.6"],
        pad_p_b=pads["U1.P"],
        pad_n_b=pads["U1.N"],
        net_class=_HS,
        pitch=_PITCH,
    )
    res, reason = pf._route_pair_impl(
        pc, committed=pf._fresh_committed(), history=PhysicalHistory(), present=1.0
    )
    assert res is None and reason == "pair-escape-a"
    from dataclasses import replace

    res, reason = pf._route_pair_impl(
        replace(pc, anchor_a=anchor),
        committed=pf._fresh_committed(),
        history=PhysicalHistory(),
        present=1.0,
    )
    assert res is not None, reason


def _chain_autorouter(extra: list[Pad] | None = None) -> object:
    from kicad_tools.router.core import Autorouter

    router = Autorouter(30, 12, strategy="lattice", net_class_map={"D+": _HS, "D-": _HS})
    for pad in list(_chain_pads().values()) + list(extra or []):
        key = (pad.ref, pad.pin if pad.ref == "U2" else f"{pad.ref}{pad.net}{pad.x}")
        router.pads[key] = pad
        router.nets.setdefault(pad.net, []).append(key)
        router.net_names[pad.net] = pad.net_name
    return router


def test_core_chains_pair_through_pass_through_part(capsys) -> None:
    router = _chain_autorouter()
    coupled, reserved = router._lattice_coupled_connections()
    assert [pc.key for pc in coupled] == [("pair", 1, 2, 0), ("pair", 1, 2, 1)]
    # The U2-side end of each segment is relocated off the part; the J1 / U1
    # ends are clear and keep the midpoint.
    assert coupled[0].anchor_a is None and coupled[0].anchor_b is not None
    assert coupled[0].anchor_b[0] < _SOT_IN_X
    assert coupled[1].anchor_a is not None and coupled[1].anchor_b is None
    assert coupled[1].anchor_a[0] > _SOT_OUT_X
    assert len(reserved[1]) == 4 and len(reserved[2]) == 4
    assert [(h[1], h[2].pin, h[3].pin) for h in router._lattice_pair_hops] == [
        (1, "3", "4"),
        (2, "1", "6"),
    ]

    routes_p = router.route_net(1)
    routes_n = router.route_net(2)
    assert router._lattice_pair_outcomes == {"D": "coupled"}, router._lattice_pair_outcomes
    assert [o for _n, _l, o in router._lattice_pair_segment_outcomes] == ["coupled", "coupled"]
    pads = _chain_pads()
    assert _net_connected(routes_p, [pads[k] for k in ("J1.P", "U2.3", "U2.4", "U1.P")])
    assert _net_connected(routes_n, [pads[k] for k in ("J1.N", "U2.1", "U2.6", "U1.N")])
    stats = router._lattice_negotiation_stats
    assert stats.routed == stats.total == 4, stats  # 2 segments + 2 hops
    out = capsys.readouterr().out
    assert "D[0] J1.1->U2.3: coupled" in out and "D[1] U2.4->U1.1: coupled" in out


def test_core_reports_partially_coupled_pair(capsys) -> None:
    # A slot wall in the J1 -> U2 segment: that segment falls back
    # single-ended (#6373, per segment) while U2 -> U1 still couples.
    walls = [
        Pad(
            x=8.0,
            y=y,
            width=0.5,
            height=h,
            net=20 + i,
            net_name=f"WALL{i}",
            layer=Layer.F_CU,
            ref=f"W{i}",
            pin="1",
        )
        for i, (y, h) in enumerate(((9.15, 6.5), (1.85, 6.5)))
    ]
    router = _chain_autorouter(walls)
    routes_p = router.route_net(1)
    router.route_net(2)
    assert router._lattice_pair_outcomes == {"D": "partially-coupled:1/2"}
    segs = router._lattice_pair_segment_outcomes
    assert segs[0][2].startswith("uncoupled-fallback:") and segs[1][2] == "coupled"
    pads = _chain_pads()
    assert _net_connected(routes_p, [pads[k] for k in ("J1.P", "U2.3", "U2.4", "U1.P")])
    out = capsys.readouterr().out
    assert "WARNING: diff pair D only partially coupled (1/2 segments)" in out
    assert "J1.1->U2.3 (uncoupled-fallback:" in out


def test_core_single_segment_pair_keeps_legacy_key() -> None:
    router = _autorouter({"D+": _HS, "D-": _HS})
    coupled, reserved = router._lattice_coupled_connections()
    assert [pc.key for pc in coupled] == [("pair", 1, 2)]
    assert coupled[0].anchor_a is None and coupled[0].anchor_b is None
    assert router._lattice_pair_hops == []
    assert all(len(v) == 2 for v in reserved.values())


def test_aggregate_pair_outcome() -> None:
    from kicad_tools.router.core import _aggregate_pair_outcome

    assert _aggregate_pair_outcome(["pair-no-path"]) == "pair-no-path"
    assert _aggregate_pair_outcome(["coupled", "coupled"]) == "coupled"
    assert _aggregate_pair_outcome(["uncoupled-fallback:x", "coupled"]) == "partially-coupled:1/2"
    assert (
        _aggregate_pair_outcome(["uncoupled-fallback:x", "uncoupled-fallback:y"])
        == "uncoupled-fallback:x"
    )


def test_lattice_key_naming_accepts_chain_keys() -> None:
    from types import SimpleNamespace

    from kicad_tools.cli.route_cmd import _net_name_for_lattice_key

    router = SimpleNamespace(net_names={1: "D+", 2: "D-", 5: "SIG"})
    assert _net_name_for_lattice_key(router, ("pair", 1, 2)) == "D+/D-"
    assert _net_name_for_lattice_key(router, ("pair", 1, 2, 1)) == "D+/D-"
    assert _net_name_for_lattice_key(router, (("pair", 1, 2, 0), "N")) == "D-"
    assert _net_name_for_lattice_key(router, (("pair", 1, 2), "P")) == "D+"
    assert _net_name_for_lattice_key(router, (1, "hop", 0)) == "D+"
    assert _net_name_for_lattice_key(router, (5, 3)) == "SIG"


def _trunk_autorouter(extra: list[Pad]) -> object:
    """J1 -> U1 pair (as on ``_chain_autorouter``) plus ``extra`` pads."""
    from kicad_tools.router.core import Autorouter

    router = Autorouter(30, 12, strategy="lattice", net_class_map={"D+": _HS, "D-": _HS})
    base = [
        _pad(3.0, 5.5, 1, "D+", ref="J1"),
        _pad(3.0, 6.5, 2, "D-", ref="J1"),
        _pad(27.0, 5.5, 1, "D+", ref="U1"),
        _pad(27.0, 6.5, 2, "D-", ref="U1"),
    ]
    for pad in base + extra:
        key = (pad.ref, f"{pad.pin}-{pad.net}-{pad.x}-{pad.y}")
        router.pads[key] = pad
        router.nets.setdefault(pad.net, []).append(key)
        router.net_names[pad.net] = pad.net_name
    return router


@pytest.mark.parametrize(
    "tp1, tp2",
    [
        ((15.0, 2.0), (15.0, 10.0)),  # far apart (8 mm), either side of the trunk
        ((15.0, 3.5), (16.0, 3.5)),  # adjacent (1 mm)
    ],
    ids=["far", "adjacent"],
)
def test_one_test_point_per_net_keeps_single_coupled_trunk(tp1, tp2) -> None:
    """A couple whose P and N sit on different refs (TP1 on D+, TP2 on D-)
    is not a pass-through part: the pair keeps the single J1 -> U1 trunk
    and the test points attach as extras, exactly as before #6378."""
    from kicad_tools.router.lattice.coupled import choose_pair_chain

    tps = [_pad(*tp1, 1, "D+", ref="TP1"), _pad(*tp2, 2, "D-", ref="TP2")]
    router = _trunk_autorouter(tps)
    coupled, _reserved = router._lattice_coupled_connections()
    assert [pc.key for pc in coupled] == [("pair", 1, 2)], "legacy single-trunk key"
    assert coupled[0].anchor_a is None and coupled[0].anchor_b is None
    assert router._lattice_pair_hops == []
    assert {coupled[0].pad_p_a.ref, coupled[0].pad_p_b.ref} == {"J1", "U1"}

    ps = [p for p in router.pads.values() if p.net == 1]
    ns = [p for p in router.pads.values() if p.net == 2]
    chain = choose_pair_chain(ps, ns)
    assert chain is not None
    assert chain.segments == [choose_pair_endpoints(ps, ns)] and chain.hops == []

    routes_p = router.route_net(1)
    routes_n = router.route_net(2)
    assert router._lattice_pair_outcomes == {"D": "coupled"}, router._lattice_pair_outcomes
    assert _net_connected(routes_p, ps)
    assert _net_connected(routes_n, ns)


def test_nets_through_different_parts_keep_single_trunk() -> None:
    """D+ runs through U2 (pins 1/6) and D- through U3 (pins 1/6): neither
    part carries both nets, so neither is a pass-through part of the PAIR.
    The pair keeps the single trunk (no cross-ref waypoints, no hops)."""
    from kicad_tools.router.lattice.coupled import choose_pair_chain

    def pin(x: float, y: float, net: int, ref: str, p: str) -> Pad:
        pad = _pad(x, y, net, "D+" if net == 1 else "D-", ref=ref)
        pad.pin = p
        return pad

    parts = [
        pin(13.0, 2.5, 1, "U2", "1"),
        pin(17.0, 2.5, 1, "U2", "6"),
        pin(13.0, 9.5, 2, "U3", "1"),
        pin(17.0, 9.5, 2, "U3", "6"),
    ]
    router = _trunk_autorouter(parts)
    ps = [p for p in router.pads.values() if p.net == 1]
    ns = [p for p in router.pads.values() if p.net == 2]
    chain = choose_pair_chain(ps, ns)
    assert chain is not None
    assert chain.segments == [choose_pair_endpoints(ps, ns)] and chain.hops == []

    coupled, _reserved = router._lattice_coupled_connections()
    assert [pc.key for pc in coupled] == [("pair", 1, 2)]
    assert router._lattice_pair_hops == []
    routes_p = router.route_net(1)
    routes_n = router.route_net(2)
    assert router._lattice_pair_outcomes == {"D": "coupled"}, router._lattice_pair_outcomes
    assert _net_connected(routes_p, ps)
    assert _net_connected(routes_n, ns)
