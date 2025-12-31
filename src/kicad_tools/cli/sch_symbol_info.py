#!/usr/bin/env python3
"""
Show detailed information about a symbol instance in a KiCad schematic.

Usage:
    python3 sch-symbol-info.py <schematic.kicad_sch> <reference>

Options:
    --json            Output as JSON
    --show-pins       Show pin details
    --show-properties Show all properties

Examples:
    # Show info about U1
    python3 sch-symbol-info.py amplifier.kicad_sch U1

    # Get JSON output
    python3 sch-symbol-info.py amplifier.kicad_sch U1 --json

    # Show all details
    python3 sch-symbol-info.py amplifier.kicad_sch U1 --show-pins --show-properties
"""

import argparse
import json
import sys

from kicad_tools.schema import Schematic


def main():
    parser = argparse.ArgumentParser(
        description="Show symbol details in a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("reference", help="Symbol reference (e.g., U1, R1)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--show-pins", action="store_true", help="Show pin details")
    parser.add_argument("--show-properties", action="store_true", help="Show all properties")

    args = parser.parse_args()

    try:
        sch = Schematic.load(args.schematic)
    except FileNotFoundError:
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading schematic: {e}", file=sys.stderr)
        sys.exit(1)

    # Find the symbol
    symbol = sch.get_symbol(args.reference)
    if not symbol:
        print(f"Error: Symbol '{args.reference}' not found", file=sys.stderr)
        # Show available symbols
        refs = sorted(s.reference for s in sch.symbols if s.reference)
        if refs:
            print(f"Available symbols: {', '.join(refs[:20])}", file=sys.stderr)
            if len(refs) > 20:
                print(f"  ... and {len(refs) - 20} more", file=sys.stderr)
        sys.exit(1)

    if args.json:
        output_json(symbol, args.show_pins, args.show_properties)
    else:
        output_text(symbol, args.show_pins, args.show_properties)


def output_json(symbol, show_pins, show_properties):
    """Output symbol info as JSON."""
    data = {
        "reference": symbol.reference,
        "value": symbol.value,
        "lib_id": symbol.lib_id,
        "footprint": symbol.footprint,
        "position": list(symbol.position),
        "rotation": symbol.rotation,
        "unit": symbol.unit,
        "uuid": symbol.uuid,
        "in_bom": symbol.in_bom,
        "dnp": symbol.dnp,
    }

    if show_properties:
        data["properties"] = [
            {"name": p.name, "value": p.value, "position": list(p.position)}
            for p in symbol.properties.values()
        ]

    if show_pins:
        data["pins"] = [{"number": p.number, "uuid": p.uuid} for p in symbol.pins]

    print(json.dumps(data, indent=2))


def output_text(symbol, show_pins, show_properties):
    """Output symbol info as formatted text."""
    print(f"Symbol: {symbol.reference}")
    print("=" * 50)
    print(f"  Value:     {symbol.value}")
    print(f"  Library:   {symbol.lib_id}")
    print(f"  Footprint: {symbol.footprint}")
    print(f"  Position:  ({symbol.position[0]:.2f}, {symbol.position[1]:.2f})")
    print(f"  Rotation:  {symbol.rotation}°")
    print(f"  Unit:      {symbol.unit}")
    print(f"  UUID:      {symbol.uuid}")
    print(f"  In BOM:    {symbol.in_bom}")
    print(f"  DNP:       {symbol.dnp}")

    if show_properties:
        print("\nProperties:")
        print("-" * 50)
        for prop in symbol.properties.values():
            print(f"  {prop.name}: {prop.value}")
            if prop.position != (0, 0):
                print(f"    Position: ({prop.position[0]:.2f}, {prop.position[1]:.2f})")

    if show_pins:
        print(f"\nPins ({len(symbol.pins)} total):")
        print("-" * 50)
        for pin in symbol.pins:
            print(f"  Pin {pin.number}: uuid={pin.uuid}")


if __name__ == "__main__":
    main()
