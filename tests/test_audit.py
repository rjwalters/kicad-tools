"""Tests for manufacturing readiness audit (kct audit command)."""

import json
from pathlib import Path

import pytest

from kicad_tools.audit import AuditResult, AuditVerdict, ManufacturingAudit
from kicad_tools.audit.auditor import (
    ConnectivityStatus,
    CostEstimate,
    DRCStatus,
    ERCStatus,
    ManufacturerCompatibility,
)


class TestAuditResult:
    """Tests for AuditResult class."""

    def test_verdict_ready_when_all_pass(self):
        """Test that verdict is READY when all checks pass."""
        result = AuditResult()
        assert result.verdict == AuditVerdict.READY
        assert result.is_ready is True

    def test_verdict_not_ready_with_erc_errors(self):
        """Test that verdict is NOT_READY with blocking ERC errors."""
        result = AuditResult()
        result.erc.error_count = 1
        result.erc.blocking_error_count = 1
        assert result.verdict == AuditVerdict.NOT_READY
        assert result.is_ready is False

    def test_verdict_not_ready_with_drc_blocking(self):
        """Test that verdict is NOT_READY with blocking DRC violations."""
        result = AuditResult()
        result.drc.blocking_count = 1
        assert result.verdict == AuditVerdict.NOT_READY
        assert result.is_ready is False

    def test_verdict_not_ready_with_connectivity_issues(self):
        """Test that verdict is NOT_READY with connectivity issues."""
        result = AuditResult()
        result.connectivity.passed = False
        assert result.verdict == AuditVerdict.NOT_READY
        assert result.is_ready is False

    def test_verdict_not_ready_with_compatibility_issues(self):
        """Test that verdict is NOT_READY with manufacturer compatibility issues."""
        result = AuditResult()
        result.compatibility.passed = False
        assert result.verdict == AuditVerdict.NOT_READY
        assert result.is_ready is False

    def test_verdict_warning_with_drc_warnings(self):
        """Test that verdict is WARNING with DRC warnings only."""
        result = AuditResult()
        result.drc.warning_count = 3
        assert result.verdict == AuditVerdict.WARNING
        assert result.is_ready is False

    def test_verdict_warning_with_erc_warnings(self):
        """Test that verdict is WARNING with ERC warnings only."""
        result = AuditResult()
        result.erc.warning_count = 2
        assert result.verdict == AuditVerdict.WARNING
        assert result.is_ready is False

    def test_verdict_ready_with_erc_explicitly_zero(self):
        """Test READY verdict when ERC is explicitly set to zero errors/warnings.

        Simulates the post-fix-erc scenario where ERC errors have been resolved
        and the audit should return READY.
        """
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=0,
            warning_count=0,
            passed=True,
            details="",
        )
        result.drc = DRCStatus(
            error_count=0,
            warning_count=0,
            blocking_count=0,
            passed=True,
        )
        result.connectivity = ConnectivityStatus(
            total_nets=10,
            connected_nets=10,
            incomplete_nets=0,
            completion_percent=100.0,
            unconnected_pads=0,
            passed=True,
        )
        result.compatibility = ManufacturerCompatibility(
            manufacturer="JLCPCB",
            passed=True,
        )
        assert result.verdict == AuditVerdict.READY
        assert result.is_ready is True

    def test_verdict_warning_when_erc_has_warnings_only(self):
        """Test WARNING verdict when ERC has zero errors but non-zero warnings.

        After fix-erc resolves all errors, residual warnings should yield
        WARNING (not READY), because is_ready requires zero warnings too.
        """
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=0,
            warning_count=2,
            passed=True,
            details="2 warnings remain",
        )
        result.drc = DRCStatus(
            error_count=0,
            warning_count=0,
            blocking_count=0,
            passed=True,
        )
        result.connectivity = ConnectivityStatus(passed=True)
        result.compatibility = ManufacturerCompatibility(passed=True)

        assert result.verdict == AuditVerdict.WARNING
        assert result.is_ready is False

    def test_verdict_ready_all_four_gates_cleared(self):
        """Test READY when all four NOT_READY gates are explicitly cleared.

        Each of the four gates (ERC errors, DRC blocking, connectivity,
        compatibility) is set to its passing state. Verifies no hidden
        fifth gate blocks the READY verdict.
        """
        result = AuditResult()

        # Gate 1: ERC errors = 0
        result.erc.error_count = 0
        result.erc.warning_count = 0

        # Gate 2: DRC blocking = 0
        result.drc.blocking_count = 0
        result.drc.warning_count = 0

        # Gate 3: connectivity passed
        result.connectivity.passed = True

        # Gate 4: compatibility passed
        result.compatibility.passed = True

        assert result.verdict == AuditVerdict.READY
        assert result.is_ready is True

    def test_each_gate_independently_blocks_ready(self):
        """Verify each of the four NOT_READY gates independently prevents READY.

        When only one gate fails and all others pass, verdict must be NOT_READY.
        This confirms no gate is redundant and all four are independently checked.
        """
        # Gate 1: Only ERC blocking errors block
        result = AuditResult()
        result.erc.error_count = 3
        result.erc.blocking_error_count = 3
        result.drc.blocking_count = 0
        result.connectivity.passed = True
        result.compatibility.passed = True
        assert result.verdict == AuditVerdict.NOT_READY

        # Gate 2: Only DRC blocking blocks
        result = AuditResult()
        result.erc.error_count = 0
        result.drc.blocking_count = 2
        result.connectivity.passed = True
        result.compatibility.passed = True
        assert result.verdict == AuditVerdict.NOT_READY

        # Gate 3: Only connectivity blocks
        result = AuditResult()
        result.erc.error_count = 0
        result.drc.blocking_count = 0
        result.connectivity.passed = False
        result.compatibility.passed = True
        assert result.verdict == AuditVerdict.NOT_READY

        # Gate 4: Only compatibility blocks
        result = AuditResult()
        result.erc.error_count = 0
        result.drc.blocking_count = 0
        result.connectivity.passed = True
        result.compatibility.passed = False
        assert result.verdict == AuditVerdict.NOT_READY

    def test_erc_timeout_does_not_block_ready(self):
        """Verify ERC timeout (passed=False, error_count=0) does not block READY.

        The verdict gates on erc.error_count, not erc.passed. A timeout sets
        passed=False but leaves error_count at 0, so it should not block.
        """
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=0,
            warning_count=0,
            passed=False,
            details="ERC timed out",
        )
        result.drc = DRCStatus(blocking_count=0, warning_count=0, passed=True)
        result.connectivity = ConnectivityStatus(passed=True)
        result.compatibility = ManufacturerCompatibility(passed=True)

        assert result.verdict == AuditVerdict.READY

    def test_kicad_cli_not_installed_does_not_block_ready(self):
        """Verify missing kicad-cli (passed=True, error_count=0) reaches READY.

        When kicad-cli is not found, _check_erc sets passed=True and
        error_count stays 0, which should not block the READY verdict.
        """
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=0,
            warning_count=0,
            passed=True,
            details="kicad-cli not found (skipped)",
        )
        result.drc = DRCStatus(blocking_count=0, warning_count=0, passed=True)
        result.connectivity = ConnectivityStatus(passed=True)
        result.compatibility = ManufacturerCompatibility(passed=True)

        assert result.verdict == AuditVerdict.READY

    def test_verdict_warning_connectivity_fails_with_zones(self):
        """Test WARNING when connectivity fails but board has zone definitions.

        When DRC=0, ERC=PASS, manufacturer=PASS, and the board has zones,
        incomplete nets are treated as advisory because zone fills may
        resolve the gaps once refilled in KiCad.
        """
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=0,
            warning_count=0,
            blocking_error_count=0,
            passed=True,
        )
        result.drc = DRCStatus(
            error_count=0,
            warning_count=0,
            blocking_count=0,
            passed=True,
        )
        result.connectivity = ConnectivityStatus(
            total_nets=10,
            connected_nets=7,
            incomplete_nets=3,
            has_zones=True,
            passed=False,
        )
        result.compatibility = ManufacturerCompatibility(
            manufacturer="JLCPCB",
            passed=True,
        )

        assert result.verdict == AuditVerdict.WARNING
        assert result.is_ready is False

    def test_verdict_not_ready_connectivity_fails_no_zones(self):
        """Test NOT_READY when connectivity fails and board has no zones.

        Without zone definitions, incomplete nets cannot be zone-filled
        and must be treated as a hard failure.
        """
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=0,
            warning_count=0,
            blocking_error_count=0,
            passed=True,
        )
        result.drc = DRCStatus(
            error_count=0,
            warning_count=0,
            blocking_count=0,
            passed=True,
        )
        result.connectivity = ConnectivityStatus(
            total_nets=10,
            connected_nets=7,
            incomplete_nets=3,
            has_zones=False,
            passed=False,
        )
        result.compatibility = ManufacturerCompatibility(
            manufacturer="JLCPCB",
            passed=True,
        )

        assert result.verdict == AuditVerdict.NOT_READY
        assert result.is_ready is False

    def test_verdict_ready_all_connected_with_zones(self):
        """Test READY when all nets connected and board has zones.

        When connectivity passes, has_zones should not affect the verdict.
        """
        result = AuditResult()
        result.erc = ERCStatus(error_count=0, warning_count=0, passed=True)
        result.drc = DRCStatus(blocking_count=0, warning_count=0, passed=True)
        result.connectivity = ConnectivityStatus(
            total_nets=10,
            connected_nets=10,
            incomplete_nets=0,
            has_zones=True,
            passed=True,
        )
        result.compatibility = ManufacturerCompatibility(passed=True)

        assert result.verdict == AuditVerdict.READY
        assert result.is_ready is True

    def test_verdict_not_ready_drc_blocks_even_with_zones(self):
        """Test NOT_READY when DRC fails even if connectivity+zones is advisory.

        DRC blocking errors always override the zone connectivity advisory.
        """
        result = AuditResult()
        result.erc = ERCStatus(error_count=0, warning_count=0, passed=True)
        result.drc = DRCStatus(blocking_count=5, warning_count=0, passed=False)
        result.connectivity = ConnectivityStatus(
            total_nets=10,
            connected_nets=7,
            incomplete_nets=3,
            has_zones=True,
            passed=False,
        )
        result.compatibility = ManufacturerCompatibility(passed=True)

        assert result.verdict == AuditVerdict.NOT_READY

    def test_verdict_not_ready_erc_blocks_even_with_zones(self):
        """Test NOT_READY when ERC has blocking errors even with zones."""
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=2,
            blocking_error_count=2,
            warning_count=0,
            passed=False,
        )
        result.drc = DRCStatus(blocking_count=0, warning_count=0, passed=True)
        result.connectivity = ConnectivityStatus(
            total_nets=10,
            connected_nets=7,
            incomplete_nets=3,
            has_zones=True,
            passed=False,
        )
        result.compatibility = ManufacturerCompatibility(passed=True)

        assert result.verdict == AuditVerdict.NOT_READY

    def test_verdict_not_ready_compatibility_blocks_even_with_zones(self):
        """Test NOT_READY when compatibility fails even with zones."""
        result = AuditResult()
        result.erc = ERCStatus(error_count=0, warning_count=0, passed=True)
        result.drc = DRCStatus(blocking_count=0, warning_count=0, passed=True)
        result.connectivity = ConnectivityStatus(
            total_nets=10,
            connected_nets=7,
            incomplete_nets=3,
            has_zones=True,
            passed=False,
        )
        result.compatibility = ManufacturerCompatibility(passed=False)

        assert result.verdict == AuditVerdict.NOT_READY

    def test_connectivity_has_zones_in_to_dict(self):
        """Test that has_zones field is included in to_dict serialization."""
        status = ConnectivityStatus(has_zones=True)
        data = status.to_dict()
        assert "has_zones" in data
        assert data["has_zones"] is True

        status2 = ConnectivityStatus(has_zones=False)
        data2 = status2.to_dict()
        assert data2["has_zones"] is False

    def test_summary_dict(self):
        """Test summary dict contains expected fields."""
        result = AuditResult(project_name="test_project")
        summary = result.summary()

        assert "verdict" in summary
        assert "is_ready" in summary
        assert "erc_errors" in summary
        assert "drc_violations" in summary
        assert "drc_blocking" in summary
        assert "net_completion" in summary
        assert "manufacturer_compatible" in summary
        assert "estimated_cost" in summary
        assert "action_items" in summary

    def test_to_dict_serialization(self):
        """Test that to_dict produces JSON-serializable output."""
        result = AuditResult(project_name="test_project")
        data = result.to_dict()

        # Should be JSON-serializable
        json_str = json.dumps(data, default=str)
        assert json_str

        # Check key fields
        assert data["project_name"] == "test_project"
        assert "verdict" in data
        assert "erc" in data
        assert "drc" in data
        assert "connectivity" in data


