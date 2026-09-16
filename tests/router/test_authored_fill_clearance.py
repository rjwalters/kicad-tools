"""Preserved fills retain authored floors without becoming reusable copper."""

import pytest
from shapely.geometry import box

from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Segment, Via
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("backend", ["grid", "lattice"])
@pytest.mark.parametrize("kind", ["trace", "via"])
@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("floor,valid", [(0.3, True), (0.4, False)])
def test_fill_checks_both_authored_floors(kind, strict_net, floor, valid, backend):
    rules = DesignRules(
        trace_clearance=0.1, via_clearance=0.1, net_clearance_floors={strict_net: floor, 3: 4.0}
    )
    grid = RoutingGrid(10, 10, rules)
    grid.fixed_fills = FixedFillObstacles((FixedFill("foreign", 2, 0, 0.1, box(4, 4, 6, 6)),))
    if backend == "lattice":
        from kicad_tools.router.lattice.obstacles import CommittedCopper

        model = CommittedCopper(
            2,
            trace_half=0.1,
            clearance=0.1,
            via_radius=0.1,
            via_via_gap=0.3,
            same_net_via_gap=0.2,
            net_clearance_floors=rules.net_clearance_floors,
        )
        model.fixed_fills = grid.fixed_fills
        if kind == "trace":
            assert model.seg_clear((3, 6.45), (7, 6.45), 0, 1) is valid
            assert model.node_clear((5, 6.45), 0, 1) is valid
        else:
            assert model.via_clear((5, 6.45), 1) is valid
        return
    if kind == "trace":
        candidate = Segment(3, 6.45, 7, 6.45, 0.2, Layer.F_CU, 1)
        actual = grid.validate_segment_clearance(candidate, 1)[0]
    else:
        candidate = Via(5, 6.45, 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 1)
        actual = grid.validate_via_clearance(candidate, 1)[0]
    assert actual is valid


def test_same_source_net_does_not_make_placement_invalid_fill_reusable():
    fills = FixedFillObstacles((FixedFill("same", 1, 0, 0.1, box(4, 4, 6, 6)),))
    assert not fills.segment_clear(
        (3, 5), (7, 5), 0, 0.1, 0.1, net=1, net_clearance_floors={1: 0.4}
    )
    assert not fills.segment_clear(
        (3, 6.45), (7, 6.45), 0, 0.1, 0.1, net=1, net_clearance_floors={1: 0.4}
    )


@pytest.mark.parametrize("kind", ["trace", "via"])
@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("floor,valid", [(0.3, True), (0.4, False)])
def test_native_fill_validation_retains_both_net_floors(kind, strict_net, floor, valid):
    from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available, router_cpp

    if not is_cpp_available():
        pytest.skip("native build required")
    rules = DesignRules(
        trace_clearance=0.1, via_clearance=0.1, net_clearance_floors={strict_net: floor, 3: 4.0}
    )
    grid = RoutingGrid(10, 10, rules)
    grid.fixed_fills = FixedFillObstacles((FixedFill("foreign", 2, 0, 0.1, box(4, 4, 6, 6)),))
    native = CppGrid.from_routing_grid(grid)
    if kind == "trace":
        candidate = router_cpp.Segment()
        candidate.x1, candidate.y1, candidate.x2, candidate.y2 = 3, 6.45, 7, 6.45
        candidate.width, candidate.layer, candidate.net = 0.2, 0, 1
        segments, vias = [candidate], []
    else:
        candidate = router_cpp.Via()
        candidate.x, candidate.y, candidate.drill, candidate.diameter = 5, 6.45, 0.1, 0.2
        candidate.layer_from, candidate.layer_to, candidate.net = 0, 1, 1
        segments, vias = [], [candidate]
    assert native._impl.validate_route(segments, vias, 1, [], 0.1, 0.1, 0.1).valid is valid


@pytest.mark.parametrize("method", ["route", "route_resumable"])
@pytest.mark.parametrize("strict_net", [1, 2])
def test_native_search_routes_outside_authored_fill_gap(method, strict_net):
    from shapely.geometry import LineString

    from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available

    if not is_cpp_available():
        pytest.skip("native build required")
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.1,
        via_clearance=0.1,
        grid_resolution=0.1,
        net_clearance_floors={strict_net: 0.4},
    )
    grid = RoutingGrid(10, 10, rules)
    polygon = box(4, 4, 6, 6)
    grid.fixed_fills = FixedFillObstacles((FixedFill("foreign", 2, 0, 0.1, polygon),))
    native = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(native, rules)
    pathfinder.set_routable_layers([0])
    result = getattr(pathfinder._impl, method)(2, 6.3, 0, 8, 6.3, 0, 1, max_search_iterations=10000)
    assert result.success and result.segments
    for segment in result.segments:
        distance = LineString([(segment.x1, segment.y1), (segment.x2, segment.y2)]).distance(
            polygon
        )
        assert distance - segment.width / 2 >= 0.4 - 1e-4
