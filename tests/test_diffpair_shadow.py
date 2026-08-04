"""Tests for issue #3508: board 06 coupled-convergence machinery.

Covers the units this issue added or changed in
``kicad_tools.router.diffpair_routing``:

1. ``CoupledPathfinder._is_cell_blocked`` -- own-net passability.  Pad
   metal and clearance-halo cells carry ``is_obstacle = True`` on the
   grid (the #2915/#2940 negotiated-mode loophole guard); the coupled
   pathfinder must NOT treat that as a block for the pad's own net,
   matching the per-net pathfinder's ``cell.net != routing_net``
   convention.  Before the fix every pad was unreachable for its own
   coupled route and convergence was 0/9 at ANY budget.

2. ``CoupledPathfinder`` weighted A* (``heuristic_weight``) -- stored,
   clamped, and applied to ``f = g + w * h``.

3. ``create_serpentine`` partner-aware mode -- one-sided bulges (never
   toward the partner) and the triangular length arithmetic actually
   delivering the requested extra length.

4. ``DiffPairRouter`` geometry helpers -- ``_point_segment_distance``
   and ``_min_distance_to_partner``.

5. Mid-route asymmetric moves (issue #3508 relaxation of the #2490
   approach-phase-only restriction): asymmetric P-advance/N-advance
   moves are generated OUTSIDE the approach radius with a tight
   tolerance.

6. The trail PROXIMITY guard: an advancing trace may not land within
   ``min_spacing_cells`` (Euclidean, same layer) of the partner's
   accumulated trail -- the exact-cell guard alone admitted 1-cell
   passes (0.05 mm centerline distance = copper overlap).
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.diffpair_routing import (
    CoupledPathfinder,
    CoupledState,
    GridPos,
    create_serpentine,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def _make_pathfinder(**kwargs) -> CoupledPathfinder:
    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    defaults = {"grid": grid, "rules": rules, "target_spacing_cells": 4}
    defaults.update(kwargs)
    return CoupledPathfinder(**defaults)


# ---------------------------------------------------------------------------
# 1. own-net passability
# ---------------------------------------------------------------------------


def test_own_net_obstacle_cell_is_passable():
    """A blocked cell carrying the routing net is passable (pad metal/halo)."""
    pf = _make_pathfinder()
    cell = pf.grid.grid[0][10][10]
    cell.blocked = True
    cell.is_obstacle = True
    cell.net = 42
    assert pf._is_cell_blocked(10, 10, 0, 42) is False


def test_foreign_net_blocked_cell_is_blocked():
    pf = _make_pathfinder()
    cell = pf.grid.grid[0][10][10]
    cell.blocked = True
    cell.net = 7
    assert pf._is_cell_blocked(10, 10, 0, 42) is True


def test_net_zero_obstacle_blocked_for_signal_nets():
    """True obstacles (keepouts, board edge) carry net 0 and stay blocked."""
    pf = _make_pathfinder()
    cell = pf.grid.grid[0][10][10]
    cell.blocked = True
    cell.is_obstacle = True
    # cell.net stays 0
    assert pf._is_cell_blocked(10, 10, 0, 42) is True


# ---------------------------------------------------------------------------
# 2. weighted A*
# ---------------------------------------------------------------------------


def test_heuristic_weight_default_is_classic():
    pf = _make_pathfinder()
    assert pf.heuristic_weight == 1.0


def test_heuristic_weight_stored():
    pf = _make_pathfinder(heuristic_weight=1.5)
    assert pf.heuristic_weight == 1.5


def test_heuristic_weight_clamped_to_at_least_one():
    """Sub-1 weights would break A* termination guarantees -- clamped."""
    pf = _make_pathfinder(heuristic_weight=0.25)
    assert pf.heuristic_weight == 1.0


# ---------------------------------------------------------------------------
# 3. partner-aware serpentine
# ---------------------------------------------------------------------------


def _straight_route(net: int, name: str, y: float, length: float = 10.0) -> Route:
    r = Route(net=net, net_name=name)
    r.segments.append(
        Segment(
            x1=1.0,
            y1=y,
            x2=1.0 + length,
            y2=y,
            width=0.2,
            layer=Layer.F_CU,
            net=net,
            net_name=name,
        )
    )
    return r


def _route_length(route: Route) -> float:
    return sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)


def test_partner_aware_serpentine_is_one_sided():
    """With a partner constraint, no bulge may cross toward the partner.

    Partner runs at y=10.35 above the target trace at y=10.0; every
    serpentine point must therefore stay at y <= 10.0 (bulge downward,
    away from the partner).
    """
    target = _straight_route(1, "P", 10.0)
    partner = _straight_route(2, "N", 10.35)
    ok = create_serpentine(
        target,
        length_to_add=2.0,
        partner_route=partner,
        intra_pair_clearance_mm=0.1,
    )
    assert ok, "partner-aware serpentine must succeed on an open straight run"
    for seg in target.segments:
        assert seg.y1 <= 10.0 + 1e-9 and seg.y2 <= 10.0 + 1e-9, (
            f"bulge crossed toward the partner: ({seg.x1},{seg.y1})->({seg.x2},{seg.y2})"
        )


def test_partner_aware_serpentine_adds_requested_length():
    """The triangular-bulge arithmetic must deliver ~length_to_add.

    The legacy square-bulge formula under-delivered by up to 10x
    (issue #3508); assert at least 70% of the request is realised.
    """
    target = _straight_route(1, "P", 10.0)
    partner = _straight_route(2, "N", 10.35)
    before = _route_length(target)
    requested = 2.5
    ok = create_serpentine(
        target,
        length_to_add=requested,
        partner_route=partner,
        intra_pair_clearance_mm=0.1,
    )
    assert ok
    added = _route_length(target) - before
    assert added >= 0.7 * requested, (
        f"serpentine added only {added:.3f}mm of the requested {requested:.3f}mm"
    )


def test_legacy_serpentine_still_alternates():
    """Without a partner constraint the legacy alternating wave persists."""
    target = _straight_route(1, "P", 10.0)
    ok = create_serpentine(target, length_to_add=2.0)
    assert ok
    above = any(s.y1 > 10.0 + 1e-9 or s.y2 > 10.0 + 1e-9 for s in target.segments)
    below = any(s.y1 < 10.0 - 1e-9 or s.y2 < 10.0 - 1e-9 for s in target.segments)
    assert above and below, "legacy serpentine should alternate sides"


# ---------------------------------------------------------------------------
# 4. geometry helpers
# ---------------------------------------------------------------------------


def _seg(x1, y1, x2, y2, layer=Layer.F_CU) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer, net=1, net_name="P")


def test_point_segment_distance_perpendicular():
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    d = DiffPairRouter._point_segment_distance(5.0, 1.0, _seg(0, 0, 10, 0))
    assert d == pytest.approx(1.0)


def test_point_segment_distance_beyond_endpoint():
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    d = DiffPairRouter._point_segment_distance(12.0, 0.0, _seg(0, 0, 10, 0))
    assert d == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 4b. inside-bend self-overlap mitigation (#4460)
# ---------------------------------------------------------------------------
#
# ``_offset_corner_join`` decides how two consecutive fixed-side parallel
# offset segments meet.  At a sharp INSIDE (concave) bend the offset polyline
# folds over itself; the old straight-bevel fallback then dove a fold segment
# clear across the guide centerline (a full-trace-width local self-cross).
# These tests pin the corrected behaviour: concave corners mitre to the
# offset-line intersection (staying on the offset side, never crossing the
# guide) while convex spikes stay bounded.


def _offset_endpoints(p0, p1, side, d):
    """Offset a guide segment ``p0->p1`` by ``d`` on the given lateral side.

    Mirrors the offset formula in ``_shadow_route_pair`` (normal is
    ``(-u_y, u_x) * side``), so the corner-join inputs match what the
    constructor actually feeds the helper.
    """
    ux, uy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(ux, uy)
    ux, uy = ux / length, uy / length
    nx, ny = -uy * side, ux * side
    return (p0[0] + d * nx, p0[1] + d * ny), (p1[0] + d * nx, p1[1] + d * ny)


def test_offset_corner_join_sharp_concave_mitres_without_crossing_guide():
    """A sharp inside bend mitres to the intersection, staying off the guide.

    Guide reverses direction near 180 deg (V0->V East, then V->V2 back to the
    upper-left).  With the old ``2*d + gap`` spike bound this concave corner
    exceeded the bound and fell back to a straight bevel from ``prev_pt`` to
    ``a`` -- a chord that crosses the guide centerline (y == 0).  The fix
    mitres instead; the apex must stay strictly on the offset side (y > 0).
    """
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    d = 0.25
    v0, v, v2 = (-10.0, 0.0), (0.0, 0.0), (-8.0, 1.0)
    pseg_start, pseg_end = _offset_endpoints(v0, v, +1.0, d)
    a, b = _offset_endpoints(v, v2, +1.0, d)

    mode, mx = DiffPairRouter._offset_corner_join(
        pseg_start, pseg_end, a, b, side=+1.0, d=d, resolution=0.05
    )

    assert mode == "miter"
    assert mx is not None
    # The offset side is +y here; a correct de-fold apex never crosses to the
    # far (guide/partner) side.
    assert mx[1] > 0.0
    # The straight bevel the fix REPLACES would have crossed the guide: the
    # chord prev_pt -> a spans from +y to -y.
    assert pseg_end[1] > 0.0 > a[1]


def test_offset_corner_join_sharp_convex_stays_bounded_as_bevel():
    """A sharp OUTSIDE bend keeps the spike bound and falls back to a bevel.

    Same near-reversal geometry on the opposite lateral side is convex; its
    miter spike is unbounded, so the helper must NOT emit it (that would be a
    long copper spur).  It degrades to the bevel, which stays on the offset
    side for a convex corner.
    """
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    d = 0.25
    v0, v, v2 = (-10.0, 0.0), (0.0, 0.0), (-8.0, 1.0)
    pseg_start, pseg_end = _offset_endpoints(v0, v, -1.0, d)
    a, b = _offset_endpoints(v, v2, -1.0, d)

    mode, mx = DiffPairRouter._offset_corner_join(
        pseg_start, pseg_end, a, b, side=-1.0, d=d, resolution=0.05
    )

    assert mode == "bevel"
    assert mx is None


def test_offset_corner_join_gentle_convex_mitres():
    """A 90 deg outside corner is within the spike bound -> mitre (unchanged)."""
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    d = 0.25
    # East then North: a right-angle turn.  Offset side +1 (left of travel)
    # is the OUTSIDE of this left turn's partner... pick the convex side.
    v0, v, v2 = (-10.0, 0.0), (0.0, 0.0), (0.0, 10.0)
    pseg_start, pseg_end = _offset_endpoints(v0, v, -1.0, d)
    a, b = _offset_endpoints(v, v2, -1.0, d)

    mode, mx = DiffPairRouter._offset_corner_join(
        pseg_start, pseg_end, a, b, side=-1.0, d=d, resolution=0.05
    )

    assert mode == "miter"
    assert mx is not None


def test_offset_corner_join_collinear_needs_no_join():
    """Collinear continuation (endpoints already meet) returns ``none``."""
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    d = 0.25
    v0, v, v2 = (-10.0, 0.0), (0.0, 0.0), (10.0, 0.0)
    pseg_start, pseg_end = _offset_endpoints(v0, v, +1.0, d)
    a, b = _offset_endpoints(v, v2, +1.0, d)

    mode, mx = DiffPairRouter._offset_corner_join(
        pseg_start, pseg_end, a, b, side=+1.0, d=d, resolution=0.05
    )

    assert mode == "none"
    assert mx is None


# ---------------------------------------------------------------------------
# 4b-bis. sub-cell offset steps must be JOINED, not dropped (#4462)
# ---------------------------------------------------------------------------
#
# ``_shadow_select_gap`` (#3990) picks the parallel-offset gap PER SEGMENT from
# an impedance-band ladder.  Two consecutive COLLINEAR guide segments that pick
# different rungs offset to endpoints separated perpendicular to travel by the
# rung difference -- on board-06's 0.05 mm grid, 0.024 mm.  ``_offset_corner_join``
# used to answer ``"none"`` ("the endpoints already meet") for any gap under half
# a grid cell, and the caller then started the next segment 0.024 mm away from
# where the previous one ended: a literal break in the emitted polyline.
#
# The break is invisible to the clearance/census gates but fatal downstream --
# ``validate_net_connectivity`` unions endpoints snapped to a 0.01 mm lattice,
# so the net reads "1/2 pads reached" and the #3540 transactional strand guard
# rips the WHOLE coupled pair.  Measured on board-06: MIPI_CLK, MIPI_D0 and
# PCIE_RX, all three already length-matched to under 0.45 mm by #4553.


def test_offset_corner_join_subcell_step_is_bevelled_not_dropped():
    """A sub-half-cell perpendicular step is real copper, not a coincidence."""
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    # Collinear eastward guide; the second segment offsets 0.024 mm further out
    # (a different rung of the variable-gap ladder).
    pseg_start, pseg_end = (0.0, 0.250), (5.0, 0.250)
    a, b = (5.0, 0.274), (10.0, 0.274)

    mode, mx = DiffPairRouter._offset_corner_join(
        pseg_start, pseg_end, a, b, side=+1.0, d=0.25, resolution=0.05
    )

    # 0.024 < resolution/2 == 0.025 -- the exact board-06 signature.
    assert math.hypot(a[0] - pseg_end[0], a[1] - pseg_end[1]) < 0.05 / 2
    assert mode == "bevel"
    assert mx is None


def test_offset_corner_join_none_only_within_a_serialization_quantum():
    """``none`` is reserved for endpoints that serialize to the same coordinate."""
    from kicad_tools.router.diffpair_routing import (
        _OFFSET_JOIN_COINCIDENT_MM,
        DiffPairRouter,
    )

    pseg_start, pseg_end = (0.0, 0.25), (5.0, 0.25)

    def _mode(dy: float) -> str:
        a = (5.0, 0.25 + dy)
        b = (10.0, 0.25 + dy)
        return DiffPairRouter._offset_corner_join(
            pseg_start, pseg_end, a, b, side=+1.0, d=0.25, resolution=0.05
        )[0]

    assert _mode(0.0) == "none"
    assert _mode(_OFFSET_JOIN_COINCIDENT_MM / 2.0) == "none"
    # One order of magnitude above the quantum is already real copper.
    assert _mode(_OFFSET_JOIN_COINCIDENT_MM * 10.0) == "bevel"


def _chain_router_and_pathfinder(resolution: float = 0.05):
    from kicad_tools.router.core import Autorouter

    rules = DesignRules()
    rules.grid_resolution = resolution
    router = Autorouter(width=20.0, height=20.0, rules=rules)
    pf = CoupledPathfinder(grid=router.grid, rules=rules, target_spacing_cells=4)
    return router._diffpair, pf


def _chain_route(points, net: int = 7) -> Route:
    route = Route(net=net, net_name="MIPI_CLK-")
    for k in range(len(points) - 1):
        (x1, y1), (x2, y2) = points[k], points[k + 1]
        route.segments.append(
            Segment(
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                width=0.1,
                layer=Layer.F_CU,
                net=net,
                net_name="MIPI_CLK-",
            )
        )
    return route


def test_close_shadow_chain_is_a_noop_on_a_connected_polyline():
    dpr, pf = _chain_router_and_pathfinder()
    route = _chain_route([(2.0, 2.0), (4.0, 2.0), (6.0, 4.0), (8.0, 4.0)])
    before = [(s.x1, s.y1, s.x2, s.y2) for s in route.segments]

    assert dpr._close_shadow_chain(route, pf) == 0
    assert [(s.x1, s.y1, s.x2, s.y2) for s in route.segments] == before


def test_close_shadow_chain_inserts_a_connector_for_a_subcell_break():
    """The board-06 signature: a 0.024 mm break is closed with real copper."""
    dpr, pf = _chain_router_and_pathfinder()
    route = _chain_route([(2.0, 2.0), (4.0, 2.0)])
    route.segments.append(
        Segment(
            x1=4.0,
            y1=2.024,
            x2=6.0,
            y2=2.024,
            width=0.1,
            layer=Layer.F_CU,
            net=7,
            net_name="MIPI_CLK-",
        )
    )

    assert dpr._close_shadow_chain(route, pf) == 1
    pts = [(s.x1, s.y1) for s in route.segments] + [(route.segments[-1].x2, route.segments[-1].y2)]
    for k in range(len(route.segments) - 1):
        a, b = route.segments[k], route.segments[k + 1]
        assert math.hypot(b.x1 - a.x2, b.y1 - a.y2) == 0.0
    assert pts[0] == (2.0, 2.0)
    assert pts[-1] == (6.0, 2.024)


def test_close_shadow_chain_snaps_a_sub_quantum_gap_to_bit_equality():
    dpr, pf = _chain_router_and_pathfinder()
    route = _chain_route([(2.0, 2.0), (4.0, 2.0)])
    route.segments.append(
        Segment(
            x1=4.0,
            y1=2.00002,
            x2=6.0,
            y2=2.0,
            width=0.1,
            layer=Layer.F_CU,
            net=7,
            net_name="MIPI_CLK-",
        )
    )

    assert dpr._close_shadow_chain(route, pf) == 1
    # Snapped, not connector-ed: still two segments, endpoints bit-identical.
    assert len(route.segments) == 2
    assert route.segments[1].y1 == route.segments[0].y2


def test_close_shadow_chain_leaves_a_large_gap_alone():
    """Beyond one grid cell the strand guard, not a blind connector, decides."""
    dpr, pf = _chain_router_and_pathfinder()
    route = _chain_route([(2.0, 2.0), (4.0, 2.0)])
    route.segments.append(
        Segment(
            x1=4.0, y1=3.0, x2=6.0, y2=3.0, width=0.1, layer=Layer.F_CU, net=7, net_name="MIPI_CLK-"
        )
    )

    assert dpr._close_shadow_chain(route, pf) == 0
    assert len(route.segments) == 2


def test_subcell_break_strands_a_net_and_closing_it_restores_connectivity():
    """Pins the whole bug->symptom chain the #3540 rollback reacted to."""
    from kicad_tools.router.observability import validate_net_connectivity
    from kicad_tools.router.primitives import Pad

    dpr, pf = _chain_router_and_pathfinder()
    route = _chain_route([(2.0, 2.0), (4.0, 2.0)])
    route.segments.append(
        Segment(
            x1=4.0,
            y1=2.024,
            x2=6.0,
            y2=2.024,
            width=0.1,
            layer=Layer.F_CU,
            net=7,
            net_name="MIPI_CLK-",
        )
    )
    pads = [
        Pad(x=2.0, y=2.0, width=0.3, height=0.3, layer=Layer.F_CU, net=7, net_name="MIPI_CLK-"),
        Pad(x=6.0, y=2.024, width=0.3, height=0.3, layer=Layer.F_CU, net=7, net_name="MIPI_CLK-"),
    ]

    broken = validate_net_connectivity([route], {7: pads})[7]
    assert broken["connected"] is False
    assert broken["connected_pads"] == 1

    dpr._close_shadow_chain(route, pf)

    fixed = validate_net_connectivity([route], {7: pads})[7]
    assert fixed["connected"] is True
    assert fixed["connected_pads"] == 2


# ---------------------------------------------------------------------------
# 4c. shadow-aware guide re-routing: non-adjacent self-approach detection (#4460)
# ---------------------------------------------------------------------------
#
# ``_offset_corner_join`` (approach 1, #4490) de-folds ADJACENT concave
# corners.  It structurally cannot separate guide legs ~20 segments apart that
# loop back within the parallel-offset clearance (USB3_RX1's arc ~2.1mm vs
# ~3.0mm in board 06).  ``_guide_self_approaches`` (approach 2, #4460) finds
# those non-adjacent loop-backs so the caller can re-route the guide away from
# itself.  These tests pin the detector: it FIRES on a hairpin loop-back,
# stays SILENT on a straight/gentle guide, and IGNORES adjacent corners.


def _diffpair_router(resolution: float = 0.1):
    from kicad_tools.router.core import Autorouter

    rules = DesignRules()
    rules.grid_resolution = resolution
    router = Autorouter(width=20.0, height=10.0, rules=rules)
    return router._diffpair


def _gseg(x1, y1, x2, y2, layer=Layer.F_CU) -> Segment:
    """A guide segment with an explicit layer (module-scoped ``_seg`` is
    redefined later in this file without a ``layer`` kwarg)."""
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer, net=1, net_name="P")


def test_guide_self_approaches_detects_hairpin_loopback():
    """A guide that doubles back within the offset clearance is flagged."""
    dpr = _diffpair_router()
    # East 5mm, up 0.3mm, back West 5mm at y=0.3: the two long legs run
    # parallel 0.3mm apart -- far in arc length, close in space.
    route = Route(net=1, net_name="USB3_RX1")
    route.segments.append(_gseg(0.0, 0.0, 5.0, 0.0))
    route.segments.append(_gseg(5.0, 0.0, 5.0, 0.3))
    route.segments.append(_gseg(5.0, 0.3, 0.0, 0.3))
    approaches = dpr._guide_self_approaches(route, spacing_cells=3, proximity=0.5)
    assert len(approaches) >= 1
    # The boost site sits between the two parallel legs (y ~ 0.15).
    ax, ay = approaches[0]
    assert 0.0 < ay < 0.3


def test_guide_self_approaches_silent_on_straight_guide():
    """A straight guide never self-approaches -> no boost sites."""
    dpr = _diffpair_router()
    route = Route(net=1, net_name="USB3_RX1")
    route.segments.append(_gseg(0.0, 0.0, 5.0, 0.0))
    route.segments.append(_gseg(5.0, 0.0, 10.0, 0.0))
    assert dpr._guide_self_approaches(route, spacing_cells=3, proximity=0.5) == []


def test_guide_self_approaches_ignores_adjacent_corner():
    """An adjacent right-angle corner is arc-close, so it is NOT a loop-back.

    Consecutive segments share an endpoint and are handled by the corner join
    (#4490); the arc-length floor must exclude them so approach 2 does not
    double-count approach 1's territory.
    """
    dpr = _diffpair_router()
    route = Route(net=1, net_name="USB3_RX1")
    route.segments.append(_gseg(0.0, 0.0, 5.0, 0.0))
    route.segments.append(_gseg(5.0, 0.0, 5.0, 5.0))
    assert dpr._guide_self_approaches(route, spacing_cells=3, proximity=0.5) == []


def test_guide_self_approaches_clusters_overlapping_sites():
    """A long parallel loop-back collapses to a small number of boost sites."""
    dpr = _diffpair_router()
    route = Route(net=1, net_name="USB3_RX1")
    # Many short colinear segments on the outbound leg, then the return leg:
    # every outbound/return midpoint pair is within clearance, but clustering
    # keeps the boost set small (not one site per pair).
    x = 0.0
    while x < 5.0:
        route.segments.append(_gseg(x, 0.0, x + 0.5, 0.0))
        x += 0.5
    route.segments.append(_gseg(5.0, 0.0, 5.0, 0.3))
    x = 5.0
    while x > 0.0:
        route.segments.append(_gseg(x, 0.3, x - 0.5, 0.3))
        x -= 0.5
    approaches = dpr._guide_self_approaches(route, spacing_cells=3, proximity=0.5)
    assert 1 <= len(approaches) <= 12


