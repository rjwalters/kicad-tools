"""Tests for the sch set-label-direction command.

Covers shape replacement for global and hierarchical labels, name filtering,
dry-run mode, hierarchy traversal, sheet filtering, and edge cases.
"""

import pytest

from kicad_tools.cli.sch_set_label_direction import (
    find_label_shape_occurrences,
    main,
    replace_label_shapes,
    set_label_direction,
)

# ---------------------------------------------------------------------------
# Minimal schematic content for testing
# ---------------------------------------------------------------------------

SCHEMATIC_WITH_GLOBAL_LABELS = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(global_label "SDA" (shape input)
\t\t(at 100 50 0)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t)
\t(global_label "SCL" (shape input)
\t\t(at 120 50 0)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t)
)
"""

SCHEMATIC_WITH_HIERARCHICAL_LABELS = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(uuid "00000000-0000-0000-0000-000000000002")
\t(paper "A4")
\t(hierarchical_label "SDA" (shape input)
\t\t(at 100 50 0)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t)
\t(hierarchical_label "MCLK" (shape output)
\t\t(at 120 50 0)
\t\t(uuid "44444444-4444-4444-4444-444444444444")
\t)
)
"""

SCHEMATIC_WITH_BOTH = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(uuid "00000000-0000-0000-0000-000000000003")
\t(paper "A4")
\t(global_label "SDA" (shape input)
\t\t(at 100 50 0)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t)
\t(hierarchical_label "SDA" (shape input)
\t\t(at 120 50 0)
\t\t(uuid "55555555-5555-5555-5555-555555555555")
\t)
\t(global_label "SCL" (shape output)
\t\t(at 140 50 0)
\t\t(uuid "66666666-6666-6666-6666-666666666666")
\t)
)
"""

ROOT_SCHEMATIC_WITH_SUBSHEET = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(uuid "00000000-0000-0000-0000-000000000010")
\t(paper "A4")
\t(global_label "SDA" (shape input)
\t\t(at 100 50 0)
\t\t(uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
\t)
\t(sheet
\t\t(at 50 50)
\t\t(size 20 20)
\t\t(property "Sheetname" "DAC")
\t\t(property "Sheetfile" "dac.kicad_sch")
\t\t(pin "SDA" input (at 50 60 0)
\t\t\t(uuid "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
\t\t)
\t)
)
"""

SUBSHEET_CONTENT = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(uuid "00000000-0000-0000-0000-000000000020")
\t(paper "A4")
\t(hierarchical_label "SDA" (shape input)
\t\t(at 10 20 0)
\t\t(uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
\t)
)
"""


# ---------------------------------------------------------------------------
# Unit tests - find_label_shape_occurrences
# ---------------------------------------------------------------------------


class TestFindLabelShapeOccurrences:
    def test_finds_global_labels(self):
        changes = find_label_shape_occurrences(
            "/fake.kicad_sch", "SDA", SCHEMATIC_WITH_GLOBAL_LABELS
        )
        assert len(changes) == 1
        assert changes[0].element_type == "global_label"
        assert changes[0].old_shape == "input"

    def test_finds_hierarchical_labels(self):
        changes = find_label_shape_occurrences(
            "/fake.kicad_sch", "SDA", SCHEMATIC_WITH_HIERARCHICAL_LABELS
        )
        assert len(changes) == 1
        assert changes[0].element_type == "hierarchical_label"
        assert changes[0].old_shape == "input"

    def test_finds_both_types(self):
        changes = find_label_shape_occurrences("/fake.kicad_sch", "SDA", SCHEMATIC_WITH_BOTH)
        assert len(changes) == 2
        types = {c.element_type for c in changes}
        assert types == {"global_label", "hierarchical_label"}

    def test_name_filtering(self):
        """Only labels matching the given name are returned."""
        changes = find_label_shape_occurrences("/fake.kicad_sch", "SCL", SCHEMATIC_WITH_BOTH)
        assert len(changes) == 1
        assert changes[0].label_name == "SCL"
        assert changes[0].element_type == "global_label"
        assert changes[0].old_shape == "output"

    def test_no_matches(self):
        changes = find_label_shape_occurrences(
            "/fake.kicad_sch", "NONEXISTENT", SCHEMATIC_WITH_GLOBAL_LABELS
        )
        assert changes == []


# ---------------------------------------------------------------------------
# Unit tests - replace_label_shapes
# ---------------------------------------------------------------------------


class TestReplaceLabelShapes:
    def test_global_label_shape_change(self):
        text, count = replace_label_shapes(SCHEMATIC_WITH_GLOBAL_LABELS, "SDA", "bidirectional")
        assert count == 1
        assert '(global_label "SDA" (shape bidirectional)' in text
        # SCL should be untouched
        assert '(global_label "SCL" (shape input)' in text

    def test_hierarchical_label_shape_change(self):
        text, count = replace_label_shapes(
            SCHEMATIC_WITH_HIERARCHICAL_LABELS, "SDA", "bidirectional"
        )
        assert count == 1
        assert '(hierarchical_label "SDA" (shape bidirectional)' in text
        # MCLK should be untouched
        assert '(hierarchical_label "MCLK" (shape output)' in text

    def test_replaces_both_types(self):
        text, count = replace_label_shapes(SCHEMATIC_WITH_BOTH, "SDA", "passive")
        assert count == 2
        assert '(global_label "SDA" (shape passive)' in text
        assert '(hierarchical_label "SDA" (shape passive)' in text
        # SCL should be untouched
        assert '(global_label "SCL" (shape output)' in text

    def test_name_filtering_in_replacement(self):
        """Only the named label's shape is changed; others are untouched."""
        text, count = replace_label_shapes(SCHEMATIC_WITH_GLOBAL_LABELS, "SCL", "tri_state")
        assert count == 1
        assert '(global_label "SCL" (shape tri_state)' in text
        assert '(global_label "SDA" (shape input)' in text

    def test_no_matches_returns_zero(self):
        text, count = replace_label_shapes(SCHEMATIC_WITH_GLOBAL_LABELS, "NONEXISTENT", "output")
        assert count == 0
        assert text == SCHEMATIC_WITH_GLOBAL_LABELS


