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


# ============================================================================
# DRC FIXER TESTS
# ============================================================================


class TestTraceInfo:
    """Tests for TraceInfo dataclass."""

    def test_creation(self):
        """Test creating TraceInfo."""
        from kicad_tools.drc.fixer import TraceInfo
        from kicad_tools.core.sexp import parse_sexp

        node = parse_sexp("(segment)")
        info = TraceInfo(
            start_x=10.0, start_y=20.0,
            end_x=30.0, end_y=20.0,
            width=0.25,
            layer="F.Cu",
            net=1,
            net_name="VCC",
            uuid="abc-123",
            node=node,
        )

        assert info.start_x == 10.0
        assert info.start_y == 20.0
        assert info.end_x == 30.0
        assert info.end_y == 20.0
        assert info.width == 0.25
        assert info.layer == "F.Cu"
        assert info.net == 1
        assert info.net_name == "VCC"


class TestViaInfo:
    """Tests for ViaInfo dataclass."""

    def test_creation(self):
        """Test creating ViaInfo."""
        from kicad_tools.drc.fixer import ViaInfo
        from kicad_tools.core.sexp import parse_sexp

        node = parse_sexp("(via)")
        info = ViaInfo(
            x=50.0, y=50.0,
            size=0.8,
            drill=0.4,
            net=2,
            uuid="via-uuid",
            node=node,
        )

        assert info.x == 50.0
        assert info.y == 50.0
        assert info.size == 0.8
        assert info.drill == 0.4
        assert info.net == 2


class TestDRCFixerInit:
    """Tests for DRCFixer initialization."""

    def test_init(self, tmp_path):
        """Test DRCFixer initialization."""
        from kicad_tools.drc.fixer import DRCFixer

        # Create minimal PCB file
        pcb_content = """(kicad_pcb
            (version 20231120)
            (net 0 "")
            (net 1 "VCC")
            (net 2 "GND")
        )"""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb_content)

        fixer = DRCFixer(str(pcb_path))

        assert fixer.path == pcb_path
        assert fixer.deleted_count == 0
        assert fixer.modified is False
        assert fixer.nets[1] == "VCC"
        assert fixer.nets[2] == "GND"
        assert fixer.net_names["VCC"] == 1
        assert fixer.net_names["GND"] == 2


class TestDRCFixerSegmentNear:
    """Tests for segment_near_point helper method."""

    @pytest.fixture
    def fixer(self, tmp_path):
        """Create a fixer with test PCB."""
        from kicad_tools.drc.fixer import DRCFixer

        pcb_content = """(kicad_pcb
            (version 20231120)
            (net 0 "")
            (net 1 "VCC")
            (segment (start 10 20) (end 30 20) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg1"))
            (segment (start 50 50) (end 50 70) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg2"))
        )"""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb_content)

        return DRCFixer(str(pcb_path))

    def test_segment_near_point_on_line(self, fixer):
        """Test point exactly on line."""
        result = fixer._segment_near_point(10, 20, 30, 20, 20, 20, 0.1)
        assert result is True

    def test_segment_near_point_close(self, fixer):
        """Test point close to line."""
        result = fixer._segment_near_point(10, 20, 30, 20, 20, 20.3, 0.5)
        assert result is True

    def test_segment_near_point_far(self, fixer):
        """Test point far from line."""
        result = fixer._segment_near_point(10, 20, 30, 20, 20, 25, 0.5)
        assert result is False

    def test_segment_near_point_at_endpoint(self, fixer):
        """Test point near endpoint."""
        result = fixer._segment_near_point(10, 20, 30, 20, 10, 20, 0.1)
        assert result is True

    def test_segment_near_point_zero_length(self, fixer):
        """Test zero-length segment (point)."""
        result = fixer._segment_near_point(10, 20, 10, 20, 10, 20, 0.1)
        assert result is True


