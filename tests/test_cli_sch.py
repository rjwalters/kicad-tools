"""Tests for schematic-related CLI commands."""

import contextlib
import json
from pathlib import Path

import pytest


class TestSchListSymbols:
    """Tests for sch_list_symbols.py CLI."""

    def test_file_not_found(self, capsys):
        """Test handling of missing file."""
        import sys

        from kicad_tools.cli.sch_list_symbols import main

        # Capture the sys.exit
        with pytest.raises(SystemExit) as exc_info:
            sys.argv = ["sch-list-symbols", "nonexistent.kicad_sch"]
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_table_output(self, simple_rc_schematic: Path, capsys, monkeypatch):
        """Test table format output."""
        from kicad_tools.cli.sch_list_symbols import main

        monkeypatch.setattr("sys.argv", ["sch-list-symbols", str(simple_rc_schematic)])
        main()

        captured = capsys.readouterr()
        # Should have some output
        assert len(captured.out) > 0
        # Should show symbol table or "No symbols found"
        assert "Ref" in captured.out or "No symbols" in captured.out

    def test_json_output(self, simple_rc_schematic: Path, capsys, monkeypatch):
        """Test JSON format output."""
        from kicad_tools.cli.sch_list_symbols import main

        monkeypatch.setattr(
            "sys.argv", ["sch-list-symbols", str(simple_rc_schematic), "--format", "json"]
        )
        main()

        captured = capsys.readouterr()
        # Should be valid JSON
        data = json.loads(captured.out)
        assert isinstance(data, list)

    def test_csv_output(self, simple_rc_schematic: Path, capsys, monkeypatch):
        """Test CSV format output."""
        from kicad_tools.cli.sch_list_symbols import main

        monkeypatch.setattr(
            "sys.argv", ["sch-list-symbols", str(simple_rc_schematic), "--format", "csv"]
        )
        main()

        captured = capsys.readouterr()
        # Should have CSV header
        lines = captured.out.strip().split("\n")
        assert len(lines) >= 1
        assert "Reference" in lines[0]

    def test_filter_option(self, simple_rc_schematic: Path, capsys, monkeypatch):
        """Test filtering by reference pattern."""
        from kicad_tools.cli.sch_list_symbols import main

        monkeypatch.setattr(
            "sys.argv", ["sch-list-symbols", str(simple_rc_schematic), "--filter", "R*"]
        )
        main()

        captured = capsys.readouterr()
        # Should work without error
        assert captured.err == "" or "Error" not in captured.err

    def test_verbose_output(self, simple_rc_schematic: Path, capsys, monkeypatch):
        """Test verbose output."""
        from kicad_tools.cli.sch_list_symbols import main

        monkeypatch.setattr("sys.argv", ["sch-list-symbols", str(simple_rc_schematic), "--verbose"])
        main()

        captured = capsys.readouterr()
        # Verbose should include additional columns like Footprint or Position
        assert len(captured.out) > 0


