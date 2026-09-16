"""Via reuse must preserve committed geometry and join each candidate layer."""

import copy

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.quantize import is_45_aligned
from kicad_tools.router.via_reuse import reuse_same_net_vias


def candidate():
    return Route(
        net=1,
        net_name="N1",
        segments=[
            Segment(0, 1, 1, 1, 0.2, Layer.F_CU, net=1),
            Segment(1, 1, 2, 1, 0.25, Layer.IN2_CU, net=1),
        ],
        vias=[Via(1, 1, 0.25, 0.6, (Layer.F_CU, Layer.IN2_CU), net=1)],
    )


@pytest.mark.parametrize("end_layer", [Layer.B_CU, Layer.IN1_CU])
def test_reuses_barrel_without_mutating_existing_copper(end_layer):
    existing = Route(
        net=1, net_name="N1", vias=[Via(1.15, 1.1, 0.25, 0.45, (Layer.F_CU, end_layer), net=1)]
    )
    before = copy.deepcopy(existing)
    route = candidate()
    original_segments = list(route.segments)
    reuse_same_net_vias(route, [existing], 0.25)
    assert existing == before
    assert not route.vias
    assert route.segments[:2] == original_segments
    for layer in (Layer.F_CU, Layer.IN2_CU):
        stubs = [s for s in route.segments[2:] if s.layer == layer]
        assert stubs[0].start == (1, 1)
        assert stubs[-1].end == (1.15, 1.1)
        assert all(is_45_aligned(s.x2 - s.x1, s.y2 - s.y1) for s in stubs)


@pytest.mark.parametrize(
    "net,layers,x,is_micro",
    [
        (2, (Layer.F_CU, Layer.B_CU), 1.1, False),
        (1, (Layer.F_CU, Layer.IN1_CU), 1.1, True),
        (1, (Layer.F_CU, Layer.B_CU), 5, False),
    ],
)
def test_ineligible_via_does_not_change_candidate(net, layers, x, is_micro):
    route = candidate()
    before = copy.deepcopy(route)
    existing = Route(
        net=net, net_name=f"N{net}", vias=[Via(x, 1, 0.25, 0.6, layers, net=net, is_micro=is_micro)]
    )
    reuse_same_net_vias(route, [existing], 0.25)
    assert route == before


@pytest.mark.parametrize("marker", ["is_micro", "in_pad"])
def test_fixed_escape_candidate_is_not_replaced(marker):
    route = candidate()
    setattr(route.vias[0], marker, True)
    before = copy.deepcopy(route)
    existing = Route(
        net=1, net_name="N1", vias=[Via(1.1, 1, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=1)]
    )
    reuse_same_net_vias(route, [existing], 0.25)
    assert route == before


def marked_context():
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.rules import DesignRules

    grid = RoutingGrid(5, 5, DesignRules(grid_resolution=0.05))
    route = candidate()
    route.segments[1].layer = Layer.B_CU
    route.vias[0].layers = (Layer.F_CU, Layer.B_CU)
    existing = Route(
        net=1, net_name="N1", vias=[Via(1.4, 1.3, 0.25, 0.45, (Layer.F_CU, Layer.B_CU), net=1)]
    )
    grid.mark_route(existing)
    return grid, route, existing


def test_marked_barrel_reuse_preserves_terminals_and_committed_copper():
    from kicad_tools.router.via_reuse import reuse_marked_vias

    grid, route, existing = marked_context()
    before = copy.deepcopy(existing)
    terminals = (route.segments[0].start, route.segments[1].end)
    reuse_marked_vias(route, grid, 0.25)
    assert not route.vias
    assert existing == before
    assert (route.segments[0].start, route.segments[1].end) == terminals
    for layer in (Layer.F_CU, Layer.B_CU):
        assert any(s.layer == layer and s.end == (1.4, 1.3) for s in route.segments)


def test_stale_route_list_cannot_authorize_unmarked_barrel():
    from kicad_tools.router.via_reuse import reuse_marked_vias

    grid, route, existing = marked_context()
    grid._unmark_via(existing.vias[0])
    # Keep both the stale route record and overlapping same-net trace occupancy.
    grid.mark_route(
        Route(net=1, net_name="N1", segments=[Segment(1.2, 1.3, 1.6, 1.3, 0.2, Layer.F_CU, net=1)])
    )
    assert existing in grid.routes
    before = copy.deepcopy(route)
    reuse_marked_vias(route, grid, 0.25)
    assert route == before


