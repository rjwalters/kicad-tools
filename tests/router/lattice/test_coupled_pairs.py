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
    # (#4605/#4672).  The wall is un-passable, so the pair must decline and
    # emit NOTHING (never split, #3906).  Since #6697 the fat centerline
    # search sees the mask itself and reports ``pair-no-path``; the finish
    # gate's emitted-leg re-verification stays as the backstop for the attach
    # doglegs (``pair-leg-keepout``).
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
    assert pf.pair_outcomes[pc.key] in {
        "pair-no-path",
        "pair-no-path-keepout-constrained",
        "pair-leg-keepout",
    }, pf.pair_outcomes
    assert pf.failure_reasons[pc.key] == pf.pair_outcomes[pc.key]
    assert routes == {}
    assert stats.routed == 0 and stats.total == 1
    # Control: the identical board with no keepout couples cleanly (proves
    # the decline above is the keepout, not the corridor).
    pf2, pc2 = _pair_board()
    routes2, stats2 = pf2.route_netset([], coupled=[pc2])
    assert pf2.pair_outcomes[pc2.key] == "coupled"
    assert stats2.routed == 1 and routes2


def test_pair_detours_around_a_keepout_reservation_and_still_couples() -> None:
    # Issue #6697: a small keepout (the shape of a #6401 pour-pad via
    # reservation) sits ON the straight pair corridor, with room to go
    # around it.  The fat centerline search used to skip the keepout mask, so
    # the shortest path ran straight through it and the finish gate declined
    # the pair (``pair-leg-keepout``) -- on board 03 this lost USB_MCU_D's
    # coupling once negotiation history displaced the centerline onto the
    # U1.5 GND reservation.  The search must route around it and couple.
    from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask

    site = KeepoutArea(
        polygon=((14.0, 4.0), (16.0, 4.0), (16.0, 7.0), (14.0, 7.0)),
        layers=frozenset({0, 1}),
        blocks_tracks=True,
        blocks_vias=True,
        only=None,
        exempt=frozenset(),
        name="plane-via-fill:GND:U1.5",
    )
    pf, pc = _pair_board()
    mask = LatticeKeepoutMask([site])
    pf.set_keepouts(mask)
    routes, _stats = pf.route_netset([], coupled=[pc])
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_outcomes
    for pol in ("P", "N"):
        for seg in routes[(pc.key, pol)].segments:
            layer = 0 if seg.layer.name.startswith("F") else 1
            assert not mask.segment_blocked(
                (seg.x1, seg.y1), (seg.x2, seg.y2), layer, seg.net, seg.width / 2.0
            )


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


def test_termination_tap_stays_on_the_trunk() -> None:
    """A termination resistor straddling P and N (one couple, entry == exit)
    is a tap, not a pass-through part: the pair keeps the single trunk
    (board 06 LVDS regression, #6378)."""
    from kicad_tools.router.lattice.coupled import choose_pair_chain

    ps = [_pad(3.0, 5.5, 1, "D+", ref="U1"), _pad(27.0, 5.5, 1, "D+", ref="U2")]
    ns = [_pad(3.0, 6.5, 2, "D-", ref="U1"), _pad(27.0, 6.5, 2, "D-", ref="U2")]
    ps.append(_pad(15.0, 5.5, 1, "D+", ref="R1"))
    ns.append(_pad(15.0, 6.5, 2, "D-", ref="R1"))
    chain = choose_pair_chain(ps, ns)
    assert chain is not None
    assert chain.segments == [choose_pair_endpoints(ps, ns)] and chain.hops == []


def test_board06_lvds_pairs_keep_single_trunk() -> None:
    """The CURRENT board 06 LVDS design (terminations R1-R4) must keep every
    pair on one coupled trunk, as on main (3/4 coupled there)."""
    from pathlib import Path

    from kicad_tools.router.io import load_pcb_for_routing

    board = (
        Path(__file__).resolve().parents[3]
        / "boards/06-diffpair-test/output/diffpair_test.kicad_pcb"
    )
    router, _ = load_pcb_for_routing(
        str(board),
        skip_nets=["GND", "+3V3", "+1V8", "+1V2", "VBUS_USB"],
        strategy="lattice",
        force_python=True,
    )
    coupled, _reserved = router._lattice_coupled_connections()
    lvds = [pc for pc in coupled if pc.pair_name.startswith("LVDS")]
    assert len(lvds) == 4, [pc.pair_name for pc in coupled]
    assert all(len(pc.key) == 3 for pc in lvds), "no chain segments for taps"
    assert router._lattice_pair_hops == []