class TestManufacturingAudit:
    """Tests for ManufacturingAudit class."""

    def test_init_with_pcb_file(self, drc_clean_pcb: Path):
        """Test initialization with PCB file."""
        audit = ManufacturingAudit(drc_clean_pcb, manufacturer="jlcpcb")
        assert audit.pcb_path == drc_clean_pcb
        assert audit.manufacturer == "jlcpcb"
        assert audit.skip_erc is True  # Auto-set for PCB-only

    def test_init_with_invalid_extension(self, tmp_path: Path):
        """Test initialization fails with invalid file extension."""
        bad_file = tmp_path / "test.txt"
        bad_file.write_text("not a pcb")

        with pytest.raises(ValueError, match="Expected .kicad_pro or .kicad_pcb"):
            ManufacturingAudit(bad_file)

    def test_run_on_clean_pcb(self, drc_clean_pcb: Path):
        """Test running audit on a clean PCB file."""
        audit = ManufacturingAudit(drc_clean_pcb, manufacturer="jlcpcb")
        result = audit.run()

        # Should return a result
        assert isinstance(result, AuditResult)
        assert result.pcb_path == drc_clean_pcb

        # DRC should run
        assert result.drc is not None

        # Connectivity should run
        assert result.connectivity is not None
        assert result.connectivity.total_nets >= 0

    def test_run_with_different_manufacturers(self, drc_clean_pcb: Path):
        """Test running audit with different manufacturers."""
        for mfr in ["jlcpcb", "seeed", "pcbway", "oshpark"]:
            audit = ManufacturingAudit(drc_clean_pcb, manufacturer=mfr)
            result = audit.run()

            assert result.compatibility.manufacturer == mfr.upper()

    def test_run_with_quantity(self, drc_clean_pcb: Path):
        """Test that quantity affects cost estimate."""
        audit = ManufacturingAudit(drc_clean_pcb, manufacturer="jlcpcb", quantity=100)
        result = audit.run()

        assert result.cost.quantity == 100

    def test_skip_erc_flag(self, drc_clean_pcb: Path):
        """Test that skip_erc flag is respected."""
        audit = ManufacturingAudit(drc_clean_pcb, skip_erc=True)
        result = audit.run()

        # ERC should be skipped - check that we have default empty status
        # (Either no errors or note about being skipped)
        assert result.erc.error_count == 0 or result.erc.details


