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

    def test_hierarchy_global_labels_across_sheets(self, hierarchical_schematic: Path, capsys):
        """Test that global labels from child sheets are included."""
        from kicad_tools.cli.sch_list_labels import main

        main([str(hierarchical_schematic), "--type", "global", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Root has SIGNAL_OUT, Logic has CLK+RESET, Output has CLK
        texts = [lbl["text"] for lbl in data]
        assert "SIGNAL_OUT" in texts
        assert "CLK" in texts
        assert "RESET" in texts
        # CLK appears in both Logic and Output sheets
        clk_labels = [lbl for lbl in data if lbl["text"] == "CLK"]
        assert len(clk_labels) == 2

    def test_hierarchy_labels_include_sheet_info(self, hierarchical_schematic: Path, capsys):
        """Test that each label entry includes sheet name and file."""
        from kicad_tools.cli.sch_list_labels import main

        main([str(hierarchical_schematic), "--type", "global", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        for lbl in data:
            assert "sheet" in lbl, "Label entry should include 'sheet' field"
            assert "sheet_file" in lbl, "Label entry should include 'sheet_file' field"

        # Check specific sheet assignments
        root_labels = [lbl for lbl in data if lbl["sheet"] == "/"]
        assert any(lbl["text"] == "SIGNAL_OUT" for lbl in root_labels)

        logic_labels = [lbl for lbl in data if lbl["sheet"] == "/Logic"]
        assert any(lbl["text"] == "CLK" for lbl in logic_labels)
        assert any(lbl["text"] == "RESET" for lbl in logic_labels)

    def test_hierarchy_all_label_types(self, hierarchical_schematic: Path, capsys):
        """Test that --type all collects all label types from all sheets."""
        from kicad_tools.cli.sch_list_labels import main

        main([str(hierarchical_schematic), "--type", "all", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        types_found = {lbl["type"] for lbl in data}
        # Should find global labels (root + children), hierarchical (children),
        # power symbols (root), and local labels (output child)
        assert "global" in types_found
        assert "hierarchical" in types_found
        assert "power" in types_found
        assert "local" in types_found

    def test_hierarchy_csv_includes_sheet_columns(self, hierarchical_schematic: Path, capsys):
        """Test that CSV output includes Sheet and Sheet File columns."""
        from kicad_tools.cli.sch_list_labels import main

        main([str(hierarchical_schematic), "--type", "global", "--format", "csv"])

        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert "Sheet" in lines[0]
        assert "Sheet File" in lines[0]

    def test_hierarchy_table_includes_sheet_column(self, hierarchical_schematic: Path, capsys):
        """Test that table output includes Sheet column."""
        from kicad_tools.cli.sch_list_labels import main

        main([str(hierarchical_schematic), "--type", "global"])

        captured = capsys.readouterr()
        assert "Sheet" in captured.out

    def test_hierarchy_filter_works_across_sheets(self, hierarchical_schematic: Path, capsys):
        """Test that --filter pattern works across all sheets."""
        from kicad_tools.cli.sch_list_labels import main

        main(
            [str(hierarchical_schematic), "--type", "global", "--filter", "CLK", "--format", "json"]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 2  # CLK in Logic and Output
        assert all(lbl["text"] == "CLK" for lbl in data)


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

    def test_hierarchical_connectivity_aggregation(self, tmp_path: Path):
        """Test that connectivity counts aggregate across all sheets in hierarchy.

        Regression test for #1888: sch summary showed zero connectivity counts
        for hierarchical projects because it only counted the root sheet.
        """
        from kicad_tools.cli.sch_summary import gather_summary

        # Create a child schematic with wires, labels, and junctions
        child_sch = tmp_path / "child.kicad_sch"
        child_sch.write_text(
            """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000010")
  (paper "A4")
  (lib_symbols)
  (hierarchical_label "IN"
    (shape input)
    (at 50 50 180)
    (effects (font (size 1.27 1.27)) (justify right))
    (uuid "hlabel-in")
  )
  (wire
    (pts (xy 50 50) (xy 80 50))
    (stroke (width 0) (type default))
    (uuid "child-wire-1")
  )
  (wire
    (pts (xy 80 50) (xy 80 80))
    (stroke (width 0) (type default))
    (uuid "child-wire-2")
  )
  (wire
    (pts (xy 80 80) (xy 120 80))
    (stroke (width 0) (type default))
    (uuid "child-wire-3")
  )
  (junction
    (at 80 50)
    (diameter 0)
    (uuid "child-junction-1")
  )
  (label "NET1"
    (at 90 50 0)
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "child-label-1")
  )
  (label "NET2"
    (at 100 80 0)
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "child-label-2")
  )
  (global_label "POWER"
    (shape input)
    (at 120 80 0)
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "child-global-1")
    (property "Intersheetrefs" "${INTERSHEET_REFS}"
      (at 120 80 0)
      (effects (font (size 1.27 1.27)) hide)
    )
  )
)"""
        )

        # Create a root schematic referencing the child
        root_sch = tmp_path / "root.kicad_sch"
        root_sch.write_text(
            """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (sheet
    (at 130 40) (size 40 30)
    (stroke (width 0.1524) (type solid))
    (fill (color 255 255 194 1.0))
    (uuid "sheet-child-uuid")
    (property "Sheetname" "Child"
      (at 130 39 0)
      (effects (font (size 1.27 1.27)) (justify left bottom))
    )
    (property "Sheetfile" "child.kicad_sch"
      (at 130 71 0)
      (effects (font (size 1.27 1.27)) (justify left top) hide)
    )
    (pin "IN" input
      (at 130 50 180)
      (effects (font (size 1.27 1.27)) (justify left))
      (uuid "sheet-pin-in")
    )
  )
  (wire
    (pts (xy 100 50) (xy 130 50))
    (stroke (width 0) (type default))
    (uuid "root-wire-1")
  )
  (junction
    (at 100 50)
    (diameter 0)
    (uuid "root-junction-1")
  )
  (label "SIG"
    (at 110 50 0)
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "root-label-1")
  )
)"""
        )

        summary = gather_summary(str(root_sch))
        conn = summary["connectivity"]

        # Root has 1 wire, child has 3 => total 4
        assert conn["wires"] == 4
        # Root has 1 junction, child has 1 => total 2
        assert conn["junctions"] == 2
        # Root has 1 label, child has 2 => total 3
        assert conn["labels"] == 3
        # Root has 0 global labels, child has 1 => total 1
        assert conn["global_labels"] == 1
        # Root has 0 hierarchical labels, child has 1 => total 1
        assert conn["hierarchical_labels"] == 1

    def test_hierarchical_connectivity_verbose_signals(self, tmp_path: Path):
        """Test that verbose unique signals aggregate across all sheets."""
        from kicad_tools.cli.sch_summary import gather_summary

        # Create a child schematic with labels
        child_sch = tmp_path / "child.kicad_sch"
        child_sch.write_text(
            """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000010")
  (paper "A4")
  (lib_symbols)
  (label "CHILD_NET"
    (at 90 50 0)
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "child-label-1")
  )
  (global_label "SHARED_POWER"
    (shape input)
    (at 120 80 0)
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "child-global-1")
    (property "Intersheetrefs" "${INTERSHEET_REFS}"
      (at 120 80 0)
      (effects (font (size 1.27 1.27)) hide)
    )
  )
)"""
        )

        # Create root schematic referencing child
        root_sch = tmp_path / "root.kicad_sch"
        root_sch.write_text(
            """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (sheet
    (at 130 40) (size 40 30)
    (stroke (width 0.1524) (type solid))
    (fill (color 255 255 194 1.0))
    (uuid "sheet-child-uuid")
    (property "Sheetname" "Child"
      (at 130 39 0)
      (effects (font (size 1.27 1.27)) (justify left bottom))
    )
    (property "Sheetfile" "child.kicad_sch"
      (at 130 71 0)
      (effects (font (size 1.27 1.27)) (justify left top) hide)
    )
  )
  (label "ROOT_NET"
    (at 110 50 0)
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "root-label-1")
  )
)"""
        )

        summary = gather_summary(str(root_sch), verbose=True)
        conn = summary["connectivity"]

        assert "unique_signals" in conn
        signals = conn["unique_signals"]
        # Should include labels from both root and child
        assert "ROOT_NET" in signals
        assert "CHILD_NET" in signals
        assert "SHARED_POWER" in signals

    def test_existing_hierarchical_fixture_aggregation(self, hierarchical_schematic: Path):
        """Test connectivity aggregation with the existing hierarchical fixture.

        The hierarchical_main.kicad_sch has 5 wires, 2 junctions, and 1 global_label
        in the root sheet, plus hierarchical labels in child sheets.
        """
        from kicad_tools.cli.sch_summary import gather_summary

        summary = gather_summary(str(hierarchical_schematic))
        conn = summary["connectivity"]

        # Root sheet: 5 wires, 2 junctions, 1 global_label; the child
        # sheets carry 2 + 1 more global labels.  gather_summary
        # aggregates connectivity across the whole sheet tree (same as
        # the hierarchical_labels count below), so the total is 4
        # (stale-test update, issue #3436 burn-down).
        assert conn["wires"] == 5
        assert conn["junctions"] == 2
        assert conn["global_labels"] == 4
        # hierarchical_labels: logic_subsheet has 3, output_subsheet has 1
        assert conn["hierarchical_labels"] == 4


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

    def test_embedded_lib_symbols_no_lib_flag(self, tmp_path, capsys):
        """Test that pins command works without --lib using embedded lib_symbols."""
        from kicad_tools.cli.sch_pin_positions import main

        # Schematic with embedded lib_symbols (same pattern used by pin-map)
        sch_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (pin_numbers hide)
      (pin_names (offset 0) hide)
      (in_bom yes) (on_board yes)
      (symbol "Device:R_0_1"
        (rectangle (start -1.016 -2.54) (end 1.016 2.54)
          (stroke (width 0.254) (type default)) (fill (type none)))
      )
      (symbol "Device:R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
)
"""
        sch_file = tmp_path / "test_embedded.kicad_sch"
        sch_file.write_text(sch_content)

        # Call without --lib flag
        main([str(sch_file), "R1", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["reference"] == "R1"
        assert data["value"] == "10k"
        assert len(data["pins"]) == 2
        # Verify pin positions are computed (not None)
        for pin in data["pins"]:
            assert pin["schematic_position"] is not None

    def test_embedded_lib_symbols_error_when_missing(self, tmp_path, capsys):
        """Test helpful error when symbol not in embedded lib_symbols and no --lib."""
        from kicad_tools.cli.sch_pin_positions import main

        # Schematic with empty lib_symbols
        sch_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
)
"""
        sch_file = tmp_path / "test_no_embedded.kicad_sch"
        sch_file.write_text(sch_content)

        with pytest.raises(SystemExit) as exc_info:
            main([str(sch_file), "R1"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err
        assert "--lib" in captured.err


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

    def test_show_pins_json_includes_name_type_net(self, tmp_path, capsys, monkeypatch):
        """Test that --show-pins --json includes name, type, net, position fields."""
        from kicad_tools.cli.sch_symbol_info import main

        # Schematic with lib_symbols so resolve_pin_map can enrich pin data
        sch_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (pin_numbers hide)
      (pin_names (offset 0) hide)
      (in_bom yes) (on_board yes)
      (symbol "Device:R_0_1"
        (rectangle (start -1.016 -2.54) (end 1.016 2.54)
          (stroke (width 0.254) (type default)) (fill (type none)))
      )
      (symbol "Device:R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (wire
    (pts (xy 100 96.19) (xy 100 90))
    (stroke (width 0) (type default))
    (uuid "00000000-0000-0000-0000-000000000010")
  )
  (label "VIN"
    (at 100 90 0)
    (effects (font (size 1.27 1.27)))
    (uuid "00000000-0000-0000-0000-000000000011")
  )
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
)
"""
        sch_file = tmp_path / "test_enriched.kicad_sch"
        sch_file.write_text(sch_content)

        monkeypatch.setattr(
            "sys.argv",
            ["sch-symbol-info", str(sch_file), "R1", "--show-pins", "--json"],
        )
        main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "pins" in data
        assert len(data["pins"]) == 2

        # Verify enriched fields are present
        pin1 = next(p for p in data["pins"] if p["number"] == "1")
        assert "name" in pin1
        assert "type" in pin1
        assert "net" in pin1
        assert "position" in pin1
        assert pin1["type"] == "passive"
        assert pin1["net"] == "VIN"

    def test_show_pins_text_includes_name_type_net(self, tmp_path, capsys, monkeypatch):
        """Test that --show-pins table output includes name, type, net columns."""
        from kicad_tools.cli.sch_symbol_info import main

        sch_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (pin_numbers hide)
      (pin_names (offset 0) hide)
      (in_bom yes) (on_board yes)
      (symbol "Device:R_0_1"
        (rectangle (start -1.016 -2.54) (end 1.016 2.54)
          (stroke (width 0.254) (type default)) (fill (type none)))
      )
      (symbol "Device:R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (wire
    (pts (xy 100 96.19) (xy 100 90))
    (stroke (width 0) (type default))
    (uuid "00000000-0000-0000-0000-000000000010")
  )
  (label "VIN"
    (at 100 90 0)
    (effects (font (size 1.27 1.27)))
    (uuid "00000000-0000-0000-0000-000000000011")
  )
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
)
"""
        sch_file = tmp_path / "test_enriched.kicad_sch"
        sch_file.write_text(sch_content)

        monkeypatch.setattr(
            "sys.argv",
            ["sch-symbol-info", str(sch_file), "R1", "--show-pins"],
        )
        main()

        captured = capsys.readouterr()
        # Table output should include column headers
        assert "Name" in captured.out
        assert "Type" in captured.out
        assert "Net" in captured.out
        # Should show actual pin data
        assert "passive" in captured.out
        assert "VIN" in captured.out

    def test_show_pins_graceful_without_lib_symbols(self, minimal_schematic, capsys, monkeypatch):
        """Test that --show-pins degrades gracefully when lib_symbols is empty."""
        from kicad_tools.cli.sch_symbol_info import main

        monkeypatch.setattr(
            "sys.argv",
            ["sch-symbol-info", str(minimal_schematic), "R1", "--show-pins", "--json"],
        )
        main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "pins" in data
        # Pins should still have number and uuid even without enrichment
        for pin in data["pins"]:
            assert "number" in pin
            assert "uuid" in pin


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


class TestSchValidateNoConnectInput:
    """Tests for check_no_connect_on_input_pins in sch_validate.py."""

    def _make_schematic(self, tmp_path: Path, pin_type: str, pin_name: str = "XSMT") -> Path:
        """Create a schematic with a symbol that has a pin of the given type
        and a no-connect marker placed on that pin.

        The symbol is placed at (100, 100) with no rotation.  The pin is
        defined at (-2.54, 0) relative to symbol centre, so its absolute
        position is (97.46, 100).  A no-connect marker is placed there.
        """
        sch_text = f"""(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "TestLib:IC1"
      (pin {pin_type} line
        (at -2.54 0 0)
        (length 2.54)
        (name "{pin_name}"
          (effects (font (size 1.27 1.27)))
        )
        (number "1"
          (effects (font (size 1.27 1.27)))
        )
      )
    )
  )
  (symbol
    (lib_id "TestLib:IC1")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "U1" (at 100 90 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "IC1" (at 100 110 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "U1")
          (unit 1)
        )
      )
    )
  )
  (no_connect
    (at 97.46 100)
    (uuid "00000000-0000-0000-0000-000000000010")
  )
)"""
        sch_file = tmp_path / "test_nc.kicad_sch"
        sch_file.write_text(sch_text)
        return sch_file

    def test_input_pin_with_no_connect_triggers_info(self, tmp_path: Path):
        """NC marker on an input-typed pin should produce an info-level issue."""
        from kicad_tools.cli.sch_validate import check_no_connect_on_input_pins

        sch_file = self._make_schematic(tmp_path, pin_type="input")
        issues = check_no_connect_on_input_pins(str(sch_file))

        assert len(issues) == 1
        assert issues[0].severity == "info"
        assert issues[0].category == "no_connect"
        assert "XSMT" in issues[0].message
        assert "U1" in issues[0].message

    def test_passive_pin_with_no_connect_no_issue(self, tmp_path: Path):
        """NC marker on a passive-typed pin should produce no issue."""
        from kicad_tools.cli.sch_validate import check_no_connect_on_input_pins

        sch_file = self._make_schematic(tmp_path, pin_type="passive", pin_name="PAD")
        issues = check_no_connect_on_input_pins(str(sch_file))

        assert len(issues) == 0

    def test_no_connect_typed_pin_no_issue(self, tmp_path: Path):
        """NC marker on a no_connect-typed pin should produce no issue."""
        from kicad_tools.cli.sch_validate import check_no_connect_on_input_pins

        sch_file = self._make_schematic(tmp_path, pin_type="no_connect", pin_name="NC")
        issues = check_no_connect_on_input_pins(str(sch_file))

        assert len(issues) == 0

    def test_output_pin_with_no_connect_no_issue(self, tmp_path: Path):
        """NC marker on an output-typed pin should produce no issue."""
        from kicad_tools.cli.sch_validate import check_no_connect_on_input_pins

        sch_file = self._make_schematic(tmp_path, pin_type="output", pin_name="DOUT")
        issues = check_no_connect_on_input_pins(str(sch_file))

        assert len(issues) == 0

    def test_quiet_mode_suppresses_info(self, tmp_path: Path, capsys, monkeypatch):
        """Info-level issues should be filtered out in --quiet mode."""
        from kicad_tools.cli.sch_validate import main

        sch_file = self._make_schematic(tmp_path, pin_type="input")
        monkeypatch.setattr("sys.argv", ["sch-validate", str(sch_file), "--quiet"])
        with contextlib.suppress(SystemExit):
            main()

        captured = capsys.readouterr()
        # In quiet mode only errors are shown; the info-level NC warning must
        # not appear in text output.
        assert "No-connect on input pin" not in captured.out

    def test_json_output_includes_no_connect_category(self, tmp_path: Path, capsys, monkeypatch):
        """JSON output should include the no_connect category issue."""
        from kicad_tools.cli.sch_validate import main

        sch_file = self._make_schematic(tmp_path, pin_type="input")
        monkeypatch.setattr("sys.argv", ["sch-validate", str(sch_file), "--format", "json"])
        with contextlib.suppress(SystemExit):
            main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        nc_issues = [i for i in data["issues"] if i["category"] == "no_connect"]
        assert len(nc_issues) == 1
        assert nc_issues[0]["severity"] == "info"

    def test_validate_schematic_includes_no_connect_input_check(self, tmp_path: Path):
        """validate_schematic should include no_connect_input in checks_run."""
        from kicad_tools.cli.sch_validate import validate_schematic

        sch_file = self._make_schematic(tmp_path, pin_type="input")
        result = validate_schematic(str(sch_file))

        assert "no_connect_input" in result.checks_run

    def test_pin_name_fallback_to_number(self, tmp_path: Path):
        """When pin name is '~', the message should use the pin number."""
        from kicad_tools.cli.sch_validate import check_no_connect_on_input_pins

        sch_file = self._make_schematic(tmp_path, pin_type="input", pin_name="~")
        issues = check_no_connect_on_input_pins(str(sch_file))

        assert len(issues) == 1
        # Pin number "1" should appear as the display name
        assert "(pin 1)" in issues[0].message


class TestSchValidateGlobalLabelDirections:
    """Tests for global label direction mismatch detection in sch_validate.py."""

    def _make_schematic(self, global_labels_sexp: str) -> str:
        """Build a minimal schematic string with the given global_label entries."""
        return f"""(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          {global_labels_sexp}
        )
        """

    def test_no_driver_detected_as_error(self, tmp_path: Path):
        """All instances are input -- no driver exists -- should be an error."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        # Parent with one global label input, child with another global label input
        parent_sch = self._make_schematic("""
          (global_label "SIG_A" (shape input) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "a1"))
          (sheet (at 100 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "Sub" (at 100 99 0))
            (property "Sheetfile" "sub.kicad_sch" (at 100 111 0)))
        """)
        child_sch = self._make_schematic("""
          (global_label "SIG_A" (shape input) (at 30 40 0)
            (effects (font (size 1.27 1.27))) (uuid "a2"))
        """)

        (tmp_path / "top.kicad_sch").write_text(parent_sch)
        (tmp_path / "sub.kicad_sch").write_text(child_sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        gl_issues = [i for i in issues if i.category == "global_label"]

        assert len(gl_issues) == 1
        assert gl_issues[0].severity == "error"
        assert "SIG_A" in gl_issues[0].message
        assert "no driver" in gl_issues[0].message

    def test_no_receiver_detected_as_warning(self, tmp_path: Path):
        """All instances are output -- no receiver exists -- should be a warning."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        parent_sch = self._make_schematic("""
          (global_label "SIG_B" (shape output) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "b1"))
          (sheet (at 100 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "Sub" (at 100 99 0))
            (property "Sheetfile" "sub.kicad_sch" (at 100 111 0)))
        """)
        child_sch = self._make_schematic("""
          (global_label "SIG_B" (shape output) (at 30 40 0)
            (effects (font (size 1.27 1.27))) (uuid "b2"))
        """)

        (tmp_path / "top.kicad_sch").write_text(parent_sch)
        (tmp_path / "sub.kicad_sch").write_text(child_sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        gl_issues = [i for i in issues if i.category == "global_label"]

        assert len(gl_issues) == 1
        assert gl_issues[0].severity == "warning"
        assert "SIG_B" in gl_issues[0].message
        assert "no receiver" in gl_issues[0].message

    def test_valid_output_input_mix(self, tmp_path: Path):
        """One output and one input on the same net -- complementary pair, no warnings."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        parent_sch = self._make_schematic("""
          (global_label "SIG_C" (shape output) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "c1"))
          (sheet (at 100 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "Sub" (at 100 99 0))
            (property "Sheetfile" "sub.kicad_sch" (at 100 111 0)))
        """)
        child_sch = self._make_schematic("""
          (global_label "SIG_C" (shape input) (at 30 40 0)
            (effects (font (size 1.27 1.27))) (uuid "c2"))
        """)

        (tmp_path / "top.kicad_sch").write_text(parent_sch)
        (tmp_path / "sub.kicad_sch").write_text(child_sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        gl_issues = [i for i in issues if i.category == "global_label"]
        assert len(gl_issues) == 0

    def test_bidirectional_counts_as_both(self, tmp_path: Path):
        """All bidirectional -- counts as both driver and receiver."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        sch = self._make_schematic("""
          (global_label "I2C_SDA" (shape bidirectional) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "d1"))
          (global_label "I2C_SDA" (shape bidirectional) (at 30 40 0)
            (effects (font (size 1.27 1.27))) (uuid "d2"))
        """)

        (tmp_path / "top.kicad_sch").write_text(sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        gl_issues = [i for i in issues if i.category == "global_label"]
        assert len(gl_issues) == 0

    def test_passive_counts_as_both(self, tmp_path: Path):
        """All passive -- counts as both driver and receiver."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        sch = self._make_schematic("""
          (global_label "GND_SENSE" (shape passive) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "e1"))
        """)

        (tmp_path / "top.kicad_sch").write_text(sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        gl_issues = [i for i in issues if i.category == "global_label"]
        assert len(gl_issues) == 0

    def test_tri_state_is_driver(self, tmp_path: Path):
        """tri_state counts as a driver but not a receiver."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        parent_sch = self._make_schematic("""
          (global_label "BUS_D0" (shape tri_state) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "f1"))
          (sheet (at 100 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "Sub" (at 100 99 0))
            (property "Sheetfile" "sub.kicad_sch" (at 100 111 0)))
        """)
        child_sch = self._make_schematic("""
          (global_label "BUS_D0" (shape tri_state) (at 30 40 0)
            (effects (font (size 1.27 1.27))) (uuid "f2"))
        """)

        (tmp_path / "top.kicad_sch").write_text(parent_sch)
        (tmp_path / "sub.kicad_sch").write_text(child_sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        gl_issues = [i for i in issues if i.category == "global_label"]

        # tri_state is driver-only, so no receiver -> warning
        assert len(gl_issues) == 1
        assert gl_issues[0].severity == "warning"
        assert "no receiver" in gl_issues[0].message

    def test_json_output_includes_global_label_category(self, tmp_path: Path, capsys, monkeypatch):
        """JSON output should include category 'global_label'."""
        from kicad_tools.cli.sch_validate import main

        sch = self._make_schematic("""
          (global_label "NOCAP" (shape input) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "g1"))
        """)
        sch_file = tmp_path / "top.kicad_sch"
        sch_file.write_text(sch)

        monkeypatch.setattr("sys.argv", ["sch-validate", str(sch_file), "--format", "json"])
        import contextlib

        with contextlib.suppress(SystemExit):
            main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        gl_issues = [i for i in data["issues"] if i["category"] == "global_label"]
        assert len(gl_issues) == 1
        assert "NOCAP" in gl_issues[0]["message"]

    def test_validate_schematic_includes_check(self, tmp_path: Path):
        """validate_schematic() should include 'global_label_directions' in checks_run."""
        from kicad_tools.cli.sch_validate import validate_schematic

        sch = self._make_schematic("")
        sch_file = tmp_path / "top.kicad_sch"
        sch_file.write_text(sch)

        result = validate_schematic(str(sch_file))
        assert "global_label_directions" in result.checks_run

    def test_input_bidirectional_no_warning(self, tmp_path: Path):
        """input + bidirectional is a valid complementary pair -- no warning."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        parent_sch = self._make_schematic("""
          (global_label "SWCLK" (shape input) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "h1"))
          (sheet (at 100 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "Sub" (at 100 99 0))
            (property "Sheetfile" "sub.kicad_sch" (at 100 111 0)))
        """)
        child_sch = self._make_schematic("""
          (global_label "SWCLK" (shape bidirectional) (at 30 40 0)
            (effects (font (size 1.27 1.27))) (uuid "h2"))
        """)

        (tmp_path / "top.kicad_sch").write_text(parent_sch)
        (tmp_path / "sub.kicad_sch").write_text(child_sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        gl_issues = [i for i in issues if i.category == "global_label"]
        assert len(gl_issues) == 0

    def test_consistent_shapes_no_warning(self, tmp_path: Path):
        """Same shape on all sheets should not emit a shape-consistency warning."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        parent_sch = self._make_schematic("""
          (global_label "SIG_D" (shape input) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "i1"))
          (sheet (at 100 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "Sub" (at 100 99 0))
            (property "Sheetfile" "sub.kicad_sch" (at 100 111 0)))
        """)
        child_sch = self._make_schematic("""
          (global_label "SIG_D" (shape input) (at 30 40 0)
            (effects (font (size 1.27 1.27))) (uuid "i2"))
        """)

        (tmp_path / "top.kicad_sch").write_text(parent_sch)
        (tmp_path / "sub.kicad_sch").write_text(child_sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        consistency_issues = [
            i for i in issues if i.category == "global_label" and "inconsistent" in i.message
        ]
        assert len(consistency_issues) == 0

    def test_single_sheet_label_no_shape_warning(self, tmp_path: Path):
        """A label on only one sheet should not emit a shape-consistency warning."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        sch = self._make_schematic("""
          (global_label "LONELY" (shape output) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "j1"))
        """)

        (tmp_path / "top.kicad_sch").write_text(sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        consistency_issues = [
            i for i in issues if i.category == "global_label" and "inconsistent" in i.message
        ]
        assert len(consistency_issues) == 0

    def test_mixed_non_driver_shapes_no_warning(self, tmp_path: Path):
        """input + bidirectional + passive across 3 sheets -- no conflicting drivers."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        parent_sch = self._make_schematic("""
          (global_label "MULTI" (shape input) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "k1"))
          (sheet (at 100 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "Sub1" (at 100 99 0))
            (property "Sheetfile" "sub1.kicad_sch" (at 100 111 0)))
          (sheet (at 200 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000003")
            (property "Sheetname" "Sub2" (at 200 99 0))
            (property "Sheetfile" "sub2.kicad_sch" (at 200 111 0)))
        """)
        sub1_sch = self._make_schematic("""
          (global_label "MULTI" (shape bidirectional) (at 30 40 0)
            (effects (font (size 1.27 1.27))) (uuid "k2"))
        """)
        sub2_sch = self._make_schematic("""
          (global_label "MULTI" (shape passive) (at 50 60 0)
            (effects (font (size 1.27 1.27))) (uuid "k3"))
        """)

        (tmp_path / "top.kicad_sch").write_text(parent_sch)
        (tmp_path / "sub1.kicad_sch").write_text(sub1_sch)
        (tmp_path / "sub2.kicad_sch").write_text(sub2_sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        gl_issues = [i for i in issues if i.category == "global_label"]
        assert len(gl_issues) == 0

    def test_conflicting_drivers_detected(self, tmp_path: Path):
        """output + tri_state on the same net -- conflicting drivers warning."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        parent_sch = self._make_schematic("""
          (global_label "BUS_CONFLICT" (shape output) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "m1"))
          (sheet (at 100 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "Sub" (at 100 99 0))
            (property "Sheetfile" "sub.kicad_sch" (at 100 111 0)))
        """)
        child_sch = self._make_schematic("""
          (global_label "BUS_CONFLICT" (shape tri_state) (at 30 40 0)
            (effects (font (size 1.27 1.27))) (uuid "m2"))
        """)

        (tmp_path / "top.kicad_sch").write_text(parent_sch)
        (tmp_path / "sub.kicad_sch").write_text(child_sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        conflict_issues = [
            i for i in issues if i.category == "global_label" and "conflicting" in i.message
        ]

        assert len(conflict_issues) == 1
        assert conflict_issues[0].severity == "warning"
        assert "BUS_CONFLICT" in conflict_issues[0].message
        assert "output" in conflict_issues[0].message
        assert "tri_state" in conflict_issues[0].message

    def test_output_bidirectional_no_warning(self, tmp_path: Path):
        """output + bidirectional is a valid combination -- no warning."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        parent_sch = self._make_schematic("""
          (global_label "SIG_OB" (shape output) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "n1"))
          (sheet (at 100 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "Sub" (at 100 99 0))
            (property "Sheetfile" "sub.kicad_sch" (at 100 111 0)))
        """)
        child_sch = self._make_schematic("""
          (global_label "SIG_OB" (shape bidirectional) (at 30 40 0)
            (effects (font (size 1.27 1.27))) (uuid "n2"))
        """)

        (tmp_path / "top.kicad_sch").write_text(parent_sch)
        (tmp_path / "sub.kicad_sch").write_text(child_sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        gl_issues = [i for i in issues if i.category == "global_label"]
        assert len(gl_issues) == 0

    def test_tri_state_input_no_warning(self, tmp_path: Path):
        """tri_state + input is a valid complementary pair -- no warning."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        parent_sch = self._make_schematic("""
          (global_label "SIG_TI" (shape tri_state) (at 10 20 0)
            (effects (font (size 1.27 1.27))) (uuid "o1"))
          (sheet (at 100 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "Sub" (at 100 99 0))
            (property "Sheetfile" "sub.kicad_sch" (at 100 111 0)))
        """)
        child_sch = self._make_schematic("""
          (global_label "SIG_TI" (shape input) (at 30 40 0)
            (effects (font (size 1.27 1.27))) (uuid "o2"))
        """)

        (tmp_path / "top.kicad_sch").write_text(parent_sch)
        (tmp_path / "sub.kicad_sch").write_text(child_sch)

        issues = check_global_label_directions(str(tmp_path / "top.kicad_sch"))
        gl_issues = [i for i in issues if i.category == "global_label"]
        assert len(gl_issues) == 0


class TestSchValidateSheetParseFailure:
    """Tests that per-sheet parse failures emit info-level ValidationIssues."""

    def _make_hierarchy_with_broken_sheet(self, tmp_path: Path) -> Path:
        """Create a two-sheet hierarchy where the sub-sheet is unparseable."""
        parent_sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (sheet (at 100 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "BrokenSub" (at 100 99 0))
            (property "Sheetfile" "broken.kicad_sch" (at 100 111 0)))
        )
        """
        parent_file = tmp_path / "top.kicad_sch"
        parent_file.write_text(parent_sch)

        # Write a truncated/broken sub-sheet
        broken_file = tmp_path / "broken.kicad_sch"
        broken_file.write_text("(kicad_sch (version 20231120) TRUNCATED")

        return parent_file

    def test_check_missing_footprints_reports_broken_sheet(self, tmp_path: Path):
        """check_missing_footprints should emit an info issue for unparseable sheets."""
        from kicad_tools.cli.sch_validate import check_missing_footprints

        parent_file = self._make_hierarchy_with_broken_sheet(tmp_path)
        issues = check_missing_footprints(str(parent_file))

        info_issues = [i for i in issues if i.severity == "info" and i.category == "footprint"]
        assert len(info_issues) >= 1
        assert any("Skipped sheet" in i.message for i in info_issues)
        # Verify location is populated
        assert all(i.location for i in info_issues)

    def test_check_missing_values_reports_broken_sheet(self, tmp_path: Path):
        """check_missing_values should emit an info issue for unparseable sheets."""
        from kicad_tools.cli.sch_validate import check_missing_values

        parent_file = self._make_hierarchy_with_broken_sheet(tmp_path)
        issues = check_missing_values(str(parent_file))

        info_issues = [i for i in issues if i.severity == "info" and i.category == "value"]
        assert len(info_issues) >= 1
        assert any("Skipped sheet" in i.message for i in info_issues)

    def test_check_global_label_directions_reports_broken_sheet(self, tmp_path: Path):
        """check_global_label_directions should emit an info issue for unparseable sheets."""
        from kicad_tools.cli.sch_validate import check_global_label_directions

        parent_file = self._make_hierarchy_with_broken_sheet(tmp_path)
        issues = check_global_label_directions(str(parent_file))

        info_issues = [i for i in issues if i.severity == "info" and i.category == "global_label"]
        assert len(info_issues) >= 1
        assert any("Skipped sheet" in i.message for i in info_issues)

    def test_all_sheets_broken_reports_one_per_sheet(self, tmp_path: Path):
        """When all sheets fail to parse, result should contain one info issue per broken sheet."""
        from kicad_tools.cli.sch_validate import check_missing_footprints

        parent_sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (sheet (at 100 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Sheetname" "Sub1" (at 100 99 0))
            (property "Sheetfile" "sub1.kicad_sch" (at 100 111 0)))
          (sheet (at 200 100) (size 10 10)
            (uuid "00000000-0000-0000-0000-000000000003")
            (property "Sheetname" "Sub2" (at 200 99 0))
            (property "Sheetfile" "sub2.kicad_sch" (at 200 111 0)))
        )
        """
        parent_file = tmp_path / "top.kicad_sch"
        parent_file.write_text(parent_sch)

        # Both sub-sheets are broken
        (tmp_path / "sub1.kicad_sch").write_text("BROKEN")
        (tmp_path / "sub2.kicad_sch").write_text("BROKEN")

        issues = check_missing_footprints(str(parent_file))

        # The parent sheet itself may also fail to parse, so we check for at least 2
        # broken sub-sheet issues (sub1 and sub2)
        info_issues = [
            i
            for i in issues
            if i.severity == "info" and i.category == "footprint" and "Skipped sheet" in i.message
        ]
        assert len(info_issues) >= 2

    def test_valid_sheets_produce_no_skip_issues(self, tmp_path: Path):
        """Valid sheets should not produce any 'Skipped sheet' info issues."""
        from kicad_tools.cli.sch_validate import check_missing_footprints

        sch = """(kicad_sch
          (version 20231120)
          (generator "test")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
        )
        """
        sch_file = tmp_path / "valid.kicad_sch"
        sch_file.write_text(sch)

        issues = check_missing_footprints(str(sch_file))

        skip_issues = [i for i in issues if "Skipped sheet" in i.message]
        assert len(skip_issues) == 0


class TestSchCheckConnections:
    """Tests for sch_check_connections.py CLI."""

    def test_file_not_found(self, capsys, monkeypatch, tmp_path):
        """Test handling of missing schematic file."""
        from kicad_tools.cli.sch_check_connections import main
        from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError

        # Create a non-existent file path -- no --lib-path needed
        missing_file = "/nonexistent_dir_12345/nonexistent.kicad_sch"
        with pytest.raises((SystemExit, KiCadFileNotFoundError)):
            main([missing_file])

    def test_no_lib_path_uses_embedded(self, capsys, tmp_path):
        """Test that connections work without --lib-path using embedded lib_symbols."""
        from kicad_tools.cli.sch_check_connections import main

        # Schematic with embedded symbol definitions
        sch_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
      (symbol "Device:R_0_1"
        (pin passive line (at 0 -3.81 90) (length 2.54) (name "1") (number "1"))
        (pin passive line (at 0 3.81 270) (length 2.54) (name "2") (number "2"))
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
  (wire
    (pts (xy 100 96.19) (xy 100 90))
    (stroke (width 0) (type default))
    (uuid "00000000-0000-0000-0000-000000000005")
  )
)
"""
        sch_file = tmp_path / "embedded.kicad_sch"
        sch_file.write_text(sch_content)

        # Should succeed without --lib-path
        try:
            main([str(sch_file), "--format", "json"])
        except SystemExit as e:
            assert e.code in (0, 1)

        captured = capsys.readouterr()
        # Must NOT contain any library error
        assert "No symbol libraries loaded" not in captured.err
        # Should produce JSON output with pin data
        if captured.out.strip():
            data = json.loads(captured.out)
            assert "pins" in data
            assert "summary" in data

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

    def test_argv_parameter(self, minimal_schematic: Path, minimal_symbol_library: Path, capsys):
        """Test calling main() with an explicit argv list (CLI runner path)."""
        from kicad_tools.cli.sch_check_connections import main

        try:
            main([str(minimal_schematic), "--lib", str(minimal_symbol_library)])
        except SystemExit as e:
            assert e.code in (0, 1)

        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_no_embedded_no_lib_path(self, minimal_schematic: Path, capsys):
        """Test that schematic with empty lib_symbols and no --lib-path produces LIBRARY_NOT_FOUND."""
        from kicad_tools.cli.sch_check_connections import main

        # minimal_schematic has empty (lib_symbols) and no --lib-path
        try:
            main([str(minimal_schematic), "--format", "json", "--verbose"])
        except SystemExit as e:
            assert e.code in (0, 1)

        captured = capsys.readouterr()
        if captured.out.strip():
            data = json.loads(captured.out)
            assert "pins" in data
            # Should report LIBRARY_NOT_FOUND for symbols without embedded definitions
            found_not_found = any(p["pin_name"] == "LIBRARY_NOT_FOUND" for p in data["pins"])
            assert found_not_found, "Expected LIBRARY_NOT_FOUND for symbol without embedded lib"

    def test_embedded_symbols_no_lib_path(self, capsys, tmp_path):
        """Test that connections work with embedded lib_symbols and no --lib-path."""
        from kicad_tools.cli.sch_check_connections import main

        # Create a schematic with embedded symbol definitions in lib_symbols
        sch_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
      (symbol "Device:R_0_1"
        (pin passive line (at 0 -3.81 90) (length 2.54) (name "1") (number "1"))
        (pin passive line (at 0 3.81 270) (length 2.54) (name "2") (number "2"))
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
  (wire
    (pts (xy 100 96.19) (xy 100 90))
    (stroke (width 0) (type default))
    (uuid "00000000-0000-0000-0000-000000000005")
  )
)
"""
        sch_file = tmp_path / "embedded.kicad_sch"
        sch_file.write_text(sch_content)

        # Should succeed without --lib-path because embedded symbols are loaded
        try:
            main([str(sch_file), "--format", "json"])
        except SystemExit as e:
            assert e.code in (0, 1)

        captured = capsys.readouterr()
        # Must NOT contain the "No symbol libraries loaded" error
        assert "No symbol libraries loaded" not in captured.err
        # Should produce JSON output with pin data
        if captured.out.strip():
            data = json.loads(captured.out)
            assert "pins" in data
            assert "summary" in data

    def test_hierarchy_iterates_sub_sheets(self, capsys, tmp_path):
        """Test that connections check iterates into sub-sheets."""
        from kicad_tools.cli.sch_check_connections import main

        # Create a sub-sheet with its own symbol
        sub_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000010")
  (paper "A4")
  (lib_symbols
    (symbol "Device:C"
      (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "C" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
      (symbol "Device:C_0_1"
        (pin passive line (at 0 -2.54 90) (length 2.54) (name "1") (number "1"))
        (pin passive line (at 0 2.54 270) (length 2.54) (name "2") (number "2"))
      )
    )
  )
  (symbol
    (lib_id "Device:C")
    (at 150 150 0)
    (uuid "00000000-0000-0000-0000-000000000011")
    (property "Reference" "C1" (at 150 140 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 150 160 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 150 150 0) (effects (hide yes)))
    (property "Datasheet" "" (at 150 150 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000012"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000013"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001/00000000-0000-0000-0000-000000000020"
          (reference "C1")
          (unit 1)
        )
      )
    )
  )
)
"""
        sub_file = tmp_path / "sub.kicad_sch"
        sub_file.write_text(sub_content)

        # Create root schematic that references the sub-sheet
        root_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
      (symbol "Device:R_0_1"
        (pin passive line (at 0 -3.81 90) (length 2.54) (name "1") (number "1"))
        (pin passive line (at 0 3.81 270) (length 2.54) (name "2") (number "2"))
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
  (sheet
    (at 200 100)
    (size 50 25)
    (uuid "00000000-0000-0000-0000-000000000020")
    (property "Sheetname" "SubSheet" (at 200 99 0) (effects (font (size 1.27 1.27))))
    (property "Sheetfile" "sub.kicad_sch" (at 200 126 0) (effects (font (size 1.27 1.27))))
  )
)
"""
        root_file = tmp_path / "root.kicad_sch"
        root_file.write_text(root_content)

        try:
            main([str(root_file), "--format", "json", "--verbose"])
        except SystemExit as e:
            assert e.code in (0, 1)

        captured = capsys.readouterr()
        if captured.out.strip():
            data = json.loads(captured.out)
            refs = {p["reference"] for p in data["pins"]}
            # Should find symbols from both root and sub-sheet
            assert "R1" in refs, "Expected R1 from root sheet"
            assert "C1" in refs, "Expected C1 from sub-sheet"


class TestConnectionsSubGridTolerance:
    """Regression tests for sub-grid pin placement tolerance (issue #2199).

    When a component is placed at a non-grid-aligned position, the
    computed pin position may differ from the wire endpoint by a fraction
    of a unit.  The old set-based point_is_connected() missed these
    connections, producing false-positive "unconnected" reports.
    """

    # Schematic with a resistor whose pin 1 is at (100, 96.19) and a wire
    # endpoint at (100, 96.19) -- exact match.
    _SCH_EXACT = """\
(kicad_sch
  (version 20231120) (generator "test") (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001") (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
      (symbol "Device:R_0_1"
        (pin passive line (at 0 -3.81 90) (length 2.54) (name "1") (number "1"))
        (pin passive line (at 0 3.81 270) (length 2.54) (name "2") (number "2"))
      )
    )
  )
  (symbol (lib_id "Device:R") (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances (project "test" (path "/00000000-0000-0000-0000-000000000001" (reference "R1") (unit 1))))
  )
  (wire (pts (xy 100 96.19) (xy 100 90)) (stroke (width 0) (type default)) (uuid "00000000-0000-0000-0000-000000000005"))
  (wire (pts (xy 100 103.81) (xy 100 110)) (stroke (width 0) (type default)) (uuid "00000000-0000-0000-0000-000000000006"))
)
"""

    # Schematic with a resistor at a sub-grid position (100.05, 100) so that
    # pin positions differ from wire endpoints by ~0.05 mm (0.5 units in
    # integer-scaled coordinates).  Wire endpoints are placed at the intended
    # pin positions (rounded to grid).
    _SCH_SUBGRID = """\
