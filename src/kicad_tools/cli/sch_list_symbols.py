#!/usr/bin/env python3
"""
List all symbols in a KiCad schematic.

Usage:
    python3 sch-list-symbols.py <schematic.kicad_sch> [options]

Options:
    --format {table,json,csv}  Output format (default: table)
    --filter <pattern>         Filter by reference pattern (e.g., "U*", "R*")
    --lib <lib_id>             Filter by library ID (e.g., "Device:R")
    --verbose                  Show additional details

Examples:
    # List all symbols
    python3 sch-list-symbols.py amplifier.kicad_sch

    # List only ICs
    python3 sch-list-symbols.py amplifier.kicad_sch --filter "U*"

    # Output as JSON
    python3 sch-list-symbols.py amplifier.kicad_sch --format json
"""

import argparse
import fnmatch
import json
import sys

from kicad_tools.schema import Schematic


def main():
    parser = argparse.ArgumentParser(
        description="List symbols in a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--format", choices=["table", "json", "csv"], default="table", help="Output format"
    )
    parser.add_argument("--filter", dest="pattern", help="Filter by reference pattern (e.g., 'U*')")
    parser.add_argument("--lib", dest="lib_id", help="Filter by library ID")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show additional details")

    args = parser.parse_args()

    try:
        sch = Schematic.load(args.schematic)
    except FileNotFoundError:
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading schematic: {e}", file=sys.stderr)
        sys.exit(1)

    # Get symbols and apply filters
    symbols = list(sch.symbols)

    if args.pattern:
        symbols = [s for s in symbols if fnmatch.fnmatch(s.reference, args.pattern)]

    if args.lib_id:
        symbols = [s for s in symbols if args.lib_id in s.lib_id]

    # Sort by reference
    symbols.sort(key=lambda s: (s.reference[0] if s.reference else "", s.reference))

    # Output
    if args.format == "json":
        output_json(symbols, args.verbose)
    elif args.format == "csv":
        output_csv(symbols, args.verbose)
    else:
        output_table(symbols, args.verbose)


def output_table(symbols, verbose):
    """Output as formatted table."""
    if not symbols:
        print("No symbols found.")
        return

    # Calculate column widths
    ref_width = max(len(s.reference) for s in symbols)
    ref_width = max(ref_width, 3)  # "Ref" header

    val_width = max(len(s.value) for s in symbols)
    val_width = max(val_width, 5)  # "Value" header

    lib_width = max(len(s.lib_id) for s in symbols)
    lib_width = max(lib_width, 10)  # "Library ID" header

    if verbose:
        fp_width = max(len(s.footprint) for s in symbols)
        fp_width = max(fp_width, 9)  # "Footprint" header

        print(
            f"{'Ref':<{ref_width}}  {'Value':<{val_width}}  "
            f"{'Library ID':<{lib_width}}  {'Footprint':<{fp_width}}  Position"
        )
        print("-" * (ref_width + val_width + lib_width + fp_width + 30))

        for sym in symbols:
            print(
                f"{sym.reference:<{ref_width}}  {sym.value:<{val_width}}  "
                f"{sym.lib_id:<{lib_width}}  {sym.footprint:<{fp_width}}  "
                f"({sym.position[0]:.1f}, {sym.position[1]:.1f})"
            )
    else:
        print(f"{'Ref':<{ref_width}}  {'Value':<{val_width}}  {'Library ID':<{lib_width}}")
        print("-" * (ref_width + val_width + lib_width + 6))

        for sym in symbols:
            print(
                f"{sym.reference:<{ref_width}}  {sym.value:<{val_width}}  {sym.lib_id:<{lib_width}}"
            )

    print(f"\nTotal: {len(symbols)} symbols")


def output_json(symbols, verbose):
    """Output as JSON."""
    data = []
    for sym in symbols:
        entry = {
            "reference": sym.reference,
            "value": sym.value,
            "lib_id": sym.lib_id,
            "footprint": sym.footprint,
        }
        if verbose:
            entry.update(
                {
                    "position": list(sym.position),
                    "rotation": sym.rotation,
                    "unit": sym.unit,
                    "uuid": sym.uuid,
                    "in_bom": sym.in_bom,
                    "dnp": sym.dnp,
                    "pins": [{"number": p.number, "uuid": p.uuid} for p in sym.pins],
                }
            )
        data.append(entry)

    print(json.dumps(data, indent=2))


def output_csv(symbols, verbose):
    """Output as CSV."""
    if verbose:
        print("Reference,Value,Library ID,Footprint,X,Y,Rotation,Unit,UUID")
        for sym in symbols:
            print(
                f"{sym.reference},{sym.value},{sym.lib_id},{sym.footprint},"
                f"{sym.position[0]},{sym.position[1]},{sym.rotation},{sym.unit},{sym.uuid}"
            )
    else:
        print("Reference,Value,Library ID,Footprint")
        for sym in symbols:
            print(f"{sym.reference},{sym.value},{sym.lib_id},{sym.footprint}")


if __name__ == "__main__":
    main()
