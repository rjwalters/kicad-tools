"""silk_over_mask_opening: silk inside graphic solder-mask openings (Issue #8861)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.silkscreen import (  # noqa: E402
    check_silk_over_copper,
    check_silk_over_mask_opening,
)

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (net 0 "")
"""

_STYLE = "(effects (font (size 1 1) (thickness 0.15)))"


def _mask_shapes(side: str) -> str:
    m = f"{side}.Mask"
    return f"""
  (gr_line (start 10 10) (end 30 10) (stroke (width 2) (type solid)) (layer "{m}"))
  (gr_circle (center 20 30) (end 22 30) (stroke (width 0) (type solid)) (fill yes) (layer "{m}"))
  (gr_poly (pts (xy 10 50) (xy 30 50) (xy 30 54) (xy 10 54)) (stroke (width 0) (type solid))
    (fill yes) (layer "{m}"))
"""


def _silk(side: str, ys: tuple[float, float, float], dy: float = 0.0) -> str:
    s = f"{side}.SilkS"
    mirror = " (justify mirror)" if side == "B" else ""
    return "".join(
        f'  (gr_text "SILK TEXT {i}" (at 20 {y + dy}) (layer "{s}")'
        f" (effects (font (size 1 1) (thickness 0.15)){mirror}))\n"
        for i, y in enumerate(ys)
    )


def _check(tmp_path: Path, body: str):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_HEAD + body + ")\n")
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)
    pcb = PCB.load(str(path))
    return check_silk_over_mask_opening(pcb, rules), check_silk_over_copper(pcb, rules)


@pytest.mark.parametrize("side", ["F", "B"])
def test_silk_over_each_graphic_opening_is_error(tmp_path: Path, side: str) -> None:
    res, copper = _check(tmp_path, _mask_shapes(side) + _silk(side, (10, 30, 52)))
    assert len(res.violations) == 3
    assert all(
        v.rule_id == "silk_over_mask_opening" and v.severity == "error" for v in res.violations
    )
    msgs = " ".join(v.message for v in res.violations)
    for kind in ("gr_line", "gr_circle", "gr_poly"):
        assert kind in msgs
    assert f"{side}.Mask" in msgs and "mm2" in msgs
    assert not copper.violations


def test_silk_clear_of_openings_is_clean(tmp_path: Path) -> None:
    # Text height 1 mm (+0.15 stroke): line opening spans y 9..11, circle 28..32,
    # poly 50..54.  Place silk well clear of all of them.
    body = _mask_shapes("F") + _silk("F", (6.5, 25.5, 47.0))
    res, _ = _check(tmp_path, body)
    assert not res.violations


def test_opposite_side_silk_not_flagged(tmp_path: Path) -> None:
    res, _ = _check(tmp_path, _mask_shapes("F") + _silk("B", (10, 30, 52)))
    assert not res.violations


def test_no_mask_graphics_no_findings(tmp_path: Path) -> None:
    res, _ = _check(tmp_path, _silk("F", (10, 30, 52)))
    assert not res.violations


def test_pad_aperture_still_silk_over_copper_warning(tmp_path: Path) -> None:
    fp = """  (footprint "R" (layer "F.Cu") (at 20 20)
    (pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu" "F.Mask" "F.Paste")))
"""
    res, copper = _check(tmp_path, fp + _silk("F", (20, 80, 90)))
    assert not res.violations
    assert [v.severity for v in copper.violations] == ["warning"]


def test_footprint_mask_graphic_opening_is_error(tmp_path: Path) -> None:
    # fp_circle on F.Mask inside a footprint at (20 20): opening spans 18..22.
    fp = """  (footprint "FID" (layer "F.Cu") (at 20 20)
    (fp_circle (center 0 0) (end 2 0) (stroke (width 0) (type solid)) (fill yes)
      (layer "F.Mask")))
"""
    res, _ = _check(tmp_path, fp + _silk("F", (20, 80, 90)))
    assert len(res.violations) == 1
    v = res.violations[0]
    assert v.rule_id == "silk_over_mask_opening" and v.severity == "error"
    assert "fp_circle" in v.message and "F.Mask" in v.message


def _silk_at(side: str, x: float, y: float) -> str:
    """One silk text centred on board point (x, y)."""
    mirror = " (justify mirror)" if side == "B" else ""
    return (
        f'  (gr_text "SILK TEXT" (at {x} {y}) (layer "{side}.SilkS")'
        f" (effects (font (size 1 1) (thickness 0.15)){mirror}))\n"
    )