def test_guide_self_approaches_detects_board06_style_loopback():
    """The board-06 USB3_RX1 geometry: legs ~d apart, ~0.9mm of arc apart.

    The offending pair in the shadow taxonomy sits ~0.4mm (~one offset gap)
    apart in space but ~0.9mm apart in arc (ratio ~2.25).  This pins that the
    detector fires at the REAL geometry, not just the exaggerated hairpin --
    the earlier detour-ratio of 4.0 silently missed it and left board-06 at
    4/9.
    """
    dpr = _diffpair_router(resolution=0.05)  # board-06 grid resolution
    route = Route(net=1, net_name="USB3_RX1")
    # Outbound leg, a tight U at the tip, and a return leg 0.4mm away.
    route.segments.append(_gseg(0.0, 0.0, 0.9, 0.0))
    route.segments.append(_gseg(0.9, 0.0, 0.9, 0.4))
    route.segments.append(_gseg(0.9, 0.4, 0.0, 0.4))
    # spacing_cells=8 @ res 0.05 -> d=0.4, proximity=0.4+0.2=0.6; the two long
    # legs sit exactly one gap apart, ~0.9mm apart in arc (ratio ~2.25).
    approaches = dpr._guide_self_approaches(route, spacing_cells=8)
    assert len(approaches) >= 1


def test_guide_self_approaches_silent_on_gentle_curve():
    """A smooth quarter-turn is NOT a loop-back (arc ~ chord, ratio ~1).

    Nearby points on a gentle bend fall within the proximity window but their
    along-path separation matches their straight-line separation, so the
    detour ratio excludes them -- the mechanism must not penalize ordinary
    bends (Test Plan edge case).
    """
    dpr = _diffpair_router()
    route = Route(net=1, net_name="USB3_RX1")
    # A 90-degree arc of radius 3mm, sampled every ~15 degrees.
    import math as _m

    r = 3.0
    prev = None
    for k in range(7):
        ang = _m.radians(15 * k)
        pt = (r * _m.sin(ang), r - r * _m.cos(ang))
        if prev is not None:
            route.segments.append(_gseg(prev[0], prev[1], pt[0], pt[1]))
        prev = pt
    assert dpr._guide_self_approaches(route, spacing_cells=3, proximity=0.5) == []


def test_guide_self_approaches_ignores_cross_layer():
    """Legs on different layers cannot overlap in copper -> not flagged."""
    dpr = _diffpair_router()
    route = Route(net=1, net_name="USB3_RX1")
    route.segments.append(_gseg(0.0, 0.0, 5.0, 0.0, layer=Layer.F_CU))
    route.segments.append(_gseg(5.0, 0.0, 5.0, 0.3, layer=Layer.F_CU))
    route.segments.append(_gseg(5.0, 0.3, 0.0, 0.3, layer=Layer.B_CU))
    assert dpr._guide_self_approaches(route, spacing_cells=3, proximity=0.5) == []


# ---------------------------------------------------------------------------
# 5. mid-route asymmetric moves
# ---------------------------------------------------------------------------


def test_asymmetric_moves_generated_mid_route():
    """Asymmetric advance moves fire OUTSIDE the approach radius.

    Issue #3508: symmetric moves freeze the P->N offset vector, so a
    pair cannot turn a corner whose leg parallels the offset; the
    offset can only rotate via asymmetric moves, which #2490 had
    restricted to the approach phase.  Verify a state far from both
    start and goal produces at least one move where exactly one head
    advanced.
    """
    pf = _make_pathfinder(target_spacing_cells=4)
    state = CoupledState(GridPos(100, 100, 0), GridPos(100, 104, 0), (1, 0))
    neighbors = pf._get_coupled_neighbors(
        state,
        1,
        2,
        p_goal=GridPos(200, 100, 0),
        n_goal=GridPos(200, 104, 0),
        p_start=GridPos(10, 100, 0),
        n_start=GridPos(10, 104, 0),
    )
    asym = [
        (ns, c, v)
        for ns, c, v in neighbors
        if (ns.p_pos != state.p_pos) != (ns.n_pos != state.n_pos)
    ]
    assert asym, "expected asymmetric single-head moves mid-route"
    # Mid-route tolerance stays tight: every generated state keeps
    # spacing within +/-1 cell of the target.
    for ns, _c, is_via in neighbors:
        if is_via:
            continue
        spacing = math.hypot(ns.p_pos.x - ns.n_pos.x, ns.p_pos.y - ns.n_pos.y)
        assert abs(spacing - 4) <= 1 + 1e-9


# ---------------------------------------------------------------------------
# 6. trail proximity guard
# ---------------------------------------------------------------------------


def test_proximity_guard_rejects_near_partner_trail():
    """Landing 1 cell from the partner's trail is rejected.

    Exact-cell guards alone admitted 0.05 mm centerline passes (the
    measured MIPI_CLK -0.175 mm overlap).  With min_spacing_cells=4,
    a candidate 1 cell from a partner trail cell must be pruned.
    """
    pf = _make_pathfinder(target_spacing_cells=4, min_spacing_cells=4)
    state = CoupledState(GridPos(100, 100, 0), GridPos(100, 104, 0), (1, 0))
    # Partner (N) trail passes right next to where P wants to go.
    n_trail_cell = (101, 101, 0)
    buckets = {(n_trail_cell[0] // 4, n_trail_cell[1] // 4): [n_trail_cell]}
    neighbors = pf._get_coupled_neighbors(
        state,
        1,
        2,
        p_goal=GridPos(200, 100, 0),
        n_goal=GridPos(200, 104, 0),
        p_start=GridPos(10, 100, 0),
        n_start=GridPos(10, 104, 0),
        p_visited=frozenset({(100, 100, 0)}),
        n_visited=frozenset({n_trail_cell}),
        n_trail_buckets=buckets,
        p_trail_buckets={},
    )
    for ns, _c, is_via in neighbors:
        if is_via:
            continue
        if ns.p_pos != state.p_pos:  # P advanced
            d = math.hypot(ns.p_pos.x - 101, ns.p_pos.y - 101)
            assert d >= 4 - 1e-9, (
                f"P landed {d:.2f} cells from the partner trail (< min_spacing_cells=4): {ns.p_pos}"
            )


def test_proximity_guard_allows_exact_min_spacing():
    """Distance exactly == min_spacing_cells is NOT a violation."""
    pf = _make_pathfinder(target_spacing_cells=4, min_spacing_cells=4)
    state = CoupledState(GridPos(100, 100, 0), GridPos(100, 104, 0), (1, 0))
    # Partner trail directly above P's forward landing cell at exactly
    # 4 cells.
    n_trail_cell = (101, 96, 0)
    buckets = {(n_trail_cell[0] // 4, n_trail_cell[1] // 4): [n_trail_cell]}
    neighbors = pf._get_coupled_neighbors(
        state,
        1,
        2,
        p_goal=GridPos(200, 100, 0),
        n_goal=GridPos(200, 104, 0),
        p_start=GridPos(10, 100, 0),
        n_start=GridPos(10, 104, 0),
        p_visited=frozenset({(100, 100, 0)}),
        n_visited=frozenset({n_trail_cell}),
        n_trail_buckets=buckets,
        p_trail_buckets={},
    )
    forward = [
        ns for ns, _c, is_via in neighbors if not is_via and ns.p_pos == GridPos(101, 100, 0)
    ]
    assert forward, (
        "P's forward symmetric step at exactly min_spacing distance from "
        "the partner trail must be admitted"
    )


# ---------------------------------------------------------------------------
# 7. Issue #3508 decomposition: shadow-constructor opt-in gate
# ---------------------------------------------------------------------------


def test_shadow_construction_flag_defaults_off():
    """The geometric shadow constructor is opt-in (default False).

    Two of the three original artifact-quality defects are fixed on main
    (stranded shadow tails -> #3665 transactional rollback; shadow-via /
    partner intersections -> #3667 full-polyline via validation), and the
    corridor-competition defect did not reproduce on the current
    tightened-width geometry (#3921 seed-42 re-run reached 15/15 singles).

    The flag stays OFF because the #3921 (2026-07-08) end-to-end
    re-measurement found shadow-ON still un-shippable on board 06:
    convergence collapsed to 3/9 (the 0.225-0.275 mm coupled widths make
    the geometric parallel offset infeasible for 6/9 pairs), the surviving
    shadow segments are off-angle (would fail the #3975 45-census if
    committed), and the fallbacks blow the CI wall-clock (>1200 s vs
    ~150 s / 21-21 shadow-OFF).  Flipping this default would regress CI;
    it stays opt-in until a shadow-aware by-construction dogleg and a
    parallel-offset feasibility fix land.  See #3921 for the full data.
    """
    from kicad_tools.router.diffpair import DifferentialPairConfig

    assert DifferentialPairConfig().enable_shadow_construction is False
    assert DifferentialPairConfig(enabled=True).enable_shadow_construction is False


def test_shadow_construction_flag_plumbed_from_config():
    """``route_all_with_diffpairs`` copies the config flag onto the router."""
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.diffpair import DifferentialPairConfig

    rules = DesignRules()
    router = Autorouter(width=12.7, height=12.7, rules=rules)
    dpr = router._diffpair
    assert dpr.enable_shadow_construction is False

    # No pairs detected -> the call returns immediately, but the flag
    # must already have been copied from the config.
    router.route_all_with_diffpairs(
        diffpair_config=DifferentialPairConfig(enabled=True, enable_shadow_construction=True)
    )
    assert dpr.enable_shadow_construction is True

    router.route_all_with_diffpairs(diffpair_config=DifferentialPairConfig(enabled=True))
    assert dpr.enable_shadow_construction is False


# ---------------------------------------------------------------------------
# 8. Issue #3547: flag-off inertness of the #3508 coupled search upgrades
#
# PR #3546 shipped the #3508 coupled machinery as a gated opt-in
# (``enable_shadow_construction``, default False) with the contract that
# a flag-off run keeps recipes on their pre-#3508 budget-exit behaviour.
# Two pieces were found always-on (not gated by the flag):
#
#   1. the near-miss rescue (``_rescue_near_miss_coupled``), which commits
#      a coupled body + single-ended tails for a search that deferred, and
#   2. the CoupledPathfinder weighted-A* search upgrade
#      (``heuristic_weight=COUPLED_HEURISTIC_WEIGHT`` > 1), which changes
#      WHICH joint states the always-running coupled pre-phase explores --
#      so a search that DEFERRED on the pre-#3508 baseline can CONVERGE
#      (and commit) with the flag off, re-exposing the gated hazards
#      (#3542 corridor competition, #3544 pre-phase seg-seg violations).
#
# Both are now gated behind ``enable_shadow_construction``.  These tests
# drive ``route_differential_pair_coupled`` against a stubbed pathfinder
# so the flag-off/flag-on behaviour of each path is asserted directly:
#   - the search upgrade by capturing the ``heuristic_weight`` the
#     CoupledPathfinder is constructed with, and
#   - the rescue by spying on ``_rescue_near_miss_coupled``.
# ---------------------------------------------------------------------------


def _two_pad_coupled_router_and_pair():
    """A 2-pad diff pair + its router, ready for the coupled pre-phase.

    Returns ``(router, pair)`` where ``router._diffpair`` is the
    :class:`DiffPairRouter` under test and ``pair`` is a
    :class:`DifferentialPair` whose pads are registered on the router.
    """
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.diffpair import (
        DifferentialPair,
        DifferentialPairType,
        DifferentialSignal,
    )

    rules = DesignRules()
    router = Autorouter(width=30.0, height=10.0, rules=rules)
    p_y, n_y = 4.8, 5.2
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": p_y,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 5.0,
                "y": n_y,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
        ],
    )
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 25.0,
                "y": p_y,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 25.0,
                "y": n_y,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
        ],
    )
    pair = DifferentialPair(
        name="USB_D",
        positive=DifferentialSignal(
            net_name="USB_D+",
            net_id=1,
            base_name="USB_D",
            polarity="P",
            notation="plus_minus",
        ),
        negative=DifferentialSignal(
            net_name="USB_D-",
            net_id=2,
            base_name="USB_D",
            polarity="N",
            notation="plus_minus",
        ),
        pair_type=DifferentialPairType.USB2,
    )
    return router, pair


class _StubPathfinder:
    """Stand-in for ``CoupledPathfinder`` with a scripted outcome.

    ``route_coupled`` returns ``_result`` (a committable (P, N) tuple to
    simulate a CONVERGED search, or ``None`` to simulate a DEFERRED one).
    The progress diagnostics are populated so the near-miss rescue branch
    is *eligible* to fire whenever the search deferred -- the only thing
    that should gate it is ``enable_shadow_construction``.
    """

    def __init__(self, result, rescue_eligible: bool = True):
        self._result = result
        self.last_timeout_exceeded = False
        # Issue #3921: iteration-vs-wall-clock discriminator read by the
        # caller's budget-exit diagnostic.
        self.last_iteration_limited = False
        self.last_iterations = 1
        self.last_best_progress = 0.0  # <= NEAR_MISS_RESCUE_CELLS
        self.last_best_state = object()
        # rescue eligibility requires a non-None last_best_node; tests that
        # are not exercising the rescue set this to None so the (real,
        # un-stubbed) rescue branch is never entered.
        self.last_best_node = object() if rescue_eligible else None
        self.last_rejections = {}

    def route_coupled(self, *_a, **_k):
        return self._result


def _patch_pathfinder_capture_weight(monkeypatch, result, rescue_eligible=True):
    """Patch the module ``CoupledPathfinder``; capture ``heuristic_weight``.

    Returns a ``captured`` dict whose ``"heuristic_weight"`` key records the
    value the pre-phase constructed the pathfinder with.
    """
    import kicad_tools.router.diffpair_routing as dpr_mod

    captured: dict[str, float] = {}

    def _factory(*_a, **kwargs):
        captured["heuristic_weight"] = kwargs.get("heuristic_weight")
        return _StubPathfinder(result, rescue_eligible=rescue_eligible)

    monkeypatch.setattr(dpr_mod, "CoupledPathfinder", _factory)
    return captured


def test_flag_off_uses_classic_astar_search(monkeypatch):
    """Flag OFF -> CoupledPathfinder built with classic A* (weight 1.0).

    The #3508 weighted-A* upgrade (``COUPLED_HEURISTIC_WEIGHT`` > 1) is
    what changes which joint states the search explores.  With
    ``enable_shadow_construction=False`` the pre-phase must construct the
    pathfinder with ``heuristic_weight == 1.0`` (the pre-#3508 search), so
    a search that deferred on the baseline still defers.
    """
    from kicad_tools.router.diffpair_routing import COUPLED_HEURISTIC_WEIGHT

    assert COUPLED_HEURISTIC_WEIGHT > 1.0, "fixture assumes the weighted-A* upgrade is > 1.0"
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = False
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)
    captured = _patch_pathfinder_capture_weight(monkeypatch, None, rescue_eligible=False)

    dpr.route_differential_pair_coupled(pair, coupled_only=True)

    assert captured.get("heuristic_weight") == 1.0, (
        "flag-off run must use classic optimal A* (heuristic_weight=1.0), "
        f"got {captured.get('heuristic_weight')}"
    )


def test_flag_on_uses_weighted_astar_search(monkeypatch):
    """Flag ON -> CoupledPathfinder built with the weighted-A* upgrade.

    Control for the search-upgrade gate: with
    ``enable_shadow_construction=True`` the pre-phase constructs the
    pathfinder with the #3508 ``COUPLED_HEURISTIC_WEIGHT``.
    """
    from kicad_tools.router.diffpair_routing import COUPLED_HEURISTIC_WEIGHT

    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = True
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)
    captured = _patch_pathfinder_capture_weight(monkeypatch, None, rescue_eligible=False)

    dpr.route_differential_pair_coupled(pair, coupled_only=True)

    assert captured.get("heuristic_weight") == COUPLED_HEURISTIC_WEIGHT, (
        "flag-on run must use the weighted-A* upgrade "
        f"({COUPLED_HEURISTIC_WEIGHT}), got {captured.get('heuristic_weight')}"
    )


# ---------------------------------------------------------------------------
# Issue #3921: coupled budget-exit DIAGNOSTIC.
#
# ``CoupledPathfinder.route_coupled`` raises the shared
# ``last_timeout_exceeded`` flag for BOTH its iteration budget
# (``max_iterations_budget``) and its wall-clock budget
# (``timeout_seconds``).  The old budget-exit WARNING hard-coded the
# ``per_pair_timeout`` seconds ("budget exceeded (120s)") even when the
# iteration budget bailed the search in 0.3s.  ``last_iteration_limited``
# now disambiguates the two, and the message reports the actual iteration
# count and per-phase split so the exit reason is no longer opaque.
#
# (The curation comment also proposed raising the flag-off iteration
# budget to a FLOOR to restore board-06 coupled convergence.  That was
# measured against the real seed-42 bench and does NOT converge any pair
# -- best-progress plateaus identically at 20x the iterations while
# wall-time balloons -- so the floor was dropped as ineffective and
# wall-time-harmful.  See the #3921 PR body.)
# ---------------------------------------------------------------------------


def test_flag_off_iteration_exit_diagnostic_reports_iterations(monkeypatch, capsys):
    """Issue #3921 diagnostic: iteration-budget exit must NOT say "120s".

    When the ITERATION budget fires (``last_iteration_limited=True``) the
    user-visible budget-exit WARNING must cite the iteration count, not
    hard-code the wall-clock ``per_pair_timeout`` seconds.  Previously the
    message read "budget exceeded (120s)" even for a search that bailed in
    0.3s after exhausting its iteration budget.
    """
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = False
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)

    # Script an ITERATION-budget exit: search deferred, timeout flag set,
    # discriminator says the iteration budget was the binding constraint.
    def _factory(*_a, **kwargs):
        stub = _StubPathfinder(None, rescue_eligible=False)
        stub.last_timeout_exceeded = True
        stub.last_iteration_limited = True
        stub.last_iterations = 20000
        return stub

    import kicad_tools.router.diffpair_routing as dpr_mod

    monkeypatch.setattr(dpr_mod, "CoupledPathfinder", _factory)

    routes, _warning = dpr.route_differential_pair_coupled(
        pair,
        coupled_only=True,
        per_pair_timeout=120.0,
        per_pair_max_iterations=2000,
    )

    out = capsys.readouterr().out
    assert "iteration budget exceeded" in out, (
        f"iteration-budget exit must report an iteration budget; got: {out!r}"
    )
    assert "20000 iters" in out, (
        f"iteration-budget exit must cite the iteration count; got: {out!r}"
    )
    # The pair is skipped (deferred to the main strategy) on a budget exit.
    assert routes == []


def test_flag_off_wallclock_exit_diagnostic_reports_seconds(monkeypatch, capsys):
    """Control: a genuine wall-clock exit still reports seconds.

    When the WALL-CLOCK budget fires (``last_iteration_limited=False``)
    the message reports the ``per_pair_timeout`` seconds, not an iteration
    budget -- the two exit reasons are now distinguished.
    """
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = False
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)
    holder: dict = {}

    def _factory(*_a, **kwargs):
        stub = _StubPathfinder(None, rescue_eligible=False)
        stub.last_timeout_exceeded = True
        stub.last_iteration_limited = False  # wall-clock, not iterations
        stub.last_iterations = 137
        holder["stub"] = stub
        return stub

    import kicad_tools.router.diffpair_routing as dpr_mod

    monkeypatch.setattr(dpr_mod, "CoupledPathfinder", _factory)

    dpr.route_differential_pair_coupled(
        pair,
        coupled_only=True,
        per_pair_timeout=120.0,
        per_pair_max_iterations=2000,
    )

    out = capsys.readouterr().out
    assert "wall-clock budget exceeded" in out, (
        f"wall-clock exit must report a wall-clock budget; got: {out!r}"
    )
    assert "120s" in out, f"wall-clock exit must cite the seconds budget; got: {out!r}"
    assert "iteration budget exceeded" not in out


def test_flag_off_does_not_invoke_near_miss_rescue(monkeypatch):
    """Flag OFF + search DEFERS near the goal -> rescue is NOT invoked.

    The stub pathfinder returns ``None`` with ``last_best_progress=0`` and
    a non-None ``last_best_node``, i.e. the exact precondition that makes
    the near-miss rescue eligible.  With the flag off the rescue must not
    even be called (spy asserts zero invocations).
    """
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = False
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)
    _patch_pathfinder_capture_weight(monkeypatch, None)

    calls = {"n": 0}

    def _spy(self, *a, **k):
        calls["n"] += 1
        return None

    monkeypatch.setattr(type(dpr), "_rescue_near_miss_coupled", _spy, raising=True)

    dpr.route_differential_pair_coupled(pair, coupled_only=True)

    assert calls["n"] == 0, (
        "near-miss rescue must NOT be invoked when enable_shadow_construction is False"
    )


def test_flag_on_invokes_near_miss_rescue(monkeypatch):
    """Flag ON + search DEFERS near the goal -> rescue IS invoked.

    Control for the rescue gate: same deferred-near-goal precondition, but
    with ``enable_shadow_construction=True`` the rescue is called.
    """
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = True
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)
    _patch_pathfinder_capture_weight(monkeypatch, None)

    calls = {"n": 0}

    def _spy(self, *a, **k):
        calls["n"] += 1
        return None  # rescue declines; we only assert it was consulted

    monkeypatch.setattr(type(dpr), "_rescue_near_miss_coupled", _spy, raising=True)

    dpr.route_differential_pair_coupled(pair, coupled_only=True)

    assert calls["n"] == 1, (
        "near-miss rescue must be invoked when enable_shadow_construction is True"
    )


# ---------------------------------------------------------------------------
# 9. Issue #3540: transactional pad-connectivity claim
#
# The shadow constructor (and its rescue-tail / stub-edge machinery) can
# commit copper that fails to actually REACH a goal pad while the per-spec
# commit has already marked that copper on the grid.  Left as-is the caller
# claims the pair's nets (#2464 reserve), the negotiated main strategy
# skips them, and the goal pads are STRANDED for the rest of the pipeline.
#
# ``route_differential_pair_coupled`` must make the claim TRANSACTIONAL:
# after committing the pair's copper (body + tails + stub edges), it
# verifies every pad of BOTH nets is reached.  On any gap it rips the
# pair's copper off the grid + route list and defers the whole pair
# (returns ``([], None)`` under ``coupled_only`` so the caller never
# claims, or falls through to the single-ended router otherwise).
# ---------------------------------------------------------------------------


