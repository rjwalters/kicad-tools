"""Pad padding is not copper to inflate again during native radius checks."""

import pytest

from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available, router_cpp
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="Native router required")


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("radius", [0, 3])
def test_padding_is_not_inflated_but_copper_and_keepouts_are(sharing, radius):
    grid = router_cpp.Grid3D(40, 40, 2, 0.1)
    rules = router_cpp.DesignRules()
    rules.grid_resolution = 0.1
    finder = router_cpp.Pathfinder(grid, rules, True)
    grid.mark_blocked(21, 20, 0, 2, False, False, True)
    assert not finder.is_trace_blocked(20, 20, 0, 1, sharing, radius)
    assert not finder.is_via_blocked(20, 20, 1, sharing, radius)
    # Overlapping non-pad keepouts must revoke the exemption.
    grid.mark_blocked(21, 20, 0, 2)
    grid.mark_blocked(21, 20, 0, 2, False, False, True)
    assert finder.is_trace_blocked(20, 20, 0, 1, sharing, radius)
    assert finder.is_via_blocked(20, 20, 1, sharing, radius)


@pytest.mark.parametrize(
    "metal,obstacle,usage", [(True, False, 0), (False, True, 0), (False, False, 1)]
)
def test_halo_provenance_does_not_exempt_copper(metal, obstacle, usage):
    grid = router_cpp.Grid3D(40, 40, 2, 0.1)
    rules = router_cpp.DesignRules()
    rules.grid_resolution = 0.1
    finder = router_cpp.Pathfinder(grid, rules, True)
    grid.mark_blocked(21, 20, 0, 2, obstacle, metal, True)
    grid.at(21, 20, 0).usage_count = usage
    assert finder.is_trace_blocked(20, 20, 0, 1, False, 3)
    assert finder.is_via_blocked(20, 20, 1, False, 3)


@pytest.mark.parametrize("incremental", [False, True])
def test_pad_provenance_survives_bulk_and_incremental_sync(incremental):
    rules = DesignRules(grid_resolution=0.1)
    grid = RoutingGrid(width=10, height=10, rules=rules, layer_stack=LayerStack.two_layer())
    cpp = CppGrid.from_routing_grid(grid) if incremental else None
    grid.add_pad(
        Pad(x=5, y=5, width=0.45, height=0.45, net=0, net_name="GND", layer=Layer.F_CU),
        pin_pitch=1.27,
    )
    if cpp is None:
        cpp = CppGrid.from_routing_grid(grid)
    halo_cells = [
        (l, y, x)
        for l, y, x in grid._pad_halo_cells
        if not grid._pad_blocked[l, y, x] and not grid._is_obstacle[l, y, x]
    ]
    assert halo_cells
    assert all(cpp._impl.at(x, y, l).pad_halo_only for l, y, x in halo_cells)
    l, y, x = halo_cells[0]
    grid.cell_at(l, y, x).blocked = True
    assert (l, y, x) not in grid._pad_halo_cells


@pytest.mark.parametrize("keepout_first", [False, True])
def test_python_keepout_overlap_stays_blocked_after_bulk_sync(keepout_first):
    rules = DesignRules(grid_resolution=0.1)
    grid = RoutingGrid(width=10, height=10, rules=rules, layer_stack=LayerStack.two_layer())
    x, y = grid.world_to_grid(5.4, 5)
    if keepout_first:
        grid.cell_at(0, y, x).blocked = True
    grid.add_pad(
        Pad(x=5, y=5, width=0.45, height=0.45, net=0, net_name="GND", layer=Layer.F_CU),
        pin_pitch=1.27,
    )
    if not keepout_first:
        grid.cell_at(0, y, x).blocked = True
    cpp = CppGrid.from_routing_grid(grid)
    assert cpp._impl.at(x, y, 0).blocked
    assert not cpp._impl.at(x, y, 0).pad_halo_only


def test_plane_padding_does_not_bypass_native39_stored_via_guard():
    grid = router_cpp.Grid3D(40, 40, 2, 0.1)
    rules = router_cpp.DesignRules()
    rules.grid_resolution = 0.1
    finder = router_cpp.Pathfinder(grid, rules, True)
    # A halo-only raster cell is passable, but the indexed physical via at
    # the same location remains forbidden even without a registered mark.
    grid.mark_blocked(20, 20, 0, 2, False, False, True)
    grid.add_stored_via(2.0, 2.0, 0.3, 0.6, 2)
    assert finder.is_via_blocked(20, 20, 1, False, 3)