def _fp_mask_shape(kind: str, layer: str) -> str:
    """A 10 x 2 mm opening centred on the footprint origin (footprint-local frame)."""
    if kind == "fp_line":
        return f'(fp_line (start -5 0) (end 5 0) (stroke (width 2) (type solid)) (layer "{layer}"))'
    return (
        "(fp_poly (pts (xy -5 -1) (xy 5 -1) (xy 5 1) (xy -5 1)) "
        f'(stroke (width 0) (type solid)) (fill yes) (layer "{layer}"))'
    )


# _fp_transform maps local (lx, ly) to (fx + lx*cos(r) - ly*sin(r), fy + lx*sin(r) + ly*cos(r))
# with r = -rotation.  For rotation 90 at (20 20): local (5 0) -> (20 15), so the horizontal
# local strip becomes the vertical strip x 19..21, y 15..25.  For rotation 45: local (5 0)
# -> (23.54 16.46), a diagonal strip along x + y = 40.  Unrotated it would be y 19..21.
@pytest.mark.parametrize("kind", ["fp_line", "fp_poly"])
@pytest.mark.parametrize(
    ("angle", "on_rotated", "on_unrotated"),
    [(90, (20, 16.5), (27, 20)), (45, (23.5, 16.5), (27, 20))],
)
def test_rotated_footprint_mask_opening_follows_rotation(
    tmp_path: Path,
    kind: str,
    angle: int,
    on_rotated: tuple[float, float],
    on_unrotated: tuple[float, float],
) -> None:
    fp = f'  (footprint "K" (layer "F.Cu") (at 20 20 {angle})\n    {_fp_mask_shape(kind, "F.Mask")})\n'
    res, _ = _check(tmp_path, fp + _silk_at("F", *on_rotated))
    assert len(res.violations) == 1
    v = res.violations[0]
    assert v.rule_id == "silk_over_mask_opening" and v.severity == "error"
    assert kind in v.message and "F.Mask" in v.message

    res, _ = _check(tmp_path, fp + _silk_at("F", *on_unrotated))
    assert not res.violations


@pytest.mark.parametrize("kind", ["fp_line", "fp_poly"])
def test_back_side_footprint_mask_opening_flags_back_silk_only(tmp_path: Path, kind: str) -> None:
    fp = f'  (footprint "K" (layer "B.Cu") (at 20 20 90)\n    {_fp_mask_shape(kind, "B.Mask")})\n'
    # Rotated 90: opening is x 19..21, y 15..25.
    res, _ = _check(tmp_path, fp + _silk_at("B", 20, 16.5))
    assert len(res.violations) == 1
    v = res.violations[0]
    assert v.rule_id == "silk_over_mask_opening" and v.severity == "error"
    assert kind in v.message and "B.Mask" in v.message

    # Same XY on the F side: not the opening's side.
    res, _ = _check(tmp_path, fp + _silk_at("F", 20, 16.5))
    assert not res.violations

    # Where the unrotated opening would have been (y 19..21, x 15..25).
    res, _ = _check(tmp_path, fp + _silk_at("B", 27, 20))
    assert not res.violations


@pytest.mark.parametrize("side", ["F", "B"])
def test_unfilled_circle_ring_hole_is_clean_ring_is_error(tmp_path: Path, side: str) -> None:
    # Ring centre (20 30), radius 6, stroke 1: ring spans r 5.5..6.5, hole r < 5.5.
    ring = (
        f"  (gr_circle (center 20 30) (end 26 30) (stroke (width 1) (type solid)) (fill no)"
        f' (layer "{side}.Mask"))\n'
    )
    # Text box (~7 x 1.2 mm) centred in the hole stays inside r 5.5.
    res, _ = _check(tmp_path, ring + _silk_at(side, 20, 30))
    assert not res.violations

    # Text centred on the top of the ring (y = 24) crosses r 5.5..6.5.
    res, _ = _check(tmp_path, ring + _silk_at(side, 20, 24))
    assert len(res.violations) == 1
    v = res.violations[0]
    assert v.rule_id == "silk_over_mask_opening" and v.severity == "error"
    assert "gr_circle" in v.message and f"{side}.Mask" in v.message
