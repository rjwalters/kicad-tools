"""Native supplemental copper uses the same precision as native validation."""

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


@pytest.mark.skipif(not is_cpp_available(), reason="native build required")
@pytest.mark.parametrize("supplemental", [False, True])
@pytest.mark.parametrize(
    "offset,valid", [(0.0, True), (-1.5258789e-6, True), (-0.0002, False), (0.001, True)]
)
def test_native_foreign_via_boundary_is_independent_of_context(supplemental, offset, valid):
    rules = DesignRules(
        trace_clearance=0.15,
        via_clearance=0.15,
        grid_resolution=0.1,
        net_clearance_floors={1: 0.2},
    )
    grid = RoutingGrid(40, 40, rules, origin_x=140, origin_y=80)
    via = Via(154.8625, 87.25, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 1)
    segment = Segment(154.85, 87.85 + offset, 154.90, 87.85 + offset, 0.2, Layer.F_CU, 2)
    if not supplemental:
        grid.routes.append(Route(1, "via", vias=[via]))
    native = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
    if supplemental:
        native.set_segment_foreign_context(foreign_vias=[via])
    pads = [Pad(x, segment.y1, 0.2, 0.2, 2, "trace") for x in (segment.x1, segment.x2)]
    route = Route(2, "trace", segments=[segment])
    assert (native._validate_route_clearance(route, *pads, 1) is None) is valid
