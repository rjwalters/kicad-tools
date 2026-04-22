"""Tests for the sch set-footprint command.

Covers set_footprint_text(), run_set_footprint(), batch mapping,
hierarchical schematic traversal, and dry-run mode.
"""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.modify_schematic import find_symbol_text_range, set_footprint_text
from kicad_tools.cli.sch_set_footprint import (
    _collect_schematic_files,
    _load_mapping,
    run_set_footprint,
)

# ---------------------------------------------------------------------------
# Minimal schematic content for testing
# ---------------------------------------------------------------------------

MINIMAL_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0402_1005Metric"
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 50 0)
\t\t(property "Reference" "C1"
\t\t\t(at 120 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100nF"
\t\t\t(at 120 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 120 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "C1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""


def _write_sch(tmp_path: Path, content: str = MINIMAL_SCHEMATIC, name: str = "test.kicad_sch") -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# set_footprint_text() unit tests
# ---------------------------------------------------------------------------


class TestSetFootprintText:
    def test_update_existing_footprint(self):
        """Update a symbol that already has a footprint assigned."""
        new_fp = "Resistor_SMD:R_0805_2012Metric"
        result, success, msg = set_footprint_text(MINIMAL_SCHEMATIC, "R1", new_fp)
        assert success is True
        assert new_fp in result
        assert "R_0402_1005Metric" not in result
        assert "Changed R1 footprint" in msg

    def test_assign_empty_footprint(self):
        """Assign a footprint to a symbol with an empty footprint property."""
        new_fp = "Capacitor_SMD:C_0603_1608Metric"
        result, success, msg = set_footprint_text(MINIMAL_SCHEMATIC, "C1", new_fp)
        assert success is True
        assert new_fp in result
        assert "Changed C1 footprint" in msg

    def test_nonexistent_reference(self):
        """Trying to set footprint on a non-existent ref returns failure."""
        result, success, msg = set_footprint_text(MINIMAL_SCHEMATIC, "U99", "some:fp")
        assert success is False
        assert result == MINIMAL_SCHEMATIC
        assert "not found" in msg

    def test_preserves_other_symbols(self):
        """Changing R1 footprint should not affect C1."""
        new_fp = "Resistor_SMD:R_0805_2012Metric"
        result, success, _ = set_footprint_text(MINIMAL_SCHEMATIC, "R1", new_fp)
        assert success is True
        # C1 should still have empty footprint
        c1_result = find_symbol_text_range(result, "C1")
        assert c1_result is not None
        _, _, info = c1_result
        assert info["footprint"] == ""

    def test_footprint_with_special_characters(self):
        """Footprint strings with colons, underscores, and numbers."""
        new_fp = "Package_TO_SOT_SMD:SOT-23-5"
        result, success, _ = set_footprint_text(MINIMAL_SCHEMATIC, "R1", new_fp)
        assert success is True
        assert new_fp in result


# ---------------------------------------------------------------------------
# find_symbol_text_range() footprint extraction
# ---------------------------------------------------------------------------


class TestFindSymbolFootprint:
    def test_extracts_footprint_from_info(self):
        result = find_symbol_text_range(MINIMAL_SCHEMATIC, "R1")
        assert result is not None
        _, _, info = result
        assert info["footprint"] == "Resistor_SMD:R_0402_1005Metric"

    def test_extracts_empty_footprint(self):
        result = find_symbol_text_range(MINIMAL_SCHEMATIC, "C1")
        assert result is not None
        _, _, info = result
        assert info["footprint"] == ""


# ---------------------------------------------------------------------------
# _load_mapping() tests
# ---------------------------------------------------------------------------


class TestLoadMapping:
    def test_json_mapping(self, tmp_path):
        data = {"R1": "Resistor_SMD:R_0805", "C1": "Capacitor_SMD:C_0603"}
        p = tmp_path / "map.json"
        p.write_text(json.dumps(data))
        result = _load_mapping(p)
        assert result == data

    def test_csv_mapping(self, tmp_path):
        p = tmp_path / "map.csv"
        p.write_text("R1,Resistor_SMD:R_0805\nC1,Capacitor_SMD:C_0603\n")
        result = _load_mapping(p)
        assert result == {"R1": "Resistor_SMD:R_0805", "C1": "Capacitor_SMD:C_0603"}

    def test_csv_with_comments(self, tmp_path):
        p = tmp_path / "map.csv"
        p.write_text("# Header comment\nR1,Resistor_SMD:R_0805\n\n# Another comment\nC1,Capacitor_SMD:C_0603\n")
        result = _load_mapping(p)
        assert len(result) == 2

    def test_invalid_csv_line(self, tmp_path):
        p = tmp_path / "map.csv"
        p.write_text("R1\n")
        with pytest.raises(ValueError, match="expected"):
            _load_mapping(p)

    def test_csv_empty_ref_raises(self, tmp_path):
        p = tmp_path / "map.csv"
        p.write_text(",Resistor_SMD:R_0805\n")
        with pytest.raises(ValueError, match="empty reference"):
            _load_mapping(p)

    def test_empty_json_object(self, tmp_path):
        p = tmp_path / "map.json"
        p.write_text("{}")
        result = _load_mapping(p)
        assert result == {}


# ---------------------------------------------------------------------------
# run_set_footprint() integration tests
# ---------------------------------------------------------------------------


class TestRunSetFootprint:
    def test_single_ref_mode(self, tmp_path):
        sch = _write_sch(tmp_path)
        ret = run_set_footprint(
            schematic_path=sch,
            ref="R1",
            footprint="Resistor_SMD:R_0805_2012Metric",
            dry_run=False,
            backup=False,
        )
        assert ret == 0
        text = sch.read_text()
        assert "Resistor_SMD:R_0805_2012Metric" in text
        assert "R_0402_1005Metric" not in text

    def test_single_ref_creates_backup(self, tmp_path):
        sch = _write_sch(tmp_path)
        ret = run_set_footprint(
            schematic_path=sch,
            ref="R1",
            footprint="Resistor_SMD:R_0805_2012Metric",
            dry_run=False,
            backup=True,
        )
        assert ret == 0
        backups = list(tmp_path.glob("test_backup_*"))
        assert len(backups) == 1

    def test_dry_run_does_not_modify(self, tmp_path):
        sch = _write_sch(tmp_path)
        original = sch.read_text()
        ret = run_set_footprint(
            schematic_path=sch,
            ref="R1",
            footprint="Resistor_SMD:R_0805_2012Metric",
            dry_run=True,
            backup=False,
        )
        assert ret == 0
        assert sch.read_text() == original

    def test_batch_json_mapping(self, tmp_path):
        sch = _write_sch(tmp_path)
        map_path = tmp_path / "map.json"
        map_path.write_text(json.dumps({
            "R1": "Resistor_SMD:R_0805_2012Metric",
            "C1": "Capacitor_SMD:C_0603_1608Metric",
        }))
        ret = run_set_footprint(
            schematic_path=sch,
            map_path=map_path,
            dry_run=False,
            backup=False,
        )
        assert ret == 0
        text = sch.read_text()
        assert "Resistor_SMD:R_0805_2012Metric" in text
        assert "Capacitor_SMD:C_0603_1608Metric" in text

    def test_nonexistent_ref_returns_error(self, tmp_path):
        sch = _write_sch(tmp_path)
        ret = run_set_footprint(
            schematic_path=sch,
            ref="U99",
            footprint="some:fp",
            dry_run=False,
            backup=False,
        )
        assert ret == 1

    def test_missing_schematic(self, tmp_path):
        ret = run_set_footprint(
            schematic_path=tmp_path / "nonexistent.kicad_sch",
            ref="R1",
            footprint="some:fp",
        )
        assert ret == 1

    def test_no_ref_or_map_returns_error(self, tmp_path):
        sch = _write_sch(tmp_path)
        ret = run_set_footprint(schematic_path=sch)
        assert ret == 1


# ---------------------------------------------------------------------------
# Hierarchical schematic support
# ---------------------------------------------------------------------------


PARENT_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0402_1005Metric"
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet
\t\t(at 150 50)
\t\t(size 20 20)
\t\t(property "Sheetname" "SubSheet"
\t\t\t(at 150 48 0)
\t\t)
\t\t(property "Sheetfile" "sub.kicad_sch"
\t\t\t(at 150 68 0)
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

CHILD_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "44444444-4444-4444-4444-444444444444")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 100 50 0)
\t\t(property "Reference" "C2"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "1uF"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "55555555-5555-5555-5555-555555555555")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/33333333-3333-3333-3333-333333333333" (reference "C2") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/33333333-3333-3333-3333-333333333333" (page "2"))
\t)
)
"""


class TestHierarchicalSchematic:
    def test_collect_schematic_files(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)

        files = _collect_schematic_files(parent)
        assert len(files) == 2
        assert files[0] == parent
        assert files[1] == child

    def test_set_footprint_in_subsheet(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)

        ret = run_set_footprint(
            schematic_path=parent,
            ref="C2",
            footprint="Capacitor_SMD:C_0805_2012Metric",
            dry_run=False,
            backup=False,
        )
        assert ret == 0
        text = child.read_text()
        assert "Capacitor_SMD:C_0805_2012Metric" in text

    def test_batch_across_hierarchy(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)

        map_path = tmp_path / "map.json"
        map_path.write_text(json.dumps({
            "R1": "Resistor_SMD:R_0805_2012Metric",
            "C2": "Capacitor_SMD:C_0805_2012Metric",
        }))
        ret = run_set_footprint(
            schematic_path=parent,
            map_path=map_path,
            dry_run=False,
            backup=False,
        )
        assert ret == 0
        assert "Resistor_SMD:R_0805_2012Metric" in parent.read_text()
        assert "Capacitor_SMD:C_0805_2012Metric" in child.read_text()

    def test_dry_run_hierarchical(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)
        original_child = child.read_text()

        ret = run_set_footprint(
            schematic_path=parent,
            ref="C2",
            footprint="Capacitor_SMD:C_0805_2012Metric",
            dry_run=True,
            backup=False,
        )
        assert ret == 0
        # File should be unchanged
        assert child.read_text() == original_child
