"""Tests for kicad_tools.feedback.suggestions module."""

import pytest

from kicad_tools.drc.suggestions import FixAction, FixSuggestion
from kicad_tools.drc.violation import DRCViolation, Severity, ViolationType
from kicad_tools.erc.violation import ERCViolation, ERCViolationType
from kicad_tools.erc.violation import Severity as ERCSeverity
from kicad_tools.feedback import (
    FixSuggestionGenerator,
    generate_drc_suggestions,
    generate_erc_suggestions,
)


class TestFixSuggestionGenerator:
    """Tests for FixSuggestionGenerator class."""

    @pytest.fixture
    def generator(self) -> FixSuggestionGenerator:
        """Create a FixSuggestionGenerator instance."""
        return FixSuggestionGenerator()

    def test_suggest_returns_list(self, generator: FixSuggestionGenerator) -> None:
        """Test that suggest() returns a list."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test violation",
        )
        suggestions = generator.suggest(violation)
        assert isinstance(suggestions, list)

    def test_suggest_non_empty_for_known_types(self, generator: FixSuggestionGenerator) -> None:
        """Test that known violation types get non-empty suggestions."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test violation",
        )
        suggestions = generator.suggest(violation)
        assert len(suggestions) > 0


class TestDRCSuggestions:
    """Tests for DRC-specific suggestions."""

    @pytest.fixture
    def generator(self) -> FixSuggestionGenerator:
        return FixSuggestionGenerator()

    def test_clearance_with_values(self, generator: FixSuggestionGenerator) -> None:
        """Test clearance suggestions when values are available."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation (required 0.20mm, actual 0.15mm)",
            required_value_mm=0.20,
            actual_value_mm=0.15,
            nets=["GND", "VCC"],
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should mention the gap distance
        assert any("0.05" in s or "apart" in s.lower() for s in suggestions)

    def test_clearance_with_track(self, generator: FixSuggestionGenerator) -> None:
        """Test clearance suggestions for track-related violations."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance between track and via",
            items=["Track [GND] on F.Cu", "Via [VCC]"],
            nets=["GND", "VCC"],
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest rerouting
        assert any("reroute" in s.lower() for s in suggestions)

    def test_unconnected_items(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for unconnected items."""
        violation = DRCViolation(
            type=ViolationType.UNCONNECTED_ITEMS,
            type_str="unconnected_items",
            severity=Severity.ERROR,
            message="Unconnected pad",
            items=["Pad 1 of R1"],
            nets=["VCC"],
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest connecting or routing
        assert any("connect" in s.lower() or "route" in s.lower() for s in suggestions)

    def test_shorting_items(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for shorting items."""
        violation = DRCViolation(
            type=ViolationType.SHORTING_ITEMS,
            type_str="shorting_items",
            severity=Severity.ERROR,
            message="Short between nets",
            nets=["VCC", "GND"],
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest removing the short
        assert any("remove" in s.lower() or "reroute" in s.lower() for s in suggestions)

    def test_track_width(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for track width violations."""
        violation = DRCViolation(
            type=ViolationType.TRACK_WIDTH,
            type_str="track_width",
            severity=Severity.ERROR,
            message="Track width too narrow",
            required_value_mm=0.25,
            actual_value_mm=0.15,
            nets=["POWER"],
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest widening track
        assert any("widen" in s.lower() or "width" in s.lower() for s in suggestions)

    def test_via_annular_ring(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for via annular ring violations."""
        violation = DRCViolation(
            type=ViolationType.VIA_ANNULAR_WIDTH,
            type_str="via_annular_width",
            severity=Severity.ERROR,
            message="Annular ring too small",
            required_value_mm=0.15,
            actual_value_mm=0.10,
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should mention via pad size or drill
        assert any(
            "via" in s.lower() or "pad" in s.lower() or "drill" in s.lower() for s in suggestions
        )

    def test_silk_over_copper(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for silk over copper violations."""
        violation = DRCViolation(
            type=ViolationType.SILK_OVER_COPPER,
            type_str="silk_over_copper",
            severity=Severity.WARNING,
            message="Silkscreen over copper",
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest moving silkscreen
        assert any("silkscreen" in s.lower() or "silk" in s.lower() for s in suggestions)

    def test_missing_footprint(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for missing footprint violations."""
        violation = DRCViolation(
            type=ViolationType.MISSING_FOOTPRINT,
            type_str="missing_footprint",
            severity=Severity.ERROR,
            message="Missing footprint for U1",
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest assigning footprint
        assert any("footprint" in s.lower() or "assign" in s.lower() for s in suggestions)

    def test_copper_edge_clearance(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for copper-to-edge clearance violations."""
        violation = DRCViolation(
            type=ViolationType.COPPER_EDGE_CLEARANCE,
            type_str="copper_edge_clearance",
            severity=Severity.ERROR,
            message="Copper too close to board edge",
            required_value_mm=0.30,
            actual_value_mm=0.20,
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest moving copper from edge
        assert any("edge" in s.lower() or "inward" in s.lower() for s in suggestions)

    def test_courtyard_overlap(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for courtyard overlap violations."""
        violation = DRCViolation(
            type=ViolationType.COURTYARD_OVERLAP,
            type_str="courtyard_overlap",
            severity=Severity.ERROR,
            message="Courtyard overlap between U1 and C1",
            items=["Footprint U1", "Footprint C1"],
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest moving components
        assert any("move" in s.lower() or "spacing" in s.lower() for s in suggestions)

    def test_unknown_type(self, generator: FixSuggestionGenerator) -> None:
        """Test that unknown violation types still get generic suggestions."""
        violation = DRCViolation(
            type=ViolationType.UNKNOWN,
            type_str="some_unknown_type",
            severity=Severity.ERROR,
            message="Unknown violation",
        )
        suggestions = generator.suggest(violation)

        # Should still return suggestions (generic ones)
        assert len(suggestions) > 0


class TestERCSuggestions:
    """Tests for ERC-specific suggestions."""

    @pytest.fixture
    def generator(self) -> FixSuggestionGenerator:
        return FixSuggestionGenerator()

    def test_pin_not_connected(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for unconnected pin."""
        violation = ERCViolation(
            type=ERCViolationType.PIN_NOT_CONNECTED,
            type_str="pin_not_connected",
            severity=ERCSeverity.ERROR,
            description="Unconnected pin",
            items=["Pin 1 of R1"],
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest connecting or adding no-connect
        assert any("connect" in s.lower() or "no-connect" in s.lower() for s in suggestions)

    def test_pin_not_driven(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for input pin not driven."""
        violation = ERCViolation(
            type=ERCViolationType.PIN_NOT_DRIVEN,
            type_str="pin_not_driven",
            severity=ERCSeverity.WARNING,
            description="Input pin not driven",
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest driving the input
        assert any(
            "driver" in s.lower() or "pull" in s.lower() or "output" in s.lower()
            for s in suggestions
        )

    def test_power_pin_not_driven(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for power input not driven."""
        violation = ERCViolation(
            type=ERCViolationType.POWER_PIN_NOT_DRIVEN,
            type_str="power_pin_not_driven",
            severity=ERCSeverity.ERROR,
            description="Power input not driven",
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest power symbol or flag
        assert any("power" in s.lower() for s in suggestions)

    def test_duplicate_reference(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for duplicate reference designator."""
        violation = ERCViolation(
            type=ERCViolationType.DUPLICATE_REFERENCE,
            type_str="duplicate_reference",
            severity=ERCSeverity.ERROR,
            description="Duplicate reference: R1",
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest annotating or renaming
        assert any("annotate" in s.lower() or "reference" in s.lower() for s in suggestions)

    def test_label_dangling(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for dangling label."""
        violation = ERCViolation(
            type=ERCViolationType.LABEL_DANGLING,
            type_str="label_dangling",
            severity=ERCSeverity.WARNING,
            description="Label not connected",
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest connecting or deleting
        assert any(
            "connect" in s.lower() or "wire" in s.lower() or "delete" in s.lower()
            for s in suggestions
        )

    def test_wire_dangling(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for dangling wire."""
        violation = ERCViolation(
            type=ERCViolationType.WIRE_DANGLING,
            type_str="wire_dangling",
            severity=ERCSeverity.WARNING,
            description="Wire not connected at both ends",
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest extending or deleting wire
        assert any(
            "wire" in s.lower() or "extend" in s.lower() or "delete" in s.lower()
            for s in suggestions
        )

    def test_unannotated(self, generator: FixSuggestionGenerator) -> None:
        """Test suggestions for unannotated symbol."""
        violation = ERCViolation(
            type=ERCViolationType.UNANNOTATED,
            type_str="unannotated",
            severity=ERCSeverity.ERROR,
            description="Symbol not annotated",
        )
        suggestions = generator.suggest(violation)

        assert len(suggestions) > 0
        # Should suggest running annotation
        assert any("annotate" in s.lower() or "reference" in s.lower() for s in suggestions)

    def test_unknown_erc_type(self, generator: FixSuggestionGenerator) -> None:
        """Test that unknown ERC types still get generic suggestions."""
        violation = ERCViolation(
            type=ERCViolationType.UNKNOWN,
            type_str="unknown_type",
            severity=ERCSeverity.WARNING,
            description="Unknown issue",
        )
        suggestions = generator.suggest(violation)

        # Should still return suggestions (generic ones)
        assert len(suggestions) > 0


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_generate_drc_suggestions(self) -> None:
        """Test generate_drc_suggestions function."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test violation",
        )
        suggestions = generate_drc_suggestions(violation)
        assert isinstance(suggestions, list)
        assert len(suggestions) > 0

    def test_generate_erc_suggestions(self) -> None:
        """Test generate_erc_suggestions function."""
        violation = ERCViolation(
            type=ERCViolationType.PIN_NOT_CONNECTED,
            type_str="pin_not_connected",
            severity=ERCSeverity.ERROR,
            description="Test violation",
        )
        suggestions = generate_erc_suggestions(violation)
        assert isinstance(suggestions, list)
        assert len(suggestions) > 0


class TestDRCReportIntegration:
    """Tests for DRC report integration with suggestions."""

    def test_parsed_violations_have_suggestions(self, fixtures_dir) -> None:
        """Test that violations parsed from reports have suggestions."""
        from kicad_tools.drc import DRCReport

        sample_report = fixtures_dir / "sample_drc.rpt"
        if not sample_report.exists():
            pytest.skip("Sample DRC report not found")

        report = DRCReport.load(sample_report)

        # All violations should have suggestions
        for violation in report.violations:
            assert hasattr(violation, "suggestions")
            assert isinstance(violation.suggestions, list)
            # Most violation types should have at least one suggestion
            if violation.type != ViolationType.UNKNOWN:
                assert len(violation.suggestions) > 0, f"No suggestions for {violation.type_str}"

    def test_to_dict_includes_suggestions(self) -> None:
        """Test that to_dict includes suggestions."""
        suggestions = [
            FixSuggestion(
                action=FixAction.MOVE,
                target="U1",
                description="Move component",
            ),
            FixSuggestion(
                action=FixAction.RESIZE,
                target="U1",
                description="Increase spacing",
            ),
        ]
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test violation",
            suggestions=suggestions,
        )
        d = violation.to_dict()
        assert "suggestions" in d
        assert len(d["suggestions"]) == 2
        assert d["suggestions"][0]["description"] == "Move component"
        assert d["suggestions"][1]["description"] == "Increase spacing"


class TestERCReportIntegration:
    """Tests for ERC report integration with suggestions."""

    def test_to_dict_includes_suggestions(self) -> None:
        """Test that to_dict includes suggestions."""
        violation = ERCViolation(
            type=ERCViolationType.PIN_NOT_CONNECTED,
            type_str="pin_not_connected",
            severity=ERCSeverity.ERROR,
            description="Test violation",
            suggestions=["Connect pin", "Add no-connect flag"],
        )
        d = violation.to_dict()
        assert "suggestions" in d
        assert d["suggestions"] == ["Connect pin", "Add no-connect flag"]
