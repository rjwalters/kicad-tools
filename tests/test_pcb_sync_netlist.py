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

        rc = _run_sync_netlist_command(Args(), pcb)
        assert rc == 1
