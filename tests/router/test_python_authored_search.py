"""Search must detour around authored floors beyond rasterized obstacles."""

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("method", ["route", "route_bidirectional"])
@pytest.mark.parametrize("kind", ["pad", "trace", "via"])
@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("sharing", [False, True])
def test_search_detours_outside_raster_halo(method, kind, strict_net, sharing):
    rules = DesignRules(
        trace_width=0.2, trace_clearance=0.1, via_clearance=0.1, grid_resolution=0.1
    )
    grid = RoutingGrid(10, 10, rules)
    if kind == "pad":
        grid.add_pad(Pad(5, 5, 0.2, 0.2, 2, "foreign", ref="U2", pin="1"))
    elif kind == "trace":
        grid.mark_route(Route(2, "foreign", segments=[Segment(4, 5, 6, 5, 0.2, Layer.F_CU, 2)]))
    else:
        grid.mark_route(
            Route(2, "foreign", vias=[Via(5, 5, 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 2)])
        )
    # The index and raster predate the larger electrical rule. Unrelated net
    # 3 must not impose its much larger floor on this pair.
    rules.net_clearance_floors = {strict_net: 0.6, 3: 4.0}
    router = Router(grid, rules)
    start = Pad(2, 5.5, 0.1, 0.1, 1, "signal")
    end = Pad(8, 5.5, 0.1, 0.1, 1, "signal")
    result = getattr(router, method)(start, end, negotiated_mode=sharing)
    assert result is not None and result.segments
    assert all(grid.validate_segment_clearance(seg, 1)[0] for seg in result.segments)