def _coupled_routes_for_pair(pair, p_end_x: float, n_end_x: float) -> tuple[Route, Route]:
    """Build a committable (P, N) result for the 2-pad fixture.

    The U1 pads sit at x=5.0 and the J1 pads at x=25.0 (y=4.8 for P,
    y=5.2 for N).  Each returned route is a single horizontal segment
    from the U1 pad to ``*_end_x``; passing 25.0 reaches the J1 goal
    pad, while a shorter value (e.g. 15.0) STRANDS it -- the exact
    "claimed-but-unconnected goal pad" failure mode #3540 fixes.
    """
    p_route = Route(net=pair.positive.net_id, net_name=pair.positive.net_name)
    p_route.segments.append(
        Segment(
            x1=5.0,
            y1=4.8,
            x2=p_end_x,
            y2=4.8,
            width=0.2,
            layer=Layer.F_CU,
            net=pair.positive.net_id,
            net_name=pair.positive.net_name,
        )
    )
    n_route = Route(net=pair.negative.net_id, net_name=pair.negative.net_name)
    n_route.segments.append(
        Segment(
            x1=5.0,
            y1=5.2,
            x2=n_end_x,
            y2=5.2,
            width=0.2,
            layer=Layer.F_CU,
            net=pair.negative.net_id,
            net_name=pair.negative.net_name,
        )
    )
    return p_route, n_route


def test_shadow_claim_rolls_back_when_goal_pad_stranded(monkeypatch):
    """Flag ON + a committed route that strands a goal pad -> full rollback.

    The stub pathfinder converges with a P route that stops at x=15.0 --
    10 mm short of the J1.1 goal pad at x=25.0 -- so the P net has only
    1 of its 2 pads reachable from the committed copper.  The
    transactional claim must:

      * NOT return any routes (so the caller never claims the nets),
      * leave the autorouter's route list empty (the committed P and N
        copper is ripped), and
      * unmark every cell it had marked on the grid (no stranded copper).
    """
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = True
    # P strands its J1 goal pad (stops at x=15.0); N reaches its goal.
    stranded = _coupled_routes_for_pair(pair, p_end_x=15.0, n_end_x=25.0)
    # Issue #3987 (unit 2a of #3921): with shadow ON the joint-state
    # ``route_coupled`` fallback is gated OFF, so the transactional rollback
    # is reached via the SHADOW constructor.  Provide a guide and stub
    # ``_shadow_route_pair`` to return the stranded result -- the connectivity
    # gate that rips it is downstream of ``result`` and fires identically.
    guide = _coupled_routes_for_pair(pair, p_end_x=25.0, n_end_x=25.0)[0]
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: guide)
    monkeypatch.setattr(dpr, "_shadow_route_pair", lambda *a, **k: stranded)
    _patch_pathfinder_capture_weight(monkeypatch, None, rescue_eligible=False)

    grid = router.grid

    def _pair_cell_count() -> int:
        net_arr = grid._net
        return int(((net_arr == pair.positive.net_id) | (net_arr == pair.negative.net_id)).sum())

    occupied_before = _pair_cell_count()

    routes, warning = dpr.route_differential_pair_coupled(pair, coupled_only=True)

    assert routes == [], (
        "a shadow pair that strands a goal pad must NOT return routes "
        "(returning routes is what makes the caller claim the nets)"
    )
    assert warning is None
    # The committed copper was ripped from the autorouter's route list.
    assert not any(r.net in (pair.positive.net_id, pair.negative.net_id) for r in router.routes), (
        "stranded pair copper must be removed from autorouter.routes on rollback"
    )
    # And every cell it marked on the grid was unmarked (clean rollback).
    occupied_after = _pair_cell_count()
    assert occupied_after == occupied_before, (
        "rollback must unmark the stranded pair's copper from the grid "
        f"(cells before={occupied_before}, after={occupied_after})"
    )


def test_shadow_claim_commits_when_all_pads_reached(monkeypatch):
    """Control: a shadow pair that reaches every goal pad IS claimed.

    Same converged-search setup as the rollback test, but both routes run
    fully from the U1 pads to the J1 pads, so both nets are fully
    connected.  The transactional check must let the claim stand: routes
    are returned and the copper stays committed on the autorouter.
    """
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = True
    good = _coupled_routes_for_pair(pair, p_end_x=25.0, n_end_x=25.0)
    # Issue #3987 (unit 2a of #3921): with shadow ON the pair is
    # shadow-or-uncoupled -- the joint-state ``route_coupled`` fallback is
    # gated OFF -- so a claimed pair reaches the transactional connectivity
    # gate via the SHADOW constructor.  Provide a guide so the shadow path
    # runs, and stub ``_shadow_route_pair`` to return the fully-connected
    # result under test (the gate is downstream of ``result`` and is
    # exercised identically whether the result came from shadow or search).
    guide = _coupled_routes_for_pair(pair, p_end_x=25.0, n_end_x=25.0)[0]
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: guide)
    monkeypatch.setattr(dpr, "_shadow_route_pair", lambda *a, **k: good)
    _patch_pathfinder_capture_weight(monkeypatch, None, rescue_eligible=False)

    routes, _warning = dpr.route_differential_pair_coupled(pair, coupled_only=True)

    p_nets = {r.net for r in routes}
    assert pair.positive.net_id in p_nets and pair.negative.net_id in p_nets, (
        "a fully-connected shadow pair must return both nets' routes so the caller claims them"
    )
    assert any(r.net == pair.positive.net_id for r in router.routes)
    assert any(r.net == pair.negative.net_id for r in router.routes)


# ---------------------------------------------------------------------------
# 10. Issue #3541: shadow via must not intersect the partner trace
#
# When the geometric shadow constructor places its own via before a guide
# layer change, the barrel sits at a perpendicular offset taken against the
# INCOMING guide leg's normal.  The guide BENDS at the via, so an offset
# that clears the incoming leg can still let the barrel intersect the
# OUTGOING leg when the guide turns back toward the shadow side.  At board
# 06's tightly-coupled gaps (0.075-0.15 mm) this produced a ~0.04 mm
# physical overlap between the shadow via and the partner copper -- a short
# that the recipe's 6b audit ripped (USB2_D / USB3_RX1 / USB3_RX2 / PCIE_TX
# de-coupled).
#
# The fix validates each candidate via site against the WHOLE guide
# polyline (barrel vs any-layer copper) with the same
# ``via_diameter/2 + trace_clearance + guide_width/2`` bound the crossing-
# tail synthesizer uses, and widens the perpendicular spread (the
# ``lat_mult`` lattice) until a site clears every guide segment.
# ---------------------------------------------------------------------------


def _via_clearance_bound(rules, guide) -> float:
    """The #3541 via-barrel-vs-partner bound the constructor enforces."""
    guide_width = max((g.width for g in guide.segments), default=rules.trace_width)
    return rules.via_diameter / 2 + rules.trace_clearance + guide_width / 2


def _layer_change_guide(p_start_pad, bend_end: tuple[float, float]) -> Route:
    """F_CU leg -> via at (12.0, 4.8) -> B_CU leg toward ``bend_end``.

    The pre-via leg approaches the layer change along +x; the post-via leg
    heads toward ``bend_end``.  Choosing a ``bend_end`` that sweeps the
    out-going leg back through the shadow-via neighbourhood is what made the
    pre-#3541 minimum-lateral via intersect the partner.
    """
    net, name = p_start_pad.net, p_start_pad.net_name
    guide = Route(net=net, net_name=name)
    guide.segments.append(
        Segment(
            x1=5.0,
            y1=4.8,
            x2=12.0,
            y2=4.8,
            width=0.2,
            layer=Layer.F_CU,
            net=net,
            net_name=name,
        )
    )
    guide.segments.append(
        Segment(
            x1=12.0,
            y1=4.8,
            x2=bend_end[0],
            y2=bend_end[1],
            width=0.2,
            layer=Layer.B_CU,
            net=net,
            net_name=name,
        )
    )
    guide.vias.append(
        Via(
            x=12.0,
            y=4.8,
            drill=0.35,
            diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU),
            net=net,
            net_name=name,
        )
    )
    return guide


def _shadow_setup(spacing_cells: int, bend_end: tuple[float, float]):
    """Build (dpr, pair, spec, pathfinder, guide) for the via-geometry test.

    Open 2-pad fixture (no obstacles), a tight coupled spacing, and a
    layer-changing guide bending toward ``bend_end``.
    """
    from kicad_tools.router.diffpair_routing import CoupledPathfinder, CoupledSegmentSpec

    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = True
    spec = CoupledSegmentSpec(
        p_start=router.pads[("U1", "1")],
        p_end=router.pads[("J1", "1")],
        n_start=router.pads[("U1", "2")],
        n_end=router.pads[("J1", "2")],
    )
    pathfinder = CoupledPathfinder(
        grid=router.grid,
        rules=router.rules,
        target_spacing_cells=spacing_cells,
        net_class_map=getattr(router, "net_class_map", None),
    )
    guide = _layer_change_guide(router.pads[("U1", "1")], bend_end)
    return dpr, pair, spec, pathfinder, guide


def _defeat_pair_self_check_gates(mp) -> None:
    """Stub off BOTH constructed-pair copper self-checks (issue #4575).

    The fixtures below deliberately drive the constructor with DEGENERATE
    geometry -- a 0.4 mm coupled gap alongside a 0.7 mm-diameter guide via,
    or a stub tail that drops a 0.6 mm via straight onto the body anchor --
    so that the single property under test is observable in what the
    constructor COMMITS rather than being masked by a blanket decline.  That
    geometry PHYSICALLY overlaps the partner (0.40 mm centre-to-centre
    against a 0.45 mm overlap threshold), which is why
    ``_pair_has_physical_overlap`` has always been stubbed off here.

    ``_route_via_violation`` is the CLEARANCE sibling of that same overlap
    check -- it fires on exactly the same geometry, one quadrant over -- so
    it is stubbed off alongside it.  Leaving it armed would turn every one of
    these fixtures into a test of the #4575 gate instead of the property it
    was written for.
    """
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    mp.setattr(DiffPairRouter, "_pair_has_physical_overlap", lambda self, p, n: False, raising=True)
    mp.setattr(DiffPairRouter, "_route_via_violation", lambda self, r: (0.0, None), raising=True)


def _stub_tail_route(
    self, _pf, head, goal, _layer, _label, _name, partner_segments=None, prefer_planar=False
):
    """Degenerate body-anchor tail (isolates the via geometry under test).

    Routing the real pad-reaching tail would have a straight fake tail
    cross the guide and trip the constructor's separate intra-pair / overlap
    self-checks -- artifacts unrelated to the #3541 via geometry.  A
    near-zero stub at the body anchor leaves the body's via intact.
    """
    r = Route(net=goal.net, net_name=goal.net_name)
    r.segments.append(
        Segment(
            x1=head.x,
            y1=head.y,
            x2=head.x + 0.001,
            y2=head.y,
            width=0.2,
            layer=head.layer,
            net=goal.net,
            net_name=goal.net_name,
        )
    )
    return r


# The #3541 load-bearing geometry: an out-going B_CU leg bending up-and-LEFT
# (``bend_end`` below x=12.0) that sweeps the guide back through the side=+1.0
# minimum-lateral (lat_mult=1.0) shadow-via neighbourhood.  At this geometry
# the un-spread via barrel lands 0.354 mm from the guide -- a SHORT against the
# ~0.65 mm ``via_clear`` bound -- and the guide's own seg-vs-seg self-check
# (``find_intra_pair_clearance_violations``) does NOT see it (it is a
# via-vs-trace overlap, not seg-vs-seg), so WITHOUT the guard the constructor
# COMMITS the shorting via.  WITH the guard the lattice widens to lat_mult>1.0
# and the via clears at 0.700 mm.  (Verified by deleting the guard: the
# committed via min-dist drops 0.700 -> 0.354 mm.)
_SHADOW_VIA_GUARD_BEND_END = (11.0, 7.7)


def test_shadow_via_clears_partner_at_tight_gap(monkeypatch):
    """End-to-end: the constructed shadow via clears the partner copper.

    Drives ``_shadow_route_pair`` with a layer-changing guide and a tightly-
    coupled spacing (4 cells = 0.4 mm, well below the ~0.65 mm via bound).
    Every via the shadow places must clear EVERY guide segment by at least
    ``via_diameter/2 + trace_clearance + guide_width/2`` -- the geometric
    guarantee the perpendicular spread provides (issue #3541 acceptance:
    "via_edge -> partner_copper >= trace_clearance, validated cell-by-cell").

    Uses the load-bearing geometry (:data:`_SHADOW_VIA_GUARD_BEND_END`) at
    which the un-spread minimum-lateral via shorts the guide, so the assertion
    is only satisfiable because the guard widened the lattice -- see
    ``test_shadow_via_guard_is_load_bearing`` for the matching negative control.
    """
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    spacing_cells = 4
    dpr, pair, spec, pathfinder, guide = _shadow_setup(
        spacing_cells, bend_end=_SHADOW_VIA_GUARD_BEND_END
    )

    monkeypatch.setattr(DiffPairRouter, "_tail_route", _stub_tail_route, raising=True)
    # Defeat the belt-and-braces copper self-checks so we observe the via the
    # constructor PRODUCES (the fix must clear the guide by construction,
    # not merely fail the side over to a reject-everything None).
    _defeat_pair_self_check_gates(monkeypatch)

    result = dpr._shadow_route_pair(pair, spec, pathfinder, guide, spacing_cells)
    assert result is not None, "shadow constructor should place a clearing via, not give up"
    _p_route, n_route = result  # P is the guide; N is the geometric shadow.

    via_clear = _via_clearance_bound(dpr.autorouter.rules, guide)
    assert n_route.vias, "the shadow must carry its own layer-change via"
    for via in n_route.vias:
        for seg in guide.segments:
            dist = DiffPairRouter._point_segment_distance(via.x, via.y, seg)
            assert dist >= via_clear - 1e-9, (
                f"shadow via at ({via.x:.3f},{via.y:.3f}) intersects partner "
                f"copper: centerline distance {dist:.4f} mm < required "
                f"{via_clear:.4f} mm (barrel overlaps the guide trace)"
            )


def test_shadow_via_guard_is_load_bearing(monkeypatch):
    """Negative control: deleting the guard makes ``_shadow_route_pair`` short.

    This is the integration-level proof the #3541 guard is load-bearing -- it
    drives ``_shadow_route_pair`` END TO END (asserting on the route it
    RETURNS, not on the geometry helper) and contrasts two runs on the same
    load-bearing geometry (:data:`_SHADOW_VIA_GUARD_BEND_END`):

      * **Guard present** (production): the minimum-lateral (lat_mult=1.0) via
        site grazes the out-going guide leg, so the guard rejects it and the
        lattice widens; the committed via clears the guide by >= ``via_clear``.
      * **Guard absent** (the guard predicate stubbed out via
        ``_min_distance_to_partner`` -> +inf, the ONLY call site of that helper
        inside ``_shadow_route_pair``): the constructor COMMITS the grazing
        lat_mult=1.0 via, whose barrel sits < ``via_clear`` from the guide -- a
        short.  The seg-vs-seg self-check (``find_intra_pair_clearance_violations``)
        does NOT catch it because the overlap is via-vs-trace, not seg-vs-seg.

    The downstream ``_pair_has_physical_overlap`` belt-and-braces gate IS the
    backstop that would otherwise defer this short in production, so it is
    stubbed off in BOTH runs to isolate the guard's contribution -- exactly the
    PCIE_TX 0.0%-continuity short #3541 locks down (a future refactor that
    silently drops the guard re-exposes it, and this test then fails).
    """
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    spacing_cells = 4

    def _committed_shadow_via(disable_guard: bool):
        # A fresh patch context per run: the guard is patched out ONLY for the
        # unguarded run, then reverted, so the guarded run sees the real guard.
        with monkeypatch.context() as mp:
            dpr, pair, spec, pathfinder, guide = _shadow_setup(
                spacing_cells, bend_end=_SHADOW_VIA_GUARD_BEND_END
            )
            mp.setattr(DiffPairRouter, "_tail_route", _stub_tail_route, raising=True)
            # Stub OFF the belt-and-braces copper self-checks in both runs:
            # they are the production backstop, but here we are isolating the
            # GUARD's effect, so what the constructor COMMITS (clean vs short)
            # must be the guard's doing, not the gates'.
            _defeat_pair_self_check_gates(mp)
            if disable_guard:
                # ``_min_distance_to_partner`` is called in exactly one place
                # inside ``_shadow_route_pair`` -- the #3541 via-vs-guide guard.
                # Forcing it to +inf makes every candidate pass the guard, i.e.
                # deletes the guard.
                mp.setattr(
                    DiffPairRouter,
                    "_min_distance_to_partner",
                    lambda self, *a, **k: float("inf"),
                    raising=True,
                )
            result = dpr._shadow_route_pair(pair, spec, pathfinder, guide, spacing_cells)
        assert result is not None, "shadow constructor should commit a route at this geometry"
        _p_route, n_route = result
        assert n_route.vias, "the shadow must carry its own layer-change via"
        via_clear = _via_clearance_bound(dpr.autorouter.rules, guide)
        min_dist = min(
            DiffPairRouter._point_segment_distance(via.x, via.y, seg)
            for via in n_route.vias
            for seg in guide.segments
        )
        return min_dist, via_clear

    # Guard ABSENT: the constructor commits the grazing lat_mult=1.0 via -- a
    # short.  This is the assertion that FAILS if the guard is restored, and
    # (equivalently) PASSES only because deleting the guard re-introduces the
    # #3541 short -- proving the guard is what prevents it.
    unguarded_dist, via_clear = _committed_shadow_via(disable_guard=True)
    assert unguarded_dist < via_clear, (
        "negative control failed: with the #3541 guard deleted, "
        "_shadow_route_pair must COMMIT a shorting via (barrel-to-guide "
        f"{unguarded_dist:.4f} mm < required {via_clear:.4f} mm).  If this "
        "passes, the geometry no longer exercises the guard and the positive "
        "case below is vacuous."
    )

    # Guard PRESENT (production): the same geometry now clears, because the
    # guard rejected the grazing site and the lattice widened.
    guarded_dist, _via_clear = _committed_shadow_via(disable_guard=False)
    assert guarded_dist >= via_clear - 1e-9, (
        "with the #3541 guard active the committed shadow via must clear the "
        f"guide (barrel-to-guide {guarded_dist:.4f} mm >= {via_clear:.4f} mm)"
    )
    # And the guard's effect is the difference between the two runs.
    assert guarded_dist > unguarded_dist, (
        "the guard must change the committed geometry: clearing via "
        f"({guarded_dist:.4f} mm) vs grazing via ({unguarded_dist:.4f} mm)"
    )


# ---------------------------------------------------------------------------
# 11. Issue #3987 (unit 2a of #3921): shadow copper is 45-compliant by
# construction.
#
# ``_shadow_route_pair`` runs the assembled shadow segments through
# ``_quantize_shadow_segments`` before its self-check gates, so every
# shadow-emitted segment is on the {0, 45, 90, 135} angle set (census-clean,
# no ``OffAngleSegmentWarning`` from the #3975 emission guard).  The guide
# side is the C++ on-grid router's output and is already aligned; the
# geometric shadow (miter apex / via jogs / pad-approach tails) was the only
# off-angle source.  The dogleg pass reuses ``quantize.dogleg_points`` (the
# #3532/#3907 file-layer transform) lifted to the route layer, and is
# obstacle-aware: each dogleg variant's legs are re-rastered against
# ``_is_cell_blocked`` and a variant that collides is rejected.
# ---------------------------------------------------------------------------


def _seg(x1, y1, x2, y2, net=1):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=Layer.F_CU, net=net, net_name="N")


def test_quantize_shadow_leaves_aligned_segments_untouched():
    """Axis/diagonal shadow legs pass through the dogleg pass unchanged."""
    from kicad_tools.router.quantize import is_45_aligned

    router, _pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    pf = CoupledPathfinder(grid=router.grid, rules=router.rules, target_spacing_cells=4)

    route = Route(net=1, net_name="N")
    route.segments.append(_seg(1.0, 1.0, 5.0, 1.0))  # horizontal (0 deg)
    route.segments.append(_seg(5.0, 1.0, 8.0, 4.0))  # exact diagonal (45 deg)
    before = list(route.segments)

    dpr._quantize_shadow_segments(route, pf)

    assert route.segments == before, "aligned segments must not be rewritten"
    for s in route.segments:
        assert is_45_aligned(s.x2 - s.x1, s.y2 - s.y1)


def test_quantize_shadow_doglegs_off_angle_segment():
    """An off-angle shadow segment is split into two 45-legal legs.

    The dogleg preserves both endpoints exactly (so the coupled gap the
    constructor established is held) and every resulting leg is on the
    {0,45,90,135} set -- the geometry the #3975 emission census reads.
    """
    from kicad_tools.router.quantize import is_45_aligned, off_angle_degrees

    router, _pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    pf = CoupledPathfinder(grid=router.grid, rules=router.rules, target_spacing_cells=4)

    # A miter-apex-like skew: 20 deg off the axis, off every 45-multiple.
    off = _seg(2.0, 2.0, 8.0, 4.0)
    assert off_angle_degrees(off.x2 - off.x1, off.y2 - off.y1) > 1.0
    route = Route(net=1, net_name="N")
    route.segments.append(off)

    dpr._quantize_shadow_segments(route, pf)

    assert len(route.segments) == 2, "off-angle segment must split into a two-leg dogleg"
    for s in route.segments:
        assert is_45_aligned(s.x2 - s.x1, s.y2 - s.y1), (
            f"dogleg leg ({s.x1},{s.y1})->({s.x2},{s.y2}) is still off-angle"
        )
    # Endpoints preserved exactly (contiguous, and the chord's ends unchanged).
    assert (route.segments[0].x1, route.segments[0].y1) == (2.0, 2.0)
    assert (route.segments[-1].x2, route.segments[-1].y2) == (8.0, 4.0)
    assert (route.segments[0].x2, route.segments[0].y2) == (
        route.segments[1].x1,
        route.segments[1].y1,
    )


def test_quantize_shadow_keeps_off_angle_when_no_clear_variant():
    """Obstacle-aware: with BOTH dogleg bulges blocked, keep the original.

    The pass re-rasters each dogleg variant against ``_is_cell_blocked``.
    If the default and ``axis_first`` variants both collide, the segment is
    left untouched (graceful degradation -- the downstream self-check /
    overlap gates and the emission census still apply) rather than shipping a
    dogleg through copper (the obstacle-blind post-hoc-quantizer short #3906
    hit).
    """
    router, _pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    pf = CoupledPathfinder(grid=router.grid, rules=router.rules, target_spacing_cells=4)

    off = _seg(2.0, 2.0, 8.0, 4.0, net=1)
    route = Route(net=1, net_name="N")
    route.segments.append(off)

    # Force every candidate leg to look blocked (foreign net everywhere).
    monkey = pf._is_cell_blocked
    try:
        pf._is_cell_blocked = lambda gx, gy, li, net: True  # type: ignore[method-assign]
        dpr._quantize_shadow_segments(route, pf)
    finally:
        pf._is_cell_blocked = monkey  # type: ignore[method-assign]

    assert len(route.segments) == 1, "no clear dogleg variant -> keep the original segment"
    assert (
        route.segments[0].x1,
        route.segments[0].y1,
        route.segments[0].x2,
        route.segments[0].y2,
    ) == (
        2.0,
        2.0,
        8.0,
        4.0,
    )


