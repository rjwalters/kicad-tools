"""Silk tools clear ``silk_hole_overlap`` over filled/capped vias (Issue #9054).

Two gaps, both measured on board 03:

* ``kct place-silk-refs`` only avoided *untented* via openings, so reference
  text over a tented (filled/capped) via drill stayed put while the
  ``silk_hole_overlap`` check flagged it.
* nothing clipped footprint outline graphics (lines, polygons) off drills.

Every fixture puts the board at a non-zero origin (Edge.Cuts at 100,100) so a
frame mix-up between the raw-tree clip and the board-relative check shows up.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.drc.repair_silkscreen import SilkscreenRepairer  # noqa: E402
from kicad_tools.manufacturers.base import DesignRules  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.sexp.parser import parse_file, serialize_sexp  # noqa: E402
from kicad_tools.silkscreen.place_refs import (  # noqa: E402
    SilkRefPlacer,
    references_over_holes,
)
from kicad_tools.validate.rules.silkscreen import (  # noqa: E402
    _fp_transform,
    _stroke_geometry,
    check_silk_hole_overlap,
)

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0) (tenting front back))
  (net 0 "")
  (net 1 "N1")
  (gr_rect (start 100 100) (end 160 160) (stroke (width 0.1) (type solid)) (fill no)
    (layer "Edge.Cuts"))
"""

DRILL = 0.3
WIDTH = 0.15


def _via(x: float, y: float, extra: str = "") -> str:
    return (
        f'  (via (at {x} {y}) (size 0.6) (drill {DRILL}) (layers "F.Cu" "B.Cu") {extra}'
        f'(net 1) (uuid "via-{x}-{y}"))\n'
    )


def _fp(body: str, *, ref: str = "U1", at: str = "130 130", layer: str = "F.Cu") -> str:
    return f"""  (footprint "X:Y" (layer "{layer}") (at {at})
    (property "Reference" "{ref}" (at 0 -6) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    {body}
  )
"""


def _line(x1, y1, x2, y2, *, layer="F.SilkS", uid="l1") -> str:
    return (
        f"(fp_line (start {x1} {y1}) (end {x2} {y2}) (stroke (width {WIDTH}) (type solid)) "
        f'(layer "{layer}") (uuid "{uid}"))'
    )


def _poly(points, *, fill="yes", uid="p1", layer="F.SilkS") -> str:
    xys = " ".join(f"(xy {x} {y})" for x, y in points)
    return (
        f"(fp_poly (pts {xys}) (stroke (width {WIDTH}) (type solid)) (fill {fill}) "
        f'(layer "{layer}") (uuid "{uid}"))'
    )


def _rules() -> DesignRules:
    return DesignRules(
        min_trace_width_mm=0.0,
        min_clearance_mm=0.0,
        min_via_drill_mm=0.0,
        min_via_diameter_mm=0.0,
        min_annular_ring_mm=0.0,
    )


