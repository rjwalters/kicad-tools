"""Tests for the silkscreen reference-designator placement solver (issue #5030).

``kct fix-silkscreen`` repairs undersized silk stroke widths and text
heights, but has no way to *move* a reference designator that collides with
a pad, other silk, a neighboring component's label, or the board edge.
``kicad_tools.silkscreen.place_refs.SilkRefPlacer`` (wired to ``kct
place-silk-refs``) is the missing placement solver.

The dense fixture below covers every acceptance criterion in one board:

* ``R1`` -- reference text starts directly over one of its own pads
  ("text over pads").
* ``C1``/``C2`` -- two footprints whose default reference positions overlap
  each other ("overlapping neighbors").
* ``Q1`` -- boxed in by a giant neighbor's courtyard (``K1``) on every side;
  no collision-free candidate exists anywhere in the search radius, and its
  own starting position is clear of ITS OWN courtyard -> ``unplaceable``.
* ``Q2`` -- same giant-neighbor trap, but its starting position already sits
  on top of its OWN courtyard -> ``under_component_fallback`` (a distinct,
  explicitly-reported outcome from plain ``unplaceable``).
* ``K1`` -- the giant static keepout neighbor; its own reference is hidden
  so it never participates as a movable element.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.place_silk_refs_cmd import main as cli_main
from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB
from kicad_tools.silkscreen.place_refs import PlaceSilkRefsResult, SilkRefPlacer
from kicad_tools.validate.rules.silkscreen import check_all_silkscreen

pytest.importorskip("shapely")


def _footprint_block(
    ref: str,
    position: tuple[float, float],
    *,
    pads: str = "",
    courtyard: str = "",
    ref_local: tuple[float, float, float] = (0.0, 0.0, 0.0),
    hidden: bool = False,
) -> str:
    rx, ry, rrot = ref_local
    at = f"{rx} {ry} {rrot}" if rrot else f"{rx} {ry}"
    hide = " (hide yes)" if hidden else ""
    return f"""
  (footprint "TestFP"
    (layer "F.Cu")
    (at {position[0]} {position[1]})
    (fp_text reference "{ref}" (at {at}) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15)){hide}))
    {pads}
    {courtyard}
  )"""


def _pad(number: str, position: tuple[float, float], size: tuple[float, float] = (0.6, 0.6)) -> str:
    return (
        f'(pad "{number}" smd rect (at {position[0]} {position[1]}) '
        f'(size {size[0]} {size[1]}) (layers "F.Cu") (net 0 ""))'
    )


def _crtyd_rect(p0: tuple[float, float], p1: tuple[float, float]) -> str:
    return (
        f"(fp_rect (start {p0[0]} {p0[1]}) (end {p1[0]} {p1[1]}) "
        f'(stroke (width 0.05) (type solid)) (layer "F.CrtYd") (fill none))'
    )


def _dense_fixture_text() -> str:
    r1 = _footprint_block(
        "R1",
        (10, 10),
        pads=_pad("1", (-0.6, 0)) + " " + _pad("2", (0.6, 0)),
        ref_local=(0.6, 0, 0),  # directly on top of pad 2 -- "text over pads"
    )
    # C1/C2: default reference positions overlap each other.
    c1 = _footprint_block("C1", (30, 10), ref_local=(0, -1, 0))
    c2 = _footprint_block("C2", (30.4, 9.5), ref_local=(0, 0, 0))
    # Q1: boxed in by K1's giant courtyard; own start position is clear of
    # its OWN (small) courtyard -> every candidate fails -> "unplaceable".
    q1 = _footprint_block(
        "Q1",
        (60, 30),
        courtyard=_crtyd_rect((-1, -1), (1, 1)),
        ref_local=(3, 3, 0),
    )
    # Q2: same trap, but starts ON TOP of its own courtyard ->
    # "under_component_fallback".
    q2 = _footprint_block(
        "Q2",
        (60, 40),
        courtyard=_crtyd_rect((-1, -1), (1, 1)),
        ref_local=(0, 0, 0),
    )
    # K1: giant static keepout neighbor enclosing Q1 and Q2's entire search
    # radius on every side. Reference hidden -- never itself a movable
    # element.
    k1 = _footprint_block(
        "K1",
        (60, 35),
        courtyard=_crtyd_rect((-20, -25), (20, 25)),
        hidden=True,
    )

    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (gr_line (start 0 0) (end 100 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_line (start 100 0) (end 100 60) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_line (start 100 60) (end 0 60) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_line (start 0 60) (end 0 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  {r1}
  {c1}
  {c2}
  {q1}
  {q2}
  {k1}
)
"""


