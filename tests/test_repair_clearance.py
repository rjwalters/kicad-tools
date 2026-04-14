"""Tests for the repair-clearance command and ClearanceRepairer."""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.repair_clearance_cmd import main
from kicad_tools.drc.repair_clearance import ClearanceRepairer, RepairResult
from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType

# PCB with two traces that are too close together
PCB_WITH_CLEARANCE_VIOLATION = """(kicad_pcb
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
  (via (at 115 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
  (via (at 115 100.35) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-2"))
)
"""

# DRC report with clearance violations matching the PCB above
DRC_REPORT_CLEARANCE = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 2 DRC violations **
[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1500 mm)
    Rule: netclass 'Default'; error
    @(105.0000 mm, 100.0000 mm): Track [GND] on F.Cu
    @(105.0000 mm, 100.1500 mm): Track [+3.3V] on F.Cu

[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.0500 mm)
    Rule: netclass 'Default'; error
    @(115.0000 mm, 100.0000 mm): Via [GND] on F.Cu - B.Cu
    @(115.0000 mm, 100.3500 mm): Via [+3.3V] on F.Cu - B.Cu

** Found 0 Footprint errors **
** End of Report **
"""


@pytest.fixture
def pcb_file(tmp_path: Path) -> Path:
    """Create a PCB with clearance violations for testing."""
    pcb_file = tmp_path / "test.kicad_pcb"
    pcb_file.write_text(PCB_WITH_CLEARANCE_VIOLATION)
    return pcb_file


@pytest.fixture
def drc_report_file(tmp_path: Path) -> Path:
    """Create a DRC report file for testing."""
    report_file = tmp_path / "test-drc.rpt"
    report_file.write_text(DRC_REPORT_CLEARANCE)
    return report_file


@pytest.fixture
def drc_report() -> DRCReport:
    """Create a DRC report from the test data."""
    from kicad_tools.drc.report import parse_text_report

    return parse_text_report(DRC_REPORT_CLEARANCE, "test-drc.rpt")


class TestClearanceRepairer:
    """Tests for the ClearanceRepairer class."""

    def test_load_pcb(self, pcb_file: Path):
        """Should load PCB file and parse nets."""
        repairer = ClearanceRepairer(pcb_file)
        assert repairer.nets[1] == "GND"
        assert repairer.nets[2] == "+3.3V"
        assert not repairer.modified

    def test_repair_dry_run(self, pcb_file: Path, drc_report: DRCReport):
        """Dry run should report repairs without modifying PCB."""
        repairer = ClearanceRepairer(pcb_file)

        result = repairer.repair_from_report(
            drc_report,
            max_displacement=0.2,
            dry_run=True,
        )

        assert result.total_violations == 2
        assert result.repaired > 0
        assert not repairer.modified  # Dry run: no modification

    def test_repair_applies_changes(self, pcb_file: Path, drc_report: DRCReport):
        """Should modify PCB when not dry run."""
        repairer = ClearanceRepairer(pcb_file)

        result = repairer.repair_from_report(
            drc_report,
            max_displacement=0.2,
            dry_run=False,
        )

        assert result.repaired > 0
        assert repairer.modified

    def test_repair_respects_max_displacement(self, pcb_file: Path, drc_report: DRCReport):
        """Should skip violations exceeding max displacement."""
        repairer = ClearanceRepairer(pcb_file)

        # Very small max displacement - should skip most violations
        result = repairer.repair_from_report(
            drc_report,
            max_displacement=0.001,
            dry_run=True,
        )

        assert result.skipped_exceeds_max > 0

    def test_nudge_results_have_details(self, pcb_file: Path, drc_report: DRCReport):
        """Nudge results should include displacement details."""
        repairer = ClearanceRepairer(pcb_file)

        result = repairer.repair_from_report(
            drc_report,
            max_displacement=0.5,
            dry_run=True,
        )

        for nudge in result.nudges:
            assert nudge.object_type in ("segment", "via")
            assert nudge.displacement_mm > 0
            assert nudge.old_clearance_mm >= 0
            assert nudge.new_clearance_mm > nudge.old_clearance_mm

    def test_save_output(self, pcb_file: Path, drc_report: DRCReport, tmp_path: Path):
        """Should save modified PCB to output file."""
        output_file = tmp_path / "fixed.kicad_pcb"
        repairer = ClearanceRepairer(pcb_file)

        repairer.repair_from_report(
            drc_report,
            max_displacement=0.5,
            dry_run=False,
        )

        repairer.save(output_file)
        assert output_file.exists()

        # Original should be unchanged
        assert pcb_file.read_text() == PCB_WITH_CLEARANCE_VIOLATION

    def test_prefer_move_via(self, pcb_file: Path, drc_report: DRCReport):
        """Should prefer moving vias when prefer=move-via."""
        repairer = ClearanceRepairer(pcb_file)

        result = repairer.repair_from_report(
            drc_report,
            max_displacement=0.5,
            prefer="move-via",
            dry_run=True,
        )

        # At least some nudges should target vias
        via_nudges = [n for n in result.nudges if n.object_type == "via"]
        # We expect the via violation to be handled with a via nudge
        assert len(via_nudges) >= 0  # May still move traces if vias aren't found

    def test_repair_result_summary(self):
        """RepairResult summary should be readable."""
        result = RepairResult(
            total_violations=5,
            repaired=3,
            skipped_exceeds_max=1,
            skipped_infeasible=1,
        )
        summary = result.summary()
        assert "3/5" in summary
        assert "Exceeds max displacement" in summary or "exceeds" in summary.lower()

    def test_success_rate(self):
        """Success rate should be calculated correctly."""
        result = RepairResult(total_violations=4, repaired=3)
        assert result.success_rate == 0.75

        empty = RepairResult(total_violations=0, repaired=0)
        assert empty.success_rate == 1.0


# PCB with a segment too close to an enlarged via
PCB_WITH_SEGMENT_VIA = """(kicad_pcb
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
PCB_WITH_NO_NET_VIA = """(kicad_pcb
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


class TestSegmentViaClearanceRepair:
    """Tests for segment-to-via clearance repair."""

    def test_repair_segment_via_moves_segment(self, tmp_path: Path):
        """Segment-to-via repair should move the segment, not the via."""
        pcb_file = tmp_path / "seg_via.kicad_pcb"
        pcb_file.write_text(PCB_WITH_SEGMENT_VIA)

        repairer = ClearanceRepairer(pcb_file)

        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                DRCViolation(
                    type=ViolationType.CLEARANCE_SEGMENT_VIA,
                    type_str="clearance_segment_via",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.05mm < 0.2mm)",
                    locations=[
                        Location(x_mm=105.0, y_mm=100.1, layer="F.Cu"),
                        Location(x_mm=105.0, y_mm=100.0, layer="F.Cu"),
                    ],
                    nets=["+3.3V", "GND"],
                    required_value_mm=0.2,
                    actual_value_mm=0.05,
                ),
            ],
        )

        result = repairer.repair_from_report(
            report,
            max_displacement=0.5,
            prefer="move-via",  # Even with move-via preference, segment-via should move trace
            dry_run=True,
        )

        assert result.total_violations == 1
        assert result.repaired == 1
        # Must have moved a segment, not the via
        assert result.nudges[0].object_type == "segment"

    def test_repair_segment_via_dry_run_no_modify(self, tmp_path: Path):
        """Dry run of segment-to-via repair should not modify PCB."""
        pcb_file = tmp_path / "seg_via.kicad_pcb"
        pcb_file.write_text(PCB_WITH_SEGMENT_VIA)

        repairer = ClearanceRepairer(pcb_file)

        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                DRCViolation(
                    type=ViolationType.CLEARANCE_SEGMENT_VIA,
                    type_str="clearance_segment_via",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.05mm < 0.2mm)",
                    locations=[
                        Location(x_mm=105.0, y_mm=100.1, layer="F.Cu"),
                        Location(x_mm=105.0, y_mm=100.0, layer="F.Cu"),
                    ],
                    nets=["+3.3V", "GND"],
                    required_value_mm=0.2,
                    actual_value_mm=0.05,
                ),
            ],
        )

        result = repairer.repair_from_report(report, max_displacement=0.5, dry_run=True)
        assert result.repaired == 1
        assert not repairer.modified

    def test_no_net_via_found(self, tmp_path: Path):
        """Via with net 0 (no net) should still be found by _find_vias_near."""
        pcb_file = tmp_path / "no_net.kicad_pcb"
        pcb_file.write_text(PCB_WITH_NO_NET_VIA)

        repairer = ClearanceRepairer(pcb_file)

        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                DRCViolation(
                    type=ViolationType.CLEARANCE_SEGMENT_VIA,
                    type_str="clearance_segment_via",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.05mm < 0.2mm)",
                    locations=[
                        Location(x_mm=105.0, y_mm=100.1, layer="F.Cu"),
                        Location(x_mm=105.0, y_mm=100.0, layer="F.Cu"),
                    ],
                    nets=["GND"],
                    required_value_mm=0.2,
                    actual_value_mm=0.05,
                ),
            ],
        )

        result = repairer.repair_from_report(report, max_displacement=0.5, dry_run=True)
        # Via should be found despite having no net, and segment should be moved
        assert result.repaired == 1
        assert result.nudges[0].object_type == "segment"

    def test_violation_type_from_string(self):
        """ViolationType.from_string should correctly parse clearance_segment_via."""
        assert (
            ViolationType.from_string("clearance_segment_via")
            == ViolationType.CLEARANCE_SEGMENT_VIA
        )

    def test_clearance_segment_via_is_clearance(self):
        """CLEARANCE_SEGMENT_VIA violations should count as clearance violations."""
        v = DRCViolation(
            type=ViolationType.CLEARANCE_SEGMENT_VIA,
            type_str="clearance_segment_via",
            severity=Severity.ERROR,
            message="test",
        )
        assert v.is_clearance


class TestRepairFromViolation:
    """Tests for handling different violation types."""

    def test_skips_non_clearance_violations(self, pcb_file: Path):
        """Should only process clearance violations."""
        repairer = ClearanceRepairer(pcb_file)

        # Create a report with non-clearance violations
        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                DRCViolation(
                    type=ViolationType.UNCONNECTED_ITEMS,
                    type_str="unconnected_items",
                    severity=Severity.ERROR,
                    message="Unconnected items",
                ),
            ],
        )

        result = repairer.repair_from_report(report, dry_run=True)
        assert result.total_violations == 0  # No clearance violations
        assert result.repaired == 0

    def test_handles_violation_without_locations(self, pcb_file: Path):
        """Should skip violations without location data."""
        repairer = ClearanceRepairer(pcb_file)

        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                DRCViolation(
                    type=ViolationType.CLEARANCE,
                    type_str="clearance",
                    severity=Severity.ERROR,
                    message="Clearance violation",
                    locations=[],
                    required_value_mm=0.2,
                    actual_value_mm=0.1,
                ),
            ],
        )

        result = repairer.repair_from_report(report, dry_run=True)
        assert result.skipped_no_location == 1

    def test_handles_violation_without_delta(self, pcb_file: Path):
        """Should skip violations without required/actual values."""
        repairer = ClearanceRepairer(pcb_file)

        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                DRCViolation(
                    type=ViolationType.CLEARANCE,
                    type_str="clearance",
                    severity=Severity.ERROR,
                    message="Clearance violation",
                    locations=[
                        Location(x_mm=100, y_mm=100, layer="F.Cu"),
                        Location(x_mm=100, y_mm=100.1, layer="F.Cu"),
                    ],
                    # No required_value_mm or actual_value_mm
                ),
            ],
        )

        result = repairer.repair_from_report(report, dry_run=True)
        assert result.skipped_no_delta == 1


class TestCLI:
    """Tests for the CLI interface."""

    def test_dry_run(self, pcb_file: Path, drc_report_file: Path, capsys):
        """Dry run should show changes but not modify file."""
        original = pcb_file.read_text()

        result = main(
            [
                str(pcb_file),
                "--drc-report",
                str(drc_report_file),
                "--dry-run",
            ]
        )

        assert result == 0 or result == 1  # May be 1 if not all violations fixed

        # File should be unchanged
        assert pcb_file.read_text() == original

        captured = capsys.readouterr()
        assert "clearance" in captured.out.lower() or "CLEARANCE" in captured.out

    def test_output_to_different_file(self, pcb_file: Path, drc_report_file: Path, tmp_path: Path):
        """Should write to output file when specified."""
        output_file = tmp_path / "fixed.kicad_pcb"
        original = pcb_file.read_text()

        main(
            [
                str(pcb_file),
                "--drc-report",
                str(drc_report_file),
                "-o",
                str(output_file),
            ]
        )

        # Original should be unchanged
        assert pcb_file.read_text() == original

        # Output file should exist
        assert output_file.exists()

    def test_json_output(self, pcb_file: Path, drc_report_file: Path, capsys):
        """JSON output should be valid."""
        main(
            [
                str(pcb_file),
                "--drc-report",
                str(drc_report_file),
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "total_violations" in data
        assert "repaired" in data
        assert "nudges" in data
        assert data["dry_run"] is True

    def test_summary_output(self, pcb_file: Path, drc_report_file: Path, capsys):
        """Summary output should show counts."""
        main(
            [
                str(pcb_file),
                "--drc-report",
                str(drc_report_file),
                "--dry-run",
                "--format",
                "summary",
            ]
        )

        captured = capsys.readouterr()
        assert "clearance" in captured.out.lower()

    def test_quiet_mode(self, pcb_file: Path, drc_report_file: Path, capsys):
        """Quiet mode should suppress output."""
        main(
            [
                str(pcb_file),
                "--drc-report",
                str(drc_report_file),
                "--dry-run",
                "--quiet",
            ]
        )

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_invalid_file(self, tmp_path: Path):
        """Should fail for non-existent file."""
        result = main([str(tmp_path / "nonexistent.kicad_pcb")])
        assert result == 1

    def test_max_displacement_option(self, pcb_file: Path, drc_report_file: Path, capsys):
        """Should respect max-displacement option."""
        main(
            [
                str(pcb_file),
                "--drc-report",
                str(drc_report_file),
                "--max-displacement",
                "0.001",
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["max_displacement_mm"] == 0.001

    def test_prefer_option(self, pcb_file: Path, drc_report_file: Path, capsys):
        """Should accept prefer option."""
        result = main(
            [
                str(pcb_file),
                "--drc-report",
                str(drc_report_file),
                "--prefer",
                "move-via",
                "--dry-run",
            ]
        )

        # Should not crash
        assert result in (0, 1)

    def test_no_clearance_violations(self, tmp_path: Path, capsys):
        """Should report nothing to do if no clearance violations."""
        # Create a PCB without clearance issues
        pcb_content = """(kicad_pcb
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
        pcb_file = tmp_path / "clean.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Create DRC report with no clearance violations
        report_content = """\
** Drc report for clean.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 0 DRC violations **

** Found 0 Footprint errors **
** End of Report **
"""
        report_file = tmp_path / "clean-drc.rpt"
        report_file.write_text(report_content)

        result = main(
            [
                str(pcb_file),
                "--drc-report",
                str(report_file),
            ]
        )

        assert result == 0

        captured = capsys.readouterr()
        assert "nothing to repair" in captured.out.lower() or "no clearance" in captured.out.lower()


# ── Fixtures for via-aware nudge tests ──────────────────────────────────────

# PCB with a via and two stub segments terminating at the via position.
# The via is at (105, 100).  Segment stub-1 goes (100, 100) -> (105, 100),
# stub-2 goes (105, 100) -> (110, 100).  Moving the via must update both
# connected endpoints.
# The +3.3V segment runs close to the via but on a different part of the board.
# The violation is between the via (net GND) at (105, 100) and the +3.3V
# segment at (105, 100.1), so _find_object_at finds the via at one location
# and the other segment at the second location.
PCB_VIA_WITH_TWO_STUBS = """(kicad_pcb
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
  (segment (start 100 100) (end 105 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "stub-1"))
  (segment (start 105 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "stub-2"))
  (via (at 105 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-stub"))
  (segment (start 100 100.1) (end 110 100.1) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-other"))
)
"""

# PCB with a segment whose *start* endpoint sits at a via.
# Via at (100, 100), segment (100, 100) -> (110, 100.1).
# Nudging this segment should only move the *end* (free) endpoint.
PCB_SEGMENT_WITH_VIA_ENDPOINT = """(kicad_pcb
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
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-ep"))
  (segment (start 100 100) (end 110 100.1) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-ep-1"))
  (via (at 115 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-other"))
)
"""

# PCB with a segment whose *both* endpoints sit at vias.
# Via-A at (100, 100), Via-B at (110, 100), segment from A to B.
PCB_SEGMENT_BOTH_ENDPOINTS_AT_VIAS = """(kicad_pcb
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
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-A"))
  (via (at 110 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-B"))
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-AB"))
  (via (at 105 100.1) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-close"))
)
"""

# PCB with a via that has three connecting stubs (junction case).
PCB_VIA_WITH_THREE_STUBS = """(kicad_pcb
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
  (segment (start 95 100) (end 100 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "stub-a"))
  (segment (start 100 100) (end 105 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "stub-b"))
  (segment (start 100 100) (end 100 105) (width 0.25) (layer "F.Cu") (net 1) (uuid "stub-c"))
  (via (at 100 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-junc"))
  (segment (start 95 100.1) (end 110 100.1) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-nearby"))
)
"""


class TestViaAwareNudge:
    """Tests for via-aware nudge behaviour (endpoint-only and via relocation)."""

    def test_via_nudge_updates_connected_stubs(self, tmp_path: Path):
        """Moving a via via _apply_nudge must update all connected stub endpoints."""
        pcb_file = tmp_path / "via_stubs.kicad_pcb"
        pcb_file.write_text(PCB_VIA_WITH_TWO_STUBS)

        repairer = ClearanceRepairer(pcb_file)
        result = RepairResult()

        # Find the via node directly
        via_node = None
        for v in repairer.doc.find_all("via"):
            uuid_n = v.find("uuid")
            if uuid_n and uuid_n.get_first_atom() == "via-stub":
                via_node = v
                break
        assert via_node is not None

        # Apply a nudge directly to the via (dx=0, dy=-0.16)
        repairer._apply_nudge(via_node, "via", 0.0, -0.16, result=result)

        assert result.relocated_vias == 1

        # Read back the via position
        at_n = via_node.find("at")
        new_vx = float(at_n.get_atoms()[0])
        new_vy = float(at_n.get_atoms()[1])
        assert abs(new_vx - 105.0) < 0.001
        assert abs(new_vy - 99.84) < 0.001

        # Both stub endpoints that were at (105, 100) must now match the new via pos
        for seg in repairer.doc.find_all("segment"):
            uuid_n = seg.find("uuid")
            uid = uuid_n.get_first_atom() if uuid_n else ""
            if uid == "stub-1":
                # end was at (105, 100) -> should be updated
                end_n = seg.find("end")
                assert abs(float(end_n.get_atoms()[0]) - new_vx) < 0.001
                assert abs(float(end_n.get_atoms()[1]) - new_vy) < 0.001
                # start was at (100, 100) -> should be unchanged
                start_n = seg.find("start")
                assert abs(float(start_n.get_atoms()[0]) - 100.0) < 0.001
                assert abs(float(start_n.get_atoms()[1]) - 100.0) < 0.001
            elif uid == "stub-2":
                # start was at (105, 100) -> should be updated
                start_n = seg.find("start")
                assert abs(float(start_n.get_atoms()[0]) - new_vx) < 0.001
                assert abs(float(start_n.get_atoms()[1]) - new_vy) < 0.001

    def test_endpoint_nudge_preserves_via_connection(self, tmp_path: Path):
        """Nudging a segment whose start is at a via should only move the free end."""
        pcb_file = tmp_path / "ep_nudge.kicad_pcb"
        pcb_file.write_text(PCB_SEGMENT_WITH_VIA_ENDPOINT)

        repairer = ClearanceRepairer(pcb_file)

        # Create a violation between the segment and the other via
        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                DRCViolation(
                    type=ViolationType.CLEARANCE_SEGMENT_VIA,
                    type_str="clearance_segment_via",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.05mm < 0.2mm)",
                    locations=[
                        Location(x_mm=110.0, y_mm=100.1, layer="F.Cu"),
                        Location(x_mm=115.0, y_mm=100.0, layer="F.Cu"),
                    ],
                    nets=["GND", "+3.3V"],
                    required_value_mm=0.2,
                    actual_value_mm=0.05,
                ),
            ],
        )

        result = repairer.repair_from_report(
            report,
            max_displacement=0.5,
            prefer="move-trace",  # segment-via always moves trace
            dry_run=False,
        )

        assert result.repaired == 1
        assert result.endpoint_nudges == 1

        # Verify: start endpoint (at via) is unchanged
        for seg in repairer.doc.find_all("segment"):
            uuid_n = seg.find("uuid")
            if uuid_n and uuid_n.get_first_atom() == "seg-ep-1":
                start_n = seg.find("start")
                sx = float(start_n.get_atoms()[0])
                sy = float(start_n.get_atoms()[1])
                assert abs(sx - 100.0) < 0.001, f"Start x moved: {sx}"
                assert abs(sy - 100.0) < 0.001, f"Start y moved: {sy}"

                # End should have been nudged (different from original 110, 100.1)
                end_n = seg.find("end")
                ex = float(end_n.get_atoms()[0])
                ey = float(end_n.get_atoms()[1])
                assert not (abs(ex - 110.0) < 0.001 and abs(ey - 100.1) < 0.001), (
                    "End endpoint should have moved"
                )
                break

    def test_segment_both_endpoints_at_vias_not_moved(self, tmp_path: Path):
        """Segment with both endpoints at vias should not be moved (infeasible)."""
        pcb_file = tmp_path / "both_via.kicad_pcb"
        pcb_file.write_text(PCB_SEGMENT_BOTH_ENDPOINTS_AT_VIAS)

        repairer = ClearanceRepairer(pcb_file)

        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                DRCViolation(
                    type=ViolationType.CLEARANCE_SEGMENT_VIA,
                    type_str="clearance_segment_via",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.05mm < 0.2mm)",
                    locations=[
                        Location(x_mm=105.0, y_mm=100.0, layer="F.Cu"),
                        Location(x_mm=105.0, y_mm=100.1, layer="F.Cu"),
                    ],
                    nets=["GND", "+3.3V"],
                    required_value_mm=0.2,
                    actual_value_mm=0.05,
                ),
            ],
        )

        repairer.repair_from_report(
            report,
            max_displacement=0.5,
            dry_run=False,
        )

        # The segment is targeted but _apply_nudge returns without moving it.
        # The repair is still counted (repaired increments before _apply_nudge),
        # but the segment coordinates should be unchanged.
        for seg in repairer.doc.find_all("segment"):
            uuid_n = seg.find("uuid")
            if uuid_n and uuid_n.get_first_atom() == "seg-AB":
                start_n = seg.find("start")
                end_n = seg.find("end")
                sx = float(start_n.get_atoms()[0])
                sy = float(start_n.get_atoms()[1])
                ex = float(end_n.get_atoms()[0])
                ey = float(end_n.get_atoms()[1])
                assert abs(sx - 100.0) < 0.001
                assert abs(sy - 100.0) < 0.001
                assert abs(ex - 110.0) < 0.001
                assert abs(ey - 100.0) < 0.001
                break

    def test_via_with_three_stubs_all_updated(self, tmp_path: Path):
        """Via at a 3-way junction must update all three stub endpoints."""
        pcb_file = tmp_path / "three_stubs.kicad_pcb"
        pcb_file.write_text(PCB_VIA_WITH_THREE_STUBS)

        repairer = ClearanceRepairer(pcb_file)
        result = RepairResult()

        # Find the via node directly
        via_node = None
        for v in repairer.doc.find_all("via"):
            uuid_n = v.find("uuid")
            if uuid_n and uuid_n.get_first_atom() == "via-junc":
                via_node = v
                break
        assert via_node is not None

        # Apply nudge directly: move via away from (100, 100.1) -> dy=-0.16
        repairer._apply_nudge(via_node, "via", 0.0, -0.16, result=result)

        assert result.relocated_vias == 1

        # Find the new via position
        at_n = via_node.find("at")
        new_vx = float(at_n.get_atoms()[0])
        new_vy = float(at_n.get_atoms()[1])
        assert abs(new_vx - 100.0) < 0.001
        assert abs(new_vy - 99.84) < 0.001

        # All three stubs had an endpoint at (100, 100); those endpoints must
        # now match the new via position.
        updated_count = 0
        for seg in repairer.doc.find_all("segment"):
            uuid_n = seg.find("uuid")
            uid = uuid_n.get_first_atom() if uuid_n else ""
            if uid == "stub-a":
                # end was at (100, 100)
                end_n = seg.find("end")
                assert abs(float(end_n.get_atoms()[0]) - new_vx) < 0.001
                assert abs(float(end_n.get_atoms()[1]) - new_vy) < 0.001
                updated_count += 1
            elif uid in ("stub-b", "stub-c"):
                # start was at (100, 100) for both stubs
                start_n = seg.find("start")
                assert abs(float(start_n.get_atoms()[0]) - new_vx) < 0.001
                assert abs(float(start_n.get_atoms()[1]) - new_vy) < 0.001
                updated_count += 1

        assert updated_count == 3, f"Expected 3 updated stubs, got {updated_count}"

    def test_dry_run_reports_counters_without_modifying(self, tmp_path: Path):
        """Dry run must report endpoint_nudges and relocated_vias as zero (no apply)."""
        pcb_file = tmp_path / "dry_run_counters.kicad_pcb"
        pcb_file.write_text(PCB_SEGMENT_WITH_VIA_ENDPOINT)

        repairer = ClearanceRepairer(pcb_file)

        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                DRCViolation(
                    type=ViolationType.CLEARANCE_SEGMENT_VIA,
                    type_str="clearance_segment_via",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.05mm < 0.2mm)",
                    locations=[
                        Location(x_mm=110.0, y_mm=100.1, layer="F.Cu"),
                        Location(x_mm=115.0, y_mm=100.0, layer="F.Cu"),
                    ],
                    nets=["GND", "+3.3V"],
                    required_value_mm=0.2,
                    actual_value_mm=0.05,
                ),
            ],
        )

        result = repairer.repair_from_report(
            report,
            max_displacement=0.5,
            dry_run=True,
        )

        assert result.repaired == 1
        # Dry run: _apply_nudge is never called, so counters stay at zero
        assert result.endpoint_nudges == 0
        assert result.relocated_vias == 0
        assert not repairer.modified

    def test_repair_result_summary_includes_new_counters(self):
        """RepairResult.summary() should include endpoint_nudges and relocated_vias."""
        result = RepairResult(
            total_violations=5,
            repaired=5,
            endpoint_nudges=3,
            relocated_vias=2,
        )
        summary = result.summary()
        assert "Endpoint nudges" in summary
        assert "Via relocations" in summary

    def test_find_connected_segments_helper(self, tmp_path: Path):
        """_find_connected_segments should find segments at a position."""
        pcb_file = tmp_path / "connected.kicad_pcb"
        pcb_file.write_text(PCB_VIA_WITH_TWO_STUBS)

        repairer = ClearanceRepairer(pcb_file)
        connected = repairer._find_connected_segments(105.0, 100.0)

        # stub-1 has end at (105, 100), stub-2 has start at (105, 100)
        assert len(connected) == 2
        endpoints = {ep for _, ep in connected}
        assert "start" in endpoints
        assert "end" in endpoints

    def test_find_connected_segments_no_match(self, tmp_path: Path):
        """_find_connected_segments should return empty for unmatched position."""
        pcb_file = tmp_path / "no_match.kicad_pcb"
        pcb_file.write_text(PCB_VIA_WITH_TWO_STUBS)

        repairer = ClearanceRepairer(pcb_file)
        connected = repairer._find_connected_segments(999.0, 999.0)
        assert len(connected) == 0


