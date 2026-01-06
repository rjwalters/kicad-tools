"""
Bill of Materials generation for KiCad schematics.

Usage:
    kicad-bom <schematic.kicad_sch> [options]

Examples:
    kicad-bom design.kicad_sch
    kicad-bom design.kicad_sch --format csv > bom.csv
    kicad-bom design.kicad_sch --group --exclude "TP*"
    kicad-bom design.kicad_sch --check-availability
    kicad-bom design.kicad_sch --check-availability --quantity 5
    kicad-bom design.kicad_sch --validate
"""

import argparse
import csv
import fnmatch
import io
import json
import sys

from ..schema.bom import BOM, BOMItem, extract_bom


def run_validation(bom: BOM, quantity: int, output_format: str) -> int:
    """Run BOM validation against JLCPCB/LCSC parts library."""
    try:
        from ..assembly.validation import AssemblyValidator
    except ImportError as e:
        print(
            f"Error: Assembly validation requires the 'requests' library.\n"
            f"Install with: pip install kicad-tools[parts]\n"
            f"Details: {e}",
            file=sys.stderr,
        )
        return 1

    print("Validating BOM against JLCPCB/LCSC parts library...", file=sys.stderr)

    try:
        with AssemblyValidator() as validator:
            result = validator.validate_bom(bom, quantity)
    except Exception as e:
        print(f"Error during validation: {e}", file=sys.stderr)
        return 1

    # Output results based on format
    if output_format == "json":
        print(json.dumps(result.to_dict(), indent=2))
    else:
        # Table format (default for validation)
        print(result.format_table())

    # Return non-zero exit code if not assembly-ready
    return 0 if result.assembly_ready else 1


def main(argv: list[str] | None = None) -> int:
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
        choices=["table", "csv", "json", "jlcpcb"],
        default="table",
        help="Output format (jlcpcb: JLCPCB assembly BOM)",
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
    parser.add_argument(
        "--check-availability",
        action="store_true",
        help="Check stock availability from LCSC/JLCPCB",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate BOM against JLCPCB/LCSC parts library for assembly",
    )
    parser.add_argument(
        "--quantity",
        type=int,
        default=1,
        help="Number of boards (for availability check or validation)",
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

    # Run validation if requested
    if args.validate:
        return run_validation(bom, args.quantity, args.format)

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

    # Check availability if requested
    if args.check_availability:
        return check_availability(bom, args.quantity, args.format)

    # Output
    if args.format == "csv":
        output_csv(items, args.group)
    elif args.format == "json":
        output_json(items, args.group)
    elif args.format == "jlcpcb":
        output_jlcpcb(bom)
    else:
        output_table(items, args.group)

    return 0


def group_items(items: list[BOMItem]) -> list[dict]:
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


def output_jlcpcb(bom: BOM) -> None:
    """Output BOM in JLCPCB assembly format.

    JLCPCB format:
    - Comment: Component value
    - Designator: Reference designator(s), comma-separated for groups
    - Footprint: Package/footprint name
    - LCSC Part #: LCSC part number for ordering
    """
    from ..export.bom_formats import JLCPCBBOMFormatter

    # Filter out virtual components and DNP
    items = [item for item in bom.items if not item.is_virtual and not item.dnp]

    formatter = JLCPCBBOMFormatter()
    print(formatter.format(items), end="")


def check_availability(bom, quantity: int, output_format: str) -> int:
    """Check availability for BOM items and output results."""
    try:
        from ..cost.availability import LCSCAvailabilityChecker
    except ImportError:
        print(
            "Error: Availability checking requires the 'requests' library.\n"
            "Install with: pip install kicad-tools[parts]",
            file=sys.stderr,
        )
        return 1

    print(f"Checking availability for {quantity} board(s)...", file=sys.stderr)

    try:
        with LCSCAvailabilityChecker() as checker:
            result = checker.check_bom(bom, quantity=quantity)
    except Exception as e:
        print(f"Error checking availability: {e}", file=sys.stderr)
        return 1

    # Output results
    if output_format == "csv":
        output_availability_csv(result)
    elif output_format == "json":
        output_availability_json(result)
    else:
        output_availability_table(result)

    # Return non-zero if any items are unavailable
    if not result.all_available:
        return 2

    return 0


def output_availability_table(result) -> None:
    """Output availability results as formatted table."""
    from ..cost.availability import AvailabilityStatus

    if not result.items:
        print("No components to check.")
        return

    # Status symbols
    status_symbols = {
        AvailabilityStatus.AVAILABLE: "\u2713",  # checkmark
        AvailabilityStatus.LOW_STOCK: "!",
        AvailabilityStatus.OUT_OF_STOCK: "\u2717",  # X
        AvailabilityStatus.DISCONTINUED: "D",
        AvailabilityStatus.UNKNOWN: "?",
        AvailabilityStatus.NO_LCSC: "-",
        AvailabilityStatus.NOT_FOUND: "?",
    }

    # Calculate column widths
    ref_width = max(len(item.reference) for item in result.items)
    ref_width = max(ref_width, 3)
    val_width = max(len(item.value) for item in result.items)
    val_width = max(val_width, 5)
    lcsc_width = max(len(item.lcsc_part or "-") for item in result.items)
    lcsc_width = max(lcsc_width, 4)

    # Header
    print(
        f"{'Ref':<{ref_width}}  {'Value':<{val_width}}  "
        f"{'LCSC':<{lcsc_width}}  {'Needed':>6}  {'Stock':>8}  Status"
    )
    print("-" * (ref_width + val_width + lcsc_width + 40))

    # Rows
    for item in result.items:
        symbol = status_symbols.get(item.status, "?")
        lcsc = item.lcsc_part or "-"
        stock_str = str(item.quantity_available) if item.quantity_available > 0 else "-"

        print(
            f"{item.reference:<{ref_width}}  {item.value:<{val_width}}  "
            f"{lcsc:<{lcsc_width}}  {item.quantity_needed:>6}  {stock_str:>8}  "
            f"{symbol} {item.status.value}"
        )

    # Summary
    summary = result.summary()
    print()
    print(f"Summary: {summary['total_items']} items checked")
    print(f"  Available:    {summary['available']}")
    print(f"  Low stock:    {summary['low_stock']}")
    print(f"  Out of stock: {summary['out_of_stock']}")
    print(f"  Missing/No LCSC: {summary['missing']}")

    if summary["total_cost"] is not None:
        print(f"  Estimated cost: ${summary['total_cost']:.2f}")

    if not summary["all_available"]:
        print("\nWarning: Some parts are not available or have insufficient stock.")


def output_availability_csv(result) -> None:
    """Output availability results as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(
        [
            "Reference",
            "Value",
            "Footprint",
            "MPN",
            "LCSC",
            "Quantity Needed",
            "Quantity Available",
            "Status",
            "In Stock",
            "Sufficient Stock",
            "Unit Price",
            "Extended Price",
            "Error",
        ]
    )

    for item in result.items:
        writer.writerow(
            [
                item.reference,
                item.value,
                item.footprint,
                item.mpn or "",
                item.lcsc_part or "",
                item.quantity_needed,
                item.quantity_available,
                item.status.value,
                item.in_stock,
                item.sufficient_stock,
                item.unit_price or "",
                item.extended_price or "",
                item.error or "",
            ]
        )

    print(output.getvalue(), end="")


def output_availability_json(result) -> None:
    """Output availability results as JSON."""
    print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    sys.exit(main())
