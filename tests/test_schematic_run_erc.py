"""Tests for Schematic.run_erc() method."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cli.runner import KiCadCLIResult
from kicad_tools.erc import ERCReport
from kicad_tools.exceptions import KiCadCLIError
from kicad_tools.schema.schematic import Schematic as SchemaSchematic
from kicad_tools.schematic.models.schematic import Schematic as BuilderSchematic


class TestSchemaSchematicRunERC:
    """Tests for schema.Schematic.run_erc() method."""

    def test_run_erc_no_path_raises_error(self, tmp_path: Path):
        """Test that run_erc raises ValueError when schematic has no path."""
        # Create a minimal schematic without a path
        from kicad_tools.sexp import SExp

        sexp = SExp.list("kicad_sch")
        sch = SchemaSchematic(sexp, path=None)

        with pytest.raises(ValueError, match="must be saved"):
            sch.run_erc()

    def test_run_erc_file_not_found_raises_error(self, tmp_path: Path):
        """Test that run_erc raises ValueError when file doesn't exist."""
        from kicad_tools.sexp import SExp

        sexp = SExp.list("kicad_sch")
        nonexistent = tmp_path / "nonexistent.kicad_sch"
        sch = SchemaSchematic(sexp, path=nonexistent)

        with pytest.raises(ValueError, match="not found"):
            sch.run_erc()

    @patch("kicad_tools.cli.runner.run_erc")
    def test_run_erc_cli_failure_raises_error(self, mock_run_erc, fixtures_dir: Path):
        """Test that run_erc raises KiCadCLIError on CLI failure."""
        # Load a real schematic
        schematic_path = fixtures_dir / "simple_rc.kicad_sch"
        sch = SchemaSchematic.load(schematic_path)

        # Mock CLI failure
        mock_run_erc.return_value = KiCadCLIResult(
            success=False,
            stderr="kicad-cli not found",
            return_code=1,
        )

        with pytest.raises(KiCadCLIError, match="ERC failed"):
            sch.run_erc()

    @patch("kicad_tools.erc.ERCReport.load")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_run_erc_success_returns_report(
        self, mock_run_erc, mock_load, fixtures_dir: Path, tmp_path: Path
    ):
        """Test that run_erc returns ERCReport on success."""
        # Load a real schematic
        schematic_path = fixtures_dir / "simple_rc.kicad_sch"
        sch = SchemaSchematic.load(schematic_path)

        # Create mock report file
        report_path = tmp_path / "erc_report.json"
        report_path.write_text('{"source": "test.kicad_sch", "sheets": []}')

        # Mock successful CLI call
        mock_run_erc.return_value = KiCadCLIResult(
            success=True,
            output_path=report_path,
            return_code=0,
        )

        # Mock ERCReport.load
        mock_report = MagicMock(spec=ERCReport)
        mock_report.error_count = 0
        mock_report.warning_count = 0
        mock_load.return_value = mock_report

        result = sch.run_erc()

        assert result is mock_report
        mock_run_erc.assert_called_once()
        mock_load.assert_called_once_with(report_path)

    @patch("kicad_tools.erc.ERCReport.load")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_run_erc_cleans_up_temp_file(
        self, mock_run_erc, mock_load, fixtures_dir: Path, tmp_path: Path
    ):
        """Test that run_erc cleans up temp file when no output_path specified."""
        # Load a real schematic
        schematic_path = fixtures_dir / "simple_rc.kicad_sch"
        sch = SchemaSchematic.load(schematic_path)

        # Create mock report file
        report_path = tmp_path / "erc_report.json"
        report_path.write_text('{"source": "test.kicad_sch", "sheets": []}')

        # Mock successful CLI call
        mock_run_erc.return_value = KiCadCLIResult(
            success=True,
            output_path=report_path,
            return_code=0,
        )

        # Mock ERCReport.load
        mock_report = MagicMock(spec=ERCReport)
        mock_load.return_value = mock_report

        sch.run_erc()

        # Temp file should be deleted
        assert not report_path.exists()

    @patch("kicad_tools.erc.ERCReport.load")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_run_erc_keeps_file_when_output_path_specified(
        self, mock_run_erc, mock_load, fixtures_dir: Path, tmp_path: Path
    ):
        """Test that run_erc keeps file when output_path is specified."""
        # Load a real schematic
        schematic_path = fixtures_dir / "simple_rc.kicad_sch"
        sch = SchemaSchematic.load(schematic_path)

        # Specify output path
        output_path = tmp_path / "my_report.json"
        output_path.write_text('{"source": "test.kicad_sch", "sheets": []}')

        # Mock successful CLI call
        mock_run_erc.return_value = KiCadCLIResult(
            success=True,
            output_path=output_path,
            return_code=0,
        )

        # Mock ERCReport.load
        mock_report = MagicMock(spec=ERCReport)
        mock_load.return_value = mock_report

        sch.run_erc(output_path=output_path)

        # File should still exist
        assert output_path.exists()


