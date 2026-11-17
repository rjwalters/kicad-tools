"""Tests for the sch set-value command.

Covers set_value_text(), run_set_value(), batch mapping,
hierarchical schematic traversal, and dry-run mode.
"""

import json
from pathlib import Path

from kicad_tools.cli.modify_schematic import (
    delete_symbol_text,
    find_symbol_text_range,
    regen_uuids_text,
    set_footprint_text,
    set_lib_id_text,
    set_value_text,
)
from kicad_tools.cli.sch_set_value import run_set_value

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


# Space-indented schematic matching real KiCad 8 output (4 spaces per level)
SPACE_INDENTED_SCHEMATIC = """\
(kicad_sch
    (version 20231120)
    (generator "test")
    (generator_version "8.0")
    (uuid "00000000-0000-0000-0000-000000000001")
    (paper "A4")
    (lib_symbols
    )
    (symbol
        (lib_id "Device:R_Small")
        (at 100 50 0)
        (property "Reference" "R8"
            (at 100 48 0)
            (effects (font (size 1.27 1.27)))
        )
        (property "Value" "1k"
            (at 100 52 0)
            (effects (font (size 1.27 1.27)))
        )
        (property "Footprint" "Resistor_SMD:R_0402_1005Metric"
            (at 100 54 0)
            (effects (font (size 1.27 1.27)) (hide yes))
        )
        (uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        (instances
            (project "dac"
                (path "/" (reference "R8") (unit 1))
            )
        )
    )
    (sheet_instances
        (path "/" (page "1"))
    )
)
"""


def _write_sch(
    tmp_path: Path, content: str = MINIMAL_SCHEMATIC, name: str = "test.kicad_sch"
) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# set_value_text() unit tests
# ---------------------------------------------------------------------------


class TestSetValueText:
    def test_update_existing_value(self):
        """Update a symbol that already has a value assigned."""
        new_val = "4.7k"
        result, success, msg = set_value_text(MINIMAL_SCHEMATIC, "R1", new_val)
        assert success is True
        assert '"Value" "4.7k"' in result
        assert '"Value" "10k"' not in result
        assert "Changed R1 value" in msg

    def test_set_empty_value(self):
        """Set an empty value string."""
        result, success, msg = set_value_text(MINIMAL_SCHEMATIC, "R1", "")
        assert success is True
        assert '"Value" ""' in result
        assert "Changed R1 value" in msg

    def test_nonexistent_reference(self):
        """Trying to set value on a non-existent ref returns failure."""
        result, success, msg = set_value_text(MINIMAL_SCHEMATIC, "U99", "SomeValue")
        assert success is False
        assert result == MINIMAL_SCHEMATIC
        assert "not found" in msg

    def test_preserves_other_symbols(self):
        """Changing R1 value should not affect C1."""
        new_val = "4.7k"
        result, success, _ = set_value_text(MINIMAL_SCHEMATIC, "R1", new_val)
        assert success is True
        # C1 should still have its original value
        c1_result = find_symbol_text_range(result, "C1")
        assert c1_result is not None
        _, _, info = c1_result
        assert info["value"] == "100nF"

    def test_value_with_special_characters(self):
        """Value strings with hyphens, dots, and mixed case."""
        new_val = "AP2204K-3.3TRG1"
        result, success, _ = set_value_text(MINIMAL_SCHEMATIC, "R1", new_val)
        assert success is True
        assert new_val in result


# ---------------------------------------------------------------------------
# Space-indented schematic tests (KiCad 8 real output format)
# ---------------------------------------------------------------------------


class TestSpaceIndentedSetValue:
    def test_find_symbol_in_space_indented(self):
        """find_symbol_text_range works with space-indented schematics."""
        result = find_symbol_text_range(SPACE_INDENTED_SCHEMATIC, "R8")
        assert result is not None
        _, _, info = result
        assert info["lib_id"] == "Device:R_Small"
        assert info["value"] == "1k"
        assert info["footprint"] == "Resistor_SMD:R_0402_1005Metric"

    def test_set_value_space_indented(self):
        """set_value_text works with space-indented schematics."""
        result, success, msg = set_value_text(SPACE_INDENTED_SCHEMATIC, "R8", "470R")
        assert success is True
        assert '"Value" "470R"' in result
        assert '"Value" "1k"' not in result
        assert "Changed R8 value" in msg

    def test_nonexistent_ref_space_indented(self):
        """Non-existent ref returns failure on space-indented schematic."""
        result, success, msg = set_value_text(SPACE_INDENTED_SCHEMATIC, "U99", "x")
        assert success is False
        assert "not found" in msg

    def test_run_set_value_space_indented(self, tmp_path):
        """run_set_value integration test with space-indented schematic."""
        sch = _write_sch(tmp_path, SPACE_INDENTED_SCHEMATIC)
        ret = run_set_value(
            schematic_path=sch,
            ref="R8",
            value="470R",
            dry_run=False,
            backup=False,
        )
        assert ret == 0
        text = sch.read_text()
        assert '"Value" "470R"' in text
        assert '"Value" "1k"' not in text


