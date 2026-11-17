"""Issue #5617: ``GridCollisionChecker`` blocked-cell prefilter equivalence.

``path_is_clear`` only acts on cells whose ``blocked`` flag is set; the
prefilter hands the walk exactly those cells (in-bounds, original order).
These tests pin that to the original per-cell ``cell_at(...).blocked`` filter.
"""

from __future__ import annotations

import random

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.collision import GridCollisionChecker
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules


def _grid():
    from kicad_tools.router.grid import RoutingGrid

    rules = DesignRules(grid_resolution=0.25, trace_width=0.2, trace_clearance=0.15)
    grid = RoutingGrid(width=10.0, height=10.0, rules=rules)
    seg = Segment(1.0, 2.5, 5.0, 2.5, 0.2, Layer.F_CU, net=1, net_name="N1")
    grid.mark_route(Route(net=1, net_name="N1", segments=[seg]))
    return grid


def test_prefilter_matches_per_cell_filter_including_out_of_bounds() -> None:
    grid = _grid()
    checker = GridCollisionChecker(grid)
    layer_idx = grid.layer_to_index(Layer.F_CU.value)
    rng = random.Random(5617)
    cells = [(rng.randint(-5, grid.cols + 5), rng.randint(-5, grid.rows + 5)) for _ in range(4000)]
    expected = [
        (gx, gy)
        for gx, gy in cells
        if 0 <= gx < grid.cols and 0 <= gy < grid.rows and grid.cell_at(layer_idx, gy, gx).blocked
    ]
    assert expected, "fixture must contain blocked cells"
    assert checker._blocked_cells_in_bounds(cells, layer_idx) == expected


def test_prefilter_empty_input() -> None:
    grid = _grid()
    assert GridCollisionChecker(grid)._blocked_cells_in_bounds([], 0) == []


def test_path_verdicts_unchanged() -> None:
    grid = _grid()
    checker = GridCollisionChecker(grid)
    assert checker.path_is_clear(1.0, 2.6, 5.0, 2.6, Layer.F_CU, 0.2, exclude_net=2) is False
    assert checker.path_is_clear(1.0, 8.0, 5.0, 8.0, Layer.F_CU, 0.2, exclude_net=2) is True
    assert checker.path_is_clear(1.0, 2.6, 5.0, 2.6, Layer.F_CU, 0.2, exclude_net=1) is True
