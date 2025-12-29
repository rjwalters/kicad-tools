#!/usr/bin/env python3
"""
Generate Bill of Materials (BOM) from a KiCad schematic.

Usage:
    python3 sch-generate-bom.py <schematic.kicad_sch> [options]

Options:
    --format {table,csv,jlcpcb,lcsc,json}  Output format (default: table)
    --output <file>                         Write to file
    --group {value+footprint,value,mpn}     Grouping mode (default: value+footprint)
    --include-dnp                           Include DNP components
    --no-hierarchy                          Only process root schematic
    --filter <pattern>                      Filter by reference (e.g., "R*", "U*")

Examples:
    # Generate BOM table
    python3 sch-generate-bom.py project.kicad_sch

    # JLCPCB format for assembly
    python3 sch-generate-bom.py project.kicad_sch --format jlcpcb --output bom.csv

    # Filter to only ICs
    python3 sch-generate-bom.py project.kicad_sch --filter "U*"

    # Include DNP components
    python3 sch-generate-bom.py project.kicad_sch --include-dnp
"""

import argparse
import csv
import json
import sys
from io import StringIO
from pathlib import Path

from kicad_tools.schema.bom import BOM, extract_bom


def main():
    parser = argparse.ArgumentParser(
        description="Generate BOM from KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--format",
        "-f",
        choices=["table", "csv", "jlcpcb", "lcsc", "json"],
        default="table",
        help="Output format",
    )
    parser.add_argument("--output", "-o", help="Output file")
    parser.add_argument(
        "--group",
        choices=["value+footprint", "value", "mpn"],
        default="value+footprint",
        help="Grouping mode",
    )
    parser.add_argument("--include-dnp", action="store_true", help="Include DNP components")
    parser.add_argument("--no-hierarchy", action="store_true", help="Only process root schematic")
    parser.add_argument("--filter", dest="pattern", help="Filter by reference pattern")

    args = parser.parse_args()

    # Extract BOM
    try:
        bom = extract_bom(args.schematic, hierarchical=not args.no_hierarchy)
    except FileNotFoundError:
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error extracting BOM: {e}", file=sys.stderr)
        sys.exit(1)

    # Apply filters
    bom = bom.filter(
        include_dnp=args.include_dnp,
        reference_pattern=args.pattern,
    )

    # Group
    groups = bom.grouped(by=args.group)

    # Generate output
    if args.format == "table":
        output = format_table(bom, groups)
    elif args.format == "csv":
        output = format_csv(groups)
    elif args.format == "jlcpcb":
        output = format_jlcpcb(groups)
    elif args.format == "lcsc":
        output = format_lcsc(groups)
    elif args.format == "json":
        output = format_json(bom, groups)
    else:
        output = format_table(bom, groups)

    # Write or print
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"BOM written to {args.output}")
        print(f"  {bom.total_components} components, {bom.unique_parts} unique parts")
    else:
        print(output)


def format_table(bom: BOM, groups: list) -> str:
    """Format as human-readable table."""
    lines = []
    lines.append(f"Bill of Materials: {Path(bom.source).name}")
    lines.append("=" * 80)
    lines.append(f"Total components: {bom.total_components}")
    lines.append(f"Unique parts: {bom.unique_parts}")
    if bom.dnp_count:
        lines.append(f"DNP components: {bom.dnp_count}")
    lines.append("")

    # Header
    lines.append(f"{'Qty':<5}  {'Value':<20}  {'Footprint':<25}  References")
    lines.append("-" * 80)

    for group in groups:
        refs = group.references
        if len(refs) > 30:
            refs = refs[:27] + "..."
        fp = group.footprint
        if len(fp) > 23:
            fp = "..." + fp[-20:]
        lines.append(f"{group.quantity:<5}  {group.value:<20}  {fp:<25}  {refs}")

    return "\n".join(lines)


def format_csv(groups: list) -> str:
    """Format as generic CSV."""
    output = StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow(["Quantity", "Value", "Footprint", "References", "LCSC", "MPN", "Description"])

    for group in groups:
        writer.writerow(
            [
                group.quantity,
                group.value,
                group.footprint,
                group.references,
                group.lcsc,
                group.mpn,
                group.description,
            ]
        )

    return output.getvalue()


def format_jlcpcb(groups: list) -> str:
    """
    Format for JLCPCB SMT assembly.

    JLCPCB requires: Comment, Designator, Footprint, LCSC Part Number
    """
    output = StringIO()
    writer = csv.writer(output)

    # JLCPCB header
    writer.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])

    for group in groups:
        writer.writerow(
            [
                group.value,
                group.references,
                group.footprint,
                group.lcsc,
            ]
        )

    return output.getvalue()


def format_lcsc(groups: list) -> str:
    """
    Format for LCSC ordering.

    Includes part numbers and quantities for ordering.
    """
    output = StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow(["LCSC Part #", "Quantity", "Value", "Footprint", "References"])

    # Sort by LCSC presence (parts with LCSC first)
    sorted_groups = sorted(groups, key=lambda g: (not bool(g.lcsc), g.value))

    for group in sorted_groups:
        writer.writerow(
            [
                group.lcsc or "NOT FOUND",
                group.quantity,
                group.value,
                group.footprint,
                group.references,
            ]
        )

    return output.getvalue()


def format_json(bom: BOM, groups: list) -> str:
    """Format as JSON."""
    data = {
        "source": bom.source,
        "summary": {
            "total_components": bom.total_components,
            "unique_parts": bom.unique_parts,
            "dnp_count": bom.dnp_count,
        },
        "groups": [
            {
                "quantity": g.quantity,
                "value": g.value,
                "footprint": g.footprint,
                "references": g.references.split(", "),
                "lcsc": g.lcsc,
                "mpn": g.mpn,
                "description": g.description,
            }
            for g in groups
        ],
        "items": [
            {
                "reference": item.reference,
                "value": item.value,
                "footprint": item.footprint,
                "lib_id": item.lib_id,
                "lcsc": item.lcsc,
                "mpn": item.mpn,
                "dnp": item.dnp,
            }
            for item in bom.items
            if not item.is_virtual
        ],
    }

    return json.dumps(data, indent=2)


if __name__ == "__main__":
    main()
