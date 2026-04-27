#!/usr/bin/env python3
"""
Rename component reference designators in KiCad schematics.

Supports single-ref mode (--ref/--new-ref), batch mode via JSON/CSV
mapping files (--map), and hierarchical schematic traversal.

Uses a two-pass temporary-rename strategy for batch renames to avoid
transient collisions when source and target references overlap.

Usage:
    # Single rename
    kicad-tools sch set-reference board.kicad_sch --ref LED3 \\
        --new-ref D3

    # Batch mode with JSON mapping
    kicad-tools sch set-reference board.kicad_sch \\
        --map ref-map.json

    # Dry run
    kicad-tools sch set-reference board.kicad_sch --ref R1 \\
        --new-ref R99 --dry-run
"""

import json
import sys
from pathlib import Path

from kicad_tools.cli.modify_schematic import create_backup
from kicad_tools.cli.sch_re_annotate import (
    _apply_reference_rename,
    _extract_symbols_from_text,
)
from kicad_tools.cli.sch_set_footprint import (
    _collect_schematic_files,
    _load_mapping,
)


def _collect_all_references(all_files: list[Path]) -> set[str]:
    """Collect all reference designators across the schematic hierarchy."""
    refs: set[str] = set()
    for sch_file in all_files:
        try:
            text = sch_file.read_text(encoding="utf-8")
        except OSError:
            continue
        symbols = _extract_symbols_from_text(text)
        for sym in symbols:
            refs.add(sym["reference"])
    return refs


def _check_duplicates(
    mapping: dict[str, str],
    existing_refs: set[str],
) -> list[str]:
    """Check for duplicate references that would result from the rename.

    Returns a list of error messages for each collision detected.
    A collision occurs when a new reference already exists and is not
    itself being renamed away.
    """
    errors: list[str] = []
    old_refs = set(mapping.keys())
    new_refs_seen: set[str] = set()

    for old_ref, new_ref in mapping.items():
        # Check if new_ref collides with an existing ref that is not being renamed
        if new_ref in existing_refs and new_ref not in old_refs:
            errors.append(
                f"Cannot rename {old_ref} -> {new_ref}: "
                f"{new_ref} already exists and is not being renamed"
            )
        # Check if two renames target the same new reference
        if new_ref in new_refs_seen:
            errors.append(
                f"Cannot rename {old_ref} -> {new_ref}: "
                f"duplicate target reference in mapping"
            )
        new_refs_seen.add(new_ref)

    return errors


def run_set_reference(
    schematic_path: Path,
    ref: str | None = None,
    new_ref: str | None = None,
    map_path: Path | None = None,
    dry_run: bool = False,
    backup: bool = True,
) -> int:
    """Run the set-reference operation.

    Returns 0 on success, 1 on error.
    """
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    # Build the old_ref -> new_ref mapping
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
    elif ref is not None and new_ref is not None:
        mapping = {ref: new_ref}
    else:
        print("Error: Provide either --ref/--new-ref or --map", file=sys.stderr)
        return 1

    # Collect all schematic files (root + sub-sheets)
    all_files = _collect_schematic_files(schematic_path)

    # Collect all existing references for duplicate detection
    existing_refs = _collect_all_references(all_files)

    # Check that all source references exist
    missing_refs = set(mapping.keys()) - existing_refs
    if missing_refs:
        for r in sorted(missing_refs):
            print(f"Error: Reference not found: {r}", file=sys.stderr)
        return 1

    # Check for identity mappings (ref renamed to itself)
    identity_refs = [old for old, new in mapping.items() if old == new]
    for r in identity_refs:
        del mapping[r]
    if not mapping:
        print("No references to rename (all mappings are identity).")
        return 0

    # Check for duplicate/collision errors
    errors = _check_duplicates(mapping, existing_refs)
    if errors:
        for err in errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    # Dry-run output
    if dry_run:
        print(f"Dry run: {len(mapping)} reference(s) would be renamed:")
        for old, new in sorted(mapping.items()):
            print(f"  {old} -> {new}")
        return 0

    # Apply renames using two-pass temporary-rename strategy
    # Pass 1: old_ref -> temporary placeholder
    # Pass 2: temporary placeholder -> new_ref
    temp_mapping: dict[str, str] = {}
    for i, old_ref in enumerate(mapping):
        temp_mapping[old_ref] = f"__SET_REF_TEMP_{i}__"

    modified_files: dict[Path, str] = {}

    for sch_file in all_files:
        try:
            text = sch_file.read_text(encoding="utf-8")
        except OSError as e:
            print(f"Error reading {sch_file}: {e}", file=sys.stderr)
            return 1

        current_text = text

        # Pass 1: rename to temporaries
        for old_ref, temp_ref in temp_mapping.items():
            current_text = _apply_reference_rename(current_text, old_ref, temp_ref)

        # Pass 2: rename temporaries to final targets
        for old_ref, temp_ref in temp_mapping.items():
            final_ref = mapping[old_ref]
            current_text = _apply_reference_rename(current_text, temp_ref, final_ref)

        if current_text != text:
            modified_files[sch_file] = current_text

    # Write out modified files
    if modified_files:
        for sch_file, new_text in modified_files.items():
            if backup:
                bak = create_backup(sch_file)
                print(f"  Backup: {bak.name}")
            try:
                sch_file.write_text(new_text, encoding="utf-8")
            except OSError as e:
                print(f"Error writing {sch_file}: {e}", file=sys.stderr)
                return 1

    # Summary
    total_renamed = len(mapping)
    print(f"Renamed {total_renamed} reference(s) across {len(modified_files)} file(s).")
    for old, new in sorted(mapping.items()):
        print(f"  {old} -> {new}")

    return 0


def main(argv: list[str] | None = None):
    """CLI entry point for standalone usage."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Rename reference designators in KiCad schematics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to .kicad_sch file")
    parser.add_argument("--ref", help="Current reference designator (e.g., LED3)")
    parser.add_argument("--new-ref", help="New reference designator (e.g., D3)")
    parser.add_argument(
        "--map",
        dest="map_file",
        type=Path,
        help="Path to JSON or CSV mapping file (old_ref -> new_ref)",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying files"
    )
    parser.add_argument(
        "--no-backup", action="store_true", help="Skip creating backup files"
    )

    args = parser.parse_args(argv)

    # Validate argument combinations
    if args.map_file and (args.ref or args.new_ref):
        print("Error: Cannot use --map with --ref/--new-ref", file=sys.stderr)
        return 1
    if args.ref and not args.new_ref:
        print("Error: --new-ref is required when using --ref", file=sys.stderr)
        return 1
    if args.new_ref and not args.ref:
        print("Error: --ref is required when using --new-ref", file=sys.stderr)
        return 1
    if not args.ref and not args.map_file:
        print("Error: Provide either --ref/--new-ref or --map", file=sys.stderr)
        return 1

    return run_set_reference(
        schematic_path=args.schematic,
        ref=args.ref,
        new_ref=args.new_ref,
        map_path=args.map_file,
        dry_run=args.dry_run,
        backup=not args.no_backup,
    )


if __name__ == "__main__":
    sys.exit(main())
