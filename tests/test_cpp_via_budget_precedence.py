"""Budget exhaustion must not be disguised as a geometric dead end."""

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="native router required")


@pytest.mark.parametrize("method", ["route", "route_resumable"])
@pytest.mark.parametrize("cap", [8, 10000])
def test_via_blocker_preserves_budget_reason(method, cap):
    from kicad_tools.router import router_cpp

    rules = DesignRules(
        trace_width=0.25,
        trace_clearance=0.2,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )
    grid = CppGrid.from_routing_grid(
        RoutingGrid(width=2, height=2, rules=rules, layer_stack=LayerStack.four_layer_all_signal())
    )
    for i in range(6):
        for j in range(6):
            grid._impl.add_stored_via(i * 0.4, j * 0.4, 0.3, 0.6, 2)
    pathfinder = CppPathfinder(grid, rules, diagonal_routing=True)
    pathfinder.set_routable_layers(grid.get_routable_indices())
    result = getattr(pathfinder._impl, method)(
        0.2, 0.2, 0, 1.8, 1.8, 3, 1, max_search_iterations=cap
    )
    assert not result.success
    assert result.blocking_via_net == 2
    if cap == 8:
        assert pathfinder.iterations == cap
        assert result.failure_reason == router_cpp.FAILURE_ITERATION_LIMIT
    else:
        assert pathfinder.iterations < cap
        assert result.failure_reason == router_cpp.FAILURE_VIA_VIA_BLOCKED
