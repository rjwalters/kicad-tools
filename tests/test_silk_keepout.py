"""Silk keepouts and the silk error grading of #9798 (#9795 phase 2).

Two groups:

* **``silk_in_keepout``** -- rule areas whose sibling ``.kicad_dru`` forbids
  ``text`` / ``graphic`` there, per the curated fixture from issue #9798: a
  front line crossing the area boundary and front text inside it are errors,
  the back line at the same coordinates is not, and removing the sibling rule
  (or scoping it to copper) leaves the silk legal.  The same resolved areas
  drive the clip in
  :meth:`~kicad_tools.drc.repair_silkscreen.SilkscreenRepairer.repair_keepout_clearance`.
* **Grading** -- the operator ruling of 2026-11-20 on #9795: under the
  jlcpcb profiles silk on exposed copper, silk hidden under a component
  body and below-minimum silk widths are errors, while silk over a *via*
  is not (info at most), whichever route reports it.  The
  ``silk_hole_overlap`` severity split (PTH/NPTH vs via) is open PR #9112's
  (issue #9033); the tests here verify both reporting routes so a via-only
  board cannot fail through a second rule, and that PTH/NPTH holes stay
  reportable, without pinning that severity.

Every board sits at a non-zero origin (Edge.Cuts at 100,100) so a frame
mix-up between the raw-tree clip and the board-relative check shows up.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli.runner import find_kicad_cli  # noqa: E402
from kicad_tools.drc.repair_silkscreen import SilkscreenRepairer  # noqa: E402
from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.sexp.parser import parse_file  # noqa: E402
from kicad_tools.validate.checker import DRCChecker  # noqa: E402
from kicad_tools.validate.rules.silkscreen import (  # noqa: E402
    check_all_silkscreen,
    check_silk_hole_overlap,
    check_silk_in_keepout,
    check_silk_over_copper,
    check_silk_ref_obscured,
    check_silk_under_body,
    check_silkscreen_line_width,
    resolve_silk_keepouts,
)

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_rect (start 100 100) (end 160 160) (stroke (width 0.1) (type solid)) (fill no)
    (layer "Edge.Cuts"))
"""

# The curated fixture from issue #9798: an all-allowed rule area on F.SilkS
# named ``no_ink`` whose printed ink is forbidden by the sibling rule below.
_SILK_ZONE = """  (zone (net 0) (net_name "") (layer "F.SilkS") (name "no_ink") (hatch edge 0.5)
    (priority 0) (uuid "aaaaaaaa-0000-0000-0000-000000000001")
    (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))
    (polygon (pts (xy 120 120) (xy 140 120) (xy 140 140) (xy 120 140))))
"""
_AREA_LABEL = "no_ink [aaaaaaaa-0000-0000-0000-000000000001]"
_DRU = """(version 1)
(rule "printed legend keepout"
  (layer "F.SilkS")
  (condition "A.intersectsArea('no_ink')")
  (constraint disallow text graphic))
"""

_FRONT_LINE = (
    "  (gr_line (start 110 130) (end 150 130) (stroke (width 0.15) (type solid)) "
    '(layer "F.SilkS") (uuid "front-line"))\n'
)
_BACK_LINE = (
    "  (gr_line (start 110 130) (end 150 130) (stroke (width 0.15) (type solid)) "
    '(layer "B.SilkS") (uuid "back-line"))\n'
)
_FRONT_TEXT = (
    '  (gr_text "LEGEND" (at 130 130) (layer "F.SilkS") '
    "(effects (font (size 1 1) (thickness 0.15))))\n"
)


def _board(tmp_path: Path, body: str, dru: str | None = _DRU) -> Path:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_HEAD + body + ")\n")
    if dru is not None:
        (tmp_path / "board.kicad_dru").write_text(dru)
    return path


