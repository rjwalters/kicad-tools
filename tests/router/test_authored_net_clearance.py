"""Authored electrical clearance must survive insertion order and local relief."""

import math

import pytest

from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available
from kicad_tools.router.grid import RTREE_AVAILABLE, RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("gap,valid", [(0.18, False), (0.21, True)])
def test_authored_floor_in_both_trace_insertion_orders(strict_net, gap, valid):
    rules = DesignRules(trace_clearance=0.1, net_clearance_floors={strict_net: 0.2, 3: 0.8})
    grid = RoutingGrid(10, 10, rules)
    a = Segment(2, 5, 8, 5, 0.2, Layer.F_CU, 1)
    b = Segment(2, 5.2 + gap, 8, 5.2 + gap, 0.2, Layer.F_CU, 2)
    for own, foreign in ((a, b), (b, a)):
        grid.routes = [Route(foreign.net, "stored", segments=[foreign])]
        assert grid.validate_segment_clearance(own, own.net)[0] is valid
        assert (
            grid.validate_segment_clearance(
                own, own.net, partner_net=foreign.net, partner_clearance=0.05
            )[0]
            is valid
        )


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("gap,valid", [(0.18, False), (0.21, True)])
def test_authored_floor_in_both_via_trace_insertion_orders(strict_net, gap, valid):
    rules = DesignRules(
        trace_clearance=0.1,
        via_clearance=0.1,
        net_clearance_floors={strict_net: 0.2, 3: 0.8},
    )
    grid = RoutingGrid(10, 10, rules)
    via = Via(5, 5, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 1)
    seg = Segment(2, 5.4 + gap, 8, 5.4 + gap, 0.2, Layer.F_CU, 2)
    grid.routes = [Route(1, "via", vias=[via])]
    assert grid.validate_segment_clearance(seg, 2)[0] is valid
    grid.routes = [Route(2, "trace", segments=[seg])]
    assert grid.validate_via_clearance(via, 1)[0] is valid
    assert (grid.worst_via_segment_deficit(via, 1)[0] <= 0) is valid


def test_same_net_exempt_and_unrelated_high_class_does_not_widen_pair():
    rules = DesignRules(trace_clearance=0.1, net_clearance_floors={1: 0.2, 2: 0.1, 3: 2.0})
    assert rules.clearance_for_nets(1, 2, 0.1) == 0.2
    assert rules.clearance_for_nets(2, 4, 0.1) == 0.1
    assert rules.clearance_for_nets(1, 1, 0.0) == 0.0
    assert rules.max_clearance == 2.0  # Spatial candidate bounds, not pair policy.


@pytest.mark.skipif(not RTREE_AVAILABLE, reason="R-tree optional dependency required")
def test_rtree_finds_farther_violation_after_authored_floor_changes(monkeypatch):
    import kicad_tools.router.grid as grid_module

    monkeypatch.setattr(grid_module, "RTREE_SEGMENT_THRESHOLD", 1)
    rules = DesignRules(trace_clearance=0.1, via_clearance=0.1)
    grid = RoutingGrid(10, 10, rules)
    for net, y in ((2, 6.0), (3, 5.35)):
        grid.mark_route(Route(net, str(net), segments=[Segment(2, y, 8, y, 0.2, Layer.F_CU, net)]))
    rules.net_clearance_floors = {2: 0.9}
    assert grid._seg_rtree_count == 2
    candidate = Segment(2, 5, 8, 5, 0.2, Layer.F_CU, 1)
    indexed = grid.validate_segment_clearance(candidate, 1)
    grid._rtree_available = False
    brute = grid.validate_segment_clearance(candidate, 1)
    assert indexed == brute
    assert not indexed[0]
    assert indexed[1] == pytest.approx(0.15)


@pytest.mark.parametrize("mode", ["clamp", "skip"])
@pytest.mark.parametrize("gap,valid", [(0.18, False), (0.21, True)])
@pytest.mark.parametrize(
    "backend",
    [
        "python",
        pytest.param(
            "cpp", marks=pytest.mark.skipif(not is_cpp_available(), reason="native build required")
        ),
    ],
)
def test_authored_pad_floor_survives_component_relief(mode, gap, valid, backend):
    rules = DesignRules(trace_clearance=0.1, net_clearance_floors={2: 0.2})
    if mode == "clamp":
        rules.component_clearances["U1"] = 0.05
    grid = RoutingGrid(10, 10, rules)
    grid.add_pad(Pad(5, 5, 1, 1, 2, "foreign", ref="U1", pin="2"))
    if mode == "skip":
        grid._relaxed_clearance_refs.add("U1")
    seg = Segment(4, 5.6 + gap, 6, 5.6 + gap, 0.2, Layer.F_CU, 1)
    via = Via(5, 5.6 + gap, 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 1)
    if backend == "cpp":
        from kicad_tools.router import router_cpp

        native = CppGrid.from_routing_grid(grid)._impl
        s = router_cpp.Segment()
        s.x1, s.y1, s.x2, s.y2, s.width, s.layer, s.net = 4, seg.y1, 6, seg.y2, 0.2, 0, 1
        v = router_cpp.Via()
        v.x, v.y, v.drill, v.diameter, v.net = via.x, via.y, 0.1, 0.2, 1
        v.layer_from, v.layer_to = 0, 1
        refs = [router_cpp.fnv1a_hash("U1")]
        for segments, vias in (([s], []), ([], [v])):
            assert native.validate_route(segments, vias, 1, refs, 0.1, 0.1, 0.1).valid is valid
        return
    assert grid.validate_segment_clearance(seg, 1, exclude_refs={"U1"})[0] is valid
    assert (grid.worst_segment_pad_deficit(seg, 1, exclude_refs={"U1"})[0] <= 0) is valid
    assert (grid.worst_via_pad_deficit(via, 1, exclude_refs={"U1"})[0] <= 0) is valid


