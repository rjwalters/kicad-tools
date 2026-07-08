"""Tests for the geometric silkscreen DRC rules.

Covers the two checks added in issue #3844:

- ``check_silk_over_copper`` -- silk text/graphics over exposed pad mask
  apertures (supersedes the crude ``silkscreen_over_pad`` centroid heuristic).
- ``check_silk_edge_clearance`` -- silk text/graphics too close to / crossing
  the ``Edge.Cuts`` board outline.

Both emit ``severity="warning"`` so they do not block the manufacturing gate.
"""

from __future__ import annotations

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import (
    PCB,
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
    check_silk_edge_clearance,
    check_silk_over_copper,
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

    def test_one_violation_per_silk_element(self):
        """A silk element overlapping two pads still fires only once."""
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
        assert len(results) == 1


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
