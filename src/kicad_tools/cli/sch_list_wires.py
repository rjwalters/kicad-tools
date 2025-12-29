#!/usr/bin/env python3
"""
List all wires and junctions in a KiCad schematic.

Usage:
    python3 sch-list-wires.py <schematic.kicad_sch> [options]

Options:
    --format {table,json,csv}  Output format (default: table)
    --stats                    Show wire statistics only
    --junctions                Include junction points

Examples:
    # List all wires
    python3 sch-list-wires.py amplifier.kicad_sch

    # Get statistics
    python3 sch-list-wires.py amplifier.kicad_sch --stats

    # Output as JSON
    python3 sch-list-wires.py amplifier.kicad_sch --format json
"""

import argparse
import json
import sys

from kicad_tools.schema import Schematic


def main():
    parser = argparse.ArgumentParser(
        description="List wires in a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--format", choices=["table", "json", "csv"], default="table", help="Output format"
    )
    parser.add_argument("--stats", action="store_true", help="Show statistics only")
    parser.add_argument("--junctions", action="store_true", help="Include junction points")

    args = parser.parse_args()

    try:
        sch = Schematic.load(args.schematic)
    except FileNotFoundError:
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading schematic: {e}", file=sys.stderr)
        sys.exit(1)

    wires = list(sch.wires)
    junctions = list(sch.junctions)

    if args.stats:
        output_stats(wires, junctions)
    elif args.format == "json":
        output_json(wires, junctions if args.junctions else [], args.junctions)
    elif args.format == "csv":
        output_csv(wires, junctions if args.junctions else [], args.junctions)
    else:
        output_table(wires, junctions if args.junctions else [], args.junctions)


def output_stats(wires, junctions):
    """Output wire statistics."""
    total_length = sum(w.length for w in wires)

    # Count horizontal vs vertical
    horizontal = sum(1 for w in wires if abs(w.start[1] - w.end[1]) < 0.1)
    vertical = sum(1 for w in wires if abs(w.start[0] - w.end[0]) < 0.1)
    diagonal = len(wires) - horizontal - vertical

    print("Wire Statistics")
    print("=" * 40)
    print(f"Total wires:      {len(wires)}")
    print(f"Total length:     {total_length:.1f} mm")
    print(f"Horizontal:       {horizontal}")
    print(f"Vertical:         {vertical}")
    print(f"Diagonal:         {diagonal}")
    print(f"Junctions:        {len(junctions)}")

    if wires:
        avg_length = total_length / len(wires)
        max_wire = max(wires, key=lambda w: w.length)
        min_wire = min(wires, key=lambda w: w.length)
        print(f"Average length:   {avg_length:.2f} mm")
        print(f"Longest wire:     {max_wire.length:.2f} mm")
        print(f"Shortest wire:    {min_wire.length:.2f} mm")


def output_table(wires, junctions, show_junctions):
    """Output as formatted table."""
    if not wires and not junctions:
        print("No wires or junctions found.")
        return

    if wires:
        print("Wires")
        print("=" * 60)
        print(f"{'#':<4}  {'Start':<20}  {'End':<20}  {'Length':<8}")
        print("-" * 60)

        for i, wire in enumerate(wires, 1):
            start = f"({wire.start[0]:.1f}, {wire.start[1]:.1f})"
            end = f"({wire.end[0]:.1f}, {wire.end[1]:.1f})"
            print(f"{i:<4}  {start:<20}  {end:<20}  {wire.length:.2f}")

        print(f"\nTotal: {len(wires)} wires")

    if show_junctions and junctions:
        print("\nJunctions")
        print("=" * 40)
        print(f"{'#':<4}  {'Position':<20}")
        print("-" * 40)

        for i, junc in enumerate(junctions, 1):
            pos = f"({junc.position[0]:.1f}, {junc.position[1]:.1f})"
            print(f"{i:<4}  {pos:<20}")

        print(f"\nTotal: {len(junctions)} junctions")


def output_json(wires, junctions, show_junctions):
    """Output as JSON."""
    data = {
        "wires": [
            {
                "start": list(w.start),
                "end": list(w.end),
                "length": w.length,
                "uuid": w.uuid,
            }
            for w in wires
        ],
        "statistics": {
            "wire_count": len(wires),
            "total_length": sum(w.length for w in wires),
            "junction_count": len(junctions),
        },
    }

    if show_junctions:
        data["junctions"] = [
            {
                "position": list(j.position),
                "uuid": j.uuid,
            }
            for j in junctions
        ]

    print(json.dumps(data, indent=2))


def output_csv(wires, junctions, show_junctions):
    """Output as CSV."""
    print("Type,X1,Y1,X2,Y2,Length,UUID")
    for w in wires:
        print(f"wire,{w.start[0]},{w.start[1]},{w.end[0]},{w.end[1]},{w.length:.2f},{w.uuid}")

    if show_junctions:
        for j in junctions:
            print(f"junction,{j.position[0]},{j.position[1]},,,0,{j.uuid}")


if __name__ == "__main__":
    main()
