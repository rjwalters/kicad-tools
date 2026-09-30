"""Keep-outs landing on a routed-copper halo must stay hard (issue #5410).

Search-time halo refinement re-measures a blocked cell against the routed
copper that owns it.  That is only sound when the route halo is the *sole*
reason the cell is blocked.  A registry-less blocker -- ``add_keepout``,
``add_obstacle``, ``mark_region_bound`` -- that lands on a cell a route halo
already owns leaves the cell attributed to that route (marking never changes
an already-blocked cell's owner), so before this fix:

* both backends refined the keep-out away and admitted a trace or via there
  (the legal-vs-route candidate below);
* ripping the route up freed the keep-out cell outright; and
* on the native backend a keep-out or obstacle added after the C++ mirror was
  built was never mirrored at all, so native A* routed straight through it.

The fixture is the same measured separation as
``tests/test_dynamic_route_halo_clearance.py``: a 0.6/0.3 mm via at grid
(60, 60) and a candidate at (55, 56) that is physically legal against it,
so every "blocked" assertion below is load-bearing -- the paired control
without the keep-out refines the same cell open.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import (
    CppGrid,
    CppPathfinder,
    is_cpp_available,
    replay_route_halo_marks,
    sync_stored_routes,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Layer, Obstacle, Route, Via
from kicad_tools.router.rules import DesignRules

CANDIDATE = (55, 56)
LAYER = 2  # an inner layer: the via halo covers every layer
BLOCKERS = ["none", "keepout", "obstacle", "region_bound"]

native = pytest.mark.skipif(not is_cpp_available(), reason="native router required")


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.127,
    )


def _grid() -> RoutingGrid:
    return RoutingGrid(
        width=20, height=20, rules=_rules(), layer_stack=LayerStack.four_layer_all_signal()
    )


def _route(grid: RoutingGrid) -> Route:
    x, y = grid.grid_to_world(60, 60)
    route = Route(net=2, net_name="N2")
    route.vias.append(Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2"))
    return route


def _apply_blocker(grid: RoutingGrid, kind: str) -> None:
    """Drop a registry-less blocker on the candidate AFTER the route.

    Keep-out and obstacle cover the 2x2 cells ``(54..55, 56..57)``: the
    candidate plus both corner cells the ``(-1, +1)`` diagonal move out of it
    tests, so every predicate below has a keep-out cell in its footprint.
    """
    x, y = CANDIDATE
    inner = Layer(grid.index_to_layer(LAYER))
    if kind == "keepout":
        x1, y1 = grid.grid_to_world(x - 1, y)
        x2, y2 = grid.grid_to_world(x, y + 1)
        grid.add_keepout(x1, y1, x2, y2, [inner])
    elif kind == "obstacle":
        # A 1.2-cell body centred on the 2x2 block, with the clearance
        # cancelling add_obstacle's own trace half-width + trace clearance
        # pad, so its stamp is exactly the 2x2 block.
        cx, cy = grid.grid_to_world(x - 1, y)
        cx, cy = cx + grid.resolution / 2, cy + grid.resolution / 2
        size = 1.2 * grid.resolution
        pad = grid.rules.trace_clearance + grid.rules.trace_width / 2
        grid.add_obstacle(Obstacle(cx, cy, size, size, inner, clearance=-pad))
    elif kind == "region_bound":
        # Confine routing to x >= candidate + 1: the candidate column and
        # everything west of it are now outside the region.
        bx, by = grid.grid_to_world(CANDIDATE[0] + 1, 0)
        ex, ey = grid.grid_to_world(grid.cols - 1, grid.rows - 1)
        grid.mark_region_bound(bx, by, ex, ey)


def _python(kind: str) -> tuple[RoutingGrid, Router]:
    grid = _grid()
    grid.mark_route(_route(grid))
    _apply_blocker(grid, kind)
    router = Router(grid, grid.rules)
    router.set_net_name_to_id({"N1": 1, "N2": 2})
    return grid, router


def _native_replayed(kind: str) -> tuple[CppGrid, CppPathfinder]:
    """The coupled search's path: bulk copy + stored geometry + mark replay."""
    grid = _grid()
    grid.mark_route(_route(grid))
    _apply_blocker(grid, kind)
    cpp = CppGrid.from_routing_grid(grid)
    sync_stored_routes(cpp, grid)
    replay_route_halo_marks(cpp, grid)
    pathfinder = CppPathfinder(cpp, grid.rules, diagonal_routing=True)
    pathfinder.set_routable_layers(cpp.get_routable_indices())
    return cpp, pathfinder