# ---------------------------------------------------------------------------
# Issue #6535: tap chain as the fallback for a declined single trunk
# ---------------------------------------------------------------------------


def _tap_pads(x: float = 15.0) -> list[Pad]:
    """A board-06-style termination resistor straddling the pair at ``x``
    (0603 rotated 90 deg: P/N pads 1.65 mm apart, centred on the pair)."""
    return [
        _pad(x, 5.175, 1, "D+", ref="R1", width=0.95, height=0.8),
        _pad(x, 6.825, 2, "D-", ref="R1", width=0.95, height=0.8),
    ]


def _tap_walls(x: float = 15.0, gap: float = 0.65) -> list[Pad]:
    """Foreign walls above and below the tap: a single trace still passes
    (``gap`` > trace + 2 x clearance), but the fat pair cannot get around
    the tap, and the tap's own pads close the corridor between them."""
    top = 5.175 - 0.4 - gap
    bot = 6.825 + 0.4 + gap
    return [
        Pad(
            x=x,
            y=y,
            width=0.5,
            height=h,
            net=20 + i,
            net_name=f"WALL{i}",
            layer=Layer.F_CU,
            ref=f"W{i}",
            pin="1",
        )
        for i, (y, h) in enumerate(((top / 2, top), ((bot + 12.0) / 2, 12.0 - bot)))
    ]


def test_choose_pair_tap_chain_splits_at_the_tap() -> None:
    from kicad_tools.router.lattice.coupled import choose_pair_chain, choose_pair_tap_chain

    ps = [_pad(3.0, 5.5, 1, "D+", ref="U1"), _pad(27.0, 5.5, 1, "D+", ref="U2")]
    ns = [_pad(3.0, 6.5, 2, "D-", ref="U1"), _pad(27.0, 6.5, 2, "D-", ref="U2")]
    # No tap: there is no alternative to retry.
    assert choose_pair_tap_chain(ps, ns) is None
    ps.append(_pad(15.0, 5.5, 1, "D+", ref="R1"))
    ns.append(_pad(15.0, 6.5, 2, "D-", ref="R1"))
    trunk = choose_pair_chain(ps, ns)
    assert trunk is not None and len(trunk.segments) == 1  # the default is unchanged
    chain = choose_pair_tap_chain(ps, ns)
    assert chain is not None
    a, b = trunk.segments[0]
    # end A -> tap couple -> end B; a tap has no hop (entry == exit).
    assert chain.segments == [(a, (2, 2)), ((2, 2), b)]
    assert chain.hops == []


def test_choose_pair_tap_chain_is_none_for_a_pass_through_only_pair() -> None:
    from kicad_tools.router.lattice.coupled import choose_pair_tap_chain

    pads = _chain_pads()
    ps = [pads["J1.P"], pads["U2.3"], pads["U2.4"], pads["U1.P"]]
    ns = [pads["J1.N"], pads["U2.1"], pads["U2.6"], pads["U1.N"]]
    # The pass-through chain already IS the default chain: nothing to retry.
    assert choose_pair_tap_chain(ps, ns) is None


def test_declined_trunk_is_retried_as_a_tap_chain(capsys, monkeypatch) -> None:
    """The trunk cannot pass the tap (its pads close the corridor, walls stop
    a detour) and declines ``pair-no-path``; the IC -> tap -> IC retry
    couples copper on BOTH nets and every pad stays connected."""
    import kicad_tools.router.core as core

    pads = _tap_pads() + _tap_walls()

    # Control: with the retry removed the trunk falls back uncoupled.
    monkeypatch.setattr(core, "_lattice_tap_chain_retry_pairs", lambda *a: frozenset())
    router = _trunk_autorouter(pads)
    router.route_net(1)
    assert router._lattice_pair_outcomes == {"D": "uncoupled-fallback:pair-no-path"}
    monkeypatch.undo()
    capsys.readouterr()

    router = _trunk_autorouter(pads)
    routes_p = router.route_net(1)
    routes_n = router.route_net(2)
    out = capsys.readouterr().out
    assert "tap-chain retry for 1 pair(s) whose trunk declined pair-no-path (D): kept" in out
    outcome = router._lattice_pair_outcomes["D"]
    assert outcome == "coupled" or outcome.startswith("partially-coupled:"), outcome
    segs = router._lattice_pair_segment_outcomes
    assert [label for _n, label, _o in segs] == ["J1.1->R1.1", "R1.1->U1.1"]
    assert any(o == "coupled" for _n, _l, o in segs)
    # Both legs present: copper on both nets, each net joined through the tap.
    ps = [p for p in router.pads.values() if p.net == 1]
    ns = [p for p in router.pads.values() if p.net == 2]
    assert len(ps) == len(ns) == 3
    assert _net_connected(routes_p, ps)
    assert _net_connected(routes_n, ns)
    stats = router._lattice_negotiation_stats
    assert stats.routed == stats.total, stats


