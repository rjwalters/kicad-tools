"""
Bill of Materials generation for KiCad schematics.

Usage:
    kicad-bom <schematic.kicad_sch> [options]

Examples:
    kicad-bom design.kicad_sch
    kicad-bom design.kicad_sch --format csv > bom.csv
    kicad-bom design.kicad_sch --group --exclude "TP*"
"""

import argparse
import csv
import fnmatch
import io
import json
import sys
from typing import List

from ..schema.bom import BOMItem, extract_bom


def main(argv: List[str] | None = None) -> int:
    """Main entry point for kicad-bom command."""
    parser = argparse.ArgumentParser(
        prog="kicad-bom",
        description="Generate bill of materials from KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--format",
        choices=["table", "csv", "json"],
        default="table",
        help="Output format",
    )
    parser.add_argument(
        "--group",
        action="store_true",
        help="Group identical components",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude references matching pattern (can repeat)",
    )
    parser.add_argument(
        "--include-dnp",
        action="store_true",
        help="Include Do Not Populate components",
    )
    parser.add_argument(
        "--sort",
        choices=["reference", "value", "footprint"],
        default="reference",
        help="Sort order",
    )

    args = parser.parse_args(argv)

    try:
        bom = extract_bom(args.schematic)
    except FileNotFoundError:
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error loading schematic: {e}", file=sys.stderr)
        return 1

    # Apply filters
    items = list(bom.items)

    # Exclude patterns
    for pattern in args.exclude:
        items = [i for i in items if not fnmatch.fnmatch(i.reference, pattern)]

    # Exclude DNP unless requested
    if not args.include_dnp:
        items = [i for i in items if not i.dnp]

    # Sort
    if args.sort == "value":
        items.sort(key=lambda i: (i.value, i.reference))
    elif args.sort == "footprint":
        items.sort(key=lambda i: (i.footprint, i.reference))
    else:
        items.sort(key=lambda i: (i.reference[0] if i.reference else "", i.reference))

    # Group if requested
    if args.group:
        items = group_items(items)

    # Output
    if args.format == "csv":
        output_csv(items, args.group)
    elif args.format == "json":
        output_json(items, args.group)
    else:
        output_table(items, args.group)

    return 0


def group_items(items: List[BOMItem]) -> List[dict]:
    """Group identical items by value and footprint."""
    groups = {}
    for item in items:
        key = (item.value, item.footprint, item.mpn)
        if key not in groups:
            groups[key] = {
                "value": item.value,
                "footprint": item.footprint,
                "mpn": item.mpn,
                "references": [],
                "quantity": 0,
            }
        groups[key]["references"].append(item.reference)
        groups[key]["quantity"] += 1

    return list(groups.values())


def output_table(items, grouped: bool) -> None:
    """Output BOM as formatted table."""
    if not items:
        print("No components found.")
        return

    if grouped:
        # Calculate column widths
        val_width = max(len(g["value"]) for g in items)
        val_width = max(val_width, 5)
        fp_width = max(len(g["footprint"]) for g in items)
        fp_width = max(fp_width, 9)

        print(f"{'Qty':<5}  {'Value':<{val_width}}  {'Footprint':<{fp_width}}  References")
        print("-" * (val_width + fp_width + 30))

        for g in items:
            refs = ", ".join(g["references"][:5])
            if len(g["references"]) > 5:
                refs += f" +{len(g['references']) - 5} more"
            print(
                f"{g['quantity']:<5}  {g['value']:<{val_width}}  "
                f"{g['footprint']:<{fp_width}}  {refs}"
            )

        print(f"\nTotal: {len(items)} groups, {sum(g['quantity'] for g in items)} components")
    else:
        # Calculate column widths
        ref_width = max(len(i.reference) for i in items)
        ref_width = max(ref_width, 3)
        val_width = max(len(i.value) for i in items)
        val_width = max(val_width, 5)
        fp_width = max(len(i.footprint) for i in items)
        fp_width = max(fp_width, 9)

        print(f"{'Ref':<{ref_width}}  {'Value':<{val_width}}  {'Footprint':<{fp_width}}")
        print("-" * (ref_width + val_width + fp_width + 6))

        for item in items:
            print(
                f"{item.reference:<{ref_width}}  {item.value:<{val_width}}  "
                f"{item.footprint:<{fp_width}}"
            )

        print(f"\nTotal: {len(items)} components")


def output_csv(items, grouped: bool) -> None:
    """Output BOM as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)

    if grouped:
        writer.writerow(["Quantity", "Value", "Footprint", "MPN", "References"])
        for g in items:
            writer.writerow(
                [
                    g["quantity"],
                    g["value"],
                    g["footprint"],
                    g["mpn"] or "",
                    ", ".join(g["references"]),
                ]
            )
    else:
        writer.writerow(["Reference", "Value", "Footprint", "MPN"])
        for item in items:
            writer.writerow(
                [
                    item.reference,
                    item.value,
                    item.footprint,
                    item.mpn or "",
                ]
            )

    print(output.getvalue(), end="")


def output_json(items, grouped: bool) -> None:
    """Output BOM as JSON."""
    if grouped:
        data = {
            "groups": [
                {
                    "quantity": g["quantity"],
                    "value": g["value"],
                    "footprint": g["footprint"],
                    "mpn": g["mpn"],
                    "references": g["references"],
                }
                for g in items
            ],
            "total_groups": len(items),
            "total_components": sum(g["quantity"] for g in items),
        }
    else:
        data = {
            "items": [
                {
                    "reference": i.reference,
                    "value": i.value,
                    "footprint": i.footprint,
                    "mpn": i.mpn,
                    "dnp": i.dnp,
                }
                for i in items
            ],
            "total": len(items),
        }

    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    sys.exit(main())