class TestDRCFixerFindSegments:
    """Tests for find_segments_near method."""

    @pytest.fixture
    def fixer(self, tmp_path):
        """Create a fixer with multiple segments."""
        from kicad_tools.drc.fixer import DRCFixer

        pcb_content = """(kicad_pcb
            (version 20231120)
            (net 0 "")
            (net 1 "VCC")
            (net 2 "GND")
            (segment (start 10 20) (end 30 20) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg1"))
            (segment (start 10 22) (end 30 22) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg2"))
            (segment (start 100 100) (end 120 100) (width 0.25) (layer "B.Cu") (net 1) (uuid "seg3"))
        )"""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb_content)

        return DRCFixer(str(pcb_path))

    def test_find_segments_near_point(self, fixer):
        """Test finding segments near a point."""
        segments = fixer.find_segments_near(20, 21, radius=2.0)
        assert len(segments) == 2  # Both seg1 and seg2 are close

    def test_find_segments_near_by_layer(self, fixer):
        """Test filtering segments by layer."""
        segments = fixer.find_segments_near(20, 21, radius=2.0, layer="F.Cu")
        assert len(segments) == 2  # Only F.Cu segments

        segments = fixer.find_segments_near(110, 100, radius=2.0, layer="B.Cu")
        assert len(segments) == 1  # Only B.Cu segment

    def test_find_segments_near_by_net(self, fixer):
        """Test filtering segments by net name."""
        segments = fixer.find_segments_near(20, 21, radius=2.0, net_name="VCC")
        assert len(segments) == 1
        assert segments[0].net_name == "VCC"


class TestDRCFixerFindVias:
    """Tests for find_vias_near method."""

    @pytest.fixture
    def fixer(self, tmp_path):
        """Create a fixer with vias."""
        from kicad_tools.drc.fixer import DRCFixer

        pcb_content = """(kicad_pcb
            (version 20231120)
            (net 0 "")
            (net 1 "VCC")
            (net 2 "GND")
            (via (at 50 50) (size 0.8) (drill 0.4) (net 1) (uuid "via1"))
            (via (at 52 50) (size 0.8) (drill 0.4) (net 2) (uuid "via2"))
            (via (at 100 100) (size 0.6) (drill 0.3) (net 1) (uuid "via3"))
        )"""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb_content)

        return DRCFixer(str(pcb_path))

    def test_find_vias_near(self, fixer):
        """Test finding vias near a point."""
        vias = fixer.find_vias_near(51, 50, radius=3.0)
        assert len(vias) == 2  # Both via1 and via2 are close

    def test_find_vias_far(self, fixer):
        """Test no vias found when far away."""
        vias = fixer.find_vias_near(0, 0, radius=1.0)
        assert len(vias) == 0

    def test_find_vias_by_net(self, fixer):
        """Test filtering vias by net name."""
        vias = fixer.find_vias_near(51, 50, radius=3.0, net_name="VCC")
        assert len(vias) == 1
        assert vias[0].net == 1


class TestDRCFixerDelete:
    """Tests for delete operations."""

    @pytest.fixture
    def fixer(self, tmp_path):
        """Create a fixer with segments and vias."""
        from kicad_tools.drc.fixer import DRCFixer

        pcb_content = """(kicad_pcb
            (version 20231120)
            (net 0 "")
            (net 1 "VCC")
            (net 2 "GND")
            (segment (start 10 20) (end 30 20) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg1"))
            (segment (start 40 20) (end 60 20) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg2"))
            (via (at 50 50) (size 0.8) (drill 0.4) (net 1) (uuid "via1"))
        )"""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb_content)

        return DRCFixer(str(pcb_path))

    def test_delete_segment(self, fixer):
        """Test deleting a segment."""
        segments = fixer.find_segments_near(20, 20, radius=1.0)
        assert len(segments) == 1

        result = fixer.delete_segment(segments[0])
        assert result is True
        assert fixer.deleted_count == 1
        assert fixer.modified is True

        # Verify segment is gone
        segments_after = fixer.find_segments_near(20, 20, radius=1.0)
        assert len(segments_after) == 0

    def test_delete_via(self, fixer):
        """Test deleting a via."""
        vias = fixer.find_vias_near(50, 50, radius=1.0)
        assert len(vias) == 1

        result = fixer.delete_via(vias[0])
        assert result is True
        assert fixer.deleted_count == 1
        assert fixer.modified is True

        # Verify via is gone
        vias_after = fixer.find_vias_near(50, 50, radius=1.0)
        assert len(vias_after) == 0

    def test_delete_net_traces(self, fixer):
        """Test deleting all traces for a net."""
        # Initial count
        segments = fixer.find_segments_near(20, 20, radius=100.0, net_name="VCC")
        assert len(segments) == 2

        # Delete all VCC traces
        deleted = fixer.delete_net_traces("VCC")
        assert deleted == 3  # 2 segments + 1 via

        # Verify all VCC traces are gone
        segments_after = fixer.find_segments_near(20, 20, radius=100.0, net_name="VCC")
        assert len(segments_after) == 0

    def test_delete_net_traces_unknown_net(self, fixer):
        """Test deleting traces for unknown net."""
        deleted = fixer.delete_net_traces("UNKNOWN_NET")
        assert deleted == 0


