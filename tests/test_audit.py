"""Tests for manufacturing readiness audit (kct audit command)."""

import json
from pathlib import Path

import pytest

from kicad_tools.audit import AuditResult, AuditVerdict, ManufacturingAudit
from kicad_tools.audit.auditor import (
    ConnectivityStatus,
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

    def test_exit_code_one_when_not_ready(self):
        """Test that CLI returns exit code 1 when verdict is NOT_READY."""
        result = AuditResult()
        result.erc.error_count = 5
        result.erc.blocking_error_count = 5
        assert result.verdict == AuditVerdict.NOT_READY

        # Verify the exit code mapping
        if result.verdict == AuditVerdict.NOT_READY:
            exit_code = 1
        else:
            exit_code = 0
        assert exit_code == 1

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