(kicad_sch
  (version 20231120) (generator "test") (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001") (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
      (symbol "Device:R_0_1"
        (pin passive line (at 0 -3.81 90) (length 2.54) (name "1") (number "1"))
        (pin passive line (at 0 3.81 270) (length 2.54) (name "2") (number "2"))
      )
    )
  )
  (symbol (lib_id "Device:R") (at 100.05 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances (project "test" (path "/00000000-0000-0000-0000-000000000001" (reference "R1") (unit 1))))
  )
  (wire (pts (xy 100 96.19) (xy 100 90)) (stroke (width 0) (type default)) (uuid "00000000-0000-0000-0000-000000000005"))
  (wire (pts (xy 100 103.81) (xy 100 110)) (stroke (width 0) (type default)) (uuid "00000000-0000-0000-0000-000000000006"))
)
"""

    # Schematic with genuinely unconnected pins (no wires at all).
    _SCH_UNCONNECTED = """\
(kicad_sch
  (version 20231120) (generator "test") (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001") (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
      (symbol "Device:R_0_1"
        (pin passive line (at 0 -3.81 90) (length 2.54) (name "1") (number "1"))
        (pin passive line (at 0 3.81 270) (length 2.54) (name "2") (number "2"))
      )
    )
  )
  (symbol (lib_id "Device:R") (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances (project "test" (path "/00000000-0000-0000-0000-000000000001" (reference "R1") (unit 1))))
  )
)
"""

    def test_connections_exact_match(self, tmp_path, capsys):
        """Pins exactly on wire endpoints are reported as connected."""
        from kicad_tools.cli.sch_check_connections import main

        sch_file = tmp_path / "exact.kicad_sch"
        sch_file.write_text(self._SCH_EXACT)

        main([str(sch_file), "--format", "json", "--verbose"])
        data = json.loads(capsys.readouterr().out)
        pins = {p["pin_number"]: p for p in data["pins"]}
        assert pins["1"]["connected"] is True, "Pin 1 should be connected (exact match)"
        assert pins["2"]["connected"] is True, "Pin 2 should be connected (exact match)"

    def test_connections_subgrid_tolerance(self, tmp_path, capsys):
        """Pins offset by ~0.05 mm from wire endpoints are still connected."""
        from kicad_tools.cli.sch_check_connections import main

        sch_file = tmp_path / "subgrid.kicad_sch"
        sch_file.write_text(self._SCH_SUBGRID)

        main([str(sch_file), "--format", "json", "--verbose"])
        data = json.loads(capsys.readouterr().out)
        pins = {p["pin_number"]: p for p in data["pins"]}
        # With the old set-based approach, these would be falsely reported
        # as unconnected due to the 0.05 mm offset.
        assert pins["1"]["connected"] is True, "Pin 1 should be connected (sub-grid snap)"
        assert pins["2"]["connected"] is True, "Pin 2 should be connected (sub-grid snap)"

    def test_connections_genuinely_unconnected(self, tmp_path, capsys):
        """Pins with no wires are correctly reported as unconnected."""
        from kicad_tools.cli.sch_check_connections import main

        sch_file = tmp_path / "unconnected.kicad_sch"
        sch_file.write_text(self._SCH_UNCONNECTED)

        main([str(sch_file), "--format", "json", "--verbose"])
        data = json.loads(capsys.readouterr().out)
        pins = {p["pin_number"]: p for p in data["pins"]}
        assert pins["1"]["connected"] is False, "Pin 1 should be unconnected (no wires)"
        assert pins["2"]["connected"] is False, "Pin 2 should be unconnected (no wires)"

    def test_unconnected_exact_match(self, tmp_path, capsys):
        """sch_find_unconnected: pins on wires are not reported as unconnected."""
        from kicad_tools.cli.sch_find_unconnected import main

        sch_file = tmp_path / "exact.kicad_sch"
        sch_file.write_text(self._SCH_EXACT)

        main([str(sch_file), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert data["summary"]["unconnected_pin_count"] == 0

    def test_unconnected_subgrid_tolerance(self, tmp_path, capsys):
        """sch_find_unconnected: sub-grid pins on wires are not false positives."""
        from kicad_tools.cli.sch_find_unconnected import main

        sch_file = tmp_path / "subgrid.kicad_sch"
        sch_file.write_text(self._SCH_SUBGRID)

        main([str(sch_file), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert data["summary"]["unconnected_pin_count"] == 0

    def test_unconnected_genuinely_unconnected(self, tmp_path, capsys):
        """sch_find_unconnected: pins with no wires are correctly detected."""
        from kicad_tools.cli.sch_find_unconnected import main

        sch_file = tmp_path / "unconnected.kicad_sch"
        sch_file.write_text(self._SCH_UNCONNECTED)

        main([str(sch_file), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert data["summary"]["unconnected_pin_count"] == 2


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

    def test_argv_parameter(self, minimal_schematic: Path, capsys):
        """Test calling main() with an explicit argv list (CLI runner path)."""
        from kicad_tools.cli.sch_find_unconnected import main

        try:
            main([str(minimal_schematic)])
        except SystemExit as e:
            assert e.code in (0, 1)

        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_argv_parameter_json(self, minimal_schematic: Path, capsys):
        """Test calling main() with argv and --format json."""
        from kicad_tools.cli.sch_find_unconnected import main

        with contextlib.suppress(SystemExit):
            main([str(minimal_schematic), "--format", "json"])

        captured = capsys.readouterr()
        if captured.out.strip():
            data = json.loads(captured.out)
            assert isinstance(data, dict)
            assert "unconnected_pins" in data

    # ---- Regression tests for issue #2665 (netlist cross-check + hierarchy walk) ----

    # Hierarchical schematic: root sheet that references a child sheet
    # via (sheet ... (property "Sheetfile" "child.kicad_sch" ...)).
    # The child sheet contains an isolated symbol whose pin has no wire,
    # so wire-graph analysis on the root would miss it -- only the
    # hierarchy walk will surface it.
    _HIERARCHY_ROOT = """\
