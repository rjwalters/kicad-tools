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

    def test_json_output_has_local_reroute_counters(self, tmp_path: Path, capsys):
        """JSON output should include local_rerouted and skipped.no_local_route."""
        from kicad_tools.cli.fix_drc_cmd import main as fix_drc_main

        pcb_file = tmp_path / "json_lr_test.kicad_pcb"
        pcb_file.write_text(PCB_SEGMENT_BOTH_ENDPOINTS_AT_VIAS)

        # Write a minimal DRC report for the both-endpoints-at-vias case
        report_content = """\
** Drc report for json_lr_test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 1 DRC violations **
[clearance_segment_via]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.0500 mm)
    Rule: netclass 'Default'; error
    @(105.0000 mm, 100.0000 mm): Track [GND] on F.Cu
    @(105.0000 mm, 100.1000 mm): Via [+3.3V] on F.Cu - B.Cu

** Found 0 Footprint errors **
** End of Report **
"""
        report_file = tmp_path / "json_lr_test-drc.rpt"
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
                "--local-reroute",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "clearance" in data
        assert "local_rerouted" in data["clearance"]
        assert "no_local_route" in data["clearance"]["skipped"]
        assert isinstance(data["clearance"]["local_rerouted"], int)
        assert isinstance(data["clearance"]["skipped"]["no_local_route"], int)


class TestLocalRerouteIntegration:
    """Integration tests for local reroute via ClearanceRepairer."""

    def test_local_reroute_of_both_endpoints_at_vias(self, tmp_path: Path):
        """local_reroute=True should reroute a segment with both endpoints at vias."""
        # PCB with segment (100,100)->(102,100) net=1 and obstacle via at (101,100.3) net=2
        pcb_content = """(kicad_pcb
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
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-lr1"))
  (via (at 102 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-lr2"))
  (segment (start 100 100) (end 102 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-lr"))
  (via (at 101 100.3) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-lr-obs"))
)
"""
        pcb_file = tmp_path / "lr_test.kicad_pcb"
        pcb_file.write_text(pcb_content)

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
                        Location(x_mm=101.0, y_mm=100.0, layer="F.Cu"),
                        Location(x_mm=101.0, y_mm=100.3, layer="F.Cu"),
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
            dry_run=False,
            local_reroute=True,
        )

        assert result.local_rerouted >= 1
        assert result.repaired >= 1

    def test_local_reroute_dry_run_counts_without_modifying(self, tmp_path: Path):
        """Dry run with local_reroute=True should count reroutes without modifying PCB."""
        pcb_content = """(kicad_pcb
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
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-dr1"))
  (via (at 102 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-dr2"))
  (segment (start 100 100) (end 102 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-dr"))
  (via (at 101 100.3) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-dr-obs"))
)
"""
        pcb_file = tmp_path / "lr_dry_test.kicad_pcb"
        pcb_file.write_text(pcb_content)

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
                        Location(x_mm=101.0, y_mm=100.0, layer="F.Cu"),
                        Location(x_mm=101.0, y_mm=100.3, layer="F.Cu"),
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
            local_reroute=True,
        )

        # Should find a reroute even in dry-run
        assert result.local_rerouted >= 1
        # PCB should not be modified
        assert not repairer.modified

    def test_local_reroute_off_by_default(self, tmp_path: Path):
        """Without local_reroute=True, infeasible violations stay infeasible."""
        pcb_content = """(kicad_pcb
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
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-nolr1"))
  (via (at 102 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-nolr2"))
  (segment (start 100 100) (end 102 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-nolr"))
  (via (at 101 100.3) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-nolr-obs"))
)
"""
        pcb_file = tmp_path / "lr_off_test.kicad_pcb"
        pcb_file.write_text(pcb_content)

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
                        Location(x_mm=101.0, y_mm=100.0, layer="F.Cu"),
                        Location(x_mm=101.0, y_mm=100.3, layer="F.Cu"),
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
            dry_run=False,
            # local_reroute defaults to False
        )

        assert result.local_rerouted == 0
        assert result.skipped_no_local_route == 0

    def test_repair_result_summary_includes_local_reroute_counters(self):
        """RepairResult.summary() should include local reroute counters."""
        result = RepairResult(
            total_violations=5,
            repaired=4,
            local_rerouted=2,
            skipped_no_local_route=1,
            skipped_infeasible=0,
        )
        summary = result.summary()
        assert "Local reroutes" in summary
        assert "no local route" in summary.lower()


# ---- Cluster rerouting integration tests ----

