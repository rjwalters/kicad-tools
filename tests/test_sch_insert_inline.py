"""Tests for the sch insert-inline command.

Covers inline insertion of 2-pin components into horizontal and vertical
wires, gap expansion, dry-run, backup, --near mode, diagonal wire
rejection, and gap-too-small error without --expand-gap.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kicad_tools.cli.sch_insert_inline import (
    PlannedAction,
    _auto_reference,
    _auto_rotation_for_wire,
    _compute_pin_span,
    _is_axis_aligned,
    _is_horizontal,
    _shift_downstream_wires,
    _snap,
    _wire_length,
    main as insert_inline_main,
    run_insert_inline,
)
from kicad_tools.schema import Schematic
from kicad_tools.schema.library import LibraryPin, LibrarySymbol

# ---------------------------------------------------------------------------
# Minimal schematic with a Device:D library symbol and a horizontal wire
# ---------------------------------------------------------------------------

# Device:D has pin 1 at (0, 0) rot=90 (anode) and pin 2 at (0, 0) rot=270 (cathode).
# For a simple test, we use a 2-pin model with pins at +/-2.54 y.
SCHEMATIC_HORIZONTAL_WIRE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:D"
      (property "Reference" "D" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "D" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:D_0_1"
        (polyline (pts (xy -1.27 1.27) (xy -1.27 -1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "Device:D_1_1"
        (pin passive line (at 0 2.54 270) (length 1.27) (name "K" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -2.54 90) (length 1.27) (name "A" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (wire (pts (xy 100 50) (xy 120 50))
    (stroke (width 0) (type default))
    (uuid "wire-horiz-1")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

SCHEMATIC_VERTICAL_WIRE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000002")
  (paper "A4")
  (lib_symbols
    (symbol "Device:D"
      (property "Reference" "D" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "D" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:D_0_1"
        (polyline (pts (xy -1.27 1.27) (xy -1.27 -1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "Device:D_1_1"
        (pin passive line (at 0 2.54 270) (length 1.27) (name "K" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -2.54 90) (length 1.27) (name "A" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (wire (pts (xy 100 50) (xy 100 70))
    (stroke (width 0) (type default))
    (uuid "wire-vert-1")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

SCHEMATIC_SHORT_WIRE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000003")
  (paper "A4")
  (lib_symbols
    (symbol "Device:D"
      (property "Reference" "D" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "D" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:D_0_1"
        (polyline (pts (xy -1.27 1.27) (xy -1.27 -1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "Device:D_1_1"
        (pin passive line (at 0 2.54 270) (length 1.27) (name "K" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -2.54 90) (length 1.27) (name "A" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (wire (pts (xy 100 50) (xy 102 50))
    (stroke (width 0) (type default))
    (uuid "wire-short-1")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

SCHEMATIC_DIAGONAL_WIRE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000004")
  (paper "A4")
  (lib_symbols
    (symbol "Device:D"
      (property "Reference" "D" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "D" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:D_0_1"
        (polyline (pts (xy -1.27 1.27) (xy -1.27 -1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "Device:D_1_1"
        (pin passive line (at 0 2.54 270) (length 1.27) (name "K" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -2.54 90) (length 1.27) (name "A" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (wire (pts (xy 100 50) (xy 110 60))
    (stroke (width 0) (type default))
    (uuid "wire-diag-1")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


def _write_sch(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test_insert_inline.kicad_sch"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Unit helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_snap(self):
        assert _snap(10.0) == pytest.approx(10.16, abs=0.01)
        assert _snap(1.27) == pytest.approx(1.27, abs=0.001)

    def test_is_axis_aligned(self):
        assert _is_axis_aligned((0, 0), (10, 0)) is True
        assert _is_axis_aligned((0, 0), (0, 10)) is True
        assert _is_axis_aligned((0, 0), (10, 10)) is False

    def test_is_horizontal(self):
        assert _is_horizontal((0, 0), (10, 0)) is True
        assert _is_horizontal((0, 0), (0, 10)) is False

    def test_wire_length(self):
        assert _wire_length((0, 0), (3, 4)) == pytest.approx(5.0)
        assert _wire_length((10, 10), (10, 20)) == pytest.approx(10.0)

    def test_auto_rotation_horizontal_wire(self):
        """For a horizontal wire with vertically-arranged pins, rotation=90."""
        lib_sym = LibrarySymbol(
            name="Device:D",
            properties={},
            pins=[
                LibraryPin(
                    number="1", name="K", type="passive",
                    position=(0, 2.54), rotation=270, length=1.27,
                ),
                LibraryPin(
                    number="2", name="A", type="passive",
                    position=(0, -2.54), rotation=90, length=1.27,
                ),
            ],
        )
        rot = _auto_rotation_for_wire((100, 50), (120, 50), lib_sym, "1", "2")
        assert rot == 90  # pins are vertical at rot=0, need 90 for horizontal

    def test_auto_rotation_vertical_wire(self):
        """For a vertical wire with vertically-arranged pins, rotation=0."""
        lib_sym = LibrarySymbol(
            name="Device:D",
            properties={},
            pins=[
                LibraryPin(
                    number="1", name="K", type="passive",
                    position=(0, 2.54), rotation=270, length=1.27,
                ),
                LibraryPin(
                    number="2", name="A", type="passive",
                    position=(0, -2.54), rotation=90, length=1.27,
                ),
            ],
        )
        rot = _auto_rotation_for_wire((100, 50), (100, 70), lib_sym, "1", "2")
        assert rot == 0  # pins already vertical

    def test_compute_pin_span(self):
        lib_sym = LibrarySymbol(
            name="Device:D",
            properties={},
            pins=[
                LibraryPin(
                    number="1", name="K", type="passive",
                    position=(0, 2.54), rotation=270, length=1.27,
                ),
                LibraryPin(
                    number="2", name="A", type="passive",
                    position=(0, -2.54), rotation=90, length=1.27,
                ),
            ],
        )
        span = _compute_pin_span(lib_sym, "1", "2", rotation=0)
        assert span == pytest.approx(5.08, abs=0.01)


# ---------------------------------------------------------------------------
# Integration tests: horizontal wire insert
# ---------------------------------------------------------------------------


class TestInsertInlineHorizontal:
    def test_basic_insert(self, tmp_path):
        """Insert a Device:D diode into a horizontal wire with sufficient gap."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_HORIZONTAL_WIRE)
        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--reference", "D1",
            "--value", "BAT54",
            "--footprint", "Diode_SMD:D_SOD-323",
            "--from", "100", "50",
            "--to", "120", "50",
        ])
        assert rc == 0

        # Reload and verify
        sch = Schematic.load(sch_path)
        # Original wire should be removed
        wire_endpoints = [(w.start, w.end) for w in sch.wires]
        # Should NOT have the original (100,50)->(120,50) wire
        original_present = any(
            abs(s[0] - 100) < 0.5 and abs(s[1] - 50) < 0.5
            and abs(e[0] - 120) < 0.5 and abs(e[1] - 50) < 0.5
            for s, e in wire_endpoints
        )
        assert not original_present, "Original wire should have been removed"

        # Should have the D1 symbol placed
        d1 = sch.get_symbol("D1")
        assert d1 is not None
        assert d1.value == "BAT54"

        # Should have at least one reconnection wire
        assert len(sch.wires) >= 1

    def test_dry_run_no_changes(self, tmp_path):
        """Dry run should not modify the schematic."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_HORIZONTAL_WIRE)
        original_content = sch_path.read_text()

        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--reference", "D1",
            "--value", "BAT54",
            "--footprint", "Diode_SMD:D_SOD-323",
            "--from", "100", "50",
            "--to", "120", "50",
            "--dry-run",
        ])
        assert rc == 0
        assert sch_path.read_text() == original_content

    def test_backup_created(self, tmp_path):
        """Backup flag should create a backup file."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_HORIZONTAL_WIRE)

        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--reference", "D1",
            "--value", "BAT54",
            "--footprint", "Diode_SMD:D_SOD-323",
            "--from", "100", "50",
            "--to", "120", "50",
            "--backup",
        ])
        assert rc == 0

        backup_files = list(tmp_path.glob("*.backup-*"))
        assert len(backup_files) >= 1

    def test_near_mode(self, tmp_path):
        """--near should find the nearest wire and insert there."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_HORIZONTAL_WIRE)

        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--reference", "D1",
            "--value", "BAT54",
            "--footprint", "Diode_SMD:D_SOD-323",
            "--near", "110", "50",
        ])
        assert rc == 0

        sch = Schematic.load(sch_path)
        d1 = sch.get_symbol("D1")
        assert d1 is not None


# ---------------------------------------------------------------------------
# Integration tests: vertical wire insert
# ---------------------------------------------------------------------------


class TestInsertInlineVertical:
    def test_basic_insert(self, tmp_path):
        """Insert a Device:D diode into a vertical wire."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_VERTICAL_WIRE)
        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--reference", "D1",
            "--value", "1N4148",
            "--footprint", "Diode_SMD:D_SOD-323",
            "--from", "100", "50",
            "--to", "100", "70",
        ])
        assert rc == 0

        sch = Schematic.load(sch_path)
        d1 = sch.get_symbol("D1")
        assert d1 is not None
        assert d1.value == "1N4148"