# ---------------------------------------------------------------------------
# 12. Issue #3987 (unit 2a of #3921): hard per-pair shadow time budget.
#
# When ``enable_shadow_construction`` is on, a pair is shadow-or-uncoupled:
# on shadow failure it must fail FAST to the uncoupled fallback WITHOUT
# running the open joint-state ``route_coupled`` search (which floods the
# cost_turn f-plateaus and drove the >1200s #3986 board-06 tail).  With the
# flag OFF the joint-state search remains the pre-phase, unchanged.
# ---------------------------------------------------------------------------


def test_shadow_on_shadow_failure_does_not_flood_open_search(monkeypatch):
    """Flag ON + shadow fails -> the joint-state search is NOT invoked."""
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = True
    # A guide exists (so the shadow path is attempted) but the shadow
    # constructor declines the pair.
    guide = _coupled_routes_for_pair(pair, p_end_x=25.0, n_end_x=25.0)[0]
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: guide)
    monkeypatch.setattr(dpr, "_shadow_route_pair", lambda *a, **k: None)
    captured = _patch_pathfinder_capture_weight(monkeypatch, None, rescue_eligible=False)
    called = {"route_coupled": 0}
    orig_factory_pf = _StubPathfinder

    def _counting_pf(*a, **k):
        pf = orig_factory_pf(None, rescue_eligible=False)
        real_rc = pf.route_coupled

        def _rc(*aa, **kk):
            called["route_coupled"] += 1
            return real_rc(*aa, **kk)

        pf.route_coupled = _rc  # type: ignore[method-assign]
        return pf

    import kicad_tools.router.diffpair_routing as dpr_mod

    monkeypatch.setattr(dpr_mod, "CoupledPathfinder", _counting_pf)

    routes, _warning = dpr.route_differential_pair_coupled(pair, coupled_only=True)

    assert called["route_coupled"] == 0, (
        "with shadow ON and the shadow constructor failing, the open "
        "joint-state route_coupled search must NOT run (fail fast to "
        "uncoupled -- never shadow-then-flooded-A*)"
    )
    # coupled_only defers cleanly (uncoupled fallback returns [], None).
    assert routes == []
    assert captured is not None


# ---------------------------------------------------------------------------
# 13. Issue #3990 (unit 2b of #3921): variable-gap parallel offset within the
# impedance band.
#
# The fixed-gap constructor offset the whole guide by a single ``d``; at the
# tightened 0.225-0.275 mm coupled widths this is infeasible for 6/9 board-06
# pairs (inside-curve self-overlap + obstacle blockage).  The per-section gap
# may vary within ``[d_min, d_max]`` -- floor from
# ``effective_intra_pair_clearance()``, ceiling from
# ``impedance_tolerance_percent`` -- tightening to dodge self-overlap and
# widening to step around obstacles, always inside the impedance band.
# ---------------------------------------------------------------------------


def test_shadow_gap_ladder_prefers_nominal_first():
    """The nominal gap is always the ladder head (easy sections unchanged)."""
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    ladder = DiffPairRouter._shadow_gap_ladder(0.30, 0.25, 0.345)
    assert ladder[0] == 0.30, "nominal gap must be tried first"
    # Every rung stays inside the band.
    assert all(0.25 - 1e-9 <= g <= 0.345 + 1e-9 for g in ladder)
    # Tighter rungs come before wider rungs (tighten-first preference).
    below = [g for g in ladder[1:] if g < 0.30 - 1e-9]
    above = [g for g in ladder[1:] if g > 0.30 + 1e-9]
    assert below, "band should include tighter rungs"
    assert above, "band should include wider rungs"
    first_above_idx = next(i for i, g in enumerate(ladder) if g > 0.30 + 1e-9)
    first_below_idx = next(i for i, g in enumerate(ladder) if g < 0.30 - 1e-9)
    assert first_below_idx < first_above_idx, "tighter rungs must precede wider rungs"


def test_shadow_gap_ladder_collapses_to_nominal_when_band_degenerate():
    """A collapsed band (d_max == d_min) yields only the nominal gap."""
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    ladder = DiffPairRouter._shadow_gap_ladder(0.30, 0.30, 0.30)
    assert ladder == [0.30], "degenerate band collapses to the fixed-gap constructor"


def test_shadow_select_gap_keeps_nominal_when_feasible():
    """An unobstructed segment far from the partner keeps the nominal gap."""
    router, _pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    pf = CoupledPathfinder(grid=router.grid, rules=router.rules, target_spacing_cells=4)

    seg = _seg(2.0, 2.0, 8.0, 2.0)  # horizontal, on F.Cu
    li = router.grid.layer_to_index(Layer.F_CU.value)
    # Partner far away (single distant segment) so partner-clearance never binds;
    # empty grid so no obstacle binds -> nominal (ladder head) is chosen.
    guide_segs = [_seg(2.0, 20.0, 8.0, 20.0)]
    ladder = [0.30, 0.25, 0.35]
    nx, ny = 0.0, 1.0
    gap = dpr._shadow_select_gap(seg, nx, ny, ladder, li, seg.net, pf, guide_segs, 0.2)
    assert gap == 0.30, "feasible nominal section must keep the nominal gap"


def test_shadow_select_gap_tightens_to_dodge_partner_overlap():
    """When the nominal offset lands too close to the partner, a tighter gap wins.

    The partner (guide) copper sits at ``y = 2.0 + 0.28`` (just inside the
    nominal 0.30 offset).  The nominal gap would put the offset segment within
    ``min_center_dist`` of the partner (self-overlap); the ladder's tighter
    rung (0.25) pulls the offset back to a legal separation.
    """
    router, _pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    pf = CoupledPathfinder(grid=router.grid, rules=router.rules, target_spacing_cells=4)

    seg = _seg(2.0, 2.0, 8.0, 2.0)
    li = router.grid.layer_to_index(Layer.F_CU.value)
    nx, ny = 0.0, 1.0  # offset upward (+y)
    # Partner copper just above the nominal offset: at y=2.30 the nominal
    # offset (y=2.30) coincides; a tighter 0.24 offset (y=2.24) clears by
    # 0.06 which exceeds min_center_dist below.
    guide_segs = [_seg(2.0, 2.30, 8.0, 2.30)]
    ladder = [0.30, 0.24, 0.36]
    min_center = 0.05
    gap = dpr._shadow_select_gap(seg, nx, ny, ladder, li, seg.net, pf, guide_segs, min_center)
    assert gap == 0.24, (
        "an inside-curve section whose nominal offset overlaps the partner "
        "must tighten to a legal gap within the band"
    )


def test_shadow_select_gap_degrades_to_nominal_when_no_rung_feasible():
    """With every rung obstacle-blocked, the nominal gap is returned (graceful)."""
    router, _pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    pf = CoupledPathfinder(grid=router.grid, rules=router.rules, target_spacing_cells=4)

    seg = _seg(2.0, 2.0, 8.0, 2.0)
    li = router.grid.layer_to_index(Layer.F_CU.value)
    guide_segs = [_seg(2.0, 20.0, 8.0, 20.0)]
    ladder = [0.30, 0.25, 0.35]
    # Force every candidate offset to look obstacle-blocked.
    saved = pf._is_cell_blocked
    try:
        pf._is_cell_blocked = lambda gx, gy, layer, net: True  # type: ignore[method-assign]
        gap = dpr._shadow_select_gap(seg, 0.0, 1.0, ladder, li, seg.net, pf, guide_segs, 0.2)
    finally:
        pf._is_cell_blocked = saved  # type: ignore[method-assign]
    assert gap == 0.30, (
        "no feasible rung must degrade to the nominal gap (ladder head), not "
        "silently ship a blocked offset -- the downstream self-check gates apply"
    )


# ---------------------------------------------------------------------------
# 10. partner-aware rescue tails (issue #4460, approach 2)
# ---------------------------------------------------------------------------
#
# The shadow constructor's rescue tails connect the trimmed offset body to the
# real pads.  The partner (guide) is NOT in the routing grid during shadow
# construction, so an obstacle-only screen cannot see it.  Measured on board 06
# BEFORE this change: every shadow ``self-check overlap`` decline traced to a
# tail drawn on top of the guide (centre-to-centre 0.018-0.035 mm).  These
# tests pin the two mechanisms that fixed it -- partner clearance as a
# CANDIDATE FILTER inside ``_synthesize_tail`` (rather than a post-hoc veto on
# the single winner) and the ``_tail_partner_clear`` two-tier screen the A*
# fallback is validated against.


class _AllClearPathfinder:
    """Pathfinder stand-in: fixed trace width, nothing obstacle-blocked.

    Isolates the tail synthesizer's CANDIDATE SELECTION from grid state, so a
    rejected candidate can only have been rejected by the partner screen.
    """

    def __init__(self, width: float = 0.2) -> None:
        self._width = width

    def _get_trace_width_for_net(self, net_name: str) -> float:
        return self._width

    def _is_cell_blocked(self, gx: int, gy: int, layer_idx: int, net: int) -> bool:
        return False

    def _is_via_blocked(self, gx: int, gy: int, net: int) -> bool:
        return False


def _tail_pads(head_xy, goal_xy):
    from kicad_tools.router.primitives import Pad

    head = Pad(
        x=head_xy[0],
        y=head_xy[1],
        width=0.3,
        height=0.3,
        net=2,
        net_name="USB3_TX1-",
        layer=Layer.F_CU,
    )
    goal = Pad(
        x=goal_xy[0],
        y=goal_xy[1],
        width=0.3,
        height=0.3,
        net=2,
        net_name="USB3_TX1-",
        layer=Layer.F_CU,
    )
    return head, goal


def _crossing_partner() -> list[Segment]:
    """A guide leg that crosses the straight head->goal corridor mid-way."""
    return [_gseg(6.5, 4.5, 6.5, 5.5)]


def test_synthesize_tail_without_partner_takes_the_direct_candidate():
    """Baseline: with no partner supplied the first clear candidate wins."""
    dpr = _diffpair_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    tail = dpr._synthesize_tail(_AllClearPathfinder(), head, goal, 0)
    assert tail is not None
    assert len(tail.segments) == 1
    seg = tail.segments[0]
    assert (seg.x1, seg.y1, seg.x2, seg.y2) == (5.0, 5.0, 8.0, 5.0)


def test_synthesize_tail_detours_around_partner_instead_of_giving_up():
    """A partner across the direct corridor selects a later detour candidate.

    Before #4460 the partner screen ran only on the winning candidate, so this
    geometry produced ``None`` and the caller fell through to a partner-blind
    A* tail.  The 20+ U-detour candidates were never consulted.
    """
    dpr = _diffpair_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = _crossing_partner()
    clearance = 0.5
    tail = dpr._synthesize_tail(
        _AllClearPathfinder(),
        head,
        goal,
        0,
        partner_segments=partner,
        partner_clearance=clearance,
    )
    assert tail is not None, "a legal detour exists; the synthesizer must find it"
    assert len(tail.segments) > 1, "the direct candidate crosses the partner"
    for seg in tail.segments:
        assert (
            dpr._min_distance_to_partner(seg.x1, seg.y1, seg.x2, seg.y2, partner, seg.layer)
            >= clearance - 1e-9
        )
    # Endpoints are still exactly head -> goal (the tail must land on the pad).
    assert (tail.segments[0].x1, tail.segments[0].y1) == (5.0, 5.0)
    assert (tail.segments[-1].x2, tail.segments[-1].y2) == (8.0, 5.0)


def test_synthesize_tail_returns_none_when_no_candidate_clears_partner():
    """An unreachable clearance still declines -- the caller then re-anchors."""
    dpr = _diffpair_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = _crossing_partner()
    tail = dpr._synthesize_tail(
        _AllClearPathfinder(),
        head,
        goal,
        0,
        partner_segments=partner,
        partner_clearance=50.0,
    )
    assert tail is None


def _tail_with(y: float, width: float = 0.2) -> Route:
    route = Route(net=2, net_name="USB3_TX1-")
    route.segments.append(
        Segment(x1=5.0, y1=y, x2=8.0, y2=y, width=width, layer=Layer.F_CU, net=2, net_name="N")
    )
    return route


def test_tail_partner_clear_physical_tier_rejects_touching_copper():
    """Tier B (``clearance <= 0``) is the copper-edge bound the gates enforce."""
    dpr = _diffpair_router()
    partner = [_gseg(4.0, 5.15, 9.0, 5.15)]  # both widths 0.2 -> edges meet at 0.2
    assert dpr._tail_partner_clear(_tail_with(5.0), partner, 0.0) is False
    assert dpr._tail_partner_clear(_tail_with(5.4), partner, 0.0) is True


def test_tail_partner_clear_strict_tier_demands_intra_pair_clearance():
    """Tier A additionally demands the coupled centre-to-centre spacing."""
    dpr = _diffpair_router()
    partner = [_gseg(4.0, 5.25, 9.0, 5.25)]
    assert dpr._tail_partner_clear(_tail_with(5.0), partner, 0.0) is True
    assert dpr._tail_partner_clear(_tail_with(5.0), partner, 0.4) is False
    assert dpr._tail_partner_clear(_tail_with(5.0), partner, 0.2) is True


def test_tail_partner_clear_ignores_partner_on_another_layer():
    """Segments on different layers cannot short -- not a tail violation."""
    dpr = _diffpair_router()
    partner = [_gseg(4.0, 5.0, 9.0, 5.0, layer=Layer.B_CU)]
    assert dpr._tail_partner_clear(_tail_with(5.0), partner, 0.4) is True


def test_tail_partner_clear_catches_via_barrel_over_partner():
    """A tail VIA spans every layer, so it must clear partner copper anywhere."""
    dpr = _diffpair_router()
    tail = Route(net=2, net_name="USB3_TX1-")
    tail.vias.append(
        Via(x=6.0, y=5.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=2)
    )
    partner = [_gseg(4.0, 5.0, 9.0, 5.0, layer=Layer.B_CU)]
    assert dpr._tail_partner_clear(tail, partner, 0.0) is False
    far = [_gseg(4.0, 7.0, 9.0, 7.0, layer=Layer.B_CU)]
    assert dpr._tail_partner_clear(tail, far, 0.0) is True


def test_partner_boost_sites_are_local_spaced_and_bounded():
    """Boost sites cover only the tail corridor, tiled at the clearance pitch."""
    dpr = _diffpair_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = [_gseg(0.0, 5.3, 20.0, 5.3)]
    seg_clear = 0.4
    sites = dpr._partner_boost_sites(head, goal, partner, seg_clear)
    assert sites, "partner copper runs through the corridor -> sites expected"
    pad = 2.0 * seg_clear + 0.5
    for sx, sy in sites:
        assert 5.0 - pad <= sx <= 8.0 + pad
        assert 5.0 - pad <= sy <= 8.0 + pad
    for i, (ax, ay) in enumerate(sites):
        for bx, by in sites[i + 1 :]:
            assert math.hypot(ax - bx, ay - by) >= seg_clear - 1e-9
    assert len(sites) <= 40


def test_partner_boost_sites_empty_without_partner():
    dpr = _diffpair_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    assert dpr._partner_boost_sites(head, goal, [], 0.4) == []
    assert dpr._partner_boost_sites(head, goal, [_gseg(0.0, 5.3, 20.0, 5.3)], 0.0) == []


def _stub_probe(dpr, result):
    """Replace the per-net guide probe, recording how it was called."""
    calls: list[tuple] = []

    def fake(start, end, per_net_timeout=None, avoid_locations=None, avoid_radius_cells=1):
        calls.append((per_net_timeout, avoid_locations, avoid_radius_cells))
        return result

    dpr._single_ended_guide_route = fake  # type: ignore[method-assign]
    return calls


def test_fallback_tail_route_without_partner_is_the_plain_probe():
    """Flag-OFF inertness guard: no partner -> the pre-#4460 call, verbatim.

    ``partner_segments`` is supplied ONLY by the shadow constructor, so with
    ``enable_shadow_construction`` off this is the whole of the fallback path.
    It must issue exactly one unbiased probe and return its result untouched.
    """
    dpr = _diffpair_router()
    probe_route = Route(net=2, net_name="USB3_TX1-")
    probe_route.segments.append(_gseg(5.0, 5.0, 8.0, 5.0))
    calls = _stub_probe(dpr, probe_route)
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    assert dpr._fallback_tail_route(head, goal, None, 0.0) is probe_route
    assert calls == [(10.0, None, 1)]


def test_fallback_tail_route_keeps_a_partner_clean_probe_unchanged():
    """A tail that already holds the coupled clearance is returned as-is."""
    dpr = _diffpair_router()
    probe_route = Route(net=2, net_name="USB3_TX1-")
    probe_route.segments.append(_gseg(5.0, 5.0, 8.0, 5.0))
    calls = _stub_probe(dpr, probe_route)
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = [_gseg(4.0, 6.0, 9.0, 6.0)]
    assert dpr._fallback_tail_route(head, goal, partner, 0.4) is probe_route
    assert len(calls) == 1, "a clean probe must not trigger the biased re-route"


def test_fallback_tail_route_discards_a_tail_drawn_on_the_partner():
    """The measured board-06 failure: an A* tail on top of the guide.

    Before #4460 this copper was emitted and the whole shadow side was then
    declined by the self-check.  Now it is discarded, so the constructor's
    anchor-stepping loop gets to retry from a deeper body anchor.
    """
    dpr = _diffpair_router()
    probe_route = Route(net=2, net_name="USB3_TX1-")
    probe_route.segments.append(_gseg(5.0, 5.0, 8.0, 5.0))
    calls = _stub_probe(dpr, probe_route)
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = [_gseg(4.0, 5.02, 9.0, 5.02)]  # centre-to-centre 0.02mm
    assert dpr._fallback_tail_route(head, goal, partner, 0.4) is None
    assert len(calls) == 2, "an overlapping probe must trigger the biased re-route"
    assert calls[1][1], "the retry must carry partner boost sites"


# ---------------------------------------------------------------------------
# Issue #4553: construction-time length symmetry primitives
# ---------------------------------------------------------------------------
#
# Measured on board-06 (shadow-ON, seed 42) the constructed shadow is ALWAYS
# the LONGER leg -- ``shadow - guide`` decomposes as parallel-offset excess
# (+0.7..2.1mm) + landing tails (1.0..9.8mm) - body trims (0..5.2mm) + the
# dogleg quantize tax (0.0..0.36mm, under 1%).  Two construction-time
# primitives close that gap; both are pure geometry and tested here without a
# board route.


def _pts(*xy):
    return [(float(x), float(y)) for x, y in xy]


def _staircase(steps: int, step: float = 0.05, run: int = 1):
    """A per-cell staircase -- exactly what the C++ per-net A* emits.

    ``run`` axis cells then one diagonal cell, repeated.  ``run=1`` is the
    pathological 2:1 slope (26.6 degrees, which no 45-legal polyline can
    follow closely); larger ``run`` values are the shallow drifts real escape
    routes make.
    """
    pts = [(0.0, 0.0)]
    for _ in range(steps):
        for _ in range(run):
            x, y = pts[-1]
            pts.append((x + step, y))
        x, y = pts[-1]
        pts.append((x + step, y + step))
    return pts


def test_simplify_45_polyline_preserves_endpoints_and_45_legality():
    from kicad_tools.router.diffpair_routing import simplify_45_polyline
    from kicad_tools.router.quantize import verify_segment_45

    pts = _staircase(40)
    out = simplify_45_polyline(pts, max_deviation=0.3)
    assert out[0] == pts[0]
    assert out[-1] == pts[-1]
    for a, b in zip(out, out[1:], strict=False):
        verify_segment_45(
            round(a[0], 4), round(a[1], 4), round(b[0], 4), round(b[1], 4), strict=True
        )


def test_simplify_45_polyline_creates_long_straight_runs():
    """The AC's structural requirement: a run the serpentine tuner can use.

    The raw staircase's longest collinear run is one grid cell (0.05mm); after
    compression a single diagonal + axis pair spans the whole run, which is far
    above ``SerpentineConfig.min_segment_length`` (2.0mm).
    """
    from kicad_tools.router.diffpair_routing import simplify_45_polyline

    pts = _staircase(20, run=8)  # a shallow eastward drift, ~9mm long
    raw_longest = max(
        math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:], strict=False)
    )
    assert raw_longest < 0.08, "the emitted path is one segment per grid cell"
    out = simplify_45_polyline(pts, max_deviation=0.75)
    longest = max(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(out, out[1:], strict=False))
    assert longest >= 2.0
    assert len(out) < len(pts)


def test_simplify_45_polyline_deviation_bound_caps_an_off_45_slope():
    """A 26.6-degree path cannot be followed by 45-legal copper.

    The bound is what keeps compression honest: the run it can compress is
    limited by how far the shortest 45-legal chord strays from the corridor
    the router chose, so an off-45 slope compresses only a little.
    """
    from kicad_tools.router.diffpair_routing import _max_deviation, simplify_45_polyline

    pts = _staircase(60, run=1)
    out = simplify_45_polyline(pts, max_deviation=0.3)
    assert len(out) < len(pts)
    assert _max_deviation(pts, out) <= 0.3 + 1e-9


def test_simplify_45_polyline_never_lengthens():
    from kicad_tools.router.diffpair_routing import _polyline_length, simplify_45_polyline

    pts = _staircase(30)
    out = simplify_45_polyline(pts, max_deviation=0.3)
    assert _polyline_length(out) <= _polyline_length(pts) + 1e-9


def test_simplify_45_polyline_respects_the_deviation_bound():
    """A deviation bound of zero permits only exact (collinear) merges."""
    from kicad_tools.router.diffpair_routing import (
        _max_deviation,
        _polyline_length,
        simplify_45_polyline,
    )

    pts = _staircase(20, run=1)
    out = simplify_45_polyline(pts, max_deviation=0.0)
    assert _max_deviation(pts, out) <= 1e-9
    assert _polyline_length(out) == pytest.approx(_polyline_length(pts))


def test_simplify_45_polyline_skips_a_blocked_shortcut():
    """An obstacle across the compressed chord keeps the original path."""
    from kicad_tools.router.diffpair_routing import simplify_45_polyline

    pts = _staircase(20)

    def blocked(_a, _b):
        return False

    assert simplify_45_polyline(pts, max_deviation=1.0, is_clear=blocked) == pts


