"""Narrow pad seeds use emitted neck-down width without waiving clearance."""

from dataclasses import replace

import pytest
from shapely.geometry import LineString, box

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("backend_name", ["python", "cpp"])
@pytest.mark.parametrize("target_y", [5.01, 4.0, 6.0])
def test_near_grid_narrow_pad_escape_preserves_authored_clearance(backend_name, target_y):
    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=0.4,
        min_trace_width=0.15,
        trace_clearance=0.15,
    )
    grid = RoutingGrid(width=10, height=10, rules=rules)
    # The offset is below the waypoint activation threshold (resolution/4).
    # Full corridor-width erosion empties the seed's Y interval despite a
    # legal 0.15mm neck-down fitting inside the 0.20mm pad.
    start = Pad(
        x=5.01,
        y=5.01,
        width=1,
        height=0.2,
        net=1,
        net_name="SIGNAL",
        ref="U1",
        pin="1",
        layer=Layer.F_CU,
    )
    foreign = replace(start, y=5.525, net=2, net_name="OTHER", pin="2")
    end = replace(start, x=8, y=target_y, width=0.8, height=0.8, ref="J1")
    for pad in (start, foreign, end):
        grid.add_pad(pad)
    if backend_name == "cpp":
        if not is_cpp_available():
            pytest.skip("C++ backend unavailable")
        backend = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
    else:
        backend = Router(grid, rules)
    route = backend.route(start, end, per_net_timeout=5)
    assert route is not None and route.segments
    assert any(segment.width == pytest.approx(0.4) for segment in route.segments)
    foreign_copper = box(
        foreign.x - foreign.width / 2,
        foreign.y - foreign.height / 2,
        foreign.x + foreign.width / 2,
        foreign.y + foreign.height / 2,
    )
    for segment in route.segments:
        assert grid.validate_segment_clearance(segment, exclude_net=1)[0]
        gap = (
            LineString([(segment.x1, segment.y1), (segment.x2, segment.y2)]).distance(
                foreign_copper
            )
            - segment.width / 2
        )
        assert gap >= rules.trace_clearance - 1e-9
    # The same-component foreign pad still rejects actual sub-clearance copper.
    bad = Segment(4.8, 5.225, 5.2, 5.225, 0.15, Layer.F_CU, net=1)
    valid, clearance, _ = grid.validate_segment_clearance(bad, exclude_net=1)
    assert not valid
    assert clearance == pytest.approx(0.125)
