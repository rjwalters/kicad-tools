"""Tests for the sch remove-component command.

Covers symbol removal, exclusive vs shared wire handling, orphaned junction
cleanup, dry-run mode, JSON output, lib_symbols cleanup, symbol_instances
cleanup, and error cases.
"""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.cli.sch_remove_component import (
    main as remove_component_main,
)
from kicad_tools.cli.sch_remove_component import (
    remove_component,
)
from kicad_tools.schema import LibraryManager, Schematic

# ---------------------------------------------------------------------------
# Minimal schematic with a symbol that has one exclusive wire
# ---------------------------------------------------------------------------

SCHEMATIC_EXCLUSIVE_WIRE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "power:PWR_FLAG"
      (property "Reference" "#FLG" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "PWR_FLAG" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:PWR_FLAG_0_1"
        (pin power_out line (at 0 0 0) (length 0) (name "pwr" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "power:PWR_FLAG") (at 100 50 0) (unit 1)
    (in_bom no) (on_board no) (dnp no)
    (uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    (property "Reference" "#FLG01" (at 100 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "PWR_FLAG" (at 100 42 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pin-uuid-1"))
  )
  (wire (pts (xy 100 50) (xy 100 60))
    (stroke (width 0) (type default))
    (uuid "wire-exclusive-1")
  )
  (symbol_instances
    (path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      (reference "#FLG01") (unit 1)
    )
  )
)
"""

# ---------------------------------------------------------------------------
# Schematic with a shared wire (another symbol also connects to the wire)
# ---------------------------------------------------------------------------

SCHEMATIC_SHARED_WIRE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "power:PWR_FLAG"
      (property "Reference" "#FLG" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "PWR_FLAG" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:PWR_FLAG_0_1"
        (pin power_out line (at 0 0 0) (length 0) (name "pwr" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GND"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GND" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:GND_0_1"
        (pin power_in line (at 0 0 0) (length 0) (name "GND" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "power:PWR_FLAG") (at 100 50 0) (unit 1)
    (in_bom no) (on_board no) (dnp no)
    (uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    (property "Reference" "#FLG01" (at 100 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "PWR_FLAG" (at 100 42 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pin-uuid-1"))
  )
  (symbol (lib_id "power:GND") (at 100 60 0) (unit 1)
    (in_bom no) (on_board no) (dnp no)
    (uuid "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    (property "Reference" "#PWR01" (at 100 65 0) (effects (font (size 1.27 1.27))))
    (property "Value" "GND" (at 100 63 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pin-uuid-2"))
  )
  (wire (pts (xy 100 50) (xy 100 60))
    (stroke (width 0) (type default))
    (uuid "wire-shared-1")
  )
  (symbol_instances
    (path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      (reference "#FLG01") (unit 1)
    )
    (path "/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
      (reference "#PWR01") (unit 1)
    )
  )
)
"""

# ---------------------------------------------------------------------------
# Schematic with a symbol that has no wires
# ---------------------------------------------------------------------------

SCHEMATIC_NO_WIRES = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "power:PWR_FLAG"
      (property "Reference" "#FLG" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "PWR_FLAG" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:PWR_FLAG_0_1"
        (pin power_out line (at 0 0 0) (length 0) (name "pwr" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "power:PWR_FLAG") (at 200 200 0) (unit 1)
    (in_bom no) (on_board no) (dnp no)
    (uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
    (property "Reference" "#FLG02" (at 200 195 0) (effects (font (size 1.27 1.27))))
    (property "Value" "PWR_FLAG" (at 200 192 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pin-uuid-3"))
  )
  (symbol_instances
    (path "/cccccccc-cccc-cccc-cccc-cccccccccccc"
      (reference "#FLG02") (unit 1)
    )
  )
)
"""

# ---------------------------------------------------------------------------
# Schematic with two instances of the same lib_id (for lib_symbols cleanup test)
# ---------------------------------------------------------------------------

SCHEMATIC_TWO_INSTANCES = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "power:PWR_FLAG"
      (property "Reference" "#FLG" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "PWR_FLAG" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:PWR_FLAG_0_1"
        (pin power_out line (at 0 0 0) (length 0) (name "pwr" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "power:PWR_FLAG") (at 100 50 0) (unit 1)
    (in_bom no) (on_board no) (dnp no)
    (uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    (property "Reference" "#FLG01" (at 100 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "PWR_FLAG" (at 100 42 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pin-uuid-1"))
  )
  (symbol (lib_id "power:PWR_FLAG") (at 200 50 0) (unit 1)
    (in_bom no) (on_board no) (dnp no)
    (uuid "dddddddd-dddd-dddd-dddd-dddddddddddd")
    (property "Reference" "#FLG02" (at 200 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "PWR_FLAG" (at 200 42 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pin-uuid-4"))
  )
  (symbol_instances
    (path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      (reference "#FLG01") (unit 1)
    )
    (path "/dddddddd-dddd-dddd-dddd-dddddddddddd"
      (reference "#FLG02") (unit 1)
    )
  )
)
"""

# ---------------------------------------------------------------------------
# Schematic with a junction that becomes orphaned
# ---------------------------------------------------------------------------

SCHEMATIC_WITH_JUNCTION = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "power:PWR_FLAG"
      (property "Reference" "#FLG" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "PWR_FLAG" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:PWR_FLAG_0_1"
        (pin power_out line (at 0 0 0) (length 0) (name "pwr" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "power:PWR_FLAG") (at 100 50 0) (unit 1)
    (in_bom no) (on_board no) (dnp no)
    (uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    (property "Reference" "#FLG01" (at 100 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "PWR_FLAG" (at 100 42 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pin-uuid-1"))
  )
  (wire (pts (xy 100 50) (xy 100 60))
    (stroke (width 0) (type default))
    (uuid "wire-excl-1")
  )
  (junction (at 100 50)
    (diameter 0)
    (color 0 0 0 0)
    (uuid "junc-1")
  )
  (symbol_instances
    (path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      (reference "#FLG01") (unit 1)
    )
  )
)
"""


def _write_sch(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test_remove.kicad_sch"
    p.write_text(content)
    return p


class TestRemoveExclusiveWire:
    """Removing a symbol with one exclusive wire: wire is removed."""

    def test_exclusive_wire_removed(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path, SCHEMATIC_EXCLUSIVE_WIRE)
        sch = Schematic.load(sch_path)

        lib_manager = LibraryManager()
        lib_manager.load_embedded(sch)

        result = remove_component(sch, lib_manager, "#FLG01")

        assert result.symbol_removed is True
        assert result.wires_removed == 1
        assert result.lib_symbol_removed is True
        assert result.instance_path_removed is True

        # Verify the wire is gone
        remaining_wires = list(sch.sexp.find_all("wire"))
        assert len(remaining_wires) == 0

        # Verify the symbol is gone
        assert sch.get_symbol("#FLG01") is None


class TestRemoveSharedWire:
    """Removing a symbol with a shared wire: wire is preserved."""

    def test_shared_wire_preserved(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path, SCHEMATIC_SHARED_WIRE)
        sch = Schematic.load(sch_path)

        lib_manager = LibraryManager()
        lib_manager.load_embedded(sch)

        result = remove_component(sch, lib_manager, "#FLG01")

        assert result.symbol_removed is True
        assert result.wires_removed == 0  # Wire is shared with GND

        # Verify the wire still exists
        remaining_wires = list(sch.sexp.find_all("wire"))
        assert len(remaining_wires) == 1

        # Verify the other symbol still exists
        assert sch.get_symbol("#PWR01") is not None

        # lib_symbols for PWR_FLAG should be removed (last instance of that lib_id)
        assert result.lib_symbol_removed is True

        # GND lib symbol should still exist
        lib_syms = sch.lib_symbols
        gnd_found = False
        for sym in lib_syms.find_all("symbol"):
            if sym.get_string(0) == "power:GND":
                gnd_found = True
        assert gnd_found


class TestRemoveNoWires:
    """Removing a symbol with no wires: only symbol is removed."""

    def test_no_wires(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path, SCHEMATIC_NO_WIRES)
        sch = Schematic.load(sch_path)

        lib_manager = LibraryManager()
        lib_manager.load_embedded(sch)

        result = remove_component(sch, lib_manager, "#FLG02")

        assert result.symbol_removed is True
        assert result.wires_removed == 0
        assert result.junctions_removed == 0
        assert result.lib_symbol_removed is True


class TestLibSymbolsNotRemovedWhenOtherInstances:
    """lib_symbols entry preserved when other instances remain."""

    def test_lib_symbol_preserved(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path, SCHEMATIC_TWO_INSTANCES)
        sch = Schematic.load(sch_path)

        lib_manager = LibraryManager()
        lib_manager.load_embedded(sch)

        result = remove_component(sch, lib_manager, "#FLG01")

        assert result.symbol_removed is True
        assert result.lib_symbol_removed is False  # #FLG02 still uses it

        # Verify the other instance is still there
        assert sch.get_symbol("#FLG02") is not None

        # Verify lib_symbols entry still exists
        lib_syms = sch.lib_symbols
        found = False
        for sym in lib_syms.find_all("symbol"):
            if sym.get_string(0) == "power:PWR_FLAG":
                found = True
        assert found


class TestOrphanedJunctionCleanup:
    """Orphaned junctions are removed when exclusive wires are removed."""

    def test_junction_removed(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path, SCHEMATIC_WITH_JUNCTION)
        sch = Schematic.load(sch_path)

        lib_manager = LibraryManager()
        lib_manager.load_embedded(sch)

        result = remove_component(sch, lib_manager, "#FLG01")

        assert result.symbol_removed is True
        assert result.wires_removed == 1
        assert result.junctions_removed == 1

        # Verify junction is gone
        remaining_junctions = list(sch.sexp.find_all("junction"))
        assert len(remaining_junctions) == 0


class TestDryRun:
    """--dry-run mode does not modify the file."""

    def test_dry_run_text(self, tmp_path: Path, capsys):
        sch_path = _write_sch(tmp_path, SCHEMATIC_EXCLUSIVE_WIRE)
        original_content = sch_path.read_text()

        rc = remove_component_main(
            [
                str(sch_path),
                "--ref",
                "#FLG01",
                "--dry-run",
            ]
        )

        assert rc == 0
        # File should be unchanged
        assert sch_path.read_text() == original_content

        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "#FLG01" in captured.out

    def test_dry_run_json(self, tmp_path: Path, capsys):
        sch_path = _write_sch(tmp_path, SCHEMATIC_EXCLUSIVE_WIRE)
        original_content = sch_path.read_text()

        rc = remove_component_main(
            [
                str(sch_path),
                "--ref",
                "#FLG01",
                "--dry-run",
                "--format",
                "json",
            ]
        )

        assert rc == 0
        assert sch_path.read_text() == original_content

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["dry_run"] is True
        assert data["removed"] is False
        assert data["reference"] == "#FLG01"
        assert "wires_exclusive" in data


class TestReferenceNotFound:
    """Exit code 1 when reference not found."""

    def test_not_found_text(self, tmp_path: Path, capsys):
        sch_path = _write_sch(tmp_path, SCHEMATIC_NO_WIRES)

        rc = remove_component_main(
            [
                str(sch_path),
                "--ref",
                "NONEXISTENT",
            ]
        )

        assert rc == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err

    def test_not_found_json(self, tmp_path: Path, capsys):
        sch_path = _write_sch(tmp_path, SCHEMATIC_NO_WIRES)

        rc = remove_component_main(
            [
                str(sch_path),
                "--ref",
                "NONEXISTENT",
                "--format",
                "json",
            ]
        )

        assert rc == 1
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["removed"] is False
        assert "error" in data


class TestJsonOutput:
    """--format json produces valid structured output."""

    def test_json_success(self, tmp_path: Path, capsys):
        sch_path = _write_sch(tmp_path, SCHEMATIC_EXCLUSIVE_WIRE)

        rc = remove_component_main(
            [
                str(sch_path),
                "--ref",
                "#FLG01",
                "--format",
                "json",
            ]
        )

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["removed"] is True
        assert data["reference"] == "#FLG01"
        assert isinstance(data["wires_removed"], int)
        assert isinstance(data["junctions_removed"], int)
        assert isinstance(data["lib_symbol_removed"], bool)
        assert isinstance(data["instance_path_removed"], bool)


class TestBackup:
    """--backup creates a timestamped backup file."""

    def test_backup_created(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path, SCHEMATIC_EXCLUSIVE_WIRE)
        original_content = sch_path.read_text()

        rc = remove_component_main(
            [
                str(sch_path),
                "--ref",
                "#FLG01",
                "--backup",
            ]
        )

        assert rc == 0

        # Find backup file
        backup_files = list(tmp_path.glob("*.backup-*"))
        assert len(backup_files) == 1
        assert backup_files[0].read_text() == original_content


class TestSaveRoundTrip:
    """Verify schematic can be saved and reloaded after removal."""

    def test_round_trip(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path, SCHEMATIC_EXCLUSIVE_WIRE)

        rc = remove_component_main(
            [
                str(sch_path),
                "--ref",
                "#FLG01",
            ]
        )
        assert rc == 0

        # Reload and verify
        sch = Schematic.load(sch_path)
        assert sch.get_symbol("#FLG01") is None
        assert len(list(sch.sexp.find_all("wire"))) == 0
