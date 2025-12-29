"""Tests for kicad_tools.drc module."""

import pytest
from pathlib import Path
from datetime import datetime
from kicad_tools.drc import (
    DRCReport,
    DRCViolation,
    ViolationType,
    Severity,
    check_manufacturer_rules,
    CheckResult,
)


@pytest.fixture
def sample_drc_report(fixtures_dir: Path) -> Path:
    """Return the path to the sample DRC report."""
    return fixtures_dir / "sample_drc.rpt"


class TestDRCReportParsing:
    """Tests for DRC report parsing."""

    def test_load_text_report(self, sample_drc_report: Path):
        """Test loading a text format DRC report."""
        report = DRCReport.load(sample_drc_report)
        assert report is not None
        assert report.pcb_name == "test-board.kicad_pcb"

    def test_parse_violation_count(self, sample_drc_report: Path):
        """Test that all violations are parsed."""
        report = DRCReport.load(sample_drc_report)
        assert report.violation_count == 5

    def test_parse_footprint_errors(self, sample_drc_report: Path):
        """Test parsing footprint error count."""
        report = DRCReport.load(sample_drc_report)
        assert report.footprint_errors == 1

    def test_parse_created_date(self, sample_drc_report: Path):
        """Test parsing creation date."""
        report = DRCReport.load(sample_drc_report)
        assert report.created_at is not None
        assert report.created_at.year == 2025
        assert report.created_at.month == 1
        assert report.created_at.day == 15

    def test_error_count(self, sample_drc_report: Path):
        """Test counting error-level violations."""
        report = DRCReport.load(sample_drc_report)
        assert report.error_count == 4

    def test_warning_count(self, sample_drc_report: Path):
        """Test counting warning-level violations."""
        report = DRCReport.load(sample_drc_report)
        assert report.warning_count == 1


class TestDRCViolation:
    """Tests for DRC violation parsing."""

    def test_parse_clearance_violation(self, sample_drc_report: Path):
        """Test parsing clearance violations."""
        report = DRCReport.load(sample_drc_report)
        clearance_violations = report.by_type(ViolationType.CLEARANCE)
        assert len(clearance_violations) == 2

        # Check first clearance violation
        v = clearance_violations[0]
        assert v.type == ViolationType.CLEARANCE
        assert v.severity == Severity.ERROR
        assert v.required_value_mm == pytest.approx(0.2)
        assert v.actual_value_mm == pytest.approx(0.15)

    def test_parse_locations(self, sample_drc_report: Path):
        """Test parsing violation locations."""
        report = DRCReport.load(sample_drc_report)
        v = report.violations[0]

        assert len(v.locations) == 2
        assert v.locations[0].x_mm == pytest.approx(100.0)
        assert v.locations[0].y_mm == pytest.approx(50.0)
        assert v.locations[0].layer == "F.Cu"

    def test_parse_nets(self, sample_drc_report: Path):
        """Test extracting net names from violations."""
        report = DRCReport.load(sample_drc_report)
        v = report.violations[0]

        assert "VCC" in v.nets
        assert "GND" in v.nets

    def test_parse_shorting_items(self, sample_drc_report: Path):
        """Test parsing shorting items violation."""
        report = DRCReport.load(sample_drc_report)
        shorts = report.by_type(ViolationType.SHORTING_ITEMS)
        assert len(shorts) == 1
        assert shorts[0].is_connection

    def test_parse_unconnected_items(self, sample_drc_report: Path):
        """Test parsing unconnected items violation."""
        report = DRCReport.load(sample_drc_report)
        unconnected = report.by_type(ViolationType.UNCONNECTED_ITEMS)
        assert len(unconnected) == 1
        assert unconnected[0].is_connection


