"""Native search must inspect authored copper beyond raster coverage."""

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules


@pytest.mark.skipif(not is_cpp_available(), reason="native build required")
@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("sharing", [False, True])
def test_authored_trace_floor_without_raster_coverage(strict_net, sharing, monkeypatch):
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.1,
        grid_resolution=0.1,
        net_clearance_floors={strict_net: 0.6, 3: 4.0},
    )
    grid = RoutingGrid(10, 10, rules)
    # Imported escape geometry may be present without a matching occupancy
    # halo. An unrelated high-clearance net must not widen this pair.
    grid.routes.append(Route(2, "foreign", segments=[Segment(4, 5, 6, 5, 0.2, Layer.F_CU, 2)]))
    router = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
    monkeypatch.setattr(router, "_try_python_fallback", lambda *a, **kw: None)
    result = router.route(
        Pad(2, 5.5, 0.1, 0.1, 1, "signal"),
        Pad(8, 5.5, 0.1, 0.1, 1, "signal"),
        negotiated_mode=sharing,
    )
    assert result is not None and result.segments
    assert all(grid.validate_segment_clearance(segment, 1)[0] for segment in result.segments)
