"""Physical board clearance applies to emitted copper radii."""

import pytest

from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

outline = [(0, 0), (10, 0), (10, 10), (0, 10)]
edges = list(zip(outline, outline[1:] + outline[:1], strict=True))


@pytest.mark.parametrize("width,extent,expected", [(0.2, 1.8, 1), (2.6, 1.8, 0), (2.6, 3.8, 1)])
@pytest.mark.parametrize("cutout", [False, True])
def test_real_route_keeps_emitted_copper_inside_edge(width, extent, expected, cutout):
    pads = [
        Pad(x, 5, 1, 1, net=1, net_name="N1", ref=f"J{i}", pin="1", layer=Layer.F_CU)
        for i, x in enumerate([2, 8])
    ]
    pf = LatticePathfinder(
        outline,
        pads,
        DesignRules(trace_width=0.2, trace_clearance=0.15, strict_layers=True),
        LayerStack.two_layer(),
    )
    physical_edges = edges
    if cutout:
        hole = [(4.9, extent), (5.1, extent), (5.1, 10 - extent), (4.9, 10 - extent)]
        physical_edges = edges + list(zip(hole, hole[1:] + hole[:1], strict=True))
    pf.set_escape_boundary(physical_edges, 0.3)
    nc = NetClassRouting(
        name="wide", trace_width=width, clearance=0.15, avoid_layers=[1], preferred_layers=[0]
    )
    fixed = [
        Route(2, "N2", segments=[Segment(5, extent, 5, 10 - extent, 0.2, Layer.F_CU, 2, "N2")])
    ]
    if cutout:
        fixed = []
    before = repr(fixed)
    routes, stats = pf.route_netset([("link", *pads, nc)], fixed_copper=fixed, max_iterations=2)
    gaps = [
        seg_seg_dist(s.start, s.end, a, b) - s.width / 2
        for r in routes.values()
        for s in r.segments
        for a, b in physical_edges
    ]
    assert repr(fixed) == before
    assert stats.routed == expected
    assert stats.converged == bool(expected)
    if expected:
        assert min(gaps) >= 0.3 - 1e-9
        assert all(not route.vias for route in routes.values())


def test_via_annulus_honors_edge_floor():
    pf = LatticePathfinder(
        outline, [], DesignRules(via_diameter=1.0), LayerStack.two_layer(), coarse=0.4, fine=0.2
    )
    pf.set_escape_boundary(edges, 0.3)
    lattice = pf.build()
    copper = pf._fresh_committed()
    checked = {False: 0, True: 0}
    for key in lattice.adj:
        x, y = lattice.node_point(key)
        if 3 < y < 7 and 0.3 <= x <= 1.2:
            legal = x >= 0.8 - 1e-9
            assert pf._via_ok(key, 1, copper) == legal
            checked[legal] += 1
    assert all(checked.values())