def test_trunk_that_couples_is_not_retried(capsys) -> None:
    """A tap that does not block the corridor: the trunk couples, keeps its
    3-tuple key, and no retry runs (byte-identical to before #6535)."""
    router = _trunk_autorouter(_tap_pads(x=24.0))
    router.route_net(1)
    assert router._lattice_pair_outcomes == {"D": "coupled"}
    assert router._lattice_tap_chain_pairs == {"D"}  # an alternative exists ...
    assert "tap-chain retry" not in capsys.readouterr().out  # ... but is not used
    coupled, _reserved = router._lattice_coupled_connections()
    assert [pc.key for pc in coupled] == [("pair", 1, 2)]


def test_tap_chain_retry_pairs_selection() -> None:
    from types import SimpleNamespace

    from kicad_tools.router.core import _lattice_tap_chain_retry_pairs

    def pc(key, name):
        return SimpleNamespace(key=key, pair_name=name)

    coupled = [
        pc(("pair", 1, 2), "A"),  # trunk, pair-no-path, alternative -> retried
        pc(("pair", 3, 4), "B"),  # trunk, other decline reason
        pc(("pair", 5, 6), "C"),  # trunk coupled
        pc(("pair", 7, 8), "D"),  # trunk, pair-no-path, no alternative
        pc(("pair", 9, 10, 0), "E"),  # a #6378 chain segment: never retried
    ]
    outcomes = {
        ("pair", 1, 2): "uncoupled-fallback:pair-no-path",
        ("pair", 3, 4): "uncoupled-fallback:pair-partner-pad",
        ("pair", 5, 6): "coupled",
        ("pair", 7, 8): "uncoupled-fallback:pair-no-path",
        ("pair", 9, 10, 0): "uncoupled-fallback:pair-no-path",
    }
    got = _lattice_tap_chain_retry_pairs(coupled, outcomes, {"A", "B", "C", "E"})
    assert got == frozenset({"A"})


# ---------------------------------------------------------------------------
# Issue #6550: the retry's keep rule scores every pair, and the discard path
# restores the first negotiation exactly
# ---------------------------------------------------------------------------


def _retry_stub(after_b: str, failed_after: int = 0):
    """Duck-typed ``self``/``pf``/``negotiate`` around ``_lattice_tap_chain_retry``.

    Pair A's trunk declined ``pair-no-path`` and is retried as a two-segment
    tap chain that couples on both segments; pair B (not retried, same
    single-segment chain in both runs) was ``coupled`` first and comes back
    as ``after_b``.  The unrouted count is 0 before and ``failed_after`` after.
    """
    from types import SimpleNamespace

    def pc(key, name):
        return SimpleNamespace(key=key, pair_name=name)

    first_coupled = [pc(("pair", 1, 2), "A"), pc(("pair", 3, 4), "B")]
    retry_coupled = [
        pc(("pair", 1, 2, 0), "A"),
        pc(("pair", 1, 2, 1), "A"),
        pc(("pair", 3, 4), "B"),
    ]
    first_outcomes = {
        ("pair", 1, 2): "uncoupled-fallback:pair-no-path",
        ("pair", 3, 4): "coupled",
    }
    first_failures: dict = {}
    first_stats = SimpleNamespace(deadline_hit=False, name="first")
    pf = SimpleNamespace(
        failure_reasons=first_failures,
        pair_outcomes=first_outcomes,
        class_pad_repair_stats="first-repair",
        _class_pad_growth={},
        _lattice="first-lattice",
        _obstacles="first-obstacles",
    )
    router = SimpleNamespace(
        _lattice_pair_outcomes={"A": "uncoupled-fallback:pair-no-path", "B": "coupled"},
        _lattice_pair_hops="first-hops",
        _lattice_tap_chain_pairs={"A"},
        _lattice_connection_endpoints="first-endpoints",
    )
    router._lattice_netset_connections = lambda retry: (retry_coupled, ["conns"])

    def negotiate(connections, coupled):
        # ``route_netset`` reassigns (never mutates) these two.
        pf.pair_outcomes = {
            ("pair", 1, 2, 0): "coupled",
            ("pair", 1, 2, 1): "coupled",
            ("pair", 3, 4): after_b,
        }
        pf.failure_reasons = dict.fromkeys(range(failed_after), "x")
        pf.class_pad_repair_stats = "retry-repair"
        pf._lattice = "retry-lattice"
        router._lattice_pair_outcomes = {"A": "coupled", "B": "?"}
        router._lattice_pair_hops = "retry-hops"
        router._lattice_connection_endpoints = "retry-endpoints"
        return {"retry": 1}, SimpleNamespace(deadline_hit=False, name="retry")

    first = ({"first": 1}, first_stats, first_coupled)
    return router, pf, negotiate, first