class TestDRCFixerSummary:
    """Tests for summary method."""

    def test_summary_no_changes(self, tmp_path):
        """Test summary with no changes."""
        from kicad_tools.drc.fixer import DRCFixer

        pcb_content = """(kicad_pcb (version 20231120))"""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb_content)

        fixer = DRCFixer(str(pcb_path))
        summary = fixer.summary()

        assert "deleted 0 elements" in summary

    def test_summary_after_delete(self, tmp_path):
        """Test summary after deletions."""
        from kicad_tools.drc.fixer import DRCFixer

        pcb_content = """(kicad_pcb
            (version 20231120)
            (net 1 "VCC")
            (segment (start 10 20) (end 30 20) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg1"))
        )"""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb_content)

        fixer = DRCFixer(str(pcb_path))
        fixer.delete_net_traces("VCC")

        summary = fixer.summary()
        assert "deleted 1 elements" in summary


class TestDRCFixerSave:
    """Tests for save method."""

    def test_save_to_same_path(self, tmp_path):
        """Test saving to same path."""
        from kicad_tools.drc.fixer import DRCFixer

        pcb_content = """(kicad_pcb (version 20231120))"""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb_content)

        fixer = DRCFixer(str(pcb_path))
        fixer.save()

        # File should still exist
        assert pcb_path.exists()

    def test_save_to_different_path(self, tmp_path):
        """Test saving to different path."""
        from kicad_tools.drc.fixer import DRCFixer

        pcb_content = """(kicad_pcb (version 20231120))"""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb_content)

        fixer = DRCFixer(str(pcb_path))

        output_path = tmp_path / "output.kicad_pcb"
        fixer.save(str(output_path))

        assert output_path.exists()


class TestDRCFixerGetAffectedNets:
    """Tests for get_affected_nets and get_unconnected_nets."""

    @pytest.fixture
    def mock_report(self):
        """Create a mock DRC report."""
        from kicad_tools.drc.violation import DRCViolation, Location, ViolationType, Severity
        from kicad_tools.drc.report import DRCReport

        violations = [
            DRCViolation(
                type=ViolationType.SHORTING_ITEMS,
                type_str="shorting_items",
                severity=Severity.ERROR,
                message="Short between VCC and GND",
                locations=[Location(100.0, 50.0, "F.Cu")],
                nets={"VCC", "GND"},
            ),
            DRCViolation(
                type=ViolationType.UNCONNECTED_ITEMS,
                type_str="unconnected_items",
                severity=Severity.ERROR,
                message="Unconnected pin on NET1",
                locations=[Location(200.0, 100.0, "F.Cu")],
                nets={"NET1"},
            ),
            DRCViolation(
                type=ViolationType.CLEARANCE,
                type_str="clearance",
                severity=Severity.WARNING,
                message="Clearance issue",
                locations=[Location(150.0, 75.0, "F.Cu")],
                nets={"NET2", "NET3"},
            ),
        ]

        return DRCReport(
            source_file="test.kicad_pcb",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=violations,
        )

    def test_get_affected_nets(self, tmp_path, mock_report):
        """Test getting all affected nets."""
        from kicad_tools.drc.fixer import DRCFixer

        pcb_content = """(kicad_pcb (version 20231120))"""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb_content)

        fixer = DRCFixer(str(pcb_path))
        nets = fixer.get_affected_nets(mock_report)

        assert "VCC" in nets
        assert "GND" in nets
        assert "NET1" in nets
        assert "NET2" in nets
        assert "NET3" in nets

    def test_get_unconnected_nets(self, tmp_path, mock_report):
        """Test getting only unconnected nets."""
        from kicad_tools.drc.fixer import DRCFixer

        pcb_content = """(kicad_pcb (version 20231120))"""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb_content)

        fixer = DRCFixer(str(pcb_path))
        nets = fixer.get_unconnected_nets(mock_report)

        assert "NET1" in nets
        assert "VCC" not in nets
        assert "GND" not in nets


