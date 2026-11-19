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
    side: str = "F",
    pad_size: tuple[float, float] = (1.5, 0.9),
    tab: tuple[float, float, float] = (TAB_NEAR, TAB_FAR, 0.45),
) -> Path:
    cu, mask, paste = f"{side}.Cu", f"{side}.Mask", f"{side}.Paste"
    t_near, t_far, t_half = tab
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
 (footprint "Package_TO_SOT_SMD:SOT-89-3" (layer "{cu}") (at {fx} {fy} {rotation})
  (property "Reference" "Q1" (at 0 0) (layer "{side}.SilkS"))
  (pad "1" smd rect (at -1.5 -1.5 {rotation}) (size 1.0 0.5) (layers "{cu}") (net 1 "GND"))
  (pad "2" smd custom (at {PAD_AT[0]} {PAD_AT[1]} {rotation}) (size {pad_size[0]} {pad_size[1]})
   (layers "{cu}" "{mask}" "{paste}") (net 2 "SIG")
   (options (clearance outline) (anchor rect))
   (primitives (gr_poly (pts (xy {t_near} -{t_half}) (xy {t_far} -{t_half})
     (xy {t_far} {t_half}) (xy {t_near} {t_half})) (width 0) (fill yes))))
  (pad "3" smd rect (at -1.5 1.5 {rotation}) (size 1.0 0.5) (layers "{cu}") (net 3 "VCC")))
 (footprint "Resistor_SMD:R_0603" (layer "{cu}") (at {r1[0]:.4f} {r1[1]:.4f})
  (property "Reference" "R1" (at 0 0) (layer "{side}.SilkS"))
  (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "{cu}") (net 2 "SIG")))
 {extra_footprints}
 (segment (start {r1[0]:.4f} {r1[1]:.4f}) (end {end[0]:.4f} {end[1]:.4f})
  (width 0.25) (layer "{cu}") (net 2)))"""
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


# The stock library's exact pad-2 numbers (KiCad 10.0 SOT-89-3): anchor at
# (-1.8625, 0) with size 1.475 x 0.9, tab gr_poly x 0.7375..3.8625, y
# +/-0.8665.  The fixture above is a structural stand-in (1.5 x 0.9, tab
# half height 0.45); these use the real outline, and flip the footprint to
# the back side, which no earlier placement does.  With the custom-pad rules
# from #9498 on main, the stock pad is measured exactly: the tab is copper
# for connectivity and no rule may stub it with an incomplete finding.
STOCK_SIZE = (1.475, 0.9)
STOCK_TAB = (0.7375, 3.8625, 0.8665)
STOCK_PLACEMENTS = [
    pytest.param(30.0, "F", id="30-front"),
    pytest.param(0.0, "B", id="0-back"),
    pytest.param(30.0, "B", id="30-back"),
]


@pytest.mark.parametrize(("rotation", "side"), STOCK_PLACEMENTS)
def test_stock_pad_exact_numbers_tab_is_real_copper(tmp_path, capsys, rotation, side):
    board = _board(
        tmp_path,
        rotation=rotation,
        track_local_x=ON_TAB,
        side=side,
        pad_size=STOCK_SIZE,
        tab=STOCK_TAB,
    )
    report = _run(board, capsys)
    rules = _rules(report)
    assert "connectivity" not in rules
    assert "track_dangling" not in rules
    stubbed = [
        v
        for v in report["violations"]
        if v["rule_id"].endswith("_incomplete") and "Q1" in v["message"]
    ]
    assert not stubbed, f"the stock pad is measurable; no rule may stub it: {stubbed}"


def test_stock_pad_exact_numbers_back_side_off_tab_still_dangling(tmp_path, capsys):
    board = _board(
        tmp_path,
        rotation=30.0,
        track_local_x=OFF_TAB,
        side="B",
        pad_size=STOCK_SIZE,
        tab=STOCK_TAB,
    )
    report = _run(board, capsys)
    rules = _rules(report)
    assert "connectivity" in rules
    assert "track_dangling" in rules
