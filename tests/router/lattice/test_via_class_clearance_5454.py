"""A through-via retains class clearance even on skipped routing layers."""

import pytest
from shapely.geometry import box

from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles
from kicad_tools.router.lattice.geometry import seg_pt_dist
from kicad_tools.router.lattice.obstacles import CommittedCopper
from kicad_tools.router.lattice.pairwise import LatticePairwise
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting


def _committed():
    return CommittedCopper(
        4, trace_half=0.1, clearance=0.15, via_radius=0.3, via_via_gap=0.75, same_net_via_gap=0.8
    )


def _route_fixture(clearance, *, fixed=True, spacing=1.5):
    pads = [
        Pad(2, 2, 1, 1, net=1, net_name="N1", ref="J1", pin="1", layer=Layer.F_CU),
        Pad(8, 8, 1, 1, net=1, net_name="N1", ref="J2", pin="1", layer=Layer.B_CU),
    ]
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, strict_layers=True)
    copper = Route(
        2,
        "N2",
        segments=[
            Segment(0, 0.5 + spacing * i, 10, 0.5 + spacing * i, 0.2, Layer.IN1_CU, 2, "N2")
            for i in range(1 + int(9 / spacing))
        ],
    )
    nc = NetClassRouting(
        name="class",
        trace_width=0.2,
        clearance=clearance,
        avoid_layers=[1, 2],
        preferred_layers=[0, 3],
    )
    pf = LatticePathfinder(
        [(0, 0), (10, 0), (10, 10), (0, 10)], pads, rules, LayerStack.four_layer_all_signal()
    )
    return pf, [("link", pads[0], pads[1], nc)], [copper] if fixed else []


@pytest.mark.parametrize(
    "clearance,spacing,expected", [(0.15, 1.5, 1), (0.8, 1.5, 0), (0.8, 3.0, 1)]
)
def test_real_route_checks_via_gap_on_skipped_inner_layer(clearance, spacing, expected):
    pf, connections, fixed = _route_fixture(clearance, spacing=spacing)
    before = repr(fixed)
    routes, stats = pf.route_netset(connections, fixed_copper=fixed, max_iterations=2)
    assert stats.routed == expected
    assert stats.converged == bool(expected)
    assert repr(fixed) == before
    if expected:
        assert routes["link"].vias
    for route in routes.values():
        assert {s.layer for s in route.segments} <= {Layer.F_CU, Layer.B_CU}
        for via in route.vias:
            gap = min(
                seg_pt_dist(s.start, s.end, (via.x, via.y)) - s.width / 2 - via.diameter / 2
                for s in fixed[0].segments
            )
            assert gap >= clearance - 1e-9


@pytest.mark.parametrize("obstacle", ["trace", "via", "fill"])
def test_query_class_grows_gap_on_all_layers(obstacle):
    copper = _committed()
    point = (5, 0.9)
    if obstacle == "trace":
        copper.add_run(1, [(0, 0), (10, 0)], 2, 0.1, 0.15)
    elif obstacle == "via":
        copper.add_via((5, 0), 2, 0.15)
    else:
        copper.fixed_fills = FixedFillObstacles((FixedFill("N2", 2, 1, 0.15, box(0, -1, 10, 0)),))
    assert copper.via_clear(point, 1)
    assert not copper.via_clear(point, 1, clearance=0.8)
    assert copper.via_clear((5, 2), 1, clearance=0.8)


def test_stored_class_and_same_net_drill_floor_remain_effective():
    copper = _committed()
    copper.add_via((0, 0), 2, 1.2)
    assert not copper.via_clear((1.5, 0), 1, clearance=0.8)
    assert copper.via_clear((2, 0), 1, clearance=0.8)
    assert copper.via_clear((1, 0), 2, clearance=10)
    assert not copper.via_clear((0.7, 0), 2, clearance=10)


def test_attach_zone_does_not_waive_query_class_clearance():
    copper = _committed()
    copper.add_run(0, [(0, 0), (10, 0)], 2, 0.1, 0.15)
    copper.pairwise = LatticePairwise({(1, 2): 2}, (), {1: 2, 2: 2})
    assert not copper.via_clear((5, 0.9), 1)
    copper.pairwise = LatticePairwise(
        {(1, 2): 2},
        ((-1, -1, 11, 4, frozenset({1, 2}), {1: frozenset({0}), 2: frozenset({0})}),),
        {1: 2, 2: 2},
    )
    assert copper.via_clear((5, 0.9), 1)
    assert not copper.via_clear((5, 0.9), 1, clearance=0.8)


def test_pad_gate_uses_query_class_clearance():
    pad = Pad(5, 5, 1, 1, net=2, net_name="N2", ref="U1", pin="1", layer=Layer.IN1_CU)
    pf = LatticePathfinder(
        [(0, 0), (10, 0), (10, 10), (0, 10)],
        [pad],
        DesignRules(trace_width=0.2, trace_clearance=0.15),
        LayerStack.four_layer_all_signal(),
    )
    lattice = pf.build()
    key = min(
        lattice.nodes,
        key=lambda k: abs(lattice.node_point(k)[0] - 6.2) + abs(lattice.node_point(k)[1] - 5),
    )
    point = lattice.node_point(key)
    gap = abs(point[0] - pad.x) - pad.width / 2 - pf.rules.via_diameter / 2
    assert 0.15 < gap < 0.8
    copper = pf._fresh_committed()
    assert pf._via_ok(key, 1, copper)
    assert not pf._via_ok(key, 1, copper, clearance=0.8)


def test_newly_routed_via_keeps_class_for_later_net(monkeypatch):
    pf, connections, fixed = _route_fixture(0.8, fixed=False)
    original = pf._fresh_committed
    passes = []

    def capture():
        copper = original()
        passes.append(copper)
        return copper

    monkeypatch.setattr(pf, "_fresh_committed", capture)
    routes, stats = pf.route_netset(connections, fixed_copper=fixed, max_iterations=1)
    assert stats.converged and routes["link"].vias
    via = routes["link"].vias[0]
    # No trace of this outer-only net occupies In1.Cu; its through-via still
    # owns its class gap when a subsequent foreign trace queries that layer.
    point = (via.x + via.diameter / 2 + 0.1 + 0.3, via.y)
    assert not passes[-1].seg_clear(point, point, 1, 99, 0.1, 0.15)


def test_no_class_retains_separate_fixed_fill_via_default():
    copper = _committed()
    copper.fixed_fill_via_clearance = 0.1
    copper.fixed_fills = FixedFillObstacles((FixedFill("N2", 2, 1, 0.1, box(0, -1, 10, 0)),))
    assert copper.via_clear((5, 0.425), 1)
    assert not copper.via_clear((5, 0.425), 1, clearance=0.8)
