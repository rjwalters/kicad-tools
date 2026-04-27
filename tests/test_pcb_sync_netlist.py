"""Tests for pcb sync-netlist command (pcb_sync_netlist module)."""

import json
from pathlib import Path

import pytest


# Minimal schematic with two components
MINIMAL_SCHEMATIC = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402" (at 100 100 0) (effects (hide yes)))
  )
  (symbol
    (lib_id "Device:C")
    (at 120 100 0)
    (uuid "00000000-0000-0000-0000-000000000003")
    (property "Reference" "C1" (at 120 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100n" (at 120 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402" (at 120 100 0) (effects (hide yes)))
  )
)
"""

# Schematic with only R1
SCHEMATIC_R1_ONLY = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402" (at 100 100 0) (effects (hide yes)))
  )
)
"""

# PCB with R1 and C1 matching schematic
MINIMAL_PCB_MATCHING = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""

# PCB with only C1 (R1 missing)
PCB_MISSING_R1 = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""

# PCB with D1 orphan (not in schematic)
PCB_WITH_ORPHAN = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "LED_SMD:LED_0603"
    (layer "F.Cu")
    (uuid "fp-d1")
    (at 140 100)
    (property "Reference" "D1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "LED" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""

# PCB with R99 (renamed to R1 in schematic)
PCB_WITH_RENAMED_REF = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r99")
    (at 100 100)
    (property "Reference" "R99" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""


class TestNormalizeFootprint:
    """Tests for _normalize_footprint helper."""

    def test_strips_library_prefix(self):
        from kicad_tools.cli.pcb_sync_netlist import _normalize_footprint

        assert _normalize_footprint("Resistor_SMD:R_0402") == "R_0402"

    def test_no_prefix_unchanged(self):
        from kicad_tools.cli.pcb_sync_netlist import _normalize_footprint

        assert _normalize_footprint("R_0402") == "R_0402"

    def test_empty_string(self):
        from kicad_tools.cli.pcb_sync_netlist import _normalize_footprint

        assert _normalize_footprint("") == ""

    def test_multiple_colons_uses_first(self):
        from kicad_tools.cli.pcb_sync_netlist import _normalize_footprint

        # Only first colon is the library separator
        assert _normalize_footprint("lib:fp:extra") == "fp:extra"


class TestSyncAction:
    """Tests for SyncAction dataclass."""

    def test_creation(self):
        from kicad_tools.cli.pcb_sync_netlist import SyncAction

        action = SyncAction(
            action="add",
            reference="R1",
            footprint="Resistor_SMD:R_0402",
            value="10k",
        )
        assert action.action == "add"
        assert action.reference == "R1"
        assert action.footprint == "Resistor_SMD:R_0402"
        assert action.value == "10k"

    def test_rename_action(self):
        from kicad_tools.cli.pcb_sync_netlist import SyncAction

        action = SyncAction(
            action="rename",
            reference="R1",
            old_reference="R99",
            footprint="Resistor_SMD:R_0402",
            value="10k",
        )
        assert action.old_reference == "R99"
        assert action.reference == "R1"


class TestSyncResult:
    """Tests for SyncResult dataclass."""

    def test_empty_has_no_changes(self):
        from kicad_tools.cli.pcb_sync_netlist import SyncResult

        result = SyncResult()
        assert result.has_changes is False

    def test_has_changes_with_added(self):
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult

        result = SyncResult(
            added=[SyncAction(action="add", reference="R1")]
        )
        assert result.has_changes is True

    def test_has_changes_with_orphaned(self):
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult

        result = SyncResult(
            orphaned=[SyncAction(action="orphan", reference="D1")]
        )
        assert result.has_changes is True

    def test_has_changes_with_renamed(self):
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult

        result = SyncResult(
            renamed=[SyncAction(action="rename", reference="R1", old_reference="R99")]
        )
        assert result.has_changes is True


class TestSyncNetlist:
    """Tests for the core sync_netlist function."""

    def test_in_sync_returns_no_changes(self, tmp_path):
        """Matching schematic and PCB produce no changes."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(MINIMAL_PCB_MATCHING)

        result = sync_netlist(sch, pcb, dry_run=True)

        assert not result.added
        assert not result.renamed
        assert not result.orphaned
        assert not result.errors
        assert not result.has_changes

    def test_detects_missing_footprint(self, tmp_path):
        """R1 missing from PCB is detected as needing to be added."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_MISSING_R1)

        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.added) == 1
        assert result.added[0].reference == "R1"
        assert "R_0402" in result.added[0].footprint
        assert result.added[0].value == "10k"
        assert not result.errors

    def test_detects_orphaned_footprint(self, tmp_path):
        """D1 in PCB but not in schematic is detected as orphaned."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_ORPHAN)

        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.orphaned) == 1
        assert result.orphaned[0].reference == "D1"
        assert not result.added
        assert not result.renamed
        assert not result.errors

    def test_detects_rename(self, tmp_path):
        """R99 in PCB matched to R1 in schematic by value+footprint is a rename."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_RENAMED_REF)

        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.renamed) == 1
        assert result.renamed[0].reference == "R1"
        assert result.renamed[0].old_reference == "R99"
        assert not result.added
        # No orphans since R99 was matched
        assert not result.orphaned
        assert not result.errors

    def test_dry_run_does_not_modify_pcb(self, tmp_path):
        """dry_run=True leaves PCB file unchanged."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_MISSING_R1)
        original_content = pcb.read_text()

        sync_netlist(sch, pcb, dry_run=True)

        assert pcb.read_text() == original_content

    def test_empty_schematic_reports_all_pcb_as_orphaned(self, tmp_path):
        """Empty/missing schematic results in all PCB footprints reported as orphaned."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "empty.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        # Write an empty schematic (no symbols)
        sch.write_text(
            "(kicad_sch (version 20231120) (generator test) (uuid 0) (paper A4))"
        )
        pcb.write_text(MINIMAL_PCB_MATCHING)

        result = sync_netlist(sch, pcb, dry_run=True)

        # Both R1 and C1 should be orphaned
        assert len(result.orphaned) == 2
        assert not result.added
        assert not result.errors

    def test_pcb_not_found_returns_error(self, tmp_path):
        """Non-existent PCB produces an error."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "nonexistent.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)

        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.errors) > 0


class TestGetBoardEdgePosition:
    """Tests for _get_board_edge_position helper."""

    def test_returns_float_tuple(self, tmp_path):
        """Returns a (float, float) tuple."""
        from kicad_tools.cli.pcb_sync_netlist import _get_board_edge_position
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB_MATCHING)
        board = PCB.load(pcb)

        pos = _get_board_edge_position(board)

        assert isinstance(pos, tuple)
        assert len(pos) == 2
        assert isinstance(pos[0], float)
        assert isinstance(pos[1], float)

    def test_fallback_no_outline(self, tmp_path):
        """Falls back to (0.0, 0.0) when there is no board outline."""
        from kicad_tools.cli.pcb_sync_netlist import _get_board_edge_position
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB_MATCHING)
        board = PCB.load(pcb)

        # Mock get_board_outline to return None
        board.get_board_outline = lambda: None

        pos = _get_board_edge_position(board)

        assert pos == (0.0, 0.0)


class TestFormatText:
    """Tests for format_text output."""

    def test_in_sync_message(self, tmp_path):
        """In-sync state produces a clear no-changes message."""
        from kicad_tools.cli.pcb_sync_netlist import SyncResult, format_text

        result = SyncResult()
        pcb = tmp_path / "test.kicad_pcb"
        output = format_text(result, dry_run=True, pcb_path=pcb)

        assert "No changes needed" in output

    def test_added_appears_in_output(self, tmp_path):
        """Added footprints appear in text output."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_text

        result = SyncResult(
            added=[SyncAction(action="add", reference="R1", footprint="Resistor_SMD:R_0402", value="10k")]
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_text(result, dry_run=True, pcb_path=pcb)

        assert "R1" in output
        assert "R_0402" in output

    def test_orphaned_appears_in_output(self, tmp_path):
        """Orphaned footprints appear in text output."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_text

        result = SyncResult(
            orphaned=[SyncAction(action="orphan", reference="D1", footprint="LED_SMD:LED_0603", value="LED")]
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_text(result, dry_run=True, pcb_path=pcb)

        assert "D1" in output
        assert "Orphan" in output

    def test_renamed_appears_in_output(self, tmp_path):
        """Renamed references appear in text output."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_text

        result = SyncResult(
            renamed=[SyncAction(action="rename", reference="R1", old_reference="R99")]
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_text(result, dry_run=True, pcb_path=pcb)

        assert "R99" in output
        assert "R1" in output

    def test_dry_run_label(self, tmp_path):
        """dry_run=True shows dry run label."""
        from kicad_tools.cli.pcb_sync_netlist import SyncResult, format_text

        result = SyncResult()
        pcb = tmp_path / "test.kicad_pcb"
        output = format_text(result, dry_run=True, pcb_path=pcb)

        assert "dry run" in output.lower()

    def test_non_dry_run_label(self, tmp_path):
        """dry_run=False shows non-dry-run label."""
        from kicad_tools.cli.pcb_sync_netlist import SyncResult, format_text

        result = SyncResult()
        pcb = tmp_path / "test.kicad_pcb"
        output = format_text(result, dry_run=False, pcb_path=pcb)

        assert "dry run" not in output.lower()


class TestFormatJson:
    """Tests for format_json output."""

    def test_valid_json(self, tmp_path):
        """Output is parseable JSON."""
        from kicad_tools.cli.pcb_sync_netlist import SyncResult, format_json

        result = SyncResult()
        pcb = tmp_path / "test.kicad_pcb"
        output = format_json(result, dry_run=True, pcb_path=pcb)

        data = json.loads(output)
        assert "pcb" in data
        assert "dry_run" in data
        assert "added" in data
        assert "orphaned" in data
        assert "renamed" in data
        assert "errors" in data

    def test_dry_run_true(self, tmp_path):
        """dry_run field is True when dry_run=True."""
        from kicad_tools.cli.pcb_sync_netlist import SyncResult, format_json

        result = SyncResult()
        pcb = tmp_path / "test.kicad_pcb"
        output = format_json(result, dry_run=True, pcb_path=pcb)

        data = json.loads(output)
        assert data["dry_run"] is True

    def test_added_in_json(self, tmp_path):
        """Added footprints are serialized in JSON."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_json

        result = SyncResult(
            added=[SyncAction(action="add", reference="R1", footprint="Resistor_SMD:R_0402", value="10k")]
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_json(result, dry_run=False, pcb_path=pcb)

        data = json.loads(output)
        assert len(data["added"]) == 1
        assert data["added"][0]["reference"] == "R1"
        assert data["added"][0]["footprint"] == "Resistor_SMD:R_0402"

    def test_renamed_in_json(self, tmp_path):
        """Renamed references are serialized in JSON."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_json

        result = SyncResult(
            renamed=[SyncAction(action="rename", reference="R1", old_reference="R99")]
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_json(result, dry_run=False, pcb_path=pcb)

        data = json.loads(output)
        assert len(data["renamed"]) == 1
        assert data["renamed"][0]["old_reference"] == "R99"
        assert data["renamed"][0]["new_reference"] == "R1"

    def test_orphaned_in_json(self, tmp_path):
        """Orphaned footprints are serialized in JSON."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_json

        result = SyncResult(
            orphaned=[SyncAction(action="orphan", reference="D1", footprint="LED_SMD:LED_0603", value="LED")]
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_json(result, dry_run=True, pcb_path=pcb)

        data = json.loads(output)
        assert len(data["orphaned"]) == 1
        assert data["orphaned"][0]["reference"] == "D1"


class TestRunSyncNetlist:
    """Tests for the run_sync_netlist entrypoint function."""

    def test_returns_0_on_success(self, tmp_path):
        """In-sync returns exit code 0."""
        from kicad_tools.cli.pcb_sync_netlist import run_sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(MINIMAL_PCB_MATCHING)

        rc = run_sync_netlist(sch, pcb, dry_run=True)
        assert rc == 0

    def test_returns_0_when_only_orphans(self, tmp_path):
        """Orphaned footprints do not cause a non-zero exit code."""
        from kicad_tools.cli.pcb_sync_netlist import run_sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_ORPHAN)

        rc = run_sync_netlist(sch, pcb, dry_run=True)
        assert rc == 0

    def test_json_format_output(self, tmp_path, capsys):
        """JSON format produces parseable output."""
        from kicad_tools.cli.pcb_sync_netlist import run_sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(MINIMAL_PCB_MATCHING)

        run_sync_netlist(sch, pcb, dry_run=True, output_format="json")

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "added" in data
        assert "orphaned" in data
        assert "renamed" in data

    def test_text_format_output(self, tmp_path, capsys):
        """Text format produces human-readable output."""
        from kicad_tools.cli.pcb_sync_netlist import run_sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(MINIMAL_PCB_MATCHING)

        run_sync_netlist(sch, pcb, dry_run=True, output_format="text")

        captured = capsys.readouterr()
        assert "PCB Sync Netlist" in captured.out


class TestPcbSyncNetlistCLIDispatch:
    """Tests for the pcb sync-netlist CLI dispatch."""

    def test_parser_has_sync_netlist_subcommand(self):
        """Parser supports 'pcb sync-netlist' subcommand."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        # Should not raise
        args = parser.parse_args(["pcb", "sync-netlist", "--schematic", "test.kicad_sch", "test.kicad_pcb"])
        assert args.pcb_command == "sync-netlist"
        assert args.schematic == "test.kicad_sch"
        assert args.pcb == "test.kicad_pcb"

    def test_parser_dry_run_flag(self):
        """Parser accepts --dry-run flag."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            ["pcb", "sync-netlist", "--schematic", "test.kicad_sch", "--dry-run", "test.kicad_pcb"]
        )
        assert args.dry_run is True

    def test_parser_format_flag(self):
        """Parser accepts --format flag."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            ["pcb", "sync-netlist", "--schematic", "test.kicad_sch", "--format", "json", "test.kicad_pcb"]
        )
        assert args.format == "json"

    def test_dispatcher_missing_schematic_returns_1(self, tmp_path):
        """Missing --schematic flag returns exit code 1."""
        from kicad_tools.cli.commands.pcb import _run_sync_netlist_command

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB_MATCHING)

        class Args:
            schematic = None
            output = None
            dry_run = False
            format = "text"
            remove_orphans = False
            force = False
            auto_rename = False

        rc = _run_sync_netlist_command(Args(), pcb)
        assert rc == 1

    def test_dispatcher_nonexistent_schematic_returns_1(self, tmp_path):
        """Non-existent schematic returns exit code 1."""
        from kicad_tools.cli.commands.pcb import _run_sync_netlist_command

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB_MATCHING)

        class Args:
            schematic = str(tmp_path / "nonexistent.kicad_sch")
            output = None
            dry_run = False
            format = "text"
            remove_orphans = False
            force = False
            auto_rename = False

        rc = _run_sync_netlist_command(Args(), pcb)
        assert rc == 1