class TestCheckerSummarize:
    """Tests for summarize_checks function."""

    def test_summarize_checks(self):
        """Test summarizing manufacturer checks."""
        from kicad_tools.drc.checker import ManufacturerCheck, CheckResult, summarize_checks
        from kicad_tools.drc.violation import DRCViolation, ViolationType, Severity

        v1 = DRCViolation(type=ViolationType.CLEARANCE, type_str="clearance", severity=Severity.ERROR, message="Test")
        v2 = DRCViolation(type=ViolationType.TRACK_WIDTH, type_str="track_width", severity=Severity.ERROR, message="Test")

        checks = [
            ManufacturerCheck(violation=v1, result=CheckResult.PASS, message="OK", manufacturer_id="jlcpcb", rule_name="min_clearance"),
            ManufacturerCheck(violation=v1, result=CheckResult.FAIL, message="Bad", manufacturer_id="jlcpcb", rule_name="min_clearance"),
            ManufacturerCheck(violation=v2, result=CheckResult.WARNING, message="Warn", manufacturer_id="jlcpcb", rule_name="min_trace_width"),
        ]

        summary = summarize_checks(checks)

        assert summary["total"] == 3
        assert summary["pass"] == 1
        assert summary["fail"] == 1
        assert summary["warning"] == 1
        assert summary["compatible"] == 2
        assert "min_clearance" in summary["by_rule"]
        assert summary["by_rule"]["min_clearance"]["pass"] == 1
        assert summary["by_rule"]["min_clearance"]["fail"] == 1


class TestManufacturerCheckProperties:
    """Tests for ManufacturerCheck properties."""

    def test_is_compatible_pass(self):
        """Test is_compatible for PASS result."""
        from kicad_tools.drc.checker import ManufacturerCheck, CheckResult
        from kicad_tools.drc.violation import DRCViolation, ViolationType, Severity

        v = DRCViolation(type=ViolationType.CLEARANCE, type_str="clearance", severity=Severity.ERROR, message="Test")
        check = ManufacturerCheck(violation=v, result=CheckResult.PASS, message="OK", manufacturer_id="jlcpcb", rule_name="test")

        assert check.is_compatible is True

    def test_is_compatible_warning(self):
        """Test is_compatible for WARNING result."""
        from kicad_tools.drc.checker import ManufacturerCheck, CheckResult
        from kicad_tools.drc.violation import DRCViolation, ViolationType, Severity

        v = DRCViolation(type=ViolationType.CLEARANCE, type_str="clearance", severity=Severity.ERROR, message="Test")
        check = ManufacturerCheck(violation=v, result=CheckResult.WARNING, message="Warn", manufacturer_id="jlcpcb", rule_name="test")

        assert check.is_compatible is True

    def test_is_compatible_fail(self):
        """Test is_compatible for FAIL result."""
        from kicad_tools.drc.checker import ManufacturerCheck, CheckResult
        from kicad_tools.drc.violation import DRCViolation, ViolationType, Severity

        v = DRCViolation(type=ViolationType.CLEARANCE, type_str="clearance", severity=Severity.ERROR, message="Test")
        check = ManufacturerCheck(violation=v, result=CheckResult.FAIL, message="Bad", manufacturer_id="jlcpcb", rule_name="test")

        assert check.is_compatible is False

    def test_str_with_values(self):
        """Test string representation with values."""
        from kicad_tools.drc.checker import ManufacturerCheck, CheckResult
        from kicad_tools.drc.violation import DRCViolation, ViolationType, Severity

        v = DRCViolation(type=ViolationType.CLEARANCE, type_str="clearance", severity=Severity.ERROR, message="Test")
        check = ManufacturerCheck(
            violation=v,
            result=CheckResult.FAIL,
            message="Clearance issue",
            manufacturer_id="jlcpcb",
            rule_name="min_clearance",
            manufacturer_limit=0.1,
            actual_value=0.08,
        )

        s = str(check)
        assert "FAIL" in s
        assert "limit" in s
        assert "actual" in s

    def test_str_without_values(self):
        """Test string representation without values."""
        from kicad_tools.drc.checker import ManufacturerCheck, CheckResult
        from kicad_tools.drc.violation import DRCViolation, ViolationType, Severity

        v = DRCViolation(type=ViolationType.CLEARANCE, type_str="clearance", severity=Severity.ERROR, message="Test")
        check = ManufacturerCheck(
            violation=v,
            result=CheckResult.WARNING,
            message="Issue detected",
            manufacturer_id="jlcpcb",
            rule_name="test",
        )

        s = str(check)
        assert "WARNING" in s
        assert "Issue detected" in s
