"""Tests for the sch add-pull-resistor command.

Covers pull-up and pull-down placement, --dry-run, --backup, auto-reference,
explicit reference, collision detection, power net defaults, and error cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.sch_add_pull_resistor import (
    _check_wire_path_crossings,
    _segments_intersect,
    _snap,
)
from kicad_tools.cli.sch_add_pull_resistor import (
    main as add_pull_main,
)
from kicad_tools.schema import Schematic

# ---------------------------------------------------------------------------
# Minimal schematic with an IC symbol (2-pin for simplicity) and Device:R + power:GND
# ---------------------------------------------------------------------------

# A minimal IC with two pins: pin "1" at (0, -2.54) direction 180 and
# pin "2" at (0, 2.54) direction 0.  Placed at (100, 80) in the schematic.
SCHEMATIC_WITH_IC = """\
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
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:R_0_1"
        (polyline (pts (xy -1.016 -2.54) (xy -1.016 2.54)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "Device:R_1_1"
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
    (symbol "power:+3.3V"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "+3.3V" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:+3.3V_0_1"
        (polyline (pts (xy 0 0) (xy 0 1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "power:+3.3V_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "+3.3V" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "TestLib:IC2"
      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "IC2" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "TestLib:IC2_1_1"
        (pin input line (at -5.08 0 0) (length 2.54) (name "IN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin output line (at 5.08 0 180) (length 2.54) (name "OUT" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
        (pin input line (at 0 5.08 270) (length 2.54) (name "VCC" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
        (pin input line (at 0 -5.08 90) (length 2.54) (name "GND" (effects (font (size 1.27 1.27)))) (number "4" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "TestLib:IC2") (at 100 80 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "ic2-uuid-0001")
    (property "Reference" "U1" (at 100 74 0) (effects (font (size 1.27 1.27))))
    (property "Value" "IC2" (at 100 76 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 80 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "" (at 100 80 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (instances
      (project "test"
        (path "/" (reference "U1") (unit 1))
      )
    )
    (pin "1" (uuid "pin1-uuid"))
    (pin "2" (uuid "pin2-uuid"))
    (pin "3" (uuid "pin3-uuid"))
    (pin "4" (uuid "pin4-uuid"))
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


def _write_sch(tmp_path: Path, content: str = SCHEMATIC_WITH_IC) -> Path:
    p = tmp_path / "test_pull.kicad_sch"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# _snap
# ---------------------------------------------------------------------------


class TestSnap:
    def test_snap_on_grid(self):
        assert _snap(5.08) == pytest.approx(5.08, abs=0.01)

    def test_snap_off_grid(self):
        assert _snap(5.0) == pytest.approx(5.08, abs=0.02)


# ---------------------------------------------------------------------------
# Pull-up basic
# ---------------------------------------------------------------------------


class TestPullUpBasic:
    def test_pull_up_places_symbols_and_wires(self, tmp_path):
        sch_path = _write_sch(tmp_path)

        # Use pin 3 (VCC pin at top of IC) for pull-up test
        # Pin 3 is at local (0, 5.08) with direction 270, length 2.54
        # So absolute pin position = IC pos (100, 80) + pin offset
        rc = add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "3",
                "--direction",
                "up",
                "--value",
                "10k",
            ]
        )
        assert rc == 0

        # Reload and check that new symbols were added
        sch = Schematic.load(sch_path)
        # Should have U1 (original) + new R? resistor + power symbol
        refs = [s.reference for s in sch.symbols]
        assert "U1" in refs
        # Check a resistor was added
        r_refs = [r for r in refs if r.startswith("R") or r == "R?"]
        assert len(r_refs) >= 1

        # Check power symbol was added (lib_id starts with "power:")
        power_syms = [s for s in sch.symbols if s.lib_id.startswith("power:")]
        assert len(power_syms) >= 1

        # Check wires were added (should have at least 1 wire)
        assert len(sch.wires) >= 1


# ---------------------------------------------------------------------------
# Pull-down basic
# ---------------------------------------------------------------------------


class TestPullDownBasic:
    def test_pull_down_places_symbols_and_wires(self, tmp_path):
        sch_path = _write_sch(tmp_path)

        rc = add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "4",
                "--direction",
                "down",
                "--value",
                "10k",
            ]
        )
        assert rc == 0

        sch = Schematic.load(sch_path)
        refs = [s.reference for s in sch.symbols]
        assert "U1" in refs

        # Resistor added
        r_refs = [r for r in refs if r.startswith("R") or r == "R?"]
        assert len(r_refs) >= 1

        # GND power symbol added
        power_syms = [s for s in sch.symbols if s.lib_id == "power:GND"]
        assert len(power_syms) >= 1


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_no_changes(self, tmp_path):
        sch_path = _write_sch(tmp_path)
        original = sch_path.read_text()

        rc = add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "3",
                "--direction",
                "up",
                "--value",
                "10k",
                "--dry-run",
            ]
        )
        assert rc == 0

        # File should be unchanged
        assert sch_path.read_text() == original

    def test_dry_run_output(self, tmp_path, capsys):
        sch_path = _write_sch(tmp_path)

        add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "3",
                "--direction",
                "up",
                "--value",
                "10k",
                "--dry-run",
            ]
        )

        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "Planned actions" in captured.out
        assert "[symbol]" in captured.out
        assert "[power]" in captured.out
        assert "[wire]" in captured.out


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


class TestBackup:
    def test_backup_created(self, tmp_path):
        sch_path = _write_sch(tmp_path)

        rc = add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "3",
                "--direction",
                "up",
                "--value",
                "10k",
                "--backup",
            ]
        )
        assert rc == 0

        # Check backup file exists
        backups = list(tmp_path.glob("*.backup-*"))
        assert len(backups) == 1


# ---------------------------------------------------------------------------
# Auto reference
# ---------------------------------------------------------------------------


class TestAutoReference:
    def test_auto_reference_no_collision(self, tmp_path):
        sch_path = _write_sch(tmp_path)

        rc = add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "3",
                "--direction",
                "up",
                "--value",
                "10k",
            ]
        )
        assert rc == 0

        sch = Schematic.load(sch_path)
        # No existing R<n> refs, so auto-assign yields R1
        r_syms = [s for s in sch.symbols if s.lib_id == "Device:R"]
        assert len(r_syms) == 1
        assert r_syms[0].reference == "R1"


# ---------------------------------------------------------------------------
# Explicit reference
# ---------------------------------------------------------------------------


class TestExplicitReference:
    def test_explicit_reference(self, tmp_path):
        sch_path = _write_sch(tmp_path)

        rc = add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "3",
                "--direction",
                "up",
                "--value",
                "10k",
                "--reference",
                "R99",
            ]
        )
        assert rc == 0

        sch = Schematic.load(sch_path)
        r_syms = [s for s in sch.symbols if s.lib_id == "Device:R"]
        assert len(r_syms) == 1
        assert r_syms[0].reference == "R99"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrors:
    def test_pin_not_found_error(self, tmp_path):
        sch_path = _write_sch(tmp_path)

        rc = add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "99",
                "--direction",
                "up",
                "--value",
                "10k",
            ]
        )
        assert rc == 1

    def test_ref_not_found_error(self, tmp_path):
        sch_path = _write_sch(tmp_path)

        rc = add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U99",
                "--pin",
                "1",
                "--direction",
                "up",
                "--value",
                "10k",
            ]
        )
        assert rc == 1

    def test_ref_not_found_message(self, tmp_path, capsys):
        sch_path = _write_sch(tmp_path)

        add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U99",
                "--pin",
                "1",
                "--direction",
                "up",
                "--value",
                "10k",
            ]
        )

        captured = capsys.readouterr()
        assert "U99" in captured.err
        assert "not found" in captured.err

    def test_pin_not_found_message(self, tmp_path, capsys):
        sch_path = _write_sch(tmp_path)

        add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "99",
                "--direction",
                "up",
                "--value",
                "10k",
            ]
        )

        captured = capsys.readouterr()
        assert "99" in captured.err
        assert "not found" in captured.err