def test_simplify_45_polyline_handles_a_single_turn():
    from kicad_tools.router.diffpair_routing import simplify_45_polyline

    # Straight east then straight north -- already minimal, nothing to gain
    # beyond merging, and the corner must survive (a shortcut across it would
    # violate the deviation bound).
    pts = _pts((0, 0), (1, 0), (2, 0), (3, 0), (3, 1), (3, 2), (3, 3))
    out = simplify_45_polyline(pts, max_deviation=0.2)
    assert out[0] == (0.0, 0.0)
    assert out[-1] == (3.0, 3.0)
    assert (3.0, 0.0) in out


def test_lateral_jog_adds_exactly_twice_the_displacement():
    from kicad_tools.router.diffpair_routing import _polyline_length, lateral_jog_polyline

    pts = _pts((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))
    before = _polyline_length(pts)
    out = lateral_jog_polyline(pts, 1, 3, 0.0, 0.5)
    assert _polyline_length(out) == pytest.approx(before + 2 * 0.5)


def test_lateral_jog_preserves_45_legality_on_a_staircase():
    """The property a serpentine cannot offer: works on ANY polyline window.

    Translation preserves each segment's displacement vector, so a 45-legal
    staircase window stays 45-legal; the two connector legs are 45-legal
    because the displacement is itself a grid direction.
    """
    from kicad_tools.router.diffpair_routing import lateral_jog_polyline
    from kicad_tools.router.quantize import verify_segment_45

    pts = _staircase(10)
    out = lateral_jog_polyline(pts, 2, 12, 0.4, 0.4)
    for a, b in zip(out, out[1:], strict=False):
        verify_segment_45(
            round(a[0], 4), round(a[1], 4), round(b[0], 4), round(b[1], 4), strict=True
        )


def test_lateral_jog_endpoints_are_untouched():
    from kicad_tools.router.diffpair_routing import lateral_jog_polyline

    pts = _pts((0, 0), (1, 0), (2, 0), (3, 0))
    out = lateral_jog_polyline(pts, 1, 2, 0.0, -0.3)
    assert out[0] == pts[0]
    assert out[-1] == pts[-1]


def test_lateral_jog_rejects_an_invalid_window():
    from kicad_tools.router.diffpair_routing import lateral_jog_polyline

    pts = _pts((0, 0), (1, 0), (2, 0))
    with pytest.raises(ValueError):
        lateral_jog_polyline(pts, 2, 1, 0.0, 0.2)


def test_route_copper_length_matches_the_polyline():
    from kicad_tools.router.diffpair_routing import _route_copper_length

    route = Route(net=1, net_name="P")
    route.segments.append(_gseg(0.0, 0.0, 3.0, 0.0))
    route.segments.append(_gseg(3.0, 0.0, 3.0, 4.0))
    assert _route_copper_length(route) == pytest.approx(7.0)


def test_simplify_guide_route_compresses_a_staircase_guide():
    """End-to-end on the router object: a staircase guide gains long runs."""
    dpr = _diffpair_router(resolution=0.05)
    pf = _make_pathfinder(grid=dpr.autorouter.grid, rules=dpr.autorouter.rules)
    route = Route(net=1, net_name="P")
    pts = _staircase(20, run=8)
    for a, b in zip(pts, pts[1:], strict=False):
        route.segments.append(_gseg(a[0] + 2.0, a[1] + 2.0, b[0] + 2.0, b[1] + 2.0))
    out = dpr._simplify_guide_route(route, pf)
    assert len(out.segments) < len(route.segments)
    longest = max(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in out.segments)
    assert longest >= 2.0
    # Endpoints preserved exactly -- the guide still lands on its pads.
    assert (out.segments[0].x1, out.segments[0].y1) == (
        route.segments[0].x1,
        route.segments[0].y1,
    )
    assert (out.segments[-1].x2, out.segments[-1].y2) == (
        route.segments[-1].x2,
        route.segments[-1].y2,
    )


def test_meander_lengthens_the_shorter_leg_away_from_its_partner():
    """The construction-time closure: a short leg is grown, not the partner."""
    dpr = _diffpair_router(resolution=0.05)
    pf = _make_pathfinder(grid=dpr.autorouter.grid, rules=dpr.autorouter.rules)
    short = Route(net=1, net_name="P")
    short.segments.append(_gseg(2.0, 5.0, 12.0, 5.0))
    partner = [_gseg(2.0, 5.4, 12.0, 5.4)]
    before = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in short.segments)
    added = dpr._meander_route_to_length(short, partner, pf, 2.0, 0.3)
    after = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in short.segments)
    assert added > 0.0
    assert after == pytest.approx(before + added, abs=1e-6)
    # Every tooth bulges AWAY from the partner (which sits at y = 5.3).
    assert min(min(s.y1, s.y2) for s in short.segments) < 5.0
    assert max(max(s.y1, s.y2) for s in short.segments) <= 5.0 + 1e-9


def test_meander_output_is_census_clean():
    from kicad_tools.router.quantize import verify_segment_45

    dpr = _diffpair_router(resolution=0.05)
    pf = _make_pathfinder(grid=dpr.autorouter.grid, rules=dpr.autorouter.rules)
    short = Route(net=1, net_name="P")
    pts = _staircase(80)
    for a, b in zip(pts, pts[1:], strict=False):
        short.segments.append(_gseg(a[0] + 2.0, a[1] + 5.0, b[0] + 2.0, b[1] + 5.0))
    partner = [_gseg(2.0, 5.4, 12.0, 5.4)]
    added = dpr._meander_route_to_length(short, partner, pf, 1.5, 0.3)
    assert added > 0.0
    for s in short.segments:
        verify_segment_45(
            round(s.x1, 4), round(s.y1, 4), round(s.x2, 4), round(s.y2, 4), strict=True
        )


def test_meander_is_a_no_op_when_nothing_is_owed():
    dpr = _diffpair_router(resolution=0.05)
    pf = _make_pathfinder(grid=dpr.autorouter.grid, rules=dpr.autorouter.rules)
    short = Route(net=1, net_name="P")
    short.segments.append(_gseg(2.0, 5.0, 12.0, 5.0))
    assert dpr._meander_route_to_length(short, [_gseg(2.0, 5.3, 12.0, 5.3)], pf, 0.0, 0.3) == 0.0
    assert len(short.segments) == 1


# ---------------------------------------------------------------------------
# 13. exact foreign-pad clearance gate for constructed copper (issue #4571)
# ---------------------------------------------------------------------------
#
# Every shadow validation gate rasterises against ``_is_cell_blocked``, whose
# pad halo is deliberately SHRUNK in fine-pitch corridors on the documented
# promise that "full manufacturer clearance is validated in post-routing DRC".
# For a diff-pair net that promise is never kept: the single-ended finalization
# backstop only demotes deficits larger than one grid resolution, and
# ``drc_verify_and_nudge`` (which would mop up the sub-resolution remainder)
# unconditionally SKIPS coupled nets (#3508 -- the nudge helpers are not
# partner-aware).  The constructor's own self-checks compare copper to copper
# only, never copper to PAD.  Measured consequence on board-06 shadow-ON:
# MIPI_CLK- copper over the MIPI_D0+/MIPI_D0- pads (0.037 mm) and over its own
# partner MIPI_CLK+'s pads (0.015 mm) -- real shorts, all BELOW the 0.05 mm
# floor the single-ended backstop would have caught.
#
# These tests pin the gate at the primitive level: sub-resolution grazes are
# rejected, own-net pads stay legal (a tail must be able to land), and the
# #3545 same-component carve-out can never re-exempt the PARTNER's pad on a
# shared fine-pitch connector ref.


def _pad_gate_router(resolution: float = 0.05):
    from kicad_tools.router.core import Autorouter

    rules = DesignRules()
    rules.grid_resolution = resolution
    router = Autorouter(width=20.0, height=10.0, rules=rules)
    return router._diffpair


def _pad_at(x, y, net, name, ref="J1", pin="1", size=0.3):
    from kicad_tools.router.primitives import Pad

    return Pad(
        x=x,
        y=y,
        width=size,
        height=size,
        layer=Layer.F_CU,
        net=net,
        net_name=name,
        ref=ref,
        pin=pin,
    )


# Geometry used throughout: a 0.3 x 0.3 pad reads as a 0.15 mm radius disc, a
# 0.2 mm trace contributes 0.1 mm of half-width, and the default manufacturer
# clearance is 0.2 mm.  A centreline 0.42 mm from the pad centre therefore has
# 0.17 mm of edge clearance -- a 0.03 mm deficit, i.e. BELOW the 0.05 mm grid
# resolution the single-ended backstop uses as its floor.
_GRAZE_DY = 0.42
_GRAZE_DEFICIT = 0.03


def test_span_pad_clear_rejects_a_subresolution_foreign_pad_graze():
    dpr = _pad_gate_router()
    dpr.autorouter.grid.add_pad(_pad_at(5.0, 5.0, net=99, name="MIPI_D0+"))

    deficit = dpr._span_pad_deficit(3.0, 5.0 + _GRAZE_DY, 7.0, 5.0 + _GRAZE_DY, 0, 7, 0.2)
    assert deficit == pytest.approx(_GRAZE_DEFICIT, abs=1e-9)
    # The whole point of the gate: this is smaller than one grid cell, so the
    # single-ended finalization backstop's ``nudge_reach`` floor would let it
    # ship, and ``drc_verify_and_nudge`` never runs on a coupled net.
    assert deficit < dpr.autorouter.grid.resolution
    assert dpr._span_pad_clear(3.0, 5.0 + _GRAZE_DY, 7.0, 5.0 + _GRAZE_DY, 0, 7, 0.2) is False


def test_span_pad_clear_accepts_copper_outside_the_halo():
    dpr = _pad_gate_router()
    dpr.autorouter.grid.add_pad(_pad_at(5.0, 5.0, net=99, name="MIPI_D0+"))

    assert dpr._span_pad_clear(3.0, 6.5, 7.0, 6.5, 0, 7, 0.2) is True


def test_span_pad_clear_ignores_the_segments_own_net_pad():
    """A landing tail MUST be able to reach (and sit on) its own pad."""
    dpr = _pad_gate_router()
    dpr.autorouter.grid.add_pad(_pad_at(5.0, 5.0, net=7, name="MIPI_CLK-"))

    # Straight through the middle of its own pad: legal, by definition.
    assert dpr._span_pad_clear(3.0, 5.0, 7.0, 5.0, 0, 7, 0.2) is True


def test_span_pad_clear_flags_a_pad_on_a_different_layer_only_when_shared():
    dpr = _pad_gate_router()
    dpr.autorouter.grid.add_pad(_pad_at(5.0, 5.0, net=99, name="MIPI_D0+"))

    b_cu = dpr.autorouter.grid.layer_to_index(Layer.B_CU.value)
    # Same geometry on the opposite layer: an SMD pad cannot be violated.
    assert dpr._span_pad_clear(3.0, 5.0, 7.0, 5.0, b_cu, 7, 0.2) is True


def test_partner_pad_on_a_shared_connector_ref_is_not_carveout_exempt():
    """The #3545 same-component carve-out must never hide a PARTNER pad.

    Diff-pair P and N legs routinely land on the same fine-pitch connector
    ``ref`` (board-06's FFC carries both MIPI_CLK+ and MIPI_CLK-).  Reusing
    ``worst_segment_pad_deficit`` the way the single-ended backstop does --
    with ``exclude_refs`` set to the net's own component refs -- hands the
    partner's pad straight to the carve-out and silences exactly the
    intra-pair overlap this gate exists to catch.
    """
    dpr = _pad_gate_router()
    grid = dpr.autorouter.grid
    # Both pair legs on one fine-pitch (0.4 mm pitch) connector ref.
    grid.add_pad(_pad_at(5.0, 5.0, net=8, name="MIPI_CLK+", ref="J1", pin="1"))
    grid.add_pad(_pad_at(5.4, 5.0, net=7, name="MIPI_CLK-", ref="J1", pin="2"))
    assert grid._component_is_fine_pitch("J1") is True

    seg = Segment(
        x1=3.0,
        y1=5.0 + _GRAZE_DY,
        x2=7.0,
        y2=5.0 + _GRAZE_DY,
        width=0.2,
        layer=Layer.F_CU,
        net=7,
        net_name="MIPI_CLK-",
    )
    # The single-ended backstop's call shape: the carve-out silences it.
    exempted, _ = grid.worst_segment_pad_deficit(seg, exclude_net=7, exclude_refs={"J1"})
    assert exempted == 0.0
    # The constructor's gate passes ``exclude_net`` ONLY -- the partner's pad
    # (and MIPI_CLK-'s own pad, which is same-net and correctly skipped) is
    # measured exactly.
    assert dpr._span_pad_deficit(seg.x1, seg.y1, seg.x2, seg.y2, 0, 7, 0.2) == pytest.approx(
        _GRAZE_DEFICIT, abs=1e-9
    )
    assert dpr._segment_pad_clear(seg) is False


def test_route_pad_violation_reports_the_worst_segment_deficit():
    dpr = _pad_gate_router()
    dpr.autorouter.grid.add_pad(_pad_at(5.0, 5.0, net=99, name="MIPI_D0+"))

    route = Route(net=7, net_name="MIPI_CLK-")
    route.segments.append(
        Segment(
            x1=3.0,
            y1=5.0 + _GRAZE_DY,
            x2=7.0,
            y2=5.0 + _GRAZE_DY,
            width=0.2,
            layer=Layer.F_CU,
            net=7,
            net_name="MIPI_CLK-",
        )
    )
    deficit, loc = dpr._route_pad_violation(route)
    assert deficit == pytest.approx(_GRAZE_DEFICIT, abs=1e-9)
    assert loc == (5.0, 5.0)


def test_route_pad_violation_covers_the_via_quadrant():
    """A shadow via barrel inside a foreign pad halo is a violation too."""
    dpr = _pad_gate_router()
    rules = dpr.autorouter.rules
    dpr.autorouter.grid.add_pad(_pad_at(5.0, 5.0, net=99, name="MIPI_D0+"))

    clean = Route(net=7, net_name="MIPI_CLK-")
    clean.vias.append(
        Via(
            x=8.0,
            y=5.0,
            drill=rules.via_drill,
            diameter=rules.via_diameter,
            layers=(Layer.F_CU, Layer.B_CU),
            net=7,
            net_name="MIPI_CLK-",
        )
    )
    assert dpr._route_pad_violation(clean)[0] == 0.0

    grazing = Route(net=7, net_name="MIPI_CLK-")
    grazing.vias.append(
        Via(
            x=5.0,
            y=5.0 + 0.15 + rules.via_diameter / 2 + 0.15,
            drill=rules.via_drill,
            diameter=rules.via_diameter,
            layers=(Layer.F_CU, Layer.B_CU),
            net=7,
            net_name="MIPI_CLK-",
        )
    )
    deficit, loc = dpr._route_pad_violation(grazing)
    assert deficit == pytest.approx(0.05, abs=1e-9)
    assert loc == (5.0, 5.0)


def test_pad_deficit_arcs_localise_the_violating_end_of_a_span():
    """Violations are reported as ARCS so the end-trim can shave them.

    A body segment that only grazes a connector pad at one END must not fail
    the whole side -- the existing trim machinery consumes the offending arc
    into the landing tail, exactly as it does for raster blockages.
    """
    dpr = _pad_gate_router()
    dpr.autorouter.grid.add_pad(_pad_at(2.5, 5.0, net=99, name="MIPI_D0+"))

    arcs = dpr._pad_deficit_arcs(2.0, 5.0 + _GRAZE_DY, 12.0, 5.0 + _GRAZE_DY, 0, 7, 0.2, 0.0)
    assert arcs, "the span grazes the pad; the scan must report it"
    # 10 mm span, pad at its head: every offending arc sits in the first 20%.
    assert max(arcs) < 2.0


def test_pad_deficit_arcs_is_empty_for_a_clean_span():
    dpr = _pad_gate_router()
    dpr.autorouter.grid.add_pad(_pad_at(2.5, 5.0, net=99, name="MIPI_D0+"))

    assert dpr._pad_deficit_arcs(2.0, 7.0, 12.0, 7.0, 0, 7, 0.2, 0.0) == []


def test_synthesize_tail_detours_around_a_foreign_pad_the_raster_cannot_see():
    """The board-06 shape: a raster-clear landing tail drawn over a pad.

    ``_AllClearPathfinder`` never blocks a cell, standing in for the
    fine-pitch corridor where the grid halo is shrunk to ``min_trace_width /
    2``.  Before #4571 the direct candidate won and shipped a pad short; now
    the pad screen runs INSIDE the candidate loop, so a later detour wins.
    """
    dpr = _pad_gate_router()
    dpr.autorouter.grid.add_pad(_pad_at(6.5, 5.0, net=99, name="MIPI_D0+"))
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))

    tail = dpr._synthesize_tail(_AllClearPathfinder(), head, goal, 0)

    assert tail is not None, "a legal detour exists; the synthesizer must find it"
    assert len(tail.segments) > 1, "the direct candidate runs straight over the pad"
    for seg in tail.segments:
        assert dpr._segment_pad_clear(seg)
    # Still lands exactly on the pads it was asked to connect.
    assert (tail.segments[0].x1, tail.segments[0].y1) == (5.0, 5.0)
    assert (tail.segments[-1].x2, tail.segments[-1].y2) == (8.0, 5.0)


def test_synthesize_tail_is_unchanged_when_no_pad_is_near():
    """No pad in reach -> the direct candidate still wins (byte-identical)."""
    dpr = _pad_gate_router()
    dpr.autorouter.grid.add_pad(_pad_at(6.5, 9.0, net=99, name="MIPI_D0+"))
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))

    tail = dpr._synthesize_tail(_AllClearPathfinder(), head, goal, 0)

    assert tail is not None
    assert len(tail.segments) == 1
    seg = tail.segments[0]
    assert (seg.x1, seg.y1, seg.x2, seg.y2) == (5.0, 5.0, 8.0, 5.0)


def test_close_shadow_chain_refuses_a_connector_through_a_pad_halo():
    """Even a sub-cell repair connector is real copper and must clear pads."""
    dpr, pf = _chain_router_and_pathfinder()
    dpr.autorouter.grid.add_pad(_pad_at(4.02, 2.0, net=99, name="MIPI_D0+"))
    route = _chain_route([(2.0, 2.0), (4.0, 2.0)])
    route.segments.append(
        Segment(
            x1=4.0,
            y1=2.024,
            x2=6.0,
            y2=2.024,
            width=0.1,
            layer=Layer.F_CU,
            net=7,
            net_name="MIPI_CLK-",
        )
    )

    assert dpr._close_shadow_chain(route, pf) == 0
    assert len(route.segments) == 2


# ---------------------------------------------------------------------------
# 14. exact foreign-VIA clearance gate for constructed copper (issue #4575)
# ---------------------------------------------------------------------------
#
# The quadrant adjacent to #4571's.  The constructor's only via-aware copper
# self-check is ``_pair_has_physical_overlap``, and it is an OVERLAP detector:
# ``via.diameter/2 + seg.width/2``, no clearance term.  Worse, the partner
# universe every tail screen receives is ``list(guide.segments)`` -- the
# guide's VIAS are not in it at all.  So a constructed leg passing 0.090 mm
# from the partner leg's barrel is accepted by every construction-time gate and
# then reported by ``clearance_segment_via`` at the 0.102 mm board minimum
# (measured on board-06 shadow-ON seed 42: USB3_TX1+ copper vs a USB3_TX1-
# barrel).  A diff-pair net is excluded from ``drc_verify_and_nudge`` (#3508),
# so nothing downstream repairs it.
#
# THE VACUITY TRAP these tests pin: ``ClearanceRule`` exempts a declared diff
# pair from the generic clearance check ONLY when both elements are SEGMENTS,
# so a segment-vs-via pair between P and N is checked at the FULL board
# minimum.  A gate built on the intra-pair relaxation would be strictly looser
# than the checker and could never fire on the finding it exists to close.


def _via_gate_via(x, y, net, name, diameter=0.7, layers=(Layer.F_CU, Layer.B_CU)):
    from kicad_tools.router.primitives import Via

    return Via(
        x=x,
        y=y,
        drill=0.35,
        diameter=diameter,
        layers=layers,
        net=net,
        net_name=name,
    )


def _via_gate_seg(x1, y1, x2, y2, net=7, name="USB3_TX1+", width=0.2, layer=Layer.F_CU):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=width, layer=layer, net=net, net_name=name)


# Geometry used throughout this section.  A 0.7 mm via reads as a 0.35 mm
# barrel radius, a 0.2 mm trace contributes 0.1 mm of half-width, and the
# default manufacturer clearance is 0.2 mm.  So:
#   * copper physically INTERSECTS below 0.45 mm centre-to-centre, and
#   * the DRC requires 0.65 mm.
# A centreline 0.55 mm away is therefore NON-overlapping (the overlap gate
# accepts it) yet 0.10 mm short of clearance -- the exact regime the board-06
# 0.090 mm finding lives in.
_BARREL_GAP = 0.55
_BARREL_DEFICIT = 0.10
_BARREL_OVERLAP_BOUND = 0.45


def test_segment_via_gate_rejects_a_subclearance_nonoverlapping_barrel_gap():
    """AC-5: the gate fires exactly where the overlap detector cannot.

    The two legs are a declared diff pair, the barrel belongs to the PARTNER,
    and the copper does not intersect -- so ``_pair_has_physical_overlap``
    (the only pre-#4575 via-aware self-check) accepts it.  The exact
    ``clearance_segment_via`` predicate does not.
    """
    from kicad_tools.router.via_clearance import segment_clears_foreign_via

    dpr = _pad_gate_router()
    rules = dpr.autorouter.rules

    partner = Route(net=8, net_name="USB3_TX1-")
    partner.vias.append(_via_gate_via(5.0, 5.0, net=8, name="USB3_TX1-"))
    shadow = Route(net=7, net_name="USB3_TX1+")
    seg = _via_gate_seg(3.0, 5.0 + _BARREL_GAP, 7.0, 5.0 + _BARREL_GAP)
    shadow.segments.append(seg)

    # Sanity: this is the non-overlapping-but-sub-clearance regime.
    assert _BARREL_OVERLAP_BOUND < _BARREL_GAP < _BARREL_OVERLAP_BOUND + rules.trace_clearance
    assert dpr._pair_has_physical_overlap(partner, shadow) is False
    assert segment_clears_foreign_via(seg, partner.vias[0], rules.trace_clearance) is False

    with dpr._shadow_foreign_copper(partner):
        deficit, loc = dpr._route_via_violation(shadow)
    assert deficit == pytest.approx(_BARREL_DEFICIT, abs=1e-9)
    assert loc == (5.0, 5.0)


