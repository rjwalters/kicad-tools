"""Tests for the sch reconnect-pin command.

Covers power-to-power reconnection, power-to-signal reconnection,
orphaned symbol removal, shared-bus preservation, dry-run, backup,
no-op when already connected, no existing connection, and error cases.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.sch_reconnect_pin import (
    _is_power_net,
    _make_power_lib_sym,
    execute_reconnect,
    plan_reconnect,
)
from kicad_tools.cli.sch_reconnect_pin import (
    main as reconnect_main,
)
from kicad_tools.schema import LibraryManager, Schematic

# ---------------------------------------------------------------------------
# Minimal schematic: C1 (Device:C) at (100, 80), pin 2 connected via a
# stub wire to a GNDA power symbol.  Pin 2 of Device:C is at (0, -3.81)
# rotation 90 in library coords, which at instance rotation 0 puts pin 2
# at approximately (100, 83.81) snapped to (100, 83.82).
#
# The stub wire goes from pin position to (100, 88.9) where the GNDA
# power symbol sits.
# ---------------------------------------------------------------------------

SCHEMATIC_POWER_STUB = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:C"
      (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:C_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GNDA"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GNDA" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:GNDA_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "GNDA" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GNDD"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GNDD" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:GNDD_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "GNDD" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "Device:C") (at 100 80 0) (unit 1)
    (in_bom yes) (on_board yes)
    (uuid "11111111-1111-1111-1111-111111111111")
    (property "Reference" "C1" (at 100 75 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 100 77 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "pin-c1-1"))
    (pin "2" (uuid "pin-c1-2"))
    (instances
      (project "test_project"
        (path "/" (reference "C1") (unit 1))
      )
    )
  )
  (symbol (lib_id "power:GNDA") (at 100 88.9 0) (unit 1)
    (in_bom no) (on_board no)
    (uuid "22222222-2222-2222-2222-222222222222")
    (property "Reference" "#PWR01" (at 100 88.9 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "GNDA" (at 100 91.44 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "pin-pwr-1"))
    (instances
      (project "test_project"
        (path "/" (reference "#PWR01") (unit 1))
      )
    )
  )
  (wire (pts (xy 100 83.82) (xy 100 88.9))
    (stroke (width 0) (type default))
    (uuid "wire-stub-1")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


def _load_from_string(content: str, tmp_path: Path) -> Schematic:
    """Write content to a temp file and load as Schematic."""
    sch_file = tmp_path / "test.kicad_sch"
    sch_file.write_text(content)
    return Schematic.load(sch_file)


def _make_lib_manager(sch: Schematic) -> LibraryManager:
    """Create a LibraryManager with embedded symbols loaded."""
    lm = LibraryManager()
    lm.load_embedded(sch)
    return lm


# ---- Tests ----


class TestPowerToPowerReconnect:
    """Power-to-power reconnection (e.g., GNDA -> GNDD)."""

    def test_plan_reconnect_power_to_power(self, tmp_path: Path):
        sch = _load_from_string(SCHEMATIC_POWER_STUB, tmp_path)
        lm = _make_lib_manager(sch)
        planned, pos, stub = plan_reconnect(sch, lm, "C1", "2", "GNDD")

        assert pos is not None
        assert stub is not None
        assert any(a.kind == "remove_wire" for a in planned)
        assert any(a.kind == "remove_symbol" and "GNDA" in a.description for a in planned)
        assert any(a.kind == "add_power" and "GNDD" in a.description for a in planned)

    def test_execute_reconnect_power_to_power(self, tmp_path: Path):
        sch = _load_from_string(SCHEMATIC_POWER_STUB, tmp_path)
        lm = _make_lib_manager(sch)

        _, pos, stub = plan_reconnect(sch, lm, "C1", "2", "GNDD")
        assert pos is not None and stub is not None

        execute_reconnect(sch, pos, stub, "GNDD")

        # Verify: GNDA symbol removed, GNDD symbol present
        gnda_symbols = [s for s in sch.symbols if s.lib_id == "power:GNDA"]
        gndd_symbols = [s for s in sch.symbols if s.lib_id == "power:GNDD"]
        assert len(gnda_symbols) == 0, "GNDA should be removed"
        assert len(gndd_symbols) == 1, "GNDD should be added"


class TestPowerToSignalReconnect:
    """Power-to-signal reconnection (e.g., GNDA -> SIG_GND global label)."""

    def test_plan_reconnect_power_to_signal(self, tmp_path: Path):
        sch = _load_from_string(SCHEMATIC_POWER_STUB, tmp_path)
        lm = _make_lib_manager(sch)
        planned, pos, stub = plan_reconnect(sch, lm, "C1", "2", "SIG_GND")

        assert pos is not None
        assert any(a.kind == "remove_symbol" and "GNDA" in a.description for a in planned)
        assert any(a.kind == "add_label" and "SIG_GND" in a.description for a in planned)

    def test_execute_reconnect_power_to_signal(self, tmp_path: Path):
        sch = _load_from_string(SCHEMATIC_POWER_STUB, tmp_path)
        lm = _make_lib_manager(sch)

        _, pos, stub = plan_reconnect(sch, lm, "C1", "2", "SIG_GND")
        assert pos is not None and stub is not None

        execute_reconnect(sch, pos, stub, "SIG_GND")

        # Verify: GNDA removed, global label SIG_GND present
        gnda_symbols = [s for s in sch.symbols if s.lib_id == "power:GNDA"]
        assert len(gnda_symbols) == 0

        global_labels = sch.global_labels
        sig_labels = [gl for gl in global_labels if gl.text == "SIG_GND"]
        assert len(sig_labels) == 1


class TestNoOp:
    """No-op when pin is already connected to the target net."""

    def test_already_connected(self, tmp_path: Path):
        sch = _load_from_string(SCHEMATIC_POWER_STUB, tmp_path)
        lm = _make_lib_manager(sch)
        planned, pos, stub = plan_reconnect(sch, lm, "C1", "2", "GNDA")

        assert pos is not None
        assert len(planned) == 1
        assert planned[0].kind == "no_op"


class TestNoExistingConnection:
    """Pin has no existing wire -- just add the new connection."""

    SCHEMATIC_NO_WIRE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:C"
      (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:C_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GNDD"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GNDD" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:GNDD_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "GNDD" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "Device:C") (at 100 80 0) (unit 1)
    (in_bom yes) (on_board yes)
    (uuid "11111111-1111-1111-1111-111111111111")
    (property "Reference" "C1" (at 100 75 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 100 77 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "pin-c1-1"))
    (pin "2" (uuid "pin-c1-2"))
    (instances
      (project "test_project"
        (path "/" (reference "C1") (unit 1))
      )
    )
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

    def test_no_existing_connection(self, tmp_path: Path):
        sch = _load_from_string(self.SCHEMATIC_NO_WIRE, tmp_path)
        lm = _make_lib_manager(sch)
        planned, pos, stub = plan_reconnect(sch, lm, "C1", "2", "GNDD")

        assert pos is not None
        # Should plan to add a power symbol (and wire)
        assert any(a.kind == "add_power" and "GNDD" in a.description for a in planned)
        # No removal actions since there's nothing to remove
        assert not any(a.kind.startswith("remove") for a in planned)

    def test_execute_no_existing_connection(self, tmp_path: Path):
        sch = _load_from_string(self.SCHEMATIC_NO_WIRE, tmp_path)
        lm = _make_lib_manager(sch)

        _, pos, stub = plan_reconnect(sch, lm, "C1", "2", "GNDD")
        assert pos is not None and stub is not None

        execute_reconnect(sch, pos, stub, "GNDD")

        gndd_symbols = [s for s in sch.symbols if s.lib_id == "power:GNDD"]
        assert len(gndd_symbols) == 1


class TestDryRun:
    """--dry-run reports actions without modifying."""

    def test_dry_run_no_modification(self, tmp_path: Path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_POWER_STUB)
        original = sch_file.read_text()

        result = reconnect_main(
            [str(sch_file), "--ref", "C1", "--pin", "2", "--to-net", "GNDD", "--dry-run"]
        )

        assert result == 0
        assert sch_file.read_text() == original, "File should not be modified in dry-run"


class TestBackup:
    """--backup creates a timestamped copy."""

    def test_backup_created(self, tmp_path: Path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_POWER_STUB)

        result = reconnect_main(
            [str(sch_file), "--ref", "C1", "--pin", "2", "--to-net", "GNDD", "--backup"]
        )

        assert result == 0
        backups = list(tmp_path.glob("test.kicad_sch.backup-*"))
        assert len(backups) == 1


class TestErrorCases:
    """Error handling for invalid ref/pin."""

    def test_invalid_ref(self, tmp_path: Path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_POWER_STUB)

        result = reconnect_main([str(sch_file), "--ref", "C999", "--pin", "2", "--to-net", "GNDD"])
        assert result == 1

    def test_invalid_pin(self, tmp_path: Path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_POWER_STUB)

        result = reconnect_main([str(sch_file), "--ref", "C1", "--pin", "99", "--to-net", "GNDD"])
        assert result == 1

    def test_missing_schematic(self, tmp_path: Path):
        result = reconnect_main(
            [
                str(tmp_path / "nonexistent.kicad_sch"),
                "--ref",
                "C1",
                "--pin",
                "2",
                "--to-net",
                "GNDD",
            ]
        )
        assert result == 1


class TestOrphanedPowerSymbolRemoval:
    """Orphaned power symbols are removed when the old connection is a stub."""

    def test_orphaned_power_removed(self, tmp_path: Path):
        sch = _load_from_string(SCHEMATIC_POWER_STUB, tmp_path)
        lm = _make_lib_manager(sch)

        # Before: 1 GNDA symbol, 1 C1 symbol
        gnda_before = [s for s in sch.symbols if s.lib_id == "power:GNDA"]
        assert len(gnda_before) == 1

        _, pos, stub = plan_reconnect(sch, lm, "C1", "2", "GNDD")
        assert pos is not None and stub is not None
        assert stub.is_stub is True

        execute_reconnect(sch, pos, stub, "GNDD")

        gnda_after = [s for s in sch.symbols if s.lib_id == "power:GNDA"]
        assert len(gnda_after) == 0, "Orphaned GNDA power symbol should be removed"


class TestSharedBusPreservation:
    """Wires to other pins are preserved when connection is not a stub."""

    # Schematic where pin 2 of C1 connects via a wire that also connects
    # to another component pin (C2 pin 2), making it a shared bus.
    # The GNDA power symbol is at the far end but wires should be kept.
    SCHEMATIC_SHARED_BUS = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:C"
      (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:C_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GNDA"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GNDA" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:GNDA_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "GNDA" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GNDD"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GNDD" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:GNDD_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "GNDD" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "Device:C") (at 100 80 0) (unit 1)
    (in_bom yes) (on_board yes)
    (uuid "11111111-1111-1111-1111-111111111111")
    (property "Reference" "C1" (at 100 75 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 100 77 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "pin-c1-1"))
    (pin "2" (uuid "pin-c1-2"))
    (instances
      (project "test_project"
        (path "/" (reference "C1") (unit 1))
      )
    )
  )
  (symbol (lib_id "Device:C") (at 110 80 0) (unit 1)
    (in_bom yes) (on_board yes)
    (uuid "33333333-3333-3333-3333-333333333333")
    (property "Reference" "C2" (at 110 75 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 110 77 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "pin-c2-1"))
    (pin "2" (uuid "pin-c2-2"))
    (instances
      (project "test_project"
        (path "/" (reference "C2") (unit 1))
      )
    )
  )
  (symbol (lib_id "power:GNDA") (at 105 93.98 0) (unit 1)
    (in_bom no) (on_board no)
    (uuid "22222222-2222-2222-2222-222222222222")
    (property "Reference" "#PWR01" (at 105 93.98 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "GNDA" (at 105 96.52 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "pin-pwr-1"))
    (instances
      (project "test_project"
        (path "/" (reference "#PWR01") (unit 1))
      )
    )
  )
  (wire (pts (xy 100 83.82) (xy 100 88.9))
    (stroke (width 0) (type default))
    (uuid "wire-1")
  )
  (wire (pts (xy 100 88.9) (xy 105 88.9))
    (stroke (width 0) (type default))
    (uuid "wire-2a")
  )
  (wire (pts (xy 105 88.9) (xy 110 88.9))
    (stroke (width 0) (type default))
    (uuid "wire-2b")
  )
  (wire (pts (xy 110 83.82) (xy 110 88.9))
    (stroke (width 0) (type default))
    (uuid "wire-3")
  )
  (wire (pts (xy 105 88.9) (xy 105 93.98))
    (stroke (width 0) (type default))
    (uuid "wire-4")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

    def test_shared_bus_wires_preserved(self, tmp_path: Path):
        sch = _load_from_string(self.SCHEMATIC_SHARED_BUS, tmp_path)
        lm = _make_lib_manager(sch)

        _, pos, stub = plan_reconnect(sch, lm, "C1", "2", "GNDD")
        assert pos is not None and stub is not None

        # With 3+ wires meeting at junction point (105, 88.9), this is
        # not a simple stub -- wires should be preserved.
        assert stub.is_stub is False

        execute_reconnect(sch, pos, stub, "GNDD")

        # GNDA removed
        gnda_after = [s for s in sch.symbols if s.lib_id == "power:GNDA"]
        assert len(gnda_after) == 0

        # GNDD placed
        gndd_after = [s for s in sch.symbols if s.lib_id == "power:GNDD"]
        assert len(gndd_after) == 1

        # Wires should still exist (not removed in shared bus mode)
        assert len(sch.wires) >= 4, "Shared bus wires should be preserved"


class TestLibraryEmbedding:
    """Target power symbol library definition is embedded if not present."""

    SCHEMATIC_NO_GNDD = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:C"
      (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:C_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GNDA"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GNDA" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:GNDA_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "GNDA" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "Device:C") (at 100 80 0) (unit 1)
    (in_bom yes) (on_board yes)
    (uuid "11111111-1111-1111-1111-111111111111")
    (property "Reference" "C1" (at 100 75 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 100 77 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "pin-c1-1"))
    (pin "2" (uuid "pin-c1-2"))
    (instances
      (project "test_project"
        (path "/" (reference "C1") (unit 1))
      )
    )
  )
  (symbol (lib_id "power:GNDA") (at 100 88.9 0) (unit 1)
    (in_bom no) (on_board no)
    (uuid "22222222-2222-2222-2222-222222222222")
    (property "Reference" "#PWR01" (at 100 88.9 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "GNDA" (at 100 91.44 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "pin-pwr-1"))
    (instances
      (project "test_project"
        (path "/" (reference "#PWR01") (unit 1))
      )
    )
  )
  (wire (pts (xy 100 83.82) (xy 100 88.9))
    (stroke (width 0) (type default))
    (uuid "wire-stub-1")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

    def test_auto_embed_power_symbol(self, tmp_path: Path):
        sch = _load_from_string(self.SCHEMATIC_NO_GNDD, tmp_path)
        lm = _make_lib_manager(sch)

        # GNDD not in lib_symbols
        assert sch.get_lib_symbol("power:GNDD") is None

        planned, pos, stub = plan_reconnect(sch, lm, "C1", "2", "GNDD")
        assert any(a.kind == "embed" for a in planned)

        assert pos is not None and stub is not None
        execute_reconnect(sch, pos, stub, "GNDD")

        # After execution, GNDD should be embedded
        assert sch.get_lib_symbol("power:GNDD") is not None


class TestMultiWireStub:
    """Multiple wires in the stub chain are all removed."""

    SCHEMATIC_MULTI_WIRE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:C"
      (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:C_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GNDA"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GNDA" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:GNDA_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "GNDA" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GNDD"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GNDD" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:GNDD_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "GNDD" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "Device:C") (at 100 80 0) (unit 1)
    (in_bom yes) (on_board yes)
    (uuid "11111111-1111-1111-1111-111111111111")
    (property "Reference" "C1" (at 100 75 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 100 77 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "pin-c1-1"))
    (pin "2" (uuid "pin-c1-2"))
    (instances
      (project "test_project"
        (path "/" (reference "C1") (unit 1))
      )
    )
  )
  (symbol (lib_id "power:GNDA") (at 100 93.98 0) (unit 1)
    (in_bom no) (on_board no)
    (uuid "22222222-2222-2222-2222-222222222222")
    (property "Reference" "#PWR01" (at 100 93.98 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "GNDA" (at 100 96.52 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "pin-pwr-1"))
    (instances
      (project "test_project"
        (path "/" (reference "#PWR01") (unit 1))
      )
    )
  )
  (wire (pts (xy 100 83.82) (xy 100 88.9))
    (stroke (width 0) (type default))
    (uuid "wire-1")
  )
  (wire (pts (xy 100 88.9) (xy 100 93.98))
    (stroke (width 0) (type default))
    (uuid "wire-2")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

    def test_multi_wire_all_removed(self, tmp_path: Path):
        sch = _load_from_string(self.SCHEMATIC_MULTI_WIRE, tmp_path)
        lm = _make_lib_manager(sch)

        assert len(sch.wires) == 2

        _, pos, stub = plan_reconnect(sch, lm, "C1", "2", "GNDD")
        assert pos is not None and stub is not None
        assert len(stub.wires) == 2
        assert stub.is_stub is True

        execute_reconnect(sch, pos, stub, "GNDD")

        # Old wires removed, new wire added
        gnda_symbols = [s for s in sch.symbols if s.lib_id == "power:GNDA"]
        assert len(gnda_symbols) == 0
        gndd_symbols = [s for s in sch.symbols if s.lib_id == "power:GNDD"]
        assert len(gndd_symbols) == 1


class TestCLIIntegration:
    """Full CLI round-trip via main()."""

    def test_cli_reconnect_and_verify(self, tmp_path: Path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_POWER_STUB)

        result = reconnect_main([str(sch_file), "--ref", "C1", "--pin", "2", "--to-net", "GNDD"])
        assert result == 0

        # Reload and verify
        sch = Schematic.load(sch_file)
        gnda = [s for s in sch.symbols if s.lib_id == "power:GNDA"]
        gndd = [s for s in sch.symbols if s.lib_id == "power:GNDD"]
        assert len(gnda) == 0
        assert len(gndd) == 1

    def test_cli_dry_run_returns_zero(self, tmp_path: Path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_POWER_STUB)

        result = reconnect_main(
            [str(sch_file), "--ref", "C1", "--pin", "2", "--to-net", "GNDD", "--dry-run"]
        )
        assert result == 0


class TestHelpers:
    """Unit tests for helper functions."""

    def test_is_power_net_with_lib_symbol(self, tmp_path: Path):
        sch = _load_from_string(SCHEMATIC_POWER_STUB, tmp_path)
        assert _is_power_net("GNDA", sch) is True
        assert _is_power_net("GNDD", sch) is True

    def test_is_power_net_heuristic(self, tmp_path: Path):
        sch = _load_from_string(SCHEMATIC_POWER_STUB, tmp_path)
        assert _is_power_net("+3V3", sch) is True
        assert _is_power_net("VCC", sch) is True
        assert _is_power_net("SIG_GND", sch) is False

    def test_make_power_lib_sym(self):
        sym = _make_power_lib_sym("GNDD")
        assert sym.name == "power:GNDD"
        assert len(sym.pins) == 1
        assert sym.pins[0].type == "power_in"
