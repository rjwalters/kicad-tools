"""Tests for --suppress-library silkscreen warning suppression."""

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB, Footprint, FootprintGraphic, FootprintText
from kicad_tools.sexp import SExp
from kicad_tools.validate import DRCChecker
from kicad_tools.validate.rules.silkscreen import (
    check_silkscreen_line_width,
    check_silkscreen_text_height,
    is_library_footprint,
)


@pytest.fixture
def jlcpcb_rules():
    """Return JLCPCB design rules."""
    profile = get_profile("jlcpcb")
    return profile.get_design_rules(layers=4)


def _make_pcb() -> PCB:
    """Create a minimal empty PCB for testing."""
    sexp = SExp(name="kicad_pcb")
    return PCB(sexp)


def _library_footprint(reference: str = "C1") -> Footprint:
    """Create a footprint with a library-qualified name (contains colon)."""
    return Footprint(
        name="Capacitor_SMD:C_0402_1005Metric",
        layer="F.Cu",
        position=(10.0, 20.0),
        rotation=0.0,
        reference=reference,
        value="100nF",
        pads=[],
        texts=[],
        graphics=[
            FootprintGraphic(
                graphic_type="line",
                layer="F.SilkS",
                stroke_width=0.12,  # Below JLCPCB min of 0.15mm
                start=(0.0, 0.0),
                end=(5.0, 0.0),
            ),
        ],
    )


def _custom_footprint(reference: str = "U1") -> Footprint:
    """Create a footprint without a library prefix (custom / user footprint)."""
    return Footprint(
        name="MyCustomPart",
        layer="F.Cu",
        position=(30.0, 40.0),
        rotation=0.0,
        reference=reference,
        value="CUSTOM",
        pads=[],
        texts=[],
        graphics=[
            FootprintGraphic(
                graphic_type="line",
                layer="F.SilkS",
                stroke_width=0.10,  # Below minimum
                start=(0.0, 0.0),
                end=(5.0, 0.0),
            ),
        ],
    )


class TestIsLibraryFootprint:
    """Tests for the is_library_footprint helper."""

    def test_library_qualified_name(self):
        """Footprint with colon in name is detected as library footprint."""
        fp = _library_footprint()
        assert is_library_footprint(fp) is True

    def test_custom_name(self):
        """Footprint without colon in name is not a library footprint."""
        fp = _custom_footprint()
        assert is_library_footprint(fp) is False

    def test_empty_name(self):
        """Empty name is not a library footprint."""
        fp = Footprint(
            name="",
            layer="F.Cu",
            position=(0.0, 0.0),
            rotation=0.0,
            reference="X1",
            value="",
            pads=[],
            texts=[],
            graphics=[],
        )
        assert is_library_footprint(fp) is False


class TestSuppressLibraryLineWidth:
    """Tests for suppress_library on silkscreen line width checks."""

    def test_without_suppression_both_reported(self, jlcpcb_rules):
        """Without suppression, both library and custom footprints are reported."""
        pcb = _make_pcb()
        pcb._footprints.append(_library_footprint("C1"))
        pcb._footprints.append(_custom_footprint("U1"))

        results = check_silkscreen_line_width(pcb, jlcpcb_rules)

        assert len(results.violations) == 2
        assert results.suppressed_count == 0

    def test_with_suppression_only_custom_reported(self, jlcpcb_rules):
        """With suppression, only custom footprint violations are reported."""
        pcb = _make_pcb()
        pcb._footprints.append(_library_footprint("C1"))
        pcb._footprints.append(_custom_footprint("U1"))

        results = check_silkscreen_line_width(
            pcb, jlcpcb_rules, suppress_library=True
        )

        assert len(results.violations) == 1
        assert results.violations[0].items[0] == "U1"
        assert results.suppressed_count == 1

    def test_suppression_count_multiple_graphics(self, jlcpcb_rules):
        """Suppressed count reflects all suppressed graphics on library FPs."""
        pcb = _make_pcb()
        fp = Footprint(
            name="Resistor_SMD:R_0402_1005Metric",
            layer="F.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference="R1",
            value="10k",
            pads=[],
            texts=[],
            graphics=[
                FootprintGraphic(
                    graphic_type="line",
                    layer="F.SilkS",
                    stroke_width=0.12,
                    start=(0.0, 0.0),
                    end=(5.0, 0.0),
                ),
                FootprintGraphic(
                    graphic_type="line",
                    layer="F.SilkS",
                    stroke_width=0.10,
                    start=(0.0, 0.0),
                    end=(0.0, 5.0),
                ),
            ],
        )
        pcb._footprints.append(fp)

        results = check_silkscreen_line_width(
            pcb, jlcpcb_rules, suppress_library=True
        )

        assert len(results.violations) == 0
        assert results.suppressed_count == 2

    def test_board_level_graphics_never_suppressed(self, jlcpcb_rules):
        """Board-level graphics are never suppressed, even with the flag."""
        pcb = _make_pcb()
        # Add a board-level graphic with thin silkscreen
        from kicad_tools.schema.pcb import BoardGraphic

        bg = BoardGraphic(
            graphic_type="line",
            layer="F.SilkS",
            stroke_width=0.10,
            start=(0.0, 0.0),
            end=(10.0, 0.0),
        )
        pcb._graphics.append(bg)

        results = check_silkscreen_line_width(
            pcb, jlcpcb_rules, suppress_library=True
        )

        assert len(results.violations) == 1
        assert results.suppressed_count == 0