# --- PCB with orphan D1 that has routed traces ---
PCB_WITH_ORPHAN_TRACED = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "LED_A")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "LED_SMD:LED_0603"
    (layer "F.Cu")
    (uuid "fp-d1")
    (at 140 100)
    (property "Reference" "D1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "LED" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "LED_A"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
  )
  (segment (start 139.5 100) (end 130 100) (width 0.25) (layer "F.Cu") (net 2))
)
"""

# --- Test fixtures for new Gap tests ---

# Schematic with R1(10k, R_0402) and R2(10k, R_0402) -- two same-signature components
SCHEMATIC_R1_R2_SAME = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402" (at 100 100 0) (effects (hide yes)))
  )
  (symbol
    (lib_id "Device:R")
    (at 120 100 0)
    (uuid "00000000-0000-0000-0000-000000000003")
    (property "Reference" "R2" (at 120 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 120 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402" (at 120 100 0) (effects (hide yes)))
  )
)
"""

# PCB with R50 and R51 -- two same-signature footprints (ambiguous match for R1/R2)
PCB_R50_R51_SAME = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r50")
    (at 100 100)
    (property "Reference" "R50" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r51")
    (at 120 100)
    (property "Reference" "R51" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""

# Schematic with U8(MCU) and U15(REG) -- for multi-rename test
SCHEMATIC_U8_U15 = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "U8" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "MCU" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Package_QFP:QFP-32" (at 100 100 0) (effects (hide yes)))
  )
  (symbol
    (lib_id "Device:R")
    (at 120 100 0)
    (uuid "00000000-0000-0000-0000-000000000011")
    (property "Reference" "U15" (at 120 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "REG" (at 120 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Package_SO:SO-8" (at 120 100 0) (effects (hide yes)))
  )
)
"""

# PCB with U3(MCU, QFP-32) and U10(REG, SO-8) -- both renamed in schematic
# U3->U8 and U10->U15 (independent renames, no collision chain)
PCB_U3_U10 = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (footprint "Package_QFP:QFP-32"
    (layer "F.Cu")
    (uuid "fp-u3")
    (at 100 100)
    (property "Reference" "U3" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "MCU" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Package_SO:SO-8"
    (layer "F.Cu")
    (uuid "fp-u10")
    (at 120 100)
    (property "Reference" "U10" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "REG" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""


class TestRemoveOrphans:
    """Tests for --remove-orphans functionality in sync_netlist."""

    def test_remove_orphans_removes_untraced_footprint(self, tmp_path):
        """Orphaned footprint without traces is removed."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist
        from kicad_tools.schema.pcb import PCB

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_ORPHAN)

        result = sync_netlist(sch, pcb, dry_run=False, remove_orphans=True)

        assert len(result.removed) == 1
        assert result.removed[0].reference == "D1"
        assert result.removed[0].action == "remove"
        assert not result.orphaned
        assert not result.errors

        # Verify D1 was actually removed from the PCB file
        board = PCB.load(pcb)
        assert board.get_footprint("D1") is None
        assert board.get_footprint("R1") is not None
        assert board.get_footprint("C1") is not None

    def test_remove_orphans_dry_run_does_not_modify(self, tmp_path):
        """--remove-orphans with --dry-run reports but does not modify."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_ORPHAN)
        original = pcb.read_text()

        result = sync_netlist(sch, pcb, dry_run=True, remove_orphans=True)

        assert len(result.removed) == 1
        assert result.removed[0].reference == "D1"
        assert pcb.read_text() == original

    def test_remove_orphans_blocks_traced_footprint(self, tmp_path):
        """Orphaned footprint with traces is blocked without --force."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_ORPHAN_TRACED)

        result = sync_netlist(sch, pcb, dry_run=True, remove_orphans=True)

        assert len(result.orphaned) == 1
        assert result.orphaned[0].reference == "D1"
        assert not result.removed
        assert any("D1" in e and "traces" in e for e in result.errors)

    def test_remove_orphans_force_removes_traced_footprint(self, tmp_path):
        """--force allows removal of orphaned footprint with traces."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist
        from kicad_tools.schema.pcb import PCB

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_ORPHAN_TRACED)

        result = sync_netlist(
            sch, pcb, dry_run=False, remove_orphans=True, force=True
        )

        assert len(result.removed) == 1
        assert result.removed[0].reference == "D1"
        assert not result.orphaned
        assert not result.errors

        board = PCB.load(pcb)
        assert board.get_footprint("D1") is None

    def test_remove_orphans_no_orphans_is_noop(self, tmp_path):
        """--remove-orphans with no orphans is a clean no-op."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(MINIMAL_PCB_MATCHING)

        result = sync_netlist(sch, pcb, dry_run=True, remove_orphans=True)

        assert not result.removed
        assert not result.orphaned
        assert not result.errors

    def test_remove_orphans_all_orphaned(self, tmp_path):
        """When all footprints are orphaned, they should all be removed."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist
        from kicad_tools.schema.pcb import PCB

        sch = tmp_path / "empty.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(
            "(kicad_sch (version 20231120) (generator test) (uuid 0) (paper A4))"
        )
        pcb.write_text(MINIMAL_PCB_MATCHING)

        result = sync_netlist(sch, pcb, dry_run=False, remove_orphans=True)

        assert len(result.removed) == 2
        refs = {a.reference for a in result.removed}
        assert refs == {"R1", "C1"}

        board = PCB.load(pcb)
        assert board.get_footprint("R1") is None
        assert board.get_footprint("C1") is None

    def test_without_remove_orphans_behavior_unchanged(self, tmp_path):
        """Without --remove-orphans, orphans are only reported (not removed)."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_ORPHAN)

        result = sync_netlist(sch, pcb, dry_run=True, remove_orphans=False)

        assert len(result.orphaned) == 1
        assert result.orphaned[0].reference == "D1"
        assert not result.removed


class TestRemoveOrphansFormatting:
    """Tests for removed footprints in text and JSON output."""

    def test_removed_in_text_output(self, tmp_path):
        """Removed footprints appear in text output."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_text

        result = SyncResult(
            removed=[SyncAction(
                action="remove",
                reference="D1",
                footprint="LED_SMD:LED_0603",
                value="LED",
            )]
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_text(result, dry_run=False, pcb_path=pcb)

        assert "Removed" in output
        assert "D1" in output

    def test_removed_in_json_output(self, tmp_path):
        """Removed footprints appear in JSON output."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_json

        result = SyncResult(
            removed=[SyncAction(
                action="remove",
                reference="D1",
                footprint="LED_SMD:LED_0603",
                value="LED",
            )]
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_json(result, dry_run=False, pcb_path=pcb)

        data = json.loads(output)
        assert "removed" in data
        assert len(data["removed"]) == 1
        assert data["removed"][0]["reference"] == "D1"


class TestSyncNetlistCLIRemoveOrphansFlags:
    """Tests for --remove-orphans and --force CLI flags."""

    def test_parser_remove_orphans_flag(self):
        """Parser accepts --remove-orphans flag."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args([
            "pcb", "sync-netlist",
            "--schematic", "test.kicad_sch",
            "--remove-orphans",
            "test.kicad_pcb",
        ])
        assert args.remove_orphans is True

    def test_parser_force_flag(self):
        """Parser accepts --force flag for sync-netlist."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args([
            "pcb", "sync-netlist",
            "--schematic", "test.kicad_sch",
            "--remove-orphans",
            "--force",
            "test.kicad_pcb",
        ])
        assert args.force is True

    def test_dispatcher_passes_remove_orphans(self, tmp_path):
        """Dispatcher passes remove_orphans and force to run_sync_netlist."""
        from kicad_tools.cli.commands.pcb import _run_sync_netlist_command

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_ORPHAN)

        class Args:
            schematic = str(sch)
            output = None
            dry_run = False
            format = "text"
            remove_orphans = True
            force = False
            auto_rename = False

        rc = _run_sync_netlist_command(Args(), pcb)
        assert rc == 0


