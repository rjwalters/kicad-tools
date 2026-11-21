"""One shared silk-legalisation entry point: ``legalize_silk`` (Issue #9797).

Phase 1 of #9795 (operator ruling: silk is legal by default).  The pass
composes the existing repairs and the reference placer, excludes vias from
what it clips / moves / reports, never waives anything it cannot fix, and is
idempotent.  Every fixture puts the board at a non-zero origin so frame
mix-ups between the raw-tree tools and the board-relative checks show up.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli.fix_silkscreen_cmd import main as fix_silkscreen_main  # noqa: E402
from kicad_tools.drc.repair_silkscreen import legalize_silk  # noqa: E402
from kicad_tools.manufacturers.base import (  # noqa: E402
    DesignRules,
    load_design_rules_from_yaml,
)
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.silkscreen.place_refs import SilkRefPlacer  # noqa: E402
from kicad_tools.validate.rules.silkscreen import check_all_silkscreen  # noqa: E402

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)
          (36 "B.SilkS" user) (37 "F.SilkS" user)
          (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0) (tenting front back))
  (net 0 "")
  (net 1 "N1")
  (gr_rect (start 100 100) (end 160 160) (stroke (width 0.1) (type solid)) (fill no)
    (layer "Edge.Cuts"))
"""


def _ref(ref: str, at: str = "0 0") -> str:
    return (
        f'(fp_text reference "{ref}" (at {at}) (layer "F.SilkS")'
        " (effects (font (size 1 1) (thickness 0.15))))"
    )


def _crtyd(half: float, uid: str) -> str:
    return (
        f"(fp_rect (start {-half} {-half}) (end {half} {half})"
        f' (stroke (width 0.1) (type solid)) (fill no) (layer "F.CrtYd") (uuid "{uid}"))'
    )


def _line(x1, y1, x2, y2, *, width: float = 0.15, uid: str) -> str:
    return (
        f"(fp_line (start {x1} {y1}) (end {x2} {y2})"
        f" (stroke (width {width}) (type solid))"
        f' (layer "F.SilkS") (uuid "{uid}"))'
    )


def _fp(at: str, body: str) -> str:
    return f'  (footprint "Test:FP" (layer "F.Cu") (at {at})\n    {body}\n  )\n'


def _via(x: float, y: float) -> str:
    return (
        f'  (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
        f'(net 1) (uuid "via-{x}-{y}"))\n'
    )


def _jlcpcb_rules() -> DesignRules:
    return load_design_rules_from_yaml("jlcpcb")["2layer_1oz"]


def _write(tmp_path: Path, body: str, name: str = "board.kicad_pcb") -> Path:
    """Write the board, normalised through one no-op save (canonical bytes)."""
    from kicad_tools.drc.repair_silkscreen import SilkscreenRepairer

    path = tmp_path / name
    path.write_text(_HEAD + body + ")\n")
    SilkscreenRepairer(path).save()
    return path


def _silk_findings(path: Path) -> list[tuple[str, str]]:
    """(rule_id, severity) of every silk finding, via the checker's own rules."""
    results = check_all_silkscreen(PCB.load(path), _jlcpcb_rules())
    return sorted((v.rule_id, v.severity) for v in results.violations)