def test_kelvin_isolation_rejects_shared_branch_then_restores_reuse():
    from kicad_tools.router.kelvin_obstacles import isolate_kelvin_branch
    from kicad_tools.router.primitives import Pad
    from kicad_tools.router.via_reuse import reuse_marked_vias

    grid, route, existing = marked_context()
    root = Pad(x=0.5, y=1, width=0.3, height=0.3, net=1, net_name="N1", layer=Layer.F_CU)
    target = Pad(x=2, y=1, width=0.3, height=0.3, net=1, net_name="N1", layer=Layer.B_CU)
    before = copy.deepcopy(route)
    copper = copy.deepcopy(existing)
    with isolate_kelvin_branch(grid, [root, target], root, target):
        reuse_marked_vias(route, grid, 0.25)
        assert route == before
    assert existing == copper
    reuse_marked_vias(route, grid, 0.25)
    assert not route.vias


def test_native_validator_rejects_foreign_copper_near_added_dogleg():
    from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
    from kicad_tools.router.primitives import Pad
    from kicad_tools.router.via_reuse import reuse_marked_vias

    if not is_cpp_available():
        pytest.skip("native router required")
    grid, route, existing = marked_context()
    reuse_marked_vias(route, grid, 0.25)
    assert not route.vias
    # Insert foreign physical copper after proposal; clearance validation must
    # inspect added stubs regardless of prior occupancy eligibility.
    foreign = Route(
        net=2, net_name="N2", segments=[Segment(1.3, 1.5, 1.5, 1.5, 0.1, Layer.F_CU, net=2)]
    )
    grid.mark_route(foreign)
    finder = CppPathfinder(CppGrid.from_routing_grid(grid), grid.rules)
    start = Pad(x=0, y=1, width=0.2, height=0.2, net=1, net_name="N1", layer=Layer.F_CU)
    end = Pad(x=2, y=1, width=0.2, height=0.2, net=1, net_name="N1", layer=Layer.B_CU)
    without_stubs = copy.deepcopy(route)
    without_stubs.segments = without_stubs.segments[:2]
    assert finder._validate_route_clearance(without_stubs, start, end, 2) is None
    assert finder._validate_route_clearance(route, start, end, 2) is not None


def test_missing_terminal_layer_keeps_candidate_barrel():
    route = candidate()
    route.segments.pop()
    before = copy.deepcopy(route)
    existing = Route(
        net=1, net_name="N1", vias=[Via(1.1, 1, 0.25, 0.6, (Layer.F_CU, Layer.B_CU), net=1)]
    )
    reuse_same_net_vias(route, [existing], 0.25)
    assert route == before


def test_native_route_validates_reused_candidate(monkeypatch):
    from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
    from kicad_tools.router.primitives import Pad

    if not is_cpp_available():
        pytest.skip("native router required")
    grid, route, existing = marked_context()
    finder = CppPathfinder(CppGrid.from_routing_grid(grid), grid.rules)
    monkeypatch.setattr(finder, "_convert_result_to_route", lambda *args: copy.deepcopy(route))
    validate = finder._validate_route_clearance
    observed = []

    def validate_candidate(proposed, *args, **kwargs):
        assert not proposed.vias
        assert len(proposed.segments) > len(route.segments)
        result = validate(proposed, *args, **kwargs)
        observed.append(result)
        return result

    monkeypatch.setattr(finder, "_validate_route_clearance", validate_candidate)
    start = Pad(x=0.2, y=1, width=0.2, height=0.2, net=1, net_name="N1", layer=Layer.F_CU)
    end = Pad(x=2, y=1, width=0.2, height=0.2, net=1, net_name="N1", layer=Layer.B_CU)
    result = finder.route(start, end, per_net_timeout=2)
    assert result is not None
    assert observed == [None]


def test_implicit_midsegment_contact_retains_barrel():
    route = candidate()
    route.segments.append(Segment(0.5, 1, 1.5, 1, 0.2, Layer.IN1_CU, net=1))
    before = copy.deepcopy(route)
    existing = Route(
        net=1, net_name="N1", vias=[Via(1.1, 1, 0.25, 0.6, (Layer.F_CU, Layer.B_CU), net=1)]
    )
    reuse_same_net_vias(route, [existing], 0.25)
    assert route == before


def test_stale_mark_ledger_cannot_override_empty_occupancy():
    from kicad_tools.router.via_reuse import reuse_marked_vias

    grid, route, existing = marked_context()
    x, y = grid.world_to_grid(existing.vias[0].x, existing.vias[0].y)
    grid._blocked[0, y, x] = False
    before = copy.deepcopy(route)
    reuse_marked_vias(route, grid, 0.25)
    assert route == before