(kicad_sch
  (version 20231120) (generator "test") (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001") (paper "A4")
  (lib_symbols)
  (sheet (at 50 50) (size 30 20)
    (uuid "00000000-0000-0000-0000-0000000000aa")
    (property "Sheetname" "Child" (at 50 49 0))
    (property "Sheetfile" "child.kicad_sch" (at 50 71 0))
  )
)
"""

    _HIERARCHY_CHILD = """\
(kicad_sch
  (version 20231120) (generator "test") (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000002") (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
      (symbol "Device:R_0_1"
        (pin passive line (at 0 -3.81 90) (length 2.54) (name "1") (number "1"))
        (pin passive line (at 0 3.81 270) (length 2.54) (name "2") (number "2"))
      )
    )
  )
  (symbol (lib_id "Device:R") (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "R99" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000012"))
    (instances (project "test" (path "/00000000-0000-0000-0000-000000000002" (reference "R99") (unit 1))))
  )
)
"""

    def test_unconnected_json_schema_includes_new_keys(self, tmp_path: Path, capsys):
        """sch unconnected JSON output exposes missing_from_netlist + summary count.

        Even when the cross-check is skipped (kicad-cli unavailable), the
        JSON structure must include the new keys so downstream consumers
        can rely on them.
        """
        from kicad_tools.cli.sch_find_unconnected import main

        sch_file = tmp_path / "minimal.kicad_sch"
        sch_file.write_text(TestConnectionsSubGridTolerance._SCH_UNCONNECTED)

        # Skip the cross-check entirely so the test does not depend on kicad-cli.
        main([str(sch_file), "--format", "json", "--no-check-netlist-export"])
        data = json.loads(capsys.readouterr().out)
        assert "missing_from_netlist" in data, "JSON output must include missing_from_netlist list"
        assert isinstance(data["missing_from_netlist"], list)
        assert "missing_from_netlist_count" in data["summary"], (
            "summary must include missing_from_netlist_count"
        )
        # With --no-check-netlist-export, no findings should be produced.
        assert data["summary"]["missing_from_netlist_count"] == 0
        # The existing schema must still work.
        assert data["summary"]["unconnected_pin_count"] >= 1

    def test_unconnected_no_check_netlist_export_skip(self, tmp_path: Path, capsys, monkeypatch):
        """--no-check-netlist-export disables the cross-check (no kicad-cli call)."""
        from kicad_tools.cli import sch_find_unconnected as mod

        sch_file = tmp_path / "minimal.kicad_sch"
        sch_file.write_text(TestConnectionsSubGridTolerance._SCH_UNCONNECTED)

        # Patch export_netlist to raise if it is unexpectedly invoked.
        def _boom(*args, **kwargs):  # pragma: no cover - assertion path
            raise AssertionError(
                "export_netlist should not be called when --no-check-netlist-export is passed"
            )

        # Import the source module to patch it as referenced from the CLI module.
        import kicad_tools.operations.netlist as netlist_mod

        monkeypatch.setattr(netlist_mod, "export_netlist", _boom)

        mod.main([str(sch_file), "--format", "json", "--no-check-netlist-export"])
        data = json.loads(capsys.readouterr().out)
        assert data["summary"]["missing_from_netlist_count"] == 0
        assert data["missing_from_netlist"] == []

    def test_unconnected_netlist_export_unavailable_skips_gracefully(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        """If kicad-cli is unavailable, the cross-check is skipped with a stderr warning.

        Important: the command MUST still produce its normal output -- a
        silent pass would defeat the purpose of the check.
        """
        from kicad_tools.cli import sch_find_unconnected as mod

        sch_file = tmp_path / "minimal.kicad_sch"
        sch_file.write_text(TestConnectionsSubGridTolerance._SCH_UNCONNECTED)

        # Simulate kicad-cli being unavailable.
        import kicad_tools.operations.netlist as netlist_mod

        def _missing(*args, **kwargs):
            raise FileNotFoundError("kicad-cli not found")

        monkeypatch.setattr(netlist_mod, "export_netlist", _missing)

        mod.main([str(sch_file), "--format", "json"])
        out = capsys.readouterr()
        # JSON output is still produced with the schematic-graph findings.
        data = json.loads(out.out)
        assert data["summary"]["unconnected_pin_count"] == 2  # R1 pins 1 + 2
        assert data["summary"]["missing_from_netlist_count"] == 0
        # And a warning is printed to stderr -- not a silent pass.
        assert "skipped" in out.err.lower() or "warning" in out.err.lower()

    def test_unconnected_detects_dropped_pin_via_netlist(self, tmp_path: Path, capsys, monkeypatch):
        """If kicad-cli drops a symbol from the netlist, the pin is reported.

        This is the core regression test for #2665: a pin that exists in
        the schematic but is missing from the kicad-cli netlist export
        MUST be surfaced as missing_from_netlist (not silently classified
        as "connected" because a wire happens to touch its coordinates).
        """
        from kicad_tools.cli import sch_find_unconnected as mod

        sch_file = tmp_path / "dropped.kicad_sch"
        # Use the existing connected-pins schematic where R1 has wires on
        # both pins; the wire-graph analysis will report 0 unconnected.
        sch_file.write_text(TestConnectionsSubGridTolerance._SCH_EXACT)

        # Fake an empty netlist: kicad-cli "ran" but dropped every symbol.
        import kicad_tools.operations.netlist as netlist_mod
        from kicad_tools.operations.netlist import Netlist

        def _empty_netlist(*args, **kwargs):
            return Netlist()  # no components, no nets

        monkeypatch.setattr(netlist_mod, "export_netlist", _empty_netlist)

        mod.main([str(sch_file), "--format", "json"])
        data = json.loads(capsys.readouterr().out)

        # Wire-graph still reports zero unconnected (the wires exist).
        assert data["summary"]["unconnected_pin_count"] == 0
        # But the netlist cross-check surfaces both of R1's pins.
        assert data["summary"]["missing_from_netlist_count"] >= 2
        refs = {(p["reference"], p["pin_number"]) for p in data["missing_from_netlist"]}
        assert ("R1", "1") in refs
        assert ("R1", "2") in refs
        # Each finding includes a remediation hint pointing at repair-instances.
        for finding in data["missing_from_netlist"]:
            assert "repair-instances" in finding["remediation"]

    def test_unconnected_no_false_positives_when_netlist_matches(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        """When the netlist contains every schematic pin, no findings are emitted."""
        import kicad_tools.operations.netlist as netlist_mod
        from kicad_tools.cli import sch_find_unconnected as mod
        from kicad_tools.operations.netlist import (
            Netlist,
            NetlistNet,
            NetNode,
        )

        sch_file = tmp_path / "good.kicad_sch"
        sch_file.write_text(TestConnectionsSubGridTolerance._SCH_EXACT)

        # Synthesize a netlist that contains both R1.1 and R1.2.
        def _matching_netlist(*args, **kwargs):
            return Netlist(
                nets=[
                    NetlistNet(
                        code=1,
                        name="N1",
                        nodes=[NetNode(reference="R1", pin="1"), NetNode(reference="R1", pin="2")],
                    )
                ]
            )

        monkeypatch.setattr(netlist_mod, "export_netlist", _matching_netlist)

        mod.main([str(sch_file), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert data["summary"]["missing_from_netlist_count"] == 0

    def test_unconnected_hierarchy_walk_includes_subsheet_pins(self, tmp_path: Path, capsys):
        """sch unconnected walks the hierarchy and analyses sub-sheets.

        Previously only the root sheet was analysed; symbols on sub-sheets
        were silently ignored.  This test sets up a root sheet with no
        symbols and a child sheet with an unwired resistor.  After the
        fix, both pins of the child's resistor should be reported.
        """
        from kicad_tools.cli.sch_find_unconnected import main

        root = tmp_path / "root.kicad_sch"
        child = tmp_path / "child.kicad_sch"
        root.write_text(self._HIERARCHY_ROOT)
        child.write_text(self._HIERARCHY_CHILD)

        # Skip the netlist cross-check -- this test is specifically about
        # the wire-graph hierarchy walk, not kicad-cli availability.
        main([str(root), "--format", "json", "--no-check-netlist-export"])
        data = json.loads(capsys.readouterr().out)
        # R99 lives on the child sheet and has two unwired pins.
        refs = {(p["reference"], p["pin_number"]) for p in data["unconnected_pins"]}
        assert ("R99", "1") in refs, f"expected R99.1 in unconnected pins, got {refs}"
        assert ("R99", "2") in refs, f"expected R99.2 in unconnected pins, got {refs}"
        # And the sheet name is propagated in the JSON output for clarity.
        sheets = {p["sheet"] for p in data["unconnected_pins"]}
        assert "child.kicad_sch" in sheets


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


class TestSchValidateMissingProjectInstances:
    """Tests for check_missing_project_instances in sch_validate.py."""

    # -- helpers ----------------------------------------------------------

    _BASE_SYMBOL = """\
  (symbol
    (lib_id "{lib_id}")
    (at 100 100 0)
    (unit {unit})
    (in_bom {in_bom})
    (on_board {on_board})
    (dnp no)
    (uuid "{uuid}")
    (property "Reference" "{ref}" (at 100 90 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "{value}" (at 100 110 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-f00000000001"))
    (pin "2" (uuid "00000000-0000-0000-0000-f00000000002"))
{instances}  )"""

    _INSTANCES_BLOCK = """\
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "{ref}")
          (unit {unit})
        )
      )
    )
