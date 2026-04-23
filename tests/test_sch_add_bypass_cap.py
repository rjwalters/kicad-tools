"""Tests for the sch add-bypass-cap command.

Covers capacitor placement, ground symbol placement, wire creation,
junction insertion, auto-reference, --dry-run, --backup, and error cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.sch_add_bypass_cap import (
    _auto_reference,
    _compute_cap_offset,
    _make_default_cap_lib_sym,
    _snap,
    main as add_bypass_main,
)
from kicad_tools.schema import Schematic

# ---------------------------------------------------------------------------
# Minimal schematic content for testing -- includes a symbol U1 with a
# power pin (pin 4, VDD) placed at known coordinates plus a Device:C and
# power:GND lib_symbol definition so the command can resolve pin positions.
# ---------------------------------------------------------------------------

# U1 is placed at (100, 80) with rotation 0.
# It has pin 4 at local position (0, -5.08) pointing down (rotation 270),
# which puts pin 4's schematic position at (100, 85.09) (snapped).
# There is a horizontal wire at y=85.09 from x=80 to x=120 to simulate
# a VDD bus so we can test junction insertion.

SCHEMATIC_WITH_IC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "test:IC"
      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "IC" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "test:IC_1_1"
        (pin power_in line (at 0 -5.08 270) (length 1.27) (name "VDD" (effects (font (size 1.27 1.27)))) (number "4" (effects (font (size 1.27 1.27)))))
        (pin input line (at -5.08 0 180) (length 1.27) (name "IN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "Device:C"
      (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:C_0_1"
        (polyline (pts (xy -1.016 -0.762) (xy 1.016 -0.762)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "Device:C_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GND"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GND" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:GND_0_1"
        (polyline (pts (xy 0 0) (xy 0 -1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "power:GND_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "GND" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GNDD"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GNDD" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:GNDD_0_1"
        (polyline (pts (xy 0 0) (xy 0 -1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "power:GNDD_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "GNDD" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "test:IC") (at 100 80 0) (unit 1)
    (in_bom yes) (on_board yes)
    (uuid "11111111-1111-1111-1111-111111111111")
    (property "Reference" "U1" (at 100 75 0) (effects (font (size 1.27 1.27))))
    (property "Value" "IC" (at 100 77 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "4" (uuid "pin-4-uuid"))
    (pin "1" (uuid "pin-1-uuid"))
    (instances
      (project "test_project"
        (path "/" (reference "U1") (unit 1))
      )
    )
  )
  (symbol (lib_id "Device:C") (at 50 50 0) (unit 1)
    (in_bom yes) (on_board yes)
    (uuid "22222222-2222-2222-2222-222222222222")
    (property "Reference" "C1" (at 50 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 50 47 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "pin-c1-1"))
    (pin "2" (uuid "pin-c1-2"))
    (instances
      (project "test_project"
        (path "/" (reference "C1") (unit 1))
      )
    )
  )
  (symbol (lib_id "Device:C") (at 60 50 0) (unit 1)
    (in_bom yes) (on_board yes)
    (uuid "33333333-3333-3333-3333-333333333333")
    (property "Reference" "C5" (at 60 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10nF" (at 60 47 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "pin-c5-1"))
    (pin "2" (uuid "pin-c5-2"))
    (instances
      (project "test_project"
        (path "/" (reference "C5") (unit 1))
      )
    )
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

# Same schematic but with a VDD bus wire crossing pin 4's position
# to test junction insertion.
# Pin 4 is at library position (0, -5.08) with rotation 270.
# Instance is at (100, 80) rotation 0.
# Pin schematic position: approximately (100.33, 74.93) after snapping.
# Place the bus wire at y=74.93 crossing through pin 4.
SCHEMATIC_WITH_BUS = SCHEMATIC_WITH_IC.replace(
    "  (sheet_instances",
    """\
  (wire (pts (xy 80 74.93) (xy 120 74.93))
    (stroke (width 0) (type default))
    (uuid "bus-wire-1")
  )
  (sheet_instances""",
)


def _write_sch(tmp_path: Path, content: str = SCHEMATIC_WITH_IC) -> Path:
    p = tmp_path / "test_bypass.kicad_sch"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class TestAutoReference:
    def test_next_after_existing(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        sch = Schematic.load(sch_path)
        # Schematic has C1 and C5, so next should be C6
        ref = _auto_reference(sch, "C")
        assert ref == "C6"

    def test_no_existing(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        sch = Schematic.load(sch_path)
        # No R-prefix symbols exist
        ref = _auto_reference(sch, "R")
        assert ref == "R1"


class TestComputeCapOffset:
    def test_pin_pointing_right(self):
        dx, dy = _compute_cap_offset(0, 5.08)
        assert dx == pytest.approx(5.08)
        assert dy == pytest.approx(0)

    def test_pin_pointing_down(self):
        dx, dy = _compute_cap_offset(270, 5.08)
        assert dx == pytest.approx(0)
        assert dy == pytest.approx(5.08)

    def test_pin_pointing_up(self):
        dx, dy = _compute_cap_offset(90, 5.08)
        assert dx == pytest.approx(0)
        assert dy == pytest.approx(-5.08)

    def test_pin_pointing_left(self):
        dx, dy = _compute_cap_offset(180, 5.08)
        assert dx == pytest.approx(-5.08)
        assert dy == pytest.approx(0)


class TestMakeDefaultCapLibSym:
    def test_has_two_pins(self):
        sym = _make_default_cap_lib_sym()
        assert len(sym.pins) == 2
        pin_numbers = {p.number for p in sym.pins}
        assert pin_numbers == {"1", "2"}

    def test_name(self):
        sym = _make_default_cap_lib_sym()
        assert sym.name == "Device:C"


# ---------------------------------------------------------------------------
# Integration tests: basic placement
# ---------------------------------------------------------------------------


class TestBasicPlacement:
    def test_place_bypass_cap(self, tmp_path: Path):
        """Place a bypass cap on U1 pin 4 and verify symbols and wires appear."""
        sch_path = _write_sch(tmp_path)
        result = add_bypass_main([
            str(sch_path),
            "--ref", "U1",
            "--pin", "4",
            "--value", "100nF",
            "--ground-net", "GND",
        ])
        assert result == 0

        # Reload and verify
        sch = Schematic.load(sch_path)

        # Should have the original U1 + C1 + C5 + new cap + new ground
        # At least one new cap symbol should exist
        cap_refs = [s.reference for s in sch.symbols if s.reference.startswith("C")]
        assert "C6" in cap_refs  # auto-assigned after C5

        # Should have at least one GND power symbol added
        gnd_symbols = [s for s in sch.symbols if s.lib_id == "power:GND"]
        assert len(gnd_symbols) >= 1

        # Should have new wires
        assert len(sch.wires) >= 1

    def test_custom_reference(self, tmp_path: Path):
        """Explicitly provide --reference."""
        sch_path = _write_sch(tmp_path)
        result = add_bypass_main([
            str(sch_path),
            "--ref", "U1",
            "--pin", "4",
            "--reference", "C42",
            "--value", "22nF",
        ])
        assert result == 0

        sch = Schematic.load(sch_path)
        cap_refs = [s.reference for s in sch.symbols if s.reference == "C42"]
        assert len(cap_refs) == 1

    def test_custom_ground_net(self, tmp_path: Path):
        """Use --ground-net GNDD."""
        sch_path = _write_sch(tmp_path)
        result = add_bypass_main([
            str(sch_path),
            "--ref", "U1",
            "--pin", "4",
            "--ground-net", "GNDD",
        ])
        assert result == 0

        sch = Schematic.load(sch_path)
        gndd_symbols = [s for s in sch.symbols if s.lib_id == "power:GNDD"]
        assert len(gndd_symbols) >= 1


# ---------------------------------------------------------------------------
# Junction insertion
# ---------------------------------------------------------------------------


class TestJunctionInsertion:
    def test_junction_on_bus_wire(self, tmp_path: Path):
        """When the target pin coordinate is on an existing wire midpoint,
        a junction should be inserted."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_WITH_BUS)
        result = add_bypass_main([
            str(sch_path),
            "--ref", "U1",
            "--pin", "4",
        ])
        assert result == 0

        sch = Schematic.load(sch_path)
        # There should be at least one junction near pin 4's position
        junctions = sch.junctions
        assert len(junctions) >= 1


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_no_file_changes(self, tmp_path: Path):
        """--dry-run should not modify the schematic file."""
        sch_path = _write_sch(tmp_path)
        original_content = sch_path.read_text()

        result = add_bypass_main([
            str(sch_path),
            "--ref", "U1",
            "--pin", "4",
            "--dry-run",
        ])
        assert result == 0

        # File should be unchanged
        assert sch_path.read_text() == original_content


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


class TestBackup:
    def test_backup_created(self, tmp_path: Path):
        """--backup should create a backup file before modifying."""
        sch_path = _write_sch(tmp_path)

        result = add_bypass_main([
            str(sch_path),
            "--ref", "U1",
            "--pin", "4",
            "--backup",
        ])
        assert result == 0

        # A backup file should exist
        backup_files = list(tmp_path.glob("*.backup-*"))
        assert len(backup_files) == 1


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrors:
    def test_unknown_ref(self, tmp_path: Path):
        """Error when --ref symbol doesn't exist."""
        sch_path = _write_sch(tmp_path)
        result = add_bypass_main([
            str(sch_path),
            "--ref", "U99",
            "--pin", "4",
        ])
        assert result == 1

    def test_unknown_pin(self, tmp_path: Path):
        """Error when --pin doesn't exist on the symbol."""
        sch_path = _write_sch(tmp_path)
        result = add_bypass_main([
            str(sch_path),
            "--ref", "U1",
            "--pin", "99",
        ])
        assert result == 1

    def test_missing_schematic(self, tmp_path: Path):
        """Error when schematic file doesn't exist."""
        result = add_bypass_main([
            str(tmp_path / "nonexistent.kicad_sch"),
            "--ref", "U1",
            "--pin", "4",
        ])
        assert result == 1
