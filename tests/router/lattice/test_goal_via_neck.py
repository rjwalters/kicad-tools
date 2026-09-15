"""A via can terminate a narrow pad neck without emitting body-width copper there."""

from dataclasses import replace

import pytest
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask
from kicad_tools.router.lattice.pairwise import LatticePairwise
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting


def make_fixture(*, via_diameter=0.7):
    start = Pad(4, 5, 1, 1, 1, "SIGNAL", ref="J1", pin="1", layer=Layer.B_CU)
    end = Pad(15, 5, 1, 1, 1, "SIGNAL", ref="J2", pin="1")
    walls = [
        Pad(15, y, 12, 0.5, 2, "FOREIGN", ref="U1", pin=str(i)) for i, y in enumerate((3.6, 6.4), 1)
    ]
    rules = DesignRules(
        trace_width=0.2, trace_clearance=0.2, manufacturer="jlcpcb", via_diameter=via_diameter
    )
    pf = LatticePathfinder(
        [(0, 0), (23, 0), (23, 10), (0, 10)],
        [start, end, *walls],
        rules,
        LayerStack.two_layer(),
    )
    nc = NetClassRouting(name="SIGNAL", trace_width=2.6, neck_trace_width=1, clearance=0.2)
    return pf, start, end, nc, walls


def test_tapered_via_arrival_routes_in_both_directions():
    pf, start, end, nc, walls = make_fixture()
    for a, b in ((end, start), (start, end)):
        result, reason = pf._route_impl(
            a, b, nc, committed=pf._fresh_committed(), history={}, present=0
        )
        assert result is not None, (a.ref, b.ref, reason)
        route = result.route
        assert route.vias
        front = [s for s in route.segments if s.layer == Layer.F_CU]
        back = [s for s in route.segments if s.layer == Layer.B_CU]
        assert front and back
        assert {s.width for s in front} == {1.0}
        assert {s.width for s in back} == {2.6}
        wall_copper = unary_union(
            [
                box(p.x - p.width / 2, p.y - p.height / 2, p.x + p.width / 2, p.y + p.height / 2)
                for p in walls
            ]
        )
        # Independently evaluate actual copper, not the search's node masks.
        for segment in front:
            copper = LineString([(segment.x1, segment.y1), (segment.x2, segment.y2)]).buffer(
                segment.width / 2
            )
            assert copper.distance(wall_copper) >= nc.clearance - 1e-9
        for via in route.vias:
            assert (
                Point(via.x, via.y).buffer(via.diameter / 2).distance(wall_copper)
                >= nc.clearance - 1e-9
            )
        # Through-vias join layers; the union must bond both physical pads.
        copper = unary_union(
            [LineString([(s.x1, s.y1), (s.x2, s.y2)]).buffer(s.width / 2) for s in route.segments]
            + [Point(v.x, v.y).buffer(v.diameter / 2) for v in route.vias]
        )
        assert copper.geom_type == "Polygon"
        assert copper.intersects(box(start.x - 0.5, start.y - 0.5, start.x + 0.5, start.y + 0.5))
        assert copper.intersects(box(end.x - 0.5, end.y - 0.5, end.x + 0.5, end.y + 0.5))


@pytest.mark.parametrize("blocker", ["neck", "via", "layer", "keepout", "pairwise", "copper"])
def test_goal_arrival_preserves_physical_constraints(blocker):
    pf, start, end, nc, _ = make_fixture(via_diameter=2.4 if blocker == "via" else 0.7)
    committed = pf._fresh_committed()
    if blocker == "neck":
        nc = replace(nc, neck_trace_width=2.0)
    elif blocker == "layer":
        nc = replace(nc, avoid_layers=[0], target_ampacity=1)
    elif blocker == "keepout":
        pf.set_keepouts(
            LatticeKeepoutMask(
                [
                    KeepoutArea(
                        polygon=((0, 0), (23, 0), (23, 10), (0, 10)),
                        layers=frozenset({0, 1}),
                        blocks_tracks=False,
                        blocks_vias=True,
                    )
                ]
            )
        )
    elif blocker == "pairwise":
        pf.set_pairwise(
            LatticePairwise(
                required_by_pair={(1, 2): 1.5},
                zones=(),
                max_by_net={1: 1.5, 2: 1.5},
            )
        )
    elif blocker == "copper":
        committed.add_run(0, [(9, 5), (21, 5)], net=2, half_width=0.2)
    result, _ = pf._route_impl(start, end, nc, committed=committed, history={}, present=0)
    assert result is None