# ---------------------------------------------------------------------------
# Integration test - set_label_direction with files on disk
# ---------------------------------------------------------------------------


class TestSetLabelDirection:
    def test_dry_run_does_not_modify(self, tmp_path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_WITH_GLOBAL_LABELS, encoding="utf-8")

        result = set_label_direction(
            str(sch_file), name="SDA", new_shape="bidirectional", dry_run=True
        )

        assert result.success
        assert len(result.changes) == 1
        assert result.files_modified == set()
        # File should not be changed
        assert sch_file.read_text(encoding="utf-8") == SCHEMATIC_WITH_GLOBAL_LABELS

    def test_applies_changes(self, tmp_path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_WITH_GLOBAL_LABELS, encoding="utf-8")

        result = set_label_direction(str(sch_file), name="SDA", new_shape="bidirectional")

        assert result.success
        assert len(result.changes) == 1
        assert str(sch_file) in result.files_modified
        modified = sch_file.read_text(encoding="utf-8")
        assert '(global_label "SDA" (shape bidirectional)' in modified

    def test_label_not_found(self, tmp_path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_WITH_GLOBAL_LABELS, encoding="utf-8")

        result = set_label_direction(str(sch_file), name="MISSING", new_shape="output")

        assert result.success
        assert result.changes == []
        assert "No labels named" in (result.error or "")

    def test_file_not_found(self):
        result = set_label_direction("/nonexistent.kicad_sch", name="SDA", new_shape="output")
        assert not result.success
        assert "not found" in (result.error or "").lower()

    def test_hierarchy_traversal(self, tmp_path):
        """Both root and sub-sheet labels are updated."""
        root_file = tmp_path / "root.kicad_sch"
        sub_file = tmp_path / "dac.kicad_sch"
        root_file.write_text(ROOT_SCHEMATIC_WITH_SUBSHEET, encoding="utf-8")
        sub_file.write_text(SUBSHEET_CONTENT, encoding="utf-8")

        result = set_label_direction(str(root_file), name="SDA", new_shape="bidirectional")

        assert result.success
        assert len(result.changes) == 2
        assert str(root_file) in result.files_modified
        assert str(sub_file) in result.files_modified

        root_text = root_file.read_text(encoding="utf-8")
        sub_text = sub_file.read_text(encoding="utf-8")
        assert '(global_label "SDA" (shape bidirectional)' in root_text
        assert '(hierarchical_label "SDA" (shape bidirectional)' in sub_text

    def test_sheet_filter(self, tmp_path):
        """With --sheet, only the matching sheet is modified."""
        root_file = tmp_path / "root.kicad_sch"
        sub_file = tmp_path / "dac.kicad_sch"
        root_file.write_text(ROOT_SCHEMATIC_WITH_SUBSHEET, encoding="utf-8")
        sub_file.write_text(SUBSHEET_CONTENT, encoding="utf-8")

        result = set_label_direction(
            str(root_file), name="SDA", new_shape="bidirectional", sheet_filter="dac"
        )

        assert result.success
        assert len(result.changes) == 1
        assert str(sub_file) in result.files_modified
        # Root should be unmodified
        root_text = root_file.read_text(encoding="utf-8")
        assert '(global_label "SDA" (shape input)' in root_text

    def test_sheet_filter_case_insensitive(self, tmp_path):
        """Sheet filter should match regardless of case (e.g., 'DAC' matches 'dac.kicad_sch')."""
        root_file = tmp_path / "root.kicad_sch"
        sub_file = tmp_path / "dac.kicad_sch"
        root_file.write_text(ROOT_SCHEMATIC_WITH_SUBSHEET, encoding="utf-8")
        sub_file.write_text(SUBSHEET_CONTENT, encoding="utf-8")

        # Uppercase filter against lowercase filename
        result = set_label_direction(
            str(root_file), name="SDA", new_shape="bidirectional", sheet_filter="DAC"
        )

        assert result.success
        assert len(result.changes) == 1
        assert str(sub_file) in result.files_modified
        # Root should be unmodified
        root_text = root_file.read_text(encoding="utf-8")
        assert '(global_label "SDA" (shape input)' in root_text

    def test_sheet_filter_mixed_case(self, tmp_path):
        """Mixed-case filter (e.g., 'dAc') should also match."""
        root_file = tmp_path / "root.kicad_sch"
        sub_file = tmp_path / "dac.kicad_sch"
        root_file.write_text(ROOT_SCHEMATIC_WITH_SUBSHEET, encoding="utf-8")
        sub_file.write_text(SUBSHEET_CONTENT, encoding="utf-8")

        result = set_label_direction(
            str(root_file), name="SDA", new_shape="bidirectional", sheet_filter="dAc"
        )

        assert result.success
        assert len(result.changes) == 1
        assert str(sub_file) in result.files_modified

    def test_backup_created(self, tmp_path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_WITH_GLOBAL_LABELS, encoding="utf-8")

        result = set_label_direction(str(sch_file), name="SDA", new_shape="output", backup=True)

        assert result.success
        # A backup file should exist in the same directory
        backup_files = list(tmp_path.glob("test_backup_*"))
        assert len(backup_files) == 1


