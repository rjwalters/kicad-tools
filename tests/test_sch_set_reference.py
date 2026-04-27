"""Tests for the sch set-reference command.

Covers single rename, batch rename via --map, duplicate detection,
hierarchical traversal, dry-run, backup, two-pass collision avoidance,
and reference-not-found error handling.
"""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.sch_set_reference import (
    _check_duplicates,
    _collect_all_references,
    run_set_reference,
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
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R2"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0402_1005Metric"
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R2") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 50 0)
\t\t(property "Reference" "C1"
\t\t\t(at 122 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100nF"
\t\t\t(at 122 50 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" "Capacitor_SMD:C_0603_1608Metric"
\t\t\t(at 120 50 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "C1") (unit 1))
\t\t\t)
\t\t)
\t)
)
"""

LED_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000002")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:LED")
\t\t(at 100 50 0)
\t\t(property "Reference" "LED3"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "Red"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(uuid "aaa11111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "LED3") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:LED")
\t\t(at 100 70 0)
\t\t(property "Reference" "LED4"
\t\t\t(at 100 68 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "Green"
\t\t\t(at 100 72 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(uuid "aaa22222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "LED4") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:LED")
\t\t(at 100 90 0)
\t\t(property "Reference" "LED5"
\t\t\t(at 100 88 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "Blue"
\t\t\t(at 100 92 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(uuid "aaa33333-3333-3333-3333-333333333333")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "LED5") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:LED")
\t\t(at 100 110 0)
\t\t(property "Reference" "LED6"
\t\t\t(at 100 108 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "White"
\t\t\t(at 100 112 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(uuid "aaa44444-4444-4444-4444-444444444444")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "LED6") (unit 1))
\t\t\t)
\t\t)
\t)
)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_schematic(tmp_path: Path) -> Path:
    """Write MINIMAL_SCHEMATIC to a temp file and return its path."""
    sch = tmp_path / "test.kicad_sch"
    sch.write_text(MINIMAL_SCHEMATIC, encoding="utf-8")
    return sch


@pytest.fixture()
def tmp_led_schematic(tmp_path: Path) -> Path:
    """Write LED_SCHEMATIC to a temp file and return its path."""
    sch = tmp_path / "led_test.kicad_sch"
    sch.write_text(LED_SCHEMATIC, encoding="utf-8")
    return sch


# ---------------------------------------------------------------------------
# Tests: _check_duplicates
# ---------------------------------------------------------------------------


class TestCheckDuplicates:
    def test_no_collision(self):
        mapping = {"R1": "R99"}
        existing = {"R1", "R2", "C1"}
        assert _check_duplicates(mapping, existing) == []

    def test_collision_with_existing(self):
        mapping = {"R1": "R2"}
        existing = {"R1", "R2", "C1"}
        errors = _check_duplicates(mapping, existing)
        assert len(errors) == 1
        assert "R2 already exists" in errors[0]

    def test_no_collision_when_target_also_renamed(self):
        """Swap R1<->R2: both are being renamed, so no collision."""
        mapping = {"R1": "R2", "R2": "R1"}
        existing = {"R1", "R2"}
        assert _check_duplicates(mapping, existing) == []

    def test_duplicate_target_in_mapping(self):
        mapping = {"R1": "R99", "R2": "R99"}
        existing = {"R1", "R2"}
        errors = _check_duplicates(mapping, existing)
        assert len(errors) == 1
        assert "duplicate target" in errors[0]


# ---------------------------------------------------------------------------
# Tests: _collect_all_references
# ---------------------------------------------------------------------------


class TestCollectAllReferences:
    def test_collects_refs(self, tmp_schematic: Path):
        refs = _collect_all_references([tmp_schematic])
        assert "R1" in refs
        assert "R2" in refs
        assert "C1" in refs


# ---------------------------------------------------------------------------
# Tests: run_set_reference -- single rename
# ---------------------------------------------------------------------------


class TestSingleRename:
    def test_rename_r1_to_r99(self, tmp_schematic: Path):
        result = run_set_reference(tmp_schematic, ref="R1", new_ref="R99", backup=False)
        assert result == 0
        text = tmp_schematic.read_text(encoding="utf-8")
        assert '(property "Reference" "R99"' in text
        assert '(reference "R99")' in text
        # R1 should no longer appear as a reference
        assert '(property "Reference" "R1"' not in text
        assert '(reference "R1")' not in text
        # R2 and C1 should be unchanged
        assert '(property "Reference" "R2"' in text
        assert '(property "Reference" "C1"' not in text or '(property "Reference" "C1"' in text

    def test_rename_updates_instances_block(self, tmp_schematic: Path):
        result = run_set_reference(tmp_schematic, ref="R1", new_ref="R99", backup=False)
        assert result == 0
        text = tmp_schematic.read_text(encoding="utf-8")
        assert '(reference "R99")' in text
        assert '(reference "R1")' not in text


# ---------------------------------------------------------------------------
# Tests: run_set_reference -- batch rename via --map
# ---------------------------------------------------------------------------


class TestBatchRename:
    def test_batch_led_rename(self, tmp_led_schematic: Path, tmp_path: Path):
        """Rename LED3-LED6 to D3-D6 using a JSON mapping file."""
        map_file = tmp_path / "ref-map.json"
        map_file.write_text(
            json.dumps({"LED3": "D3", "LED4": "D4", "LED5": "D5", "LED6": "D6"}),
            encoding="utf-8",
        )
        result = run_set_reference(tmp_led_schematic, map_path=map_file, backup=False)
        assert result == 0
        text = tmp_led_schematic.read_text(encoding="utf-8")
        assert '(property "Reference" "D3"' in text
        assert '(property "Reference" "D4"' in text
        assert '(property "Reference" "D5"' in text
        assert '(property "Reference" "D6"' in text
        assert '(reference "D3")' in text
        assert '(reference "D4")' in text
        # No LED references should remain
        assert "LED3" not in text
        assert "LED4" not in text
        assert "LED5" not in text
        assert "LED6" not in text

    def test_batch_csv_mapping(self, tmp_led_schematic: Path, tmp_path: Path):
        """Test batch rename via CSV mapping file."""
        map_file = tmp_path / "ref-map.csv"
        map_file.write_text(
            "LED3,D3\nLED4,D4\n",
            encoding="utf-8",
        )
        result = run_set_reference(tmp_led_schematic, map_path=map_file, backup=False)
        assert result == 0
        text = tmp_led_schematic.read_text(encoding="utf-8")
        assert '(property "Reference" "D3"' in text
        assert '(property "Reference" "D4"' in text
        # LED5 and LED6 should be unchanged
        assert '(property "Reference" "LED5"' in text
        assert '(property "Reference" "LED6"' in text


# ---------------------------------------------------------------------------
# Tests: duplicate detection
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    def test_rename_to_existing_ref_fails(self, tmp_schematic: Path):
        """Attempt to rename R1 to R2 when R2 already exists."""
        result = run_set_reference(tmp_schematic, ref="R1", new_ref="R2", backup=False)
        assert result == 1
        # Verify no files were modified
        text = tmp_schematic.read_text(encoding="utf-8")
        assert '(property "Reference" "R1"' in text
        assert '(property "Reference" "R2"' in text


# ---------------------------------------------------------------------------
# Tests: dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_no_modification(self, tmp_schematic: Path):
        original_text = tmp_schematic.read_text(encoding="utf-8")
        result = run_set_reference(
            tmp_schematic, ref="R1", new_ref="R99", dry_run=True, backup=False
        )
        assert result == 0
        after_text = tmp_schematic.read_text(encoding="utf-8")
        assert original_text == after_text


# ---------------------------------------------------------------------------
# Tests: backup
# ---------------------------------------------------------------------------


class TestBackup:
    def test_backup_created(self, tmp_schematic: Path):
        result = run_set_reference(tmp_schematic, ref="R1", new_ref="R99", backup=True)
        assert result == 0
        # create_backup() produces {stem}_backup_{timestamp}{suffix}
        bak_files = list(tmp_schematic.parent.glob("*_backup_*"))
        assert len(bak_files) >= 1


# ---------------------------------------------------------------------------
# Tests: two-pass collision avoidance (swap)
# ---------------------------------------------------------------------------


class TestTwoPassCollisionAvoidance:
    def test_swap_references(self, tmp_schematic: Path, tmp_path: Path):
        """Swap R1 and R2: both rename simultaneously without data loss."""
        map_file = tmp_path / "swap-map.json"
        map_file.write_text(
            json.dumps({"R1": "R2", "R2": "R1"}),
            encoding="utf-8",
        )
        result = run_set_reference(tmp_schematic, map_path=map_file, backup=False)
        assert result == 0
        text = tmp_schematic.read_text(encoding="utf-8")
        # After swap: old R1 (10k) should now be R2, old R2 (4.7k) should now be R1
        # Check that both references exist
        assert '(property "Reference" "R1"' in text
        assert '(property "Reference" "R2"' in text
        # Verify values swapped correctly
        # R1 should now have value 4.7k (was R2) and R2 should have 10k (was R1)
        # Find R1's value
        import re

        r1_block = re.search(
            r'\(property "Reference" "R1".*?\(property "Value" "([^"]+)"',
            text,
            re.DOTALL,
        )
        r2_block = re.search(
            r'\(property "Reference" "R2".*?\(property "Value" "([^"]+)"',
            text,
            re.DOTALL,
        )
        assert r1_block is not None
        assert r2_block is not None
        assert r1_block.group(1) == "4.7k"
        assert r2_block.group(1) == "10k"


# ---------------------------------------------------------------------------
# Tests: reference not found
# ---------------------------------------------------------------------------


class TestReferenceNotFound:
    def test_nonexistent_ref_error(self, tmp_schematic: Path):
        result = run_set_reference(
            tmp_schematic, ref="X99", new_ref="X100", backup=False
        )
        assert result == 1

    def test_file_not_found(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.kicad_sch"
        result = run_set_reference(missing, ref="R1", new_ref="R99")
        assert result == 1


# ---------------------------------------------------------------------------
# Tests: hierarchical traversal
# ---------------------------------------------------------------------------


PARENT_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000099")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 50 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 50 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "1k"
\t\t\t(at 50 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(uuid "p1111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet
\t\t(at 100 100) (size 20 20)
\t\t(uuid "sheet-uuid-1234")
\t\t(property "Sheetname" "child_sheet"
\t\t\t(at 100 100 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Sheetfile" "child.kicad_sch"
\t\t\t(at 100 120 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t)
)
"""

