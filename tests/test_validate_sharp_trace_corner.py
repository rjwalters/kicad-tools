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


def test_zero_length_segment_artifact_not_flagged(tmp_path):
    # A 0.0004 mm stub makes start~start match first; the true bend is 135 deg.
    v = _check(
        tmp_path,
        _seg((17.775, 33.0), (17.775, 32.9996)),
        _seg((17.775, 32.9996), (17.139, 32.3636)),
    )
    assert v == []


def test_vertex_buried_under_same_net_pad_not_flagged(tmp_path):
    pad = (
        '(footprint "Test:FP" (layer "F.Cu") (uuid "fp") (at 10 0)\n'
        '  (property "Reference" "C1" (at 0 -2 0) (layer "F.SilkS"))\n'
        '  (pad "1" smd rect (at 0 0) (size 1.6 1.6) (layers "F.Cu") (net 1 "SIG")))\n'
    )
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_board(_seg((0, 0), (10, 0)), _seg((10, 0), (0, 3)), pad))
    assert SharpTraceCornerRule().check(PCB.load(str(p)), None).violations == []


def _long_pad(rotation: int = 0) -> str:
    # A 1.475 x 0.3 mm LQFP-style land: its inscribed circle (r 0.15) is far
    # smaller than its copper.
    return (
        f'(footprint "Test:FP" (layer "F.Cu") (uuid "fp") (at 10 0 {rotation})\n'
        '  (property "Reference" "U1" (at 0 -2 0) (layer "F.SilkS"))\n'
        f'  (pad "1" smd rect (at 0 0 {rotation}) (size 1.475 0.3) (layers "F.Cu") '
        '(net 1 "SIG")))\n'
    )


@pytest.mark.parametrize("rotation", [0, 90])
def test_vertex_inside_elongated_pad_off_centre_not_flagged(tmp_path, rotation):
    # Issue #9037: a vertex 0.5 mm along an elongated pad lies under pad copper
    # but outside its inscribed circle; the pad fills the wedge.
    for offset in (0.5, -0.5):
        vx, vy = (10 + offset, 0) if rotation == 0 else (10, offset)
        p = tmp_path / f"b{rotation}{offset}.kicad_pcb"
        p.write_text(
            _board(_seg((0, vy), (vx, vy)), _seg((vx, vy), (0, vy + 3)), _long_pad(rotation))
        )
        assert SharpTraceCornerRule().check(PCB.load(str(p)), None).violations == [], offset


@pytest.mark.parametrize("rotation", [0, 90])
def test_vertex_just_outside_elongated_pad_still_flagged(tmp_path, rotation):
    # Beyond the pad's end (half-length 0.7375 mm) the wedge is open copper.
    vx, vy = (10.9, 0) if rotation == 0 else (10, 0.9)
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_board(_seg((0, vy), (vx, vy)), _seg((vx, vy), (0, vy + 3)), _long_pad(rotation)))
    v = SharpTraceCornerRule().check(PCB.load(str(p)), None).violations
    assert [x.rule_id for x in v] == ["sharp_trace_corner"]


def test_vertex_inside_other_net_pad_still_flagged(tmp_path):
    pad = _long_pad().replace('(net 1 "SIG")', '(net 2 "OTHER")')
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_board(_seg((0, 0), (10.5, 0)), _seg((10.5, 0), (0, 3)), pad))
    assert [x.rule_id for x in SharpTraceCornerRule().check(PCB.load(str(p)), None).violations] == [
        "sharp_trace_corner"
    ]


def _wseg(a, b, width, layer="F.Cu", net=1):
    return (
        f"(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) "
        f'(layer "{layer}") (net {net}))'
    )


def test_vertex_at_through_via_on_inner_layer_not_flagged(tmp_path):
    via = '(via (at 10 0) (size 0.65) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))'
    p = tmp_path / "b.kicad_pcb"
    p.write_text(
        _board(
            _seg((0, 0), (10, 0), layer="B.Cu"),
            _seg((10, 0), (0, 3), layer="B.Cu"),
            via,
        )
    )
    assert SharpTraceCornerRule().check(PCB.load(str(p)), None).violations == []


def test_width_filled_hairpin_not_flagged(tmp_path):
    # Out and back 0.08 mm apart with 0.18 mm traces: the copper is solid.
    v = _check(
        tmp_path,
        _wseg((0, 31.72), (10, 31.72), 0.18),
        _wseg((10, 31.72), (8, 31.80), 0.18),
    )
    assert v == []


def test_thin_trace_inside_wide_trace_not_flagged(tmp_path):
    v = _check(
        tmp_path,
        _wseg((0, 0), (10, 0), 2.0),
        _wseg((10, 0), (6, 0.9), 0.25),
    )
    assert v == []


def test_stacked_duplicate_segments_reported_once(tmp_path):
    v = _check(
        tmp_path,
        _seg((0, 0), (10, 0)),
        _seg((0, 0), (10, 0)),
        _seg((10, 0), (0, 3)),
        _seg((10, 0), (0, 3)),
    )
    assert len(v) == 1


# The three vertices JLC's DFM tool flagged as "Sharp trace corner" on the
# consumer board behind epic #8838, posted on issue #8840 relative to the
# vertex: (width1, length1, heading1, width2, length2, heading2, layer).
# Headings are degrees in KiCad axes, pointing away from the vertex.  These
# are known fab positives, so the rule must report every one of them.  The
# eight unflagged look-alikes on that board are deliberately NOT encoded as
# negatives: the issue records them as unknown, not as clean.
JLC_FLAGGED_VERTICES = [
    (0.20, 0.40, 90, 0.20, 0.57, 45, "F.Cu"),
    (0.20, 0.80, -90, 0.30, 1.41, -135, "F.Cu"),
    (0.30, 2.20, -90, 0.20, 1.70, -135, "B.Cu"),
]


@pytest.mark.parametrize("vertex", JLC_FLAGGED_VERTICES)
def test_jlc_flagged_fold_backs_are_reported(tmp_path, vertex):
    import math

    w1, l1, h1, w2, l2, h2, layer = vertex
    o = (20.0, 20.0)

    def far(length, heading):
        r = math.radians(heading)
        return (round(o[0] + length * math.cos(r), 4), round(o[1] + length * math.sin(r), 4))

    v = _check(
        tmp_path,
        _wseg(far(l1, h1), o, w1, layer=layer),
        _wseg(o, far(l2, h2), w2, layer=layer),
    )
    assert len(v) == 1
    assert v[0].layer == layer
    assert v[0].actual_value == pytest.approx(45.0, abs=0.5)
