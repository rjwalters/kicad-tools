"""Optimized copper must respect serialized barrels, not search spans."""

from types import SimpleNamespace

import pytest

from kicad_tools.router import DesignRules, Layer, Route, RoutingGrid, Segment, TraceOptimizer
from kicad_tools.router.geometry import point_to_segment_distance
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.optimizer import make_collision_checker
from kicad_tools.router.optimizer.trace import optimize_routes_grid_synced
from kicad_tools.router.primitives import Via


@pytest.mark.parametrize("is_micro", [False, True])
@pytest.mark.parametrize("indexed_vias", [False, True])
def test_staircase_transaction_respects_physical_barrel(is_micro, indexed_vias):
    grid = RoutingGrid(15, 15, DesignRules(), layer_stack=LayerStack.four_layer_all_signal())
    segments = []
    point = (10.0, 10.0)
    for i in range(10):
        end = (point[0] - 1, point[1]) if i % 2 == 0 else (point[0] - 0.5, point[1] - 0.5)
        segments.append(Segment(*point, *end, 0.15, Layer.B_CU, net=1))
        point = end
    route = Route(net=1, net_name="SIGNAL", segments=segments)
    via = Via(8.5, 8.5, 0.3, 0.6, (Layer.F_CU, Layer.IN1_CU), net=2, is_micro=is_micro)
    obstacle = Route(net=2, net_name="OTHER", vias=[via])
    grid.mark_route(route)
    grid.mark_route(obstacle)
    if not indexed_vias:
        grid._via_rtree = None
        grid._via_rtree_items = {}
    checker = make_collision_checker(grid)

    # The original staircase is physically clear even of a through barrel.
    def gap(s):
        return point_to_segment_distance(via.x, via.y, s.x1, s.y1, s.x2, s.y2) - 0.375

    assert min(map(gap, segments)) > grid.rules.via_clearance
    router = SimpleNamespace(grid=grid, routes=[route, obstacle])
    optimize_routes_grid_synced(router, TraceOptimizer(collision_checker=checker), skip_nets={2})
    result = router.routes[0]
    assert router.routes[1] is obstacle
    assert via.layers == (Layer.F_CU, Layer.IN1_CU)
    assert result.segments[0].start == segments[0].start
    assert result.segments[-1].end == segments[-1].end
    if is_micro:
        # A typed microvia ending at In1 really permits the B.Cu shortcut.
        assert len(result.segments) == 2
        assert min(map(gap, result.segments)) < 0
    else:
        assert min(map(gap, result.segments)) >= grid.rules.via_clearance
    assert result in grid.routes
