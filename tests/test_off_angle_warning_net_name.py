"""Issue #8219: off-angle warning names the net and is not repeated."""

from __future__ import annotations

import warnings

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.pcb import replace_segments
from kicad_tools.router.primitives import Segment
from kicad_tools.router.quantize import OffAngleSegmentError, OffAngleSegmentWarning


def _seg(net_name: str = "GND") -> Segment:
    return Segment(
        x1=0.0, y1=0.0, x2=10.0, y2=3.0, width=0.2, layer=Layer.F_CU, net=0, net_name=net_name
    )


def _off_angle(w: list) -> list:
    return [x for x in w if issubclass(x.category, OffAngleSegmentWarning)]


def test_warning_uses_net_name():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _seg().to_sexp()
    msgs = [str(x.message) for x in _off_angle(w)]
    assert len(msgs) == 1
    assert "net GND on F.Cu" in msgs[0]


def test_unnamed_net_falls_back_to_id():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _seg("").to_sexp()
    assert "net 0 on F.Cu" in str(_off_angle(w)[0].message)


def test_strict_error_uses_same_context(monkeypatch):
    from kicad_tools.router.quantize import SEGMENT_45_STRICT_ENV

    monkeypatch.setenv(SEGMENT_45_STRICT_ENV, "1")
    with pytest.raises(OffAngleSegmentError, match="net GND on F.Cu"):
        _seg().to_sexp()


def test_scratch_replace_segments_does_not_warn():
    pcb = "(kicad_pcb\n)"
    segs = {"GND": [_seg()]}
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        replace_segments(pcb, {}, segs, scratch=True)
        replace_segments(pcb, {}, segs, scratch=True)
        assert _off_angle(w) == []
        replace_segments(pcb, {}, segs)
    assert len(_off_angle(w)) == 1