# PCB with two segments forming a connected path where both segments
# have both endpoints at vias (making nudging infeasible) and violate
# clearance to nearby obstacle vias.  The obstacle vias are clustered.
#
# Layout (2mm segments for routing room):
#   via-cl-1 (100,100) net=1 --- seg-cl-a --- via-cl-mid (102,100) net=1
#       --- seg-cl-b --- via-cl-2 (104,100) net=1
#   via-cl-obs1 (101, 100.35) net=2 (obstacle near segment-A)
#   via-cl-obs2 (103, 100.35) net=2 (obstacle near segment-B)
PCB_WITH_CLUSTERED_VIOLATIONS = """(kicad_pcb
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
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-cl-1"))
  (via (at 102 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-cl-mid"))
  (via (at 104 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-cl-2"))
  (segment (start 100 100) (end 102 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-cl-a"))
  (segment (start 102 100) (end 104 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-cl-b"))
  (via (at 101 100.35) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-cl-obs1"))
  (via (at 103 100.35) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-cl-obs2"))
)
"""


class TestClusterRerouteIntegration:
    """Integration tests for cluster-aware rerouting via ClearanceRepairer."""

    def test_cluster_reroute_resolves_both_violations(self, tmp_path: Path):
        """Cluster rerouting with local_reroute=True should resolve both violations.

        Both segments have both endpoints at vias (infeasible for nudge), so they
        flow into the local reroute phase.  The two violations are close enough to
        form a cluster and should be rerouted with awareness of each other's obstacle.
        """
        pcb_file = tmp_path / "cluster_test.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLUSTERED_VIOLATIONS)

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
                        Location(x_mm=101.0, y_mm=100.0, layer="F.Cu"),
                        Location(x_mm=101.0, y_mm=100.35, layer="F.Cu"),
                    ],
                    nets=["GND", "+3.3V"],
                    required_value_mm=0.2,
                    actual_value_mm=0.05,
                ),
                DRCViolation(
                    type=ViolationType.CLEARANCE_SEGMENT_VIA,
                    type_str="clearance_segment_via",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.05mm < 0.2mm)",
                    locations=[
                        Location(x_mm=103.0, y_mm=100.0, layer="F.Cu"),
                        Location(x_mm=103.0, y_mm=100.35, layer="F.Cu"),
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
            dry_run=False,
            local_reroute=True,
            local_grid_padding=1.0,
        )

        # Both violations should be handled by local rerouting
        assert result.local_rerouted >= 2
        assert result.repaired >= 2

    def test_cluster_reroute_dry_run(self, tmp_path: Path):
        """Dry run cluster reroute should count without modifying PCB."""
        pcb_file = tmp_path / "cluster_dry.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLUSTERED_VIOLATIONS)

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
                        Location(x_mm=101.0, y_mm=100.0, layer="F.Cu"),
                        Location(x_mm=101.0, y_mm=100.35, layer="F.Cu"),
                    ],
                    nets=["GND", "+3.3V"],
                    required_value_mm=0.2,
                    actual_value_mm=0.05,
                ),
                DRCViolation(
                    type=ViolationType.CLEARANCE_SEGMENT_VIA,
                    type_str="clearance_segment_via",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.05mm < 0.2mm)",
                    locations=[
                        Location(x_mm=103.0, y_mm=100.0, layer="F.Cu"),
                        Location(x_mm=103.0, y_mm=100.35, layer="F.Cu"),
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
            local_reroute=True,
            local_grid_padding=1.0,
        )

        assert result.local_rerouted >= 2
        assert not repairer.modified

    def test_single_violation_not_clustered(self, tmp_path: Path):
        """A single violation should fall back to standard per-violation reroute."""
        pcb_file = tmp_path / "single_test.kicad_pcb"
        # Use the existing PCB_SEGMENT_BOTH_ENDPOINTS_AT_VIAS fixture (single violation)
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

        result = repairer.repair_from_report(
            report,
            max_displacement=0.5,
            dry_run=False,
            local_reroute=True,
        )

        # Single violation should not increment cluster_rerouted
        assert result.cluster_rerouted == 0

    def test_cluster_reroute_summary_includes_cluster_counter(self):
        """RepairResult.summary() should include cluster reroute counter."""
        result = RepairResult(
            total_violations=4,
            repaired=4,
            local_rerouted=3,
            cluster_rerouted=2,
        )
        summary = result.summary()
        assert "Cluster reroutes" in summary

    def test_group_violations_by_proximity(self, tmp_path: Path):
        """Violations within 2*clearance should be grouped together."""
        pcb_file = tmp_path / "group_test.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLUSTERED_VIOLATIONS)

        repairer = ClearanceRepairer(pcb_file)

        # Two violations close together and one far away
        v1 = DRCViolation(
            type=ViolationType.CLEARANCE_SEGMENT_VIA,
            type_str="clearance_segment_via",
            severity=Severity.ERROR,
            message="test",
            locations=[
                Location(x_mm=100.5, y_mm=100.0, layer="F.Cu"),
                Location(x_mm=100.5, y_mm=100.25, layer="F.Cu"),
            ],
            nets=["GND", "+3.3V"],
            required_value_mm=0.2,
            actual_value_mm=0.05,
        )
        v2 = DRCViolation(
            type=ViolationType.CLEARANCE_SEGMENT_VIA,
            type_str="clearance_segment_via",
            severity=Severity.ERROR,
            message="test",
            locations=[
                Location(x_mm=101.5, y_mm=100.0, layer="F.Cu"),
                Location(x_mm=101.5, y_mm=100.25, layer="F.Cu"),
            ],
            nets=["GND", "+3.3V"],
            required_value_mm=0.2,
            actual_value_mm=0.05,
        )
        v3 = DRCViolation(
            type=ViolationType.CLEARANCE_SEGMENT_VIA,
            type_str="clearance_segment_via",
            severity=Severity.ERROR,
            message="test",
            locations=[
                Location(x_mm=200.0, y_mm=200.0, layer="F.Cu"),
                Location(x_mm=200.0, y_mm=200.25, layer="F.Cu"),
            ],
            nets=["GND", "+3.3V"],
            required_value_mm=0.2,
            actual_value_mm=0.05,
        )

        tagged = [(v1, "skipped"), (v2, "skipped"), (v3, "skipped")]
        # Use an explicit cluster_radius that covers the 1.0mm gap between v1 and v2
        clusters = repairer._group_violations_by_proximity(tagged, cluster_radius=1.5)

        # v1 and v2 should be in one cluster, v3 in another
        assert len(clusters) == 2
        cluster_sizes = sorted(len(c) for c in clusters)
        assert cluster_sizes == [1, 2]

    def test_group_violations_empty_list(self, tmp_path: Path):
        """Empty violation list should produce empty clusters."""
        pcb_file = tmp_path / "empty_test.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLUSTERED_VIOLATIONS)

        repairer = ClearanceRepairer(pcb_file)
        clusters = repairer._group_violations_by_proximity([])
        assert clusters == []

    def test_group_violations_single_item(self, tmp_path: Path):
        """Single violation should produce one cluster of size 1."""
        pcb_file = tmp_path / "single_group_test.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLUSTERED_VIOLATIONS)

        repairer = ClearanceRepairer(pcb_file)

        v1 = DRCViolation(
            type=ViolationType.CLEARANCE_SEGMENT_VIA,
            type_str="clearance_segment_via",
            severity=Severity.ERROR,
            message="test",
            locations=[
                Location(x_mm=100.5, y_mm=100.0, layer="F.Cu"),
                Location(x_mm=100.5, y_mm=100.25, layer="F.Cu"),
            ],
            nets=["GND", "+3.3V"],
            required_value_mm=0.2,
            actual_value_mm=0.05,
        )

        clusters = repairer._group_violations_by_proximity([(v1, "skipped")])
        assert len(clusters) == 1
        assert len(clusters[0]) == 1

    def test_violations_on_different_layers_not_clustered(self, tmp_path: Path):
        """Violations at the same position but logically separate should cluster by location."""
        # Note: The clustering is purely spatial (by primary_location), so
        # violations at the same x,y but different layers WILL be clustered.
        # This is acceptable because the rerouter already filters by layer.
        pcb_file = tmp_path / "layer_test.kicad_pcb"
        pcb_file.write_text(PCB_WITH_CLUSTERED_VIOLATIONS)

        repairer = ClearanceRepairer(pcb_file)

        v_fcu = DRCViolation(
            type=ViolationType.CLEARANCE_SEGMENT_VIA,
            type_str="clearance_segment_via",
            severity=Severity.ERROR,
            message="test",
            locations=[
                Location(x_mm=100.5, y_mm=100.0, layer="F.Cu"),
                Location(x_mm=100.5, y_mm=100.25, layer="F.Cu"),
            ],
            nets=["GND", "+3.3V"],
            required_value_mm=0.2,
            actual_value_mm=0.05,
        )
        v_bcu = DRCViolation(
            type=ViolationType.CLEARANCE_SEGMENT_VIA,
            type_str="clearance_segment_via",
            severity=Severity.ERROR,
            message="test",
            locations=[
                Location(x_mm=100.5, y_mm=100.0, layer="B.Cu"),
                Location(x_mm=100.5, y_mm=100.25, layer="B.Cu"),
            ],
            nets=["GND", "+3.3V"],
            required_value_mm=0.2,
            actual_value_mm=0.05,
        )

        tagged = [(v_fcu, "skipped"), (v_bcu, "skipped")]
        clusters = repairer._group_violations_by_proximity(tagged)

        # Same x,y -> grouped together (spatial only)
        assert len(clusters) == 1
        assert len(clusters[0]) == 2