class TestBuilderSchematicRunERC:
    """Tests for schematic.models.Schematic.run_erc() method."""

    def test_run_erc_not_saved_raises_error(self):
        """Test that run_erc raises ValueError when schematic not saved."""
        sch = BuilderSchematic("Test")

        with pytest.raises(ValueError, match="must be saved"):
            sch.run_erc()

    @patch("kicad_tools.erc.ERCReport.load")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_run_erc_after_write_success(
        self, mock_run_erc, mock_load, tmp_path: Path
    ):
        """Test that run_erc works after write()."""
        sch = BuilderSchematic("Test Design")
        output_file = tmp_path / "test.kicad_sch"

        # Write the schematic
        sch.write(output_file)

        # Create mock report file
        report_path = tmp_path / "erc_report.json"
        report_path.write_text('{"source": "test.kicad_sch", "sheets": []}')

        # Mock successful CLI call
        mock_run_erc.return_value = KiCadCLIResult(
            success=True,
            output_path=report_path,
            return_code=0,
        )

        # Mock ERCReport.load
        mock_report = MagicMock(spec=ERCReport)
        mock_report.error_count = 0
        mock_report.warning_count = 2
        mock_load.return_value = mock_report

        result = sch.run_erc()

        assert result is mock_report
        mock_run_erc.assert_called_once()
        # Verify it was called with the correct schematic path
        call_args = mock_run_erc.call_args
        assert call_args[0][0] == output_file

    @patch("kicad_tools.cli.runner.run_erc")
    def test_run_erc_cli_failure_raises_error(self, mock_run_erc, tmp_path: Path):
        """Test that run_erc raises KiCadCLIError on CLI failure."""
        sch = BuilderSchematic("Test Design")
        output_file = tmp_path / "test.kicad_sch"
        sch.write(output_file)

        # Mock CLI failure
        mock_run_erc.return_value = KiCadCLIResult(
            success=False,
            stderr="kicad-cli not found",
            return_code=1,
        )

        with pytest.raises(KiCadCLIError, match="ERC failed"):
            sch.run_erc()


class TestRunERCIntegration:
    """Integration tests for run_erc() that require kicad-cli."""

    @pytest.fixture
    def skip_if_no_kicad_cli(self):
        """Skip test if kicad-cli is not installed."""
        from kicad_tools.cli.runner import find_kicad_cli

        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not installed")

    @pytest.mark.slow
    def test_run_erc_on_simple_schematic(
        self, fixtures_dir: Path, skip_if_no_kicad_cli
    ):
        """Test running actual ERC on a simple schematic."""
        schematic_path = fixtures_dir / "simple_rc.kicad_sch"

        if not schematic_path.exists():
            pytest.skip("simple_rc.kicad_sch fixture not found")

        sch = SchemaSchematic.load(schematic_path)
        report = sch.run_erc()

        # Should get a valid report back
        assert report is not None
        assert isinstance(report, ERCReport)
        assert hasattr(report, "error_count")
        assert hasattr(report, "warning_count")
        assert hasattr(report, "violations")
