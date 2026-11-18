"""place-silk-refs names the coordinate frame of placements (issue #9364).

``RefPlacement.reason`` obstacle labels quote sheet-absolute coordinates
(e.g. ``pad aperture drill via at (130.000, 80.000)``) while
``old/new_position`` are board-relative -- the same defect class #9327
fixed for the board-text report, in the reference-placement path.  The
placement output must name the frame and carry the origin so a consumer
can map one to the other, without changing any existing value.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kicad_tools.cli.place_silk_refs_cmd import main as cli_main
from kicad_tools.silkscreen.place_refs import SilkRefPlacer

pytest.importorskip("shapely")

_SHEET_COORDS_RE = re.compile(r"\((-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?)\)")

# Knobs that make every candidate for R1 fail on the via's aperture, so the
# unplaceable reason is the coordinate-bearing aperture label.
_PLAN_KNOBS = {"clearance_mm": 2.0, "max_offset_mm": 0.2, "step_mm": 0.1}
_CLI_KNOBS = ["--clearance", "2.0", "--max-offset", "0.2", "--step", "0.1"]

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_rect (start {ox} {oy}) (end {ex} {ey}) (stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts"))
"""


def _fp(ref: str, x: float, y: float) -> str:
    return f"""  (footprint "TestFP" (layer "F.Cu") (at {x} {y})
    (fp_text reference "{ref}" (at 0 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
  )
"""


def _board(tmp_path: Path, ox: float, oy: float) -> Path:
    """The Observed-section probe board: untented through via at sheet
    ``(ox + 30, oy + 30)`` with a refdes-sized box on the same physical
    point (board-relative ``(30, 30)``), plus a clear reference at (5, 5)."""
    via = (
        f'  (via (at {ox + 30} {oy + 30}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0))\n'
    )
    text = (
        _HEAD.format(ox=ox, oy=oy, ex=ox + 40, ey=oy + 40)
        + via
        + _fp("R1", ox + 30, oy + 30)
        + _fp("R2", ox + 5, oy + 5)
        + ")\n"
    )
    path = tmp_path / f"frame_{int(ox)}_{int(oy)}.kicad_pcb"
    path.write_text(text)
    return path


def _placements(path: Path) -> dict[str, dict]:
    result = SilkRefPlacer(path).plan(**_PLAN_KNOBS)
    return {p.footprint_ref: p.to_dict() for p in result.placements}


def test_offset_board_names_frame_and_maps_reason_to_sheet(tmp_path: Path):
    placements = _placements(_board(tmp_path, 100.0, 50.0))
    r1 = placements["R1"]

    assert r1["status"] == "unplaceable"
    assert r1["frame"] == "board-relative"
    assert r1["origin_mm"] == [100.0, 50.0]
    assert r1["new_position_mm"] == [30.0, 30.0]

    # The coordinates the reason quotes are sheet-absolute: adding the
    # reported origin to the board-relative position recovers them.
    quoted = _SHEET_COORDS_RE.findall(r1["reason"])
    assert len(quoted) == 1, r1["reason"]
    qx, qy = (float(v) for v in quoted[0])
    assert (qx, qy) == pytest.approx((130.0, 80.0))
    assert (qx, qy) == pytest.approx(
        (
            r1["origin_mm"][0] + r1["new_position_mm"][0],
            r1["origin_mm"][1] + r1["new_position_mm"][1],
        )
    )


def test_offset_board_to_sheet_helper(tmp_path: Path):
    result = SilkRefPlacer(_board(tmp_path, 100.0, 50.0)).plan(**_PLAN_KNOBS)
    (r1,) = (p for p in result.placements if p.footprint_ref == "R1")
    assert r1.origin == (100.0, 50.0)
    assert r1.to_sheet(r1.new_position) == pytest.approx((130.0, 80.0))


def test_zero_origin_board_byte_identical_positions_and_reason(tmp_path: Path):
    """AC: an origin-(0, 0) board keeps today's ``old/new_position_mm`` and
    ``reason`` exactly (values pinned against pre-#9364 main)."""
    placements = _placements(_board(tmp_path, 0.0, 0.0))
    r1, r2 = placements["R1"], placements["R2"]
    assert r1["old_position_mm"] == [30.0, 30.0]
    assert r1["new_position_mm"] == [30.0, 30.0]
    assert r1["reason"] == "pad aperture drill via at (30.000, 30.000)"
    assert r2["old_position_mm"] == [5.0, 5.0]
    assert r2["new_position_mm"] == [5.0, 5.0]
    assert r2["reason"] == ""
    assert r1["frame"] == "board-relative"
    assert r1["origin_mm"] == [0.0, 0.0]


def test_to_dict_keys_are_additive_only(tmp_path: Path):
    """Only ``frame`` and ``origin_mm`` are new; the pre-#9364 key set and
    its values are untouched."""
    placements = _placements(_board(tmp_path, 100.0, 50.0))
    pre9364_keys = {
        "footprint_ref",
        "layer",
        "old_position_mm",
        "new_position_mm",
        "old_rotation_deg",
        "new_rotation_deg",
        "status",
        "reason",
        "association_margin_mm",
        "low_association",
    }
    for placement in placements.values():
        assert set(placement) - {"frame", "origin_mm"} == pre9364_keys


def test_every_status_carries_frame_and_origin(tmp_path: Path):
    for ox, oy in ((100.0, 50.0), (0.0, 0.0)):
        placements = _placements(_board(tmp_path, ox, oy))
        assert {p["status"] for p in placements.values()} == {"unplaceable", "unchanged"}
        for placement in placements.values():
            assert placement["frame"] == "board-relative"
            assert placement["origin_mm"] == [ox, oy]


def test_moved_status_carries_frame_and_origin(tmp_path: Path):
    # Default knobs: the reference can escape the drill aperture -> "moved".
    result = SilkRefPlacer(_board(tmp_path, 100.0, 50.0)).plan(clearance_mm=0.15)
    (r1,) = (p for p in result.placements if p.footprint_ref == "R1")
    assert r1.status == "moved"
    assert r1.to_dict()["frame"] == "board-relative"
    assert r1.to_dict()["origin_mm"] == [100.0, 50.0]


def test_text_output_labels_the_frame_of_reason_coordinates(tmp_path: Path, capsys):
    rc = cli_main([str(_board(tmp_path, 100.0, 50.0)), "--dry-run", *_CLI_KNOBS])
    assert rc == 0
    out = capsys.readouterr().out
    assert "R1: pad aperture drill via at (130.000, 80.000) [sheet coords]" in out
    # Coordinate-free reasons stay unlabelled.
    assert "R2:" not in out


def test_json_output_carries_frame_and_origin(tmp_path: Path, capsys):
    rc = cli_main(
        [str(_board(tmp_path, 100.0, 50.0)), "--dry-run", "--format", "json", *_CLI_KNOBS]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    (r1,) = (p for p in data["placements"] if p["footprint_ref"] == "R1")
    assert r1["frame"] == "board-relative"
    assert r1["origin_mm"] == [100.0, 50.0]
    assert r1["new_position_mm"] == [30.0, 30.0]
    assert "(130.000, 80.000)" in r1["reason"]
