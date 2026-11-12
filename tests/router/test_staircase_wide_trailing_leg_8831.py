"""A wide staircase with a trailing V leg compresses to <= 2 segments (Issue #8831)."""

from __future__ import annotations

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import OptimizationConfig
from kicad_tools.router.optimizer.algorithms import compress_staircase
from kicad_tools.router.primitives import Segment

PTS = [(0, 0), (4, 0), (6, 2), (10, 2), (12, 4), (16, 4), (18, 6), (22, 6), (22, 8), (24, 10)]


def _segs(pts, width=2.6):
    return [
        Segment(
            x1=a[0], y1=a[1], x2=b[0], y2=b[1], width=width, layer=Layer.F_CU, net=1, net_name="P"
        )
        for a, b in zip(pts, pts[1:], strict=False)
    ]


def test_wide_staircase_with_trailing_v_leg_reduces_to_two_segments():
    out = compress_staircase(_segs(PTS), OptimizationConfig())
    assert len(out) <= 2
    assert (out[0].x1, out[0].y1) == (0, 0)
    assert (out[-1].x2, out[-1].y2) == (24, 10)


def test_falls_back_to_strict_run_when_longer_path_blocked():
    # Reject any path ending at the final point: only the strict run compresses.
    def clear(seg):
        return not (abs(seg.x2 - 24) < 1e-6 and abs(seg.y2 - 10) < 1e-6)

    out = compress_staircase(_segs(PTS), OptimizationConfig(), clear)
    assert len(out) < 9
    assert (out[-1].x2, out[-1].y2) == (24, 10)