def test_tap_chain_retry_discarded_when_another_pair_loses_coupling(capsys) -> None:
    """Gap 1 of the #6542 review: the retried pair improves (0/1 -> 2/2) and
    the unrouted count holds, but pair B falls from coupled to the uncoupled
    fallback (still "routed", #6373) -- the retry must be discarded."""
    from kicad_tools.router.core import Autorouter

    router, pf, negotiate, first = _retry_stub("uncoupled-fallback:pair-no-path")
    got = Autorouter._lattice_tap_chain_retry(router, pf, negotiate, frozenset({"A"}), first)
    out = capsys.readouterr().out
    assert got == first
    assert got[1].name == "first"
    assert (
        "(A): discarded (coupled segments 0 -> 1, unrouted connections 0 -> 0, "
        "regressed: B 1/1 -> 0/1; issue #6535)" in out
    ), out
    # Every piece of saved state is back.
    assert router._lattice_pair_outcomes == {"A": "uncoupled-fallback:pair-no-path", "B": "coupled"}
    assert router._lattice_pair_hops == "first-hops"
    assert router._lattice_connection_endpoints == "first-endpoints"
    assert pf.pair_outcomes[("pair", 3, 4)] == "coupled"
    assert pf.class_pad_repair_stats == "first-repair"
    assert pf._lattice == "first-lattice"


def test_tap_chain_retry_kept_when_no_other_pair_regresses(capsys) -> None:
    """Control for the test above: same setup, pair B stays coupled -> kept,
    and the log line is the pre-#6550 one (no ``regressed:`` suffix)."""
    from kicad_tools.router.core import Autorouter

    router, pf, negotiate, first = _retry_stub("coupled")
    got = Autorouter._lattice_tap_chain_retry(router, pf, negotiate, frozenset({"A"}), first)
    out = capsys.readouterr().out
    assert got[1].name == "retry"
    assert "(A): kept (coupled segments 0 -> 1, unrouted connections 0 -> 0; issue #6535)" in out, (
        out
    )
    assert pf._lattice == "retry-lattice"


def test_tap_chain_retry_discarded_when_unrouted_count_rises(capsys) -> None:
    from kicad_tools.router.core import Autorouter

    router, pf, negotiate, first = _retry_stub("coupled", failed_after=1)
    got = Autorouter._lattice_tap_chain_retry(router, pf, negotiate, frozenset({"A"}), first)
    out = capsys.readouterr().out
    assert got[1].name == "first"
    # Discarded for another reason: the line carries no ``regressed:`` suffix.
    assert (
        "(A): discarded (coupled segments 0 -> 1, unrouted connections 0 -> 1; issue #6535)" in out
    ), out


