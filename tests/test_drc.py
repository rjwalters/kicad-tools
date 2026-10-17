"""Tests for kicad_tools.drc module."""

from pathlib import Path

import pytest

from kicad_tools.drc import (
    CheckResult,
    DRCReport,
    Severity,
    ViolationType,
    check_manufacturer_rules,
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


class TestExtractValuesHoleToHoleWording:
    """Issue #5534: kicad-cli 10's hole-to-hole wording must populate values."""

    def test_min_actual_wording_is_parsed(self):
        """kicad-cli 10 phrases hole-to-hole violations as 'min X mm; actual Y mm'."""
        from kicad_tools.drc.report import _extract_values

        data: dict = {}
        message = (
            "Drilled holes too close together (netclass 'Default', min 0.4995 mm; actual 0.4500 mm)"
        )
        _extract_values(data, message)

        assert data["required_value_mm"] == pytest.approx(0.4995)
        assert data["actual_value_mm"] == pytest.approx(0.45)

    def test_clearance_wording_still_takes_precedence(self):
        """The new fallback pattern must not shadow the existing 'clearance' pattern."""
        from kicad_tools.drc.report import _extract_values

        data: dict = {}
        message = "Clearance violation (clearance 0.2000 mm; actual 0.1500 mm)"
        _extract_values(data, message)

        assert data["required_value_mm"] == pytest.approx(0.2)
        assert data["actual_value_mm"] == pytest.approx(0.15)

    def test_minimum_wording_still_parsed(self):
        """The pre-existing 'minimum' (drill clearance) wording keeps working."""
        from kicad_tools.drc.report import _extract_values

        data: dict = {}
        message = "Drill out of range (minimum 0.3000 mm; actual 0.2000 mm)"
        _extract_values(data, message)

        assert data["required_value_mm"] == pytest.approx(0.3)
        assert data["actual_value_mm"] == pytest.approx(0.2)

    def test_width_wording_still_parsed(self):
        """The pre-existing 'width' wording keeps working."""
        from kicad_tools.drc.report import _extract_values

        data: dict = {}
        message = "Track width too small (width 0.1500 mm; actual 0.1000 mm)"
        _extract_values(data, message)

        assert data["required_value_mm"] == pytest.approx(0.15)
        assert data["actual_value_mm"] == pytest.approx(0.1)


class TestUnconnectedItemsReportingBoundary:
    """Issue #4498: connectivity items must not become geometric violations.

    KiCad's JSON report carries ratsnest relationships in a top-level
    ``unconnected_items`` array.  They are parsed into the dedicated
    :attr:`DRCReport.unconnected_items` collection so that every consumer
    of ``violations`` / ``error_count`` (``run_geometric_drc``, the route
    gate, the "board is geometrically clean" regressions) keeps asking
    the same geometric question it asked before.
    """

    @staticmethod
    def _json_report(tmp_path: Path, *, violations: list, unconnected: list) -> DRCReport:
        import json

        path = tmp_path / "report.json"
        path.write_text(
            json.dumps(
                {
                    "$schema": "https://schemas.kicad.org/drc.v1.json",
                    "source": "board.kicad_pcb",
                    "violations": violations,
                    "unconnected_items": unconnected,
                    "schematic_parity": [],
                }
            )
        )
        return DRCReport.load(path)

    @staticmethod
    def _unconnected(index: int) -> dict:
        return {
            "type": "unconnected_items",
            "severity": "error",
            "description": "Missing connection between items",
            "items": [
                {"description": "Zone [GND] on F.Cu", "pos": {"x": float(index), "y": 1.0}},
                {"description": f"Pad {index} [GND] of U1 on B.Cu", "pos": {"x": 2.0, "y": 2.0}},
            ],
        }

    @staticmethod
    def _clearance() -> dict:
        return {
            "type": "clearance",
            "severity": "error",
            "description": "Clearance violation (clearance 0.2000 mm; actual 0.1000 mm)",
            "items": [{"description": "Track [VCC] on F.Cu", "pos": {"x": 5.0, "y": 5.0}}],
        }

    def test_unconnected_items_are_not_geometric_violations(self, tmp_path: Path):
        """A JSON report of pure connectivity items is geometrically clean."""
        report = self._json_report(
            tmp_path, violations=[], unconnected=[self._unconnected(i) for i in range(12)]
        )

        # Geometric contract: unchanged, still zero.
        assert report.violations == []
        assert report.violation_count == 0
        assert report.error_count == 0
        assert report.by_type(ViolationType.UNCONNECTED_ITEMS) == []

        # Connectivity contract: the 12 relationships are available.
        assert report.unconnected_item_count == 12
        assert len(report.connectivity_items()) == 12
        assert all(v.type == ViolationType.UNCONNECTED_ITEMS for v in report.unconnected_items)
        assert all(v.is_connection for v in report.unconnected_items)
        assert report.unconnected_items[0].nets == ["GND"]

    def test_geometric_violations_still_counted_alongside_connectivity(self, tmp_path: Path):
        """Geometry keeps counting geometry; connectivity is not added in."""
        report = self._json_report(
            tmp_path,
            violations=[self._clearance()],
            unconnected=[self._unconnected(i) for i in range(12)],
        )

        assert report.violation_count == 1
        assert report.error_count == 1
        assert report.violations[0].type == ViolationType.CLEARANCE
        assert report.unconnected_item_count == 12

    def test_text_report_connectivity_shape_is_unchanged(self, sample_drc_report: Path):
        """The flat text ``.rpt`` format keeps its legacy single-list shape."""
        report = DRCReport.load(sample_drc_report)

        # Text reports have no sibling array -- the entries stay in
        # ``violations`` exactly as before #4498 ...
        assert report.unconnected_items == []
        assert len(report.by_type(ViolationType.UNCONNECTED_ITEMS)) == 1
        # ... and ``connectivity_items()`` still finds them (no double count).
        assert len(report.connectivity_items()) == 1

    def test_apply_filters_preserves_connectivity_items(self, tmp_path: Path):
        """Geometric filtering never silently drops connectivity items."""
        from kicad_tools.validate.filters import ViolationFilter

        report = self._json_report(
            tmp_path,
            violations=[self._clearance()],
            unconnected=[self._unconnected(i) for i in range(3)],
        )
        filtered = report.apply_filters(
            [ViolationFilter(type_pattern="clearance", action="ignore")]
        )

        assert filtered.violation_count == 0
        assert filtered.unconnected_item_count == 3


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

        json_content = """{
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
        }"""

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
        assert (
            ViolationType.from_string("copper_edge_clearance")
            == ViolationType.COPPER_EDGE_CLEARANCE
        )
        assert (
            ViolationType.from_string("edge clearance violation")
            == ViolationType.COPPER_EDGE_CLEARANCE
        )

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
        assert (
            ViolationType.from_string("Via annular ring too small")
            == ViolationType.VIA_ANNULAR_WIDTH
        )
        assert (
            ViolationType.from_string("Via hole larger than pad")
            == ViolationType.VIA_HOLE_LARGER_THAN_PAD
        )
        assert ViolationType.from_string("Micro via hole") == ViolationType.MICRO_VIA_HOLE_TOO_SMALL

    def test_drill_hole(self):
        """Test drill hole detection."""
        assert (
            ViolationType.from_string("drill_hole_too_small") == ViolationType.DRILL_HOLE_TOO_SMALL
        )
        assert (
            ViolationType.from_string("Drill size too small") == ViolationType.DRILL_HOLE_TOO_SMALL
        )

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
        assert (
            ViolationType.from_string("Duplicate footprint found")
            == ViolationType.DUPLICATE_FOOTPRINT
        )
        assert ViolationType.from_string("extra_footprint") == ViolationType.EXTRA_FOOTPRINT
        assert (
            ViolationType.from_string("Extra footprint on board") == ViolationType.EXTRA_FOOTPRINT
        )
        assert ViolationType.from_string("missing_footprint") == ViolationType.MISSING_FOOTPRINT
        assert ViolationType.from_string("Missing footprint") == ViolationType.MISSING_FOOTPRINT

    def test_outline(self):
        """Test outline detection."""
        assert ViolationType.from_string("malformed_outline") == ViolationType.MALFORMED_OUTLINE
        assert (
            ViolationType.from_string("Board outline malformed") == ViolationType.MALFORMED_OUTLINE
        )


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
        from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType

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
        from kicad_tools.drc.violation import DRCViolation, Severity, ViolationType

        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test",
        )
        assert v.primary_location is None

    def test_violation_str(self):
        """Test DRCViolation string representation."""
        from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType

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