@pytest.fixture
def dense_pcb(tmp_path: Path) -> Path:
    path = tmp_path / "dense.kicad_pcb"
    path.write_text(_dense_fixture_text())
    return path


def _rules():
    return get_profile("jlcpcb").get_design_rules(layers=2)


def _plan(path: Path, **kwargs):
    placer = SilkRefPlacer(path)
    result = placer.plan(clearance_mm=0.15, **kwargs)
    return placer, result


# ---------------------------------------------------------------------------
# Dry run: no bytes touched
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_plan_alone_never_touches_the_file(self, dense_pcb: Path):
        """Calling plan() (never apply()/save()) leaves the file byte-identical."""
        original = dense_pcb.read_bytes()
        _plan(dense_pcb)
        assert dense_pcb.read_bytes() == original

    def test_cli_dry_run_leaves_file_unchanged(self, dense_pcb: Path):
        original = dense_pcb.read_bytes()
        rc = cli_main([str(dense_pcb), "--dry-run", "--quiet"])
        assert rc == 0
        assert dense_pcb.read_bytes() == original

    def test_cli_dry_run_reports_planned_moves_without_applying(self, dense_pcb: Path, capsys):
        rc = cli_main([str(dense_pcb), "--dry-run", "--format", "json"])
        assert rc == 0
        import json

        data = json.loads(capsys.readouterr().out)
        assert data["dry_run"] is True
        assert data["total_moved"] == 2  # R1, C1
        assert data["total_unplaceable"] == 1  # Q1
        assert data["total_under_component_fallback"] == 1  # Q2


# ---------------------------------------------------------------------------
# Apply: collisions resolved, geometry/labels preserved
# ---------------------------------------------------------------------------


