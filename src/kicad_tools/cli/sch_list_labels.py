#!/usr/bin/env python3
"""
List all labels in a KiCad schematic.

Usage:
    python3 sch-list-labels.py <schematic.kicad_sch> [options]

Options:
    --format {table,json,csv}  Output format (default: table)
    --type {all,local,global,hierarchical,power}  Filter by label type
    --filter <pattern>         Filter by label text pattern

Examples:
    # List all labels
    python3 sch-list-labels.py amplifier.kicad_sch

    # List only global labels
    python3 sch-list-labels.py amplifier.kicad_sch --type global

    # Find labels matching pattern
    python3 sch-list-labels.py amplifier.kicad_sch --filter "OUT*"
"""

import argparse
import fnmatch
import json
import sys

from kicad_tools.schema import Schematic


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="List labels in a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--format", choices=["table", "json", "csv"], default="table", help="Output format"
    )
    parser.add_argument(
        "--type",
        choices=["all", "local", "global", "hierarchical", "power"],
        default="all",
        help="Filter by label type",
    )
    parser.add_argument("--filter", dest="pattern", help="Filter by label text pattern")

    args = parser.parse_args(argv)

    try:
        sch = Schematic.load(args.schematic)
    except FileNotFoundError:
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading schematic: {e}", file=sys.stderr)
        sys.exit(1)

    # Collect labels by type
    labels = []

    if args.type in ["all", "local"]:
        for lbl in sch.labels:
            labels.append(
                {
                    "type": "local",
                    "text": lbl.text,
                    "position": lbl.position,
                    "rotation": lbl.rotation,
                    "uuid": lbl.uuid,
                }
            )

    if args.type in ["all", "global"]:
        for lbl in sch.global_labels:
            labels.append(
                {
                    "type": "global",
                    "text": lbl.text,
                    "position": lbl.position,
                    "rotation": lbl.rotation,
                    "shape": lbl.shape,
                    "uuid": lbl.uuid,
                }
            )

    if args.type in ["all", "hierarchical"]:
        for lbl in sch.hierarchical_labels:
            labels.append(
                {
                    "type": "hierarchical",
                    "text": lbl.text,
                    "position": lbl.position,
                    "rotation": lbl.rotation,
                    "shape": lbl.shape,
                    "uuid": lbl.uuid,
                }
            )

    if args.type in ["all", "power"]:
        for sym in sch.symbols:
            if sym.lib_id.startswith("power:"):
                labels.append(
                    {
                        "type": "power",
                        "text": sym.value,
                        "position": sym.position,
                        "rotation": sym.rotation,
                        "lib_id": sym.lib_id,
                        "uuid": sym.uuid,
                    }
                )

    # Apply pattern filter
    if args.pattern:
        labels = [lbl for lbl in labels if fnmatch.fnmatch(lbl["text"], args.pattern)]

    # Sort by text
    labels.sort(key=lambda lbl: (lbl["type"], lbl["text"]))

    # Output
    if args.format == "json":
        output_json(labels)
    elif args.format == "csv":
        output_csv(labels)
    else:
        output_table(labels)


def output_table(labels):
    """Output as formatted table."""
    if not labels:
        print("No labels found.")
        return

    # Calculate column widths
    type_width = max(len(lbl["type"]) for lbl in labels)
    type_width = max(type_width, 4)

    text_width = max(len(lbl["text"]) for lbl in labels)
    text_width = max(text_width, 4)

    print(f"{'Type':<{type_width}}  {'Text':<{text_width}}  {'Position':<20}  Shape/Lib")
    print("-" * (type_width + text_width + 40))

    for lbl in labels:
        pos = f"({lbl['position'][0]:.1f}, {lbl['position'][1]:.1f})"
        extra = lbl.get("shape", lbl.get("lib_id", ""))
        print(f"{lbl['type']:<{type_width}}  {lbl['text']:<{text_width}}  {pos:<20}  {extra}")

    print(f"\nTotal: {len(labels)} labels")

    # Summary by type
    type_counts = {}
    for lbl in labels:
        t = lbl["type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    if len(type_counts) > 1:
        print("\nBy type:")
        for t, count in sorted(type_counts.items()):
            print(f"  {t}: {count}")


def output_json(labels):
    """Output as JSON."""
    print(json.dumps(labels, indent=2))


def output_csv(labels):
    """Output as CSV."""
    print("Type,Text,X,Y,Rotation,Shape,UUID")
    for lbl in labels:
        shape = lbl.get("shape", lbl.get("lib_id", ""))
        print(
            f"{lbl['type']},{lbl['text']},{lbl['position'][0]},{lbl['position'][1]},"
            f"{lbl['rotation']},{shape},{lbl['uuid']}"
        )


if __name__ == "__main__":
    main()
