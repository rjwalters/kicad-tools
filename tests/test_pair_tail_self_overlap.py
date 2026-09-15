"""A rejected tail must not hide a later physically valid completion."""

import math
import time

import pytest

from kicad_tools.router.construction_validation import route_topology_issue
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting


@pytest.mark.parametrize("gap", [0.1, 0.3])
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_tail_skips_parallel_copper_overlap_before_selecting_winner(angle, gap):
    rules = DesignRules(grid_resolution=0.1, trace_width=0.15, trace_clearance=0.15)
    auto = Autorouter(width=40, height=40, rules=rules)
    finder = CoupledPathfinder(
        auto.grid,
        rules,
        target_spacing_cells=4,
        min_spacing_cells=3,
        net_class_map={"P": NetClassRouting(name="P", trace_width=0.15, clearance=0.15)},
    )

    def point(x, y):
        a = math.radians(angle)
        return (20 + x * math.cos(a) - y * math.sin(a), 20 + x * math.sin(a) + y * math.cos(a))

    def pad(xy):
        return Pad(*xy, 0.3, 0.3, 1, "P", layer=Layer.F_CU)

    points = [point(x, y) for x, y in [(0, 0), (4, 0), (4, -2), (-2, -2), (-2, gap), (-1, gap)]]
    body = [
        Segment(*a, *b, 0.15, Layer.F_CU, 1, net_name="P")
        for a, b in zip(points[:-1], points[1:], strict=True)
    ]
    goal = pad(point(5, gap))
    head = pad(points[-1])
    # The straight candidate has simple centerlines but its 0.15mm copper
    # overlaps the earlier run at 0.1mm; the 0.3mm control remains legal.
    direct = Segment(head.x, head.y, goal.x, goal.y, 0.15, Layer.F_CU, 1, net_name="P")
    assert route_topology_issue(
        Route(net=1, net_name="P", segments=body + [direct]),
        pad(points[0]),
        goal,
        deadline=time.monotonic() + 5,
    ) == ("self_overlap" if gap == 0.1 else None)
    tail = auto._diffpair._synthesize_tail(
        finder,
        head,
        goal,
        0,
        preceding_segments=body,
    )
    assert tail is not None
    if gap == 0.3:
        assert tail.segments == [direct]
    assert (
        route_topology_issue(
            Route(net=1, net_name="P", segments=body + tail.segments),
            pad(points[0]),
            goal,
            deadline=time.monotonic() + 5,
        )
        is None
    )