class TestFootprintHasTraces:
    """Tests for PCB.footprint_has_traces helper."""

    def test_footprint_without_traces(self, tmp_path):
        """Footprint with no connected segments returns False."""
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(PCB_WITH_ORPHAN)
        board = PCB.load(pcb)

        # D1 has no net connections (net 0)
        assert board.footprint_has_traces("D1") is False

    def test_footprint_with_traces(self, tmp_path):
        """Footprint with connected segments returns True."""
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(PCB_WITH_ORPHAN_TRACED)
        board = PCB.load(pcb)

        # D1 pad 1 is on net 2 with a segment touching it
        assert board.footprint_has_traces("D1") is True

    def test_nonexistent_footprint(self, tmp_path):
        """Non-existent footprint returns False."""
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB_MATCHING)
        board = PCB.load(pcb)

        assert board.footprint_has_traces("Z99") is False


class TestCollisionSafeRenames:
    """Tests for Gap 1: collision-safe rename handling via _build_rename_plan."""

    def test_multiple_renames_detected(self, tmp_path):
        """U3->U8 and U10->U15 should both be detected as renames."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_U8_U15)
        pcb.write_text(PCB_U3_U10)

        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.renamed) == 2
        renames = {a.old_reference: a.reference for a in result.renamed}
        assert renames == {"U3": "U8", "U10": "U15"}
        assert not result.added
        assert not result.orphaned
        assert not result.errors

    def test_multiple_renames_applied_safely(self, tmp_path):
        """Multiple renames are applied using collision-safe rename plan."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist
        from kicad_tools.schema.pcb import PCB

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_U8_U15)
        pcb.write_text(PCB_U3_U10)

        result = sync_netlist(sch, pcb, dry_run=False, auto_rename=True)

        assert len(result.renamed) == 2
        assert not result.errors

        # Verify the PCB was actually updated correctly
        board = PCB.load(pcb)
        refs = {fp.reference for fp in board.footprints}
        assert refs == {"U8", "U15"}

        # Verify the values are on the correct references
        ref_vals = {fp.reference: fp.value for fp in board.footprints}
        assert ref_vals["U8"] == "MCU"
        assert ref_vals["U15"] == "REG"

    def test_build_rename_plan_resolves_chain(self):
        """_build_rename_plan correctly resolves collision chains via temp refs."""
        from kicad_tools.cli.commands.pcb import _build_rename_plan

        # Simulate a collision chain: A->B and B->C
        # B is both a source and a target
        mapping = {"A": "B", "B": "C"}
        existing = {"A", "B"}

        steps, warnings, errors = _build_rename_plan(mapping, existing)

        assert not errors
        # Should use temp refs to avoid overwriting B before it's renamed
        assert len(steps) > 2  # Direct would be 2, chain needs more

        # Verify that after applying all steps in order, we get the right result
        state = {"A": "A_val", "B": "B_val"}
        for from_ref, to_ref, _via in steps:
            if from_ref in state:
                state[to_ref] = state.pop(from_ref)

        assert "B" in state and state["B"] == "A_val"
        assert "C" in state and state["C"] == "B_val"

    def test_apply_renames_safe_delegates_to_build_rename_plan(self, tmp_path):
        """_apply_renames_safe uses _build_rename_plan for safe execution."""
        from kicad_tools.cli.pcb_sync_netlist import SyncResult, _apply_renames_safe
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(PCB_WITH_RENAMED_REF)
        board = PCB.load(pcb)

        result = SyncResult()
        rename_map = {"R99": "R1"}
        pcb_refs = {"R99", "C1"}

        _apply_renames_safe(board, rename_map, pcb_refs, result)

        assert not result.errors
        refs = {fp.reference for fp in board.footprints}
        assert "R1" in refs
        assert "R99" not in refs


