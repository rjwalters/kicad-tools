"""Tests for net name propagation in clearance violation output.

Verifies that net names from PCB schema objects flow through the
validate-layer DRCViolation, the compat bridge, and into CLI output.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from kicad_tools.validate.rules.clearance import CopperElement
from kicad_tools.validate.violations import DRCViolation

# ---------------------------------------------------------------------------
# CopperElement carries net_name
# ---------------------------------------------------------------------------


class TestCopperElementNetName:
    """CopperElement factory methods propagate net_name from schema objects."""

    def test_from_segment_carries_net_name(self):
        seg = MagicMock()
        seg.layer = "F.Cu"
        seg.net_number = 1
        seg.net_name = "GND"
        seg.start = (10.0, 20.0)
        seg.end = (30.0, 20.0)
        seg.width = 0.25
        seg.uuid = "abcdef12"

        elem = CopperElement.from_segment(seg)
        assert elem.net_name == "GND"

    def test_from_segment_net0_gives_empty_string(self):
        seg = MagicMock()
        seg.layer = "F.Cu"
        seg.net_number = 0
        seg.net_name = ""
        seg.start = (10.0, 20.0)
        seg.end = (30.0, 20.0)
        seg.width = 0.25
        seg.uuid = "abcdef12"

        elem = CopperElement.from_segment(seg)
        assert elem.net_name == ""

    def test_from_via_carries_net_name(self):
        via = MagicMock()
        via.net_number = 2
        via.net_name = "+3V3"
        via.position = (50.0, 60.0)
        via.size = 0.6
        via.layers = ["F.Cu", "B.Cu"]
        via.uuid = "deadbeef"

        elem = CopperElement.from_via(via)
        assert elem.net_name == "+3V3"

    def test_from_via_net0_gives_empty_string(self):
        via = MagicMock()
        via.net_number = 0
        via.net_name = ""
        via.position = (50.0, 60.0)
        via.size = 0.6
        via.layers = ["F.Cu", "B.Cu"]
        via.uuid = "deadbeef"

        elem = CopperElement.from_via(via)
        assert elem.net_name == ""

    def test_from_pad_carries_net_name(self):
        pad = MagicMock()
        pad.net_number = 3
        pad.net_name = "SDA"
        pad.position = (0.0, 0.0)
        pad.size = (1.0, 1.0)
        pad.layers = ["F.Cu"]
        # Absolute pad angle (issue #3902); a bare MagicMock attribute would
        # break the cardinal-rotation comparisons in _transform_pad_dimensions.
        pad.rotation = 0.0

        footprint = MagicMock()
        footprint.reference = "U1"
        footprint.position = (100.0, 100.0)
        footprint.rotation = 0.0

        elem = CopperElement.from_pad(pad, footprint)
        assert elem.net_name == "SDA"

    def test_from_pad_net0_gives_empty_string(self):
        pad = MagicMock()
        pad.net_number = 0
        pad.net_name = ""
        pad.position = (0.0, 0.0)
        pad.size = (1.0, 1.0)
        pad.layers = ["F.Cu"]
        # Absolute pad angle (issue #3902); see test_from_pad_carries_net_name.
        pad.rotation = 0.0

        footprint = MagicMock()
        footprint.reference = "U1"
        footprint.position = (100.0, 100.0)
        footprint.rotation = 0.0

        elem = CopperElement.from_pad(pad, footprint)
        assert elem.net_name == ""


# ---------------------------------------------------------------------------
# Validate-layer DRCViolation carries nets
# ---------------------------------------------------------------------------


class TestValidateDRCViolationNets:
    """validate.violations.DRCViolation includes nets field."""

    def test_default_nets_is_empty_tuple(self):
        v = DRCViolation(
            rule_id="clearance_segment_segment",
            severity="error",
            message="test",
        )
        assert v.nets == ()

    def test_nets_field_populated(self):
        v = DRCViolation(
            rule_id="clearance_segment_segment",
            severity="error",
            message="test",
            nets=("GND", "+3V3"),
        )
        assert v.nets == ("GND", "+3V3")

    def test_to_dict_includes_nets(self):
        v = DRCViolation(
            rule_id="clearance_segment_segment",
            severity="error",
            message="test",
            nets=("GND", "+3V3"),
        )
        d = v.to_dict()
        assert "nets" in d
        assert d["nets"] == ["GND", "+3V3"]

    def test_to_dict_nets_empty_when_no_nets(self):
        v = DRCViolation(
            rule_id="clearance_segment_segment",
            severity="error",
            message="test",
        )
        d = v.to_dict()
        assert d["nets"] == []


# ---------------------------------------------------------------------------
# Compat bridge propagates nets
# ---------------------------------------------------------------------------


class TestCompatBridgeNets:
    """drc_results_to_report propagates nets from validate to report layer."""

    def test_nets_propagated(self):
        from kicad_tools.drc.compat import drc_results_to_report
        from kicad_tools.validate.violations import DRCResults

        results = DRCResults()
        results.add(
            DRCViolation(
                rule_id="clearance_segment_segment",
                severity="error",
                message="Segment to segment clearance 0.100mm < minimum 0.200mm",
                location=(10.0, 20.0),
                layer="F.Cu",
                actual_value=0.1,
                required_value=0.2,
                items=("Trace-abc", "Trace-def"),
                nets=("GND", "+3V3"),
            )
        )
        results.rules_checked = 1

        report = drc_results_to_report(results)
        assert len(report.violations) == 1
        assert report.violations[0].nets == ["GND", "+3V3"]

    def test_empty_nets_propagated(self):
        from kicad_tools.drc.compat import drc_results_to_report
        from kicad_tools.validate.violations import DRCResults

        results = DRCResults()
        results.add(
            DRCViolation(
                rule_id="clearance_segment_segment",
                severity="error",
                message="test",
            )
        )
        results.rules_checked = 1

        report = drc_results_to_report(results)
        assert report.violations[0].nets == []


# ---------------------------------------------------------------------------
# kct-check JSON round-trip preserves nets
# ---------------------------------------------------------------------------


class TestKctCheckJsonNets:
    """kct check JSON output includes nets and round-trips correctly."""

    def test_json_round_trip_with_nets(self):
        from kicad_tools.drc.report import _parse_kct_check_json

        kct_json = {
            "file": "/tmp/test.kicad_pcb",
            "manufacturer": "jlcpcb",
            "layers": 2,
            "summary": {
                "errors": 1,
                "warnings": 0,
                "rules_checked": 1,
                "passed": False,
            },
            "violations": [
                {
                    "rule_id": "clearance_segment_segment",
                    "severity": "error",
                    "message": "Segment to segment clearance 0.100mm < minimum 0.200mm",
                    "location": [10.0, 20.0],
                    "layer": "F.Cu",
                    "actual_value": 0.1,
                    "required_value": 0.2,
                    "items": ["Trace-abc", "Trace-def"],
                    "nets": ["GND", "+3V3"],
                },
            ],
        }

        report = _parse_kct_check_json(kct_json, "test.json")
        assert len(report.violations) == 1
        assert report.violations[0].nets == ["GND", "+3V3"]

    def test_json_round_trip_without_nets_key(self):
        """Backward compatibility: missing nets key defaults to empty list."""
        from kicad_tools.drc.report import _parse_kct_check_json

        kct_json = {
            "file": "/tmp/test.kicad_pcb",
            "manufacturer": "jlcpcb",
            "layers": 2,
            "summary": {
                "errors": 1,
                "warnings": 0,
                "rules_checked": 1,
                "passed": False,
            },
            "violations": [
                {
                    "rule_id": "clearance_segment_segment",
                    "severity": "error",
                    "message": "test",
                    "location": [10.0, 20.0],
                    "layer": "F.Cu",
                    "items": [],
                },
            ],
        }

        report = _parse_kct_check_json(kct_json, "test.json")
        assert report.violations[0].nets == []


# ---------------------------------------------------------------------------
# Integration: ClearanceRule produces violations with net names
# ---------------------------------------------------------------------------


class TestClearanceRuleNetNames:
    """ClearanceRule populates nets in violations from a real PCB parse."""

    PCB_CONTENT = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3V3")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1 "GND") (uuid "seg-gnd1"))
  (segment (start 100 100.15) (end 110 100.15) (width 0.25) (layer "F.Cu") (net 2 "+3V3") (uuid "seg-3v3"))
)
"""

    def test_clearance_violation_has_net_names(self, tmp_path: Path):
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(self.PCB_CONTENT)
        pcb = PCB.load(pcb_path)

        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, copper_oz=1.0)
        results = checker.check_clearances()

        # Should find at least one clearance violation
        assert len(results.violations) > 0

        v = results.violations[0]
        # Nets should contain both net names (order may vary due to sorted types)
        assert set(v.nets) == {"GND", "+3V3"}

    PCB_WITH_VIA = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3V3")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1 "GND") (uuid "seg-gnd1"))
  (via (at 100 100.25) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2 "+3V3") (uuid "via-3v3"))
)
"""

    def test_segment_via_violation_has_net_names(self, tmp_path: Path):
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(self.PCB_WITH_VIA)
        pcb = PCB.load(pcb_path)

        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, copper_oz=1.0)
        results = checker.check_clearances()

        assert len(results.violations) > 0
        v = results.violations[0]
        assert set(v.nets) == {"GND", "+3V3"}


# ---------------------------------------------------------------------------
# Reverse net name resolution: (net N) without inline name
# ---------------------------------------------------------------------------


class TestReverseNetNameResolution:
    """Net names resolved from net_number using header declarations."""

    PCB_NUMBER_ONLY = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "DAC_CLK")
  (net 2 "GNDD")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-clk"))
  (segment (start 100 100.15) (end 110 100.15) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-gnd"))
)
"""

    def test_segments_get_net_names_from_header(self, tmp_path: Path):
        """Traditional (net N) format without inline name resolves from header."""
        from kicad_tools.schema.pcb import PCB

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(self.PCB_NUMBER_ONLY)
        pcb = PCB.load(pcb_path)

        # After fixup, segments should have net_name resolved
        assert pcb.segments[0].net_name == "DAC_CLK"
        assert pcb.segments[1].net_name == "GNDD"

    def test_clearance_violation_has_resolved_net_names(self, tmp_path: Path):
        """Clearance violations include net names resolved from number-only format."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(self.PCB_NUMBER_ONLY)
        pcb = PCB.load(pcb_path)

        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, copper_oz=1.0)
        results = checker.check_clearances()

        assert len(results.violations) > 0
        v = results.violations[0]
        assert set(v.nets) == {"DAC_CLK", "GNDD"}

    PCB_VIA_NUMBER_ONLY = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "DAC_CLK")
  (net 2 "GNDD")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-clk"))
  (via (at 100 100.25) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-gnd"))
)
"""

    def test_via_gets_net_name_from_header(self, tmp_path: Path):
        """Via with (net N) format resolves net_name from header."""
        from kicad_tools.schema.pcb import PCB

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(self.PCB_VIA_NUMBER_ONLY)
        pcb = PCB.load(pcb_path)

        assert pcb.vias[0].net_name == "GNDD"

    def test_net_zero_stays_empty(self, tmp_path: Path):
        """Net 0 (unconnected) keeps empty net_name."""
        from kicad_tools.schema.pcb import PCB

        pcb_content = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 0) (uuid "seg-nc"))
)
"""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb_content)
        pcb = PCB.load(pcb_path)

        # Net 0 should remain with empty name
        assert pcb.segments[0].net_number == 0
        assert pcb.segments[0].net_name == ""


# ---------------------------------------------------------------------------
# Drill clearance violations include nets
# ---------------------------------------------------------------------------


class TestDrillClearanceNets:
    """Drill clearance violations include net names."""

    PCB_DRILL_CLOSE = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1 "VCC") (uuid "via-vcc"))
  (via (at 100.3 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2 "GND") (uuid "via-gnd"))
)
"""

    def test_drill_clearance_violation_has_nets(self, tmp_path: Path):
        """Drill clearance violations include both net names."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(self.PCB_DRILL_CLOSE)
        pcb = PCB.load(pcb_path)

        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, copper_oz=1.0)
        results = checker.check_dimensions()

        drill_violations = [
            v for v in results.violations if v.rule_id == "dimension_drill_clearance"
        ]
        assert len(drill_violations) > 0

        v = drill_violations[0]
        assert set(v.nets) == {"VCC", "GND"}
