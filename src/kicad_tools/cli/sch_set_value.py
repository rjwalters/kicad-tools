#!/usr/bin/env python3
"""
Set value assignments for symbols in KiCad schematics.

Supports single-ref mode, batch mode via JSON/CSV mapping files,
and hierarchical schematic traversal.

Usage:
    # Single symbol
    kicad-tools sch set-value board.kicad_sch --ref U4 \\
        --value "AP2204K-3.3TRG1"

    # Batch mode with JSON mapping
    kicad-tools sch set-value board.kicad_sch \\
        --map value-map.json

    # Dry run
    kicad-tools sch set-value board.kicad_sch --ref R1 \\
        --value "4.7k" --dry-run
"""

import json
import sys
from pathlib import Path

from kicad_tools.cli.modify_schematic import (
    create_backup,
    find_symbol_text_range,
    set_value_text,
)
from kicad_tools.cli.sch_set_footprint import (
    _collect_schematic_files,
    _load_mapping,
)


def run_set_value(
    schematic_path: Path,
    ref: str | None = None,
    value: str | None = None,
    map_path: Path | None = None,
    dry_run: bool = False,
    backup: bool = True,
) -> int:
    """Run the set-value operation.

    Returns 0 on success, 1 on error.
    """
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    # Build the ref -> value mapping
    if map_path is not None:
        if not map_path.exists():
            print(f"Error: Mapping file not found: {map_path}", file=sys.stderr)
            return 1
        try:
            mapping = _load_mapping(map_path)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Error reading mapping file: {e}", file=sys.stderr)
            return 1
        if not mapping:
            print("Error: Mapping file is empty", file=sys.stderr)
            return 1
    elif ref is not None and value is not None:
        mapping = {ref: value}
    else:
        print("Error: Provide either --ref/--value or --map", file=sys.stderr)
        return 1

    # Collect all schematic files (root + sub-sheets)
    all_files = _collect_schematic_files(schematic_path)

    total_changed = 0
    total_errors = 0
    remaining = dict(mapping)
    modified_files: dict[Path, str] = {}

    for sch_file in all_files:
        try:
            text = sch_file.read_text(encoding="utf-8")
        except OSError as e:
            print(f"Error reading {sch_file}: {e}", file=sys.stderr)
            total_errors += 1
            continue

        file_modified = False
        current_text = text

        # Try each remaining reference against this file
        for r in list(remaining.keys()):
            v = remaining[r]
            result = find_symbol_text_range(current_text, r)
            if result is None:
                continue

            if dry_run:
                _, _, info = result
                old_val = info.get("value", "")
                print(f"  {r}: '{old_val}' -> '{v}' (in {sch_file.name})")
                total_changed += 1
                del remaining[r]
            else:
                current_text, success, msg = set_value_text(current_text, r, v)
                if success:
                    print(f"  {msg} (in {sch_file.name})")
                    file_modified = True
                    total_changed += 1
                    del remaining[r]
                else:
                    print(f"  Warning: {msg}", file=sys.stderr)
                    total_errors += 1

        if file_modified:
            modified_files[sch_file] = current_text

    # Write out modified files
    if not dry_run and modified_files:
        for sch_file, new_text in modified_files.items():
            if backup:
                bak = create_backup(sch_file)
                print(f"  Backup: {bak.name}")
            try:
                sch_file.write_text(new_text, encoding="utf-8")
            except OSError as e:
                print(f"Error writing {sch_file}: {e}", file=sys.stderr)
                total_errors += 1

    # Summary
    print()
    if dry_run:
        print(f"Dry run: {total_changed} value(s) would be changed")
    else:
        print(f"Changed {total_changed} value(s)")

    if remaining:
        print(f"Warning: {len(remaining)} reference(s) not found: {', '.join(sorted(remaining))}")
        total_errors += len(remaining)

    return 1 if total_errors > 0 and total_changed == 0 else 0


def main(argv: list[str] | None = None):
    """CLI entry point for standalone usage."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Set value assignments in KiCad schematics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to .kicad_sch file")
    parser.add_argument("--ref", help="Symbol reference (e.g., U4, R1)")
    parser.add_argument("--value", help="Value to assign (e.g., AP2204K-3.3TRG1)")
    parser.add_argument(
        "--map",
        dest="map_file",
        type=Path,
        help="Path to JSON or CSV mapping file (ref -> value)",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying files"
    )
    parser.add_argument("--no-backup", action="store_true", help="Skip creating backup files")

    args = parser.parse_args(argv)

    # Validate argument combinations
    if args.map_file and (args.ref or args.value):
        print("Error: Cannot use --map with --ref/--value", file=sys.stderr)
        return 1
    if args.ref and not args.value:
        print("Error: --value is required when using --ref", file=sys.stderr)
        return 1
    if args.value and not args.ref:
        print("Error: --ref is required when using --value", file=sys.stderr)
        return 1
    if not args.ref and not args.map_file:
        print("Error: Provide either --ref/--value or --map", file=sys.stderr)
        return 1

    return run_set_value(
        schematic_path=args.schematic,
        ref=args.ref,
        value=args.value,
        map_path=args.map_file,
        dry_run=args.dry_run,
        backup=not args.no_backup,
    )


if __name__ == "__main__":
    sys.exit(main())
