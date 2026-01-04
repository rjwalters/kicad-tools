#!/usr/bin/env python3
"""
Synchronize sheet pins and hierarchical labels in KiCad hierarchical schematics.

Detects mismatches between sheet pins (in parent) and hierarchical labels (in child),
and can automatically fix them by adding missing labels or removing orphan pins.

Usage:
    kct sch sync-hierarchy project.kicad_sch              # Analyze mismatches
    kct sch sync-hierarchy project.kicad_sch --add-labels # Add missing labels
    kct sch sync-hierarchy project.kicad_sch --remove-orphan-pins  # Remove orphans
    kct sch sync-hierarchy project.kicad_sch --interactive # Interactive mode
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from kicad_tools.schema.hierarchy import (
    HierarchicalLabelInfo,
    build_hierarchy,
)
from kicad_tools.schema.hierarchy_validation import (
    ValidationIssueType,
    validate_hierarchy,
)
from kicad_tools.sexp import SExp, parse_sexp
from kicad_tools.sexp.builders import hier_label_node


@dataclass
class SyncAction:
    """Represents a synchronization action to take."""

    action_type: str  # "add_label" or "remove_pin"
    name: str  # Signal name
    direction: str  # Direction/shape
    file_path: str  # File to modify
    sheet_name: str  # Sheet this affects
    parent_file: str  # Parent schematic file
    # For add_label: position info
    position: tuple[float, float] | None = None
    rotation: float = 0


@dataclass
class SyncResult:
    """Result of a sync operation."""

    success: bool
    action: SyncAction
    message: str


def _get_schematic_size(sexp: SExp) -> tuple[float, float]:
    """Get the schematic page size from paper node."""
    paper = sexp.find("paper")
    if paper:
        paper_size = paper.get_string(0)
        # Common paper sizes
        sizes = {
            "A4": (297, 210),
            "A3": (420, 297),
            "A2": (594, 420),
            "A1": (841, 594),
            "A0": (1189, 841),
            "A": (279.4, 215.9),  # US Letter
            "B": (431.8, 279.4),  # US Legal
            "C": (558.8, 431.8),
            "D": (863.6, 558.8),
            "E": (1117.6, 863.6),
        }
        if paper_size in sizes:
            return sizes[paper_size]
    # Default to A4
    return (297, 210)


def _calculate_label_position(
    direction: str,
    existing_labels: list[HierarchicalLabelInfo],
    page_size: tuple[float, float],
) -> tuple[tuple[float, float], float]:
    """Calculate position and rotation for a new hierarchical label.

    Places labels at schematic edges based on direction:
    - input: left edge (rotation 0)
    - output: right edge (rotation 180)
    - bidirectional/passive: left edge (rotation 0)

    Returns:
        Tuple of ((x, y), rotation)
    """
    width, height = page_size
    margin = 10.0  # Distance from edge
    spacing = 5.08  # Standard KiCad spacing (200 mil)

    # Determine side and rotation based on direction
    if direction in ("output",):
        # Output labels go on right edge, pointing left
        base_x = width - margin
        rotation = 180
    else:
        # Input, bidirectional, passive go on left edge
        base_x = margin
        rotation = 0

    # Find Y position - avoid existing labels on same edge
    same_edge_labels = [lbl for lbl in existing_labels if lbl.rotation == rotation]
    used_y = {lbl.position[1] for lbl in same_edge_labels}

    # Start from top quarter of page, work down
    base_y = height * 0.25
    y = base_y
    while y in used_y or any(abs(y - uy) < spacing for uy in used_y):
        y += spacing
        if y > height - margin:
            # Wrap to next column
            y = base_y

    return (base_x, y), rotation


def _add_hierarchical_label(
    file_path: str,
    name: str,
    direction: str,
    position: tuple[float, float],
    rotation: float,
    dry_run: bool = False,
) -> bool:
    """Add a hierarchical label to a schematic file.

    Args:
        file_path: Path to the .kicad_sch file
        name: Label name/text
        direction: Label shape (input, output, bidirectional, passive)
        position: (x, y) position
        rotation: Rotation in degrees
        dry_run: If True, don't actually write the file

    Returns:
        True if successful
    """
    path = Path(file_path)
    content = path.read_text()

    # Generate new label S-expression
    new_uuid = str(uuid.uuid4())
    label = hier_label_node(name, position[0], position[1], direction, rotation, new_uuid)
    label_sexp = label.to_string(indent=1)

    # Find a good insertion point - after existing hierarchical_label nodes
    # or before first symbol if no hier labels exist
    lines = content.split("\n")
    insert_idx = None

    # Look for last hierarchical_label
    for i in range(len(lines) - 1, -1, -1):
        if "(hierarchical_label" in lines[i]:
            # Find closing paren of this label block
            depth = 0
            for j in range(i, len(lines)):
                depth += lines[j].count("(") - lines[j].count(")")
                if depth == 0:
                    insert_idx = j + 1
                    break
            break

    # If no hierarchical_label found, insert before first symbol
    if insert_idx is None:
        for i, line in enumerate(lines):
            if "(symbol" in line and "(lib_id" in lines[i + 1] if i + 1 < len(lines) else "":
                insert_idx = i
                break

    # If still not found, insert before closing paren
    if insert_idx is None:
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == ")":
                insert_idx = i
                break

    if insert_idx is None:
        return False

    if dry_run:
        return True

    # Insert the new label
    lines.insert(insert_idx, "")
    lines.insert(insert_idx + 1, label_sexp)

    path.write_text("\n".join(lines))
    return True


def _remove_sheet_pin(
    file_path: str,
    sheet_name: str,
    pin_name: str,
    dry_run: bool = False,
) -> bool:
    """Remove a sheet pin from a schematic file.

    Args:
        file_path: Path to the parent .kicad_sch file
        sheet_name: Name of the sheet containing the pin
        pin_name: Name of the pin to remove
        dry_run: If True, don't actually write the file

    Returns:
        True if successful
    """
    path = Path(file_path)
    content = path.read_text()

    # Parse and find the sheet block
    sexp = parse_sexp(content)

    # Find the sheet by name
    target_sheet = None
    for sheet in sexp.find_all("sheet"):
        for prop in sheet.find_all("property"):
            if prop.get_string(0) == "Sheetname" and prop.get_string(1) == sheet_name:
                target_sheet = sheet
                break
        if target_sheet:
            break

    if not target_sheet:
        return False

    # Find the pin by name
    pin_to_remove = None
    for pin in target_sheet.find_all("pin"):
        if pin.get_string(0) == pin_name:
            pin_to_remove = pin
            break

    if not pin_to_remove:
        return False

    if dry_run:
        return True

    # Remove the pin from the sheet using text-based approach to preserve formatting
    # Find and remove the pin in the content
    # The pin format is: (pin "NAME" (shape) (at X Y R) (effects ...) (uuid ...))
    # We need to find the complete pin block

    # Build a regex pattern for the pin
    # This is a simplified approach - find lines containing the pin and remove them
    lines = content.split("\n")
    new_lines = []
    skip_until_close = False
    skip_depth = 0
    in_target_sheet = False
    sheet_depth = 0

    for line in lines:
        # Track if we're in the target sheet
        if "(sheet" in line:
            in_target_sheet = False
            sheet_depth = 1
        elif in_target_sheet or sheet_depth > 0:
            sheet_depth += line.count("(") - line.count(")")
            if sheet_depth == 0:
                in_target_sheet = False

        # Check if this is the start of our target sheet
        if f'Sheetname" "{sheet_name}"' in line or f'Sheetname"{sheet_name}"' in line:
            in_target_sheet = True

        # If we're in target sheet and hit the pin to remove
        if in_target_sheet and f'(pin "{pin_name}"' in line:
            skip_until_close = True
            skip_depth = line.count("(") - line.count(")")
            continue

        if skip_until_close:
            skip_depth += line.count("(") - line.count(")")
            if skip_depth <= 0:
                skip_until_close = False
            continue

        new_lines.append(line)

    path.write_text("\n".join(new_lines))
    return True


def analyze_hierarchy(root_schematic: str, specific_sheet: str | None = None) -> list[SyncAction]:
    """Analyze hierarchy and return list of potential sync actions.

    Args:
        root_schematic: Path to root schematic
        specific_sheet: Optional specific sheet to analyze

    Returns:
        List of SyncAction objects representing fixes
    """
    result = validate_hierarchy(root_schematic, specific_sheet=specific_sheet)
    actions = []

    for issue in result.issues:
        if issue.issue_type == ValidationIssueType.MISSING_LABEL:
            # Pin exists but no label - can add label
            actions.append(
                SyncAction(
                    action_type="add_label",
                    name=issue.pin_name,
                    direction=issue.pin.direction if issue.pin else "passive",
                    file_path=issue.sheet_file,
                    sheet_name=issue.sheet_name,
                    parent_file=issue.parent_sheet_file,
                )
            )
        elif issue.issue_type == ValidationIssueType.MISSING_PIN:
            # Label exists but no pin (orphan) - can remove pin
            # Actually this is an orphan label - the action is to suggest
            # either adding a pin OR removing the label
            # For sync-hierarchy, we focus on pins, so this suggests
            # that a pin should be added to parent OR label removed from child
            pass  # We'll handle this differently

    return actions


def find_orphan_labels(root_schematic: str) -> list[SyncAction]:
    """Find orphan labels (labels without corresponding pins).

    Returns actions to remove the orphan labels from child sheets.
    """
    result = validate_hierarchy(root_schematic)
    actions = []

    for issue in result.issues:
        if issue.issue_type == ValidationIssueType.MISSING_PIN:
            # Label exists but no pin - orphan label
            # Action would be to remove the label from child
            actions.append(
                SyncAction(
                    action_type="remove_label",
                    name=issue.label_name,
                    direction=issue.label.shape if issue.label else "passive",
                    file_path=issue.sheet_file,
                    sheet_name=issue.sheet_name,
                    parent_file=issue.parent_sheet_file,
                )
            )

    return actions


def find_orphan_pins(root_schematic: str) -> list[SyncAction]:
    """Find orphan pins (pins without corresponding labels).

    Returns actions to remove the orphan pins from parent sheets.
    """
    result = validate_hierarchy(root_schematic)
    actions = []

    for issue in result.issues:
        if issue.issue_type == ValidationIssueType.MISSING_LABEL:
            # Pin exists but no label
            actions.append(
                SyncAction(
                    action_type="remove_pin",
                    name=issue.pin_name,
                    direction=issue.pin.direction if issue.pin else "passive",
                    file_path=issue.parent_sheet_file,
                    sheet_name=issue.sheet_name,
                    parent_file=issue.parent_sheet_file,
                )
            )

    return actions


def execute_add_labels(
    root_schematic: str,
    dry_run: bool = False,
    interactive: bool = False,
) -> list[SyncResult]:
    """Add missing hierarchical labels to child sheets.

    For each sheet pin without a matching label, adds the label to the child.
    """
    results = []
    actions = analyze_hierarchy(root_schematic)

    # Build hierarchy to get label positions
    hierarchy = build_hierarchy(root_schematic)

    for action in actions:
        if action.action_type != "add_label":
            continue

        # Find the child node to get existing labels and page size
        child_node = None
        for node in hierarchy.all_nodes():
            if Path(node.path).name == action.file_path or node.path == action.file_path:
                child_node = node
                break

        if not child_node:
            # Try resolving relative path
            child_path = Path(root_schematic).parent / action.file_path
            for node in hierarchy.all_nodes():
                if Path(node.path) == child_path:
                    child_node = node
                    break

        if not child_node:
            results.append(
                SyncResult(
                    success=False,
                    action=action,
                    message=f"Could not find child schematic: {action.file_path}",
                )
            )
            continue

        # Get page size
        child_path = Path(child_node.path)
        child_content = child_path.read_text()
        child_sexp = parse_sexp(child_content)
        page_size = _get_schematic_size(child_sexp)

        # Calculate position for new label
        position, rotation = _calculate_label_position(
            action.direction, child_node.hierarchical_label_info, page_size
        )

        if interactive:
            print(f"\nAdd hierarchical label '{action.name}' ({action.direction})?")
            print(f"  To: {action.file_path}")
            print(f"  Position: ({position[0]:.2f}, {position[1]:.2f})")
            response = input("  [A]dd / [S]kip / [Q]uit: ").strip().lower()
            if response == "q":
                break
            if response == "s":
                results.append(SyncResult(success=False, action=action, message="Skipped by user"))
                continue

        if dry_run:
            results.append(
                SyncResult(
                    success=True,
                    action=action,
                    message=f"Would add label '{action.name}' to {action.file_path}",
                )
            )
        else:
            success = _add_hierarchical_label(
                child_node.path,
                action.name,
                action.direction,
                position,
                rotation,
                dry_run=False,
            )
            results.append(
                SyncResult(
                    success=success,
                    action=action,
                    message=f"Added label '{action.name}'" if success else "Failed to add label",
                )
            )

    return results


def execute_remove_orphan_pins(
    root_schematic: str,
    dry_run: bool = False,
    interactive: bool = False,
) -> list[SyncResult]:
    """Remove orphan pins from parent sheets.

    For each sheet pin without a matching label, removes the pin from parent.
    """
    results = []
    actions = find_orphan_pins(root_schematic)

    for action in actions:
        if interactive:
            print(f"\nRemove orphan pin '{action.name}' ({action.direction})?")
            print(f"  From: {action.file_path} (sheet: {action.sheet_name})")
            response = input("  [R]emove / [S]kip / [Q]uit: ").strip().lower()
            if response == "q":
                break
            if response == "s":
                results.append(SyncResult(success=False, action=action, message="Skipped by user"))
                continue

        if dry_run:
            results.append(
                SyncResult(
                    success=True,
                    action=action,
                    message=f"Would remove pin '{action.name}' from {action.sheet_name}",
                )
            )
        else:
            success = _remove_sheet_pin(
                action.file_path,
                action.sheet_name,
                action.name,
                dry_run=False,
            )
            results.append(
                SyncResult(
                    success=success,
                    action=action,
                    message=f"Removed pin '{action.name}'" if success else "Failed to remove pin",
                )
            )

    return results


def format_analysis_report(root_schematic: str) -> str:
    """Generate a formatted analysis report."""
    result = validate_hierarchy(root_schematic)
    lines = []

    lines.append("Hierarchy Sync Analysis")
    lines.append("=" * 60)
    lines.append("")

    # Group issues by sheet
    sheets: dict[str, list] = {}
    for issue in result.issues:
        key = f"{issue.sheet_name} ({issue.sheet_file})"
        if key not in sheets:
            sheets[key] = {"missing_labels": [], "orphan_labels": []}

        if issue.issue_type == ValidationIssueType.MISSING_LABEL:
            sheets[key]["missing_labels"].append(issue)
        elif issue.issue_type == ValidationIssueType.MISSING_PIN:
            sheets[key]["orphan_labels"].append(issue)

    if not sheets:
        lines.append("No sync issues found. Hierarchy is synchronized.")
        return "\n".join(lines)

    for sheet_key, issues in sheets.items():
        lines.append(f"{sheet_key}:")

        if issues["missing_labels"]:
            lines.append("  Sheet pins without matching labels:")
            for issue in issues["missing_labels"]:
                direction = issue.pin.direction if issue.pin else "?"
                lines.append(f"    x {issue.pin_name} ({direction}) - no label in sub-schematic")

        if issues["orphan_labels"]:
            lines.append("  Labels without matching sheet pins:")
            for issue in issues["orphan_labels"]:
                shape = issue.label.shape if issue.label else "?"
                lines.append(f"    x {issue.label_name} ({shape}) - orphaned")

        if issues["missing_labels"]:
            lines.append("")
            lines.append("  Sync options:")
            lines.append(
                f"    [A] Add hierarchical labels for {len(issues['missing_labels'])} pin(s)"
            )
            lines.append(f"    [R] Remove {len(issues['missing_labels'])} sheet pin(s)")

        lines.append("")

    # Summary
    total_missing = sum(len(s["missing_labels"]) for s in sheets.values())
    total_orphan = sum(len(s["orphan_labels"]) for s in sheets.values())
    lines.append(f"Summary: {total_missing} pins without labels, {total_orphan} orphaned labels")

    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Synchronize sheet pins and hierarchical labels",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to root .kicad_sch file")
    parser.add_argument(
        "--add-labels",
        action="store_true",
        help="Add missing hierarchical labels to child sheets",
    )
    parser.add_argument(
        "--remove-orphan-pins",
        action="store_true",
        help="Remove sheet pins that have no matching labels",
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Interactive mode - prompt for each action",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview changes without modifying files",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--sheet",
        help="Focus on a specific sheet (by name or filename)",
    )

    args = parser.parse_args(argv)

    # Validate schematic exists
    if not Path(args.schematic).exists():
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        return 1

    # Execute requested actions
    if args.add_labels:
        results = execute_add_labels(
            args.schematic,
            dry_run=args.dry_run,
            interactive=args.interactive,
        )

        if args.format == "json":
            output = {
                "action": "add_labels",
                "dry_run": args.dry_run,
                "results": [
                    {
                        "success": r.success,
                        "name": r.action.name,
                        "file": r.action.file_path,
                        "message": r.message,
                    }
                    for r in results
                ],
            }
            print(json.dumps(output, indent=2))
        else:
            if args.dry_run:
                print("Dry run - no files modified\n")

            for r in results:
                status = "+" if r.success else "x"
                print(f"  {status} {r.message}")

            success_count = sum(1 for r in results if r.success)
            print(
                f"\n{success_count}/{len(results)} labels {'would be ' if args.dry_run else ''}added"
            )

    elif args.remove_orphan_pins:
        results = execute_remove_orphan_pins(
            args.schematic,
            dry_run=args.dry_run,
            interactive=args.interactive,
        )

        if args.format == "json":
            output = {
                "action": "remove_orphan_pins",
                "dry_run": args.dry_run,
                "results": [
                    {
                        "success": r.success,
                        "name": r.action.name,
                        "sheet": r.action.sheet_name,
                        "message": r.message,
                    }
                    for r in results
                ],
            }
            print(json.dumps(output, indent=2))
        else:
            if args.dry_run:
                print("Dry run - no files modified\n")

            for r in results:
                status = "-" if r.success else "x"
                print(f"  {status} {r.message}")

            success_count = sum(1 for r in results if r.success)
            print(
                f"\n{success_count}/{len(results)} pins {'would be ' if args.dry_run else ''}removed"
            )

    else:
        # Analysis mode (default)
        if args.format == "json":
            result = validate_hierarchy(args.schematic)
            output = {
                "schematic": args.schematic,
                "issues": [],
            }
            for issue in result.issues:
                output["issues"].append(
                    {
                        "type": issue.issue_type.value,
                        "sheet_name": issue.sheet_name,
                        "sheet_file": issue.sheet_file,
                        "pin_name": issue.pin_name,
                        "label_name": issue.label_name,
                        "message": issue.message,
                    }
                )
            print(json.dumps(output, indent=2))
        else:
            print(format_analysis_report(args.schematic))

    return 0


if __name__ == "__main__":
    sys.exit(main())
