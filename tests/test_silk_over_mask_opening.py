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
