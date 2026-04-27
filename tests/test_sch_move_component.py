"""Tests for the sch move-component command.

Covers symbol repositioning, property label shifting, wire endpoint
reconnection, dry-run mode, JSON output, backup creation, grid snapping,
no-op detection, and error cases.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from kicad_tools.cli.sch_move_component import (
    MoveComponentResult,
    main as move_component_main,
    move_component,
    preview_move_component,
)
from kicad_tools.schema import LibraryManager, Schematic
from kicad_tools.sexp import SExp
from kicad_tools.sexp.parser import parse_string

# ---------------------------------------------------------------------------
# Minimal schematic with a resistor symbol and connected wires
# ---------------------------------------------------------------------------

SCHEMATIC_WITH_WIRES = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "Device:R_0_1"
        (pin passive line (at 0 -3.81 90) (length 2.54) (name "1" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 3.81 270) (length 2.54) (name "2" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    (property "Reference" "R1" (at 102 48 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 102 52 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 100 50 0) (effects (font (size 1.27 1.27)) hide))
    (pin "1" (uuid "pin-uuid-1"))
    (pin "2" (uuid "pin-uuid-2"))
  )
  (wire (pts (xy 100 46.19) (xy 100 40))
    (stroke (width 0) (type default))
    (uuid "wire-1")
  )
  (wire (pts (xy 100 53.81) (xy 100 60))
    (stroke (width 0) (type default))
    (uuid "wire-2")
  )
  (symbol_instances
    (path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      (reference "R1") (unit 1)
    )
  )
)
"""

# ---------------------------------------------------------------------------
# Minimal schematic with no wires (just a symbol)
# ---------------------------------------------------------------------------