class TestAmbiguousMatchWarnings:
    """Tests for Gap 2: ambiguous match warnings."""

    def test_ambiguous_match_produces_warning(self, tmp_path):
        """Multiple orphans with same signature produce a warning."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_R1_R2_SAME)
        pcb.write_text(PCB_R50_R51_SAME)

        result = sync_netlist(sch, pcb, dry_run=True)

        # No renames because signature is ambiguous (2 schematic, 2 PCB)
        assert not result.renamed
        # Both schematic refs should be added (not matched)
        assert len(result.added) == 2
        # Both PCB refs should be orphaned (not matched)
        assert len(result.orphaned) == 2
        # Should have a warning about ambiguous match
        assert len(result.warnings) == 1
        assert "Ambiguous" in result.warnings[0]
        assert "R_0402" in result.warnings[0]

    def test_warning_in_text_output(self, tmp_path):
        """Warnings appear in text format output alongside other changes."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_text

        result = SyncResult(
            orphaned=[SyncAction(action="orphan", reference="R50", footprint="R_0402", value="10k")],
            warnings=["Ambiguous match for (R_0402, 10k)"],
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_text(result, dry_run=True, pcb_path=pcb)

        assert "Warnings" in output
        assert "Ambiguous" in output

    def test_warning_in_json_output(self, tmp_path):
        """Warnings appear in JSON format output."""
        from kicad_tools.cli.pcb_sync_netlist import SyncResult, format_json

        result = SyncResult(warnings=["Ambiguous match for (R_0402, 10k)"])
        pcb = tmp_path / "test.kicad_pcb"
        output = format_json(result, dry_run=True, pcb_path=pcb)

        import json
        data = json.loads(output)
        assert "warnings" in data
        assert len(data["warnings"]) == 1
        assert "Ambiguous" in data["warnings"][0]

    def test_no_warning_for_unique_match(self, tmp_path):
        """Unique 1:1 matches produce no warnings."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_RENAMED_REF)

        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.renamed) == 1
        assert not result.warnings


class TestAutoRenameFlag:
    """Tests for Gap 3: --auto-rename flag and interactive confirmation."""

    def test_parser_accepts_auto_rename(self):
        """Parser supports --auto-rename flag."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args([
            "pcb", "sync-netlist",
            "--schematic", "test.kicad_sch",
            "--auto-rename",
            "test.kicad_pcb",
        ])
        assert args.auto_rename is True

    def test_parser_auto_rename_default_false(self):
        """--auto-rename defaults to False."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args([
            "pcb", "sync-netlist",
            "--schematic", "test.kicad_sch",
            "test.kicad_pcb",
        ])
        assert args.auto_rename is False

    def test_auto_rename_applies_renames(self, tmp_path):
        """auto_rename=True applies renames without prompt."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist
        from kicad_tools.schema.pcb import PCB

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_RENAMED_REF)

        result = sync_netlist(sch, pcb, dry_run=False, auto_rename=True)

        assert len(result.renamed) == 1
        assert not result.errors

        # Verify rename was applied
        board = PCB.load(pcb)
        refs = {fp.reference for fp in board.footprints}
        assert "R1" in refs
        assert "R99" not in refs

    def test_no_auto_rename_skips_application(self, tmp_path):
        """auto_rename=False does not apply renames (leaves for caller to confirm)."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_RENAMED_REF)
        original_content = pcb.read_text()

        result = sync_netlist(sch, pcb, dry_run=False, auto_rename=False)

        # Renames are detected but not applied
        assert len(result.renamed) == 1
        # PCB file should be unchanged
        assert pcb.read_text() == original_content

    def test_dry_run_overrides_auto_rename(self, tmp_path):
        """dry_run=True prevents application even with auto_rename=True."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_RENAMED_REF)
        original_content = pcb.read_text()

        result = sync_netlist(sch, pcb, dry_run=True, auto_rename=True)

        assert len(result.renamed) == 1
        assert pcb.read_text() == original_content

    def test_dispatcher_passes_auto_rename(self, tmp_path):
        """_run_sync_netlist_command passes auto_rename to run_sync_netlist."""
        from kicad_tools.cli.commands.pcb import _run_sync_netlist_command

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(MINIMAL_PCB_MATCHING)

        class Args:
            schematic = str(sch)
            output = None
            dry_run = True
            format = "text"
            remove_orphans = False
            force = False
            auto_rename = True

        rc = _run_sync_netlist_command(Args(), pcb)
        assert rc == 0

    def test_run_sync_netlist_interactive_prompt_decline(self, tmp_path, monkeypatch):
        """Declining interactive prompt skips renames."""
        from kicad_tools.cli.pcb_sync_netlist import run_sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_RENAMED_REF)
        original_content = pcb.read_text()

        # Simulate user declining
        monkeypatch.setattr("builtins.input", lambda _: "n")

        rc = run_sync_netlist(sch, pcb, dry_run=False, auto_rename=False)

        assert rc == 0
        # PCB should be unchanged
        assert pcb.read_text() == original_content

    def test_run_sync_netlist_interactive_prompt_accept(self, tmp_path, monkeypatch):
        """Accepting interactive prompt applies renames."""
        from kicad_tools.cli.pcb_sync_netlist import run_sync_netlist
        from kicad_tools.schema.pcb import PCB

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_RENAMED_REF)

        # Simulate user accepting
        monkeypatch.setattr("builtins.input", lambda _: "y")

        rc = run_sync_netlist(sch, pcb, dry_run=False, auto_rename=False)

        assert rc == 0
        # Verify rename was applied
        board = PCB.load(pcb)
        refs = {fp.reference for fp in board.footprints}
        assert "R1" in refs
        assert "R99" not in refs

    def test_run_sync_netlist_eof_declines(self, tmp_path, monkeypatch):
        """EOFError on input (e.g., piped stdin) acts as decline."""
        from kicad_tools.cli.pcb_sync_netlist import run_sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_RENAMED_REF)
        original_content = pcb.read_text()

        def raise_eof(_):
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)

        rc = run_sync_netlist(sch, pcb, dry_run=False, auto_rename=False)

        assert rc == 0
        assert pcb.read_text() == original_content


