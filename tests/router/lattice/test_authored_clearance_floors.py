"""Lattice committed copper must retain mandatory authored electrical floors."""

import pytest

from kicad_tools.router.lattice.obstacles import CommittedCopper


@pytest.mark.parametrize("waiver", [False, True])
@pytest.mark.parametrize("query", ["segment", "node", "via"])
@pytest.mark.parametrize("stored", ["segment", "via"])
@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("floor,valid", [(2.1, True), (2.3, False)])
def test_floor_covers_both_nets_without_blanket_widening(
    query, stored, strict_net, floor, valid, waiver
):
    model = CommittedCopper(
        2,
        trace_half=0.1,
        clearance=0.1,
        via_radius=0.1,
        via_via_gap=0.3,
        same_net_via_gap=0.2,
    )
    if waiver:
        from kicad_tools.router.lattice.pairwise import LatticePairwise

        model.pairwise = LatticePairwise(
            {(1, 2): 5.0},
            ((0, 0, 20, 20, frozenset({1, 2}), None),),
            {1: 5.0, 2: 5.0},
        )
    if stored == "segment":
        model.add_run(0, [(5, 8.2), (7, 8.2)], 2, 0.1)
    else:
        model.add_via((6, 8.2), 2)
    # A distant, unrelated class must affect the candidate query bounds only.
    model.net_clearance_floors = {strict_net: floor, 3: 4.0}
    if query == "segment":
        actual = model.seg_clear((5, 5.8), (7, 5.8), 0, 1)
    elif query == "node":
        actual = model.node_clear((6, 5.8), 0, 1)
    else:
        actual = model.via_clear((6, 5.8), 1)
    assert actual is valid


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("floor,expected", [(0.15, 1), (0.8, 0)])
def test_route_checks_authored_floor_on_skipped_inner_layer(strict_net, floor, expected):
    from kicad_tools.router.lattice.pathfinder import LatticePathfinder
    from kicad_tools.router.layers import Layer, LayerStack
    from kicad_tools.router.primitives import Pad, Route, Segment
    from kicad_tools.router.rules import DesignRules, NetClassRouting

    pads = [
        Pad(2, 2, 1, 1, net=1, net_name="N1", ref="J1", pin="1", layer=Layer.F_CU),
        Pad(8, 8, 1, 1, net=1, net_name="N1", ref="J2", pin="1", layer=Layer.B_CU),
    ]
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        strict_layers=True,
        net_clearance_floors={strict_net: floor},
    )
    fixed = Route(
        2,
        "N2",
        segments=[
            Segment(0, 0.5 + 1.5 * i, 10, 0.5 + 1.5 * i, 0.2, Layer.IN1_CU, 2, "N2")
            for i in range(7)
        ],
    )
    nc = NetClassRouting(
        name="ordinary",
        trace_width=0.2,
        clearance=0.15,
        avoid_layers=[1, 2],
        preferred_layers=[0, 3],
    )
    pf = LatticePathfinder(
        [(0, 0), (10, 0), (10, 10), (0, 10)], pads, rules, LayerStack.four_layer_all_signal()
    )
    before = repr(fixed)
    routes, stats = pf.route_netset(
        [("link", pads[0], pads[1], nc)], fixed_copper=[fixed], max_iterations=2
    )
    assert stats.routed == expected
    assert stats.converged == bool(expected)
    assert repr(fixed) == before
    if expected:
        assert routes["link"].vias


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("floor,valid", [(0.15, True), (0.8, False)])
def test_via_pad_gate_keeps_authored_floor_on_inner_layer(strict_net, floor, valid):
    from kicad_tools.router.lattice.pathfinder import LatticePathfinder
    from kicad_tools.router.layers import Layer, LayerStack
    from kicad_tools.router.primitives import Pad
    from kicad_tools.router.rules import DesignRules

    pad = Pad(5, 5, 1, 1, net=2, net_name="N2", ref="U1", pin="1", layer=Layer.IN1_CU)
    pf = LatticePathfinder(
        [(0, 0), (10, 0), (10, 10), (0, 10)],
        [pad],
        DesignRules(
            trace_width=0.2, trace_clearance=0.15, net_clearance_floors={strict_net: floor, 3: 4.0}
        ),
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
    assert pf._via_ok(key, 1, pf._fresh_committed()) is valid


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("width", [0.2, 0.6])
def test_trace_route_keeps_authored_pad_floor_at_emitted_width(strict_net, width):
    from shapely.geometry import LineString, box

    from kicad_tools.router.lattice.pathfinder import LatticePathfinder
    from kicad_tools.router.layers import Layer, LayerStack
    from kicad_tools.router.primitives import Pad
    from kicad_tools.router.rules import DesignRules, NetClassRouting

    pads = [
        Pad(2, 5, 1, 1, net=1, net_name="N1", ref="J1", pin="1", layer=Layer.F_CU),
        Pad(8, 5, 1, 1, net=1, net_name="N1", ref="J2", pin="1", layer=Layer.F_CU),
        Pad(5, 5, 1, 1, net=2, net_name="N2", ref="U1", pin="1", layer=Layer.F_CU),
    ]
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        strict_layers=True,
        net_clearance_floors={strict_net: 0.8, 3: 4.0},
    )
    pf = LatticePathfinder(
        [(0, 0), (10, 0), (10, 10), (0, 10)], pads, rules, LayerStack.two_layer()
    )
    nc = NetClassRouting(
        name="ordinary", trace_width=width, clearance=0.15, avoid_layers=[1], preferred_layers=[0]
    )
    routes, stats = pf.route_netset([("link", pads[0], pads[1], nc)], max_iterations=2)
    assert stats.converged
    route = routes["link"]
    assert route.segments and not route.vias
    obstacle = box(4.5, 4.5, 5.5, 5.5)
    for seg in route.segments:
        gap = LineString([seg.start, seg.end]).distance(obstacle) - seg.width / 2
        assert gap >= 0.8 - 1e-9


