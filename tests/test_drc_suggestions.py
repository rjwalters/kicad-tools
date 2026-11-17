"""Tests for kicad_tools.drc.suggestions module."""

from pathlib import Path

import pytest

from kicad_tools.drc import (
    DRCReport,
    DRCViolation,
    FixAction,
    FixSuggestion,
    Location,
    Severity,
    ViolationType,
    generate_fix_suggestions,
)
from kicad_tools.drc.suggestions import (
    calculate_clearance_fix,
    direction_name,
)


@pytest.fixture
def sample_drc_report(fixtures_dir: Path) -> Path:
    """Return the path to the sample DRC report."""
    return fixtures_dir / "sample_drc.rpt"


class TestDirectionName:
    """Tests for direction_name helper function."""

    def test_right(self):
        """Test rightward direction."""
        assert direction_name(1.0, 0.0) == "right"

    def test_left(self):
        """Test leftward direction."""
        assert direction_name(-1.0, 0.0) == "left"

    def test_up(self):
        """Test upward direction (negative Y in KiCad)."""
        assert direction_name(0.0, -1.0) == "up"

    def test_down(self):
        """Test downward direction (positive Y in KiCad)."""
        assert direction_name(0.0, 1.0) == "down"

    def test_up_right(self):
        """Test diagonal up-right direction."""
        assert direction_name(1.0, -1.0) == "up-right"

    def test_down_left(self):
        """Test diagonal down-left direction."""
        assert direction_name(-1.0, 1.0) == "down-left"

    def test_in_place(self):
        """Test zero movement."""
        assert direction_name(0.0, 0.0) == "in place"


class TestFixSuggestion:
    """Tests for FixSuggestion dataclass."""

    def test_to_dict(self):
        """Test serialization to dictionary."""
        suggestion = FixSuggestion(
            action=FixAction.MOVE,
            target="R1",
            parameters={"dx": 0.5, "dy": 0.0},
            description="Move R1 0.5mm right",
            priority=1,
            complexity="easy",
        )

        d = suggestion.to_dict()
        assert d["action"] == "move"
        assert d["target"] == "R1"
        assert d["parameters"]["dx"] == 0.5
        assert d["description"] == "Move R1 0.5mm right"

    def test_to_dict_with_alternatives(self):
        """Test serialization includes alternatives."""
        alt = FixSuggestion(
            action=FixAction.REROUTE,
            target="NET1",
            parameters={},
            description="Reroute NET1",
        )
        suggestion = FixSuggestion(
            action=FixAction.MOVE,
            target="R1",
            parameters={},
            description="Move R1",
            alternatives=[alt],
        )

        d = suggestion.to_dict()
        assert len(d["alternatives"]) == 1
        assert d["alternatives"][0]["action"] == "reroute"

    def test_str(self):
        """Test string representation."""
        suggestion = FixSuggestion(
            action=FixAction.MOVE,
            target="R1",
            parameters={},
            description="Move R1 0.5mm right",
        )
        assert str(suggestion) == "Move R1 0.5mm right"


class TestClearanceFix:
    """Tests for clearance violation fix calculation."""

    def test_clearance_fix_with_values(self):
        """Test fix calculation when measurements are available."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            required_value_mm=0.2,
            actual_value_mm=0.15,
            locations=[
                Location(x_mm=100.0, y_mm=50.0, layer="F.Cu"),
                Location(x_mm=100.5, y_mm=50.0, layer="F.Cu"),
            ],
            items=["Pad 1 of R1", "Via [GND]"],
        )

        suggestion = calculate_clearance_fix(violation)
        assert suggestion is not None
        assert suggestion.action == FixAction.MOVE
        assert suggestion.target == "Pad 1 of R1"
        assert "distance_mm" in suggestion.parameters
        # Delta = 0.2 - 0.15 + 0.1 margin = 0.15
        assert suggestion.parameters["distance_mm"] == pytest.approx(0.15, rel=0.01)

    def test_clearance_fix_includes_direction(self):
        """Test that fix includes direction in description."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            required_value_mm=0.2,
            actual_value_mm=0.15,
            locations=[
                Location(x_mm=100.0, y_mm=50.0, layer="F.Cu"),
                Location(x_mm=99.0, y_mm=50.0, layer="F.Cu"),  # Element2 is to the left
            ],
            items=["R1", "U1"],
        )

        suggestion = calculate_clearance_fix(violation)
        assert suggestion is not None
        # Element1 should move away from Element2 (to the right)
        assert "right" in suggestion.description.lower()

    def test_clearance_fix_without_values(self):
        """Test fix when measurements are not available."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
        )

        suggestion = calculate_clearance_fix(violation)
        assert suggestion is not None
        assert suggestion.action == FixAction.REROUTE

    def test_clearance_fix_includes_alternatives(self):
        """Test that alternatives are provided."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            required_value_mm=0.2,
            actual_value_mm=0.15,
            locations=[
                Location(x_mm=100.0, y_mm=50.0, layer="F.Cu"),
                Location(x_mm=100.5, y_mm=50.0, layer="F.Cu"),
            ],
            items=["R1", "U1"],
            nets=["VCC"],
        )

        suggestion = calculate_clearance_fix(violation)
        assert suggestion is not None
        assert len(suggestion.alternatives) >= 1
        # Should have alternative to move the other element
        alt_targets = [alt.target for alt in suggestion.alternatives]
        assert "U1" in alt_targets or "VCC" in alt_targets

    def test_non_clearance_returns_none(self):
        """Test that non-clearance violations return None."""
        violation = DRCViolation(
            type=ViolationType.SHORTING_ITEMS,
            type_str="shorting_items",
            severity=Severity.ERROR,
            message="Short circuit",
        )

        suggestion = calculate_clearance_fix(violation)
        assert suggestion is None


