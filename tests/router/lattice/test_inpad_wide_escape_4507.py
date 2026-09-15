"""Wide rated-pad attachment changes geometry, never declared floors."""

from dataclasses import replace

import pytest
from shapely.geometry import box

from kicad_tools.geometry.copper import segment_copper_polygon
from kicad_tools.router.lattice.pairwise import LatticePairwise
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting


def fixture(*, crowded=False):
    root = Pad(3, 5, 1.025, 1.4, 1, "HV", ref="R1", pin="1", shape="roundrect")
    sibling = Pad(4.825, 5, 1.025, 1.4, 2, "LV", ref="R1", pin="2", shape="roundrect")
    pads = [root, sibling]
    if crowded:
        pads.append(Pad(1.175, 5, 1.025, 1.4, 3, "OTHER", ref="R2", pin="1"))
    pf = LatticePathfinder(
        [(0, 0), (9, 0), (9, 10), (0, 10)],
        pads,
        DesignRules(trace_width=0.2, trace_clearance=0.15),
        LayerStack.two_layer(),
    )
    nc = NetClassRouting(
        name="HV", trace_width=2.6, neck_trace_width=2.0, clearance=0.4, target_ampacity=15
    )
    return pf, root, sibling, nc


def escape(pf, root, nc):
    return pf._escape_stubs(
        root,
        root.net,
        None,
        kmax=4,
        extra_clearance=0,
        partner_net=None,
        layers=(0,),
        exempt_pads=None,
        net_class=nc,
    )


def test_actual_declared_neck_attaches_inside_pad_without_lowering_floors():
    pf, root, sibling, nc = fixture()
    assert pf._scan_stubs(root, 1, None, 1.0, 0.4, kmax=4, search_radius=3) == []
    stubs, width = escape(pf, root, nc)
    assert stubs and width == 2.0
    pad_copper = (
        box(
            root.x - root.width / 2,
            root.y - root.height / 2,
            root.x + root.width / 2,
            root.y + root.height / 2,
        )
        .buffer(-0.25 * min(root.width, root.height))
        .buffer(0.25 * min(root.width, root.height))
    )
    foreign = box(
        sibling.x - sibling.width / 2,
        sibling.y - sibling.height / 2,
        sibling.x + sibling.width / 2,
        sibling.y + sibling.height / 2,
    )
    for _, _, points, _ in stubs:
        first = segment_copper_polygon(points[0], points[1], width)
        assert first.intersects(pad_copper)
        assert points[0] != (root.x, root.y)
        for a, b in zip(points, points[1:], strict=False):
            assert segment_copper_polygon(a, b, width).distance(foreign) >= 0.4 - 1e-9
    assert (nc.trace_width, nc.neck_trace_width, nc.clearance, nc.target_ampacity) == (
        2.6,
        2.0,
        0.4,
        15,
    )


def test_foreign_copper_on_both_sides_still_refuses():
    pf, root, _, nc = fixture(crowded=True)
    stubs, width = escape(pf, root, nc)
    assert not stubs and width == 2.0


def test_pairwise_waiver_remains_required_and_unrelated_net_is_not_waived():
    pf, root, _, nc = fixture()
    pf.set_pairwise(
        LatticePairwise(required_by_pair={(1, 2): 1.6}, zones=(), max_by_net={1: 1.6, 2: 1.6})
    )
    assert not escape(pf, root, nc)[0]
    pf.set_pairwise(
        LatticePairwise(
            required_by_pair={(1, 2): 1.6},
            zones=((0, 0, 9, 10, frozenset({1, 2}), None),),
            max_by_net={1: 1.6, 2: 1.6},
        )
    )
    assert escape(pf, root, nc)[0]
    pf.set_pairwise(
        LatticePairwise(
            required_by_pair={(1, 2): 1.6},
            zones=((0, 0, 9, 10, frozenset({1, 3}), None),),
            max_by_net={1: 1.6, 2: 1.6},
        )
    )
    assert not escape(pf, root, nc)[0]


def test_existing_successful_center_escape_is_unchanged():
    pf, root, _, nc = fixture()
    nc = replace(nc, trace_width=0.2, neck_trace_width=None)
    before = pf.pad_stubs(root, root.net, None, kmax=4, layers=(0,), net_class=nc)
    after, width = escape(pf, root, nc)
    assert before and after == before and width == 0.2


@pytest.mark.parametrize(
    "shape,rotation",
    [("rect", 0), ("rect", 35), ("rect", 90), ("circle", 35), ("oval", 35), ("roundrect", 35)],
)
def test_complete_emitted_route_bonds_pad_without_reintroducing_center_leg(shape, rotation):
    from shapely.geometry import Point
    from shapely.ops import unary_union

    from kicad_tools.router.layers import Layer

    _, root, sibling, nc = fixture()
    root = replace(root, shape=shape, rotation=rotation)
    target = Pad(-1, 5, 3, 3, 1, "HV", ref="J1", pin="1")
    pf = LatticePathfinder(
        [(-4, 0), (9, 0), (9, 10), (-4, 10)],
        [root, sibling, target],
        DesignRules(trace_width=0.2, trace_clearance=0.15),
        LayerStack.two_layer(),
    )
    route = pf.route(root, target, nc)
    assert route is not None
    assert route.segments and all(s.width >= 2.0 for s in route.segments)
    assert all(s.layer == Layer.F_CU for s in route.segments)
    copper = unary_union([segment_copper_polygon(s.start, s.end, s.width) for s in route.segments])
    from types import SimpleNamespace

    from kicad_tools.validate.rules.clearance import _pad_polygon

    rootpoly = _pad_polygon(
        SimpleNamespace(
            position=(root.x, root.y),
            size=(root.width, root.height),
            shape=shape,
            rotation=rotation,
            roundrect_rratio=0.25,
        ),
        SimpleNamespace(position=(0, 0), rotation=0),
    )
    targetpoly = box(-2.5, 3.5, 0.5, 6.5)
    foreign = box(
        sibling.x - sibling.width / 2,
        sibling.y - sibling.height / 2,
        sibling.x + sibling.width / 2,
        sibling.y + sibling.height / 2,
    )
    assert copper.intersection(rootpoly).area > 0
    assert copper.intersection(targetpoly).area > 0
    assert copper.geom_type == "Polygon"
    assert copper.distance(foreign) >= 0.4 - 1e-9
    # The wide cap at the original center is exactly what failed. Emission
    # must keep the legal in-pad endpoint, not append a center connector.
    assert all(s.start != (root.x, root.y) and s.end != (root.x, root.y) for s in route.segments)
    assert rootpoly.contains(Point(route.segments[0].start))
    assert route.segments[0].to_sexp()


@pytest.mark.parametrize("shape", ["custom", "trapezoid", "unknown"])
def test_unproved_pad_shapes_do_not_get_interior_attachment_fallback(shape):
    pf, root, _, nc = fixture()
    # Current Pad construction rejects these; retain a defensive guard for
    # legacy/mutated carriers whose nominal box does not establish copper.
    root.shape = shape
    stubs, width = escape(pf, root, nc)
    assert not stubs and width == 2.0