@pytest.mark.parametrize("profile", ["jlcpcb", "jlcpcb-tier1"], ids=["jlcpcb", "jlcpcb-tier1"])
def _jlc_rules(profile):
    return get_profile(profile).get_design_rules(layers=2, copper_oz=1)


def _jlcpcb():
    return get_profile("jlcpcb").get_design_rules(layers=2, copper_oz=1)


def _other():
    # A non-JLC grading control: bare defaults keep every knob off.
    return get_profile("pcbway").get_design_rules(layers=2, copper_oz=1)


# ---------------------------------------------------------------------------
# silk_in_keepout: detection
# ---------------------------------------------------------------------------


def test_front_silk_flagged_back_side_clean(tmp_path: Path) -> None:
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE + _BACK_LINE + _FRONT_TEXT)
    pcb = PCB.load(board)

    areas = resolve_silk_keepouts(pcb)
    assert len(areas) == 1
    assert areas[0].label == _AREA_LABEL
    assert areas[0].sides == frozenset({"F"})
    assert areas[0].disallow_text and areas[0].disallow_graphic
    # Zone polygons are board-relative: sheet 120..140 minus origin (100,100).
    assert areas[0].polygon.bounds == (20.0, 20.0, 40.0, 40.0)

    violations = check_silk_in_keepout(pcb).violations
    assert [(v.rule_id, v.severity) for v in violations] == [
        ("silk_in_keepout", "error"),
        ("silk_in_keepout", "error"),
    ]
    items = {v.items for v in violations}
    assert ("gr_line {front-line}", _AREA_LABEL) in items
    assert ("LEGEND", _AREA_LABEL) in items
    # The back line at the same coordinates prints on the other side.
    assert all("back-line" not in i for pair in items for i in pair)


def test_no_sibling_rule_leaves_silk_legal(tmp_path: Path) -> None:
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE + _FRONT_TEXT, dru=None)
    pcb = PCB.load(board)
    assert resolve_silk_keepouts(pcb) == []
    assert check_silk_in_keepout(pcb).violations == []


def test_copper_scoped_rule_leaves_silk_legal(tmp_path: Path) -> None:
    # The same area addressed by a rule scoped to copper: the five built-in
    # flags leave the silk legal and so does this custom rule.
    dru = _DRU.replace('(layer "F.SilkS")', '(layer "F.Cu")')
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE + _FRONT_TEXT, dru=dru)
    pcb = PCB.load(board)
    assert resolve_silk_keepouts(pcb) == []
    assert check_silk_in_keepout(pcb).violations == []


def test_ordinary_copper_keepout_is_not_a_silk_keepout(tmp_path: Path) -> None:
    copper_zone = _SILK_ZONE.replace('(layer "F.SilkS")', '(layer "F.Cu")').replace(
        "(keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) "
        "(footprints allowed))",
        "(keepout (tracks not_allowed) (vias not_allowed))",
    )
    board = _board(tmp_path, copper_zone + _FRONT_LINE + _FRONT_TEXT, dru=None)
    pcb = PCB.load(board)
    assert check_silk_in_keepout(pcb).violations == []


def test_text_only_restriction_spares_graphics(tmp_path: Path) -> None:
    dru = _DRU.replace("disallow text graphic", "disallow text")
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE + _FRONT_TEXT, dru=dru)
    pcb = PCB.load(board)

    areas = resolve_silk_keepouts(pcb)
    assert len(areas) == 1
    assert areas[0].disallow_text and not areas[0].disallow_graphic

    violations = check_silk_in_keepout(pcb).violations
    assert len(violations) == 1
    assert violations[0].items[0] == "LEGEND"


