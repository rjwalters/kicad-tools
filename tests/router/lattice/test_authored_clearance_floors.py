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
