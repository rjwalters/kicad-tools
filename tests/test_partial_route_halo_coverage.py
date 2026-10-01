"""Unregistered corridors must not disable refinement in unrelated regions."""

import pytest

from kicad_tools.router.primitives import Layer, Via

# Issue #5660 (Epic #5509 Phase 3a): route halos are the clearance kernel's
# exact disc, not the Chebyshev square they used to be.  Every probe below
# sits at ``(55, 56)`` -- offset ``(-5, -4)`` from the via at ``(60, 60)``,
# Euclidean distance ``sqrt(41) = 6.40`` cells -- which the old six-cell
# square covered at its corner and the disc does not.  A seven-cell marking
# radius restores the premise these tests are about: a cell inside a
# *conservative* halo whose real geometry is legal.  The physical predicates
# are untouched by this -- they read rule values, never the marking radius.
HALO_RADIUS_CELLS = 7


@pytest.fixture(params=["native", "python"])
def context(request):
    from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.pathfinder import Router
    from kicad_tools.router.primitives import Route
    from kicad_tools.router.rules import DesignRules

    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        min_drill_clearance=0.5,
        grid_resolution=0.127,
    )
    grid = RoutingGrid(
        width=20, height=20, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    x, y = grid.grid_to_world(60, 60)
    if request.param == "native":
        if not is_cpp_available():
            pytest.skip("native router required")
        native = CppGrid.from_routing_grid(grid)
        native._impl.add_stored_via(x, y, 0.3, 0.6, 2)
        native._impl.mark_via(60, 60, 2, HALO_RADIUS_CELLS)
        router = CppPathfinder(native, rules)
        router.set_routable_layers(native.get_routable_indices())
    else:
        native = None
        route = Route(net=2, net_name="N2")
        route.vias.append(Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2"))
        # ``max_trace_width`` is the only knob on ``_mark_via``'s radius; 0.6 mm
        # yields HALO_RADIUS_CELLS on this 0.127 mm grid.
        grid.mark_route(route, max_trace_width=0.6)
        grid._test_unknown_snapshots = {}
        router = Router(grid, rules)
    return grid, native, router


def unknown(context, x, y, net=3, radius=HALO_RADIUS_CELLS, add=True):
    grid, native, _ = context
    if native:
        method = native._impl.mark_via if add else native._impl.unmark_via
        method(x, y, net, radius)
    else:
        wx, wy = grid.grid_to_world(x, y)
        via = Via(wx, wy, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net, f"N{net}")
        grid._route_halo.record(grid._route_halo.via_key(via), radius, add)
        if add and net == 1:
            # Exercise Python's insertion order as well as native's key order:
            # the registered mark is visited after the unknown footprint.
            for (mark, mark_radius), count in list(grid._route_halo.marks.items()):
                if mark[1] == 2:
                    for _ in range(count):
                        grid._route_halo.record(mark, mark_radius, False)
                    for _ in range(count):
                        grid._route_halo.record(mark, mark_radius, True)
        region = (slice(None), slice(y - radius, y + radius + 1), slice(x - radius, x + radius + 1))
        key = (x, y, net, radius)
        if add:
            grid._test_unknown_snapshots[key] = (
                grid._blocked[region].copy(),
                grid._net[region].copy(),
            )
            for layer in range(grid.num_layers):
                for cy in range(y - radius, y + radius + 1):
                    for cx in range(x - radius, x + radius + 1):
                        cell = grid.cell_at(layer, cy, cx)
                        if not cell.blocked:
                            cell.net = net
                        cell.blocked = True
        else:
            grid._blocked[region], grid._net[region] = grid._test_unknown_snapshots.pop(key)
        grid.bump_occupancy_generation()


def blocked(context, kind, x=55, y=56, sharing=True):
    _, native, router = context
    if native:
        if kind == "via":
            return router._impl.is_via_blocked(x, y, 99, sharing, 4)
        return router._impl.is_trace_blocked(x, y, 2, 99, sharing, 2)
    if kind == "via":
        return router._is_via_blocked(x, y, 2, 99, sharing, radius=4)
    return router._is_trace_blocked(x, y, 2, 99, sharing, radius=2)


def known(context, x=55, y=56):
    grid, native, _ = context
    if native:
        return native._impl.route_cell_has_geometry(x, y, 2)
    return grid._route_halo.cell_known(x, y, 2)


@pytest.mark.parametrize("kind", ["trace", "via"])
@pytest.mark.parametrize("sharing", [False, True])
def test_far_unknown_corridor_preserves_local_physical_refinement(context, kind, sharing):
    assert not blocked(context, kind, sharing=sharing)
    unknown(context, 120, 120)
    assert known(context)
    assert not blocked(context, kind, sharing=sharing)
    assert blocked(context, "via", x=56, y=56, sharing=sharing)
    unknown(context, 120, 120, add=False)
    assert not blocked(context, kind, sharing=sharing)


@pytest.mark.parametrize("unknown_net", [1, 3])
@pytest.mark.parametrize("kind", ["trace", "via"])
def test_unknown_overlap_cannot_hide_behind_registered_owner(context, unknown_net, kind):
    grid, native, _ = context
    unknown(context, 60, 60, net=unknown_net)
    # The known via remains the visible owner. Unknown marks must still veto
    # local refinement, whether they sort before or after the known key.
    cell = native._impl.at(55, 56, 2) if native else grid.cell_at(2, 56, 55)
    assert cell.net == 2
    assert not known(context)
    assert blocked(context, kind)
    unknown(context, 60, 60, net=unknown_net, add=False)
    assert known(context)
    assert not blocked(context, kind)


@pytest.mark.parametrize("kind,x", [("trace", 52), ("via", 50)])
def test_unknown_at_candidate_kernel_edge_stays_hard(context, kind, x):
    unknown(context, x, 56, radius=1)
    assert known(context)  # Candidate center alone is insufficient evidence.
    assert blocked(context, kind)


@pytest.mark.parametrize("kind", ["trace", "via"])
def test_changed_registered_geometry_invalidates_partial_coverage(context, kind):
    grid, native, _ = context
    unknown(context, 120, 120)
    assert not blocked(context, kind)
    if native:
        native._impl.clear_stored_routes()
        x, y = grid.grid_to_world(90, 90)
        native._impl.add_stored_via(x, y, 0.3, 0.6, 2)
    else:
        via = grid.routes[0].vias[0]
        via.x, via.y = grid.grid_to_world(90, 90)
        grid.bump_occupancy_generation()
    assert not known(context)
    assert blocked(context, kind)
    if native:
        native._impl.clear_stored_routes()
        x, y = grid.grid_to_world(60, 60)
        native._impl.add_stored_via(x, y, 0.3, 0.6, 2)
    else:
        via.x, via.y = grid.grid_to_world(60, 60)
        grid.bump_occupancy_generation()
    assert known(context)
    assert not blocked(context, kind)


@pytest.mark.parametrize("kind", ["trace", "via"])
@pytest.mark.parametrize("pad", [False, True])
def test_partial_coverage_preserves_static_and_pad_exclusions(context, kind, pad):
    grid, native, _ = context
    unknown(context, 120, 120)
    assert not blocked(context, kind)
    if native:
        native._impl.mark_blocked(55, 56, 2, 2, pad, pad)
    else:
        grid._static_blocked[2, 56, 55] = True
        grid._pad_blocked[2, 56, 55] = pad
        grid._is_obstacle[2, 56, 55] = pad
        grid.bump_occupancy_generation()
    assert not known(context)
    assert blocked(context, kind)


def test_partial_coverage_preserves_reservation_exclusion(context):
    grid, native, _ = context
    unknown(context, 120, 120)
    if native:
        native._impl.reserve_cell(55, 56, 2, [2], False)
    else:
        grid.reserve_corridor_cells(2, {(55, 56)}, {2})
    assert not known(context)
    assert blocked(context, "trace")


def test_repeated_unknown_mark_requires_all_unmarks(context):
    grid, native, _ = context
    unknown(context, 60, 60)
    if native:
        native._impl.mark_via(60, 60, 3, HALO_RADIUS_CELLS)
        native._impl.unmark_via(60, 60, 3, HALO_RADIUS_CELLS)
    else:
        x, y = grid.grid_to_world(60, 60)
        via = Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 3, "N3")
        key = grid._route_halo.via_key(via)
        grid._route_halo.record(key, HALO_RADIUS_CELLS, True)
        grid._route_halo.record(key, HALO_RADIUS_CELLS, False)
    assert not known(context)
    assert blocked(context, "trace")
    unknown(context, 60, 60, add=False)
    assert known(context)
    assert not blocked(context, "trace")


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("radius", [None, 4])
def test_partial_coverage_preserves_same_net_drill_spacing(context, sharing, radius):
    _, native, router = context

    def via_blocked(x, y):
        if native:
            return router._impl.is_via_blocked(x, y, 2, sharing, radius or 0)
        return router._is_via_blocked(x, y, 2, 2, sharing, radius=radius)

    assert via_blocked(56, 56)
    assert not via_blocked(55, 55)
    unknown(context, 120, 120)
    assert via_blocked(56, 56)
    assert not via_blocked(55, 55)


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("case", ["foreign_close", "foreign_clear", "same_net"])
def test_known_trace_still_blocks_via_with_incomplete_raster(context, sharing, case):
    """Known copper stays authoritative when another route mark lacks geometry."""
    from kicad_tools.router.primitives import Route, Segment

    grid, native, router = context
    unknown(context, 120, 120)
    net = 99 if case == "same_net" else 24
    x1, y = grid.grid_to_world(90, 90)
    x2, _ = grid.grid_to_world(110, 90)
    if native:
        # Stored geometry can be present without a matching raster mark while
        # imported or incrementally synchronized routes have partial coverage.
        native._impl.add_stored_segment(x1, y, x2, y, 0.375, 1, net, (90, 90, 110, 90))
        assert not native._impl.route_geometry_complete()
    else:
        segment = Segment(x1, y, x2, y, 0.375, Layer.IN1_CU, net)
        grid.routes.append(Route(net=net, net_name=f"N{net}", segments=[segment]))
        grid.bump_occupancy_generation()
        assert not grid._route_halo.complete
    cy = 98 if case == "foreign_clear" else 94
    # Close gap is 4*0.127 - 0.3 - 0.1875 = 0.0205mm, below the
    # 0.2mm via floor. The other-layer trace must constrain a through via.
    if native:
        actual = router._impl.is_via_blocked(100, cy, 99, sharing, 4)
    else:
        actual = router._is_via_blocked(100, cy, 0, 99, sharing, radius=4)
    assert actual == (case == "foreign_close")
