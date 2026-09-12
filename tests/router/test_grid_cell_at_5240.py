"""``RoutingGrid.cell_at`` matches the legacy ``grid[layer][y][x]`` view chain.

Issue #5240 (CI runtime): profiling the pure-Python A* fallback's hot
neighbor-expansion loop (``Pathfinder._route_impl`` and helpers like
``_is_diagonal_corner_blocked``) showed millions of throwaway
``_LayerView``/``_RowView`` allocations per small re-route -- every
``self.grid.grid[layer][y][x]`` access walks three separate
``__getitem__`` calls and constructs two intermediate objects that exist
only to reach the next index.  ``RoutingGrid.cell_at(layer, y, x)``
returns the identical ``_CellView`` in one call instead of three, with
zero behavioral change: same type, same properties, same mutation
semantics.

These tests are a same-values correctness cross-check (mirroring the
independent-reference-implementation pattern used by the sibling
allocation-reduction fixes on this issue, #5253/#5256/#5267/#5269), not a
timing assertion -- CI runner variance makes wall-clock assertions flaky.
A reproducible local microbenchmark showing a ~1.4x speedup for the
accessor itself is included in the PR description.
"""

from __future__ import annotations

import random

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.rules import DesignRules


def _make_grid() -> RoutingGrid:
    rules = DesignRules(crossing_penalty=5.0, grid_resolution=0.5)
    return RoutingGrid(20.0, 20.0, rules)


def test_cell_at_returns_same_type_as_legacy_chain():
    """``cell_at`` returns the same runtime type as ``grid[layer][y][x]``."""
    grid = _make_grid()
    legacy = grid.grid[0][1][2]
    direct = grid.cell_at(0, 1, 2)
    assert type(direct) is type(legacy)


def test_cell_at_matches_legacy_chain_on_default_grid():
    """All read properties agree with the legacy chain on a freshly built grid."""
    grid = _make_grid()
    for layer in range(grid.num_layers):
        for y in (0, grid.rows // 2, grid.rows - 1):
            for x in (0, grid.cols // 2, grid.cols - 1):
                legacy = grid.grid[layer][y][x]
                direct = grid.cell_at(layer, y, x)
                assert direct.blocked == legacy.blocked
                assert direct.net == legacy.net
                assert direct.usage_count == legacy.usage_count
                assert direct.history_cost == legacy.history_cost
                assert direct.is_obstacle == legacy.is_obstacle
                assert direct.is_zone == legacy.is_zone
                assert direct.pad_blocked == legacy.pad_blocked
                assert direct.original_net == legacy.original_net
                assert direct.zone_id == legacy.zone_id
                assert direct.x == legacy.x
                assert direct.y == legacy.y
                assert direct.layer == legacy.layer


def test_cell_at_matches_legacy_chain_after_mutation():
    """Same values after cells are marked blocked/net/zone/obstacle/usage.

    Random cross-check across many coordinates (issue #5240 methodology:
    an independent read path compared against the original for a large
    sample, matching #5269's 1,600-sample batch-cost cross-check).
    """
    grid = _make_grid()
    rng = random.Random(42)

    mutated: list[tuple[int, int, int]] = []
    for i in range(500):
        layer = rng.randrange(grid.num_layers)
        y = rng.randrange(grid.rows)
        x = rng.randrange(grid.cols)
        grid._blocked[layer, y, x] = True
        grid._net[layer, y, x] = (i % 11) + 1
        grid._usage_count[layer, y, x] = i % 4
        grid._history_cost[layer, y, x] = float(i % 3) * 0.5
        grid._is_obstacle[layer, y, x] = i % 2 == 0
        grid._is_zone[layer, y, x] = i % 3 == 0
        grid._pad_blocked[layer, y, x] = i % 6 == 0
        grid._original_net[layer, y, x] = i % 5
        mutated.append((layer, y, x))

    sample_coords = mutated + [
        (
            rng.randrange(grid.num_layers),
            rng.randrange(grid.rows),
            rng.randrange(grid.cols),
        )
        for _ in range(500)
    ]

    for layer, y, x in sample_coords:
        legacy = grid.grid[layer][y][x]
        direct = grid.cell_at(layer, y, x)
        assert direct.blocked == legacy.blocked
        assert direct.net == legacy.net
        assert direct.usage_count == legacy.usage_count
        assert direct.history_cost == pytest.approx(legacy.history_cost)
        assert direct.is_obstacle == legacy.is_obstacle
        assert direct.is_zone == legacy.is_zone
        assert direct.pad_blocked == legacy.pad_blocked
        assert direct.original_net == legacy.original_net


def test_cell_at_write_through_matches_legacy_chain():
    """Writes via a ``cell_at`` cell view mutate the grid identically to the
    legacy chain (both are the same ``_CellView`` type over the same arrays).
    """
    grid = _make_grid()

    direct = grid.cell_at(1, 3, 4)
    direct.blocked = True
    direct.net = 7
    direct.usage_count = 2
    direct.is_obstacle = True
    direct.is_zone = True

    legacy = grid.grid[1][3][4]
    assert legacy.blocked is True
    assert legacy.net == 7
    assert legacy.usage_count == 2
    assert legacy.is_obstacle is True
    assert legacy.is_zone is True

    # And the occupancy generation counter (Issue #4794) still advances --
    # cell_at goes through the same _CellView setters, so this invariant
    # is untouched by the new accessor.
    assert grid.occupancy_generation > 0
