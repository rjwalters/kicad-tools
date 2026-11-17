"""Edge keepouts added after backend creation must survive routing/rip-up."""

import pytest

from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("native", [False, True])
def test_late_edge_keepout_matches_backend_and_survives_ripup(native):
    if native and not is_cpp_available():
        pytest.skip("C++ backend unavailable")
    grid = RoutingGrid(5, 5, DesignRules(grid_resolution=0.1, trace_width=0.2))
    cpp = CppGrid.from_routing_grid(grid) if native else None
    # A snapshot may already exist when late static geometry is added.
    grid._ensure_static_blockage_snapshot()
    grid.add_edge_keepout([((5.0, 0.0), (5.0, 5.0))], clearance=0.3)
    blocked_x, y = grid.world_to_grid(4.7, 2.5)
    legal_x, _ = grid.world_to_grid(4.6, 2.5)
    route = Route(net=1, net_name="SIG", segments=[Segment(4.5, 2.0, 4.5, 3.0, 0.2, Layer.F_CU, 1)])
    for stage in range(2):
        for layer in grid.get_routable_indices():
            assert grid.grid[layer][y][blocked_x].blocked
            assert grid.grid[layer][y][blocked_x].is_obstacle
            assert grid.grid[layer][y][blocked_x].net == 0
            assert not grid.grid[layer][y][legal_x].blocked
            if cpp is not None:
                assert cpp.is_blocked(blocked_x, y, layer)
                assert not cpp.is_blocked(legal_x, y, layer)
        if stage == 0:
            grid.mark_route(route)
            grid.unmark_route(route)
    # The last open centre leaves 0.3 mm copper-to-edge clearance.
    wx, _ = grid.grid_to_world(legal_x, y)
    assert 5.0 - wx - route.segments[0].width / 2 >= 0.3 - 1e-9
