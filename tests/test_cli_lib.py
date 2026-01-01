"""Tests for kicad_tools lib CLI commands."""

import json

import pytest


def _kicad_installed() -> bool:
    """Check if KiCad standard libraries are available."""
    from kicad_tools.schematic.grid import KICAD_SYMBOL_PATHS

    return any(path.exists() and any(path.glob("*.kicad_sym")) for path in KICAD_SYMBOL_PATHS)


class TestLibListCommand:
    """Tests for the lib list command."""

    def test_lib_list_no_options(self, capsys):
        """Test lib list with no options shows both symbol and footprint libraries."""
        from kicad_tools.cli import main

        result = main(["lib", "list"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Symbol Libraries" in captured.out
        assert "Footprint Libraries" in captured.out

    def test_lib_list_symbols_only(self, capsys):
        """Test lib list --symbols shows only symbol libraries."""
        from kicad_tools.cli import main

        result = main(["lib", "list", "--symbols"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Symbol Libraries" in captured.out
        assert "Footprint Libraries" not in captured.out

    def test_lib_list_footprints_only(self, capsys):
        """Test lib list --footprints shows only footprint libraries."""
        from kicad_tools.cli import main

        result = main(["lib", "list", "--footprints"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Footprint Libraries" in captured.out
        assert "Symbol Libraries" not in captured.out

    def test_lib_list_json_format(self, capsys):
        """Test lib list with JSON output."""
        from kicad_tools.cli import main

        result = main(["lib", "list", "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "symbols" in data or "footprints" in data


class TestLibSymbolInfoCommand:
    """Tests for the lib symbol-info command."""

    def test_symbol_info_not_found_library(self, capsys):
        """Test symbol-info with non-existent library."""
        from kicad_tools.cli import main

        result = main(["lib", "symbol-info", "NonExistent", "R"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    def test_symbol_info_not_found_symbol(self, capsys):
        """Test symbol-info with non-existent symbol in valid library."""
        from kicad_tools.cli import main

        # Device library should exist in standard KiCad installation
        result = main(["lib", "symbol-info", "Device", "NonExistentSymbol"])

        # May succeed or fail depending on KiCad installation
        # If library found, symbol should not be found
        if result == 1:
            captured = capsys.readouterr()
            assert "not found" in captured.err.lower() or "not found" in captured.out.lower()

    @pytest.mark.skipif(
        not _kicad_installed(),
        reason="KiCad symbols not installed",
    )
    def test_symbol_info_device_r(self, capsys):
        """Test symbol-info for Device:R (common resistor symbol)."""
        from kicad_tools.cli import main

        result = main(["lib", "symbol-info", "Device", "R"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Symbol: R" in captured.out
        assert "Pin count:" in captured.out

    @pytest.mark.skipif(
        not _kicad_installed(),
        reason="KiCad symbols not installed",
    )
    def test_symbol_info_json_format(self, capsys):
        """Test symbol-info with JSON output."""
        from kicad_tools.cli import main

        result = main(["lib", "symbol-info", "Device", "R", "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["name"] == "R"
        assert "pin_count" in data

    @pytest.mark.skipif(
        not _kicad_installed(),
        reason="KiCad symbols not installed",
    )
    def test_symbol_info_with_pins(self, capsys):
        """Test symbol-info with --pins option."""
        from kicad_tools.cli import main

        result = main(["lib", "symbol-info", "Device", "R", "--pins"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Pins:" in captured.out


class TestLibFootprintInfoCommand:
    """Tests for the lib footprint-info command."""

    def test_footprint_info_not_found_library(self, capsys):
        """Test footprint-info with non-existent library."""
        from kicad_tools.cli import main

        result = main(["lib", "footprint-info", "NonExistent", "C_0402"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    @pytest.mark.skipif(
        not _kicad_installed(),
        reason="KiCad footprints not installed",
    )
    def test_footprint_info_capacitor(self, capsys):
        """Test footprint-info for standard capacitor footprint."""
        from kicad_tools.cli import main

        result = main(["lib", "footprint-info", "Capacitor_SMD", "C_0402_1005Metric"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Footprint:" in captured.out
        assert "Pads:" in captured.out


class TestLibPlaceholderCommands:
    """Tests for placeholder commands (not yet implemented)."""

    def test_create_symbol_lib_not_implemented(self, capsys):
        """Test create-symbol-lib returns exit code 2."""
        from kicad_tools.cli import main

        result = main(["lib", "create-symbol-lib", "test.kicad_sym"])
        assert result == 2

        captured = capsys.readouterr()
        assert "not yet implemented" in captured.err.lower()
        # Issue reference is printed to stdout
        assert "#85" in captured.out or "issues/85" in captured.out

    def test_create_footprint_lib_not_implemented(self, capsys):
        """Test create-footprint-lib returns exit code 2."""
        from kicad_tools.cli import main

        result = main(["lib", "create-footprint-lib", "Test.pretty"])
        assert result == 2

        captured = capsys.readouterr()
        assert "not yet implemented" in captured.err.lower()
        assert "#87" in captured.out or "issues/87" in captured.out

    def test_generate_footprint_not_implemented(self, capsys):
        """Test generate-footprint returns exit code 2."""
        from kicad_tools.cli import main

        result = main(["lib", "generate-footprint", "Test.pretty", "soic", "--pins", "8"])
        assert result == 2

        captured = capsys.readouterr()
        assert "not yet implemented" in captured.err.lower()
        assert "#88" in captured.out or "issues/88" in captured.out


class TestLibExportCommand:
    """Tests for the lib export command."""

    def test_export_not_found(self, capsys):
        """Test export with non-existent file."""
        from kicad_tools.cli import main

        result = main(["lib", "export", "nonexistent.kicad_sym"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    def test_export_unsupported_type(self, tmp_path, capsys):
        """Test export with unsupported file type."""
        from kicad_tools.cli import main

        # Create a dummy file
        dummy = tmp_path / "test.txt"
        dummy.write_text("test")

        result = main(["lib", "export", str(dummy)])
        assert result == 1

        captured = capsys.readouterr()
        assert "unsupported" in captured.err.lower()
