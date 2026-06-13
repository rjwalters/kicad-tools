"""Tests for the CoupledPathfinder min_spacing_cells floor (issue #3012).

The CoupledPathfinder used to permit the search to drop within-pair
spacing to ``0`` cells during the "approach phase" near goal pads, and
to permit the asymmetric "converge" moves to collapse spacing without a
floor.  When a per-pair ``NetClassRouting.intra_pair_clearance`` exceeds
the default rule clearance, the resulting routes had P/N centerlines
that overlapped or were below the intra-pair threshold (manifesting as
``diffpair_clearance_intra`` violations of up to ``-0.150 mm``).

The fix:

1. ``CoupledPathfinder.__init__`` accepts ``min_spacing_cells`` as a
   hard floor on the Euclidean spacing between P and N grid positions.
2. ``_get_coupled_neighbors`` enforces ``new_spacing >= min_spacing_cells``
   in all three move classes (symmetric, P-advance, N-advance), with an
   exemption only when BOTH new positions are endpoint cells.
3. ``DiffPairRouter.route_differential_pair_coupled`` computes
   ``min_spacing_cells = ceil((trace_width + intra_pair_clearance) /
   grid.resolution)`` from the per-pair ``NetClassRouting`` and passes
   it through.
4. The 2-pad fast path in ``route_differential_pair_coupled`` now
   detects polarity-swap (P/N rows inverted between the two endpoint
   footprints) via ``_polarity_swap_between`` -- previously only the
   ``_pair_pads_for_coupled_routing_npad`` (N-pad) path did so.  Without
   this the 2-pad fast path silently set ``polarity_swap=False`` for
   inverted-row layouts (board 07 DDR strobe), and the coupled search
   collapsed spacing mid-run instead of dropping a swap-via.
"""

from __future__ import annotations

import math

from kicad_tools.router.diffpair_routing import CoupledPathfinder, CoupledState, GridPos
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.rules import DesignRules


def _make_pathfinder(min_spacing_cells: int) -> CoupledPathfinder:
    """Construct a CoupledPathfinder with a small grid for unit tests."""
    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    return CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=8,  # 8-cell target (board-07 MIPI start pitch)
        min_spacing_cells=min_spacing_cells,
    )


# ---------------------------------------------------------------------------
# CoupledPathfinder.__init__ stores min_spacing_cells
# ---------------------------------------------------------------------------


def test_min_spacing_cells_defaults_to_zero():
    """Legacy callers that don't supply ``min_spacing_cells`` get zero (no floor)."""
    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    pf = CoupledPathfinder(grid=grid, rules=rules, target_spacing_cells=2)
    assert pf.min_spacing_cells == 0


def test_min_spacing_cells_negative_clamped_to_zero():
    """Negative ``min_spacing_cells`` (defensive) is clamped to zero."""
    pf = _make_pathfinder(min_spacing_cells=-3)
    assert pf.min_spacing_cells == 0


def test_min_spacing_cells_stored_positive():
    """Positive ``min_spacing_cells`` is stored as-is."""
    pf = _make_pathfinder(min_spacing_cells=2)
    assert pf.min_spacing_cells == 2


# ---------------------------------------------------------------------------
# Symmetric move floor: spacing below floor is rejected (no endpoint bypass)
# ---------------------------------------------------------------------------


