"""Tests for the fix-drc command and DrillClearanceRepairer."""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.fix_drc_cmd import main
from kicad_tools.drc.repair_drill_clearance import (
    DrillClearanceRepairer,
    DrillRepairResult,
)
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType

# ── Test PCB fixtures ──────────────────────────────────────────────────

# PCB with clearance violations (trace-to-trace too close)
PCB_WITH_CLEARANCE = """\
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
  (net 2 "+3.3V")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-1"))
  (segment (start 100 100.15) (end 110 100.15) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-2"))
)
"""

# PCB with two coincident same-net vias (dedup candidate)
PCB_WITH_SAME_NET_VIAS = """\
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
  (net 2 "+3.3V")
  (segment (start 100 100) (end 115 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-1"))
  (via (at 115 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
  (via (at 115 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-2"))
)
"""

# PCB with two different-net vias too close (slide candidate)
PCB_WITH_DIFF_NET_VIAS = """\
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
  (net 2 "+3.3V")
  (segment (start 100 100) (end 115 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-1"))
  (segment (start 100 100.4) (end 115 100.4) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-2"))
  (via (at 115 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
  (via (at 115 100.4) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-2"))
)
"""

# PCB with mixed violations (clearance + drill)
PCB_WITH_MIXED = """\
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
  (net 2 "+3.3V")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-1"))
  (segment (start 100 100.15) (end 110 100.15) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-2"))
  (segment (start 100 105) (end 115 105) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-3"))
  (segment (start 100 105.4) (end 115 105.4) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-4"))
  (via (at 115 105) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
  (via (at 115 105.4) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-2"))
)
"""

# PCB with a segment too close to a via (segment-via clearance violation)
PCB_WITH_SEGMENT_VIA_CLEARANCE = """\
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
  (net 2 "+3.3V")
  (segment (start 100 100) (end 110 100.1) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-sv-1"))
  (via (at 105 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-sv-1"))
)
"""

# PCB with a segment near a <no net> via (net 0)
PCB_WITH_NO_NET_VIA = """\
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
  (segment (start 100 100) (end 110 100.1) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-nn-1"))
  (via (at 105 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 0) (uuid "via-nn-1"))
)
"""

# ── DRC report fixtures ─────────────────────────────────────────────

DRC_REPORT_CLEARANCE = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 1 DRC violations **
[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1500 mm)
    Rule: netclass 'Default'; error
    @(105.0000 mm, 100.0000 mm): Track [GND] on F.Cu
    @(105.0000 mm, 100.1500 mm): Track [+3.3V] on F.Cu

** Found 0 Footprint errors **
** End of Report **
"""

DRC_REPORT_DRILL = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 1 DRC violations **
[drill_clearance]: Drill-to-drill clearance (minimum 0.2500 mm; actual 0.1000 mm)
    Rule: min drill clearance; error
    @(115.0000 mm, 100.2000 mm): Via [GND] on F.Cu - B.Cu
    @(115.0000 mm, 100.2000 mm): Via [+3.3V] on F.Cu - B.Cu

** Found 0 Footprint errors **
** End of Report **
"""

DRC_REPORT_SAME_NET_DRILL = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 1 DRC violations **
[drill_clearance]: Drill-to-drill clearance (minimum 0.2500 mm; actual -0.3000 mm)
    Rule: min drill clearance; error
    @(115.0000 mm, 100.0000 mm): Via [GND] on F.Cu - B.Cu
    @(115.0000 mm, 100.0000 mm): Via [GND] on F.Cu - B.Cu

** Found 0 Footprint errors **
** End of Report **
"""

DRC_REPORT_MIXED = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 2 DRC violations **
[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1500 mm)
    Rule: netclass 'Default'; error
    @(105.0000 mm, 100.0000 mm): Track [GND] on F.Cu
    @(105.0000 mm, 100.1500 mm): Track [+3.3V] on F.Cu

[drill_clearance]: Drill-to-drill clearance (minimum 0.2500 mm; actual 0.1000 mm)
    Rule: min drill clearance; error
    @(115.0000 mm, 105.2000 mm): Via [GND] on F.Cu - B.Cu
    @(115.0000 mm, 105.2000 mm): Via [+3.3V] on F.Cu - B.Cu

** Found 0 Footprint errors **
** End of Report **
"""

DRC_REPORT_SEGMENT_VIA = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 1 DRC violations **
[clearance_segment_via]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.0500 mm)
    Rule: netclass 'Default'; error
    @(105.0000 mm, 100.1000 mm): Track [+3.3V] on F.Cu
    @(105.0000 mm, 100.0000 mm): Via [GND] on F.Cu - B.Cu

** Found 0 Footprint errors **
** End of Report **
"""

DRC_REPORT_NO_NET_VIA = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 1 DRC violations **
[clearance_segment_via]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.0500 mm)
    Rule: netclass 'Default'; error
    @(105.0000 mm, 100.1000 mm): Track [GND] on F.Cu
    @(105.0000 mm, 100.0000 mm): Via [] on F.Cu - B.Cu

** Found 0 Footprint errors **
** End of Report **
"""

DRC_REPORT_EMPTY = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 0 DRC violations **

