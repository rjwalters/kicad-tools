"""The Python post-route validators share the native 1e-4 mm clearance slack.

Issue #6509: ``Grid3D`` (``grid.cpp``) accepts a gap of
``required - CLEARANCE_EPSILON_MM`` and above; the Python
``validate_*`` comparisons in ``grid.py`` rejected anything below
``required`` exactly, so a step landing on the rule to float noise
(0.14999999999999963 against 0.15) was legal natively and refused in
Python.  Each comparison is pinned on both sides of the slack: a gap
``5e-5`` under the rule is accepted, ``2e-4`` under is rejected.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.clearance_kernel import CLEARANCE_EPSILON_MM
from kicad_tools.router.grid import (
    RTREE_AVAILABLE,
    RTREE_SEGMENT_THRESHOLD,
    RoutingGrid,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

CLEARANCE = 0.2
TRACE = 0.2
VIA_DIAMETER = 0.6

#: (gap below the rule, expected verdict).  Inside the slack is legal,
#: outside it is a violation; real overlap stays a violation.
BOUNDARY = [
    pytest.param(-5e-5, True, id="inside-epsilon"),
    pytest.param(-2e-4, False, id="outside-epsilon"),
    pytest.param(-CLEARANCE - 0.05, False, id="overlap"),
]


def test_epsilon_is_the_native_value() -> None:
    assert CLEARANCE_EPSILON_MM == 1e-4


def _grid() -> RoutingGrid:
    rules = DesignRules(
        grid_resolution=0.1,
        trace_width=TRACE,
        trace_clearance=CLEARANCE,
        via_clearance=CLEARANCE,
        via_drill=0.3,
        via_diameter=VIA_DIAMETER,
    )
    return RoutingGrid(width=40.0, height=40.0, rules=rules)


def _seg(x1: float, y1: float, x2: float, y2: float, net: int) -> Segment:
    return Segment(
        x1=x1, y1=y1, x2=x2, y2=y2, width=TRACE, layer=Layer.F_CU, net=net, net_name=f"N{net}"
    )


def _via(x: float, y: float, net: int) -> Via:
    return Via(x=x, y=y, drill=0.3, diameter=VIA_DIAMETER, layers=(Layer.F_CU, Layer.B_CU), net=net)


def _route(net: int, segments=(), vias=()) -> Route:
    route = Route(net=net, net_name=f"N{net}")
    route.segments.extend(segments)
    route.vias.extend(vias)
    return route


# ---------------------------------------------------------------------------
# Segment vs pad (the pad loop shared with the own-pad step gate)
# ---------------------------------------------------------------------------


def _pad_grid() -> RoutingGrid:
    grid = _grid()
    # Right edge at x = 5.5.
    grid.add_pad(Pad(x=5.0, y=5.0, width=1.0, height=1.0, layer=Layer.F_CU, net=1, net_name="N1"))
    return grid


@pytest.mark.parametrize(("delta", "legal"), BOUNDARY)
def test_segment_pad_clearance_boundary(delta: float, legal: bool) -> None:
    grid = _pad_grid()
    x = 5.5 + TRACE / 2 + CLEARANCE + delta
    gap, loc = grid.segment_pad_clearance(x, 3.0, x, 7.0, 0, TRACE / 2, 2, CLEARANCE)
    assert gap == pytest.approx(CLEARANCE + delta, abs=1e-9)  # raw gap, no slack
    assert (loc is None) is legal


@pytest.mark.parametrize(("delta", "legal"), BOUNDARY)
def test_validate_segment_vs_pad_boundary(delta: float, legal: bool) -> None:
    grid = _pad_grid()
    x = 5.5 + TRACE / 2 + CLEARANCE + delta
    ok, actual, _loc = grid.validate_segment_clearance(_seg(x, 3.0, x, 7.0, 2), exclude_net=2)
    assert ok is legal
    assert actual == pytest.approx(CLEARANCE + delta, abs=1e-9)


# ---------------------------------------------------------------------------
# Segment vs stored segment (brute force and R-tree) and vs stored via
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("delta", "legal"), BOUNDARY)
def test_validate_segment_vs_segment_brute_force_boundary(delta: float, legal: bool) -> None:
    grid = _grid()
    grid.routes.append(_route(1, [_seg(2.0, 10.0, 18.0, 10.0, 1)]))
    y = 10.0 + TRACE + CLEARANCE + delta
    ok, actual, _loc = grid.validate_segment_clearance(_seg(2.0, y, 18.0, y, 2), exclude_net=2)
    assert ok is legal
    assert actual == pytest.approx(CLEARANCE + delta, abs=1e-9)


@pytest.mark.skipif(not RTREE_AVAILABLE, reason="rtree not installed")
@pytest.mark.parametrize(("delta", "legal"), BOUNDARY)
def test_validate_segment_vs_segment_rtree_boundary(delta: float, legal: bool) -> None:
    grid = _grid()
    # Far-away filler routes push the index over the R-tree threshold.
    for i in range(RTREE_SEGMENT_THRESHOLD):
        net = 100 + i
        x0 = 25.0 + (i % 10) * 1.2
        y0 = 25.0 + (i // 10) * 0.6
        grid.mark_route(_route(net, [_seg(x0, y0, x0 + 0.5, y0, net)]))
    grid.mark_route(_route(1, [_seg(2.0, 10.0, 18.0, 10.0, 1)]))
    assert grid._seg_rtree_count >= RTREE_SEGMENT_THRESHOLD
    y = 10.0 + TRACE + CLEARANCE + delta
    ok, actual, _loc = grid.validate_segment_clearance(_seg(2.0, y, 18.0, y, 2), exclude_net=2)
    assert ok is legal
    assert actual == pytest.approx(CLEARANCE + delta, abs=1e-9)


@pytest.mark.parametrize(("delta", "legal"), BOUNDARY)
def test_validate_segment_vs_via_boundary(delta: float, legal: bool) -> None:
    grid = _grid()
    grid.routes.append(_route(1, vias=[_via(10.0, 10.0, 1)]))
    y = 10.0 + VIA_DIAMETER / 2 + TRACE / 2 + CLEARANCE + delta
    ok, actual, _loc = grid.validate_segment_clearance(_seg(2.0, y, 18.0, y, 2), exclude_net=2)
    assert ok is legal
    assert actual == pytest.approx(CLEARANCE + delta, abs=1e-9)


# ---------------------------------------------------------------------------
# Via vs stored segment, via vs stored via
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("delta", "legal"), BOUNDARY)
def test_validate_via_vs_segment_boundary(delta: float, legal: bool) -> None:
    grid = _grid()
    grid.routes.append(_route(1, [_seg(2.0, 10.0, 18.0, 10.0, 1)]))
    y = 10.0 + TRACE / 2 + VIA_DIAMETER / 2 + CLEARANCE + delta
    ok, actual, _loc = grid.validate_via_clearance(_via(10.0, y, 2), exclude_net=2)
    assert ok is legal
    assert actual == pytest.approx(CLEARANCE + delta, abs=1e-9)


@pytest.mark.parametrize(("delta", "legal"), BOUNDARY)
def test_validate_via_vs_via_boundary(delta: float, legal: bool) -> None:
    grid = _grid()
    grid.routes.append(_route(1, vias=[_via(10.0, 10.0, 1)]))
    x = 10.0 + VIA_DIAMETER + CLEARANCE + delta
    ok, actual, _loc = grid.validate_via_to_via_clearance(_via(x, 10.0, 2), exclude_net=2)
    assert ok is legal
    assert actual == pytest.approx(CLEARANCE + delta, abs=1e-9)