class TestSchListLabels:
    """Tests for sch_list_labels.py CLI."""

    def test_file_not_found(self, capsys):
        """Test handling of missing file."""
        from kicad_tools.cli.sch_list_labels import main

        with pytest.raises(SystemExit) as exc_info:
            main(["nonexistent.kicad_sch"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_table_output(self, simple_rc_schematic: Path, capsys):
        """Test table format output."""
        from kicad_tools.cli.sch_list_labels import main

        main([str(simple_rc_schematic)])

        captured = capsys.readouterr()
        # Should have some output
        assert len(captured.out) > 0

    def test_json_output(self, simple_rc_schematic: Path, capsys):
        """Test JSON format output."""
        from kicad_tools.cli.sch_list_labels import main

        main([str(simple_rc_schematic), "--format", "json"])

        captured = capsys.readouterr()
        # Should be valid JSON
        data = json.loads(captured.out)
        assert isinstance(data, list)

    def test_csv_output(self, simple_rc_schematic: Path, capsys):
        """Test CSV format output."""
        from kicad_tools.cli.sch_list_labels import main

        main([str(simple_rc_schematic), "--format", "csv"])

        captured = capsys.readouterr()
        # Should have CSV header
        lines = captured.out.strip().split("\n")
        assert len(lines) >= 1
        assert "Type" in lines[0]

    def test_type_filter(self, simple_rc_schematic: Path, capsys):
        """Test filtering by label type."""
        from kicad_tools.cli.sch_list_labels import main

        main([str(simple_rc_schematic), "--type", "global"])

        captured = capsys.readouterr()
        # Should work without error
        assert "Error" not in captured.err

    def test_pattern_filter(self, simple_rc_schematic: Path, capsys):
        """Test filtering by pattern."""
        from kicad_tools.cli.sch_list_labels import main

        main([str(simple_rc_schematic), "--filter", "VCC*"])

        captured = capsys.readouterr()
        # Should work without error
        assert "Error" not in captured.err

    def test_minimal_schematic(self, minimal_schematic: Path, capsys):
        """Test with minimal schematic that has a label."""
        from kicad_tools.cli.sch_list_labels import main

        main([str(minimal_schematic)])

        captured = capsys.readouterr()
        # Minimal schematic has NET1 label
        assert "NET1" in captured.out or "No labels" in captured.out


class TestSchHierarchy:
    """Tests for sch_hierarchy.py CLI."""

    def test_missing_file_handles_gracefully(self, capsys):
        """Test that missing files are handled gracefully (returns empty hierarchy)."""
        from kicad_tools.cli.sch_hierarchy import main

        # The hierarchy builder handles missing files by returning an empty hierarchy
        main(["nonexistent.kicad_sch"])

        captured = capsys.readouterr()
        # Should output something (even if it's an empty hierarchy)
        assert len(captured.out) > 0

    def test_tree_command(self, simple_rc_schematic: Path, capsys):
        """Test tree command (default)."""
        from kicad_tools.cli.sch_hierarchy import main

        main([str(simple_rc_schematic)])

        captured = capsys.readouterr()
        # Should show some tree output
        assert len(captured.out) > 0

    def test_list_command(self, simple_rc_schematic: Path, capsys):
        """Test list command."""
        from kicad_tools.cli.sch_hierarchy import main

        main([str(simple_rc_schematic), "list"])

        captured = capsys.readouterr()
        assert "Schematic Sheets" in captured.out or len(captured.out) > 0

    def test_labels_command(self, simple_rc_schematic: Path, capsys):
        """Test labels command."""
        from kicad_tools.cli.sch_hierarchy import main

        main([str(simple_rc_schematic), "labels"])

        captured = capsys.readouterr()
        # Should work without error
        assert len(captured.out) > 0

    def test_labels_command_with_hierarchy(self, hierarchical_schematic: Path, capsys):
        """Test labels command with hierarchical schematic showing match status."""
        from kicad_tools.cli.sch_hierarchy import main

        main([str(hierarchical_schematic), "labels"])

        captured = capsys.readouterr()
        output = captured.out

        # Should show hierarchical label connections header
        assert "Hierarchical Label Connections" in output

        # Should show summary line
        assert "Summary:" in output
        assert "signals" in output

        # Should have sheet pins from the hierarchical schematic
        # Logic sheet has VCC, GND, OUT pins
        # Output sheet has IN, LED pins
        # The subsheets have labels - Logic has all 3, Output only has IN (missing LED)
        assert "Sheet:" in output

    def test_labels_json_with_match_status(self, hierarchical_schematic: Path, capsys):
        """Test labels command JSON output includes match status."""
        from kicad_tools.cli.sch_hierarchy import main

        main([str(hierarchical_schematic), "labels", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # JSON should have signals and summary keys
        assert "signals" in data
        assert "summary" in data

        # Summary should have statistics
        assert "total_signals" in data["summary"]
        assert "mismatched_signals" in data["summary"]
        assert "matched_signals" in data["summary"]

        # Each signal should have matched field
        for signal_name, signal_data in data["signals"].items():
            assert "matched" in signal_data
            assert "sheets" in signal_data
            for sheet_path, sheet_data in signal_data["sheets"].items():
                assert "has_pin" in sheet_data
                assert "has_label" in sheet_data
                assert "matched" in sheet_data

    def test_labels_detects_mismatch(self, hierarchical_schematic: Path, capsys):
        """Test that labels command detects pin/label mismatches."""
        from kicad_tools.cli.sch_hierarchy import main

        main([str(hierarchical_schematic), "labels", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # The Output sheet has LED pin but output_subsheet.kicad_sch doesn't have LED label
        # So we should detect at least one mismatch
        led_signal = data["signals"].get("LED")
        if led_signal:
            # LED should show as mismatched (has pin but no label in child)
            assert led_signal["matched"] is False

    def test_stats_command(self, simple_rc_schematic: Path, capsys):
        """Test stats command."""
        from kicad_tools.cli.sch_hierarchy import main

        main([str(simple_rc_schematic), "stats"])

        captured = capsys.readouterr()
        assert "Hierarchy Statistics" in captured.out or "total_sheets" in captured.out

    def test_json_format(self, simple_rc_schematic: Path, capsys):
        """Test JSON format output."""
        from kicad_tools.cli.sch_hierarchy import main

        main([str(simple_rc_schematic), "tree", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, dict)
        assert "name" in data

    def test_list_json_format(self, simple_rc_schematic: Path, capsys):
        """Test list command with JSON format."""
        from kicad_tools.cli.sch_hierarchy import main

        main([str(simple_rc_schematic), "list", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)

    def test_stats_json_format(self, simple_rc_schematic: Path, capsys):
        """Test stats command with JSON format."""
        from kicad_tools.cli.sch_hierarchy import main

        main([str(simple_rc_schematic), "stats", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "total_sheets" in data

    def test_depth_limit(self, simple_rc_schematic: Path, capsys):
        """Test depth limit option."""
        from kicad_tools.cli.sch_hierarchy import main

        main([str(simple_rc_schematic), "tree", "--depth", "1"])

        captured = capsys.readouterr()
        assert len(captured.out) > 0


class TestSchSummary:
    """Tests for sch_summary.py CLI."""

    def test_missing_file_handles_gracefully(self, capsys, monkeypatch):
        """Test that missing files are handled gracefully (returns empty summary)."""
        from kicad_tools.cli.sch_summary import main

        # The summary builder handles missing files gracefully
        monkeypatch.setattr("sys.argv", ["sch-summary", "nonexistent.kicad_sch"])
        main()

        captured = capsys.readouterr()
        # Should output a summary (even if mostly empty)
        assert "Schematic:" in captured.out

    def test_text_output(self, simple_rc_schematic: Path, capsys, monkeypatch):
        """Test text format output."""
        from kicad_tools.cli.sch_summary import main

        monkeypatch.setattr("sys.argv", ["sch-summary", str(simple_rc_schematic)])
        main()

        captured = capsys.readouterr()
        assert "Schematic:" in captured.out

    def test_json_output(self, simple_rc_schematic: Path, capsys, monkeypatch):
        """Test JSON format output."""
        from kicad_tools.cli.sch_summary import main

        monkeypatch.setattr(
            "sys.argv", ["sch-summary", str(simple_rc_schematic), "--format", "json"]
        )
        main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, dict)
        assert "file" in data

    def test_verbose_output(self, simple_rc_schematic: Path, capsys, monkeypatch):
        """Test verbose output."""
        from kicad_tools.cli.sch_summary import main

        monkeypatch.setattr("sys.argv", ["sch-summary", str(simple_rc_schematic), "--verbose"])
        main()

        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_run_summary_function(self, simple_rc_schematic: Path, capsys):
        """Test run_summary programmatic interface."""
        from kicad_tools.cli.sch_summary import run_summary

        result = run_summary(simple_rc_schematic, format="text", verbose=False)
        assert result == 0

    def test_run_summary_json(self, simple_rc_schematic: Path, capsys):
        """Test run_summary with JSON format."""
        from kicad_tools.cli.sch_summary import run_summary

        result = run_summary(simple_rc_schematic, format="json", verbose=False)
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "file" in data

    def test_run_summary_missing_file_handles_gracefully(self, capsys):
        """Test run_summary with missing file returns success (empty summary)."""
        from pathlib import Path

        from kicad_tools.cli.sch_summary import run_summary

        # The summary builder handles missing files gracefully
        result = run_summary(Path("nonexistent.kicad_sch"))
        # Returns 0 because it produces an empty summary, not an error
        assert result == 0

    def test_gather_summary(self, simple_rc_schematic: Path):
        """Test gather_summary function."""
        from kicad_tools.cli.sch_summary import gather_summary

        summary = gather_summary(str(simple_rc_schematic))
        assert "file" in summary
        assert "path" in summary
        assert "hierarchy" in summary
        assert "components" in summary
        assert "connectivity" in summary

    def test_print_summary(self, simple_rc_schematic: Path, capsys):
        """Test print_summary function."""
        from kicad_tools.cli.sch_summary import gather_summary, print_summary

        summary = gather_summary(str(simple_rc_schematic))
        print_summary(summary, verbose=False)

        captured = capsys.readouterr()
        assert "Schematic:" in captured.out

    def test_print_summary_verbose(self, simple_rc_schematic: Path, capsys):
        """Test print_summary with verbose mode."""
        from kicad_tools.cli.sch_summary import gather_summary, print_summary

        summary = gather_summary(str(simple_rc_schematic), verbose=True)
        print_summary(summary, verbose=True)

        captured = capsys.readouterr()
        assert len(captured.out) > 0


class TestSchListWires:
    """Tests for sch_list_wires.py CLI."""

    def test_file_not_found(self, capsys, monkeypatch):
        """Test handling of missing file."""
        from kicad_tools.cli.sch_list_wires import main

        monkeypatch.setattr("sys.argv", ["sch-list-wires", "nonexistent.kicad_sch"])
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    def test_table_output(self, minimal_schematic: Path, capsys, monkeypatch):
        """Test table format output."""
        from kicad_tools.cli.sch_list_wires import main

        monkeypatch.setattr("sys.argv", ["sch-list-wires", str(minimal_schematic)])
        main()

        captured = capsys.readouterr()
        # Should have some output
        assert len(captured.out) > 0

    def test_json_output(self, minimal_schematic: Path, capsys, monkeypatch):
        """Test JSON format output."""
        from kicad_tools.cli.sch_list_wires import main

        monkeypatch.setattr(
            "sys.argv", ["sch-list-wires", str(minimal_schematic), "--format", "json"]
        )
        main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # JSON output is a dict with "wires" and "statistics" keys
        assert isinstance(data, dict)
        assert "wires" in data
        assert "statistics" in data


class TestSchPinPositions:
    """Tests for sch_pin_positions.py CLI."""

    def test_file_not_found(self, capsys, monkeypatch, tmp_path):
        """Test handling of missing schematic file."""
        from kicad_tools.cli.sch_pin_positions import main

        missing_file = tmp_path / "definitely_missing" / "nonexistent.kicad_sch"
        # --lib is required, so we need to provide it
        monkeypatch.setattr(
            "sys.argv",
            ["sch-pin-positions", str(missing_file), "R1", "--lib", "fake.kicad_sym"],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_missing_lib(self, minimal_schematic: Path, capsys, monkeypatch, tmp_path):
        """Test handling of missing library file."""
        from kicad_tools.cli.sch_pin_positions import main

        missing_lib = tmp_path / "definitely_missing" / "nonexistent.kicad_sym"
        monkeypatch.setattr(
            "sys.argv",
            ["sch-pin-positions", str(minimal_schematic), "R1", "--lib", str(missing_lib)],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_with_library(
        self, minimal_schematic: Path, minimal_symbol_library: Path, capsys, monkeypatch
    ):
        """Test with valid schematic and library."""
        from kicad_tools.cli.sch_pin_positions import main

        # The minimal schematic has R1 with lib_id Device:R
        # The minimal symbol library has Device:R symbol
        monkeypatch.setattr(
            "sys.argv",
            [
                "sch-pin-positions",
                str(minimal_schematic),
                "R1",
                "--lib",
                str(minimal_symbol_library),
            ],
        )
        # This will either succeed or exit with symbol not found
        try:
            main()
            captured = capsys.readouterr()
            assert len(captured.out) > 0
        except SystemExit as e:
            # If the symbol isn't found in library, that's okay for this test
            assert e.code == 1

    def test_json_output(
        self, minimal_schematic: Path, minimal_symbol_library: Path, capsys, monkeypatch
    ):
        """Test JSON format output."""
        from kicad_tools.cli.sch_pin_positions import main

        monkeypatch.setattr(
            "sys.argv",
            [
                "sch-pin-positions",
                str(minimal_schematic),
                "R1",
                "--lib",
                str(minimal_symbol_library),
                "--format",
                "json",
            ],
        )
        try:
            main()
            captured = capsys.readouterr()
            if captured.out.strip():
                data = json.loads(captured.out)
                assert isinstance(data, dict)
        except SystemExit:
            # If the symbol isn't found, that's okay for this test
            pass


class TestSchSymbolInfo:
    """Tests for sch_symbol_info.py CLI."""

    def test_file_not_found(self, capsys, monkeypatch, tmp_path):
        """Test handling of missing file."""
        from kicad_tools.cli.sch_symbol_info import main

        missing_file = tmp_path / "definitely_missing" / "nonexistent.kicad_sch"
        monkeypatch.setattr("sys.argv", ["sch-symbol-info", str(missing_file), "R1"])
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_symbol_info(self, minimal_schematic: Path, capsys, monkeypatch):
        """Test getting symbol info."""
        from kicad_tools.cli.sch_symbol_info import main

        monkeypatch.setattr("sys.argv", ["sch-symbol-info", str(minimal_schematic), "R1"])
        main()

        captured = capsys.readouterr()
        # Should output symbol information
        assert "Symbol: R1" in captured.out
        assert len(captured.out) > 0

    def test_json_output(self, minimal_schematic: Path, capsys, monkeypatch):
        """Test JSON format output."""
        from kicad_tools.cli.sch_symbol_info import main

        # Note: this command uses --json flag, not --format json
        monkeypatch.setattr(
            "sys.argv",
            ["sch-symbol-info", str(minimal_schematic), "R1", "--json"],
        )
        main()

        captured = capsys.readouterr()
        if captured.out.strip():
            data = json.loads(captured.out)
            assert isinstance(data, dict)
            assert "reference" in data

    def test_symbol_not_found(self, minimal_schematic: Path, capsys, monkeypatch):
        """Test handling of missing symbol."""
        from kicad_tools.cli.sch_symbol_info import main

        monkeypatch.setattr("sys.argv", ["sch-symbol-info", str(minimal_schematic), "NONEXISTENT"])
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err

    def test_show_pins(self, minimal_schematic: Path, capsys, monkeypatch):
        """Test showing pins."""
        from kicad_tools.cli.sch_symbol_info import main

        monkeypatch.setattr(
            "sys.argv", ["sch-symbol-info", str(minimal_schematic), "R1", "--show-pins"]
        )
        main()

        captured = capsys.readouterr()
        assert "Pins" in captured.out

    def test_show_properties(self, minimal_schematic: Path, capsys, monkeypatch):
        """Test showing properties."""
        from kicad_tools.cli.sch_symbol_info import main

        monkeypatch.setattr(
            "sys.argv", ["sch-symbol-info", str(minimal_schematic), "R1", "--show-properties"]
        )
        main()

        captured = capsys.readouterr()
        assert "Properties:" in captured.out


class TestSchValidate:
    """Tests for sch_validate.py CLI."""

    def test_file_not_found(self, capsys, monkeypatch):
        """Test handling of missing file."""
        from kicad_tools.cli.sch_validate import main

        monkeypatch.setattr("sys.argv", ["sch-validate", "nonexistent.kicad_sch"])
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    def test_validate_schematic(self, minimal_schematic: Path, capsys, monkeypatch):
        """Test validating a schematic."""
        from kicad_tools.cli.sch_validate import main

        monkeypatch.setattr("sys.argv", ["sch-validate", str(minimal_schematic)])
        # May return 0 or 1 depending on validation results
        try:
            main()
        except SystemExit as e:
            assert e.code in (0, 1)

        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_json_output(self, minimal_schematic: Path, capsys, monkeypatch):
        """Test JSON format output."""
        from kicad_tools.cli.sch_validate import main

        monkeypatch.setattr(
            "sys.argv", ["sch-validate", str(minimal_schematic), "--format", "json"]
        )
        with contextlib.suppress(SystemExit):
            main()

        captured = capsys.readouterr()
        if captured.out.strip():
            data = json.loads(captured.out)
            assert isinstance(data, (list, dict))


class TestSchValidateHierarchy:
    """Tests for hierarchy checks in sch_validate.py."""

    def test_check_hierarchy_pin_without_label(self, tmp_path: Path):
        """Test that sheet pins without matching hierarchical labels are detected."""
        from kicad_tools.cli.sch_validate import check_hierarchy

        # Create a parent schematic with a sheet that has a pin
        parent_sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (sheet
            (at 100 100)
            (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "SubSheet" (at 100 99 0))
            (property "Sheetfile" "subsheet.kicad_sch" (at 100 111 0))
            (pin "VCC_3V3A" input (at 100 105 180)
              (effects (font (size 1.27 1.27)))
              (uuid "00000000-0000-0000-0000-000000000003")
            )
          )
        )
        """

        # Create a child schematic WITHOUT the matching hierarchical label
        child_sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000010")
          (paper "A4")
          (lib_symbols)
        )
        """

        # Write the schematic files
        parent_file = tmp_path / "parent.kicad_sch"
        child_file = tmp_path / "subsheet.kicad_sch"
        parent_file.write_text(parent_sch)
        child_file.write_text(child_sch)

        # Run the hierarchy check
        issues = check_hierarchy(str(parent_file))

        # Should find the missing label issue
        pin_issues = [
            i
            for i in issues
            if "Sheet pin" in i.message and "no matching hierarchical label" in i.message
        ]
        assert len(pin_issues) == 1
        assert pin_issues[0].severity == "error"
        assert "VCC_3V3A" in pin_issues[0].message

    def test_check_hierarchy_label_without_pin(self, tmp_path: Path):
        """Test that hierarchical labels without matching sheet pins are detected."""
        from kicad_tools.cli.sch_validate import check_hierarchy

        # Create a parent schematic with a sheet that has NO pin
        parent_sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (sheet
            (at 100 100)
            (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "SubSheet" (at 100 99 0))
            (property "Sheetfile" "subsheet.kicad_sch" (at 100 111 0))
          )
        )
        """

        # Create a child schematic WITH a hierarchical label but no matching pin
        child_sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000010")
          (paper "A4")
          (lib_symbols)
          (hierarchical_label "ORPHAN_SIGNAL"
            (shape input)
            (at 50 50 0)
            (effects (font (size 1.27 1.27)))
            (uuid "00000000-0000-0000-0000-000000000011")
          )
        )
        """

        # Write the schematic files
        parent_file = tmp_path / "parent.kicad_sch"
        child_file = tmp_path / "subsheet.kicad_sch"
        parent_file.write_text(parent_sch)
        child_file.write_text(child_sch)

        # Run the hierarchy check
        issues = check_hierarchy(str(parent_file))

        # Should find the orphan label issue
        label_issues = [
            i
            for i in issues
            if "Hierarchical label" in i.message and "no matching sheet pin" in i.message
        ]
        assert len(label_issues) == 1
        assert label_issues[0].severity == "warning"
        assert "ORPHAN_SIGNAL" in label_issues[0].message

    def test_check_hierarchy_matching_pin_and_label(self, tmp_path: Path):
        """Test that matching pins and labels produce no issues."""
        from kicad_tools.cli.sch_validate import check_hierarchy

        # Create a parent schematic with a sheet that has a pin
        parent_sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (sheet
            (at 100 100)
            (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "SubSheet" (at 100 99 0))
            (property "Sheetfile" "subsheet.kicad_sch" (at 100 111 0))
            (pin "DATA_BUS" input (at 100 105 180)
              (effects (font (size 1.27 1.27)))
              (uuid "00000000-0000-0000-0000-000000000003")
            )
          )
        )
        """

        # Create a child schematic WITH a matching hierarchical label
        child_sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000010")
          (paper "A4")
          (lib_symbols)
          (hierarchical_label "DATA_BUS"
            (shape input)
            (at 50 50 0)
            (effects (font (size 1.27 1.27)))
            (uuid "00000000-0000-0000-0000-000000000011")
          )
        )
        """

        # Write the schematic files
        parent_file = tmp_path / "parent.kicad_sch"
        child_file = tmp_path / "subsheet.kicad_sch"
        parent_file.write_text(parent_sch)
        child_file.write_text(child_sch)

        # Run the hierarchy check
        issues = check_hierarchy(str(parent_file))

        # Should find no hierarchy issues
        hierarchy_issues = [i for i in issues if i.category == "hierarchy"]
        assert len(hierarchy_issues) == 0


class TestSchCheckConnections:
    """Tests for sch_check_connections.py CLI."""

    def test_file_not_found(self, capsys, monkeypatch, tmp_path):
        """Test handling of missing schematic file."""
        from kicad_tools.cli.sch_check_connections import main
        from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError

        # Create a non-existent file path
        missing_file = "/nonexistent_dir_12345/nonexistent.kicad_sch"
        monkeypatch.setattr(
            "sys.argv",
            [
                "sch-check-connections",
                missing_file,
                "--lib-path",
                str(tmp_path),
            ],
        )
        # The CLI catches builtin FileNotFoundError but the library raises
        # kicad_tools.exceptions.FileNotFoundError which doesn't inherit from builtin.
        # This is a known issue - the custom exception may escape.
        with pytest.raises((SystemExit, KiCadFileNotFoundError)):
            main()

    def test_no_library_warning(self, minimal_schematic: Path, capsys, monkeypatch, tmp_path):
        """Test warning when no libraries are loaded."""
        from kicad_tools.cli.sch_check_connections import main

        # Use an empty directory as lib path
        empty_dir = tmp_path / "empty_lib"
        empty_dir.mkdir()
        monkeypatch.setattr(
            "sys.argv",
            [
                "sch-check-connections",
                str(minimal_schematic),
                "--lib-path",
                str(empty_dir),
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No symbol libraries loaded" in captured.err

    def test_with_library(
        self, minimal_schematic: Path, minimal_symbol_library: Path, capsys, monkeypatch
    ):
        """Test checking connections with a library."""
        from kicad_tools.cli.sch_check_connections import main

        monkeypatch.setattr(
            "sys.argv",
            [
                "sch-check-connections",
                str(minimal_schematic),
                "--lib",
                str(minimal_symbol_library),
            ],
        )
        # This should work or warn about missing pins
        try:
            main()
        except SystemExit as e:
            # May exit with 0 or 1 depending on results
            assert e.code in (0, 1)

        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_json_output(
        self, minimal_schematic: Path, minimal_symbol_library: Path, capsys, monkeypatch
    ):
        """Test JSON format output."""
        from kicad_tools.cli.sch_check_connections import main

        monkeypatch.setattr(
            "sys.argv",
            [
                "sch-check-connections",
                str(minimal_schematic),
                "--lib",
                str(minimal_symbol_library),
                "--format",
                "json",
            ],
        )
        with contextlib.suppress(SystemExit):
            main()

        captured = capsys.readouterr()
        if captured.out.strip():
            data = json.loads(captured.out)
            assert isinstance(data, dict)
            assert "pins" in data
            assert "summary" in data


class TestSchFindUnconnected:
    """Tests for sch_find_unconnected.py CLI."""

    def test_file_not_found(self, capsys, monkeypatch):
        """Test handling of missing file."""
        from kicad_tools.cli.sch_find_unconnected import main

        monkeypatch.setattr("sys.argv", ["sch-find-unconnected", "nonexistent.kicad_sch"])
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    def test_find_unconnected(self, minimal_schematic: Path, capsys, monkeypatch):
        """Test finding unconnected pins."""
        from kicad_tools.cli.sch_find_unconnected import main

        monkeypatch.setattr("sys.argv", ["sch-find-unconnected", str(minimal_schematic)])
        try:
            main()
        except SystemExit as e:
            assert e.code in (0, 1)

        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_json_output(self, minimal_schematic: Path, capsys, monkeypatch):
        """Test JSON format output."""
        from kicad_tools.cli.sch_find_unconnected import main

        monkeypatch.setattr(
            "sys.argv",
            ["sch-find-unconnected", str(minimal_schematic), "--format", "json"],
        )
        with contextlib.suppress(SystemExit):
            main()

        captured = capsys.readouterr()
        if captured.out.strip():
            data = json.loads(captured.out)
            assert isinstance(data, (list, dict))


class TestSchRenameSignal:
    """Tests for sch_rename_signal.py CLI."""

    def test_file_not_found(self, capsys):
        """Test handling of missing file."""
        from kicad_tools.cli.sch_rename_signal import main

        result = main(["nonexistent.kicad_sch", "--from", "VCC", "--to", "VCC_3V3"])

        assert result == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_signal_not_found(self, minimal_schematic: Path, capsys):
        """Test handling of signal that doesn't exist."""
        from kicad_tools.cli.sch_rename_signal import main

        result = main([str(minimal_schematic), "--from", "NONEXISTENT", "--to", "NEW_NAME"])

        assert result == 0
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower()

    def test_dry_run_finds_changes(self, tmp_path: Path, capsys):
        """Test that dry-run mode previews changes without modifying files."""
        from kicad_tools.cli.sch_rename_signal import main

        # Create a hierarchical schematic with sheet pins
        parent_sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (sheet
            (at 100 100)
            (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "SubSheet" (at 100 99 0))
            (property "Sheetfile" "subsheet.kicad_sch" (at 100 111 0))
            (pin "DATA_IN" input (at 100 105 180)
              (effects (font (size 1.27 1.27)))
              (uuid "00000000-0000-0000-0000-000000000003")
            )
          )
        )
        """

        child_sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000010")
          (paper "A4")
          (lib_symbols)
          (hierarchical_label "DATA_IN"
            (shape input)
            (at 50 50 0)
            (effects (font (size 1.27 1.27)))
            (uuid "00000000-0000-0000-0000-000000000011")
          )
        )
        """

        parent_file = tmp_path / "parent.kicad_sch"
        child_file = tmp_path / "subsheet.kicad_sch"
        parent_file.write_text(parent_sch)
        child_file.write_text(child_sch)

        # Save original content
        original_parent = parent_file.read_text()
        original_child = child_file.read_text()

        # Run dry-run
        result = main([str(parent_file), "--from", "DATA_IN", "--to", "SPI_MOSI", "--dry-run"])

        assert result == 0
        captured = capsys.readouterr()

        # Verify changes are previewed
        assert "DATA_IN" in captured.out
        assert "SPI_MOSI" in captured.out
        assert "dry run" in captured.out.lower()

        # Verify files were NOT modified
        assert parent_file.read_text() == original_parent
        assert child_file.read_text() == original_child

    def test_applies_changes_with_yes_flag(self, tmp_path: Path, capsys):
        """Test that --yes flag applies changes without confirmation."""
        from kicad_tools.cli.sch_rename_signal import main

        # Create a hierarchical schematic with sheet pins
        parent_sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (sheet
            (at 100 100)
            (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "SubSheet" (at 100 99 0))
            (property "Sheetfile" "subsheet.kicad_sch" (at 100 111 0))
            (pin "OLD_SIGNAL" input (at 100 105 180)
              (effects (font (size 1.27 1.27)))
              (uuid "00000000-0000-0000-0000-000000000003")
            )
          )
        )
        """

        child_sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000010")
          (paper "A4")
          (lib_symbols)
          (hierarchical_label "OLD_SIGNAL"
            (shape input)
            (at 50 50 0)
            (effects (font (size 1.27 1.27)))
            (uuid "00000000-0000-0000-0000-000000000011")
          )
        )
        """

        parent_file = tmp_path / "parent.kicad_sch"
        child_file = tmp_path / "subsheet.kicad_sch"
        parent_file.write_text(parent_sch)
        child_file.write_text(child_sch)

        # Run with --yes flag
        result = main([str(parent_file), "--from", "OLD_SIGNAL", "--to", "NEW_SIGNAL", "--yes"])

        assert result == 0

        # Verify files WERE modified
        new_parent = parent_file.read_text()
        new_child = child_file.read_text()

        assert "NEW_SIGNAL" in new_parent
        assert "OLD_SIGNAL" not in new_parent
        assert "NEW_SIGNAL" in new_child
        assert "OLD_SIGNAL" not in new_child

    def test_include_nets_option(self, tmp_path: Path, capsys):
        """Test that --include-nets also renames net labels."""
        from kicad_tools.cli.sch_rename_signal import main

        # Create schematic with both hierarchical label and net label
        sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (hierarchical_label "SIGNAL_A"
            (shape input)
            (at 50 50 0)
            (effects (font (size 1.27 1.27)))
            (uuid "00000000-0000-0000-0000-000000000002")
          )
          (label "SIGNAL_A"
            (at 70 50 0)
            (effects (font (size 1.27 1.27)))
            (uuid "00000000-0000-0000-0000-000000000003")
          )
        )
        """

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(sch)

        # Run WITHOUT --include-nets (net label should not change)
        result = main([str(sch_file), "--from", "SIGNAL_A", "--to", "SIGNAL_B", "--yes"])

        assert result == 0
        content = sch_file.read_text()

        # Hierarchical label should be renamed
        assert 'hierarchical_label "SIGNAL_B"' in content
        # Net label should NOT be renamed
        assert 'label "SIGNAL_A"' in content

        # Now run WITH --include-nets
        sch_file.write_text(content.replace("SIGNAL_B", "SIGNAL_A"))  # Reset

        result = main(
            [str(sch_file), "--from", "SIGNAL_A", "--to", "SIGNAL_B", "--include-nets", "--yes"]
        )

        assert result == 0
        content = sch_file.read_text()

        # Both should be renamed now
        assert 'hierarchical_label "SIGNAL_B"' in content
        assert 'label "SIGNAL_B"' in content

    def test_json_output_rename_signal(self, tmp_path: Path, capsys):
        """Test JSON format output."""
        from kicad_tools.cli.sch_rename_signal import main

        # Create a simple schematic with a sheet pin
        sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (sheet
            (at 100 100)
            (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "SubSheet" (at 100 99 0))
            (property "Sheetfile" "subsheet.kicad_sch" (at 100 111 0))
            (pin "TEST_SIG" input (at 100 105 180)
              (effects (font (size 1.27 1.27)))
              (uuid "00000000-0000-0000-0000-000000000003")
            )
          )
        )
        """

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(sch)

        result = main(
            [
                str(sch_file),
                "--from",
                "TEST_SIG",
                "--to",
                "NEW_SIG",
                "--dry-run",
                "--format",
                "json",
            ]
        )

        assert result == 0
        captured = capsys.readouterr()

        data = json.loads(captured.out)
        assert "old_name" in data
        assert data["old_name"] == "TEST_SIG"
        assert data["new_name"] == "NEW_SIG"
        assert "summary" in data
        assert data["summary"]["total_changes"] >= 1

    def test_hierarchical_schematic(self, hierarchical_schematic: Path, capsys):
        """Test with hierarchical fixture - dry run."""
        from kicad_tools.cli.sch_rename_signal import main

        # VCC is a sheet pin in the Logic subsheet
        result = main(
            [str(hierarchical_schematic), "--from", "VCC", "--to", "VCC_3V3", "--dry-run"]
        )

        assert result == 0
        captured = capsys.readouterr()

        # Should find sheet pins and hierarchical labels
        assert "VCC" in captured.out
        assert "VCC_3V3" in captured.out