def _native_incremental(kind: str) -> tuple[RoutingGrid, CppGrid, CppPathfinder]:
    """The per-net search's path: mirror built first, then incremental marks."""
    grid = _grid()
    cpp = CppGrid.from_routing_grid(grid)  # attaches grid._cpp_grid
    route = _route(grid)
    grid.mark_route(route)
    gx, gy = grid.world_to_grid(route.vias[0].x, route.vias[0].y)
    cpp.mark_via(gx, gy, 2, 6)
    sync_stored_routes(cpp, grid)
    _apply_blocker(grid, kind)
    pathfinder = CppPathfinder(cpp, grid.rules, diagonal_routing=True)
    pathfinder.set_routable_layers(cpp.get_routable_indices())
    return grid, cpp, pathfinder


def _python_blocked(router: Router, sharing: bool) -> tuple[bool, bool, bool]:
    x, y = CANDIDATE
    return (
        router._is_trace_blocked(x, y, LAYER, 1, sharing, radius=2),
        router._is_diagonal_corner_blocked(x, y, -1, 1, LAYER, 1, sharing),
        router._is_via_blocked(x, y, LAYER, 1, sharing, radius=4),
    )


def _native_blocked(pathfinder: CppPathfinder, sharing: bool) -> tuple[bool, bool, bool]:
    x, y = CANDIDATE
    impl = pathfinder._impl
    return (
        impl.is_trace_blocked(x, y, LAYER, 1, sharing, 2),
        impl.is_diagonal_blocked(x, y, -1, 1, LAYER, 1, sharing),
        impl.is_via_blocked(x, y, 1, sharing, 4),
    )


# ---------------------------------------------------------------------------
# Python backend
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("kind", BLOCKERS)
def test_python_blocker_on_route_halo_is_not_refined(kind, sharing):
    grid, router = _python(kind)
    x, y = CANDIDATE
    # The candidate cell stays attributed to the route that blocked it first.
    assert grid._blocked[LAYER, y, x]
    assert int(grid._net[LAYER, y, x]) == 2
    if kind == "none":
        # Control: the legal candidate is refined open inside the halo.
        assert grid._route_halo.cell_known(x, y, LAYER)
        assert _python_blocked(router, sharing) == (False, False, False)
    else:
        assert grid.raster_only_blocked_cell(x, y, LAYER)
        assert grid._static_blocked[LAYER, y, x]
        assert not grid._route_halo.cell_known(x, y, LAYER)
        window = grid._route_halo.window_known(x, y, x + 1, y + 1)
        assert window is not None and not window[LAYER, 0, 0]
        assert _python_blocked(router, sharing) == (True, True, True)


@pytest.mark.parametrize("kind", ["keepout", "obstacle", "region_bound"])
def test_python_ripup_keeps_a_blocker_that_landed_on_the_route_halo(kind):
    grid, _ = _python(kind)
    route = grid.routes[0]
    x, y = CANDIDATE
    grid.unmark_route(route)
    assert grid._blocked[LAYER, y, x], "rip-up erased the keep-out"
    assert int(grid._net[LAYER, y, x]) != 2
    # Cells the blocker never covered are released as before.
    assert not grid._blocked[LAYER, 60, 62]


def test_python_blocker_before_the_first_route_is_captured_by_the_snapshot():
    grid = _grid()
    assert grid._static_blocked is None
    _apply_blocker(grid, "keepout")
    grid.mark_route(_route(grid))
    x, y = CANDIDATE
    assert grid._static_blocked[LAYER, y, x]
    assert not grid._route_halo.cell_known(x, y, LAYER)


# ---------------------------------------------------------------------------
# Native backend
# ---------------------------------------------------------------------------


