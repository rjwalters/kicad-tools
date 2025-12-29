#!/usr/bin/env python3
"""
Show exact pin positions for a symbol instance in a schematic.

Usage:
    python3 sch-pin-positions.py <schematic.kicad_sch> <reference> --lib <library.kicad_sym>

Options:
    --lib <path>           Path to symbol library file (required)
    --format {table,json}  Output format (default: table)

Examples:
    # Show pin positions for U1
    python3 sch-pin-positions.py amplifier.kicad_sch U1 --lib lib/symbols/TPA3116D2.kicad_sym

    # Output as JSON
    python3 sch-pin-positions.py amplifier.kicad_sch U1 --lib lib/TPA3116D2.kicad_sym --format json
"""

import argparse
import json
import sys

from kicad_tools.schema import Schematic, SymbolLibrary


def main():
    parser = argparse.ArgumentParser(
        description="Show pin positions for a symbol instance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("reference", help="Symbol reference (e.g., U1)")
    parser.add_argument("--lib", required=True, help="Path to symbol library file")
    parser.add_argument(
        "--format", choices=["table", "json"], default="table", help="Output format"
    )

    args = parser.parse_args()

    # Load schematic
    try:
        sch = Schematic.load(args.schematic)
    except FileNotFoundError:
        print(f"Error: Schematic not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading schematic: {e}", file=sys.stderr)
        sys.exit(1)

    # Load library
    try:
        lib = SymbolLibrary.load(args.lib)
    except FileNotFoundError:
        print(f"Error: Library not found: {args.lib}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading library: {e}", file=sys.stderr)
        sys.exit(1)

    # Find the symbol instance
    symbol = sch.get_symbol(args.reference)
    if not symbol:
        print(f"Error: Symbol '{args.reference}' not found in schematic", file=sys.stderr)
        refs = sorted(s.reference for s in sch.symbols if s.reference)
        if refs:
            print(f"Available symbols: {', '.join(refs[:20])}", file=sys.stderr)
        sys.exit(1)

    # Find the library symbol
    # Extract symbol name from lib_id (e.g., "chorus-revA:TPA3116D2" -> "TPA3116D2")
    lib_id = symbol.lib_id
    if ":" in lib_id:
        sym_name = lib_id.split(":", 1)[1]
    else:
        sym_name = lib_id

    lib_sym = lib.get_symbol(sym_name)
    if not lib_sym:
        # Try all symbols in the library
        if len(lib.symbols) == 1:
            lib_sym = list(lib.symbols.values())[0]
        else:
            print(f"Error: Symbol '{sym_name}' not found in library", file=sys.stderr)
            print(f"Available: {', '.join(lib.symbols.keys())}", file=sys.stderr)
            sys.exit(1)

    # Calculate pin positions
    pin_positions = lib_sym.get_all_pin_positions(
        instance_pos=symbol.position,
        instance_rot=symbol.rotation,
        mirror=symbol.mirror,
    )

    if args.format == "json":
        output_json(symbol, lib_sym, pin_positions)
    else:
        output_table(symbol, lib_sym, pin_positions)


def output_table(symbol, lib_sym, pin_positions):
    """Output as formatted table."""
    print(f"Symbol: {symbol.reference} ({symbol.value})")
    print(f"Library: {lib_sym.name}")
    print(f"Instance position: ({symbol.position[0]:.2f}, {symbol.position[1]:.2f})")
    print(f"Rotation: {symbol.rotation}°")
    if symbol.mirror:
        print(f"Mirror: {symbol.mirror}")
    print("=" * 70)

    print(f"\n{'Pin':<5}  {'Name':<15}  {'Type':<12}  {'Schematic Position':<25}")
    print("-" * 70)

    for pin in sorted(
        lib_sym.pins,
        key=lambda p: (
            not p.number.isdigit(),
            int(p.number) if p.number.isdigit() else 0,
            p.number,
        ),
    ):
        if pin.number in pin_positions:
            pos = pin_positions[pin.number]
            pos_str = f"({pos[0]:.2f}, {pos[1]:.2f})"
        else:
            pos_str = "N/A"
        print(f"{pin.number:<5}  {pin.name:<15}  {pin.type:<12}  {pos_str:<25}")

    print(f"\nTotal: {len(pin_positions)} pins")


def output_json(symbol, lib_sym, pin_positions):
    """Output as JSON."""
    data = {
        "reference": symbol.reference,
        "value": symbol.value,
        "lib_id": symbol.lib_id,
        "instance_position": list(symbol.position),
        "rotation": symbol.rotation,
        "mirror": symbol.mirror,
        "pins": [
            {
                "number": pin.number,
                "name": pin.name,
                "type": pin.type,
                "library_position": list(pin.position),
                "schematic_position": list(pin_positions[pin.number])
                if pin.number in pin_positions
                else None,
            }
            for pin in lib_sym.pins
        ],
    }
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
