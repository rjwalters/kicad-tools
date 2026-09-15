"""Search must refine dynamic route halos without relaxing physical clearances.

Issue #5410: the measured Board07 via separation is five by four cells on a
0.127 mm grid. Two 0.6 mm vias at that separation have 0.213 mm copper gap
and 0.513 mm drill gap, but the six-cell marking square rejects them.
"""

import math

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="native router required")


def _context():
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.127,
    )
    grid = RoutingGrid(
        width=20, height=20, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    native = CppGrid.from_routing_grid(grid)
    x, y = grid.grid_to_world(60, 60)
    native._impl.add_stored_via(x, y, 0.3, 0.6, 2)
    radius = math.ceil((0.3 + 0.2 + 0.1) / grid.resolution) + 1
    assert radius == 6
    native._impl.mark_via(60, 60, 2, radius)
    pathfinder = CppPathfinder(native, rules, diagonal_routing=True)
    pathfinder.set_routable_layers(native.get_routable_indices())
    return grid, native, pathfinder


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("offset,legal", [((-5, -4), True), ((-4, -4), False)])
def test_via_search_matches_physical_clearance_inside_dynamic_halo(sharing, offset, legal):
    from kicad_tools.router import router_cpp

    grid, native, pathfinder = _context()
    gx, gy = 60 + offset[0], 60 + offset[1]
    via = router_cpp.Via()
    via.x, via.y = grid.grid_to_world(gx, gy)
    via.drill, via.diameter = 0.3, 0.6
    via.layer_from, via.layer_to, via.net = 0, 3, 1
    result = native._impl.validate_route([], [via], 1, [], 0.15, 0.2, 0.5)
    assert result.valid == legal
    assert pathfinder._impl.is_via_blocked(gx, gy, 1, sharing, 4) == (not legal)


@pytest.mark.parametrize("sharing", [False, True])
def test_trace_search_can_cross_legal_dynamic_via_halo(sharing):
    from kicad_tools.router import router_cpp

    grid, native, pathfinder = _context()
    seg = router_cpp.Segment()
    seg.x1, seg.y1 = grid.grid_to_world(55, 56)
    seg.x2, seg.y2 = grid.grid_to_world(54, 57)
    seg.width, seg.layer, seg.net = 0.15, 2, 1
    assert native._impl.validate_route([seg], [], 1, [], 0.15, 0.2, 0.5).valid
    assert not pathfinder._impl.is_trace_blocked(55, 56, 2, 1, sharing, 2)
    assert not pathfinder._impl.is_diagonal_blocked(55, 56, -1, 1, 2, 1, sharing)


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("kind", ["pad", "static_halo", "missing_geometry", "reservation"])
def test_refinement_preserves_hard_or_unverifiable_blockage(sharing, kind):
    _, native, pathfinder = _context()
    if kind == "missing_geometry":
        native._impl.clear_stored_routes()
    elif kind == "reservation":
        native._impl.reserve_cell(55, 56, 2, [2], False)
    else:
        native._impl.mark_blocked(55, 56, 2, 2, kind == "pad", kind == "pad")
    assert pathfinder._impl.is_trace_blocked(55, 56, 2, 1, sharing, 2)
    if kind != "reservation":
        # Via reservation handling has a separate gate in the neighbor loop.
        assert pathfinder._impl.is_via_blocked(55, 56, 1, sharing, 4)


def test_route_geometry_coverage_tracks_missing_and_overlapping_marks():
    grid, native, _ = _context()
    assert native._impl.route_geometry_complete()
    native._impl.mark_via(61, 60, 3, 6)
    assert not native._impl.route_geometry_complete()
    x, y = grid.grid_to_world(61, 60)
    native._impl.add_stored_via(x, y, 0.3, 0.6, 3)
    assert native._impl.route_geometry_complete()
    native._impl.unmark_via(61, 60, 3, 6)
    assert native._impl.route_geometry_complete()
    native._impl.clear_stored_routes()
    assert not native._impl.route_geometry_complete()
    # Re-registering only the removed route must not cover the surviving mark.
    native._impl.add_stored_via(x, y, 0.3, 0.6, 3)
    assert not native._impl.route_geometry_complete()
    x, y = grid.grid_to_world(60, 60)
    native._impl.add_stored_via(x, y, 0.3, 0.6, 2)
    assert native._impl.route_geometry_complete()


def test_route_geometry_coverage_preserves_repeated_marks():
    grid, native, _ = _context()
    native._impl.mark_via(60, 60, 2, 6)
    native._impl.unmark_via(60, 60, 2, 6)
    native._impl.clear_stored_routes()
    assert not native._impl.route_geometry_complete()
    x, y = grid.grid_to_world(60, 60)
    native._impl.add_stored_via(x, y, 0.3, 0.6, 2)
    assert native._impl.route_geometry_complete()
    native._impl.unmark_via(60, 60, 2, 6)
    assert not native._impl.route_geometry_complete()


def test_route_geometry_coverage_matches_segment_direction_and_layer():
    grid, native, _ = _context()
    native._impl.mark_segment(50, 50, 55, 55, 2, 3, 2)
    x1, y1 = grid.grid_to_world(50, 50)
    x2, y2 = grid.grid_to_world(55, 55)
    native._impl.add_stored_segment(x2, y2, x1, y1, 0.15, 1, 3)
    assert not native._impl.route_geometry_complete()
    native._impl.add_stored_segment(x2, y2, x1, y1, 0.15, 2, 3)
    assert native._impl.route_geometry_complete()
    native._impl.unmark_segment(55, 55, 50, 50, 2, 3, 2)
    assert native._impl.route_geometry_complete()


def test_route_geometry_index_covers_extents_deduplicates_and_clears():
    grid, native, _ = _context()
    x, y = grid.grid_to_world(60, 60)
    native._impl.add_stored_segment(-4, -4, 8, 8, 0.4, 2, 3)
    native._impl.add_stored_via(18, 18, 0.3, 0.6, 4)
    segments, vias = native._impl.route_geometry_candidates(x - 1, y - 1, x + 1, y + 1)
    assert segments == [0]
    assert vias == [0]
    segments, vias = native._impl.route_geometry_candidates(-4.2, -4.2, -4, -4)
    assert segments == [0]
    assert vias == []
    segments, vias = native._impl.route_geometry_candidates(-10, -10, 20, 20)
    assert segments == [0]
    assert vias == [0, 1]
    native._impl.clear_stored_routes()
    assert native._impl.route_geometry_candidates(-10, -10, 20, 20) == ([], [])


@pytest.mark.parametrize("sharing", [False, True])
def test_drill_floor_remains_hard_when_copper_gap_is_legal(sharing):
    from kicad_tools.router import router_cpp

    grid, native, pathfinder = _context()
    via = router_cpp.Via()
    via.x, via.y = grid.grid_to_world(56, 56)
    via.diameter, via.drill, via.net = 0.3, 0.3, 1
    via.layer_from, via.layer_to = 0, 3
    # Copper gap is .268 mm, but drill gap is .418 mm (< .5 mm).
    assert native._impl.validate_route([], [via], 1, [], 0.15, 0.2, 0.102).valid
    assert not native._impl.route_via_geometry_clear(via, 0.2, 0.5, 0.102)
    pathfinder._impl.set_search_pair_widths(0.075, 0.15)
    # Even negotiated occupancy cannot make an illegal hole negotiable.
    for layer in range(4):
        for y in range(50, 70):
            for x in range(50, 70):
                native._impl.increment_usage(x, y, layer)
    assert pathfinder._impl.is_via_blocked(56, 56, 1, sharing, 3)


def test_dynamic_cell_coverage_does_not_infer_ownership_from_net_alone():
    _, native, _ = _context()
    assert native._impl.route_cell_has_geometry(55, 56, 2)
    cell = native._impl.at(10, 10, 2)
    cell.net, cell.blocked = 2, True
    assert not native._impl.route_cell_has_geometry(10, 10, 2)
    native._impl.mark_blocked(55, 56, 2, 2, False, False)
    assert not native._impl.route_cell_has_geometry(55, 56, 2)


@pytest.mark.parametrize("method", ["route", "route_resumable"])
@pytest.mark.parametrize("sharing", [False, True])
def test_search_crosses_dynamic_halo_with_valid_swept_geometry(method, sharing):
    grid, native, pathfinder = _context()
    start = grid.grid_to_world(52, 55)
    end = grid.grid_to_world(56, 55)
    result = getattr(pathfinder._impl, method)(
        start[0],
        start[1],
        2,
        end[0],
        end[1],
        2,
        1,
        negotiated_mode=sharing,
        max_search_iterations=100,
    )
    assert result.success
    assert native._impl.validate_route(result.segments, result.vias, 1, [], 0.15, 0.2, 0.102).valid


@pytest.mark.parametrize("sharing", [False, True])
def test_dynamic_refinement_preserves_pairwise_widening(sharing):
    _, native, pathfinder = _context()
    native._impl.set_pairwise_domains([-1, 0, 1], [[0.0, 0.4], [0.4, 0.0]])
    assert pathfinder._impl.is_via_blocked(55, 56, 1, sharing, 4)


def test_dynamic_refinement_uses_authored_partner_gap():
    from kicad_tools.router import router_cpp

    grid, native, pathfinder = _context()
    segment = router_cpp.Segment()
    segment.x1, segment.y1 = grid.grid_to_world(55, 56)
    segment.x2, segment.y2 = grid.grid_to_world(54, 57)
    segment.width, segment.layer, segment.net = 0.15, 2, 1
    assert native._impl.route_trace_geometry_clear(segment, 0.15, 2, 0.1)
    assert not native._impl.route_trace_geometry_clear(segment, 0.15, 2, 0.6)
    pathfinder._impl.set_search_partner_clearance(2, 0.6)
    assert pathfinder._impl.is_trace_blocked(55, 56, 2, 1, False, 2, 2, 6)


def test_trace_refinement_checks_swept_step_not_only_endpoints():
    grid, native, pathfinder = _context()
    native._impl.unmark_via(60, 60, 2, 6)
    native._impl.clear_stored_routes()
    x, y = grid.grid_to_world(55, 56)
    vx, vy = x + grid.resolution / 2, y + 0.474
    gx, gy = grid.world_to_grid(vx, vy)
    native._impl.add_stored_via(vx, vy, 0.3, 0.6, 2)
    native._impl.mark_via(gx, gy, 2, 6)
    pathfinder._impl.set_search_pair_widths(0.075, 0.3)
    pathfinder._impl.set_search_fill_clearances(0.1, 0.2)
    # Both endpoints clear the 0.475 mm centerline limit. The interior doesn't.
    assert not pathfinder._impl.is_trace_blocked(55, 56, 2, 1, False, 2)
    assert not pathfinder._impl.is_trace_blocked(56, 56, 2, 1, False, 2)
    assert pathfinder._impl.is_trace_blocked(56, 56, 2, 1, False, 2, -1, 0, 55, 56)


@pytest.mark.parametrize("method", ["route", "route_resumable"])
def test_partner_clearance_does_not_leak_into_next_route(method):
    grid, _, pathfinder = _context()
    pathfinder._impl.set_search_partner_clearance(2, 0.6)
    assert pathfinder._impl.is_trace_blocked(55, 56, 2, 1, False, 2)
    start, end = grid.grid_to_world(20, 20), grid.grid_to_world(22, 20)
    result = getattr(pathfinder._impl, method)(*start, 2, *end, 2, 3, max_search_iterations=100)
    assert result.success
    assert not pathfinder._impl.is_trace_blocked(55, 56, 2, 1, False, 2)


def test_coverage_rejects_a_changed_cell_owner():
    _, native, _ = _context()
    assert native._impl.route_cell_has_geometry(55, 56, 2)
    native._impl.at(55, 56, 2).net = 99
    assert not native._impl.route_cell_has_geometry(55, 56, 2)


@pytest.mark.parametrize("backend", ["cpp", "python"])
def test_captured_board07_dq3_via_with_original_pad_geometry(backend):
    """Reduced real #5393 witness; full project/refill evidence lives in #5410.

    Keep the captured grid alignment, DQ3 pads, DQS_N copper and effective
    routing rules. This isolates the legal via predicate, not whole-board reach.
    """
    from kicad_tools.router.pathfinder import Router
    from kicad_tools.router.primitives import Layer, Pad, Route, Via
    from kicad_tools.router.rules import NetClassRouting

    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.127,
    )
    grid = RoutingGrid(
        width=65,
        height=50,
        origin_x=100.089,
        origin_y=100.051,
        rules=rules,
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    for x, y, ref, pin in [(125.0, 127.0, "U1", "28"), (145.0, 123.0, "U2", "4")]:
        grid.add_pad(
            Pad(
                x=x,
                y=y,
                width=0.7,
                height=0.3,
                net=7,
                net_name="DQ3",
                layer=Layer.F_CU,
                ref=ref,
                pin=pin,
            )
        )
    native = CppGrid.from_routing_grid(grid)
    prior = Via(143.7769928, 123.8000031, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 14, "DQS_N")
    route = Route(net=14, net_name="DQS_N", vias=[prior])
    grid.mark_route(route)
    gx, gy = grid.world_to_grid(143.142, 123.292)
    assert (gx, gy) == (339, 183)
    assert grid.grid_to_world(gx, gy) == pytest.approx((143.142, 123.292))
    copper_gap = math.hypot(prior.x - 143.142, prior.y - 123.292) - 0.6
    assert 0.213 < copper_gap < 0.214
    assert copper_gap + 0.3 > rules.min_hole_to_hole
    if backend == "cpp":
        vx, vy = grid.world_to_grid(prior.x, prior.y)
        native._impl.mark_via(vx, vy, 14, 6)
        native._impl.add_stored_via(prior.x, prior.y, prior.drill, prior.diameter, 14)
        router = CppPathfinder(native, rules)
        router._impl.set_search_pair_widths(0.075, 0.3)
        router._impl.set_search_fill_clearances(0.1, 0.2)
        assert native._impl.route_cell_has_geometry(gx, gy, 2)
        assert not router._impl.is_via_blocked(gx, gy, 7, False, 4)
        assert router._impl.is_via_blocked(gx + 1, gy, 7, False, 4)
    else:
        router = Router(
            grid,
            rules,
            {
                "DQ3": NetClassRouting(
                    name="DDR_DATA_BYTE_0",
                    trace_width=0.15,
                    clearance=0.1,
                    via_size=0.6,
                )
            },
        )
        router.set_net_name_to_id({"DQ3": 7, "DQS_N": 14})
        assert grid._route_halo.cell_known(gx, gy, 2)
        assert not router._is_via_blocked(gx, gy, 2, 7, False, radius=4)
        assert router._is_via_blocked(gx + 1, gy, 2, 7, False, radius=4)


def test_native_overlap_checks_hidden_owner_and_rebuild_drops_ripped_geometry():
    grid, native, router = _context()
    assert not router._impl.is_via_blocked(55, 56, 1, False, 4)
    x, y = grid.grid_to_world(59, 58)
    native._impl.mark_via(59, 58, 3, 6)
    native._impl.add_stored_via(x, y, 0.3, 0.6, 3)
    assert native._impl.at(59, 58, 2).net == 2
    assert router._impl.is_via_blocked(55, 56, 1, False, 4)
    native._impl.unmark_via(59, 58, 3, 6)
    native._impl.clear_stored_routes()
    x, y = grid.grid_to_world(60, 60)
    native._impl.add_stored_via(x, y, 0.3, 0.6, 2)
    assert native._impl.route_geometry_complete()
    assert not router._impl.is_via_blocked(55, 56, 1, False, 4)


@pytest.mark.parametrize("kind", ["segment", "via"])
def test_stored_geometry_uses_the_python_mark_coordinates_at_half_cells(kind):
    """Board06's real neck-down endpoint rounds to352 in Python,353 in C++."""
    from kicad_tools.router.primitives import Layer, Route, Segment, Via

    rules = DesignRules(grid_resolution=0.05)
    grid = RoutingGrid(
        width=65,
        height=50,
        origin_x=98.5,
        origin_y=47.5,
        rules=rules,
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    native = CppGrid.from_routing_grid(grid)
    x1, y1, x2, y2 = 112.30000305175781, 65.0999984741211, 112.30000305175781, 65.125
    assert grid.world_to_grid(x2, y2) == (276, 352)
    assert native._impl.world_to_grid(x2, y2) == (276, 353)
    route = Route(net=7, net_name="USB2_D-")
    if kind == "segment":
        route.segments.append(Segment(x1, y1, x2, y2, 0.23145000000078977, Layer.F_CU, 7))
        native._impl.mark_segment(*grid.world_to_grid(x1, y1), *grid.world_to_grid(x2, y2), 0, 7, 6)
    else:
        route.vias.append(Via(x2, y2, 0.25, 0.6, (Layer.F_CU, Layer.B_CU), 7))
        native._impl.mark_via(*grid.world_to_grid(x2, y2), 7, 6)
    grid.mark_route(route)
    router = CppPathfinder(native, rules)
    assert not native._impl.route_geometry_complete()
    router._sync_stored_routes(grid)
    assert native._impl.route_geometry_complete()
    assert native._impl.route_cell_has_geometry(276, 352, 0)
    native.invalidate_stored_routes()
    assert not native._impl.route_geometry_complete()
    router._sync_stored_routes(grid)
    assert native._impl.route_geometry_complete()
