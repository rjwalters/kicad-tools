"""Kelvin branches must remain separate in emitted copper, not only in the plan."""

import numpy as np
import pytest
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import CppPathfinder, get_backend_info
from kicad_tools.router.kelvin_obstacles import isolate_kelvin_branch


@pytest.mark.parametrize("sense_positions", [((16, 12), (16, 8)), ((16, 10), (16, 12))])
@pytest.mark.parametrize("force_python", [True, False])
def test_kelvin_branches_do_not_share_copper_away_from_shunt(sense_positions, force_python):
    """The collinear sense terminal must not reconnect through the force pad."""
    if not force_python and not get_backend_info()["available"]:
        pytest.skip("C++ extension unavailable")
    router = Autorouter(
        22, 22, force_python=force_python, physics_enabled=False, per_net_iterations=20000
    )
    if not force_python:
        assert isinstance(router.router, CppPathfinder)
    for ref, (x, y) in zip(
        ("R1", "Q1", "U1", "U2"), ((4, 10), (12, 10), *sense_positions), strict=True
    ):
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

    routes = router.route_net(1)
    if not force_python:
        assert router.router.fallback_stats["fallback_count"] == 0
    assert len(routes) == 3, "All three shunt-to-terminal connections must route"
    shunt_contact = box(3.6, 9.6, 4.4, 10.4)
    for index, branch in enumerate(routes):
        for other in routes[index + 1 :]:
            for layer in (0, 5):

                def metal(route):
                    return unary_union(
                        [
                            LineString([segment.start, segment.end]).buffer(segment.width / 2)
                            for segment in route.segments
                            if segment.layer.value == layer
                        ]
                        + [Point(via.x, via.y).buffer(via.diameter / 2) for via in route.vias]
                    )

                shared = metal(branch).intersection(metal(other)).difference(shunt_contact)
                assert shared.area < 1e-9, (
                    f"Kelvin branches share {shared.area:.6f} mm² away from the shunt"
                )


@pytest.mark.parametrize("force_python", [True, False])
def test_branch_obstacles_restore_state_when_search_raises(force_python):
    if not force_python and not get_backend_info()["available"]:
        pytest.skip("C++ extension unavailable")
    router = Autorouter(5, 5, force_python=force_python, physics_enabled=False)
    for ref, x, y in [("R1", 1, 1), ("Q1", 3, 1), ("U1", 3, 3)]:
        router.add_component(
            ref, [{"number": "1", "x": x, "y": y, "net": 1, "net_name": "ISENSE_TEST"}]
        )
    pads = list(router.pads.values())
    grid = router.grid
    fields = ("_net", "_blocked", "_is_obstacle", "_pad_blocked", "_usage_count")
    before = {field: getattr(grid, field).copy() for field in fields}
    cpp = getattr(grid, "_cpp_grid", None)
    cpp_fields = ("net", "blocked", "is_obstacle", "pad_blocked", "static_blocked", "usage_count")
    x, y = grid.world_to_grid(3, 1)
    cell = cpp._impl.at(x, y, 0) if cpp is not None else None
    cpp_before = {field: getattr(cell, field) for field in cpp_fields} if cell is not None else {}
    with pytest.raises(RuntimeError, match="search failed"):
        with isolate_kelvin_branch(grid, pads, pads[0], pads[2]):
            assert grid._net[0, y, x] == 0
            if cell is not None:
                assert cell.net == 0 and cell.static_blocked
            raise RuntimeError("search failed")
    for field, values in before.items():
        np.testing.assert_array_equal(getattr(grid, field), values)
    for field, value in cpp_before.items():
        assert getattr(cell, field) == value
