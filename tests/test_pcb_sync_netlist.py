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

        result = SyncResult(added=[SyncAction(action="add", reference="R1")])
        assert result.has_changes is True

    def test_has_changes_with_orphaned(self):
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult

        result = SyncResult(orphaned=[SyncAction(action="orphan", reference="D1")])
        assert result.has_changes is True

    def test_has_changes_with_renamed(self):
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult

        result = SyncResult(
            renamed=[SyncAction(action="rename", reference="R1", old_reference="R99")]
        )
        assert result.has_changes is True


class TestSyncNetlist:
    """Tests for the core sync_netlist function."""

    def test_in_sync_returns_no_footprint_changes(self, tmp_path):
        """Matching schematic and PCB produce no footprint-level changes."""
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
        sch.write_text("(kicad_sch (version 20231120) (generator test) (uuid 0) (paper A4))")
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
            added=[
                SyncAction(
                    action="add", reference="R1", footprint="Resistor_SMD:R_0402", value="10k"
                )
            ]
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_text(result, dry_run=True, pcb_path=pcb)

        assert "R1" in output
        assert "R_0402" in output

    def test_orphaned_appears_in_output(self, tmp_path):
        """Orphaned footprints appear in text output."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_text

        result = SyncResult(
            orphaned=[
                SyncAction(
                    action="orphan", reference="D1", footprint="LED_SMD:LED_0603", value="LED"
                )
            ]
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
            added=[
                SyncAction(
                    action="add", reference="R1", footprint="Resistor_SMD:R_0402", value="10k"
                )
            ]
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
            orphaned=[
                SyncAction(
                    action="orphan", reference="D1", footprint="LED_SMD:LED_0603", value="LED"
                )
            ]
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
        args = parser.parse_args(
            ["pcb", "sync-netlist", "--schematic", "test.kicad_sch", "test.kicad_pcb"]
        )
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
            [
                "pcb",
                "sync-netlist",
                "--schematic",
                "test.kicad_sch",
                "--format",
                "json",
                "test.kicad_pcb",
            ]
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

        result = sync_netlist(sch, pcb, dry_run=False, remove_orphans=True, force=True)

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
        sch.write_text("(kicad_sch (version 20231120) (generator test) (uuid 0) (paper A4))")
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
            removed=[
                SyncAction(
                    action="remove",
                    reference="D1",
                    footprint="LED_SMD:LED_0603",
                    value="LED",
                )
            ]
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_text(result, dry_run=False, pcb_path=pcb)

        assert "Removed" in output
        assert "D1" in output

    def test_removed_in_json_output(self, tmp_path):
        """Removed footprints appear in JSON output."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_json

        result = SyncResult(
            removed=[
                SyncAction(
                    action="remove",
                    reference="D1",
                    footprint="LED_SMD:LED_0603",
                    value="LED",
                )
            ]
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
        args = parser.parse_args(
            [
                "pcb",
                "sync-netlist",
                "--schematic",
                "test.kicad_sch",
                "--remove-orphans",
                "test.kicad_pcb",
            ]
        )
        assert args.remove_orphans is True

    def test_parser_force_flag(self):
        """Parser accepts --force flag for sync-netlist."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "sync-netlist",
                "--schematic",
                "test.kicad_sch",
                "--remove-orphans",
                "--force",
                "test.kicad_pcb",
            ]
        )
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
            orphaned=[
                SyncAction(action="orphan", reference="R50", footprint="R_0402", value="10k")
            ],
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
        args = parser.parse_args(
            [
                "pcb",
                "sync-netlist",
                "--schematic",
                "test.kicad_sch",
                "--auto-rename",
                "test.kicad_pcb",
            ]
        )
        assert args.auto_rename is True

    def test_parser_auto_rename_default_false(self):
        """--auto-rename defaults to False."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "sync-netlist",
                "--schematic",
                "test.kicad_sch",
                "test.kicad_pcb",
            ]
        )
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


# --- Hierarchical schematic fixtures ---

# Root schematic with only sheet references (no direct symbols)
HIERARCHICAL_ROOT = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "root-uuid")
  (paper "A4")
  (sheet
    (at 50 50)
    (size 30 20)
    (uuid "sheet1-uuid")
    (property "Sheetname" "Clock" (at 50 49 0) (effects (font (size 1.27 1.27))))
    (property "Sheetfile" "clock.kicad_sch" (at 50 72 0) (effects (font (size 1.27 1.27))))
  )
  (sheet
    (at 100 50)
    (size 30 20)
    (uuid "sheet2-uuid")
    (property "Sheetname" "MCU" (at 100 49 0) (effects (font (size 1.27 1.27))))
    (property "Sheetfile" "mcu.kicad_sch" (at 100 72 0) (effects (font (size 1.27 1.27))))
  )
)
"""

HIERARCHICAL_CLOCK_SHEET = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "clock-uuid")
  (paper "A4")
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "r1-uuid")
    (property "Reference" "R1" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402" (at 100 100 0) (effects (hide yes)))
  )
)
"""

