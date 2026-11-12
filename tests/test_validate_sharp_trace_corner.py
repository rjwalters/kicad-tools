"""Tests for the ``sharp_trace_corner`` rule (#8840)."""

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.sharp_trace_corner import SharpTraceCornerRule

pytest.importorskip("shapely")


def _seg(a, b, layer="F.Cu", net=1):
    return (
        f"(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.2) "
        f'(layer "{layer}") (net {net}))'
    )


def _board(*segs: str) -> str:
    return f"""(kicad_pcb (version 20240108) (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "") (net 1 "SIG") (net 2 "OTHER")
  {"".join(segs)}
)
"""


def _check(tmp_path, *segs):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_board(*segs))
    return SharpTraceCornerRule().check(PCB.load(str(p)), None).violations


def test_acute_corner_flagged(tmp_path):
    v = _check(tmp_path, _seg((0, 0), (10, 0)), _seg((10, 0), (0, 3)))
    assert [(x.rule_id, x.severity) for x in v] == [("sharp_trace_corner", "warning")]
    assert v[0].actual_value < 90 and v[0].layer == "F.Cu"


def test_clean_corners_not_flagged(tmp_path):
    # 90 deg, 135 deg and collinear joints are fine.
    assert _check(tmp_path, _seg((0, 0), (10, 0)), _seg((10, 0), (10, 10))) == []
    assert _check(tmp_path, _seg((0, 0), (10, 0)), _seg((10, 0), (20, 10))) == []
    assert _check(tmp_path, _seg((0, 0), (10, 0)), _seg((10, 0), (20, 0))) == []


def test_different_net_or_layer_not_joined(tmp_path):
    assert _check(tmp_path, _seg((0, 0), (10, 0)), _seg((10, 0), (0, 3), net=2)) == []
    assert _check(tmp_path, _seg((0, 0), (10, 0)), _seg((10, 0), (0, 3), layer="B.Cu")) == []


def test_checker_method_registered(tmp_path):
    assert "check_sharp_trace_corner" in DRCChecker.CHECK_ALL_METHODS
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_board(_seg((0, 0), (10, 0)), _seg((10, 0), (0, 3))))
    checker = DRCChecker(PCB.load(str(p)), manufacturer="jlcpcb", layers=2)
    assert checker.check_sharp_trace_corner().violations[0].rule_id == "sharp_trace_corner"