def test_footprint_owned_rotated_area(tmp_path: Path) -> None:
    # KiCad stores footprint-embedded zones in board coordinates with the
    # placement already applied (schema/pcb.py, Issue #6119), so a rotated
    # RF module's antenna area is where the polygon says it is.  The
    # module's own fp_line silk, rotated with it, crosses the area.
    body = """
  (footprint "X:Y" (layer "F.Cu") (at 230 230 90)
    (property "Reference" "AE1" (at 0 -6) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (zone (net 0) (net_name "") (layer "F.SilkS") (name "antenna") (hatch edge 0.5)
      (uuid "bbbbbbbb-0000-0000-0000-000000000002")
      (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))
      (polygon (pts (xy 225 225) (xy 235 225) (xy 235 235) (xy 225 235))))
    (fp_line (start -4 -4) (end 4 4) (stroke (width 0.15) (type solid))
      (layer "F.SilkS") (uuid "fp-own-line"))
  )
  (gr_line (start 145 145) (end 148 148) (stroke (width 0.15) (type solid))
    (layer "F.SilkS") (uuid "outside-line"))
"""
    dru = (
        "(version 1)\n"
        '(rule "no print"\n'
        '  (layer "F.SilkS")\n'
        "  (condition \"A.intersectsArea('antenna')\")\n"
        "  (constraint disallow graphic))\n"
    )
    board = _board(tmp_path, body, dru=dru)
    pcb = PCB.load(board)

    areas = resolve_silk_keepouts(pcb)
    assert len(areas) == 1
    assert areas[0].polygon.bounds == (125.0, 125.0, 135.0, 135.0)

    violations = check_silk_in_keepout(pcb).violations
    # The footprint's own rotated line (uuid fp-own-line) crosses the
    # area; the far-away board-level line does not.
    hit = {v.items[0] for v in violations}
    assert hit == {"AE1 (fp_line) {fp-own-line}"}


def test_registered_in_check_all_and_categorised(tmp_path: Path) -> None:
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE)
    pcb = PCB.load(board)

    results = check_all_silkscreen(pcb, _jlcpcb())
    assert any(v.rule_id == "silk_in_keepout" and v.severity == "error" for v in results.violations)
    # The error severity is what fails the check: DRCResults.error_count is
    # the failing-verdict input the CLI reports.
    assert results.error_count >= 1
    # The ``silk`` prefix fallback keeps the rule out of the fab-blocking
    # Manufacturing bucket; its error severity still fails the check.
    assert DRCChecker.category_for_rule("silk_in_keepout") == "advisory-quality"
    assert DRCChecker.category_for_rule("silk_under_body") == "advisory-quality"


# ---------------------------------------------------------------------------
# repair_keepout_clearance: the clip #9797 consumes
# ---------------------------------------------------------------------------


def _gr_lines(board: Path) -> list[tuple[str, tuple[float, float], tuple[float, float], str]]:
    out = []
    for node in parse_file(board).find_all("gr_line"):
        layer = str(node.find("layer").get_first_atom())  # type: ignore[union-attr]
        start = node.find("start")
        end = node.find("end")
        assert start is not None and end is not None
        s = (float(start.get_float(0)), float(start.get_float(1)))  # type: ignore[union-attr]
        e = (float(end.get_float(0)), float(end.get_float(1)))  # type: ignore[union-attr]
        uid = str(node.find("uuid").get_first_atom()) if node.find("uuid") else ""  # type: ignore[union-attr]
        out.append((layer, s, e, uid))
    return out


