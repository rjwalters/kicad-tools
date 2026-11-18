"""``kct check`` on a board with the stock SOT-89-3 custom pad (Issue #9468).

Pad 2 of ``Package_TO_SOT_SMD:SOT-89-3`` is a ``custom`` pad: a rectangular
anchor plus a zero-width filled polygon forming the tab.  The pad-grid check
used to abort the whole run with ``ValueError: Unsupported routing geometry``,
and the two copper checks measured the nominal anchor box instead of the tab.

The fixture mirrors the stock pad's structure with a tab that reaches far
past its anchor, so a track ending on the tab touches *only* the primitive.
Assertions use individual rule IDs so unrelated fixture advisories (missing
courtyard, fiducials, ...) cannot mask a regression.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from kicad_tools.cli.check_cmd import main

# Pad-local geometry (mm): anchor 1.5 x 0.9 centred on the pad origin, tab
# from x=0.5 to x=3.8625 (well beyond the anchor's right edge at 0.75).
PAD_AT = (-1.8625, 0.0)
TAB_NEAR, TAB_FAR = 0.5, 3.8625
ON_TAB = 2.8625  # local x on the tab, 2.1 mm past the anchor edge
OFF_TAB = 4.4  # local x 0.54 mm past the tab end


def _rot(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    """KiCad's footprint transform: the stored angle is negated."""
    a = math.radians(-angle_deg)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def _board(
    tmp_path: Path,
    *,
    rotation: float,
    track_local_x: float,
    fp_at: tuple[float, float] = (110.0, 110.0),
    extra_footprints: str = "",
) -> Path:
    fx, fy = fp_at
    ax, ay = _rot(*PAD_AT, rotation)
    anchor = (fx + ax, fy + ay)  # board position of the custom pad's anchor
    # R1 sits along the tab axis, off the copper; the track runs from R1 to
    # the chosen point on the pad's local x axis.
    rx, ry = _rot(7.0, 0.0, rotation)
    r1 = (anchor[0] + rx, anchor[1] + ry)
    ex, ey = _rot(track_local_x, 0.0, rotation)
    end = (anchor[0] + ex, anchor[1] + ey)
    g1x, g1y = _rot(-1.5, -1.5, rotation)
    g3x, g3y = _rot(-1.5, 1.5, rotation)
    text = f"""(kicad_pcb (version 20240108) (generator "test")
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (setup (pad_to_mask_clearance 0))
 (net 0 "") (net 1 "GND") (net 2 "SIG") (net 3 "VCC")
 (gr_rect (start 90 90) (end 130 130) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
 (footprint "Package_TO_SOT_SMD:SOT-89-3" (layer "F.Cu") (at {fx} {fy} {rotation})
  (property "Reference" "Q1" (at 0 0) (layer "F.SilkS"))
  (pad "1" smd rect (at -1.5 -1.5 {rotation}) (size 1.0 0.5) (layers "F.Cu") (net 1 "GND"))
  (pad "2" smd custom (at {PAD_AT[0]} {PAD_AT[1]} {rotation}) (size 1.5 0.9)
   (layers "F.Cu" "F.Mask" "F.Paste") (net 2 "SIG")
   (options (clearance outline) (anchor rect))
   (primitives (gr_poly (pts (xy {TAB_NEAR} -0.45) (xy {TAB_FAR} -0.45)
     (xy {TAB_FAR} 0.45) (xy {TAB_NEAR} 0.45)) (width 0) (fill yes))))
  (pad "3" smd rect (at -1.5 1.5 {rotation}) (size 1.0 0.5) (layers "F.Cu") (net 3 "VCC")))
 (footprint "Resistor_SMD:R_0603" (layer "F.Cu") (at {r1[0]:.4f} {r1[1]:.4f})
  (property "Reference" "R1" (at 0 0) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "SIG")))
 {extra_footprints}
 (segment (start {r1[0]:.4f} {r1[1]:.4f}) (end {end[0]:.4f} {end[1]:.4f})
  (width 0.25) (layer "F.Cu") (net 2)))"""
    del g1x, g1y, g3x, g3y
    path = tmp_path / "sot89.kicad_pcb"
    path.write_text(text)
    return path


def _run(board: Path, capsys, *extra: str) -> dict:
    capsys.readouterr()
    main([str(board), "--mfr", "jlcpcb", "--layers", "2", "--format", "json", *extra])
    out = capsys.readouterr().out
    assert out.strip(), "kct check must emit a report, not abort with empty stdout"
    return json.loads(out)


def _rules(report: dict) -> set[str]:
    return {v["rule_id"] for v in report["violations"]}


# A rotated placement away from the origin catches both a missed transform and
# a double one: the tab lies along the footprint's local +x axis.
ROTATIONS = [0.0, 90.0, 30.0]


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_default_command_completes_with_report(tmp_path, capsys, rotation):
    report = _run(_board(tmp_path, rotation=rotation, track_local_x=ON_TAB), capsys)
    assert report["verdict"] in {"pass", "fail", "incomplete"}
    assert report["violations"], "fixture advisories prove the full rule set ran"


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_track_ending_on_tab_satisfies_connectivity_and_dangling(tmp_path, capsys, rotation):
    report = _run(_board(tmp_path, rotation=rotation, track_local_x=ON_TAB), capsys)
    rules = _rules(report)
    assert "connectivity" not in rules
    assert "track_dangling" not in rules


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_track_ending_clear_of_tab_is_still_reported(tmp_path, capsys, rotation):
    report = _run(_board(tmp_path, rotation=rotation, track_local_x=OFF_TAB), capsys)
    rules = _rules(report)
    assert "connectivity" in rules
    assert "track_dangling" in rules


def test_only_pad_grid_runs_and_still_flags_ordinary_off_grid_pad(tmp_path, capsys):
    off_grid = (
        '(footprint "Resistor_SMD:R_0603" (layer "F.Cu") (at 95.05 95.05)'
        ' (property "Reference" "R9" (at 0 0) (layer "F.SilkS"))'
        ' (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND")))'
    )
    board = _board(tmp_path, rotation=0.0, track_local_x=ON_TAB, extra_footprints=off_grid)
    report = _run(board, capsys, "--only", "pad_grid")
    flagged = [v for v in report["violations"] if v["rule_id"] == "pad_grid"]
    assert flagged, "an ordinary off-grid pad must still be identified"
    assert all("R9" in v["message"] for v in flagged)
    assert not any("Q1" in v["message"] for v in flagged)
