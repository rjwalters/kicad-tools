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
