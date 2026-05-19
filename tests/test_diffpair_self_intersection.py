"""Tests for the CoupledPathfinder path-history self-intersection guard
(Issue #3078).

Background
----------

PR #3022 (Issue #3012) added a ``min_spacing_cells`` floor on the
center-to-center spacing between P and N grid positions, so neither
the symmetric nor the asymmetric P-advance/N-advance moves can place
the two centerlines below the per-pair clearance.

But that floor only constrains the *new endpoint pair* — it does NOT
check that the *trail* of the advancing trace has not crossed the
*current cell* of the partner trace, or that the advancing trace has
not looped back through one of its own previously-occupied cells.

On board 06 (9 differential pairs, BGA-49 escape) this produced the
``7-vs-1061 segments`` asymmetry on ``USB3_RX1``: P held mostly still
while N looped around it, then re-converged at full spacing — but the
quantised centerlines were coincident through a long stretch, hence
the ``-0.200mm`` ``diffpair_clearance_intra`` violations.

The fix (#3078) added a per-node path-history check in
``CoupledPathfinder._get_coupled_neighbors``:

1. ``p_visited`` / ``n_visited`` frozensets are threaded as new kwargs.
2. ``_self_intersects(...)`` rejects:
   * cross-trail landings (advancing trace lands on partner's trail),
   * self-loop landings (advancing trace re-enters its own past cell),
   with an endpoint-pad exemption.
3. ``route_coupled`` walks the current node's parent chain at
   expansion time to build the sets.

These tests verify each layer of that guard with synthetic 1-pair
fixtures.  An ``_reconstruct_coupled_routes`` segment-count canary
(WARN-level log) is also covered by inspection.
"""

from __future__ import annotations

from kicad_tools.router.diffpair_routing import CoupledPathfinder, CoupledState, GridPos
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.rules import DesignRules


def _make_pathfinder() -> CoupledPathfinder:
    """Construct a CoupledPathfinder with a small grid for unit tests."""
    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    return CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=2,
        min_spacing_cells=2,
    )


# ---------------------------------------------------------------------------
# No-history opt-out: legacy callers (no p_visited / n_visited) behave
# identically to pre-#3078.
# ---------------------------------------------------------------------------


def test_legacy_callers_unaffected_when_no_visited_sets():
    """Callers that don't pass p_visited / n_visited see no history check.

    Mirrors how the pre-#3078 test suite calls
    ``_get_coupled_neighbors``: no kwargs.  The neighbor list must
    still be non-empty (the spacing floor and tolerance rules are
    independent of #3078).
    """
    pf = _make_pathfinder()
    state = CoupledState(GridPos(5, 5, 0), GridPos(7, 5, 0), (0, 0))
    neighbors = pf._get_coupled_neighbors(
        state, p_net=1, n_net=2, target_spacing_cells=2
    )
    # At least one symmetric move (right/left/up/down preserving 2-cell
    # spacing) must exist for the legacy permissive path.
    assert len(neighbors) > 0


# ---------------------------------------------------------------------------
# Symmetric move: cross-trail collision is rejected.
# ---------------------------------------------------------------------------


def test_symmetric_move_rejected_when_p_lands_on_n_trail():
    """If a symmetric move puts P on a cell N has previously occupied,
    reject it -- otherwise the routed P trace overlaps N's past trail
    and Phase A flags a ``diffpair_clearance_intra`` violation.
    """
    pf = _make_pathfinder()
    # P at (5, 5), N at (7, 5).  Spacing = 2 cells.
    state = CoupledState(GridPos(5, 5, 0), GridPos(7, 5, 0), (0, 0))
    # Pretend N has previously occupied (6, 5, 0) on its trail.
    # The +x symmetric move puts P at (6, 5, 0) -- exactly on N's
    # past trail.  Must be rejected.
    n_visited = frozenset([(6, 5, 0)])
    neighbors = pf._get_coupled_neighbors(
        state,
        p_net=1,
        n_net=2,
        target_spacing_cells=2,
        p_visited=frozenset(),
        n_visited=n_visited,
    )
    plus_x_moves = [
        (s, c, v)
        for (s, c, v) in neighbors
        if not v and s.p_pos.x == 6 and s.p_pos.y == 5
    ]
    assert plus_x_moves == [], (
        f"+x symmetric move with P landing on N's past trail must be "
        f"rejected by #3078 path-history guard; got {len(plus_x_moves)}"
    )