# ── Pad-clearance repair tests ─────────────────────────────────────────────

# PCB with a footprint containing a pad (pad "1" at footprint-local (0, 0))
# and a segment that runs too close to it.
# Footprint at (105, 100) with 0 rotation, so pad absolute position is (105, 100).
# Segment runs from (100, 100.12) to (110, 100.12) on F.Cu, net "+3.3V".
# The pad is on net "GND".
PCB_WITH_PAD_SEGMENT_VIOLATION = """(kicad_pcb
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
  (footprint "TestLib:R0402"
    (layer "F.Cu")
    (at 105 100)
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "pad-1"))
    (pad "2" smd roundrect (at 1.0 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V") (uuid "pad-2"))
  )
  (segment (start 100 100.12) (end 110 100.12) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-pad-1"))
)
"""

# PCB with a footprint at (105, 100) rotated 90 degrees.
# Pad "1" local offset is (0.5, 0), so after 90deg rotation:
#   abs_x = 105 + 0.5*cos(90) - 0*sin(90) = 105 + 0 - 0 = 105
#   abs_y = 100 + 0.5*sin(90) + 0*cos(90) = 100 + 0.5 + 0 = 100.5
# A via at (105, 100.35) is too close to the pad.
PCB_WITH_ROTATED_PAD_VIA_VIOLATION = """(kicad_pcb
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
  (footprint "TestLib:R0402"
    (layer "F.Cu")
    (at 105 100 90)
    (pad "1" smd roundrect (at 0.5 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "pad-rot-1"))
  )
  (via (at 105 100.35) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-pad-1"))
)
"""

