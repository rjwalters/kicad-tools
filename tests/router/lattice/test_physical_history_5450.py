"""Negotiation must learn from geometric contention beyond shared edge keys."""

from shapely.geometry import LineString, Point

from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.history import PhysicalHistory
from kicad_tools.router.lattice.pairwise import LatticePairwise
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting


def test_physical_demand_covers_different_edges_and_layers():
    history = PhysicalHistory()
    history.add_segment((0, 0), (10, 0), 0, 1, 1.0, 0.4, 3.0)
    # Parallel centerlines need not share a graph edge to compete for copper.
    assert history.cost((2, 1.4), (8, 1.4), 0, 2, 0.1, 0.2) > 0
    assert history.cost((2, 1.6), (8, 1.6), 0, 2, 0.1, 0.2) == 0
    assert history.cost((2, 1.4), (8, 1.4), 1, 2, 0.1, 0.2) == 0
    assert history.cost((2, 0), (8, 0), 0, 1, 0.1, 0.2) == 0
    assert history.cost((2, 0), (8, 0), 0, 2, 0.1, 0.2, partner_net=1) == 0


def test_via_demand_uses_its_copper_radius():
    history = PhysicalHistory()
    history.add_segment((5, 5), (5, 5), 1, 1, 0.5, 0.2, 4.0)
    assert history.cost((0, 5.6), (10, 5.6), 1, 2, 0.1, 0.2) > 0
    assert history.cost((0, 5.9), (10, 5.9), 1, 2, 0.1, 0.2) == 0
    assert history.cost((0, 5.6), (10, 5.6), 0, 2, 0.1, 0.2) == 0


def test_history_respects_pairwise_radius_and_layer_scoped_attach_zone():
    history = PhysicalHistory()
    for layer in (0, 1):
        history.add_segment((0, 0), (10, 0), layer, 1, 0.1, 0.2, 3.0)
    pairwise = LatticePairwise({(1, 2): 3.0}, (), {1: 3.0, 2: 3.0})
    assert history.cost((2, 2.5), (8, 2.5), 0, 2, 0.1, 0.2) == 0
    assert history.cost((2, 2.5), (8, 2.5), 0, 2, 0.1, 0.2, pairwise) > 0
    exempt = LatticePairwise(
        pairwise.required_by_pair,
        ((-1, -1, 11, 4, frozenset({1, 2}), {1: frozenset({0}), 2: frozenset({0})}),),
        pairwise.max_by_net,
    )
    assert history.cost((2, 2.5), (8, 2.5), 0, 2, 0.1, 0.2, exempt) == 0
    assert history.cost((2, 2.5), (8, 2.5), 1, 2, 0.1, 0.2, exempt) > 0


def test_escape_conflict_negotiates_all_three_nets():
    pads = [
        Pad(
            110,
            88.4,
            1.8,
            1.8,
            net=1,
            net_name="N1",
            ref="J1",
            pin="1",
            layer=Layer.F_CU,
            through_hole=True,
            drill=0.8,
            shape="circle",
        ),
        Pad(
            93.3625,
            129.95,
            1.325,
            0.6,
            net=2,
            net_name="N2",
            ref="U1",
            pin="1",
            layer=Layer.F_CU,
            shape="roundrect",
        ),
        Pad(
            95.6375,
            129.95,
            1.325,
            0.6,
            net=1,
            net_name="N1",
            ref="U1",
            pin="2",
            layer=Layer.F_CU,
            shape="roundrect",
        ),
        Pad(
            95.6375,
            129,
            1.325,
            0.6,
            net=3,
            net_name="N3",
            ref="U1",
            pin="3",
            layer=Layer.F_CU,
            shape="roundrect",
        ),
        Pad(
            83.5375,
            129,
            1.225,
            3.35,
            net=2,
            net_name="N2",
            ref="R1",
            pin="1",
            layer=Layer.F_CU,
            shape="roundrect",
        ),
        Pad(
            94.02,
            139,
            0.56,
            0.62,
            net=3,
            net_name="N3",
            ref="C1",
            pin="1",
            layer=Layer.F_CU,
            shape="roundrect",
        ),
    ]
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb")
    pf = LatticePathfinder(
        [(78.5375, 83.4), (115, 83.4), (115, 144), (78.5375, 144)],
        pads,
        rules,
        LayerStack.four_layer_sig_gnd_pwr_sig(),
    )
    wide = NetClassRouting(
        name="wide",
        trace_width=2.6,
        neck_trace_width=2.0,
        clearance=0.5,
        target_ampacity=15,
        avoid_layers=["In1.Cu", "In2.Cu"],
    )
    power = NetClassRouting(
        name="power",
        trace_width=2.6,
        clearance=0.4,
        target_ampacity=25,
        avoid_layers=["In1.Cu", "In2.Cu"],
    )
    thin = NetClassRouting(name="thin", trace_width=0.2, clearance=0.2)
    connections = [
        (0, pads[3], pads[5], thin),
        (1, pads[1], pads[4], power),
        (2, pads[0], pads[2], wide),
    ]
    routes, stats = pf.route_netset(connections, max_iterations=8)
    assert stats.converged and stats.routed == stats.total == 3
    assert set(routes) == {0, 1, 2}
    assert all(route.segments for route in routes.values())
    # Independently connect emitted copper by real polygon contact on shared
    # layers. Through-vias bridge layers; neither endpoint may be omitted.
    all_layers = {pf.layer_stack.index_to_layer_enum(i) for i in range(pf.num_layers)}
    for key, start, end, _cls in connections:
        route = routes[key]
        copper = [
            ({s.layer}, LineString([(s.x1, s.y1), (s.x2, s.y2)]).buffer(s.width / 2))
            for s in route.segments
        ]
        copper.extend((all_layers, Point(v.x, v.y).buffer(v.diameter / 2)) for v in route.vias)
        copper.extend(
            (
                all_layers if p.through_hole else {p.layer},
                Point(p.x, p.y)
                .buffer(min(p.width, p.height) / 2)
                .difference(Point(p.x, p.y).buffer(p.drill / 2) if p.through_hole else Point()),
            )
            for p in (start, end)
        )
        reached = {len(copper) - 2}
        while True:
            neighbors = {
                j
                for i in reached
                for j in range(len(copper))
                if copper[i][0] & copper[j][0] and copper[i][1].intersects(copper[j][1])
            }
            if neighbors <= reached:
                break
            reached |= neighbors
        assert reached == set(range(len(copper))), (
            key,
            [
                (j, min(copper[j][1].distance(copper[i][1]) for i in reached))
                for j in set(range(len(copper))) - reached
            ],
        )
    # Verify inter-net trace gaps independently of the search predicates.
    for i, (_, _, _, cls_a) in enumerate(connections):
        for j in range(i + 1, len(connections)):
            cls_b = connections[j][3]
            for a in routes[i].segments:
                for b in routes[j].segments:
                    if a.layer == b.layer:
                        gap = (
                            seg_seg_dist((a.x1, a.y1), (a.x2, a.y2), (b.x1, b.y1), (b.x2, b.y2))
                            - (a.width + b.width) / 2
                        )
                        assert gap >= max(cls_a.clearance, cls_b.clearance) - 1e-6