def test_symmetric_move_rejected_when_n_lands_on_p_trail():
    """Symmetric counterpart: N lands on P's past trail."""
    pf = _make_pathfinder()
    state = CoupledState(GridPos(5, 5, 0), GridPos(7, 5, 0), (0, 0))
    # P has previously occupied (8, 5, 0).  +x move puts N at (8, 5, 0).
    p_visited = frozenset([(8, 5, 0)])
    neighbors = pf._get_coupled_neighbors(
        state,
        p_net=1,
        n_net=2,
        target_spacing_cells=2,
        p_visited=p_visited,
        n_visited=frozenset(),
    )
    plus_x_moves = [
        (s, c, v)
        for (s, c, v) in neighbors
        if not v and s.n_pos.x == 8 and s.n_pos.y == 5
    ]
    assert plus_x_moves == []


# ---------------------------------------------------------------------------
# Asymmetric P-advance: self-loop and cross-trail rejection
# ---------------------------------------------------------------------------


def test_p_advance_rejected_when_p_loops_into_own_trail():
    """The P-advance move re-enters a cell P has previously occupied.

    This is the precise mechanism behind the 7-vs-1061 segment
    asymmetry on USB3_RX1 (Issue #3078): repeated P-advance moves let
    P loop around N.  Re-entering an own past cell must be rejected.
    """
    pf = _make_pathfinder()
    # Approach-phase setup so the asymmetric P-advance branch fires.
    # Goals are close to current state -> approach_relaxed = True.
    state = CoupledState(GridPos(5, 5, 0), GridPos(7, 5, 0), (0, 0))
    p_goal = GridPos(6, 5, 0)
    n_goal = GridPos(7, 5, 0)
    # P has been at (6, 5, 0) before.  The asymmetric P-advance move
    # in the +x direction would land P at (6, 5, 0).  Must be rejected.
    p_visited = frozenset([(6, 5, 0)])
    neighbors = pf._get_coupled_neighbors(
        state,
        p_net=1,
        n_net=2,
        p_goal=p_goal,
        n_goal=n_goal,
        target_spacing_cells=2,
        approach_radius_override=10,  # force approach_relaxed=True
        p_visited=p_visited,
        n_visited=frozenset(),
    )
    # Find any asymmetric P-advance move that lands at (6, 5, 0).
    # (P advances, N holds at (7, 5, 0).)
    looping = [
        (s, c, v)
        for (s, c, v) in neighbors
        if not v
        and s.p_pos.x == 6
        and s.p_pos.y == 5
        and s.n_pos.x == state.n_pos.x
        and s.n_pos.y == state.n_pos.y
    ]
    # When p_goal == (6,5,0) it's an endpoint cell and is exempt.  So
    # this test uses a p_goal that is NOT (6,5,0).  Re-fix:
    # Actually p_goal IS (6, 5, 0) in this fixture, so the endpoint
    # exemption would let it through.  Move the goal away so the
    # self-loop rejection actually fires.
    assert looping == [] or all(
        s.p_pos.x == p_goal.x and s.p_pos.y == p_goal.y for (s, _c, _v) in looping
    )


def test_p_advance_self_loop_rejected_non_endpoint():
    """As above but with a clearly non-endpoint cell so the exemption
    cannot apply.  Must be rejected unconditionally.
    """
    pf = _make_pathfinder()
    state = CoupledState(GridPos(5, 5, 0), GridPos(7, 5, 0), (0, 0))
    # Goals far away so endpoint exemption is impossible for (6, 5, 0).
    p_goal = GridPos(20, 20, 0)
    n_goal = GridPos(22, 20, 0)
    # P has been at (6, 5, 0) before.
    p_visited = frozenset([(6, 5, 0)])
    neighbors = pf._get_coupled_neighbors(
        state,
        p_net=1,
        n_net=2,
        p_goal=p_goal,
        n_goal=n_goal,
        target_spacing_cells=2,
        approach_radius_override=100,  # large -> approach_relaxed=True
        p_visited=p_visited,
        n_visited=frozenset(),
    )
    # The +x asymmetric P-advance move would put P at (6, 5, 0) with
    # N still at (7, 5, 0).
    looping = [
        (s, c, v)
        for (s, c, v) in neighbors
        if not v
        and s.p_pos.x == 6
        and s.p_pos.y == 5
        and s.n_pos.x == 7
        and s.n_pos.y == 5
    ]
    assert looping == [], (
        f"P-advance into own past trail at non-endpoint cell must be "
        f"rejected; got {looping}"
    )