HIERARCHICAL_MCU_SHEET = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "mcu-uuid")
  (paper "A4")
  (symbol
    (lib_id "Device:C")
    (at 120 100 0)
    (uuid "c1-uuid")
    (property "Reference" "C1" (at 120 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100n" (at 120 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402" (at 120 100 0) (effects (hide yes)))
  )
)
"""

# Schematic with a net tie (in_bom=False, on_board=True)
SCHEMATIC_WITH_NET_TIE = """(kicad_sch
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
    (lib_id "Device:NetTie_2")
    (at 140 100 0)
    (in_bom no)
    (on_board yes)
    (uuid "00000000-0000-0000-0000-000000000004")
    (property "Reference" "NT1" (at 140 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "NetTie_2" (at 140 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "NetTie:NetTie-2_SMD_Pad0.5mm" (at 140 100 0) (effects (hide yes)))
  )
)
"""

# PCB with R1 and NT1
PCB_WITH_R1_AND_NT1 = """(kicad_pcb
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
  (footprint "NetTie:NetTie-2_SMD_Pad0.5mm"
    (layer "F.Cu")
    (uuid "fp-nt1")
    (at 140 100)
    (property "Reference" "NT1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "NetTie_2" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.25 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.25 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""


class TestHierarchicalSync:
    """Tests for sync-netlist with hierarchical schematics."""

    def test_finds_components_in_sub_sheets(self, tmp_path):
        """Components in sub-sheets are found and not reported as orphaned."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        root = tmp_path / "root.kicad_sch"
        clock = tmp_path / "clock.kicad_sch"
        mcu = tmp_path / "mcu.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"

        root.write_text(HIERARCHICAL_ROOT)
        clock.write_text(HIERARCHICAL_CLOCK_SHEET)
        mcu.write_text(HIERARCHICAL_MCU_SHEET)
        pcb.write_text(MINIMAL_PCB_MATCHING)

        result = sync_netlist(root, pcb, dry_run=True)

        assert not result.orphaned, f"Unexpected orphans: {[o.reference for o in result.orphaned]}"
        assert not result.added
        assert not result.errors

    def test_root_only_symbols_flagged_as_orphaned(self, tmp_path):
        """Without sub-sheet traversal, all PCB footprints would be orphaned."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        # Use root schematic that has NO symbols and NO sheet references
        root = tmp_path / "empty.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        root.write_text("(kicad_sch (version 20231120) (generator test) (uuid 0) (paper A4))")
        pcb.write_text(MINIMAL_PCB_MATCHING)

        result = sync_netlist(root, pcb, dry_run=True)

        # Both R1 and C1 should be orphaned since empty schematic
        assert len(result.orphaned) == 2

    def test_detects_missing_sub_sheet_component(self, tmp_path):
        """Component in sub-sheet but missing from PCB is detected."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        root = tmp_path / "root.kicad_sch"
        clock = tmp_path / "clock.kicad_sch"
        mcu = tmp_path / "mcu.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"

        root.write_text(HIERARCHICAL_ROOT)
        clock.write_text(HIERARCHICAL_CLOCK_SHEET)
        mcu.write_text(HIERARCHICAL_MCU_SHEET)
        # PCB only has C1, missing R1 from clock sub-sheet
        pcb.write_text(PCB_MISSING_R1)

        result = sync_netlist(root, pcb, dry_run=True)

        assert len(result.added) == 1
        assert result.added[0].reference == "R1"
        assert not result.orphaned


class TestNetTieSync:
    """Tests for net tie components (in_bom=False, on_board=True)."""

    def test_net_tie_not_orphaned(self, tmp_path):
        """Net tie with in_bom=False on_board=True should not be orphaned."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_WITH_NET_TIE)
        pcb.write_text(PCB_WITH_R1_AND_NT1)

        result = sync_netlist(sch, pcb, dry_run=True)

        assert not result.orphaned, f"Unexpected orphans: {[o.reference for o in result.orphaned]}"
        assert not result.added
        assert not result.errors

    def test_net_tie_missing_from_pcb_is_added(self, tmp_path):
        """Net tie in schematic but missing from PCB is detected as needing addition."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_WITH_NET_TIE)
        # PCB only has R1, no NT1
        pcb.write_text("""(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)""")

        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.added) == 1
        assert result.added[0].reference == "NT1"
        assert not result.orphaned


class TestGetBoardEdgePositionCoordinates:
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
            (0, 0),
            (50, 0),
            (50, 30),
            (0, 30),
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


# ---------------------------------------------------------------------------
# Pin-count mismatch detection tests
# ---------------------------------------------------------------------------

# Schematic with a 5-pin connector (Conn_01x05)
SCHEMATIC_CONN_5PIN = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (symbol
    (lib_id "Connector:Conn_01x05_Pin")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "J1" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "Conn_01x05" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical" (at 100 100 0) (effects (hide yes)))
  )
)
"""

# PCB with a 4-pad connector footprint (mismatched with 5-pin schematic)
PCB_CONN_4PAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"
    (layer "F.Cu")
    (uuid "fp-j1")
    (at 100 100)
    (property "Reference" "J1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "Conn_01x04" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" thru_hole oval (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu") (net 0 ""))
    (pad "2" thru_hole oval (at 0 2.54) (size 1.7 1.7) (drill 1) (layers "*.Cu") (net 0 ""))
    (pad "3" thru_hole oval (at 0 5.08) (size 1.7 1.7) (drill 1) (layers "*.Cu") (net 0 ""))
    (pad "4" thru_hole oval (at 0 7.62) (size 1.7 1.7) (drill 1) (layers "*.Cu") (net 0 ""))
  )
)
"""

# PCB with a 5-pad connector footprint (matches 5-pin schematic)
PCB_CONN_5PAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical"
    (layer "F.Cu")
    (uuid "fp-j1")
    (at 100 100)
    (property "Reference" "J1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "Conn_01x05" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" thru_hole oval (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu") (net 0 ""))
    (pad "2" thru_hole oval (at 0 2.54) (size 1.7 1.7) (drill 1) (layers "*.Cu") (net 0 ""))
    (pad "3" thru_hole oval (at 0 5.08) (size 1.7 1.7) (drill 1) (layers "*.Cu") (net 0 ""))
    (pad "4" thru_hole oval (at 0 7.62) (size 1.7 1.7) (drill 1) (layers "*.Cu") (net 0 ""))
    (pad "5" thru_hole oval (at 0 10.16) (size 1.7 1.7) (drill 1) (layers "*.Cu") (net 0 ""))
  )
)
"""

# Schematic with an 8-pin SOIC IC
SCHEMATIC_IC_8PIN = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (symbol
    (lib_id "Amplifier_Operational:LM358")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000020")
    (property "Reference" "U1" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "LM358" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm" (at 100 100 0) (effects (hide yes)))
  )
)
"""

# PCB with 9-pad SOIC (8 signal + 1 thermal/exposed pad)
PCB_IC_9PAD_THERMAL = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (footprint "Package_SO:SOIC-8-1EP_3.9x4.9mm_P1.27mm"
    (layer "F.Cu")
    (uuid "fp-u1")
    (at 100 100)
    (property "Reference" "U1" (at 0 -3 0) (layer "F.SilkS"))
    (property "Value" "LM358" (at 0 3 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -2.5 -1.905) (size 1.5 0.6) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at -2.5 -0.635) (size 1.5 0.6) (layers "F.Cu") (net 0 ""))
    (pad "3" smd roundrect (at -2.5 0.635) (size 1.5 0.6) (layers "F.Cu") (net 0 ""))
    (pad "4" smd roundrect (at -2.5 1.905) (size 1.5 0.6) (layers "F.Cu") (net 0 ""))
    (pad "5" smd roundrect (at 2.5 1.905) (size 1.5 0.6) (layers "F.Cu") (net 0 ""))
    (pad "6" smd roundrect (at 2.5 0.635) (size 1.5 0.6) (layers "F.Cu") (net 0 ""))
    (pad "7" smd roundrect (at 2.5 -0.635) (size 1.5 0.6) (layers "F.Cu") (net 0 ""))
    (pad "8" smd roundrect (at 2.5 -1.905) (size 1.5 0.6) (layers "F.Cu") (net 0 ""))
    (pad "9" smd roundrect (at 0 0) (size 2.5 2.5) (layers "F.Cu") (net 0 ""))
  )
)
"""

# Schematic with R1 (2-pin passive) for no-false-positive check
SCHEMATIC_PASSIVE_2PIN = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000030")
    (property "Reference" "R1" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
  )
)
"""

# PCB with R1 matching 2-pad resistor
PCB_PASSIVE_2PAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""


# --- PCB with stale net assignments for net update tests ---

# R1 pad 1 is assigned to "old_net" but schematic says GND.
# R1 pad 2 has no net (empty) but schematic generates Net-(R1-2).
PCB_STALE_NETS = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "old_net")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "old_net"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""


class TestPinMismatch:
    """Tests for PinMismatch dataclass."""

    def test_creation(self):
        from kicad_tools.cli.pcb_sync_netlist import PinMismatch

        pm = PinMismatch(
            reference="J1",
            schematic_footprint="Connector:PinHeader_1x05",
            pcb_footprint="Connector:PinHeader_1x04",
            schematic_pins=5,
            pcb_pads=4,
        )
        assert pm.reference == "J1"
        assert pm.schematic_pins == 5
        assert pm.pcb_pads == 4
        assert pm.severity == "warning"

    def test_delta_property(self):
        from kicad_tools.cli.pcb_sync_netlist import PinMismatch

        pm = PinMismatch(
            reference="U1",
            schematic_footprint="Package_SO:SOIC-8",
            pcb_footprint="Package_SO:SOIC-8-1EP",
            schematic_pins=8,
            pcb_pads=9,
            severity="info",
        )
        assert pm.delta == 1

    def test_negative_delta(self):
        from kicad_tools.cli.pcb_sync_netlist import PinMismatch

        pm = PinMismatch(
            reference="J1",
            schematic_footprint="Connector:PinHeader_1x05",
            pcb_footprint="Connector:PinHeader_1x04",
            schematic_pins=5,
            pcb_pads=4,
        )
        assert pm.delta == -1

    def test_info_severity(self):
        from kicad_tools.cli.pcb_sync_netlist import PinMismatch

        pm = PinMismatch(
            reference="U1",
            schematic_footprint="SOIC-8",
            pcb_footprint="SOIC-8-1EP",
            schematic_pins=8,
            pcb_pads=9,
            severity="info",
        )
        assert pm.severity == "info"


class TestSyncResultPinMismatches:
    """Tests for SyncResult.has_changes with pin_mismatches."""

    def test_has_changes_with_pin_mismatches(self):
        from kicad_tools.cli.pcb_sync_netlist import PinMismatch, SyncResult

        result = SyncResult(
            pin_mismatches=[
                PinMismatch(
                    reference="J1",
                    schematic_footprint="PinHeader_1x05",
                    pcb_footprint="PinHeader_1x04",
                    schematic_pins=5,
                    pcb_pads=4,
                )
            ]
        )
        assert result.has_changes is True

    def test_no_changes_without_pin_mismatches(self):
        from kicad_tools.cli.pcb_sync_netlist import SyncResult

        result = SyncResult()
        assert result.has_changes is False


class TestPinMismatchDetection:
    """Tests for pin-count mismatch detection in sync_netlist."""

    def test_detects_pin_pad_mismatch(self, tmp_path):
        """5-pin schematic connector with 4-pad PCB footprint is detected."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_CONN_5PIN)
        pcb.write_text(PCB_CONN_4PAD)

        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.pin_mismatches) == 1
        pm = result.pin_mismatches[0]
        assert pm.reference == "J1"
        assert pm.schematic_pins == 5
        assert pm.pcb_pads == 4
        assert pm.severity == "warning"

    def test_no_mismatch_when_counts_match(self, tmp_path):
        """Same footprint on both sides produces no pin mismatch."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_CONN_5PIN)
        pcb.write_text(PCB_CONN_5PAD)

        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.pin_mismatches) == 0

    def test_thermal_pad_surplus_is_info(self, tmp_path):
        """IC with +1 thermal pad gets info severity, not warning."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_IC_8PIN)
        pcb.write_text(PCB_IC_9PAD_THERMAL)

        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.pin_mismatches) == 1
        pm = result.pin_mismatches[0]
        assert pm.reference == "U1"
        assert pm.schematic_pins == 8
        assert pm.pcb_pads == 9
        assert pm.severity == "info"

    def test_same_footprint_no_false_positive(self, tmp_path):
        """Passive with identical footprint on both sides has no mismatch."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_PASSIVE_2PIN)
        pcb.write_text(PCB_PASSIVE_2PAD)

        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.pin_mismatches) == 0
        # No footprint-level changes (net_updated may be non-empty because
        # the unconditional net assignment pass assigns nets to bare pads).
        assert not result.added
        assert not result.renamed
        assert not result.orphaned
        assert not result.removed

    def test_dry_run_does_not_modify_pcb(self, tmp_path):
        """Dry run with mismatches does not modify the PCB file."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_CONN_5PIN)
        pcb.write_text(PCB_CONN_4PAD)

        original_content = pcb.read_text()
        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.pin_mismatches) == 1
        assert pcb.read_text() == original_content


