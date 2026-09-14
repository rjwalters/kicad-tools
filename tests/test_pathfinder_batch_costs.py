"""Regression coverage for Router._batch_congestion_costs / _batch_turn_costs
(Issue #5240).

CI runtime investigation: these two methods compute per-neighbor A* costs
for every single node expansion in the pure-Python router fallback
(:meth:`kicad_tools.router.pathfinder.Router._route_impl`). Since
``Router.neighbors_2d`` only ever has 4 (orthogonal) or 8 (orthogonal +
diagonal) entries, the original NumPy-vectorized implementation paid for
~10 temporary array allocations and per-call ufunc dispatch overhead on
every node expansion just to operate on a handful of scalars -- profiling
``TestStagnationRecovery::test_stagnation_recovery_fires_on_oscillating_cohort``
(a deliberately tiny 3-net board that heavily exercises the Python A*
fallback) showed ``_batch_congestion_costs`` as the single hottest
function in that path.

This module locks in that the scalar-loop replacement produces IDENTICAL
costs to the original vectorized formula (kept here as an independent
reference implementation) across many random grid states, neighbor
offsets and out-of-bounds edge cases -- the same "independently reproduce
the original equation" pattern used by the sampled-force-calculation
regression coverage from Issue #5240's #5253 partial increment.
"""

from __future__ import annotations

import random

import numpy as np
import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.rules import DesignRules

# ---------------------------------------------------------------------------
# Independent reference implementation (the ORIGINAL vectorized formula,
# Issue #963). Kept intentionally separate from pathfinder.py so a future
# accidental edit to the production method cannot silently drag this
# reference along with it.
# ---------------------------------------------------------------------------


def _reference_batch_congestion_costs(
    router: Router, current_x: int, current_y: int, layer: int
) -> np.ndarray:
    nx_arr = current_x + router._neighbor_dx
    ny_arr = current_y + router._neighbor_dy

    valid = (
        (nx_arr >= 0) & (nx_arr < router.grid.cols) & (ny_arr >= 0) & (ny_arr < router.grid.rows)
    )

    congestion_size = router.grid.congestion_size
    cx_arr = np.minimum(nx_arr // congestion_size, router.grid.congestion_cols - 1)
    cy_arr = np.minimum(ny_arr // congestion_size, router.grid.congestion_rows - 1)

    costs = np.zeros(len(router.neighbors_2d), dtype=np.float64)

    valid_indices = np.where(valid)[0]
    if len(valid_indices) == 0:
        return costs

    max_cells = congestion_size * congestion_size
    congestion_counts = router.grid._congestion[layer, cy_arr[valid_indices], cx_arr[valid_indices]]
    congestion_levels = np.minimum(1.0, congestion_counts / max_cells)

    threshold = router.rules.congestion_threshold
    exceeds = congestion_levels > threshold
    excess = np.maximum(0, congestion_levels - threshold)
    valid_costs = np.where(exceeds, router.rules.cost_congestion * (1.0 + excess * 2.0), 0.0)
    costs[valid_indices] = valid_costs

    return costs


def _reference_batch_turn_costs(router: Router, current_direction: tuple[int, int]):
    if current_direction == (0, 0):
        return np.zeros(len(router.neighbors_2d), dtype=np.float64)

    dx_match = router._neighbor_dx == current_direction[0]
    dy_match = router._neighbor_dy == current_direction[1]
    matches = dx_match & dy_match

    return np.where(matches, 0.0, router.rules.cost_turn)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_router(diagonal_routing: bool, size_cells: int = 40) -> Router:
    rules = DesignRules()
    span = size_cells * rules.grid_resolution
    grid = RoutingGrid(width=span, height=span, rules=rules)
    return Router(grid, rules, diagonal_routing=diagonal_routing)


def _randomize_congestion(router: Router, rng: np.random.Generator, density: float) -> None:
    """Fill the congestion grid with a mix of zero and non-zero counts.

    Values run above and below ``congestion_grid_size**2`` so both the
    "below threshold" (cost 0) and "above threshold" (nonzero cost) branches
    of the reference formula are exercised.
    """
    arr = router.grid._congestion
    max_val = router.grid.congestion_size**2 + 2
    mask = rng.random(arr.shape) < density
    vals = rng.integers(0, max_val, size=arr.shape)
    arr[mask] = vals[mask]


DIRECTIONS = [(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("diagonal_routing", [True, False])
def test_batch_congestion_costs_matches_reference(diagonal_routing: bool) -> None:
    """Scalar implementation matches the original vectorized formula exactly.

    Samples in-bounds cells, out-of-bounds cells (negative and beyond
    cols/rows), and every layer, against congestion counts that straddle
    the threshold in both directions.
    """
    router = _make_router(diagonal_routing)
    rng = np.random.default_rng(20260912)
    _randomize_congestion(router, rng, density=0.4)

    py_rng = random.Random(20260912)
    comparisons = 0
    for _ in range(500):
        x = py_rng.randint(-3, router.grid.cols + 2)
        y = py_rng.randint(-3, router.grid.rows + 2)
        layer = py_rng.randint(0, router.grid.num_layers - 1)

        expected = _reference_batch_congestion_costs(router, x, y, layer)
        actual = router._batch_congestion_costs(x, y, layer)

        assert list(actual) == pytest.approx(list(expected), abs=0.0)
        comparisons += 1

    assert comparisons == 500


@pytest.mark.parametrize("diagonal_routing", [True, False])
def test_batch_turn_costs_matches_reference(diagonal_routing: bool) -> None:
    router = _make_router(diagonal_routing)

    for direction in DIRECTIONS:
        expected = _reference_batch_turn_costs(router, direction)
        actual = router._batch_turn_costs(direction)
        assert list(actual) == pytest.approx(list(expected), abs=0.0)


def test_batch_congestion_costs_returns_plain_list() -> None:
    """The scalar rewrite drops the NumPy array allocation entirely.

    Callers only ever index the result by neighbor position (see
    ``Router._route_impl``), so a plain ``list[float]`` is a strict
    behavioral improvement (no allocation) with no observable difference
    to any caller.
    """
    router = _make_router(diagonal_routing=True)
    costs = router._batch_congestion_costs(0, 0, 0)
    assert isinstance(costs, list)
    assert len(costs) == len(router.neighbors_2d)

    turn_costs = router._batch_turn_costs((1, 0))
    assert isinstance(turn_costs, list)
    assert len(turn_costs) == len(router.neighbors_2d)


def test_batch_congestion_costs_all_out_of_bounds() -> None:
    """Every neighbor out of bounds -> all-zero costs, no crash."""
    router = _make_router(diagonal_routing=True)
    costs = router._batch_congestion_costs(-1000, -1000, 0)
    assert costs == [0.0] * len(router.neighbors_2d)