def test_tap_chain_retry_keeps_rule() -> None:
    from kicad_tools.router.core import _lattice_pair_coupling, _lattice_tap_chain_retry_keeps

    retry = frozenset({"A"})
    first = {"A": (0, 1), "B": (1, 1), "C": (1, 2)}
    # A one-total-score rule (0+1+0.5 -> 1+0+1) would keep this; B regressed.
    after = {"A": (2, 2), "B": (0, 1), "C": (2, 2)}
    assert _lattice_tap_chain_retry_keeps(first, after, retry, 0, 0, False) == (
        False,
        ["B 1/1 -> 0/1"],
    )
    ok = {"A": (1, 2), "B": (1, 1), "C": (1, 2)}
    assert _lattice_tap_chain_retry_keeps(first, ok, retry, 0, 0, False) == (True, [])
    # Retried pairs must strictly improve (exact fractions: 1/2 == 2/4).
    assert _lattice_tap_chain_retry_keeps({"A": (1, 2)}, {"A": (2, 4)}, retry, 0, 0, False) == (
        False,
        [],
    )
    assert _lattice_tap_chain_retry_keeps(first, ok, retry, 0, 1, False) == (False, [])
    assert _lattice_tap_chain_retry_keeps(first, ok, retry, 0, 0, True) == (False, [])

    from types import SimpleNamespace

    coupled = [
        SimpleNamespace(key=("pair", 1, 2, 0), pair_name="A"),
        SimpleNamespace(key=("pair", 1, 2, 1), pair_name="A"),
        SimpleNamespace(key=("pair", 3, 4), pair_name="B"),
    ]
    outcomes = {("pair", 1, 2, 0): "coupled", ("pair", 3, 4): "uncoupled-fallback:pair-no-path"}
    assert _lattice_pair_coupling(coupled, outcomes) == {"A": (1, 2), "B": (0, 1)}


def _route_geometry(routes: list) -> list:
    return [
        (
            [(s.x1, s.y1, s.x2, s.y2, s.layer, s.width) for s in r.segments],
            [(v.x, v.y, v.layers, v.drill, v.diameter) for v in r.vias],
        )
        for r in routes
    ]


def test_discarded_tap_chain_retry_equals_a_run_without_retry(capsys, monkeypatch) -> None:
    """Gap 2 of the #6542 review: forcing the retry's discard on the tap
    fixture leaves exactly the state a run with no retry at all leaves."""
    import dataclasses

    import kicad_tools.router.core as core

    pads = _tap_pads() + _tap_walls()

    def run():
        router = _trunk_autorouter(pads)
        routes = {net: _route_geometry(router.route_net(net)) for net in (1, 2)}
        return router, routes

    monkeypatch.setattr(core, "_lattice_tap_chain_retry_pairs", lambda *a: frozenset())
    base, base_routes = run()
    assert "tap-chain retry" not in capsys.readouterr().out
    monkeypatch.undo()

    monkeypatch.setattr(core, "_lattice_tap_chain_retry_keeps", lambda *a: (False, []))
    forced, forced_routes = run()
    out = capsys.readouterr().out
    assert "tap-chain retry for 1 pair(s) whose trunk declined pair-no-path (D): discarded (" in out

    assert forced_routes == base_routes
    for attr in (
        "_lattice_pair_outcomes",
        "_lattice_pair_segment_outcomes",
        "_lattice_failure_reasons",
        "_lattice_connection_endpoints",
        "_lattice_pair_hops",
        "_lattice_tap_chain_pairs",
    ):
        assert getattr(forced, attr) == getattr(base, attr), attr
    # Every stats field but the wall clock (two separate runs never time alike).
    a, b = forced._lattice_negotiation_stats, base._lattice_negotiation_stats
    for f in dataclasses.fields(a):
        if f.name != "elapsed_s":
            assert getattr(a, f.name) == getattr(b, f.name), f.name


# ---------------------------------------------------------------------------
# Issue #6698: launch from the row facing the chain + generated row ties
# ---------------------------------------------------------------------------

# A USB-C receptacle's D+/D- rows (board 03 J1, GCT USB4085): 0.7 mm round THT
# pads, 0.85 mm pitch, rows 1.35 mm apart.  The A row faces the ESD part U2;
# the B row sits behind it, its D+/D- order mirrored (B6 behind A7).
_USBC_A_X, _USBC_B_X = 3.0, 1.65
_USBC_LO, _USBC_HI = 5.575, 6.425


def _usbc(x: float, y: float, net: int, name: str, pin: str) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=0.7,
        height=0.7,
        net=net,
        net_name=name,
        layer=Layer.F_CU,
        ref="J1",
        pin=pin,
        through_hole=True,
        drill=0.4,
        shape="circle",
    )