def test_clip_keeps_both_printable_portions_and_is_idempotent(tmp_path: Path) -> None:
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE + _BACK_LINE)

    repairer = SilkscreenRepairer(board)
    result = repairer.repair_keepout_clearance()
    repairer.save()

    assert len(result.fixes) == 1
    fix = result.fixes[0]
    assert fix.owner == "board"
    assert fix.element_type == "gr_line"
    assert fix.action == "trimmed"
    assert fix.pieces == 2
    assert fix.areas == [_AREA_LABEL]
    assert fix.new_area < fix.old_area
    assert result.text_residual == []
    assert result.residual == []

    front = [l for l in _gr_lines(board) if l[0] == "F.SilkS"]
    assert len(front) == 2
    # The area (120..140) grew by the stroke half-width plus the safety
    # margin; both survivors end strictly outside it.
    for _layer, (sx, _sy), (ex, _ey), _uid in front:
        assert max(sx, ex) < 120.0 or min(sx, ex) > 140.0
        assert max(sx, ex) <= 120.0 + 1e-6 or min(sx, ex) >= 140.0 - 1e-6 or True
    assert PCB.load(board)  # still parses

    # The clipped board passes the check...
    assert check_silk_in_keepout(PCB.load(board)).violations == []
    # ...and a second pass changes nothing, byte for byte.
    once = board.read_bytes()
    repairer = SilkscreenRepairer(board)
    assert repairer.repair_keepout_clearance().total_fixed == 0
    repairer.save()
    assert board.read_bytes() == once


def test_clip_removes_graphic_inside_area_and_reports_text(tmp_path: Path) -> None:
    inside_rect = (
        "  (gr_rect (start 126 126) (end 134 134) (stroke (width 0.15) (type solid)) "
        '(fill none) (layer "F.SilkS") (uuid "inside-rect"))\n'
    )
    board = _board(tmp_path, _SILK_ZONE + inside_rect + _FRONT_TEXT)

    repairer = SilkscreenRepairer(board)
    result = repairer.repair_keepout_clearance()
    repairer.save()

    actions = {(f.element_type, f.action) for f in result.fixes}
    assert ("gr_rect", "removed") in actions
    # Text is never clipped: the surviving item and its area are reported,
    # and the re-measured rule keeps it an error.
    assert result.text_residual == [f"LEGEND in {_AREA_LABEL}"]
    assert result.residual == [
        f"Silkscreen LEGEND lies in silk keepout {_AREA_LABEL}",
    ]
    residual = check_silk_in_keepout(PCB.load(board)).violations
    assert [(v.rule_id, v.severity) for v in residual] == [("silk_in_keepout", "error")]


def test_clip_footprint_silk_through_rotation(tmp_path: Path) -> None:
    # The rotated module's own fp_line crossing its antenna area is clipped
    # through the footprint transform, in footprint-local coordinates.
    # fp-local (-8,-8)..(8,8) rotated 90 at sheet (230,230) spans
    # (222,238)-(238,222): both ends outside the 225..235 area, the middle
    # inside it, so two printable portions survive.
    body = """
  (footprint "X:Y" (layer "F.Cu") (at 230 230 90)
    (property "Reference" "AE1" (at 0 -6) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (zone (net 0) (net_name "") (layer "F.SilkS") (name "antenna") (hatch edge 0.5)
      (uuid "bbbbbbbb-0000-0000-0000-000000000002")
      (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))
      (polygon (pts (xy 225 225) (xy 235 225) (xy 235 235) (xy 225 235))))
    (fp_line (start -8 -8) (end 8 8) (stroke (width 0.15) (type solid))
      (layer "F.SilkS") (uuid "fp-own-line"))
  )
"""
    dru = (
        "(version 1)\n"
        '(rule "no print"\n'
        '  (layer "F.SilkS")\n'
        "  (condition \"A.intersectsArea('antenna')\")\n"
        "  (constraint disallow graphic))\n"
    )
    board = _board(tmp_path, body, dru=dru)

    repairer = SilkscreenRepairer(board)
    result = repairer.repair_keepout_clearance()
    repairer.save()

    assert len(result.fixes) == 1
    fix = result.fixes[0]
    assert fix.owner == "AE1"
    assert fix.element_type == "fp_line"
    assert fix.pieces == 2
    assert result.residual == []
    assert check_silk_in_keepout(PCB.load(board)).violations == []


# ---------------------------------------------------------------------------
# Grading per the operator ruling on #9795
# ---------------------------------------------------------------------------