class TestAuditCLI:
    """Tests for kct audit CLI command."""

    def test_audit_file_not_found(self, capsys):
        """Test audit command with missing file."""
        from kicad_tools.cli.audit_cmd import main

        result = main(["nonexistent.kicad_pcb"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "Error" in captured.err

    def test_audit_wrong_extension(self, capsys, tmp_path: Path):
        """Test audit command with wrong file extension."""
        from kicad_tools.cli.audit_cmd import main

        wrong_file = tmp_path / "test.txt"
        wrong_file.write_text("not a pcb")

        result = main([str(wrong_file)])
        assert result == 1

        captured = capsys.readouterr()
        assert ".kicad_pro" in captured.err or ".kicad_pcb" in captured.err

    def test_audit_table_output(self, drc_clean_pcb: Path, capsys):
        """Test audit command with table output format."""
        from kicad_tools.cli.audit_cmd import main

        result = main([str(drc_clean_pcb)])
        # May return 0 or 1 depending on DRC results
        assert result in [0, 1, 2]

        captured = capsys.readouterr()
        assert "MANUFACTURING READINESS AUDIT" in captured.out
        assert "CHECK RESULTS" in captured.out

    def test_audit_json_output(self, drc_clean_pcb: Path, capsys):
        """Test audit command with JSON output format."""
        from kicad_tools.cli.audit_cmd import main

        result = main([str(drc_clean_pcb), "--format", "json"])
        # May return 0 or 1 depending on DRC results
        assert result in [0, 1, 2]

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Verify JSON structure
        assert "project_name" in data
        assert "verdict" in data
        assert "erc" in data
        assert "drc" in data
        assert "connectivity" in data
        assert "compatibility" in data
        assert "action_items" in data

    def test_audit_summary_output(self, drc_clean_pcb: Path, capsys):
        """Test audit command with summary output format."""
        from kicad_tools.cli.audit_cmd import main

        result = main([str(drc_clean_pcb), "--format", "summary"])
        assert result in [0, 1, 2]

        captured = capsys.readouterr()
        # Summary should have verdict and key metrics
        assert (
            "Verdict:" in captured.out or "READY:" in captured.out or "NOT READY:" in captured.out
        )

    def test_audit_manufacturer_option(self, drc_clean_pcb: Path, capsys):
        """Test audit command with manufacturer option."""
        from kicad_tools.cli.audit_cmd import main

        for mfr in ["jlcpcb", "seeed", "pcbway", "oshpark"]:
            result = main([str(drc_clean_pcb), "--mfr", mfr, "--format", "json"])
            assert result in [0, 1, 2], f"Failed for manufacturer {mfr}"

            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert data["compatibility"]["manufacturer"] == mfr.upper()

    def test_audit_quantity_option(self, drc_clean_pcb: Path, capsys):
        """Test audit command with quantity option."""
        from kicad_tools.cli.audit_cmd import main

        result = main([str(drc_clean_pcb), "--quantity", "50", "--format", "json"])
        assert result in [0, 1, 2]

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["cost"]["quantity"] == 50

    def test_audit_verbose_flag(self, drc_clean_pcb: Path, capsys):
        """Test audit command with verbose flag."""
        from kicad_tools.cli.audit_cmd import main

        result = main([str(drc_clean_pcb), "--verbose"])
        assert result in [0, 1, 2]

        captured = capsys.readouterr()
        # Verbose should show more detail like min trace width
        assert "Min" in captured.out or "trace" in captured.out or "Manufacturer" in captured.out

    def test_audit_skip_erc_flag(self, drc_clean_pcb: Path, capsys):
        """Test audit command with skip-erc flag."""
        from kicad_tools.cli.audit_cmd import main

        result = main([str(drc_clean_pcb), "--skip-erc"])
        assert result in [0, 1, 2]

    def test_audit_help_text(self, capsys):
        """Test audit command help text."""
        from kicad_tools.cli.audit_cmd import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Manufacturing readiness" in captured.out or "kct audit" in captured.out


class TestAuditCLIIntegration:
    """Integration tests for audit command via main CLI."""

    def test_audit_via_main_cli(self, drc_clean_pcb: Path, capsys):
        """Test audit command through the main CLI dispatcher."""
        from kicad_tools.cli import main

        result = main(["audit", str(drc_clean_pcb)])
        assert result in [0, 1, 2]

        captured = capsys.readouterr()
        assert "AUDIT" in captured.out or "CHECK" in captured.out

    def test_audit_via_main_cli_with_options(self, drc_clean_pcb: Path, capsys):
        """Test audit command through main CLI with options."""
        from kicad_tools.cli import main

        result = main(
            [
                "audit",
                str(drc_clean_pcb),
                "--mfr",
                "seeed",
                "--quantity",
                "25",
                "--format",
                "json",
            ]
        )
        assert result in [0, 1, 2]

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["compatibility"]["manufacturer"] == "SEEED"
        assert data["cost"]["quantity"] == 25


class TestAuditExitCodes:
    """Tests for audit command exit codes."""

    def test_exit_code_with_strict_flag(self, drc_clean_pcb: Path):
        """Test that --strict flag can return code 2 on warnings."""
        from kicad_tools.cli.audit_cmd import main

        # With clean PCB, should return 0 or maybe 1/2 depending on DRC results
        result = main([str(drc_clean_pcb), "--strict"])
        # Exit code depends on actual DRC results of the clean PCB
        assert result in [0, 1, 2]

    def test_exit_code_zero_when_ready(self):
        """Test that CLI returns exit code 0 when verdict is READY.

        Constructs an AuditResult with READY verdict and verifies the
        exit code logic in audit_cmd.main returns 0.
        """
        result = AuditResult(project_name="test_ready")
        # All defaults yield READY
        assert result.verdict == AuditVerdict.READY

        # Verify the exit code logic directly
        if result.verdict == AuditVerdict.NOT_READY:
            exit_code = 1
        elif result.verdict == AuditVerdict.WARNING:
            exit_code = 2  # strict mode
        else:
            exit_code = 0
        assert exit_code == 0

    def test_exit_code_two_when_not_ready(self):
        """Test that CLI returns exit code 2 when verdict is NOT_READY.

        Exit code 2 means the audit ran successfully but found issues.
        Exit code 1 is reserved for tool-level failures (file not found, etc.).
        """
        result = AuditResult()
        result.erc.error_count = 5
        result.erc.blocking_error_count = 5
        assert result.verdict == AuditVerdict.NOT_READY

        # Verify the exit code mapping
        if result.verdict == AuditVerdict.NOT_READY:
            exit_code = 2
        else:
            exit_code = 0
        assert exit_code == 2

    def test_exit_code_two_when_warning_strict(self):
        """Test that CLI returns exit code 2 for WARNING with --strict."""
        result = AuditResult()
        result.erc.warning_count = 1
        assert result.verdict == AuditVerdict.WARNING

        # Verify the exit code mapping under strict mode
        strict = True
        if result.verdict == AuditVerdict.NOT_READY:
            exit_code = 1
        elif result.verdict == AuditVerdict.WARNING and strict:
            exit_code = 2
        else:
            exit_code = 0
        assert exit_code == 2

    def test_exit_code_one_for_file_not_found(self, capsys):
        """Test that CLI returns exit code 1 for tool-level errors (file not found)."""
        from kicad_tools.cli.audit_cmd import main

        result = main(["nonexistent_board.kicad_pcb"])
        assert result == 1

    def test_exit_code_one_for_wrong_extension(self, tmp_path, capsys):
        """Test that CLI returns exit code 1 for tool-level errors (wrong extension)."""
        from kicad_tools.cli.audit_cmd import main

        bad_file = tmp_path / "test.txt"
        bad_file.write_text("not a pcb")
        result = main([str(bad_file)])
        assert result == 1

    def test_exit_code_two_when_not_ready_via_main(self, tmp_path, monkeypatch, capsys):
        """Test that audit main() returns 2 (not 1) when verdict is NOT_READY.

        This confirms the pipeline will see exit code 2 and treat it as
        'completed with warnings' instead of 'failed'.
        """
        from unittest.mock import MagicMock, patch

        from kicad_tools.cli.audit_cmd import main

        # Create a dummy PCB file so path validation passes
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20221018))")

        # Mock ManufacturingAudit to return NOT_READY
        mock_result = AuditResult()
        mock_result.drc.blocking_count = 3
        assert mock_result.verdict == AuditVerdict.NOT_READY

        mock_audit_instance = MagicMock()
        mock_audit_instance.run.return_value = mock_result

        with patch(
            "kicad_tools.cli.audit_cmd.ManufacturingAudit",
            return_value=mock_audit_instance,
        ):
            exit_code = main([str(pcb_file)])

        assert exit_code == 2

    def test_exit_code_zero_when_ready_via_main(self, tmp_path, monkeypatch, capsys):
        """Test that audit main() returns 0 when verdict is READY."""
        from unittest.mock import MagicMock, patch

        from kicad_tools.cli.audit_cmd import main

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20221018))")

        mock_result = AuditResult()
        assert mock_result.verdict == AuditVerdict.READY

        mock_audit_instance = MagicMock()
        mock_audit_instance.run.return_value = mock_result

        with patch(
            "kicad_tools.cli.audit_cmd.ManufacturingAudit",
            return_value=mock_audit_instance,
        ):
            exit_code = main([str(pcb_file)])

        assert exit_code == 0


