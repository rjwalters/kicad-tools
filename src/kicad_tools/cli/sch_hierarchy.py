#!/usr/bin/env python3
"""
Navigate and analyze hierarchical KiCad schematics.

Usage:
    python3 sch-hierarchy.py <schematic.kicad_sch> [command] [options]

Commands:
    tree        Show the hierarchy tree (default)
    list        List all sheets with details
    labels      Show hierarchical label connections
    path        Show path to a specific sheet
    validate    Validate hierarchy connections (pins vs labels)

Options:
    --format {tree,json,text}  Output format (default: tree for tree/list, text for validate)
    --sheet <name>             Focus on a specific sheet
    --depth <n>                Maximum depth to show
    --fix                      Auto-fix simple issues (for validate command)

Examples:
    # Show hierarchy tree
    python3 sch-hierarchy.py project.kicad_sch

    # List all sheets
    python3 sch-hierarchy.py project.kicad_sch list

    # Show hierarchical label connections
    python3 sch-hierarchy.py project.kicad_sch labels

    # Validate hierarchy (check pins match labels)
    python3 sch-hierarchy.py project.kicad_sch validate

    # Auto-fix simple issues
    python3 sch-hierarchy.py project.kicad_sch validate --fix

    # Focus on a specific sheet
    python3 sch-hierarchy.py project.kicad_sch tree --sheet Power
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.schema.hierarchy import HierarchyNode, build_hierarchy
from kicad_tools.schema.hierarchy_validation import (
    apply_fix,
    format_validation_report,
    validate_hierarchy,
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Navigate hierarchical KiCad schematics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to root .kicad_sch file")
    parser.add_argument(
        "command",
        nargs="?",
        default="tree",
        choices=["tree", "list", "labels", "path", "stats", "validate"],
        help="Command to run (default: tree)",
    )
    parser.add_argument(
        "--format", choices=["tree", "json", "text"], default="tree", help="Output format"
    )
    parser.add_argument("--sheet", help="Focus on a specific sheet")
    parser.add_argument("--depth", type=int, help="Maximum depth to show")
    parser.add_argument(
        "--fix", action="store_true", help="Auto-fix simple issues (for validate command)"
    )

    args = parser.parse_args(argv)

    # Handle validate command separately (doesn't need full hierarchy in memory)
    if args.command == "validate":
        return cmd_validate(args)

    # Build hierarchy
    try:
        root = build_hierarchy(args.schematic)
    except FileNotFoundError:
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error building hierarchy: {e}", file=sys.stderr)
        sys.exit(1)

    # Focus on specific sheet if requested
    if args.sheet:
        node = root.find_by_name(args.sheet)
        if not node:
            print(f"Error: Sheet '{args.sheet}' not found", file=sys.stderr)
            all_names = [n.name for n in root.all_nodes() if n.name != "Root"]
            if all_names:
                print(f"Available sheets: {', '.join(all_names)}", file=sys.stderr)
            sys.exit(1)
        root = node

    # Execute command
    if args.command == "tree":
        cmd_tree(root, args)
    elif args.command == "list":
        cmd_list(root, args)
    elif args.command == "labels":
        cmd_labels(root, args)
    elif args.command == "path":
        cmd_path(root, args)
    elif args.command == "stats":
        cmd_stats(root, args)


def cmd_tree(root: HierarchyNode, args):
    """Show hierarchy tree."""
    if args.format == "json":
        print(json.dumps(node_to_dict(root, args.depth), indent=2))
    else:
        print(format_tree(root, args.depth))


def cmd_list(root: HierarchyNode, args):
    """List all sheets."""
    all_nodes = root.all_nodes()

    if args.format == "json":
        data = []
        for node in all_nodes:
            data.append(
                {
                    "name": node.name,
                    "path": node.get_path_string(),
                    "file": node.path,
                    "depth": node.depth,
                    "children": len(node.children),
                    "hierarchical_labels": node.hierarchical_labels,
                }
            )
        print(json.dumps(data, indent=2))
    else:
        print("Schematic Sheets")
        print("=" * 70)
        print(f"{'Path':<30}  {'File':<25}  {'Labels':<10}  Children")
        print("-" * 70)

        for node in all_nodes:
            path = node.get_path_string()
            if len(path) > 28:
                path = "..." + path[-25:]
            filename = Path(node.path).name
            if len(filename) > 23:
                filename = filename[:20] + "..."
            labels = len(node.hierarchical_labels)
            children = len(node.children)
            print(f"{path:<30}  {filename:<25}  {labels:<10}  {children}")

        print(f"\nTotal: {len(all_nodes)} sheets")


def cmd_labels(root: HierarchyNode, args):
    """Show hierarchical label connections with match status."""
    # Build a mapping from sheet name to child node for quick lookup
    sheet_to_child: dict[str, HierarchyNode] = {}
    for node in root.all_nodes():
        for sheet in node.sheets:
            # Find the child node that corresponds to this sheet
            for child in node.children:
                if child.name == sheet.name:
                    sheet_to_child[f"{node.get_path_string()}/{sheet.name}"] = child
                    break

    # Collect signal data grouped by signal name, then by sheet
    # Structure: {signal_name: {sheet_path: {"pin": pin_info, "label": bool, "child_node": node}}}
    signals: dict[str, dict[str, dict]] = {}

    for node in root.all_nodes():
        # For each sheet in this node, check pins vs labels in child
        for sheet in node.sheets:
            sheet_path = f"{node.get_path_string()}/{sheet.name}"
            child_key = sheet_path
            child_node = sheet_to_child.get(child_key)
            child_labels = set(child_node.hierarchical_labels) if child_node else set()

            for pin in sheet.pins:
                if pin.name not in signals:
                    signals[pin.name] = {}

                has_label = pin.name in child_labels

                signals[pin.name][sheet_path] = {
                    "sheet_name": sheet.name,
                    "parent_name": node.name,
                    "pin": {
                        "name": pin.name,
                        "direction": pin.direction,
                    },
                    "has_label": has_label,
                    "matched": has_label,
                }

            # Also check for labels in child that don't have corresponding pins
            if child_node:
                pin_names = {p.name for p in sheet.pins}
                for label in child_node.hierarchical_labels:
                    if label not in pin_names:
                        # Label exists without corresponding pin
                        if label not in signals:
                            signals[label] = {}
                        signals[label][sheet_path] = {
                            "sheet_name": sheet.name,
                            "parent_name": node.name,
                            "pin": None,
                            "has_label": True,
                            "matched": False,  # No pin for this label
                        }

    # Calculate summary statistics
    total_signals = len(signals)
    mismatched_signals = 0
    for signal_name, sheet_data in signals.items():
        for sheet_path, info in sheet_data.items():
            if not info["matched"]:
                mismatched_signals += 1
                break  # Count each signal only once

    if args.format == "json":
        # Build JSON output with match information
        json_output = {
            "signals": {},
            "summary": {
                "total_signals": total_signals,
                "mismatched_signals": mismatched_signals,
                "matched_signals": total_signals - mismatched_signals,
            },
        }

        for signal_name in sorted(signals.keys()):
            sheet_data = signals[signal_name]
            signal_matched = all(info["matched"] for info in sheet_data.values())
            json_output["signals"][signal_name] = {
                "matched": signal_matched,
                "sheets": {},
            }
            for sheet_path, info in sheet_data.items():
                json_output["signals"][signal_name]["sheets"][sheet_path] = {
                    "sheet_name": info["sheet_name"],
                    "parent": info["parent_name"],
                    "has_pin": info["pin"] is not None,
                    "pin_direction": info["pin"]["direction"] if info["pin"] else None,
                    "has_label": info["has_label"],
                    "matched": info["matched"],
                }

        print(json.dumps(json_output, indent=2))
    else:
        print("Hierarchical Label Connections")
        print("=" * 70)

        for signal_name in sorted(signals.keys()):
            sheet_data = signals[signal_name]
            signal_matched = all(info["matched"] for info in sheet_data.values())
            status_icon = "✓" if signal_matched else "✗"
            print(f"\n⚡ {signal_name} {status_icon}")

            for sheet_path in sorted(sheet_data.keys()):
                info = sheet_data[sheet_path]
                print(f"   Sheet: {info['sheet_name']}")

                # Show pin status
                if info["pin"]:
                    direction = info["pin"]["direction"]
                    dir_icon = (
                        "→" if direction == "output" else "←" if direction == "input" else "↔"
                    )
                    print(f"     Pin:   {info['pin']['name']} ({direction}) {dir_icon} ✓")
                else:
                    print(f"     Pin:   {signal_name} ✗ MISSING")

                # Show label status
                if info["has_label"]:
                    print(f"     Label: {signal_name} ✓")
                else:
                    print(f"     Label: {signal_name} ✗ MISSING")

        # Summary line
        print(
            f"\nSummary: {total_signals} signals, {mismatched_signals} mismatch{'es' if mismatched_signals != 1 else ''}"
        )


def cmd_path(root: HierarchyNode, args):
    """Show path to a specific sheet."""
    if not args.sheet:
        print("Error: --sheet required for path command", file=sys.stderr)
        sys.exit(1)

    node = root.find_by_name(args.sheet)
    if not node:
        print(f"Error: Sheet '{args.sheet}' not found", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        path_parts = []
        current = node
        while current:
            path_parts.insert(
                0,
                {
                    "name": current.name,
                    "file": current.path,
                },
            )
            current = current.parent
        print(json.dumps(path_parts, indent=2))
    else:
        print(f"Path to '{args.sheet}':")
        print(node.get_path_string())
        print(f"\nFile: {node.path}")

        if node.hierarchical_labels:
            print("\nHierarchical labels in this sheet:")
            for label in node.hierarchical_labels:
                print(f"  ⚡ {label}")

        if node.sheets:
            print("\nChild sheets:")
            for sheet in node.sheets:
                print(f"  📄 {sheet.name} ({sheet.filename})")
                for pin in sheet.pins:
                    icon = (
                        "→"
                        if pin.direction == "output"
                        else "←"
                        if pin.direction == "input"
                        else "↔"
                    )
                    print(f"     {icon} {pin.name}")


def cmd_stats(root: HierarchyNode, args):
    """Show hierarchy statistics."""
    all_nodes = root.all_nodes()

    total_sheets = len(all_nodes)
    max_depth = max(n.depth for n in all_nodes)
    total_labels = sum(len(n.hierarchical_labels) for n in all_nodes)
    total_pins = sum(sum(len(s.pins) for s in n.sheets) for n in all_nodes)
    leaf_sheets = sum(1 for n in all_nodes if n.is_leaf)

    if args.format == "json":
        print(
            json.dumps(
                {
                    "total_sheets": total_sheets,
                    "max_depth": max_depth,
                    "leaf_sheets": leaf_sheets,
                    "total_hierarchical_labels": total_labels,
                    "total_sheet_pins": total_pins,
                },
                indent=2,
            )
        )
    else:
        print("Hierarchy Statistics")
        print("=" * 40)
        print(f"Total sheets:           {total_sheets}")
        print(f"Maximum depth:          {max_depth}")
        print(f"Leaf sheets:            {leaf_sheets}")
        print(f"Hierarchical labels:    {total_labels}")
        print(f"Sheet pins:             {total_pins}")


def cmd_validate(args) -> int:
    """Validate hierarchy connections."""
    try:
        # Validate hierarchy
        specific_sheet = None
        if args.sheet:
            # Convert sheet name to filename if needed
            specific_sheet = args.sheet
            if not specific_sheet.endswith(".kicad_sch"):
                specific_sheet = f"{specific_sheet}.kicad_sch"

        result = validate_hierarchy(args.schematic, specific_sheet=specific_sheet)

    except FileNotFoundError:
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error validating hierarchy: {e}", file=sys.stderr)
        return 1

    # Apply auto-fixes if requested
    fixes_applied = 0
    if args.fix:
        for issue in result.issues:
            for suggestion in issue.suggestions:
                if suggestion.auto_fixable:
                    if apply_fix(suggestion):
                        fixes_applied += 1
                        print(f"Fixed: {suggestion.description}")

        if fixes_applied > 0:
            print(f"\nApplied {fixes_applied} automatic fix(es).")
            # Re-validate after fixes
            result = validate_hierarchy(args.schematic, specific_sheet=specific_sheet)
            print("")

    # Output results
    if args.format == "json":
        output = {
            "schematic": result.root_schematic,
            "sheets_checked": result.sheets_checked,
            "pins_checked": result.pins_checked,
            "labels_checked": result.labels_checked,
            "error_count": result.error_count,
            "warning_count": result.warning_count,
            "issues": [
                {
                    "type": issue.issue_type.value,
                    "severity": issue.severity,
                    "sheet_name": issue.sheet_name,
                    "sheet_file": issue.sheet_file,
                    "parent_sheet": issue.parent_sheet_name,
                    "pin_name": issue.pin_name,
                    "label_name": issue.label_name,
                    "message": issue.message,
                    "pin": {
                        "name": issue.pin.name,
                        "direction": issue.pin.direction,
                        "position": list(issue.pin.position),
                        "uuid": issue.pin.uuid,
                    }
                    if issue.pin
                    else None,
                    "label": {
                        "name": issue.label.name,
                        "shape": issue.label.shape,
                        "position": list(issue.label.position),
                        "uuid": issue.label.uuid,
                    }
                    if issue.label
                    else None,
                    "suggestions": [
                        {
                            "type": s.fix_type.value,
                            "description": s.description,
                            "auto_fixable": s.auto_fixable,
                            "file_path": s.file_path,
                        }
                        for s in issue.suggestions
                    ],
                    "possible_causes": issue.possible_causes,
                }
                for issue in result.issues
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        print(format_validation_report(result))

    # Return exit code based on errors
    return 1 if result.has_errors else 0


def format_tree(
    node: HierarchyNode, max_depth: int = None, prefix: str = "", is_last: bool = True
) -> str:
    """Format hierarchy as ASCII tree."""
    lines = []

    # Node name and info
    if node.is_root:
        icon = "📁"
        name = Path(node.path).name
    else:
        icon = "📄"
        name = node.name

    info_parts = []
    if node.hierarchical_labels:
        info_parts.append(f"{len(node.hierarchical_labels)} labels")
    if node.children:
        info_parts.append(f"{len(node.children)} children")
    info = f" ({', '.join(info_parts)})" if info_parts else ""

    connector = "└─ " if is_last else "├─ "
    if node.is_root:
        lines.append(f"{icon} {name}{info}")
    else:
        lines.append(f"{prefix}{connector}{icon} {name}{info}")

    # Check depth limit
    if max_depth is not None and node.depth >= max_depth:
        if node.children:
            child_prefix = prefix + ("   " if is_last else "│  ")
            lines.append(f"{child_prefix}└─ ... ({len(node.children)} more)")
        return "\n".join(lines)

    # Children
    child_prefix = prefix + ("   " if is_last else "│  ")
    for i, child in enumerate(node.children):
        child_is_last = i == len(node.children) - 1
        child_tree = format_tree(child, max_depth, child_prefix, child_is_last)
        lines.append(child_tree)

    return "\n".join(lines)


def node_to_dict(node: HierarchyNode, max_depth: int = None) -> dict:
    """Convert node to dictionary for JSON output."""
    result = {
        "name": node.name,
        "path": node.get_path_string(),
        "file": node.path,
        "hierarchical_labels": node.hierarchical_labels,
        "sheets": [
            {
                "name": s.name,
                "file": s.filename,
                "pins": [{"name": p.name, "direction": p.direction} for p in s.pins],
            }
            for s in node.sheets
        ],
    }

    if max_depth is None or node.depth < max_depth:
        result["children"] = [node_to_dict(child, max_depth) for child in node.children]
    elif node.children:
        result["children_count"] = len(node.children)

    return result


if __name__ == "__main__":
    main()
