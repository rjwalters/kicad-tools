"""The native backend enforces authored per-net minima in search and validation (#6243).

Ported from closed PR #5502 (``test_authored_net_clearance.py`` native cases,
``test_native_authored_search.py``) onto ``Grid3D``'s kernel-backed authored
gate.  Skipped without the compiled extension.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="native build required")


def _seg(router_cpp, x1, y1, x2, y2, width, layer, net):
    s = router_cpp.Segment()
    s.x1, s.y1, s.x2, s.y2, s.width, s.layer, s.net = x1, y1, x2, y2, width, layer, net
    return s


def _via(router_cpp, x, y, drill, diameter, net, layers=(0, 1)):
    v = router_cpp.Via()
    v.x, v.y, v.drill, v.diameter, v.net = x, y, drill, diameter, net
    v.layer_from, v.layer_to = layers
    return v


def test_dormant_without_floors():
    native = CppGrid.from_routing_grid(RoutingGrid(10, 10, DesignRules()))._impl
    assert not native.authored_active()
    assert native.authored_segment_clear(0, 0, 1, 0, 0.2, 0, 1)


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("gap,valid", [(0.18, False), (0.21, True)])
def test_native_partner_and_attach_zone_keep_authored_floor(strict_net, gap, valid):
    from kicad_tools.router import router_cpp

    rules = DesignRules(trace_clearance=0.1, net_clearance_floors={strict_net: 0.2, 3: 2.0})
    native = CppGrid.from_routing_grid(RoutingGrid(10, 10, rules))._impl
    # An HV table can be installed independently of the authored floors.
    native.set_pairwise_domains([-1, 0, 1], [[0, 0.4], [0.4, 0]])
    zone = router_cpp.AttachZone()
    zone.min_x, zone.min_y, zone.max_x, zone.max_y = 0, 0, 10, 10
    zone.net_ids = [1, 2]
    native.set_attach_zones([zone])
    seg = _seg(router_cpp, 2, 5.2 + gap, 8, 5.2 + gap, 0.2, 0, 1)
    native.add_stored_segment(2, 5, 8, 5, 0.2, 0, 2)
    for partner in (-1, 2):
        result = native.validate_route(
            [seg], [], 1, [], 0.1, 0.1, 0.1, partner_net=partner, intra_pair_clearance=0.05
        )
        assert result.valid is valid
        if not valid:
            assert result.violation_type == 9
    native.set_pairwise_domains([], [])
    assert native.authored_pair_floor(1, 2) == pytest.approx(0.2)
    assert native.authored_pair_floor(4, 5) == 0.0
    assert native.authored_pair_floor(3, 3) == 0.0
    assert native.validate_route([seg], [], 1, [], 0.1, 0.1, 0.1).valid is valid


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("gap,valid", [(0.18, False), (0.21, True)])
def test_native_via_quadrants_enforce_both_nets(strict_net, gap, valid):
    from kicad_tools.router import router_cpp

    rules = DesignRules(
        trace_clearance=0.1, via_clearance=0.1, net_clearance_floors={strict_net: 0.2}
    )
    native = CppGrid.from_routing_grid(RoutingGrid(10, 10, rules))._impl
    seg = _seg(router_cpp, 2, 5.4 + gap, 8, 5.4 + gap, 0.2, 0, 2)
    via = _via(router_cpp, 5, 5, 0.3, 0.6, 1)
    native.add_stored_via(5, 5, 0.3, 0.6, 1)
    assert native.validate_route([seg], [], 2, [], 0.1, 0.1, 0.1).valid is valid
    native.clear_stored_routes()
    native.add_stored_segment(2, seg.y1, 8, seg.y2, 0.2, 0, 2)
    assert native.validate_route([], [via], 1, [], 0.1, 0.1, 0.1).valid is valid
    native.clear_stored_routes()
    native.add_stored_via(5.6 + gap, 5, 0.3, 0.6, 2)
    assert native.validate_route([], [via], 1, [], 0.1, 0.1, 0.1).valid is valid


@pytest.mark.parametrize("mode", ["clamp", "skip"])
@pytest.mark.parametrize("gap,valid", [(0.18, False), (0.21, True)])
def test_native_pad_floor_survives_component_relief(mode, gap, valid):
    from kicad_tools.router import router_cpp

    rules = DesignRules(trace_clearance=0.1, net_clearance_floors={2: 0.2})
    if mode == "clamp":
        rules.component_clearances["U1"] = 0.05
    grid = RoutingGrid(10, 10, rules)
    grid.add_pad(Pad(5, 5, 1, 1, 2, "foreign", ref="U1", pin="2"))
    if mode == "skip":
        grid._relaxed_clearance_refs.add("U1")
    native = CppGrid.from_routing_grid(grid)._impl
    seg = _seg(router_cpp, 4, 5.6 + gap, 6, 5.6 + gap, 0.2, 0, 1)
    via = _via(router_cpp, 5, 5.6 + gap, 0.1, 0.2, 1)
    refs = [router_cpp.fnv1a_hash("U1")]
    for segments, vias in (([seg], []), ([], [via])):
        assert native.validate_route(segments, vias, 1, refs, 0.1, 0.1, 0.1).valid is valid


@pytest.mark.parametrize("gap,valid", [(0.45, False), (0.55, True)])
def test_native_item_floors_on_neutral_pad_and_copper(gap, valid):
    from kicad_tools.router import router_cpp

    grid = RoutingGrid(10, 10, DesignRules(trace_clearance=0.1))
    grid.add_pad(Pad(5, 5, 1, 1, 0, "", ref="J1", pin="1", authored_clearance=0.5))
    native = CppGrid.from_routing_grid(grid)._impl
    assert native.authored_active()
    seg = _seg(router_cpp, 4, 5.6 + gap, 6, 5.6 + gap, 0.2, 0, 1)
    assert native.validate_route([seg], [], 1, [], 0.1, 0.1, 0.1).valid is valid
    # Neutral route copper with its own floor activates a dormant grid.
    plain = CppGrid.from_routing_grid(RoutingGrid(10, 10, DesignRules(trace_clearance=0.1)))._impl
    assert not plain.authored_active()
    plain.add_stored_segment(2, 5, 8, 5, 0.2, 0, 0, None, 0.5)
    assert plain.authored_active()
    probe = _seg(router_cpp, 2, 5.2 + gap, 8, 5.2 + gap, 0.2, 0, 1)
    assert plain.validate_route([probe], [], 1, [], 0.1, 0.1, 0.1).valid is valid


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("sharing", [False, True])
def test_native_search_detours_without_raster_coverage(strict_net, sharing, monkeypatch):
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.1,
        grid_resolution=0.1,
        net_clearance_floors={strict_net: 0.6, 3: 4.0},
    )
    grid = RoutingGrid(10, 10, rules)
    # Imported copper may be present without a matching occupancy halo.  An
    # unrelated high-clearance net must not widen this pair.
    grid.routes.append(Route(2, "foreign", segments=[Segment(4, 5, 6, 5, 0.2, Layer.F_CU, 2)]))
    router = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
    monkeypatch.setattr(router, "_try_python_fallback", lambda *a, **kw: None)
    result = router.route(
        Pad(2, 5.5, 0.1, 0.1, 1, "signal"),
        Pad(8, 5.5, 0.1, 0.1, 1, "signal"),
        negotiated_mode=sharing,
    )
    assert result is not None and result.segments
    assert all(grid.validate_segment_clearance(seg, 1)[0] for seg in result.segments)


@pytest.mark.parametrize("method", ["route", "route_resumable"])
def test_native_search_routes_around_authored_via_floor(method):
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
