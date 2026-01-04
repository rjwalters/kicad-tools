"""Suggest command handlers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

__all__ = ["run_suggest_command"]


def run_suggest_command(args) -> int:
    """Handle suggest subcommands."""
    if not args.suggest_command:
        print("Usage: kicad-tools suggest <command> [options]")
        print("Commands: alternatives")
        return 1

    if args.suggest_command == "alternatives":
        return _run_alternatives_command(args)

    return 1


def _run_alternatives_command(args) -> int:
    """Suggest alternative parts for BOM items with availability issues."""
    from kicad_tools.cost import AlternativePartFinder
    from kicad_tools.parts import LCSCClient
    from kicad_tools.schema.bom import extract_bom

    input_path = Path(args.schematic)
    if not input_path.exists():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        return 1

    # Load BOM items
    try:
        if args.bom:
            # Load from CSV
            bom = _load_bom_from_csv(input_path)
        else:
            # Load from schematic
            bom = extract_bom(str(input_path))
    except Exception as e:
        print(f"Error loading BOM: {e}", file=sys.stderr)
        return 1

    if not bom.items:
        print("No BOM items found.", file=sys.stderr)
        return 1

    # Filter to items with LCSC numbers
    items_with_lcsc = [item for item in bom.items if item.lcsc and not item.dnp]

    if not items_with_lcsc:
        print("No BOM items with LCSC part numbers found.", file=sys.stderr)
        return 1

    # Create client and check availability
    try:
        client = LCSCClient(use_cache=not args.no_cache)
    except ImportError:
        print(
            "Error: The 'requests' library is required for this feature.",
            file=sys.stderr,
        )
        print("Install with: pip install kicad-tools[parts]", file=sys.stderr)
        return 1

    print(f"Checking availability for {len(items_with_lcsc)} parts...", file=sys.stderr)

    # Check availability
    availability = client.check_bom(items_with_lcsc)

    # Filter to problematic items (or all if --show-all)
    if args.show_all:
        target_items = items_with_lcsc
        target_avail = availability.items
    else:
        # Find items with issues
        target_items = []
        target_avail = []
        for item, avail in zip(items_with_lcsc, availability.items, strict=True):
            if avail.error or not avail.matched or not avail.in_stock or not avail.sufficient_stock:
                target_items.append(item)
                target_avail.append(avail)

    if not target_items:
        print("All parts are available. No alternatives needed.")
        return 0

    print(
        f"Finding alternatives for {len(target_items)} problematic parts...",
        file=sys.stderr,
    )

    # Find alternatives
    finder = AlternativePartFinder(client)
    suggestions = finder.suggest_for_bom(
        target_items, target_avail, max_results_per_item=args.max_alternatives
    )

    # Output results
    if args.suggest_format == "json":
        _print_json_suggestions(suggestions)
    else:
        _print_text_suggestions(suggestions, verbose=args.verbose)

    return 0


def _load_bom_from_csv(path: Path):
    """Load BOM from a CSV file."""
    import csv

    from kicad_tools.schema.bom import BOM, BOMItem

    items = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Try to find common column names
            reference = (
                row.get("Reference")
                or row.get("reference")
                or row.get("Designator")
                or row.get("designator")
                or ""
            )
            value = (
                row.get("Value")
                or row.get("value")
                or row.get("Comment")
                or row.get("comment")
                or ""
            )
            footprint = (
                row.get("Footprint")
                or row.get("footprint")
                or row.get("Package")
                or row.get("package")
                or ""
            )
            lcsc = (
                row.get("LCSC")
                or row.get("lcsc")
                or row.get("LCSC Part")
                or row.get("JLC Part")
                or ""
            )
            mpn = (
                row.get("MPN")
                or row.get("mpn")
                or row.get("Manufacturer Part")
                or row.get("Part Number")
                or ""
            )

            if reference:
                items.append(
                    BOMItem(
                        reference=reference,
                        value=value,
                        footprint=footprint,
                        lib_id="",
                        lcsc=lcsc,
                        mpn=mpn,
                    )
                )

    return BOM(items=items, source=str(path))


def _print_json_suggestions(suggestions: list) -> None:
    """Print suggestions as JSON."""
    output = {
        "suggestions": [s.to_dict() for s in suggestions],
        "summary": {
            "items_with_suggestions": len(suggestions),
            "total_alternatives": sum(len(s.alternatives) for s in suggestions),
        },
    }
    print(json.dumps(output, indent=2))


def _print_text_suggestions(suggestions: list, verbose: bool = False) -> None:
    """Print suggestions in human-readable format."""
    if not suggestions:
        print("\nNo alternative suggestions available.")
        return

    print("\nAlternative Part Suggestions:\n")

    for suggestion in suggestions:
        # Header
        status_str = _format_status(suggestion.status)
        print(f"  {suggestion.reference}: {suggestion.value} ({status_str})")

        if suggestion.original_lcsc:
            print(f"    Original: {suggestion.original_lcsc}")

        if not suggestion.alternatives:
            print("    No alternatives found.\n")
            continue

        print()

        for i, alt in enumerate(suggestion.alternatives, 1):
            # Determine if recommended
            recommended = "[RECOMMENDED]" if alt.recommendation else ""

            print(f"    {i}. {alt.alternative_mpn} ({alt.alternative_lcsc}) {recommended}")
            print(f"       Compatibility: {alt.compatibility}")

            # Price comparison
            if alt.alternative_price is not None:
                price_str = f"${alt.alternative_price:.4f}/unit"
                if alt.price_delta != 0:
                    delta_sign = "+" if alt.price_delta > 0 else ""
                    price_str += f" ({delta_sign}${alt.price_delta:.4f})"
                print(f"       Price: {price_str}")

            # Stock
            print(f"       Stock: {alt.stock_quantity:,}")

            # Basic part indicator
            if alt.is_basic:
                print("       JLCPCB Basic Part (no extended fee)")

            # Differences
            if alt.differences:
                print("       Differences:")
                for diff in alt.differences:
                    print(f"         - {diff}")

            # Warnings
            if alt.warnings:
                for warning in alt.warnings:
                    print(f"       Warning: {warning}")

            # Recommendation reason
            if verbose and alt.recommendation:
                print(f"       Note: {alt.recommendation}")

            print()

        print()


def _format_status(status: str) -> str:
    """Format status for display."""
    status_map = {
        "out_of_stock": "out of stock",
        "low_stock": "low stock",
        "not_found": "not found",
        "expensive": "expensive",
        "long_lead_time": "long lead time",
    }
    return status_map.get(status, status)