# ---------------------------------------------------------------------------
# Collision warning
# ---------------------------------------------------------------------------


class TestCollisionWarning:
    def test_collision_warning(self, tmp_path, capsys):
        """Place a pull resistor near an existing symbol to trigger collision warning."""
        sch_path = _write_sch(tmp_path)

        # Pin 3 (VCC) is at local (0, 5.08); after Y-negation the schematic
        # position is (100, 74.92).  With direction "down" and offset=2.0 the
        # resistor center lands at approximately (100, 77.47) which is within
        # the default 2.54mm tolerance of U1 at (100, 80) — collision expected.
        rc = add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "3",
                "--direction",
                "down",
                "--value",
                "10k",
                "--offset",
                "2.0",
            ]
        )

        captured = capsys.readouterr()
        assert rc == 0
        assert "Warning" in captured.err


# ---------------------------------------------------------------------------
# Power net defaults
# ---------------------------------------------------------------------------


class TestPowerNetDefaults:
    def test_power_net_default_up(self, tmp_path, capsys):
        sch_path = _write_sch(tmp_path)

        add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "3",
                "--direction",
                "up",
                "--value",
                "10k",
                "--dry-run",
            ]
        )

        captured = capsys.readouterr()
        assert "+3.3V" in captured.out

    def test_power_net_default_down(self, tmp_path, capsys):
        sch_path = _write_sch(tmp_path)

        add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "4",
                "--direction",
                "down",
                "--value",
                "10k",
                "--dry-run",
            ]
        )

        captured = capsys.readouterr()
        assert "GND" in captured.out


