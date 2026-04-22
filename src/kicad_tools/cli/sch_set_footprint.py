#!/usr/bin/env python3
"""
Set footprint assignments for symbols in KiCad schematics.

Supports single-ref mode, batch mode via JSON/CSV mapping files,
and hierarchical schematic traversal.

Usage:
    # Single symbol
    kicad-tools sch set-footprint board.kicad_sch --ref U2 \\
        --footprint "Package_TO_SOT_SMD:SOT-23-5"

    # Batch mode with JSON mapping
    kicad-tools sch set-footprint board.kicad_sch \\
        --map footprint-map.json

    # Dry run
    kicad-tools sch set-footprint board.kicad_sch --ref R1 \\
        --footprint "Resistor_SMD:R_0805_2012Metric" --dry-run
"""

import json
import re
import sys
from pathlib import Path

from kicad_tools.cli.modify_schematic import (
    create_backup,
    find_symbol_text_range,
    set_footprint_text,
)


def _find_subsheet_files(text: str, schematic_dir: Path) -> list[Path]:
    """Extract sub-sheet file paths from schematic text.

    Looks for (sheet ... (property "Sheetfile" "filename.kicad_sch" ...)) blocks.
    """
    subsheets = []
    pattern = re.compile(r'\(property "Sheetfile" "([^"]+)"')
    for match in pattern.finditer(text):
        subsheet_path = schematic_dir / match.group(1)
        if subsheet_path.exists():
            subsheets.append(subsheet_path)
    return subsheets


def _collect_schematic_files(root_path: Path) -> list[Path]:
    """Recursively collect all schematic files in the hierarchy.

    Returns list starting with root, followed by sub-sheets (depth-first).
    """
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


def _load_mapping(map_path: Path) -> dict[str, str]:
    """Load a reference-to-footprint mapping from JSON or CSV file.

    JSON format: {"R1": "Resistor_SMD:R_0805", "C1": "Capacitor_SMD:C_0603"}
    CSV format (no header): R1,Resistor_SMD:R_0805
    """
    text = map_path.read_text(encoding="utf-8").strip()

    # Try JSON first
    if text.startswith("{"):
        mapping = json.loads(text)
        if not isinstance(mapping, dict):
            raise ValueError(f"JSON mapping must be an object, got {type(mapping).__name__}")
        return mapping

    # Fall back to CSV (ref,footprint per line)
    mapping: dict[str, str] = {}
    for line_num, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",", 1)
        if len(parts) != 2:
            raise ValueError(f"Line {line_num}: expected 'REF,FOOTPRINT', got: {line}")
        ref, fp = parts[0].strip(), parts[1].strip()
        if not ref:
            raise ValueError(f"Line {line_num}: empty reference")
        mapping[ref] = fp
    return mapping


def run_set_footprint(
    schematic_path: Path,
    ref: str | None = None,
    footprint: str | None = None,
    map_path: Path | None = None,
    dry_run: bool = False,
    backup: bool = True,
) -> int:
    """Run the set-footprint operation.

    Returns 0 on success, 1 on error.
    """
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    # Build the ref -> footprint mapping
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
    elif ref is not None and footprint is not None:
        mapping = {ref: footprint}
    else:
        print("Error: Provide either --ref/--footprint or --map", file=sys.stderr)
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
            fp = remaining[r]
            result = find_symbol_text_range(current_text, r)
            if result is None:
                continue

            if dry_run:
                _, _, info = result
                old_fp = info.get("footprint", "")
                print(f"  {r}: '{old_fp}' -> '{fp}' (in {sch_file.name})")
                total_changed += 1
                del remaining[r]
            else:
                current_text, success, msg = set_footprint_text(current_text, r, fp)
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
        print(f"Dry run: {total_changed} footprint(s) would be changed")
    else:
        print(f"Changed {total_changed} footprint(s)")

    if remaining:
        print(f"Warning: {len(remaining)} reference(s) not found: {', '.join(sorted(remaining))}")
        total_errors += len(remaining)

    return 1 if total_errors > 0 and total_changed == 0 else 0


def main(argv: list[str] | None = None):
    """CLI entry point for standalone usage."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Set footprint assignments in KiCad schematics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to .kicad_sch file")
    parser.add_argument("--ref", help="Symbol reference (e.g., U2, R1)")
    parser.add_argument(
        "--footprint", help="Footprint to assign (e.g., Package_TO_SOT_SMD:SOT-23-5)"
    )
    parser.add_argument(
        "--map",
        dest="map_file",
        type=Path,
        help="Path to JSON or CSV mapping file (ref -> footprint)",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying files"
    )
    parser.add_argument(
        "--no-backup", action="store_true", help="Skip creating backup files"
    )

    args = parser.parse_args(argv)

    # Validate argument combinations
    if args.map_file and (args.ref or args.footprint):
        print("Error: Cannot use --map with --ref/--footprint", file=sys.stderr)
        return 1
    if args.ref and not args.footprint:
        print("Error: --footprint is required when using --ref", file=sys.stderr)
        return 1
    if args.footprint and not args.ref:
        print("Error: --ref is required when using --footprint", file=sys.stderr)
        return 1
    if not args.ref and not args.map_file:
        print("Error: Provide either --ref/--footprint or --map", file=sys.stderr)
        return 1

    return run_set_footprint(
        schematic_path=args.schematic,
        ref=args.ref,
        footprint=args.footprint,
        map_path=args.map_file,
        dry_run=args.dry_run,
        backup=not args.no_backup,
    )


if __name__ == "__main__":
    sys.exit(main())