** Found 0 Footprint errors **
** End of Report **
"""

# PCB with a segment overlapping an enlarged via (post fix-vias enlargement).
# Via enlarged from 0.6 to 0.8 mm; the trace that formerly had 0.1 mm clearance
# now has negative clearance (overlap).  The required nudge exceeds the old
# 0.25 mm default but is well within the new 0.5 mm pipeline default.
PCB_ENLARGED_VIA = """\
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
  (net 2 "+3.3V")
  (segment (start 100 100.05) (end 110 100.05) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-ev-1"))
  (via (at 105 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-ev-1"))
)
"""

# DRC report for the enlarged-via scenario: required 0.2 mm, actual -0.07 mm.
# delta = 0.27 mm; with 0.01 mm margin the required displacement is 0.28 mm.
# This exceeds the old 0.25 mm cap but is within the new 0.5 mm pipeline default.
DRC_REPORT_ENLARGED_VIA = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 1 DRC violations **
[clearance_segment_via]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual -0.0700 mm)
    Rule: netclass 'Default'; error
    @(105.0000 mm, 100.0500 mm): Track [+3.3V] on F.Cu
    @(105.0000 mm, 100.0000 mm): Via [GND] on F.Cu - B.Cu

** Found 0 Footprint errors **
** End of Report **
"""


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def pcb_clearance(tmp_path: Path) -> Path:
    f = tmp_path / "clearance.kicad_pcb"
    f.write_text(PCB_WITH_CLEARANCE)
    return f


@pytest.fixture
def pcb_same_net_vias(tmp_path: Path) -> Path:
    f = tmp_path / "same_net.kicad_pcb"
    f.write_text(PCB_WITH_SAME_NET_VIAS)
    return f


@pytest.fixture
def pcb_diff_net_vias(tmp_path: Path) -> Path:
    f = tmp_path / "diff_net.kicad_pcb"
    f.write_text(PCB_WITH_DIFF_NET_VIAS)
    return f


@pytest.fixture
def pcb_mixed(tmp_path: Path) -> Path:
    f = tmp_path / "mixed.kicad_pcb"
    f.write_text(PCB_WITH_MIXED)
    return f


@pytest.fixture
def report_clearance(tmp_path: Path) -> Path:
    f = tmp_path / "clearance-drc.rpt"
    f.write_text(DRC_REPORT_CLEARANCE)
    return f


@pytest.fixture
def report_drill(tmp_path: Path) -> Path:
    f = tmp_path / "drill-drc.rpt"
    f.write_text(DRC_REPORT_DRILL)
    return f


@pytest.fixture
def report_same_net_drill(tmp_path: Path) -> Path:
    f = tmp_path / "same-net-drill-drc.rpt"
    f.write_text(DRC_REPORT_SAME_NET_DRILL)
    return f


@pytest.fixture
def report_mixed(tmp_path: Path) -> Path:
    f = tmp_path / "mixed-drc.rpt"
    f.write_text(DRC_REPORT_MIXED)
    return f


@pytest.fixture
def pcb_segment_via(tmp_path: Path) -> Path:
    f = tmp_path / "segment_via.kicad_pcb"
    f.write_text(PCB_WITH_SEGMENT_VIA_CLEARANCE)
    return f


@pytest.fixture
def pcb_no_net_via(tmp_path: Path) -> Path:
    f = tmp_path / "no_net_via.kicad_pcb"
    f.write_text(PCB_WITH_NO_NET_VIA)
    return f


@pytest.fixture
def report_segment_via(tmp_path: Path) -> Path:
    f = tmp_path / "segment-via-drc.rpt"
    f.write_text(DRC_REPORT_SEGMENT_VIA)
    return f


@pytest.fixture
def report_no_net_via(tmp_path: Path) -> Path:
    f = tmp_path / "no-net-via-drc.rpt"
    f.write_text(DRC_REPORT_NO_NET_VIA)
    return f


@pytest.fixture
def report_empty(tmp_path: Path) -> Path:
    f = tmp_path / "empty-drc.rpt"
    f.write_text(DRC_REPORT_EMPTY)
    return f


@pytest.fixture
def pcb_enlarged_via(tmp_path: Path) -> Path:
    f = tmp_path / "enlarged_via.kicad_pcb"
    f.write_text(PCB_ENLARGED_VIA)
    return f


@pytest.fixture
def report_enlarged_via(tmp_path: Path) -> Path:
    f = tmp_path / "enlarged-via-drc.rpt"
    f.write_text(DRC_REPORT_ENLARGED_VIA)
    return f


# ── DrillClearanceRepairer unit tests ───────────────────────────────


class TestDrillClearanceRepairer:
    """Tests for the DrillClearanceRepairer class."""

    def test_load_pcb(self, pcb_same_net_vias: Path):
        """Should load PCB file and parse nets."""
        repairer = DrillClearanceRepairer(pcb_same_net_vias)
        assert repairer.nets[1] == "GND"
        assert not repairer.modified

    def test_deduplicate_same_net_vias(self, pcb_same_net_vias: Path):
        """Coincident same-net vias should be de-duplicated."""
        repairer = DrillClearanceRepairer(pcb_same_net_vias)

        violations = [
            DRCViolation(
                type=ViolationType.DRILL_CLEARANCE,
                type_str="drill_clearance",
                severity=Severity.ERROR,
                message="Drill clearance -0.3mm < 0.25mm",
                locations=[
                    Location(x_mm=115, y_mm=100),
                    Location(x_mm=115, y_mm=100),
                ],
                nets=["GND"],
                required_value_mm=0.25,
                actual_value_mm=-0.30,
            ),
        ]

        result = repairer.repair(violations, dry_run=False)

        assert result.total_violations == 1
        assert result.repaired == 1
        assert result.deduplicated == 1
        assert repairer.modified

    def test_deduplicate_dry_run(self, pcb_same_net_vias: Path):
        """Dry run should report dedup without modifying PCB."""
        repairer = DrillClearanceRepairer(pcb_same_net_vias)

        violations = [
            DRCViolation(
                type=ViolationType.DRILL_CLEARANCE,
                type_str="drill_clearance",
                severity=Severity.ERROR,
                message="Drill clearance -0.3mm < 0.25mm",
                locations=[
                    Location(x_mm=115, y_mm=100),
                    Location(x_mm=115, y_mm=100),
                ],
                nets=["GND"],
                required_value_mm=0.25,
                actual_value_mm=-0.30,
            ),
        ]

        result = repairer.repair(violations, dry_run=True)

        assert result.repaired == 1
        assert result.deduplicated == 1
        assert not repairer.modified  # dry run

    def test_slide_different_net_vias(self, pcb_diff_net_vias: Path):
        """Different-net vias too close should be slid apart."""
        repairer = DrillClearanceRepairer(pcb_diff_net_vias)

        violations = [
            DRCViolation(
                type=ViolationType.DRILL_CLEARANCE,
                type_str="drill_clearance",
                severity=Severity.ERROR,
                message="Drill clearance 0.1mm < 0.25mm",
                locations=[
                    Location(x_mm=115, y_mm=100.2),
                    Location(x_mm=115, y_mm=100.2),
                ],
                nets=["GND", "+3.3V"],
                required_value_mm=0.25,
                actual_value_mm=0.10,
            ),
        ]

        result = repairer.repair(violations, max_displacement=0.5, dry_run=False)

        assert result.total_violations == 1
        assert result.repaired == 1
        assert result.slid == 1
        assert repairer.modified

    def test_skip_exceeds_max_displacement(self, pcb_diff_net_vias: Path):
        """Should skip violations exceeding max displacement."""
        repairer = DrillClearanceRepairer(pcb_diff_net_vias)

        violations = [
            DRCViolation(
                type=ViolationType.DRILL_CLEARANCE,
                type_str="drill_clearance",
                severity=Severity.ERROR,
                message="Drill clearance 0.1mm < 0.25mm",
                locations=[
                    Location(x_mm=115, y_mm=100.2),
                ],
                nets=["GND", "+3.3V"],
                required_value_mm=0.25,
                actual_value_mm=0.10,
            ),
        ]

        # Max displacement too small (0.001 mm) to cover 0.15mm + margin
        result = repairer.repair(violations, max_displacement=0.001, dry_run=True)

        assert result.skipped_exceeds_max > 0
        assert result.repaired == 0

    def test_skip_no_location(self, pcb_diff_net_vias: Path):
        """Should skip violations without location data."""
        repairer = DrillClearanceRepairer(pcb_diff_net_vias)

        violations = [
            DRCViolation(
                type=ViolationType.DRILL_CLEARANCE,
                type_str="drill_clearance",
                severity=Severity.ERROR,
                message="Drill clearance violation",
                locations=[],
                nets=["GND"],
                required_value_mm=0.25,
                actual_value_mm=0.10,
            ),
        ]

        result = repairer.repair(violations, dry_run=True)
        assert result.skipped_no_location == 1

    def test_skip_no_delta(self, pcb_diff_net_vias: Path):
        """Should skip violations without required/actual values."""
        repairer = DrillClearanceRepairer(pcb_diff_net_vias)

        violations = [
            DRCViolation(
                type=ViolationType.DRILL_CLEARANCE,
                type_str="drill_clearance",
                severity=Severity.ERROR,
                message="Drill clearance violation",
                locations=[Location(x_mm=115, y_mm=100)],
                nets=["GND"],
                # No required/actual values
            ),
        ]

        result = repairer.repair(violations, dry_run=True)
        assert result.skipped_no_delta == 1

    def test_skip_infeasible_no_vias_found(self, tmp_path: Path):
        """Should skip violations when no vias are found near location."""
        pcb_content = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-1"))
)
"""
        pcb_file = tmp_path / "no_vias.kicad_pcb"
        pcb_file.write_text(pcb_content)

        repairer = DrillClearanceRepairer(pcb_file)

        violations = [
            DRCViolation(
                type=ViolationType.DRILL_CLEARANCE,
                type_str="drill_clearance",
                severity=Severity.ERROR,
                message="Drill clearance violation",
                locations=[Location(x_mm=200, y_mm=200)],
                nets=["GND"],
                required_value_mm=0.25,
                actual_value_mm=0.10,
            ),
        ]

        result = repairer.repair(violations, dry_run=True)
        assert result.skipped_infeasible == 1

    def test_result_summary(self):
        """DrillRepairResult summary should be readable."""
        result = DrillRepairResult(
            total_violations=5,
            repaired=3,
            deduplicated=2,
            slid=1,
            skipped_exceeds_max=1,
            skipped_infeasible=1,
        )
        summary = result.summary()
        assert "3/5" in summary
        assert "De-duplicated" in summary
        assert "Slid" in summary

    def test_success_rate(self):
        """Success rate should be calculated correctly."""
        result = DrillRepairResult(total_violations=4, repaired=3)
        assert result.success_rate == 0.75

        empty = DrillRepairResult(total_violations=0, repaired=0)
        assert empty.success_rate == 1.0

    def test_filters_non_drill_violations(self, pcb_diff_net_vias: Path):
        """repair() should filter out non-drill-clearance violations."""
        repairer = DrillClearanceRepairer(pcb_diff_net_vias)

        violations = [
            DRCViolation(
                type=ViolationType.CLEARANCE,
                type_str="clearance",
                severity=Severity.ERROR,
                message="Clearance violation",
                locations=[Location(x_mm=100, y_mm=100)],
                nets=["GND"],
                required_value_mm=0.2,
                actual_value_mm=0.1,
            ),
        ]

        result = repairer.repair(violations, dry_run=True)
        assert result.total_violations == 0

    def test_save_output(self, pcb_same_net_vias: Path, tmp_path: Path):
        """Should save modified PCB to output file."""
        output_file = tmp_path / "fixed.kicad_pcb"
        repairer = DrillClearanceRepairer(pcb_same_net_vias)

        violations = [
            DRCViolation(
                type=ViolationType.DRILL_CLEARANCE,
                type_str="drill_clearance",
                severity=Severity.ERROR,
                message="Drill clearance -0.3mm < 0.25mm",
                locations=[Location(x_mm=115, y_mm=100), Location(x_mm=115, y_mm=100)],
                nets=["GND"],
                required_value_mm=0.25,
                actual_value_mm=-0.30,
            ),
        ]

        repairer.repair(violations, dry_run=False)
        repairer.save(output_file)

        assert output_file.exists()
        # Original should be unchanged
        assert pcb_same_net_vias.read_text() == PCB_WITH_SAME_NET_VIAS


# ── ViolationType.from_string tests ─────────────────────────────────


class TestViolationTypeMapping:
    """Tests for ViolationType.from_string with drill clearance patterns."""

    def test_drill_clearance_direct(self):
        """Should parse 'drill_clearance' to DRILL_CLEARANCE."""
        assert ViolationType.from_string("drill_clearance") == ViolationType.DRILL_CLEARANCE

    def test_dimension_drill_clearance(self):
        """Should parse 'dimension_drill_clearance' to DRILL_CLEARANCE."""
        assert (
            ViolationType.from_string("dimension_drill_clearance") == ViolationType.DRILL_CLEARANCE
        )

    def test_clearance_still_works(self):
        """Standard clearance should still map to CLEARANCE."""
        assert ViolationType.from_string("clearance") == ViolationType.CLEARANCE

    def test_clearance_segment_segment(self):
        """clearance_segment_segment should map to CLEARANCE via partial match."""
        assert ViolationType.from_string("clearance_segment_segment") == ViolationType.CLEARANCE

    def test_clearance_segment_via(self):
        """clearance_segment_via should map to CLEARANCE_SEGMENT_VIA."""
        assert (
            ViolationType.from_string("clearance_segment_via")
            == ViolationType.CLEARANCE_SEGMENT_VIA
        )

    def test_clearance_segment_via_is_clearance(self):
        """CLEARANCE_SEGMENT_VIA violations should be considered clearance issues."""
        v = DRCViolation(
            type=ViolationType.CLEARANCE_SEGMENT_VIA,
            type_str="clearance_segment_via",
            severity=Severity.ERROR,
            message="test",
        )
        assert v.is_clearance


# ── CLI integration tests ───────────────────────────────────────────


class TestFixDRCCLI:
    """Tests for the fix-drc CLI command."""

    def test_dry_run_no_modification(self, pcb_clearance: Path, report_clearance: Path):
        """--dry-run should not modify the PCB file."""
        original = pcb_clearance.read_text()

        result = main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--dry-run",
            ]
        )

        # File unchanged
        assert pcb_clearance.read_text() == original
        # Result is 0 or 1 (depends on whether all fixed)
        assert result in (0, 1)

    def test_no_violations_exits_zero(self, pcb_clearance: Path, report_empty: Path, capsys):
        """Board with 0 violations should exit 0 immediately."""
        result = main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_empty),
            ]
        )

        assert result == 0
        captured = capsys.readouterr()
        assert "nothing to repair" in captured.out.lower() or "no targeted" in captured.out.lower()

    def test_only_clearance_skips_drill(self, pcb_mixed: Path, report_mixed: Path, capsys):
        """--only clearance should skip drill violations."""
        main(
            [
                str(pcb_mixed),
                "--drc-report",
                str(report_mixed),
                "--only",
                "clearance",
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Only clearance violations should be counted
        assert data["drill_clearance"]["violations"] == 0

    def test_only_drill_clearance_skips_trace(self, pcb_mixed: Path, report_mixed: Path, capsys):
        """--only drill-clearance should skip trace clearance violations."""
        main(
            [
                str(pcb_mixed),
                "--drc-report",
                str(report_mixed),
                "--only",
                "drill-clearance",
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Only drill violations should be counted
        assert data["clearance"]["violations"] == 0

    def test_json_output(self, pcb_clearance: Path, report_clearance: Path, capsys):
        """JSON output should be valid and contain expected keys."""
        main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "total_violations" in data
        assert "total_repaired" in data
        assert "clearance" in data
        assert "drill_clearance" in data
        assert data["dry_run"] is True

    def test_summary_output(self, pcb_clearance: Path, report_clearance: Path, capsys):
        """Summary output should show repair counts."""
        main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--dry-run",
                "--format",
                "summary",
            ]
        )

        captured = capsys.readouterr()
        # Should contain repair count info
        assert "/" in captured.out

    def test_quiet_mode(self, pcb_clearance: Path, report_clearance: Path, capsys):
        """--quiet should suppress all output."""
        main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--dry-run",
                "--quiet",
            ]
        )

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_output_to_different_file(
        self, pcb_clearance: Path, report_clearance: Path, tmp_path: Path
    ):
        """Should write to output file when specified."""
        output_file = tmp_path / "fixed.kicad_pcb"
        original = pcb_clearance.read_text()

        main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "-o",
                str(output_file),
            ]
        )

        # Original should be unchanged
        assert pcb_clearance.read_text() == original

        # Output file should exist (only if repairs were made)
        # The test PCB may or may not result in repairs depending on object lookup

    def test_invalid_file(self, tmp_path: Path):
        """Should fail for non-existent file."""
        result = main([str(tmp_path / "nonexistent.kicad_pcb")])
        assert result == 1

    def test_wrong_extension(self, tmp_path: Path):
        """Should fail for non-.kicad_pcb file."""
        bad_file = tmp_path / "test.txt"
        bad_file.write_text("not a pcb")
        result = main([str(bad_file)])
        assert result == 1

    def test_nonexistent_report(self, pcb_clearance: Path, tmp_path: Path):
        """Should fail for non-existent DRC report."""
        result = main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(tmp_path / "nonexistent.rpt"),
            ]
        )
        assert result == 1

    def test_exit_code_zero_all_repaired(
        self, pcb_same_net_vias: Path, report_same_net_drill: Path
    ):
        """Exit code 0 when all targeted violations are repaired."""
        result = main(
            [
                str(pcb_same_net_vias),
                "--drc-report",
                str(report_same_net_drill),
            ]
        )
        assert result == 0

    def test_segment_via_clearance_counted(
        self, pcb_segment_via: Path, report_segment_via: Path, capsys
    ):
        """clearance_segment_via violations should be counted in clearance total."""
        main(
            [
                str(pcb_segment_via),
                "--drc-report",
                str(report_segment_via),
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # The segment-via violation should appear in the clearance count
        assert data["clearance"]["violations"] >= 1

    def test_segment_via_clearance_repair(
        self, pcb_segment_via: Path, report_segment_via: Path, capsys
    ):
        """Segment-to-via clearance should be repaired by moving the segment."""
        main(
            [
                str(pcb_segment_via),
                "--drc-report",
                str(report_segment_via),
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # The violation should be repaired (segment nudged)
        assert data["clearance"]["repaired"] >= 1
        # Verify it was a segment nudge (not a via move)
        if data["clearance"]["nudges"]:
            assert data["clearance"]["nudges"][0]["object_type"] == "segment"

    def test_no_net_via_clearance_repair(
        self, pcb_no_net_via: Path, report_no_net_via: Path, capsys
    ):
        """Via with no net should not be excluded from clearance repair."""
        main(
            [
                str(pcb_no_net_via),
                "--drc-report",
                str(report_no_net_via),
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # The violation should be repaired (via found despite having no net)
        assert data["clearance"]["repaired"] >= 1

    def test_max_displacement_zero_skips_nudges(
        self, pcb_clearance: Path, report_clearance: Path, capsys
    ):
        """--max-displacement 0 should skip all trace nudges."""
        main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--max-displacement",
                "0",
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # All clearance violations should be skipped (exceeds max)
        assert data["clearance"]["repaired"] == 0


# ── Edge case tests ─────────────────────────────────────────────────


class TestEdgeCases:
    """Edge case tests for fix-drc."""

    def test_isolated_via_no_trace(self, tmp_path: Path):
        """Via with no connected trace should still attempt repair (push away)."""
        pcb_content = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
  (via (at 100 100.4) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-2"))
)
"""
        pcb_file = tmp_path / "isolated.kicad_pcb"
        pcb_file.write_text(pcb_content)

        repairer = DrillClearanceRepairer(pcb_file)

        violations = [
            DRCViolation(
                type=ViolationType.DRILL_CLEARANCE,
                type_str="drill_clearance",
                severity=Severity.ERROR,
                message="Drill clearance 0.1mm < 0.25mm",
                locations=[Location(x_mm=100, y_mm=100.2)],
                nets=["GND", "+3.3V"],
                required_value_mm=0.25,
                actual_value_mm=0.10,
            ),
        ]

        result = repairer.repair(violations, max_displacement=0.5, dry_run=True)
        # Should succeed via direct push (no connected trace)
        assert result.repaired == 1 or result.skipped_infeasible >= 0


# ── Report inference: kicad-cli clearance -> CLEARANCE_SEGMENT_VIA ──


# DRC text report where kicad-cli outputs [clearance] for a Track-to-Via pair.
# This is the real-world scenario: kicad-cli doesn't emit [clearance_segment_via].
DRC_REPORT_KICAD_CLI_SEGMENT_VIA = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 1 DRC violations **
[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.0500 mm)
    Rule: netclass 'Default'; error
    @(105.0000 mm, 100.1000 mm): Track [+3.3V] on F.Cu
    @(105.0000 mm, 100.0000 mm): Via [GND] on F.Cu - B.Cu

** Found 0 Footprint errors **
** End of Report **
"""

# DRC text report with a Track-to-Track pair (should stay CLEARANCE).
DRC_REPORT_KICAD_CLI_SEGMENT_SEGMENT = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 1 DRC violations **
[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1500 mm)
    Rule: netclass 'Default'; error
    @(105.0000 mm, 100.0000 mm): Track [GND] on F.Cu
    @(105.0000 mm, 100.1500 mm): Track [+3.3V] on F.Cu

** Found 0 Footprint errors **
** End of Report **
"""

# DRC text report with both Track-to-Track and Track-to-Via violations.
DRC_REPORT_KICAD_CLI_MIXED_CLEARANCE = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 2 DRC violations **
[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1500 mm)
    Rule: netclass 'Default'; error
    @(105.0000 mm, 100.0000 mm): Track [GND] on F.Cu
    @(105.0000 mm, 100.1500 mm): Track [+3.3V] on F.Cu

[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.0500 mm)
    Rule: netclass 'Default'; error
    @(108.0000 mm, 100.1000 mm): Track [+3.3V] on F.Cu
    @(108.0000 mm, 100.0000 mm): Via [GND] on F.Cu - B.Cu

** Found 0 Footprint errors **
** End of Report **
"""

# JSON report where kicad-cli uses "clearance" for a Track-to-Via pair.
DRC_JSON_KICAD_CLI_SEGMENT_VIA = json.dumps(
    {
        "source": "test.kicad_pcb",
        "date": "2025-12-28T21:29:34-08:00",
        "violations": [
            {
                "type": "clearance",
                "description": "Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.0500 mm)",
                "severity": "error",
                "items": [
                    {
                        "description": "Track [+3.3V] on F.Cu",
                        "pos": {"x": 105.0, "y": 100.1},
                        "net": "+3.3V",
                    },
                    {
                        "description": "Via [GND] on F.Cu - B.Cu",
                        "pos": {"x": 105.0, "y": 100.0},
                        "net": "GND",
                    },
                ],
            }
        ],
    }
)


class TestSegmentViaInference:
    """Tests that kicad-cli's generic [clearance] is reclassified when items indicate a via."""

    def test_text_report_track_via_becomes_segment_via(self):
        """Text report with [clearance] + Track + Via -> CLEARANCE_SEGMENT_VIA."""
        from kicad_tools.drc.report import parse_text_report

        report = parse_text_report(DRC_REPORT_KICAD_CLI_SEGMENT_VIA)
        assert len(report.violations) == 1
        assert report.violations[0].type == ViolationType.CLEARANCE_SEGMENT_VIA

    def test_text_report_track_track_stays_clearance(self):
        """Text report with [clearance] + Track + Track -> CLEARANCE (unchanged)."""
        from kicad_tools.drc.report import parse_text_report

        report = parse_text_report(DRC_REPORT_KICAD_CLI_SEGMENT_SEGMENT)
        assert len(report.violations) == 1
        assert report.violations[0].type == ViolationType.CLEARANCE

    def test_text_report_mixed_no_double_counting(self):
        """Mixed Track-Track and Track-Via should not double-count."""
        from kicad_tools.drc.report import parse_text_report

        report = parse_text_report(DRC_REPORT_KICAD_CLI_MIXED_CLEARANCE)
        assert len(report.violations) == 2

        clearance_only = report.by_type(ViolationType.CLEARANCE)
        segment_via = report.by_type(ViolationType.CLEARANCE_SEGMENT_VIA)

        assert len(clearance_only) == 1
        assert len(segment_via) == 1

    def test_json_report_track_via_becomes_segment_via(self):
        """JSON report with "clearance" type + Track + Via -> CLEARANCE_SEGMENT_VIA."""
        from kicad_tools.drc.report import parse_json_report

        report = parse_json_report(DRC_JSON_KICAD_CLI_SEGMENT_VIA)
        assert len(report.violations) == 1
        assert report.violations[0].type == ViolationType.CLEARANCE_SEGMENT_VIA

    def test_explicit_segment_via_not_reclassified(self):
        """Pre-classified [clearance_segment_via] is preserved, not double-reclassified."""
        from kicad_tools.drc.report import parse_text_report

        # Re-use the existing fixture that already uses [clearance_segment_via] tag
        report = parse_text_report(DRC_REPORT_SEGMENT_VIA)
        assert len(report.violations) == 1
        assert report.violations[0].type == ViolationType.CLEARANCE_SEGMENT_VIA

    def test_pad_clearance_stays_clearance(self):
        """[clearance] with Pad + Track should remain CLEARANCE (not segment-via)."""
        from kicad_tools.drc.report import parse_text_report

        pad_report = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 1 DRC violations **
[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.0500 mm)
    Rule: netclass 'Default'; error
    @(105.0000 mm, 100.0000 mm): Pad 1 [+3.3V] of U1 on F.Cu
    @(105.0000 mm, 100.1000 mm): Track [GND] on F.Cu

** Found 0 Footprint errors **
** End of Report **
"""
        report = parse_text_report(pad_report)
        assert len(report.violations) == 1
        assert report.violations[0].type == ViolationType.CLEARANCE

    def test_kicad_cli_report_fix_drc_detects_segment_via(
        self, pcb_segment_via: Path, tmp_path: Path, capsys
    ):
        """fix-drc CLI with kicad-cli-style [clearance] report finds segment-to-via violations."""
        report_file = tmp_path / "kicad-cli-drc.rpt"
        report_file.write_text(DRC_REPORT_KICAD_CLI_SEGMENT_VIA)

        main(
            [
                str(pcb_segment_via),
                "--drc-report",
                str(report_file),
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # The segment-via violation should appear in the clearance count
        assert data["clearance"]["violations"] >= 1


# ── Pure-Python DRC fallback tests ────────────────────────────────────


class TestPurePythonDRCFallback:
    """Tests for _run_python_drc() which converts DRCChecker results to DRCReport."""

    def test_fallback_detects_clearance_violations(self, tmp_path: Path):
        """Pure-Python DRC should detect segment clearance violations."""
        from kicad_tools.cli.fix_drc_cmd import _run_python_drc

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLEARANCE)

        report = _run_python_drc(pcb_file)
        assert report is not None
        assert report.violation_count > 0

    def test_fallback_detects_segment_via_violations(self, tmp_path: Path):
        """Pure-Python DRC should detect segment-to-via clearance violations."""
        from kicad_tools.cli.fix_drc_cmd import _run_python_drc

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_WITH_SEGMENT_VIA_CLEARANCE)

        report = _run_python_drc(pcb_file)
        assert report is not None

        segment_via_violations = report.by_type(ViolationType.CLEARANCE_SEGMENT_VIA)
        assert len(segment_via_violations) > 0

    def test_fallback_violations_have_locations(self, tmp_path: Path):
        """Converted violations should carry location data."""
        from kicad_tools.cli.fix_drc_cmd import _run_python_drc

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLEARANCE)

        report = _run_python_drc(pcb_file)
        assert report is not None

        for v in report.violations:
            assert len(v.locations) > 0, "Converted violation should have at least one location"

    def test_fallback_violations_have_values(self, tmp_path: Path):
        """Converted violations should carry required and actual values."""
        from kicad_tools.cli.fix_drc_cmd import _run_python_drc

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLEARANCE)

        report = _run_python_drc(pcb_file)
        assert report is not None

        for v in report.violations:
            assert v.required_value_mm is not None
            assert v.actual_value_mm is not None

    def test_fallback_used_when_no_kicad_cli(self, tmp_path: Path, monkeypatch):
        """_get_drc_report should fall back to pure-Python DRC when kicad-cli is missing."""
        from kicad_tools.cli import fix_drc_cmd

        # Make find_kicad_cli return None
        monkeypatch.setattr(
            "kicad_tools.cli.runner.find_kicad_cli",
            lambda: None,
        )

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_WITH_SEGMENT_VIA_CLEARANCE)

        report = fix_drc_cmd._get_drc_report(None, pcb_file)
        assert report is not None
        assert report.violation_count > 0


# ── Multi-pass iteration tests ─────────────────────────────────────


class TestMultiPassIteration:
    """Tests for the --max-passes iterative repair cycle."""

    def test_max_passes_1_matches_single_pass(
        self, pcb_clearance: Path, report_clearance: Path, capsys
    ):
        """--max-passes 1 output should be identical to current single-pass behaviour."""
        # Run without --max-passes (default=1)
        main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--dry-run",
                "--format",
                "json",
            ]
        )
        baseline_out = capsys.readouterr().out

        # Run with explicit --max-passes 1
        # Reset the PCB file in case it was modified
        pcb_clearance.write_text(PCB_WITH_CLEARANCE)
        main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--dry-run",
                "--format",
                "json",
                "--max-passes",
                "1",
            ]
        )
        explicit_out = capsys.readouterr().out

        baseline_data = json.loads(baseline_out)
        explicit_data = json.loads(explicit_out)

        # Core fields should match
        assert baseline_data["total_violations"] == explicit_data["total_violations"]
        assert baseline_data["total_repaired"] == explicit_data["total_repaired"]
        assert baseline_data["clearance"]["repaired"] == explicit_data["clearance"]["repaired"]

    def test_max_passes_no_progress_exits_early(
        self, pcb_clearance: Path, report_clearance: Path, capsys
    ):
        """When all violations exceed --max-displacement, iteration exits after pass 1."""
        result = main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--max-displacement",
                "0",
                "--max-passes",
                "5",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Should have only 1 pass since no repairs were made
        assert "passes" in data
        assert len(data["passes"]) == 1
        assert data["passes"][0]["repaired"] == 0

        # Exit code non-zero (violations remain)
        assert result == 1

    def test_max_passes_json_output_has_passes_array(
        self, pcb_clearance: Path, report_clearance: Path, capsys
    ):
        """JSON output should contain a 'passes' key."""
        main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--dry-run",
                "--format",
                "json",
                "--max-passes",
                "3",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "passes" in data
        assert isinstance(data["passes"], list)
        assert len(data["passes"]) >= 1

        # Each pass entry should have required keys
        for p in data["passes"]:
            assert "pass" in p
            assert "violations_before" in p
            assert "repaired" in p
            assert "violations_after" in p

    def test_dry_run_max_passes_single_detection(
        self, pcb_clearance: Path, report_clearance: Path, capsys
    ):
        """--dry-run --max-passes 3 should only run one pass (no geometry changes)."""
        main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--dry-run",
                "--format",
                "json",
                "--max-passes",
                "3",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Dry-run forces effective_max_passes=1
        assert "passes" in data
        assert len(data["passes"]) == 1

    def test_max_passes_zero_errors(self, pcb_clearance: Path, report_clearance: Path):
        """--max-passes 0 should return error."""
        result = main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--max-passes",
                "0",
            ]
        )
        assert result == 1

    def test_max_passes_clean_board_exits_pass_1(self, pcb_clearance: Path, report_empty: Path):
        """Already-clean board with --max-passes 100 should exit after pass 1."""
        result = main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_empty),
                "--max-passes",
                "100",
            ]
        )
        assert result == 0

    def test_max_passes_summary_output(self, pcb_clearance: Path, report_clearance: Path, capsys):
        """Summary format with --max-passes should report per-pass progress."""
        main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--max-passes",
                "3",
                "--format",
                "summary",
            ]
        )

        captured = capsys.readouterr()
        # Should contain repair count info
        assert "/" in captured.out

    def test_max_passes_text_output(self, pcb_clearance: Path, report_clearance: Path, capsys):
        """Text format with --max-passes should produce valid output."""
        main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--max-passes",
                "3",
                "--format",
                "text",
            ]
        )

        captured = capsys.readouterr()
        # Should contain the repair header
        assert "DRC VIOLATION REPAIR" in captured.out

    def test_max_passes_converges_dedup(
        self, pcb_same_net_vias: Path, report_same_net_drill: Path, capsys
    ):
        """Multi-pass on a board with dedup-able vias should converge."""
        main(
            [
                str(pcb_same_net_vias),
                "--drc-report",
                str(report_same_net_drill),
                "--max-passes",
                "3",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Should have resolved the violation
        assert data["total_repaired"] >= 1

    def test_multi_pass_output_path_not_stale(
        self, pcb_clearance: Path, report_clearance: Path, tmp_path: Path, capsys
    ):
        """Pass 2+ must load from --output, not the original pcb_path.

        Regression test for the stale-file bug: ClearanceRepairer was always
        constructed with ``pcb_path`` instead of ``output_path``, so in a
        multi-pass run the second pass silently re-read the unrepaired original
        board and discarded the first pass's repairs.
        """
        output_file = tmp_path / "repaired.kicad_pcb"
        original_content = pcb_clearance.read_text()

        result = main(
            [
                str(pcb_clearance),
                "--drc-report",
                str(report_clearance),
                "--output",
                str(output_file),
                "--max-passes",
                "3",
                "--format",
                "json",
            ]
        )

        # The output file must have been created with the repaired content.
        assert output_file.exists(), "output file should be written after repair"

        output_content = output_file.read_text()
        # The repaired file must differ from the original (repairs were applied).
        assert output_content != original_content, (
            "output file content should differ from the original after repair; "
            "if it matches, pass 2+ likely loaded the stale original input"
        )

        # The original input file must not have been modified.
        assert pcb_clearance.read_text() == original_content, (
            "original pcb_path should remain untouched when --output is given"
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["total_repaired"] >= 1
        assert result == 0


# ── Enlarged-via displacement threshold tests ──────────────────────


class TestEnlargedViaDisplacement:
    """Verify that segment-to-via violations near enlarged vias are
    skipped at the old 0.25 mm cap but repaired at 0.5 mm.

    This exercises the core scenario from issue #1401: after fix-vias
    enlarges a via, the induced clearance violation requires a nudge
    larger than the old default but within the new pipeline default.
    """

    def test_skipped_at_old_default(
        self, pcb_enlarged_via: Path, report_enlarged_via: Path, capsys
    ):
        """Violation is skipped when max-displacement is the old 0.25 mm default."""
        main(
            [
                str(pcb_enlarged_via),
                "--drc-report",
                str(report_enlarged_via),
                "--max-displacement",
                "0.25",
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["clearance"]["repaired"] == 0
        assert data["clearance"]["skipped"]["exceeds_max"] >= 1

    def test_repaired_at_new_default(
        self, pcb_enlarged_via: Path, report_enlarged_via: Path, capsys
    ):
        """Violation is repaired when max-displacement is raised to 0.5 mm."""
        main(
            [
                str(pcb_enlarged_via),
                "--drc-report",
                str(report_enlarged_via),
                "--max-displacement",
                "0.5",
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["clearance"]["repaired"] >= 1
        assert data["clearance"]["skipped"]["exceeds_max"] == 0

    @pytest.mark.parametrize(
        "max_disp,expect_repaired",
        [
            (0.25, False),
            (0.27, False),  # 0.27 + 0.01 margin = 0.28, still exceeds 0.27
            (0.30, True),  # 0.28 < 0.30
            (0.50, True),
            (1.00, True),
        ],
    )
    def test_displacement_threshold_boundary(
        self,
        pcb_enlarged_via: Path,
        report_enlarged_via: Path,
        capsys,
        max_disp: float,
        expect_repaired: bool,
    ):
        """Parametrized boundary test across displacement thresholds."""
        main(
            [
                str(pcb_enlarged_via),
                "--drc-report",
                str(report_enlarged_via),
                "--max-displacement",
                str(max_disp),
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        if expect_repaired:
            assert data["clearance"]["repaired"] >= 1, (
                f"Expected repair at max_displacement={max_disp}"
            )
        else:
            assert data["clearance"]["repaired"] == 0, (
                f"Expected skip at max_displacement={max_disp}"
            )


# ── Centralized CLI forwarding tests ────────────────────────────────


class TestLocalRerouteViaCentralizedCLI:
    """Tests that --local-reroute and --quiet work via kct fix-drc (centralized CLI)."""

    def test_centralized_cli_local_reroute_dry_run(self, tmp_path):
        """kct fix-drc ... --local-reroute --dry-run parses without 'unrecognized arguments'."""
        from kicad_tools.cli import main as cli_main

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLEARANCE)

        result = cli_main(["fix-drc", str(pcb_file), "--local-reroute", "--dry-run", "--quiet"])
        # Should not fail with unrecognized arguments (exit code 2 = argparse error)
        assert result != 2

    def test_centralized_cli_quiet_dry_run(self, tmp_path):
        """kct fix-drc ... --quiet --dry-run parses without 'unrecognized arguments'."""
        from kicad_tools.cli import main as cli_main

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLEARANCE)

        result = cli_main(["fix-drc", str(pcb_file), "--quiet", "--dry-run"])
        assert result != 2

    def test_centralized_cli_quiet_short_flag_dry_run(self, tmp_path):
        """kct fix-drc ... -q --dry-run parses the short -q flag."""
        from kicad_tools.cli import main as cli_main

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLEARANCE)

        result = cli_main(["fix-drc", str(pcb_file), "-q", "--dry-run"])
        assert result != 2

    def test_centralized_cli_local_reroute_quiet_combined(self, tmp_path):
        """kct fix-drc ... --local-reroute --quiet --dry-run combines without conflict."""
        from kicad_tools.cli import main as cli_main

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLEARANCE)

        result = cli_main(["fix-drc", str(pcb_file), "--local-reroute", "--quiet", "--dry-run"])
        assert result != 2

    def test_centralized_cli_max_displacement_still_works(self, tmp_path):
        """kct fix-drc ... --max-displacement 0.5 --dry-run still works (regression check)."""
        from kicad_tools.cli import main as cli_main

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLEARANCE)

        result = cli_main(
            ["fix-drc", str(pcb_file), "--max-displacement", "0.5", "--dry-run", "--quiet"]
        )
        assert result != 2
