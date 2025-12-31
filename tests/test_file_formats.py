"""Tests for additional KiCad file format support.

Tests for:
- .kicad_mod (footprint) loading and saving
- .kicad_dru (design rules) loading and saving
- CLI commands for footprint and design rules files
"""

import json
from pathlib import Path

import pytest

from kicad_tools.core.sexp_file import (
    load_design_rules,
    load_footprint,
    save_design_rules,
    save_footprint,
)
from kicad_tools.exceptions import FileFormatError
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError


class TestLoadFootprint:
    """Tests for load_footprint function."""

    def test_load_kicad6_footprint(self, minimal_footprint: Path):
        """Test loading a KiCad 6+ format footprint."""
        sexp = load_footprint(minimal_footprint)

        assert sexp.tag == "footprint"
        assert sexp.values[0] == "R_0402_1005Metric"

        # Check for pads
        pads = sexp.find_children("pad")
        assert len(pads) == 2

    def test_load_kicad5_footprint(self, minimal_footprint_kicad5: Path):
        """Test loading a KiCad 5 format footprint (module tag)."""
        sexp = load_footprint(minimal_footprint_kicad5)

        assert sexp.tag == "module"
        assert sexp.values[0] == "R_0402_1005Metric"

    def test_load_footprint_not_found(self, tmp_path: Path):
        """Test that loading a non-existent file raises appropriate error."""
        with pytest.raises(KiCadFileNotFoundError):
            load_footprint(tmp_path / "nonexistent.kicad_mod")

    def test_load_footprint_wrong_format(self, tmp_path: Path):
        """Test that loading a non-footprint file raises appropriate error."""
        wrong_file = tmp_path / "wrong.kicad_mod"
        wrong_file.write_text("(kicad_sch (version 1))")

        with pytest.raises(FileFormatError) as exc_info:
            load_footprint(wrong_file)

        assert "Not a KiCad footprint" in str(exc_info.value)


class TestSaveFootprint:
    """Tests for save_footprint function."""

    def test_save_footprint_roundtrip(self, minimal_footprint: Path, tmp_path: Path):
        """Test that saving and reloading a footprint preserves content."""
        # Load
        sexp = load_footprint(minimal_footprint)

        # Save to new location
        output_path = tmp_path / "output.kicad_mod"
        save_footprint(sexp, output_path)

        # Reload and verify
        reloaded = load_footprint(output_path)
        assert reloaded.tag == sexp.tag
        assert reloaded.values[0] == sexp.values[0]

        # Compare pad count
        assert len(reloaded.find_children("pad")) == len(sexp.find_children("pad"))

    def test_save_footprint_wrong_type(self, tmp_path: Path):
        """Test that saving non-footprint S-expression raises error."""
        from kicad_tools.sexp import SExp

        wrong_sexp = SExp.list("kicad_sch")

        with pytest.raises(FileFormatError):
            save_footprint(wrong_sexp, tmp_path / "wrong.kicad_mod")


class TestLoadDesignRules:
    """Tests for load_design_rules function."""

    def test_load_design_rules(self, minimal_design_rules: Path):
        """Test loading a design rules file."""
        sexp = load_design_rules(minimal_design_rules)

        assert sexp.tag == "design_rules"

        # Check for version
        version = sexp.find_child("version")
        assert version is not None
        assert version.values[0] == 1

        # Check for rules
        rules = sexp.find_children("rule")
        assert len(rules) == 6

    def test_load_design_rules_not_found(self, tmp_path: Path):
        """Test that loading a non-existent file raises appropriate error."""
        with pytest.raises(KiCadFileNotFoundError):
            load_design_rules(tmp_path / "nonexistent.kicad_dru")

    def test_load_design_rules_empty(self, tmp_path: Path):
        """Test that loading an empty file raises appropriate error."""
        empty_file = tmp_path / "empty.kicad_dru"
        empty_file.write_text("")

        with pytest.raises(FileFormatError) as exc_info:
            load_design_rules(empty_file)

        assert "Empty design rules" in str(exc_info.value)

    def test_load_design_rules_no_version(self, tmp_path: Path):
        """Test that loading a file without version raises appropriate error."""
        no_version = tmp_path / "no_version.kicad_dru"
        no_version.write_text('(rule "Test" (constraint clearance))')

        with pytest.raises(FileFormatError) as exc_info:
            load_design_rules(no_version)

        assert "Invalid design rules" in str(exc_info.value)