class TestPinMismatchFormatText:
    """Tests for pin mismatch output in format_text."""

    def test_format_text_includes_pin_mismatches(self):
        from kicad_tools.cli.pcb_sync_netlist import PinMismatch, SyncResult, format_text

        result = SyncResult(
            pin_mismatches=[
                PinMismatch(
                    reference="J1",
                    schematic_footprint="Connector:PinHeader_1x05",
                    pcb_footprint="Connector:PinHeader_1x04",
                    schematic_pins=5,
                    pcb_pads=4,
                    severity="warning",
                )
            ]
        )
        text = format_text(result, dry_run=True, pcb_path=Path("test.kicad_pcb"))

        assert "Pin-count mismatches (1):" in text
        assert "[warning] J1:" in text
        assert "schematic expects 5 pins" in text
        assert "PCB has 4 pads" in text

    def test_format_text_info_severity(self):
        from kicad_tools.cli.pcb_sync_netlist import PinMismatch, SyncResult, format_text

        result = SyncResult(
            pin_mismatches=[
                PinMismatch(
                    reference="U1",
                    schematic_footprint="Package_SO:SOIC-8",
                    pcb_footprint="Package_SO:SOIC-8-1EP",
                    schematic_pins=8,
                    pcb_pads=9,
                    severity="info",
                )
            ]
        )
        text = format_text(result, dry_run=True, pcb_path=Path("test.kicad_pcb"))

        assert "[info] U1:" in text
        assert "schematic expects 8 pins" in text
        assert "PCB has 9 pads" in text


