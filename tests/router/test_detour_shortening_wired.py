"""`kct route` wires the detour-shortening pass to a real board model (#8217).

Real ``Autorouter`` + grid collision checker (not ``MockCollisionChecker``):
a clear tent collapses to one segment, a foreign trace across the straight
path or a mid-chain same-net pad blocks it, and length-matched nets are skipped.
"""

import argparse

import pytest

from kicad_tools.cli.route_cmd import _detour_net_anchors, _optimize_traces
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment

# Tent: climb 45deg, run flat, descend 45deg between (2,10) and (22,10).
TENT = [(2, 10), (6, 14), (18, 14), (22, 10)]


def _seg(a, b, net, name):
    return Segment(a[0], a[1], b[0], b[1], 0.2, Layer.F_CU, net=net, net_name=name)


def _route(points, net=1, name="NET1") -> Route:
    segs = [_seg(a, b, net, name) for a, b in zip(points, points[1:], strict=False)]
    return Route(net=net, net_name=name, segments=segs)


def _pad(router, ref, xy, net, name):
    router.add_component(
        ref,
        [
            {
                "number": "1",
                "x": xy[0],
                "y": xy[1],
                "width": 0.6,
                "height": 0.6,
                "net": net,
                "net_name": name,
            }
        ],
    )


def _board(extra_pads=(), extra_routes=(), tent=TENT) -> Autorouter:
    router = Autorouter(30, 30, force_python=True, physics_enabled=False)
    _pad(router, "R1", tent[0], 1, "NET1")
    _pad(router, "R2", tent[-1], 1, "NET1")
    for ref, xy, net, name in extra_pads:
        _pad(router, ref, xy, net, name)
    routes = [_route(tent), *extra_routes]
    for route in routes:
        router.grid.mark_route(route)
    router.routes = routes
    return router


def _run(router):
    args = argparse.Namespace(quiet=True)
    _optimize_traces(router, args, quiet=True)
    return {r.net: r for r in router.routes}


def _length(route):
    return sum(((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) ** 0.5 for s in route.segments)


def test_clear_tent_becomes_shorter_path():
    router = _board()
    before = _length(router.routes[0])
    out = _run(router)[1]
    assert len(out.segments) < 3
    assert _length(out) < before - 1.0
    assert (out.segments[0].x1, out.segments[0].y1) == (2, 10)
    assert (out.segments[-1].x2, out.segments[-1].y2) == (22, 10)


def test_foreign_trace_across_straight_path_blocks_it():
    # GND spans y=6..12 at x=12: blocks the straight y=10 path, not the tent (y=14).
    gnd = _route([(12, 6), (12, 12)], net=2, name="GND")
    router = _board(
        extra_pads=[("C1", (12, 6), 2, "GND"), ("C2", (12, 12), 2, "GND")],
        extra_routes=[gnd],
    )
    before = [(s.x1, s.y1, s.x2, s.y2) for s in router.routes[0].segments]
    out = _run(router)[1]
    assert [(s.x1, s.y1, s.x2, s.y2) for s in out.segments] == before


def test_mid_chain_same_net_pad_stays_connected():
    router = _board(extra_pads=[("R3", (12, 14), 1, "NET1")])
    out = _run(router)[1]

    # The mid-chain pad at (12,14) must still lie on the copper.
    def on_copper(px, py):
        for s in out.segments:
            dx, dy = s.x2 - s.x1, s.y2 - s.y1
            n = dx * dx + dy * dy
            t = 0 if n == 0 else max(0, min(1, ((px - s.x1) * dx + (py - s.y1) * dy) / n))
            if ((px - s.x1 - t * dx) ** 2 + (py - s.y1 - t * dy) ** 2) ** 0.5 <= 0.2:
                return True
        return False

    assert on_copper(12, 14)


def test_length_matched_net_excluded_from_anchors():
    router = _board()

    class _NC:
        def effective_length_match_group(self):
            return "grp"

    router.net_class_map = {"NET1": _NC()}
    router.net_names = {1: "NET1"}
    assert 1 not in _detour_net_anchors(router)
    assert 1 in _detour_net_anchors(_board())


@pytest.mark.parametrize("net", [1])
def test_anchors_include_pads(net):
    anchors = _detour_net_anchors(_board())
    assert (2, 10) in anchors[net] and (22, 10) in anchors[net]