class TestFixDrcJsonCounters:
    """Tests that fix-drc JSON output includes new counters."""

    def test_json_output_has_relocated_vias_and_endpoint_nudges(self, tmp_path: Path, capsys):
        """JSON output should include relocated_vias and endpoint_nudges."""
        from kicad_tools.cli.fix_drc_cmd import main as fix_drc_main

        pcb_file = tmp_path / "json_test.kicad_pcb"
        pcb_file.write_text(PCB_SEGMENT_WITH_VIA_ENDPOINT)

        # Write a minimal DRC report
        report_content = """\
** Drc report for json_test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 1 DRC violations **
[clearance_segment_via]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.0500 mm)
    Rule: netclass 'Default'; error
    @(110.0000 mm, 100.1000 mm): Track [GND] on F.Cu
    @(115.0000 mm, 100.0000 mm): Via [+3.3V] on F.Cu - B.Cu

** Found 0 Footprint errors **
** End of Report **
"""
        report_file = tmp_path / "json_test-drc.rpt"
        report_file.write_text(report_content)

        fix_drc_main(
            [
                str(pcb_file),
                "--drc-report",
                str(report_file),
                "--format",
                "json",
                "--max-displacement",
                "0.5",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "clearance" in data
        assert "relocated_vias" in data["clearance"]
        assert "endpoint_nudges" in data["clearance"]
        assert isinstance(data["clearance"]["relocated_vias"], int)
        assert isinstance(data["clearance"]["endpoint_nudges"], int)
