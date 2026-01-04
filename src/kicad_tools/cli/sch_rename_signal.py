#!/usr/bin/env python3
"""
Rename a signal across the entire schematic hierarchy.

Updates both sheet pins in parent schematics and hierarchical labels in child schematics.
Optionally includes net labels matching the signal name.

Usage:
    kct sch rename-signal project.kicad_sch --from "MCLK_DAC" --to "CLK_DAC"

    # Dry run (preview changes)
    kct sch rename-signal project.kicad_sch --from "MCLK_DAC" --to "CLK_DAC" --dry-run

    # Include net labels
    kct sch rename-signal project.kicad_sch --from "MCLK_DAC" --to "CLK_DAC" --include-nets

    # Non-interactive mode (skip confirmation)
    kct sch rename-signal project.kicad_sch --from "MCLK_DAC" --to "CLK_DAC" --yes
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SignalChange:
    """Represents a single signal rename change."""

    file_path: str
    element_type: str  # "sheet_pin", "hierarchical_label", "net_label", "global_label"
    old_name: str
    new_name: str
    line_number: int
    context: str  # Additional context like sheet name


@dataclass
class RenameResult:
    """Result of a rename operation."""

    success: bool
    changes: list[SignalChange]
    files_modified: set[str]
    error: str | None = None


def find_signal_occurrences(
    root_schematic: str,
    signal_name: str,
    include_nets: bool = False,
    include_globals: bool = False,
) -> list[SignalChange]:
    """
    Find all occurrences of a signal name across the schematic hierarchy.

    Args:
        root_schematic: Path to the root schematic file
        signal_name: The signal name to search for
        include_nets: Include local net labels
        include_globals: Include global labels

    Returns:
        List of SignalChange objects for each occurrence
    """
    from kicad_tools.schema import build_hierarchy

    changes = []
    root_path = Path(root_schematic)

    # Build hierarchy to find all files and their relationships
    hierarchy = build_hierarchy(str(root_path))

    # Process all nodes in hierarchy
    for node in hierarchy.all_nodes():
        if not node.path or not Path(node.path).exists():
            continue

        file_path = node.path
        text = Path(file_path).read_text(encoding="utf-8")

        # Find sheet pins in this schematic
        # Pattern: (pin "SIGNAL_NAME" direction ...)
        sheet_pin_pattern = re.compile(
            r'\(pin\s+"(' + re.escape(signal_name) + r')"\s+(\w+)',
            re.MULTILINE,
        )
        for match in sheet_pin_pattern.finditer(text):
            # Find the line number
            line_num = text[: match.start()].count("\n") + 1
            # Find which sheet this pin belongs to
            sheet_context = _find_parent_sheet(text, match.start())
            changes.append(
                SignalChange(
                    file_path=file_path,
                    element_type="sheet_pin",
                    old_name=signal_name,
                    new_name="",  # To be filled in
                    line_number=line_num,
                    context=f"Sheet: {sheet_context}" if sheet_context else "",
                )
            )

        # Find hierarchical labels
        # Pattern: (hierarchical_label "SIGNAL_NAME" ...)
        hlabel_pattern = re.compile(
            r'\(hierarchical_label\s+"(' + re.escape(signal_name) + r')"',
            re.MULTILINE,
        )
        for match in hlabel_pattern.finditer(text):
            line_num = text[: match.start()].count("\n") + 1
            changes.append(
                SignalChange(
                    file_path=file_path,
                    element_type="hierarchical_label",
                    old_name=signal_name,
                    new_name="",
                    line_number=line_num,
                    context=f"File: {Path(file_path).name}",
                )
            )

        # Find net labels if requested
        if include_nets:
            # Pattern: (label "SIGNAL_NAME" ...)
            label_pattern = re.compile(
                r'\(label\s+"(' + re.escape(signal_name) + r')"',
                re.MULTILINE,
            )
            for match in label_pattern.finditer(text):
                line_num = text[: match.start()].count("\n") + 1
                changes.append(
                    SignalChange(
                        file_path=file_path,
                        element_type="net_label",
                        old_name=signal_name,
                        new_name="",
                        line_number=line_num,
                        context=f"File: {Path(file_path).name}",
                    )
                )

        # Find global labels if requested
        if include_globals:
            # Pattern: (global_label "SIGNAL_NAME" ...)
            glabel_pattern = re.compile(
                r'\(global_label\s+"(' + re.escape(signal_name) + r')"',
                re.MULTILINE,
            )
            for match in glabel_pattern.finditer(text):
                line_num = text[: match.start()].count("\n") + 1
                changes.append(
                    SignalChange(
                        file_path=file_path,
                        element_type="global_label",
                        old_name=signal_name,
                        new_name="",
                        line_number=line_num,
                        context=f"File: {Path(file_path).name}",
                    )
                )

    return changes


def _find_parent_sheet(text: str, position: int) -> str:
    """Find the sheet name that contains the pin at the given position."""
    # Look backwards from position to find the containing (sheet ...) block
    # and extract the Sheetname property
    before = text[:position]

    # Find the last (sheet that isn't closed
    sheet_starts = list(re.finditer(r"\(sheet\b", before))
    if not sheet_starts:
        return ""

    # Get the most recent sheet start
    last_sheet = sheet_starts[-1]
    sheet_text = text[last_sheet.start() : position]

    # Extract Sheetname from properties
    name_match = re.search(r'\(property\s+"Sheetname"\s+"([^"]+)"', sheet_text)
    if name_match:
        return name_match.group(1)

    return ""


def rename_signal_in_file(
    file_path: str,
    old_name: str,
    new_name: str,
    include_nets: bool = False,
    include_globals: bool = False,
) -> tuple[str, int]:
    """
    Rename all occurrences of a signal in a single schematic file.

    Returns:
        Tuple of (modified_text, change_count)
    """
    text = Path(file_path).read_text(encoding="utf-8")
    change_count = 0

    # Replace sheet pins: (pin "OLD_NAME" direction ...) -> (pin "NEW_NAME" direction ...)
    text, count = re.subn(
        r'(\(pin\s+)"' + re.escape(old_name) + r'"(\s+\w+)',
        r'\1"' + new_name + r'"\2',
        text,
    )
    change_count += count

    # Replace hierarchical labels
    text, count = re.subn(
        r'(\(hierarchical_label\s+)"' + re.escape(old_name) + r'"',
        r'\1"' + new_name + r'"',
        text,
    )
    change_count += count

    # Replace net labels if requested
    if include_nets:
        text, count = re.subn(
            r'(\(label\s+)"' + re.escape(old_name) + r'"',
            r'\1"' + new_name + r'"',
            text,
        )
        change_count += count

    # Replace global labels if requested
    if include_globals:
        text, count = re.subn(
            r'(\(global_label\s+)"' + re.escape(old_name) + r'"',
            r'\1"' + new_name + r'"',
            text,
        )
        change_count += count

    return text, change_count


def rename_signal(
    root_schematic: str,
    old_name: str,
    new_name: str,
    dry_run: bool = False,
    include_nets: bool = False,
    include_globals: bool = False,
) -> RenameResult:
    """
    Rename a signal across the entire schematic hierarchy.

    Args:
        root_schematic: Path to the root schematic file
        old_name: The signal name to rename
        new_name: The new signal name
        dry_run: If True, don't actually modify files
        include_nets: Also rename matching net labels
        include_globals: Also rename matching global labels

    Returns:
        RenameResult with details of changes made
    """
    root_path = Path(root_schematic)
    if not root_path.exists():
        return RenameResult(
            success=False,
            changes=[],
            files_modified=set(),
            error=f"File not found: {root_schematic}",
        )

    # Find all occurrences
    changes = find_signal_occurrences(
        str(root_path),
        old_name,
        include_nets=include_nets,
        include_globals=include_globals,
    )

    if not changes:
        return RenameResult(
            success=True,
            changes=[],
            files_modified=set(),
            error=f"Signal '{old_name}' not found in hierarchy",
        )

    # Fill in the new_name for all changes
    for change in changes:
        change.new_name = new_name

    if dry_run:
        return RenameResult(
            success=True,
            changes=changes,
            files_modified={c.file_path for c in changes},
        )

    # Apply changes to each file
    files_to_modify = {c.file_path for c in changes}
    files_modified = set()

    for file_path in files_to_modify:
        modified_text, count = rename_signal_in_file(
            file_path,
            old_name,
            new_name,
            include_nets=include_nets,
            include_globals=include_globals,
        )
        if count > 0:
            Path(file_path).write_text(modified_text, encoding="utf-8")
            files_modified.add(file_path)

    return RenameResult(
        success=True,
        changes=changes,
        files_modified=files_modified,
    )


def format_changes_text(changes: list[SignalChange], old_name: str, new_name: str) -> str:
    """Format changes for text output."""
    if not changes:
        return f"No occurrences of '{old_name}' found."

    lines = [f'Renaming "{old_name}" -> "{new_name}" across hierarchy...\n']
    lines.append("Changes:")

    # Group changes by file
    by_file: dict[str, list[SignalChange]] = {}
    for change in changes:
        if change.file_path not in by_file:
            by_file[change.file_path] = []
        by_file[change.file_path].append(change)

    for file_path, file_changes in sorted(by_file.items()):
        rel_path = Path(file_path).name
        lines.append(f"  {rel_path}:")
        for change in sorted(file_changes, key=lambda c: c.line_number):
            element_desc = {
                "sheet_pin": "Sheet pin",
                "hierarchical_label": "Hierarchical label",
                "net_label": "Net label",
                "global_label": "Global label",
            }.get(change.element_type, change.element_type)
            lines.append(
                f"    - {element_desc}: {old_name} -> {new_name} (line {change.line_number})"
            )

    total_files = len(by_file)
    total_changes = len(changes)
    lines.append(f"\n{total_changes} changes in {total_files} files.")

    return "\n".join(lines)


def format_changes_json(
    changes: list[SignalChange], old_name: str, new_name: str, files_modified: set[str]
) -> dict:
    """Format changes for JSON output."""
    by_file: dict[str, list[dict]] = {}
    for change in changes:
        if change.file_path not in by_file:
            by_file[change.file_path] = []
        by_file[change.file_path].append(
            {
                "element_type": change.element_type,
                "old_name": change.old_name,
                "new_name": change.new_name,
                "line_number": change.line_number,
                "context": change.context,
            }
        )

    return {
        "old_name": old_name,
        "new_name": new_name,
        "files": by_file,
        "summary": {
            "total_changes": len(changes),
            "total_files": len(by_file),
            "files_modified": list(files_modified),
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Main entry point for rename-signal command."""
    parser = argparse.ArgumentParser(
        description="Rename a signal across the schematic hierarchy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to root .kicad_sch file")
    parser.add_argument(
        "--from",
        dest="old_name",
        required=True,
        help="Current signal name to rename",
    )
    parser.add_argument(
        "--to",
        dest="new_name",
        required=True,
        help="New signal name",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview changes without modifying files",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    parser.add_argument(
        "--include-nets",
        action="store_true",
        help="Also rename matching net labels",
    )
    parser.add_argument(
        "--include-globals",
        action="store_true",
        help="Also rename matching global labels",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )

    args = parser.parse_args(argv)

    # Validate schematic exists
    schematic_path = Path(args.schematic)
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    if args.schematic.endswith(".kicad_sch") is False:
        print(f"Error: Not a schematic file: {args.schematic}", file=sys.stderr)
        return 1

    # Find all occurrences first (for preview)
    changes = find_signal_occurrences(
        args.schematic,
        args.old_name,
        include_nets=args.include_nets,
        include_globals=args.include_globals,
    )

    if not changes:
        if args.format == "json":
            import json

            print(
                json.dumps(
                    {
                        "old_name": args.old_name,
                        "new_name": args.new_name,
                        "files": {},
                        "summary": {"total_changes": 0, "total_files": 0, "files_modified": []},
                        "error": f"Signal '{args.old_name}' not found in hierarchy",
                    }
                )
            )
        else:
            print(f"Signal '{args.old_name}' not found in hierarchy.")
        return 0

    # Set new_name for all changes
    for change in changes:
        change.new_name = args.new_name

    # Preview changes
    if args.format == "text":
        print(format_changes_text(changes, args.old_name, args.new_name))
    else:
        import json

        preview = format_changes_json(changes, args.old_name, args.new_name, set())
        preview["dry_run"] = args.dry_run
        print(json.dumps(preview, indent=2))

    # If dry run, we're done
    if args.dry_run:
        if args.format == "text":
            print("\n(No changes made - dry run)")
        return 0

    # Confirm unless --yes
    if not args.yes and args.format == "text":
        try:
            response = input("\nApply changes? [y/N] ")
            if response.lower() not in ("y", "yes"):
                print("Cancelled.")
                return 0
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 0

    # Apply changes
    result = rename_signal(
        args.schematic,
        args.old_name,
        args.new_name,
        dry_run=False,
        include_nets=args.include_nets,
        include_globals=args.include_globals,
    )

    if result.success and result.files_modified:
        if args.format == "text":
            print(f"\nApplied changes to {len(result.files_modified)} files.")
        else:
            import json

            output = format_changes_json(
                result.changes, args.old_name, args.new_name, result.files_modified
            )
            output["applied"] = True
            print(json.dumps(output, indent=2))
        return 0
    elif result.error:
        if args.format == "text":
            print(f"Error: {result.error}", file=sys.stderr)
        else:
            import json

            print(json.dumps({"error": result.error}))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
