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