class TestPinMismatchFormatJson:
    """Tests for pin mismatch output in format_json."""

    def test_format_json_includes_pin_mismatches(self):
        from kicad_tools.cli.pcb_sync_netlist import PinMismatch, SyncResult, format_json

        result = SyncResult(
            pin_mismatches=[
                PinMismatch(
                    reference="J1",
                    schematic_footprint="Connector:PinHeader_1x05",
                    pcb_footprint="Connector:PinHeader_1x04",
                    schematic_pins=5,
                    pcb_pads=4,
                    severity="warning",
                )
            ]
        )
        output = json.loads(format_json(result, dry_run=True, pcb_path=Path("test.kicad_pcb")))

        assert len(output["pin_mismatches"]) == 1
        pm = output["pin_mismatches"][0]
        assert pm["reference"] == "J1"
        assert pm["schematic_footprint"] == "Connector:PinHeader_1x05"
        assert pm["pcb_footprint"] == "Connector:PinHeader_1x04"
        assert pm["schematic_pins"] == 5
        assert pm["pcb_pads"] == 4
        assert pm["severity"] == "warning"

    def test_format_json_empty_pin_mismatches(self):
        from kicad_tools.cli.pcb_sync_netlist import SyncResult, format_json

        result = SyncResult()
        output = json.loads(format_json(result, dry_run=True, pcb_path=Path("test.kicad_pcb")))

        assert output["pin_mismatches"] == []


# PCB with an orphan net that is not assigned to any pad
PCB_WITH_ORPHAN_NET = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "stale_net")
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