class TestApplyResolvesCollisions:
    def test_dense_fixture_classifies_every_reference_correctly(self, dense_pcb: Path):
        _, result = _plan(dense_pcb)
        by_ref = {p.footprint_ref: p for p in result.placements}

        assert by_ref["R1"].status == "moved"
        assert by_ref["C1"].status == "moved"
        assert by_ref["C2"].status == "unchanged"
        assert by_ref["Q1"].status == "unplaceable"
        assert by_ref["Q2"].status == "under_component_fallback"
        # K1's reference is hidden -- never a candidate for placement at all.
        assert "K1" not in by_ref

    def test_apply_resolves_silk_over_copper_and_overlap(self, dense_pcb: Path, tmp_path: Path):
        rules = _rules()
        before = check_all_silkscreen(PCB.load(dense_pcb), rules)
        assert len(before) > 0  # sanity: the fixture is genuinely dirty

        placer, result = _plan(dense_pcb)
        applied = placer.apply(result)
        assert applied == 2  # R1 + C1

        out = tmp_path / "applied.kicad_pcb"
        placer.save(out)

        after = check_all_silkscreen(PCB.load(out), rules)
        assert len(after) == 0, [tuple(v.items) for v in after.violations]

    def test_apply_preserves_footprint_and_pad_geometry(self, dense_pcb: Path, tmp_path: Path):
        """Coordinate-only mutation: footprints, pads, and nets are untouched."""
        placer, result = _plan(dense_pcb)
        placer.apply(result)
        out = tmp_path / "applied.kicad_pcb"
        placer.save(out)

        before = PCB.load(dense_pcb)
        after = PCB.load(out)
        for ref in ("R1", "C1", "C2", "Q1", "Q2", "K1"):
            fp_before = before.get_footprint(ref)
            fp_after = after.get_footprint(ref)
            assert fp_before.position == fp_after.position, ref
            assert fp_before.rotation == fp_after.rotation, ref
            assert len(fp_before.pads) == len(fp_after.pads), ref
            for pad_before, pad_after in zip(fp_before.pads, fp_after.pads, strict=True):
                assert pad_before.position == pad_after.position
                assert pad_before.size == pad_after.size
                assert pad_before.net_number == pad_after.net_number
                assert pad_before.net_name == pad_after.net_name

    def test_apply_preserves_label_visibility_height_and_stroke(
        self, dense_pcb: Path, tmp_path: Path
    ):
        """Moved references keep their text, visibility, height, and stroke width.

        The solver must never "fix" a collision by hiding, shrinking, or
        deleting the label -- only its (x, y, [angle]) may change.
        """
        placer, result = _plan(dense_pcb)
        placer.apply(result)
        out = tmp_path / "applied.kicad_pcb"
        placer.save(out)

        before = PCB.load(dense_pcb)
        after = PCB.load(out)
        for ref in ("R1", "C1"):  # the two that actually moved
            text_before = next(
                t for t in before.get_footprint(ref).texts if t.text_type == "reference"
            )
            text_after = next(
                t for t in after.get_footprint(ref).texts if t.text_type == "reference"
            )
            assert text_before.text == text_after.text
            assert text_before.font_size == text_after.font_size
            assert text_before.font_thickness == text_after.font_thickness
            assert text_before.hidden is False
            assert text_after.hidden is False
            # And it actually did move (regression guard against a no-op apply).
            assert text_before.position != text_after.position

    def test_unmoved_reference_position_is_byte_stable(self, dense_pcb: Path, tmp_path: Path):
        """C2 (status 'unchanged') is not rewritten even though it was visited."""
        placer, result = _plan(dense_pcb)
        placer.apply(result)
        out = tmp_path / "applied.kicad_pcb"
        placer.save(out)

        before = PCB.load(dense_pcb)
        after = PCB.load(out)
        text_before = next(
            t for t in before.get_footprint("C2").texts if t.text_type == "reference"
        )
        text_after = next(t for t in after.get_footprint("C2").texts if t.text_type == "reference")
        assert text_before.position == text_after.position


# ---------------------------------------------------------------------------
# Impossible placements: reported, never silently dropped
# ---------------------------------------------------------------------------