# ---------------------------------------------------------------------------
# Gap expansion tests
# ---------------------------------------------------------------------------


class TestGapExpansion:
    def test_gap_too_small_without_expand(self, tmp_path):
        """Without --expand-gap, inserting into a short wire should fail."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_SHORT_WIRE)
        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--reference", "D1",
            "--value", "BAT54",
            "--footprint", "Diode_SMD:D_SOD-323",
            "--from", "100", "50",
            "--to", "102", "50",
        ])
        assert rc == 1  # Should fail

    def test_gap_expansion_succeeds(self, tmp_path):
        """With --expand-gap, a short wire should be expanded."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_SHORT_WIRE)
        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--reference", "D1",
            "--value", "BAT54",
            "--footprint", "Diode_SMD:D_SOD-323",
            "--from", "100", "50",
            "--to", "102", "50",
            "--expand-gap",
        ])
        assert rc == 0

        sch = Schematic.load(sch_path)
        d1 = sch.get_symbol("D1")
        assert d1 is not None


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrors:
    def test_diagonal_wire_rejected(self, tmp_path):
        """Diagonal wires should produce an error."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_DIAGONAL_WIRE)
        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--reference", "D1",
            "--value", "BAT54",
            "--footprint", "Diode_SMD:D_SOD-323",
            "--from", "100", "50",
            "--to", "110", "60",
        ])
        assert rc == 1

    def test_no_wire_found(self, tmp_path):
        """Specifying endpoints that don't match should fail."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_HORIZONTAL_WIRE)
        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--reference", "D1",
            "--value", "BAT54",
            "--footprint", "Diode_SMD:D_SOD-323",
            "--from", "200", "200",
            "--to", "300", "200",
        ])
        assert rc == 1

    def test_mutual_exclusion_from_near(self, tmp_path):
        """--from/--to and --near are mutually exclusive."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_HORIZONTAL_WIRE)
        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--reference", "D1",
            "--value", "BAT54",
            "--footprint", "Diode_SMD:D_SOD-323",
            "--from", "100", "50",
            "--to", "120", "50",
            "--near", "110", "50",
        ])
        assert rc == 1

    def test_neither_from_nor_near(self, tmp_path):
        """Must specify either --from/--to or --near."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_HORIZONTAL_WIRE)
        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--reference", "D1",
            "--value", "BAT54",
            "--footprint", "Diode_SMD:D_SOD-323",
        ])
        assert rc == 1

    def test_auto_reference(self, tmp_path):
        """When --reference is omitted, auto-assign from prefix."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_HORIZONTAL_WIRE)
        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--value", "BAT54",
            "--footprint", "Diode_SMD:D_SOD-323",
            "--from", "100", "50",
            "--to", "120", "50",
        ])
        assert rc == 0

        sch = Schematic.load(sch_path)
        # Should auto-assign D1 since no existing D references
        d1 = sch.get_symbol("D1")
        assert d1 is not None


# ---------------------------------------------------------------------------
# Wire at pin endpoint (edge case)
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_wire_exactly_at_pin_span(self, tmp_path):
        """Wire length exactly equals pin span -- no gap expansion needed."""
        # The Device:D has pin span ~5.08 mm at rotation 90 (horizontal).
        # Create a wire that is exactly 5.08 mm long.
        content = SCHEMATIC_HORIZONTAL_WIRE.replace(
            "(xy 120 50)", "(xy 105.08 50)"
        )
        sch_path = _write_sch(tmp_path, content)
        rc = insert_inline_main([
            str(sch_path),
            "--lib-id", "Device:D",
            "--reference", "D1",
            "--value", "BAT54",
            "--footprint", "Diode_SMD:D_SOD-323",
            "--from", "100", "50",
            "--to", "105.08", "50",
        ])
        assert rc == 0
