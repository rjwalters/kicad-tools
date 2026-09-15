"""Negotiation may cross soft traces, but must route around stored via metal."""

import math

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="native router required")


def context():
    rules = DesignRules(
        grid_resolution=0.05, trace_width=0.1, trace_clearance=0.2, via_diameter=0.3, via_drill=0.15
    )
    grid = RoutingGrid(10, 10, rules)
    native = CppGrid.from_routing_grid(grid)
    router = CppPathfinder(native, rules)
    router.set_routable_layers([0])
    return grid, native, router


def gap(segment):
    dx, dy = segment.x2 - segment.x1, segment.y2 - segment.y1
    t = max(0, min(1, ((5 - segment.x1) * dx + (5 - segment.y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(5 - segment.x1 - t * dx, 5 - segment.y1 - t * dy) - (segment.width + 0.6) / 2


@pytest.mark.parametrize("method", ["route", "route_resumable"])
def test_negotiated_search_routes_around_actual_via_diameter(method):
    grid, native, router = context()
    native._impl.add_stored_via(5, 5, 0.3, 0.6, 2)
    native._impl.increment_usage(*grid.world_to_grid(5, 5), 0)
    result = getattr(router._impl, method)(
        3, 5, 0, 7, 5, 0, 1, negotiated_mode=True, emit_trace_width=0.2, max_search_iterations=20000
    )
    assert result.success
    assert result.segments
    assert not result.vias
    assert all(s.width == pytest.approx(0.2) for s in result.segments)
    # Independent swept-circle measurement; rules' default via is only 0.3mm.
    assert min(gap(s) for s in result.segments) >= 0.2 - 1e-4
    assert native._impl.validate_route(result.segments, [], 1, [], 0.2, 0.2, 0.2).valid


@pytest.mark.parametrize(
    "net,layer,y,partner,required,legal",
    [
        (2, 0, 5.0, -1, -1, False),
        (1, 0, 5.0, -1, -1, True),
        (2, 1, 5.0, -1, -1, True),
        (2, 0, 5.6, -1, -1, True),
        (2, 0, 5.59, -1, -1, False),
        (2, 0, 5.5, 2, 0.1, True),
        (2, 0, 5.49, 2, 0.1, False),
    ],
)
def test_via_gate_preserves_layer_net_partner_and_boundary(net, layer, y, partner, required, legal):
    from kicad_tools.router import router_cpp

    _, native, _ = context()
    native._impl.add_stored_via(5, 5, 0.3, 0.6, net, layer_from=0, layer_to=0)
    segment = router_cpp.Segment()
    segment.x1, segment.y1, segment.x2, segment.y2 = 4, y, 6, y
    segment.width, segment.layer, segment.net = 0.2, layer, 1
    assert native._impl.trace_stored_vias_clear(segment, 0.2, partner, required) == legal
    validation = native._impl.validate_route(
        [segment], [], 1, [], 0.2, 0.2, 0.2, partner_net=partner, intra_pair_clearance=required
    )
    assert validation.valid == legal


@pytest.mark.parametrize("method", ["route", "route_resumable"])
def test_foreign_trace_still_negotiates_across_full_width_barrier(method):
    grid, native, router = context()
    native._impl.add_stored_segment(5, 0, 5, 10, 0.2, 0, 2)
    for gy in range(grid.rows):
        native._impl.increment_usage(grid.world_to_grid(5, 0)[0], gy, 0)
    result = getattr(router._impl, method)(
        3, 5, 0, 7, 5, 0, 1, negotiated_mode=True, emit_trace_width=0.2, max_search_iterations=20000
    )
    assert result.success
    assert not result.vias


def test_pairwise_widening_and_attach_zone_preserve_scalar_floor():
    from kicad_tools.router import router_cpp

    _, native, _ = context()
    native._impl.add_stored_via(5, 5, 0.3, 0.6, 2)
    native._impl.set_pairwise_domains([-1, 0, 1], [[0.0, 0.4], [0.4, 0.0]])
    segment = router_cpp.Segment()
    segment.x1, segment.y1, segment.x2, segment.y2 = 4, 5.7, 6, 5.7
    segment.width, segment.layer, segment.net = 0.2, 0, 1
    assert not native._impl.trace_stored_vias_clear(segment, 0.2)
    assert native._impl.trace_stored_vias_clear(segment, 0.2, 2, 0.1)
    zone = router_cpp.AttachZone()
    zone.min_x, zone.min_y, zone.max_x, zone.max_y = 4, 4, 6, 6
    zone.net_ids = [1, 2]
    native._impl.set_attach_zones([zone])
    assert native._impl.trace_stored_vias_clear(segment, 0.2)
    segment.y1 = segment.y2 = 5.59
    assert not native._impl.trace_stored_vias_clear(segment, 0.2)


def test_python_route_sync_preserves_stored_via_layer_span():
    from kicad_tools.router import router_cpp
    from kicad_tools.router.layers import Layer, LayerStack
    from kicad_tools.router.primitives import Route, Via

    rules = DesignRules(grid_resolution=0.05)
    grid = RoutingGrid(10, 10, rules, layer_stack=LayerStack.four_layer_all_signal())
    native = CppGrid.from_routing_grid(grid)
    router = CppPathfinder(native, rules)
    grid.routes.append(Route(2, "other", vias=[Via(5, 5, 0.3, 0.6, (Layer.F_CU, Layer.IN1_CU), 2)]))
    router._sync_stored_routes(grid)
    segment = router_cpp.Segment()
    segment.x1, segment.y1, segment.x2, segment.y2 = 4, 5, 6, 5
    segment.width, segment.net = 0.2, 1
    for layer, legal in [(0, False), (1, False), (2, True), (3, True)]:
        segment.layer = layer
        assert native._impl.trace_stored_vias_clear(segment, 0.2) == legal
