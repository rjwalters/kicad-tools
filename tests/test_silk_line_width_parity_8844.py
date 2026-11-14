"""Silk line width vs JLCDFM's "Silkscreen line width" category (Issue #8844).

JLCDFM reported 63 line-width warnings on the epic #8838 board while
``kct check --mfr jlcpcb`` reported none.  The board's own gerbers have no silk
stroke below 0.150 mm (every front-silk draw uses one 0.150 mm aperture), and
JLCPCB publishes ">= 0.15 mm" as the minimum line width.  So the gap is the
fab tool's warning threshold sitting *above* the published floor -- not a silk
primitive kct skips and not a zero-width stroke.  These tests pin each half of
that:

* every silk graphic primitive type is measured (a 0.149 mm one is flagged),
* the published floor is inclusive (0.150 mm passes),
* a zero-width *filled* polygon is not a line-width finding (it has no stroke).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.silkscreen import (
    check_silkscreen_line_width,
    check_silkscreen_text_thickness,
)

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (44 "Edge.Cuts" user))
  (net 0 "")
  (setup (tenting front back))
"""


def _stroke(width: float) -> str:
    return f"(stroke (width {width}) (type solid))"


# One template per silk graphic primitive kct parses; ``{w}`` is the stroke.
_FP_PRIMITIVES = {
    "line": '(fp_line (start -2 -2) (end 2 -2) {s} (layer "F.SilkS"))',
    "rect": '(fp_rect (start -2 -2) (end 2 2) {s} (fill none) (layer "F.SilkS"))',
    "circle": '(fp_circle (center 0 0) (end 2 0) {s} (fill none) (layer "F.SilkS"))',
    "arc": '(fp_arc (start -2 0) (mid 0 -2) (end 2 0) {s} (layer "F.SilkS"))',
    "poly": ('(fp_poly (pts (xy -2 3) (xy 2 3) (xy 0 4)) {s} (fill none) (layer "F.SilkS"))'),
}
_GR_PRIMITIVES = {
    "line": '(gr_line (start 20 20) (end 24 20) {s} (layer "B.SilkS"))',
    "rect": '(gr_rect (start 20 20) (end 24 24) {s} (fill none) (layer "B.SilkS"))',
    "circle": '(gr_circle (center 22 22) (end 24 22) {s} (fill none) (layer "B.SilkS"))',
    "arc": '(gr_arc (start 20 22) (mid 22 20) (end 24 22) {s} (layer "B.SilkS"))',
    "poly": '(gr_poly (pts (xy 20 25) (xy 24 25) (xy 22 26)) {s} (fill none) (layer "B.SilkS"))',
}


def _footprint(silk: str) -> str:
    return f"""  (footprint "X:Y" (layer "F.Cu") (at 10 10)
    (property "Reference" "U1" (at 0 -4) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    {silk}
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste")))
"""


def _load(tmp_path: Path, body: str) -> PCB:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_HEAD + body + ")\n")
    return PCB.load(path)


def _rules():
    return get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=2)


def test_jlcpcb_floor_is_the_published_minimum():
    # https://jlcpcb.com/capabilities/pcb-capabilities, "Legend" -> "Minimum
    # Line Width >= 0.15mm" (retrieved 2026-11-14).
    assert _rules().min_silkscreen_width_mm == pytest.approx(0.15)


@pytest.mark.parametrize("kind", sorted(_FP_PRIMITIVES))
def test_every_footprint_silk_primitive_below_floor_is_flagged(tmp_path, kind):
    pcb = _load(tmp_path, _footprint(_FP_PRIMITIVES[kind].format(s=_stroke(0.149))))
    found = check_silkscreen_line_width(pcb, _rules()).violations
    assert [v.rule_id for v in found] == ["silkscreen_line_width"]
    assert f"fp_{kind}@" in found[0].items[1]


@pytest.mark.parametrize("kind", sorted(_GR_PRIMITIVES))
def test_every_board_silk_primitive_below_floor_is_flagged(tmp_path, kind):
    pcb = _load(tmp_path, _GR_PRIMITIVES[kind].format(s=_stroke(0.149)) + "\n")
    found = check_silkscreen_line_width(pcb, _rules()).violations
    assert [v.rule_id for v in found] == ["silkscreen_line_width"]
    assert found[0].items[0].startswith(f"gr_{kind}")


def test_strokes_at_exactly_the_floor_pass(tmp_path):
    """The epic board's case: every primitive at 0.150 mm -> no finding."""
    silk = "\n    ".join(t.format(s=_stroke(0.15)) for t in _FP_PRIMITIVES.values())
    board = "\n".join(t.format(s=_stroke(0.15)) for t in _GR_PRIMITIVES.values())
    pcb = _load(tmp_path, _footprint(silk) + board + "\n")
    assert not check_silkscreen_line_width(pcb, _rules()).violations


def test_zero_width_filled_poly_is_not_a_line_width_finding(tmp_path):
    poly = '(fp_poly (pts (xy -2 3) (xy 2 3) (xy 0 4)) {s} (fill yes) (layer "F.SilkS"))'
    pcb = _load(tmp_path, _footprint(poly.format(s=_stroke(0))))
    assert not check_silkscreen_line_width(pcb, _rules()).violations


def test_text_stroke_is_held_to_the_same_floor(tmp_path):
    def text(thickness: float) -> str:
        return (
            '(gr_text "REV C" (at 30 30) (layer "F.SilkS") '
            f"(effects (font (size 1 1) (thickness {thickness}))))\n"
        )

    thin = check_silkscreen_text_thickness(_load(tmp_path, text(0.149)), _rules())
    assert [v.rule_id for v in thin.violations] == ["silkscreen_text_thickness"]
    at_floor = check_silkscreen_text_thickness(_load(tmp_path, text(0.15)), _rules())
    assert not at_floor.violations