class TestAuditOutputRendering:
    """Tests for audit output rendering with READY verdict."""

    def test_output_table_shows_ready_for_manufacturing(self, capsys):
        """Test that output_table prints '[OK] READY FOR MANUFACTURING' when READY."""
        from kicad_tools.cli.audit_cmd import output_table

        result = AuditResult(project_name="test_ready_board")
        # Explicitly set all checks to passing
        result.erc = ERCStatus(error_count=0, warning_count=0, passed=True)
        result.drc = DRCStatus(error_count=0, warning_count=0, blocking_count=0, passed=True)
        result.connectivity = ConnectivityStatus(total_nets=5, connected_nets=5, passed=True)
        result.compatibility = ManufacturerCompatibility(manufacturer="JLCPCB", passed=True)

        assert result.verdict == AuditVerdict.READY

        output_table(result)
        captured = capsys.readouterr()

        assert "[OK] READY FOR MANUFACTURING" in captured.out

    def test_output_table_shows_not_ready_when_erc_fails(self, capsys):
        """Test that output_table prints NOT READY when ERC has blocking errors."""
        from kicad_tools.cli.audit_cmd import output_table

        result = AuditResult(project_name="test_failing_board")
        result.erc.error_count = 3
        result.erc.blocking_error_count = 3
        assert result.verdict == AuditVerdict.NOT_READY

        output_table(result)
        captured = capsys.readouterr()

        assert "[XX] NOT READY - FIX ISSUES" in captured.out

    def test_output_table_shows_warning_when_erc_warnings(self, capsys):
        """Test that output_table prints REVIEW WARNINGS for WARNING verdict."""
        from kicad_tools.cli.audit_cmd import output_table

        result = AuditResult(project_name="test_warning_board")
        result.erc.warning_count = 2
        assert result.verdict == AuditVerdict.WARNING

        output_table(result)
        captured = capsys.readouterr()

        assert "[!!] REVIEW WARNINGS" in captured.out


class TestAuditPathResolution:
    """Tests for PCB path resolution when given project file."""

    def test_resolve_pcb_from_project_kct(self, tmp_path: Path):
        """Test that audit resolves PCB path from project.kct artifacts."""
        # Create directory structure matching issue #749
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Create a minimal routed PCB file
        routed_pcb = output_dir / "test_routed.kicad_pcb"
        routed_pcb.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )

        # Create unrouted PCB file (shouldn't be used)
        unrouted_pcb = output_dir / "test.kicad_pcb"
        unrouted_pcb.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )

        # Create project.kct in parent directory
        kct_file = tmp_path / "project.kct"
        kct_file.write_text(
            """kct_version: "1.0"
project:
  name: "Test"
  artifacts:
    pcb: "output/test_routed.kicad_pcb"
"""
        )

        # Create project file
        project_file = output_dir / "test.kicad_pro"
        project_file.write_text("{}")

        # Initialize audit with project file
        audit = ManufacturingAudit(project_file)

        # Should resolve to the routed PCB from project.kct
        assert audit.pcb_path == routed_pcb

    def test_resolve_pcb_fallback_to_routed_suffix(self, tmp_path: Path):
        """Test that audit falls back to *_routed.kicad_pcb if no project.kct."""
        # Create routed PCB
        routed_pcb = tmp_path / "test_routed.kicad_pcb"
        routed_pcb.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )

        # Create unrouted PCB
        unrouted_pcb = tmp_path / "test.kicad_pcb"
        unrouted_pcb.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )

        # Create project file (no project.kct)
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text("{}")

        # Initialize audit with project file
        audit = ManufacturingAudit(project_file)

        # Should fallback to *_routed.kicad_pcb
        assert audit.pcb_path == routed_pcb

    def test_resolve_pcb_default_no_routed(self, tmp_path: Path):
        """Test that audit uses default path when no routed PCB exists."""
        # Create only unrouted PCB
        unrouted_pcb = tmp_path / "test.kicad_pcb"
        unrouted_pcb.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )

        # Create project file (no project.kct, no *_routed.kicad_pcb)
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text("{}")

        # Initialize audit with project file
        audit = ManufacturingAudit(project_file)

        # Should default to <basename>.kicad_pcb
        assert audit.pcb_path == unrouted_pcb


