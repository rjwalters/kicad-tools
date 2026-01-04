"""Tests for sync-hierarchy command."""

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli.sch_sync_hierarchy import (
    SyncAction,
    SyncResult,
    _calculate_label_position,
    _get_schematic_size,
    analyze_hierarchy,
    execute_add_labels,
    execute_remove_orphan_pins,
    find_orphan_pins,
    format_analysis_report,
    main,
)
from kicad_tools.schema.hierarchy import HierarchicalLabelInfo
from kicad_tools.sexp import parse_sexp


@pytest.fixture
def hierarchical_project(tmp_path: Path) -> Path:
    """Copy hierarchical test project to temp directory for modification tests."""
    fixtures = Path(__file__).parent / "fixtures" / "projects"

    # Copy all files
    for f in [
        "hierarchical_main.kicad_sch",
        "logic_subsheet.kicad_sch",
        "output_subsheet.kicad_sch",
    ]:
        src = fixtures / f
        if src.exists():
            shutil.copy(src, tmp_path / f)

    return tmp_path / "hierarchical_main.kicad_sch"


class TestGetSchematicSize:
    """Tests for schematic size detection."""

    def test_a4_paper(self):
        """A4 paper should return correct size."""
        sexp = parse_sexp('(kicad_sch (paper "A4"))')
        width, height = _get_schematic_size(sexp)
        assert width == 297
        assert height == 210

    def test_us_letter(self):
        """US Letter should return correct size."""
        sexp = parse_sexp('(kicad_sch (paper "A"))')
        width, height = _get_schematic_size(sexp)
        assert width == 279.4
        assert height == 215.9

    def test_default_size(self):
        """Missing paper should default to A4."""
        sexp = parse_sexp("(kicad_sch)")
        width, height = _get_schematic_size(sexp)
        assert width == 297
        assert height == 210


class TestCalculateLabelPosition:
    """Tests for label position calculation."""

    def test_output_on_right_edge(self):
        """Output labels should be on right edge."""
        position, rotation = _calculate_label_position(
            direction="output",
            existing_labels=[],
            page_size=(297, 210),
        )
        # Right edge should be near width minus margin
        assert position[0] > 280
        assert rotation == 180

    def test_input_on_left_edge(self):
        """Input labels should be on left edge."""
        position, rotation = _calculate_label_position(
            direction="input",
            existing_labels=[],
            page_size=(297, 210),
        )
        # Left edge should be near margin
        assert position[0] < 20
        assert rotation == 0

    def test_bidirectional_on_left_edge(self):
        """Bidirectional labels should default to left edge."""
        position, rotation = _calculate_label_position(
            direction="bidirectional",
            existing_labels=[],
            page_size=(297, 210),
        )
        assert position[0] < 20
        assert rotation == 0

    def test_avoids_existing_labels(self):
        """New labels should avoid existing label positions."""
        existing = [
            HierarchicalLabelInfo(
                name="TEST",
                shape="input",
                position=(10.0, 52.5),  # Base Y position
                rotation=0,
                uuid="test-uuid",
            )
        ]
        position, rotation = _calculate_label_position(
            direction="input",
            existing_labels=existing,
            page_size=(297, 210),
        )
        # Should be offset from existing label
        assert abs(position[1] - 52.5) >= 5.08  # At least one spacing unit


class TestAnalyzeHierarchy:
    """Tests for hierarchy analysis."""

    def test_analyze_finds_missing_labels(self, hierarchical_project: Path):
        """Analysis should find missing labels."""
        actions = analyze_hierarchy(str(hierarchical_project))

        # The output_subsheet is missing a label for "LED" pin
        missing_led = [a for a in actions if a.name == "LED"]
        assert len(missing_led) == 1
        assert missing_led[0].action_type == "add_label"

    def test_analyze_correct_direction(self, hierarchical_project: Path):
        """Analysis should preserve pin direction."""
        actions = analyze_hierarchy(str(hierarchical_project))

        missing_led = [a for a in actions if a.name == "LED"]
        assert len(missing_led) == 1
        assert missing_led[0].direction == "output"


class TestFindOrphanPins:
    """Tests for finding orphan pins."""

    def test_find_orphan_pins(self, hierarchical_project: Path):
        """Should find pins without matching labels."""
        actions = find_orphan_pins(str(hierarchical_project))

        # The output sheet has pin "LED" without label
        orphan_led = [a for a in actions if a.name == "LED"]
        assert len(orphan_led) == 1
        assert orphan_led[0].action_type == "remove_pin"


class TestFormatAnalysisReport:
    """Tests for analysis report formatting."""

    def test_format_report_basic(self, hierarchical_project: Path):
        """Report should include basic sections."""
        report = format_analysis_report(str(hierarchical_project))

        assert "Hierarchy Sync Analysis" in report
        assert "Summary" in report

    def test_format_report_shows_missing(self, hierarchical_project: Path):
        """Report should show missing labels."""
        report = format_analysis_report(str(hierarchical_project))

        # Should mention the LED pin without label
        assert "LED" in report or "pins without labels" in report


