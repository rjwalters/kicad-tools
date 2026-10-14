"""Issue #5617: ``GridCollisionChecker`` acted-on-cell walk equivalence.

``path_is_clear`` now pre-reads the ``is_obstacle`` / ``pad_blocked`` /
``net`` planes for the blocked cells in one array op and walks only the cells
some branch acts on.  These tests pin its verdict to the original per-cell
``_CellView`` walk (forced by making ``_active_blocked_cells`` decline) over a
grid that exercises every branch: foreign route copper, own-net copper, a
skip-net pad (``net == 0``, ``pad_blocked``), a signal pad and a keepout.
"""

from __future__ import annotations

import random

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.collision import GridCollisionChecker
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules


def _grid():
    from kicad_tools.router.grid import RoutingGrid

    rules = DesignRules(grid_resolution=0.25, trace_width=0.2, trace_clearance=0.15)
    grid = RoutingGrid(width=12.0, height=12.0, rules=rules)
    for net, y in ((1, 2.5), (2, 4.0), (3, 7.5)):
        seg = Segment(1.0, y, 9.0, y, 0.2, Layer.F_CU, net=net, net_name=f"N{net}")
        grid.mark_route(Route(net=net, net_name=f"N{net}", segments=[seg]))
    # A diagonal on N2 crossing N1's corridor gives overused / shared cells.
    seg = Segment(2.0, 1.0, 6.0, 5.0, 0.2, Layer.F_CU, net=2, net_name="N2")
    grid.mark_route(Route(net=2, net_name="N2", segments=[seg]))
    grid.add_pad(Pad(x=5.0, y=9.5, width=1.0, height=0.6, net=0, net_name="", ref="U1", pin="1"))
    grid.add_pad(Pad(x=8.0, y=9.5, width=1.0, height=0.6, net=4, net_name="N4", ref="U1", pin="2"))
    grid.add_keepout(10.0, 10.0, 11.0, 11.0, layers=[Layer.F_CU])
    return grid


def _legacy(checker: GridCollisionChecker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checker, "_active_blocked_cells", lambda *a, **k: None)


@pytest.mark.parametrize("ignore_overflow", [False, True])
def test_verdicts_match_per_cell_walk(
    ignore_overflow: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    grid = _grid()
    fast = GridCollisionChecker(grid, ignore_overflow=ignore_overflow)
    slow = GridCollisionChecker(grid, ignore_overflow=ignore_overflow)
    _legacy(slow, monkeypatch)
    rng = random.Random(5617)
    verdicts = {True: 0, False: 0}
    for _ in range(600):
        x1, y1, x2, y2 = (rng.uniform(-0.5, 12.5) for _ in range(4))
        width = rng.choice((0.15, 0.2, 0.3))
        net = rng.choice((0, 1, 2, 3, 4, 99))
        expected = slow.path_is_clear(x1, y1, x2, y2, Layer.F_CU, width, net)
        assert fast.path_is_clear(x1, y1, x2, y2, Layer.F_CU, width, net) is expected
        verdicts[expected] += 1
    assert verdicts[True] and verdicts[False], verdicts


def test_acted_on_cells_are_the_walks_acting_cells() -> None:
    """Every acted-on cell is one the per-cell walk would enter a branch for."""
    grid = _grid()
    checker = GridCollisionChecker(grid)
    layer_idx = grid.layer_to_index(Layer.F_CU.value)
    cells = [(gx, gy) for gy in range(-2, grid.rows + 2) for gx in range(-2, grid.cols + 2)]
    for exclude in (0, 1, 2, 4, 99):
        expected = []
        for gx, gy in checker._blocked_cells_in_bounds(cells, layer_idx):
            cell = grid.cell_at(layer_idx, gy, gx)
            if cell.net == exclude:
                continue
            if cell.is_obstacle or cell.pad_blocked or cell.net != 0:
                expected.append((gx, gy, cell.is_obstacle, cell.pad_blocked, cell.net))
        assert expected
        assert checker._active_blocked_cells(cells, layer_idx, exclude) == expected


def test_declines_without_real_planes() -> None:
    class _Double:
        cols = rows = 4
        _blocked = None

    checker = GridCollisionChecker(_Double())  # type: ignore[arg-type]
    assert checker._active_blocked_cells([(0, 0)], 0, 1) is None
