"""Tests for the sch add-wire command.

Covers single wire placement, multi-segment wires, grid snapping,
junction auto-insertion, --dry-run, --backup, round-trip, and error cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.sch_add_wire import (
    _point_on_wire_midpoint,
    _snap,
)
from kicad_tools.cli.sch_add_wire import (
    main as add_wire_main,
)
from kicad_tools.schema import Schematic

# ---------------------------------------------------------------------------
# Minimal schematic content for testing
# ---------------------------------------------------------------------------

MINIMAL_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
  )
  (wire (pts (xy 100 50) (xy 150 50))
    (stroke (width 0) (type default))
    (uuid "wire-1")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

# Schematic with a wire on the 1.27mm grid for junction testing.
# Wire from (100.33, 50.8) to (149.86, 50.8) -- these are on-grid.
SCHEMATIC_ON_GRID = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
  )
  (wire (pts (xy 100.33 50.8) (xy 149.86 50.8))
    (stroke (width 0) (type default))
    (uuid "wire-grid-1")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

EMPTY_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


def _write_sch(tmp_path: Path, content: str = MINIMAL_SCHEMATIC) -> Path:
    p = tmp_path / "test_add_wire.kicad_sch"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


class TestUtilityFunctions:
    def test_snap(self):
        # round(100/1.27) = round(78.74) = 79, 79 * 1.27 = 100.33
        assert _snap(100.0) == pytest.approx(100.33, abs=0.01)
        assert _snap(100.33) == pytest.approx(100.33, abs=0.01)
        assert _snap(2.54) == pytest.approx(2.54, abs=0.01)

    def test_point_on_wire_midpoint(self):
        # Point at midpoint of horizontal wire
        assert _point_on_wire_midpoint((125, 50), (100, 50), (150, 50)) is True
        # Point at endpoint
        assert _point_on_wire_midpoint((100, 50), (100, 50), (150, 50)) is False
        # Point off the wire
        assert _point_on_wire_midpoint((125, 60), (100, 50), (150, 50)) is False


# ---------------------------------------------------------------------------
# Single wire
# ---------------------------------------------------------------------------


class TestSingleWire:
    def test_add_single_wire(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        sch_before = Schematic.load(sch_path)
        original_wire_count = len(sch_before.wires)

        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "100.33",
                "50.8",
                "--to",
                "120.65",
                "50.8",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        assert len(sch.wires) == original_wire_count + 1
        new_wire = sch.wires[-1]
        assert new_wire.start[0] == pytest.approx(100.33, abs=0.02)
        assert new_wire.start[1] == pytest.approx(50.8, abs=0.02)
        assert new_wire.end[0] == pytest.approx(120.65, abs=0.02)
        assert new_wire.end[1] == pytest.approx(50.8, abs=0.02)


# ---------------------------------------------------------------------------
# Multi-segment wires
# ---------------------------------------------------------------------------


class TestMultiSegment:
    def test_two_segment_wire(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        sch_before = Schematic.load(sch_path)
        original_wire_count = len(sch_before.wires)

        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "100.33",
                "50.8",
                "--to",
                "120.65",
                "50.8",
                "--to",
                "120.65",
                "80.01",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        assert len(sch.wires) == original_wire_count + 2

    def test_three_segment_wire(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        sch_before = Schematic.load(sch_path)
        original_wire_count = len(sch_before.wires)

        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "100.33",
                "50.8",
                "--to",
                "120.65",
                "50.8",
                "--to",
                "120.65",
                "80.01",
                "--to",
                "140.97",
                "80.01",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        assert len(sch.wires) == original_wire_count + 3


# ---------------------------------------------------------------------------
# Grid snapping
# ---------------------------------------------------------------------------


class TestGridSnap:
    def test_coordinates_snapped_to_grid(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)

        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "100",
                "50",
                "--to",
                "120",
                "50",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        new_wire = sch.wires[-1]
        # 100 -> snapped to 100.33, 50 -> snapped to 49.53
        assert new_wire.start[0] == pytest.approx(_snap(100), abs=0.02)
        assert new_wire.start[1] == pytest.approx(_snap(50), abs=0.02)
        assert new_wire.end[0] == pytest.approx(_snap(120), abs=0.02)
        assert new_wire.end[1] == pytest.approx(_snap(50), abs=0.02)


# ---------------------------------------------------------------------------
# Junction auto-insert
# ---------------------------------------------------------------------------


class TestJunction:
    def test_junction_auto_insert(self, tmp_path: Path):
        """When --junction is passed and an endpoint lands on an existing wire
        midpoint, a junction should be created."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_ON_GRID)
        sch_before = Schematic.load(sch_path)
        original_junc_count = len(sch_before.junctions)

        # Existing wire goes from (100.33, 50.8) to (149.86, 50.8)
        # Use 125.73 which snaps to 125.73 (99*1.27) and is on the wire midpoint
        # 50.8 snaps to 50.8 (40*1.27) matching the wire y coordinate
        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "125.73",
                "50.8",
                "--to",
                "125.73",
                "80.01",
                "--junction",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        assert len(sch.junctions) >= original_junc_count + 1

    def test_no_junction_without_flag(self, tmp_path: Path):
        """Without --junction, no junction is added even if wires cross."""
        sch_path = _write_sch(tmp_path)
        sch_before = Schematic.load(sch_path)
        original_junc_count = len(sch_before.junctions)

        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "125",
                "50",
                "--to",
                "125",
                "80.01",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        assert len(sch.junctions) == original_junc_count

    def test_junction_at_endpoint_not_midpoint(self, tmp_path: Path):
        """Junction should NOT be added when endpoint matches a wire endpoint
        (not midpoint)."""
        sch_path = _write_sch(tmp_path, SCHEMATIC_ON_GRID)
        sch_before = Schematic.load(sch_path)
        original_junc_count = len(sch_before.junctions)

        # Wire endpoint is at (100.33, 50.8) -- not a midpoint
        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "100.33",
                "50.8",
                "--to",
                "100.33",
                "80.01",
                "--junction",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        assert len(sch.junctions) == original_junc_count


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_no_changes(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        original_content = sch_path.read_text()

        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "100.33",
                "50.8",
                "--to",
                "120.65",
                "50.8",
                "--dry-run",
            ]
        )
        assert result == 0

        assert sch_path.read_text() == original_content

    def test_dry_run_with_junction(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path, SCHEMATIC_ON_GRID)
        original_content = sch_path.read_text()

        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "125.73",
                "50.8",
                "--to",
                "125.73",
                "80.01",
                "--junction",
                "--dry-run",
            ]
        )
        assert result == 0

        assert sch_path.read_text() == original_content


