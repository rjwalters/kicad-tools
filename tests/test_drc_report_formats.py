"""Tests for DRC report JSON format parsing (KiCad-cli and kct-check formats)."""

import json

from kicad_tools.drc.report import parse_json_report
from kicad_tools.drc.violation import Severity, ViolationType


class TestKctCheckJsonFormat:
    """Tests for parsing kct-check JSON format written by ``kct check --output``."""

    def test_parse_empty_violations(self):
        """Parse kct-check report with no violations (DRC passed)."""
        data = {
            "file": "/path/to/board.kicad_pcb",
            "manufacturer": "jlcpcb",
            "layers": 2,
            "summary": {
                "errors": 0,
                "warnings": 0,
                "rules_checked": 4,
                "passed": True,
            },
            "violations": [],
        }
        report = parse_json_report(json.dumps(data), source_file="test.json")
        assert report.pcb_name == "/path/to/board.kicad_pcb"
        assert report.violation_count == 0
        assert report.error_count == 0
        assert report.warning_count == 0

    def test_parse_with_violations(self):
        """Parse kct-check report with both errors and warnings."""
        data = {
            "file": "/path/to/board.kicad_pcb",
            "manufacturer": "jlcpcb",
            "layers": 2,
            "summary": {
                "errors": 1,
                "warnings": 1,
                "rules_checked": 4,
                "passed": False,
            },
            "violations": [
                {
                    "rule_id": "clearance_pad_pad",
                    "severity": "warning",
                    "message": "Pad-to-pad clearance 0.15 mm below minimum 0.20 mm",
                    "location": [100.5, 200.3],
                    "layer": "F.Cu",
                    "actual_value": 0.15,
                    "required_value": 0.20,
                    "items": ["Pad 1 of U1", "Pad 2 of C3"],
                },
                {
                    "rule_id": "track_width",
                    "severity": "error",
                    "message": "Track width too narrow",
                    "location": [50.0, 75.0],
                    "layer": "B.Cu",
                    "actual_value": 0.1,
                    "required_value": 0.15,
                    "items": ["Track on B.Cu"],
                },
            ],
        }
        report = parse_json_report(json.dumps(data), source_file="test.json")
        assert report.violation_count == 2
        assert report.error_count == 1
        assert report.warning_count == 1

        # Check warning violation
        warning = report.warnings[0]
        assert warning.type_str == "clearance_pad_pad"
        assert warning.severity == Severity.WARNING
        assert warning.message == "Pad-to-pad clearance 0.15 mm below minimum 0.20 mm"
        assert len(warning.locations) == 1
        assert warning.locations[0].x_mm == 100.5
        assert warning.locations[0].y_mm == 200.3
        assert warning.locations[0].layer == "F.Cu"
        assert warning.items == ["Pad 1 of U1", "Pad 2 of C3"]
        assert warning.actual_value_mm == 0.15
        assert warning.required_value_mm == 0.20

        # Check error violation
        error = report.errors[0]
        assert error.type_str == "track_width"
        assert error.severity == Severity.ERROR
        assert error.type == ViolationType.TRACK_WIDTH

    def test_parse_no_location(self):
        """Parse kct-check violation without location field."""
        data = {
            "file": "board.kicad_pcb",
            "summary": {"errors": 1, "warnings": 0, "rules_checked": 1, "passed": False},
            "violations": [
                {
                    "rule_id": "unconnected_items",
                    "severity": "error",
                    "message": "Unconnected net",
                    "items": ["Net VCC"],
                },
            ],
        }
        report = parse_json_report(json.dumps(data))
        assert report.violation_count == 1
        v = report.violations[0]
        assert len(v.locations) == 0
        assert v.items == ["Net VCC"]

    def test_parse_empty_items(self):
        """Parse kct-check violation with empty items list."""
        data = {
            "file": "board.kicad_pcb",
            "manufacturer": "jlcpcb",
            "summary": {"errors": 1, "warnings": 0, "rules_checked": 1, "passed": False},
            "violations": [
                {
                    "rule_id": "unknown_rule",
                    "severity": "error",
                    "message": "Some error",
                    "items": [],
                },
            ],
        }
        report = parse_json_report(json.dumps(data))
        assert report.violation_count == 1
        assert report.violations[0].items == []

    def test_format_detection_with_manufacturer_only(self):
        """Detect kct-check format when only 'manufacturer' key is present."""
        data = {
            "file": "board.kicad_pcb",
            "manufacturer": "pcbway",
            "layers": 4,
            "violations": [],
        }
        report = parse_json_report(json.dumps(data))
        assert report.pcb_name == "board.kicad_pcb"
        assert report.violation_count == 0

    def test_format_detection_with_summary_only(self):
        """Detect kct-check format when only 'summary' key is present."""
        data = {
            "file": "board.kicad_pcb",
            "summary": {"errors": 0, "warnings": 0, "rules_checked": 2, "passed": True},
            "violations": [],
        }
        report = parse_json_report(json.dumps(data))
        assert report.pcb_name == "board.kicad_pcb"
        assert report.violation_count == 0


class TestKicadCliJsonFormat:
    """Tests for parsing KiCad-cli JSON format (regression tests)."""

    def test_parse_empty_violations(self):
        """Parse KiCad-cli report with no violations."""
        data = {
            "source": "board.kicad_pcb",
            "date": "2025-12-28T21:29:34",
            "violations": [],
        }
        report = parse_json_report(json.dumps(data))
        assert report.pcb_name == "board.kicad_pcb"
        assert report.violation_count == 0
        assert report.created_at is not None
        assert report.created_at.year == 2025

    def test_parse_with_violations(self):
        """Parse KiCad-cli report with dict-style items."""
        data = {
            "source": "board.kicad_pcb",
            "date": "2025-12-28T21:29:34",
            "violations": [
                {
                    "type": "clearance",
                    "description": "Clearance violation (0.20 mm required, actual 0.15 mm)",
                    "severity": "error",
                    "pos": {"x": 162.45, "y": 100.32},
                    "items": [
                        {
                            "description": "Pad 6 [VCC] of U3 on F.Cu",
                            "pos": {"x": 162.45, "y": 100.32},
                            "net": "VCC",
                        },
                        {
                            "description": "Via [SPI_NSS] on F.Cu - B.Cu",
                            "pos": {"x": 161.6, "y": 100.9},
                            "net": "SPI_NSS",
                        },
                    ],
                },
            ],
        }
        report = parse_json_report(json.dumps(data))
        assert report.violation_count == 1
        v = report.violations[0]
        assert v.type_str == "clearance"
        assert v.severity == Severity.ERROR
        assert len(v.items) == 2
        assert "Pad 6 [VCC] of U3 on F.Cu" in v.items
        assert len(v.nets) == 2
        assert "VCC" in v.nets
        assert "SPI_NSS" in v.nets
        # 3 locations: one from violation pos + two from items
        assert len(v.locations) == 3

    def test_parse_with_no_items_key(self):
        """Parse KiCad-cli violation without items."""
        data = {
            "source": "board.kicad_pcb",
            "date": "2025-01-01T00:00:00",
            "violations": [
                {
                    "type": "footprint",
                    "description": "Footprint error",
                    "severity": "warning",
                },
            ],
        }
        report = parse_json_report(json.dumps(data))
        assert report.violation_count == 1
        assert report.violations[0].items == []