def test_symmetric_move_floor_rejects_below_floor():
    """When both new positions sit below the floor, the symmetric move is rejected.

    Set up a state where P and N are at small spacing (1 cell), so all
    symmetric moves keep them at 1 cell.  With ``min_spacing_cells=2``,
    every symmetric move must be rejected (no neighbor returned for the
    symmetric direction-set).
    """
    pf = _make_pathfinder(min_spacing_cells=2)
    # P at (10, 10), N at (11, 10).  Spacing = 1 cell.
    state = CoupledState(GridPos(10, 10, 0), GridPos(11, 10, 0), (0, 0))
    # No goals -- approach phase off (approach_relaxed = False).
    neighbors = pf._get_coupled_neighbors(state, p_net=1, n_net=2, target_spacing_cells=1)
    # All symmetric neighbors would keep spacing at 1 cell.  Without the
    # floor (target=1, tol=1) they'd pass the tolerance check; with the
    # floor (>= 2) they must be rejected.  Vias don't change spacing
    # either, so the only allowed move is "no symmetric move accepted".
    symmetric_moves = [
        (s, c, v)
        for (s, c, v) in neighbors
        if not v  # not via
    ]
    assert symmetric_moves == [], (
        f"Expected zero symmetric moves with min_spacing_cells=2 from a "
        f"1-cell-spacing state; got {len(symmetric_moves)}"
    )


def test_symmetric_move_floor_accepts_at_floor():
    """Spacing exactly at the floor (within epsilon) is accepted."""
    pf = _make_pathfinder(min_spacing_cells=2)
    # P at (10, 10), N at (12, 10).  Spacing = 2 cells exactly.
    state = CoupledState(GridPos(10, 10, 0), GridPos(12, 10, 0), (0, 0))
    neighbors = pf._get_coupled_neighbors(state, p_net=1, n_net=2, target_spacing_cells=2)
    # Symmetric moves at spacing=2 should be accepted (tolerance=1 around target=2).
    symmetric_moves = [(s, c, v) for (s, c, v) in neighbors if not v]
    assert len(symmetric_moves) > 0


# ---------------------------------------------------------------------------
# Approach-phase: my floor still bites despite the widened tolerance
# ---------------------------------------------------------------------------


def test_approach_phase_floor_still_enforced():
    """In the approach phase the tolerance widens, but the floor still bites.

    With target=8 and approach_relaxed=True, tolerance = max(1, 8) = 8.
    Naive search allows spacing in [0, 16].  My floor (=2) restricts
    that to [2, 16].  Every accepted move must keep spacing >= 2
    UNLESS both new positions sit on endpoint cells (start or goal).
    """
    pf = _make_pathfinder(min_spacing_cells=2)
    # P at (10, 10), N at (11, 10) -- spacing = 1 cell.  Goal at (10, 9)
    # for P and (12, 9) for N (very close so approach_relaxed=True).
    state = CoupledState(GridPos(10, 10, 0), GridPos(11, 10, 0), (0, 0))
    p_goal = GridPos(10, 9, 0)
    n_goal = GridPos(12, 9, 0)
    neighbors = pf._get_coupled_neighbors(
        state,
        p_net=1,
        n_net=2,
        p_goal=p_goal,
        n_goal=n_goal,
        target_spacing_cells=8,
    )
    # All accepted segment moves must respect the floor, with the
    # endpoint bypass: spacing < 2 cells is only acceptable when both
    # candidate positions are on the goal/start cells.
    for st, _cost, is_via in neighbors:
        if is_via:
            continue
        sp = math.sqrt((st.p_pos.x - st.n_pos.x) ** 2 + (st.p_pos.y - st.n_pos.y) ** 2)
        p_at_endpoint = (st.p_pos.x == p_goal.x and st.p_pos.y == p_goal.y) or (
            st.p_pos.x == state.p_pos.x and st.p_pos.y == state.p_pos.y
        )
        n_at_endpoint = (st.n_pos.x == n_goal.x and st.n_pos.y == n_goal.y) or (
            st.n_pos.x == state.n_pos.x and st.n_pos.y == state.n_pos.y
        )
        if p_at_endpoint and n_at_endpoint:
            continue
        assert sp + 1e-9 >= pf.min_spacing_cells, (
            f"Approach-phase tolerance must not bypass the floor: "
            f"got spacing={sp} at ({st.p_pos.x},{st.p_pos.y})/"
            f"({st.n_pos.x},{st.n_pos.y})"
        )


