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
"""

from __future__ import annotations

import pytest

from kicad_tools.manufacturers import get_profile
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
from kicad_tools.sexp import SExp
from kicad_tools.validate.rules.silkscreen import (
    SILK_EDGE_CLEARANCE_MM,
    check_all_silkscreen,
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
) -> FootprintGraphic:
    return FootprintGraphic(
        graphic_type="line",
        layer=layer,
        stroke_width=stroke_width,
        start=start,
        end=end,
    )


class TestSilkOverlap:
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


# Real-board regression. These boards live under boards/*/output and are
# checked into the repo, so no KiCad install is required to load them.
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
            "05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb",
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
    "03-usb-joystick/output/usb_joystick_routed.kicad_pcb": {
        ("C11", "C10:1"),
        ("C11", "C10:2"),
        ("R11", "R10:1"),
        ("R11", "R10:2"),
    },
    "05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb": {
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
    "06-diffpair-test/output/diffpair_test_routed.kicad_pcb": {
        ("U1", "U1:12"),
        ("U1", "U1:13"),
        ("U2", "U2:A4"),
        ("U3", "U3:18"),
        ("U3", "U3:19"),
        ("U4", "U4:9"),
        ("U4", "U4:10"),
    },
    "07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb": {
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
        _BOARD_ROOT, "05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb"
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
