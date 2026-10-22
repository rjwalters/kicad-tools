"""Hairpin collapse keeps the 0.5 mm hole-to-hole floor to the partner rail (Issue #7818).

Board 07's MIPI_DAT1 pair left ``kct route`` with its two 0.3 mm vias 0.762 mm
apart (0.462 mm hole to hole).  ``_collapse_committed_via_hairpins`` moved each
rail's via back to its turn, checking only copper clearance, so the second
rail's via landed beside the first rail's already-committed via.
"""

from __future__ import annotations

import itertools
import math

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_routing import DiffPairRouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules

STEP = 0.127
Y = 25.451
PITCH = 0.762
TURN_X = 25.108
DRILL = 0.3


def _seg(x1, y1, x2, y2, layer, net, name):
    return Segment(x1, y1, x2, y2, 0.1, layer, net=net, net_name=name)


def _hairpin_route() -> Route:
    segs = [_seg(24.981, Y, TURN_X, Y, Layer.F_CU, 1, "N1")]
    xs = [TURN_X + STEP * i for i in range(6)]  # 25.108 .. 25.743
    segs += [_seg(a, Y, b, Y, Layer.F_CU, 1, "N1") for a, b in itertools.pairwise(xs)]
    segs += [_seg(b, Y, a, Y, Layer.B_CU, 1, "N1") for a, b in itertools.pairwise(xs)]
    ys = [Y - STEP * i for i in range(13)]
    segs += [_seg(TURN_X, a, TURN_X, b, Layer.B_CU, 1, "N1") for a, b in itertools.pairwise(ys)]
    via = Via(xs[-1], Y, DRILL, 0.6, (Layer.F_CU, Layer.B_CU), net=1, net_name="N1")
    return Route(net=1, net_name="N1", segments=segs, vias=[via])


def _partner_route() -> Route:
    """The other rail: a committed via 0.762 mm beside the turn."""
    py = Y + PITCH
    via = Via(TURN_X, py, DRILL, 0.6, (Layer.F_CU, Layer.B_CU), net=2, net_name="N2")
    seg = _seg(TURN_X, py, TURN_X + 2.0, py, Layer.B_CU, 2, "N2")
    return Route(net=2, net_name="N2", segments=[seg], vias=[via])


def test_collapsed_via_keeps_hole_to_hole_floor_to_partner_via():
    # Copper clearance alone (0.1 mm) admits 0.7 mm centres for 0.6 mm pads;
    # the 0.5 mm drill floor needs 0.8 mm for 0.3 mm drills.
    rules = DesignRules(
        trace_width=0.1, trace_clearance=0.1, via_clearance=0.1, grid_resolution=STEP
    )
    router = Autorouter(width=40.0, height=40.0, rules=rules)
    route, partner = _hairpin_route(), _partner_route()
    for r in (route, partner):
        router.grid.mark_route(r)
        router.routes.append(r)

    DiffPairRouter(router)._collapse_committed_via_hairpins([route])

    via, other = route.vias[0], partner.vias[0]
    edge_gap = math.hypot(via.x - other.x, via.y - other.y) - (via.drill + other.drill) / 2
    assert edge_gap >= rules.min_hole_to_hole - 1e-6