def _check_cli_silk_findings(path: Path, capsys) -> list[dict]:
    """Silk findings from the public ``kct check`` command (JSON)."""
    from kicad_tools.cli import main as kct_main

    capsys.readouterr()  # flush anything an earlier in-process command printed
    kct_main(["check", str(path), "--mfr", "jlcpcb", "--only", "silkscreen", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    return [v for v in data["violations"] if v["rule_id"].startswith(("silk", "silkscreen"))]


#: The synthetic fixture of the acceptance criteria: silk over a pad, silk
#: over a hole, an obscured reference, a sub-minimum line width, a
#: sub-minimum text height and a legal-height thin text stroke -- plus a
#: rotated footprint and references that are already clear.
MAIN_BODY = (
    _fp(
        "112 115",
        _ref("U1")  # reference sits inside its own courtyard: obscured
        + _crtyd(2.5, "u1-crtyd")
        + _line(-3, 1.5, 3, 1.5, uid="u1-overpad")  # crosses the SMD pad below
        + ' (pad "1" smd rect (at 0 1.5) (size 1 0.5) (layers "F.Cu" "F.Mask")'
        ' (net 1) (uuid "u1-pad1"))',
    )
    + _fp(
        "130 115 90",  # rotated footprint: NPTH hole clip + thin line
        _ref("U2", "0 3.5")
        + _crtyd(2, "u2-crtyd")
        + _line(-3, 0, 3, 0, uid="u2-overhole")  # crosses the NPTH drill
        + _line(-1, -3, 1, -3, width=0.1, uid="u2-thin")  # sub-minimum width
        + ' (pad "" np_thru_hole circle (at 0 0) (size 1 1) (drill 1)'
        ' (layers "*.Cu") (uuid "u2-npth"))',
    )
    + _fp("148 115", _ref("U3", "0 3.5") + _crtyd(2, "u3-crtyd"))
    + '  (gr_text "REV A" (at 148 110.5) (layer "F.SilkS")'
    " (effects (font (size 1 1) (thickness 0.1))))\n"  # legal height, thin stroke
    + _fp(
        "112 140",
        _ref("U4", "0 3.5")
        + _crtyd(2, "u4-crtyd")
        + ' (fp_text user "1V8" (at 0 0) (layer "F.SilkS")'
        " (effects (font (size 0.8 0.8) (thickness 0.15))))",  # sub-minimum height
    )
)


def test_fixture_finds_all_targeted_rules_before(tmp_path: Path):
    path = _write(tmp_path, MAIN_BODY)
    rules = {rule for rule, _sev in _silk_findings(path)}
    assert {
        "silk_pad_clearance",
        "silk_hole_overlap",
        "silk_ref_obscured",
        "silkscreen_line_width",
        "silkscreen_text_height",
        "silkscreen_text_thickness",
    } <= rules


def test_public_cli_all_legalizes_fixture(tmp_path: Path, capsys):
    from kicad_tools.cli import main as kct_main

    path = _write(tmp_path, MAIN_BODY)
    assert _check_cli_silk_findings(path, capsys)

    assert kct_main(["fix-silkscreen", str(path), "--all", "--mfr", "jlcpcb"]) == 0

    findings = _check_cli_silk_findings(path, capsys)
    assert [f for f in findings if f["severity"] in ("error", "warning")] == []


def test_legalize_silk_report_names_every_edit(tmp_path: Path):
    path = _write(tmp_path, MAIN_BODY)
    report = legalize_silk(path, _jlcpcb_rules())

    assert report.width.total_fixed >= 1  # u2-thin widened
    assert report.heights.total_fixed >= 1  # U4 user text rescaled
    assert report.strokes.total_fixed >= 1  # gr_text stroke thickened
    assert report.pads.total_fixed >= 1  # u1-overpad trimmed (rotated U2 too)
    assert report.holes.total_fixed >= 1  # u2-overhole clipped off the NPTH
    moved = [p.footprint_ref for p in (report.refs.moved if report.refs else [])]
    assert moved == ["U1"]  # only the obscured reference relocates
    assert report.unresolved == []
    assert report.output_path == str(path)

    # Widths, heights and thicknesses now meet the profile.
    pcb = PCB.load(path)
    u2 = next(fp for fp in pcb.footprints if fp.reference == "U2")
    thin = next(g for g in u2.graphics if g.uuid == "u2-thin")
    assert thin.stroke_width == pytest.approx(0.15)
    (gr_text,) = pcb.texts
    assert gr_text.font_thickness == pytest.approx(0.15)
    assert gr_text.font_size[1] == pytest.approx(1.0)  # height was already legal
    u4 = next(fp for fp in pcb.footprints if fp.reference == "U4")
    user = next(t for t in u4.texts if t.text_type == "user")
    assert user.font_size[1] == pytest.approx(1.0)

    # The obscured reference moved out of its own courtyard, clear of the pad.
    u1 = next(fp for fp in pcb.footprints if fp.reference == "U1")
    u1_ref = next(t for t in u1.texts if t.text_type == "reference")
    assert u1_ref.position != (0.0, 0.0)
    assert _silk_findings(path) == []


def test_idempotent_second_run_changes_nothing(tmp_path: Path):
    path = _write(tmp_path, MAIN_BODY)
    legalize_silk(path, _jlcpcb_rules())
    once = path.read_bytes()

    report = legalize_silk(path, _jlcpcb_rules())

    assert report.total_fixed == 0
    assert report.unresolved == []
    assert path.read_bytes() == once


def test_dry_run_reports_staged_result_and_preserves_bytes(tmp_path: Path, capsys):
    path = _write(tmp_path, MAIN_BODY)
    before = path.read_bytes()

    assert (
        fix_silkscreen_main(
            [str(path), "--all", "--mfr", "jlcpcb", "--dry-run", "--format", "json"]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)

    assert data["dry_run"] is True
    assert data["total_fixed"] > 0
    assert data["total_refs_moved"] == 1
    assert data["unresolved"] == []
    assert path.read_bytes() == before


def test_separate_output_preserves_original(tmp_path: Path):
    path = _write(tmp_path, MAIN_BODY)
    out = tmp_path / "legalized.kicad_pcb"
    before = path.read_bytes()

    report = legalize_silk(path, _jlcpcb_rules(), output=out)

    assert out.exists()
    assert path.read_bytes() == before
    assert _silk_findings(out) == []
    assert report.output_path == str(out)


def test_already_legal_board_is_noop(tmp_path: Path):
    body = _fp("130 130", _ref("U1", "0 3.5") + _crtyd(2, "c") + _line(-2, -3, 2, -3, uid="ok"))
    path = _write(tmp_path, body)
    before = path.read_bytes()

    report = legalize_silk(path, _jlcpcb_rules())

    assert report.total_fixed == 0
    assert report.unresolved == []
    assert path.read_bytes() == before
    assert _silk_findings(path) == []


# ---------------------------------------------------------------------------
# The #9795 via exception: silk over a via is never clipped, moved or reported
# ---------------------------------------------------------------------------


def test_via_only_overlap_is_untouched_for_graphics_and_references(tmp_path: Path):
    body = (
        _fp(
            "148 140",
            _ref("U5", "0 3.5")  # directly over via B (board 148, 143.5)
            + _crtyd(2, "u5-crtyd")
            + _line(3, 0, 5.5, 0, uid="u5-line"),  # crosses via A (board 152, 140)
        )
        + _via(152, 140)
        + _via(148, 143.5)
    )
    path = _write(tmp_path, body)
    before = path.read_bytes()
    (line_finding, ref_finding) = sorted(
        f for f in _silk_findings(path) if f[0] == "silk_hole_overlap"
    )
    assert line_finding[0] == ref_finding[0] == "silk_hole_overlap"  # both still flagged

    report = legalize_silk(path, _jlcpcb_rules())

    # Nothing to fix anywhere: the only overlaps are via overlaps.
    assert report.total_fixed == 0
    assert report.unresolved == []
    assert path.read_bytes() == before
    # ...and the checker still reports both (grading belongs to #9798).
    assert sorted(f for f in _silk_findings(path) if f[0] == "silk_hole_overlap") == [
        line_finding,
        ref_finding,
    ]


def test_placer_keeps_reference_over_via(tmp_path: Path):
    body = _fp("148 140", _ref("U5", "0 3.5") + _crtyd(2, "u5-crtyd")) + _via(148, 143.5)
    path = _write(tmp_path, body)

    placer = SilkRefPlacer(path)
    result = placer.plan(include_via_obstacles=False)
    (placement,) = result.placements
    assert placement.status == "unchanged", placement.reason


# ---------------------------------------------------------------------------
# No allowance: what the pass cannot fix is named and stays a check finding
# ---------------------------------------------------------------------------


def test_unrepairable_arc_is_named_and_still_flagged(tmp_path: Path, capsys):
    arc = (
        "(fp_arc (start -1 0) (mid 0 -1) (end 1 0) (stroke (width 0.15) (type solid))"
        ' (layer "F.SilkS") (uuid "u6-arc"))'
    )
    body = _fp(
        "112 140",
        _ref("U6", "0 3.5")
        + _crtyd(2, "u6-crtyd")
        + arc  # passes over the PTH drill at (0, -1): arcs are not clipped
        + ' (pad "1" thru_hole circle (at 0 -1) (size 1.2 1.2) (drill 0.8)'
        ' (layers "*.Cu") (net 1) (uuid "u6-pth"))',
    )
    path = _write(tmp_path, body)
    before = [f for f in _silk_findings(path) if f[0] == "silk_hole_overlap"]
    assert before

    assert fix_silkscreen_main([str(path), "--all", "--mfr", "jlcpcb", "--format", "json"]) == 1
    data = json.loads(capsys.readouterr().out)

    (unresolved,) = data["unresolved"]
    assert unresolved["kind"] == "silk_over_hole"
    assert "fp_arc" in unresolved["item"]
    # The item stays a checker finding; no waiver was created for it.
    assert [f for f in _silk_findings(path) if f[0] == "silk_hole_overlap"] == before
    assert not list(path.parent.glob("*.kct-waivers.json"))


# ---------------------------------------------------------------------------
# Profile-supplied hole clearance (both paths)
# ---------------------------------------------------------------------------


def test_default_rules_leave_hole_clearance_unset():
    assert (
        DesignRules(
            min_trace_width_mm=0.1,
            min_clearance_mm=0.1,
            min_via_drill_mm=0.2,
            min_via_diameter_mm=0.4,
            min_annular_ring_mm=0.1,
        ).min_silk_to_hole_clearance_mm
        is None
    )
    for ruleset in load_design_rules_from_yaml("jlcpcb").values():
        assert ruleset.min_silk_to_hole_clearance_mm is None


def test_profile_supplied_hole_clearance_is_honoured(tmp_path: Path):
    from shapely.geometry import Point

    from kicad_tools.validate.rules.silkscreen import _fp_transform, _stroke_geometry

    body = _fp(
        "130 130",
        _ref("U2", "0 3.5")
        + _crtyd(2, "u2-crtyd")
        + _line(-3, 0, 3, 0, uid="u2-overhole")
        + ' (pad "" np_thru_hole circle (at 0 0) (size 1 1) (drill 1)'
        ' (layers "*.Cu") (uuid "u2-npth"))',
    )
    path = _write(tmp_path, body)
    rules = _jlcpcb_rules()
    assert rules.min_silk_to_hole_clearance_mm is None  # omitted in the YAML ...

    report = legalize_silk(path, rules)
    assert report.min_hole_clearance_mm == pytest.approx(0.0)  # ... falls back to the rule's

    (tmp_path / "again").mkdir()
    path = _write(tmp_path / "again", body, name="board.kicad_pcb")
    from dataclasses import replace

    report = legalize_silk(path, replace(rules, min_silk_to_hole_clearance_mm=0.2))
    assert report.min_hole_clearance_mm == pytest.approx(0.2)

    pcb = PCB.load(path)
    (fp,) = pcb.footprints
    hole = Point(130, 130).buffer(0.5)
    xf = _fp_transform(fp)
    for g in fp.graphics:
        assert _stroke_geometry(g, xf).distance(hole) >= 0.2 - 1e-6


# ---------------------------------------------------------------------------
# The public command reaches the entry point through the kct dispatch
# ---------------------------------------------------------------------------


def test_kct_dispatch_forwards_all(tmp_path: Path, capsys):
    from kicad_tools.cli import main as kct_main

    path = _write(tmp_path, MAIN_BODY)
    assert (
        kct_main(["fix-silkscreen", str(path), "--all", "--mfr", "jlcpcb", "--format", "summary"])
        == 0
    )
    out = capsys.readouterr().out
    assert "1 reference(s) moved" in out
    assert _silk_findings(path) == []


def test_existing_flags_keep_behaviour(tmp_path: Path, capsys):
    """Without --all the command still runs the per-flag passes only."""
    path = _write(tmp_path, MAIN_BODY)

    assert fix_silkscreen_main([str(path), "--mfr", "jlcpcb", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert data["total_line_width_fixed"] >= 1
    assert data["total_text_height_fixed"] >= 1
    assert "hole_clearance_fixes" not in data  # --clip-to-holes not given
    assert "reference_placements" not in data  # --all not given
    # No clipping happened: the pad/hole/ref findings survive.
    assert any(rule == "silk_hole_overlap" for rule, _ in _silk_findings(path))