# ---------------------------------------------------------------------------
# CLI main() tests
# ---------------------------------------------------------------------------


class TestMain:
    def test_dry_run_cli(self, tmp_path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_WITH_GLOBAL_LABELS, encoding="utf-8")

        rc = main([str(sch_file), "--name", "SDA", "--shape", "bidirectional", "--dry-run"])
        assert rc == 0
        # File should not be changed
        assert sch_file.read_text(encoding="utf-8") == SCHEMATIC_WITH_GLOBAL_LABELS

    def test_invalid_shape_rejected(self, tmp_path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_WITH_GLOBAL_LABELS, encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            main([str(sch_file), "--name", "SDA", "--shape", "invalid_shape"])
        assert exc_info.value.code != 0

    def test_not_a_schematic_file(self, tmp_path):
        txt_file = tmp_path / "test.txt"
        txt_file.write_text("hello", encoding="utf-8")

        rc = main([str(txt_file), "--name", "SDA", "--shape", "input"])
        assert rc == 1

    def test_file_not_found_cli(self):
        rc = main(["/nonexistent.kicad_sch", "--name", "SDA", "--shape", "input"])
        assert rc == 1

    def test_label_not_found_exits_zero(self, tmp_path):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(SCHEMATIC_WITH_GLOBAL_LABELS, encoding="utf-8")

        rc = main([str(sch_file), "--name", "NONEXISTENT", "--shape", "output"])
        assert rc == 0
