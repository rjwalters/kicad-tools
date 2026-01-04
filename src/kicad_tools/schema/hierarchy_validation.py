"""
Hierarchy validation for KiCad schematics.

Validates connections between sheet pins and hierarchical labels,
detecting mismatches and providing actionable fix suggestions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path

from .hierarchy import (
    HierarchicalLabelInfo,
    SheetPin,
    build_hierarchy,
)


class ValidationIssueType(Enum):
    """Types of hierarchy validation issues."""

    MISSING_LABEL = "missing_label"  # Sheet pin exists but no label in child
    MISSING_PIN = "missing_pin"  # Label exists but no pin in parent (orphan)
    DIRECTION_MISMATCH = "direction_mismatch"  # Label direction doesn't match pin
    NAME_MISMATCH = "name_mismatch"  # Similar names but not exact match


class FixType(Enum):
    """Types of automatic fixes available."""

    ADD_LABEL = "add_label"  # Add hierarchical label to child sheet
    REMOVE_PIN = "remove_pin"  # Remove orphan pin from parent
    FIX_DIRECTION = "fix_direction"  # Fix direction mismatch
    RENAME_LABEL = "rename_label"  # Fix name typo in label
    RENAME_PIN = "rename_pin"  # Fix name typo in pin


@dataclass
class FixSuggestion:
    """A suggested fix for a validation issue."""

    fix_type: FixType
    description: str
    file_path: str
    auto_fixable: bool = False
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        prefix = "[Auto-fixable] " if self.auto_fixable else ""
        return f"{prefix}{self.description}"


@dataclass
class ValidationIssue:
    """A single hierarchy validation issue."""

    issue_type: ValidationIssueType
    sheet_name: str
    sheet_file: str
    parent_sheet_name: str
    parent_sheet_file: str
    pin_name: str | None
    label_name: str | None
    pin: SheetPin | None
    label: HierarchicalLabelInfo | None
    message: str
    suggestions: list[FixSuggestion] = field(default_factory=list)
    possible_causes: list[str] = field(default_factory=list)

    @property
    def severity(self) -> str:
        """Get severity level."""
        if self.issue_type == ValidationIssueType.MISSING_LABEL:
            return "error"
        elif self.issue_type in (
            ValidationIssueType.MISSING_PIN,
            ValidationIssueType.DIRECTION_MISMATCH,
            ValidationIssueType.NAME_MISMATCH,
        ):
            return "warning"
        return "info"


@dataclass
class ValidationResult:
    """Result of hierarchy validation."""

    root_schematic: str
    issues: list[ValidationIssue] = field(default_factory=list)
    sheets_checked: int = 0
    pins_checked: int = 0
    labels_checked: int = 0

    @property
    def has_errors(self) -> bool:
        """Check if there are any error-level issues."""
        return any(i.severity == "error" for i in self.issues)

    @property
    def error_count(self) -> int:
        """Count error-level issues."""
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        """Count warning-level issues."""
        return sum(1 for i in self.issues if i.severity == "warning")

    def issues_for_sheet(self, sheet_file: str) -> list[ValidationIssue]:
        """Get issues affecting a specific sheet."""
        return [i for i in self.issues if i.sheet_file == sheet_file]


def _directions_compatible(pin_direction: str, label_shape: str) -> bool:
    """
    Check if a sheet pin direction is compatible with a hierarchical label shape.

    In KiCad:
    - Sheet pin direction indicates data flow FROM THE PARENT'S PERSPECTIVE
    - Hierarchical label shape indicates data flow FROM THE CHILD'S PERSPECTIVE

    They should be the same (both indicate which direction data flows).
    """
    # Normalize names
    pin_dir = pin_direction.lower()
    label_dir = label_shape.lower()

    # Direct match
    if pin_dir == label_dir:
        return True

    # Bidirectional/passive are compatible with anything
    if pin_dir in ("bidirectional", "passive") or label_dir in ("bidirectional", "passive"):
        return True

    return False


def _find_similar_name(target: str, candidates: list[str], threshold: float = 0.8) -> str | None:
    """Find a similar name among candidates using fuzzy matching."""
    best_match = None
    best_ratio = threshold

    for candidate in candidates:
        # Case-insensitive comparison
        ratio = SequenceMatcher(None, target.lower(), candidate.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = candidate

    return best_match


def validate_hierarchy(
    root_schematic: str, *, specific_sheet: str | None = None
) -> ValidationResult:
    """
    Validate schematic hierarchy connections.

    Checks that:
    1. Every sheet pin has a matching hierarchical label in the child sheet
    2. Every hierarchical label in a child has a matching pin in the parent
    3. Directions/shapes match between pins and labels
    4. Names match exactly (with suggestions for similar names)

    Args:
        root_schematic: Path to the root .kicad_sch file
        specific_sheet: Optional - only validate this sheet

    Returns:
        ValidationResult with all issues found
    """
    result = ValidationResult(root_schematic=root_schematic)

    # Build hierarchy
    root = build_hierarchy(root_schematic)

    # Validate each node
    for node in root.all_nodes():
        if specific_sheet and Path(node.path).name != specific_sheet:
            continue

        result.sheets_checked += 1

        # For each sheet instance in this node, check pins vs child labels
        for sheet in node.sheets:
            # Find the child node
            child = None
            for c in node.children:
                if c.name == sheet.name:
                    child = c
                    break

            if not child:
                continue

            result.pins_checked += len(sheet.pins)
            result.labels_checked += len(child.hierarchical_label_info)

            # Check each pin has a matching label
            child_label_names = {lbl.name for lbl in child.hierarchical_label_info}
            child_labels_by_name = {lbl.name: lbl for lbl in child.hierarchical_label_info}

            for pin in sheet.pins:
                if pin.name in child_labels_by_name:
                    # Found matching label - check direction
                    label = child_labels_by_name[pin.name]
                    if not _directions_compatible(pin.direction, label.shape):
                        issue = ValidationIssue(
                            issue_type=ValidationIssueType.DIRECTION_MISMATCH,
                            sheet_name=sheet.name,
                            sheet_file=sheet.filename,
                            parent_sheet_name=node.name,
                            parent_sheet_file=node.path,
                            pin_name=pin.name,
                            label_name=label.name,
                            pin=pin,
                            label=label,
                            message=(
                                f'Direction mismatch: pin "{pin.name}" is {pin.direction}, '
                                f"but label is {label.shape}"
                            ),
                            suggestions=[
                                FixSuggestion(
                                    fix_type=FixType.FIX_DIRECTION,
                                    description=f"Change label shape to '{pin.direction}'",
                                    file_path=child.path,
                                    auto_fixable=True,
                                    details={
                                        "new_direction": pin.direction,
                                        "label_uuid": label.uuid,
                                    },
                                )
                            ],
                            possible_causes=[
                                "Pin direction was changed after label was created",
                                "Copy-paste error when creating the sheet",
                            ],
                        )
                        result.issues.append(issue)
                else:
                    # Missing label
                    similar = _find_similar_name(pin.name, list(child_label_names))
                    suggestions = []
                    causes = []

                    if similar:
                        suggestions.append(
                            FixSuggestion(
                                fix_type=FixType.RENAME_LABEL,
                                description=f'Rename label "{similar}" to "{pin.name}"',
                                file_path=child.path,
                                auto_fixable=False,
                                details={"old_name": similar, "new_name": pin.name},
                            )
                        )
                        causes.append(f'Possible typo: found similar label "{similar}"')

                    suggestions.append(
                        FixSuggestion(
                            fix_type=FixType.ADD_LABEL,
                            description=f'Add hierarchical label "{pin.name}" ({pin.direction})',
                            file_path=child.path,
                            auto_fixable=False,
                            details={"name": pin.name, "direction": pin.direction},
                        )
                    )

                    causes.extend(
                        [
                            "Label was deleted from child sheet",
                            "Sheet was replaced with different version",
                            "Pin was added to parent without corresponding label",
                        ]
                    )

                    issue = ValidationIssue(
                        issue_type=ValidationIssueType.MISSING_LABEL,
                        sheet_name=sheet.name,
                        sheet_file=sheet.filename,
                        parent_sheet_name=node.name,
                        parent_sheet_file=node.path,
                        pin_name=pin.name,
                        label_name=None,
                        pin=pin,
                        label=None,
                        message=f'Sheet pin "{pin.name}" has no matching hierarchical label in child sheet',
                        suggestions=suggestions,
                        possible_causes=causes,
                    )
                    result.issues.append(issue)

            # Check for orphan labels (labels without pins)
            pin_names = {p.name for p in sheet.pins}
            for label in child.hierarchical_label_info:
                if label.name not in pin_names:
                    similar = _find_similar_name(label.name, list(pin_names))
                    suggestions = []
                    causes = []

                    if similar:
                        suggestions.append(
                            FixSuggestion(
                                fix_type=FixType.RENAME_PIN,
                                description=f'Rename pin "{similar}" to "{label.name}"',
                                file_path=node.path,
                                auto_fixable=False,
                                details={"old_name": similar, "new_name": label.name},
                            )
                        )
                        causes.append(f'Possible typo: found similar pin "{similar}"')

                    causes.extend(
                        [
                            "Pin was removed from parent sheet",
                            "Label was added without corresponding pin",
                            "Sheet structure was reorganized",
                        ]
                    )

                    issue = ValidationIssue(
                        issue_type=ValidationIssueType.MISSING_PIN,
                        sheet_name=sheet.name,
                        sheet_file=sheet.filename,
                        parent_sheet_name=node.name,
                        parent_sheet_file=node.path,
                        pin_name=None,
                        label_name=label.name,
                        pin=None,
                        label=label,
                        message=(
                            f'Hierarchical label "{label.name}" has no matching pin '
                            f"in parent sheet (orphan label)"
                        ),
                        suggestions=suggestions,
                        possible_causes=causes,
                    )
                    result.issues.append(issue)

    return result


def apply_fix(fix: FixSuggestion) -> bool:
    """
    Apply an automatic fix to a schematic file.

    Args:
        fix: The fix to apply

    Returns:
        True if fix was applied successfully
    """
    if not fix.auto_fixable:
        return False

    if fix.fix_type == FixType.FIX_DIRECTION:
        return _fix_label_direction(
            fix.file_path, fix.details["label_uuid"], fix.details["new_direction"]
        )

    return False


def _fix_label_direction(file_path: str, label_uuid: str, new_direction: str) -> bool:
    """
    Fix a hierarchical label's direction/shape.

    Args:
        file_path: Path to the schematic file
        label_uuid: UUID of the label to fix
        new_direction: New direction to set

    Returns:
        True if fix was applied
    """
    try:
        path = Path(file_path)
        content = path.read_text()

        # Find the label by UUID and update its shape
        # This is a simple text-based replacement
        # Format: (hierarchical_label "NAME" (shape output) ... (uuid "UUID"))

        lines = content.split("\n")
        in_label = False
        label_start = -1
        found_uuid = False
        modified = False

        for i, line in enumerate(lines):
            if "hierarchical_label" in line:
                in_label = True
                label_start = i
                found_uuid = False
            elif in_label:
                if f'uuid "{label_uuid}"' in line or f"uuid {label_uuid}" in line:
                    found_uuid = True
                elif line.strip().startswith(")") and "(" not in line:
                    # End of label block
                    if found_uuid:
                        # Go back and fix the shape
                        for j in range(label_start, i + 1):
                            if "(shape " in lines[j]:
                                # Replace the shape value
                                import re

                                lines[j] = re.sub(
                                    r"\(shape \w+\)", f"(shape {new_direction})", lines[j]
                                )
                                modified = True
                                break
                    in_label = False
                    label_start = -1

        if modified:
            path.write_text("\n".join(lines))
            return True

    except Exception:
        pass

    return False


def format_validation_report(result: ValidationResult, *, show_tree: bool = False) -> str:
    """
    Format validation results as a readable report.

    Args:
        result: Validation result to format
        show_tree: Whether to include hierarchy tree

    Returns:
        Formatted report string
    """
    lines = []

    # Header
    lines.append(f"Schematic Hierarchy Validation: {Path(result.root_schematic).name}")
    lines.append("=" * 70)
    lines.append("")

    # Summary
    lines.append(f"Sheets checked: {result.sheets_checked}")
    lines.append(f"Pins checked: {result.pins_checked}")
    lines.append(f"Labels checked: {result.labels_checked}")
    lines.append("")

    if not result.issues:
        lines.append("No issues found. Hierarchy is valid.")
        return "\n".join(lines)

    # Issues by sheet
    lines.append("Validation Results:")
    lines.append("-" * 70)

    sheets_with_issues: dict[str, list[ValidationIssue]] = {}
    for issue in result.issues:
        key = f"{issue.sheet_name} ({issue.sheet_file})"
        if key not in sheets_with_issues:
            sheets_with_issues[key] = []
        sheets_with_issues[key].append(issue)

    for sheet_key, issues in sheets_with_issues.items():
        lines.append("")
        error_count = sum(1 for i in issues if i.severity == "error")
        status = "FAIL" if error_count > 0 else "WARN"
        lines.append(f"[{status}] Sheet: {sheet_key}")

        for issue in issues:
            icon = "x" if issue.severity == "error" else "!"
            lines.append(f"  {icon} {issue.message}")

            if issue.pin:
                lines.append(
                    f'    Pin: "{issue.pin.name}" ({issue.pin.direction}) '
                    f"@ ({issue.pin.position[0]:.2f}, {issue.pin.position[1]:.2f})"
                )

            if issue.label:
                lines.append(
                    f'    Label: "{issue.label.name}" ({issue.label.shape}) '
                    f"@ ({issue.label.position[0]:.2f}, {issue.label.position[1]:.2f})"
                )

            if issue.possible_causes:
                lines.append("    Possible causes:")
                for cause in issue.possible_causes[:2]:  # Limit to 2 causes
                    lines.append(f"      - {cause}")

            if issue.suggestions:
                lines.append("    Suggested fixes:")
                for suggestion in issue.suggestions:
                    auto = " [auto-fixable]" if suggestion.auto_fixable else ""
                    lines.append(f"      - {suggestion.description}{auto}")

    # Summary
    lines.append("")
    lines.append("-" * 70)
    lines.append(
        f"Summary: {result.error_count} error(s), {result.warning_count} warning(s) "
        f"in {len(sheets_with_issues)} sheet(s)"
    )

    return "\n".join(lines)