class TestNetUpdates:
    """Tests for unconditional pad net assignment updates."""

    def test_stale_nets_detected_in_dry_run(self, tmp_path):
        """Dry run detects stale pad-net assignments."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_R1_ONLY)
        pcb.write_text(PCB_STALE_NETS)

        result = sync_netlist(sch, pcb, dry_run=True)

        # R1 pad 1 was "old_net", should change
        assert len(result.net_updated) > 0
        details = [a.detail for a in result.net_updated]
        assert any("R1" in d and "old_net" in d for d in details)

    def test_stale_nets_corrected_on_apply(self, tmp_path):
        """Non-dry-run corrects stale pad-net assignments in the PCB."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist
        from kicad_tools.schema.pcb import PCB

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_R1_ONLY)
        pcb.write_text(PCB_STALE_NETS)

        result = sync_netlist(sch, pcb, dry_run=False, remove_orphans=True)

        assert len(result.net_updated) > 0
        assert not result.errors

        # Reload and verify R1 pad 1 no longer has "old_net"
        board = PCB.load(pcb)
        r1 = board.get_footprint("R1")
        assert r1 is not None
        pad1_nets = [p.net_name for p in r1.pads if p.number == "1"]
        assert pad1_nets
        assert pad1_nets[0] != "old_net"

    def test_net_update_runs_even_without_footprint_changes(self, tmp_path):
        """Net assignment runs even when no footprints are added/renamed/removed."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_STALE_NETS)

        result = sync_netlist(sch, pcb, dry_run=True)

        # No footprint-level changes (both R1 and C1 exist)
        assert not result.added
        assert not result.renamed
        # But net updates should be detected
        assert len(result.net_updated) > 0

    def test_correct_nets_produce_no_net_updates(self, tmp_path):
        """PCB already matching schematic nets produces empty net_updated."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(SCHEMATIC_R1_ONLY)

        # First, sync to correct the nets
        pcb.write_text(PCB_STALE_NETS)
        sync_netlist(sch, pcb, dry_run=False, remove_orphans=True)

        # Now sync again -- should produce no net updates
        result = sync_netlist(sch, pcb, dry_run=True)
        # Filter net_updated to only R1 (C1 was removed as orphan)
        r1_updates = [a for a in result.net_updated if a.reference == "R1"]
        assert not r1_updates

    def test_dry_run_does_not_modify_pcb_with_net_updates(self, tmp_path):
        """Dry run with net updates does not change the PCB file."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_STALE_NETS)
        original = pcb.read_text()

        result = sync_netlist(sch, pcb, dry_run=True)

        assert len(result.net_updated) > 0
        assert pcb.read_text() == original

    def test_net_updated_in_text_output(self, tmp_path):
        """Net updates appear in text format output."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_text

        result = SyncResult(
            net_updated=[
                SyncAction(
                    action="net_updated",
                    reference="R1",
                    detail='R1.1: "old_net" -> "GND"',
                )
            ]
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_text(result, dry_run=True, pcb_path=pcb)

        assert "Net updates" in output
        assert "R1.1" in output
        assert "old_net" in output
        assert "GND" in output

    def test_net_updated_in_json_output(self, tmp_path):
        """Net updates appear in JSON format output."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult, format_json

        result = SyncResult(
            net_updated=[
                SyncAction(
                    action="net_updated",
                    reference="R1",
                    detail='R1.1: "old_net" -> "GND"',
                )
            ]
        )
        pcb = tmp_path / "test.kicad_pcb"
        output = format_json(result, dry_run=False, pcb_path=pcb)

        data = json.loads(output)
        assert "net_updated" in data
        assert len(data["net_updated"]) == 1
        assert data["net_updated"][0]["reference"] == "R1"
        assert "old_net" in data["net_updated"][0]["detail"]

    def test_has_changes_with_net_updated(self):
        """SyncResult.has_changes is True when only net_updated has entries."""
        from kicad_tools.cli.pcb_sync_netlist import SyncAction, SyncResult

        result = SyncResult(net_updated=[SyncAction(action="net_updated", reference="R1")])
        assert result.has_changes is True


class TestNetAssignmentErrorSurfacing:
    """Tests for error surfacing from _assign_nets_from_schematic."""

    def test_export_netlist_error_surfaced(self, tmp_path, monkeypatch):
        """Failure in export_netlist produces an error in result."""
        from kicad_tools.cli.pcb_sync_netlist import _assign_nets_from_schematic
        from kicad_tools.schema.pcb import PCB

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(MINIMAL_PCB_MATCHING)
        board = PCB.load(pcb_path)

        # Mock export_netlist to raise

        (__builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__)

        def mock_export_netlist(path):
            raise RuntimeError("kicad-cli not found")

        monkeypatch.setattr(
            "kicad_tools.operations.netlist.export_netlist",
            mock_export_netlist,
        )

        actions, errors = _assign_nets_from_schematic(board, tmp_path / "nonexistent.kicad_sch")

        assert len(errors) > 0
        assert "Failed to export netlist" in errors[0]
        assert not actions


class TestRemoveOrphanNets:
    """Tests for --remove-orphan-nets functionality."""

    def test_parser_accepts_remove_orphan_nets(self):
        """Parser supports --remove-orphan-nets flag."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "sync-netlist",
                "--schematic",
                "test.kicad_sch",
                "--remove-orphan-nets",
                "test.kicad_pcb",
            ]
        )
        assert args.remove_orphan_nets is True

    def test_parser_remove_orphan_nets_default_false(self):
        """--remove-orphan-nets defaults to False."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "sync-netlist",
                "--schematic",
                "test.kicad_sch",
                "test.kicad_pcb",
            ]
        )
        assert args.remove_orphan_nets is False

    def test_orphan_net_removed(self, tmp_path):
        """Net with no pad references is removed when flag is set."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist
        from kicad_tools.schema.pcb import PCB

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_ORPHAN_NET)

        result = sync_netlist(sch, pcb, dry_run=False, remove_orphan_nets=True)

        # stale_net had no pad references and should be removed
        board = PCB.load(pcb)
        assert board.get_net_by_name("stale_net") is None
        # Warning should mention removal
        assert any("stale_net" in w for w in result.warnings)

    def test_orphan_net_not_removed_without_flag(self, tmp_path):
        """Net with no pad references is preserved when flag is not set."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist
        from kicad_tools.schema.pcb import PCB

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb.write_text(PCB_WITH_ORPHAN_NET)

        sync_netlist(sch, pcb, dry_run=False, remove_orphan_nets=False)

        board = PCB.load(pcb)
        assert board.get_net_by_name("stale_net") is not None

    def test_dispatcher_passes_remove_orphan_nets(self, tmp_path):
        """Dispatcher passes remove_orphan_nets to run_sync_netlist."""
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
            auto_rename = False
            remove_orphan_nets = False

        rc = _run_sync_netlist_command(Args(), pcb)
        assert rc == 0


class TestNormalizeKicadCliNetNames:
    """Tests for the ``/``-prefix normalisation of kicad-cli net names.

    Issue #3370: ``kicad-cli sch export netlist`` writes hierarchical
    local labels with a leading sheet-path slash (``/BST_A``) while
    power-symbol globals (``+3V3``, ``GND``) come through unprefixed.
    PCB net tables conventionally store bare names; mixing the two
    conventions causes the ``/``-prefixed adds to land in the PCB as
    ghost nets that get dropped by orphan cleanup, leaving the intended
    pads on stale net values.
    """

    def _make_net(self, code: int, name: str):
        """Construct a minimal NetlistNet with the given code and name."""
        from kicad_tools.operations.netlist import NetlistNet

        return NetlistNet(code=code, name=name, nodes=[])

    def _make_netlist(self, nets):
        """Construct a minimal Netlist with the given nets."""
        from kicad_tools.operations.netlist import Netlist

        return Netlist(
            source_file="test.kicad_sch",
            tool="Eeschema 10.0.1",
            components=[],
            nets=list(nets),
        )

    def test_single_slash_stripped(self):
        """A single leading ``/`` is stripped from hierarchical labels."""
        from kicad_tools.cli.pcb_sync_netlist import _normalize_kicad_cli_net_names

        netlist = self._make_netlist(
            [
                self._make_net(1, "/BST_A"),
                self._make_net(2, "/DRV_CP1"),
                self._make_net(3, "/SPI_MISO"),
            ]
        )
        _normalize_kicad_cli_net_names(netlist)
        names = [n.name for n in netlist.nets]
        assert names == ["BST_A", "DRV_CP1", "SPI_MISO"]

    def test_power_symbol_names_unchanged(self):
        """Power-symbol names (``+3V3``, ``GND``, ``+5V``) are untouched."""
        from kicad_tools.cli.pcb_sync_netlist import _normalize_kicad_cli_net_names

        netlist = self._make_netlist(
            [
                self._make_net(1, "+3V3"),
                self._make_net(2, "GND"),
                self._make_net(3, "+5V"),
                self._make_net(4, "+3.3V"),
                self._make_net(5, "-12V"),
            ]
        )
        _normalize_kicad_cli_net_names(netlist)
        names = [n.name for n in netlist.nets]
        assert names == ["+3V3", "GND", "+5V", "+3.3V", "-12V"]

    def test_bare_names_unchanged(self):
        """Already-bare names from the Python fallback are untouched."""
        from kicad_tools.cli.pcb_sync_netlist import _normalize_kicad_cli_net_names

        netlist = self._make_netlist(
            [
                self._make_net(1, "BST_A"),
                self._make_net(2, "PHASE_A"),
                self._make_net(3, "Net-(R1-Pad2)"),
            ]
        )
        _normalize_kicad_cli_net_names(netlist)
        names = [n.name for n in netlist.nets]
        assert names == ["BST_A", "PHASE_A", "Net-(R1-Pad2)"]

    def test_empty_name_unchanged(self):
        """The empty net (code 0) has no name; normalisation is a no-op."""
        from kicad_tools.cli.pcb_sync_netlist import _normalize_kicad_cli_net_names

        netlist = self._make_netlist([self._make_net(0, "")])
        _normalize_kicad_cli_net_names(netlist)
        assert netlist.nets[0].name == ""

    def test_only_leading_slash_stripped(self):
        """Only the first ``/`` is stripped; deeper paths keep their tail."""
        from kicad_tools.cli.pcb_sync_netlist import _normalize_kicad_cli_net_names

        # Defensive: a sub-sheet hierarchical path comes through as
        # /sheetA/SIGNAL.  We strip ONE leading slash so the surviving
        # form (sheetA/SIGNAL) is a unique-but-bare name that will
        # round-trip to the same canonical key on every sync invocation.
        netlist = self._make_netlist([self._make_net(1, "/sheetA/SIGNAL")])
        _normalize_kicad_cli_net_names(netlist)
        assert netlist.nets[0].name == "sheetA/SIGNAL"

    def test_mixed_names_partial_normalisation(self):
        """Mixed power+hierarchical netlist: only ``/``-prefixed names change."""
        from kicad_tools.cli.pcb_sync_netlist import _normalize_kicad_cli_net_names

        netlist = self._make_netlist(
            [
                self._make_net(1, "/BST_A"),
                self._make_net(2, "+3V3"),
                self._make_net(3, "/DRV_CP1"),
                self._make_net(4, "GND"),
                self._make_net(5, "PWM_AH"),
            ]
        )
        _normalize_kicad_cli_net_names(netlist)
        names = [n.name for n in netlist.nets]
        assert names == ["BST_A", "+3V3", "DRV_CP1", "GND", "PWM_AH"]

    def test_normalisation_is_idempotent(self):
        """Running the normaliser twice produces the same result."""
        from kicad_tools.cli.pcb_sync_netlist import _normalize_kicad_cli_net_names

        netlist = self._make_netlist(
            [
                self._make_net(1, "/BST_A"),
                self._make_net(2, "+3V3"),
            ]
        )
        _normalize_kicad_cli_net_names(netlist)
        first = [n.name for n in netlist.nets]
        _normalize_kicad_cli_net_names(netlist)
        second = [n.name for n in netlist.nets]
        assert first == second == ["BST_A", "+3V3"]


# ---------------------------------------------------------------------------
# Board 05 round-trip / drift regression (issue #3370)
#
# The fixture pair is FROZEN from git commit 9f11c3ab (PR #3377, the
# commit that introduced this regression test) so that ongoing board-05
# rework cannot invalidate the pinned drift scenario again (issue #3521):
#
#   git show 9f11c3ab:boards/05-bldc-motor-controller/output/bldc_controller.kicad_sch
#   git show 9f11c3ab:boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb
#
# The PCB copy has zone ``filled_polygon`` blocks stripped (~390 KB of
# fill geometry irrelevant to netlist sync); footprints, pads, nets,
# segments, vias, and zone definitions are verbatim.  In this frozen
# state the PCB carries exactly 40 named nets, none of the 14 drift
# nets, and U3.1/.3/.5/.15/.17/.29/.30 are stuck on stale rails
# (GND / +5V / +3.3V / PWM_AH / VMOTOR / ISENSE_B+).
# ---------------------------------------------------------------------------

_BOARD_05_DIR = Path("tests/fixtures/board05_sync_drift")
_BOARD_05_SCH = _BOARD_05_DIR / "bldc_controller.kicad_sch"
_BOARD_05_PCB = _BOARD_05_DIR / "bldc_controller_drifted.kicad_pcb"

# The 14 hierarchical-local nets that were dropped before the
# ``/``-prefix fix.  ``+3.3V`` is intentionally excluded -- it is a
# power-symbol alias handled by ``canonicalize_power_nets``.
_BOARD_05_DRIFT_NETS = frozenset(
    {
        "BST_A",
        "BST_B",
        "BST_C",
        "DRV_CP1",
        "DRV_CP2",
        "DRV_FAULTn",
        "DRV_LOCKn",
        "DRV_VCP",
        "DRV_VINT",
        "DRV_VREG",
        "DRV_VSW",
        "FGFB",
        "FGOUT",
        "SPI_MISO",
    }
)

# After the fix, the PCB must carry at least this many named nets
# (40 pre-fix + 13 recovered drift nets = 53; ``+3.3V`` already
# present pre-fix as an alias).
_BOARD_05_MIN_NET_COUNT = 53


class TestBoard05SyncDriftRegression:
    """Round-trip + invariants for board 05 schematic-PCB drift (#3370).

    These tests load the frozen board-05 fixture pair (schematic +
    drifted routed PCB, see module comment above for provenance),
    apply :func:`sync_netlist` to a copy of the PCB, and assert:

      1. All 14 drift nets land in the synced PCB.
      2. The synced PCB exposes >= 53 named nets (was 40 pre-fix).
      3. No ``/``-prefixed net names appear in the synced PCB.
      4. U3 pad assignments match the schematic's intended nets.
    """

    @pytest.fixture
    def synced_pcb(self, tmp_path):
        """Run sync_netlist against a copy of the board-05 PCB."""
        import shutil

        from kicad_tools.cli.pcb_sync_netlist import sync_netlist
        from kicad_tools.schema.pcb import PCB

        dst_pcb = tmp_path / "bldc_controller_synced.kicad_pcb"
        shutil.copy(_BOARD_05_PCB, dst_pcb)

        result = sync_netlist(
            schematic_path=_BOARD_05_SCH,
            pcb_path=dst_pcb,
            dry_run=False,
            remove_orphan_nets=True,
        )
        assert not result.errors, f"sync_netlist errors: {result.errors}"
        return PCB.load(dst_pcb), result

    def test_drift_nets_present(self, synced_pcb):
        """All 14 drift nets land in the synced PCB."""
        pcb, _ = synced_pcb
        named = {n.name for n in pcb.nets.values() if n.name}
        missing = _BOARD_05_DRIFT_NETS - named
        assert not missing, f"Drift nets still missing after sync: {sorted(missing)}"

    def test_net_count_above_floor(self, synced_pcb):
        """The synced PCB carries >= 53 named nets (was 40 pre-fix)."""
        pcb, _ = synced_pcb
        named = {n.name for n in pcb.nets.values() if n.name}
        assert len(named) >= _BOARD_05_MIN_NET_COUNT, (
            f"Synced PCB has only {len(named)} named nets, expected "
            f">= {_BOARD_05_MIN_NET_COUNT}.  Drift recovery regressed."
        )

    def test_no_slash_prefixed_names(self, synced_pcb):
        """No ``/``-prefixed net names appear in the synced PCB."""
        pcb, _ = synced_pcb
        named = {n.name for n in pcb.nets.values() if n.name}
        prefixed = {n for n in named if n.startswith("/")}
        assert not prefixed, f"Found /-prefixed net names in synced PCB: {sorted(prefixed)}"

    def test_u3_drift_pads_assigned_correctly(self, synced_pcb):
        """U3 pads land on the schematic-intended drift nets, not stale rails.

        Pre-fix, U3.1 / U3.3 / U3.5 / U3.15 / U3.17 / U3.29 / U3.30
        were stuck on ``GND`` / ``+5V`` / ``+3.3V`` / ``PWM_AH`` from
        the original bottom-up footprint generator's DRV8301_PINS
        table.  After the fix, sync rewrites them to the schematic's
        BST_A / BST_B / BST_C / SPI_MISO / DRV_FAULTn / DRV_CP2 /
        DRV_CP1 assignments.
        """
        pcb, _ = synced_pcb
        u3 = pcb.get_footprint("U3")
        assert u3 is not None, "U3 footprint not found"
        pad_nets = {p.number: p.net_name for p in u3.pads}
        expected = {
            "1": "BST_A",
            "3": "BST_B",
            "5": "BST_C",
            "15": "SPI_MISO",
            "17": "DRV_FAULTn",
            "29": "DRV_CP2",
            "30": "DRV_CP1",
        }
        for pad_num, want_net in expected.items():
            got = pad_nets.get(pad_num)
            assert got == want_net, f"U3.{pad_num}: expected net {want_net!r}, got {got!r}"


# ---------------------------------------------------------------------------
# gr_poly / gr_curve Edge.Cuts outline handling (issue #4098, Bug A)
# ---------------------------------------------------------------------------

# Board whose Edge.Cuts outline is a single gr_poly (chamfered rectangle),
# offset far from the sheet origin (x in [116.5, 181.5], y in [100, 175]) —
# mirrors the reporter's 65x75mm board that landed new parts off-board.
PCB_GR_POLY_OUTLINE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (gr_poly
    (pts
      (xy 116.5 105) (xy 176.5 100) (xy 181.5 105)
      (xy 181.5 175) (xy 116.5 175)
    )
    (stroke (width 0.1) (type default))
    (layer "Edge.Cuts")
    (uuid "poly-outline-1")
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 150 140)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""

# Board with no Edge.Cuts geometry at all (edge case for the loud fallback).
PCB_NO_OUTLINE = """(kicad_pcb
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


class TestGetBoardOutlineGrPoly:
    """PCB.get_board_outline() must handle gr_poly / gr_curve outlines (#4098)."""

    def test_gr_poly_outline_returns_nonempty_polygon(self, tmp_path):
        """A gr_poly Edge.Cuts outline yields a non-empty polygon (not [])."""
        from kicad_tools.schema.pcb import PCB

        pcb_path = tmp_path / "poly.kicad_pcb"
        pcb_path.write_text(PCB_GR_POLY_OUTLINE)
        pcb = PCB.load(pcb_path)

        outline = pcb.get_board_outline()

        assert outline, "gr_poly outline should not return an empty list"
        xs = [pt[0] for pt in outline]
        ys = [pt[1] for pt in outline]
        assert min(xs) == pytest.approx(116.5)
        assert max(xs) == pytest.approx(181.5)
        assert min(ys) == pytest.approx(100.0)
        assert max(ys) == pytest.approx(175.0)

    def test_gr_curve_outline_returns_nonempty_polygon(self, tmp_path):
        """A gr_curve Edge.Cuts outline also contributes its vertex chain."""
        from kicad_tools.schema.pcb import PCB

        pcb_text = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "")
  (gr_curve
    (pts (xy 10 10) (xy 40 5) (xy 70 10) (xy 70 60) (xy 10 60))
    (stroke (width 0.1) (type default))
    (layer "Edge.Cuts")
    (uuid "curve-1")
  )
)
"""
        pcb_path = tmp_path / "curve.kicad_pcb"
        pcb_path.write_text(pcb_text)
        pcb = PCB.load(pcb_path)

        outline = pcb.get_board_outline()

        assert outline, "gr_curve outline should not return an empty list"
        xs = [pt[0] for pt in outline]
        assert min(xs) == pytest.approx(10.0)
        assert max(xs) == pytest.approx(70.0)

    def test_get_board_edge_position_uses_gr_poly_outline(self, tmp_path):
        """Staging position is derived from the gr_poly outline, not (0, 0)."""
        from kicad_tools.cli.pcb_sync_netlist import _get_board_edge_position
        from kicad_tools.schema.pcb import PCB

        pcb_path = tmp_path / "poly.kicad_pcb"
        pcb_path.write_text(PCB_GR_POLY_OUTLINE)
        pcb = PCB.load(pcb_path)

        x, y = _get_board_edge_position(pcb)

        # Staged 10mm to the right of the outline's max_x (181.5), not at origin.
        assert x == pytest.approx(191.5)
        assert (x, y) != (0.0, 0.0)


class TestOutlineBboxHelpers:
    """Tests for _outline_bbox / _is_outside_bbox (issue #4098)."""

    def test_outline_bbox_from_gr_poly(self, tmp_path):
        """_outline_bbox returns the correct board-relative bbox for gr_poly."""
        from kicad_tools.cli.pcb_sync_netlist import _outline_bbox
        from kicad_tools.schema.pcb import PCB

        pcb_path = tmp_path / "poly.kicad_pcb"
        pcb_path.write_text(PCB_GR_POLY_OUTLINE)
        pcb = PCB.load(pcb_path)

        bbox = _outline_bbox(pcb)

        assert bbox is not None
        min_x, min_y, max_x, max_y = bbox
        assert (min_x, min_y, max_x, max_y) == pytest.approx((116.5, 100.0, 181.5, 175.0))

    def test_outline_bbox_none_when_no_outline(self, tmp_path):
        """_outline_bbox returns None when no Edge.Cuts geometry exists."""
        from kicad_tools.cli.pcb_sync_netlist import _outline_bbox
        from kicad_tools.schema.pcb import PCB

        pcb_path = tmp_path / "none.kicad_pcb"
        pcb_path.write_text(PCB_NO_OUTLINE)
        pcb = PCB.load(pcb_path)

        assert _outline_bbox(pcb) is None

    def test_is_outside_bbox_flags_far_point(self):
        """A point well beyond the padded bbox is flagged as outside."""
        from kicad_tools.cli.pcb_sync_netlist import _is_outside_bbox

        bbox = (116.5, 100.0, 181.5, 175.0)
        # 100mm past max_x — clearly off-board.
        assert _is_outside_bbox(281.5, 137.5, bbox) is True

    def test_is_outside_bbox_exempts_staging_offset(self):
        """The deliberate ~10mm staging offset is within the padded bbox."""
        from kicad_tools.cli.pcb_sync_netlist import _is_outside_bbox

        bbox = (116.5, 100.0, 181.5, 175.0)
        # max_x + 10 (staging) plus a couple of 5mm spacings — should NOT warn.
        assert _is_outside_bbox(191.5, 100.0, bbox) is False
        assert _is_outside_bbox(201.5, 100.0, bbox) is False


class TestSyncOffBoardWarnings:
    """sync_netlist surfaces off-board / no-outline warnings (issue #4098)."""

    def test_no_outline_fallback_emits_warning(self, tmp_path, monkeypatch):
        """A board with no Edge.Cuts outline emits an explicit fallback warning."""
        from kicad_tools.cli import pcb_sync_netlist
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist
        from kicad_tools.schema.pcb import PCB

        sch = tmp_path / "test.kicad_sch"
        pcb_path = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)  # R1 + C1
        pcb_path.write_text(PCB_NO_OUTLINE)  # only C1, no outline

        # Avoid depending on KiCad standard libraries: stub the actual add.
        monkeypatch.setattr(PCB, "add_footprint", lambda self, **kwargs: None)

        result = sync_netlist(sch, pcb_path, output_path=tmp_path / "out.kicad_pcb")

        assert any("No Edge.Cuts outline detected" in w for w in result.warnings), (
            f"expected fallback warning, got {result.warnings!r}"
        )
        # And it must be rendered by both formatters.
        text = pcb_sync_netlist.format_text(result, dry_run=False, pcb_path=pcb_path)
        assert "No Edge.Cuts outline detected" in text
        payload = json.loads(pcb_sync_netlist.format_json(result, dry_run=False, pcb_path=pcb_path))
        assert any("No Edge.Cuts outline detected" in w for w in payload["warnings"])

    def test_off_board_placement_emits_per_reference_warning(self, tmp_path, monkeypatch):
        """A footprint placed outside the outline bbox warns naming the ref."""
        from kicad_tools.cli import pcb_sync_netlist
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist
        from kicad_tools.schema.pcb import PCB

        sch = tmp_path / "test.kicad_sch"
        pcb_path = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)  # R1 + C1
        pcb_path.write_text(PCB_GR_POLY_OUTLINE)  # gr_poly outline, only C1

        # Force placement far outside the outline bbox (simulating Bug A/B).
        monkeypatch.setattr(
            pcb_sync_netlist, "_get_board_edge_position", lambda pcb: (500.0, 500.0)
        )
        monkeypatch.setattr(PCB, "add_footprint", lambda self, **kwargs: None)

        result = sync_netlist(sch, pcb_path, output_path=tmp_path / "out.kicad_pcb")

        off_board = [w for w in result.warnings if "placed outside board outline" in w]
        assert off_board, f"expected off-board warning, got {result.warnings!r}"
        assert any("R1" in w for w in off_board)
        assert any("(500.000, 500.000)" in w for w in off_board)

        # Rendered in both formatters.
        text = pcb_sync_netlist.format_text(result, dry_run=False, pcb_path=pcb_path)
        assert "placed outside board outline" in text
        payload = json.loads(pcb_sync_netlist.format_json(result, dry_run=False, pcb_path=pcb_path))
        assert any("placed outside board outline" in w for w in payload["warnings"])

    def test_in_bounds_staging_does_not_warn(self, tmp_path, monkeypatch):
        """Legitimate outline-derived staging placement produces no new warning."""
        from kicad_tools.cli.pcb_sync_netlist import sync_netlist
        from kicad_tools.schema.pcb import PCB

        sch = tmp_path / "test.kicad_sch"
        pcb_path = tmp_path / "test.kicad_pcb"
        sch.write_text(MINIMAL_SCHEMATIC)  # R1 + C1
        pcb_path.write_text(PCB_GR_POLY_OUTLINE)  # gr_poly outline present, only C1

        monkeypatch.setattr(PCB, "add_footprint", lambda self, **kwargs: None)

        result = sync_netlist(sch, pcb_path, output_path=tmp_path / "out.kicad_pcb")

        assert not any("placed outside board outline" in w for w in result.warnings)
        assert not any("No Edge.Cuts outline detected" in w for w in result.warnings)
