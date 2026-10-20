"""MIPI_D0's skew fix on board 06 (Issue #7548).

Two reasons the tuner left the pair 2.3 mm over: it measured off-angle teeth
the 45-degree quantizer later lengthens, and its only free stretch was the
last couple of mm of a 16 mm host.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.diffpair_length_tuning import AMPLITUDE_LADDER
from kicad_tools.router.match_group_tuning import _centered_sub_host
from kicad_tools.router.primitives import Segment
from kicad_tools.router.quantize import dogleg_length_extra_mm, dogleg_points


def _host(x2: float = 16.0) -> Segment:
    return Segment(x1=0.0, y1=0.0, x2=x2, y2=0.0, width=0.225, layer=None, net=1, net_name="N")


@pytest.mark.parametrize(
    ("dx", "dy"), [(0.62, 0.30), (-0.62, 0.30), (0.30, 0.62), (3.0, -1.0), (0.5, 0.01)]
)
def test_dogleg_extra_matches_the_dogleg_the_quantizer_writes(dx, dy):
    pts = dogleg_points(0.0, 0.0, dx, dy)
    written = sum(math.dist(a, b) for a, b in zip(pts, pts[1:], strict=False))
    assert dogleg_length_extra_mm(0.0, 0.0, dx, dy) == pytest.approx(written - math.hypot(dx, dy))
    assert dogleg_length_extra_mm(0.0, 0.0, dx, dy) > 0.0


@pytest.mark.parametrize(("dx", "dy"), [(1.0, 0.0), (0.0, -2.0), (1.5, 1.5), (-0.7, 0.7)])
def test_aligned_segment_adds_nothing(dx, dy):
    assert dogleg_length_extra_mm(0.0, 0.0, dx, dy) == 0.0


def test_board_06_tooth_grows_by_the_amount_the_quantizer_adds():
    # MIPI_D0-'s 19-degree tooth (0.62 x 0.30): 0.689 mm becomes 0.745 mm; 24
    # of them are the 1.3 mm the tuner used not to see.
    assert dogleg_length_extra_mm(108.0828, 116.75, 108.7034, 117.05) == pytest.approx(
        0.0555, abs=1e-3
    )


def test_end_anchor_seats_the_trombone_at_the_far_end():
    sub, lead_in, lead_out = _centered_sub_host(
        _host(), loops=1, gap_mm=0.4, min_length_mm=0.0, anchor=1.0
    )
    assert lead_out == []
    assert sub.x2 == pytest.approx(16.0)
    assert lead_in[0].x2 == pytest.approx(sub.x1)
    assert sub.x2 - sub.x1 == pytest.approx(3 * 0.4 / 0.9, abs=1e-3)


def test_centred_is_unchanged():
    sub, lead_in, lead_out = _centered_sub_host(_host(), loops=1, gap_mm=0.4, min_length_mm=2.0)
    assert lead_in[0].x1 == 0.0 and lead_out[0].x2 == 16.0
    assert lead_in[0].x2 - 0.0 == pytest.approx(16.0 - lead_out[0].x1)


def test_no_taller_loops_are_tried():
    assert AMPLITUDE_LADDER == (1.0, 0.5, 0.25)


def test_quantized_route_length_adds_the_dogleg_growth():
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.primitives import Route

    off = Segment(x1=0.0, y1=0.0, x2=0.62, y2=0.30, width=0.2, layer=None, net=1, net_name="N")
    aligned = Segment(
        x1=0.62, y1=0.30, x2=2.62, y2=0.30, width=0.2, layer=None, net=1, net_name="N"
    )
    route = Route(net=1, net_name="N", segments=[off, aligned], vias=[])
    base = Autorouter._quantized_route_length(lambda r: 10.0)
    assert base(route) == pytest.approx(10.0 + dogleg_length_extra_mm(0.0, 0.0, 0.62, 0.30))
