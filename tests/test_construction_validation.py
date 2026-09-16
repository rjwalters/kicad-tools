import time
from dataclasses import replace

import pytest

from kicad_tools.router.construction_validation import (
    constructed_pair_geometry_issue,
    route_topology_issue,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def fixture():
    rules = DesignRules(grid_resolution=0.1)
    auto = Autorouter(width=10, height=10, rules=rules)
    finder = CoupledPathfinder(auto.grid, rules, target_spacing_cells=4, min_spacing_cells=2)
    routes = [
        Route(
            net=net,
            net_name=str(net),
            segments=[Segment(x1=2, y1=y, x2=5, y2=y, width=0.15, layer=Layer.F_CU, net=net)],
        )
        for net, y in ((1, 2), (2, 2.4))
    ]
    pads = tuple(
        Pad(x=x, y=y, width=0.2, height=0.2, layer=Layer.F_CU, net=net, net_name=str(net))
        for net, y in ((1, 2), (2, 2.4))
        for x in (2, 5)
    )
    return auto, finder, routes, pads


def check(auto, finder, routes, pads, deadline=None):
    return constructed_pair_geometry_issue(
        auto._diffpair,
        finder,
        *routes,
        pads,
        intra_pair_clearance=0.1,
        deadline=time.monotonic() + 5 if deadline is None else deadline,
    )


def test_clear_pair_and_expired_budget():
    auto, finder, routes, pads = fixture()
    assert check(auto, finder, routes, pads) is None
    assert check(auto, finder, routes, pads, deadline=0) == "deadline"
    assert not auto.routes and not auto.grid.routes


@pytest.mark.parametrize("kind", ["island", "duplicate", "cycle", "off_angle"])
def test_rejects_bad_copper_topology(kind):
    auto, finder, routes, pads = fixture()
    route = routes[0]
    base = route.segments[0]
    if kind == "island":
        route.segments.append(replace(base, x1=6, x2=7))
    elif kind == "duplicate":
        route.segments.append(replace(base))
    elif kind == "off_angle":
        route.segments[0] = replace(base, y2=2.2)
    else:
        route.segments.extend(
            replace(base, x1=a[0], y1=a[1], x2=b[0], y2=b[1])
            for a, b in [((3, 2), (3, 3)), ((3, 3), (4, 3)), ((4, 3), (4, 2))]
        )
    assert check(auto, finder, routes, pads) in {
        "disconnected",
        "self_intersection",
        "segment_geometry",
    }


def test_unknown_blocker_cannot_be_resolved_as_geometry():
    auto, finder, routes, pads = fixture()
    x, y = auto.grid.world_to_grid(3, 2)
    auto.grid.cell_at(0, y, x).blocked = True
    assert check(auto, finder, routes, pads) == "unknown_blocker"


def test_foreign_trace_and_uncommitted_partner_barrel_are_checked():
    auto, finder, routes, pads = fixture()
    foreign = Route(
        net=9, net_name="OTHER", segments=[replace(routes[0].segments[0], net=9, y1=1.8, y2=1.8)]
    )
    auto.routes.append(foreign)
    assert check(auto, finder, routes, pads) == "trace_clearance"
    auto.routes.clear()
    routes[1].vias.append(
        Via(x=3, y=2.4, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=2)
    )
    assert check(auto, finder, routes, pads) == "via_clearance"


def test_topology_requires_a_via_to_connect_different_layers():
    _, _, routes, pads = fixture()
    base = routes[0].segments[0]
    routes[0].segments = [replace(base, x2=3), replace(base, x1=3, layer=Layer.B_CU)]
    end = replace(pads[1], layer=Layer.B_CU)
    assert (
        route_topology_issue(routes[0], pads[0], end, deadline=time.monotonic() + 5)
        == "disconnected"
    )
    routes[0].vias.append(
        Via(x=3, y=2, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=1)
    )
    assert route_topology_issue(routes[0], pads[0], end, deadline=time.monotonic() + 5) is None


def test_via_keepout_on_another_layer_and_drill_spacing():
    auto, finder, routes, pads = fixture()
    auto.rules.via_diameter = 0.2
    auto.rules.via_drill = 0.1
    via = Via(x=3, y=2, diameter=0.2, drill=0.1, layers=(Layer.F_CU, Layer.B_CU), net=1)
    routes[0].vias.append(via)
    assert check(auto, finder, routes, pads) is None
    x, y = auto.grid.world_to_grid(3, 2)
    layer = auto.grid.layer_to_index(Layer.B_CU.value)
    auto.grid.cell_at(layer, y, x).blocked = True
    assert check(auto, finder, routes, pads) == "unknown_via_blocker"
    auto.grid.cell_at(layer, y, x).blocked = False
    auto.routes.append(Route(net=9, net_name="OTHER", vias=[replace(via, net=9, y=1.6)]))
    assert check(auto, finder, routes, pads) == "drill_clearance"


def test_reserved_future_trace_participates_without_occupancy_commit():
    auto, finder, routes, pads = fixture()
    reservation = Route(
        net=3,
        net_name="future",
        segments=[
            replace(
                routes[0].segments[0],
                net=3,
                x1=3,
                x2=3,
                y1=1,
                y2=3,
            )
        ],
    )
    assert (
        constructed_pair_geometry_issue(
            auto._diffpair,
            finder,
            *routes,
            pads,
            intra_pair_clearance=0.1,
            deadline=time.monotonic() + 5,
            reserved_routes=(reservation,),
        )
        == "trace_clearance"
    )
    assert not auto.routes and not auto.grid.routes


@pytest.mark.parametrize("kind", ["trace", "via"])
@pytest.mark.parametrize("soft", [False, True])
def test_reservation_only_cells_are_not_waived_by_exact_validation(kind, soft):
    auto, finder, routes, pads = fixture()
    grid = auto.grid
    x, y = grid.world_to_grid(3, 2)
    layer = 0
    if kind == "via":
        auto.rules.via_diameter = 0.2
        auto.rules.via_drill = 0.1
        routes[0].vias.append(
            Via(x=3, y=2, diameter=0.2, drill=0.1, layers=(Layer.F_CU, Layer.B_CU), net=1)
        )
        layer = grid.layer_to_index(Layer.B_CU.value)
    assert check(auto, finder, routes, pads) is None
    assert not grid.cell_at(layer, y, x).blocked
    grid.reserve_corridor_cells(layer, {(x, y)}, {99}, soft=soft)
    assert check(auto, finder, routes, pads) == (
        None if soft else "unknown_via_blocker" if kind == "via" else "unknown_blocker"
    )
    assert grid._reserved_for_nets[(layer, y, x)] == frozenset({99})