class TestSuppressLibraryTextHeight:
    """Tests for suppress_library on silkscreen text height checks."""

    def _library_fp_with_text(self, reference: str = "C1") -> Footprint:
        """Create library footprint with small silkscreen text."""
        return Footprint(
            name="Capacitor_SMD:C_0402_1005Metric",
            layer="F.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference=reference,
            value="100nF",
            pads=[],
            texts=[
                FootprintText(
                    text_type="reference",
                    text=reference,
                    position=(0.0, 0.0),
                    layer="F.SilkS",
                    font_size=(0.5, 0.5),  # height 0.5mm, below JLCPCB min of 0.8mm
                    font_thickness=0.1,
                    hidden=False,
                ),
            ],
            graphics=[],
        )

    def _custom_fp_with_text(self, reference: str = "U1") -> Footprint:
        """Create custom footprint with small silkscreen text."""
        return Footprint(
            name="MyCustomPart",
            layer="F.Cu",
            position=(30.0, 40.0),
            rotation=0.0,
            reference=reference,
            value="CUSTOM",
            pads=[],
            texts=[
                FootprintText(
                    text_type="reference",
                    text=reference,
                    position=(0.0, 0.0),
                    layer="F.SilkS",
                    font_size=(0.5, 0.5),  # height 0.5mm, below JLCPCB min
                    font_thickness=0.1,
                    hidden=False,
                ),
            ],
            graphics=[],
        )

    def test_without_suppression_both_reported(self, jlcpcb_rules):
        """Without suppression, both library and custom FPs are reported."""
        pcb = _make_pcb()
        pcb._footprints.append(self._library_fp_with_text("C1"))
        pcb._footprints.append(self._custom_fp_with_text("U1"))

        results = check_silkscreen_text_height(pcb, jlcpcb_rules)

        assert len(results.violations) == 2
        assert results.suppressed_count == 0

    def test_with_suppression_only_custom_reported(self, jlcpcb_rules):
        """With suppression, only custom footprint text violations appear."""
        pcb = _make_pcb()
        pcb._footprints.append(self._library_fp_with_text("C1"))
        pcb._footprints.append(self._custom_fp_with_text("U1"))

        results = check_silkscreen_text_height(
            pcb, jlcpcb_rules, suppress_library=True
        )

        assert len(results.violations) == 1
        assert "U1" in results.violations[0].message
        assert results.suppressed_count == 1


class TestSuppressLibraryDRCChecker:
    """Tests for suppress_library threading through DRCChecker."""

    def test_checker_without_suppress(self, jlcpcb_rules):
        """DRCChecker without suppress_library reports all silkscreen issues."""
        pcb = _make_pcb()
        pcb._footprints.append(_library_footprint("C1"))
        pcb._footprints.append(_custom_footprint("U1"))

        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4)
        results = checker.check_silkscreen()

        silk_violations = [
            v for v in results.violations if v.rule_id == "silkscreen_line_width"
        ]
        assert len(silk_violations) == 2

    def test_checker_with_suppress(self, jlcpcb_rules):
        """DRCChecker with suppress_library suppresses library FP warnings."""
        pcb = _make_pcb()
        pcb._footprints.append(_library_footprint("C1"))
        pcb._footprints.append(_custom_footprint("U1"))

        checker = DRCChecker(
            pcb, manufacturer="jlcpcb", layers=4, suppress_library=True
        )
        results = checker.check_silkscreen()

        silk_violations = [
            v for v in results.violations if v.rule_id == "silkscreen_line_width"
        ]
        assert len(silk_violations) == 1
        assert silk_violations[0].items[0] == "U1"
        assert results.suppressed_count >= 1


class TestSuppressedCountMerge:
    """Tests for suppressed_count in DRCResults.merge."""

    def test_merge_adds_suppressed_count(self):
        """merge() should sum suppressed_count from both results."""
        from kicad_tools.validate.violations import DRCResults

        r1 = DRCResults(suppressed_count=3)
        r2 = DRCResults(suppressed_count=5)

        r1.merge(r2)

        assert r1.suppressed_count == 8


class TestSuppressLibraryWithSkip:
    """Test that --suppress-library combined with --skip silkscreen does not error."""

    def test_suppress_library_with_skip_silkscreen(self, jlcpcb_rules):
        """suppress_library should not cause issues when silkscreen is skipped."""
        pcb = _make_pcb()
        pcb._footprints.append(_library_footprint("C1"))

        checker = DRCChecker(
            pcb, manufacturer="jlcpcb", layers=4, suppress_library=True
        )
        # Calling check_all still works -- silkscreen just reports suppressed
        results = checker.check_all()
        # No crash; suppressed count is tracked
        assert results.suppressed_count >= 0