class TestDRCReportQueries:
    """Tests for DRC report query methods."""

    def test_by_type(self, sample_drc_report: Path):
        """Test filtering violations by type."""
        report = DRCReport.load(sample_drc_report)
        clearance = report.by_type(ViolationType.CLEARANCE)
        assert len(clearance) == 2

    def test_by_net(self, sample_drc_report: Path):
        """Test filtering violations by net."""
        report = DRCReport.load(sample_drc_report)
        vcc_violations = report.by_net("VCC")
        assert len(vcc_violations) >= 1

    def test_violations_by_type(self, sample_drc_report: Path):
        """Test grouping violations by type."""
        report = DRCReport.load(sample_drc_report)
        grouped = report.violations_by_type()

        assert ViolationType.CLEARANCE in grouped
        assert len(grouped[ViolationType.CLEARANCE]) == 2

    def test_violations_near(self, sample_drc_report: Path):
        """Test finding violations near a point."""
        report = DRCReport.load(sample_drc_report)
        nearby = report.violations_near(100.0, 50.0, radius_mm=2.0)
        assert len(nearby) >= 1

    def test_summary(self, sample_drc_report: Path):
        """Test generating report summary."""
        report = DRCReport.load(sample_drc_report)
        summary = report.summary()

        assert summary["total_violations"] == 5
        assert summary["errors"] == 4
        assert summary["warnings"] == 1
        assert "clearance" in summary["by_type"]


class TestViolationType:
    """Tests for violation type enum."""

    def test_from_string_clearance(self):
        """Test parsing clearance type."""
        assert ViolationType.from_string("clearance") == ViolationType.CLEARANCE

    def test_from_string_unconnected(self):
        """Test parsing unconnected items type."""
        assert ViolationType.from_string("unconnected_items") == ViolationType.UNCONNECTED_ITEMS

    def test_from_string_shorting(self):
        """Test parsing shorting items type."""
        assert ViolationType.from_string("shorting_items") == ViolationType.SHORTING_ITEMS

    def test_from_string_unknown(self):
        """Test parsing unknown type."""
        assert ViolationType.from_string("some_random_type") == ViolationType.UNKNOWN


class TestManufacturerChecks:
    """Tests for manufacturer rule checking."""

    def test_check_jlcpcb_rules(self, sample_drc_report: Path):
        """Test checking violations against JLCPCB rules."""
        report = DRCReport.load(sample_drc_report)
        checks = check_manufacturer_rules(report, "jlcpcb", layers=2)

        # Should have checks for clearance, connection issues, etc.
        assert len(checks) > 0

        # Connection issues should always fail
        connection_checks = [c for c in checks if c.rule_name == "connection"]
        for check in connection_checks:
            assert check.result == CheckResult.FAIL

    def test_check_unknown_manufacturer(self, sample_drc_report: Path):
        """Test checking with unknown manufacturer."""
        report = DRCReport.load(sample_drc_report)
        checks = check_manufacturer_rules(report, "unknown_mfr", layers=2)

        # Should return unknown results
        for check in checks:
            assert check.result == CheckResult.UNKNOWN

    def test_clearance_check_with_values(self, sample_drc_report: Path):
        """Test clearance check includes values."""
        report = DRCReport.load(sample_drc_report)
        checks = check_manufacturer_rules(report, "jlcpcb", layers=2)

        clearance_checks = [c for c in checks if c.rule_name == "min_clearance"]
        assert len(clearance_checks) > 0

        # At least one should have actual value
        checks_with_values = [c for c in clearance_checks if c.actual_value is not None]
        assert len(checks_with_values) > 0


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

    def test_from_string_default(self):
        """Test default severity."""
        assert Severity.from_string("unknown") == Severity.INFO


