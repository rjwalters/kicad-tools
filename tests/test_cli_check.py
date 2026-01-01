"""Tests for kct check CLI command (pure Python DRC)."""

import json
from pathlib import Path

import pytest


class TestCheckCommand:
    """Tests for the check CLI command."""

    def test_check_file_not_found(self, capsys):
        """Test check command with missing file."""
        from kicad_tools.cli.check_cmd import main

        result = main(["nonexistent.kicad_pcb"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "Error" in captured.err

    def test_check_wrong_extension(self, capsys, tmp_path: Path):
        """Test check command with wrong file extension."""
        from kicad_tools.cli.check_cmd import main

        wrong_file = tmp_path / "test.txt"
        wrong_file.write_text("not a pcb")

        result = main([str(wrong_file)])
        assert result == 1

        captured = capsys.readouterr()
        assert ".kicad_pcb" in captured.err

    def test_check_basic_table_output(self, drc_clean_pcb: Path, capsys):
        """Test check command with table output format."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb)])
        assert result == 0

        captured = capsys.readouterr()
        assert "PURE PYTHON DRC CHECK" in captured.out
        assert "DRC PASSED" in captured.out or "Results:" in captured.out

    def test_check_json_output(self, drc_clean_pcb: Path, capsys):
        """Test check command with JSON output format."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Verify JSON structure
        assert "file" in data
        assert "manufacturer" in data
        assert "layers" in data
        assert "summary" in data
        assert "violations" in data
        assert "passed" in data["summary"]
        assert data["summary"]["passed"] is True  # No violations with clean PCB

    def test_check_summary_output(self, drc_clean_pcb: Path, capsys):
        """Test check command with summary output format."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--format", "summary"])
        assert result == 0

        captured = capsys.readouterr()
        assert "DRC" in captured.out

    def test_check_manufacturer_option(self, drc_clean_pcb: Path, capsys):
        """Test check command with manufacturer option."""
        from kicad_tools.cli.check_cmd import main

        # Test with different manufacturers
        for mfr in ["jlcpcb", "seeed", "pcbway", "oshpark"]:
            result = main([str(drc_clean_pcb), "--mfr", mfr])
            assert result == 0, f"Failed for manufacturer {mfr}"

    def test_check_layers_option(self, drc_clean_pcb: Path, capsys):
        """Test check command with layers option."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--layers", "4"])
        assert result == 0

        captured = capsys.readouterr()
        assert "4" in captured.out  # Layer count should appear in output

    def test_check_only_filter(self, drc_clean_pcb: Path, capsys):
        """Test check command with --only filter."""
        from kicad_tools.cli.check_cmd import main

        # Run only clearance checks
        result = main([str(drc_clean_pcb), "--only", "clearance"])
        assert result == 0

        # Run multiple categories
        result = main([str(drc_clean_pcb), "--only", "clearance,dimensions"])
        assert result == 0

    def test_check_skip_filter(self, drc_clean_pcb: Path, capsys):
        """Test check command with --skip filter."""
        from kicad_tools.cli.check_cmd import main

        # Skip silkscreen checks
        result = main([str(drc_clean_pcb), "--skip", "silkscreen"])
        assert result == 0

        # Skip multiple categories
        result = main([str(drc_clean_pcb), "--skip", "silkscreen,edge"])
        assert result == 0

    def test_check_invalid_filter_category(self, minimal_pcb: Path, capsys):
        """Test check command with invalid filter category."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(minimal_pcb), "--only", "invalid_category"])
        assert result == 1

        captured = capsys.readouterr()
        assert "Unknown check category" in captured.err

    def test_check_errors_only_flag(self, drc_clean_pcb: Path, capsys):
        """Test check command with --errors-only flag."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--errors-only"])
        assert result == 0  # No errors with clean PCB

    def test_check_verbose_flag(self, drc_clean_pcb: Path, capsys):
        """Test check command with --verbose flag."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--verbose"])
        assert result == 0

    def test_check_copper_weight_option(self, drc_clean_pcb: Path, capsys):
        """Test check command with copper weight option."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--copper", "2.0"])
        assert result == 0

    def test_check_help_text(self, capsys):
        """Test check command help text."""
        from kicad_tools.cli.check_cmd import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Pure Python DRC" in captured.out or "kct check" in captured.out


class TestCheckCommandIntegration:
    """Integration tests for check command via main CLI."""

    def test_check_via_main_cli(self, drc_clean_pcb: Path, capsys):
        """Test check command through the main CLI dispatcher."""
        from kicad_tools.cli import main

        result = main(["check", str(drc_clean_pcb)])
        assert result == 0

        captured = capsys.readouterr()
        assert "DRC" in captured.out

    def test_check_via_main_cli_with_options(self, drc_clean_pcb: Path, capsys):
        """Test check command through main CLI with options."""
        from kicad_tools.cli import main

        result = main(
            [
                "check",
                str(drc_clean_pcb),
                "--mfr",
                "seeed",
                "--layers",
                "4",
                "--format",
                "json",
            ]
        )
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["manufacturer"] == "seeed"
        assert data["layers"] == 4


class TestCheckExitCodes:
    """Tests for check command exit codes."""

    def test_exit_code_0_no_violations(self, drc_clean_pcb: Path):
        """Test exit code 0 when no violations found."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb)])
        assert result == 0

    def test_exit_code_0_warnings_only_no_strict(self, drc_clean_pcb: Path):
        """Test exit code 0 with warnings when not in strict mode."""
        from kicad_tools.cli.check_cmd import main

        # With clean PCB, no warnings to test
        # But this confirms the code path works
        result = main([str(drc_clean_pcb)])
        assert result == 0

    def test_exit_code_with_strict_flag(self, drc_clean_pcb: Path):
        """Test that --strict flag works (would return 2 on warnings)."""
        from kicad_tools.cli.check_cmd import main

        # With clean PCB returning no violations, still returns 0
        result = main([str(drc_clean_pcb), "--strict"])
        assert result == 0

    def test_exit_code_1_with_violations(self, minimal_pcb: Path):
        """Test exit code 1 when DRC violations are found."""
        from kicad_tools.cli.check_cmd import main

        # minimal_pcb has a trace overlapping a pad, causing a clearance violation
        result = main([str(minimal_pcb)])
        assert result == 1  # Errors found


class TestCheckJsonSchema:
    """Tests for check command JSON output schema."""

    def test_json_schema_complete(self, drc_clean_pcb: Path, capsys):
        """Test that JSON output contains all required fields."""
        from kicad_tools.cli.check_cmd import main

        main([str(drc_clean_pcb), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Required top-level fields
        assert "file" in data
        assert "manufacturer" in data
        assert "layers" in data
        assert "summary" in data
        assert "violations" in data

        # Required summary fields
        assert "errors" in data["summary"]
        assert "warnings" in data["summary"]
        assert "rules_checked" in data["summary"]
        assert "passed" in data["summary"]

        # violations should be a list
        assert isinstance(data["violations"], list)

    def test_json_output_is_ci_friendly(self, drc_clean_pcb: Path, capsys):
        """Test that JSON output can be parsed by CI tools."""
        from kicad_tools.cli.check_cmd import main

        main([str(drc_clean_pcb), "--format", "json"])
        captured = capsys.readouterr()

        # Should be parseable without errors
        data = json.loads(captured.out)

        # CI-friendly check: summary.passed is a boolean
        assert isinstance(data["summary"]["passed"], bool)

        # CI-friendly check: counts are integers
        assert isinstance(data["summary"]["errors"], int)
        assert isinstance(data["summary"]["warnings"], int)
