"""Board-level ``gr_text`` rotation is honoured by silk rules (Issue #8724)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules import silkscreen as silk  # noqa: E402

_TEMPLATE = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user) (44 "Edge.Cuts" user))
  (net 0 "")
  (gr_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.1) (type default))
    (fill none) (layer "Edge.Cuts"))
  (footprint "Test:P" (layer "F.Cu") (at {px} {py})
    (property "Reference" "P1" (at 0 -3) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste")))
  (gr_text "ABCD" (at {tx} {ty} {angle}) (layer "F.SilkS")
    (effects (font (size 1 1) (thickness 0.15))))
)
"""


def _load(tmp_path: Path, angle: float, origin: float = 0.0) -> PCB:
    # Pad at (origin+20, origin+20); 4-char text 1.5 mm to its right.  Upright
    # the box is ~2.95 wide x 1.15 tall (overlaps the pad); at 90/270 it is
    # 1.15 wide x 2.95 tall, so its near edge (~0.92 mm) clears the pad edge.
    o = origin
    path = tmp_path / f"b_{int(angle)}_{int(o)}.kicad_pcb"
    path.write_text(
        _TEMPLATE.format(
            x0=o,
            y0=o,
            x1=o + 40,
            y1=o + 40,
            px=o + 20,
            py=o + 20,
            tx=o + 21.5,
            ty=o + 20,
            angle=angle,
        )
    )
    return PCB.load(path)


def _over_copper(pcb: PCB) -> list:
    rules = get_profile("jlcpcb-tier1").get_design_rules(layers=2)
    return [
        v
        for v in silk.check_silk_over_copper(pcb, rules).violations
        if v.rule_id == "silk_over_copper"
    ]


@pytest.mark.parametrize("origin", [0.0, 100.0])
@pytest.mark.parametrize(
    ("angle", "expect_finding"), [(0, True), (180, True), (90, False), (270, False)]
)
def test_gr_text_rotation_changes_silk_over_copper(tmp_path, angle, expect_finding, origin):
    found = _over_copper(_load(tmp_path, angle, origin))
    assert bool(found) is expect_finding


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_gr_text_bbox_extents_follow_rotation(tmp_path, angle):
    pcb = _load(tmp_path, angle)
    (_, geom, *_rest) = next(e for e in silk._iter_silk_elements(pcb) if e[2] == "ABCD")
    minx, miny, maxx, maxy = geom.bounds
    w, h = maxx - minx, maxy - miny
    long_, short = 4 * silk._TEXT_CHAR_WIDTH_FACTOR + 0.15, 1.15
    if angle in (90, 270):
        assert (w, h) == pytest.approx((short, long_))
    else:
        assert (w, h) == pytest.approx((long_, short))


@pytest.mark.parametrize("angle", [0, 90])
def test_edge_iterator_follows_rotation(tmp_path, angle):
    pcb = _load(tmp_path, angle)
    geom = next(e[0] for e in silk._iter_board_frame_texts(pcb) if e[1] == "ABCD")
    minx, miny, maxx, maxy = geom.bounds
    assert ((maxx - minx) > (maxy - miny)) is (angle == 0)
