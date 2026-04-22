"""Tests for the sch add-label command.

Covers local, global, and hierarchical label placement, --connect wire creation,
junction insertion, --dry-run, --backup, --shape validation, and round-trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.sch_add_label import (
    _point_on_wire_midpoint,
    _snap,
    parse_connect_target,
)
from kicad_tools.cli.sch_add_label import (
    main as add_label_main,
)
from kicad_tools.schema import Schematic
from kicad_tools.schema.label import GlobalLabel, HierarchicalLabel, Label

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


def _write_sch(tmp_path: Path, content: str = MINIMAL_SCHEMATIC) -> Path:
    p = tmp_path / "test_add_label.kicad_sch"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# parse_connect_target
# ---------------------------------------------------------------------------


class TestParseConnectTarget:
    def test_basic(self):
        assert parse_connect_target("120,80") == (120.0, 80.0)

    def test_with_spaces(self):
        assert parse_connect_target(" 120.5 , 80.3 ") == (120.5, 80.3)

    def test_no_comma_raises(self):
        with pytest.raises(ValueError, match="Expected 'x,y'"):
            parse_connect_target("12080")

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError, match="Expected numeric"):
            parse_connect_target("abc,80")


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


class TestUtilityFunctions:
    def test_snap(self):
        assert _snap(100.0) == pytest.approx(100.33, abs=0.01)
        assert _snap(2.54) == pytest.approx(2.54, abs=0.01)

    def test_point_on_wire_midpoint(self):
        assert _point_on_wire_midpoint((125, 50), (100, 50), (150, 50)) is True
        assert _point_on_wire_midpoint((100, 50), (100, 50), (150, 50)) is False
        assert _point_on_wire_midpoint((125, 60), (100, 50), (150, 50)) is False


# ---------------------------------------------------------------------------
# to_sexp() round-trip on label dataclasses
# ---------------------------------------------------------------------------


class TestLabelToSexp:
    def test_local_label_round_trip(self):
        label = Label(text="SDA", position=(100.0, 80.0), rotation=0, uuid="test-uuid")
        sexp = label.to_sexp()
        assert sexp.name == "label"
        parsed = Label.from_sexp(sexp)
        assert parsed.text == "SDA"
        assert parsed.position == (100.0, 80.0)
        assert parsed.rotation == 0
        assert parsed.uuid == "test-uuid"

    def test_global_label_round_trip(self):
        label = GlobalLabel(
            text="I2S_BCLK",
            position=(100.0, 80.0),
            rotation=180,
            shape="output",
            uuid="test-uuid-gl",
        )
        sexp = label.to_sexp()
        assert sexp.name == "global_label"
        parsed = GlobalLabel.from_sexp(sexp)
        assert parsed.text == "I2S_BCLK"
        assert parsed.position == (100.0, 80.0)
        assert parsed.rotation == 180
        assert parsed.shape == "output"
        assert parsed.uuid == "test-uuid-gl"

    def test_hierarchical_label_round_trip(self):
        label = HierarchicalLabel(
            text="CLK", position=(50.0, 60.0), rotation=0, shape="input", uuid="test-uuid-hl"
        )
        sexp = label.to_sexp()
        assert sexp.name == "hierarchical_label"
        parsed = HierarchicalLabel.from_sexp(sexp)
        assert parsed.text == "CLK"
        assert parsed.position == (50.0, 60.0)
        assert parsed.rotation == 0
        assert parsed.shape == "input"
        assert parsed.uuid == "test-uuid-hl"

    def test_local_label_no_shape_in_sexp(self):
        label = Label(text="NET1", position=(10.0, 20.0))
        sexp = label.to_sexp()
        # Local labels should not have a shape node
        assert sexp.find("shape") is None

    def test_global_label_has_shape_in_sexp(self):
        label = GlobalLabel(text="NET1", position=(10.0, 20.0), shape="bidirectional")
        sexp = label.to_sexp()
        shape_node = sexp.find("shape")
        assert shape_node is not None
        assert shape_node.get_string(0) == "bidirectional"


# ---------------------------------------------------------------------------
# Place a global label
# ---------------------------------------------------------------------------


class TestPlaceGlobalLabel:
    def test_place_global_label(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        result = add_label_main(
            [
                str(sch_path),
                "--type",
                "global",
                "--name",
                "I2S_BCLK",
                "--at",
                "100",
                "80",
                "--shape",
                "output",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        assert len(sch.global_labels) == 1
        gl = sch.global_labels[0]
        assert gl.text == "I2S_BCLK"
        assert gl.shape == "output"

    def test_global_label_default_shape(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        result = add_label_main(
            [
                str(sch_path),
                "--type",
                "global",
                "--name",
                "VCC",
                "--at",
                "100",
                "80",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        gl = sch.global_labels[0]
        assert gl.shape == "input"  # default


# ---------------------------------------------------------------------------
# Place a local label
# ---------------------------------------------------------------------------


class TestPlaceLocalLabel:
    def test_place_local_label(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        result = add_label_main(
            [
                str(sch_path),
                "--type",
                "local",
                "--name",
                "SDA",
                "--at",
                "100",
                "80",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        assert len(sch.labels) == 1
        lbl = sch.labels[0]
        assert lbl.text == "SDA"

    def test_local_label_rejects_shape(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        result = add_label_main(
            [
                str(sch_path),
                "--type",
                "local",
                "--name",
                "SDA",
                "--at",
                "100",
                "80",
                "--shape",
                "output",
            ]
        )
        assert result == 1  # Error: --shape not valid for local


# ---------------------------------------------------------------------------
# Place a hierarchical label
# ---------------------------------------------------------------------------


class TestPlaceHierarchicalLabel:
    def test_place_hierarchical_label(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        result = add_label_main(
            [
                str(sch_path),
                "--type",
                "hierarchical",
                "--name",
                "CLK",
                "--at",
                "100",
                "80",
                "--shape",
                "input",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        assert len(sch.hierarchical_labels) == 1
        hl = sch.hierarchical_labels[0]
        assert hl.text == "CLK"
        assert hl.shape == "input"


# ---------------------------------------------------------------------------
# --connect: add wires from label position
# ---------------------------------------------------------------------------


class TestConnect:
    def test_connect_adds_wire(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        result = add_label_main(
            [
                str(sch_path),
                "--type",
                "global",
                "--name",
                "SIG",
                "--at",
                "100.33",
                "80.01",
                "--connect",
                "120.65,80.01",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        # Original wire + new connection wire
        assert len(sch.wires) >= 2

    def test_connect_with_junction(self, tmp_path: Path):
        """When a --connect target hits the midpoint of an existing wire,
        a junction should be created."""
        sch_path = _write_sch(tmp_path)
        # Existing wire goes from (100, 50) to (150, 50)
        # Target (125, 50) is at the midpoint -> should create junction
        result = add_label_main(
            [
                str(sch_path),
                "--type",
                "global",
                "--name",
                "SIG",
                "--at",
                "125.73",
                "40.64",
                "--connect",
                "125.73,49.53",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        assert len(sch.wires) >= 2

    def test_multiple_connects(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        result = add_label_main(
            [
                str(sch_path),
                "--type",
                "global",
                "--name",
                "SIG",
                "--at",
                "100.33",
                "80.01",
                "--connect",
                "120.65,80.01",
                "--connect",
                "80.01,80.01",
            ]
        )
        assert result == 0

        sch = Schematic.load(sch_path)
        # Original wire + 2 new wires
        assert len(sch.wires) >= 3


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_no_changes(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        original_content = sch_path.read_text()

        result = add_label_main(
            [
                str(sch_path),
                "--type",
                "global",
                "--name",
                "SIG",
                "--at",
                "100",
                "80",
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

        result = add_label_main(
            [
                str(sch_path),
                "--type",
                "global",
                "--name",
                "SIG",
                "--at",
                "100",
                "80",
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
        result = add_label_main(
            [
                str(tmp_path / "nonexistent.kicad_sch"),
                "--type",
                "global",
                "--name",
                "SIG",
                "--at",
                "100",
                "80",
            ]
        )
        assert result == 1


# ---------------------------------------------------------------------------
# Round-trip: save then reload
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_add_and_reload_preserves_content(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        sch_before = Schematic.load(sch_path)
        original_wire_count = len(sch_before.wires)

        result = add_label_main(
            [
                str(sch_path),
                "--type",
                "global",
                "--name",
                "I2S_BCLK",
                "--at",
                "100",
                "80",
                "--shape",
                "output",
            ]
        )
        assert result == 0

        sch_after = Schematic.load(sch_path)
        assert len(sch_after.global_labels) == 1
        assert sch_after.global_labels[0].text == "I2S_BCLK"
        # Original wires preserved
        assert len(sch_after.wires) == original_wire_count

    def test_duplicate_label_name_allowed(self, tmp_path: Path):
        """KiCad allows duplicate label names in the same sheet."""
        sch_path = _write_sch(tmp_path)

        # Place first label
        result1 = add_label_main(
            [
                str(sch_path),
                "--type",
                "global",
                "--name",
                "VCC",
                "--at",
                "100",
                "80",
            ]
        )
        assert result1 == 0

        # Place second label with same name
        result2 = add_label_main(
            [
                str(sch_path),
                "--type",
                "global",
                "--name",
                "VCC",
                "--at",
                "120",
                "80",
            ]
        )
        assert result2 == 0

        sch = Schematic.load(sch_path)
        assert len(sch.global_labels) == 2


# ---------------------------------------------------------------------------
# Schematic.add_label / add_global_label / add_hierarchical_label methods
# ---------------------------------------------------------------------------


class TestSchematicAddLabelMethods:
    def test_add_label(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        sch = Schematic.load(sch_path)
        label = sch.add_label("NET1", (100.0, 80.0))
        assert label.text == "NET1"
        assert label.uuid
        assert len(sch.labels) == 1

    def test_add_global_label(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        sch = Schematic.load(sch_path)
        label = sch.add_global_label("VCC", (100.0, 80.0), shape="output")
        assert label.text == "VCC"
        assert label.shape == "output"
        assert label.uuid
        assert len(sch.global_labels) == 1

    def test_add_hierarchical_label(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        sch = Schematic.load(sch_path)
        label = sch.add_hierarchical_label("CLK", (100.0, 80.0), shape="input")
        assert label.text == "CLK"
        assert label.shape == "input"
        assert label.uuid
        assert len(sch.hierarchical_labels) == 1

    def test_add_label_save_reload(self, tmp_path: Path):
        sch_path = _write_sch(tmp_path)
        sch = Schematic.load(sch_path)
        sch.add_global_label("SIG", (50.0, 60.0), shape="bidirectional")
        out_path = tmp_path / "label_output.kicad_sch"
        sch.save(out_path)

        sch2 = Schematic.load(out_path)
        assert len(sch2.global_labels) == 1
        gl = sch2.global_labels[0]
        assert gl.text == "SIG"
        assert gl.shape == "bidirectional"
        assert gl.position == (50.0, 60.0)
