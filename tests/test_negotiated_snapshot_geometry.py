"""Best-state rollback restores geometry queried by post-route optimization."""

import copy

import pytest
from shapely.geometry import LineString

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import make_collision_checker
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("force_python", [True, False])
@pytest.mark.parametrize("deferred", [False, True])
def test_forced_best_restore_updates_obstacles_and_rejects_optimizer_shortcut(
    force_python, deferred
):
    rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15)
    router = Autorouter(width=15, height=15, rules=rules, force_python=force_python)
    if not force_python and router.grid._cpp_grid is None:
        pytest.skip("C++ backend unavailable")
    layer = Layer.F_CU
    kept = Route(net=6, net_name="B", segments=[Segment(9.3, 8.3, 9.3, 3.4, 0.2, layer, net=6)])
    discarded = Route(net=7, net_name="C", segments=[Segment(8, 11, 8.1, 11, 0.2, layer, net=7)])
    restored = Route(net=7, net_name="C", segments=[Segment(8, 7.5, 8.1, 7.5, 0.2, layer, net=7)])
    for route in (kept, discarded):
        router.routes.append(route)
        router._mark_route(route)
        router.grid.mark_route_usage(route)
    independent = Route(
        net=9, net_name="fixed", segments=[Segment(2, 12, 3, 12, 0.2, layer, net=9)]
    )
    router.grid.mark_route(independent)
    # This proposed shortcut is clear of the discarded iteration's copper,
    # but only 0.08284mm from the restored foreign net (authored floor 0.15).
    checker = make_collision_checker(router.grid, ignore_overflow=True)
    args = (9.3, 8.3, 4.4, 3.4, layer, 0.2, 6)
    assert checker.path_is_clear(*args)
    gap = LineString([(9.3, 8.3), (4.4, 3.4)]).distance(LineString([(8, 7.5), (8.1, 7.5)])) - 0.2
    assert gap == pytest.approx(0.082842712474619, abs=1e-9)
    snapshot = copy.deepcopy([kept, restored])
    if deferred:
        router.grid.begin_cpp_unmark_deferral()
    router._restore_negotiated_route_snapshot(snapshot)
    if deferred:
        router._flush_corridor_reservation({r.net: [r] for r in snapshot})
    assert {id(r) for r in router.grid.routes} == {id(r) for r in router.routes} | {id(independent)}
    assert not checker.path_is_clear(*args)
    for x, y, expected_net, usage in ((8, 11, 0, 0), (8, 7.5, 7, 1)):
        gx, gy = router.grid.world_to_grid(x, y)
        cell = router.grid.grid[0][gy][gx]
        assert cell.net == expected_net
        assert cell.usage_count == usage
        if not force_python:
            assert router.grid._cpp_grid.is_blocked_for_net(gx, gy, 0, 6) is bool(expected_net)
    if router.grid._rtree_available:
        indexed = [s for items in router.grid._seg_rtree_items.values() for s in items.values()]
        assert {id(s) for s in indexed} == {
            id(s) for r in [*snapshot, independent] for s in r.segments
        }
    # Repeating restoration must not duplicate routes or congestion usage.
    router._restore_negotiated_route_snapshot(snapshot)
    assert len(router.grid.routes) == 3
    gx, gy = router.grid.world_to_grid(8, 7.5)
    assert router.grid.grid[0][gy][gx].usage_count == 1