"""

    def _make_schematic(
        self,
        tmp_path: Path,
        *,
        lib_id: str = "Device:R",
        ref: str = "R1",
        value: str = "100k",
        include_instances: bool = True,
        in_bom: str = "yes",
        on_board: str = "yes",
        uuid: str = "00000000-0000-0000-0000-000000000002",
        unit: int = 1,
        extra_symbols: str = "",
    ) -> Path:
        instances = ""
        if include_instances:
            instances = self._INSTANCES_BLOCK.format(ref=ref, unit=unit)
        sym = self._BASE_SYMBOL.format(
            lib_id=lib_id,
            ref=ref,
            value=value,
            in_bom=in_bom,
            on_board=on_board,
            uuid=uuid,
            unit=unit,
            instances=instances,
        )
        sch_text = f"""(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
{sym}
{extra_symbols}
)"""
        # Filename stem MUST equal the (project "test"...) name embedded
        # in _INSTANCES_BLOCK above — _detect_project_info uses the root
        # schematic's filename stem as the canonical project name.  See
        # sch_re_annotate._detect_project_info and issue #2664.
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(sch_text)
        return sch_file

    # -- tests ------------------------------------------------------------

    def test_symbol_without_instances_flagged(self, tmp_path: Path):
        """A symbol missing its instances block should produce a warning."""
        from kicad_tools.cli.sch_validate import check_missing_project_instances

        sch_file = self._make_schematic(tmp_path, include_instances=False)
        issues = check_missing_project_instances(str(sch_file))

        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert issues[0].category == "project_instances"
        assert "R1" in issues[0].message
        assert "100k" in issues[0].message

    def test_power_symbol_not_flagged(self, tmp_path: Path):
        """Power symbols should never be reported."""
        from kicad_tools.cli.sch_validate import check_missing_project_instances

        sch_file = self._make_schematic(
            tmp_path,
            lib_id="power:GND",
            ref="#PWR01",
            value="GND",
            include_instances=False,
        )
        issues = check_missing_project_instances(str(sch_file))

        assert len(issues) == 0

    def test_in_bom_false_on_board_false_skipped(self, tmp_path: Path):
        """Symbols with both in_bom=no and on_board=no should be skipped."""
        from kicad_tools.cli.sch_validate import check_missing_project_instances

        sch_file = self._make_schematic(
            tmp_path,
            include_instances=False,
            in_bom="no",
            on_board="no",
        )
        issues = check_missing_project_instances(str(sch_file))

        assert len(issues) == 0

    def test_symbol_with_valid_instances_clean(self, tmp_path: Path):
        """A symbol with a valid instances block should produce no issues."""
        from kicad_tools.cli.sch_validate import check_missing_project_instances

        sch_file = self._make_schematic(tmp_path, include_instances=True)
        issues = check_missing_project_instances(str(sch_file))

        assert len(issues) == 0

    def test_multi_unit_ic_flagged_once(self, tmp_path: Path):
        """A two-unit IC with both units missing instances produces one warning."""
        from kicad_tools.cli.sch_validate import check_missing_project_instances

        # Build a second unit of the same IC (same ref + lib_id, different uuid)
        unit2 = self._BASE_SYMBOL.format(
            lib_id="Device:R",
            ref="R1",
            value="100k",
            in_bom="yes",
            on_board="yes",
            uuid="00000000-0000-0000-0000-000000000099",
            unit=2,
            instances="",
        )
        sch_file = self._make_schematic(
            tmp_path,
            include_instances=False,
            extra_symbols=unit2,
        )
        issues = check_missing_project_instances(str(sch_file))

        assert len(issues) == 1
        assert "R1" in issues[0].message

    def test_validate_schematic_includes_project_instances_check(self, tmp_path: Path):
        """validate_schematic should include project_instances in checks_run."""
        from kicad_tools.cli.sch_validate import validate_schematic

        sch_file = self._make_schematic(tmp_path, include_instances=False)
        result = validate_schematic(str(sch_file))

        assert "project_instances" in result.checks_run
        pi_issues = [i for i in result.issues if i.category == "project_instances"]
        assert len(pi_issues) == 1

    def test_json_output_includes_project_instances(self, tmp_path: Path, capsys, monkeypatch):
        """JSON output should include the project_instances category."""
        from kicad_tools.cli.sch_validate import main

        sch_file = self._make_schematic(tmp_path, include_instances=False)
        monkeypatch.setattr("sys.argv", ["sch-validate", str(sch_file), "--format", "json"])
        with contextlib.suppress(SystemExit):
            main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        pi_issues = [i for i in data["issues"] if i["category"] == "project_instances"]
        assert len(pi_issues) == 1
        assert pi_issues[0]["severity"] == "warning"

    # -- new tests for issue #2664: wrong_project + loose_project_blocks ----

    # Variant of _INSTANCES_BLOCK that names a *different* project.
    # When the root schematic stem is "test", this instances block names
    # "other-project" — kicad-cli will silently drop this symbol from the
    # netlist because the project name does not match.
    _WRONG_PROJECT_INSTANCES_BLOCK = """\
    (instances
      (project "other-project"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "{ref}")
          (unit {unit})
        )
      )
    )