@pytest.mark.skipif(not is_cpp_available(), reason="native build required")
@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("gap,valid", [(0.18, False), (0.21, True)])
def test_native_partner_and_attach_zone_keep_authored_floor(strict_net, gap, valid):
    from kicad_tools.router import router_cpp

    rules = DesignRules(trace_clearance=0.1, net_clearance_floors={strict_net: 0.2, 3: 2.0})
    grid = RoutingGrid(10, 10, rules)
    native = CppGrid.from_routing_grid(grid)._impl
    # An HV table can be installed/cleared independently of the authored floors.
    native.set_pairwise_domains([-1, 0, 1], [[0, 0.4], [0.4, 0]])
    zone = router_cpp.AttachZone()
    zone.min_x, zone.min_y, zone.max_x, zone.max_y = 0, 0, 10, 10
    zone.net_ids = [1, 2]
    native.set_attach_zones([zone])
    seg = router_cpp.Segment()
    seg.x1, seg.x2, seg.y1, seg.y2 = 2, 8, 5.2 + gap, 5.2 + gap
    seg.width, seg.layer, seg.net = 0.2, 0, 1
    native.add_stored_segment(2, 5, 8, 5, 0.2, 0, 2)
    for partner in (-1, 2):
        assert (
            native.validate_route(
                [seg],
                [],
                1,
                [],
                0.1,
                0.1,
                0.1,
                partner_net=partner,
                intra_pair_clearance=0.05,
            ).valid
            is valid
        )
        assert native.route_trace_geometry_clear(seg, 0.1, partner, 0.05, 0.1) is valid
    native.set_pairwise_domains([], [])
    assert native.net_clearance_floor(1, 2) == pytest.approx(0.2)
    assert native.validate_route([seg], [], 1, [], 0.1, 0.1, 0.1).valid is valid


@pytest.mark.skipif(not is_cpp_available(), reason="native build required")
@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("gap,valid", [(0.18, False), (0.21, True)])
def test_native_via_quadrants_enforce_both_nets(strict_net, gap, valid):
    from kicad_tools.router import router_cpp

    rules = DesignRules(
        trace_clearance=0.1, via_clearance=0.1, net_clearance_floors={strict_net: 0.2}
    )
    native = CppGrid.from_routing_grid(RoutingGrid(10, 10, rules))._impl
    seg = router_cpp.Segment()
    seg.x1, seg.x2, seg.y1, seg.y2 = 2, 8, 5.4 + gap, 5.4 + gap
    seg.width, seg.layer, seg.net = 0.2, 0, 2
    via = router_cpp.Via()
    via.x, via.y, via.drill, via.diameter, via.net = 5, 5, 0.3, 0.6, 1
    via.layer_from, via.layer_to = 0, 1
    native.add_stored_via(5, 5, 0.3, 0.6, 1)
    assert native.validate_route([seg], [], 2, [], 0.1, 0.1, 0.1).valid is valid
    native.clear_stored_routes()
    native.add_stored_segment(2, seg.y1, 8, seg.y2, 0.2, 0, 2)
    assert native.validate_route([], [via], 1, [], 0.1, 0.1, 0.1).valid is valid
    native.clear_stored_routes()
    native.add_stored_via(5.6 + gap, 5, 0.3, 0.6, 2)
    assert native.validate_route([], [via], 1, [], 0.1, 0.1, 0.1).valid is valid


@pytest.mark.skipif(not is_cpp_available(), reason="native build required")
@pytest.mark.parametrize("method", ["route", "route_resumable"])
def test_native_search_routes_around_authored_via_floor(method):
    from kicad_tools.router.cpp_backend import CppPathfinder

    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.1,
        via_clearance=0.1,
        grid_resolution=0.05,
        net_clearance_floors={2: 0.4},
    )
    grid = RoutingGrid(10, 10, rules)
    native = CppGrid.from_routing_grid(grid)
    router = CppPathfinder(native, rules)
    router.set_routable_layers([0])
    native._impl.add_stored_via(5, 5, 0.3, 0.6, 2)
    native._impl.increment_usage(*grid.world_to_grid(5, 5), 0)
    route = getattr(router._impl, method)(
        3,
        5,
        0,
        7,
        5,
        0,
        1,
        negotiated_mode=True,
        emit_trace_width=0.2,
        max_search_iterations=20000,
    )
    assert route.success and route.segments
    for s in route.segments:
        dx, dy = s.x2 - s.x1, s.y2 - s.y1
        t = max(0, min(1, ((5 - s.x1) * dx + (5 - s.y1) * dy) / (dx * dx + dy * dy)))
        gap = math.hypot(5 - s.x1 - t * dx, 5 - s.y1 - t * dy) - (s.width + 0.6) / 2
        assert gap >= 0.4 - 1e-4