class TestDRCReportJSON:
    """Tests for JSON format DRC reports."""

    def test_parse_json_report(self):
        """Test parsing JSON format report."""
        from kicad_tools.drc import parse_json_report

        json_content = '''{
            "source": "test.kicad_pcb",
            "date": "2025-01-15T12:00:00",
            "violations": [
                {
                    "type": "clearance",
                    "severity": "error",
                    "description": "Clearance violation (required 0.2mm, actual 0.15mm)",
                    "pos": {"x": 100.0, "y": 50.0},
                    "items": [
                        {"description": "Pad 1 of R1", "net": "VCC"},
                        {"description": "Via", "net": "GND"}
                    ]
                }
            ],
            "footprint_errors": 0
        }'''

        report = parse_json_report(json_content, "test.json")
        assert report.violation_count == 1
        assert report.violations[0].type == ViolationType.CLEARANCE


class TestDRCViolationMethods:
    """Tests for DRCViolation methods."""

    def test_is_error(self, sample_drc_report: Path):
        """Test is_error property."""
        report = DRCReport.load(sample_drc_report)

        # First violation is error
        assert report.violations[0].is_error

        # Find the warning
        warnings = [v for v in report.violations if v.severity == Severity.WARNING]
        assert len(warnings) == 1
        assert not warnings[0].is_error

    def test_is_clearance(self, sample_drc_report: Path):
        """Test is_clearance property."""
        report = DRCReport.load(sample_drc_report)
        clearance = report.by_type(ViolationType.CLEARANCE)
        assert clearance[0].is_clearance

    def test_to_dict(self, sample_drc_report: Path):
        """Test converting violation to dict."""
        report = DRCReport.load(sample_drc_report)
        v = report.violations[0]
        d = v.to_dict()

        assert "type" in d
        assert "severity" in d
        assert "message" in d
        assert "locations" in d
        assert d["type"] == "clearance"


class TestViolationTypeFromString:
    """Additional tests for ViolationType.from_string."""

    def test_edge_clearance(self):
        """Test copper edge clearance detection."""
        assert ViolationType.from_string("copper_edge_clearance") == ViolationType.COPPER_EDGE_CLEARANCE
        assert ViolationType.from_string("edge clearance violation") == ViolationType.COPPER_EDGE_CLEARANCE

    def test_courtyard(self):
        """Test courtyard overlap detection."""
        assert ViolationType.from_string("courtyard_overlap") == ViolationType.COURTYARD_OVERLAP
        assert ViolationType.from_string("Courtyard overlapping") == ViolationType.COURTYARD_OVERLAP

    def test_track_width(self):
        """Test track width detection."""
        assert ViolationType.from_string("track_width") == ViolationType.TRACK_WIDTH
        assert ViolationType.from_string("Track width too small") == ViolationType.TRACK_WIDTH

    def test_via_types(self):
        """Test via-related violations."""
        assert ViolationType.from_string("via_annular_width") == ViolationType.VIA_ANNULAR_WIDTH
        assert ViolationType.from_string("Via annular ring too small") == ViolationType.VIA_ANNULAR_WIDTH
        assert ViolationType.from_string("Via hole larger than pad") == ViolationType.VIA_HOLE_LARGER_THAN_PAD
        assert ViolationType.from_string("Micro via hole") == ViolationType.MICRO_VIA_HOLE_TOO_SMALL

    def test_drill_hole(self):
        """Test drill hole detection."""
        assert ViolationType.from_string("drill_hole_too_small") == ViolationType.DRILL_HOLE_TOO_SMALL
        assert ViolationType.from_string("Drill size too small") == ViolationType.DRILL_HOLE_TOO_SMALL

    def test_silk_types(self):
        """Test silkscreen violations."""
        assert ViolationType.from_string("silk_over_copper") == ViolationType.SILK_OVER_COPPER
        assert ViolationType.from_string("Silk over copper pad") == ViolationType.SILK_OVER_COPPER
        assert ViolationType.from_string("silk_overlap") == ViolationType.SILK_OVERLAP
        assert ViolationType.from_string("Silkscreen overlap") == ViolationType.SILK_OVERLAP

    def test_solder_mask(self):
        """Test solder mask bridge detection."""
        assert ViolationType.from_string("solder_mask_bridge") == ViolationType.SOLDER_MASK_BRIDGE
        assert ViolationType.from_string("Solder mask bridge") == ViolationType.SOLDER_MASK_BRIDGE

    def test_footprint_types(self):
        """Test footprint-related violations."""
        assert ViolationType.from_string("footprint") == ViolationType.FOOTPRINT
        assert ViolationType.from_string("duplicate_footprint") == ViolationType.DUPLICATE_FOOTPRINT
        assert ViolationType.from_string("Duplicate footprint found") == ViolationType.DUPLICATE_FOOTPRINT
        assert ViolationType.from_string("extra_footprint") == ViolationType.EXTRA_FOOTPRINT
        assert ViolationType.from_string("Extra footprint on board") == ViolationType.EXTRA_FOOTPRINT
        assert ViolationType.from_string("missing_footprint") == ViolationType.MISSING_FOOTPRINT
        assert ViolationType.from_string("Missing footprint") == ViolationType.MISSING_FOOTPRINT

    def test_outline(self):
        """Test outline detection."""
        assert ViolationType.from_string("malformed_outline") == ViolationType.MALFORMED_OUTLINE
        assert ViolationType.from_string("Board outline malformed") == ViolationType.MALFORMED_OUTLINE


