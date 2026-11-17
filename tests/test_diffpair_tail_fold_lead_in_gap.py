"""Tail-fold lead-in end gap is held to the own-copper clearance (issue #7943)."""

from __future__ import annotations

import math

from kicad_tools.router.diffpair_own_copper import OwnCopperRule
from kicad_tools.router.diffpair_tail_fold import tail_fold_options
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.serpentine import SerpentineConfig
from kicad_tools.router.primitives import Route, Segment

W = 0.1
CLEARANCE = 0.4


def _route(net: int, y: float, x2: float = 10.0) -> Route:
    seg = Segment(0.0, y, x2, y, W, Layer.F_CU, net=net, net_name=f"N{net}")
    return Route(net=net, net_name=f"N{net}", segments=[seg], vias=[])


def _options(own_copper):
    return list(
        tail_fold_options(
            route=_route(1, 0.0),
            partner=_route(2, -0.3, x2=14.0),
            length_needed=3.0,
            config=SerpentineConfig(amplitude=0.6, corner_chamfer_widths=0.15),
            ladder=(),
            own_copper=own_copper,
        )
    )


def test_lead_in_gap_meets_clearance_without_pad():
    options = _options(OwnCopperRule({}, CLEARANCE))
    assert options, "fold should still fit on a 10 mm host"
    for cand, fold in options:
        tx, ty = fold.terminal
        # Lead-in: the host-line segment that does not touch the terminal.
        lead = [
            seg
            for seg in cand.segments
            if abs(seg.y1 - ty) < 1e-9
            and abs(seg.y2 - ty) < 1e-9
            and min(math.hypot(seg.x1 - tx, seg.y1 - ty), math.hypot(seg.x2 - tx, seg.y2 - ty))
            > 1e-6
        ]
        assert len(lead) == 1
        gap = min(abs(lead[0].x1 - tx), abs(lead[0].x2 - tx)) - W
        assert gap >= CLEARANCE - 1e-9
