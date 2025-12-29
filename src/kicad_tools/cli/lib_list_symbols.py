#!/usr/bin/env python3
"""
List symbols in a KiCad symbol library.

Usage:
    python3 lib-list-symbols.py <library.kicad_sym> [options]

Options:
    --format {table,json}  Output format (default: table)
    --pins                 Show pin details for each symbol

Examples:
    # List all symbols in a library
    python3 lib-list-symbols.py TPA3116D2.kicad_sym

    # Show with pin details
    python3 lib-list-symbols.py TPA3116D2.kicad_sym --pins
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.schema import SymbolLibrary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="List symbols in a KiCad symbol library",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("library", help="Path to .kicad_sym file")
    parser.add_argument(
        "--format", choices=["table", "json"], default="table", help="Output format"
    )
    parser.add_argument("--pins", action="store_true", help="Show pin details")

    args = parser.parse_args(argv)

    try:
        lib = SymbolLibrary.load(args.library)
    except FileNotFoundError:
        print(f"Error: File not found: {args.library}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading library: {e}", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        output_json(lib, args.pins)
    else:
        output_table(lib, args.pins)


def output_table(lib, show_pins):
    """Output as formatted table."""
    print(f"Symbol Library: {Path(lib.path).name}")
    print("=" * 60)

    if not lib.symbols:
        print("No symbols found.")
        return

    for name, sym in sorted(lib.symbols.items()):
        print(f"\n{name}")
        print("-" * 40)

        # Show properties
        if "Value" in sym.properties:
            print(f"  Value:       {sym.properties['Value']}")
        if "Footprint" in sym.properties:
            print(f"  Footprint:   {sym.properties['Footprint']}")
        if "Description" in sym.properties:
            desc = sym.properties["Description"]
            if len(desc) > 50:
                desc = desc[:47] + "..."
            print(f"  Description: {desc}")

        print(f"  Pin count:   {sym.pin_count}")

        if show_pins and sym.pins:
            print("\n  Pins:")
            print(f"  {'#':<5}  {'Name':<15}  {'Type':<12}  Position")
            print("  " + "-" * 50)
            for pin in sorted(
                sym.pins,
                key=lambda p: (
                    p.number.isdigit(),
                    int(p.number) if p.number.isdigit() else 0,
                    p.number,
                ),
            ):
                pos_str = f"({pin.position[0]:.1f}, {pin.position[1]:.1f})"
                print(f"  {pin.number:<5}  {pin.name:<15}  {pin.type:<12}  {pos_str}")

    print(f"\nTotal: {len(lib.symbols)} symbols")


def output_json(lib, show_pins):
    """Output as JSON."""
    data = {"path": lib.path, "symbol_count": len(lib.symbols), "symbols": []}

    for name, sym in sorted(lib.symbols.items()):
        sym_data = {
            "name": name,
            "properties": sym.properties,
            "pin_count": sym.pin_count,
        }

        if show_pins:
            sym_data["pins"] = [
                {
                    "number": p.number,
                    "name": p.name,
                    "type": p.type,
                    "position": list(p.position),
                    "rotation": p.rotation,
                    "length": p.length,
                }
                for p in sym.pins
            ]

        data["symbols"].append(sym_data)

    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