class TestSaveDesignRules:
    """Tests for save_design_rules function."""

    def test_save_design_rules_roundtrip(self, minimal_design_rules: Path, tmp_path: Path):
        """Test that saving and reloading design rules preserves content."""
        # Load
        sexp = load_design_rules(minimal_design_rules)

        # Save to new location
        output_path = tmp_path / "output.kicad_dru"
        save_design_rules(sexp, output_path)

        # Reload and verify
        reloaded = load_design_rules(output_path)
        assert len(reloaded.find_children("rule")) == len(sexp.find_children("rule"))

    def test_save_design_rules_wrong_type(self, tmp_path: Path):
        """Test that saving non-design-rules S-expression raises error."""
        from kicad_tools.sexp import SExp

        wrong_sexp = SExp.list("footprint", "test")

        with pytest.raises(FileFormatError):
            save_design_rules(wrong_sexp, tmp_path / "wrong.kicad_dru")


class TestLibFootprintsCLI:
    """Tests for lib footprints CLI commands."""

    def test_list_footprints(self, footprint_library_dir: Path, capsys):
        """Test listing footprints in a directory."""
        from kicad_tools.cli.lib_footprints import list_footprints

        result = list_footprints(footprint_library_dir, "table")
        assert result == 0

        captured = capsys.readouterr()
        assert "R_0402_1005Metric" in captured.out
        assert "C_0402_1005Metric" in captured.out
        assert "2 footprints" in captured.out

    def test_list_footprints_json(self, footprint_library_dir: Path, capsys):
        """Test listing footprints in JSON format."""
        from kicad_tools.cli.lib_footprints import list_footprints

        result = list_footprints(footprint_library_dir, "json")
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 2
        assert any(fp["name"] == "R_0402_1005Metric" for fp in data)

    def test_show_footprint(self, minimal_footprint: Path, capsys):
        """Test showing footprint details."""
        from kicad_tools.cli.lib_footprints import show_footprint

        result = show_footprint(minimal_footprint, "text", show_pads=False)
        assert result == 0

        captured = capsys.readouterr()
        assert "R_0402_1005Metric" in captured.out
        assert "Pads: 2" in captured.out

    def test_show_footprint_with_pads(self, minimal_footprint: Path, capsys):
        """Test showing footprint details with pad information."""
        from kicad_tools.cli.lib_footprints import show_footprint

        result = show_footprint(minimal_footprint, "text", show_pads=True)
        assert result == 0

        captured = capsys.readouterr()
        assert "Pad Details" in captured.out
        assert "smd" in captured.out

    def test_show_footprint_json(self, minimal_footprint: Path, capsys):
        """Test showing footprint details in JSON format."""
        from kicad_tools.cli.lib_footprints import show_footprint

        result = show_footprint(minimal_footprint, "json", show_pads=True)
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["name"] == "R_0402_1005Metric"
        assert data["pad_count"] == 2
        assert "pads" in data


class TestMfrDruCLI:
    """Tests for mfr import-dru CLI command."""

    def test_import_dru(self, minimal_design_rules: Path, capsys):
        """Test importing a design rules file."""
        from kicad_tools.cli.mfr_dru import import_dru

        result = import_dru(minimal_design_rules, "text")
        assert result == 0

        captured = capsys.readouterr()
        assert "Version: 1" in captured.out
        assert "Trace Width" in captured.out
        assert "Clearance" in captured.out
        assert "6 total" in captured.out

    def test_import_dru_json(self, minimal_design_rules: Path, capsys):
        """Test importing a design rules file in JSON format."""
        from kicad_tools.cli.mfr_dru import import_dru

        result = import_dru(minimal_design_rules, "json")
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["version"] == 1
        assert len(data["rules"]) == 6
        assert any(r["name"] == "Trace Width" for r in data["rules"])

    def test_import_dru_constraint_values(self, minimal_design_rules: Path, capsys):
        """Test that constraint values are properly parsed."""
        from kicad_tools.cli.mfr_dru import import_dru

        result = import_dru(minimal_design_rules, "json")
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Find the trace width rule
        trace_rule = next(r for r in data["rules"] if r["name"] == "Trace Width")
        assert trace_rule["constraint"]["type"] == "track_width"
        assert trace_rule["constraint"]["min"]["value"] == 0.127
        assert trace_rule["constraint"]["min"]["unit"] == "mm"
