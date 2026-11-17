"""Trace/via clearance is independent of insertion order and pair membership."""

import pytest

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def context(trace=0.15, via_clearance=0.2, gap=0.18):
    rules = DesignRules(
        trace_width=0.2, trace_clearance=trace, via_clearance=via_clearance, grid_resolution=0.05
    )
    grid = RoutingGrid(10, 10, rules)
    via = Via(5, 5, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 1, "VIA")
    segment = Segment(4, 5.4 + gap, 6, 5.4 + gap, 0.2, Layer.F_CU, 2, "TRACE")
    return rules, grid, via, segment


@pytest.mark.parametrize(
    "trace,via_floor,gap,legal",
    [
        (0.15, 0.2, 0.18, False),
        (0.25, 0.2, 0.23, False),
        (0.15, 0.2, 0.21, True),
        (0.25, 0.2, 0.26, True),
    ],
)
def test_python_grid_and_revalidation_share_via_floor(trace, via_floor, gap, legal):
    rules, grid, via, segment = context(trace, via_floor, gap)
    via_route, trace_route = Route(1, "VIA", vias=[via]), Route(2, "TRACE", segments=[segment])
    grid.routes = [via_route]
    assert grid.validate_segment_clearance(segment, 2)[0] is legal
    grid.routes = [trace_route]
    assert grid.validate_via_clearance(via, 1)[0] is legal
    assert (grid.worst_via_segment_deficit(via, 1)[0] <= 0) is legal
    router = Router(grid, rules)
    negotiated = NegotiatedRouter(grid, router, rules, {})
    routes = {1: [via_route], 2: [trace_route]}
    assert negotiated.find_nets_with_segment_via_violations(routes, trace) == ([] if legal else [2])
    assert negotiated.find_nets_with_via_segment_violations(routes, trace) == ([] if legal else [1])


def test_closer_legal_trace_does_not_hide_via_violation():
    _, grid, via, segment = context()
    nearer = Segment(4, segment.y1 + 0.36, 6, segment.y2 + 0.36, 0.2, Layer.F_CU, 3)
    grid.routes = [Route(3, "NEARER", segments=[nearer]), Route(1, "VIA", vias=[via])]
    valid, minimum, location = grid.validate_segment_clearance(segment, 2)
    assert minimum == pytest.approx(0.16)
    assert not valid
    assert location == (via.x, via.y)


@pytest.mark.skipif(not is_cpp_available(), reason="native router required")
@pytest.mark.parametrize("partner", [-1, 1])
@pytest.mark.parametrize(
    "trace,via_floor,gap,legal",
    [
        (0.15, 0.2, 0.18, False),
        (0.25, 0.2, 0.23, False),
        (0.15, 0.2, 0.21, True),
        (0.25, 0.2, 0.26, True),
    ],
)
def test_native_commit_keeps_via_floor_for_partner(partner, trace, via_floor, gap, legal):
    from kicad_tools.router import router_cpp

    rules, grid, via, segment = context(trace, via_floor, gap)
    native = CppGrid.from_routing_grid(grid)
    native._impl.add_stored_via(via.x, via.y, via.drill, via.diameter, via.net)
    s = router_cpp.Segment()
    s.x1, s.y1, s.x2, s.y2 = segment.x1, segment.y1, segment.x2, segment.y2
    s.width, s.layer, s.net = segment.width, 0, segment.net
    result = native._impl.validate_route(
        [s],
        [],
        2,
        [],
        rules.trace_clearance,
        rules.via_clearance,
        0.1,
        partner_net=partner,
        intra_pair_clearance=0.1,
    )
    assert result.valid is legal
    if not legal:
        assert result.violation_type == 3
    native._impl.clear_stored_routes()
    native._impl.add_stored_segment(s.x1, s.y1, s.x2, s.y2, s.width, 0, 2)
    v = router_cpp.Via()
    v.x, v.y, v.drill, v.diameter, v.net = via.x, via.y, via.drill, via.diameter, 1
    v.layer_from, v.layer_to = 0, 1
    reverse = native._impl.validate_route([], [v], 1, [], trace, via_floor, 0.1)
    assert reverse.valid is legal
    if not legal:
        assert reverse.violation_type == 4


@pytest.mark.skipif(not is_cpp_available(), reason="native router required")
@pytest.mark.parametrize("method", ["route", "route_resumable"])
def test_native_search_uses_via_floor_before_commit(method):
    rules, grid, via, _ = context()
    native = CppGrid.from_routing_grid(grid)
    native._impl.add_stored_via(via.x, via.y, via.drill, via.diameter, via.net)
    native._impl.increment_usage(*grid.world_to_grid(5, 5), 0)
    router = CppPathfinder(native, rules)
    router.set_routable_layers([0])
    result = getattr(router._impl, method)(
        3, 5, 0, 7, 5, 0, 2, negotiated_mode=True, emit_trace_width=0.2, max_search_iterations=20000
    )
    assert result.success and result.segments and not result.vias
    for segment in result.segments:
        distance = grid._point_to_segment_distance(
            5, 5, segment.x1, segment.y1, segment.x2, segment.y2
        )
        assert distance - (segment.width + via.diameter) / 2 >= rules.via_clearance - 1e-4


@pytest.mark.parametrize("backend", ["python", "cpp"])
def test_supplemental_foreign_context_enforces_via_floor(backend):
    from kicad_tools.router.primitives import Pad

    rules, grid, via, segment = context()
    route = Route(2, "TRACE", segments=[segment])
    if backend == "python":
        router = Router(grid, rules)
        router.set_segment_foreign_context(foreign_vias=[via])
        assert not router._validate_route_clearance(route, exclude_net=2)
    else:
        if not is_cpp_available():
            pytest.skip("native router required")
        router = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
        router.set_segment_foreign_context(foreign_vias=[via])
        start = Pad(
            x=segment.x1,
            y=segment.y1,
            width=0.2,
            height=0.2,
            layer=Layer.F_CU,
            net=2,
            net_name="TRACE",
        )
        end = Pad(
            x=segment.x2,
            y=segment.y2,
            width=0.2,
            height=0.2,
            layer=Layer.F_CU,
            net=2,
            net_name="TRACE",
        )
        assert router._validate_route_clearance(route, start, end, 1) is not None
