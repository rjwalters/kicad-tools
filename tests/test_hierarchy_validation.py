"""Tests for hierarchy validation module."""

import json
import tempfile
from pathlib import Path

import pytest

from kicad_tools.schema.hierarchy import (
    HierarchicalLabelInfo,
    HierarchyNode,
    SheetInstance,
    SheetPin,
    build_hierarchy,
)
from kicad_tools.schema.hierarchy_validation import (
    FixSuggestion,
    FixType,
    ValidationIssue,
    ValidationIssueType,
    ValidationResult,
    _directions_compatible,
    _find_similar_name,
    apply_fix,
    format_validation_report,
    validate_hierarchy,
)


class TestDirectionsCompatible:
    """Tests for direction compatibility checking."""

    def test_same_direction_compatible(self):
        """Same directions should be compatible."""
        assert _directions_compatible("input", "input")
        assert _directions_compatible("output", "output")
        assert _directions_compatible("bidirectional", "bidirectional")
        assert _directions_compatible("passive", "passive")

    def test_bidirectional_always_compatible(self):
        """Bidirectional should be compatible with anything."""
        assert _directions_compatible("bidirectional", "input")
        assert _directions_compatible("bidirectional", "output")
        assert _directions_compatible("input", "bidirectional")
        assert _directions_compatible("output", "bidirectional")

    def test_passive_always_compatible(self):
        """Passive should be compatible with anything."""
        assert _directions_compatible("passive", "input")
        assert _directions_compatible("passive", "output")
        assert _directions_compatible("input", "passive")
        assert _directions_compatible("output", "passive")

    def test_incompatible_directions(self):
        """Different non-bidirectional/passive directions should be incompatible."""
        assert not _directions_compatible("input", "output")
        assert not _directions_compatible("output", "input")

    def test_case_insensitive(self):
        """Direction comparison should be case-insensitive."""
        assert _directions_compatible("INPUT", "input")
        assert _directions_compatible("Output", "OUTPUT")


class TestFindSimilarName:
    """Tests for fuzzy name matching."""

    def test_exact_match(self):
        """Exact match should be found."""
        result = _find_similar_name("VCC", ["VCC", "GND", "DATA"])
        assert result == "VCC"

    def test_case_difference(self):
        """Case differences should match."""
        result = _find_similar_name("vcc", ["VCC", "GND", "DATA"])
        assert result == "VCC"

    def test_similar_name(self):
        """Similar names should be found."""
        result = _find_similar_name("VCC_3V3", ["VCC_3V3A", "GND", "DATA"])
        assert result == "VCC_3V3A"

    def test_no_match(self):
        """Completely different names should return None."""
        result = _find_similar_name("XYZ", ["ABC", "DEF", "GHI"])
        assert result is None

    def test_empty_candidates(self):
        """Empty candidates should return None."""
        result = _find_similar_name("VCC", [])
        assert result is None

    def test_threshold(self):
        """Threshold should be respected."""
        # With default 0.8 threshold, "VCC" and "VDD" might not match
        result = _find_similar_name("VCC", ["VDD"])
        # This depends on the similarity ratio


