"""Committed mesh obstacles retain physical widths, net identity and vias."""

import pytest

from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.mesh.pathfinder import MeshPathfinder
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("kind", ["trace", "via"])
@pytest.mark.parametrize("floor,valid", [(0.3, True), (0.4, False)])
def test_mesh_via_checks_authored_committed_copper(strict_net, kind, floor, valid):
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.1,
        via_diameter=0.2,
        via_clearance=0.1,
        net_clearance_floors={strict_net: floor, 3: 4.0},
    )
    pf = MeshPathfinder(
        [(0, 0), (10, 0), (10, 10), (0, 10)],
        [],
        rules,
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    route = Route(2, "foreign")
    if kind == "trace":
        route.segments.append(Segment(3, 5, 7, 5, 0.2, Layer.IN1_CU, 2))
    else:
        route.vias.append(Via(5, 5, 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 2))
    committed = pf._route_obstacles_by_layer(route)
    assert pf._via_allowed_at((5, 5.55), 1, 0.2, (0, 3), committed) is valid


@pytest.mark.parametrize("stored_width", [0.2, 0.6])
@pytest.mark.parametrize("query_half", [0.1, 0.3])
def test_mesh_copper_queries_retain_actual_widths_and_late_rule_changes(stored_width, query_half):
    from kicad_tools.router.mesh.obstacles import ObstacleModel

    rules = DesignRules(trace_width=0.2, trace_clearance=0.1)
    outline = [(0, 0), (10, 0), (10, 10), (0, 10)]
    pf = MeshPathfinder(outline, [], rules)
    route = Route(2, "foreign", segments=[Segment(3, 5, 7, 5, stored_width, Layer.F_CU, 2)])
    polygons = pf._route_obstacles_by_layer(route)[0]
    original = repr(route)
    y = 5 + stored_width / 2 + query_half + 0.35
    rules.net_clearance_floors = {2: 0.4, 3: 4.0}
    assert not ObstacleModel(outline, [], pf._query_copper(polygons, 1, query_half)).is_clear(
        (4, y), (6, y)
    )
    rules.net_clearance_floors = {2: 0.3, 3: 4.0}
    assert ObstacleModel(outline, [], pf._query_copper(polygons, 1, query_half)).is_clear(
        (4, y), (6, y)
    )
    assert repr(route) == original
