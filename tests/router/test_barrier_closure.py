"""Barrier-closure diagnostic (Issue #9000): report-only loop detection."""

from __future__ import annotations

import pytest

from kicad_tools.router.barrier_closure import (
    ENV_FLAG,
    BarrierClosureTracker,
    point_in_polygon,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment

TILE = 10.0
LAYER_INDEX = {Layer.F_CU: 0, Layer.B_CU: 1}


def _region_of(x: float, y: float) -> int | None:
    if not (0 <= x < 40 and 0 <= y < 40):
        return None
    return int(y // TILE) * 4 + int(x // TILE)


def _route(net: int, layer: Layer, *pts: tuple[float, float]) -> Route:
    segs = [
        Segment(x1=a[0], y1=a[1], x2=b[0], y2=b[1], width=0.2, layer=layer, net=net)
        for a, b in zip(pts, pts[1:], strict=False)
    ]
    return Route(net=net, net_name=f"N{net}", segments=segs)


def _tracker(signal: list[int], pads: dict[int, list[tuple[float, float]]]):
    return BarrierClosureTracker(_region_of, LAYER_INDEX.get, signal, pads)


# A square of copper around (20, 20), one route per side.  Corners sit in four
# distinct tiles, so each side welds two tiles and the fourth closes the loop.
SQUARE = [
    [(12.0, 12.0), (28.0, 12.0)],
    [(28.0, 12.0), (28.0, 28.0)],
    [(28.0, 28.0), (12.0, 28.0)],
    [(12.0, 28.0), (12.0, 12.0)],
]
# Net 9 has a pad inside the square and one outside: it must cross the loop.
PADS = {9: [(20.0, 20.0), (35.0, 35.0)], 8: [(34.0, 34.0), (36.0, 36.0)]}


def _commit_square(tracker, layer: Layer, committed: list[Route], base_net: int = 1):
    events = []
    for i, pts in enumerate(SQUARE):
        r = _route(base_net + i, layer, *pts)
        committed.append(r)
        events.append(tracker.on_commit(r, committed))
    return events


def test_point_in_polygon():
    sq = [(0, 0), (4, 0), (4, 4), (0, 4)]
    assert point_in_polygon((2, 2), sq)
    assert not point_in_polygon((5, 2), sq)
    assert not point_in_polygon((1, 1), [(0, 0), (1, 1)])


def test_loop_around_pad_reports_severed_net_single_layer():
    tracker = _tracker([0], PADS)
    events = _commit_square(tracker, Layer.F_CU, [])
    assert events[:3] == [None, None, None]
    ev = events[3]
    assert ev is not None
    assert ev.severed_nets == (9,)
    assert ev.severed_connections == 1
    assert ev.layer == 0
    assert tracker.events == [ev]


def test_no_loop_reports_nothing():
    tracker = _tracker([0], PADS)
    committed: list[Route] = []
    for pts in SQUARE[:3]:  # open "C" shape, not closed
        r = _route(1, Layer.F_CU, *pts)
        committed.append(r)
        assert tracker.on_commit(r, committed) is None
    assert tracker.ring_count == 0
    assert tracker.events == []


def test_loop_on_one_of_two_layers_is_layer_blocked_not_severed():
    tracker = _tracker([0, 1], PADS)
    ev = _commit_square(tracker, Layer.F_CU, [])[3]
    assert ev is not None
    assert ev.severed_nets == ()
    assert ev.layer_blocked_nets == (9,)


def test_loop_on_every_layer_is_severed():
    tracker = _tracker([0, 1], PADS)
    committed: list[Route] = []
    _commit_square(tracker, Layer.F_CU, committed, base_net=1)
    ev = _commit_square(tracker, Layer.B_CU, committed, base_net=5)[3]
    assert ev is not None
    assert ev.severed_nets == (9,)
    assert ev.severed_connections == 1


def test_net_inside_the_loop_only_is_not_severed():
    pads = {9: [(20.0, 20.0), (21.0, 21.0)]}  # both pads inside
    tracker = _tracker([0], pads)
    assert _commit_square(tracker, Layer.F_CU, [])[3] is None


def test_already_connected_net_is_not_severed():
    tracker = _tracker([0], PADS)
    committed = [_route(9, Layer.B_CU, (20.0, 20.0), (35.0, 35.0))]
    ev = _commit_square(tracker, Layer.F_CU, committed)[3]
    assert ev is None


def test_loop_nets_are_not_counted_as_severed_by_their_own_loop():
    # Net 9 owns one side of the square, so its copper merges with the wall.
    tracker = _tracker([0], PADS)
    committed: list[Route] = []
    for i, pts in enumerate(SQUARE):
        r = _route(9 if i == 0 else i, Layer.F_CU, *pts)
        committed.append(r)
        ev = tracker.on_commit(r, committed)
    assert ev is None


def test_ripup_of_closing_copper_clears_the_weld():
    tracker = _tracker([0], PADS)
    committed: list[Route] = []
    _commit_square(tracker, Layer.F_CU, committed)
    assert tracker.ring_count == 1

    # Rip up the closing side, then commit unrelated copper elsewhere.
    committed.pop()
    other = _route(7, Layer.F_CU, (2.0, 2.0), (2.0, 8.0))
    committed.append(other)
    assert tracker.on_commit(other, committed) is None
    assert tracker.rebuilds == 1
    assert tracker.ring_count == 0

    # Re-landing the same closing copper closes the loop again, exactly once.
    again = _route(4, Layer.F_CU, *SQUARE[3])
    committed.append(again)
    ev = tracker.on_commit(again, committed)
    assert ev is not None and ev.severed_nets == (9,)
    assert tracker.ring_count == 1


def test_no_stale_weld_after_ripup():
    tracker = _tracker([0], {})
    committed: list[Route] = []
    a = _route(1, Layer.F_CU, (5.0, 5.0), (15.0, 5.0))  # tiles 0 - 1
    b = _route(2, Layer.F_CU, (15.0, 5.0), (25.0, 5.0))  # tiles 1 - 2
    for r in (a, b):
        committed.append(r)
        tracker.on_commit(r, committed)
    committed.remove(a)  # rip up a
    c = _route(3, Layer.F_CU, (5.0, 5.0), (25.0, 5.0))  # tiles 0 - 2
    committed.append(c)
    tracker.on_commit(c, committed)
    # With a gone, tile 0 is not joined to tile 2: c does not close a loop.
    assert tracker.ring_count == 0


def _small_router() -> Autorouter:
    router = Autorouter(width=30.0, height=20.0)
    for i, net in enumerate((1, 2, 3)):
        pads = [
            {
                "number": str(n + 1),
                "x": 4.0 + 8.0 * i,
                "y": 4.0 + 12.0 * n,
                "width": 0.5,
                "height": 0.5,
                "net": net,
                "net_name": f"N{net}",
            }
            for n in range(2)
        ]
        router.add_component(f"U{net}", pads)
    return router


def _geometry(routes: list[Route]):
    return [
        (
            r.net,
            [(s.x1, s.y1, s.x2, s.y2, s.layer.value) for s in r.segments],
            [(v.x, v.y) for v in r.vias],
        )
        for r in routes
    ]


def test_hook_leaves_routing_byte_identical(monkeypatch):
    monkeypatch.delenv(ENV_FLAG, raising=False)
    off = _small_router()
    off_routes = off.route_all_negotiated(max_iterations=2)
    assert getattr(off, "_barrier_closure_tracker", None) is None

    monkeypatch.setenv(ENV_FLAG, "1")
    on = _small_router()
    on_routes = on.route_all_negotiated(max_iterations=2)
    tracker = getattr(on, "_barrier_closure_tracker", None)
    assert tracker is not None, "hook never reached the negotiated commit path"
    assert off_routes, "fixture must route something"
    assert _geometry(on_routes) == _geometry(off_routes)


def test_hook_failure_never_breaks_routing(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")

    def boom(self, route, committed=None):
        raise RuntimeError("diagnostic bug")

    monkeypatch.setattr(BarrierClosureTracker, "on_commit", boom)
    router = _small_router()
    assert router.route_all_negotiated(max_iterations=1)


@pytest.mark.parametrize("value", ["0", "", "off"])
def test_hook_off_for_falsey_values(monkeypatch, value):
    monkeypatch.setenv(ENV_FLAG, value)
    router = _small_router()
    router.route_all_negotiated(max_iterations=1)
    assert getattr(router, "_barrier_closure_tracker", None) is None