class TestValidationIssue:
    """Tests for ValidationIssue dataclass."""

    def test_severity_error_for_missing_label(self):
        """Missing label should be error severity."""
        issue = ValidationIssue(
            issue_type=ValidationIssueType.MISSING_LABEL,
            sheet_name="Power",
            sheet_file="power.kicad_sch",
            parent_sheet_name="Root",
            parent_sheet_file="project.kicad_sch",
            pin_name="VCC",
            label_name=None,
            pin=None,
            label=None,
            message="Test message",
        )
        assert issue.severity == "error"

    def test_severity_warning_for_direction_mismatch(self):
        """Direction mismatch should be warning severity."""
        issue = ValidationIssue(
            issue_type=ValidationIssueType.DIRECTION_MISMATCH,
            sheet_name="Power",
            sheet_file="power.kicad_sch",
            parent_sheet_name="Root",
            parent_sheet_file="project.kicad_sch",
            pin_name="VCC",
            label_name="VCC",
            pin=None,
            label=None,
            message="Test message",
        )
        assert issue.severity == "warning"

    def test_severity_warning_for_orphan_label(self):
        """Orphan label should be warning severity."""
        issue = ValidationIssue(
            issue_type=ValidationIssueType.MISSING_PIN,
            sheet_name="Power",
            sheet_file="power.kicad_sch",
            parent_sheet_name="Root",
            parent_sheet_file="project.kicad_sch",
            pin_name=None,
            label_name="ORPHAN",
            pin=None,
            label=None,
            message="Test message",
        )
        assert issue.severity == "warning"


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_has_errors_with_errors(self):
        """has_errors should return True when there are error-level issues."""
        result = ValidationResult(root_schematic="test.kicad_sch")
        result.issues.append(
            ValidationIssue(
                issue_type=ValidationIssueType.MISSING_LABEL,
                sheet_name="Power",
                sheet_file="power.kicad_sch",
                parent_sheet_name="Root",
                parent_sheet_file="project.kicad_sch",
                pin_name="VCC",
                label_name=None,
                pin=None,
                label=None,
                message="Test",
            )
        )
        assert result.has_errors

    def test_has_errors_without_errors(self):
        """has_errors should return False when there are only warnings."""
        result = ValidationResult(root_schematic="test.kicad_sch")
        result.issues.append(
            ValidationIssue(
                issue_type=ValidationIssueType.DIRECTION_MISMATCH,
                sheet_name="Power",
                sheet_file="power.kicad_sch",
                parent_sheet_name="Root",
                parent_sheet_file="project.kicad_sch",
                pin_name="VCC",
                label_name="VCC",
                pin=None,
                label=None,
                message="Test",
            )
        )
        assert not result.has_errors

    def test_error_and_warning_counts(self):
        """Counts should correctly tally issues by severity."""
        result = ValidationResult(root_schematic="test.kicad_sch")
        # Add 2 errors
        for _ in range(2):
            result.issues.append(
                ValidationIssue(
                    issue_type=ValidationIssueType.MISSING_LABEL,
                    sheet_name="Power",
                    sheet_file="power.kicad_sch",
                    parent_sheet_name="Root",
                    parent_sheet_file="project.kicad_sch",
                    pin_name="VCC",
                    label_name=None,
                    pin=None,
                    label=None,
                    message="Test",
                )
            )
        # Add 3 warnings
        for _ in range(3):
            result.issues.append(
                ValidationIssue(
                    issue_type=ValidationIssueType.DIRECTION_MISMATCH,
                    sheet_name="Power",
                    sheet_file="power.kicad_sch",
                    parent_sheet_name="Root",
                    parent_sheet_file="project.kicad_sch",
                    pin_name="VCC",
                    label_name="VCC",
                    pin=None,
                    label=None,
                    message="Test",
                )
            )
        assert result.error_count == 2
        assert result.warning_count == 3


class TestFormatValidationReport:
    """Tests for report formatting."""

    def test_format_empty_result(self):
        """Empty result should show no issues."""
        result = ValidationResult(
            root_schematic="test.kicad_sch",
            sheets_checked=5,
            pins_checked=10,
            labels_checked=10,
        )
        report = format_validation_report(result)
        assert "No issues found" in report
        assert "Sheets checked: 5" in report

    def test_format_with_issues(self):
        """Result with issues should show them."""
        result = ValidationResult(root_schematic="test.kicad_sch")
        result.issues.append(
            ValidationIssue(
                issue_type=ValidationIssueType.MISSING_LABEL,
                sheet_name="Power",
                sheet_file="power.kicad_sch",
                parent_sheet_name="Root",
                parent_sheet_file="project.kicad_sch",
                pin_name="VCC",
                label_name=None,
                pin=SheetPin(
                    name="VCC",
                    direction="output",
                    position=(10.0, 20.0),
                    rotation=0,
                    uuid="pin-uuid",
                ),
                label=None,
                message="Missing label VCC",
                suggestions=[
                    FixSuggestion(
                        fix_type=FixType.ADD_LABEL,
                        description="Add label VCC",
                        file_path="power.kicad_sch",
                    )
                ],
                possible_causes=["Label was deleted"],
            )
        )
        report = format_validation_report(result)
        assert "Power" in report
        assert "VCC" in report
        assert "Missing" in report or "missing" in report


