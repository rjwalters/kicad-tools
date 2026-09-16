"""Reconstructed endpoint geometry must survive angle repair and length tuning."""

import math

import pytest
from shapely.geometry import LineString

from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route
from kicad_tools.router.quantize import is_45_aligned
from kicad_tools.router.rules import DesignRules


def build(start, end, path):
    rules = DesignRules()
    grid = RoutingGrid(width=10, height=10, rules=rules)
    finder = CoupledPathfinder(grid=grid, rules=rules, target_spacing_cells=2, min_spacing_cells=2)
    pads = [
        Pad(x=x, y=y, width=0.3, height=0.3, net=1, net_name="P", layer=layer)
        for x, y, layer in (start, end)
    ]
    route = Route(net=1, net_name="P")
    finder._build_route_from_path(route, path, *pads)
    return route


@pytest.mark.parametrize("reverse", [False, True])
def test_attachment_alignment_removes_root_backtracking(reverse):
    start = (2.0, 2.4, Layer.F_CU)
    end = (3.0, 2.197, Layer.F_CU)
    path = [(1.981, 2.451, 0, False), (1.981, 2.197, 0, False), (3.0, 2.197, 0, False)]
    if reverse:
        start, end = end, start
        path.reverse()
    route = build(start, end, path)
    assert route.segments[0].start == start[:2]
    assert route.segments[-1].end == end[:2]
    for a, b in zip(route.segments[:-1], route.segments[1:], strict=True):
        assert a.end == b.start
    for i, seg in enumerate(route.segments):
        assert is_45_aligned(seg.x2 - seg.x1, seg.y2 - seg.y1)
        for other in route.segments[:i]:
            assert (
                LineString([seg.start, seg.end])
                .intersection(LineString([other.start, other.end]))
                .length
                <= 1e-9
            )


def test_attachment_preserves_via_vertex():
    route = build(
        (2.0, 2.4, Layer.F_CU),
        (3.0, 2.197, Layer.B_CU),
        [
            (1.981, 2.451, 0, False),
            (1.981, 2.451, 1, True),
            (1.981, 2.197, 1, False),
            (3.0, 2.197, 1, False),
        ],
    )
    assert len(route.vias) == 1
    via = route.vias[0]
    assert (via.x, via.y) == (1.981, 2.451)
    assert any(s.end == (via.x, via.y) and s.layer == Layer.F_CU for s in route.segments)
    assert any(s.start == (via.x, via.y) and s.layer == Layer.B_CU for s in route.segments)


def test_sub_ten_micron_offsets_keep_exact_endpoints():
    route = build(
        (2.001, 2.003, Layer.F_CU),
        (3.004, 2.002, Layer.F_CU),
        [(2.0, 2.0, 0, False), (3.0, 2.0, 0, False)],
    )
    assert route.segments[0].start == (2.001, 2.003)
    assert route.segments[-1].end == (3.004, 2.002)
    assert all(is_45_aligned(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)
    assert all(
        math.dist(a.end, b.start) < 1e-12
        for a, b in zip(route.segments[:-1], route.segments[1:], strict=True)
    )


def test_captured_dqs_pad_grid_bridge_is_aligned():
    # Current-factory Board07 witness: U1.30 at (125,125.4) and the
    # first 0.127mm grid point. The former direct bridge had dx=-.019,
    # dy=.051 and failed every constructed candidate's angle check.
    route = build(
        (125.0, 125.4, Layer.F_CU),
        (124.981, 125.197, Layer.F_CU),
        [(124.981, 125.451, 0, False), (124.981, 125.197, 0, False)],
    )
    assert route.segments[0].start == (125.0, 125.4)
    assert route.segments[-1].end == (124.981, 125.197)
    assert all(is_45_aligned(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)
    for a, b in zip(route.segments[:-1], route.segments[1:], strict=True):
        assert a.end == b.start
    for i, segment in enumerate(route.segments):
        for other in route.segments[:i]:
            assert LineString([segment.start, segment.end]).intersection(
                LineString([other.start, other.end])
            ).length <= 1e-9