def _usbc_pads(*, smd: bool = False) -> dict[str, Pad]:
    pads = {
        "A6": _usbc(_USBC_A_X, _USBC_LO, 1, "D+", "A6"),
        "A7": _usbc(_USBC_A_X, _USBC_HI, 2, "D-", "A7"),
        "B6": _usbc(_USBC_B_X, _USBC_HI, 1, "D+", "B6"),
        "B7": _usbc(_USBC_B_X, _USBC_LO, 2, "D-", "B7"),
    }
    if smd:
        from dataclasses import replace

        pads = {k: replace(p, through_hole=False, drill=0.0) for k, p in pads.items()}
    return pads


def _usbc_chain_pads(*, smd: bool = False) -> dict[str, Pad]:
    pads = {k: v for k, v in _chain_pads().items() if not k.startswith("J1.")}
    return {**{f"J1.{k}": v for k, v in _usbc_pads(smd=smd).items()}, **pads}


def test_choose_pair_chain_launches_from_the_row_facing_the_chain() -> None:
    from kicad_tools.router.lattice.coupled import choose_pair_chain

    pads = _usbc_chain_pads()
    ps = [pads[k] for k in ("J1.B6", "J1.A6", "U2.3", "U2.4", "U1.P")]
    ns = [pads[k] for k in ("J1.B7", "J1.A7", "U2.1", "U2.6", "U1.N")]
    # The farthest-apart end is still the walled-in B row ...
    ends = choose_pair_endpoints(ps, ns)
    assert ends is not None and ends[0] == (0, 0)
    chain = choose_pair_chain(ps, ns)
    assert chain is not None
    # ... but segment 0 now starts at the A row; hops and the far end are unchanged.
    assert chain.segments == [((1, 1), (2, 2)), ((3, 3), (4, 4))]
    assert chain.hops == [((2, 2), (3, 3))]


def test_choose_pair_chain_end_repick_needs_an_interior_part() -> None:
    from kicad_tools.router.lattice.coupled import choose_pair_chain

    # Dual-row end, no interior part: the single farthest-apart trunk (B row).
    usbc = _usbc_pads()
    far_p = _pad(27.0, 5.5, 1, "D+", ref="U1")
    far_n = _pad(27.0, 6.5, 2, "D-", ref="U1")
    ps = [usbc["B6"], usbc["A6"], far_p]
    ns = [usbc["B7"], usbc["A7"], far_n]
    chain = choose_pair_chain(ps, ns)
    assert chain is not None
    assert chain.segments == [choose_pair_endpoints(ps, ns)] and chain.hops == []


def _tie_kwargs(**over):
    kw = {"width": 0.30, "clearance": 0.15, "min_width": 0.1016}
    kw.update(over)
    return kw


def _plan_both(pads: dict[str, Pad], layers, **over):
    from kicad_tools.router.lattice.coupled import plan_equivalent_pin_tie

    foreign = list(pads.values())
    tie_p = plan_equivalent_pin_tie(pads["B6"], pads["A6"], foreign, layers, **_tie_kwargs(**over))
    accepted = [tie_p] if tie_p is not None else []
    tie_n = plan_equivalent_pin_tie(
        pads["B7"], pads["A7"], foreign, layers, accepted=accepted, **_tie_kwargs(**over)
    )
    return tie_p, tie_n


def _tie_pad_clearance(tie, pad: Pad) -> float:
    from shapely.geometry import LineString, Point

    copper = LineString(tie.points).buffer(tie.width / 2.0, quad_segs=64)
    return copper.distance(Point(pad.x, pad.y).buffer(pad.width / 2.0, quad_segs=64))


def test_equivalent_pin_ties_neck_down_onto_different_layers() -> None:
    pads = _usbc_pads()
    tie_p, tie_n = _plan_both(pads, [0, 1])
    assert tie_p is not None and tie_n is not None
    for tie in (tie_p, tie_n):
        assert tie.length == pytest.approx(1.70, abs=0.01)
        assert tie.width == pytest.approx(0.20)
        assert tie.class_width == pytest.approx(0.30)
        assert all(_is_octilinear(a, b) for a, b in zip(tie.points, tie.points[1:], strict=False))
    # The two ties cross, so they land on different layers.
    assert tie_p.layer != tie_n.layer
    # Exact copper clears every other-net pad by the class clearance.
    for tie in (tie_p, tie_n):
        for pad in pads.values():
            if pad.net != tie.net:
                assert _tie_pad_clearance(tie, pad) >= 0.15 - 1e-6


