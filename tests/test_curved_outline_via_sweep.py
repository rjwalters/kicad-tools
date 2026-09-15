"""Production via repair must respect the authored circle, not just its chords."""

import math

import pytest

from kicad_tools.core.board_outline import circle_segment_count
from kicad_tools.router.core import Autorouter
from kicad_tools.router.drc_nudge import drc_verify_and_nudge
from kicad_tools.router.io import _extract_edge_segments
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Via
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("margin, accepted", [(0.0, False), (0.01, True)])
def test_real_via_repair_respects_circle_error(margin, accepted):
    router = Autorouter(
        width=12, height=12, rules=DesignRules(trace_clearance=0.2, manufacturer="pcbway")
    )
    router.add_component(
        "A",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": 5.0,
                "width": 1.0,
                "height": 1.0,
                "net": 2,
                "net_name": "A",
                "layer": Layer.F_CU,
            }
        ],
    )
    via = Via(x=4.5, y=5.0, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=1)
    router.routes = [Route(net=1, net_name="SIG", vias=[via])]
    radius, clearance = 1.0, 0.05
    angle = math.pi / circle_segment_count(radius)
    sagitta = radius * (1 - math.cos(angle))
    radial = radius + via.diameter / 2 + clearance - 1e-6 - sagitta / 2 + margin
    center = (3.98 - radial * math.cos(angle), 5 - radial * math.sin(angle))
    text = (
        f"(kicad_pcb (gr_circle (center {center[0]} {center[1]}) "
        f'(end {center[0] + radius} {center[1]}) (layer "Edge.Cuts")))'
    )
    router._edge_segments = _extract_edge_segments(text)
    router._edge_clearance = clearance
    router.grid.add_edge_keepout(router._edge_segments, clearance)
    drc_verify_and_nudge(router, max_passes=1)
    if accepted:
        assert (via.x, via.y) == (3.98, 5.0)
        assert math.dist((via.x, via.y), center) - radius - via.diameter / 2 >= clearance
    else:
        assert (via.x, via.y) == (4.5, 5.0)


def test_swept_error_cannot_cancel_old_error():
    from types import SimpleNamespace

    from kicad_tools.core.board_outline import OutlineSegments
    from kicad_tools.router.drc_nudge import _via_edge_sweep_clear

    radius, count, offset = 5.0, 1571, 0.2
    half_angle = math.pi / count
    x, y = radius * math.sin(half_angle), radius * math.cos(half_angle)
    points = [
        (
            radius * math.sin(-half_angle + 2 * math.pi * i / count),
            radius * math.cos(-half_angle + 2 * math.pi * i / count),
        )
        for i in range(count)
    ]
    edges = OutlineSegments(
        list(zip(points, points[1:] + points[:1], strict=False)), max_error_mm=1e-5
    )
    via = Via(x=0, y=y + offset, diameter=0.4, drill=0.2, layers=(Layer.F_CU, Layer.B_CU), net=1)
    router = SimpleNamespace(_edge_segments=edges, _edge_clearance=0.1)
    assert radius - y < edges.max_error_mm
    assert math.hypot(-x, y + offset) - (y + offset) > 1e-6
    assert not _via_edge_sweep_clear(-x, y + offset, via, router)


@pytest.mark.parametrize("end_y, expected", [(0.2, True), (0.19, False)])
def test_straight_edge_retains_existing_below_floor_semantics(end_y, expected):
    from types import SimpleNamespace

    from kicad_tools.router.drc_nudge import _via_edge_sweep_clear

    via = Via(x=1, y=end_y, diameter=0.4, drill=0.2, layers=(Layer.F_CU, Layer.B_CU), net=1)
    router = SimpleNamespace(_edge_segments=[((-2, 0), (2, 0))], _edge_clearance=0.1)
    assert _via_edge_sweep_clear(0, 0.2, via, router) is expected