CHILD_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "child-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:LED")
\t\t(at 100 50 0)
\t\t(property "Reference" "LED1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "Red"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(uuid "c1111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/00000000-0000-0000-0000-000000000099/sheet-uuid-1234"
\t\t\t\t\t(reference "LED1") (unit 1))
\t\t\t)
\t\t)
\t)
)
"""


class TestHierarchicalTraversal:
    def test_rename_in_child_sheet(self, tmp_path: Path):
        """Rename LED1 to D1 in a child sheet."""
        parent = tmp_path / "parent.kicad_sch"
        child = tmp_path / "child.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC, encoding="utf-8")
        child.write_text(CHILD_SCHEMATIC, encoding="utf-8")

        result = run_set_reference(parent, ref="LED1", new_ref="D1", backup=False)
        assert result == 0

        child_text = child.read_text(encoding="utf-8")
        assert '(property "Reference" "D1"' in child_text
        assert '(reference "D1")' in child_text
        assert "LED1" not in child_text

        # Parent should be unchanged (no LED1 in parent)
        parent_text = parent.read_text(encoding="utf-8")
        assert '(property "Reference" "R1"' in parent_text


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_mapping_file(self, tmp_schematic: Path, tmp_path: Path):
        map_file = tmp_path / "empty.json"
        map_file.write_text("{}", encoding="utf-8")
        result = run_set_reference(tmp_schematic, map_path=map_file, backup=False)
        assert result == 1  # Empty mapping is an error

    def test_missing_map_file(self, tmp_schematic: Path, tmp_path: Path):
        missing = tmp_path / "missing.json"
        result = run_set_reference(tmp_schematic, map_path=missing, backup=False)
        assert result == 1

    def test_identity_mapping_noop(self, tmp_schematic: Path, tmp_path: Path):
        """Renaming R1 to R1 should be a no-op."""
        map_file = tmp_path / "identity.json"
        map_file.write_text(json.dumps({"R1": "R1"}), encoding="utf-8")
        original_text = tmp_schematic.read_text(encoding="utf-8")
        result = run_set_reference(tmp_schematic, map_path=map_file, backup=False)
        assert result == 0
        after_text = tmp_schematic.read_text(encoding="utf-8")
        assert original_text == after_text

    def test_no_args_error(self, tmp_schematic: Path):
        result = run_set_reference(tmp_schematic)
        assert result == 1