@native
@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("kind", BLOCKERS)
def test_native_replayed_blocker_on_route_halo_is_not_refined(kind, sharing):
    cpp, pathfinder = _native_replayed(kind)
    x, y = CANDIDATE
    known = cpp._impl.route_cell_has_geometry(x, y, LAYER)
    if kind == "none":
        assert known
        assert _native_blocked(pathfinder, sharing) == (False, False, False)
    else:
        assert not known
        assert cpp._impl.at(x, y, LAYER).static_blocked
        assert _native_blocked(pathfinder, sharing) == (True, True, True)


@native
@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("kind", BLOCKERS)
def test_native_incremental_blocker_on_route_halo_is_not_refined(kind, sharing):
    grid, cpp, pathfinder = _native_incremental(kind)
    x, y = CANDIDATE
    cell = cpp._impl.at(x, y, LAYER)
    assert cell.blocked
    assert cell.net == int(grid._net[LAYER, y, x]) == 2
    if kind == "none":
        assert cpp._impl.route_cell_has_geometry(x, y, LAYER)
        assert _native_blocked(pathfinder, sharing) == (False, False, False)
    else:
        assert cell.static_blocked
        assert not cpp._impl.route_cell_has_geometry(x, y, LAYER)
        assert _native_blocked(pathfinder, sharing) == (True, True, True)


@native
@pytest.mark.parametrize("kind", ["keepout", "obstacle", "region_bound"])
def test_native_ripup_keeps_a_blocker_that_landed_on_the_route_halo(kind):
    grid, cpp, _ = _native_incremental(kind)
    x, y = CANDIDATE
    gx, gy = grid.world_to_grid(*grid.grid_to_world(60, 60))
    cpp._impl.unmark_via(gx, gy, 2, 6)
    grid.unmark_route(grid.routes[0])
    cell = cpp._impl.at(x, y, LAYER)
    assert cell.blocked, "native rip-up erased the keep-out"
    assert cell.net != 2
    assert not cpp._impl.at(62, 60, LAYER).blocked
    # Both backends agree on the post-rip-up cell.
    assert bool(grid._blocked[LAYER, y, x]) == cell.blocked
    assert int(grid._net[LAYER, y, x]) == cell.net


@native
@pytest.mark.parametrize("kind", ["keepout", "obstacle"])
def test_blocker_added_after_the_native_mirror_reaches_it(kind):
    """A keep-out on free board must block native search, not just Python."""
    grid = _grid()
    cpp = CppGrid.from_routing_grid(grid)
    x, y = 20, 20
    before = cpp._impl.at(x, y, LAYER)
    assert not before.blocked
    cx, cy = grid.grid_to_world(x, y)
    inner = Layer(grid.index_to_layer(LAYER))
    if kind == "keepout":
        grid.add_keepout(cx, cy, cx, cy, [inner])
    else:
        pad = grid.rules.trace_clearance + grid.rules.trace_width / 2
        grid.add_obstacle(Obstacle(cx, cy, 0.0, 0.0, inner, clearance=-pad))
    cell = cpp._impl.at(x, y, LAYER)
    assert cell.blocked and cell.static_blocked
    assert cell.net == int(grid._net[LAYER, y, x]) == 0
    assert cell.is_obstacle == bool(grid._is_obstacle[LAYER, y, x])


@native
def test_native_autorouter_does_not_route_through_a_late_obstacle():
    """End to end: ``Autorouter.add_obstacle`` after the native router exists."""
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.cpp_backend import CppPathfinder as NativePathfinder

    router = Autorouter(width=20, height=20, rules=DesignRules(grid_resolution=0.25))
    assert isinstance(router.router, NativePathfinder)
    pad = {"number": "1", "width": 0.6, "height": 0.6, "net": 1, "net_name": "A"}
    router.add_component("R1", [{**pad, "x": 3, "y": 10}])
    router.add_component("R2", [{**pad, "x": 17, "y": 10}])
    # A full-height F.Cu wall between the two SMD pads.
    router.add_obstacle(10, 10, 2, 19, Layer.F_CU)
    routes = router.route_all(suppress_no_timeout_warning=True)
    assert routes
    crossing = [
        seg
        for route in routes
        for seg in route.segments
        if seg.layer == Layer.F_CU and min(seg.x1, seg.x2) <= 10 <= max(seg.x1, seg.x2)
    ]
    assert not crossing, f"native route crossed the F.Cu obstacle: {crossing}"
