"""Via hairpins in a committed joint-search leg (Issue #7782).

Board 07's DQS legs run past their via on F.Cu and return over the same cells
on B.Cu.  The doubled copper makes 3+-end junctions, which the kicad-cli
parity measure resolves in file order, so the measured length moved with
segment order.  ``collapse_via_hairpins`` moves the via back to the turn.
"""

from __future__ import annotations

import itertools
import random

import pytest

from kicad_tools.router.kicad_length import LSeg, LVia, net_length
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.via_hairpin import collapse_via_hairpins, merge_junction_runs

STEP = 0.127
Y = 25.451


def _seg(x1, y1, x2, y2, layer):
    return Segment(x1, y1, x2, y2, 0.1, layer, net=1, net_name="N1")


def _route(order=None) -> Route:
    segs = [_seg(24.981, Y, 25.108, Y, Layer.F_CU)]
    xs = [25.108 + STEP * i for i in range(6)]  # 25.108 .. 25.743
    segs += [_seg(a, Y, b, Y, Layer.F_CU) for a, b in itertools.pairwise(xs)]
    segs += [_seg(b, Y, a, Y, Layer.B_CU) for a, b in itertools.pairwise(xs)]
    segs.append(_seg(25.108, Y, 25.108, 23.927, Layer.B_CU))
    segs.append(_seg(25.108, 23.927, 45.0, 23.927, Layer.B_CU))
    if order is not None:
        segs = [segs[i] for i in order]
    via = Via(25.743, Y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=1, net_name="N1")
    return Route(net=1, net_name="N1", segments=segs, vias=[via])


def _length(route: Route) -> float:
    """The kicad-cli parity measure over the segments in list (file) order."""
    segs = [LSeg(s.x1, s.y1, s.x2, s.y2, s.layer.kicad_name) for s in route.segments]
    vias = [LVia(v.x, v.y, v.diameter, ("F.Cu", "B.Cu")) for v in route.vias]
    return net_length(segs, vias, [], None)


def _orders(n: int) -> list[list[int]]:
    rng = random.Random(7782)
    out = []
    for _ in range(12):
        idx = list(range(n))
        rng.shuffle(idx)
        out.append(idx)
    return out


def test_hairpin_length_depends_on_segment_order_before_the_fix():
    n = len(_route().segments)
    lengths = {round(_length(_route(o)), 6) for o in _orders(n)}
    assert len(lengths) > 1


def test_collapse_is_order_independent_and_removes_doubled_copper():
    n = len(_route().segments)
    lengths = set()
    for order in _orders(n):
        route = _route(order)
        assert collapse_via_hairpins(route, lambda v: True) == 1
        assert (route.vias[0].x, route.vias[0].y) == pytest.approx((25.108, Y))
        # No B.Cu copper runs over F.Cu cells any more.
        assert not [s for s in route.segments if s.layer == Layer.B_CU and s.y1 == s.y2 == Y]
        lengths.add(round(_length(route), 6))
    assert len(lengths) == 1


def test_via_stays_when_no_site_is_legal():
    route = _route()
    before = [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in route.segments]
    assert collapse_via_hairpins(route, lambda v: False) == 0
    assert (route.vias[0].x, route.vias[0].y) == (25.743, Y)
    assert before == [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in route.segments]


def test_nearest_legal_site_wins():
    route = _route()
    assert collapse_via_hairpins(route, lambda v: v.x >= 25.4) == 1
    assert route.vias[0].x == pytest.approx(25.489, abs=1e-6)


def test_unmovable_hairpin_is_still_order_independent_after_joining_runs():
    n = len(_route().segments)
    lengths = set()
    for order in _orders(n):
        route = _route(order)
        assert collapse_via_hairpins(route, lambda v: False) == 0
        assert merge_junction_runs(route) > 0
        assert (route.vias[0].x, route.vias[0].y) == (25.743, Y)
        lengths.add(round(_length(route), 6))
    assert len(lengths) == 1


def test_committed_collapse_keeps_grid_state_consistent():
    """The hook edits a registered route without desyncing the grid (#7782)."""
    from types import SimpleNamespace

    from kicad_tools.router.diffpair_routing import DiffPairRouter
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.rules import DesignRules

    grid = RoutingGrid(width=60.0, height=40.0, rules=DesignRules())
    route = _route()
    grid.mark_route(route)
    routes_before = list(grid.routes)
    marked_to_cpp = []
    stub = SimpleNamespace(
        autorouter=SimpleNamespace(
            grid=grid, _mark_route_on_cpp_grid=lambda r: marked_to_cpp.append(r)
        )
    )
    stub._remark_route_cells = lambda r: DiffPairRouter._remark_route_cells(stub, r)

    assert DiffPairRouter._collapse_committed_via_hairpins(stub, [route]) >= 1

    # Registered exactly once; never unmarked/re-appended.
    assert grid.routes == routes_before
    assert marked_to_cpp == [route]
    # The moved via and every surviving segment are blocked for their own net.
    via = route.vias[0]
    gx, gy = grid.world_to_grid(via.x, via.y)
    assert not grid.is_blocked(gx, gy, Layer.F_CU, net=route.net)
    assert grid.is_blocked(gx, gy, Layer.F_CU, net=route.net + 1)
    # Via R-tree tracks the edited via object, not a stale one.
    if grid._via_rtree is not None:
        assert id(via) in grid._via_rtree_items


def test_committed_collapse_rolls_back_when_edit_adds_violation():
    """A hairpin edit that adds a clearance violation is rolled back (#7782)."""
    from types import SimpleNamespace

    from kicad_tools.router.diffpair_routing import DiffPairRouter
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.rules import DesignRules

    grid = RoutingGrid(width=60.0, height=40.0, rules=DesignRules())
    route = _route()
    grid.mark_route(route)
    snapshot = (
        [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in route.segments],
        [(v.x, v.y) for v in route.vias],
    )
    # via_ok accepts the move; the trial's copies of the edited copper
    # (not the registered originals) violate clearance, so the post-edit
    # guard must roll back.
    originals = {id(s) for s in route.segments}
    grid.validate_segment_clearance = lambda seg, net, *a, **k: (
        (True, 1.0, None) if id(seg) in originals else (False, -1.0, (seg.x2, seg.y2))
    )
    stub = SimpleNamespace(
        autorouter=SimpleNamespace(grid=grid, _mark_route_on_cpp_grid=lambda r: None)
    )
    stub._remark_route_cells = lambda r: DiffPairRouter._remark_route_cells(stub, r)

    assert DiffPairRouter._collapse_committed_via_hairpins(stub, [route]) == 0
    assert snapshot == (
        [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in route.segments],
        [(v.x, v.y) for v in route.vias],
    )
