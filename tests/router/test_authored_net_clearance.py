"""Authored per-net clearance minima survive insertion order and local relief (#6243).

Ported from the closed PR #5502 (``tests/router/test_authored_net_clearance.py``)
onto the shared clearance kernel: every verdict here comes from
``kicad_tools.router.authored_clearance``'s kernel-backed predicate, reached
through the grid's own validators.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.authored_clearance import (
    AuthoredCopperIndex,
    authored_violations,
    pair_floor,
)
from kicad_tools.router.grid import RTREE_AVAILABLE, RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("gap,valid", [(0.18, False), (0.21, True)])
def test_authored_floor_in_both_trace_insertion_orders(strict_net, gap, valid):
    rules = DesignRules(trace_clearance=0.1, net_clearance_floors={strict_net: 0.2, 3: 0.8})
    grid = RoutingGrid(10, 10, rules)
    a = Segment(2, 5, 8, 5, 0.2, Layer.F_CU, 1)
    b = Segment(2, 5.2 + gap, 8, 5.2 + gap, 0.2, Layer.F_CU, 2)
    for own, foreign in ((a, b), (b, a)):
        grid.routes = [Route(foreign.net, "stored", segments=[foreign])]
        assert grid.validate_segment_clearance(own, own.net)[0] is valid
        # A diff-pair partner gap is a fab relief, never an electrical waiver.
        assert (
            grid.validate_segment_clearance(
                own, own.net, partner_net=foreign.net, partner_clearance=0.05
            )[0]
            is valid
        )


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("gap,valid", [(0.18, False), (0.21, True)])
def test_authored_floor_in_both_via_trace_insertion_orders(strict_net, gap, valid):
    rules = DesignRules(
        trace_clearance=0.1,
        via_clearance=0.1,
        net_clearance_floors={strict_net: 0.2, 3: 0.8},
    )
    grid = RoutingGrid(10, 10, rules)
    via = Via(5, 5, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 1)
    seg = Segment(2, 5.4 + gap, 8, 5.4 + gap, 0.2, Layer.F_CU, 2)
    grid.routes = [Route(1, "via", vias=[via])]
    assert grid.validate_segment_clearance(seg, 2)[0] is valid
    grid.routes = [Route(2, "trace", segments=[seg])]
    assert grid.validate_via_clearance(via, 1)[0] is valid


def test_pair_floor_is_kicad_max_semantics():
    floors = {1: 0.2, 2: 0.1, 3: 2.0}
    rules = DesignRules(trace_clearance=0.1, net_clearance_floors=floors)
    assert rules.clearance_for_nets(1, 2, 0.1) == 0.2
    assert rules.clearance_for_nets(2, 4, 0.1) == 0.1
    assert rules.clearance_for_nets(4, 5, 0.15) == 0.15
    assert rules.clearance_for_nets(1, 1, 0.0) == 0.0  # same net is exempt
    assert pair_floor(floors, 1, 2) == 0.2
    assert pair_floor(floors, 4, 5) == 0.0
    assert pair_floor(floors, 3, 3) == 0.0
    assert pair_floor(floors, 0, 4, own_a=0.5) == 0.5  # neutral pad's own floor
    assert rules.max_clearance == 2.0  # spatial bound only, never pair policy


@pytest.mark.parametrize(
    "floors",
    [{-1: 0.2}, {1: -0.1}, {1: float("nan")}, {1: True}, {"1": 0.2}],
)
def test_malformed_floors_fail_at_construction(floors):
    with pytest.raises(ValueError, match="authored net clearance floor"):
        DesignRules(net_clearance_floors=floors)


def test_empty_floors_keep_validators_byte_identical():
    rules = DesignRules(trace_clearance=0.1)
    grid = RoutingGrid(10, 10, rules)
    grid.routes = [Route(2, "b", segments=[Segment(2, 5.25, 8, 5.25, 0.2, Layer.F_CU, 2)])]
    seg = Segment(2, 5, 8, 5, 0.2, Layer.F_CU, 1)
    assert grid.authored_violation(seg, 1) is None
    assert grid.validate_segment_clearance(seg, 1)[0] is False  # 0.05 < base 0.1


@pytest.mark.parametrize("mode", ["clamp", "skip"])
@pytest.mark.parametrize("gap,valid", [(0.18, False), (0.21, True)])
def test_authored_pad_floor_survives_component_relief(mode, gap, valid):
    rules = DesignRules(trace_clearance=0.1, net_clearance_floors={2: 0.2})
    if mode == "clamp":
        rules.component_clearances["U1"] = 0.05
    grid = RoutingGrid(10, 10, rules)
    grid.add_pad(Pad(5, 5, 1, 1, 2, "foreign", ref="U1", pin="2"))
    if mode == "skip":
        grid._relaxed_clearance_refs.add("U1")
    seg = Segment(4, 5.6 + gap, 6, 5.6 + gap, 0.2, Layer.F_CU, 1)
    via = Via(5, 5.6 + gap, 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 1)
    assert grid.validate_segment_clearance(seg, 1, exclude_refs={"U1"})[0] is valid
    assert grid.validate_via_clearance(via, 1)[0] is valid


@pytest.mark.parametrize("gap,valid", [(0.45, False), (0.55, True)])
def test_neutral_pad_keeps_its_own_authored_floor(gap, valid):
    """A skipped / placement-excluded pad is net 0 but keeps its class minimum."""
    grid = RoutingGrid(10, 10, DesignRules(trace_clearance=0.1))
    grid.add_pad(Pad(5, 5, 1, 1, 0, "", ref="J1", pin="1", authored_clearance=0.5))
    seg = Segment(4, 5.6 + gap, 6, 5.6 + gap, 0.2, Layer.F_CU, 1)
    assert grid.validate_segment_clearance(seg, 1)[0] is valid


@pytest.mark.skipif(not RTREE_AVAILABLE, reason="R-tree optional dependency required")
def test_authored_floor_found_after_floors_change(monkeypatch):
    import kicad_tools.router.grid as grid_module

    monkeypatch.setattr(grid_module, "RTREE_SEGMENT_THRESHOLD", 1)
    rules = DesignRules(trace_clearance=0.1, via_clearance=0.1)
    grid = RoutingGrid(10, 10, rules)
    for net, y in ((2, 6.0), (3, 5.35)):
        grid.mark_route(Route(net, str(net), segments=[Segment(2, y, 8, y, 0.2, Layer.F_CU, net)]))
    candidate = Segment(2, 5, 8, 5, 0.2, Layer.F_CU, 1)
    assert grid.validate_segment_clearance(candidate, 1)[0]
    rules.net_clearance_floors = {2: 0.9}
    is_valid, actual, _loc = grid.validate_segment_clearance(candidate, 1)
    assert not is_valid
    assert actual == pytest.approx(0.8)


def test_census_reports_each_offending_side_once():
    floors = {1: 0.3}
    pads = [Pad(5, 8, 1, 1, 9, "other", ref="R1", pin="1")]
    a = Route(1, "a", segments=[Segment(2, 5, 8, 5, 0.2, Layer.F_CU, 1)])
    b = Route(2, "b", segments=[Segment(2, 5.4, 8, 5.4, 0.2, Layer.F_CU, 2)])
    found = authored_violations(floors, pads, [a, b])
    assert {(v.net, v.other_net) for v in found} == {(1, 2), (2, 1)}
    assert all(v.required == pytest.approx(0.3) for v in found)
    assert all(v.actual == pytest.approx(0.2) for v in found)
    assert authored_violations({}, pads, [a, b]) == []


def test_unfloored_candidate_only_walks_floored_copper():
    index = AuthoredCopperIndex(
        {5: 0.4},
        pads=[Pad(5, 5, 1, 1, 7, "plain", ref="R1", pin="1")],
        routes=[Route(5, "hv", segments=[Segment(0, 9, 10, 9, 0.2, Layer.F_CU, 5)])],
    )
    # 0.05 mm from an unfloored pad: the base machinery's business, not ours.
    assert index.segment_clear(Segment(4, 5.65, 6, 5.65, 0.2, Layer.F_CU, 1))
    # 0.3 mm from the floored HV trace: below its 0.4 mm minimum.
    assert not index.segment_clear(Segment(0, 8.5, 10, 8.5, 0.2, Layer.F_CU, 1))
    # On another layer the trace cannot interact.
    assert index.segment_clear(Segment(0, 8.5, 10, 8.5, 0.2, Layer.B_CU, 1))


def test_finalize_backstop_demotes_one_side_of_an_authored_violation():
    """Whatever engine produced it, copper below an authored minimum is
    demoted to unrouted at finalize -- greedily, one net of the pair."""
    from kicad_tools.router.core import Autorouter

    rules = DesignRules(trace_clearance=0.1, via_clearance=0.1, net_clearance_floors={1: 0.5})
    router = Autorouter(width=10, height=10, rules=rules, force_python=True)
    strict = Route(1, "HV", segments=[Segment(1, 5, 9, 5, 0.2, Layer.F_CU, 1)])
    plain = Route(2, "SIG", segments=[Segment(1, 5.5, 9, 5.5, 0.2, Layer.F_CU, 2)])
    far = Route(3, "FAR", segments=[Segment(1, 9, 9, 9, 0.2, Layer.F_CU, 3)])
    for route in (strict, plain, far):
        router.grid.mark_route(route)
        router.routes.append(route)
    demoted = router.demote_authored_floor_violation_nets()
    assert demoted == [1]
    assert strict not in router.routes and plain in router.routes and far in router.routes
    assert strict not in router.grid.routes
    assert router.demote_authored_floor_violation_nets() == []


def test_finalize_backstop_is_dormant_without_floors():
    from kicad_tools.router.core import Autorouter

    router = Autorouter(width=10, height=10, rules=DesignRules(), force_python=True)
    route = Route(1, "A", segments=[Segment(1, 5, 9, 5, 0.2, Layer.F_CU, 1)])
    router.routes.append(route)
    assert router.demote_authored_floor_violation_nets() == []
    assert router.routes == [route]


@pytest.mark.parametrize("gap,clear", [(0.45, False), (0.55, True)])
def test_optimizer_collision_checkers_keep_authored_minimum(gap, clear):
    """The post-route optimizer may not slide copper below an authored floor."""
    from kicad_tools.router.optimizer.collision import (
        GridCollisionChecker,
        VectorCollisionChecker,
    )

    rules = DesignRules(trace_clearance=0.1, via_clearance=0.1, net_clearance_floors={2: 0.5})
    grid = RoutingGrid(10, 10, rules)
    grid.mark_route(Route(2, "HV", segments=[Segment(1, 5, 9, 5, 0.2, Layer.F_CU, 2)]))
    y = 5.2 + gap
    for checker in (GridCollisionChecker(grid, ignore_overflow=True), VectorCollisionChecker(grid)):
        verdict = checker.path_is_clear(1, y, 9, y, Layer.F_CU, 0.2, 1)
        if not clear:
            assert verdict is False, type(checker).__name__


@pytest.mark.parametrize("gap,clear", [(0.45, False), (0.55, True)])
def test_mesh_obstacle_model_clears_a_floored_pad_at_its_floor(gap, clear):
    from kicad_tools.router.mesh.obstacles import ObstacleModel

    pad = Pad(5, 5, 1, 1, 2, "HV", ref="J1", pin="1")
    model = ObstacleModel(
        [],
        [],
        half=0.1,
        clearance=0.1,
        pads=[pad],
        pad_floors={2: 0.5},
    )
    y = 5.6 + gap
    assert model.is_clear((4, y), (6, y)) is clear
    plain = ObstacleModel([], [], half=0.1, clearance=0.1, pads=[pad])
    assert plain.is_clear((4, y), (6, y))  # without the floor the base 0.1 rules


@pytest.mark.parametrize("gap,kept", [(0.45, False), (0.55, True)])
def test_escape_commit_defers_a_stub_below_an_authored_minimum(gap, kept):
    """An escape stub that would sit below a stricter netclass is handed back to
    the main router (whose search honours the same gate), never committed."""
    from kicad_tools.router.escape import EscapeDirection, EscapeRoute, EscapeRouter
    from kicad_tools.router.layers import LayerStack

    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_clearance=0.15,
        grid_resolution=0.1,
        net_clearance_floors={9: 0.5},
    )
    grid = RoutingGrid(20, 20, rules, layer_stack=LayerStack.two_layer())
    grid.mark_route(Route(9, "HV", segments=[Segment(2, 8, 18, 8, 0.2, Layer.F_CU, 9)]))
    router = EscapeRouter(grid, rules, component_holes=())
    y = 8.2 + gap
    seg = Segment(5, y, 10, y, 0.2, Layer.F_CU, 5, "SIG")
    pad = Pad(5, y, 0.3, 0.3, 5, "SIG", layer=Layer.F_CU, ref="U1", pin="1")
    escape = EscapeRoute(
        pad=pad,
        direction=EscapeDirection.EAST,
        escape_point=(10.0, y),
        escape_layer=Layer.F_CU,
        via_pos=None,
        segments=[seg],
        via=None,
        ring_index=0,
    )
    escapes = [escape]
    routes = router.apply_escape_routes(escapes)
    assert (len(routes) == 1) is kept
    assert (escapes == [escape]) is kept
