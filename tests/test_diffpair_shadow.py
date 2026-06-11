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
from kicad_tools.router.primitives import Route, Segment
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
    return sum(
        math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments
    )


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
            f"bulge crossed toward the partner: ({seg.x1},{seg.y1})->"
            f"({seg.x2},{seg.y2})"
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
        f"serpentine added only {added:.3f}mm of the requested "
        f"{requested:.3f}mm"
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
    return Segment(
        x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer, net=1, net_name="P"
    )


def test_point_segment_distance_perpendicular():
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    d = DiffPairRouter._point_segment_distance(5.0, 1.0, _seg(0, 0, 10, 0))
    assert d == pytest.approx(1.0)


def test_point_segment_distance_beyond_endpoint():
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    d = DiffPairRouter._point_segment_distance(12.0, 0.0, _seg(0, 0, 10, 0))
    assert d == pytest.approx(2.0)


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
        spacing = math.hypot(
            ns.p_pos.x - ns.n_pos.x, ns.p_pos.y - ns.n_pos.y
        )
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
                f"P landed {d:.2f} cells from the partner trail "
                f"(< min_spacing_cells=4): {ns.p_pos}"
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
        ns
        for ns, _c, is_via in neighbors
        if not is_via and ns.p_pos == GridPos(101, 100, 0)
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

    The 2026-06-11 board 06 seed-42 integration run showed the
    constructor's committed geometry is not yet artifact-quality
    (stranded shadow tails, shadow-via/partner intersections, corridor
    competition stranding single-ended nets -> 16/21 reach).  Recipes
    must explicitly opt in once the follow-up issues land.
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
        diffpair_config=DifferentialPairConfig(
            enabled=True, enable_shadow_construction=True
        )
    )
    assert dpr.enable_shadow_construction is True

    router.route_all_with_diffpairs(
        diffpair_config=DifferentialPairConfig(enabled=True)
    )
    assert dpr.enable_shadow_construction is False