class TestExecuteAddLabels:
    """Tests for adding missing labels."""

    def test_add_labels_dry_run(self, hierarchical_project: Path):
        """Dry run should not modify files."""
        results = execute_add_labels(str(hierarchical_project), dry_run=True)

        # Should have results for missing labels
        led_results = [r for r in results if r.action.name == "LED"]
        assert len(led_results) == 1
        assert led_results[0].success is True
        assert "Would add" in led_results[0].message

        # File should be unchanged
        output_subsheet = hierarchical_project.parent / "output_subsheet.kicad_sch"
        content = output_subsheet.read_text()
        assert 'hierarchical_label "LED"' not in content

    def test_add_labels_actual(self, hierarchical_project: Path):
        """Actual add should modify files."""
        results = execute_add_labels(str(hierarchical_project), dry_run=False)

        # Should have succeeded
        led_results = [r for r in results if r.action.name == "LED"]
        assert len(led_results) == 1
        assert led_results[0].success is True

        # File should be modified
        output_subsheet = hierarchical_project.parent / "output_subsheet.kicad_sch"
        content = output_subsheet.read_text()
        assert 'hierarchical_label "LED"' in content


class TestExecuteRemoveOrphanPins:
    """Tests for removing orphan pins."""

    def test_remove_orphan_pins_dry_run(self, hierarchical_project: Path):
        """Dry run should not modify files."""
        results = execute_remove_orphan_pins(str(hierarchical_project), dry_run=True)

        # Should have results for orphan pins
        led_results = [r for r in results if r.action.name == "LED"]
        assert len(led_results) == 1
        assert led_results[0].success is True
        assert "Would remove" in led_results[0].message

        # File should be unchanged
        content = hierarchical_project.read_text()
        assert '(pin "LED"' in content


class TestCliMain:
    """Tests for CLI main function."""

    def test_analysis_mode_text(self, hierarchical_project: Path, capsys):
        """Analysis mode should output text report."""
        result = main([str(hierarchical_project)])

        captured = capsys.readouterr()
        assert "Hierarchy Sync Analysis" in captured.out
        assert result == 0

    def test_analysis_mode_json(self, hierarchical_project: Path, capsys):
        """Analysis mode with --format json should output valid JSON."""
        result = main([str(hierarchical_project), "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "schematic" in data
        assert "issues" in data
        assert result == 0

    def test_add_labels_dry_run(self, hierarchical_project: Path, capsys):
        """--add-labels --dry-run should preview changes."""
        result = main([str(hierarchical_project), "--add-labels", "--dry-run"])

        captured = capsys.readouterr()
        assert "Dry run" in captured.out or "Would add" in captured.out
        assert result == 0

    def test_add_labels_json(self, hierarchical_project: Path, capsys):
        """--add-labels --format json should output valid JSON."""
        result = main([str(hierarchical_project), "--add-labels", "--dry-run", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert data["action"] == "add_labels"
        assert data["dry_run"] is True
        assert "results" in data
        assert result == 0

    def test_remove_orphan_pins_dry_run(self, hierarchical_project: Path, capsys):
        """--remove-orphan-pins --dry-run should preview changes."""
        result = main([str(hierarchical_project), "--remove-orphan-pins", "--dry-run"])

        captured = capsys.readouterr()
        assert "Dry run" in captured.out or "Would remove" in captured.out
        assert result == 0

    def test_missing_file_error(self, capsys):
        """Should return error for missing file."""
        result = main(["nonexistent.kicad_sch"])

        captured = capsys.readouterr()
        assert "not found" in captured.err or result == 1


class TestSyncAction:
    """Tests for SyncAction dataclass."""

    def test_sync_action_add_label(self):
        """SyncAction for add_label should have correct fields."""
        action = SyncAction(
            action_type="add_label",
            name="TEST",
            direction="output",
            file_path="child.kicad_sch",
            sheet_name="Child",
            parent_file="parent.kicad_sch",
        )

        assert action.action_type == "add_label"
        assert action.name == "TEST"
        assert action.direction == "output"

    def test_sync_action_remove_pin(self):
        """SyncAction for remove_pin should have correct fields."""
        action = SyncAction(
            action_type="remove_pin",
            name="ORPHAN",
            direction="input",
            file_path="parent.kicad_sch",
            sheet_name="Child",
            parent_file="parent.kicad_sch",
        )

        assert action.action_type == "remove_pin"
        assert action.name == "ORPHAN"


class TestSyncResult:
    """Tests for SyncResult dataclass."""

    def test_sync_result_success(self):
        """SyncResult should track success state."""
        action = SyncAction(
            action_type="add_label",
            name="TEST",
            direction="output",
            file_path="child.kicad_sch",
            sheet_name="Child",
            parent_file="parent.kicad_sch",
        )
        result = SyncResult(success=True, action=action, message="Added label")

        assert result.success is True
        assert "Added" in result.message

    def test_sync_result_failure(self):
        """SyncResult should track failure state."""
        action = SyncAction(
            action_type="add_label",
            name="TEST",
            direction="output",
            file_path="child.kicad_sch",
            sheet_name="Child",
            parent_file="parent.kicad_sch",
        )
        result = SyncResult(success=False, action=action, message="Failed to add")

        assert result.success is False
        assert "Failed" in result.message