# ---------------------------------------------------------------------------
# find_symbol_text_range() value extraction
# ---------------------------------------------------------------------------


class TestFindSymbolValue:
    def test_extracts_value_from_info(self):
        result = find_symbol_text_range(MINIMAL_SCHEMATIC, "R1")
        assert result is not None
        _, _, info = result
        assert info["value"] == "10k"

    def test_extracts_value_c1(self):
        result = find_symbol_text_range(MINIMAL_SCHEMATIC, "C1")
        assert result is not None
        _, _, info = result
        assert info["value"] == "100nF"


# ---------------------------------------------------------------------------
# run_set_value() integration tests
# ---------------------------------------------------------------------------


class TestRunSetValue:
    def test_single_ref_mode(self, tmp_path):
        sch = _write_sch(tmp_path)
        ret = run_set_value(
            schematic_path=sch,
            ref="R1",
            value="4.7k",
            dry_run=False,
            backup=False,
        )
        assert ret == 0
        text = sch.read_text()
        assert '"Value" "4.7k"' in text
        assert '"Value" "10k"' not in text

    def test_single_ref_creates_backup(self, tmp_path):
        sch = _write_sch(tmp_path)
        ret = run_set_value(
            schematic_path=sch,
            ref="R1",
            value="4.7k",
            dry_run=False,
            backup=True,
        )
        assert ret == 0
        backups = list(tmp_path.glob("test_backup_*"))
        assert len(backups) == 1

    def test_dry_run_does_not_modify(self, tmp_path):
        sch = _write_sch(tmp_path)
        original = sch.read_text()
        ret = run_set_value(
            schematic_path=sch,
            ref="R1",
            value="4.7k",
            dry_run=True,
            backup=False,
        )
        assert ret == 0
        assert sch.read_text() == original

    def test_batch_json_mapping(self, tmp_path):
        sch = _write_sch(tmp_path)
        map_path = tmp_path / "map.json"
        map_path.write_text(
            json.dumps(
                {
                    "R1": "4.7k",
                    "C1": "220nF",
                }
            )
        )
        ret = run_set_value(
            schematic_path=sch,
            map_path=map_path,
            dry_run=False,
            backup=False,
        )
        assert ret == 0
        text = sch.read_text()
        assert '"Value" "4.7k"' in text
        assert '"Value" "220nF"' in text

    def test_batch_csv_mapping(self, tmp_path):
        sch = _write_sch(tmp_path)
        map_path = tmp_path / "map.csv"
        map_path.write_text("R1,4.7k\nC1,220nF\n")
        ret = run_set_value(
            schematic_path=sch,
            map_path=map_path,
            dry_run=False,
            backup=False,
        )
        assert ret == 0
        text = sch.read_text()
        assert '"Value" "4.7k"' in text
        assert '"Value" "220nF"' in text

    def test_nonexistent_ref_returns_error(self, tmp_path):
        sch = _write_sch(tmp_path)
        ret = run_set_value(
            schematic_path=sch,
            ref="U99",
            value="SomeValue",
            dry_run=False,
            backup=False,
        )
        assert ret == 1

    def test_missing_schematic(self, tmp_path):
        ret = run_set_value(
            schematic_path=tmp_path / "nonexistent.kicad_sch",
            ref="R1",
            value="4.7k",
        )
        assert ret == 1

    def test_no_ref_or_map_returns_error(self, tmp_path):
        sch = _write_sch(tmp_path)
        ret = run_set_value(schematic_path=sch)
        assert ret == 1

    def test_empty_mapping_file(self, tmp_path):
        sch = _write_sch(tmp_path)
        map_path = tmp_path / "map.json"
        map_path.write_text("{}")
        ret = run_set_value(
            schematic_path=sch,
            map_path=map_path,
            dry_run=False,
            backup=False,
        )
        assert ret == 1

    def test_missing_mapping_file(self, tmp_path):
        sch = _write_sch(tmp_path)
        ret = run_set_value(
            schematic_path=sch,
            map_path=tmp_path / "nonexistent.json",
            dry_run=False,
            backup=False,
        )
        assert ret == 1

    def test_invalid_mapping_format(self, tmp_path):
        sch = _write_sch(tmp_path)
        map_path = tmp_path / "map.csv"
        map_path.write_text("R1\n")
        ret = run_set_value(
            schematic_path=sch,
            map_path=map_path,
            dry_run=False,
            backup=False,
        )
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
    def test_set_value_in_subsheet(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)

        ret = run_set_value(
            schematic_path=parent,
            ref="C2",
            value="2.2uF",
            dry_run=False,
            backup=False,
        )
        assert ret == 0
        text = child.read_text()
        assert '"Value" "2.2uF"' in text

    def test_batch_across_hierarchy(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)

        map_path = tmp_path / "map.json"
        map_path.write_text(
            json.dumps(
                {
                    "R1": "4.7k",
                    "C2": "2.2uF",
                }
            )
        )
        ret = run_set_value(
            schematic_path=parent,
            map_path=map_path,
            dry_run=False,
            backup=False,
        )
        assert ret == 0
        assert '"Value" "4.7k"' in parent.read_text()
        assert '"Value" "2.2uF"' in child.read_text()

    def test_dry_run_hierarchical(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)
        original_child = child.read_text()

        ret = run_set_value(
            schematic_path=parent,
            ref="C2",
            value="2.2uF",
            dry_run=True,
            backup=False,
        )
        assert ret == 0
        # File should be unchanged
        assert child.read_text() == original_child

    def test_warning_for_unmatched_ref_in_hierarchy(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)

        map_path = tmp_path / "map.json"
        map_path.write_text(
            json.dumps(
                {
                    "R1": "4.7k",
                    "ZZZZ": "NotFound",
                }
            )
        )
        ret = run_set_value(
            schematic_path=parent,
            map_path=map_path,
            dry_run=False,
            backup=False,
        )
        # Should still succeed (partial match) but return 0 because some changed
        assert ret == 0
        assert '"Value" "4.7k"' in parent.read_text()