# ---------------------------------------------------------------------------
# Offset override
# ---------------------------------------------------------------------------


class TestOffsetOverride:
    def test_offset_changes_placement(self, tmp_path, capsys):
        sch_path = _write_sch(tmp_path)

        rc = add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "3",
                "--direction",
                "up",
                "--value",
                "10k",
                "--offset",
                "10.16",
                "--dry-run",
            ]
        )

        captured = capsys.readouterr()
        assert rc == 0
        assert "Planned actions" in captured.out


# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------


class TestOutputFormat:
    def test_output_matches_add_commands(self, tmp_path, capsys):
        """Output format should match other sch add-* commands."""
        sch_path = _write_sch(tmp_path)

        add_pull_main(
            [
                str(sch_path),
                "--ref",
                "U1",
                "--pin",
                "3",
                "--direction",
                "up",
                "--value",
                "10k",
                "--dry-run",
            ]
        )

        captured = capsys.readouterr()
        # Should have "Planned actions (N):" header
        assert "Planned actions (" in captured.out
        # Should have [kind] description lines
        assert "[symbol]" in captured.out
        assert "[power]" in captured.out
        assert "[wire]" in captured.out


# ---------------------------------------------------------------------------
# Wire crossing detection helpers
# ---------------------------------------------------------------------------


class TestSegmentsIntersect:
    def test_crossing_segments(self):
        assert _segments_intersect((0, 0), (10, 0), (5, -5), (5, 5)) is True

    def test_parallel_no_cross(self):
        assert _segments_intersect((0, 0), (10, 0), (0, 1), (10, 1)) is False

    def test_t_junction_excluded(self):
        # Endpoint of segment 2 touches endpoint of segment 1 -- not a crossing
        assert _segments_intersect((0, 0), (10, 0), (10, 0), (10, 5)) is False

    def test_shared_endpoint_excluded(self):
        assert _segments_intersect((0, 0), (5, 5), (5, 5), (10, 0)) is False

    def test_no_overlap(self):
        assert _segments_intersect((0, 0), (1, 0), (3, 0), (5, 0)) is False