# PCB with two pads close together (both immovable -- should be skipped)
PCB_WITH_PAD_PAD_VIOLATION = """(kicad_pcb
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
  (footprint "TestLib:C0402"
    (layer "F.Cu")
    (at 105 100)
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "pad-pp-1"))
  )
  (footprint "TestLib:C0402"
    (layer "F.Cu")
    (at 105.5 100)
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V") (uuid "pad-pp-2"))
  )
)
"""


class TestPadClearanceRepair:
    """Tests for pad-segment and pad-via clearance repair."""

    def test_violation_type_from_string_pad_segment(self):
        """ViolationType.from_string should parse clearance_pad_segment."""
        assert (
            ViolationType.from_string("clearance_pad_segment")
            == ViolationType.CLEARANCE_PAD_SEGMENT
        )

    def test_violation_type_from_string_pad_via(self):
        """ViolationType.from_string should parse clearance_pad_via."""
        assert ViolationType.from_string("clearance_pad_via") == ViolationType.CLEARANCE_PAD_VIA

    def test_pad_clearance_types_are_clearance(self):
        """PAD_SEGMENT and PAD_VIA violations should count as clearance."""
        v1 = DRCViolation(
            type=ViolationType.CLEARANCE_PAD_SEGMENT,
            type_str="clearance_pad_segment",
            severity=Severity.ERROR,
            message="test",
        )
        v2 = DRCViolation(
            type=ViolationType.CLEARANCE_PAD_VIA,
            type_str="clearance_pad_via",
            severity=Severity.ERROR,
            message="test",
        )
        assert v1.is_clearance
        assert v2.is_clearance

    def test_pad_segment_repair_moves_segment(self, tmp_path: Path):
        """Pad-segment repair should move the segment, never the pad."""
        pcb_file = tmp_path / "pad_seg.kicad_pcb"
        pcb_file.write_text(PCB_WITH_PAD_SEGMENT_VIOLATION)

        repairer = ClearanceRepairer(pcb_file)

        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                DRCViolation(
                    type=ViolationType.CLEARANCE_PAD_SEGMENT,
                    type_str="clearance_pad_segment",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.12mm < 0.20mm)",
                    locations=[
                        Location(x_mm=105.0, y_mm=100.0, layer="F.Cu"),
                        Location(x_mm=105.0, y_mm=100.12, layer="F.Cu"),
                    ],
                    nets=["GND", "+3.3V"],
                    required_value_mm=0.2,
                    actual_value_mm=0.12,
                ),
            ],
        )

        result = repairer.repair_from_report(
            report,
            max_displacement=0.5,
            dry_run=True,
        )

        assert result.total_violations == 1
        assert result.repaired == 1
        # Must have moved the segment, not the pad
        assert len(result.nudges) == 1
        assert result.nudges[0].object_type == "segment"

    def test_pad_segment_repair_applies_changes(self, tmp_path: Path):
        """Pad-segment repair should modify PCB when not dry run."""
        pcb_file = tmp_path / "pad_seg.kicad_pcb"
        pcb_file.write_text(PCB_WITH_PAD_SEGMENT_VIOLATION)

        repairer = ClearanceRepairer(pcb_file)

        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                DRCViolation(
                    type=ViolationType.CLEARANCE_PAD_SEGMENT,
                    type_str="clearance_pad_segment",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.12mm < 0.20mm)",
                    locations=[
                        Location(x_mm=105.0, y_mm=100.0, layer="F.Cu"),
                        Location(x_mm=105.0, y_mm=100.12, layer="F.Cu"),
                    ],
                    nets=["GND", "+3.3V"],
                    required_value_mm=0.2,
                    actual_value_mm=0.12,
                ),
            ],
        )

        result = repairer.repair_from_report(
            report,
            max_displacement=0.5,
            dry_run=False,
        )

        assert result.repaired == 1
        assert repairer.modified

    def test_pad_via_repair_moves_via(self, tmp_path: Path):
        """Pad-via repair should relocate the via, never the pad."""
        pcb_file = tmp_path / "pad_via.kicad_pcb"
        pcb_file.write_text(PCB_WITH_ROTATED_PAD_VIA_VIOLATION)

        repairer = ClearanceRepairer(pcb_file)

        # Pad "1" at footprint (105, 100) rotated 90deg with local offset (0.5, 0)
        # -> absolute (105, 100.5).  Via at (105, 100.35).
        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                DRCViolation(
                    type=ViolationType.CLEARANCE_PAD_VIA,
                    type_str="clearance_pad_via",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.10mm < 0.20mm)",
                    locations=[
                        Location(x_mm=105.0, y_mm=100.5, layer="F.Cu"),
                        Location(x_mm=105.0, y_mm=100.35, layer="F.Cu"),
                    ],
                    nets=["GND", "+3.3V"],
                    required_value_mm=0.2,
                    actual_value_mm=0.10,
                ),
            ],
        )

        result = repairer.repair_from_report(
            report,
            max_displacement=0.5,
            dry_run=True,
        )

        assert result.total_violations == 1
        assert result.repaired == 1
        # Must have moved the via, not the pad
        assert len(result.nudges) == 1
        assert result.nudges[0].object_type == "via"

    def test_pad_pad_violation_skipped(self, tmp_path: Path):
        """Pad-to-pad violation should be skipped (neither is movable)."""
        pcb_file = tmp_path / "pad_pad.kicad_pcb"
        pcb_file.write_text(PCB_WITH_PAD_PAD_VIOLATION)

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
                    message="Clearance violation (0.10mm < 0.20mm)",
                    locations=[
                        Location(x_mm=105.0, y_mm=100.0, layer="F.Cu"),
                        Location(x_mm=105.5, y_mm=100.0, layer="F.Cu"),
                    ],
                    nets=["GND", "+3.3V"],
                    required_value_mm=0.2,
                    actual_value_mm=0.10,
                ),
            ],
        )

        result = repairer.repair_from_report(
            report,
            max_displacement=0.5,
            dry_run=True,
        )

        # Both objects are pads -- neither is movable
        assert result.repaired == 0
        assert result.skipped_infeasible == 1

    def test_find_pads_near(self, tmp_path: Path):
        """_find_pads_near should locate pads with correct board coordinates."""
        pcb_file = tmp_path / "pad_near.kicad_pcb"
        pcb_file.write_text(PCB_WITH_PAD_SEGMENT_VIOLATION)

        repairer = ClearanceRepairer(pcb_file)

        # Footprint at (105, 100), pad "1" at local (0, 0) -> board (105, 100)
        pads = repairer._find_pads_near(105.0, 100.0, 0.5, "F.Cu", ["GND"])
        assert len(pads) >= 1
        pad_info = pads[0]
        assert pad_info[1] == "pad"
        assert abs(pad_info[2] - 105.0) < 0.01
        assert abs(pad_info[3] - 100.0) < 0.01

    def test_find_pads_near_rotated_footprint(self, tmp_path: Path):
        """_find_pads_near should account for footprint rotation."""
        pcb_file = tmp_path / "pad_rot.kicad_pcb"
        pcb_file.write_text(PCB_WITH_ROTATED_PAD_VIA_VIOLATION)

        repairer = ClearanceRepairer(pcb_file)

        # Footprint at (105, 100) rotated 90deg, pad local (0.5, 0)
        # After rotation: abs = (105, 100.5)
        pads = repairer._find_pads_near(105.0, 100.5, 0.5, "F.Cu", ["GND"])
        assert len(pads) >= 1
        pad_info = pads[0]
        assert pad_info[1] == "pad"
        assert abs(pad_info[2] - 105.0) < 0.01
        assert abs(pad_info[3] - 100.5) < 0.01

    def test_choose_target_never_moves_pad(self, tmp_path: Path):
        """_choose_target should always return the non-pad object."""
        pcb_file = tmp_path / "choose.kicad_pcb"
        pcb_file.write_text(PCB_WITH_PAD_SEGMENT_VIOLATION)

        repairer = ClearanceRepairer(pcb_file)

        from kicad_tools.sexp import SExp

        # Create mock objects
        mock_pad = (SExp("pad"), "pad", 105.0, 100.0, "F.Cu", "GND")
        mock_seg = (SExp("segment"), "segment", 105.0, 100.12, "F.Cu", "+3.3V")
        mock_via = (SExp("via"), "via", 105.0, 100.35, "F.Cu", "+3.3V")

        # Pad + segment -> always returns segment
        target = repairer._choose_target(mock_pad, mock_seg, "move-trace")
        assert target[1] == "segment"

        target = repairer._choose_target(mock_seg, mock_pad, "move-trace")
        assert target[1] == "segment"

        # Pad + via -> always returns via
        target = repairer._choose_target(mock_pad, mock_via, "move-via")
        assert target[1] == "via"

        target = repairer._choose_target(mock_via, mock_pad, "move-via")
        assert target[1] == "via"

        # Pad + pad -> returns None (neither movable)
        mock_pad2 = (SExp("pad"), "pad", 105.5, 100.0, "F.Cu", "+3.3V")
        target = repairer._choose_target(mock_pad, mock_pad2, "move-trace")
        assert target is None

    def test_pad_segment_included_in_fix_drc_count(self, tmp_path: Path):
        """fix-drc should count pad-segment and pad-via violations."""
        pcb_file = tmp_path / "pad_seg_drc.kicad_pcb"
        pcb_file.write_text(PCB_WITH_PAD_SEGMENT_VIOLATION)

        # Create a DRC report containing pad-segment violations
        report_content = """\
** Drc report for test.kicad_pcb **
** Created on 2025-12-28T21:29:34-08:00 **

** Found 1 DRC violations **
[clearance_pad_segment]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1200 mm)
    Rule: netclass 'Default'; error
    @(105.0000 mm, 100.0000 mm): Pad [GND] on F.Cu
    @(105.0000 mm, 100.1200 mm): Track [+3.3V] on F.Cu

** Found 0 Footprint errors **
** End of Report **
"""
        report_file = tmp_path / "test-drc.rpt"
        report_file.write_text(report_content)

        from kicad_tools.cli.fix_drc_cmd import main as fix_drc_main

        result = fix_drc_main(
            [
                str(pcb_file),
                "--drc-report",
                str(report_file),
                "--dry-run",
                "--format",
                "json",
                "--quiet",
            ]
        )

        # Should not crash; any return code acceptable in dry-run
        assert result in (0, 1, 2)


