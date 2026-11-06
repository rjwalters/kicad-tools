"""Issue #8488: collinear fusing must not emit off-angle copper.

``merge_collinear_same_width`` accepts joints up to ``tolerance_mm`` (1 um)
off the fused line.  A vertical run with a 1 um jog fused into one ~4 mm
segment, 0.0143 deg off vertical, which ``Segment.to_sexp`` then warned about.
"""

import warnings
from types import SimpleNamespace

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.merge import merge_collinear_same_width
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.quantize import (
    SEGMENT_45_STRICT_ENV,
    OffAngleSegmentWarning,
)


def _seg(x1, y1, x2, y2):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=Layer.F_CU, net=1, net_name="SENSE")


def _skewed_route() -> Route:
    # Vertical, a 1 um 45-degree jog, vertical: every segment is 45-legal.
    return Route(
        net=1,
        net_name="SENSE",
        segments=[
            _seg(99.5865, 134.9810, 99.5865, 137.0),
            _seg(99.5865, 137.0, 99.5875, 137.001),
            _seg(99.5875, 137.001, 99.5875, 139.0),
        ],
        vias=[],
    )


def test_fuse_does_not_emit_off_angle_segment(monkeypatch):
    monkeypatch.setenv(SEGMENT_45_STRICT_ENV, "1")
    route = _skewed_route()
    merge_collinear_same_width(SimpleNamespace(routes=[route]))
    with warnings.catch_warnings():
        warnings.simplefilter("error", OffAngleSegmentWarning)
        for seg in route.segments:
            seg.to_sexp()  # strict mode raises OffAngleSegmentError if skewed


def test_exact_vertical_chain_still_fuses():
    route = Route(
        net=1,
        net_name="SENSE",
        segments=[_seg(1.0, 0.0, 1.0, 1.0), _seg(1.0, 1.0, 1.0, 2.0)],
        vias=[],
    )
    assert merge_collinear_same_width(SimpleNamespace(routes=[route])) == 1
