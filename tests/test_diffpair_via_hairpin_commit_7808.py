"""Via-hairpin collapse on the real commit path (Issue #7808).

PR #7784's collapse checked a new via site against foreign copper only
(``worst_via_pad_deficit`` skips same-net pads).  On board 07 the DQS_P turn
sits on the net's own U2 land, so the collapse put the via there, and
``kct route``'s post-route via-in-pad sweep (``drc_verify_and_nudge``) moved
it again and re-grew a doubled stub: DQS skew went from 0.119 mm to
1.291 mm.  Where no via site is legal the doubled leg also stayed, and its
four-end turn junction kept the kicad-cli length measure order-dependent.

These tests drive a real :class:`Autorouter` and :class:`DiffPairRouter` --
real pads, real grid validators -- and then the real post-route
``drc_verify_and_nudge`` pass, instead of the stubbed router of
``test_diffpair_via_hairpin_7782.py``.
"""

from __future__ import annotations

import itertools
import math
import random

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_routing import DiffPairRouter
from kicad_tools.router.drc_nudge import (
    _router_pad_bbox,
    _via_drill_overlaps_bbox,
    drc_verify_and_nudge,
)
from kicad_tools.router.kicad_length import LSeg, LVia, net_length
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules

STEP = 0.127
Y = 25.451
NET = 1


def _seg(x1, y1, x2, y2, layer):
    return Segment(x1, y1, x2, y2, 0.1, layer, net=NET, net_name="N1")


def _pad(number, x, y, size):
    return {
        "number": number,
        "x": x,
        "y": y,
        "width": size,
        "height": size,
        "net": NET,
        "net_name": "N1",
        "layer": Layer.F_CU,
    }


def _hairpin_route() -> Route:
    """Board 07's DQS_P near end: F.Cu runs past the turn to the via, B.Cu returns."""
    segs = [_seg(24.981, Y, 25.108, Y, Layer.F_CU)]
    xs = [25.108 + STEP * i for i in range(6)]  # 25.108 .. 25.743
    segs += [_seg(a, Y, b, Y, Layer.F_CU) for a, b in itertools.pairwise(xs)]
    segs += [_seg(b, Y, a, Y, Layer.B_CU) for a, b in itertools.pairwise(xs)]
    ys = [Y - STEP * i for i in range(13)]  # 25.451 .. 23.927
    segs += [_seg(25.108, a, 25.108, b, Layer.B_CU) for a, b in itertools.pairwise(ys)]
    segs.append(_seg(25.108, ys[-1], 30.0, ys[-1], Layer.B_CU))
    via = Via(25.743, Y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=NET, net_name="N1")
    return Route(net=NET, net_name="N1", segments=segs, vias=[via])


def _router(pad_size: float) -> tuple[Autorouter, Route]:
    rules = DesignRules(trace_width=0.1, trace_clearance=0.1, grid_resolution=STEP)
    router = Autorouter(width=40.0, height=40.0, rules=rules)
    # The near pad sits under the hairpin's turn, as U2.6 does on board 07.
    router.add_component("U1", [_pad("1", 24.981, Y, pad_size)])
    router.add_component("U2", [_pad("1", 30.0, Y - 12 * STEP, 0.4)])
    router.net_names[NET] = "N1"
    route = _hairpin_route()
    router.grid.mark_route(route)
    router.routes.append(route)
    return router, route


def _ends(seg: Segment) -> tuple[tuple[float, float], tuple[float, float]]:
    return (seg.x1, seg.y1), (seg.x2, seg.y2)


def _doubled(route: Route) -> float:
    """Copper of one layer lying collinearly over the other layer's copper."""
    total = 0.0
    for a, b in itertools.combinations(route.segments, 2):
        if a.layer == b.layer:
            continue
        length = math.hypot(a.x2 - a.x1, a.y2 - a.y1)
        if length < 1e-9:
            continue
        ux, uy = (a.x2 - a.x1) / length, (a.y2 - a.y1) / length
        if any(abs((px - a.x1) * uy - (py - a.y1) * ux) > 1e-6 for px, py in _ends(b)):
            continue
        ts = sorted(((px - a.x1) * ux + (py - a.y1) * uy) for px, py in _ends(b))
        total += max(0.0, min(length, ts[1]) - max(0.0, ts[0]))
    return total


def _lengths(route: Route) -> set[float]:
    vias = [LVia(v.x, v.y, v.diameter, ("F.Cu", "B.Cu")) for v in route.vias]
    rng = random.Random(7808)
    out = set()
    for _ in range(24):
        segs = list(route.segments)
        rng.shuffle(segs)
        lsegs = [LSeg(s.x1, s.y1, s.x2, s.y2, s.layer.kicad_name) for s in segs]
        out.add(round(net_length(lsegs, vias, [], None), 6))
    return out


def _own_pad_overlap(router: Autorouter, via: Via) -> bool:
    return any(
        _via_drill_overlaps_bbox(via, _router_pad_bbox(pad))
        for pad in router.pads.values()
        if pad.net == via.net
    )


def test_collapse_never_lands_the_via_on_its_own_pad():
    router, route = _router(pad_size=0.4)
    DiffPairRouter(router)._collapse_committed_via_hairpins([route])
    via = route.vias[0]
    assert not _own_pad_overlap(router, via)
    at = (via.x, via.y)

    # The post-route via-in-pad sweep finds nothing to move, so it cannot
    # re-grow the doubled stub after the pair's skew was tuned.
    drc_verify_and_nudge(router)
    assert (route.vias[0].x, route.vias[0].y) == at
    assert _doubled(route) < 1e-6
    assert len(_lengths(route)) == 1


def test_unmovable_hairpin_loses_its_doubled_leg():
    """Every via site is illegal: the doubled B.Cu leg is shortcut instead."""
    router, route = _router(pad_size=1.6)  # the land covers every site to the turn
    before = _doubled(route)
    assert before > 0.6
    DiffPairRouter(router)._collapse_committed_via_hairpins([route])
    assert (route.vias[0].x, route.vias[0].y) == (25.743, Y)
    assert _doubled(route) < 1e-6
    assert len(_lengths(route)) == 1
    # Still one connected net: the via's B.Cu end reaches the far pad.
    b_ends = {
        (round(x, 3), round(y, 3))
        for s in route.segments
        if s.layer == Layer.B_CU
        for x, y in _ends(s)
    }
    assert (25.743, Y) in b_ends
    assert (30.0, round(Y - 12 * STEP, 3)) in b_ends