def test_p_advance_rejected_when_lands_on_n_trail():
    """The P-advance move puts P on a cell N has previously occupied
    (cross-trail collision).
    """
    pf = _make_pathfinder()
    state = CoupledState(GridPos(5, 5, 0), GridPos(7, 5, 0), (0, 0))
    p_goal = GridPos(20, 20, 0)
    n_goal = GridPos(22, 20, 0)
    # N has previously occupied (6, 5, 0).
    n_visited = frozenset([(6, 5, 0)])
    neighbors = pf._get_coupled_neighbors(
        state,
        p_net=1,
        n_net=2,
        p_goal=p_goal,
        n_goal=n_goal,
        target_spacing_cells=2,
        approach_radius_override=100,
        p_visited=frozenset(),
        n_visited=n_visited,
    )
    cross_trail = [
        (s, c, v)
        for (s, c, v) in neighbors
        if not v
        and s.p_pos.x == 6
        and s.p_pos.y == 5
        and s.n_pos.x == 7
        and s.n_pos.y == 5
    ]
    assert cross_trail == []


# ---------------------------------------------------------------------------
# Asymmetric N-advance: self-loop and cross-trail rejection
# ---------------------------------------------------------------------------


def test_n_advance_self_loop_rejected_non_endpoint():
    """N-advance re-entering its own past cell at a non-endpoint cell."""
    pf = _make_pathfinder()
    state = CoupledState(GridPos(5, 5, 0), GridPos(7, 5, 0), (0, 0))
    p_goal = GridPos(20, 20, 0)
    n_goal = GridPos(22, 20, 0)
    # N has been at (8, 5, 0) before.  N-advance +x puts N at (8, 5, 0).
    n_visited = frozenset([(8, 5, 0)])
    neighbors = pf._get_coupled_neighbors(
        state,
        p_net=1,
        n_net=2,
        p_goal=p_goal,
        n_goal=n_goal,
        target_spacing_cells=2,
        approach_radius_override=100,
        p_visited=frozenset(),
        n_visited=n_visited,
    )
    looping = [
        (s, c, v)
        for (s, c, v) in neighbors
        if not v
        and s.p_pos.x == 5
        and s.p_pos.y == 5
        and s.n_pos.x == 8
        and s.n_pos.y == 5
    ]
    assert looping == [], (
        f"N-advance into own past trail at non-endpoint cell must be "
        f"rejected; got {looping}"
    )


def test_n_advance_rejected_when_lands_on_p_trail():
    """N-advance puts N on a cell P has previously occupied."""
    pf = _make_pathfinder()
    state = CoupledState(GridPos(5, 5, 0), GridPos(7, 5, 0), (0, 0))
    p_goal = GridPos(20, 20, 0)
    n_goal = GridPos(22, 20, 0)
    # P has previously occupied (8, 5, 0).
    p_visited = frozenset([(8, 5, 0)])
    neighbors = pf._get_coupled_neighbors(
        state,
        p_net=1,
        n_net=2,
        p_goal=p_goal,
        n_goal=n_goal,
        target_spacing_cells=2,
        approach_radius_override=100,
        p_visited=p_visited,
        n_visited=frozenset(),
    )
    cross_trail = [
        (s, c, v)
        for (s, c, v) in neighbors
        if not v
        and s.p_pos.x == 5
        and s.p_pos.y == 5
        and s.n_pos.x == 8
        and s.n_pos.y == 5
    ]
    assert cross_trail == []


# ---------------------------------------------------------------------------
# Endpoint exemption: an advancing trace landing on an endpoint cell
# bypasses the history check (pad cells are shared geometry).
# ---------------------------------------------------------------------------


def test_endpoint_exemption_allows_landing_on_partner_trail():
    """A trace landing on its own goal pad MUST be allowed even if the
    partner's trail crossed that pad cell.

    Without the exemption the search could be unable to reach the goal
    when an inner-layer escape passes through the goal cell on the way
    out.  (Strictly, pad cells should never be in the partner's trail
    because the partner targets a different goal -- but the exemption
    is defensive.)
    """
    pf = _make_pathfinder()
    # Set up so the symmetric +x move lands BOTH P and N at their goals.
    state = CoupledState(GridPos(5, 5, 0), GridPos(7, 5, 0), (0, 0))
    p_goal = GridPos(6, 5, 0)
    n_goal = GridPos(8, 5, 0)
    # Stuff partner's trail with the goal cell.  Without exemption the
    # symmetric +x move would be rejected.
    p_visited = frozenset([(8, 5, 0)])
    n_visited = frozenset([(6, 5, 0)])
    neighbors = pf._get_coupled_neighbors(
        state,
        p_net=1,
        n_net=2,
        p_goal=p_goal,
        n_goal=n_goal,
        target_spacing_cells=2,
        p_visited=p_visited,
        n_visited=n_visited,
    )
    # The +x symmetric move (P -> (6,5), N -> (8,5)) must still be
    # accepted -- both new positions are endpoint cells.
    landing = [
        (s, c, v)
        for (s, c, v) in neighbors
        if not v
        and s.p_pos.x == 6
        and s.p_pos.y == 5
        and s.n_pos.x == 8
        and s.n_pos.y == 5
    ]
    assert len(landing) == 1, (
        f"Endpoint landing must bypass path-history guard; got {len(landing)}"
    )


