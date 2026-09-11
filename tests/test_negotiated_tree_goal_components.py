"""A Steiner branch must reach its new terminal, not return to its own tree."""

from unittest.mock import patch

import pytest
from shapely.geometry import LineString, Point

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


@pytest.fixture(params=["python", "cpp"], autouse=True)
def routing_backend(request, monkeypatch):
    if request.param == "cpp":
        import sys

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available

        if not is_cpp_available():
            pytest.skip("C++ backend unavailable")
        monkeypatch.setattr(
            sys.modules[__name__],
            "Router",
            lambda grid, rules: CppPathfinder(CppGrid.from_routing_grid(grid), rules),
        )

    return request.param


def _run(edges, *, virtual=False, points=None, fail_edge=None):
    rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.2)
    grid = RoutingGrid(12, 12, rules)
    pads = [
        Pad(
            x=x,
            y=y,
            width=0.5,
            height=0.5,
            net=1,
            net_name="N",
            layer=Layer.F_CU,
            ref=f"P{i}",
            pin="1",
        )
        for i, (x, y) in enumerate(points or [(2, 2), (7, 2), (2, 7)])
    ]
    if virtual:
        pads[0].width = pads[0].height = 0
        pads[0].ref = pads[0].pin = ""
        pads[0].steiner_point = True
    for pad in pads:
        if not pad.steiner_point:
            grid.add_pad(pad)
    grid.add_pad(
        Pad(x=2, y=4.5, width=1, height=1, net=2, net_name="X", layer=Layer.F_CU, ref="X", pin="1")
    )
    grid.add_pad(
        Pad(
            x=4.5,
            y=4.5,
            width=1,
            height=1,
            net=2,
            net_name="X",
            layer=Layer.F_CU,
            ref="X2",
            pin="1",
        )
    )
    pathfinder = Router(grid, rules)
    original = pathfinder.route
    calls = []
    failures = []

    def route(start, end, **kwargs):
        cells = kwargs.get("extra_goal_cells")
        calls.append((start, end, set(cells) if cells else None))
        if fail_edge is not None and len(calls) - 1 == fail_edge:
            return None
        return original(start, end, **kwargs)

    pathfinder.route = route
    negotiated = NegotiatedRouter(grid, pathfinder, rules, {})
    with patch("kicad_tools.router.algorithms.steiner.build_rsmt", return_value=(pads, edges)):
        routes = negotiated.route_net_negotiated(
            pads, 1.0, grid.mark_route, failure_callback=lambda a, b: failures.append((a, b))
        )
    return pads, routes, calls, failures


def _touches(pad, routes):
    return any(
        s.layer == pad.layer
        and LineString([(s.x1, s.y1), (s.x2, s.y2)]).distance(Point(pad.x, pad.y))
        <= s.width / 2 + min(pad.width, pad.height) / 2 + 1e-8
        for route in routes
        for s in route.segments
    )


@pytest.mark.parametrize("virtual", [False, True])
def test_connected_source_branch_reaches_new_terminal(virtual):
    pads, routes, _, _ = _run([(0, 1), (0, 2)], virtual=virtual)
    assert len(routes) == 2
    # Old code returned a nonempty detour ending at (2.3, 2), omitting P2.
    assert _touches(pads[2], routes)


def test_unconnected_source_keeps_shortcut_to_connected_target(routing_backend):
    pads, routes, calls, _ = _run([(0, 1), (2, 1)])
    assert _touches(pads[2], routes)
    assert calls[1][2]
    # The branch can join the tree before reaching the distant P1 terminal.
    # The native fast path may reach the exact target; its Python fallback
    # consumes the same optional goal set.
    if routing_backend == "python":
        assert not _touches(pads[1], [routes[1]])


def test_disjoint_forest_does_not_offer_unrelated_tree_as_goal():
    pads, routes, calls, _ = _run([(0, 1), (2, 3), (1, 2)], points=[(2, 2), (7, 2), (2, 7), (7, 7)])
    assert calls[1][2] is None
    assert all(_touches(pad, routes) for pad in pads)
    assert calls[2][2]
    # Every terminal touched is insufficient: require one physical copper
    # island, with inter-layer connections only through actual vias.
    shapes = [
        ({s.layer.value}, LineString([(s.x1, s.y1), (s.x2, s.y2)]).buffer(s.width / 2))
        for route in routes
        for s in route.segments
    ]
    shapes.extend(
        (
            set(
                range(
                    min(layer.value for layer in v.layers),
                    max(layer.value for layer in v.layers) + 1,
                )
            ),
            Point(v.x, v.y).buffer(v.diameter / 2),
        )
        for route in routes
        for v in route.vias
    )
    reached = {0}
    pending = [0]
    while pending:
        i = pending.pop()
        for j, (layers, copper) in enumerate(shapes):
            if j not in reached and shapes[i][0] & layers and shapes[i][1].intersects(copper):
                reached.add(j)
                pending.append(j)
    assert len(reached) == len(shapes)
    # Target component is the top branch; bottom branch is not a legal shortcut.
    assert all(gy >= 65 for _, gy, _ in calls[2][2])


def test_failed_swapped_edge_preserves_callback_and_component_state():
    pads, _, calls, failures = _run(
        [(0, 1), (0, 2), (2, 3)],
        points=[(2, 2), (7, 2), (2, 7), (7, 7)],
        fail_edge=1,
    )
    assert failures == [(pads[0], pads[2])]
    assert calls[2][2] is None


@pytest.mark.parametrize(
    "span", [(Layer.F_CU, Layer.IN1_CU), (Layer.IN1_CU, Layer.IN2_CU), (Layer.F_CU, Layer.B_CU)]
)
def test_tree_via_goals_respect_physical_layer_span(span):
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.primitives import Route, Via

    rules = DesignRules(grid_resolution=0.1)
    grid = RoutingGrid(12, 12, rules, layer_stack=LayerStack.four_layer_all_signal())
    negotiated = NegotiatedRouter(grid, Router(grid, rules), rules, {})
    cells = set()
    negotiated._collect_route_cells(
        Route(
            net=1, net_name="N", vias=[Via(x=2, y=2, diameter=0.6, drill=0.3, layers=span, net=1)]
        ),
        cells,
    )
    first, last = sorted(grid.layer_to_index(layer.value) for layer in span)
    assert {layer for _, _, layer in cells} == set(range(first, last + 1))