def test_via_gate_threshold_is_the_inter_net_clearance_not_the_intra_pair_one():
    """AC-5 (the vacuity trap), stated as arithmetic.

    ``DiffPairClearanceIntraRule``'s per-class threshold is deliberately
    TIGHTER than the manufacturer clearance, and ``ClearanceRule``'s diff-pair
    exemption covers segment-to-SEGMENT edges only.  Measuring this quadrant
    against the intra-pair bound would therefore produce a gate that passes
    the very geometry the DRC reports.
    """
    from kicad_tools.router.via_clearance import segment_via_deficit

    dpr = _pad_gate_router()
    rules = dpr.autorouter.rules
    via = _via_gate_via(5.0, 5.0, net=8, name="USB3_TX1-")
    seg = _via_gate_seg(3.0, 5.0 + _BARREL_GAP, 7.0, 5.0 + _BARREL_GAP)

    intra_pair = 0.08  # a representative per-class intra-pair clearance
    assert intra_pair < rules.trace_clearance
    # The trap: at the relaxed bound this geometry reads CLEAN ...
    assert segment_via_deficit(seg, via, intra_pair) < 0.0
    # ... while the threshold the checker actually applies reports the deficit.
    assert segment_via_deficit(seg, via, rules.trace_clearance) == pytest.approx(
        _BARREL_DEFICIT, abs=1e-9
    )


def test_segment_via_deficit_agrees_with_the_boolean_predicate():
    """Drift guard: the gate and ``segment_clears_foreign_via`` are one formula."""
    from kicad_tools.router.via_clearance import segment_clears_foreign_via, segment_via_deficit

    via = _via_gate_via(5.0, 5.0, net=8, name="USB3_TX1-")
    for dy in (0.30, 0.44, 0.45, 0.55, 0.6499, 0.65, 0.80):
        seg = _via_gate_seg(3.0, 5.0 + dy, 7.0, 5.0 + dy)
        clear = segment_clears_foreign_via(seg, via, 0.2)
        assert clear == (segment_via_deficit(seg, via, 0.2) <= 1e-9), dy


def test_via_gate_ignores_the_segments_own_net_via():
    """A tail MUST still be able to land on (and run into) its own net's via."""
    dpr = _pad_gate_router()

    own = Route(net=7, net_name="USB3_TX1+")
    own.vias.append(_via_gate_via(5.0, 5.0, net=7, name="USB3_TX1+"))
    shadow = Route(net=7, net_name="USB3_TX1+")
    shadow.segments.append(_via_gate_seg(3.0, 5.0, 7.0, 5.0))  # straight through it

    with dpr._shadow_foreign_copper(own):
        assert dpr._route_via_violation(shadow)[0] == 0.0


def test_partner_via_on_a_shared_connector_ref_is_still_measured():
    """AC-6: ``exclude_net`` only -- no ``exclude_refs`` carve-out (#3545).

    Diff-pair P and N legs routinely land on the same fine-pitch connector
    ``ref`` (board-06's FFC carries both MIPI_CLK+ and MIPI_CLK-).  The #3545
    same-component carve-out that the single-ended backstop applies would
    exempt the partner's copper on exactly the connectors this gate exists
    for, so the gate must never consult a ref at all.
    """
    dpr = _pad_gate_router()
    grid = dpr.autorouter.grid
    grid.add_pad(_pad_at(5.0, 5.0, net=8, name="MIPI_CLK+", ref="J1", pin="1"))
    grid.add_pad(_pad_at(5.4, 5.0, net=7, name="MIPI_CLK-", ref="J1", pin="2"))
    assert grid._component_is_fine_pitch("J1") is True

    partner = Route(net=8, net_name="MIPI_CLK+")
    partner.vias.append(_via_gate_via(5.0, 5.0, net=8, name="MIPI_CLK+"))
    shadow = Route(net=7, net_name="MIPI_CLK-")
    shadow.segments.append(
        _via_gate_seg(3.0, 5.0 + _BARREL_GAP, 7.0, 5.0 + _BARREL_GAP, net=7, name="MIPI_CLK-")
    )

    with dpr._shadow_foreign_copper(partner):
        assert dpr._route_via_violation(shadow)[0] == pytest.approx(_BARREL_DEFICIT, abs=1e-9)


def test_segment_endpoint_colocated_with_a_foreign_via_is_not_flagged():
    """AC-7: mirror ``ClearanceRule``'s #2706 in-pad-escape carve-out.

    The router's in-pad escape places segment endpoints EXACTLY at via
    centres, and the DRC skips such pairs.  A gate without the carve-out
    would be STRICTER than the checker and decline sides over geometry that
    is never reported -- pure reach loss for zero DRC gain.
    """
    dpr = _pad_gate_router()

    foreign = Route(net=99, net_name="OTHER")
    foreign.vias.append(_via_gate_via(5.0, 5.0, net=99, name="OTHER"))
    shadow = Route(net=7, net_name="USB3_TX1+")
    shadow.segments.append(_via_gate_seg(5.0, 5.0, 8.0, 5.0))  # endpoint ON the via centre

    with dpr._shadow_foreign_copper(foreign):
        assert dpr._route_via_violation(shadow)[0] == 0.0

    # One quantum away from the carve-out epsilon the copper IS measured.
    moved = Route(net=7, net_name="USB3_TX1+")
    moved.segments.append(_via_gate_seg(5.01, 5.0, 8.0, 5.0))
    with dpr._shadow_foreign_copper(foreign):
        assert dpr._route_via_violation(moved)[0] > 0.0


def test_blind_via_that_does_not_span_the_segments_layer_is_not_flagged():
    """Layer-span awareness: a barrel is only copper where it actually runs."""
    dpr = _pad_gate_router()

    shadow = Route(net=7, net_name="USB3_TX1+")
    shadow.segments.append(
        _via_gate_seg(3.0, 5.0 + _BARREL_GAP, 7.0, 5.0 + _BARREL_GAP, layer=Layer.F_CU)
    )

    blind = Route(net=99, net_name="OTHER")
    blind.vias.append(
        _via_gate_via(5.0, 5.0, net=99, name="OTHER", layers=(Layer.B_CU, Layer.B_CU))
    )
    with dpr._shadow_foreign_copper(blind):
        assert dpr._route_via_violation(shadow)[0] == 0.0

    # Positive control: the SAME site as a through via does span F.Cu.
    through = Route(net=99, net_name="OTHER")
    through.vias.append(_via_gate_via(5.0, 5.0, net=99, name="OTHER"))
    with dpr._shadow_foreign_copper(through):
        assert dpr._route_via_violation(shadow)[0] == pytest.approx(_BARREL_DEFICIT, abs=1e-9)


def test_via_gate_measures_the_via_vs_via_quadrant_too():
    """A constructed BARREL must clear a foreign barrel at ``via_clearance``."""
    dpr = _pad_gate_router()
    rules = dpr.autorouter.rules
    bound = rules.via_diameter + rules.via_clearance  # centre-to-centre

    foreign = Route(net=99, net_name="OTHER")
    foreign.vias.append(_via_gate_via(5.0, 5.0, net=99, name="OTHER"))

    clean = Route(net=7, net_name="USB3_TX1+")
    clean.vias.append(_via_gate_via(5.0 + bound, 5.0, net=7, name="USB3_TX1+"))
    grazing = Route(net=7, net_name="USB3_TX1+")
    grazing.vias.append(_via_gate_via(5.0 + bound - 0.05, 5.0, net=7, name="USB3_TX1+"))

    with dpr._shadow_foreign_copper(foreign):
        assert dpr._route_via_violation(clean)[0] == pytest.approx(0.0, abs=1e-9)
        deficit, loc = dpr._route_via_violation(grazing)
    assert deficit == pytest.approx(0.05, abs=1e-9)
    assert loc == (5.0, 5.0)


def test_via_gate_measures_a_constructed_barrel_against_foreign_segments():
    """The mirror direction: shadow BARREL vs the partner's trace."""
    dpr = _pad_gate_router()
    rules = dpr.autorouter.rules
    bound = rules.via_diameter / 2 + 0.1 + rules.via_clearance

    partner = Route(net=8, net_name="USB3_TX1-")
    partner.segments.append(_via_gate_seg(0.0, 5.0, 10.0, 5.0, net=8, name="USB3_TX1-"))
    shadow = Route(net=7, net_name="USB3_TX1+")
    shadow.vias.append(_via_gate_via(5.0, 5.0 + bound - 0.05, net=7, name="USB3_TX1+"))

    with dpr._shadow_foreign_copper(partner):
        assert dpr._route_via_violation(shadow)[0] == pytest.approx(0.05, abs=1e-9)


def test_via_gate_is_a_no_op_when_disarmed():
    """AC-8: outside a ``_shadow_route_pair`` call the gate does not exist.

    This is what keeps the shadow-construction-OFF path (and every ordinary,
    non-shadow ``_tail_route`` call) byte-identical.
    """
    dpr = _pad_gate_router()
    shadow = Route(net=7, net_name="USB3_TX1+")
    shadow.segments.append(_via_gate_seg(3.0, 5.0, 7.0, 5.0))
    shadow.vias.append(_via_gate_via(5.0, 5.0, net=7, name="USB3_TX1+"))

    partner = Route(net=8, net_name="USB3_TX1-")
    partner.vias.append(_via_gate_via(5.0, 5.0 + _BARREL_GAP, net=8, name="USB3_TX1-"))

    assert dpr._shadow_foreign_universe is None
    assert dpr._route_via_violation(shadow) == (0.0, None)
    assert dpr._span_via_clear(3.0, 5.0, 7.0, 5.0, 0, 7, 0.2) is True
    # ... and it is restored to inert after an armed block, including nesting.
    with dpr._shadow_foreign_copper(partner):
        assert dpr._route_via_violation(shadow)[0] > 0.0
        with dpr._shadow_foreign_copper(Route(net=8, net_name="P")):
            assert dpr._route_via_violation(shadow) == (0.0, None)
        assert dpr._route_via_violation(shadow)[0] > 0.0
    assert dpr._shadow_foreign_universe is None
    assert dpr._route_via_violation(shadow) == (0.0, None)


def test_sibling_leg_copper_is_only_visible_inside_the_extended_universe():
    """The board-06 residual's actual shape: LATE guide-leg copper.

    The #4553 length matcher and the #4570 via mirror both mutate ONE leg
    after the other leg is already built, and that sibling leg is in neither
    the committed route list nor the pre-assembly guide.  So the ambient
    universe cannot see its barrels; ``_shadow_foreign_copper_extended`` is
    what makes the meander-tooth / z-jog screens see them.
    """
    dpr = _pad_gate_router()

    guide = Route(net=8, net_name="USB3_TX1-")
    guide.segments.append(_via_gate_seg(0.0, 5.0, 10.0, 5.0, net=8, name="USB3_TX1-"))
    sibling = Route(net=8, net_name="USB3_TX1-")
    sibling.vias.append(_via_gate_via(5.0, 5.0 + _BARREL_GAP, net=8, name="USB3_TX1-"))
    tooth = Route(net=7, net_name="USB3_TX1+")
    tooth.segments.append(_via_gate_seg(3.0, 5.0 + 2 * _BARREL_GAP, 7.0, 5.0 + 2 * _BARREL_GAP))

    with dpr._shadow_foreign_copper(guide):
        # Blind: the sibling leg's barrel is nowhere in the universe.
        assert dpr._route_via_violation(tooth)[0] == 0.0
        with dpr._shadow_foreign_copper_extended(sibling):
            assert dpr._route_via_violation(tooth)[0] == pytest.approx(_BARREL_DEFICIT, abs=1e-9)
        # ... and the widening is scoped.
        assert dpr._route_via_violation(tooth)[0] == 0.0
    # Disarmed, the extension is a no-op rather than an implicit arming.
    with dpr._shadow_foreign_copper_extended(sibling):
        assert dpr._shadow_foreign_universe is None
        assert dpr._route_via_violation(tooth)[0] == 0.0


def test_pair_with_no_vias_anywhere_is_completely_unaffected():
    """The gate is a no-op for a via-free pair -- no cost, no behaviour change."""
    dpr = _pad_gate_router()
    partner = Route(net=8, net_name="USB3_TX1-")
    partner.segments.append(_via_gate_seg(0.0, 5.0, 10.0, 5.0, net=8, name="USB3_TX1-"))
    shadow = Route(net=7, net_name="USB3_TX1+")
    shadow.segments.append(_via_gate_seg(0.0, 5.2, 10.0, 5.2))

    with dpr._shadow_foreign_copper(partner):
        assert dpr._route_via_violation(shadow) == (0.0, None)


def test_synthesize_tail_detours_around_a_partner_via_the_raster_cannot_see():
    """The REPAIR half of the fix: a grazing candidate loses to a later one.

    The partner guide is never in the routing grid, so ``_AllClearPathfinder``
    (which blocks nothing) is a faithful stand-in.  With the gate disarmed the
    direct candidate wins and ships copper straight through the partner's
    barrel; with it armed the synthesizer detours instead of declining.
    """
    dpr = _pad_gate_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = Route(net=99, net_name="OTHER")
    partner.vias.append(_via_gate_via(6.5, 5.0, net=99, name="OTHER"))

    # Disarmed: unchanged pre-#4575 behaviour -- the direct candidate wins.
    plain = dpr._synthesize_tail(_AllClearPathfinder(), head, goal, 0)
    assert plain is not None and len(plain.segments) == 1

    with dpr._shadow_foreign_copper(partner):
        tail = dpr._synthesize_tail(_AllClearPathfinder(), head, goal, 0)
        assert tail is not None, "a legal detour exists; the synthesizer must find it"
        assert len(tail.segments) > 1, "the direct candidate runs through the barrel"
        assert dpr._route_via_violation(tail)[0] == 0.0
    # Still lands exactly on the pads it was asked to connect.
    assert (tail.segments[0].x1, tail.segments[0].y1) == (5.0, 5.0)
    assert (tail.segments[-1].x2, tail.segments[-1].y2) == (8.0, 5.0)


def test_shadow_pair_declines_a_side_on_foreign_via_clearance(monkeypatch):
    """The FAILOVER half: a violating side loses to the other offset side.

    The guide (partner) carries a barrel 0.55 mm off the ``side=+1`` offset
    line.  It is not in the routing grid (the guide never is, so the
    constructor's raster cannot see it), it is not a pad (#4571's gate cannot
    see it), and at 0.55 mm the copper does not intersect
    (``_pair_has_physical_overlap``'s bound is 0.45 mm, so it cannot see it
    either) -- only the exact ``clearance_segment_via`` predicate can.  The
    side is declined with a distinct reason and ``side=-1`` ships instead.

    The #4570 via-signature gate is switched off for the duration: a
    single-via guide is deliberately asymmetric, and its ``via-skew`` decline
    reason would otherwise mask the one under test.
    """
    import kicad_tools.router.diffpair_routing as dpr_mod
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    monkeypatch.setattr(dpr_mod, "_SHADOW_VIA_SYMMETRY", False, raising=True)

    spacing_cells = 4
    dpr, pair, spec, pathfinder, _ = _shadow_setup(spacing_cells, bend_end=(11.0, 7.7))
    guide = _planar_guide(spec.p_start)  # straight F.Cu run at y = 4.8
    # side=+1 offsets the shadow to y = 5.2; put the partner's barrel in the
    # sub-clearance-but-non-overlapping band above it.
    guide.vias.append(
        _via_gate_via(15.0, 5.2 + _BARREL_GAP, net=spec.p_start.net, name=spec.p_start.net_name)
    )
    monkeypatch.setattr(DiffPairRouter, "_tail_route", _stub_tail_route_planar, raising=True)

    result = dpr._shadow_route_pair(pair, spec, pathfinder, guide, spacing_cells)

    assert result is not None, "the other offset side is legal; the pair must still ship"
    assert dpr._last_shadow_decline_reason == "via-clearance"
    _p_route, n_route = result
    assert n_route.segments, "the shadow leg must carry copper"
    for seg in n_route.segments:
        assert seg.y1 < 4.8 and seg.y2 < 4.8, "the surviving side is the one away from the barrel"


# ---------------------------------------------------------------------------
# Issue #4572: coupling-aware landing-tail synthesis
# ---------------------------------------------------------------------------
# The shadow constructor trims the coupled body at both ends and reconnects to
# the real pads with ``_synthesize_tail``.  Every gate that tail passed was a
# legality FLOOR (raster-clear, ``partner_clearance`` minimum distance, exact
# foreign-pad clearance); NOTHING preferred a tail that ran alongside the
# partner.  So the first legal candidate in the fixed shape enumeration won,
# and its copper was uncoupled -- on BOTH legs, because
# ``diffpair_routing_continuity`` scores a pair as ``(frac_a + frac_b) / 2``
# and the partner copper the tail failed to follow is uncoupled too.
#
# These tests pin the fix at the unit level: the candidate list is EXTENDED
# with partner-anchored detours and ORDERED by the exact coupled fraction the
# DRC rule measures, while every legality gate stays exactly where it was.


def _horizontal_partner(y: float, x1: float = 4.0, x2: float = 9.0) -> list[Segment]:
    """A guide run parallel to the direct head->goal corridor, offset in y."""
    return [_gseg(x1, y, x2, y)]


def _copper_element(x1: float, y1: float, x2: float, y2: float, width: float = 0.2):
    """A DRC-side ``CopperElement`` for the same geometry the router emits.

    ``CopperElement.from_segment`` consumes the SCHEMA's segment type; the
    router's :class:`~kicad_tools.router.primitives.Segment` is a different
    class, so the element is built field-by-field here.
    """
    from kicad_tools.validate.rules.clearance import CopperElement

    return CopperElement(
        element_type="segment",
        layer=Layer.F_CU.value,
        net_number=2,
        geometry=(x1, y1, x2, y2, width),
        reference="Trace",
        net_name="N",
    )


def test_coupling_constants_match_the_drc_rule():
    """Drift guard: the constructor optimizes the rule's EXACT predicate.

    ``diffpair_routing.py`` mirrors these two values rather than importing
    them (``router`` keeps no module-level ``validate`` dependency), so this
    test is the thing that stops the two definitions of "coupled" drifting.
    """
    from kicad_tools.router.diffpair_routing import (
        _COUPLING_PARALLEL_TOL_DEG,
        _COUPLING_WINDOW_MM,
    )
    from kicad_tools.validate.rules.diffpair_routing_continuity import (
        DEFAULT_COUPLING_WINDOW_MM,
        DEFAULT_PARALLEL_TOLERANCE_DEG,
    )

    assert _COUPLING_WINDOW_MM == DEFAULT_COUPLING_WINDOW_MM
    assert _COUPLING_PARALLEL_TOL_DEG == DEFAULT_PARALLEL_TOLERANCE_DEG


def test_spans_coupled_fraction_agrees_with_the_drc_rule():
    """The constructor's scorer and the DRC rule must return the same verdict.

    Same geometry, two independent implementations: the router's span-level
    scorer and ``DiffPairRoutingContinuityRule._coupled_length``.
    """
    from kicad_tools.router.diffpair_routing import _spans_coupled_fraction
    from kicad_tools.validate.rules.diffpair_routing_continuity import (
        DiffPairRoutingContinuityRule,
    )

    partner = _horizontal_partner(6.0)
    rule = DiffPairRoutingContinuityRule()

    # Three own-spans: coupled (0.3 mm edge gap), too far (0.8 mm edge gap),
    # and perpendicular (never coupled regardless of distance).
    cases = [
        ([(5.0, 5.5, 8.0, 5.5)], 1.0),
        ([(5.0, 5.0, 8.0, 5.0)], 0.0),
        ([(5.0, 5.5, 5.0, 4.0)], 0.0),
    ]
    for spans, expected in cases:
        assert _spans_coupled_fraction(spans, 0.2, Layer.F_CU, partner) == pytest.approx(expected)

        own = [_copper_element(x1, y1, x2, y2) for x1, y1, x2, y2 in spans]
        partner_elems = [_copper_element(ps.x1, ps.y1, ps.x2, ps.y2) for ps in partner]
        total = sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in spans)
        assert rule._coupled_length(own, partner_elems) / total == pytest.approx(expected)


def test_synthesize_tail_prefers_the_candidate_that_parallels_the_partner():
    """Two legal candidates; the one inside the coupling window must win.

    ``head -> goal`` is a straight 3 mm corridor at ``y = 5.0`` and the guide
    runs parallel at ``y = 6.0``.  The DIRECT candidate is legal (1.0 mm
    centre-to-centre, well above the 0.4 mm clearance floor) but sits 0.8 mm
    edge-to-edge from the guide -- outside the 0.5 mm coupling window, so the
    continuity rule scores it as 100% UNCOUPLED.  A detour that walks the
    corridor closer to the guide is equally legal and fully coupled.
    """
    from kicad_tools.router.diffpair_routing import _spans_coupled_fraction

    dpr = _diffpair_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = _horizontal_partner(6.0)

    tail = dpr._synthesize_tail(
        _AllClearPathfinder(),
        head,
        goal,
        0,
        partner_segments=partner,
        partner_clearance=0.4,
    )

    assert tail is not None
    spans = [(s.x1, s.y1, s.x2, s.y2) for s in tail.segments]
    # NOT the direct candidate (which the pre-#4572 first-legal-wins order
    # returned) -- it is the uncoupled one.
    assert spans != [(5.0, 5.0, 8.0, 5.0)]
    # The bulk of the tail is copper the continuity rule counts as coupled.
    assert _spans_coupled_fraction(spans, 0.2, Layer.F_CU, partner) > 0.5
    # Legality is untouched: the clearance floor still holds everywhere.
    for seg in tail.segments:
        assert (
            dpr._min_distance_to_partner(seg.x1, seg.y1, seg.x2, seg.y2, partner, seg.layer)
            >= 0.4 - 1e-9
        )
    # ... and the tail still lands exactly on head and goal.
    assert (tail.segments[0].x1, tail.segments[0].y1) == (5.0, 5.0)
    assert (tail.segments[-1].x2, tail.segments[-1].y2) == (8.0, 5.0)


def test_synthesize_tail_keeps_the_direct_candidate_when_nothing_can_couple():
    """No reachable partner copper => the historical shape order is preserved.

    The guide here is 6 mm away: no candidate in the (bounded) detour lattice
    can reach its coupling window, so every candidate scores 0.0 and the
    STABLE sort leaves the enumeration exactly as it was before #4572.
    """
    dpr = _diffpair_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = _horizontal_partner(11.0)

    tail = dpr._synthesize_tail(
        _AllClearPathfinder(),
        head,
        goal,
        0,
        partner_segments=partner,
        partner_clearance=0.4,
    )

    assert tail is not None
    assert len(tail.segments) == 1
    seg = tail.segments[0]
    assert (seg.x1, seg.y1, seg.x2, seg.y2) == (5.0, 5.0, 8.0, 5.0)


