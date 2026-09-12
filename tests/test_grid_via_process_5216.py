"""Manufacturing capability applies to grid vias on own-net pad copper."""

import math

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("backend", ["python", "cpp"])
@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize(
    "manufacturer,through_hole,x,allowed",
    [
        ("jlcpcb", False, 10.0, False),
        ("jlcpcb", False, 10.6, False),
        ("jlcpcb", False, 11.0, True),
        ("jlcpcb", True, 10.0, True),
        ("pcbway", False, 10.0, True),
    ],
)
def test_grid_via_process(manufacturer, through_hole, x, allowed, sharing, backend):
    rules = DesignRules(grid_resolution=0.1, manufacturer=manufacturer)
    grid = RoutingGrid(width=20, height=20, rules=rules, layer_stack=LayerStack.two_layer())
    grid.add_pad(
        Pad(x=10, y=10, width=1, height=1, net=1, net_name="SIG", through_hole=through_hole)
    )
    if backend == "cpp":
        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available

        if not is_cpp_available():
            pytest.skip("native backend not built")
        router = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
    else:
        router = Router(grid, rules)
    gx, gy = grid.world_to_grid(x, 10)
    for _ in range(2):
        if backend == "cpp":
            assert (not router._impl.is_via_blocked(gx, gy, 1, sharing)) == allowed
        else:
            assert (
                router._check_via_placement_cached(gx, gy, net=1, allow_sharing=sharing) == allowed
            )


@pytest.mark.parametrize("backend", ["python", "cpp"])
@pytest.mark.parametrize("angle", [-45, 30, 45, 90, 270])
@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("u,v,allowed", [(1.5, 0, False), (1.5, 0.6, False), (0, 1.2, True)])
def test_grid_via_process_rotated_pad(backend, angle, sharing, u, v, allowed):
    from kicad_tools.router.io import _resolve_pad_dims_and_rotation

    rules = DesignRules(grid_resolution=0.05, manufacturer="jlcpcb")
    width, height, rotation = _resolve_pad_dims_and_rotation(angle, 4, 1)
    grid = RoutingGrid(width=20, height=20, rules=rules, layer_stack=LayerStack.two_layer())
    grid.add_pad(Pad(10, 10, width, height, 1, "SIG", rotation=rotation))
    # Physical clockwise rotation, independent of the predicate under test.
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    gx, gy = grid.world_to_grid(10 + c * u + s * v, 10 - s * u + c * v)
    if backend == "cpp":
        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available

        if not is_cpp_available():
            pytest.skip("native backend not built")
        router = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
        for _ in range(2):
            assert (not router._impl.is_via_blocked(gx, gy, 1, sharing)) == allowed
    else:
        router = Router(grid, rules)
        for _ in range(2):
            assert (
                router._check_via_placement_cached(gx, gy, net=1, allow_sharing=sharing) == allowed
            )
