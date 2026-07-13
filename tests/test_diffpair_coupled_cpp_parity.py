"""Parity tests for the Issue #4065 C++ coupled diff-pair A* port.

Compares the C++ ``CoupledPathfinder`` joint-state search against the
pure-Python ``route_coupled`` fallback on the established synthetic
fixtures.  Per the curator's Section 4 the parity bar is COST-EQUALITY (and
route validity), not byte-identical geometry -- A* with a LIFO seq tie-break
is unique only up to equal-cost paths, and the two implementations enumerate
moves in independently-written order.

Test axes:
- cost-equality: total routed length (segment count / g-equivalent) agrees.
- determinism: 3x repeat through each backend is stable run-to-run.
- corridor-bounded parity: the path board 07 actually exercises by default.
- fallback correctness: with the backend forced off, ``route_coupled`` uses
  the pure-Python search and produces a correct route.
- budget-exit diagnostics parity: a tiny iteration budget blows on both
  backends with consistent ``last_timeout_exceeded`` / ``last_iteration_limited``.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair_routing import (
    CoupledPathfinder,
    build_corridor_mask,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ coupled parity requires the router_cpp backend (kct build-native)",
)


def _make_grid(width: float = 12.7, height: float = 12.7) -> RoutingGrid:
    rules = DesignRules()
    return RoutingGrid(width=width, height=height, rules=rules)


def _make_simple_pair_pads() -> tuple[Pad, Pad, Pad, Pad]:
    """A straight horizontal pair: P at y=4mm, N at y=6mm, x 2->10mm."""
    p_start = Pad(x=2.0, y=4.0, width=0.4, height=0.4, net=1, net_name="D+", layer=Layer.F_CU)
    p_end = Pad(x=10.0, y=4.0, width=0.4, height=0.4, net=1, net_name="D+", layer=Layer.F_CU)
    n_start = Pad(x=2.0, y=6.0, width=0.4, height=0.4, net=2, net_name="D-", layer=Layer.F_CU)
    n_end = Pad(x=10.0, y=6.0, width=0.4, height=0.4, net=2, net_name="D-", layer=Layer.F_CU)
    return p_start, p_end, n_start, n_end


def _straight_guide_route(grid: RoutingGrid, y: float = 5.0) -> Route:
    route = Route(net=1, net_name="GUIDE")
    route.segments.append(
        Segment(x1=2.0, y1=y, x2=10.0, y2=y, width=0.2, layer=Layer.F_CU, net=1, net_name="GUIDE")
    )
    return route


def _route_length(route: Route) -> float:
    total = 0.0
    for seg in route.segments:
        total += math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)
    return total


def _make_pf(grid: RoutingGrid, use_cpp: bool) -> CoupledPathfinder:
    pf = CoupledPathfinder(
        grid=grid, rules=DesignRules(), target_spacing_cells=2, min_spacing_cells=2
    )
    pf._use_cpp_coupled = use_cpp
    return pf


def _corridor(grid: RoutingGrid, pads: tuple[Pad, Pad, Pad, Pad]) -> frozenset:
    p_start, p_end, n_start, n_end = pads
    # Guide down the pair centerline (y=5mm is between the P/N pads at y=4/6),
    # with a radius wide enough to admit both heads plus maneuvering slack.
    return build_corridor_mask(
        grid,
        _straight_guide_route(grid, y=5.0),
        radius_cells=20,
        extra_cells=(
            grid.world_to_grid(p_start.x, p_start.y),
            grid.world_to_grid(p_end.x, p_end.y),
            grid.world_to_grid(n_start.x, n_start.y),
            grid.world_to_grid(n_end.x, n_end.y),
        ),
    )


# ---------------------------------------------------------------------------
# Cost-equality parity
# ---------------------------------------------------------------------------


def test_cpp_and_python_agree_on_open_search():
    """Both backends route the simple pair and agree on routed length."""
    pads = _make_simple_pair_pads()

    py_pf = _make_pf(_make_grid(), use_cpp=False)
    cpp_pf = _make_pf(_make_grid(), use_cpp=True)

    py_res = py_pf.route_coupled(*pads)
    cpp_res = cpp_pf.route_coupled(*pads)

    assert py_res is not None, "python fallback must route the simple pair"
    assert cpp_res is not None, "C++ coupled search must route the simple pair"

    py_p, py_n = py_res
    cpp_p, cpp_n = cpp_res

    # Cost-equality: total routed lengths agree within a grid cell.
    assert _route_length(cpp_p) == pytest.approx(_route_length(py_p), abs=0.2)
    assert _route_length(cpp_n) == pytest.approx(_route_length(py_n), abs=0.2)

    # Both routes are non-trivial and use the same nets.
    assert cpp_p.net == 1 and cpp_n.net == 2
    assert len(cpp_p.segments) > 0 and len(cpp_n.segments) > 0


def test_cpp_and_python_agree_inside_corridor():
    """Corridor-bounded parity -- the board-07 default path."""
    pads = _make_simple_pair_pads()

    py_grid = _make_grid()
    cpp_grid = _make_grid()
    py_pf = _make_pf(py_grid, use_cpp=False)
    cpp_pf = _make_pf(cpp_grid, use_cpp=True)

    py_res = py_pf.route_coupled(*pads, corridor=_corridor(py_grid, pads))
    cpp_res = cpp_pf.route_coupled(*pads, corridor=_corridor(cpp_grid, pads))

    assert py_res is not None and cpp_res is not None
    py_p, py_n = py_res
    cpp_p, cpp_n = cpp_res
    assert _route_length(cpp_p) == pytest.approx(_route_length(py_p), abs=0.2)
    assert _route_length(cpp_n) == pytest.approx(_route_length(py_n), abs=0.2)


# ---------------------------------------------------------------------------
# Determinism (3x repeat)
# ---------------------------------------------------------------------------


def test_cpp_coupled_deterministic():
    """The C++ coupled search is stable run-to-run on the same fixture."""
    pads = _make_simple_pair_pads()
    lengths = []
    for _ in range(3):
        pf = _make_pf(_make_grid(), use_cpp=True)
        res = pf.route_coupled(*pads)
        assert res is not None
        p, n = res
        lengths.append((round(_route_length(p), 6), round(_route_length(n), 6)))
    assert lengths[0] == lengths[1] == lengths[2], f"non-deterministic C++ result: {lengths}"


def test_python_coupled_deterministic():
    """The pure-Python fallback is stable run-to-run (regression guard)."""
    pads = _make_simple_pair_pads()
    lengths = []
    for _ in range(3):
        pf = _make_pf(_make_grid(), use_cpp=False)
        res = pf.route_coupled(*pads)
        assert res is not None
        p, n = res
        lengths.append((round(_route_length(p), 6), round(_route_length(n), 6)))
    assert lengths[0] == lengths[1] == lengths[2]


# ---------------------------------------------------------------------------
# Fallback correctness (backend forced off)
# ---------------------------------------------------------------------------


def test_route_coupled_falls_back_when_cpp_disabled():
    """With ``_use_cpp_coupled`` off, ``route_coupled`` uses pure Python and
    still routes correctly -- the backend-selection contract."""
    pads = _make_simple_pair_pads()
    pf = _make_pf(_make_grid(), use_cpp=False)
    assert not pf._cpp_coupled_available()
    res = pf.route_coupled(*pads)
    assert res is not None
    p, n = res
    assert len(p.segments) > 0 and len(n.segments) > 0


def test_swap_via_and_manhattan_defer_to_python():
    """The v1-deferred features must NOT be handled by the C++ path."""
    grid = _make_grid()
    # allow_swap_via -> deferred.
    pf_swap = CoupledPathfinder(
        grid=grid, rules=DesignRules(), target_spacing_cells=2, allow_swap_via=True
    )
    assert not pf_swap._cpp_coupled_available()
    # manhattan_sum heuristic -> deferred.
    pf_man = CoupledPathfinder(
        grid=grid,
        rules=DesignRules(),
        target_spacing_cells=2,
        heuristic_mode="manhattan_sum",
    )
    assert not pf_man._cpp_coupled_available()


# ---------------------------------------------------------------------------
# Budget-exit diagnostics parity
# ---------------------------------------------------------------------------


def test_budget_exit_diagnostics_parity():
    """A tiny iteration budget blows on both backends; the boolean flags and
    'made no progress to goal' signal agree (values need not be identical)."""
    pads = _make_simple_pair_pads()

    py_pf = _make_pf(_make_grid(), use_cpp=False)
    cpp_pf = _make_pf(_make_grid(), use_cpp=True)

    tiny = 5
    py_res = py_pf.route_coupled(*pads, max_iterations_budget=tiny)
    cpp_res = cpp_pf.route_coupled(*pads, max_iterations_budget=tiny)

    # Both must fail (budget too small to converge).
    assert py_res is None
    assert cpp_res is None
    # Both must classify the exit as an ITERATION-budget timeout.
    assert py_pf.last_timeout_exceeded is True
    assert cpp_pf.last_timeout_exceeded is True
    assert py_pf.last_iteration_limited is True
    assert cpp_pf.last_iteration_limited is True
    # Both consumed a bounded number of iterations near the budget.
    assert cpp_pf.last_iterations <= tiny
    assert py_pf.last_iterations <= tiny


def test_cpp_reports_best_progress_on_partial_search():
    """The C++ search populates ``last_best_progress`` even on a budget exit
    (the #4052 diagnostic vocabulary the epic depends on)."""
    pads = _make_simple_pair_pads()
    pf = _make_pf(_make_grid(), use_cpp=True)
    pf.route_coupled(*pads, max_iterations_budget=8)
    # best_progress is a finite non-negative joint remaining distance once at
    # least one node has been popped.
    assert pf.last_best_progress != float("inf")
    assert pf.last_best_progress >= 0


# ---------------------------------------------------------------------------
# Converging-search reach parity (Issue #4065 regression guard)
# ---------------------------------------------------------------------------
#
# The synthetic open/corridor pairs above route down a straight, obstacle-free
# channel: their frontier never accumulates many equal-``f_score`` /
# unequal-``g_score`` nodes, so the C++ comparator's now-removed ``g_score``
# secondary tie level was never exercised and the 8/8 gate stayed green while
# board-06's USB3 pairs regressed (20/21, USB3_RX1- dropped).  The tests below
# force a CONVERGING search -- an obstacle wall with a single gap that both
# heads must funnel through -- which is exactly the frontier shape where the
# extra g level reordered the pop sequence and drove the C++ route to a
# different (worse) outcome than Python.  Deterministic (fixed geometry, LIFO
# seq, no wall clock), so CI-stable.


def _block_wall_with_gap(
    grid: RoutingGrid,
    x_mm: float,
    gap_lo_mm: float,
    gap_hi_mm: float,
) -> None:
    """Block a full-height vertical wall at ``x_mm`` on every routable layer,
    leaving a single horizontal gap in ``[gap_lo_mm, gap_hi_mm]``.

    Both the P and N heads must funnel through the gap, forcing a converging
    frontier with many equal-f competing detour nodes -- the geometry that
    exposed the #4065 comparator divergence.
    """
    gx, _ = grid.world_to_grid(x_mm, 0.0)
    for layer in (Layer.F_CU, Layer.B_CU):
        layer_idx = grid.layer_to_index(layer.value)
        for gy in range(grid.rows):
            y_mm = grid.origin_y + gy * grid.resolution
            if gap_lo_mm <= y_mm <= gap_hi_mm:
                continue
            grid._blocked[layer_idx, gy, gx] = True
            grid._is_obstacle[layer_idx, gy, gx] = True


def _converging_pair_pads() -> tuple[Pad, Pad, Pad, Pad]:
    """A pair whose start/goal straddle a gap wall placed at x=6mm."""
    p_start = Pad(x=2.0, y=4.6, width=0.4, height=0.4, net=1, net_name="D+", layer=Layer.F_CU)
    p_end = Pad(x=10.0, y=4.6, width=0.4, height=0.4, net=1, net_name="D+", layer=Layer.F_CU)
    n_start = Pad(x=2.0, y=6.6, width=0.4, height=0.4, net=2, net_name="D-", layer=Layer.F_CU)
    n_end = Pad(x=10.0, y=6.6, width=0.4, height=0.4, net=2, net_name="D-", layer=Layer.F_CU)
    return p_start, p_end, n_start, n_end


def test_cpp_matches_python_on_converging_gap_search():
    """C++ and Python coupled searches reach the goal AND agree on cost when
    the frontier must converge through an obstacle gap.

    This is the board-06-representative regression guard: it is the frontier
    shape that dropped USB3_RX1- when the C++ comparator carried an extra
    ``g_score`` tie level Python does not have.  Reach parity (both route) and
    cost-equality here stand in for the full-board 21/21 reach check the CI
    diffpair-coverage gate runs, at unit-test cost.
    """
    pads = _converging_pair_pads()

    py_grid = _make_grid()
    cpp_grid = _make_grid()
    # Identical obstacle geometry on both grids (CppGrid.from_routing_grid
    # marshals the blocked/obstacle arrays, so the backends see the same wall).
    _block_wall_with_gap(py_grid, x_mm=6.0, gap_lo_mm=4.2, gap_hi_mm=7.0)
    _block_wall_with_gap(cpp_grid, x_mm=6.0, gap_lo_mm=4.2, gap_hi_mm=7.0)

    py_pf = _make_pf(py_grid, use_cpp=False)
    cpp_pf = _make_pf(cpp_grid, use_cpp=True)

    py_res = py_pf.route_coupled(*pads)
    cpp_res = cpp_pf.route_coupled(*pads)

    # REACH PARITY: both backends must route through the gap.  Before the
    # comparator fix the C++ search could diverge here (the board-06 failure
    # class); this assertion is the unit-scale analogue of "USB3_RX1- routes".
    assert py_res is not None, "python coupled must route through the gap"
    assert cpp_res is not None, (
        "C++ coupled must reach parity with Python and route through the gap "
        "(Issue #4065 reach regression guard)"
    )

    py_p, py_n = py_res
    cpp_p, cpp_n = cpp_res
    # COST PARITY: equal-cost up to a grid cell (A* is unique only up to
    # equal-cost paths; the comparator now shares Python's (f_score, seq) key).
    assert _route_length(cpp_p) == pytest.approx(_route_length(py_p), abs=0.3)
    assert _route_length(cpp_n) == pytest.approx(_route_length(py_n), abs=0.3)


def test_cpp_converging_search_is_deterministic():
    """The C++ converging-gap route is stable run-to-run (guards against a
    reintroduced allocator-dependent tie-break)."""
    pads = _converging_pair_pads()
    lengths = []
    for _ in range(3):
        grid = _make_grid()
        _block_wall_with_gap(grid, x_mm=6.0, gap_lo_mm=4.2, gap_hi_mm=7.0)
        pf = _make_pf(grid, use_cpp=True)
        res = pf.route_coupled(*pads)
        assert res is not None, "C++ converging-gap route must reach the goal"
        p, n = res
        lengths.append((round(_route_length(p), 6), round(_route_length(n), 6)))
    assert lengths[0] == lengths[1] == lengths[2], f"non-deterministic C++ result: {lengths}"


# ---------------------------------------------------------------------------
# Issue #4080: corridor attractor in the coupled joint-state cost loop
# ---------------------------------------------------------------------------
#
# The single-ended attractor (#4071) discounts a reserved cell's step cost for
# the owning net.  Issue #4080 wires the same discount into the coupled
# joint-state loop -- both the pure-Python ``_get_coupled_neighbors`` and the
# C++ ``CoupledPathfinder``.  These tests prove:
#   (a) the per-net discount fires through the coupled loop for the owning net
#       set and NOT for a foreign net / unreserved cell (mirrors
#       ``test_grid_cpp_parity.py::test_cpp_attractor_discounts_owning_net_step_cost``
#       but exercised through the joint-state neighbor expansion), and
#   (b) with a corridor reserved for the pair's net set, both backends still
#       agree on routed cost (parity is preserved with the attractor active).


def _reserve_pair_corridor(grid: RoutingGrid, pads, p_net: int, n_net: int) -> int:
    """Reserve every cell on the F_CU layer along the pair centerline band
    for the ``{p_net, n_net}`` owner set.

    The band spans the y-range the P and N heads sweep (their pads plus the
    coupled channel between them) across the full x-span of the run, so any
    joint-state move the search takes lands on reserved cells and picks up the
    attractor discount.
    """
    p_start, p_end, n_start, n_end = pads
    layer_idx = grid.layer_to_index(Layer.F_CU.value)
    gx_lo, gy_lo = grid.world_to_grid(min(p_start.x, p_end.x), min(p_start.y, p_end.y))
    gx_hi, gy_hi = grid.world_to_grid(max(n_start.x, n_end.x), max(n_start.y, n_end.y))
    cells = [(gx, gy) for gx in range(gx_lo, gx_hi + 1) for gy in range(gy_lo, gy_hi + 1)]
    return grid.reserve_corridor_cells(layer_idx, cells, frozenset({p_net, n_net}))


def test_coupled_attractor_discount_fires_only_for_owning_pair():
    """The coupled attractor discounts a reserved-corridor step for the pair's
    own nets, and returns 0 for a foreign net / unreserved cell.

    Low-level analogue of ``test_cpp_attractor_discounts_owning_net_step_cost``
    driven through the joint-state cost sites: a symmetric coupled move that
    lands both heads on reserved cells must cost ``cost_corridor_attractor``
    (one per net) less than the same move on an unreserved grid.
    """
    from kicad_tools.router.cpp_backend import CppGrid
    from kicad_tools.router.diffpair_routing import CoupledState, GridPos

    rules = DesignRules()
    bonus = rules.cost_corridor_attractor
    p_net, n_net = 1, 2
    # P and N heads exactly ``target_spacing_cells`` (=2) apart so a symmetric
    # +x step is spacing-legal and produces a straight coupled continuation.
    p_gx, p_gy = 30, 40
    n_gx, n_gy = 30, 42  # 2 cells below P
    layer_idx = _make_grid().layer_to_index(Layer.F_CU.value)

    def _min_straight_cost(grid: RoutingGrid) -> float:
        pf = _make_pf(grid, use_cpp=False)
        state = CoupledState(GridPos(p_gx, p_gy, layer_idx), GridPos(n_gx, n_gy, layer_idx), (1, 0))
        neighbors = pf._get_coupled_neighbors(state, p_net, n_net)
        # The straight +x continuation (no turn) is the cheapest non-via move.
        straight = [cost for st, cost, is_via in neighbors if not is_via and st.direction == (1, 0)]
        assert straight, "expected a straight +x coupled continuation move"
        return min(straight)

    # Plain grid: no reservation, no discount.
    plain = _make_grid()
    plain_cost = _min_straight_cost(plain)

    # Reserved grid: reserve exactly the two +x landing cells for the pair.
    reserved = _make_grid()
    n_reserved = reserved.reserve_corridor_cells(
        layer_idx, [(p_gx + 1, p_gy), (n_gx + 1, n_gy)], frozenset({p_net, n_net})
    )
    assert n_reserved == 2, "fixture must reserve both landing cells"
    reserved_cost = _min_straight_cost(reserved)

    # Both P and N land on reserved cells => two bonuses subtracted.
    assert reserved_cost == pytest.approx(max(0.0, plain_cost - 2.0 * bonus))

    # A foreign net must NOT pick up the discount: the same reserved grid,
    # queried with net ids not in the owner set, yields the plain cost.
    foreign_pf = _make_pf(reserved, use_cpp=False)
    foreign_state = CoupledState(
        GridPos(p_gx, p_gy, layer_idx), GridPos(n_gx, n_gy, layer_idx), (1, 0)
    )
    foreign_neighbors = foreign_pf._get_coupled_neighbors(foreign_state, 888, 999)
    foreign_straight = [
        c for st, c, is_via in foreign_neighbors if not is_via and st.direction == (1, 0)
    ]
    assert foreign_straight, "expected a straight +x move for the foreign query too"
    assert min(foreign_straight) == pytest.approx(plain_cost)

    # C++ primitive parity: the reserved cells discount for the owning nets
    # and NOT for a foreign net, matching the Python query the loop makes.
    cpp = CppGrid.from_routing_grid(reserved)
    gx, gy = p_gx + 1, p_gy
    assert cpp._impl.corridor_attractor_bonus(gx, gy, layer_idx, p_net, bonus) == pytest.approx(
        reserved.get_corridor_attractor_bonus(layer_idx, gx, gy, p_net, bonus)
    )
    assert cpp._impl.corridor_attractor_bonus(gx, gy, layer_idx, 999, bonus) == pytest.approx(0.0)


def test_cpp_and_python_agree_with_reserved_corridor():
    """Reservation-fixture parity: with a corridor reserved for the pair's net
    set, both backends route the pair AND agree on routed cost.

    Guards the Issue #4080 acceptance criterion that the coupled path honors
    the attractor with Python/C++ parity -- the per-net discount is applied
    identically on both sides, so the two backends do not diverge when a
    corridor is active.
    """
    pads = _make_simple_pair_pads()
    p_net, n_net = 1, 2

    py_grid = _make_grid()
    cpp_grid = _make_grid()
    py_n = _reserve_pair_corridor(py_grid, pads, p_net, n_net)
    cpp_n = _reserve_pair_corridor(cpp_grid, pads, p_net, n_net)
    assert py_n == cpp_n and py_n > 0

    py_pf = _make_pf(py_grid, use_cpp=False)
    cpp_pf = _make_pf(cpp_grid, use_cpp=True)

    py_res = py_pf.route_coupled(*pads)
    cpp_res = cpp_pf.route_coupled(*pads)

    assert py_res is not None, "python fallback must route the reserved pair"
    assert cpp_res is not None, "C++ coupled must route the reserved pair"

    py_p, py_n_route = py_res
    cpp_p, cpp_n_route = cpp_res

    # Cost-equality with the attractor active on both backends.
    assert _route_length(cpp_p) == pytest.approx(_route_length(py_p), abs=0.2)
    assert _route_length(cpp_n_route) == pytest.approx(_route_length(py_n_route), abs=0.2)