def test_coupling_preference_never_bypasses_the_partner_clearance_floor():
    """Preference re-ORDERS candidates; it must never re-GATE them.

    The best-coupled placement here would be a hair off the guide, but the
    clearance floor is 0.9 mm centre-to-centre -- above the 0.7 mm ceiling at
    which copper can still be inside the 0.5 mm coupling window.  Every
    surviving candidate is therefore uncoupled, and the one that ships must
    still respect the floor.
    """
    dpr = _diffpair_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = _horizontal_partner(6.0)

    tail = dpr._synthesize_tail(
        _AllClearPathfinder(),
        head,
        goal,
        0,
        partner_segments=partner,
        partner_clearance=0.9,
    )

    assert tail is not None
    for seg in tail.segments:
        assert (
            dpr._min_distance_to_partner(seg.x1, seg.y1, seg.x2, seg.y2, partner, seg.layer)
            >= 0.9 - 1e-9
        )


def test_coupling_preference_never_bypasses_the_foreign_pad_gate():
    """The best-coupled candidate is a pad short; the gate must still veto it.

    A foreign pad sits directly on the best-coupled corridor.  #4573's exact
    ``clearance_pad_segment`` screen runs INSIDE the candidate loop, so
    re-ordering the loop must not let a violating candidate through: the
    synthesizer has to fall through to the next-best legal one.
    """
    dpr = _pad_gate_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = _horizontal_partner(6.0)
    # Foreign pad centred on the y = 5.6 corridor (the tightest coupled wall).
    dpr.autorouter.grid.add_pad(_pad_at(6.5, 5.6, net=99, name="OTHER"))

    tail = dpr._synthesize_tail(
        _AllClearPathfinder(),
        head,
        goal,
        0,
        partner_segments=partner,
        partner_clearance=0.4,
    )

    assert tail is not None
    for seg in tail.segments:
        assert dpr._segment_pad_clear(seg) is True


def test_partner_parallel_candidates_land_inside_the_coupling_window():
    """Every generated wall is both legal-by-floor and coupled-by-window."""
    from kicad_tools.router.diffpair_routing import (
        _COUPLING_WINDOW_MM,
        _spans_coupled_fraction,
    )

    dpr = _diffpair_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = _horizontal_partner(6.0)
    clearance = 0.4

    cands = dpr._partner_parallel_tail_candidates(head, goal, partner, 0.2, clearance)

    assert cands, "a parallel partner run in reach must produce candidates"
    for spans in cands:
        wall_y = spans[1][1]
        centre = abs(wall_y - 6.0)
        assert centre >= clearance - 1e-9, "below the intra-pair clearance floor"
        assert centre - 0.2 <= _COUPLING_WINDOW_MM + 1e-9, "outside the coupling window"
        # The long run is what the rule scores; it must be genuinely coupled.
        assert _spans_coupled_fraction([spans[1]], 0.2, Layer.F_CU, partner) == pytest.approx(1.0)


def test_partner_parallel_candidates_ignore_an_out_of_reach_partner():
    """A guide beyond the detour envelope contributes no candidates."""
    dpr = _diffpair_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))

    assert dpr._partner_parallel_tail_candidates(head, goal, [], 0.2, 0.4) == []
    assert (
        dpr._partner_parallel_tail_candidates(head, goal, _horizontal_partner(20.0), 0.2, 0.4) == []
    )


# --- Issue #4572: guide-following landing tails -----------------------------
# The axis-aligned repertoire (direct / dogleg / U-detour) cannot parallel a
# partner that runs diagonally, so in a dense pad field ``_synthesize_tail``
# declines and the caller falls through to a partner-BLIND A* probe -- an
# 84-to-92-segment staircase that is 100% uncoupled on BOTH legs (measured on
# board-06's USB3_TX1 / USB3_TX2 landings).  The partner, though, already IS a
# legal path through that field: offsetting ITS slice is a tail that is coupled
# by construction.


def _elbow_partner() -> list[Segment]:
    """A 45-degree run into a horizontal one -- unreachable for axis-aligned
    detours, trivially followable as an offset."""
    return [_gseg(4.0, 4.0, 6.0, 6.0), _gseg(6.0, 6.0, 8.0, 6.0)]


def test_following_tail_offsets_the_partners_own_landing_run():
    dpr = _diffpair_router()
    partner = _elbow_partner()
    # head sits one coupled gap off the partner's start, on the -1 side.
    head, goal = _tail_pads((4.283, 3.717), (8.0, 5.6))

    tail = dpr._synthesize_following_tail(_AllClearPathfinder(), head, goal, 0, partner, 0.4)

    assert tail is not None, "the partner's own run is followable here"
    assert (tail.segments[0].x1, tail.segments[0].y1) == (4.283, 3.717)
    assert (tail.segments[-1].x2, tail.segments[-1].y2) == (8.0, 5.6)
    # The followed run is coupled; only the short landing hop onto the pad
    # (synthesized by the ordinary axis-aligned lander) is not.
    assert dpr._tail_coupled_fraction(tail, partner) > 0.8
    for seg in tail.segments:
        assert (
            dpr._min_distance_to_partner(seg.x1, seg.y1, seg.x2, seg.y2, partner, seg.layer)
            >= 0.4 - 1e-9
        )


def test_following_tail_declines_a_detour_far_longer_than_the_direct_hop():
    """Coupling is not worth an unbounded excursion (it would cost skew)."""
    dpr = _diffpair_router()
    # A partner that loops far away between the two anchors.
    partner = [
        _gseg(4.0, 4.0, 4.0, 1.0),
        _gseg(4.0, 1.0, 9.0, 1.0),
        _gseg(9.0, 1.0, 9.0, 4.0),
    ]
    head, goal = _tail_pads((4.4, 4.0), (9.4, 4.0))

    assert (
        dpr._synthesize_following_tail(_AllClearPathfinder(), head, goal, 0, partner, 0.4) is None
    )


def test_following_tail_needs_a_partner_and_a_clearance_floor():
    dpr = _diffpair_router()
    head, goal = _tail_pads((4.283, 3.717), (8.0, 5.6))

    assert dpr._synthesize_following_tail(_AllClearPathfinder(), head, goal, 0, [], 0.4) is None
    assert (
        dpr._synthesize_following_tail(_AllClearPathfinder(), head, goal, 0, _elbow_partner(), 0.0)
        is None
    )


def test_tail_route_lands_coupled_copper_on_a_diagonal_partner():
    """End-to-end: the tail the constructor actually ships must be coupled."""
    dpr = _diffpair_router()
    partner = _elbow_partner()
    head, goal = _tail_pads((4.283, 3.717), (8.0, 5.6))

    tail = dpr._tail_route(
        _AllClearPathfinder(),
        head,
        goal,
        0,
        "shadow-end",
        "USB3_TX1",
        partner_segments=partner,
    )

    assert tail is not None
    assert dpr._tail_coupled_fraction(tail, partner) > 0.6


def test_tail_route_is_unchanged_when_the_partner_is_out_of_reach():
    """No coupling available anywhere => the historical direct tail ships."""
    dpr = _diffpair_router()
    partner = _horizontal_partner(11.0)
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))

    tail = dpr._tail_route(
        _AllClearPathfinder(),
        head,
        goal,
        0,
        "shadow-end",
        "USB3_TX1",
        partner_segments=partner,
    )

    assert tail is not None
    assert len(tail.segments) == 1
    seg = tail.segments[0]
    assert (seg.x1, seg.y1, seg.x2, seg.y2) == (5.0, 5.0, 8.0, 5.0)


def test_truncated_follow_gives_up_the_walled_in_last_stretch():
    """A pad wall on the partner's final approach must not kill the whole tail."""
    dpr = _pad_gate_router()
    partner = _elbow_partner()
    head, goal = _tail_pads((4.283, 3.717), (8.0, 5.6))
    # Foreign pad squarely on the offset of the partner's LAST stretch.
    dpr.autorouter.grid.add_pad(_pad_at(7.6, 5.6, net=99, name="OTHER"))

    tail = dpr._synthesize_following_tail(_AllClearPathfinder(), head, goal, 0, partner, 0.4)

    if tail is not None:
        # Whatever ships must still clear the pad exactly (#4573's gate).
        for seg in tail.segments:
            assert dpr._segment_pad_clear(seg) is True


def test_following_tail_is_chain_connected_end_to_end():
    """A spliced tail that does not MEET is copper that strands the net.

    The follow path assembles lead-in + offset run + landing.  Each piece is
    produced independently, and ``_synthesize_tail`` silently drops a
    sub-0.01 mm span -- harmless at a pad, a real break mid-splice (measured:
    PCIE_RX stranded at U3.32 and the whole pair ripped by the #3540
    transactional strand guard).
    """
    dpr = _diffpair_router()
    partner = _elbow_partner()
    head, goal = _tail_pads((4.283, 3.717), (8.0, 5.6))

    tail = dpr._synthesize_following_tail(_AllClearPathfinder(), head, goal, 0, partner, 0.4)

    assert tail is not None
    assert dpr._route_is_chained(tail, head, goal)


def test_route_is_chained_rejects_a_subcell_hole():
    """The chain predicate must fire on exactly the #4462-class break."""
    dpr = _diffpair_router()
    head, goal = _tail_pads((0.0, 0.0), (2.0, 0.0))
    route = Route(net=2, net_name="USB3_TX1-")
    route.segments.append(
        Segment(x1=0.0, y1=0.0, x2=1.0, y2=0.0, width=0.2, layer=Layer.F_CU, net=2, net_name="N")
    )
    route.segments.append(
        Segment(x1=1.005, y1=0.0, x2=2.0, y2=0.0, width=0.2, layer=Layer.F_CU, net=2, net_name="N")
    )

    assert dpr._route_is_chained(route, head, goal) is False
    # Closing the 5 um hole makes it a chain again.
    route.segments[1].x1 = 1.0
    assert dpr._route_is_chained(route, head, goal) is True


# ---------------------------------------------------------------------------
# Issue #4570: via-count symmetry between the two legs of a constructed pair.
#
# ``diffpair_length_skew`` measures ELECTRICAL length -- planar copper plus
# each via's drilled length -- while the constructor's length matcher is planar
# only.  A pair whose two legs carry different vias can therefore be driven to
# a ~0.01 mm planar delta and still ship a multi-millimetre skew violation
# (board-06 seed 42: 2 x 1.6 mm of unmatched through-via on PCIE_RX /
# USB3_TX1).  The constructor now measures a thickness-free per-leg via
# signature, asks the landing tails to stay planar when the partner leg has no
# via to match, and DECLINES the side rather than shipping the asymmetry.
# ---------------------------------------------------------------------------


def _via(layers, x=1.0, y=1.0, net=1, is_micro=False) -> Via:
    return Via(
        x=x,
        y=y,
        drill=0.3,
        diameter=0.6,
        layers=layers,
        net=net,
        net_name="P",
        is_micro=is_micro,
    )


def _planar_leg(length: float = 10.0, net: int = 1) -> Route:
    r = Route(net=net, net_name="P")
    r.segments.append(
        Segment(
            x1=0.0, y1=0.0, x2=length, y2=0.0, width=0.2, layer=Layer.F_CU, net=net, net_name="P"
        )
    )
    return r


def test_via_signature_is_zero_for_a_planar_route():
    from kicad_tools.router.diffpair_routing import _route_via_signature

    assert _route_via_signature(_planar_leg(), 4) == (0, 0)
    assert _route_via_signature(None, 4) == (0, 0)


def test_via_signature_counts_both_vias_and_their_stack_spans():
    """The measured board-06 tail case: two full F.Cu->B.Cu through-vias."""
    from kicad_tools.router.diffpair_routing import _route_via_signature

    leg = _planar_leg()
    leg.vias.append(_via((Layer.F_CU, Layer.B_CU)))
    leg.vias.append(_via((Layer.F_CU, Layer.B_CU), x=5.0))

    # 4-layer stack: F.Cu -> B.Cu spans 3 stack positions.
    assert _route_via_signature(leg, 4) == (2, 6)
    # ...and 1 on a 2-layer stack: the signature follows the STACK, not the
    # KiCad layer enum (which would call F.Cu->B.Cu a 5-layer span either way).
    assert _route_via_signature(leg, 2) == (2, 2)


def test_via_signature_separates_a_blind_via_from_a_through_via():
    """The span sum -- not just the count -- is what has to match (#4570 AC)."""
    from kicad_tools.router.diffpair_routing import _route_via_signature

    blind = _planar_leg()
    blind.vias.append(_via((Layer.F_CU, Layer.IN1_CU), is_micro=True))
    through = _planar_leg()
    through.vias.append(_via((Layer.F_CU, Layer.B_CU)))

    assert _route_via_signature(blind, 4) == (1, 1)
    assert _route_via_signature(through, 4) == (1, 3)
    assert _route_via_signature(blind, 4) != _route_via_signature(through, 4)


@pytest.mark.parametrize("thickness", [0.6, 1.0, 1.6, 2.4])
@pytest.mark.parametrize("blind_buried", [True, False])
def test_equal_via_signatures_cancel_the_skew_rule_via_term(thickness, blind_buried):
    """The thickness-free claim, asserted against the checker's own model.

    Two legs whose ``(via count, sum |delta stack index|)`` signatures agree
    contribute EQUAL via length under ``DiffPairLengthTracker._measure_route``
    -- the exact measurement ``diffpair_length_skew`` performs -- for any
    ``board_thickness_mm`` and with or without through-via promotion (#4007).
    That is why the constructor can enforce z-length symmetry without a
    thickness source (the router's ``DesignRules`` carries none).
    """
    from kicad_tools.router.diffpair_length import DiffPairLengthTracker
    from kicad_tools.router.diffpair_routing import _route_via_signature

    leg_a = _planar_leg()
    leg_a.vias.append(_via((Layer.F_CU, Layer.B_CU)))
    leg_a.vias.append(_via((Layer.IN1_CU, Layer.IN2_CU), x=4.0))
    # Same signature, different placement/order.
    leg_b = _planar_leg()
    leg_b.vias.append(_via((Layer.IN2_CU, Layer.IN1_CU), x=7.0))
    leg_b.vias.append(_via((Layer.B_CU, Layer.F_CU), x=2.0))

    assert _route_via_signature(leg_a, 4) == _route_via_signature(leg_b, 4)

    len_a = DiffPairLengthTracker._measure_route(
        leg_a, thickness, 4, blind_buried_supported=blind_buried
    )
    len_b = DiffPairLengthTracker._measure_route(
        leg_b, thickness, 4, blind_buried_supported=blind_buried
    )
    assert abs(len_a - len_b) < 1e-9


def test_unequal_via_signatures_are_exactly_the_measured_board06_skew():
    """0 vias vs 2 through-vias on a 1.6 mm 4-layer board = 3.2 mm of skew."""
    from kicad_tools.router.diffpair_length import DiffPairLengthTracker
    from kicad_tools.router.diffpair_routing import _route_via_signature

    guide_leg = _planar_leg()
    shadow_leg = _planar_leg()
    shadow_leg.vias.append(_via((Layer.F_CU, Layer.B_CU)))
    shadow_leg.vias.append(_via((Layer.F_CU, Layer.B_CU), x=5.0))

    assert _route_via_signature(guide_leg, 4) != _route_via_signature(shadow_leg, 4)
    skew = DiffPairLengthTracker._measure_route(
        shadow_leg, 1.6, 4, blind_buried_supported=False
    ) - DiffPairLengthTracker._measure_route(guide_leg, 1.6, 4, blind_buried_supported=False)
    assert abs(skew - 3.2) < 1e-9


def test_via_signature_tolerates_an_odd_via_count_without_crashing():
    """A single-via leg is physically odd but must not break the gate."""
    from kicad_tools.router.diffpair_routing import _route_via_signature

    leg = _planar_leg()
    leg.vias.append(_via((Layer.F_CU, Layer.B_CU)))
    assert _route_via_signature(leg, 4) == (1, 3)


# ---------------------------------------------------------------------------
# Remediation A: the planar-preferred landing tail.
# ---------------------------------------------------------------------------


class _FakeRouterLayerLock:
    """Records ``set_routable_layers`` calls so the lock can be asserted."""

    def __init__(self, layers):
        self._routable_layers = list(layers)
        self.calls: list[list[int]] = []

    def set_routable_layers(self, layers):
        self.calls.append(list(layers))
        self._routable_layers = list(layers)


def _diving_route(net=2) -> Route:
    r = Route(net=net, net_name="USB3_TX1-")
    r.segments.append(
        Segment(x1=0.0, y1=0.0, x2=2.0, y2=0.0, width=0.2, layer=Layer.F_CU, net=net, net_name="N")
    )
    r.segments.append(
        Segment(x1=2.0, y1=0.0, x2=4.0, y2=0.0, width=0.2, layer=Layer.B_CU, net=net, net_name="N")
    )
    r.vias.append(_via((Layer.F_CU, Layer.B_CU), x=2.0, y=0.0, net=net))
    return r


def _planar_route(net=2) -> Route:
    r = Route(net=net, net_name="USB3_TX1-")
    r.segments.append(
        Segment(x1=0.0, y1=0.0, x2=4.0, y2=0.5, width=0.2, layer=Layer.F_CU, net=net, net_name="N")
    )
    return r


def _fallback_tail_probe_spy(dpr, monkeypatch, fake_router):
    """Wire a scripted ``_single_ended_guide_route`` onto ``dpr``.

    Returns the diving route while the router is unlocked and a planar one
    while it is locked -- i.e. exactly the board-06 situation the planar
    preference exists for.
    """
    monkeypatch.setattr(dpr.autorouter, "router", fake_router, raising=False)
    monkeypatch.setattr(dpr, "_route_pad_violation", lambda route: (0.0, None), raising=False)

    def _probe(head, goal, per_net_timeout=None, avoid_locations=None, avoid_radius_cells=1):
        locked = fake_router._routable_layers == [0]
        return _planar_route() if locked else _diving_route()

    monkeypatch.setattr(dpr, "_single_ended_guide_route", _probe, raising=False)


def test_planar_preference_displaces_a_diving_astar_tail(monkeypatch):
    """The via-inventing A* tail is replaced by a layer-locked planar one."""
    dpr = _diffpair_router()
    fake_router = _FakeRouterLayerLock([0, 1])
    _fallback_tail_probe_spy(dpr, monkeypatch, fake_router)
    head, goal = _tail_pads((0.0, 0.0), (4.0, 0.5))

    tail = dpr._fallback_tail_route(head, goal, None, 0.0, prefer_planar_layer=0)

    assert tail is not None
    assert tail.vias == [], "a planar tail was available; the dive must not ship"
    # The lock was applied and then RESTORED (a leaked narrowing would forbid
    # vias for every remaining net on the board).
    assert fake_router.calls == [[0], [0, 1]]
    assert fake_router._routable_layers == [0, 1]


def test_planar_preference_is_not_attempted_without_the_flag(monkeypatch):
    """Default (and the only shadow-OFF-reachable) path: no extra probe."""
    dpr = _diffpair_router()
    fake_router = _FakeRouterLayerLock([0, 1])
    _fallback_tail_probe_spy(dpr, monkeypatch, fake_router)
    head, goal = _tail_pads((0.0, 0.0), (4.0, 0.5))

    tail = dpr._fallback_tail_route(head, goal, None, 0.0)

    assert tail is not None
    assert tail.vias, "the legacy chain returns the unbiased (diving) probe"
    assert fake_router.calls == [], "the router must not be touched at all"


def test_planar_preference_keeps_the_dive_when_no_planar_tail_is_legal(monkeypatch):
    """A planar candidate that fails the exact pad predicate is REJECTED.

    The remediation may never bypass the gates the rest of the constructed
    copper passes (#4571's lesson).  When the planar probe's copper violates
    them the diving tail is kept and the caller's symmetry gate -- not this
    function -- decides the side's fate.
    """
    dpr = _diffpair_router()
    fake_router = _FakeRouterLayerLock([0, 1])
    _fallback_tail_probe_spy(dpr, monkeypatch, fake_router)
    # Every PLANAR candidate violates the exact foreign-pad predicate.
    monkeypatch.setattr(
        dpr,
        "_route_pad_violation",
        lambda route: ((0.5, (1.0, 1.0)) if not route.vias else (0.0, None)),
        raising=False,
    )
    head, goal = _tail_pads((0.0, 0.0), (4.0, 0.5))

    tail = dpr._fallback_tail_route(head, goal, None, 0.0, prefer_planar_layer=0)

    assert tail is not None
    assert tail.vias, "the rejected planar candidate must not displace the dive"
    assert fake_router.calls == [[0], [0, 1]], "lock applied then restored"


def test_layer_lock_is_a_noop_without_a_set_routable_layers_backend():
    """The pure-Python backend has no layer vector: report the lock unavailable."""
    dpr = _diffpair_router()

    class _NoLockRouter:
        pass

    dpr.autorouter.router = _NoLockRouter()
    with dpr._layer_locked_router(0) as locked:
        assert locked is False


def test_layer_lock_declines_a_layer_that_is_not_routable():
    dpr = _diffpair_router()
    fake_router = _FakeRouterLayerLock([0, 1])
    dpr.autorouter.router = fake_router
    with dpr._layer_locked_router(5) as locked:
        assert locked is False
    assert fake_router.calls == []


# ---------------------------------------------------------------------------
# The gate itself, driven through ``_shadow_route_pair``.
# ---------------------------------------------------------------------------


def _planar_guide(p_start_pad) -> Route:
    """A straight, VIA-FREE F.Cu guide -- board-06's PCIE_RX / USB3_TX1 case."""
    net, name = p_start_pad.net, p_start_pad.net_name
    guide = Route(net=net, net_name=name)
    guide.segments.append(
        Segment(
            x1=5.0, y1=4.8, x2=25.0, y2=4.8, width=0.2, layer=Layer.F_CU, net=net, net_name=name
        )
    )
    return guide


def _stub_tail_route_planar(
    self, _pf, head, goal, _layer, _label, _name, partner_segments=None, prefer_planar=False
):
    """Degenerate planar body-anchor tail (see ``_stub_tail_route``)."""
    r = Route(net=goal.net, net_name=goal.net_name)
    r.segments.append(
        Segment(
            x1=head.x,
            y1=head.y,
            x2=head.x + 0.001,
            y2=head.y,
            width=0.2,
            layer=head.layer,
            net=goal.net,
            net_name=goal.net_name,
        )
    )
    return r


def _stub_tail_route_diving(
    self, _pf, head, goal, _layer, _label, _name, partner_segments=None, prefer_planar=False
):
    """A stub tail that dives -- the unmatched via #4570 is about."""
    r = _stub_tail_route_planar(
        self, _pf, head, goal, _layer, _label, _name, partner_segments, prefer_planar
    )
    r.vias.append(
        Via(
            x=head.x,
            y=head.y,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=goal.net,
            net_name=goal.net_name,
        )
    )
    return r


def _run_shadow_with_tails(monkeypatch, tail_stub):
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    spacing_cells = 4
    dpr, pair, spec, pathfinder, _ = _shadow_setup(spacing_cells, bend_end=(11.0, 7.7))
    guide = _planar_guide(spec.p_start)
    monkeypatch.setattr(DiffPairRouter, "_tail_route", tail_stub, raising=True)
    _defeat_pair_self_check_gates(monkeypatch)
    result = dpr._shadow_route_pair(pair, spec, pathfinder, guide, spacing_cells)
    return dpr, result


