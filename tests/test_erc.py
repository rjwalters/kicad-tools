"""Tests for kicad_tools.erc module."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.erc import (
    ERC_TYPE_DESCRIPTIONS,
    ERCReport,
    ERCViolationType,
    Severity,
)
from kicad_tools.erc.cross_sheet import (
    _extract_power_net_name,
    filter_cross_sheet_power_violations,
)


@pytest.fixture
def sample_erc_report(fixtures_dir: Path) -> Path:
    """Return the path to the sample ERC report."""
    return fixtures_dir / "sample_erc.json"


class TestERCReportParsing:
    """Tests for ERC report parsing."""

    def test_load_json_report(self, sample_erc_report: Path):
        """Test loading a JSON format ERC report."""
        report = ERCReport.load(sample_erc_report)
        assert report is not None
        assert report.source_file == "test-schematic.kicad_sch"
        assert report.kicad_version == "8.0.0"

    def test_parse_violation_count(self, sample_erc_report: Path):
        """Test that violations are parsed correctly."""
        report = ERCReport.load(sample_erc_report)
        # 7 total violations, 1 excluded
        assert report.violation_count == 6

    def test_error_count(self, sample_erc_report: Path):
        """Test counting error-level violations."""
        report = ERCReport.load(sample_erc_report)
        assert report.error_count == 4

    def test_warning_count(self, sample_erc_report: Path):
        """Test counting warning-level violations."""
        report = ERCReport.load(sample_erc_report)
        # 3 warnings, but 1 is excluded
        assert report.warning_count == 2

    def test_exclusion_count(self, sample_erc_report: Path):
        """Test counting excluded violations."""
        report = ERCReport.load(sample_erc_report)
        assert report.exclusion_count == 1


class TestERCViolation:
    """Tests for ERC violation parsing."""

    def test_parse_pin_not_connected(self, sample_erc_report: Path):
        """Test parsing pin_not_connected violation."""
        report = ERCReport.load(sample_erc_report)
        pin_violations = report.by_type(ERCViolationType.PIN_NOT_CONNECTED)
        assert len(pin_violations) == 1

        v = pin_violations[0]
        assert v.type == ERCViolationType.PIN_NOT_CONNECTED
        assert v.severity == Severity.ERROR
        assert v.is_connection_issue

    def test_parse_locations(self, sample_erc_report: Path):
        """Test parsing violation locations."""
        report = ERCReport.load(sample_erc_report)
        v = report.violations[0]

        assert v.pos_x == pytest.approx(100.0)
        assert v.pos_y == pytest.approx(50.0)

    def test_parse_items(self, sample_erc_report: Path):
        """Test parsing violation items."""
        report = ERCReport.load(sample_erc_report)
        v = report.violations[0]

        assert len(v.items) >= 1
        assert "R1" in v.items[0]

    def test_parse_sheet(self, sample_erc_report: Path):
        """Test parsing sheet information."""
        report = ERCReport.load(sample_erc_report)
        subsheet_violations = report.by_sheet("/subsheet")
        assert len(subsheet_violations) == 1  # One not excluded
        assert subsheet_violations[0].sheet == "/subsheet"

    def test_type_description(self, sample_erc_report: Path):
        """Test getting type description."""
        report = ERCReport.load(sample_erc_report)
        v = report.violations[0]
        assert v.type_description == "Unconnected pin"


class TestERCReportQueries:
    """Tests for ERC report query methods."""

    def test_by_type(self, sample_erc_report: Path):
        """Test filtering violations by type."""
        report = ERCReport.load(sample_erc_report)
        label_violations = report.by_type(ERCViolationType.LABEL_DANGLING)
        assert len(label_violations) == 1

    def test_by_sheet(self, sample_erc_report: Path):
        """Test filtering violations by sheet."""
        report = ERCReport.load(sample_erc_report)
        root_violations = report.by_sheet("/")
        assert len(root_violations) == 5

    def test_violations_by_type(self, sample_erc_report: Path):
        """Test grouping violations by type."""
        report = ERCReport.load(sample_erc_report)
        grouped = report.violations_by_type()

        assert ERCViolationType.PIN_NOT_CONNECTED in grouped
        assert len(grouped[ERCViolationType.PIN_NOT_CONNECTED]) == 1

    def test_violations_by_sheet(self, sample_erc_report: Path):
        """Test grouping violations by sheet."""
        report = ERCReport.load(sample_erc_report)
        grouped = report.violations_by_sheet()

        assert "/" in grouped
        assert "/subsheet" in grouped
        assert len(grouped["/"]) == 5

    def test_filter_by_type(self, sample_erc_report: Path):
        """Test filtering by type (partial match)."""
        report = ERCReport.load(sample_erc_report)

        # Filter by partial match on type (matches label_dangling, similar_labels, hier_label_mismatch)
        label_violations = report.filter_by_type("label")
        assert len(label_violations) == 3

        # Filter by partial match on description
        connected = report.filter_by_type("connected")
        assert len(connected) >= 1

    def test_summary(self, sample_erc_report: Path):
        """Test generating report summary."""
        report = ERCReport.load(sample_erc_report)
        summary = report.summary()

        assert summary["total_violations"] == 6
        assert summary["errors"] == 4
        assert summary["warnings"] == 2
        assert summary["exclusions"] == 1


class TestERCViolationType:
    """Tests for violation type enum."""

    def test_from_string_pin_not_connected(self):
        """Test parsing pin_not_connected type."""
        assert (
            ERCViolationType.from_string("pin_not_connected") == ERCViolationType.PIN_NOT_CONNECTED
        )

    def test_from_string_power_pin(self):
        """Test parsing power_pin_not_driven type."""
        assert (
            ERCViolationType.from_string("power_pin_not_driven")
            == ERCViolationType.POWER_PIN_NOT_DRIVEN
        )

    def test_from_string_label(self):
        """Test parsing label types."""
        assert ERCViolationType.from_string("label_dangling") == ERCViolationType.LABEL_DANGLING
        assert (
            ERCViolationType.from_string("global_label_dangling")
            == ERCViolationType.GLOBAL_LABEL_DANGLING
        )

    def test_from_string_unknown(self):
        """Test parsing unknown type."""
        assert ERCViolationType.from_string("some_random_type") == ERCViolationType.UNKNOWN

    def test_new_types_parse_correctly(self):
        """Test that new ERC types parse to their enum values, not UNKNOWN."""
        assert (
            ERCViolationType.from_string("lib_symbol_mismatch")
            == ERCViolationType.LIB_SYMBOL_MISMATCH
        )
        assert (
            ERCViolationType.from_string("footprint_link_issues")
            == ERCViolationType.FOOTPRINT_LINK_ISSUES
        )
        assert ERCViolationType.from_string("pin_to_pin") == ERCViolationType.PIN_TO_PIN
        assert (
            ERCViolationType.from_string("isolated_pin_label")
            == ERCViolationType.ISOLATED_PIN_LABEL
        )
        assert (
            ERCViolationType.from_string("single_global_label")
            == ERCViolationType.SINGLE_GLOBAL_LABEL
        )

    def test_new_types_not_unknown(self):
        """Verify none of the five new types resolve to UNKNOWN."""
        new_type_strings = [
            "lib_symbol_mismatch",
            "footprint_link_issues",
            "pin_to_pin",
            "isolated_pin_label",
            "single_global_label",
        ]
        for type_str in new_type_strings:
            parsed = ERCViolationType.from_string(type_str)
            assert parsed != ERCViolationType.UNKNOWN, f"'{type_str}' should not parse as UNKNOWN"


class TestSeverity:
    """Tests for Severity enum."""

    def test_from_string_error(self):
        """Test parsing error severity."""
        assert Severity.from_string("error") == Severity.ERROR
        assert Severity.from_string("ERROR") == Severity.ERROR

    def test_from_string_warning(self):
        """Test parsing warning severity."""
        assert Severity.from_string("warning") == Severity.WARNING
        assert Severity.from_string("WARNING") == Severity.WARNING

    def test_from_string_exclusion(self):
        """Test parsing exclusion severity."""
        assert Severity.from_string("exclusion") == Severity.EXCLUSION


class TestERCViolationMethods:
    """Tests for ERCViolation methods."""

    def test_is_error(self, sample_erc_report: Path):
        """Test is_error property."""
        report = ERCReport.load(sample_erc_report)

        # First violation is error
        assert report.violations[0].is_error

        # Find a warning
        warnings = [
            v for v in report.violations if v.severity == Severity.WARNING and not v.excluded
        ]
        assert len(warnings) >= 1
        assert not warnings[0].is_error

    def test_is_connection_issue(self, sample_erc_report: Path):
        """Test is_connection_issue property."""
        report = ERCReport.load(sample_erc_report)
        pin_violations = report.by_type(ERCViolationType.PIN_NOT_CONNECTED)
        assert pin_violations[0].is_connection_issue

        label_violations = report.by_type(ERCViolationType.LABEL_DANGLING)
        assert not label_violations[0].is_connection_issue

    def test_is_label_issue(self, sample_erc_report: Path):
        """Test is_label_issue property."""
        report = ERCReport.load(sample_erc_report)
        label_violations = report.by_type(ERCViolationType.LABEL_DANGLING)
        assert label_violations[0].is_label_issue

        pin_violations = report.by_type(ERCViolationType.PIN_NOT_CONNECTED)
        assert not pin_violations[0].is_label_issue

    def test_location_str(self, sample_erc_report: Path):
        """Test location_str property."""
        report = ERCReport.load(sample_erc_report)
        v = report.violations[0]

        loc = v.location_str
        assert "100" in loc
        assert "50" in loc

    def test_to_dict(self, sample_erc_report: Path):
        """Test converting violation to dict."""
        report = ERCReport.load(sample_erc_report)
        v = report.violations[0]
        d = v.to_dict()

        assert "type" in d
        assert "severity" in d
        assert "description" in d
        assert "position" in d
        assert d["type"] == "pin_not_connected"


class TestERCTypeDescriptions:
    """Tests for ERC type descriptions."""

    def test_all_types_have_descriptions(self):
        """Test that all enum types have descriptions."""
        for vtype in ERCViolationType:
            if vtype != ERCViolationType.UNKNOWN:
                assert vtype.value in ERC_TYPE_DESCRIPTIONS

    def test_description_content(self):
        """Test specific descriptions."""
        assert ERC_TYPE_DESCRIPTIONS["pin_not_connected"] == "Unconnected pin"
        assert ERC_TYPE_DESCRIPTIONS["power_pin_not_driven"] == "Power input not driven"
        assert ERC_TYPE_DESCRIPTIONS["label_dangling"] == "Label not connected"


class TestExtractPowerNetName:
    """Tests for _extract_power_net_name helper."""

    def test_extract_from_item_with_type(self):
        """Extract net name from 'Pin VCC (power_in) of U1' format."""
        items = [{"description": "Pin VCC (power_in) of U1"}]
        assert _extract_power_net_name("Power pin not driven", items) == "VCC"

    def test_extract_from_item_without_type(self):
        """Extract net name from 'Pin +3V3 of U3' format."""
        items = [{"description": "Pin +3V3 of U3"}]
        assert _extract_power_net_name("Power pin not driven", items) == "+3V3"

    def test_extract_from_description_fallback(self):
        """Extract net name from top-level description when items empty."""
        assert _extract_power_net_name("Pin GND not driven") == "GND"

    def test_returns_none_for_unparseable(self):
        """Return None when no pin name can be extracted."""
        assert _extract_power_net_name("Some random text") is None
        assert _extract_power_net_name("Some random text", []) is None

    def test_prefers_power_in_match(self):
        """When (power_in) qualifier is present, prefer that match."""
        items = [{"description": "Pin +3.3V (power_in) of U3"}]
        assert _extract_power_net_name("", items) == "+3.3V"


class TestFilterCrossSheetPowerViolations:
    """Tests for filter_cross_sheet_power_violations."""

    def _make_violation(self, vtype: str, net_name: str) -> dict:
        """Create a synthetic violation dict."""
        return {
            "type": vtype,
            "severity": "error",
            "description": "Power input pin not driven by any power output",
            "items": [{"description": f"Pin {net_name} (power_in) of U1"}],
        }

    @patch("kicad_tools.erc.cross_sheet.build_power_driver_inventory")
    def test_suppresses_driven_net(self, mock_inventory):
        """Violation for a net with a driver on another sheet is suppressed."""
        mock_inventory.return_value = {"+3V3", "GND", "VCC"}

        violations = [
            self._make_violation("power_pin_not_driven", "VCC"),
            {"type": "pin_not_connected", "description": "other"},
        ]

        result = filter_cross_sheet_power_violations(violations, "/fake/path")
        assert len(result) == 1
        assert result[0]["type"] == "pin_not_connected"

    @patch("kicad_tools.erc.cross_sheet.build_power_driver_inventory")
    def test_preserves_undriven_net(self, mock_inventory):
        """Violation for a net with NO driver anywhere is preserved (true positive)."""
        mock_inventory.return_value = {"GND"}  # VCC is NOT driven

        violations = [
            self._make_violation("power_pin_not_driven", "VCC"),
        ]

        result = filter_cross_sheet_power_violations(violations, "/fake/path")
        assert len(result) == 1
        assert result[0]["type"] == "power_pin_not_driven"

    @patch("kicad_tools.erc.cross_sheet.build_power_driver_inventory")
    def test_preserves_unparseable_violation(self, mock_inventory):
        """Violation with unparseable description is preserved to be safe."""
        mock_inventory.return_value = {"+3V3"}

        violations = [
            {
                "type": "power_pin_not_driven",
                "severity": "error",
                "description": "Some generic message",
                "items": [],
            },
        ]

        result = filter_cross_sheet_power_violations(violations, "/fake/path")
        assert len(result) == 1

    def test_skips_traversal_when_no_target_violations(self):
        """No hierarchy traversal if no power_pin_not_driven violations exist."""
        violations = [
            {"type": "pin_not_connected", "description": "other"},
        ]

        # Should return immediately without calling build_power_driver_inventory
        result = filter_cross_sheet_power_violations(violations, "/nonexistent/path")
        assert len(result) == 1

    @patch("kicad_tools.erc.cross_sheet.build_power_driver_inventory")
    def test_other_types_pass_through(self, mock_inventory):
        """Non-power violations pass through unchanged."""
        mock_inventory.return_value = {"+3V3"}

        violations = [
            {"type": "label_dangling", "description": "Label issue"},
            {"type": "pin_not_connected", "description": "Pin issue"},
            self._make_violation("power_pin_not_driven", "+3V3"),
        ]

        result = filter_cross_sheet_power_violations(violations, "/fake/path")
        assert len(result) == 2
        types = [v["type"] for v in result]
        assert "label_dangling" in types
        assert "pin_not_connected" in types
        assert "power_pin_not_driven" not in types
