"""Authored minima survive revalidation, foreign copper and cache reuse."""

import pytest

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.cache import CacheKey, SubProblemSignature
from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def fixture():
    rules = DesignRules(trace_clearance=0.1, via_clearance=0.1, grid_resolution=0.05)
    grid = RoutingGrid(10, 10, rules)
    via = Via(5, 5, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 1)
    segment = Segment(3, 5.75, 7, 5.75, 0.2, Layer.F_CU, 2)
    return rules, grid, via, segment


def test_negotiated_memo_and_bbox_follow_both_insertion_orders():
    rules, grid, via, segment = fixture()
    router = NegotiatedRouter(grid, Router(grid, rules), rules, {})
    routes = {1: [Route(1, "via", vias=[via])], 2: [Route(2, "trace", segments=[segment])]}
    for method, expected in (
        (router.find_nets_with_segment_via_violations, [2]),
        (router.find_nets_with_via_segment_violations, [1]),
    ):
        rules.net_clearance_floors = {}
        assert method(routes, 0.1, cache_key="same-copper") == []
        rules.net_clearance_floors = {1: 0.4}
        assert method(routes, 0.1, cache_key="same-copper") == expected
        rules.net_clearance_floors = {1: 0.3}
        assert method(routes, 0.1, cache_key="same-copper") == []


@pytest.mark.parametrize(
    "backend",
    [
        "python",
        pytest.param(
            "cpp", marks=pytest.mark.skipif(not is_cpp_available(), reason="native build required")
        ),
    ],
)
@pytest.mark.parametrize("floor,valid", [(0.3, True), (0.4, False)])
def test_foreign_via_context_retains_electrical_floor(backend, floor, valid):
    rules, grid, via, segment = fixture()
    rules.net_clearance_floors = {1: floor}
    route = Route(2, "trace", segments=[segment])
    if backend == "python":
        router = Router(grid, rules)
        router.set_segment_foreign_context(foreign_vias=[via])
        assert router._validate_route_clearance(route, exclude_net=2) is valid
    else:
        native = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
        native.set_segment_foreign_context(foreign_vias=[via])
        pads = [Pad(x, segment.y1, 0.2, 0.2, 2, "trace") for x in (segment.x1, segment.x2)]
        assert (native._validate_route_clearance(route, *pads, 1) is None) is valid


@pytest.mark.parametrize("kind", ["board", "subproblem"])
def test_route_cache_keys_include_floor_values_without_order_dependence(kind):
    rules = DesignRules()
    pads = [Pad(2, 2, 0.2, 0.2, 1, "N"), Pad(4, 4, 0.2, 0.2, 1, "N")]

    def key():
        if kind == "board":
            return CacheKey.compute("same board", rules, 0.05).rules_hash
        return SubProblemSignature.compute(pads, rules).rules_hash

    original = key()
    rules.net_clearance_floors = {1: 0.2, 2: 0.3}
    constrained = key()
    assert constrained != original
    rules.net_clearance_floors = {2: 0.3, 1: 0.2}
    assert key() == constrained
    rules.net_clearance_floors[1] = 0.4
    assert key() != constrained
    rules.net_clearance_floors = {}
    assert key() == original