def test_equivalent_pin_tie_single_layer_connector_yields_one_tie() -> None:
    tie_p, tie_n = _plan_both(_usbc_pads(smd=True), [0])
    assert tie_p is not None and tie_n is None


def test_equivalent_pin_tie_declines_when_no_fab_width_fits() -> None:
    tie_p, tie_n = _plan_both(_usbc_pads(), [0, 1], clearance=0.25)
    assert tie_p is None and tie_n is None
    # Without a fab floor there is no neck-down: 0.30 mm never fits.
    assert _plan_both(_usbc_pads(), [0, 1], min_width=None) == (None, None)


def test_equivalent_pin_tie_roundrect_falls_back_to_the_pad_box() -> None:
    from dataclasses import replace

    from kicad_tools.router.lattice.coupled import plan_equivalent_pin_tie

    pads = _usbc_pads()
    # A roundrect foreign pad (``_pad_waiver_polygon`` returns None for it) is
    # its full bounding box -- never "no obstacle".  A 0.3 mm probe 0.38 mm
    # off the D+ tie's diagonal leaves room for a necked tie as a circle, but
    # its box corner points at the leg and leaves none.
    off = 0.38 / math.sqrt(2.0)
    mid = ((_USBC_B_X + _USBC_B_X + 0.85) / 2.0, (_USBC_HI + _USBC_LO) / 2.0)
    probe = replace(
        pads["B7"], x=mid[0] + off, y=mid[1] + off, width=0.3, height=0.3, net=9, net_name="X"
    )
    as_circle = plan_equivalent_pin_tie(
        pads["B6"], pads["A6"], [*pads.values(), probe], [0], **_tie_kwargs()
    )
    assert as_circle is not None, "the round probe leaves room"
    boxed = replace(probe, shape="roundrect")
    assert (
        plan_equivalent_pin_tie(
            pads["B6"], pads["A6"], [*pads.values(), boxed], [0], **_tie_kwargs()
        )
        is None
    )


def _usbc_autorouter(*, smd: bool = False) -> object:
    from kicad_tools.router.core import Autorouter

    # Board 03 under the protocol: 0.15 mm board clearance, jlcpcb 4-layer
    # 1 oz fab floor (``rules.min_trace_width_floor``, #4700).
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb")
    rules.min_trace_width_floor = 0.1016
    router = Autorouter(
        30, 12, strategy="lattice", rules=rules, net_class_map={"D+": _HS, "D-": _HS}
    )
    for key, pad in _usbc_chain_pads(smd=smd).items():
        router.pads[(pad.ref, key)] = pad
        router.nets.setdefault(pad.net, []).append((pad.ref, key))
        router.net_names[pad.net] = pad.net_name
    return router


def test_core_ties_the_far_usbc_row_as_fixed_copper(capsys, monkeypatch) -> None:
    router = _usbc_autorouter()
    pf = router._ensure_lattice_pathfinder()
    seen: dict = {}
    real = pf.route_netset

    def spy(conns, **kw):
        seen.setdefault("fixed", kw.get("fixed_copper"))
        seen.setdefault("conns", list(conns))
        return real(conns, **kw)

    monkeypatch.setattr(pf, "route_netset", spy)
    routes_p = router.route_net(1)
    routes_n = router.route_net(2)
    out = capsys.readouterr().out
    assert "Diff-pair tie D+ J1.B6->J1.A6" in out and "Diff-pair tie D- J1.B7->J1.A7" in out
    assert "D[0] J1.A6->U2.3" in out
    ties = router._lattice_pair_ties
    assert [t[0] for t in ties] == [(1, "tie", 0), (2, "tie", 1)]
    # No single-ended connection for a tied extra.
    assert not any(isinstance(k, tuple) and k[1:] == (0,) for k, *_ in seen["conns"])
    tie_routes = [route for _k, route, _e in ties]
    assert all(any(r is f for f in seen["fixed"]) for r in tie_routes)
    assert all(any(r is t for t in routes_p + routes_n) for r in tie_routes)
    pads = _usbc_chain_pads()
    assert _net_connected(routes_p, [pads[k] for k in ("J1.A6", "J1.B6", "U2.3", "U2.4", "U1.P")])
    assert _net_connected(routes_n, [pads[k] for k in ("J1.A7", "J1.B7", "U2.1", "U2.6", "U1.N")])
    assert router._lattice_connection_endpoints[(1, "tie", 0)] == ("J1.B6", "J1.A6")