# ---------------------------------------------------------------------------
# Endpoint bypass: when both P and N are at their goal/start pad, the
# floor is bypassed so the search can land on physically narrow-pitch
# pads (e.g. USB-C 0.5 mm pitch).
# ---------------------------------------------------------------------------


def test_floor_bypassed_at_both_endpoint_cells():
    """Both P and N at goal cells: floor is bypassed.

    The pads themselves define the spacing here; the router has no
    choice but to land on them.  Without the bypass the search would
    never reach narrow-pitch goal pads (USB-C 0.5 mm).
    """
    pf = _make_pathfinder(min_spacing_cells=4)
    # Set goals so both P and N candidate positions ARE the goals.
    p_goal = GridPos(10, 10, 0)
    n_goal = GridPos(11, 10, 0)  # 1 cell apart -- below floor of 4
    # Start state: P one step away from p_goal, N one step away from n_goal.
    state = CoupledState(GridPos(9, 10, 0), GridPos(10, 10, 0), (1, 0))
    neighbors = pf._get_coupled_neighbors(
        state,
        p_net=1,
        n_net=2,
        p_goal=p_goal,
        n_goal=n_goal,
        target_spacing_cells=1,
    )
    # The +x move drops both into goal cells at spacing=1.  Even though
    # spacing < floor (1 < 4), the move must be accepted because both
    # candidate positions are endpoint cells.
    landing_moves = [
        (s, c, v)
        for (s, c, v) in neighbors
        if not v
        and s.p_pos.x == p_goal.x
        and s.p_pos.y == p_goal.y
        and s.n_pos.x == n_goal.x
        and s.n_pos.y == n_goal.y
    ]
    assert len(landing_moves) == 1, (
        f"Endpoint landing move must bypass the floor; got {len(landing_moves)}"
    )


# ---------------------------------------------------------------------------
# Asymmetric "converge" moves (only fire in approach phase) also respect
# the floor.
# ---------------------------------------------------------------------------


def test_asymmetric_moves_respect_floor():
    """Asymmetric P-advance / N-advance moves must respect the floor too.

    Without my fix, the asymmetric ``approach-relaxed`` branch
    permitted spacing to fall to zero (it only checked the tolerance
    around target_spacing_cells, which in approach phase is
    max(1, target_spacing_cells) -- on a target of 8 that's a window
    of [0, 16]).
    """
    pf = _make_pathfinder(min_spacing_cells=3)
    # P at (10, 10), N at (13, 10) -- spacing = 3 cells exactly.  An
    # asymmetric "N advances right" move would put N at (14, 10),
    # taking spacing to 4 cells (allowed).  An asymmetric "P advances
    # right" move would put P at (11, 10), taking spacing to 2 cells
    # (below my floor of 3) -- must be rejected.
    state = CoupledState(GridPos(10, 10, 0), GridPos(13, 10, 0), (0, 0))
    neighbors = pf._get_coupled_neighbors(
        state,
        p_net=1,
        n_net=2,
        p_goal=GridPos(10, 10, 0),  # P at goal -> approach phase
        n_goal=GridPos(14, 10, 0),  # N 1 cell from goal -> approach phase
        target_spacing_cells=3,
    )
    # Find any asymmetric move that would put spacing < 3.  None should
    # exist.
    for st, _cost, is_via in neighbors:
        if is_via:
            continue
        dx_sp = st.p_pos.x - st.n_pos.x
        dy_sp = st.p_pos.y - st.n_pos.y
        sp = math.sqrt(dx_sp * dx_sp + dy_sp * dy_sp)
        # Endpoint bypass: both at their goal cells.
        p_at_goal = st.p_pos.x == 10 and st.p_pos.y == 10
        n_at_goal = st.n_pos.x == 14 and st.n_pos.y == 10
        if p_at_goal and n_at_goal:
            continue
        assert sp + 1e-9 >= 3, (
            f"Asymmetric move produced spacing={sp} (< floor=3) at "
            f"({st.p_pos.x},{st.p_pos.y})/({st.n_pos.x},{st.n_pos.y})"
        )
