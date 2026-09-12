"""Standard-pitch seeds preserve search freedom and authored foreign-pad clearance."""

from dataclasses import replace

import pytest
from shapely.geometry import LineString, box

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("backend_name", ["python", "cpp"])
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("foreign_net", [0, 2])
def test_standard_pitch_neighbor_clearance(backend_name, strict, foreign_net):
    rules = DesignRules(
        grid_resolution=0.05, trace_width=0.15, trace_clearance=0.15, strict_pad_clearance=strict
    )
    grid = RoutingGrid(width=12, height=12, rules=rules)
    start = Pad(
        x=5.0125,
        y=5,
        width=1.5,
        height=1.5,
        net=1,
        net_name="SIGNAL",
        ref="U1",
        pin="1",
        layer=Layer.F_CU,
    )
    foreign = replace(start, y=7, net=foreign_net, net_name="OTHER", pin="2")
    end = replace(start, x=9, y=8, ref="J1", width=0.6, height=0.6)
    for pad in (start, foreign, end):
        grid.add_pad(pad)
    assert grid.compute_component_pitches()["U1"] == 2
    assert not grid._component_is_fine_pitch("U1")
    if backend_name == "cpp":
        if not is_cpp_available():
            pytest.skip("C++ backend unavailable")
        backend = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
    else:
        backend = Router(grid, rules)
    # Automatic erosion is needed for dense fine-pitch escape tails, but
    # eroding standard-pitch pads needlessly changes negotiated route choices.
    # Explicit strict mode still requests the narrower seed region everywhere.
    if backend_name == "cpp":

        def metal_bounds(width):
            bounds = backend._compute_pad_bounds(start, width)
            return (bounds.metal_gx1, bounds.metal_gy1, bounds.metal_gx2, bounds.metal_gy2)
    else:

        def metal_bounds(width):
            return backend._get_pad_metal_bounds(start, width)

    if strict:
        assert metal_bounds(rules.trace_width) != metal_bounds(None)
    else:
        assert metal_bounds(rules.trace_width) == metal_bounds(None)
    route = backend.route(start, end, per_net_timeout=5)
    assert route and route.segments
    metal = box(foreign.x - 0.75, foreign.y - 0.75, foreign.x + 0.75, foreign.y + 0.75)
    for segment in route.segments:
        gap = (
            LineString([(segment.x1, segment.y1), (segment.x2, segment.y2)]).distance(metal)
            - segment.width / 2
        )
        assert gap >= 0.15 - 1e-9
        assert grid.validate_segment_clearance(segment, exclude_net=1, exclude_refs={"U1", "J1"})[0]
    bad = Segment(4.8, 6.05, 5.2, 6.05, 0.15, Layer.F_CU, net=1)
    valid, gap, _ = grid.validate_segment_clearance(bad, exclude_net=1, exclude_refs={"U1", "J1"})
    assert not valid and gap == pytest.approx(0.125)
    if backend_name == "cpp":
        assert (
            backend._validate_route_clearance(
                Route(net=1, net_name="SIGNAL", segments=[bad]), start, end, 5
            )
            is not None
        )
