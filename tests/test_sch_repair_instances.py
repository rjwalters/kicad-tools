"""Tests for the sch repair-instances command.

Covers detection of missing project instances, assignment of
non-conflicting reference designators, dry-run mode, backup,
and hierarchical schematic support.
"""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.sch_repair_instances import (
    _collect_existing_refs,
    _extract_symbols_with_instance_info,
    run_repair_instances,
)

# ---------------------------------------------------------------------------
# Minimal schematic content for testing
# ---------------------------------------------------------------------------

# Root schematic with one annotated symbol (has instances) and a sub-sheet
ROOT_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
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
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-1")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-uuid-2")
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test_project"
\t\t\t\t(path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
\t\t\t\t\t(reference "R1")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
\t(sheet
\t\t(at 150 50)
\t\t(size 20 15)
\t\t(uuid "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
\t\t(property "Sheetname" "sub"
\t\t\t(at 150 49 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Sheetfile" "sub.kicad_sch"
\t\t\t(at 150 65.5 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t)
)
"""

# Sub-sheet with two symbols missing instances
SUB_SCHEMATIC_MISSING = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R2"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-3")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-uuid-4")
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 50 0)
\t\t(property "Reference" "C?"
\t\t\t(at 120 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100nF"
\t\t\t(at 120 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-5")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-uuid-6")
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t)
)
"""

# Sub-sheet where all symbols already have instances
SUB_SCHEMATIC_OK = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "dddddddd-dddd-dddd-dddd-dddddddddddd")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R3"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "1k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-7")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-uuid-8")
\t\t)
\t\t(uuid "44444444-4444-4444-4444-444444444444")
\t\t(instances
\t\t\t(project "test_project"
\t\t\t\t(path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
\t\t\t\t\t(reference "R3")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""


class TestExtractSymbolsWithInstanceInfo:
    """Tests for _extract_symbols_with_instance_info."""

    def test_symbol_with_instances(self):
        symbols = _extract_symbols_with_instance_info(
            ROOT_SCHEMATIC, "test_project"
        )
        assert len(symbols) == 1
        assert symbols[0]["reference"] == "R1"
        assert symbols[0]["has_project_instance"] is True

    def test_symbol_missing_instances(self):
        symbols = _extract_symbols_with_instance_info(
            SUB_SCHEMATIC_MISSING, "test_project"
        )
        assert len(symbols) == 2
        # Both should be missing instances
        assert all(not s["has_project_instance"] for s in symbols)
        refs = {s["reference"] for s in symbols}
        assert refs == {"R2", "C?"}

    def test_symbol_with_wrong_project(self):
        """Symbols with instances for a different project are flagged."""
        symbols = _extract_symbols_with_instance_info(
            ROOT_SCHEMATIC, "other_project"
        )
        assert len(symbols) == 1
        # Has instances but not for "other_project"
        assert symbols[0]["has_project_instance"] is False

    def test_power_symbols_skipped(self):
        """Power symbols (lib_id starting with power:) are skipped."""
        text = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(uuid "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "power:GND")
\t\t(at 100 50 0)
\t\t(property "Reference" "#PWR01"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "GND"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-pwr")
\t\t)
\t\t(uuid "55555555-5555-5555-5555-555555555555")
\t)
)
"""
        symbols = _extract_symbols_with_instance_info(text, "test_project")
        assert len(symbols) == 0


class TestCollectExistingRefs:
    """Tests for _collect_existing_refs."""

    def test_collects_annotated_refs(self):
        file_symbols = {
            Path("a.kicad_sch"): [
                {"prefix": "R", "number": 1, "has_project_instance": True},
                {"prefix": "R", "number": 3, "has_project_instance": True},
                {"prefix": "C", "number": 1, "has_project_instance": True},
            ]
        }
        existing = _collect_existing_refs(file_symbols)
        assert existing["R"] == {1, 3}
        assert existing["C"] == {1}

    def test_skips_unannotated(self):
        file_symbols = {
            Path("a.kicad_sch"): [
                {"prefix": "R", "number": None, "has_project_instance": False},
            ]
        }
        existing = _collect_existing_refs(file_symbols)
        assert "R" not in existing

    def test_skips_symbols_without_instances(self):
        file_symbols = {
            Path("a.kicad_sch"): [
                {"prefix": "R", "number": 5, "has_project_instance": False},
            ]
        }
        existing = _collect_existing_refs(file_symbols)
        assert "R" not in existing


class TestRunRepairInstances:
    """Integration tests for run_repair_instances."""

    def test_dry_run_finds_missing(self, tmp_path, capsys):
        """Dry run identifies missing instances without modifying files."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_MISSING, encoding="utf-8")

        result = run_repair_instances(root, dry_run=True, backup=False)
        assert result == 0

        captured = capsys.readouterr()
        assert "2 symbol(s) needing repair" in captured.out
        assert "Dry run: no changes made" in captured.out
        # File should not be modified
        assert sub.read_text(encoding="utf-8") == SUB_SCHEMATIC_MISSING

    def test_dry_run_json(self, tmp_path, capsys):
        """Dry run with JSON output."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_MISSING, encoding="utf-8")

        result = run_repair_instances(
            root, dry_run=True, backup=False, format="json"
        )
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["total"] == 2
        assert data["dry_run"] is True

    def test_repairs_missing_instances(self, tmp_path, capsys):
        """Actually repairs missing instances."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_MISSING, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        # Verify the sub-sheet was modified
        modified_text = sub.read_text(encoding="utf-8")
        assert '(instances' in modified_text
        assert '(project "test_project"' in modified_text

        # R2 should keep its reference
        assert '(reference "R2")' in modified_text

        # C? should have been assigned C1
        assert '(reference "C1")' in modified_text

    def test_preserves_existing_annotated_refs(self, tmp_path, capsys):
        """Annotated symbols keep their reference designators."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_MISSING, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        modified_text = sub.read_text(encoding="utf-8")
        # R2 should keep its reference (annotated but missing instances)
        assert '(reference "R2")' in modified_text

    def test_avoids_ref_conflicts(self, tmp_path, capsys):
        """New refs assigned to unannotated symbols avoid existing numbers."""
        # Create a root with R1 already in use
        root = tmp_path / "test_project.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")

        # Sub-sheet with unannotated R? (should get R2, not R1)
        sub_text = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R?"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-3")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-uuid-4")
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t)
)
"""
        sub = tmp_path / "sub.kicad_sch"
        sub.write_text(sub_text, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        modified_text = sub.read_text(encoding="utf-8")
        # Should get R2 (R1 is already used in root)
        assert '(reference "R2")' in modified_text

    def test_no_changes_needed(self, tmp_path, capsys):
        """Returns 0 and reports no changes when all instances present."""
        root = tmp_path / "test_project.kicad_sch"
        # Use a root schematic without sub-sheets referencing missing files
        simple_root = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
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
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-1")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-uuid-2")
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test_project"
\t\t\t\t(path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
\t\t\t\t\t(reference "R1")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""
        root.write_text(simple_root, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        captured = capsys.readouterr()
        assert "No repairs needed" in captured.out

    def test_backup_created(self, tmp_path, capsys):
        """Backup files are created when backup=True."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_MISSING, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=True)
        assert result == 0

        # Check that backup file exists — create_backup() produces
        # <stem>_backup_<timestamp><suffix>, e.g. sub_backup_20260423_120000.kicad_sch
        backups = list(tmp_path.glob("*_backup_*.kicad_sch"))
        assert len(backups) >= 1

    def test_file_not_found(self, tmp_path, capsys):
        """Returns 1 for non-existent file."""
        result = run_repair_instances(
            tmp_path / "nonexistent.kicad_sch",
            dry_run=True,
            backup=False,
        )
        assert result == 1

    def test_correct_instance_path(self, tmp_path, capsys):
        """Instance path includes root UUID and sheet UUID."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_MISSING, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        modified_text = sub.read_text(encoding="utf-8")
        # The instance path should be /root-uuid/sheet-uuid
        expected_path = (
            "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            "/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        )
        assert f'(path "{expected_path}"' in modified_text


# ---------------------------------------------------------------------------
# Tests for wrong-project detection and repair
# ---------------------------------------------------------------------------

# Sub-sheet with a symbol that has instances for the WRONG project
SUB_SCHEMATIC_WRONG_PROJECT = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R2"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-3")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-uuid-4")
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "wrong_project_name"
\t\t\t\t(path "/old-uuid/old-sheet-uuid"
\t\t\t\t\t(reference "R2")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""

# Sub-sheet with a PWR_FLAG that has instances for the WRONG project
SUB_SCHEMATIC_WRONG_PROJECT_POWER = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "power:PWR_FLAG")
\t\t(at 100 50 0)
\t\t(property "Reference" "#PWR_FLAG"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "PWR_FLAG"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-pwr")
\t\t)
\t\t(uuid "66666666-6666-6666-6666-666666666666")
\t\t(instances
\t\t\t(project "wrong_project_name"
\t\t\t\t(path "/old-uuid"
\t\t\t\t\t(reference "#FLG01")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""


class TestWrongProjectDetection:
    """Tests for wrong-project-name detection and replacement."""

    def test_extract_detects_wrong_project(self):
        """Symbol with instances for wrong project is flagged."""
        symbols = _extract_symbols_with_instance_info(
            SUB_SCHEMATIC_WRONG_PROJECT, "test_project"
        )
        assert len(symbols) == 1
        assert symbols[0]["has_project_instance"] is False
        assert symbols[0]["has_wrong_project"] is True

    def test_wrong_project_replaced_not_appended(self, tmp_path, capsys):
        """Wrong project name is replaced, not duplicated."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_WRONG_PROJECT, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        modified_text = sub.read_text(encoding="utf-8")
        # Correct project name should appear
        assert '(project "test_project"' in modified_text
        # Wrong project name should be gone
        assert "wrong_project_name" not in modified_text
        # Should only have ONE (project entry, not two
        assert modified_text.count("(project") == 1

    def test_wrong_project_updates_path_and_ref(self, tmp_path, capsys):
        """Wrong project replacement also updates path and reference."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_WRONG_PROJECT, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        modified_text = sub.read_text(encoding="utf-8")
        expected_path = (
            "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            "/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        )
        assert f'(path "{expected_path}"' in modified_text
        assert '(reference "R2")' in modified_text

    def test_dry_run_reports_wrong_project(self, tmp_path, capsys):
        """Dry-run distinguishes wrong-project from missing-instances."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_WRONG_PROJECT, encoding="utf-8")

        result = run_repair_instances(root, dry_run=True, backup=False)
        assert result == 0

        captured = capsys.readouterr()
        assert "[wrong project]" in captured.out

    def test_json_output_includes_repair_type(self, tmp_path, capsys):
        """JSON output includes repair_type field."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_WRONG_PROJECT, encoding="utf-8")

        result = run_repair_instances(
            root, dry_run=True, backup=False, format="json"
        )
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["repairs"][0]["repair_type"] == "wrong_project"


class TestPowerSymbolWrongProject:
    """Tests for power symbols with wrong project names."""

    def test_power_symbol_no_instances_still_skipped(self):
        """Power symbols with no instances block are still skipped."""
        text = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(uuid "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "power:GND")
\t\t(at 100 50 0)
\t\t(property "Reference" "#PWR01"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "GND"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-pwr")
\t\t)
\t\t(uuid "55555555-5555-5555-5555-555555555555")
\t)
)
"""
        symbols = _extract_symbols_with_instance_info(text, "test_project")
        assert len(symbols) == 0

    def test_power_symbol_correct_project_skipped(self):
        """Power symbols with the correct project are skipped."""
        text = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(uuid "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "power:GND")
\t\t(at 100 50 0)
\t\t(property "Reference" "#PWR01"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "GND"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-pwr")
\t\t)
\t\t(uuid "55555555-5555-5555-5555-555555555555")
\t\t(instances
\t\t\t(project "test_project"
\t\t\t\t(path "/eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
\t\t\t\t\t(reference "#PWR01")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""
        symbols = _extract_symbols_with_instance_info(text, "test_project")
        assert len(symbols) == 0

    def test_power_symbol_wrong_project_detected(self):
        """Power symbols with wrong project ARE detected for repair."""
        symbols = _extract_symbols_with_instance_info(
            SUB_SCHEMATIC_WRONG_PROJECT_POWER, "test_project"
        )
        assert len(symbols) == 1
        assert symbols[0]["has_project_instance"] is False
        assert symbols[0]["has_wrong_project"] is True
        assert symbols[0]["lib_id"] == "power:PWR_FLAG"

    def test_power_symbol_wrong_project_repaired(self, tmp_path, capsys):
        """Power symbol with wrong project gets its project name fixed."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_WRONG_PROJECT_POWER, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        modified_text = sub.read_text(encoding="utf-8")
        assert '(project "test_project"' in modified_text
        assert "wrong_project_name" not in modified_text


# ---------------------------------------------------------------------------
# Tests for loose-project-block detection and repair (issue #2624)
# ---------------------------------------------------------------------------

# Sub-sheet where a symbol has TWO (project ...) blocks at symbol-child
# indent (siblings of, not children of, (instances)) AND an empty
# (instances) block.  This is the malformed shape produced by KiCad
# variants / stale standalone-sheet edits, where kicad-cli silently drops
# the symbol from the netlist.  See issue #2624.
SUB_SCHEMATIC_LOOSE_PROJECT = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R2"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-3")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-uuid-4")
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(project "test_project"
\t\t\t(path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
\t\t\t\t(reference "R2")
\t\t\t\t(unit 1)
\t\t\t)
\t\t)
\t\t(project "sub"
\t\t\t(path "/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
\t\t\t\t(reference "R2")
\t\t\t\t(unit 1)
\t\t\t)
\t\t)
\t\t(instances)
\t)
)
"""


# Variant: only the correct-project loose block is present (no stray
# standalone-sheet leftover) — still malformed because it's at symbol-child
# indent and (instances) is empty.
SUB_SCHEMATIC_LOOSE_PROJECT_SINGLE = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R2"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-3")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-uuid-4")
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(project "test_project"
\t\t\t(path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
\t\t\t\t(reference "R2")
\t\t\t\t(unit 1)
\t\t\t)
\t\t)
\t\t(instances)
\t)
)
"""


# Variant: well-formed instances block (with our project) AND a stray
# sibling (project "other" ...) at symbol-child indent.  This must still
# be flagged for repair: the sibling has to be dropped.
SUB_SCHEMATIC_LOOSE_PROJECT_PARTIAL = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R2"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-3")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-uuid-4")
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(project "stale_sheet"
\t\t\t(path "/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
\t\t\t\t(reference "R2")
\t\t\t\t(unit 1)
\t\t\t)
\t\t)
\t\t(instances
\t\t\t(project "test_project"
\t\t\t\t(path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
\t\t\t\t\t(reference "R2")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""


class TestLooseProjectBlocksDetection:
    """Tests for detection of (project ...) blocks at symbol-child indent."""

    def test_loose_project_detected(self):
        """Symbol with loose (project) siblings of empty (instances)."""
        symbols = _extract_symbols_with_instance_info(
            SUB_SCHEMATIC_LOOSE_PROJECT, "test_project"
        )
        assert len(symbols) == 1
        s = symbols[0]
        assert s["reference"] == "R2"
        # has_project_instance must be False (project is NOT inside instances)
        assert s["has_project_instance"] is False
        # has_loose_project_blocks must be True (project is at symbol-child)
        assert s["has_loose_project_blocks"] is True
        # has_wrong_project must be False (loose-project repair takes
        # precedence; wrong_project is reserved for the well-formed-but-
        # wrong-name shape, which this isn't)
        assert s["has_wrong_project"] is False
        # Both loose blocks should be captured
        names = {p["name"] for p in s["loose_project_blocks"]}
        assert names == {"test_project", "sub"}

    def test_well_formed_does_not_report_loose(self):
        """Properly nested (project) inside (instances) is NOT flagged loose."""
        symbols = _extract_symbols_with_instance_info(
            SUB_SCHEMATIC_OK, "test_project"
        )
        assert len(symbols) == 1
        assert symbols[0]["has_loose_project_blocks"] is False

    def test_wrong_project_does_not_report_loose(self):
        """Wrong-project (still inside instances) is NOT flagged loose."""
        symbols = _extract_symbols_with_instance_info(
            SUB_SCHEMATIC_WRONG_PROJECT, "test_project"
        )
        assert len(symbols) == 1
        assert symbols[0]["has_loose_project_blocks"] is False
        assert symbols[0]["has_wrong_project"] is True

    def test_missing_instances_does_not_report_loose(self):
        """Symbol with no instances at all is NOT flagged loose."""
        symbols = _extract_symbols_with_instance_info(
            SUB_SCHEMATIC_MISSING, "test_project"
        )
        # Two symbols, both have no instances and no loose blocks
        assert len(symbols) == 2
        assert all(not s["has_loose_project_blocks"] for s in symbols)

    def test_loose_project_single_block(self):
        """Single loose (project) sibling of empty (instances) is detected."""
        symbols = _extract_symbols_with_instance_info(
            SUB_SCHEMATIC_LOOSE_PROJECT_SINGLE, "test_project"
        )
        assert len(symbols) == 1
        s = symbols[0]
        assert s["has_project_instance"] is False
        assert s["has_loose_project_blocks"] is True
        names = {p["name"] for p in s["loose_project_blocks"]}
        assert names == {"test_project"}

    def test_partial_loose_with_correct_instances(self):
        """Well-formed instances + stray loose sibling is also flagged."""
        symbols = _extract_symbols_with_instance_info(
            SUB_SCHEMATIC_LOOSE_PROJECT_PARTIAL, "test_project"
        )
        assert len(symbols) == 1
        s = symbols[0]
        # The instances block names the correct project, but the stray
        # sibling at symbol-child indent must still be cleaned up.
        # has_project_instance reports the structural truth (project IS
        # nested correctly inside instances).
        assert s["has_project_instance"] is True
        assert s["has_loose_project_blocks"] is True
        names = {p["name"] for p in s["loose_project_blocks"]}
        assert names == {"stale_sheet"}


class TestLooseProjectBlocksRepair:
    """End-to-end tests for repairing loose-project-block schematics."""

    def test_dry_run_flags_loose_project(self, tmp_path, capsys):
        """Dry run identifies loose-project repairs with the right tag."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_LOOSE_PROJECT, encoding="utf-8")

        result = run_repair_instances(root, dry_run=True, backup=False)
        assert result == 0

        captured = capsys.readouterr()
        assert "1 symbol(s) needing repair" in captured.out
        assert "[loose project blocks]" in captured.out
        assert "1 loose project blocks" in captured.out
        # File should not be modified
        assert sub.read_text(encoding="utf-8") == SUB_SCHEMATIC_LOOSE_PROJECT

    def test_dry_run_json_reports_loose_project(self, tmp_path, capsys):
        """JSON dry-run output sets repair_type=loose_project_blocks."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_LOOSE_PROJECT, encoding="utf-8")

        result = run_repair_instances(
            root, dry_run=True, backup=False, format="json"
        )
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["total"] == 1
        assert data["repairs"][0]["repair_type"] == "loose_project_blocks"
        # Reference must be preserved (R2 -> R2, no re-annotation)
        assert data["repairs"][0]["old_ref"] == "R2"
        assert data["repairs"][0]["new_ref"] == "R2"

    def test_repairs_loose_project_blocks(self, tmp_path, capsys):
        """Actually rewrites the malformed shape into a well-formed one."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_LOOSE_PROJECT, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        modified_text = sub.read_text(encoding="utf-8")
        # Exactly one (project ...) form should remain, nested inside
        # (instances).
        assert modified_text.count("(project") == 1
        assert '(project "test_project"' in modified_text
        # The stale standalone-sheet leftover must be gone.
        assert '(project "sub"' not in modified_text
        # Reference must be preserved (R2 -> R2, no re-annotation).
        assert '(reference "R2")' in modified_text
        # The path inside (instances) is the canonical hierarchical path.
        expected_path = (
            "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            "/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        )
        assert f'(path "{expected_path}"' in modified_text

    def test_repair_removes_loose_blocks_from_symbol(self, tmp_path, capsys):
        """After repair, no (project ...) at symbol-child indent."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_LOOSE_PROJECT, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        modified_text = sub.read_text(encoding="utf-8")
        # Re-run extraction on the repaired text: no loose blocks should
        # remain, and the project instance is correctly nested.
        symbols = _extract_symbols_with_instance_info(
            modified_text, "test_project"
        )
        assert len(symbols) == 1
        assert symbols[0]["has_loose_project_blocks"] is False
        assert symbols[0]["has_project_instance"] is True

    def test_repair_does_not_re_annotate_reference(self, tmp_path, capsys):
        """Loose-project repair must NOT re-annotate the reference."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_LOOSE_PROJECT, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        modified_text = sub.read_text(encoding="utf-8")
        # The Reference *property* on the symbol must remain "R2".
        assert '(property "Reference" "R2"' in modified_text

    def test_backup_created_for_loose_project_repair(
        self, tmp_path, capsys
    ):
        """Backup files are created when backup=True for loose-project repair."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_LOOSE_PROJECT, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=True)
        assert result == 0

        backups = list(tmp_path.glob("*_backup_*.kicad_sch"))
        assert len(backups) >= 1

    def test_repair_drops_stale_sibling_with_correct_instances(
        self, tmp_path, capsys
    ):
        """Stray sibling project gets dropped even when instances is correct."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(
            SUB_SCHEMATIC_LOOSE_PROJECT_PARTIAL, encoding="utf-8"
        )

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        modified_text = sub.read_text(encoding="utf-8")
        # The stale_sheet sibling block must be gone.
        assert '(project "stale_sheet"' not in modified_text
        assert "stale_sheet" not in modified_text
        # The correct project entry inside (instances) must remain.
        assert '(project "test_project"' in modified_text
        # Only one project entry total.
        assert modified_text.count("(project") == 1
        # Reference preserved.
        assert '(property "Reference" "R2"' in modified_text

    def test_other_symbols_unaffected(self, tmp_path, capsys):
        """Symbols not exhibiting the loose-project shape are untouched."""
        root = tmp_path / "test_project.kicad_sch"
        sub = tmp_path / "sub.kicad_sch"
        root.write_text(ROOT_SCHEMATIC, encoding="utf-8")
        sub.write_text(SUB_SCHEMATIC_LOOSE_PROJECT, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        # The root schematic has a properly-instanced R1.  After repair
        # of the sub-sheet, the root must be unchanged.
        assert root.read_text(encoding="utf-8") == ROOT_SCHEMATIC


class TestPwrFlagReferenceReannotation:
    """Tests for #PWR_FLAG bare-name reference re-annotation."""

    def test_parse_reference_pwr_flag(self):
        """_parse_reference handles #PWR_FLAG correctly."""
        from kicad_tools.cli.sch_re_annotate import _parse_reference

        prefix, number, suffix = _parse_reference("#PWR_FLAG")
        # Bare name with no number -- parsed as unannotated
        assert prefix == "#PWR_FLAG"
        assert number is None
        assert suffix == ""

    def test_parse_reference_pwr_flag_annotated(self):
        """_parse_reference handles #PWR_FLAG01 correctly."""
        from kicad_tools.cli.sch_re_annotate import _parse_reference

        prefix, number, suffix = _parse_reference("#PWR_FLAG01")
        assert prefix == "#PWR_FLAG"
        assert number == 1
        assert suffix == ""

    def test_pwr_flag_gets_flg_prefix(self, tmp_path, capsys):
        """#PWR_FLAG reference is re-annotated with #FLG prefix."""
        # Root with no existing #FLG refs
        simple_root = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "power:PWR_FLAG")
\t\t(at 100 50 0)
\t\t(property "Reference" "#PWR_FLAG"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "PWR_FLAG"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-uuid-flag")
\t\t)
\t\t(uuid "77777777-7777-7777-7777-777777777777")
\t\t(instances
\t\t\t(project "wrong_name"
\t\t\t\t(path "/old-uuid"
\t\t\t\t\t(reference "#PWR_FLAG")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""
        root = tmp_path / "test_project.kicad_sch"
        root.write_text(simple_root, encoding="utf-8")

        result = run_repair_instances(root, dry_run=False, backup=False)
        assert result == 0

        modified_text = root.read_text(encoding="utf-8")
        assert '(project "test_project"' in modified_text
        # The reference should be re-annotated as #FLG01
        assert '(reference "#FLG01")' in modified_text