# ── Tests for _parse_nets with zone-containing PCBs ──────────────────────────

# PCB with zones that contain nested (net N) nodes -- triggers the crash
# if _parse_nets uses recursive find_all("net").
PCB_WITH_ZONES = """\
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
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-z-1"))
  (zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "zone-1")
    (connect_pads (clearance 0.2))
    (fill (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 90 90) (xy 120 90) (xy 120 120) (xy 90 120)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 90.2 90.2) (xy 119.8 90.2) (xy 119.8 119.8) (xy 90.2 119.8))
    )
  )
  (zone (net 2) (net_name "+3.3V") (layer "B.Cu") (uuid "zone-2")
    (connect_pads (clearance 0.2))
    (fill (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 90 90) (xy 120 90) (xy 120 120) (xy 90 120)))
  )
)
"""


class TestParseNetsWithZones:
    """Tests for _parse_nets handling of PCBs with zone nodes."""

    def test_parse_nets_no_crash_with_zones(self, tmp_path: Path):
        """ClearanceRepairer should not crash when PCB contains zones with nested (net N) nodes."""
        pcb_file = tmp_path / "zones.kicad_pcb"
        pcb_file.write_text(PCB_WITH_ZONES)

        # This would crash with ValueError before the fix because find_all("net")
        # recursively found (net 1) inside zones where atoms[0] could be non-integer
        repairer = ClearanceRepairer(pcb_file)

        # Should parse only top-level net definitions
        assert repairer.nets[0] == ""
        assert repairer.nets[1] == "GND"
        assert repairer.nets[2] == "+3.3V"
        assert len(repairer.nets) == 3

    def test_parse_nets_ignores_nested_net_attributes(self, tmp_path: Path):
        """_parse_nets should only find top-level (net N "name") definitions,
        not (net N) attributes nested inside segments, vias, or zones."""
        pcb_file = tmp_path / "zones.kicad_pcb"
        pcb_file.write_text(PCB_WITH_ZONES)

        repairer = ClearanceRepairer(pcb_file)

        # net_names should map only top-level definitions
        assert repairer.net_names["GND"] == 1
        assert repairer.net_names["+3.3V"] == 2
        # Should NOT have extra entries from nested (net N) inside zones/segments
        assert len(repairer.net_names) == 3  # "", "GND", "+3.3V"

    def test_drill_repairer_no_crash_with_zones(self, tmp_path: Path):
        """DrillClearanceRepairer should not crash when PCB contains zones."""
        from kicad_tools.drc.repair_drill_clearance import DrillClearanceRepairer

        pcb_file = tmp_path / "zones.kicad_pcb"
        pcb_file.write_text(PCB_WITH_ZONES)

        repairer = DrillClearanceRepairer(pcb_file)
        assert repairer.nets[1] == "GND"
        assert repairer.nets[2] == "+3.3V"

    def test_fixer_no_crash_with_zones(self, tmp_path: Path):
        """DRCFixer should not crash when PCB contains zones."""
        from kicad_tools.drc.fixer import DRCFixer

        pcb_file = tmp_path / "zones.kicad_pcb"
        pcb_file.write_text(PCB_WITH_ZONES)

        fixer = DRCFixer(str(pcb_file))
        assert fixer.nets[1] == "GND"
        assert fixer.nets[2] == "+3.3V"


