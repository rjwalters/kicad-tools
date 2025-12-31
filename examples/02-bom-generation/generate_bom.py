#!/usr/bin/env python3
"""
Example: BOM Generation

Demonstrates how to extract a Bill of Materials from a KiCad schematic
using the kicad-tools Python API.

Usage:
    python generate_bom.py [schematic_file]

If no file is specified, uses the included simple_rc.kicad_sch.
"""

import csv
import json
import sys
from io import StringIO
from pathlib import Path

from kicad_tools import extract_bom


def generate_bom(schematic_path: Path) -> None:
    """Generate BOM from a KiCad schematic file."""
    print(f"Extracting BOM from: {schematic_path}")
    print("=" * 70)

    # Extract BOM (hierarchical=False for simple schematics)
    bom = extract_bom(str(schematic_path), hierarchical=False)

    # Filter to only include items with references (skip library definitions)
    bom.items = [item for item in bom.items if item.reference]

    # Table format (ungrouped)
    print("\n=== Individual Components ===")
    print(f"{'Reference':<12} {'Value':<15} {'Footprint':<35} {'DNP'}")
    print("-" * 70)
    for item in sorted(bom.items, key=lambda x: x.reference):
        dnp = "Yes" if item.dnp else ""
        print(f"{item.reference:<12} {item.value:<15} {item.footprint:<35} {dnp}")

    # Grouped format
    print("\n=== Grouped BOM ===")
    groups = bom.grouped()
    print(f"{'Qty':<5} {'References':<15} {'Value':<15} {'Footprint'}")
    print("-" * 70)
    for group in groups:
        print(f"{group.quantity:<5} {group.references:<15} {group.value:<15} {group.footprint}")

    # Summary
    print("\n=== Summary ===")
    print(f"Total components: {bom.total_components}")
    print(f"Unique parts: {bom.unique_parts}")
    if bom.dnp_count > 0:
        print(f"DNP (Do Not Populate): {bom.dnp_count}")

    # Export examples
    print("\n=== Export Formats ===")

    # CSV output
    print("\n--- CSV Format ---")
    csv_output = StringIO()
    writer = csv.writer(csv_output)
    writer.writerow(["Quantity", "References", "Value", "Footprint"])
    for group in groups:
        writer.writerow([group.quantity, group.references, group.value, group.footprint])
    print(csv_output.getvalue().strip())

    # JSON output
    print("\n--- JSON Format ---")
    json_data = [
        {
            "quantity": group.quantity,
            "references": group.references,
            "value": group.value,
            "footprint": group.footprint,
        }
        for group in groups
    ]
    print(json.dumps(json_data, indent=2))


def main() -> int:
    """Main entry point."""
    # Default to the included sample schematic
    if len(sys.argv) > 1:
        schematic_path = Path(sys.argv[1])
    else:
        schematic_path = Path(__file__).parent / "simple_rc.kicad_sch"

    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    try:
        generate_bom(schematic_path)
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