class TestLocationParsing:
    """Tests for Location.from_string parsing."""

    def test_parse_standard_format(self):
        """Test parsing standard @(x mm, y mm) format."""
        from kicad_tools.drc.violation import Location

        loc = Location.from_string("@(162.4500 mm, 100.3250 mm)")
        assert loc is not None
        assert loc.x_mm == pytest.approx(162.45)
        assert loc.y_mm == pytest.approx(100.325)

    def test_parse_with_spaces(self):
        """Test parsing with extra spaces."""
        from kicad_tools.drc.violation import Location

        loc = Location.from_string("@( 100.0 mm , 50.0 mm )")
        assert loc is not None
        assert loc.x_mm == pytest.approx(100.0)
        assert loc.y_mm == pytest.approx(50.0)

    def test_parse_json_format(self):
        """Test parsing JSON pos format."""
        from kicad_tools.drc.violation import Location

        loc = Location.from_string('{"x": 123.45, "y": 67.89}')
        assert loc is not None
        assert loc.x_mm == pytest.approx(123.45)
        assert loc.y_mm == pytest.approx(67.89)

    def test_parse_invalid(self):
        """Test parsing invalid format returns None."""
        from kicad_tools.drc.violation import Location

        loc = Location.from_string("invalid string")
        assert loc is None

    def test_location_str(self):
        """Test Location string representation."""
        from kicad_tools.drc.violation import Location

        loc = Location(x_mm=100.0, y_mm=50.0)
        assert "(100.00, 50.00) mm" in str(loc)

        loc_with_layer = Location(x_mm=100.0, y_mm=50.0, layer="F.Cu")
        assert "F.Cu" in str(loc_with_layer)


class TestDRCViolationProperties:
    """Tests for DRCViolation property methods."""

    def test_primary_location_exists(self):
        """Test primary_location when locations exist."""
        from kicad_tools.drc.violation import DRCViolation, Location, ViolationType, Severity

        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test",
            locations=[Location(100.0, 50.0)],
        )
        assert v.primary_location is not None
        assert v.primary_location.x_mm == 100.0

    def test_primary_location_empty(self):
        """Test primary_location when no locations."""
        from kicad_tools.drc.violation import DRCViolation, ViolationType, Severity

        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test",
        )
        assert v.primary_location is None

    def test_violation_str(self):
        """Test DRCViolation string representation."""
        from kicad_tools.drc.violation import DRCViolation, Location, ViolationType, Severity

        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test message",
            locations=[Location(100.0, 50.0)],
        )
        s = str(v)
        assert "clearance" in s
        assert "Test message" in s