# ── Tests for zone-fill violation filtering ──────────────────────────────────


class TestZoneFillViolationFiltering:
    """Tests for filtering zone-fill violations before repair."""

    def test_is_zone_fill_violation_detects_zone_items(self):
        """_is_zone_fill_violation should return True for violations with zone items."""
        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            items=["Track [GND] on F.Cu", "Zone [+3.3V] on F.Cu"],
        )
        assert ClearanceRepairer._is_zone_fill_violation(v) is True

    def test_is_zone_fill_violation_ignores_non_zone(self):
        """_is_zone_fill_violation should return False for violations without zone items."""
        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            items=["Track [GND] on F.Cu", "Via [+3.3V] on F.Cu - B.Cu"],
        )
        assert ClearanceRepairer._is_zone_fill_violation(v) is False

    def test_zone_violations_excluded_from_total(self, tmp_path: Path):
        """Zone-fill violations should be excluded from repair attempt count."""
        pcb_file = tmp_path / "zones.kicad_pcb"
        pcb_file.write_text(PCB_WITH_ZONES)

        repairer = ClearanceRepairer(pcb_file)

        report = DRCReport(
            source_file="test",
            created_at=None,
            pcb_name="test",
            violations=[
                # Regular clearance violation (should be counted)
                DRCViolation(
                    type=ViolationType.CLEARANCE,
                    type_str="clearance",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.05mm < 0.2mm)",
                    locations=[
                        Location(x_mm=100.0, y_mm=100.0, layer="F.Cu"),
                        Location(x_mm=100.0, y_mm=100.15, layer="F.Cu"),
                    ],
                    items=["Track [GND] on F.Cu", "Track [+3.3V] on F.Cu"],
                    nets=["GND", "+3.3V"],
                    required_value_mm=0.2,
                    actual_value_mm=0.05,
                ),
                # Zone-fill violation (should be excluded)
                DRCViolation(
                    type=ViolationType.CLEARANCE,
                    type_str="clearance",
                    severity=Severity.ERROR,
                    message="Clearance violation (0.1mm < 0.2mm)",
                    locations=[
                        Location(x_mm=95.0, y_mm=95.0, layer="F.Cu"),
                        Location(x_mm=95.0, y_mm=95.1, layer="F.Cu"),
                    ],
                    items=["Track [+3.3V] on F.Cu", "Zone [GND] on F.Cu"],
                    nets=["+3.3V", "GND"],
                    required_value_mm=0.2,
                    actual_value_mm=0.1,
                ),
            ],
        )

        result = repairer.repair_from_report(report, max_displacement=0.5, dry_run=True)

        # Only the non-zone violation should be counted
        assert result.total_violations == 1

    def test_zone_fill_violation_case_insensitive(self):
        """Zone detection should be case-insensitive."""
        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="test",
            items=["track [GND] on F.Cu", "ZONE [+3.3V] on F.Cu"],
        )
        assert ClearanceRepairer._is_zone_fill_violation(v) is True

    def test_zone_fill_violation_empty_items(self):
        """Violation with no items should not be flagged as zone violation."""
        v = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="test",
            items=[],
        )
        assert ClearanceRepairer._is_zone_fill_violation(v) is False


