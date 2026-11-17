"""Bit-exact equivalence for the inlined segment-distance leaf (Issue #5617).

``segment_to_segment_distance`` sits at the bottom of the router's clearance
kernel.  Its body was inlined (helper calls and the ``min``/``max`` clamp
replaced by equivalent conditionals) purely for speed; these tests pin the
result to a verbatim copy of the previous implementation, compared with
``==`` / ``math.isnan`` rather than a tolerance.
"""

from __future__ import annotations

import math
import random

import pytest

from kicad_tools.core.geometry import (
    point_to_segment_distance,
    segment_to_segment_distance,
    segments_intersect,
)


def _ref_segments_intersect(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2):
    def _cross(ox, oy, px, py, qx, qy):
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)

    d1 = _cross(bx1, by1, bx2, by2, ax1, ay1)
    d2 = _cross(bx1, by1, bx2, by2, ax2, ay2)
    d3 = _cross(ax1, ay1, ax2, ay2, bx1, by1)
    d4 = _cross(ax1, ay1, ax2, ay2, bx2, by2)
    return bool(
        ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0))
        and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0))
    )


def _ref_segment_to_segment_distance(x1, y1, x2, y2, x3, y3, x4, y4):
    if _ref_segments_intersect(x1, y1, x2, y2, x3, y3, x4, y4):
        return 0.0
    d1 = point_to_segment_distance(x1, y1, x3, y3, x4, y4)
    d2 = point_to_segment_distance(x2, y2, x3, y3, x4, y4)
    d3 = point_to_segment_distance(x3, y3, x1, y1, x2, y2)
    d4 = point_to_segment_distance(x4, y4, x1, y1, x2, y2)
    return min(d1, d2, d3, d4)


def _same(a: float, b: float) -> bool:
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)


def _cases():
    rng = random.Random(5617)
    for _ in range(20000):
        yield tuple(rng.uniform(-30.0, 30.0) for _ in range(8))
    # Grid-snapped coordinates: lots of collinear / touching / shared-endpoint
    # pairs, the knife-edge inputs for the strict-inequality straddle test.
    for _ in range(20000):
        yield tuple(float(rng.randint(-4, 4)) for _ in range(8))
    # Degenerate (zero-length) segments on either side.
    for _ in range(2000):
        px, py, qx, qy, rx, ry = (rng.uniform(-5, 5) for _ in range(6))
        yield (px, py, px, py, qx, qy, rx, ry)
        yield (qx, qy, rx, ry, px, py, px, py)
        yield (px, py, px, py, qx, qy, qx, qy)


def test_segment_to_segment_distance_is_bit_identical():
    for args in _cases():
        assert _same(segment_to_segment_distance(*args), _ref_segment_to_segment_distance(*args)), (
            args
        )


def test_segments_intersect_is_unchanged():
    for args in _cases():
        assert segments_intersect(*args) == _ref_segments_intersect(*args), args


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_inputs_match_reference(bad):
    args = (0.0, 0.0, 1.0, 1.0, bad, 0.0, 2.0, 2.0)
    assert _same(segment_to_segment_distance(*args), _ref_segment_to_segment_distance(*args))
