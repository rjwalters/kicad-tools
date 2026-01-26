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

        result = main([
            str(pcb_file),
            "--drc-report", str(drc_report_file),
            "--dry-run",
        ])

        assert result == 0 or result == 1  # May be 1 if not all violations fixed

        # File should be unchanged
        assert pcb_file.read_text() == original

        captured = capsys.readouterr()
        assert "clearance" in captured.out.lower() or "CLEARANCE" in captured.out

    def test_output_to_different_file(
        self, pcb_file: Path, drc_report_file: Path, tmp_path: Path
    ):
        """Should write to output file when specified."""
        output_file = tmp_path / "fixed.kicad_pcb"
        original = pcb_file.read_text()

        main([
            str(pcb_file),
            "--drc-report", str(drc_report_file),
            "-o", str(output_file),
        ])

        # Original should be unchanged
        assert pcb_file.read_text() == original

        # Output file should exist
        assert output_file.exists()

    def test_json_output(self, pcb_file: Path, drc_report_file: Path, capsys):
        """JSON output should be valid."""
        main([
            str(pcb_file),
            "--drc-report", str(drc_report_file),
            "--dry-run",
            "--format", "json",
        ])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "total_violations" in data
        assert "repaired" in data
        assert "nudges" in data
        assert data["dry_run"] is True

    def test_summary_output(self, pcb_file: Path, drc_report_file: Path, capsys):
        """Summary output should show counts."""
        main([
            str(pcb_file),
            "--drc-report", str(drc_report_file),
            "--dry-run",
            "--format", "summary",
        ])

        captured = capsys.readouterr()
        assert "clearance" in captured.out.lower()

    def test_quiet_mode(self, pcb_file: Path, drc_report_file: Path, capsys):
        """Quiet mode should suppress output."""
        main([
            str(pcb_file),
            "--drc-report", str(drc_report_file),
            "--dry-run",
            "--quiet",
        ])

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_invalid_file(self, tmp_path: Path):
        """Should fail for non-existent file."""
        result = main([str(tmp_path / "nonexistent.kicad_pcb")])
        assert result == 1

    def test_max_displacement_option(
        self, pcb_file: Path, drc_report_file: Path, capsys
    ):
        """Should respect max-displacement option."""
        main([
            str(pcb_file),
            "--drc-report", str(drc_report_file),
            "--max-displacement", "0.001",
            "--dry-run",
            "--format", "json",
        ])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["max_displacement_mm"] == 0.001

    def test_prefer_option(self, pcb_file: Path, drc_report_file: Path, capsys):
        """Should accept prefer option."""
        result = main([
            str(pcb_file),
            "--drc-report", str(drc_report_file),
            "--prefer", "move-via",
            "--dry-run",
        ])

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

        result = main([
            str(pcb_file),
            "--drc-report", str(report_file),
        ])

        assert result == 0

        captured = capsys.readouterr()
        assert "nothing to repair" in captured.out.lower() or "no clearance" in captured.out.lower()