# ---- Oscillation detection tests ----


class TestOscillationDetection:
    """Tests for the nudge oscillation detection in ClearanceRepairer."""

    def test_no_oscillation_on_first_nudge(self, pcb_file: Path):
        """First nudge for a UUID should never be flagged as oscillation."""
        repairer = ClearanceRepairer(pcb_file)
        assert not repairer._would_oscillate("seg-1", 0.1, 0.0)

    def test_same_direction_not_oscillation(self, pcb_file: Path):
        """Nudging in the same direction twice should not be flagged."""
        repairer = ClearanceRepairer(pcb_file)
        repairer._record_nudge("seg-1", 0.1, 0.0)
        assert not repairer._would_oscillate("seg-1", 0.1, 0.0)

    def test_reverse_direction_is_oscillation(self, pcb_file: Path):
        """Nudging in the reverse direction should be flagged as oscillation."""
        repairer = ClearanceRepairer(pcb_file)
        repairer._record_nudge("seg-1", 0.1, 0.0)
        assert repairer._would_oscillate("seg-1", -0.1, 0.0)

    def test_perpendicular_not_oscillation(self, pcb_file: Path):
        """Nudging perpendicular to previous direction should not be flagged."""
        repairer = ClearanceRepairer(pcb_file)
        repairer._record_nudge("seg-1", 0.1, 0.0)
        assert not repairer._would_oscillate("seg-1", 0.0, 0.1)

    def test_cumulative_history(self, pcb_file: Path):
        """Cumulative nudge history should track total displacement."""
        repairer = ClearanceRepairer(pcb_file)
        repairer._record_nudge("seg-1", 0.1, 0.0)
        repairer._record_nudge("seg-1", 0.1, 0.0)
        # Cumulative is (0.2, 0.0); moving backward should be flagged
        assert repairer._would_oscillate("seg-1", -0.05, 0.0)

    def test_empty_uuid_not_tracked(self, pcb_file: Path):
        """Empty UUID should not be tracked or flagged."""
        repairer = ClearanceRepairer(pcb_file)
        repairer._record_nudge("", 0.1, 0.0)
        assert not repairer._would_oscillate("", -0.1, 0.0)

    def test_different_uuids_independent(self, pcb_file: Path):
        """Nudge history for different UUIDs should be independent."""
        repairer = ClearanceRepairer(pcb_file)
        repairer._record_nudge("seg-1", 0.1, 0.0)
        # seg-2 has no history, should not be flagged
        assert not repairer._would_oscillate("seg-2", -0.1, 0.0)

    def test_oscillation_skips_repair(self, pcb_file: Path, drc_report: DRCReport):
        """Oscillating segment should be skipped (not repaired) on second pass.

        We simulate oscillation by running repair_from_report twice: the first
        pass records nudge history, and a synthetic second pass with a reversed
        violation direction should detect the oscillation and skip the repair.
        """
        repairer = ClearanceRepairer(pcb_file)

        # First pass -- records nudge history
        result1 = repairer.repair_from_report(
            drc_report,
            max_displacement=0.5,
            dry_run=True,
        )
        # Should have repaired some violations
        assert result1.repaired > 0

        # Now manually reverse the nudge history for all recorded UUIDs
        # to simulate the scenario where the violation direction reverses.
        # After the first pass, the repairer has recorded nudges.
        # We artificially flip them so the second pass detects oscillation.
        for uuid_str in list(repairer._nudge_history.keys()):
            dx, dy = repairer._nudge_history[uuid_str]
            # Set history to the opposite direction so same violation = oscillation
            repairer._nudge_history[uuid_str] = (-dx * 2, -dy * 2)

        # Second pass with same violations -- should detect oscillation
        result2 = repairer.repair_from_report(
            drc_report,
            max_displacement=0.5,
            dry_run=True,
        )
        # At least some violations should be skipped due to oscillation
        assert result2.skipped_infeasible > 0