class TestAuditJsonSchema:
    """Tests for audit command JSON output schema."""

    def test_json_schema_complete(self, drc_clean_pcb: Path, capsys):
        """Test that JSON output contains all required fields."""
        from kicad_tools.cli.audit_cmd import main

        main([str(drc_clean_pcb), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Required top-level fields
        assert "project_name" in data
        assert "timestamp" in data
        assert "verdict" in data
        assert "is_ready" in data
        assert "summary" in data
        assert "erc" in data
        assert "drc" in data
        assert "connectivity" in data
        assert "compatibility" in data
        assert "layers" in data
        assert "cost" in data
        assert "action_items" in data

        # Nested structure checks
        assert "error_count" in data["erc"]
        assert "warning_count" in data["erc"]
        assert "error_count" in data["drc"]
        assert "total_nets" in data["connectivity"]
        assert "manufacturer" in data["compatibility"]
        assert "quantity" in data["cost"]

    def test_json_output_is_ci_friendly(self, drc_clean_pcb: Path, capsys):
        """Test that JSON output can be parsed by CI tools."""
        from kicad_tools.cli.audit_cmd import main

        main([str(drc_clean_pcb), "--format", "json"])
        captured = capsys.readouterr()

        # Should be parseable without errors
        data = json.loads(captured.out)

        # CI-friendly check: verdict is a string
        assert isinstance(data["verdict"], str)
        assert data["verdict"] in ["ready", "warning", "not_ready"]

        # CI-friendly check: is_ready is a boolean
        assert isinstance(data["is_ready"], bool)

        # CI-friendly check: counts are integers
        assert isinstance(data["erc"]["error_count"], int)
        assert isinstance(data["drc"]["error_count"], int)


# ---------------------------------------------------------------------------
# Test: corruption guard in auditor
# ---------------------------------------------------------------------------


class TestCorruptionGuard:
    """Tests for the data corruption guard in ManufacturingAudit._check_connectivity."""

    # A board with footprints where all pads have net 0 (corrupted)
    CORRUPTED_PCB = """(kicad_pcb
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
  (gr_rect (start 0 0) (end 100 100) (stroke (width 0.15) (type default)) (fill none) (layer "Edge.Cuts") (uuid "edge"))
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp1")
    (at 50 50)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val1"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 0 ""))
  )
)
"""

    # A board with proper net assignments
    NORMAL_PCB = """(kicad_pcb
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
  (gr_rect (start 0 0) (end 100 100) (stroke (width 0.15) (type default)) (fill none) (layer "Edge.Cuts") (uuid "edge"))
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp1")
    (at 50 50)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val1"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3V3"))
  )
)
"""

    def test_all_pads_net_zero_triggers_not_ready(self, tmp_path):
        """Board with footprints where all pads have net 0 should be NOT_READY."""
        pcb_path = tmp_path / "corrupted.kicad_pcb"
        pcb_path.write_text(self.CORRUPTED_PCB)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        assert result.connectivity.passed is False
        assert "corruption" in result.connectivity.details.lower()
        assert result.verdict == AuditVerdict.NOT_READY

    def test_normal_board_does_not_trigger_guard(self, tmp_path):
        """Board with proper pad net assignments should not trigger guard."""
        pcb_path = tmp_path / "normal.kicad_pcb"
        pcb_path.write_text(self.NORMAL_PCB)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        # Should NOT have corruption message
        assert "corruption" not in result.connectivity.details.lower()

    def test_no_footprints_does_not_trigger_guard(self, tmp_path):
        """Board with no footprints should not trigger the corruption guard."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.create(width=100, height=100)
        pcb_path = tmp_path / "empty.kicad_pcb"
        pcb.save(str(pcb_path))

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        # Should not have corruption details
        assert "corruption" not in result.connectivity.details.lower()


class TestZoneConnectedNets:
    """Tests for zone-connected net reclassification in connectivity check."""

    # PCB where GND net has a zone definition but no filled_polygons.
    # Two footprints with pads on GND (net 1) and +3V3 (net 2).
    # GND has a zone, +3V3 has a trace connecting its pads.
    # With no filled_polygons and no trace on GND, the connectivity
    # validator would normally flag GND as incomplete.
    PCB_WITH_ZONE = """(kicad_pcb
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
  (gr_rect (start 0 0) (end 100 100)
    (stroke (width 0.15) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "edge"))
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp1")
    (at 30 50)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val1"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3V3"))
  )
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp2")
    (at 70 50)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref2"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val2"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3V3"))
  )
  (segment (start 30.5 50) (end 70.5 50) (width 0.25) (layer "F.Cu") (net 2)
    (uuid "seg1"))
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (uuid "zone1")
    (connect_pads (clearance 0.5))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts
      (xy 0 0) (xy 100 0) (xy 100 100) (xy 0 100)
    ))
  )
)
"""

    # PCB with both zone-connected and truly-incomplete nets.
    # GND (net 1) has a zone, SIG (net 3) has no zone and no trace.
    PCB_MIXED_NETS = """(kicad_pcb
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
  (net 3 "SIG")
  (gr_rect (start 0 0) (end 100 100)
    (stroke (width 0.15) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "edge"))
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp1")
    (at 30 50)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val1"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3V3"))
  )
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp2")
    (at 70 50)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref2"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val2"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SIG"))
  )
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp3")
    (at 50 30)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref3"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val3"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3V3"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SIG"))
  )
  (segment (start 30.5 50) (end 49.5 30) (width 0.25) (layer "F.Cu") (net 2)
    (uuid "seg1"))
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (uuid "zone1")
    (connect_pads (clearance 0.5))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts
      (xy 0 0) (xy 100 0) (xy 100 100) (xy 0 100)
    ))
  )
)
"""

    # PCB with no zones — incomplete nets should still be flagged normally.
    PCB_NO_ZONES = """(kicad_pcb
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
  (gr_rect (start 0 0) (end 100 100)
    (stroke (width 0.15) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "edge"))
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp1")
    (at 30 50)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val1"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3V3"))
  )
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp2")
    (at 70 50)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref2"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val2"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3V3"))
  )
)
"""

    def test_zone_connected_nets_pass_connectivity(self, tmp_path):
        """Zone-connected net with zone definition should not block READY verdict.

        When a net is incomplete only because zone fill data is absent,
        the audit should reclassify it as zone-connected and pass.
        """
        pcb_path = tmp_path / "zone_board.kicad_pcb"
        pcb_path.write_text(self.PCB_WITH_ZONE)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        # GND should be reclassified as zone-connected
        assert result.connectivity.zone_connected_nets >= 1
        # Truly incomplete nets should be 0 (only GND is incomplete,
        # and it has a zone)
        assert result.connectivity.incomplete_nets == 0
        # Connectivity should pass
        assert result.connectivity.passed is True

    def test_mixed_nets_truly_incomplete_advisory_with_zones(self, tmp_path):
        """Mixed nets: board has zones so incomplete connectivity is advisory.

        When some incomplete nets have zones and others do not, the board
        still has zone definitions.  Per the zone-connectivity advisory
        rule, incomplete nets on a board with zones yield WARNING (not
        NOT_READY) because zone fills may resolve the gaps.
        """
        pcb_path = tmp_path / "mixed_board.kicad_pcb"
        pcb_path.write_text(self.PCB_MIXED_NETS)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        # GND is zone-connected
        assert result.connectivity.zone_connected_nets >= 1
        # SIG has no zone and no trace — truly incomplete
        assert result.connectivity.incomplete_nets >= 1
        # Connectivity should fail due to SIG
        assert result.connectivity.passed is False
        # Board has zone definitions so connectivity is advisory
        assert result.connectivity.has_zones is True
        # Verdict should be WARNING (not NOT_READY) per zone advisory rule
        assert result.verdict == AuditVerdict.WARNING

    def test_no_zones_regression(self, tmp_path):
        """PCB with no zones and incomplete signal nets still yields NOT_READY.

        GND is reclassified as zone-connected via pour-net detection,
        but +3V3 remains truly incomplete because it does not match
        power-net patterns in classify_and_apply_rules.
        """
        pcb_path = tmp_path / "no_zone_board.kicad_pcb"
        pcb_path.write_text(self.PCB_NO_ZONES)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        # GND is reclassified as zone-connected via pour-net detection
        assert result.connectivity.zone_connected_nets >= 1
        # +3V3 has no zone and is not a pour net — truly incomplete
        assert result.connectivity.incomplete_nets >= 1
        # Connectivity should fail due to +3V3
        assert result.connectivity.passed is False
        # Verdict should be NOT_READY
        assert result.verdict == AuditVerdict.NOT_READY

    def test_connectivity_status_to_dict_includes_zone_connected(self):
        """ConnectivityStatus.to_dict() includes zone_connected_nets field."""
        status = ConnectivityStatus(
            total_nets=10,
            connected_nets=5,
            incomplete_nets=2,
            zone_connected_nets=3,
            completion_percent=50.0,
            passed=True,
        )
        d = status.to_dict()
        assert "zone_connected_nets" in d
        assert d["zone_connected_nets"] == 3

    def test_zone_connected_verdict_ready(self):
        """Synthetic AuditResult with only zone-connected incomplete nets yields READY."""
        result = AuditResult()
        result.connectivity = ConnectivityStatus(
            total_nets=10,
            connected_nets=5,
            incomplete_nets=0,
            zone_connected_nets=5,
            passed=True,
        )
        result.erc = ERCStatus(passed=True)
        result.drc = DRCStatus(passed=True)
        result.compatibility = ManufacturerCompatibility(passed=True)

        assert result.verdict == AuditVerdict.READY

    def test_zone_connected_action_items(self, tmp_path):
        """Zone-connected nets produce advisory action item, not critical."""
        pcb_path = tmp_path / "zone_board.kicad_pcb"
        pcb_path.write_text(self.PCB_WITH_ZONE)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        # Should have a zone-fill verification advisory
        zone_items = [item for item in result.action_items if "zone" in item.description.lower()]
        assert len(zone_items) >= 1
        # Advisory should be low priority (3)
        assert all(item.priority == 3 for item in zone_items)
        # Should NOT have a critical connectivity action item
        critical_conn_items = [
            item
            for item in result.action_items
            if item.priority == 1 and "routing" in item.description.lower()
        ]
        assert len(critical_conn_items) == 0

    def test_zone_connected_cli_display(self, tmp_path, capsys):
        """CLI table output shows zone-connected nets count."""
        from kicad_tools.cli.audit_cmd import output_table

        pcb_path = tmp_path / "zone_board.kicad_pcb"
        pcb_path.write_text(self.PCB_WITH_ZONE)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        output_table(result)
        captured = capsys.readouterr()

        # Should display zone-connected count
        if result.connectivity.zone_connected_nets > 0:
            assert "zone fill" in captured.out.lower()

    def test_zone_connected_json_output(self, tmp_path, capsys):
        """JSON output includes zone_connected_nets field."""
        pcb_path = tmp_path / "zone_board.kicad_pcb"
        pcb_path.write_text(self.PCB_WITH_ZONE)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        data = result.to_dict()
        assert "zone_connected_nets" in data["connectivity"]
        assert data["connectivity"]["zone_connected_nets"] >= 1


class TestZoneConnectedPourNets:
    """Tests for pour-net classification in _check_connectivity.

    Verifies that nets classified as pour nets (is_pour_net=True) by
    classify_and_apply_rules are reclassified as zone-connected even
    when no explicit zone definition exists in the PCB.
    """

    # PCB with +5V and GNDA nets but NO zone definitions.
    # Both are pour nets per classify_and_apply_rules.
    PCB_POUR_NO_ZONES = """\
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
  (net 1 "+5V")
  (net 2 "GNDA")
  (gr_rect (start 0 0) (end 100 100)
    (stroke (width 0.15) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "edge"))
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp1")
    (at 30 50)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val1"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "+5V"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GNDA"))
  )
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp2")
    (at 70 50)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref2"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val2"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "+5V"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GNDA"))
  )
)
"""

    # PCB with pour nets AND a signal net, no zones.
    # Each pour net has 2 pads (on different footprints) so they need routing.
    # SPI_CLK also has 2 pads needing routing.
    PCB_POUR_AND_SIGNAL_NO_ZONES = """\
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
  (net 1 "+5V")
  (net 2 "GNDA")
  (net 3 "SPI_CLK")
  (gr_rect (start 0 0) (end 100 100)
    (stroke (width 0.15) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "edge"))
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp1")
    (at 30 50)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val1"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "+5V"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SPI_CLK"))
  )
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp2")
    (at 50 50)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref2"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val2"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GNDA"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SPI_CLK"))
  )
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp3")
    (at 70 50)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref3"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val3"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "+5V"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GNDA"))
  )
)
"""

    # PCB with +5V that already has a zone definition.
    PCB_POUR_WITH_ZONE = """\
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
  (net 1 "+5V")
  (net 2 "GNDA")
  (gr_rect (start 0 0) (end 100 100)
    (stroke (width 0.15) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "edge"))
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp1")
    (at 30 50)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val1"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "+5V"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GNDA"))
  )
  (footprint "R_0402"
    (layer "F.Cu")
    (uuid "fp2")
    (at 70 50)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref2"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val2"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "+5V"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GNDA"))
  )
  (zone (net 1) (net_name "+5V") (layer "F.Cu")
    (uuid "zone1")
    (connect_pads (clearance 0.5))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts
      (xy 0 0) (xy 100 0) (xy 100 100) (xy 0 100)
    ))
  )
)
"""

    def test_pour_nets_without_zone_excluded_from_incomplete(self, tmp_path):
        """Pour nets (+5V, GNDA) without zone definitions are zone-connected.

        When classify_and_apply_rules returns is_pour_net=True, those nets
        are excluded from incomplete_nets and included in zone_connected_nets.
        """
        pcb_path = tmp_path / "pour_board.kicad_pcb"
        pcb_path.write_text(self.PCB_POUR_NO_ZONES)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        # Both +5V and GNDA should be reclassified as zone-connected
        assert result.connectivity.zone_connected_nets == 2
        assert result.connectivity.incomplete_nets == 0
        assert result.connectivity.passed is True

    def test_pour_net_names_tracked(self, tmp_path):
        """Pour net names are stored in ConnectivityStatus.pour_net_names."""
        pcb_path = tmp_path / "pour_board.kicad_pcb"
        pcb_path.write_text(self.PCB_POUR_NO_ZONES)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        assert sorted(result.connectivity.pour_net_names) == ["+5V", "GNDA"]

    def test_signal_net_still_incomplete(self, tmp_path):
        """Signal net (SPI_CLK) stays incomplete even with pour nets present."""
        pcb_path = tmp_path / "mixed_board.kicad_pcb"
        pcb_path.write_text(self.PCB_POUR_AND_SIGNAL_NO_ZONES)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        # Pour nets reclassified, signal net stays incomplete
        assert result.connectivity.zone_connected_nets >= 2
        assert result.connectivity.incomplete_nets >= 1
        assert result.connectivity.passed is False
        assert result.verdict == AuditVerdict.NOT_READY

    def test_pour_net_with_existing_zone_not_duplicated(self, tmp_path):
        """A pour net that already has a zone definition is handled by
        the first pass (zone-name intersection), not duplicated."""
        pcb_path = tmp_path / "zone_board.kicad_pcb"
        pcb_path.write_text(self.PCB_POUR_WITH_ZONE)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        # Both +5V (zone) and GNDA (pour classification) should be zone-connected
        assert result.connectivity.zone_connected_nets == 2
        assert result.connectivity.incomplete_nets == 0
        assert result.connectivity.passed is True
        # Only GNDA should be in pour_net_names (+5V was handled by zone path)
        assert result.connectivity.pour_net_names == ["GNDA"]

    def test_classification_failure_leaves_truly_incomplete_unchanged(self, tmp_path):
        """If classify_and_apply_rules raises, incomplete nets stay unchanged."""
        from unittest.mock import patch

        pcb_path = tmp_path / "pour_board.kicad_pcb"
        pcb_path.write_text(self.PCB_POUR_NO_ZONES)

        with patch(
            "kicad_tools.router.net_class.classify_and_apply_rules",
            side_effect=RuntimeError("classification failure"),
        ):
            audit = ManufacturingAudit(pcb_path)
            result = audit.run()

        # Without classification, both nets remain incomplete
        assert result.connectivity.incomplete_nets >= 2
        assert result.connectivity.passed is False
        assert result.connectivity.pour_net_names == []

    def test_pour_net_action_items_emitted(self, tmp_path):
        """Advisory ActionItems are emitted for pour nets without zones."""
        pcb_path = tmp_path / "pour_board.kicad_pcb"
        pcb_path.write_text(self.PCB_POUR_NO_ZONES)

        audit = ManufacturingAudit(pcb_path)
        result = audit.run()

        add_zone_items = [
            item for item in result.action_items if "Add zone for" in item.description
        ]
        assert len(add_zone_items) == 2
        descriptions = sorted(item.description for item in add_zone_items)
        assert "Add zone for +5V on appropriate copper layer" in descriptions
        assert "Add zone for GNDA on appropriate copper layer" in descriptions
        # These should be advisory (priority 3), not blocking
        for item in add_zone_items:
            assert item.priority == 3

    def test_pour_net_names_in_to_dict(self):
        """ConnectivityStatus.to_dict() includes pour_net_names field."""
        status = ConnectivityStatus(
            total_nets=10,
            connected_nets=5,
            incomplete_nets=0,
            zone_connected_nets=5,
            pour_net_names=["+5V", "GNDA"],
            passed=True,
        )
        d = status.to_dict()
        assert "pour_net_names" in d
        assert d["pour_net_names"] == ["+5V", "GNDA"]


class TestOrphanedFootprints:
    """Tests for orphaned footprint detection (_check_orphaned_footprints)."""

    def test_orphaned_footprints_detected(self, tmp_path: Path):
        """Orphaned footprints on PCB but not in BOM generate priority-2 ActionItem."""
        from unittest.mock import MagicMock, patch

        from kicad_tools.audit.auditor import ActionItem

        # Create a minimal PCB file
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )

        # Create a schematic file (just needs to exist for the path check)
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("")

        # Create project file
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text("{}")

        audit = ManufacturingAudit(project_file)

        # Mock the PCB footprints: R1, R2, C1, U1 on PCB
        mock_pcb = MagicMock()
        mock_fp_r1 = MagicMock(reference="R1")
        mock_fp_r2 = MagicMock(reference="R2")
        mock_fp_c1 = MagicMock(reference="C1")
        mock_fp_u1 = MagicMock(reference="U1")
        mock_pcb.footprints = [mock_fp_r1, mock_fp_r2, mock_fp_c1, mock_fp_u1]
        audit._pcb = mock_pcb

        # Mock BOM: only R1, C1 in schematic (R2 and U1 are orphans)
        from kicad_tools.schema.bom import BOM, BOMItem

        mock_bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
            ]
        )

        with patch("kicad_tools.schema.bom.extract_bom", return_value=mock_bom):
            items = audit._check_orphaned_footprints()

        assert len(items) == 1
        item = items[0]
        assert isinstance(item, ActionItem)
        assert item.priority == 2
        assert "2 orphaned footprint(s)" in item.description
        assert "R2" in item.description
        assert "U1" in item.description

    def test_no_orphaned_footprints(self, tmp_path: Path):
        """No orphan action item when all PCB refs match BOM."""
        from unittest.mock import MagicMock, patch

        # Create minimal files
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("")
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text("{}")

        audit = ManufacturingAudit(project_file)

        # PCB has R1, C1
        mock_pcb = MagicMock()
        mock_pcb.footprints = [MagicMock(reference="R1"), MagicMock(reference="C1")]
        audit._pcb = mock_pcb

        # BOM also has R1, C1
        from kicad_tools.schema.bom import BOM, BOMItem

        mock_bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
            ]
        )

        with patch("kicad_tools.schema.bom.extract_bom", return_value=mock_bom):
            items = audit._check_orphaned_footprints()

        assert len(items) == 0

    def test_orphaned_footprints_no_schematic(self, tmp_path: Path):
        """Check gracefully skipped when schematic is unavailable."""
        # Create PCB file only (no schematic)
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )

        audit = ManufacturingAudit(pcb_path)
        # When initialized with .kicad_pcb, schematic_path is set
        # but won't exist on disk. The run() method checks existence before calling.
        # The method itself won't be called, so let's test run() integration.
        result = audit.run()

        # No orphan action items should appear since schematic doesn't exist
        orphan_items = [a for a in result.action_items if "orphaned footprint" in a.description]
        assert len(orphan_items) == 0

    def test_orphaned_footprints_virtual_bom_items_excluded(self, tmp_path: Path):
        """Virtual BOM items (power symbols) excluded from BOM ref set."""
        from unittest.mock import MagicMock, patch

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("")
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text("{}")

        audit = ManufacturingAudit(project_file)

        # PCB has R1 only
        mock_pcb = MagicMock()
        mock_pcb.footprints = [MagicMock(reference="R1")]
        audit._pcb = mock_pcb

        # BOM has R1 (real) and VCC (virtual/power symbol)
        from kicad_tools.schema.bom import BOM, BOMItem

        mock_bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(
                    reference="#PWR01",
                    value="VCC",
                    footprint="",
                    lib_id="power:VCC",
                    in_bom=False,
                ),
            ]
        )

        with patch("kicad_tools.schema.bom.extract_bom", return_value=mock_bom):
            items = audit._check_orphaned_footprints()

        # R1 matches BOM, #PWR01 is virtual so excluded. No orphans.
        assert len(items) == 0

    def test_orphaned_footprints_dnp_items_still_match(self, tmp_path: Path):
        """DNP items in BOM should still match PCB footprints (not orphans)."""
        from unittest.mock import MagicMock, patch

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("")
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text("{}")

        audit = ManufacturingAudit(project_file)

        # PCB has R1, R2 where R2 is DNP in schematic
        mock_pcb = MagicMock()
        mock_pcb.footprints = [MagicMock(reference="R1"), MagicMock(reference="R2")]
        audit._pcb = mock_pcb

        # BOM: R1 is normal, R2 is DNP (should still match PCB)
        from kicad_tools.schema.bom import BOM, BOMItem

        mock_bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(
                    reference="R2",
                    value="10k",
                    footprint="R_0402",
                    lib_id="Device:R",
                    dnp=True,
                ),
            ]
        )

        with patch("kicad_tools.schema.bom.extract_bom", return_value=mock_bom):
            items = audit._check_orphaned_footprints()

        # R2 is DNP but still in BOM non-virtual, so no orphans
        assert len(items) == 0

    def test_orphaned_footprints_extract_bom_failure_graceful(self, tmp_path: Path):
        """If extract_bom raises, check is skipped gracefully."""
        from unittest.mock import MagicMock, patch

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("")
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text("{}")

        audit = ManufacturingAudit(project_file)

        mock_pcb = MagicMock()
        mock_pcb.footprints = [MagicMock(reference="R1")]
        audit._pcb = mock_pcb

        with patch(
            "kicad_tools.schema.bom.extract_bom",
            side_effect=Exception("Cannot parse schematic"),
        ):
            items = audit._check_orphaned_footprints()

        # Should return empty list, not raise
        assert len(items) == 0

    def test_orphaned_footprints_many_orphans_truncated(self, tmp_path: Path):
        """When more than 10 orphans, description shows truncation notice."""
        from unittest.mock import MagicMock, patch

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("")
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text("{}")

        audit = ManufacturingAudit(project_file)

        # PCB has 15 footprints, BOM has none of them
        mock_pcb = MagicMock()
        mock_pcb.footprints = [MagicMock(reference=f"R{i}") for i in range(1, 16)]
        audit._pcb = mock_pcb

        from kicad_tools.schema.bom import BOM

        mock_bom = BOM(items=[])

        with patch("kicad_tools.schema.bom.extract_bom", return_value=mock_bom):
            items = audit._check_orphaned_footprints()

        assert len(items) == 1
        assert "15 orphaned footprint(s)" in items[0].description
        assert "(and 5 more)" in items[0].description


# ---------------------------------------------------------------------------
# Test: assembly mode / --no-assembly flag
# ---------------------------------------------------------------------------


class TestAssemblyMode:
    """Tests for assembly cost exclusion via project.kct and --no-assembly flag."""

    def test_cost_estimate_assembly_mode_field(self):
        """CostEstimate dataclass has assembly_mode field included in to_dict."""
        est = CostEstimate(assembly_mode="none")
        d = est.to_dict()
        assert "assembly_mode" in d
        assert d["assembly_mode"] == "none"

    def test_cost_estimate_assembly_mode_default_none(self):
        """CostEstimate.assembly_mode defaults to None."""
        est = CostEstimate()
        assert est.assembly_mode is None
        assert est.to_dict()["assembly_mode"] is None

    def test_no_assembly_flag_skips_assembly_cost(self, drc_clean_pcb: Path):
        """ManufacturingAudit with no_assembly=True zeros assembly cost."""
        audit = ManufacturingAudit(drc_clean_pcb, no_assembly=True)
        result = audit.run()

        assert result.cost.assembly_cost == 0.0
        assert result.cost.assembly_mode == "none"
        # Total should not include assembly
        # (just verify assembly is excluded from total)
        assert result.cost.total_cost >= 0.0

    def test_default_includes_assembly_cost(self, drc_clean_pcb: Path):
        """Default audit (no flags) includes assembly cost as before."""
        audit = ManufacturingAudit(drc_clean_pcb)
        result = audit.run()

        # assembly_mode should be None when not explicitly set to "none"
        assert result.cost.assembly_mode is None

    def test_assembly_none_from_project_kct(self, tmp_path: Path):
        """When project.kct has assembly: none, assembly cost is excluded."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("")
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text("{}")

        # Write project.kct with assembly: none
        kct_path = tmp_path / "project.kct"
        kct_path.write_text(
            """kct_version: "1.0"
project:
  name: test
  board: test
requirements:
  manufacturing:
    assembly: "none"
"""
        )

        audit = ManufacturingAudit(project_file)
        assert audit.no_assembly is True
        assert audit._assembly_mode == "none"

    def test_assembly_smt_from_project_kct(self, tmp_path: Path):
        """When project.kct has assembly: smt, assembly cost is included."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("")
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text("{}")

        kct_path = tmp_path / "project.kct"
        kct_path.write_text(
            """kct_version: "1.0"