class TestCheckerSummarize:
    """Tests for summarize_checks function."""

    def test_summarize_checks(self):
        """Test summarizing manufacturer checks."""
        from kicad_tools.drc.checker import CheckResult, ManufacturerCheck, summarize_checks
        from kicad_tools.drc.violation import DRCViolation, Severity, ViolationType

        v1 = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test",
        )
        v2 = DRCViolation(
            type=ViolationType.TRACK_WIDTH,
            type_str="track_width",
            severity=Severity.ERROR,
            message="Test",
        )

        checks = [
            ManufacturerCheck(
                violation=v1,
                result=CheckResult.PASS,
                message="OK",
                manufacturer_id="jlcpcb",
                rule_name="min_clearance",
            ),
            ManufacturerCheck(
                violation=v1,
                result=CheckResult.FAIL,
                message="Bad",
                manufacturer_id="jlcpcb",
                rule_name="min_clearance",
            ),
            ManufacturerCheck(
                violation=v2,
                result=CheckResult.WARNING,
                message="Warn",
                manufacturer_id="jlcpcb",
                rule_name="min_trace_width",
            ),
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
        from kicad_tools.drc.checker import CheckResult, ManufacturerCheck
        from kicad_tools.drc.violation import DRCViolation, Severity, ViolationType

        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test",
        )
        check = ManufacturerCheck(
            violation=v,
            result=CheckResult.PASS,
            message="OK",
            manufacturer_id="jlcpcb",
            rule_name="test",
        )

        assert check.is_compatible is True

    def test_is_compatible_warning(self):
        """Test is_compatible for WARNING result."""
        from kicad_tools.drc.checker import CheckResult, ManufacturerCheck
        from kicad_tools.drc.violation import DRCViolation, Severity, ViolationType

        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test",
        )
        check = ManufacturerCheck(
            violation=v,
            result=CheckResult.WARNING,
            message="Warn",
            manufacturer_id="jlcpcb",
            rule_name="test",
        )

        assert check.is_compatible is True

    def test_is_compatible_fail(self):
        """Test is_compatible for FAIL result."""
        from kicad_tools.drc.checker import CheckResult, ManufacturerCheck
        from kicad_tools.drc.violation import DRCViolation, Severity, ViolationType

        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test",
        )
        check = ManufacturerCheck(
            violation=v,
            result=CheckResult.FAIL,
            message="Bad",
            manufacturer_id="jlcpcb",
            rule_name="test",
        )

        assert check.is_compatible is False

    def test_str_with_values(self):
        """Test string representation with values."""
        from kicad_tools.drc.checker import CheckResult, ManufacturerCheck
        from kicad_tools.drc.violation import DRCViolation, Severity, ViolationType

        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test",
        )
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
        from kicad_tools.drc.checker import CheckResult, ManufacturerCheck
        from kicad_tools.drc.violation import DRCViolation, Severity, ViolationType

        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Test",
        )
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