def _write(tmp_path: Path, body: str) -> Path:
    """Write the board, normalised through one no-op save (canonical bytes)."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_HEAD + body + ")\n")
    SilkscreenRepairer(path).save()
    return path


def _hole_findings(path: Path) -> list[tuple[str, ...]]:
    return [v.items for v in check_silk_hole_overlap(PCB.load(path), _rules()).violations]


def _clip(path: Path, **kwargs):
    repairer = SilkscreenRepairer(path)
    result = repairer.repair_hole_clearance(**kwargs)
    repairer.save()
    return result


def _silk_nodes(path: Path) -> list[str]:
    doc = parse_file(path)
    out = []
    for fp in doc.find_all("footprint"):
        for child in fp.children:
            if child.is_atom or child.name not in ("fp_line", "fp_poly", "fp_rect", "fp_arc"):
                continue
            layer = child.find("layer")
            if layer is not None and "SilkS" in str(layer.get_first_atom()):
                out.append(serialize_sexp(child))
    return out


def _ink(path: Path) -> float:
    """Total printed silk ink area (mm^2), as the silk checks model it."""
    from shapely.ops import unary_union

    pcb = PCB.load(path)
    geoms = []
    for fp in pcb.footprints:
        xf = _fp_transform(fp)
        for g in fp.graphics:
            if "SilkS" in g.layer and (geom := _stroke_geometry(g, xf)) is not None:
                geoms.append(geom)
    return unary_union(geoms).area if geoms else 0.0


# ---------------------------------------------------------------------------
# Gap 1: the reference placer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "via_extra",
    ["", "(tenting (front yes) (back yes)) "],
    ids=["board-default-tented", "per-via-tented"],
)
def test_placer_moves_reference_off_tented_via_drill(tmp_path: Path, via_extra: str):
    # Visible F.SilkS reference text centred right over a tented via.
    body = _fp(
        '(fp_text reference "C6" (at 0 0) (layer "F.SilkS")'
        " (effects (font (size 1 1) (thickness 0.15))))",
        ref="C6",
    ) + _via(130, 130, via_extra)
    path = _write(tmp_path, body)
    assert _hole_findings(path) == [("C6 (reference)", "via at (130.000, 130.000)")]

    placer = SilkRefPlacer(path)
    result = placer.plan()
    (placement,) = result.placements
    assert placement.status == "moved", placement.reason
    placer.apply(result)
    placer.save()
    assert _hole_findings(path) == []


def test_references_over_holes_names_only_offenders(tmp_path: Path):
    body = (
        _fp(
            '(fp_text reference "C6" (at 0 0) (layer "F.SilkS")'
            " (effects (font (size 1 1) (thickness 0.15))))",
            ref="C6",
        )
        + _fp(
            '(fp_text reference "C7" (at 0 0) (layer "F.SilkS")'
            " (effects (font (size 1 1) (thickness 0.15))))",
            ref="C7",
            at="115 115",
        )
        + _via(130, 130)
    )
    path = _write(tmp_path, body)
    placer = SilkRefPlacer(path)
    assert references_over_holes(placer.pcb) == {"C6"}


def test_place_silk_refs_cli_over_holes(tmp_path: Path):
    from kicad_tools.cli.place_silk_refs_cmd import main

    body = (
        _fp(
            '(fp_text reference "C6" (at 0 0) (layer "F.SilkS")'
            " (effects (font (size 1 1) (thickness 0.15))))",
            ref="C6",
        )
        # C7 sits clear of every hole but too close to its own pad for the
        # placer's clearance: --over-holes must leave it where it is.
        + _fp(
            '(fp_text reference "C7" (at 0 0) (layer "F.SilkS")'
            " (effects (font (size 1 1) (thickness 0.15))))"
            ' (pad "1" smd rect (at 0 0.8) (size 1 0.5) (layers "F.Cu" "F.Mask"))',
            ref="C7",
            at="115 115",
        )
        + _via(130, 130)
    )
    path = _write(tmp_path, body)
    assert main([str(path), "--over-holes", "--quiet"]) == 0
    assert _hole_findings(path) == []
    c7 = next(fp for fp in PCB.load(path).footprints if fp.reference == "C7")
    (text,) = [t for t in c7.texts if t.text_type == "reference" and t.layer == "F.SilkS"]
    assert text.position == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Gap 2: the outline clip
# ---------------------------------------------------------------------------


def _reach(clearance: float = 0.0) -> float:
    """Centreline keep-out radius the clip uses around a via drill."""
    return DRILL / 2 + clearance + WIDTH / 2 + 1e-3


def test_line_through_drill_is_split_with_minimum_removed(tmp_path: Path):
    # Footprint at (130,130); line along local y=-1 from x=-3..3, via at
    # (130, 129.05): 0.05 mm off the centreline.
    body = _fp(_line(-3, -1, 3, -1) + _line(-3, 1, 3, 1, uid="keep")) + _via(130, 129.05)
    path = _write(tmp_path, body)
    keep_before = [n for n in _silk_nodes(path) if '"keep"' in n]
    assert len(_hole_findings(path)) == 1

    result = _clip(path)
    (fix,) = result.fixes
    assert (fix.element_type, fix.action, fix.pieces) == ("fp_line", "trimmed", 2)
    assert result.residual == [] and result.text_residual == []
    assert _hole_findings(path) == []

    pcb = PCB.load(path)
    (fp,) = pcb.footprints
    lines = [g for g in fp.graphics if g.graphic_type == "line" and g.uuid != "keep"]
    kept = sum(math.dist(g.start, g.end) for g in lines)
    chord = 2 * math.sqrt(_reach() ** 2 - 0.05**2)
    # Only the chord through the keep-out goes (quad_segs tolerance).
    assert kept == pytest.approx(6.0 - chord, abs=2e-3)
    # The untouched line is byte-identical.
    assert [n for n in _silk_nodes(path) if '"keep"' in n] == keep_before


def test_drill_at_line_endpoint_trims_one_end(tmp_path: Path):
    body = _fp(_line(0, -1, 3, -1)) + _via(130, 129)
    path = _write(tmp_path, body)
    (fix,) = _clip(path).fixes
    assert (fix.action, fix.pieces) == ("trimmed", 1)
    (g,) = PCB.load(path).footprints[0].graphics
    assert math.dist(g.start, g.end) == pytest.approx(3.0 - _reach(), abs=2e-3)
    assert _hole_findings(path) == []


def test_filled_polygon_partly_over_drill_is_cut_minimally(tmp_path: Path):
    square = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
    # Via on the polygon's right edge (local x=1): half the drill under ink.
    body = _fp(_poly(square)) + _via(131, 130)
    path = _write(tmp_path, body)
    before = _ink(path)
    assert len(_hole_findings(path)) == 1

    result = _clip(path)
    (fix,) = result.fixes
    assert (fix.element_type, fix.action, fix.pieces) == ("fp_poly", "trimmed", 1)
    assert _hole_findings(path) == []
    after = _ink(path)
    # Removed ink is (about) the keep-out disc's overlap with the old ink;
    # for a centre on the stroke's outer half that is under one disc.
    disc = math.pi * (DRILL / 2 + WIDTH / 2 + WIDTH / 2) ** 2
    assert 0 < before - after < disc
    assert after == pytest.approx(fix.new_area, rel=1e-3)


def test_filled_polygon_containing_drill_keeps_a_keyhole(tmp_path: Path):
    square = [(-2, -2), (2, -2), (2, 2), (-2, 2)]
    body = _fp(_poly(square)) + _via(130, 130)
    path = _write(tmp_path, body)
    before = _ink(path)
    (fix,) = _clip(path).fixes
    assert (fix.action, fix.pieces) == ("trimmed", 1)
    assert _hole_findings(path) == []
    removed = before - _ink(path)
    # Only the drill itself (plus the 1 um safety margin) loses its ink: the
    # cut polygon's own stroke grows back up to the drill edge.
    assert removed == pytest.approx(math.pi * (DRILL / 2 + 1e-3) ** 2, rel=0.02)


def test_polygon_wholly_under_drill_is_removed(tmp_path: Path):
    tiny = [(-0.05, -0.05), (0.05, -0.05), (0.05, 0.05)]
    body = _fp(_poly(tiny) + _line(-3, 2, 3, 2, uid="keep")) + _via(130, 130)
    path = _write(tmp_path, body)
    (fix,) = _clip(path).fixes
    assert (fix.action, fix.pieces) == ("removed", 0)
    assert _hole_findings(path) == []
    assert len(_silk_nodes(path)) == 1


def test_unfilled_polygon_outline_becomes_lines(tmp_path: Path):
    square = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
    body = _fp(_poly(square, fill="no")) + _via(131, 130)
    path = _write(tmp_path, body)
    (fix,) = _clip(path).fixes
    assert fix.action == "trimmed"
    assert _hole_findings(path) == []
    graphics = PCB.load(path).footprints[0].graphics
    assert {g.graphic_type for g in graphics} == {"line"}
    assert len(graphics) == fix.pieces


def test_back_side_silk_is_clipped(tmp_path: Path):
    body = _fp(_line(-3, 0, 3, 0, layer="B.SilkS")) + _via(130, 130)
    path = _write(tmp_path, body)
    (fix,) = _clip(path).fixes
    assert (fix.layer, fix.pieces) == ("B.SilkS", 2)
    assert _hole_findings(path) == []


def test_rotated_footprint_is_clipped_in_its_own_frame(tmp_path: Path):
    body = _fp(_line(-3, -1, 3, -1), at="130 130 90") + _via(129, 130)
    path = _write(tmp_path, body)
    assert len(_hole_findings(path)) == 1
    (fix,) = _clip(path).fixes
    assert fix.pieces == 2
    assert _hole_findings(path) == []


def test_pth_and_npth_holes_clip_too(tmp_path: Path):
    body = _fp(
        _line(-3, 0, 3, 0)
        + ' (pad "1" thru_hole circle (at 2 0) (size 1.2 1.2) (drill 0.8) (layers "*.Cu"))'
        + ' (pad "" np_thru_hole circle (at -2 0) (size 1 1) (drill 1) (layers "*.Cu"))'
    )
    path = _write(tmp_path, body)
    assert len(_hole_findings(path)) == 2
    result = _clip(path)
    assert len(result.fixes) == 1
    assert sorted(result.fixes[0].holes) == ["U1 pad  hole", "U1 pad 1 hole"]
    assert _hole_findings(path) == []


def test_positive_clearance_is_honoured(tmp_path: Path):
    from shapely.geometry import Point

    body = _fp(_line(-3, 0, 3, 0)) + _via(130, 130)
    path = _write(tmp_path, body)
    _clip(path, min_clearance_mm=0.2)
    pcb = PCB.load(path)
    (fp,) = pcb.footprints
    hole = Point(pcb.vias[0].position).buffer(DRILL / 2)
    xf = _fp_transform(fp)
    for g in fp.graphics:
        assert _stroke_geometry(g, xf).distance(hole) >= 0.2


def test_untouched_board_is_byte_identical_and_rerun_is_noop(tmp_path: Path):
    body = _fp(_line(-3, -1, 3, -1)) + _via(130, 129)
    path = _write(tmp_path, body)
    _clip(path)
    once = path.read_bytes()
    result = _clip(path)
    assert result.fixes == []
    assert path.read_bytes() == once


def test_clip_is_deterministic(tmp_path: Path):
    body = _fp(_poly([(-2, -2), (2, -2), (2, 2), (-2, 2)]) + _line(-3, 3, 3, 3)) + _via(130, 130)
    outputs = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        path = _write(tmp_path / name, body)
        _clip(path)
        outputs.append(path.read_bytes())
    assert outputs[0] == outputs[1]


def test_arc_over_drill_is_reported_not_clipped(tmp_path: Path):
    arc = (
        "(fp_arc (start -1 0) (mid 0 -1) (end 1 0) (stroke (width 0.15) (type solid))"
        ' (layer "F.SilkS") (uuid "a1"))'
    )
    body = _fp(arc) + _via(130, 129)
    path = _write(tmp_path, body)
    result = _clip(path)
    assert result.fixes == []
    (skip,) = result.skipped
    assert skip.element_type == "fp_arc"
    assert len(result.residual) == 1


def test_text_is_left_for_the_placer(tmp_path: Path):
    body = _fp(
        '(fp_text reference "C6" (at 0 0) (layer "F.SilkS")'
        " (effects (font (size 1 1) (thickness 0.15))))",
        ref="C6",
    ) + _via(130, 130)
    path = _write(tmp_path, body)
    before = path.read_bytes()
    result = _clip(path)
    assert result.fixes == [] and result.residual == []
    assert result.text_residual == ["C6 (reference) / via at (130.000, 130.000)"]
    assert path.read_bytes() == before


def test_dry_run_does_not_modify(tmp_path: Path):
    body = _fp(_line(-3, 0, 3, 0)) + _via(130, 130)
    path = _write(tmp_path, body)
    before = path.read_bytes()
    repairer = SilkscreenRepairer(path)
    result = repairer.repair_hole_clearance(dry_run=True)
    assert len(result.fixes) == 1
    repairer.save()
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_fix_silkscreen_clip_to_holes_cli(tmp_path: Path, capsys):
    import json

    from kicad_tools.cli.fix_silkscreen_cmd import main

    body = _fp(_line(-3, 0, 3, 0)) + _via(130, 130)
    path = _write(tmp_path, body)
    assert main([str(path), "--clip-to-holes", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total_hole_clearance_fixed"] == 1
    assert data["hole_clearance_residual"] == []
    assert _hole_findings(path) == []


def test_fix_silkscreen_clip_to_holes_fails_on_residual(tmp_path: Path):
    from kicad_tools.cli.fix_silkscreen_cmd import main

    arc = (
        "(fp_arc (start -1 0) (mid 0 -1) (end 1 0) (stroke (width 0.15) (type solid))"
        ' (layer "F.SilkS") (uuid "a1"))'
    )
    path = _write(tmp_path, _fp(arc) + _via(130, 129))
    assert main([str(path), "--clip-to-holes", "--quiet"]) == 1


def test_kct_dispatch_forwards_hole_flags(tmp_path: Path):
    from kicad_tools.cli import main as kct_main

    body = _fp(_line(-3, 0, 3, 0)) + _via(130, 130)
    path = _write(tmp_path, body)
    assert (
        kct_main(
            [
                "fix-silkscreen",
                str(path),
                "--mfr",
                "jlcpcb",
                "--clip-to-holes",
                "--hole-clearance",
                "0.1",
            ]
        )
        == 0
    )
    assert _hole_findings(path) == []


# ---------------------------------------------------------------------------
# Pin-1 marker loss report (Issue #9077)
# ---------------------------------------------------------------------------

_PADS_3 = (
    ' (pad "1" smd rect (at -1.5 0) (size 1 0.5) (layers "F.Cu" "F.Mask"))'
    ' (pad "2" smd rect (at 0 0) (size 1 0.5) (layers "F.Cu" "F.Mask"))'
    ' (pad "3" smd rect (at 1.5 0) (size 1 0.5) (layers "F.Cu" "F.Mask"))'
)


def test_marker_wholly_under_drill_is_removed_and_reported(tmp_path: Path):
    # A filled pin-1 dot ~0.9mm left of pad 1, wholly inside a via drill's
    # keep-out: the clip erases it outright, and the rule that recognised it
    # before the clip must flag the footprint after.  A dry run edits
    # nothing, so it never reports a loss.
    dot = _poly([(-2.84, -0.04), (-2.76, -0.04), (-2.76, 0.04)])
    path = _write(tmp_path, _fp(_PADS_3 + dot) + _via(127.2, 130))
    assert SilkscreenRepairer(path).repair_hole_clearance(dry_run=True).marker_lost == []

    result = _clip(path)
    (fix,) = result.fixes
    assert (fix.element_type, fix.action, fix.pieces) == ("fp_poly", "removed", 0)
    assert result.marker_lost == ["U1"]


def test_cli_reports_marker_loss(tmp_path: Path, capsys):
    import json

    from kicad_tools.cli.fix_silkscreen_cmd import main

    body = _fp(_PADS_3 + _poly([(-2.84, -0.04), (-2.76, -0.04), (-2.76, 0.04)])) + _via(127.2, 130)
    for fmt, expect in (
        ("text", "pin-1 / polarity marker lost (re-add clear of the hole): U1"),
        ("summary", "1 pin-1 marker(s) lost"),
    ):
        (tmp_path / fmt).mkdir()
        path = _write(tmp_path / fmt, body)
        assert main([str(path), "--clip-to-holes", "--format", fmt]) == 0
        assert expect in capsys.readouterr().out
    (tmp_path / "json").mkdir()
    path = _write(tmp_path / "json", body)
    assert main([str(path), "--clip-to-holes", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["hole_clearance_marker_lost"] == ["U1"]


def test_trimmed_marker_that_survives_is_not_reported(tmp_path: Path):
    # Board 03 U1 shape: the drill takes a bite out of a bigger marker and
    # the remainder is still recognised -- no loss, no report.
    triangle = _poly([(-3.3, -0.4), (-3.3, 0.4), (-2.5, 0)])
    path = _write(tmp_path, _fp(_PADS_3 + triangle) + _via(126.7, 130))
    result = _clip(path)
    (fix,) = result.fixes
    assert fix.element_type == "fp_poly" and fix.pieces == 1
    assert _hole_findings(path) == []
    assert result.marker_lost == []


def test_footprint_without_marker_is_not_reported(tmp_path: Path):
    # U1 never had a marker: the rule flags it before *and* after the clip,
    # so the before/after diff -- not the bare after-state -- is reported.
    path = _write(tmp_path, _fp(_PADS_3 + _line(-3, 3, 3, 3)) + _via(130, 133))
    result = _clip(path)
    assert len(result.fixes) == 1
    assert result.marker_lost == []
