"""Stored copper retains physical spans when routing selects a layer subset."""

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerDefinition, LayerStack, LayerType
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="native router required")


def _context(layers):
    stack = LayerStack(
        [
            LayerDefinition(layer.kicad_name, i, LayerType.SIGNAL, layer.is_outer)
            for i, layer in enumerate(layers)
        ]
    )
    rules = DesignRules(
        grid_resolution=0.1,
        trace_width=0.2,
        trace_clearance=0.15,
        via_clearance=0.2,
        min_drill_clearance=0.5,
        min_hole_to_hole=0.5,
    )
    grid = RoutingGrid(10, 10, rules=rules, layer_stack=stack)
    native = CppGrid.from_routing_grid(grid)
    return grid, native, CppPathfinder(native, rules)


def _native_trace_valid(native, index):
    from kicad_tools.router import router_cpp

    segment = router_cpp.Segment()
    segment.x1, segment.y1, segment.x2, segment.y2 = 3, 4, 5, 4
    segment.width, segment.layer, segment.net = 0.2, index, 1
    return native._impl.validate_route([segment], [], 1, [], 0.15, 0.2, 0.5).valid


@pytest.mark.parametrize(
    "active",
    [
        [Layer.F_CU],
        [Layer.B_CU],
        [Layer.IN1_CU],
        [Layer.F_CU, Layer.IN2_CU, Layer.B_CU],
        [Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU, Layer.B_CU],
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_through_via_blocks_every_selected_layer(active, reverse):
    grid, native, pathfinder = _context(active)
    span = (Layer.B_CU, Layer.F_CU) if reverse else (Layer.F_CU, Layer.B_CU)
    via = Via(4, 4, 0.3, 0.6, span, 2)
    grid.routes.append(Route(2, "stored", vias=[via]))
    pathfinder._sync_stored_routes(grid)
    assert all(not _native_trace_valid(native, i) for i in range(len(active)))
    assert via.layers == span  # Imported physical endpoints remain authored.
    pathfinder._sync_stored_routes(grid)
    assert native._synced_route_count == 1


@pytest.mark.parametrize(
    "span",
    [
        (Layer.F_CU, Layer.IN1_CU),
        (Layer.IN1_CU, Layer.F_CU),
        (Layer.IN1_CU, Layer.IN2_CU),
        (Layer.IN2_CU, Layer.IN1_CU),
        (Layer.IN3_CU, Layer.IN4_CU),
    ],
)
def test_blind_and_buried_copper_does_not_spread_to_unrelated_layers(span):
    active = [Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU, Layer.B_CU]
    grid, native, pathfinder = _context(active)
    grid.routes.append(Route(2, "stored", vias=[Via(4, 4, 0.3, 0.6, span, 2)]))
    pathfinder._sync_stored_routes(grid)
    lo, hi = sorted(layer.value for layer in span)
    for i, layer in enumerate(active):
        assert _native_trace_valid(native, i) is not (lo <= layer.value <= hi)


def test_off_grid_via_still_enforces_global_drill_spacing():
    from kicad_tools.router import router_cpp

    grid, native, pathfinder = _context([Layer.F_CU])
    grid.routes.append(
        Route(2, "stored", vias=[Via(4, 4, 0.3, 0.6, (Layer.IN1_CU, Layer.IN2_CU), 2)])
    )
    pathfinder._sync_stored_routes(grid)
    assert _native_trace_valid(native, 0)  # No barrel copper on F.Cu.
    candidate = router_cpp.Via()
    candidate.x, candidate.y, candidate.diameter, candidate.drill = 4.65, 4, 0.1, 0.1
    candidate.layer_from = candidate.layer_to = 0
    candidate.net = 1
    # Copper edges are 0.30 apart (>0.2), but drills only 0.45 apart (<0.5).
    assert not native._impl.route_via_geometry_clear(candidate, 0.2, 0.5, 0.5)
    candidate.net = 2  # The final validator's same-net drill-spacing guard.
    assert not native._impl.validate_route([], [candidate], 2, [], 0.15, 0.2, 0.5).valid
    candidate.x = 4.8
    assert native._impl.validate_route([], [candidate], 2, [], 0.15, 0.2, 0.5).valid
    candidate.net, candidate.x = 1, 4.5
    assert not native._impl.validate_route([], [candidate], 1, [], 0.15, 0.2, 0.5).valid


def _validate(pathfinder, route):
    start = Pad(2, 2, 0.2, 0.2, 1, "candidate")
    end = Pad(8, 8, 0.2, 0.2, 1, "candidate")
    return pathfinder._validate_route_clearance(route, start, end, 1)


@pytest.mark.parametrize(
    "span,blocked",
    [
        ((Layer.F_CU, Layer.B_CU), True),
        ((Layer.B_CU, Layer.F_CU), True),
        ((Layer.F_CU, Layer.IN1_CU), False),
    ],
)
def test_omitted_layer_trace_remains_an_obstacle_to_physical_via(span, blocked):
    grid, native, pathfinder = _context([Layer.F_CU, Layer.B_CU])
    stored = Segment(3, 4, 5, 4, 0.2, Layer.IN2_CU, 2)
    grid.routes.append(Route(2, "stored", segments=[stored]))
    pathfinder._sync_stored_routes(grid)
    assert _native_trace_valid(native, 0) and _native_trace_valid(native, 1)
    via = Via(4, 4, 0.3, 0.6, span, 1)
    assert (_validate(pathfinder, Route(1, "candidate", vias=[via])) is not None) is blocked
    # A valid spaced candidate remains allowed; omitted copper is not a blanket ban.
    via.y = 5
    assert _validate(pathfinder, Route(1, "candidate", vias=[via])) is None
    grid.routes.clear()
    native.invalidate_stored_routes()
    via.y = 4
    assert _validate(pathfinder, Route(1, "candidate", vias=[via])) is None


def test_selected_trace_maps_to_its_dense_index_and_rebuilds_after_invalidation():
    grid, native, pathfinder = _context([Layer.IN2_CU, Layer.B_CU])
    grid.routes.append(Route(2, "stored", segments=[Segment(3, 4, 5, 4, 0.2, Layer.B_CU, 2)]))
    pathfinder._sync_stored_routes(grid)
    assert _native_trace_valid(native, 0)
    assert not _native_trace_valid(native, 1)
    grid.routes[:] = [Route(2, "stored", segments=[Segment(3, 4, 5, 4, 0.2, Layer.IN2_CU, 2)])]
    native.invalidate_stored_routes()
    pathfinder._sync_stored_routes(grid)
    assert not _native_trace_valid(native, 0)
    assert _native_trace_valid(native, 1)


@pytest.mark.parametrize("reverse", [False, True])
def test_reordered_stack_keeps_unrelated_plane_outside_blind_via(reverse):
    active = [Layer.F_CU, Layer.B_CU, Layer.IN1_CU]
    grid, native, pathfinder = _context(active)
    span = (Layer.IN1_CU, Layer.F_CU) if reverse else (Layer.F_CU, Layer.IN1_CU)
    grid.routes.append(Route(2, "stored", vias=[Via(4, 4, 0.3, 0.6, span, 2)]))
    pathfinder._sync_stored_routes(grid)
    assert not _native_trace_valid(native, 0)
    assert _native_trace_valid(native, 1)
    assert not _native_trace_valid(native, 2)

    # Candidate-side projection must likewise avoid treating B.Cu as spanned.
    grid.routes[:] = [Route(2, "stored", segments=[Segment(3, 4, 5, 4, 0.2, Layer.B_CU, 2)])]
    native.invalidate_stored_routes()
    assert _validate(pathfinder, Route(1, "candidate", vias=[Via(4, 4, 0.3, 0.6, span, 1)])) is None
    grid.routes[:] = [Route(2, "stored", segments=[Segment(3, 4, 5, 4, 0.2, Layer.IN1_CU, 2)])]
    native.invalidate_stored_routes()
    assert (
        _validate(pathfinder, Route(1, "candidate", vias=[Via(4, 4, 0.3, 0.6, span, 1)]))
        is not None
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_full_stack_keeps_native_span_arguments_and_incremental_counts(reverse):
    grid, native, pathfinder = _context([Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU, Layer.B_CU])
    impl = native._impl
    calls = []

    class RecordingGrid:
        def __getattr__(self, name):
            return getattr(impl, name)

        def add_stored_via(self, *args):
            calls.append(args)
            return impl.add_stored_via(*args)

    native._impl = RecordingGrid()
    span = (Layer.B_CU, Layer.F_CU) if reverse else (Layer.F_CU, Layer.B_CU)
    grid.routes.append(Route(2, "stored", vias=[Via(4, 4, 0.3, 0.6, span, 2)]))
    pathfinder._sync_stored_routes(grid)
    pathfinder._sync_stored_routes(grid)
    assert len(calls) == 1 and native._synced_route_count == 1
    assert calls[0] == (
        4,
        4,
        0.3,
        0.6,
        2,
        grid.world_to_grid(4, 4),
        *((3, 0) if reverse else (0, 3)),
    )
    assert all(not _native_trace_valid(native, i) for i in range(4))