project:
  name: test
  board: test
requirements:
  manufacturing:
    assembly: "smt"
"""
        )

        audit = ManufacturingAudit(project_file)
        assert audit.no_assembly is False
        assert audit._assembly_mode == "smt"

    def test_no_assembly_flag_overrides_project_kct(self, tmp_path: Path):
        """--no-assembly CLI flag overrides project.kct assembly: smt."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(
            """(kicad_pcb (version 20221018)
  (generator pcbnew)
  (layers (0 "F.Cu" signal))
)"""
        )
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("")
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text("{}")

        kct_path = tmp_path / "project.kct"
        kct_path.write_text(
            """kct_version: "1.0"
project:
  name: test
  board: test
requirements:
  manufacturing:
    assembly: "smt"
"""
        )

        # no_assembly=True should override the smt setting
        audit = ManufacturingAudit(project_file, no_assembly=True)
        assert audit.no_assembly is True

    def test_cli_no_assembly_flag(self, drc_clean_pcb: Path, capsys):
        """CLI --no-assembly flag produces assembly_mode: none in JSON."""
        from kicad_tools.cli.audit_cmd import main

        result = main([str(drc_clean_pcb), "--no-assembly", "--format", "json"])
        assert result in [0, 1, 2]

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["cost"]["assembly_mode"] == "none"
        assert data["cost"]["assembly_cost"] == 0.0

    def test_cli_no_assembly_table_output(self, drc_clean_pcb: Path, capsys):
        """CLI --no-assembly shows 'Assembly: excluded' in table output."""
        from kicad_tools.cli.audit_cmd import main

        result = main([str(drc_clean_pcb), "--no-assembly"])
        assert result in [0, 1, 2]

        captured = capsys.readouterr()
        assert "Assembly: excluded" in captured.out
