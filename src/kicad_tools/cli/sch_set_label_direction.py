#!/usr/bin/env python3
"""
Change the shape (direction) of global and hierarchical labels in KiCad schematics.

Updates the ``(shape ...)`` attribute of ``global_label`` and ``hierarchical_label``
elements that match a given name.  Traverses the full schematic hierarchy by default,
or restricts to a single sheet with ``--sheet``.

Usage:
    kct sch set-label-direction board.kicad_sch --name SDA --shape bidirectional

    # Dry run (preview changes)
    kct sch set-label-direction board.kicad_sch --name SDA --shape bidirectional --dry-run

    # Restrict to a single sheet
    kct sch set-label-direction board.kicad_sch --name SDA --shape output --sheet DAC
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

VALID_SHAPES = ("input", "output", "bidirectional", "tri_state", "passive")


@dataclass
class LabelChange:
    """A single label shape change."""

    file_path: str
    element_type: str  # "global_label" or "hierarchical_label"
    label_name: str
    old_shape: str
    new_shape: str
    line_number: int


@dataclass
class SetLabelDirectionResult:
    """Result of a set-label-direction operation."""

    success: bool
    changes: list[LabelChange] = field(default_factory=list)
    files_modified: set[str] = field(default_factory=set)
    error: str | None = None


# ---------------------------------------------------------------------------
# Hierarchy traversal (shared helper, duplicated from sch_set_footprint.py to
# keep the module self-contained without circular imports).
# ---------------------------------------------------------------------------


def _find_subsheet_files(text: str, schematic_dir: Path) -> list[Path]:
    """Extract sub-sheet file paths from schematic text."""
    subsheets: list[Path] = []
    pattern = re.compile(r'\(property "Sheetfile" "([^"]+)"')
    for match in pattern.finditer(text):
        subsheet_path = schematic_dir / match.group(1)
        if subsheet_path.exists():
            subsheets.append(subsheet_path)
    return subsheets


def _collect_schematic_files(root_path: Path) -> list[Path]:
    """Recursively collect all schematic files in the hierarchy."""
    visited: set[Path] = set()
    result: list[Path] = []

    def _walk(sch_path: Path) -> None:
        resolved = sch_path.resolve()
        if resolved in visited:
            return
        visited.add(resolved)
        result.append(sch_path)

        try:
            text = sch_path.read_text(encoding="utf-8")
        except OSError:
            return

        for sub in _find_subsheet_files(text, sch_path.parent):
            _walk(sub)

    _walk(root_path)
    return result


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _build_pattern(element_type: str, name: str) -> re.Pattern[str]:
    """Build a regex that matches the shape attribute of *element_type* labels named *name*.

    Groups:
        1 - everything before the shape value (prefix including ``(shape ``)
        2 - the old shape value
        3 - closing paren
    """
    return re.compile(
        r"(\(" + element_type + r'\s+"' + re.escape(name) + r'"\s+\(shape\s+)(\w+)(\))'
    )


def find_label_shape_occurrences(
    file_path: str,
    name: str,
    text: str,
) -> list[LabelChange]:
    """Find all global/hierarchical labels with the given *name* in *text*."""
    changes: list[LabelChange] = []
    for element_type in ("global_label", "hierarchical_label"):
        pattern = _build_pattern(element_type, name)
        for match in pattern.finditer(text):
            line_num = text[: match.start()].count("\n") + 1
            changes.append(
                LabelChange(
                    file_path=file_path,
                    element_type=element_type,
                    label_name=name,
                    old_shape=match.group(2),
                    new_shape="",  # filled in later
                    line_number=line_num,
                )
            )
    return changes


def replace_label_shapes(
    text: str,
    name: str,
    new_shape: str,
) -> tuple[str, int]:
    """Replace shape values for all matching labels in *text*.

    Returns:
        Tuple of (modified_text, number_of_replacements).
    """
    total = 0
    for element_type in ("global_label", "hierarchical_label"):
        pattern = _build_pattern(element_type, name)
        text, count = pattern.subn(r"\g<1>" + new_shape + r"\3", text)
        total += count
    return text, total


def set_label_direction(
    root_schematic: str,
    name: str,
    new_shape: str,
    dry_run: bool = False,
    backup: bool = False,
    sheet_filter: str | None = None,
) -> SetLabelDirectionResult:
    """Change the shape of labels named *name* across the schematic hierarchy.

    Args:
        root_schematic: Path to the root ``.kicad_sch`` file.
        name: Label name to match.
        new_shape: New shape value (one of :data:`VALID_SHAPES`).
        dry_run: If ``True`` report changes without writing files.
        backup: If ``True`` create a backup before modifying each file.
        sheet_filter: Optional sheet name/path substring to restrict processing.

    Returns:
        A :class:`SetLabelDirectionResult`.
    """
    root_path = Path(root_schematic)
    if not root_path.exists():
        return SetLabelDirectionResult(
            success=False,
            error=f"File not found: {root_schematic}",
        )

    all_files = _collect_schematic_files(root_path)

    # Optionally filter to a single sheet (case-insensitive)
    if sheet_filter:
        sf_lower = sheet_filter.lower()
        all_files = [
            f for f in all_files
            if sf_lower in f.name.lower() or sf_lower in str(f).lower()
        ]

    all_changes: list[LabelChange] = []
    files_modified: set[str] = set()

    for sch_file in all_files:
        try:
            text = sch_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error reading {sch_file}: {exc}", file=sys.stderr)
            continue

        file_changes = find_label_shape_occurrences(str(sch_file), name, text)
        if not file_changes:
            continue

        # Fill in new_shape for reporting
        for change in file_changes:
            change.new_shape = new_shape

        all_changes.extend(file_changes)

        if dry_run:
            continue

        modified_text, count = replace_label_shapes(text, name, new_shape)
        if count > 0:
            if backup:
                from kicad_tools.cli.modify_schematic import create_backup

                bak = create_backup(sch_file)
                print(f"  Backup: {bak.name}")
            sch_file.write_text(modified_text, encoding="utf-8")
            files_modified.add(str(sch_file))

    if not all_changes:
        return SetLabelDirectionResult(
            success=True,
            error=f"No labels named '{name}' found in hierarchy",
        )

    return SetLabelDirectionResult(
        success=True,
        changes=all_changes,
        files_modified=files_modified,
    )


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``sch set-label-direction``."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Change label shape (direction) in KiCad schematics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to root .kicad_sch file")
    parser.add_argument("--name", required=True, help="Label name to match")
    parser.add_argument(
        "--shape",
        required=True,
        choices=VALID_SHAPES,
        help="New shape value",
    )
    parser.add_argument("--sheet", help="Restrict to a specific sheet (name or path substring)")
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying files"
    )
    parser.add_argument("--backup", action="store_true", help="Create backup before modifying")

    args = parser.parse_args(argv)

    schematic_path = Path(args.schematic)
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    if not args.schematic.endswith(".kicad_sch"):
        print(f"Error: Not a schematic file: {args.schematic}", file=sys.stderr)
        return 1

    result = set_label_direction(
        root_schematic=args.schematic,
        name=args.name,
        new_shape=args.shape,
        dry_run=args.dry_run,
        backup=args.backup,
        sheet_filter=args.sheet,
    )

    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        return 1

    if not result.changes:
        print(result.error or f"No labels named '{args.name}' found.")
        return 0

    # Print changes grouped by file
    by_file: dict[str, list[LabelChange]] = {}
    for change in result.changes:
        by_file.setdefault(change.file_path, []).append(change)

    for file_path, file_changes in sorted(by_file.items()):
        rel_name = Path(file_path).name
        print(f"  {rel_name}:")
        for change in sorted(file_changes, key=lambda c: c.line_number):
            label_desc = (
                "Global label" if change.element_type == "global_label" else "Hierarchical label"
            )
            print(
                f"    - {label_desc} '{change.label_name}': "
                f"{change.old_shape} -> {change.new_shape} (line {change.line_number})"
            )
        print(f"    ({len(file_changes)} label(s) in this sheet)")

    total = len(result.changes)
    total_files = len(by_file)

    if args.dry_run:
        print(f"\nDry run: {total} label(s) would be changed in {total_files} file(s)")
    else:
        print(f"\nChanged {total} label(s) in {total_files} file(s)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
