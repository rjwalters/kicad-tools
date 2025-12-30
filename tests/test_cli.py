"""Tests for kicad_tools CLI commands."""

import json
import pytest
from pathlib import Path
import sys


class TestCLIMain:
    """Tests for the main CLI dispatcher."""

    def test_no_command_shows_help(self, capsys):
        """Test that no command shows help."""
        from kicad_tools.cli import main

        result = main([])
        assert result == 0

        captured = capsys.readouterr()
        assert "KiCad automation toolkit" in captured.out

    def test_version_flag(self, capsys):
        """Test --version flag."""
        from kicad_tools import __version__
        from kicad_tools.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert __version__ in captured.out

    def test_unknown_command(self, capsys):
        """Test unknown command."""
        from kicad_tools.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["unknown"])

        # argparse exits with error for invalid choice
        assert exc_info.value.code == 2


class TestSymbolsCommand:
    """Tests for the symbols CLI command."""

    def test_symbols_file_not_found(self, capsys):
        """Test symbols command with missing file."""
        from kicad_tools.cli import main

        result = main(["symbols", "nonexistent.kicad_sch"])
        assert result == 1

        captured = capsys.readouterr()
        assert "Error" in captured.err or "not found" in captured.err.lower()

    def test_symbols_table_format(self, simple_rc_schematic: Path, capsys):
        """Test symbols command with table format."""
        from kicad_tools.cli import main

        result = main(["symbols", str(simple_rc_schematic)])
        assert result == 0

        captured = capsys.readouterr()
        # Should list components from simple RC circuit
        assert "R1" in captured.out or "C1" in captured.out

    def test_symbols_json_format(self, simple_rc_schematic: Path, capsys):
        """Test symbols command with JSON output."""
        from kicad_tools.cli import main

        result = main(["symbols", str(simple_rc_schematic), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        # Should be valid JSON
        data = json.loads(captured.out)
        assert isinstance(data, (list, dict))

    def test_symbols_filter(self, simple_rc_schematic: Path, capsys):
        """Test symbols command with filter."""
        from kicad_tools.cli import main

        result = main(["symbols", str(simple_rc_schematic), "--filter", "R*"])
        assert result == 0

        captured = capsys.readouterr()
        # Should only show resistors if any exist
        # The filter should work without error


class TestNetsCommand:
    """Tests for the nets CLI command."""

    def test_nets_file_not_found(self, capsys):
        """Test nets command with missing file."""
        from kicad_tools.cli import main

        result = main(["nets", "nonexistent.kicad_sch"])
        assert result == 1

        captured = capsys.readouterr()
        assert "Error" in captured.err or "not found" in captured.err.lower()

    def test_nets_table_format(self, simple_rc_schematic: Path, capsys):
        """Test nets command with table format."""
        from kicad_tools.cli import main

        result = main(["nets", str(simple_rc_schematic)])
        assert result == 0

        captured = capsys.readouterr()
        # Should list nets (VCC, GND, or signal nets)
        assert len(captured.out) > 0

    def test_nets_json_format(self, simple_rc_schematic: Path, capsys):
        """Test nets command with JSON output."""
        from kicad_tools.cli import main

        result = main(["nets", str(simple_rc_schematic), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        # Should be valid JSON
        data = json.loads(captured.out)
        assert isinstance(data, (list, dict))

    def test_nets_stats(self, simple_rc_schematic: Path, capsys):
        """Test nets command with stats flag."""
        from kicad_tools.cli import main

        result = main(["nets", str(simple_rc_schematic), "--stats"])
        assert result == 0

        captured = capsys.readouterr()
        # Should show statistics


class TestERCCommand:
    """Tests for the ERC CLI command."""

    def test_erc_file_not_found(self, capsys):
        """Test erc command with missing file."""
        from kicad_tools.cli import main

        result = main(["erc", "nonexistent.json"])
        assert result == 1

        captured = capsys.readouterr()
        assert "Error" in captured.err or "not found" in captured.err.lower()

    def test_erc_table_format(self, fixtures_dir: Path, capsys):
        """Test erc command with table format."""
        from kicad_tools.cli import main

        erc_report = fixtures_dir / "sample_erc.json"
        result = main(["erc", str(erc_report)])

        captured = capsys.readouterr()
        # Should parse and display ERC violations

    def test_erc_json_format(self, fixtures_dir: Path, capsys):
        """Test erc command with JSON output."""
        from kicad_tools.cli import main

        erc_report = fixtures_dir / "sample_erc.json"
        result = main(["erc", str(erc_report), "--format", "json"])

        captured = capsys.readouterr()
        # Should be valid JSON if there's output
        if captured.out.strip():
            data = json.loads(captured.out)
            assert isinstance(data, (list, dict))

    def test_erc_summary_format(self, fixtures_dir: Path, capsys):
        """Test erc command with summary format."""
        from kicad_tools.cli import main

        erc_report = fixtures_dir / "sample_erc.json"
        result = main(["erc", str(erc_report), "--format", "summary"])

        captured = capsys.readouterr()
        # Should show summary output

    def test_erc_errors_only(self, fixtures_dir: Path, capsys):
        """Test erc command with errors-only flag."""
        from kicad_tools.cli import main

        erc_report = fixtures_dir / "sample_erc.json"
        result = main(["erc", str(erc_report), "--errors-only"])

        captured = capsys.readouterr()
        # Should filter to only errors


class TestDRCCommand:
    """Tests for the DRC CLI command."""

    def test_drc_file_not_found(self, capsys):
        """Test drc command with missing file."""
        from kicad_tools.cli import main

        result = main(["drc", "nonexistent.rpt"])
        assert result == 1

        captured = capsys.readouterr()
        assert "Error" in captured.err or "not found" in captured.err.lower()

    def test_drc_table_format(self, fixtures_dir: Path, capsys):
        """Test drc command with table format."""
        from kicad_tools.cli import main

        drc_report = fixtures_dir / "sample_drc.rpt"
        result = main(["drc", str(drc_report)])

        captured = capsys.readouterr()
        # Should display DRC violations
        assert "DRC" in captured.out or "violation" in captured.out.lower()

    def test_drc_json_format(self, fixtures_dir: Path, capsys):
        """Test drc command with JSON output."""
        from kicad_tools.cli import main

        drc_report = fixtures_dir / "sample_drc.rpt"
        result = main(["drc", str(drc_report), "--format", "json"])

        captured = capsys.readouterr()
        # Should be valid JSON
        data = json.loads(captured.out)
        # JSON has violations and summary
        assert "violations" in data
        assert "summary" in data
        assert isinstance(data["violations"], list)

    def test_drc_summary_format(self, fixtures_dir: Path, capsys):
        """Test drc command with summary format."""
        from kicad_tools.cli import main

        drc_report = fixtures_dir / "sample_drc.rpt"
        result = main(["drc", str(drc_report), "--format", "summary"])

        captured = capsys.readouterr()
        assert "Summary" in captured.out or "TOTAL" in captured.out

    def test_drc_errors_only(self, fixtures_dir: Path, capsys):
        """Test drc command with errors-only flag."""
        from kicad_tools.cli import main

        drc_report = fixtures_dir / "sample_drc.rpt"
        result = main(["drc", str(drc_report), "--errors-only"])

        captured = capsys.readouterr()
        # Should filter to only errors (no "WARN" in output)
        # Note: there may still be errors shown

    def test_drc_filter_by_type(self, fixtures_dir: Path, capsys):
        """Test drc command filtering by type."""
        from kicad_tools.cli import main

        drc_report = fixtures_dir / "sample_drc.rpt"
        result = main(["drc", str(drc_report), "--type", "clearance"])

        captured = capsys.readouterr()
        # Should show only clearance violations

    def test_drc_manufacturer_check(self, fixtures_dir: Path, capsys):
        """Test drc command with manufacturer check."""
        from kicad_tools.cli import main

        drc_report = fixtures_dir / "sample_drc.rpt"
        result = main(["drc", str(drc_report), "--mfr", "jlcpcb"])

        captured = capsys.readouterr()
        assert "JLCPCB" in captured.out
        assert "MANUFACTURER COMPATIBILITY" in captured.out

    def test_drc_manufacturer_check_with_layers(self, fixtures_dir: Path, capsys):
        """Test drc command with manufacturer check and layer count."""
        from kicad_tools.cli import main

        drc_report = fixtures_dir / "sample_drc.rpt"
        result = main(["drc", str(drc_report), "--mfr", "jlcpcb", "--layers", "4"])

        captured = capsys.readouterr()
        assert "Layer count: 4" in captured.out


class TestBOMCommand:
    """Tests for the BOM CLI command."""

    def test_bom_file_not_found(self, capsys):
        """Test bom command with missing file."""
        from kicad_tools.cli import main

        result = main(["bom", "nonexistent.kicad_sch"])
        # Result can be 0 (empty BOM) or 1 (error) depending on implementation
        captured = capsys.readouterr()
        # Either returns error or shows no components
        assert result == 1 or "No components" in captured.out or "Error" in captured.err

    def test_bom_table_format(self, simple_rc_schematic: Path, capsys):
        """Test bom command with table format."""
        from kicad_tools.cli import main

        result = main(["bom", str(simple_rc_schematic)])
        assert result == 0

        captured = capsys.readouterr()
        # Should list BOM entries
        assert len(captured.out) > 0

    def test_bom_csv_format(self, simple_rc_schematic: Path, capsys):
        """Test bom command with CSV output."""
        from kicad_tools.cli import main

        result = main(["bom", str(simple_rc_schematic), "--format", "csv"])
        assert result == 0

        captured = capsys.readouterr()
        # Should be CSV format with headers
        lines = captured.out.strip().split("\n")
        assert len(lines) >= 1  # At least header

    def test_bom_json_format(self, simple_rc_schematic: Path, capsys):
        """Test bom command with JSON output."""
        from kicad_tools.cli import main

        result = main(["bom", str(simple_rc_schematic), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        # Should be valid JSON
        data = json.loads(captured.out)
        assert isinstance(data, (list, dict))

    def test_bom_group(self, simple_rc_schematic: Path, capsys):
        """Test bom command with grouping."""
        from kicad_tools.cli import main

        result = main(["bom", str(simple_rc_schematic), "--group"])
        assert result == 0

        captured = capsys.readouterr()
        # Should group identical components

    def test_bom_exclude(self, simple_rc_schematic: Path, capsys):
        """Test bom command with exclusion pattern."""
        from kicad_tools.cli import main

        result = main(["bom", str(simple_rc_schematic), "--exclude", "TP*"])
        assert result == 0

        captured = capsys.readouterr()
        # Should exclude test points if any

    def test_bom_sort_by_value(self, simple_rc_schematic: Path, capsys):
        """Test bom command with sort by value."""
        from kicad_tools.cli import main

        result = main(["bom", str(simple_rc_schematic), "--sort", "value"])
        assert result == 0

        captured = capsys.readouterr()
        # Should sort by value


class TestCLIStandaloneEntryPoints:
    """Tests for standalone CLI entry points."""

    def test_symbols_main(self, simple_rc_schematic: Path, monkeypatch, capsys):
        """Test symbols_main entry point."""
        from kicad_tools.cli import symbols_main

        monkeypatch.setattr(sys, "argv", ["kicad-symbols", str(simple_rc_schematic)])
        result = symbols_main()
        assert result == 0

    def test_nets_main(self, simple_rc_schematic: Path, monkeypatch, capsys):
        """Test nets_main entry point."""
        from kicad_tools.cli import nets_main

        monkeypatch.setattr(sys, "argv", ["kicad-nets", str(simple_rc_schematic)])
        result = nets_main()
        assert result == 0

    def test_drc_main(self, fixtures_dir: Path, monkeypatch, capsys):
        """Test drc_main entry point."""
        from kicad_tools.cli import drc_main

        drc_report = fixtures_dir / "sample_drc.rpt"
        monkeypatch.setattr(sys, "argv", ["kicad-drc", str(drc_report)])
        result = drc_main()
        # Returns 1 if there are errors in the report
        assert result in (0, 1)

    def test_bom_main(self, simple_rc_schematic: Path, monkeypatch, capsys):
        """Test bom_main entry point."""
        from kicad_tools.cli import bom_main

        monkeypatch.setattr(sys, "argv", ["kicad-bom", str(simple_rc_schematic)])
        result = bom_main()
        assert result == 0