"""

    # Variant where the (project ...) form is a *sibling* of (instances)
    # at symbol-child indent — the malformed shape documented in #2624.
    # (instances) itself is empty.  kicad-cli drops the symbol from the
    # netlist because it only looks inside (instances).
    _LOOSE_PROJECT_BLOCK = """\
    (project "test"
      (path "/00000000-0000-0000-0000-000000000001"
        (reference "{ref}")
        (unit {unit})
      )
    )
    (instances)
"""

    def _make_schematic_with_block(
        self,
        tmp_path: Path,
        block: str,
        *,
        lib_id: str = "Device:R",
        ref: str = "R1",
        value: str = "100k",
        in_bom: str = "yes",
        on_board: str = "yes",
        uuid: str = "00000000-0000-0000-0000-000000000002",
        unit: int = 1,
        extra_symbols: str = "",
    ) -> Path:
        """Build a single-symbol schematic with the given child block.

        ``block`` is the literal text (already formatted) to splice in at the
        ``{instances}`` slot of ``_BASE_SYMBOL``.
        """
        sym = self._BASE_SYMBOL.format(
            lib_id=lib_id,
            ref=ref,
            value=value,
            in_bom=in_bom,
            on_board=on_board,
            uuid=uuid,
            unit=unit,
            instances=block,
        )
        sch_text = f"""(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
{sym}
{extra_symbols}
)"""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(sch_text)
        return sch_file

    def test_wrong_project_flagged_as_error(self, tmp_path: Path):
        """A symbol whose (instances) names the wrong project errors out."""
        from kicad_tools.cli.sch_validate import check_missing_project_instances

        block = self._WRONG_PROJECT_INSTANCES_BLOCK.format(ref="R1", unit=1)
        sch_file = self._make_schematic_with_block(tmp_path, block)

        issues = check_missing_project_instances(str(sch_file))

        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert issues[0].category == "project_instances"
        assert "Wrong project" in issues[0].message
        assert "R1" in issues[0].message
        # Fix hint must be present so users can grep for the command
        assert "repair-instances" in issues[0].message

    def test_wrong_project_power_symbol_flagged(self, tmp_path: Path):
        """Power symbols with wrong_project are still flagged (they need repair too)."""
        from kicad_tools.cli.sch_validate import check_missing_project_instances

        block = self._WRONG_PROJECT_INSTANCES_BLOCK.format(
            ref="#PWR01", unit=1
        )
        sch_file = self._make_schematic_with_block(
            tmp_path,
            block,
            lib_id="power:GND",
            ref="#PWR01",
            value="GND",
        )

        issues = check_missing_project_instances(str(sch_file))

        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "Wrong project" in issues[0].message

    def test_loose_project_blocks_flagged_as_error(self, tmp_path: Path):
        """A symbol with loose (project) blocks outside (instances) errors out."""
        from kicad_tools.cli.sch_validate import check_missing_project_instances

        block = self._LOOSE_PROJECT_BLOCK.format(ref="R1", unit=1)
        sch_file = self._make_schematic_with_block(tmp_path, block)

        issues = check_missing_project_instances(str(sch_file))

        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert issues[0].category == "project_instances"
        assert "Loose" in issues[0].message
        assert "R1" in issues[0].message
        assert "repair-instances" in issues[0].message

    def test_loose_project_blocks_power_symbol_flagged(self, tmp_path: Path):
        """Power symbols with loose_project_blocks are still flagged."""
        from kicad_tools.cli.sch_validate import check_missing_project_instances

        block = self._LOOSE_PROJECT_BLOCK.format(ref="#PWR01", unit=1)
        sch_file = self._make_schematic_with_block(
            tmp_path,
            block,
            lib_id="power:GND",
            ref="#PWR01",
            value="GND",
        )

        issues = check_missing_project_instances(str(sch_file))

        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "Loose" in issues[0].message

    def test_missing_instances_message_includes_fix_hint(self, tmp_path: Path):
        """The legacy missing-instances message must include the repair hint."""
        from kicad_tools.cli.sch_validate import check_missing_project_instances

        sch_file = self._make_schematic(tmp_path, include_instances=False)
        issues = check_missing_project_instances(str(sch_file))

        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert "repair-instances" in issues[0].message

    def test_wrong_project_in_bom_on_board_skip_applies(self, tmp_path: Path):
        """Graphical-only (in_bom=no, on_board=no) symbols skipped for wrong_project too."""
        from kicad_tools.cli.sch_validate import check_missing_project_instances

        block = self._WRONG_PROJECT_INSTANCES_BLOCK.format(ref="R1", unit=1)
        sch_file = self._make_schematic_with_block(
            tmp_path,
            block,
            in_bom="no",
            on_board="no",
        )

        issues = check_missing_project_instances(str(sch_file))
        assert len(issues) == 0

    # List of unrelated checks to skip in end-to-end exit-code tests so the
    # exit code reflects ONLY project_instances severity.  The minimal
    # hand-built fixtures used here trigger noise from ERC, lib_symbols
    # consistency, footprint completeness, etc. — none of which are
    # relevant to whether project_instances raised the severity bar.
    _OTHER_CHECKS = [
        "erc",
        "footprints",
        "values",
        "bom_variety",
        "value_consistency",
        "hierarchy",
        "no_connect_input",
        "global_label_directions",
        "connector_pinout",
        "duplicate_references",
        "pin_assignment",
        "swd_routing",
        "power_short",
        "power_polarity",
        "i2c_pullups",
        "boot0_pulldown",
        "unconnected_component",
        "nrst_filter",
        "symbol_footprint_pin_count",
        "lib_symbols_mismatch",
        "matched_channel_symmetry",
        "orphan_label",
        "wire_stub",
    ]

    def _validate_argv(self, sch_file: Path) -> list[str]:
        """Build argv that runs ONLY the project_instances check."""
        argv = ["sch-validate", str(sch_file)]
        for c in self._OTHER_CHECKS:
            argv.extend(["--skip", c])
        return argv

    def test_exit_code_nonzero_on_wrong_project(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        """End-to-end: kct sch validate exits non-zero when wrong_project present."""
        from kicad_tools.cli.sch_validate import main

        block = self._WRONG_PROJECT_INSTANCES_BLOCK.format(ref="R1", unit=1)
        sch_file = self._make_schematic_with_block(tmp_path, block)

        monkeypatch.setattr("sys.argv", self._validate_argv(sch_file))
        with pytest.raises(SystemExit) as exc_info:
            main()

        # error_count > 0 -> sys.exit(1)
        assert exc_info.value.code == 1

    def test_exit_code_nonzero_on_loose_project(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        """End-to-end: kct sch validate exits non-zero when loose_project_blocks present."""
        from kicad_tools.cli.sch_validate import main

        block = self._LOOSE_PROJECT_BLOCK.format(ref="R1", unit=1)
        sch_file = self._make_schematic_with_block(tmp_path, block)

        monkeypatch.setattr("sys.argv", self._validate_argv(sch_file))
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    def test_exit_code_zero_on_missing_only(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        """End-to-end: missing-instances alone stays a warning (exit 0)."""
        from kicad_tools.cli.sch_validate import main

        sch_file = self._make_schematic(tmp_path, include_instances=False)

        monkeypatch.setattr("sys.argv", self._validate_argv(sch_file))
        # Either no SystemExit at all, or SystemExit(0).
        try:
            main()
            exit_code = 0
        except SystemExit as e:
            exit_code = e.code or 0
        assert exit_code == 0

    def test_json_output_wrong_project_is_error(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        """JSON output for wrong_project surfaces severity=error in project_instances."""
        from kicad_tools.cli.sch_validate import main

        block = self._WRONG_PROJECT_INSTANCES_BLOCK.format(ref="R1", unit=1)
        sch_file = self._make_schematic_with_block(tmp_path, block)

        monkeypatch.setattr(
            "sys.argv", ["sch-validate", str(sch_file), "--format", "json"]
        )
        with contextlib.suppress(SystemExit):
            main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        pi_issues = [
            i for i in data["issues"] if i["category"] == "project_instances"
        ]
        assert len(pi_issues) == 1
        assert pi_issues[0]["severity"] == "error"

    def test_validator_and_repair_dry_run_agree(
        self, tmp_path: Path, capsys
    ):
        """Validator and repair-instances --dry-run report the same count."""
        from kicad_tools.cli.sch_repair_instances import run_repair_instances
        from kicad_tools.cli.sch_validate import check_missing_project_instances

        # Schematic with one wrong_project symbol + one loose_project symbol.
        wrong_block = self._WRONG_PROJECT_INSTANCES_BLOCK.format(
            ref="R1", unit=1
        )
        loose_block = self._LOOSE_PROJECT_BLOCK.format(ref="R2", unit=1)

        sym1 = self._BASE_SYMBOL.format(
            lib_id="Device:R",
            ref="R1",
            value="10k",
            in_bom="yes",
            on_board="yes",
            uuid="00000000-0000-0000-0000-000000000002",
            unit=1,
            instances=wrong_block,
        )
        sym2 = self._BASE_SYMBOL.format(
            lib_id="Device:R",
            ref="R2",
            value="4.7k",
            in_bom="yes",
            on_board="yes",
            uuid="00000000-0000-0000-0000-000000000003",
            unit=1,
            instances=loose_block,
        )
        sch_text = f"""(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
{sym1}
{sym2}
)"""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(sch_text)

        # Validator side
        issues = check_missing_project_instances(str(sch_file))
        validator_count = sum(1 for i in issues if i.severity == "error")
        assert validator_count == 2  # one wrong + one loose

        # Repair-instances dry-run side (JSON for stable counting)
        run_repair_instances(
            sch_file, dry_run=True, backup=False, format="json"
        )
        captured = capsys.readouterr()
        # The JSON envelope reports a 'total' field
        data = json.loads(captured.out)
        repair_count = data["total"]

        assert (
            validator_count == repair_count
        ), (
            f"validate reported {validator_count} errors but "
            f"repair-instances dry-run reported {repair_count}"
        )
