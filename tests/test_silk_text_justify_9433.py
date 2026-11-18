"""Silk text boxes honour ``(justify ...)`` (Issue #9433).

``_text_bbox_geometry`` used to build every text box centred on its anchor, so
a ``(justify left)`` text whose ink starts at the anchor was modelled half a
width to the left of it and raised phantom silk findings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules import silkscreen as silk  # noqa: E402

TEXT = "ABCD"
SIZE = (1.0, 1.0)
THICK = 0.15
WIDTH = len(TEXT) * SIZE[0] * silk._TEXT_CHAR_WIDTH_FACTOR + THICK
HEIGHT = SIZE[1] + THICK


def _bbox(center, angle=0.0, justify=()):
    geom = silk._text_bbox_geometry(TEXT, SIZE, THICK, center, angle, justify)
    assert geom is not None
    return geom.bounds


def test_default_is_centred_and_unchanged():
    assert _bbox((10, 20)) == _bbox((10, 20), justify=())
    minx, miny, maxx, maxy = _bbox((10, 20))
    assert (minx, maxx) == pytest.approx((10 - WIDTH / 2, 10 + WIDTH / 2))
    assert (miny, maxy) == pytest.approx((20 - HEIGHT / 2, 20 + HEIGHT / 2))


def test_left_starts_at_anchor_right_ends_at_anchor():
    assert _bbox((10, 20), justify=("left",))[0] == pytest.approx(10)
    assert _bbox((10, 20), justify=("right",))[2] == pytest.approx(10)


def test_top_hangs_below_anchor_bottom_stands_above():
    # Board y grows downward: "top" anchors the top edge.
    assert _bbox((10, 20), justify=("top",))[1] == pytest.approx(20)
    assert _bbox((10, 20), justify=("bottom",))[3] == pytest.approx(20)


def test_combined_vertical_and_horizontal_tokens():
    minx, miny, maxx, maxy = _bbox((10, 20), justify=("left", "bottom"))
    assert (minx, maxy) == pytest.approx((10, 20))
    assert (maxx, miny) == pytest.approx((10 + WIDTH, 20 - HEIGHT))


def test_three_justifications_same_ink_same_geometry():
    """Anchors shifted so the ink is in the same place give identical boxes."""
    centre = _bbox((20, 20))
    left = _bbox((20 - WIDTH / 2, 20), justify=("left",))
    right = _bbox((20 + WIDTH / 2, 20), justify=("right",))
    assert left == pytest.approx(centre)
    assert right == pytest.approx(centre)


@pytest.mark.parametrize("angle", [90, 180, 270, 30])
def test_rotated_left_justified_matches_rotated_centred(angle):
    """Rotation is about the anchor, so a left text's ink lies along its own axis."""
    import math

    # Centre of a left-justified box sits +width/2 along the text direction.
    # KiCad positive angles turn toward negative board y.
    theta = math.radians(angle)
    dx, dy = (WIDTH / 2) * math.cos(theta), -(WIDTH / 2) * math.sin(theta)
    anchor = (20.0, 20.0)
    centred_at = (anchor[0] + dx, anchor[1] + dy)
    assert _bbox(anchor, angle, ("left",)) == pytest.approx(_bbox(centred_at, angle))


def test_rotated_90_left_extends_toward_negative_y():
    minx, miny, maxx, maxy = _bbox((20, 20), 90, ("left",))
    assert maxy == pytest.approx(20)
    assert miny == pytest.approx(20 - WIDTH)
    assert (minx, maxx) == pytest.approx((20 - HEIGHT / 2, 20 + HEIGHT / 2))


def test_mirror_swaps_left_and_right():
    """Verified vs KiCad 10.0.6: ``left mirror`` behaves as unmirrored ``right``."""
    assert _bbox((10, 20), justify=("mirror", "left")) == pytest.approx(
        _bbox((10, 20), justify=("right",))
    )
    assert _bbox((10, 20), justify=("mirror", "right")) == pytest.approx(
        _bbox((10, 20), justify=("left",))
    )
    assert _bbox((10, 20), justify=("mirror", "left"))[2] == pytest.approx(10)


def test_mirror_left_rotated_90_extends_toward_positive_y():
    minx, miny, maxx, maxy = _bbox((20, 20), 90, ("left", "mirror"))
    assert miny == pytest.approx(20)
    assert maxy == pytest.approx(20 + WIDTH)