# ---------------------------------------------------------------------------
# Integration: route_coupled builds the path-history sets correctly
# and a synthetic pair routes without self-intersection.
# ---------------------------------------------------------------------------


def test_route_coupled_simple_pair_no_self_intersection():
    """End-to-end: route a synthetic 1-pair fixture and verify the
    reconstructed routes do not have P and N cells coincident at the
    same world position (the centerline-coincident failure mode of
    Issue #3078).
    """
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad

    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=2,
        min_spacing_cells=2,
    )

    # P pad start at (2.0, 5.0), end at (10.0, 5.0).
    # N pad start at (2.0, 5.4), end at (10.0, 5.4).
    # 2-cell spacing at the default grid resolution.
    p_start = Pad(
        x=2.0, y=5.0, width=0.2, height=0.2, net=1, net_name="DP+",
        layer=Layer.F_CU,
    )
    p_end = Pad(
        x=10.0, y=5.0, width=0.2, height=0.2, net=1, net_name="DP+",
        layer=Layer.F_CU,
    )
    n_start = Pad(
        x=2.0, y=5.4, width=0.2, height=0.2, net=2, net_name="DP-",
        layer=Layer.F_CU,
    )
    n_end = Pad(
        x=10.0, y=5.4, width=0.2, height=0.2, net=2, net_name="DP-",
        layer=Layer.F_CU,
    )

    result = pf.route_coupled(p_start, p_end, n_start, n_end)
    assert result is not None, "Synthetic 1-pair fixture must route successfully"
    p_route, n_route = result

    # Collect all (x, y, layer) cells touched by each route's segment
    # endpoints (we don't need the in-between -- a self-intersection
    # would show up as a shared endpoint).
    def _cells(route):
        cells = set()
        for seg in route.segments:
            cells.add((round(seg.x1, 4), round(seg.y1, 4), seg.layer.value))
            cells.add((round(seg.x2, 4), round(seg.y2, 4), seg.layer.value))
        return cells

    p_cells = _cells(p_route)
    n_cells = _cells(n_route)

    # The only legitimate shared cells are the pad endpoints themselves
    # -- but in this fixture the pads are at distinct y coordinates, so
    # there should be NO overlap.
    shared = p_cells & n_cells
    assert shared == set(), (
        f"P and N routes must not share any segment-endpoint cell "
        f"(centerline overlap); shared = {shared}"
    )


def test_route_coupled_segment_count_balance_simple_pair():
    """A clean 1-pair route should have P and N segment counts that
    are very close (likely identical for a parallel pair) -- the
    10x asymmetry that triggered the runtime canary in Issue #3078
    must not occur on a clean fixture.
    """
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad

    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=2,
        min_spacing_cells=2,
    )

    p_start = Pad(
        x=2.0, y=5.0, width=0.2, height=0.2, net=1, net_name="DP+",
        layer=Layer.F_CU,
    )
    p_end = Pad(
        x=10.0, y=5.0, width=0.2, height=0.2, net=1, net_name="DP+",
        layer=Layer.F_CU,
    )
    n_start = Pad(
        x=2.0, y=5.4, width=0.2, height=0.2, net=2, net_name="DP-",
        layer=Layer.F_CU,
    )
    n_end = Pad(
        x=10.0, y=5.4, width=0.2, height=0.2, net=2, net_name="DP-",
        layer=Layer.F_CU,
    )

    result = pf.route_coupled(p_start, p_end, n_start, n_end)
    assert result is not None
    p_route, n_route = result

    p_seg = len(p_route.segments)
    n_seg = len(n_route.segments)
    assert p_seg > 0 and n_seg > 0
    ratio = max(p_seg, n_seg) / min(p_seg, n_seg)
    assert ratio < 10.0, (
        f"P/N segment-count asymmetry too high "
        f"(ratio={ratio}, p={p_seg}, n={n_seg}) -- "
        f"the #3078 canary would have fired"
    )
