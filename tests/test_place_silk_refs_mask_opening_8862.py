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


# --- Issue #9327: candidates avoid other silk; output uses one named frame ---


def _gr_text_at(text: str, x: float, y: float, size: float = 1.0) -> str:
    return (
        f'  (gr_text "{text}" (at {x} {y}) (layer "F.SilkS")'
        f" (effects (font (size {size} {size}) (thickness 0.15))))\n"
    )


def _box_of(text: str, size: float, point, rotation: float = 0.0):
    from kicad_tools.silkscreen.place_refs import _oriented_text_geometry

    return _oriented_text_geometry(text, (size, size), 0.15, point, rotation)


def test_candidates_avoid_other_board_text(tmp_path: Path):
    body = _opening() + _gr_text_at("FIRST", 30, 28.0, 1.2) + _gr_text_at("SECOND", 30, 26.0, 1.5)
    result = _plan(tmp_path, body)
    assert {c.text for c in result.fixed_text_conflicts} == {"FIRST", "SECOND"}
    current = {
        "FIRST": _box_of("FIRST", 1.2, (30, 28.0)),
        "SECOND": _box_of("SECOND", 1.5, (30, 26.0)),
    }
    sizes = {"FIRST": 1.2, "SECOND": 1.5}
    for c in result.fixed_text_conflicts:
        other = next(k for k in current if k != c.text)
        assert c.candidates
        for pt in c.candidates:
            assert not _box_of(c.text, sizes[c.text], pt).intersects(current[other]), (c.text, pt)


def test_candidates_avoid_static_footprint_silk(tmp_path: Path):
    # A silk line hugging the opening's top edge; no candidate box may touch it.
    wall = (
        "  (gr_line (start 20 26) (end 40 26) (stroke (width 0.15) (type solid))"
        ' (layer "F.SilkS"))\n'
    )
    result = _plan(tmp_path, _opening() + wall + _gr_text())
    (c,) = result.fixed_text_conflicts
    from shapely.geometry import LineString

    line = LineString([(20, 26), (40, 26)])
    assert c.candidates
    for pt in c.candidates:
        assert not _box_of("NOTE", 1.0, pt).intersects(line)


def test_text_output_uses_named_sheet_frame_on_offset_board(tmp_path: Path, capsys):
    from kicad_tools.cli.place_silk_refs_cmd import _print_fixed_text_conflicts

    ox, oy = 100.0, 50.0
    head = _HEAD.replace("(start 0 0) (end 60 60)", f"(start {ox} {oy}) (end {ox + 60} {oy + 60})")
    body = (
        f"  (gr_rect (start {ox + 27} {oy + 27}) (end {ox + 33} {oy + 33})"
        ' (stroke (width 0) (type solid)) (fill yes) (layer "F.Mask"))\n'
        + _gr_text_at("NOTE", ox + 30, oy + 30)
    )
    path = tmp_path / "off.kicad_pcb"
    path.write_text(head + body + ")\n")
    result = SilkRefPlacer(path).plan(clearance_mm=0.15)
    (c,) = result.fixed_text_conflicts
    assert c.position == pytest.approx((30.0, 30.0))
    assert c.to_sheet(c.position) == pytest.approx((130.0, 80.0))
    assert c.to_dict()["frame"] == "board-relative"
    _print_fixed_text_conflicts(result)
    out = capsys.readouterr().out
    assert "at (130.000, 80.000) [sheet coords]" in out
    # the opening label is quoted in the same (sheet) frame
    assert "(130.000, 80.000)" in c.opening
    assert "candidate clear positions [sheet coords]" in out
