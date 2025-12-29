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

Options:
    --format {tree,json}   Output format (default: tree)
    --sheet <name>         Focus on a specific sheet
    --depth <n>            Maximum depth to show

Examples:
    # Show hierarchy tree
    python3 sch-hierarchy.py project.kicad_sch

    # List all sheets
    python3 sch-hierarchy.py project.kicad_sch list

    # Show hierarchical label connections
    python3 sch-hierarchy.py project.kicad_sch labels

    # Focus on a specific sheet
    python3 sch-hierarchy.py project.kicad_sch tree --sheet Power
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

from kicad_tools.schema.hierarchy import HierarchyNode, build_hierarchy


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
        choices=["tree", "list", "labels", "path", "stats"],
        help="Command to run (default: tree)",
    )
    parser.add_argument("--format", choices=["tree", "json"], default="tree", help="Output format")
    parser.add_argument("--sheet", help="Focus on a specific sheet")
    parser.add_argument("--depth", type=int, help="Maximum depth to show")

    args = parser.parse_args(argv)

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
    """Show hierarchical label connections."""
    # Collect all hierarchical labels and sheet pins
    connections: Dict[str, List[dict]] = {}

    for node in root.all_nodes():
        # Labels in this sheet
        for label in node.hierarchical_labels:
            if label not in connections:
                connections[label] = []
            connections[label].append(
                {
                    "type": "label",
                    "sheet": node.name,
                    "path": node.get_path_string(),
                }
            )

        # Pins on sheets in this schematic
        for sheet in node.sheets:
            for pin in sheet.pins:
                if pin.name not in connections:
                    connections[pin.name] = []
                connections[pin.name].append(
                    {
                        "type": "pin",
                        "direction": pin.direction,
                        "sheet": sheet.name,
                        "parent": node.name,
                        "path": node.get_path_string(),
                    }
                )

    if args.format == "json":
        print(json.dumps(connections, indent=2))
    else:
        print("Hierarchical Label Connections")
        print("=" * 70)

        for name in sorted(connections.keys()):
            entries = connections[name]
            print(f"\n⚡ {name}")

            for entry in entries:
                if entry["type"] == "label":
                    print(f"   📄 Label in {entry['sheet']} ({entry['path']})")
                else:
                    direction = entry["direction"]
                    icon = "→" if direction == "output" else "←" if direction == "input" else "↔"
                    print(
                        f"   {icon} Pin ({direction}) on sheet {entry['sheet']} in {entry['parent']}"
                    )

        print(f"\nTotal: {len(connections)} unique signals")


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