@pytest.mark.parametrize("strict_net", [1, 2])
def test_coupled_pitch_cannot_waive_authored_floor(strict_net):
    from dataclasses import replace

    from tests.router.lattice.test_coupled_pairs import _pair_board

    pf, pc = _pair_board()
    pf.rules = replace(pf.rules, net_clearance_floors={strict_net: 0.2})
    result, reason = pf._route_pair_impl(
        pc, committed=pf._fresh_committed(), history={}, present=1.0
    )
    assert result is None
    assert reason == "pair-authored-clearance"


@pytest.mark.parametrize("query", ["segment", "point"])
@pytest.mark.parametrize("obstacle", ["trace", "via", "fill"])
@pytest.mark.parametrize("strict_net", [1, 2, 3])
@pytest.mark.parametrize("floor,valid", [(0.3, True), (0.5, False)])
def test_coupled_envelope_retains_either_rail_and_obstacle_floor(
    query, obstacle, strict_net, floor, valid
):
    from shapely.geometry import box

    from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles
    from kicad_tools.router.lattice.coupled import (
        committed_point_clear_grown,
        committed_seg_clear_grown,
    )

    model = CommittedCopper(
        2,
        trace_half=0.1,
        clearance=0.1,
        via_radius=0.1,
        via_via_gap=0.3,
        same_net_via_gap=0.2,
        net_clearance_floors={strict_net: floor, 4: 3.0},
    )
    if obstacle == "trace":
        model.add_run(0, [(4, 5), (8, 5)], 3, 0.1)
    elif obstacle == "via":
        model.add_via((6, 5), 3)
    else:
        model.fixed_fills = FixedFillObstacles(
            (FixedFill("foreign", 3, 0, 0.1, box(4, 4, 8, 5.1)),)
        )
    if query == "segment":
        actual = committed_seg_clear_grown(model, (4, 5.8), (8, 5.8), 0, {1, 2}, 0.2)
    else:
        actual = committed_point_clear_grown(model, (6, 5.8), 0, {1, 2}, 0.2)
    assert actual is valid


def test_coupled_emitted_legs_keep_foreign_pad_floor():
    from dataclasses import replace

    from shapely.geometry import LineString, box

    from tests.router.lattice.test_coupled_pairs import _pad, _pair_board

    obstacle = _pad(15, 6.4, 3, "foreign", ref="U2", width=0.3, height=0.3)
    pf, pc = _pair_board([obstacle])
    pf.rules = replace(pf.rules, net_clearance_floors={3: 1.3})
    routes, stats = pf.route_netset([], coupled=[pc], max_iterations=2)
    assert stats.routed == 1 and pf.pair_outcomes[pc.key] == "coupled"
    polygon = box(14.85, 6.25, 15.15, 6.55)
    for route in routes.values():
        for segment in route.segments:
            gap = LineString([segment.start, segment.end]).distance(polygon) - segment.width / 2
            assert gap >= 1.3 - 1e-9


@pytest.mark.parametrize("kind", ["segment", "point"])
@pytest.mark.parametrize("strict_net", [1, 2, 3])
@pytest.mark.parametrize("floor,blocked", [(0.6, False), (0.8, True)])
def test_coupled_pad_envelope_uses_only_participating_net_floors(kind, strict_net, floor, blocked):
    from kicad_tools.router.lattice.coupled import pads_block_point_grown, pads_block_segment_grown
    from kicad_tools.router.lattice.pathfinder import LatticePathfinder
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.primitives import Pad
    from kicad_tools.router.rules import DesignRules

    rules = DesignRules(trace_width=0.2, trace_clearance=0.15)
    pf = LatticePathfinder(
        [(0, 0), (10, 0), (10, 10), (0, 10)],
        [Pad(5, 5, 1, 1, 3, "foreign")],
        rules,
        LayerStack.two_layer(),
    )
    kwargs = {"floors": {strict_net: floor, 4: 5.0}, "base_clearance": rules.trace_clearance}
    if kind == "segment":
        actual = pads_block_segment_grown(
            pf.obstacles, (4, 6.5), (6, 6.5), 0, {1, 2}, 0.2, **kwargs
        )
    else:
        actual = pads_block_point_grown(pf.obstacles, (5, 6.5), 0, {1, 2}, 0.2, **kwargs)
    assert actual is blocked