# ---------------------------------------------------------------------------
# Regression: symbols without (instances ...) block (#2100)
# ---------------------------------------------------------------------------

# C18 lacks an (instances ...) block; C21 has one.
# The old regex would span from C18 through C21, causing set-value on C21
# to incorrectly modify C18's value.
MULTI_SYMBOL_NO_INSTANCES = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 100 50 0)
\t\t(property "Reference" "C18"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100nF"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" "Capacitor_SMD:C_0402_1005Metric"
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 50 0)
\t\t(property "Reference" "C21"
\t\t\t(at 120 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "1uF"
\t\t\t(at 120 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" "Capacitor_SMD:C_0603_1608Metric"
\t\t\t(at 120 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "C21") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# Both symbols lack (instances ...) blocks
MULTI_SYMBOL_BOTH_NO_INSTANCES = """\
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
\t\t(property "Reference" "R5"
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
\t\t(uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 120 50 0)
\t\t(property "Reference" "R6"
\t\t\t(at 120 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 120 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0402_1005Metric"
\t\t\t(at 120 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "dddddddd-dddd-dddd-dddd-dddddddddddd")
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""


class TestNoInstancesBlock:
    """Regression tests for issue #2100: symbols without (instances ...) block."""

    def test_find_symbol_without_instances(self):
        """find_symbol_text_range locates C18 which has no instances block."""
        result = find_symbol_text_range(MULTI_SYMBOL_NO_INSTANCES, "C18")
        assert result is not None
        _, _, info = result
        assert info["lib_id"] == "Device:C"
        assert info["value"] == "100nF"
        assert info["uuid"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    def test_find_symbol_with_instances(self):
        """find_symbol_text_range locates C21 which has an instances block."""
        result = find_symbol_text_range(MULTI_SYMBOL_NO_INSTANCES, "C21")
        assert result is not None
        _, _, info = result
        assert info["lib_id"] == "Device:C"
        assert info["value"] == "1uF"
        assert info["uuid"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    def test_set_value_targets_correct_symbol(self):
        """set_value_text on C21 must not modify C18 (the bug in #2100)."""
        result, success, msg = set_value_text(MULTI_SYMBOL_NO_INSTANCES, "C21", "2.2uF")
        assert success is True
        assert "Changed C21 value" in msg
        # C21 should have the new value
        c21 = find_symbol_text_range(result, "C21")
        assert c21 is not None
        assert c21[2]["value"] == "2.2uF"
        # C18 must be unchanged
        c18 = find_symbol_text_range(result, "C18")
        assert c18 is not None
        assert c18[2]["value"] == "100nF"

    def test_set_value_on_symbol_without_instances(self):
        """set_value_text on C18 (no instances block) works correctly."""
        result, success, msg = set_value_text(MULTI_SYMBOL_NO_INSTANCES, "C18", "220nF")
        assert success is True
        assert "Changed C18 value" in msg
        c18 = find_symbol_text_range(result, "C18")
        assert c18 is not None
        assert c18[2]["value"] == "220nF"
        # C21 must be unchanged
        c21 = find_symbol_text_range(result, "C21")
        assert c21 is not None
        assert c21[2]["value"] == "1uF"

    def test_set_footprint_targets_correct_symbol(self):
        """set_footprint_text on C21 must not modify C18."""
        new_fp = "Capacitor_SMD:C_0805_2012Metric"
        result, success, _ = set_footprint_text(MULTI_SYMBOL_NO_INSTANCES, "C21", new_fp)
        assert success is True
        c21 = find_symbol_text_range(result, "C21")
        assert c21 is not None
        assert c21[2]["footprint"] == new_fp
        c18 = find_symbol_text_range(result, "C18")
        assert c18 is not None
        assert c18[2]["footprint"] == "Capacitor_SMD:C_0402_1005Metric"

    def test_set_lib_id_targets_correct_symbol(self):
        """set_lib_id_text on C21 must not modify C18."""
        result, success, _ = set_lib_id_text(MULTI_SYMBOL_NO_INSTANCES, "C21", "Device:C_Polarized")
        assert success is True
        c21 = find_symbol_text_range(result, "C21")
        assert c21 is not None
        assert c21[2]["lib_id"] == "Device:C_Polarized"
        c18 = find_symbol_text_range(result, "C18")
        assert c18 is not None
        assert c18[2]["lib_id"] == "Device:C"

    def test_delete_targets_correct_symbol(self):
        """delete_symbol_text on C21 must not delete C18."""
        result, success, _ = delete_symbol_text(MULTI_SYMBOL_NO_INSTANCES, "C21")
        assert success is True
        # C18 must still be present
        c18 = find_symbol_text_range(result, "C18")
        assert c18 is not None
        assert c18[2]["value"] == "100nF"
        # C21 must be gone
        assert find_symbol_text_range(result, "C21") is None

    def test_regen_uuids_targets_correct_symbol(self):
        """regen_uuids_text on C21 must not change C18 UUIDs."""
        result, success, _ = regen_uuids_text(MULTI_SYMBOL_NO_INSTANCES, "C21")
        assert success is True
        # C18 UUID must be unchanged
        c18 = find_symbol_text_range(result, "C18")
        assert c18 is not None
        assert c18[2]["uuid"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        # C21 UUID must be different
        c21 = find_symbol_text_range(result, "C21")
        assert c21 is not None
        assert c21[2]["uuid"] != "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    def test_both_symbols_without_instances(self):
        """Operations work when both symbols lack instances blocks."""
        result, success, _ = set_value_text(MULTI_SYMBOL_BOTH_NO_INSTANCES, "R6", "100k")
        assert success is True
        r6 = find_symbol_text_range(result, "R6")
        assert r6 is not None
        assert r6[2]["value"] == "100k"
        r5 = find_symbol_text_range(result, "R5")
        assert r5 is not None
        assert r5[2]["value"] == "10k"

    def test_single_symbol_without_instances(self):
        """A schematic with a single symbol lacking instances still works."""
        single = """\
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
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""
        result = find_symbol_text_range(single, "R1")
        assert result is not None
        _, _, info = result
        assert info["value"] == "10k"
        # set-value should also work
        modified, success, _ = set_value_text(single, "R1", "22k")
        assert success is True
        r1 = find_symbol_text_range(modified, "R1")
        assert r1 is not None
        assert r1[2]["value"] == "22k"
