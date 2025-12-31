#!/usr/bin/env python3
"""
Example: Schematic Analysis

Demonstrates how to load a KiCad schematic and extract information about
symbols, nets, and wires using the kicad-tools Python API.

Usage:
    python analyze.py [schematic_file]

If no file is specified, uses the included simple_rc.kicad_sch.
"""

import sys
from pathlib import Path

from kicad_tools import Schematic


def analyze_schematic(schematic_path: Path) -> None:
    """Load and analyze a KiCad schematic file."""
    print(f"Loading schematic: {schematic_path}")
    print("=" * 60)

    sch = Schematic.load(schematic_path)

    # Title block information
    tb = sch.title_block
    if tb.title:
        print(f"\nTitle: {tb.title}")
        if tb.rev:
            print(f"Revision: {tb.rev}")
        if tb.date:
            print(f"Date: {tb.date}")
        if tb.company:
            print(f"Company: {tb.company}")

    # Symbols - filter to only include instances (those with lib_id set)
    instances = [s for s in sch.symbols if s.lib_id]
    print("\n=== Symbols ===")
    print(f"{'Reference':<12} {'Value':<15} {'Library ID'}")
    print("-" * 60)
    for symbol in sorted(instances, key=lambda s: s.reference or ""):
        ref = symbol.reference or "(no ref)"
        val = symbol.value or "(no value)"
        lib = symbol.lib_id
        print(f"{ref:<12} {val:<15} {lib}")

    # Labels (local net names)
    print("\n=== Labels (Nets) ===")
    if sch.labels:
        for label in sch.labels:
            print(f"  {label.text}")
    else:
        print("  (no local labels)")

    # Global labels
    global_labels = sch.global_labels
    if global_labels:
        print("\n=== Global Labels ===")
        for label in global_labels:
            print(f"  {label.text}")

    # Hierarchical labels
    if sch.hierarchical_labels:
        print("\n=== Hierarchical Labels ===")
        for label in sch.hierarchical_labels:
            print(f"  {label.text}")

    # Summary
    print("\n=== Summary ===")
    print(f"Total symbols: {len(instances)}")
    print(f"Total wires: {len(sch.wires)}")
    print(f"Total junctions: {len(sch.junctions)}")
    print(f"Total labels: {len(sch.labels)}")

    if sch.is_hierarchical():
        print(f"Hierarchical sheets: {len(sch.sheets)}")


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
        analyze_schematic(schematic_path)
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
