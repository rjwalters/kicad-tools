"""place-silk-refs treats gr_* mask openings as obstacles (issue #8862)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.silkscreen.place_refs import SilkRefPlacer

pytest.importorskip("shapely")

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_rect (start 0 0) (end 60 60) (stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts"))
"""


def _fp(ref: str = "R1") -> str:
    return f"""  (footprint "TestFP" (layer "F.Cu") (at 30 30)
    (fp_text reference "{ref}" (at 0 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
  )
"""


def _opening(layer: str = "F.Mask") -> str:
    return (
        "  (gr_rect (start 27 27) (end 33 33) (stroke (width 0) (type solid))"
        f' (fill yes) (layer "{layer}"))\n'
    )


def _gr_text() -> str:
    return (
        '  (gr_text "NOTE" (at 30 30) (layer "F.SilkS")'
        " (effects (font (size 1 1) (thickness 0.15))))\n"
    )


def _plan(tmp_path: Path, body: str):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_HEAD + body + ")\n")
    return SilkRefPlacer(path).plan(clearance_mm=0.15)


def test_footprint_ref_relocates_off_graphic_opening(tmp_path: Path):
    result = _plan(tmp_path, _opening() + _fp())
    (p,) = result.placements
    assert p.status == "moved"


def test_opening_on_opposite_side_is_ignored(tmp_path: Path):
    result = _plan(tmp_path, _opening("B.Mask") + _fp())
    (p,) = result.placements
    assert p.status == "unchanged"


def test_gr_text_over_opening_reported_not_moved(tmp_path: Path):
    result = _plan(tmp_path, _opening() + _gr_text())
    (c,) = result.fixed_text_conflicts
    assert c.text == "NOTE"
    assert c.position == (30.0, 30.0)
    assert c.candidates, "expected candidate clear positions"
    assert all(not (27 < x < 33 and 27 < y < 33) for x, y in c.candidates)


def test_gr_text_without_opening_not_reported(tmp_path: Path):
    result = _plan(tmp_path, _gr_text())
    assert result.fixed_text_conflicts == []
