"""Rip-up offers the outer-ring blockers (Epic #5509 Phase 4e slice 4, Issue #6994).

``CppPathfinder.find_blocking_nets`` scans a square of radius
``trace_half_width_cells`` around the straight start->end line for foreign
routed copper to offer for rip-up.  Before Issue #6994 that radius was
rounded to *nearest* while the search itself used the ceiling, so on a
0.127 mm grid with ``DesignRules()`` (``x = (0.1 + 0.2) / 0.127 = 2.36``) the
rip-up scan covered 2 cells where the search was blocked at 3: a foreign net
3 cells from the path blocked the route but was never offered for rip-up.

The unified guarded radius is 3, so the net at 3 cells is reported; a control
at 4 cells (outside every radius) is not.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not built")

_SOURCE_NET = 1
_FOREIGN_NET = 7
_ROW = 20
_START_COL = 10
_END_COL = 60


def _setup(foreign_offset_cells: int) -> tuple[CppPathfinder, Pad, Pad]:
    rules = DesignRules()
    rules.grid_resolution = 0.127
    grid = RoutingGrid(width=10.0, height=5.0, rules=rules, layer_stack=LayerStack.two_layer())
    assert grid.resolution == pytest.approx(0.127)
    # The exact (guarded) radius this test is about: ceil(2.362) == 3, where
    # the old round-to-nearest rip-up scan used 2.
    x = (rules.trace_width / 2 + rules.trace_clearance) / grid.resolution
    assert math.ceil(round(x, 6)) == 3
    assert int(x + 0.5) == 2

    cpp_grid = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(cpp_grid, rules)
    layer = cpp_grid.layer_to_index(Layer.F_CU.value)

    # Foreign routed copper (blocked + usage) perpendicular to the midpoint
    # of the straight start->end line.
    fx, fy = (_START_COL + _END_COL) // 2, _ROW + foreign_offset_cells
    cpp_grid._impl.mark_blocked(fx, fy, layer, _FOREIGN_NET)
    cpp_grid._impl.increment_usage(fx, fy, layer)

    sx, sy = cpp_grid.grid_to_world(_START_COL, _ROW)
    ex, ey = cpp_grid.grid_to_world(_END_COL, _ROW)
    start = Pad(
        x=sx, y=sy, width=0.5, height=0.5, net=_SOURCE_NET, net_name="SRC", layer=Layer.F_CU
    )
    end = Pad(x=ex, y=ey, width=0.5, height=0.5, net=_SOURCE_NET, net_name="SRC", layer=Layer.F_CU)
    return pathfinder, start, end


def test_foreign_net_three_cells_from_path_is_offered_for_ripup() -> None:
    pathfinder, start, end = _setup(foreign_offset_cells=3)
    assert _FOREIGN_NET in pathfinder.find_blocking_nets(start, end)


def test_foreign_net_four_cells_from_path_is_not_reported() -> None:
    pathfinder, start, end = _setup(foreign_offset_cells=4)
    assert _FOREIGN_NET not in pathfinder.find_blocking_nets(start, end)