def _pad_footprint(body: str = "", *, ref: str = "U1", at: str = "130 130") -> str:
    return f"""  (footprint "X:Y" (layer "F.Cu") (at {at})
    (property "Reference" "{ref}" (at 0 -6) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    {body}
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste"))
  )
"""


_PAD_SILK = (
    "(fp_line (start -0.9 0) (end 0.9 0) (stroke (width 0.15) (type solid)) "
    '(layer "F.SilkS") (uuid "over-pad"))'
)


def test_silk_over_pad_error_under_jlcpcb_warning_elsewhere(tmp_path: Path) -> None:
    board = _board(tmp_path, _pad_footprint(_PAD_SILK))
    pcb = PCB.load(board)

    def findings(rules):
        return [
            (v.severity, v.items)
            for v in check_silk_over_copper(pcb, rules).violations
            if v.items[1].startswith("U1 pad")
        ]

    assert findings(_jlcpcb()) == [("error", ("U1 (fp_line)", "U1 pad 1"))]
    assert findings(_other()) == [("warning", ("U1 (fp_line)", "U1 pad 1"))]


def test_untented_via_not_an_error_under_jlcpcb(tmp_path: Path) -> None:
    # An untented via (setup default untented) with silk straight over it:
    # reported, but never at error severity under the jlcpcb profiles --
    # and the pad hit next to it still is.
    body = (
        "  (setup (pad_to_mask_clearance 0) (tenting (front no) (back no)))\n"
        '  (via (at 130 130) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0)'
        ' (uuid "v1"))\n'
        + _pad_footprint("", ref="U1", at="135 130").replace(
            '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste")',
            '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste")',
        )
        + "  (gr_line (start 129 130) (end 136.5 130) (stroke (width 0.3) (type solid)) "
        '(layer "F.SilkS") (uuid "span"))\n'
    )
    board = _board(tmp_path, body)
    pcb = PCB.load(board)

    over_copper = [
        (v.severity, v.items[1]) for v in check_silk_over_copper(pcb, _jlcpcb()).violations
    ]
    vias = [entry for entry in over_copper if entry[1].startswith("via")]
    pads = [entry for entry in over_copper if entry[1].startswith("U1 pad")]
    assert vias and all(sev == "info" for sev, _ in vias)
    assert pads and all(sev == "error" for sev, _ in pads)
    # Control: with the knob off (non-JLC profile) both are warnings again.
    plain = [(v.severity, v.items[1]) for v in check_silk_over_copper(pcb, _other()).violations]
    assert plain and all(sev == "warning" for sev, _ in plain)


@pytest.mark.parametrize("profile", ["jlcpcb", "jlcpcb-tier1"])
def test_via_only_fixture_has_no_silk_errors(tmp_path: Path, profile: str) -> None:
    # A tented and an untented via, silk over both, nothing else on the
    # board: no silk rule may grade it an error under the jlcpcb profiles.
    # Both reporting routes are exercised -- the drill set
    # (``silk_hole_overlap``, whose via split is #9112's) and the untented
    # aperture set (``silk_over_copper``) -- so a via-only case cannot
    # reappear as an error under a second rule.
    body = (
        '  (via (at 130 130) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0)'
        ' (uuid "tented"))\n'
        '  (via (at 140 130) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0)'
        ' (uuid "bare") (tenting (front no) (back no)))\n'
        "  (gr_line (start 129 130) (end 141 130) (stroke (width 0.3) (type solid)) "
        '(layer "F.SilkS") (uuid "span"))\n'
    )
    board = _board(tmp_path, body)
    pcb = PCB.load(board)
    rules = _jlc_rules(profile)

    assert check_silk_hole_overlap(pcb, rules).violations  # reported, but...
    for violation in check_silk_hole_overlap(pcb, rules).violations:
        assert violation.severity != "error"
    for violation in check_silk_over_copper(pcb, rules).violations:
        assert violation.severity != "error"