class TestGenerateFixSuggestions:
    """Tests for the main generate_fix_suggestions function."""

    def test_clearance_suggestion(self, sample_drc_report: Path):
        """Test generating suggestion for clearance violation."""
        report = DRCReport.load(sample_drc_report)
        clearance_violations = report.by_type(ViolationType.CLEARANCE)

        assert len(clearance_violations) > 0
        suggestion = generate_fix_suggestions(clearance_violations[0])
        assert suggestion is not None
        assert suggestion.action == FixAction.MOVE

    def test_shorting_suggestion(self, sample_drc_report: Path):
        """Test generating suggestion for short circuit."""
        report = DRCReport.load(sample_drc_report)
        shorts = report.by_type(ViolationType.SHORTING_ITEMS)

        assert len(shorts) > 0
        suggestion = generate_fix_suggestions(shorts[0])
        assert suggestion is not None
        assert suggestion.action == FixAction.DELETE

    def test_unconnected_suggestion(self, sample_drc_report: Path):
        """Test generating suggestion for unconnected items."""
        report = DRCReport.load(sample_drc_report)
        unconnected = report.by_type(ViolationType.UNCONNECTED_ITEMS)

        assert len(unconnected) > 0
        suggestion = generate_fix_suggestions(unconnected[0])
        assert suggestion is not None
        assert suggestion.action == FixAction.CONNECT

    def test_track_width_suggestion(self, sample_drc_report: Path):
        """Test generating suggestion for track width violation."""
        report = DRCReport.load(sample_drc_report)
        track_violations = report.by_type(ViolationType.TRACK_WIDTH)

        assert len(track_violations) > 0
        suggestion = generate_fix_suggestions(track_violations[0])
        assert suggestion is not None
        assert suggestion.action == FixAction.RESIZE

    def test_via_annular_width_suggestion(self):
        """Test generating suggestion for via annular width violation."""
        violation = DRCViolation(
            type=ViolationType.VIA_ANNULAR_WIDTH,
            type_str="via_annular_width",
            severity=Severity.ERROR,
            message="Via annular width too small",
            required_value_mm=0.15,
            actual_value_mm=0.10,
        )

        suggestion = generate_fix_suggestions(violation)
        assert suggestion is not None
        assert suggestion.action == FixAction.RESIZE
        assert "pad" in suggestion.description.lower() or "via" in suggestion.description.lower()

    def test_silk_over_copper_suggestion(self):
        """Test generating suggestion for silkscreen over copper."""
        violation = DRCViolation(
            type=ViolationType.SILK_OVER_COPPER,
            type_str="silk_over_copper",
            severity=Severity.WARNING,
            message="Silkscreen over copper",
        )

        suggestion = generate_fix_suggestions(violation)
        assert suggestion is not None
        assert suggestion.action == FixAction.MOVE

    def test_copper_edge_clearance_suggestion(self):
        """Test generating suggestion for copper-to-edge clearance."""
        violation = DRCViolation(
            type=ViolationType.COPPER_EDGE_CLEARANCE,
            type_str="copper_edge_clearance",
            severity=Severity.ERROR,
            message="Copper too close to edge",
            required_value_mm=0.25,
            actual_value_mm=0.15,
        )

        suggestion = generate_fix_suggestions(violation)
        assert suggestion is not None
        assert suggestion.action == FixAction.MOVE
        assert "edge" in suggestion.description.lower()


class TestDRCViolationWithSuggestions:
    """Tests for DRCViolation integration with suggestions."""

    def test_violation_to_dict_includes_suggestions(self):
        """Test that to_dict includes suggestions when populated."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            required_value_mm=0.2,
            actual_value_mm=0.15,
        )

        # Add suggestion
        suggestion = generate_fix_suggestions(violation)
        if suggestion:
            violation.suggestions = [suggestion]

        d = violation.to_dict()
        assert "suggestions" in d
        assert len(d["suggestions"]) == 1
        assert d["suggestions"][0]["action"] == "move" or d["suggestions"][0]["action"] == "reroute"

    def test_violation_delta_mm(self):
        """Test delta_mm property calculation."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            required_value_mm=0.2,
            actual_value_mm=0.15,
        )

        assert violation.delta_mm == pytest.approx(0.05)

    def test_violation_delta_mm_none_when_missing(self):
        """Test delta_mm returns None when values missing."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
        )

        assert violation.delta_mm is None

    def test_to_dict_includes_delta(self):
        """Test that to_dict includes delta_mm."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            required_value_mm=0.2,
            actual_value_mm=0.15,
        )

        d = violation.to_dict()
        assert d["delta_mm"] == pytest.approx(0.05)


class TestFixActionEnum:
    """Tests for FixAction enum."""

    def test_all_actions_have_values(self):
        """Test that all actions have string values."""
        for action in FixAction:
            assert isinstance(action.value, str)
            assert len(action.value) > 0

    def test_expected_actions_exist(self):
        """Test that expected action types exist."""
        expected = ["move", "reroute", "resize", "delete", "connect", "adjust_rule"]
        actual = [a.value for a in FixAction]
        for exp in expected:
            assert exp in actual