class TestImpossiblePlacementsAreReported:
    def test_unplaceable_reference_left_exactly_where_it_was(self, dense_pcb: Path, tmp_path: Path):
        placer, result = _plan(dense_pcb)
        q1 = next(p for p in result.placements if p.footprint_ref == "Q1")
        assert q1.status == "unplaceable"
        assert q1.old_position == q1.new_position
        assert q1.reason  # a human-readable reason is always populated

        placer.apply(result)
        out = tmp_path / "applied.kicad_pcb"
        placer.save(out)
        after = PCB.load(out)
        text_after = next(t for t in after.get_footprint("Q1").texts if t.text_type == "reference")
        text_before = next(
            t for t in PCB.load(dense_pcb).get_footprint("Q1").texts if t.text_type == "reference"
        )
        assert text_after.position == text_before.position
        assert text_after.hidden is False  # never hidden as a destructive fallback
        assert text_after.text == "Q1"  # never deleted/renamed

    def test_under_component_fallback_is_distinct_from_unplaceable(self, dense_pcb: Path):
        """Q2 (already on its own courtyard) is flagged distinctly from Q1."""
        _, result = _plan(dense_pcb)
        q2 = next(p for p in result.placements if p.footprint_ref == "Q2")
        assert q2.status == "under_component_fallback"
        assert q2.old_position == q2.new_position
        assert q2 in result.under_component_fallback
        assert q2 not in result.unplaceable
        assert result.total_unplaceable == 2  # both count toward the aggregate total

    def test_apply_never_moves_unplaceable_or_fallback_references(
        self, dense_pcb: Path, tmp_path: Path
    ):
        placer, result = _plan(dense_pcb)
        applied = placer.apply(result)
        # Only the two genuinely-moved references are ever written.
        assert applied == len(result.moved) == 2
        assert placer._ref_nodes  # sanity: nodes were discovered at all

    def test_cli_text_output_names_unplaceable_and_fallback_refs(self, dense_pcb: Path, capsys):
        rc = cli_main([str(dense_pcb), "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Q1" in out
        assert "Q2" in out
        assert "could not be placed cleanly" in out
        assert "fell back under their own component body" in out


# ---------------------------------------------------------------------------
# Configurable clearance
# ---------------------------------------------------------------------------


class TestConfigurableClearance:
    def test_tighter_clearance_still_moves_the_same_references(self, dense_pcb: Path):
        """Chorus v25's concrete result: tightening clearance (.05 -> .15mm)
        still finds a valid placement for every genuinely movable reference.
        """
        placer = SilkRefPlacer(dense_pcb)
        loose = placer.plan(clearance_mm=0.05)
        tight = placer.plan(clearance_mm=0.15)
        assert {p.footprint_ref for p in loose.moved} == {p.footprint_ref for p in tight.moved}

    def test_zero_clearance_is_a_valid_configuration(self, dense_pcb: Path):
        """clearance_mm=0 (bare non-overlap) does not raise -- it is a real,
        if permissive, manufacturer profile."""
        placer = SilkRefPlacer(dense_pcb)
        result = placer.plan(clearance_mm=0.0)
        assert result.clearance_mm == 0.0


# ---------------------------------------------------------------------------
# Rendered review artifact (DRC alone does not prove readable placement)
# ---------------------------------------------------------------------------


class TestRenderedReviewArtifact:
    def test_render_svg_produces_a_nonempty_artifact(self, dense_pcb: Path, tmp_path: Path):
        placer, result = _plan(dense_pcb)
        svg_path = tmp_path / "review.svg"
        returned = placer.render_svg(result, svg_path)

        assert returned == svg_path
        assert svg_path.exists()
        content = svg_path.read_text()
        assert content.startswith("<svg")
        assert content.rstrip().endswith("</svg>")

    def test_render_svg_labels_every_reference_and_distinguishes_status(
        self, dense_pcb: Path, tmp_path: Path
    ):
        placer, result = _plan(dense_pcb)
        svg_path = tmp_path / "review.svg"
        placer.render_svg(result, svg_path)
        content = svg_path.read_text()

        # Every movable reference is visible by name in the artifact.
        for ref in ("R1", "C1", "C2", "Q1", "Q2"):
            assert f">{ref}<" in content
        # The legend documents the color coding a reviewer needs (moved vs.
        # unchanged vs. unplaceable) -- this is what makes it a *review*
        # artifact and not just a geometry dump.
        assert "unplaceable" in content.lower() or "fallback" in content.lower()

    def test_render_svg_requires_plan_first(self, dense_pcb: Path, tmp_path: Path):
        placer = SilkRefPlacer(dense_pcb)
        with pytest.raises(RuntimeError):
            placer.render_svg(PlaceSilkRefsResult(clearance_mm=0.15), tmp_path / "x.svg")

    def test_cli_render_flag_writes_artifact_in_dry_run(self, dense_pcb: Path, tmp_path: Path):
        svg_path = tmp_path / "review.svg"
        original = dense_pcb.read_bytes()
        rc = cli_main([str(dense_pcb), "--dry-run", "--quiet", "--render", str(svg_path)])
        assert rc == 0
        assert svg_path.exists()
        assert svg_path.stat().st_size > 0
        # The render is a read-only review step -- it must not touch the board.
        assert dense_pcb.read_bytes() == original


# ---------------------------------------------------------------------------
# CLI wiring smoke tests
# ---------------------------------------------------------------------------


class TestCLIWiring:
    def test_kct_place_silk_refs_is_registered(self):
        from kicad_tools.cli import parser as cli_parser

        parsed_parser = cli_parser.create_parser()
        args = parsed_parser.parse_args(["place-silk-refs", "board.kicad_pcb", "--dry-run"])
        assert args.command == "place-silk-refs"

    def test_dispatch_reaches_the_command_handler(self, dense_pcb: Path, capsys):
        from kicad_tools.cli import main as cli_entry

        rc = cli_entry(["place-silk-refs", str(dense_pcb), "--dry-run", "--format", "summary"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "clearance" in out.lower()

    def test_missing_file_reports_error(self, tmp_path: Path):
        rc = cli_main([str(tmp_path / "does-not-exist.kicad_pcb")])
        assert rc == 1

    def test_wrong_suffix_reports_error(self, tmp_path: Path):
        bogus = tmp_path / "board.txt"
        bogus.write_text("not a pcb")
        rc = cli_main([str(bogus)])
        assert rc == 1


def _orientation_fixture(tmp_path, *, angle, fp_angle=0, obstacle=(0, 2), kind="pad"):
    import math

    # Reference local (5,0) moves with the footprint, but its stored angle is
    # already board-frame. Place the obstacle in board coordinates independently.
    theta = math.radians(fp_angle)
    cx, cy = 10 + 5 * math.cos(theta), 10 - 5 * math.sin(theta)
    ox, oy = cx + obstacle[0], cy + obstacle[1]
    extra = (
        _footprint_block("Z", (ox, oy), pads=_pad("1", (0, 0)), hidden=True)
        if kind == "pad"
        else _footprint_block("A", (ox, oy), ref_local=(0, 0, 90))
    )
    path = tmp_path / "orientation.kicad_pcb"
    path.write_text(f"""(kicad_pcb (version 20240108) (generator test)
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user))
      (setup (pad_to_mask_clearance 0)) (net 0 "")
      (gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") (width .05))
      (footprint "Test" (layer "F.Cu") (at 10 10 {fp_angle})
        (fp_text reference "R11111111" (at 5 0 {angle}) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness .15)))))
      {extra})""")
    return path, (cx, cy)


@pytest.mark.parametrize(
    "angle,fp_angle,obstacle,collides",
    [
        (90, 0, (0, 2), True),
        (90, 0, (2, 0), False),
        (30, 90, (2.598, -1.5), True),
        (30, 90, (1.5, 2.598), False),
        (-30, 45, (2.598, 1.5), True),
        (-30, 45, (1.5, -2.598), False),
    ],
)
def test_existing_text_orientation_is_absolute(tmp_path, angle, fp_angle, obstacle, collides):
    path, center = _orientation_fixture(tmp_path, angle=angle, fp_angle=fp_angle, obstacle=obstacle)
    placer = SilkRefPlacer(path)
    result = placer.plan()
    ref = next(p for p in result.placements if p.footprint_ref == "R11111111")
    assert ref.old_position == pytest.approx(center)
    assert (ref.status == "moved") == collides
    assert ref.new_rotation == angle
    if not collides:
        assert ref.status == "unchanged"
    placer.apply(result)
    # Text dimensions and footprint placement survive the coordinate update.
    placer.save()
    reparsed = SilkRefPlacer(path)
    text = next(t for t in reparsed.pcb.footprints[0].texts if t.text_type == "reference")
    assert text.rotation == angle
    assert text.font_size == (1, 1) and text.font_thickness == 0.15 and not text.hidden
    assert reparsed.pcb.footprints[0].position == (10, 10)
    assert reparsed.pcb.footprints[0].rotation == fp_angle


def test_square_font_rotates_complete_label_and_svg(tmp_path):
    import xml.etree.ElementTree as ET

    path, center = _orientation_fixture(tmp_path, angle=0, obstacle=(2, 0))
    placer = SilkRefPlacer(path)
    result = placer.plan(allow_rotate=True)
    ref = result.placements[0]
    assert ref.status == "moved" and ref.new_rotation == 90
    assert ref.new_position == center
    svg = placer.render_svg(result, tmp_path / "rotated.svg")
    root = ET.parse(svg).getroot()
    ns = {"s": "http://www.w3.org/2000/svg"}
    poly = next(p for p in root.findall("s:polygon", ns) if p.find("s:title", ns) is not None)
    points = [tuple(map(float, pair.split(","))) for pair in poly.attrib["points"].split()]
    assert max(y for x, y in points) - min(y for x, y in points) > 5 * (
        max(x for x, y in points) - min(x for x, y in points)
    )
    label = root.find("s:text", ns)
    assert label is not None and label.attrib["transform"].startswith("rotate(-90 ")
    placer.apply(result)
    placer.save()
    assert SilkRefPlacer(path).plan().placements[0].status == "unchanged"


@pytest.mark.parametrize("kind", ["fp_text", "property", "gr_text"])
def test_rotated_static_text_obstacle(tmp_path, kind):
    path, _ = _orientation_fixture(tmp_path, angle=90, obstacle=(8, 8))
    # A short reference at (15,12) intersects the vertical static label but
    # clears its incorrect horizontal envelope. No footprint angle is added.
    source = path.read_text().replace('"R11111111" (at 5 0 90)', '"R" (at 5 2)')
    effects = '(layer "F.SilkS") (effects (font (size 1 1) (thickness .15)))'
    if kind == "gr_text":
        extra = f'(gr_text "STATICLONG" (at 15 10 90) {effects})'
    else:
        tag = "fp_text user" if kind == "fp_text" else 'property "Value"'
        extra = f'(footprint "Static" (layer "F.Cu") (at 10 10 90) ({tag} "STATICLONG" (at 0 5 90) {effects}))'
    path.write_text(source.rstrip()[:-1] + extra + ")")
    placer = SilkRefPlacer(path)
    ref = next(p for p in placer.plan().placements if p.footprint_ref == "R")
    assert ref.status == "moved"


def test_rotated_dynamic_reference_obstacles(tmp_path):
    from shapely.affinity import rotate

    from kicad_tools.validate.rules.silkscreen import _text_bbox_geometry

    path, _ = _orientation_fixture(tmp_path, angle=90, obstacle=(0, 2), kind="reference")
    placer = SilkRefPlacer(path)
    result = placer.plan()
    assert result.moved
    geoms = []
    for ref in result.placements:
        box = _text_bbox_geometry(ref.footprint_ref, (1, 1), 0.15, ref.new_position)
        geoms.append(rotate(box, -ref.new_rotation, origin=ref.new_position))
    assert geoms[0].distance(geoms[1]) >= 0.15 - 1e-4


@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("options", [[], ["--dry-run"], ["--output", "explicit"]])
def test_duplicate_references_reject_without_mutating(tmp_path, capsys, hidden, options):
    path = tmp_path / "duplicate.kicad_pcb"
    # A hidden duplicate still aliases own-component obstacle identities.
    original_ref = "K1" if hidden else "C2"
    source = _dense_fixture_text().replace(f'reference "{original_ref}"', 'reference "C1"')
    path.write_text(source)
    original = path.read_bytes()
    placer = SilkRefPlacer(path)
    from kicad_tools.sexp import serialize_sexp

    before = serialize_sexp(placer.doc)
    with pytest.raises(ValueError, match="Duplicate footprint reference"):
        placer.plan()
    assert serialize_sexp(placer.doc) == before
    output = tmp_path / "result.kicad_pcb"
    args = [str(path), "--format", "json"] + [
        str(output) if x == "explicit" else x for x in options
    ]
    assert cli_main(args) == 1
    assert "Duplicate footprint reference" in capsys.readouterr().err
    assert path.read_bytes() == original
    assert not output.exists()