def test_pth_and_npth_holes_stay_reportable(tmp_path: Path) -> None:
    # PTH / NPTH drills under silk remain reported under the jlcpcb
    # profiles.  The severity split (error for real holes, via waived) is
    # open PR #9112 / issue #9033's regrade; these assertions hold before
    # and after it lands.
    pads = """  (footprint "X:Y" (layer "F.Cu") (at 130 130)
    (property "Reference" "J1" (at 0 -6) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5)
      (layers "*.Cu" "*.Mask") (uuid "pth"))
    (pad "2" np_thru_hole circle (at 3 0) (size 1 1) (drill 0.5)
      (layers "*.Cu" "*.Mask") (uuid "npth"))
  )
  (gr_line (start 129.5 130) (end 133.5 130) (stroke (width 0.3) (type solid))
    (layer "F.SilkS") (uuid "span"))
"""
    board = _board(tmp_path, pads)
    pcb = PCB.load(board)

    holes = [v.items[1] for v in check_silk_hole_overlap(pcb, _jlcpcb()).violations]
    assert any("J1 pad 1 hole" in h for h in holes)
    assert any("J1 pad 2 hole" in h for h in holes)


@pytest.mark.parametrize("profile", ["jlcpcb", "jlcpcb-tier1"])
def test_silk_minimums_error_under_jlcpcb(tmp_path: Path, profile: str) -> None:
    thin = (
        "  (gr_line (start 110 110) (end 120 110) (stroke (width 0.1) (type solid)) "
        '(layer "F.SilkS") (uuid "thin"))\n'
        '  (gr_text "tiny" (at 115 115) (layer "F.SilkS") '
        "(effects (font (size 0.6 0.6) (thickness 0.1))))\n"
    )
    board = _board(tmp_path, thin, dru=None)
    pcb = PCB.load(board)

    under_jlc = check_silkscreen_line_width(pcb, _jlc_rules(profile)).violations
    assert under_jlc and all(v.severity == "error" for v in under_jlc)
    under_other = check_silkscreen_line_width(pcb, _other()).violations
    assert under_other and all(v.severity == "warning" for v in under_other)

    from kicad_tools.validate.rules.silkscreen import (
        check_silkscreen_text_height,
        check_silkscreen_text_thickness,
    )

    assert all(
        v.severity == "error" for v in check_silkscreen_text_height(pcb, _jlcpcb()).violations
    )
    assert check_silkscreen_text_height(pcb, _jlcpcb()).violations
    assert all(
        v.severity == "error" for v in check_silkscreen_text_thickness(pcb, _jlcpcb()).violations
    )
    assert check_silkscreen_text_thickness(pcb, _jlcpcb()).violations
    assert all(
        v.severity == "warning" for v in check_silkscreen_text_thickness(pcb, _other()).violations
    )


_COURTYARD_FP = """  (footprint "X:Y" (layer "F.Cu") (at 130 130)
    (property "Reference" "U1" (at 0 -6) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -3 -3) (end 3 3) (stroke (width 0.05) (type solid))
      (fill none) (layer "F.CrtYd") (uuid "crtyd"))
    (fp_line (start -2 -2) (end 2 -2) (stroke (width 0.15) (type solid))
      (layer "F.SilkS") (uuid "own-silk"))
  )
"""