def _todays_connections(router, monkeypatch) -> list:
    """The connection list with the #6698 tie pass switched off."""
    with monkeypatch.context() as m:
        m.setattr(router, "_lattice_plan_pair_ties", lambda reserved: set())
        _c, conns = router._lattice_netset_connections()
    return conns


def test_core_without_a_tie_keeps_todays_connections(monkeypatch) -> None:
    import kicad_tools.router.lattice.coupled as coupled_mod

    router = _usbc_autorouter()
    router._ensure_lattice_pathfinder()
    today = _todays_connections(router, monkeypatch)
    monkeypatch.setattr(coupled_mod, "plan_equivalent_pin_tie", lambda *a, **k: None)
    _c, declined = router._lattice_netset_connections()
    assert router._lattice_pair_ties == []
    assert declined == today
    assert any(k == (1, 0) for k, *_ in today) and any(k == (2, 0) for k, *_ in today)


def test_core_ties_a_pair_all_or_nothing(capsys, monkeypatch) -> None:
    # A single-layer (SMD) receptacle has room for only one of the two
    # crossing ties: the pair keeps today's connections for BOTH extras
    # rather than attaching D+ and D- by different mechanisms (board 06rf).
    router = _usbc_autorouter(smd=True)
    router._ensure_lattice_pathfinder()
    today = _todays_connections(router, monkeypatch)
    _c, conns = router._lattice_netset_connections()
    assert router._lattice_pair_ties == []
    assert conns == today
    assert "Diff-pair ties D+/D-: not used (J1.B7->J1.A7 has no clearing tie)" in (
        capsys.readouterr().out
    )


def test_core_ties_neither_net_when_only_one_has_a_candidate(capsys, monkeypatch) -> None:
    # Issue #6697: when only D+ has an eligible far-row extra (D-'s B7 shares
    # no routing layer with its anchor here), tying D+ alone would attach P
    # and N by different mechanisms -- the half-tie that made 06rf USB2_D's
    # uncoupled length worse.  Neither net is tied.
    router = _usbc_autorouter()
    pf = router._ensure_lattice_pathfinder()
    real = pf._pad_layer_indices

    def layers(pad):
        if pad.ref == "J1" and pad.pin == "B7":
            return []
        return real(pad)

    monkeypatch.setattr(pf, "_pad_layer_indices", layers)
    today = _todays_connections(router, monkeypatch)
    _c, conns = router._lattice_netset_connections()
    assert router._lattice_pair_ties == []
    assert conns == today
    assert any(k == (1, 0) for k, *_ in conns) and any(k == (2, 0) for k, *_ in conns)
    assert "Diff-pair ties D+/D-: not used (partner net has no tie candidate)" in (
        capsys.readouterr().out
    )


def test_core_declines_ties_through_a_fixed_fill(capsys) -> None:
    # Preserved zone fills / degraded custom-pad copper are in neither
    # ``pf.pads`` nor ``existing_routes``: a fill across the connector must
    # veto every dogleg, and the all-or-nothing rule then drops both ties.
    from shapely.geometry import box

    from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles

    router = _usbc_autorouter()
    copper = box(_USBC_B_X - 0.2, _USBC_LO - 0.5, _USBC_A_X + 0.2, _USBC_HI + 0.5)
    router.grid.install_fixed_fills(
        FixedFillObstacles(tuple(FixedFill("GND", 99, layer, 0.2, copper) for layer in (0, 1)))
    )
    router._lattice_netset_connections()
    assert router._lattice_pair_ties == []
    assert "Diff-pair ties D+/D-: not used" in capsys.readouterr().out


def test_core_declines_ties_through_a_preserved_other_net_via(capsys) -> None:
    from kicad_tools.router.primitives import Route, Via

    router = _usbc_autorouter()
    mid = ((_USBC_A_X + _USBC_B_X) / 2.0, (_USBC_LO + _USBC_HI) / 2.0)
    via = Via(x=mid[0], y=mid[1], drill=0.6, diameter=1.2, layers=(Layer.F_CU, Layer.B_CU), net=99)
    router.existing_routes.append(Route(net=99, net_name="GND", segments=[], vias=[via]))
    router._ensure_lattice_pathfinder()
    router._lattice_netset_connections()
    assert router._lattice_pair_ties == []
    assert "Diff-pair ties D+/D-: not used" in capsys.readouterr().out