def test_shadow_pair_with_symmetric_planar_legs_is_accepted(monkeypatch):
    """Control: a via-free guide plus via-free tails passes the gate."""
    dpr, result = _run_shadow_with_tails(monkeypatch, _stub_tail_route_planar)

    assert result is not None
    p_route, n_route = result
    assert p_route.vias == [] and n_route.vias == []
    assert dpr._last_shadow_decline_reason != "via-skew"


def test_shadow_pair_declines_when_only_one_leg_carries_a_via(monkeypatch):
    """The #4570 defect under STRICT: 0 vias on the guide, 2 on the shadow.

    Before the gate this pair SHIPPED -- the planar length matcher drove it to
    a sub-hundredth-mm planar delta and reported success while
    ``diffpair_length_skew`` measured 3.19 mm.  Under
    ``KCT_SHADOW_VIA_SYMMETRY_STRICT`` the pair is dropped instead, with a
    reason distinct from ``overlap`` / ``blockage``.
    """
    import kicad_tools.router.diffpair_routing as dpr_mod

    monkeypatch.setattr(dpr_mod, "_SHADOW_VIA_SYMMETRY_STRICT", True, raising=True)
    dpr, result = _run_shadow_with_tails(monkeypatch, _stub_tail_route_diving)

    assert result is None, "strict mode must not ship an asymmetric pair"
    assert dpr._last_shadow_decline_reason == "via-skew"


def test_shadow_pair_records_the_asymmetry_it_ships_by_default(monkeypatch):
    """Default policy: the pair still ships, but never SILENTLY.

    Dropping the pair costs a routed net on board-06 (measured: reach 19 -> 18,
    and the "improved" DRC count is partly vacuity because
    ``diffpair_length_skew`` only fires on an ENGAGED pair).  So the default
    keeps the pair -- and records ``via-skew`` so the outcome is visible to the
    #4459 taxonomy and to ``KCT_SHADOW_DEBUG``.
    """
    dpr, result = _run_shadow_with_tails(monkeypatch, _stub_tail_route_diving)

    assert result is not None
    assert dpr._last_shadow_decline_reason == "via-skew", (
        "an asymmetric ship must still be recorded, not silent"
    )


def test_symmetric_side_beats_an_asymmetric_one(monkeypatch):
    """The gate's cheapest win: prefer the offset side whose legs match.

    ``asym_fallback`` is only consulted after BOTH sides have been tried, so a
    side-``+1`` pair that dives can never displace a symmetric side-``-1`` one.
    """
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    seen: list[float] = []

    def _tail(self, _pf, head, goal, _layer, _label, _name, partner_segments=None, **kw):
        # Dive on the first side attempted, stay planar on the second.
        first = not seen
        if _label == "shadow-start":
            seen.append(1.0)
        stub = _stub_tail_route_diving if first else _stub_tail_route_planar
        return stub(self, _pf, head, goal, _layer, _label, _name, partner_segments)

    monkeypatch.setattr(DiffPairRouter, "_tail_route", _tail, raising=True)
    _defeat_pair_self_check_gates(monkeypatch)
    spacing_cells = 4
    dpr, pair, spec, pathfinder, _ = _shadow_setup(spacing_cells, bend_end=(11.0, 7.7))
    guide = _planar_guide(spec.p_start)

    result = dpr._shadow_route_pair(pair, spec, pathfinder, guide, spacing_cells)

    assert result is not None
    p_route, n_route = result
    assert p_route.vias == [] and n_route.vias == [], "the symmetric side must win"


def test_shadow_via_symmetry_gate_honours_its_kill_switch(monkeypatch):
    """``KCT_SHADOW_VIA_SYMMETRY=0`` restores the pre-#4570 behaviour exactly."""
    import kicad_tools.router.diffpair_routing as dpr_mod

    monkeypatch.setattr(dpr_mod, "_SHADOW_VIA_SYMMETRY", False, raising=True)
    dpr, result = _run_shadow_with_tails(monkeypatch, _stub_tail_route_diving)

    assert result is not None, "with the gate off the asymmetric pair ships as before"
    assert dpr._last_shadow_decline_reason != "via-skew"


def test_symmetric_two_via_legs_are_not_misdiagnosed(monkeypatch):
    """Both legs dive (the deliberate polarity-swap crossover): leave it alone.

    A crossing tail is a two-via construction BY DESIGN.  It is only a defect
    when the partner leg has no counterpart, so a guide that carries the same
    via signature must still be accepted.
    """
    from kicad_tools.router.diffpair_routing import _route_via_signature

    guide_leg = _planar_leg()
    guide_leg.vias.append(_via((Layer.F_CU, Layer.B_CU)))
    guide_leg.vias.append(_via((Layer.B_CU, Layer.F_CU), x=6.0))
    shadow_leg = _planar_leg(net=2)
    shadow_leg.vias.append(_via((Layer.F_CU, Layer.B_CU), x=1.2, net=2))
    shadow_leg.vias.append(_via((Layer.F_CU, Layer.B_CU), x=6.2, net=2))

    assert _route_via_signature(guide_leg, 4) == _route_via_signature(shadow_leg, 4)


# ---------------------------------------------------------------------------
# Remediation B: the mirrored, centreline-preserving z-jog.
# ---------------------------------------------------------------------------


def _mirror_legs(dpr, partner_x_offset: float = 20.0):
    """A long straight leg plus a partner far enough away to allow a mirror."""
    net = 1
    deficient = Route(net=net, net_name="USB_D+")
    deficient.segments.append(
        Segment(
            x1=2.0, y1=2.0, x2=16.0, y2=2.0, width=0.2, layer=Layer.F_CU, net=net, net_name="USB_D+"
        )
    )
    partner = Route(net=2, net_name="USB_D-")
    partner.segments.append(
        Segment(
            x1=2.0,
            y1=2.0 + partner_x_offset,
            x2=16.0,
            y2=2.0 + partner_x_offset,
            width=0.2,
            layer=Layer.F_CU,
            net=2,
            net_name="USB_D-",
        )
    )
    partner.vias.append(_via((Layer.F_CU, Layer.B_CU), x=6.0, y=2.0 + partner_x_offset, net=2))
    partner.vias.append(_via((Layer.F_CU, Layer.B_CU), x=9.0, y=2.0 + partner_x_offset, net=2))
    return deficient, partner


def test_mirrored_z_jog_matches_the_signature_without_moving_copper():
    """The mirror equalises the drilled length without changing plan view.

    The window is re-emitted on the other layer, so the leg's PLANAR length --
    what ``_length_match_constructed_pair`` works on -- is untouched, and no
    new angle is introduced (so the 45-census cannot regress).
    """
    from kicad_tools.router.diffpair_routing import _route_copper_length, _route_via_signature

    dpr = _diffpair_router()
    deficient, partner = _mirror_legs(dpr)
    before_len = _route_copper_length(deficient)

    ok = dpr._match_pair_via_signature(deficient, partner, _AllClearPathfinder(), 2)

    assert ok is True
    assert _route_via_signature(deficient, 2) == _route_via_signature(partner, 2)
    assert abs(_route_copper_length(deficient) - before_len) < 1e-9
    # Exactly one stretch changed layer, bracketed by the two new vias.
    assert [s.layer for s in deficient.segments] == [Layer.F_CU, Layer.B_CU, Layer.F_CU]
    assert len(deficient.vias) == 2
    for v in deficient.vias:
        assert v.layers == (Layer.F_CU, Layer.B_CU)


def test_mirrored_z_jog_is_refused_when_the_barrel_would_graze_the_partner():
    """A coupled body has no legal mirror site -- the barrel bound is ~0.6 mm.

    This is why board-06's crossing-tail pairs cannot be repaired by mirroring:
    the intra-pair gap is a tenth of the via-barrel-to-partner-copper bound, so
    a centreline-preserving jog has nowhere to sit.
    """
    dpr = _diffpair_router()
    deficient, partner = _mirror_legs(dpr, partner_x_offset=0.3)

    assert dpr._match_pair_via_signature(deficient, partner, _AllClearPathfinder(), 2) is False
    assert deficient.vias == [], "a refused mirror must not leave copper behind"


def test_mirrored_z_jog_is_refused_over_a_foreign_pad():
    """The mirror passes the exact foreign-pad predicate, not just the raster."""
    dpr = _pad_gate_router()
    deficient, partner = _mirror_legs(dpr)
    # Drop a foreign pad on the middle of the leg, where the jog window sits.
    dpr.autorouter.grid.add_pad(_pad_at(9.0, 2.0, net=99, name="OTHER"))

    ok = dpr._match_pair_via_signature(deficient, partner, _AllClearPathfinder(), 2)

    if ok:
        # A legal window elsewhere on the leg is fine; what must never happen
        # is a via inside the foreign pad's halo.
        for v in deficient.vias:
            assert dpr._via_pad_deficit(v) <= 1e-4
    else:
        assert deficient.vias == []


def test_mirrored_z_jog_is_refused_when_the_partner_occupies_the_jog_layer():
    """The relocated stretch is screened on the layer it MOVES TO.

    The partner's own crossing tail dives to exactly the layer the jog lands
    on, so a mirror that only checks the barrels leaves same-layer P/N copper
    at a fraction of the clearance (measured: 12 extra
    ``diffpair_clearance_intra`` errors on board-06 seed 42).
    """
    dpr = _diffpair_router()
    deficient, partner = _mirror_legs(dpr)
    # Partner copper laid directly over the jog window, on the jog's layer.
    partner.segments.append(
        Segment(
            x1=2.0,
            y1=2.05,
            x2=16.0,
            y2=2.05,
            width=0.2,
            layer=Layer.B_CU,
            net=2,
            net_name="USB_D-",
        )
    )

    assert dpr._match_pair_via_signature(deficient, partner, _AllClearPathfinder(), 2) is False
    assert deficient.vias == []


def test_mirror_is_opt_in_and_the_gate_does_not_depend_on_it(monkeypatch):
    """Remediation B ships OFF; the gate still detects and records without it.

    ``_mirror_z_jog``'s own board-06 measurement (one legal site, and taking it
    landed that pair on the 0.100-vs-0.1016 mm intra-pair rung) says it needs a
    site-selection policy first, so the default path never calls it.
    """
    import kicad_tools.router.diffpair_routing as dpr_mod

    assert dpr_mod._SHADOW_VIA_MIRROR is False

    called: list[int] = []
    monkeypatch.setattr(
        dpr_mod.DiffPairRouter,
        "_match_pair_via_signature",
        lambda self, *a, **k: called.append(1) or True,
        raising=True,
    )
    dpr, result = _run_shadow_with_tails(monkeypatch, _stub_tail_route_diving)

    assert called == [], "the mirror must not run with KCT_SHADOW_VIA_MIRROR unset"
    assert result is not None
    assert dpr._last_shadow_decline_reason == "via-skew", "detection is independent of the mirror"


def test_mirror_refuses_an_odd_via_excess():
    """A z-jog adds vias in PAIRS; an odd difference is refused, not papered over."""
    dpr = _diffpair_router()
    deficient, partner = _mirror_legs(dpr)
    partner.vias.pop()  # leaves a single unmatched via

    assert dpr._match_pair_via_signature(deficient, partner, _AllClearPathfinder(), 2) is False


def _spy_shadow_entry(monkeypatch, shadow_enabled: bool) -> list[str]:
    """Drive the coupled pre-phase once and record whether #4570 code ran."""
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    assert dpr.enable_shadow_construction is False, "the default must stay OFF"
    dpr.enable_shadow_construction = shadow_enabled

    calls: list[str] = []
    monkeypatch.setattr(
        DiffPairRouter,
        "_shadow_route_pair",
        lambda self, *a, **k: calls.append("shadow") or None,
        raising=True,
    )
    monkeypatch.setattr(
        DiffPairRouter,
        "_layer_locked_router",
        lambda self, layer_idx: calls.append("lock"),
        raising=True,
    )
    dpr.route_differential_pair_coupled(pair, per_pair_timeout=2.0, coupled_only=True)
    return calls


def test_via_symmetry_path_is_unreachable_with_shadow_construction_off(monkeypatch):
    """Shadow-OFF inertness, asserted structurally (#4536: no byte-identity).

    Every #4570 behaviour hangs off ONE gated entry point -- the
    ``enable_shadow_construction`` guard in front of ``_shadow_route_pair``.
    With the flag off that method is never called, so neither the symmetry
    gate nor the planar-tail preference can run.
    """
    assert _spy_shadow_entry(monkeypatch, shadow_enabled=False) == [], (
        "shadow-OFF must reach neither the gate nor the layer lock"
    )


def test_shadow_entry_point_spy_is_not_vacuous(monkeypatch):
    """Positive control for the inertness test above.

    Without this, a fixture that never reached the shadow branch at all would
    make the shadow-OFF assertion pass for the wrong reason.
    """
    assert _spy_shadow_entry(monkeypatch, shadow_enabled=True).count("shadow") >= 1


# --- Issue #4574: escape-channel-aware crossing-tail via sites ---------------
# ``_synthesize_crossing_tail`` enumerates a fixed 3x5 lattice around each
# endpoint and ships the FIRST candidate that survives the legality gauntlet.
# The enumeration order carries no information about the board, so an all-layer
# barrel routinely lands in the middle of a neighbouring fine-pitch pad's only
# escape (board-06: MIPI_D0's corridor probe returns ``segments=0`` -- sealed,
# not congested).  The remedy re-ORDERS the lattice by how deeply each barrel
# intrudes on not-yet-routed pads' direct escapes; it adds no gate, and with an
# empty/uninformative registry it must reproduce the first-legal result exactly.


class _CrossingPathfinder:
    """Pathfinder stand-in for the crossover lattice: nothing is blocked.

    Isolates via-SITE SELECTION from grid state, so a site that loses can only
    have lost to the ordering or to an explicit gate.
    """

    def __init__(self, width: float = 0.2) -> None:
        self._width = width

    def _get_trace_width_for_net(self, net_name: str) -> float:
        return self._width

    def _is_cell_blocked(self, gx: int, gy: int, layer_idx: int, net: int) -> bool:
        return False

    def _is_via_blocked(self, gx: int, gy: int, net: int) -> bool:
        return False


def _crossing_router(shadow: bool = True):
    """A router whose crossover lattice has a second routable layer to dive to."""
    from kicad_tools.router.core import Autorouter

    rules = DesignRules()
    rules.grid_resolution = 0.1
    router = Autorouter(width=20.0, height=10.0, rules=rules)
    dpr = router._diffpair
    dpr.enable_shadow_construction = shadow
    return dpr


def _register_unrouted_neighbour(
    dpr,
    pad_xy=(7.0, 5.6),
    far_xy=(12.0, 5.6),
    net: int = 9,
    pitch: float = 0.5,
) -> None:
    """An unrouted two-pad net escaping east along ``y = pad_xy[1]``.

    Registered on the AUTOROUTER (``pads`` / ``nets`` / ``component_pitches``)
    and deliberately NOT on the grid, so the neighbour can only influence the
    result through the preference -- never through the #4571 pad gate.
    """
    dpr.autorouter.pads[("J9", "1")] = _pad_at(
        pad_xy[0], pad_xy[1], net=net, name="NEIGH", ref="J9", pin="1"
    )
    dpr.autorouter.pads[("U9", "1")] = _pad_at(
        far_xy[0], far_xy[1], net=net, name="NEIGH", ref="U9", pin="1"
    )
    dpr.autorouter.nets[net] = [("J9", "1"), ("U9", "1")]
    dpr.autorouter._component_pitches = {"J9": pitch, "U9": pitch}


def _crossing_tail(dpr):
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    return dpr._synthesize_crossing_tail(_CrossingPathfinder(), head, goal, 0, [])


def _via_sites(tail):
    return [(pytest.approx(v.x), pytest.approx(v.y)) for v in tail.vias]


def test_crossing_tail_first_legal_site_is_the_unscored_baseline():
    """Control: with nothing to avoid, both barrels sit on the endpoints."""
    tail = _crossing_tail(_crossing_router())

    assert tail is not None
    assert len(tail.vias) == 2
    assert (tail.vias[0].x, tail.vias[0].y) == (5.0, 5.0)
    assert (tail.vias[1].x, tail.vias[1].y) == (8.0, 5.0)


def test_crossing_tail_steps_off_an_unrouted_pads_escape_channel():
    """The seal this issue is about: the first-legal site plugs a neighbour.

    The lattice's a=0/b=0 goal-side site sits 0.6 mm from the neighbour's
    escape ray -- inside the 0.65 mm a foreign trace needs from the barrel --
    so the neighbour's direct exit becomes illegal.  Later lattice sites are
    equally legal and leave it open; one of those must ship instead.
    """
    from kicad_tools.router.diffpair_routing import _channel_seal_penalty

    dpr = _crossing_router()
    _register_unrouted_neighbour(dpr)
    channels = dpr._escape_channel_registry(frozenset({2}))
    keepout = dpr.autorouter.rules.via_diameter / 2 + dpr.autorouter.rules.trace_clearance + 0.2 / 2
    # Pre-condition: the site the unscored loop ships really does seal it.
    assert _channel_seal_penalty(8.0, 5.0, keepout, channels) > 0.0

    tail = _crossing_tail(dpr)

    assert tail is not None
    assert (tail.vias[1].x, tail.vias[1].y) == (8.0, 4.4)
    for via in tail.vias:
        assert _channel_seal_penalty(via.x, via.y, keepout, channels) == 0.0


def test_crossing_tail_scoring_is_inert_with_shadow_construction_off():
    """AC-5: the stub-edge caller (``partner_segments=[]``, shadow OFF) is untouched.

    Same board state as the test above -- only the flag differs -- so a
    changed result here could only come from the scoring leaking onto the
    default path.
    """
    dpr = _crossing_router(shadow=False)
    _register_unrouted_neighbour(dpr)

    tail = _crossing_tail(dpr)

    assert tail is not None, "the stub-edge path must still synthesize a tail"
    assert (tail.vias[1].x, tail.vias[1].y) == (8.0, 5.0)


def test_crossing_tail_with_an_empty_registry_is_bit_for_bit_first_legal():
    """AC-4: no unrouted neighbours -> the preference cannot change anything."""
    scored = _crossing_tail(_crossing_router())
    baseline = _crossing_tail(_crossing_router(shadow=False))

    assert scored is not None and baseline is not None
    assert _via_sites(scored) == _via_sites(baseline)
    assert [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in scored.segments] == [
        (s.x1, s.y1, s.x2, s.y2, s.layer) for s in baseline.segments
    ]


def test_crossing_tail_breaks_uniform_penalty_ties_on_enumeration_order(monkeypatch):
    """AC-4 / AC-6: when scoring cannot discriminate, first-legal still wins."""
    import kicad_tools.router.diffpair_routing as dpr_mod

    dpr = _crossing_router()
    _register_unrouted_neighbour(dpr)
    monkeypatch.setattr(dpr_mod, "_channel_seal_penalty", lambda x, y, keepout, channels: 1.0)

    tail = _crossing_tail(dpr)

    assert tail is not None
    assert (tail.vias[1].x, tail.vias[1].y) == (8.0, 5.0)


def test_crossing_tail_preference_never_bypasses_the_foreign_pad_gate():
    """A zero-penalty site that is a pad short must still lose (#4571's lesson).

    The site the preference wants -- (8.0, 4.4) -- is occupied by a foreign
    pad ON THE GRID here, so the exact ``clearance_pad_segment`` screen inside
    the candidate loop has to veto it and the loop has to fall through to the
    next site the preference likes.
    """
    import kicad_tools.router.diffpair_routing as dpr_mod

    dpr = _crossing_router()
    _register_unrouted_neighbour(dpr)
    dpr.autorouter.grid.add_pad(_pad_at(8.0, 4.4, net=99, name="OTHER"))

    tail = _crossing_tail(dpr)

    assert tail is not None
    assert (tail.vias[1].x, tail.vias[1].y) != (8.0, 4.4)
    assert dpr._route_pad_violation(tail)[0] <= dpr_mod._SHADOW_PAD_DEFICIT_EPS


def test_escape_channel_registry_skips_routed_and_pitchless_nets():
    """The registry is an UNROUTED-neighbour oracle, not a pad dump."""
    dpr = _crossing_router()
    _register_unrouted_neighbour(dpr)

    assert len(dpr._escape_channel_registry(frozenset({2}))) == 2
    # The net under construction (and its partner) never scores itself.
    assert dpr._escape_channel_registry(frozenset({2, 9})) == []
    # A component with no known pitch contributes no channel.
    dpr.autorouter._component_pitches = {}
    assert dpr._escape_channel_registry(frozenset({2})) == []
    # Copper already committed for the net -> it is no longer waiting to escape.
    dpr.autorouter._component_pitches = {"J9": 0.5, "U9": 0.5}
    dpr.autorouter.routes.append(Route(net=9, net_name="NEIGH"))
    assert dpr._escape_channel_registry(frozenset({2})) == []


def test_escape_channel_registry_is_empty_without_autorouter_state():
    """AC-4 degradation: test doubles and pitchless boards score nothing."""
    dpr = _crossing_router()

    assert dpr._escape_channel_registry(frozenset()) == []


def test_channel_seal_penalty_only_fires_inside_the_channel():
    """Behind the pad, past the reach, or clear laterally: all cost nothing."""
    from kicad_tools.router.diffpair_routing import _channel_seal_penalty, _EscapeChannel

    channel = [_EscapeChannel(x=0.0, y=0.0, ux=1.0, uy=0.0, reach=1.5)]

    assert _channel_seal_penalty(-0.5, 0.0, 0.65, channel) == 0.0  # behind the pad
    assert _channel_seal_penalty(2.0, 0.0, 0.65, channel) == 0.0  # past the reach
    assert _channel_seal_penalty(0.75, 0.65, 0.65, channel) == 0.0  # clear laterally
    # Dead centre in the channel is the deepest intrusion there is.
    assert _channel_seal_penalty(0.75, 0.0, 0.65, channel) == pytest.approx(0.65)
    # ...and the penalty decreases monotonically as the barrel steps aside.
    assert _channel_seal_penalty(0.75, 0.3, 0.65, channel) == pytest.approx(0.35)


def test_channel_seal_penalty_sums_over_every_channel_it_reaches():
    """Two sealed neighbours are worse than one; the score is additive."""
    from kicad_tools.router.diffpair_routing import _channel_seal_penalty, _EscapeChannel

    channels = [
        _EscapeChannel(x=0.0, y=0.0, ux=1.0, uy=0.0, reach=1.5),
        _EscapeChannel(x=0.0, y=0.4, ux=1.0, uy=0.0, reach=1.5),
    ]

    one = _channel_seal_penalty(0.75, -0.4, 0.65, channels)
    both = _channel_seal_penalty(0.75, 0.2, 0.65, channels)
    assert one == pytest.approx(0.25)
    assert both == pytest.approx(0.9)