class TestGetBoardEdgePosition:
    """Tests for _get_board_edge_position coordinate handling."""

    def test_nonzero_origin_no_double_subtraction(self):
        """Position must use board-relative outline directly.

        get_board_outline() returns board-relative coords, so
        _get_board_edge_position must NOT subtract the origin again.
        """
        from unittest.mock import MagicMock

        from kicad_tools.cli.pcb_sync_netlist import _get_board_edge_position

        mock_pcb = MagicMock()
        mock_pcb.board_origin = (100.0, 80.0)
        # Board-relative outline: rect (0,0)-(50,30)
        mock_pcb.get_board_outline.return_value = [
            (0, 0), (50, 0), (50, 30), (0, 30),
        ]

        x, y = _get_board_edge_position(mock_pcb)

        # 10mm to the right of max_x=50, at min_y=0
        assert x == pytest.approx(60.0)
        assert y == pytest.approx(0.0)

    def test_no_outline_returns_origin(self):
        """Without outline, falls back to (0, 0)."""
        from unittest.mock import MagicMock

        from kicad_tools.cli.pcb_sync_netlist import _get_board_edge_position

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []

        x, y = _get_board_edge_position(mock_pcb)

        assert x == pytest.approx(0.0)
        assert y == pytest.approx(0.0)