SCHEMATIC_NO_WIRES = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "Device:R_0_1"
        (pin passive line (at 0 -3.81 90) (length 2.54) (name "1" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 3.81 270) (length 2.54) (name "2" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    (property "Reference" "R2" (at 102 48 0) (effects (font (size 1.27 1.27))))
    (property "Value" "4.7k" (at 102 52 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pin-uuid-3"))
    (pin "2" (uuid "pin-uuid-4"))
  )
  (symbol_instances
    (path "/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
      (reference "R2") (unit 1)
    )
  )
)
"""


def _load_schematic_from_string(content: str) -> Schematic:
    """Parse a schematic from string content."""
    sexp = parse_string(content)
    return Schematic(sexp)


def _make_lib_manager(sch: Schematic) -> LibraryManager:
    """Create a LibraryManager with the schematic's embedded symbols."""
    lib_manager = LibraryManager()
    lib_manager.load_embedded(sch)
    return lib_manager


# ---------------------------------------------------------------------------
# Tests: Core move operation
# ---------------------------------------------------------------------------


class TestMoveComponent:
    """Test the core move_component function."""

    def test_move_symbol_position_updated(self):
        """Moving a symbol updates the (at X Y) node."""
        sch = _load_schematic_from_string(SCHEMATIC_NO_WIRES)
        lib_mgr = _make_lib_manager(sch)

        result = move_component(sch, lib_mgr, "R2", (110.0, 60.0))

        assert result.old_position == (100.0, 50.0)
        assert result.new_position == (110.0, 60.0)

        # Verify the S-expression was actually updated
        sym = sch.get_symbol("R2")
        assert sym is not None
        assert sym.position == (110.0, 60.0)

    def test_move_shifts_properties(self):
        """Moving a symbol shifts all property (at) nodes by the same delta."""
        sch = _load_schematic_from_string(SCHEMATIC_NO_WIRES)
        lib_mgr = _make_lib_manager(sch)

        result = move_component(sch, lib_mgr, "R2", (110.0, 60.0))

        # Delta is (+10, +10)
        # Original Reference at (102, 48) -> (112, 58)
        # Original Value at (102, 52) -> (112, 62)
        assert result.properties_shifted >= 2

        # Verify by re-parsing the symbol
        from kicad_tools.cli.sch_move_component import _find_symbol_sexp

        sym_sexp = _find_symbol_sexp(sch, "R2")
        assert sym_sexp is not None

        props = {}
        for prop in sym_sexp.find_all("property"):
            name = prop.get_string(0)
            at_node = prop.find("at")
            if at_node:
                props[name] = (at_node.get_float(0), at_node.get_float(1))

        assert props["Reference"] == (112.0, 58.0)
        assert props["Value"] == (112.0, 62.0)

    def test_move_adjusts_wire_endpoints(self):
        """Moving a symbol updates wire endpoints connected to its pins."""
        sch = _load_schematic_from_string(SCHEMATIC_WITH_WIRES)
        lib_mgr = _make_lib_manager(sch)

        # Move R1 from (100, 50) to (110, 50) -- shift 10mm in X
        result = move_component(sch, lib_mgr, "R1", (110.0, 50.0))

        assert result.wires_adjusted == 2

        # Verify wire endpoints were updated
        wires = list(sch.sexp.find_all("wire"))
        assert len(wires) == 2

        # Check that wire endpoints shifted by +10 in X
        for wire in wires:
            pts = wire.find("pts")
            xy_nodes = pts.find_all("xy")
            # The endpoint connected to the pin should have moved
            x0 = xy_nodes[0].get_float(0)
            # At least one endpoint per wire should be at x=110
            x1 = xy_nodes[1].get_float(0)
            assert x0 == 110.0 or x1 == 110.0

    def test_move_preserves_uuid(self):
        """Moving a symbol does not change its UUID."""
        sch = _load_schematic_from_string(SCHEMATIC_NO_WIRES)
        lib_mgr = _make_lib_manager(sch)

        sym_before = sch.get_symbol("R2")
        uuid_before = sym_before.uuid

        move_component(sch, lib_mgr, "R2", (110.0, 60.0))

        sym_after = sch.get_symbol("R2")
        assert sym_after.uuid == uuid_before

    def test_move_to_same_position_is_noop(self):
        """Moving to the same position does nothing."""
        sch = _load_schematic_from_string(SCHEMATIC_NO_WIRES)
        lib_mgr = _make_lib_manager(sch)

        result = move_component(sch, lib_mgr, "R2", (100.0, 50.0))

        assert result.properties_shifted == 0
        assert result.wires_adjusted == 0
        assert not result.warnings

    def test_move_nonexistent_reference(self):
        """Moving a nonexistent reference returns warnings."""
        sch = _load_schematic_from_string(SCHEMATIC_NO_WIRES)
        lib_mgr = _make_lib_manager(sch)

        result = move_component(sch, lib_mgr, "U99", (110.0, 60.0))

        assert result.warnings
        assert "not found" in result.warnings[0]


# ---------------------------------------------------------------------------
# Tests: Preview (dry-run logic)
# ---------------------------------------------------------------------------


class TestPreviewMoveComponent:
    """Test preview_move_component."""

    def test_preview_returns_expected_fields(self):
        """Preview returns key information about the planned move."""
        sch = _load_schematic_from_string(SCHEMATIC_WITH_WIRES)
        lib_mgr = _make_lib_manager(sch)

        preview = preview_move_component(sch, lib_mgr, "R1", (120.0, 50.0))

        assert preview["reference"] == "R1"
        assert preview["old_position"] == [100.0, 50.0]
        assert preview["new_position"] == [120.0, 50.0]
        assert preview["delta"] == [20.0, 0.0]
        assert preview["wires_to_adjust"] == 2
        assert preview["is_noop"] is False

    def test_preview_noop_detection(self):
        """Preview detects same-position as noop."""
        sch = _load_schematic_from_string(SCHEMATIC_NO_WIRES)
        lib_mgr = _make_lib_manager(sch)

        preview = preview_move_component(sch, lib_mgr, "R2", (100.0, 50.0))

        assert preview["is_noop"] is True

    def test_preview_not_found(self):
        """Preview returns error for nonexistent reference."""
        sch = _load_schematic_from_string(SCHEMATIC_NO_WIRES)
        lib_mgr = _make_lib_manager(sch)

        preview = preview_move_component(sch, lib_mgr, "U99", (110.0, 60.0))

        assert "error" in preview


# ---------------------------------------------------------------------------
# Tests: CLI integration (main function)
# ---------------------------------------------------------------------------


class TestMoveComponentCLI:
    """Test the CLI main() function."""

    def test_dry_run_does_not_modify(self, tmp_path):
        """--dry-run does not write any changes to the file."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_NO_WIRES)
        original_content = sch_file.read_text()

        ret = move_component_main([
            str(sch_file), "--ref", "R2", "--to", "120", "60", "--dry-run"
        ])

        assert ret == 0
        assert sch_file.read_text() == original_content

    def test_backup_creates_file(self, tmp_path):
        """--backup creates a timestamped backup file."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_NO_WIRES)

        ret = move_component_main([
            str(sch_file), "--ref", "R2", "--to", "120", "60", "--backup"
        ])

        assert ret == 0
        backups = list(tmp_path.glob("*.backup-*"))
        assert len(backups) == 1

    def test_json_output(self, tmp_path, capsys):
        """--format json produces valid JSON output."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_NO_WIRES)

        ret = move_component_main([
            str(sch_file), "--ref", "R2", "--to", "120", "60", "--format", "json"
        ])

        assert ret == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["moved"] is True
        assert data["reference"] == "R2"

    def test_not_found_error(self, tmp_path, capsys):
        """Error exit when reference not found."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_NO_WIRES)

        ret = move_component_main([
            str(sch_file), "--ref", "U99", "--to", "120", "60"
        ])

        assert ret == 1

    def test_grid_snapping(self, tmp_path):
        """Non-grid-aligned input is snapped to 1.27mm grid."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_NO_WIRES)

        # 110.5 should snap to nearest 1.27 multiple
        ret = move_component_main([
            str(sch_file), "--ref", "R2", "--to", "110.5", "60.3"
        ])

        assert ret == 0

        # Verify position was snapped
        sch = Schematic.load(sch_file)
        sym = sch.get_symbol("R2")
        # 110.5 / 1.27 = 87.01 -> round to 87 -> 87 * 1.27 = 110.49
        # 60.3 / 1.27 = 47.48 -> round to 47 -> 47 * 1.27 = 59.69
        assert abs(sym.position[0] % 1.27) < 0.01 or abs(sym.position[0] % 1.27 - 1.27) < 0.01
        assert abs(sym.position[1] % 1.27) < 0.01 or abs(sym.position[1] % 1.27 - 1.27) < 0.01

    def test_dry_run_json_output(self, tmp_path, capsys):
        """--dry-run with --format json produces valid preview JSON."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_WITH_WIRES)

        ret = move_component_main([
            str(sch_file), "--ref", "R1", "--to", "120", "50",
            "--dry-run", "--format", "json"
        ])

        assert ret == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["dry_run"] is True
        assert data["moved"] is False
        assert data["wires_to_adjust"] == 2

    def test_file_not_found(self, tmp_path, capsys):
        """Error exit when schematic file doesn't exist."""
        ret = move_component_main([
            str(tmp_path / "nonexistent.kicad_sch"), "--ref", "R1", "--to", "120", "50"
        ])

        assert ret == 1
