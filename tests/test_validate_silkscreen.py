"""Tests for the geometric silkscreen DRC rules.

Covers the checks added in issue #3844:

- ``check_silk_over_copper`` -- silk text/graphics over exposed pad mask
  apertures (supersedes the crude ``silkscreen_over_pad`` centroid heuristic).
- ``check_silk_edge_clearance`` -- silk text/graphics too close to / crossing
  the ``Edge.Cuts`` board outline.

plus the check and counting-model change from issue #4612:

- ``check_silk_over_copper`` now emits one violation per **(silk item, mask
  aperture) pair**, matching ``kicad-cli pcb drc``, instead of de-duplicating
  to one violation per silk element (which under-reported ~2x and named an
  arbitrary member of the collision set).
- ``check_silk_overlap`` -- silk over other silk, the previously-missing
  producer for the already-wired ``silk_overlap`` violation type.

All emit ``severity="warning"`` so they do not block the manufacturing gate.

plus the polygon-geometry fix from issue #5811:

- ``_stroke_geometry`` models ``fp_poly`` / ``gr_poly`` silk in BOTH fill
  states (see ``TestSilkPolygonGeometry`` and the native-parity pair
  ``test_kct_poly_silk_floor_matches_measured_contract`` /
  ``test_native_poly_silk_floor_parity``).  Before the fix every polygon
  returned ``None``, so a filled pin-1 marker contributed no geometry at all
  and a real native ``silk_over_copper`` violation was invisible to kct.
- ``check_silk_coverage`` reports the primitives still NOT modeled
  (circle/arc) as ``info`` findings, so an omitted shape is visible as
  incomplete coverage instead of reading as a clean board
  (``TestSilkGeometryCoverage``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.manufacturers import get_profile, write_drc_constraints
from kicad_tools.schema.pcb import (
    PCB,
    BoardGraphic,
    Footprint,
    FootprintGraphic,
    FootprintText,
    GraphicLine,
    GraphicText,
    Pad,
)
from kicad_tools.sexp import SExp, parse_string
from kicad_tools.validate.rules.factory_clearance import check_silk_pad_clearance
from kicad_tools.validate.rules.silkscreen import (
    SILK_EDGE_CLEARANCE_MM,
    SILK_GEOMETRY_UNMODELED_RULE_ID,
    _stroke_geometry,
    check_all_silkscreen,
    check_silk_coverage,
    check_silk_edge_clearance,
    check_silk_over_copper,
    check_silk_overlap,
)


def _rules():
    return get_profile("jlcpcb").get_design_rules(layers=2)


def _empty_pcb() -> PCB:
    return PCB(SExp(name="kicad_pcb"))


def _make_footprint(
    *,
    reference: str = "U1",
    position: tuple[float, float] = (10.0, 10.0),
    rotation: float = 0.0,
    layer: str = "F.Cu",
    pads: list[Pad] | None = None,
    texts: list[FootprintText] | None = None,
    graphics: list[FootprintGraphic] | None = None,
) -> Footprint:
    return Footprint(
        name="TestFP",
        layer=layer,
        position=position,
        rotation=rotation,
        reference=reference,
        value="TEST",
        pads=pads or [],
        texts=texts or [],
        graphics=graphics or [],
    )


def _ref_text(
    *,
    text: str = "U1",
    position: tuple[float, float],
    layer: str = "F.SilkS",
    font_size: tuple[float, float] = (1.0, 1.0),
    font_thickness: float = 0.15,
    hidden: bool = False,
) -> FootprintText:
    return FootprintText(
        text_type="reference",
        text=text,
        position=position,
        layer=layer,
        font_size=font_size,
        font_thickness=font_thickness,
        hidden=hidden,
    )


def _smd_pad(
    *,
    number: str = "1",
    position: tuple[float, float],
    size: tuple[float, float] = (1.0, 1.0),
) -> Pad:
    return Pad(
        number=number,
        type="smd",
        shape="rect",
        position=position,
        size=size,
        layers=["F.Cu"],
    )


def _thru_hole_pad(
    *,
    number: str = "1",
    position: tuple[float, float],
    size: tuple[float, float] = (1.5, 1.5),
) -> Pad:
    return Pad(
        number=number,
        type="thru_hole",
        shape="circle",
        position=position,
        size=size,
        layers=["*.Cu"],
        drill=0.8,
    )


# ---------------------------------------------------------------------------
# silk_over_copper
# ---------------------------------------------------------------------------


class TestSilkOverCopper:
    def test_text_over_smd_pad_flags(self):
        """Silk text whose bbox covers an SMD pad aperture is flagged."""
        pcb = _empty_pcb()
        fp = _make_footprint(
            pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
            texts=[_ref_text(position=(0.0, 0.0))],
        )
        pcb._footprints.append(fp)

        results = check_silk_over_copper(pcb, _rules())

        assert len(results) == 1
        v = results.violations[0]
        assert v.rule_id == "silk_over_copper"
        assert v.severity == "warning"
        assert v.items[0].startswith("U1")
        assert "pad 1" in v.items[1]

    def test_text_clear_of_pad_passes(self):
        """Silk text well clear of all pads produces no violation."""
        pcb = _empty_pcb()
        fp = _make_footprint(
            pads=[_smd_pad(position=(0.0, 0.0), size=(1.0, 1.0))],
            texts=[_ref_text(position=(0.0, -5.0))],
        )
        pcb._footprints.append(fp)

        results = check_silk_over_copper(pcb, _rules())

        assert len(results) == 0
        assert results.passed is True

    def test_rotated_footprint_transform(self):
        """A 90-degree footprint still maps silk into the rotated pad frame.

        The text and pad share local (0,0); after a 90-degree rotation both
        land on the same board point, so the overlap must still be detected
        (exercises the radians(-rotation) transform-sign path).
        """
        pcb = _empty_pcb()
        fp = _make_footprint(
            rotation=90.0,
            pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
            texts=[_ref_text(position=(0.0, 0.0))],
        )
        pcb._footprints.append(fp)

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 1

    def test_rotated_footprint_offset_pad(self):
        """270-degree rotation maps an offset pad/text pair correctly.

        Pad at local (1,0) and text at local (1,0): both rotate to the same
        board point regardless of angle, so the overlap persists.  This guards
        against a transform that drops the rotation entirely.
        """
        pcb = _empty_pcb()
        fp = _make_footprint(
            rotation=270.0,
            pads=[_smd_pad(position=(1.0, 0.0), size=(2.0, 2.0))],
            texts=[_ref_text(position=(1.0, 0.0))],
        )
        pcb._footprints.append(fp)

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 1

    def test_silk_line_stroke_over_pad(self):
        """An fp_line silk stroke crossing a pad aperture is flagged."""
        pcb = _empty_pcb()
        fp = _make_footprint(
            texts=[],
            pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
            graphics=[
                FootprintGraphic(
                    graphic_type="line",
                    layer="F.SilkS",
                    stroke_width=0.2,
                    start=(-3.0, 0.0),
                    end=(3.0, 0.0),
                ),
            ],
        )
        pcb._footprints.append(fp)

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 1
        assert "fp_line" in results.violations[0].items[0]

    def test_thru_hole_pad_exposed_both_sides(self):
        """A back-side silk text over a thru-hole pad is flagged.

        Thru-hole pads expose copper on both sides, so silk on B.SilkS must
        still be checked against them even though the footprint is on F.Cu.
        """
        pcb = _empty_pcb()
        fp = _make_footprint(
            layer="F.Cu",
            pads=[_thru_hole_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
            texts=[_ref_text(position=(0.0, 0.0), layer="B.SilkS")],
        )
        pcb._footprints.append(fp)

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 1

    def test_smd_pad_not_exposed_on_opposite_side(self):
        """SMD copper on F.Cu does not collide with B.SilkS silk."""
        pcb = _empty_pcb()
        fp = _make_footprint(
            layer="F.Cu",
            pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
            texts=[_ref_text(position=(0.0, 0.0), layer="B.SilkS")],
        )
        pcb._footprints.append(fp)

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 0

    def test_hidden_text_skipped(self):
        """Hidden silk text never produces a silk_over_copper violation."""
        pcb = _empty_pcb()
        fp = _make_footprint(
            pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
            texts=[_ref_text(position=(0.0, 0.0), hidden=True)],
        )
        pcb._footprints.append(fp)

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 0

    def test_empty_text_skipped(self):
        """Zero-length text strings are ignored."""
        pcb = _empty_pcb()
        fp = _make_footprint(
            pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
            texts=[_ref_text(text="", position=(0.0, 0.0))],
        )
        pcb._footprints.append(fp)

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 0

    def test_silk_over_other_footprint_pad(self):
        """A reference field overlapping a *different* footprint's pad fires.

        This is the board-05 pattern (e.g. ref of Q1 over a pad of R20); the
        check builds a global aperture index, not a per-footprint one.
        """
        pcb = _empty_pcb()
        fp_text = _make_footprint(
            reference="Q1",
            position=(0.0, 0.0),
            texts=[_ref_text(text="Q1", position=(0.0, 0.0))],
        )
        fp_pad = _make_footprint(
            reference="R20",
            position=(0.0, 0.0),
            pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
        )
        pcb._footprints.extend([fp_text, fp_pad])

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 1
        assert results.violations[0].items[0].startswith("Q1")
        assert "R20" in results.violations[0].items[1]

    def test_one_violation_per_silk_aperture_pair(self):
        """A silk element overlapping two pads fires TWICE, once per pair.

        AC1 of #4612.  This test previously asserted the opposite (one
        violation per silk element); the de-dup was removed because it made
        kct's count incomparable with ``kicad-cli pcb drc``, which reports one
        violation per (silk item, mask aperture) pair.
        """
        pcb = _empty_pcb()
        fp = _make_footprint(
            pads=[
                _smd_pad(number="1", position=(-0.6, 0.0), size=(2.0, 2.0)),
                _smd_pad(number="2", position=(0.6, 0.0), size=(2.0, 2.0)),
            ],
            texts=[_ref_text(position=(0.0, 0.0), font_size=(1.5, 1.5))],
        )
        pcb._footprints.append(fp)

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 2
        assert [v.items[1] for v in results.violations] == ["U1 pad 1", "U1 pad 2"]

    def test_multi_pad_straddle_names_every_pad(self):
        """A refdes straddling 4 pads yields 4 violations naming all 4.

        AC3/AC9 of #4612 -- the board-05 ``U10`` pattern (a TSSOP reference
        field over pads 27/28/29/30).  Before the fix the loop broke at the
        first STRtree hit, so kct emitted a single violation naming an
        arbitrary member of the collision set.  This is the regression guard
        against a future "de-dup for readability" change silently
        reintroducing the under-count.
        """
        pcb = _empty_pcb()
        fp = _make_footprint(
            reference="U10",
            pads=[
                _smd_pad(number="27", position=(-1.8, 0.0), size=(1.0, 2.0)),
                _smd_pad(number="28", position=(-0.6, 0.0), size=(1.0, 2.0)),
                _smd_pad(number="29", position=(0.6, 0.0), size=(1.0, 2.0)),
                _smd_pad(number="30", position=(1.8, 0.0), size=(1.0, 2.0)),
            ],
            texts=[_ref_text(text="U10", position=(0.0, 0.0), font_size=(2.0, 2.0))],
        )
        pcb._footprints.append(fp)

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) >= 3  # AC9: >= 3 apertures -> >= 3 violations
        assert [v.items[1] for v in results.violations] == [
            "U10 pad 27",
            "U10 pad 28",
            "U10 pad 29",
            "U10 pad 30",
        ]

    def test_violations_are_sorted_deterministically(self):
        """Emitted pairs are sorted by (silk label, pad label).

        ``STRtree.query`` order is unspecified, so the rule sorts before
        emitting; downstream reporting and any pair-set diff against kicad-cli
        depend on that being stable.
        """
        pcb = _empty_pcb()
        fp = _make_footprint(
            reference="U1",
            pads=[
                _smd_pad(number="3", position=(1.2, 0.0), size=(1.0, 2.0)),
                _smd_pad(number="1", position=(-1.2, 0.0), size=(1.0, 2.0)),
                _smd_pad(number="2", position=(0.0, 0.0), size=(1.0, 2.0)),
            ],
            texts=[_ref_text(position=(0.0, 0.0), font_size=(2.0, 2.0))],
        )
        pcb._footprints.append(fp)

        pairs = [(v.items[0], v.items[1]) for v in check_silk_over_copper(pcb, _rules()).violations]
        assert pairs == sorted(pairs)


# ---------------------------------------------------------------------------
# silk_over_copper: untented-via mask openings (#4624)
# ---------------------------------------------------------------------------
#
# No committed board can serve as a fixture here: every board under ``boards/``
# sets ``(tenting (front yes) (back yes))``, so no fleet via has a mask opening
# at all.  These synthetic fixtures mirror hand-authored probe boards fed to
# ``kicad-cli pcb drc --severity-all --format json`` (KiCad 10.0.5, 2026-08-05),
# which measured (silk segments on BOTH F.SilkS and B.SilkS over one via):
#
#   no (tenting ...) anywhere (absent token)          -> 0   (default = tented)
#   setup (front yes) (back yes)                      -> 0
#   setup (front no) (back no)                        -> 2   (F and B)
#   board tented  + via (tenting (front no) (back no))  -> 2
#   board untented + via (tenting (front yes)(back yes))-> 0
#   board tented  + via (front no) (back yes)         -> 1   (F only)
#   board untented + via (front none) (back yes)      -> 1   (F only; none=inherit)
#
# i.e. the per-via override beats the board default in both directions, sides
# resolve independently, ``none`` inherits, and KiCad's absent-token default is
# TENTED.  The assertions below encode exactly those counts.


def _via_probe_pcb(
    *,
    setup_tenting: tuple[str, str] | None,
    via_tenting: tuple[str, str] | None,
    silk_layers: tuple[str, ...] = ("F.SilkS",),
) -> PCB:
    """Build a PCB with one GND via at (10, 10) and silk segment(s) across it.

    ``setup_tenting`` / ``via_tenting`` are ``(front, back)`` token pairs
    (``"yes"`` / ``"no"`` / ``"none"``) or ``None`` to omit the node entirely,
    exercising the real ``(tenting ...)`` parse path in both places.
    """
    setup_block = ""
    if setup_tenting is not None:
        setup_block = f"(tenting (front {setup_tenting[0]}) (back {setup_tenting[1]}))"
    via_block = ""
    if via_tenting is not None:
        via_block = f"(tenting (front {via_tenting[0]}) (back {via_tenting[1]}))"
    silk_lines = "".join(
        f"""
    (gr_line (start 8 10) (end 12 10)
        (stroke (width 0.2) (type solid))
        (layer "{layer}") (uuid "2222-{i}"))"""
        for i, layer in enumerate(silk_layers)
    )
    text = f"""(kicad_pcb
    (version 20260206)
    (generator "pcbnew")
    (setup
        (pad_to_mask_clearance 0)
        {setup_block}
    )
    (net 0 "")
    (net 1 "GND")
    (via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")
        {via_block}
        (net 1) (uuid "1111"))
    {silk_lines}
)"""
    from kicad_tools.sexp import parse_string

    return PCB(parse_string(text))


class TestSilkOverUntentedVia:
    def test_untented_via_under_silk_flags(self):
        """Board-untented via with silk across it yields one pair naming the via."""
        pcb = _via_probe_pcb(setup_tenting=("no", "no"), via_tenting=None)
        violations = check_silk_over_copper(pcb, _rules()).violations
        assert len(violations) == 1
        assert violations[0].items[1] == "via [GND] at (10.000, 10.000)"

    def test_tented_via_same_geometry_clean(self):
        """Same geometry with board-tented vias yields nothing (no regression)."""
        pcb = _via_probe_pcb(setup_tenting=("yes", "yes"), via_tenting=None)
        assert len(check_silk_over_copper(pcb, _rules())) == 0

    def test_absent_tenting_token_defaults_to_tented(self):
        """No ``(tenting ...)`` node anywhere -> tented -> no aperture.

        The absent-token default is MEASURED, not assumed: kicad-cli 10.0.5
        reports zero ``silk_over_copper`` findings on this exact probe board,
        and a fresh pcbnew board writes ``(front yes) (back yes)``.
        """
        pcb = _via_probe_pcb(
            setup_tenting=None, via_tenting=None, silk_layers=("F.SilkS", "B.SilkS")
        )
        assert len(check_silk_over_copper(pcb, _rules())) == 0

    def test_via_override_beats_board_tented_default(self):
        """Per-via untented override on a board-tented default IS reported."""
        pcb = _via_probe_pcb(setup_tenting=("yes", "yes"), via_tenting=("no", "no"))
        violations = check_silk_over_copper(pcb, _rules()).violations
        assert len(violations) == 1
        assert violations[0].items[1] == "via [GND] at (10.000, 10.000)"

    def test_via_override_beats_board_untented_default(self):
        """Per-via tented override on a board-untented default is NOT reported."""
        pcb = _via_probe_pcb(setup_tenting=("no", "no"), via_tenting=("yes", "yes"))
        assert len(check_silk_over_copper(pcb, _rules())) == 0

    def test_front_back_sides_resolve_independently(self):
        """A front-untented/back-tented via is an aperture only against F silk."""
        pcb = _via_probe_pcb(
            setup_tenting=("yes", "yes"),
            via_tenting=("no", "yes"),
            silk_layers=("F.SilkS", "B.SilkS"),
        )
        violations = check_silk_over_copper(pcb, _rules()).violations
        assert len(violations) == 1
        assert violations[0].layer == "F.SilkS"

    def test_none_token_inherits_board_default(self):
        """``none`` on a side falls through to the board default for that side."""
        pcb = _via_probe_pcb(
            setup_tenting=("no", "no"),
            via_tenting=("none", "yes"),
            silk_layers=("F.SilkS", "B.SilkS"),
        )
        violations = check_silk_over_copper(pcb, _rules()).violations
        assert len(violations) == 1
        assert violations[0].layer == "F.SilkS"

    def test_both_sides_untented_pairs_with_both_silk_sides(self):
        """F and B silk over a both-sides-untented via yield one pair each."""
        pcb = _via_probe_pcb(
            setup_tenting=("no", "no"),
            via_tenting=None,
            silk_layers=("F.SilkS", "B.SilkS"),
        )
        violations = check_silk_over_copper(pcb, _rules()).violations
        assert len(violations) == 2
        assert sorted(v.layer for v in violations) == ["B.SilkS", "F.SilkS"]

    def test_silk_clear_of_untented_via_not_flagged(self):
        """An untented via with silk elsewhere on the board yields nothing."""
        pcb = _via_probe_pcb(setup_tenting=("no", "no"), via_tenting=None)
        # Move the silk line well away from the via.
        pcb.graphics[0].start = (30.0, 30.0)
        pcb.graphics[0].end = (34.0, 30.0)
        assert len(check_silk_over_copper(pcb, _rules())) == 0


# ---------------------------------------------------------------------------
# silk_overlap (#4612)
# ---------------------------------------------------------------------------
#
# No committed board can serve as a fixture here: ``kicad-cli pcb drc
# --severity-all`` reports ZERO ``silk_overlap`` on every board under
# ``boards/`` (and ``silk_overlap`` is not in the report's ``ignored_checks``,
# so the test genuinely ran), because the fleet's silkscreen is reference
# designator text only -- no footprint silk graphics at all.  These synthetic
# fixtures were cross-checked against a hand-authored probe ``.kicad_pcb`` fed
# to ``kicad-cli pcb drc`` (KiCad 10.0.5, 2026-08-04), which returned exactly
# the two pairs modelled below:
#
#   silk_overlap | warning | Reference field of A1 <-> Segment of A1 on F.Silkscreen
#   silk_overlap | warning | Reference field of B1 <-> Reference field of C1
#
# i.e. kicad-cli counts BOTH same-footprint and cross-footprint pairs, which is
# the AC6 tiebreaker.


def _silk_line(
    *,
    start: tuple[float, float],
    end: tuple[float, float],
    layer: str = "F.SilkS",
    stroke_width: float = 0.15,
    uuid: str = "",
) -> FootprintGraphic:
    return FootprintGraphic(
        graphic_type="line",
        layer=layer,
        stroke_width=stroke_width,
        start=start,
        end=end,
        uuid=uuid,
    )


class TestSilkOverlap:
    def test_joined_outline_corner_is_not_a_collision(self):
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                graphics=[
                    _silk_line(start=(0, 0), end=(2, 0)),
                    _silk_line(start=(2, 0), end=(2, 2)),
                ]
            )
        )
        assert len(check_silk_overlap(pcb, _rules())) == 0

    def test_joined_outline_corner_tolerates_float_rounding(self):
        """A corner join whose endpoints differ by a few ULPs is still exempt.

        Regression for #4987: real KiCad-authored/generated footprint
        outlines routinely have adjacent line endpoints that are numerically
        close but not bit-identical (independent rounding at export time).
        The pre-fix exact-tuple-equality match dropped the join exemption for
        exactly this case, producing a false-positive ``silk_overlap``.
        """
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                graphics=[
                    # Nominally shares (2, 0) with the second line, but off by
                    # 1e-7mm -- three orders of magnitude below any
                    # manufacturing tolerance, and well within
                    # ``_CLEARANCE_EPSILON_MM`` (1e-4mm).
                    _silk_line(start=(0, 0), end=(2.0000001, 0)),
                    _silk_line(start=(2, 0), end=(2, 2)),
                ]
            )
        )
        assert len(check_silk_overlap(pcb, _rules())) == 0

    def test_closed_rectangle_outline_all_corners_exempt(self):
        """A full closed 4-line rectangle outline is exempt at every corner.

        Mirrors a standard footprint courtyard/outline: four ``fp_line``
        strokes meeting end-to-end at right angles.  Native kicad-cli reports
        zero ``silk_overlap`` findings for this shape (#4987) -- each of the
        four shared corners must be recognized as an intentional join, not
        just the first pair.
        """
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                graphics=[
                    _silk_line(start=(0, 0), end=(4, 0)),
                    _silk_line(start=(4, 0), end=(4, 3)),
                    _silk_line(start=(4, 3), end=(0, 3)),
                    _silk_line(start=(0, 3), end=(0, 0)),
                ]
            )
        )
        results = check_silk_overlap(pcb, _rules())
        assert len(results) == 0, [tuple(v.items) for v in results.violations]

    def test_collinear_overlap_with_shared_endpoint_still_flags(self):
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                graphics=[
                    _silk_line(start=(0, 0), end=(2, 0)),
                    _silk_line(start=(0, 0), end=(1, 0)),
                ]
            )
        )
        assert len(check_silk_overlap(pcb, _rules())) == 1

    @pytest.mark.parametrize("endpoints", [((0, 0), (0.02, 0)), ((0.02, 0), (0, 0))])
    @pytest.mark.parametrize("rotation", [0, 37, 90])
    def test_short_collinear_overlap_inside_joint_still_flags(self, endpoints, rotation):
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                rotation=rotation,
                graphics=[
                    _silk_line(start=(0, 0), end=(2, 0)),
                    _silk_line(start=endpoints[0], end=endpoints[1]),
                ],
            )
        )
        assert len(check_silk_overlap(pcb, _rules())) == 1

    @pytest.mark.parametrize("offset", [0, 1e-7, 1e-5, 2e-4])
    @pytest.mark.parametrize("reverse_a", [False, True])
    @pytest.mark.parametrize("reverse_b", [False, True])
    @pytest.mark.parametrize("swap", [False, True])
    @pytest.mark.parametrize("rotation", [0, 37])
    def test_offset_parallel_short_strokes_still_overlap(
        self, offset, reverse_a, reverse_b, swap, rotation
    ):
        """Endpoint rounding must not turn parallel overdraw into a corner."""
        a = [(0, 0), (2, 0)]
        b = [(0, offset), (0.05, offset)]
        if reverse_a:
            a.reverse()
        if reverse_b:
            b.reverse()
        graphics = [_silk_line(start=a[0], end=a[1]), _silk_line(start=b[0], end=b[1])]
        if swap:
            graphics.reverse()
        pcb = _empty_pcb()
        pcb._footprints.append(_make_footprint(rotation=rotation, graphics=graphics))
        assert len(check_silk_overlap(pcb, _rules())) == 1

    def test_straight_outline_continuation_is_not_a_collision(self):
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                graphics=[
                    _silk_line(start=(0, 0), end=(2, 0)),
                    _silk_line(start=(2, 0), end=(4, 0)),
                ]
            )
        )
        assert len(check_silk_overlap(pcb, _rules())) == 0

    def test_duplicate_outline_still_flags(self):
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                graphics=[
                    _silk_line(start=(0, 0), end=(2, 0)),
                    _silk_line(start=(0, 0), end=(2, 0)),
                ]
            )
        )
        assert len(check_silk_overlap(pcb, _rules())) == 1

    def test_two_footprints_refdes_overlap_flags(self):
        """Two footprints whose reference fields overlap yield one pair.

        Mirrors the probe fixture's ``B1 <-> C1`` finding.
        """
        pcb = _empty_pcb()
        pcb._footprints.extend(
            [
                _make_footprint(
                    reference="B1",
                    position=(10.0, 10.0),
                    texts=[_ref_text(text="B1", position=(0.0, 0.0))],
                ),
                _make_footprint(
                    reference="C1",
                    position=(10.4, 10.0),
                    texts=[_ref_text(text="C1", position=(0.0, 0.0))],
                ),
            ]
        )

        results = check_silk_overlap(pcb, _rules())

        assert len(results) == 1
        v = results.violations[0]
        assert v.rule_id == "silk_overlap"
        assert v.severity == "warning"
        assert {v.items[0], v.items[1]} == {"B1 (reference)", "C1 (reference)"}

    def test_negative_control_clear_silk_passes(self):
        """The same two footprints, moved apart, produce no violation."""
        pcb = _empty_pcb()
        pcb._footprints.extend(
            [
                _make_footprint(
                    reference="B1",
                    position=(10.0, 10.0),
                    texts=[_ref_text(text="B1", position=(0.0, 0.0))],
                ),
                _make_footprint(
                    reference="C1",
                    position=(10.0, 20.0),
                    texts=[_ref_text(text="C1", position=(0.0, 0.0))],
                ),
            ]
        )

        results = check_silk_overlap(pcb, _rules())
        assert len(results) == 0
        assert results.passed is True

    def test_same_footprint_refdes_over_own_graphic_flags(self):
        """A refdes overlapping its OWN footprint's silk art counts (AC6).

        kicad-cli is the tiebreaker and it reports this pair
        (``Reference field of A1 <-> Segment of A1 on F.Silkscreen``), so the
        rule deliberately does not filter same-footprint pairs.
        """
        pcb = _empty_pcb()
        fp = _make_footprint(
            reference="A1",
            texts=[_ref_text(text="A1", position=(0.0, 0.0))],
            graphics=[
                # Crosses the refdes bbox.
                _silk_line(start=(-2.0, 0.0), end=(2.0, 0.0)),
                # Well clear of it (negative control within the same footprint).
                _silk_line(start=(-2.0, -2.0), end=(2.0, -2.0)),
            ],
        )
        pcb._footprints.append(fp)

        results = check_silk_overlap(pcb, _rules())
        assert len(results) == 1
        assert {results.violations[0].items[0], results.violations[0].items[1]} == {
            "A1 (reference)",
            "A1 (fp_line)",
        }

    def test_element_never_pairs_with_itself(self):
        """A lone silk element cannot collide with itself."""
        pcb = _empty_pcb()
        pcb._footprints.append(_make_footprint(texts=[_ref_text(position=(0.0, 0.0))]))

        assert len(check_silk_overlap(pcb, _rules())) == 0

    def test_pair_emitted_only_once(self):
        """``(A, B)`` and ``(B, A)`` collapse to a single violation."""
        pcb = _empty_pcb()
        pcb._footprints.extend(
            [
                _make_footprint(
                    reference="B1",
                    position=(10.0, 10.0),
                    texts=[_ref_text(text="XXXX", position=(0.0, 0.0))],
                ),
                _make_footprint(
                    reference="C1",
                    position=(10.0, 10.0),
                    texts=[_ref_text(text="XXXX", position=(0.0, 0.0))],
                ),
            ]
        )

        # Two *identical* geometries on different parts: still distinct
        # elements (compared by index, not geometry), so they pair -- once.
        assert len(check_silk_overlap(pcb, _rules())) == 1

    def test_front_silk_never_pairs_with_back_silk(self):
        """Coincident F.SilkS and B.SilkS elements do not collide."""
        pcb = _empty_pcb()
        pcb._footprints.extend(
            [
                _make_footprint(
                    reference="B1",
                    position=(10.0, 10.0),
                    texts=[_ref_text(text="B1", position=(0.0, 0.0), layer="F.SilkS")],
                ),
                _make_footprint(
                    reference="C1",
                    position=(10.0, 10.0),
                    texts=[_ref_text(text="C1", position=(0.0, 0.0), layer="B.SilkS")],
                ),
            ]
        )

        assert len(check_silk_overlap(pcb, _rules())) == 0

    def test_board_level_text_participates(self):
        """A board-level gr_text overlapping a footprint refdes is flagged."""
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                reference="B1",
                position=(10.0, 10.0),
                texts=[_ref_text(text="B1", position=(0.0, 0.0))],
            )
        )
        pcb._texts.append(
            GraphicText(
                text="LOGO",
                position=(10.0, 10.0),
                layer="F.SilkS",
                font_size=(1.0, 1.0),
                font_thickness=0.15,
            )
        )

        results = check_silk_overlap(pcb, _rules())
        assert len(results) == 1
        assert "LOGO" in results.violations[0].items

    def test_board_level_graphic_participates(self):
        """A board-level gr_line crossing a footprint refdes is flagged."""
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                reference="B1",
                position=(10.0, 10.0),
                texts=[_ref_text(text="B1", position=(0.0, 0.0))],
            )
        )
        pcb._graphics.append(
            BoardGraphic(
                graphic_type="line",
                layer="F.SilkS",
                stroke_width=0.3,
                start=(8.0, 10.0),
                end=(12.0, 10.0),
            )
        )

        results = check_silk_overlap(pcb, _rules())
        assert len(results) == 1
        assert "gr_line" in results.violations[0].items

    def test_crossing_min_width_strokes_flag(self):
        """Two 0.15mm strokes crossing at right angles are NOT gated out.

        Their intersection area is only 0.0225 mm^2 -- below
        ``_MIN_OVERLAP_AREA_MM2`` (0.05), which is why ``silk_overlap`` uses
        its own, smaller ``_MIN_SILK_OVERLAP_AREA_MM2`` gate.
        """
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                graphics=[
                    _silk_line(start=(-2.0, 0.0), end=(2.0, 0.0)),
                    _silk_line(start=(0.0, -2.0), end=(0.0, 2.0)),
                ],
            )
        )

        assert len(check_silk_overlap(pcb, _rules())) == 1

    def test_overlap_items_carry_distinct_uuids_for_same_label_siblings(self):
        """``items`` disambiguates same-label siblings by their own UUID.

        Two ``fp_line`` strokes on the same footprint share the generic
        ``"U1 (fp_line)"`` label; without a per-element identifier a real
        finding can't be scoped to the one offending corner/segment with a
        per-element waiver (#4987).
        """
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                reference="U1",
                graphics=[
                    _silk_line(start=(-2.0, 0.0), end=(2.0, 0.0), uuid="line-a-uuid"),
                    _silk_line(start=(0.0, -2.0), end=(0.0, 2.0), uuid="line-b-uuid"),
                ],
            )
        )

        results = check_silk_overlap(pcb, _rules())
        assert len(results) == 1
        items = results.violations[0].items
        assert {items[0], items[1]} == {
            "U1 (fp_line) {line-a-uuid}",
            "U1 (fp_line) {line-b-uuid}",
        }

    def test_overlap_items_omit_uuid_suffix_when_unset(self):
        """No trailing UUID suffix is added when the source element has none.

        Synthetic fixtures (and any pre-#4987 caller depending on the bare
        label) are unaffected by the UUID-disambiguation feature.
        """
        pcb = _empty_pcb()
        pcb._footprints.extend(
            [
                _make_footprint(
                    reference="B1",
                    position=(10.0, 10.0),
                    texts=[_ref_text(text="B1", position=(0.0, 0.0))],
                ),
                _make_footprint(
                    reference="C1",
                    position=(10.4, 10.0),
                    texts=[_ref_text(text="C1", position=(0.0, 0.0))],
                ),
            ]
        )

        results = check_silk_overlap(pcb, _rules())
        assert len(results) == 1
        assert {results.violations[0].items[0], results.violations[0].items[1]} == {
            "B1 (reference)",
            "C1 (reference)",
        }

    def test_hidden_and_empty_text_skipped(self):
        """Hidden and zero-length silk text never participate."""
        pcb = _empty_pcb()
        pcb._footprints.extend(
            [
                _make_footprint(
                    reference="B1",
                    position=(10.0, 10.0),
                    texts=[_ref_text(text="B1", position=(0.0, 0.0), hidden=True)],
                ),
                _make_footprint(
                    reference="C1",
                    position=(10.0, 10.0),
                    texts=[_ref_text(text="", position=(0.0, 0.0))],
                ),
                _make_footprint(
                    reference="D1",
                    position=(10.0, 10.0),
                    texts=[_ref_text(text="D1", position=(0.0, 0.0))],
                ),
            ]
        )

        assert len(check_silk_overlap(pcb, _rules())) == 0

    def test_board_with_no_silk_is_noop(self):
        """A board with zero silk yields zero violations, not an exception."""
        assert len(check_silk_overlap(_empty_pcb(), _rules())) == 0

    def test_reachable_through_check_all_silkscreen(self):
        """``silk_overlap`` is merged into the silkscreen check group (AC5).

        ``kct check --only silkscreen`` dispatches to
        ``DesignRuleChecker.check_silkscreen`` -> ``check_all_silkscreen``, so
        merging the producer there is what makes the rule reachable with no
        ``cli/check_cmd.py`` change.
        """
        pcb = _empty_pcb()
        pcb._footprints.extend(
            [
                _make_footprint(
                    reference="B1",
                    position=(10.0, 10.0),
                    texts=[_ref_text(text="B1", position=(0.0, 0.0))],
                ),
                _make_footprint(
                    reference="C1",
                    position=(10.4, 10.0),
                    texts=[_ref_text(text="C1", position=(0.0, 0.0))],
                ),
            ]
        )

        results = check_all_silkscreen(pcb, _rules())
        overlaps = [v for v in results.violations if v.rule_id == "silk_overlap"]
        assert len(overlaps) == 1
        assert overlaps[0].severity == "warning"


# ---------------------------------------------------------------------------
# silk_edge_clearance
# ---------------------------------------------------------------------------


def _square_outline(pcb: PCB, size: float = 20.0) -> None:
    """Add a square Edge.Cuts outline from (0,0) to (size,size)."""
    corners = [
        ((0.0, 0.0), (size, 0.0)),
        ((size, 0.0), (size, size)),
        ((size, size), (0.0, size)),
        ((0.0, size), (0.0, 0.0)),
    ]
    for start, end in corners:
        pcb._graphic_lines.append(GraphicLine(start=start, end=end, layer="Edge.Cuts", width=0.1))


class TestSilkEdgeClearance:
    def test_text_crossing_edge_flags(self):
        """Silk text straddling the board outline is flagged."""
        pcb = _empty_pcb()
        _square_outline(pcb)
        # Text centered exactly on the left edge (x=0) crosses it.
        fp = _make_footprint(
            position=(0.0, 10.0),
            texts=[_ref_text(position=(0.0, 0.0))],
        )
        pcb._footprints.append(fp)

        results = check_silk_edge_clearance(pcb, _rules())
        assert len(results) == 1
        v = results.violations[0]
        assert v.rule_id == "silk_edge_clearance"
        assert v.severity == "warning"
        assert v.items[1] == "Edge.Cuts"
        assert v.actual_value == pytest.approx(0.0, abs=1e-6)

    def test_text_within_threshold_flags(self):
        """Silk text closer than SILK_EDGE_CLEARANCE_MM to the edge is flagged."""
        pcb = _empty_pcb()
        _square_outline(pcb)
        # Place the text bbox so its left edge is ~0.1mm inboard of x=0
        # (within the 0.2mm threshold). bbox half-width = 1.0*2*0.7/2+... ~0.79.
        fp = _make_footprint(
            position=(0.85, 10.0),
            texts=[_ref_text(position=(0.0, 0.0))],
        )
        pcb._footprints.append(fp)

        results = check_silk_edge_clearance(pcb, _rules())
        assert len(results) == 1
        assert results.violations[0].actual_value < SILK_EDGE_CLEARANCE_MM

    def test_text_inboard_passes(self):
        """Silk text well inside the board edge produces no violation."""
        pcb = _empty_pcb()
        _square_outline(pcb)
        fp = _make_footprint(
            position=(10.0, 10.0),
            texts=[_ref_text(position=(0.0, 0.0))],
        )
        pcb._footprints.append(fp)

        results = check_silk_edge_clearance(pcb, _rules())
        assert len(results) == 0
        assert results.passed is True

    def test_no_outline_is_noop(self):
        """With no Edge.Cuts outline the edge check is a no-op."""
        pcb = _empty_pcb()
        fp = _make_footprint(
            position=(0.0, 0.0),
            texts=[_ref_text(position=(0.0, 0.0))],
        )
        pcb._footprints.append(fp)

        results = check_silk_edge_clearance(pcb, _rules())
        assert len(results) == 0

    def test_nonzero_board_origin_frame(self):
        """Outline and silk stay consistent under a non-zero board origin.

        ``get_board_outline_segments`` converts the outline to board-relative
        space using ``_board_origin``; footprint positions are already
        board-relative.  A text well inboard must NOT be flagged regardless of
        the origin offset (coordinate-frame regression).
        """
        pcb = _empty_pcb()
        # Outline stored in sheet-absolute space, offset by the board origin.
        ox, oy = 100.0, 50.0
        size = 20.0
        corners = [
            ((ox, oy), (ox + size, oy)),
            ((ox + size, oy), (ox + size, oy + size)),
            ((ox + size, oy + size), (ox, oy + size)),
            ((ox, oy + size), (ox, oy)),
        ]
        for start, end in corners:
            pcb._graphic_lines.append(
                GraphicLine(start=start, end=end, layer="Edge.Cuts", width=0.1)
            )
        pcb._board_origin = (ox, oy)

        # Footprint at board-relative center -> well inboard.
        fp = _make_footprint(
            position=(10.0, 10.0),
            texts=[_ref_text(position=(0.0, 0.0))],
        )
        pcb._footprints.append(fp)

        results = check_silk_edge_clearance(pcb, _rules())
        assert len(results) == 0

        # Now move the text to the board-relative left edge -> flagged.
        fp.texts[0].position = (0.0, 0.0)
        fp.position = (0.0, 10.0)
        results = check_silk_edge_clearance(pcb, _rules())
        assert len(results) == 1

    def test_board_level_text_near_edge(self):
        """Board-level gr_text near the edge is flagged (no fp transform)."""
        pcb = _empty_pcb()
        _square_outline(pcb)
        pcb._texts.append(
            GraphicText(
                text="LOGO",
                position=(0.0, 10.0),
                layer="F.SilkS",
                font_size=(1.0, 1.0),
                font_thickness=0.15,
            )
        )

        results = check_silk_edge_clearance(pcb, _rules())
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Severity / real-board regression
# ---------------------------------------------------------------------------


class TestSilkSeverity:
    def test_all_violations_are_warnings(self):
        """Every emitted silk violation is warning severity (non-blocking)."""
        pcb = _empty_pcb()
        _square_outline(pcb)
        fp = _make_footprint(
            position=(0.0, 10.0),
            pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
            texts=[_ref_text(position=(0.0, 0.0))],
        )
        pcb._footprints.append(fp)

        over = check_silk_over_copper(pcb, _rules())
        edge = check_silk_edge_clearance(pcb, _rules())
        for v in (*over.violations, *edge.violations):
            assert v.severity == "warning"
        assert over.violations or edge.violations  # at least one fired


# Historical defect witnesses are frozen under regression-fixture/.
# Manufacturing output must be allowed to become clean without erasing detector coverage.
_BOARD_ROOT = "boards"


@pytest.mark.parametrize(
    "rel_path, rule_id",
    [
        # Issue #3939 moved board 01's connector refdes off pad-1 copper, so
        # it no longer yields silk_over_copper (it now lives in the clean-board
        # list below). Board 05 remains the silk_edge_clearance fixture. The
        # silk_over_copper detector itself is exercised by the synthetic unit
        # tests above (see the ``silk_over_copper`` section).
        (
            "05-bldc-motor-controller/regression-fixture/bldc_controller_routed.kicad_pcb",
            "silk_edge_clearance",
        ),
    ],
)
def test_real_board_regression(rel_path, rule_id):
    """board-05 yields silk_edge_clearance."""
    import os

    path = os.path.join(_BOARD_ROOT, rel_path)
    if not os.path.exists(path):
        pytest.skip(f"board fixture not present: {path}")

    pcb = PCB.load(path)
    rules = _rules()
    over = check_silk_over_copper(pcb, rules)
    edge = check_silk_edge_clearance(pcb, rules)
    by_rule = {
        "silk_over_copper": over.violations,
        "silk_edge_clearance": edge.violations,
    }
    assert len(by_rule[rule_id]) >= 1
    for v in (*over.violations, *edge.violations):
        assert v.severity == "warning"


# --- Cross-gate parity with ``kicad-cli pcb drc`` (AC2/AC3 of #4612) --------
#
# These pair sets were captured from
#   kicad-cli pcb drc --refill-zones --severity-all --format json
# run on COPIES of the committed boards (KiCad 10.0.5, 2026-08-04), reducing
# each violation's two items to (silk owner refdes, "<footprint>:<pad>").
# Before #4612 kct emitted 2/6/4/3 against kicad-cli's 4/12/7/5.
_KICAD_CLI_SILK_OVER_COPPER_PAIRS: dict[str, set[tuple[str, str]]] = {
    "03-usb-joystick/regression-fixture/usb_joystick_routed.kicad_pcb": {
        ("C11", "C10:1"),
        ("C11", "C10:2"),
        ("R11", "R10:1"),
        ("R11", "R10:2"),
    },
    "05-bldc-motor-controller/regression-fixture/bldc_controller_routed.kicad_pcb": {
        ("C7", "C4:2"),
        ("C8", "C5:1"),
        ("Q1", "R20:1"),
        ("Q1", "R20:2"),
        ("Q3", "R21:1"),
        ("Q3", "R21:2"),
        ("Q5", "R22:1"),
        ("Q5", "R22:2"),
        ("U10", "U10:27"),
        ("U10", "U10:28"),
        ("U10", "U10:29"),
        ("U10", "U10:30"),
    },
    "06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb": {
        ("U1", "U1:12"),
        ("U1", "U1:13"),
        ("U2", "U2:A4"),
        ("U3", "U3:18"),
        ("U3", "U3:19"),
        ("U4", "U4:9"),
        ("U4", "U4:10"),
    },
    "07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb": {
        ("U3", "U3:9"),
        ("U3", "U3:10"),
        ("U4", "U4:A4"),
        ("U5", "U5:18"),
        ("U5", "U5:19"),
    },
}


def _kct_pair_set(pcb: PCB) -> set[tuple[str, str]]:
    """Reduce kct ``silk_over_copper`` violations to comparable pair keys."""
    import re

    pairs = set()
    for v in check_silk_over_copper(pcb, _rules()).violations:
        silk = re.sub(r" \(\w+\)$", "", v.items[0])
        pad = re.sub(r" pad ", ":", v.items[1])
        pairs.add((silk, pad))
    return pairs


@pytest.mark.parametrize("rel_path", sorted(_KICAD_CLI_SILK_OVER_COPPER_PAIRS))
def test_silk_over_copper_pair_parity_with_kicad_cli(rel_path):
    """kct's (silk, pad) pair SET equals kicad-cli's, not just the count.

    AC2 (exact count parity: 4 / 12 / 7 / 5) and AC3 (pair identity, so a
    multi-pad straddle names every pad rather than an arbitrary representative)
    of #4612.
    """
    import os

    path = os.path.join(_BOARD_ROOT, rel_path)
    if not os.path.exists(path):
        pytest.skip(f"board fixture not present: {path}")

    expected = _KICAD_CLI_SILK_OVER_COPPER_PAIRS[rel_path]
    actual = _kct_pair_set(PCB.load(path))
    assert actual == expected
    assert len(actual) == len(expected)


def test_board_05_u10_names_all_four_straddled_pads():
    """The board-05 ``U10`` refdes names pads 27, 28, 29 AND 30.

    The concrete AC3 case: before #4612 kct reported only ``U10 pad 28``, an
    arbitrary R-tree-ordered member of the real collision set.
    """
    import os

    path = os.path.join(
        _BOARD_ROOT, "05-bldc-motor-controller/regression-fixture/bldc_controller_routed.kicad_pcb"
    )
    if not os.path.exists(path):
        pytest.skip(f"board fixture not present: {path}")

    pairs = _kct_pair_set(PCB.load(path))
    u10_pads = sorted(pad.split(":")[1] for silk, pad in pairs if silk == "U10")
    assert u10_pads == ["27", "28", "29", "30"]


@pytest.mark.parametrize(
    "rel_path",
    sorted(_KICAD_CLI_SILK_OVER_COPPER_PAIRS)
    + [
        "01-voltage-divider/output/voltage_divider_routed.kicad_pcb",
        "02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb",
        "04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb",
    ],
)
def test_silk_overlap_zero_on_all_committed_boards(rel_path):
    """kicad-cli reports zero ``silk_overlap`` on every committed board.

    ``silk_overlap`` is NOT in the DRC report's ``ignored_checks``, so the test
    genuinely ran and genuinely found nothing (the fleet's silkscreen is refdes
    text only).  kct must agree -- this is the no-false-positive guard for the
    new rule, the analogue of ``test_clean_boards_no_false_positives``.
    """
    import os

    path = os.path.join(_BOARD_ROOT, rel_path)
    if not os.path.exists(path):
        pytest.skip(f"board fixture not present: {path}")

    results = check_silk_overlap(PCB.load(path), _rules())
    assert len(results) == 0, [tuple(v.items) for v in results.violations]


@pytest.mark.parametrize(
    "rel_path",
    [
        # Issue #3939: board 01's connector refdes now clears pad-1 copper.
        "01-voltage-divider/output/voltage_divider_routed.kicad_pcb",
        "02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb",
        "04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb",
    ],
)
def test_clean_boards_no_false_positives(rel_path):
    """Boards kicad-cli considers silk-clean produce zero silk violations."""
    import os

    path = os.path.join(_BOARD_ROOT, rel_path)
    if not os.path.exists(path):
        pytest.skip(f"board fixture not present: {path}")

    pcb = PCB.load(path)
    rules = _rules()
    over = check_silk_over_copper(pcb, rules)
    edge = check_silk_edge_clearance(pcb, rules)
    assert len(over) == 0
    assert len(edge) == 0


# ---------------------------------------------------------------------------
# Polygon silk geometry (#5811)
# ---------------------------------------------------------------------------
#
# Before #5811 ``_stroke_geometry`` returned ``None`` for every graphic type
# other than line/rect, so ``fp_poly`` / ``gr_poly`` silk -- the shape KiCad
# footprint libraries use for pin-1 and polarity markers -- contributed NO
# geometry to any of the four silk clearance checks.  A filled pin-1 triangle
# 0.1052 mm from a pad on a real board was therefore reported clean by kct
# while native KiCad DRC reported a ``silk_over_copper`` error.
#
# Both fill states are modeled because they print different shapes, and the
# difference is measurable with native KiCad DRC (kicad-cli 10.0.6, JLCPCB
# ``Silk to Pad`` rule at the 0.15 mm floor; see ``_POLY_FLOOR_CASES`` and
# ``test_native_poly_silk_floor_parity``):
#
#   polygon ring drawn AROUND a pad, (fill no), stroke 0.15 -> 0 findings
#   the same ring with (fill yes)                           -> 1, actual 0.0000 mm
#
# i.e. an unfilled outline leaves its interior blank (a pad inside it is not
# covered), while a filled one prints the whole interior.


def _silk_poly(
    *,
    points: list[tuple[float, float]],
    layer: str = "F.SilkS",
    stroke_width: float = 0.15,
    fill: str = "yes",
    uuid: str = "",
) -> FootprintGraphic:
    return FootprintGraphic(
        graphic_type="poly",
        layer=layer,
        stroke_width=stroke_width,
        points=list(points),
        fill=fill,
        uuid=uuid,
    )


#: A square ring of vertices centred on the footprint origin, comfortably
#: outside a 2x2 mm pad placed there: the ring's own outline never touches the
#: pad, so only the *filled* interpretation covers it.
_RING_AROUND_ORIGIN = [(-3.0, -3.0), (3.0, -3.0), (3.0, 3.0), (-3.0, 3.0)]

#: A triangle that straddles the footprint origin -- overlaps a pad there
#: under either fill state.
_TRIANGLE_OVER_ORIGIN = [(-1.5, -1.5), (1.5, 0.0), (-1.5, 1.5)]


class TestSilkPolygonGeometry:
    def test_filled_zero_stroke_poly_over_pad_flags(self):
        """A filled polygon with ``(stroke (width 0))`` is real ink.

        The exact regression: the pre-#5811 zero-width early return dropped
        this shape even before the graphic-type gate would have, and a filled
        marker with no outline stroke is the most common pin-1 triangle in
        KiCad's libraries.
        """
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
                graphics=[_silk_poly(points=_TRIANGLE_OVER_ORIGIN, stroke_width=0.0)],
            )
        )

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 1
        assert "fp_poly" in results.violations[0].items[0]

    def test_unfilled_stroked_poly_outline_over_pad_flags(self):
        """An unfilled polygon whose outline crosses a pad is flagged."""
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
                graphics=[
                    _silk_poly(points=_TRIANGLE_OVER_ORIGIN, stroke_width=0.3, fill="no"),
                ],
            )
        )

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 1
        assert "fp_poly" in results.violations[0].items[0]

    def test_unfilled_poly_interior_does_not_cover_pad(self):
        """A pad inside an UNFILLED ring is not covered -- native agrees.

        Measured: kicad-cli 10.0.6 reports zero ``Silk to Pad`` findings for
        this shape (``ring_unfilled``).  Modeling an unfilled polygon as a
        solid area would turn every courtyard-style polygon outline into a
        false positive over the part's own pads.
        """
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
                graphics=[_silk_poly(points=_RING_AROUND_ORIGIN, stroke_width=0.15, fill="no")],
            )
        )

        assert len(check_silk_over_copper(pcb, _rules())) == 0

    def test_filled_poly_interior_does_cover_pad(self):
        """The same ring, FILLED, covers the pad -- native agrees (actual 0.0mm)."""
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
                graphics=[_silk_poly(points=_RING_AROUND_ORIGIN, stroke_width=0.15, fill="yes")],
            )
        )

        assert len(check_silk_over_copper(pcb, _rules())) == 1

    @pytest.mark.parametrize("fill", ["yes", "solid", "true"])
    def test_filled_fill_tokens(self, fill):
        """KiCad 7+ ``yes`` and the legacy ``solid`` spelling both mean filled.

        Both appear in this repository's committed boards (``(fill yes)`` on
        board 04, ``(fill solid)`` in ``tests/fixtures/pin1_marker.kicad_pcb``).
        """
        graphic = _silk_poly(points=_RING_AROUND_ORIGIN, stroke_width=0.0, fill=fill)
        assert graphic.is_filled is True
        assert _stroke_geometry(graphic, None) is not None

    @pytest.mark.parametrize("fill", ["no", "none", ""])
    def test_unfilled_fill_tokens_with_zero_stroke_print_nothing(self, fill):
        """Unfilled AND unstroked prints no ink at all, so there is no geometry."""
        graphic = _silk_poly(points=_RING_AROUND_ORIGIN, stroke_width=0.0, fill=fill)
        assert graphic.is_filled is False
        assert _stroke_geometry(graphic, None) is None

    @pytest.mark.parametrize("points", [[], [(0.0, 0.0)], [(0.0, 0.0), (1.0, 0.0)]])
    def test_degenerate_poly_is_skipped(self, points):
        """Fewer than three vertices cannot bound an area; skip, do not raise."""
        graphic = _silk_poly(points=points, stroke_width=0.15, fill="yes")
        assert _stroke_geometry(graphic, None) is None

    def test_self_intersecting_poly_is_repaired_not_raised(self):
        """A bow-tie polygon yields usable geometry instead of an exception."""
        bowtie = [(-1.0, -1.0), (1.0, 1.0), (1.0, -1.0), (-1.0, 1.0)]
        geom = _stroke_geometry(_silk_poly(points=bowtie, stroke_width=0.0), None)
        assert geom is not None
        assert geom.area > 0.0

    @pytest.mark.parametrize("rotation", [0.0, 90.0, 180.0, 270.0, 37.0])
    def test_footprint_rotation_transforms_poly_vertices(self, rotation):
        """A marker over a pad stays over it at every footprint rotation.

        Pad and polygon share the same footprint-local frame, so the rotation
        maps both the same way -- the assertion fails if the poly branch skips
        the ``xf`` transform (a plausible copy/paste slip, since the vertices
        are the only coordinates in this branch).
        """
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                rotation=rotation,
                pads=[_smd_pad(position=(2.0, 1.0), size=(2.0, 2.0))],
                graphics=[
                    _silk_poly(
                        points=[(1.0, 0.0), (3.0, 0.0), (3.0, 2.0), (1.0, 2.0)],
                        stroke_width=0.0,
                    )
                ],
            )
        )

        assert len(check_silk_over_copper(pcb, _rules())) == 1

    def test_rotated_poly_geometry_matches_manual_rotation(self):
        """The transformed polygon's vertices land where the fp transform says.

        A count assertion alone cannot distinguish "rotation applied" from
        "rotation applied with the wrong sign" for a symmetric shape, so this
        pins the actual coordinates: under KiCad's negated-angle convention
        (the one ``_fp_transform`` implements) a 90-degree footprint at
        ``(10, 10)`` maps local ``(1, 0)`` to board ``(10, 9)`` -- the
        opposite y direction from a naive positive-angle rotation.
        """
        footprint = _make_footprint(
            position=(10.0, 10.0),
            rotation=90.0,
            graphics=[_silk_poly(points=[(1.0, 0.0), (2.0, 0.0), (2.0, 1.0)], stroke_width=0.0)],
        )
        from kicad_tools.validate.rules.silkscreen import _fp_transform

        geom = _stroke_geometry(footprint.graphics[0], _fp_transform(footprint))
        assert geom is not None
        corners = {(round(x, 6), round(y, 6)) for x, y in geom.exterior.coords}
        assert corners == {(10.0, 9.0), (10.0, 8.0), (11.0, 8.0)}

    def test_back_side_poly_not_checked_against_front_copper(self):
        """A B.SilkS polygon does not collide with F.Cu-only SMD copper."""
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                layer="F.Cu",
                pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
                graphics=[
                    _silk_poly(points=_TRIANGLE_OVER_ORIGIN, stroke_width=0.0, layer="B.SilkS")
                ],
            )
        )

        assert len(check_silk_over_copper(pcb, _rules())) == 0

    def test_back_side_poly_is_checked_against_thru_hole_copper(self):
        """...but a thru-hole aperture is exposed on both sides, so it fires."""
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                layer="F.Cu",
                pads=[_thru_hole_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
                graphics=[
                    _silk_poly(points=_TRIANGLE_OVER_ORIGIN, stroke_width=0.0, layer="B.SilkS")
                ],
            )
        )

        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 1
        assert results.violations[0].layer == "B.SilkS"

    def test_poly_participates_in_silk_overlap_with_uuid_attribution(self):
        """A filled marker over a refdes is a ``silk_overlap`` pair, UUID-tagged."""
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                reference="U1",
                texts=[_ref_text(text="U1", position=(0.0, 0.0))],
                graphics=[
                    _silk_poly(points=_TRIANGLE_OVER_ORIGIN, stroke_width=0.0, uuid="poly-uuid")
                ],
            )
        )

        results = check_silk_overlap(pcb, _rules())
        assert len(results) == 1
        assert {results.violations[0].items[0], results.violations[0].items[1]} == {
            "U1 (reference)",
            "U1 (fp_poly) {poly-uuid}",
        }

    def test_poly_participates_in_silk_edge_clearance(self):
        """A filled marker straddling Edge.Cuts is flagged; moved inboard it is not."""
        pcb = _empty_pcb()
        _square_outline(pcb)
        footprint = _make_footprint(
            position=(0.0, 10.0),
            graphics=[_silk_poly(points=_TRIANGLE_OVER_ORIGIN, stroke_width=0.0)],
        )
        pcb._footprints.append(footprint)

        assert len(check_silk_edge_clearance(pcb, _rules())) == 1

        footprint.position = (10.0, 10.0)
        assert len(check_silk_edge_clearance(pcb, _rules())) == 0

    def test_board_level_gr_poly_is_parsed_and_checked(self):
        """``gr_poly`` silk is parsed into ``PCB.graphics`` and checked (#5811).

        Board-level polygons were previously not parsed *at all*, so the
        ``_stroke_geometry(board_graphic, None)`` call site could never see
        one no matter what the geometry helper did.  Built from real
        S-expression text so the schema path is what is under test.
        """
        pcb = PCB(
            parse_string(
                """(kicad_pcb
    (version 20240108)
    (generator "pcbnew")
    (setup (pad_to_mask_clearance 0))
    (net 0 "")
    (gr_poly
        (pts (xy 9 9) (xy 11 9) (xy 11 11) (xy 9 11))
        (stroke (width 0) (type solid))
        (fill yes)
        (layer "F.SilkS")
        (uuid "00000000-0000-0000-0000-0000000000aa"))
)"""
            )
        )

        polys = [g for g in pcb.graphics if g.graphic_type == "poly"]
        assert len(polys) == 1
        assert polys[0].points == [(9.0, 9.0), (11.0, 9.0), (11.0, 11.0), (9.0, 11.0)]
        assert polys[0].fill == "yes"
        assert polys[0].is_filled is True
        assert polys[0].uuid == "00000000-0000-0000-0000-0000000000aa"
        # A gr_poly has no (start ...) node; consumers that report
        # ``graphic.start`` as the element's location get its first vertex
        # rather than the board origin.
        assert polys[0].start == (9.0, 9.0)

        pcb._footprints.append(
            _make_footprint(position=(10.0, 10.0), pads=[_smd_pad(position=(0.0, 0.0))])
        )
        results = check_silk_over_copper(pcb, _rules())
        assert len(results) == 1
        assert "gr_poly" in results.violations[0].items[0]

    def test_unfilled_gr_poly_parses_fill_token(self):
        """``(fill no)`` on a board polygon resolves to not-filled."""
        pcb = PCB(
            parse_string(
                """(kicad_pcb
    (version 20240108)
    (generator "pcbnew")
    (net 0 "")
    (gr_poly
        (pts (xy 0 0) (xy 2 0) (xy 2 2))
        (stroke (width 0.15) (type solid))
        (fill no)
        (layer "F.SilkS"))
)"""
            )
        )
        poly = next(g for g in pcb.graphics if g.graphic_type == "poly")
        assert poly.fill == "no"
        assert poly.is_filled is False


# ---------------------------------------------------------------------------
# Silk geometry coverage: unmodeled primitives stay visible (#5811)
# ---------------------------------------------------------------------------


def _silk_circle(*, uuid: str = "", layer: str = "F.SilkS") -> FootprintGraphic:
    return FootprintGraphic(
        graphic_type="circle",
        layer=layer,
        stroke_width=0.12,
        center=(0.0, 0.0),
        end=(0.5, 0.0),
        fill="yes",
        uuid=uuid,
    )


def _silk_arc(*, uuid: str = "", layer: str = "F.SilkS") -> FootprintGraphic:
    graphic = FootprintGraphic(
        graphic_type="arc",
        layer=layer,
        stroke_width=0.12,
        start=(-1.0, 0.0),
        end=(1.0, 0.0),
        uuid=uuid,
    )
    graphic.mid = (0.0, 1.0)
    return graphic


class TestSilkGeometryCoverage:
    def test_circle_is_reported_as_unmodeled_not_clean(self):
        """A silk circle over a pad: zero clearance findings, one coverage info.

        This is the whole point of the rule.  ``check_silk_over_copper`` cannot
        see the circle (the geometry model has no circle branch), and the
        pre-#5811 behaviour was to say nothing at all -- indistinguishable
        from "checked and clean".  The ``info`` finding makes the gap a
        reported fact.
        """
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                pads=[_smd_pad(position=(0.0, 0.0), size=(2.0, 2.0))],
                graphics=[_silk_circle(uuid="circle-uuid")],
            )
        )

        assert len(check_silk_over_copper(pcb, _rules())) == 0

        coverage = check_silk_coverage(pcb, _rules())
        assert len(coverage) == 1
        violation = coverage.violations[0]
        assert violation.rule_id == SILK_GEOMETRY_UNMODELED_RULE_ID
        assert violation.severity == "info"
        assert violation.items == ("U1 (fp_circle) {circle-uuid}",)
        assert violation.layer == "F.SilkS"
        assert "circle" in violation.message

    def test_arc_is_reported_as_unmodeled(self):
        pcb = _empty_pcb()
        pcb._footprints.append(_make_footprint(graphics=[_silk_arc(uuid="arc-uuid")]))

        coverage = check_silk_coverage(pcb, _rules())
        assert len(coverage) == 1
        assert coverage.violations[0].items == ("U1 (fp_arc) {arc-uuid}",)

    def test_coverage_item_omits_uuid_suffix_when_unset(self):
        pcb = _empty_pcb()
        pcb._footprints.append(_make_footprint(graphics=[_silk_circle()]))

        assert check_silk_coverage(pcb, _rules()).violations[0].items == ("U1 (fp_circle)",)

    @pytest.mark.parametrize("layer", ["F.Fab", "F.CrtYd", "F.Cu"])
    def test_non_silk_layer_shapes_are_not_reported(self, layer):
        """Coverage is a statement about SILK geometry only."""
        pcb = _empty_pcb()
        pcb._footprints.append(_make_footprint(graphics=[_silk_circle(layer=layer)]))

        assert len(check_silk_coverage(pcb, _rules())) == 0

    def test_modeled_primitives_produce_no_coverage_finding(self):
        """line / rect / poly are modeled, so they are not reported as gaps."""
        pcb = _empty_pcb()
        pcb._footprints.append(
            _make_footprint(
                graphics=[
                    _silk_line(start=(0.0, 0.0), end=(1.0, 0.0)),
                    FootprintGraphic(
                        graphic_type="rect",
                        layer="F.SilkS",
                        stroke_width=0.15,
                        start=(0.0, 0.0),
                        end=(1.0, 1.0),
                    ),
                    _silk_poly(points=_TRIANGLE_OVER_ORIGIN, stroke_width=0.0),
                ]
            )
        )

        assert len(check_silk_coverage(pcb, _rules())) == 0

    def test_board_level_unmodeled_graphic_is_reported(self):
        pcb = _empty_pcb()
        pcb._graphics.append(
            BoardGraphic(
                graphic_type="circle",
                layer="F.SilkS",
                stroke_width=0.12,
                start=(5.0, 5.0),
                center=(5.0, 5.0),
                end=(5.5, 5.0),
                uuid="gr-circle-uuid",
            )
        )

        coverage = check_silk_coverage(pcb, _rules())
        assert len(coverage) == 1
        assert coverage.violations[0].items == ("gr_circle {gr-circle-uuid}",)

    def test_coverage_reachable_through_check_all_silkscreen(self):
        """``kct check --only silkscreen`` surfaces the coverage advisory."""
        pcb = _empty_pcb()
        pcb._footprints.append(_make_footprint(graphics=[_silk_circle(uuid="circle-uuid")]))

        results = check_all_silkscreen(pcb, _rules())
        unmodeled = [v for v in results.violations if v.rule_id == SILK_GEOMETRY_UNMODELED_RULE_ID]
        assert len(unmodeled) == 1
        assert unmodeled[0].severity == "info"
        # An advisory about kct's own model must never fail the gate.
        assert results.error_count == 0

    def test_coverage_advisory_is_categorized_as_advisory(self):
        """The new rule id must not default into the fab-blocking bucket.

        ``category_for_rule`` files unknown ids under Manufacturing, which
        would present a statement about kct's own coverage as a fab defect.
        """
        from kicad_tools.validate.checker import DRCChecker

        assert (
            DRCChecker.category_for_rule(SILK_GEOMETRY_UNMODELED_RULE_ID)
            == DRCChecker.CATEGORY_ADVISORY
        )

    def test_committed_board_02_arc_is_reported(self):
        """Board 02's one F.SilkS ``fp_arc`` is the real-fleet instance."""
        import os

        path = os.path.join(
            _BOARD_ROOT, "02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb"
        )
        if not os.path.exists(path):
            pytest.skip(f"board fixture not present: {path}")

        coverage = check_silk_coverage(PCB.load(path), _rules())
        assert len(coverage) == 1
        assert "fp_arc" in coverage.violations[0].items[0]


# ---------------------------------------------------------------------------
# Native parity: a polygon marker straddling the 0.15 mm silk-to-pad floor
# ---------------------------------------------------------------------------
#
# The reduced version of the Chorus U10 witness in #5811: one masked 1x1 mm SMD
# pad and one polygon marker whose printed ink sits a known distance from that
# pad's copper.  ``kicad-cli pcb drc`` is the referee via the JLCPCB
# ``Silk to Pad`` rule emitted by ``write_drc_constraints`` (the #5059
# measurement established that the project-level ``min_silk_clearance`` key
# does NOT gate a sub-floor gap -- the explicit .kicad_dru rule does).
#
# Measured on kicad-cli 10.0.6 (2026-09-30), and asserted in both directions so
# the threshold itself is pinned rather than merely proving the rule is noisy:
#
#   filled,   stroke 0.00, gap 0.085 -> 1 finding, "actual 0.0850 mm"
#   filled,   stroke 0.00, gap 0.160 -> 0 findings
#   unfilled, stroke 0.15, gap 0.085 -> 1 finding, "actual 0.0850 mm"
#   unfilled, stroke 0.15, gap 0.160 -> 0 findings
#   filled,   stroke 0.00, gap 0.085, footprint rotated -90 -> 1, "0.0850 mm"
#
# Native echoes the fixture's own ``(uuid ...)`` for the offending polygon, so
# the UUID attribution is compared item-for-item rather than assumed.

#: JLCPCB's published silkscreen-to-pad floor (mm).
_SILK_PAD_FLOOR_MM = 0.15

#: The polygon's UUID in the probe fixture.  Must be UUID-shaped: kicad-cli
#: replaces a non-conforming id with a generated one, which would make the
#: attribution comparison below vacuous.
_PROBE_POLY_UUID = "00000000-0000-0000-0000-0000000000aa"

# (id, gap_mm, filled, stroke_width, footprint_rotation, expected_finding)
_POLY_FLOOR_CASES = [
    ("filled-zero-stroke-below-floor", 0.085, True, 0.0, 0.0, True),
    ("filled-zero-stroke-above-floor", 0.16, True, 0.0, 0.0, False),
    ("unfilled-stroked-below-floor", 0.085, False, 0.15, 0.0, True),
    ("unfilled-stroked-above-floor", 0.16, False, 0.15, 0.0, False),
    ("filled-rotated-below-floor", 0.085, True, 0.0, -90.0, True),
]


def _write_poly_floor_probe(
    path: Path,
    *,
    gap_mm: float,
    filled: bool,
    stroke_width: float,
    rotation: float,
) -> Path:
    """One masked SMD pad and one triangular silk polygon ``gap_mm`` from it.

    The pad is 1x1 mm at the footprint origin with ``pad_to_mask_clearance 0``,
    so its copper (and mask aperture) edge is at local ``x = 0.5``.  The
    polygon's near edge is the segment at local ``x``; whichever fill state is
    in force, the printed ink starts ``stroke_width / 2`` inboard of it (a
    filled polygon is dilated by the stroke, an unfilled one is the buffered
    ring), so placing the vertices at ``0.5 + gap + stroke_width / 2`` puts the
    ink exactly ``gap_mm`` from the copper in both cases.

    The reference designator lives on F.Fab deliberately: a silk refdes would
    add findings of its own and blur the single-pair assertion.
    """
    near = 0.5 + gap_mm + stroke_width / 2.0
    pts = " ".join(f"(xy {x} {y})" for x, y in [(near, -0.3), (near + 0.6, 0.0), (near, 0.3)])
    path.write_text(
        f"""(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
   (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user) (49 "F.Fab" user))
  (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "A")
  (gr_rect (start 0 0) (end 20 20) (stroke (width .1) (type default))
   (fill none) (layer "Edge.Cuts"))
  (footprint "Probe:Probe" (layer "F.Cu") (at 10 10 {rotation})
    (uuid "00000000-0000-0000-0000-00000000000f")
    (property "Reference" "U1" (at 0 -3 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-0000000000ef")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_poly (pts {pts})
      (stroke (width {stroke_width}) (type solid))
      (fill {"yes" if filled else "no"})
      (layer "F.SilkS")
      (uuid "{_PROBE_POLY_UUID}"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "A"))))
"""
    )
    return path


def _factory_rules():
    return get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)


@pytest.mark.parametrize(
    "gap_mm,filled,stroke_width,rotation,expected",
    [case[1:] for case in _POLY_FLOOR_CASES],
    ids=[case[0] for case in _POLY_FLOOR_CASES],
)
def test_kct_poly_silk_floor_matches_measured_contract(
    tmp_path, gap_mm, filled, stroke_width, rotation, expected
):
    """kct reports the polygon marker exactly where native DRC does.

    Runs with no native CLI required, so the measured contract is guarded on
    every machine; ``test_native_poly_silk_floor_parity`` re-derives the same
    table from kicad-cli when it is installed.
    """
    rules = _factory_rules()
    assert rules.min_silk_to_pad_clearance_mm == _SILK_PAD_FLOOR_MM

    pcb = PCB.load(
        _write_poly_floor_probe(
            tmp_path / "probe.kicad_pcb",
            gap_mm=gap_mm,
            filled=filled,
            stroke_width=stroke_width,
            rotation=rotation,
        )
    )
    results = check_silk_pad_clearance(pcb, rules)

    assert bool(results) == expected, [tuple(v.items) for v in results.violations]
    if expected:
        violation = results.violations[0]
        assert violation.rule_id == "silk_pad_clearance"
        assert violation.severity == "error"
        assert violation.actual_value == pytest.approx(gap_mm, abs=1e-4)
        assert violation.required_value == _SILK_PAD_FLOOR_MM
        # UUID attribution: the finding names this polygon, not just "U1 poly".
        assert violation.items[0] == f"U1 poly {{{_PROBE_POLY_UUID}}}"
        assert violation.items[1] == "U1-1"


@pytest.mark.parametrize(
    "gap_mm,filled,stroke_width,rotation,expected",
    [case[1:] for case in _POLY_FLOOR_CASES],
    ids=[case[0] for case in _POLY_FLOOR_CASES],
)
def test_native_poly_silk_floor_parity(tmp_path, gap_mm, filled, stroke_width, rotation, expected):
    """``kicad-cli pcb drc`` is the referee for the same five probe boards.

    Asserts the *native* verdict, its reported ``actual`` distance and the
    UUID it attributes the finding to -- which is what makes the sibling
    kct-only test above a parity test rather than a self-consistent fiction.
    """
    if find_kicad_cli() is None:
        pytest.skip("Native KiCad CLI is not installed")

    board = _write_poly_floor_probe(
        tmp_path / "probe.kicad_pcb",
        gap_mm=gap_mm,
        filled=filled,
        stroke_width=stroke_width,
        rotation=rotation,
    )
    rules = _factory_rules()
    write_drc_constraints(board, rules, manufacturer_id="jlcpcb", layers=4)

    report = tmp_path / "native.json"
    proc = subprocess.run(
        [
            str(find_kicad_cli()),
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
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    violations = json.loads(report.read_text())["violations"]
    # A malformed custom rule makes KiCad discard the whole .kicad_dru (#4999),
    # which would turn every "no finding" expectation into a false pass.
    assert not [v for v in violations if v["type"] == "drc_rule_error"], violations

    findings = [v for v in violations if "Silk to Pad" in v["description"]]
    assert bool(findings) == expected, violations
    if expected:
        assert len(findings) == 1, findings
        assert findings[0]["type"] == "silk_over_copper"
        assert f"{gap_mm:.4f} mm" in findings[0]["description"], findings[0]
        silk_items = [
            item for item in findings[0]["items"] if "F.Silkscreen" in item["description"]
        ]
        assert len(silk_items) == 1, findings[0]
        assert "Polygon" in silk_items[0]["description"]
        assert silk_items[0]["uuid"] == _PROBE_POLY_UUID


def test_native_and_kct_agree_on_unfilled_polygon_interior(tmp_path):
    """The fill-state discriminator, measured rather than assumed.

    A polygon ring drawn AROUND a pad is reported by neither engine when it is
    unfilled (its interior is blank) and by both when it is filled.  Without
    this pair a model that treats every polygon as a solid area would pass the
    floor tests above while flagging every polygon outline over its own part's
    pads.
    """
    rules = _factory_rules()
    cli = find_kicad_cli()

    def probe(fill: str) -> Path:
        path = tmp_path / f"ring_{fill}.kicad_pcb"
        path.write_text(
            f"""(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
   (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user) (49 "F.Fab" user))
  (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "A")
  (gr_rect (start 0 0) (end 20 20) (stroke (width .1) (type default))
   (fill none) (layer "Edge.Cuts"))
  (footprint "Probe:Probe" (layer "F.Cu") (at 10 10)
    (uuid "00000000-0000-0000-0000-00000000000f")
    (property "Reference" "U1" (at 0 -3 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-0000000000ef")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_poly (pts (xy -2 -2) (xy 2 -2) (xy 2 2) (xy -2 2))
      (stroke (width 0.15) (type solid)) (fill {fill}) (layer "F.SilkS")
      (uuid "{_PROBE_POLY_UUID}"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "A"))))
"""
        )
        return path

    def native_findings(path: Path) -> list[dict]:
        report = path.with_suffix(".json")
        proc = subprocess.run(
            [
                str(cli),
                "pcb",
                "drc",
                "--severity-all",
                "--format",
                "json",
                "-o",
                str(report),
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        violations = json.loads(report.read_text())["violations"]
        assert not [v for v in violations if v["type"] == "drc_rule_error"], violations
        return [v for v in violations if "Silk to Pad" in v["description"]]

    unfilled = probe("no")
    filled = probe("yes")

    assert len(check_silk_pad_clearance(PCB.load(unfilled), rules)) == 0
    filled_results = check_silk_pad_clearance(PCB.load(filled), rules)
    assert len(filled_results) == 1
    assert filled_results.violations[0].actual_value == pytest.approx(0.0, abs=1e-6)

    if cli is None:
        pytest.skip("Native KiCad CLI is not installed (kct-side assertions above still ran)")

    write_drc_constraints(unfilled, rules, manufacturer_id="jlcpcb", layers=4)
    write_drc_constraints(filled, rules, manufacturer_id="jlcpcb", layers=4)
    assert native_findings(unfilled) == []
    native_filled = native_findings(filled)
    assert len(native_filled) == 1, native_filled
    assert "0.0000 mm" in native_filled[0]["description"], native_filled[0]
