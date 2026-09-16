"""Existing destination escape copper must remain reachable in a Kelvin star."""

from dataclasses import replace

import pytest
from shapely.geometry import LineString, box
from shapely.ops import unary_union

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import get_backend_info
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment


@pytest.mark.parametrize("force_python", [True, False])
@pytest.mark.parametrize("escape_flag", [True, False])
def test_kelvin_destination_escape_remains_reachable(force_python, escape_flag):
    if not force_python and not get_backend_info()["available"]:
        pytest.skip("C++ extension unavailable")
    router = Autorouter(
        22, 22, force_python=force_python, physics_enabled=False, per_net_iterations=20000
    )
    positions = {"R1": (4, 10), "Q1": (12, 10), "U1": (16, 12), "U2": (16, 8)}
    for ref, (x, y) in positions.items():
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "ISENSE_TEST",
                }
            ],
        )
    stub = Route(
        1, "ISENSE_TEST", [Segment(16, 12, 14, 12, 0.2, Layer.F_CU, 1)], is_escape=escape_flag
    )
    original = stub.copy_geometry()
    router._mark_route(stub)
    router.routes.append(stub)
    router._escape_pad_overrides[("U1", "1")] = replace(
        router.pads[("U1", "1")], x=14, width=0.14, height=0.14
    )
    routes = router.route_net(1)
    assert len(routes) == 3, "All branches, including the escaped destination, must route"
    assert stub == original
    pads = {ref: box(x - 0.4, y - 0.4, x + 0.4, y + 0.4) for ref, (x, y) in positions.items()}
    copper = unary_union(
        [
            LineString([s.start, s.end]).buffer(s.width / 2)
            for r in [stub, *routes]
            for s in r.segments
            if s.layer == Layer.F_CU
        ]
        + list(pads.values())
    )
    components = list(copper.geoms) if hasattr(copper, "geoms") else [copper]
    assert any(all(part.intersects(pad) for pad in pads.values()) for part in components)
    without_root = copper.difference(pads["R1"].buffer(1e-7))
    components = list(without_root.geoms) if hasattr(without_root, "geoms") else [without_root]
    assert all(
        sum(part.intersects(pad) for ref, pad in pads.items() if ref != "R1") <= 1
        for part in components
    )


@pytest.mark.parametrize("force_python", [True, False])
@pytest.mark.parametrize("shared_force_path", [False, True])
def test_destination_exemption_preserves_other_terminal_and_layer_barriers(
    force_python, shared_force_path
):
    from kicad_tools.router.kelvin_obstacles import isolate_kelvin_branch

    if not force_python and not get_backend_info()["available"]:
        pytest.skip("C++ extension unavailable")
    router = Autorouter(22, 22, force_python=force_python, physics_enabled=False)
    for ref, x, y in [("R1", 4, 10), ("Q1", 12, 10), ("U1", 16, 12), ("U2", 16, 8)]:
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "ISENSE_TEST",
                }
            ],
        )
    segments = [Segment(16, 12, 14, 12, 0.2, Layer.F_CU, 1)]
    if shared_force_path:
        segments.append(Segment(14, 12, 12, 10, 0.2, Layer.F_CU, 1))
    for route in [
        Route(1, "ISENSE_TEST", segments),
        Route(1, "ISENSE_TEST", [Segment(14, 12, 14, 13, 0.2, Layer.B_CU, 1)]),
    ]:
        router._mark_route(route)
        router.routes.append(route)
    root = router.pads[("R1", "1")]
    target = replace(router.pads[("U1", "1")], x=14, width=0.14, height=0.14)
    pads = [root, router.pads[("Q1", "1")], target, router.pads[("U2", "1")]]
    x, y = router.grid.world_to_grid(14, 12)
    front = router.grid.layer_to_index(Layer.F_CU.value)
    back = router.grid.layer_to_index(Layer.B_CU.value)
    before = router.grid._net[:, y, x].copy()
    with isolate_kelvin_branch(router.grid, pads, root, target):
        assert router.grid._net[front, y, x] == (0 if shared_force_path else 1)
        assert router.grid._net[back, y, x] == 0, (
            "Unbridged back-layer copper is not the target escape"
        )
        if not force_python:
            assert router.grid._cpp_grid._impl.at(x, y, front).net == (
                0 if shared_force_path else 1
            )
            assert router.grid._cpp_grid._impl.at(x, y, back).net == 0
    assert (router.grid._net[:, y, x] == before).all()
