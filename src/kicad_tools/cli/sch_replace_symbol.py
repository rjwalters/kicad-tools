#!/usr/bin/env python3
"""
Replace a symbol in a KiCad schematic.

Usage:
    python3 sch-replace-symbol.py <schematic.kicad_sch> <reference> <new_lib_id> [options]

Options:
    --value <value>        Set new value for the symbol
    --footprint <fp>       Set new footprint
    --dry-run              Show what would change without modifying
    --backup               Create backup before modifying

Examples:
    # Replace U1 with a different library symbol
    python3 sch-replace-symbol.py amplifier.kicad_sch U1 "chorus-revA:TPA3116D2"

    # Replace with new value and footprint
    python3 sch-replace-symbol.py amplifier.kicad_sch U1 "chorus-revA:TPA3116D2" \\
        --value "TPA3116D2" --footprint "Package_SO:HTSSOP-32-1EP_6.1x11mm_P0.65mm_EP5.2x11mm"

    # Dry run to see changes
    python3 sch-replace-symbol.py amplifier.kicad_sch U1 "chorus-revA:TPA3116D2" --dry-run
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from kicad_tools.operations.symbol_ops import replace_symbol_lib_id


def main():
    parser = argparse.ArgumentParser(
        description="Replace a symbol in a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("reference", help="Symbol reference to replace (e.g., U1)")
    parser.add_argument("new_lib_id", help="New library ID (e.g., 'chorus-revA:TPA3116D2')")
    parser.add_argument("--value", help="New value for the symbol")
    parser.add_argument("--footprint", help="New footprint")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without modifying")
    parser.add_argument("--backup", action="store_true", help="Create backup before modifying")

    args = parser.parse_args()

    # Validate input
    if not Path(args.schematic).exists():
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)

    # Create backup if requested
    if args.backup and not args.dry_run:
        backup_path = f"{args.schematic}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(args.schematic, backup_path)
        print(f"Backup created: {backup_path}")

    try:
        result = replace_symbol_lib_id(
            schematic_path=args.schematic,
            reference=args.reference,
            new_lib_id=args.new_lib_id,
            new_value=args.value,
            new_footprint=args.footprint,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Output results
    if args.dry_run:
        print("DRY RUN - No changes made")
        print("=" * 50)

    print(f"Symbol: {result.reference}")
    print(f"Library ID: {result.old_lib_id} → {result.new_lib_id}")
    print(f"Pin count: {result.old_pin_count}")
    print()

    if result.changes_made:
        print("Changes:")
        for change in result.changes_made:
            print(f"  • {change}")
    else:
        print("No changes made")

    if result.preserved_properties:
        print()
        print(f"Preserved properties: {', '.join(result.preserved_properties)}")

    if not args.dry_run:
        print()
        print("✓ Schematic updated successfully")
        print()
        print("⚠️  Important: This replacement only updated the lib_id and properties.")
        print("   The pin connections remain from the old symbol.")
        print("   If the new symbol has a different pinout, you must:")
        print("   1. Open the schematic in KiCad")
        print("   2. Delete and re-place the symbol from the library")
        print("   3. Reconnect all wires to the correct pins")


if __name__ == "__main__":
    main()