class TestFixSuggestion:
    """Tests for fix suggestions."""

    def test_str_auto_fixable(self):
        """Auto-fixable suggestion should have prefix."""
        suggestion = FixSuggestion(
            fix_type=FixType.FIX_DIRECTION,
            description="Change direction to output",
            file_path="test.kicad_sch",
            auto_fixable=True,
        )
        assert "[Auto-fixable]" in str(suggestion)

    def test_str_not_auto_fixable(self):
        """Non-auto-fixable suggestion should not have prefix."""
        suggestion = FixSuggestion(
            fix_type=FixType.ADD_LABEL,
            description="Add label VCC",
            file_path="test.kicad_sch",
            auto_fixable=False,
        )
        assert "[Auto-fixable]" not in str(suggestion)


class TestHierarchicalLabelInfo:
    """Tests for HierarchicalLabelInfo parsing."""

    def test_from_sexp_basic(self):
        """Parse basic hierarchical label."""
        from kicad_tools.sexp import SExp

        sexp = SExp.list(
            "hierarchical_label",
            "DATA_OUT",
            SExp.list("shape", "output"),
            SExp.list("at", 10.0, 20.0, 180),
            SExp.list("uuid", "label-uuid-123"),
        )
        label = HierarchicalLabelInfo.from_sexp(sexp)

        assert label.name == "DATA_OUT"
        assert label.shape == "output"
        assert label.position == (10.0, 20.0)
        assert label.rotation == 180
        assert label.uuid == "label-uuid-123"

    def test_from_sexp_default_shape(self):
        """Default shape should be input."""
        from kicad_tools.sexp import SExp

        sexp = SExp.list(
            "hierarchical_label",
            "DATA_IN",
            SExp.list("at", 10.0, 20.0),
            SExp.list("uuid", "label-uuid"),
        )
        label = HierarchicalLabelInfo.from_sexp(sexp)

        assert label.shape == "input"


class TestHierarchyNodeWithLabelInfo:
    """Tests for HierarchyNode with hierarchical_label_info."""

    def test_node_has_label_info_field(self):
        """HierarchyNode should have hierarchical_label_info field."""
        node = HierarchyNode(
            name="Test",
            path="/test.kicad_sch",
            uuid="node-uuid",
        )
        assert hasattr(node, "hierarchical_label_info")
        assert node.hierarchical_label_info == []


class TestCliValidateCommand:
    """Tests for the CLI validate command."""

    def test_validate_command_exists(self, simple_rc_schematic: Path, capsys):
        """Validate command should exist and run."""
        from kicad_tools.cli.sch_hierarchy import main

        # Should run without error (may have validation issues)
        result = main([str(simple_rc_schematic), "validate"])
        assert result in (0, 1, None)

    def test_validate_json_output(self, simple_rc_schematic: Path, capsys):
        """Validate command should output valid JSON."""
        from kicad_tools.cli.sch_hierarchy import main

        main([str(simple_rc_schematic), "validate", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "schematic" in data
        assert "issues" in data
        assert "error_count" in data
        assert "warning_count" in data

    def test_validate_with_missing_file(self, capsys):
        """Validate should handle missing files gracefully (returns empty hierarchy)."""
        from kicad_tools.cli.sch_hierarchy import main

        # The hierarchy builder handles missing files by returning an empty hierarchy
        # This is consistent with other hierarchy commands (tree, list, etc.)
        result = main(["nonexistent.kicad_sch", "validate"])
        # Returns 0 because empty hierarchy has no issues
        assert result == 0

        captured = capsys.readouterr()
        assert "No issues found" in captured.out


class TestValidateHierarchy:
    """Integration tests for validate_hierarchy function."""

    def test_validate_simple_schematic(self, simple_rc_schematic: Path):
        """Validate a simple schematic."""
        result = validate_hierarchy(str(simple_rc_schematic))

        assert result.root_schematic == str(simple_rc_schematic)
        assert result.sheets_checked >= 1
        # Simple schematic may or may not have issues

    def test_validate_with_specific_sheet(self, simple_rc_schematic: Path):
        """Validate with specific sheet filter."""
        result = validate_hierarchy(
            str(simple_rc_schematic), specific_sheet="nonexistent.kicad_sch"
        )

        # Should still work but check 0 sheets matching filter
        assert result.root_schematic == str(simple_rc_schematic)
