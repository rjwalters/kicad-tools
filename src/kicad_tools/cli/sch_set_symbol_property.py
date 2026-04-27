#!/usr/bin/env python3
"""
Set symbol-level boolean flags in KiCad schematics.

Modifies on_board, in_bom, dnp, and exclude_from_sim flags on placed
symbol instances. Supports hierarchical schematic traversal.

Usage:
    # Set on_board to yes for a power symbol
    kicad-tools sch set-symbol-property board.kicad_sch --ref "#PWR052" \
        --property on_board --value yes

    # Dry run
    kicad-tools sch set-symbol-property board.kicad_sch --ref "#FLG05" \
        --property on_board --value yes --dry-run
"""

import sys
from pathlib import Path

from kicad_tools.cli.modify_schematic import (
    create_backup,
    find_symbol_text_range,
    set_symbol_flag_text,
)
from kicad_tools.cli.sch_set_footprint import _collect_schematic_files

# Recognized symbol-level boolean flags
RECOGNIZED_FLAGS = frozenset({"on_board", "in_bom", "dnp", "exclude_from_sim"})

# Accepted truthy/falsy inputs normalized to KiCad yes/no
_VALUE_MAP = {
    "yes": "yes",
    "no": "no",
    "true": "yes",
    "false": "no",
    "1": "yes",
    "0": "no",
}


def _normalize_flag_value(raw: str) -> str | None:
    """Normalize a user-supplied flag value to 'yes' or 'no'.

    Returns None if the value is not recognized.
    """
    return _VALUE_MAP.get(raw.lower())


def run_set_symbol_property(
    schematic_path: Path,
    ref: str,
    property_name: str,
    value: str,
    dry_run: bool = False,
    backup: bool = True,
) -> int:
    """Run the set-symbol-property operation.

    Returns 0 on success, 1 on error.
    """
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    # Validate property name
    if property_name not in RECOGNIZED_FLAGS:
        print(
            f"Error: Unrecognized property '{property_name}'. "
            f"Recognized properties: {', '.join(sorted(RECOGNIZED_FLAGS))}",
            file=sys.stderr,
        )
        return 1

    # Normalize value
    normalized = _normalize_flag_value(value)
    if normalized is None:
        print(
            f"Error: Invalid value '{value}'. "
            f"Accepted values: yes, no, true, false, 1, 0",
            file=sys.stderr,
        )
        return 1

    # Collect all schematic files (root + sub-sheets)
    all_files = _collect_schematic_files(schematic_path)

    for sch_file in all_files:
        try:
            text = sch_file.read_text(encoding="utf-8")
        except OSError as e:
            print(f"Error reading {sch_file}: {e}", file=sys.stderr)
            continue

        # Check if symbol exists in this file
        result = find_symbol_text_range(text, ref)
        if result is None:
            continue

        # Found the symbol in this file
        if dry_run:
            _, _, info = result
            # Read the current flag value from the block
            start, end, _ = result
            block = text[start:end]
            import re

            flag_match = re.search(
                rf"\({re.escape(property_name)}\s+(yes|no)\)", block
            )
            if flag_match:
                old_val = flag_match.group(1)
                print(
                    f"  {ref}: {property_name} '{old_val}' -> '{normalized}'"
                    f" (in {sch_file.name})"
                )
            else:
                print(
                    f"  Warning: Flag '{property_name}' not found on"
                    f" symbol '{ref}' in {sch_file.name}",
                    file=sys.stderr,
                )
                return 1
            print()
            print(
                f"Dry run: {property_name} on {ref} would be changed"
                f" to '{normalized}'"
            )
            return 0

        # Apply the change
        new_text, success, msg = set_symbol_flag_text(
            text, ref, property_name, normalized
        )
        if not success:
            print(f"Error: {msg}", file=sys.stderr)
            return 1

        # Write back
        if backup:
            bak = create_backup(sch_file)
            print(f"  Backup: {bak.name}")

        try:
            sch_file.write_text(new_text, encoding="utf-8")
        except OSError as e:
            print(f"Error writing {sch_file}: {e}", file=sys.stderr)
            return 1

        print(f"  {msg} (in {sch_file.name})")
        return 0

    # Symbol not found in any file
    print(
        f"Error: Symbol '{ref}' not found in {schematic_path.name}"
        f" or its sub-sheets",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None):
    """CLI entry point for standalone usage."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Set symbol-level boolean flags in KiCad schematics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to .kicad_sch file")
    parser.add_argument(
        "--ref", required=True, help="Symbol reference (e.g., #PWR052, U1)"
    )
    parser.add_argument(
        "--property",
        dest="property_name",
        required=True,
        help="Flag to modify (on_board, in_bom, dnp, exclude_from_sim)",
    )
    parser.add_argument(
        "--value",
        required=True,
        help="Value to set (yes/no/true/false/1/0)",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview changes without modifying files",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip creating backup files",
    )

    args = parser.parse_args(argv)

    return run_set_symbol_property(
        schematic_path=args.schematic,
        ref=args.ref,
        property_name=args.property_name,
        value=args.value,
        dry_run=args.dry_run,
        backup=not args.no_backup,
    )


if __name__ == "__main__":
    sys.exit(main())
