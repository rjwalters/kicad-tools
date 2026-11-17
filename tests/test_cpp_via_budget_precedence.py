"""Budget exhaustion must not be disguised as a geometric dead end."""

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="native router required")


def _carpet_pathfinder(size=2, cap=0):
    rules = DesignRules(
        trace_width=0.25,
        trace_clearance=0.2,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )
    grid = CppGrid.from_routing_grid(
        RoutingGrid(
            width=size, height=size, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
        )
    )
    for i in range(int(size / 0.4) + 1):
        for j in range(int(size / 0.4) + 1):
            # Keep F.Cu clear so trace expansion reaches the via/budget
            # decision instead of failing the stored-via trace guard first.
            grid._impl.add_stored_via(i * 0.4, j * 0.4, 0.3, 0.6, 2, layer_from=1, layer_to=3)
    pathfinder = CppPathfinder(grid, rules, diagonal_routing=True, per_net_iterations=cap)
    pathfinder.set_routable_layers(grid.get_routable_indices())
    return pathfinder


@pytest.mark.parametrize("method", ["route", "route_resumable"])
@pytest.mark.parametrize("cap", [8, 10000])
def test_via_blocker_preserves_budget_reason(method, cap):
    from kicad_tools.router import router_cpp

    pathfinder = _carpet_pathfinder()
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


@pytest.mark.parametrize("method", ["route", "route_resumable"])
def test_via_blocker_preserves_timeout_reason(method):
    from kicad_tools.router import router_cpp

    pathfinder = _carpet_pathfinder(size=10)
    result = getattr(pathfinder._impl, method)(
        0.2, 0.2, 0, 9.8, 9.8, 3, 1, per_net_timeout_seconds=0.000001, max_search_iterations=100000
    )
    assert not result.success
    assert result.blocking_via_net == 2
    assert pathfinder.iterations >= 1024
    assert result.failure_reason == router_cpp.FAILURE_TIMEOUT


def test_exhausted_native_search_does_not_construct_python_retry():
    from kicad_tools.router import router_cpp
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad

    pathfinder = _carpet_pathfinder(cap=8)
    start = Pad(x=0.2, y=0.2, width=0.2, height=0.2, layer=Layer.F_CU, net=1, net_name="N1")
    end = Pad(x=1.8, y=1.8, width=0.2, height=0.2, layer=Layer.B_CU, net=1, net_name="N1")
    assert pathfinder.route(start, end) is None
    assert (
        pathfinder.get_last_failure_info()["failure_reason"] == router_cpp.FAILURE_ITERATION_LIMIT
    )
    assert pathfinder._py_router is None