def test_mirror_leaves_vertical_tokens_alone():
    assert _bbox((10, 20), justify=("mirror", "top"))[1] == pytest.approx(20)
    assert _bbox((10, 20), justify=("mirror", "bottom"))[3] == pytest.approx(20)


def test_rotated_footprint_text_box_uses_text_rotation(tmp_path):
    """Rotated justified fp_text is rotated at every call site (not shifted along x)."""
    board = _BOARD.replace(
        '(fp_text user "{text}" (at {fx} 0)', '(fp_text user "{text}" (at {fx} 0 90)'
    )
    path = tmp_path / "rot.kicad_pcb"
    path.write_text(board.format(text=TEXT, x=0, fx=0, justify=" (justify right)"))
    pcb = PCB.load(str(path))
    a = [e[0] for e in silk._iter_board_frame_texts(pcb) if e[1] == "Q1 (user)"]
    b = [e[1] for e in silk._iter_silk_elements(pcb) if e[2] == "Q1 (user)"]
    assert a and b
    assert a[0].bounds == pytest.approx(b[0].bounds)


_BOARD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user) (44 "Edge.Cuts" user))
  (net 0 "")
  (gr_rect (start 0 0) (end 40 40) (stroke (width 0.1) (type default))
    (fill none) (layer "Edge.Cuts"))
  (footprint "Test:P" (layer "F.Cu") (at 19 20)
    (property "Reference" "P1" (at 0 -3) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste")))
  (gr_text "{text}" (at {x} 20) (layer "F.SilkS")
    (effects (font (size 1 1) (thickness 0.15)){justify}))
  (footprint "Test:Q" (layer "F.Cu") (at 19 30)
    (property "Reference" "Q1" (at 0 -3) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_text user "{text}" (at {fx} 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15)){justify}))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste")))
)
"""


def _load(tmp_path: Path, name: str, justify: str, shift: float) -> PCB:
    # Ink is the same for all three: centre at x=20 (gr_text) / local x=1 on a footprint at x=19 (fp_text); the pads sit under the ink.
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(_BOARD.format(text=TEXT, x=20 + shift, fx=1 + shift, justify=justify))
    return PCB.load(path)


_CASES = [
    ("centre", "", 0.0),
    ("left", " (justify left)", -WIDTH / 2),
    ("right", " (justify right)", WIDTH / 2),
]


def test_parser_records_justify(tmp_path):
    pcb = _load(tmp_path, "parse", " (justify left bottom mirror)", 0.0)
    assert pcb.texts[0].justify == ("left", "bottom", "mirror")
    fp_text = next(t for fp in pcb.footprints for t in fp.texts if t.text_type == "user")
    assert fp_text.justify == ("left", "bottom", "mirror")
    assert _load(tmp_path, "plain", "", 0.0).texts[0].justify == ()


def test_property_text_parses_justify():
    from kicad_tools.schema.pcb import FootprintText
    from kicad_tools.sexp import parse_string

    sexp = parse_string(
        '(property "Reference" "U1" (at 0 -4) (layer "F.SilkS") '
        "(effects (font (size 1 1)) (justify right top)))"
    )
    assert FootprintText._from_property_sexp(sexp, "reference").justify == ("right", "top")


def test_same_ink_gives_same_silk_geometry_and_findings(tmp_path):
    rules = get_profile("jlcpcb-tier1").get_design_rules(layers=2)
    geoms, findings = [], []
    for name, justify, shift in _CASES:
        pcb = _load(tmp_path, name, justify, shift)
        elems = [e for e in silk._iter_silk_elements(pcb) if e[2] in (TEXT, "Q1 (user)")]
        geoms.append(sorted((e[2], tuple(round(b, 6) for b in e[1].bounds)) for e in elems))
        findings.append(
            sorted(
                (v.rule_id, tuple(sorted(v.items)))
                for v in silk.check_silk_over_copper(pcb, rules).violations
            )
        )
    assert geoms[0] == geoms[1] == geoms[2]
    assert findings[0] == findings[1] == findings[2]
    assert findings[0], "fixture must raise a finding or the equivalence is vacuous"
    # Sanity: the shifted left/right texts really are different inputs.
    assert _CASES[1][2] != _CASES[2][2]