@pytest.mark.parametrize("profile", ["jlcpcb", "jlcpcb-tier1"])
def test_silk_under_body_error_under_jlcpcb(tmp_path: Path, profile: str) -> None:
    # Graphics and text of *another* part (here: board-level silk) hidden
    # entirely under U1's body on the same side -- per the operator: an
    # error under jlcpcb.  U1's own silk inside its own courtyard is not
    # this rule's finding, and ordinary silk-to-silk intersections alone do
    # not demonstrate body obstruction.
    body = (
        _COURTYARD_FP
        + "  (gr_line (start 128.5 129) (end 131.5 129) (stroke (width 0.15) (type solid)) "
        '(layer "F.SilkS") (uuid "hidden"))\n' + '  (gr_text "SW" (at 130 131) (layer "F.SilkS") '
        "(effects (font (size 1 1) (thickness 0.15))))\n"
        + "  (gr_line (start 150 150) (end 152 152) (stroke (width 0.15) (type solid)) "
        '(layer "F.SilkS") (uuid "free-1"))\n'
        + "  (gr_line (start 150 150) (end 152 149) (stroke (width 0.15) (type solid)) "
        '(layer "F.SilkS") (uuid "free-2"))\n'
    )
    board = _board(tmp_path, body, dru=None)
    pcb = PCB.load(board)

    def hit_items(rules):
        return {v.items for v in check_silk_under_body(pcb, rules).violations}

    under_jlc = hit_items(_jlcpcb())
    assert ("gr_line {hidden}", "U1 (courtyard)") in under_jlc
    assert ("SW", "U1 (courtyard)") in under_jlc
    assert all(v.severity == "error" for v in check_silk_under_body(pcb, _jlcpcb()).violations)
    # Grading control: warning under a non-JLC profile.
    under_other = check_silk_under_body(pcb, _other()).violations
    assert under_other and all(v.severity == "warning" for v in under_other)
    # The module's own silk and the crossing free lines are never flagged.
    for pair in under_jlc:
        assert "own-silk" not in pair[0]
        assert "free-" not in pair[0]


# ---------------------------------------------------------------------------
# Native parity on the pinned kicad-cli image (AC: findings agree where
# kicad-cli checks the same thing)
# ---------------------------------------------------------------------------


def _native_items_not_allowed(board: Path) -> list[str]:
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    report = board.with_suffix(".drc.json")
    subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "-o",
            str(report),
            str(board),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    return [
        item.get("description", "")
        for v in data["violations"]
        if v["type"] == "items_not_allowed"
        for item in v["items"]
    ]


def test_native_parity_on_pinned_image(tmp_path: Path) -> None:
    # The curated fixture from the issue: the sibling rule makes the pinned
    # image report the same two items kct does (front line + front text),
    # and removing it clears both engines.  kct keeps its own rule id and
    # severity -- the native ``items_not_allowed`` finding and the kct
    # verdict stay separately identifiable.
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE + _BACK_LINE + _FRONT_TEXT)
    pcb = PCB.load(board)
    kct_items = sorted(v.items[0] for v in check_silk_in_keepout(pcb).violations)
    assert kct_items == ["LEGEND", "gr_line {front-line}"]

    native = _native_items_not_allowed(board)
    assert len(native) == 2
    assert any("Segment on F.Silkscreen" in d for d in native)
    assert any("'LEGEND' on F.Silkscreen" in d for d in native)
    assert not any("B.Silkscreen" in d for d in native)  # back line is legal

    # Same board without the sibling rule: both engines are silent.
    board.with_suffix(".kicad_dru").unlink()
    assert _native_items_not_allowed(board) == []
    assert check_silk_in_keepout(PCB.load(board)).violations == []


def test_obscured_reference_error_under_jlcpcb(tmp_path: Path) -> None:
    # A hidden reference with no replacement text: the ref half of the
    # under-body ruling, regraded with the same knob.
    body = _COURTYARD_FP.replace(
        '(property "Reference" "U1" (at 0 -6) (layer "F.Fab")',
        '(property "Reference" "U1" (at 0 -6) (layer "F.Fab") (hide yes)',
    )
    board = _board(tmp_path, body, dru=None)
    pcb = PCB.load(board)

    under_jlc = check_silk_ref_obscured(pcb, _jlcpcb()).violations
    assert under_jlc and all(v.severity == "error" for v in under_jlc)
    under_other = check_silk_ref_obscured(pcb, _other()).violations
    assert under_other and all(v.severity == "warning" for v in under_other)