# ---------------------------------------------------------------------------
# --backup
# ---------------------------------------------------------------------------


class TestBackup:
    def test_backup_creates_file(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)

        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "100.33",
                "50.8",
                "--to",
                "120.65",
                "50.8",
                "--backup",
            ]
        )
        assert result == 0

        backup_files = list(tmp_path.glob("*.backup-*"))
        assert len(backup_files) == 1


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrors:
    def test_schematic_not_found(self, tmp_path: Path):
        result = add_wire_main(
            [
                str(tmp_path / "nonexistent.kicad_sch"),
                "--from",
                "100",
                "50",
                "--to",
                "120",
                "50",
            ]
        )
        assert result == 1

    def test_zero_length_wire_skipped(self, tmp_path: Path, capsys):
        """A zero-length wire (same from and to after snapping) should be
        handled gracefully."""
        sch_path = _write_sch(tmp_path)
        sch_before = Schematic.load(sch_path)
        original_wire_count = len(sch_before.wires)

        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "100.33",
                "50.8",
                "--to",
                "100.33",
                "50.8",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        # No new wires added since the segment was zero-length
        assert len(sch.wires) == original_wire_count

        captured = capsys.readouterr()
        assert "Zero-length" in captured.err or "No wire segments" in captured.out


# ---------------------------------------------------------------------------
# Round-trip: save then reload
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_add_and_reload_preserves_content(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        sch_before = Schematic.load(sch_path)
        original_wire_count = len(sch_before.wires)

        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "100.33",
                "50.8",
                "--to",
                "120.65",
                "50.8",
            ]
        )
        assert result == 0

        sch_after = Schematic.load(sch_path)
        assert len(sch_after.wires) == original_wire_count + 1

        # Verify the new wire has correct S-expression structure
        new_wire = sch_after.wires[-1]
        assert new_wire.uuid  # UUID was generated
        assert new_wire.start[0] == pytest.approx(100.33, abs=0.02)
        assert new_wire.end[0] == pytest.approx(120.65, abs=0.02)

    def test_multi_segment_round_trip(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)

        result = add_wire_main(
            [
                str(sch_path),
                "--from",
                "100.33",
                "50.8",
                "--to",
                "120.65",
                "50.8",
                "--to",
                "120.65",
                "80.01",
            ]
        )
        assert result == 0

        # Reload and re-save
        sch = Schematic.load(sch_path)
        out_path = tmp_path / "round_trip.kicad_sch"
        sch.save(out_path)

        sch2 = Schematic.load(out_path)
        # Original wire + 2 new wires
        assert len(sch2.wires) == 3
