"""Tests for kicad_tools.erc module."""

import pytest
from pathlib import Path
from kicad_tools.erc import (
    ERCReport,
    ERCViolation,
    ERCViolationType,
    Severity,
    ERC_TYPE_DESCRIPTIONS,
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
        assert ERCViolationType.from_string("pin_not_connected") == ERCViolationType.PIN_NOT_CONNECTED

    def test_from_string_power_pin(self):
        """Test parsing power_pin_not_driven type."""
        assert ERCViolationType.from_string("power_pin_not_driven") == ERCViolationType.POWER_PIN_NOT_DRIVEN

    def test_from_string_label(self):
        """Test parsing label types."""
        assert ERCViolationType.from_string("label_dangling") == ERCViolationType.LABEL_DANGLING
        assert ERCViolationType.from_string("global_label_dangling") == ERCViolationType.GLOBAL_LABEL_DANGLING

    def test_from_string_unknown(self):
        """Test parsing unknown type."""
        assert ERCViolationType.from_string("some_random_type") == ERCViolationType.UNKNOWN


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
        warnings = [v for v in report.violations if v.severity == Severity.WARNING and not v.excluded]
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