# ---------------------------------------------------------------------------
# Schematic with a horizontal wire crossing the pull-up path
# ---------------------------------------------------------------------------

# IC at (100, 80) with pin 3 (VCC) at local (0, 5.08) dir 270.
# After Y-negation the pin is at (100, 74.92).
# With direction "up" and default offset 5.08, the resistor center is
# at y ~ 69.85.  The wire from the IC pin goes from y=74.92 to y ~73.66
# (resistor pin 1).
#
# We place a horizontal wire at y=73.0 crossing x=100 to create a crossing.
SCHEMATIC_WITH_CROSSING_WIRE = """\
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
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:R_0_1"
        (polyline (pts (xy -1.016 -2.54) (xy -1.016 2.54)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "Device:R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:+3.3V"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "+3.3V" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:+3.3V_0_1"
        (polyline (pts (xy 0 0) (xy 0 1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "power:+3.3V_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "+3.3V" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
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
    (symbol "TestLib:IC2"
      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "IC2" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "TestLib:IC2_1_1"
        (pin input line (at -5.08 0 0) (length 2.54) (name "IN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin output line (at 5.08 0 180) (length 2.54) (name "OUT" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
        (pin input line (at 0 5.08 270) (length 2.54) (name "VCC" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
        (pin input line (at 0 -5.08 90) (length 2.54) (name "GND" (effects (font (size 1.27 1.27)))) (number "4" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (wire (pts (xy 98 74.3) (xy 102 74.3)) (stroke (width 0) (type default)) (uuid "crossing-wire-uuid"))
  (symbol (lib_id "TestLib:IC2") (at 100 80 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "ic2-uuid-0001")
    (property "Reference" "U1" (at 100 74 0) (effects (font (size 1.27 1.27))))
    (property "Value" "IC2" (at 100 76 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 80 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "" (at 100 80 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (instances
      (project "test"
        (path "/" (reference "U1") (unit 1))
      )
    )
    (pin "1" (uuid "pin1-uuid"))
    (pin "2" (uuid "pin2-uuid"))
    (pin "3" (uuid "pin3-uuid"))
    (pin "4" (uuid "pin4-uuid"))
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

# Schematic with a global label on the planned wire path
SCHEMATIC_WITH_LABEL_ON_PATH = """\
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
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:R_0_1"
        (polyline (pts (xy -1.016 -2.54) (xy -1.016 2.54)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "Device:R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:+3.3V"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "+3.3V" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:+3.3V_0_1"
        (polyline (pts (xy 0 0) (xy 0 1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "power:+3.3V_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "+3.3V" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
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
    (symbol "TestLib:IC2"
      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "IC2" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "TestLib:IC2_1_1"
        (pin input line (at -5.08 0 0) (length 2.54) (name "IN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin output line (at 5.08 0 180) (length 2.54) (name "OUT" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
        (pin input line (at 0 5.08 270) (length 2.54) (name "VCC" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
        (pin input line (at 0 -5.08 90) (length 2.54) (name "GND" (effects (font (size 1.27 1.27)))) (number "4" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (global_label "DAC_CLK" (at 100 73 0) (effects (font (size 1.27 1.27)) (justify left)) (uuid "glabel-uuid-001")
    (property "Intersheetrefs" "${INTERSHEET_REFS}" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
  )
  (symbol (lib_id "TestLib:IC2") (at 100 80 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "ic2-uuid-0001")
    (property "Reference" "U1" (at 100 74 0) (effects (font (size 1.27 1.27))))
    (property "Value" "IC2" (at 100 76 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 80 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "" (at 100 80 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (instances
      (project "test"
        (path "/" (reference "U1") (unit 1))
      )
    )
    (pin "1" (uuid "pin1-uuid"))
    (pin "2" (uuid "pin2-uuid"))
    (pin "3" (uuid "pin3-uuid"))
    (pin "4" (uuid "pin4-uuid"))
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


# ---------------------------------------------------------------------------
# Wire crossing detection tests
# ---------------------------------------------------------------------------


class TestWireCrossingDetection:
    def test_crossing_wire_detected(self, tmp_path):
        """A horizontal wire crossing the planned vertical path is detected."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_WITH_CROSSING_WIRE)
        sch = Schematic.load(sch_path)

        # The planned wire goes vertically through x=100, y=74.92 to y~73.66
        # The existing wire goes from (98,74.3) to (102,74.3) -- crosses at x=100
        crossings = _check_wire_path_crossings(
            sch, (100.0, 74.92), (100.0, 71.12)
        )
        assert len(crossings) >= 1
        assert "crosses existing wire" in crossings[0]

    def test_no_crossing_when_clear(self, tmp_path):
        """No crossing detected when the path is clear."""
        sch_path = _write_sch(tmp_path)  # No extra wires
        sch = Schematic.load(sch_path)

        crossings = _check_wire_path_crossings(
            sch, (100.0, 74.92), (100.0, 71.12)
        )
        assert len(crossings) == 0

    def test_global_label_on_path_detected(self, tmp_path):
        """A global label sitting on the planned wire path is detected."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_WITH_LABEL_ON_PATH)
        sch = Schematic.load(sch_path)

        # Vertical wire through (100, 74.92) to (100, 71.12) passes
        # through the global label at (100, 73)
        crossings = _check_wire_path_crossings(
            sch, (100.0, 74.92), (100.0, 71.12)
        )
        assert len(crossings) >= 1
        assert "global label" in crossings[0].lower()


# ---------------------------------------------------------------------------
# L-shaped reroute tests
# ---------------------------------------------------------------------------


class TestLShapedReroute:
    def test_reroute_avoids_crossing(self, tmp_path, capsys):
        """When a crossing wire exists, the command reroutes via L-shape."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_WITH_CROSSING_WIRE)

        rc = add_pull_main(
            [
                str(sch_path),
                "--ref", "U1",
                "--pin", "3",
                "--direction", "up",
                "--value", "10k",
            ]
        )
        assert rc == 0

        captured = capsys.readouterr()
        assert "Rerouted" in captured.err or "L-shape" in captured.err

        # The resulting schematic should have wires
        sch = Schematic.load(sch_path)
        assert len(sch.wires) >= 3  # original + at least 2 L-shape segments + power wire

    def test_force_bypasses_crossing_check(self, tmp_path, capsys):
        """With --force, a straight wire is placed despite crossings."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_WITH_CROSSING_WIRE)

        rc = add_pull_main(
            [
                str(sch_path),
                "--ref", "U1",
                "--pin", "3",
                "--direction", "up",
                "--value", "10k",
                "--force",
            ]
        )
        assert rc == 0

        captured = capsys.readouterr()
        # Should NOT reroute
        assert "Rerouted" not in captured.err
        assert "L-shape" not in captured.err

    def test_no_crossing_straight_wire(self, tmp_path, capsys):
        """Without crossings, a straight wire is placed (no regression)."""
        sch_path = _write_sch(tmp_path)

        rc = add_pull_main(
            [
                str(sch_path),
                "--ref", "U1",
                "--pin", "3",
                "--direction", "up",
                "--value", "10k",
            ]
        )
        assert rc == 0

        captured = capsys.readouterr()
        assert "Rerouted" not in captured.err
        assert "L-shape" not in captured.err

        sch = Schematic.load(sch_path)
        # Original schematic has no wires, so all wires are ours
        # Straight route: 1 IC-to-R wire + 1 R-to-power wire = 2
        # (one may be zero-length and skipped)
        assert len(sch.wires) >= 1
