"""
CLI commands for parts lookup and management.

Usage:
    kct parts lookup C123456              - Look up LCSC part
    kct parts search "100nF 0402"         - Search for parts
    kct parts availability design.kicad_sch - Check BOM availability
    kct parts suggest design.kicad_sch    - Suggest LCSC parts based on value/footprint
    kct parts suggest design.kicad_sch --apply - Apply suggestions to schematic
    kct parts import STM32F103C8T6 --library myproject.kicad_sym
    kct parts cache stats                 - Show cache statistics
    kct parts cache clear                 - Clear the cache
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Parts command entry point."""
    parser = argparse.ArgumentParser(
        prog="kct parts",
        description="LCSC/JLCPCB parts lookup and management",
    )

    subparsers = parser.add_subparsers(dest="action", help="Parts commands")

    # lookup subcommand
    lookup_parser = subparsers.add_parser("lookup", help="Look up part by LCSC number")
    lookup_parser.add_argument("part", help="LCSC part number (e.g., C123456)")
    lookup_parser.add_argument("--format", choices=["text", "json"], default="text")
    lookup_parser.add_argument("--no-cache", action="store_true", help="Bypass cache")

    # search subcommand
    search_parser = subparsers.add_parser("search", help="Search for parts")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--format", choices=["text", "json", "table"], default="table")
    search_parser.add_argument("--limit", type=int, default=20, help="Max results")
    search_parser.add_argument("--in-stock", action="store_true", help="Only in-stock parts")
    search_parser.add_argument("--basic", action="store_true", help="Only JLCPCB basic parts")

    # import subcommand
    import_parser = subparsers.add_parser("import", help="Import part from datasheet to library")
    import_parser.add_argument(
        "parts", nargs="+", help="Part number(s) to import (e.g., STM32F103C8T6)"
    )
    import_parser.add_argument(
        "--library", "-l", required=True, help="Output symbol library (.kicad_sym)"
    )
    import_parser.add_argument(
        "--symbol-lib",
        dest="symbol_library",
        help="Symbol library (alias for --library)",
    )
    import_parser.add_argument(
        "--footprint-lib", help="Project footprint library (.pretty directory)"
    )
    import_parser.add_argument("--package", "-p", help="Specific package variant (e.g., LQFP48)")
    import_parser.add_argument(
        "--layout",
        choices=["functional", "physical", "simple"],
        default="functional",
        help="Pin layout style (default: functional)",
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be imported without making changes",
    )
    import_parser.add_argument(
        "--interactive", "-i", action="store_true", help="Interactive mode with prompts"
    )
    import_parser.add_argument(
        "--overwrite", action="store_true", help="Overwrite existing symbols"
    )
    import_parser.add_argument("--format", choices=["text", "json"], default="text")

    # availability subcommand
    avail_parser = subparsers.add_parser("availability", help="Check BOM part availability on LCSC")
    avail_parser.add_argument("schematic", help="Path to .kicad_sch file or BOM CSV")
    avail_parser.add_argument(
        "--quantity", "-q", type=int, default=1, help="Number of boards to manufacture (default: 1)"
    )
    avail_parser.add_argument(
        "--format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format (default: table)",
    )
    avail_parser.add_argument(
        "--no-alternatives", action="store_true", help="Don't search for alternative parts"
    )
    avail_parser.add_argument(
        "--issues-only", action="store_true", help="Only show parts with availability issues"
    )

    # suggest subcommand
    suggest_parser = subparsers.add_parser(
        "suggest", help="Suggest LCSC part numbers based on value/footprint"
    )
    suggest_parser.add_argument("schematic", help="Path to .kicad_sch file")
    suggest_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    suggest_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply suggested LCSC numbers to the schematic",
    )
    suggest_parser.add_argument(
        "--all",
        action="store_true",
        help="Show all components, not just those missing LCSC numbers",
    )
    suggest_parser.add_argument(
        "--min-stock",
        type=int,
        default=100,
        help="Minimum stock level to consider (default: 100)",
    )
    suggest_parser.add_argument(
        "--no-prefer-basic",
        action="store_true",
        help="Don't prefer JLCPCB Basic parts",
    )

    # cache subcommand
    cache_parser = subparsers.add_parser("cache", help="Cache management")
    cache_subparsers = cache_parser.add_subparsers(dest="cache_action", help="Cache commands")

    cache_subparsers.add_parser("stats", help="Show cache statistics")
    cache_subparsers.add_parser("clear", help="Clear all cached parts")
    cache_subparsers.add_parser("clear-expired", help="Clear expired entries only")

    args = parser.parse_args(argv)

    if not args.action:
        parser.print_help()
        return 0

    if args.action == "lookup":
        return _lookup(args)
    elif args.action == "search":
        return _search(args)
    elif args.action == "import":
        return _import(args)
    elif args.action == "availability":
        return _availability(args)
    elif args.action == "suggest":
        return _suggest(args)
    elif args.action == "cache":
        return _cache(args)

    return 0


def _lookup(args) -> int:
    """Handle parts lookup command."""
    try:
        from ..parts import LCSCClient
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Install with: pip install kicad-tools[parts]", file=sys.stderr)
        return 1

    client = LCSCClient()
    part = client.lookup(args.part, bypass_cache=args.no_cache)

    if part is None:
        print(f"Part not found: {args.part}", file=sys.stderr)
        return 1

    if args.format == "json":
        data = {
            "lcsc_part": part.lcsc_part,
            "mfr_part": part.mfr_part,
            "manufacturer": part.manufacturer,
            "description": part.description,
            "category": part.category.value,
            "package": part.package,
            "package_type": part.package_type.value,
            "stock": part.stock,
            "is_basic": part.is_basic,
            "is_preferred": part.is_preferred,
            "prices": [{"quantity": p.quantity, "unit_price": p.unit_price} for p in part.prices],
            "datasheet_url": part.datasheet_url,
            "product_url": part.product_url,
        }
        print(json.dumps(data, indent=2))
    else:
        print(f"LCSC Part:    {part.lcsc_part}")
        print(f"MFR Part:     {part.mfr_part}")
        print(f"Manufacturer: {part.manufacturer}")
        print(f"Description:  {part.description}")
        print(f"Category:     {part.category.value}")
        print(f"Package:      {part.package} ({part.package_type.value})")
        print(f"Stock:        {part.stock:,}")
        if part.is_basic:
            print("Type:         JLCPCB Basic (no extra fee)")
        elif part.is_preferred:
            print("Type:         JLCPCB Preferred")
        else:
            print("Type:         Extended")
        if part.prices:
            print("Pricing:")
            for price in part.prices[:5]:  # Show first 5 price breaks
                print(f"  {price.quantity:>6} pcs: ${price.unit_price:.4f} ea")
        if part.datasheet_url:
            print(f"Datasheet:    {part.datasheet_url}")
        print(f"Product URL:  {part.product_url}")

    return 0


def _search(args) -> int:
    """Handle parts search command."""
    try:
        from ..parts import LCSCClient
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Install with: pip install kicad-tools[parts]", file=sys.stderr)
        return 1

    client = LCSCClient()
    results = client.search(
        args.query,
        page_size=args.limit,
        in_stock=args.in_stock,
        basic_only=args.basic,
    )

    if not results.parts:
        print(f"No parts found for: {args.query}", file=sys.stderr)
        return 1

    if args.format == "json":
        data = {
            "query": results.query,
            "total": results.total_count,
            "parts": [
                {
                    "lcsc_part": p.lcsc_part,
                    "mfr_part": p.mfr_part,
                    "description": p.description,
                    "package": p.package,
                    "stock": p.stock,
                    "is_basic": p.is_basic,
                    "best_price": p.best_price,
                }
                for p in results.parts
            ],
        }
        print(json.dumps(data, indent=2))
    elif args.format == "table":
        print(f"Found {results.total_count} results for '{args.query}':")
        print()
        # Header
        print(
            f"{'LCSC':<10} {'MFR Part':<25} {'Package':<12} {'Stock':>8} {'Price':>8} {'Type':<8}"
        )
        print("-" * 80)
        for part in results.parts:
            price_str = f"${part.best_price:.4f}" if part.best_price else "N/A"
            type_str = "Basic" if part.is_basic else ("Pref" if part.is_preferred else "Ext")
            # Truncate long strings
            mfr = part.mfr_part[:24] if len(part.mfr_part) > 24 else part.mfr_part
            pkg = part.package[:11] if len(part.package) > 11 else part.package
            print(
                f"{part.lcsc_part:<10} {mfr:<25} {pkg:<12} {part.stock:>8,} {price_str:>8} {type_str:<8}"
            )
    else:  # text
        print(f"Found {results.total_count} results for '{args.query}':")
        print()
        for part in results.parts:
            price_str = f"${part.best_price:.4f}" if part.best_price else "N/A"
            basic_str = " [Basic]" if part.is_basic else ""
            print(
                f"{part.lcsc_part}: {part.mfr_part} - {part.package} - {part.stock:,} in stock - {price_str}{basic_str}"
            )

    return 0


def _cache(args) -> int:
    """Handle cache commands."""
    from ..parts import PartsCache

    cache = PartsCache()

    if not args.cache_action or args.cache_action == "stats":
        stats = cache.stats()
        print("Parts Cache Statistics:")
        print(f"  Database:     {stats['db_path']}")
        print(f"  Total parts:  {stats['total']:,}")
        print(f"  Valid:        {stats['valid']:,}")
        print(f"  Expired:      {stats['expired']:,}")
        print(f"  TTL:          {stats['ttl_days']} days")
        if stats["oldest"]:
            print(f"  Oldest entry: {stats['oldest']}")
        if stats["newest"]:
            print(f"  Newest entry: {stats['newest']}")
        if stats["categories"]:
            print("  Categories:")
            for cat, count in sorted(stats["categories"].items()):
                print(f"    {cat}: {count}")

    elif args.cache_action == "clear":
        count = cache.clear()
        print(f"Cleared {count} parts from cache")

    elif args.cache_action == "clear-expired":
        count = cache.clear_expired()
        print(f"Cleared {count} expired entries")

    return 0


def _import(args) -> int:
    """Handle parts import command."""
    try:
        from ..parts import ImportOptions, LayoutStyle, PartImporter
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Install with: pip install kicad-tools[parts,datasheet]", file=sys.stderr)
        return 1

    # Determine symbol library path
    symbol_lib = Path(args.symbol_library or args.library)

    # Create import options
    layout_map = {
        "functional": LayoutStyle.FUNCTIONAL,
        "physical": LayoutStyle.PHYSICAL,
        "simple": LayoutStyle.SIMPLE,
    }
    options = ImportOptions(
        package=args.package,
        layout=layout_map.get(args.layout, LayoutStyle.FUNCTIONAL),
        dry_run=args.dry_run,
        overwrite=args.overwrite,
    )

    # Create importer
    importer = PartImporter(
        symbol_library=symbol_lib,
        footprint_library=Path(args.footprint_lib) if args.footprint_lib else None,
    )

    # Track results for summary
    success_count = 0
    failure_count = 0
    all_results = []

    def progress_callback(current: int, total: int, part_number: str):
        if args.format == "text":
            print(f"[{current}/{total}] Importing {part_number}...")

    try:
        # Import parts
        if len(args.parts) == 1:
            # Single part - show detailed progress
            def stage_callback(stage, msg):
                if args.format == "text":
                    print(f"  {stage.value}: {msg}")

            result = importer.import_part(args.parts[0], options, progress_callback=stage_callback)
            all_results.append(result)
        else:
            # Multiple parts
            all_results = importer.import_parts(
                args.parts, options, progress_callback=progress_callback
            )

        # Count results
        for result in all_results:
            if result.success:
                success_count += 1
            else:
                failure_count += 1

        # Output results
        if args.format == "json":
            data = {
                "success_count": success_count,
                "failure_count": failure_count,
                "results": [
                    {
                        "part_number": r.part_number,
                        "success": r.success,
                        "message": r.message,
                        "symbol_name": r.symbol_name,
                        "footprint_match": r.footprint_match,
                        "footprint_confidence": r.footprint_confidence,
                        "pin_count": r.pin_count,
                        "error_stage": r.error_stage.value if r.error_stage else None,
                        "error_details": r.error_details,
                        "warnings": r.warnings,
                    }
                    for r in all_results
                ],
            }
            print(json.dumps(data, indent=2))
        else:
            # Text format
            print()
            if args.dry_run:
                print("DRY RUN - No changes were made")
                print()

            for result in all_results:
                status = "✓" if result.success else "✗"
                print(f"{status} {result.part_number}: {result.message}")

                if result.success:
                    if result.symbol_name:
                        print(f"    Symbol: {result.symbol_name}")
                    if result.footprint_match:
                        conf = (
                            f"{result.footprint_confidence:.0%}"
                            if result.footprint_confidence
                            else "?"
                        )
                        print(f"    Footprint: {result.footprint_match} ({conf} match)")
                    if result.pin_count:
                        print(f"    Pins: {result.pin_count}")
                    if result.datasheet_path:
                        print(f"    Datasheet: {result.datasheet_path}")

                if result.warnings:
                    for warning in result.warnings:
                        print(f"    ⚠ {warning}")

                if result.error_details:
                    print(f"    Error: {result.error_details}")

            # Summary
            print()
            if len(args.parts) > 1:
                print(f"Summary: {success_count} succeeded, {failure_count} failed")

            if not args.dry_run and success_count > 0:
                print(f"Library updated: {symbol_lib}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        importer.close()

    return 0 if failure_count == 0 else 1


def _availability(args) -> int:
    """Handle parts availability command."""
    try:
        from ..cost.availability import (
            AvailabilityStatus,
            LCSCAvailabilityChecker,
        )
        from ..schema.bom import extract_bom
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Install with: pip install kicad-tools[parts]", file=sys.stderr)
        return 1

    schematic_path = Path(args.schematic)
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    # Extract BOM
    try:
        bom = extract_bom(str(schematic_path))
    except Exception as e:
        print(f"Error loading schematic: {e}", file=sys.stderr)
        return 1

    if not bom.items:
        print("No components found in schematic", file=sys.stderr)
        return 1

    # Check availability
    checker = LCSCAvailabilityChecker(
        find_alternatives=not args.no_alternatives,
    )

    try:
        result = checker.check_bom(bom, quantity=args.quantity)
    finally:
        checker.close()

    # Filter if requested
    items = result.items
    if args.issues_only:
        items = [item for item in items if item.status != AvailabilityStatus.AVAILABLE]

    # Output
    if args.format == "json":
        _availability_json(result, items)
    elif args.format == "summary":
        _availability_summary(result, schematic_path)
    else:
        _availability_table(result, items, schematic_path, args.quantity)

    # Return error code if issues found
    if result.out_of_stock or result.missing:
        return 1
    return 0


def _availability_json(result, items) -> None:
    """Output availability as JSON."""
    output = result.to_dict()
    output["items"] = [item.to_dict() for item in items]
    print(json.dumps(output, indent=2))


def _availability_summary(result, schematic_path: Path) -> None:
    """Output availability summary."""
    summary = result.summary()

    print(f"Part Availability Check: {schematic_path.name}")
    print("=" * 50)
    print(f"Boards:        {summary['quantity_multiplier']}")
    print(f"Total parts:   {summary['total_items']}")
    print()
    print(f"  ✓ Available:    {summary['available']}")
    print(f"  ⚠ Low stock:    {summary['low_stock']}")
    print(f"  ✗ Out of stock: {summary['out_of_stock']}")
    print(f"  ? Missing:      {summary['missing']}")
    print()

    if summary["total_cost"] is not None:
        print(f"Est. cost:     ${summary['total_cost']:.2f}")

    if summary["all_available"]:
        print("\n✓ All parts available")
    else:
        print("\n✗ Some parts have availability issues")


def _suggest(args) -> int:
    """Handle parts suggest command."""
    try:
        from ..cost.suggest import PartSuggester
        from ..schema.bom import extract_bom
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Install with: pip install kicad-tools[parts]", file=sys.stderr)
        return 1

    schematic_path = Path(args.schematic)
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    # Extract BOM
    try:
        bom = extract_bom(str(schematic_path))
    except Exception as e:
        print(f"Error loading schematic: {e}", file=sys.stderr)
        return 1

    if not bom.items:
        print("No components found in schematic", file=sys.stderr)
        return 1

    # Create suggester
    suggester = PartSuggester(
        prefer_basic=not args.no_prefer_basic,
        min_stock=args.min_stock,
    )

    try:
        result = suggester.suggest_for_bom(bom)
    finally:
        suggester.close()

    # Filter suggestions
    suggestions = result.suggestions
    if not args.all:
        suggestions = [s for s in suggestions if s.needs_lcsc]

    if not suggestions:
        if args.all:
            print("No components found in schematic")
        else:
            print("All components already have LCSC part numbers")
        return 0

    # Output results
    if args.format == "json":
        _suggest_json(result, suggestions)
    else:
        _suggest_table(result, suggestions, schematic_path)

    # Apply if requested
    if args.apply:
        return _apply_suggestions(schematic_path, result)

    return 0


def _suggest_json(result, suggestions) -> None:
    """Output suggestions as JSON."""
    output = {
        "summary": {
            "total_components": result.total_components,
            "missing_lcsc": result.missing_lcsc,
            "found_suggestions": result.found_suggestions,
            "no_suggestions": result.no_suggestions,
        },
        "suggestions": [],
    }

    for s in suggestions:
        item = {
            "reference": s.reference,
            "value": s.value,
            "footprint": s.footprint,
            "package": s.package,
            "existing_lcsc": s.existing_lcsc,
            "search_query": s.search_query,
            "error": s.error,
            "best_suggestion": None,
            "all_suggestions": [],
        }

        if s.best_suggestion:
            item["best_suggestion"] = {
                "lcsc_part": s.best_suggestion.lcsc_part,
                "mfr_part": s.best_suggestion.mfr_part,
                "description": s.best_suggestion.description,
                "package": s.best_suggestion.package,
                "stock": s.best_suggestion.stock,
                "is_basic": s.best_suggestion.is_basic,
                "unit_price": s.best_suggestion.unit_price,
                "confidence": s.best_suggestion.confidence,
            }

        for sug in s.suggestions:
            item["all_suggestions"].append(
                {
                    "lcsc_part": sug.lcsc_part,
                    "mfr_part": sug.mfr_part,
                    "description": sug.description,
                    "package": sug.package,
                    "stock": sug.stock,
                    "is_basic": sug.is_basic,
                    "unit_price": sug.unit_price,
                    "confidence": sug.confidence,
                }
            )

        output["suggestions"].append(item)

    print(json.dumps(output, indent=2))


def _suggest_table(result, suggestions, schematic_path: Path) -> None:
    """Output suggestions as formatted table."""
    print()
    print("=" * 90)
    print("LCSC PART SUGGESTIONS")
    print("=" * 90)
    print(f"Schematic: {schematic_path.name}")
    print(f"Components analyzed: {result.total_components}")
    print(f"Missing LCSC numbers: {result.missing_lcsc}")
    print()

    # Table header
    print(
        f"{'Ref':<6} {'Value':<12} {'Footprint':<18} {'Suggested LCSC':<12} "
        f"{'Stock':>8} {'Type':<6}"
    )
    print("-" * 90)

    # Group by status
    found = [s for s in suggestions if s.has_suggestion]
    not_found = [s for s in suggestions if not s.has_suggestion and s.needs_lcsc]
    has_lcsc = [s for s in suggestions if not s.needs_lcsc]

    # Show found suggestions
    for s in found:
        best = s.best_suggestion
        # Truncate long values
        value = s.value[:11] if len(s.value) > 11 else s.value
        fp = s.package[:17] if len(s.package) > 17 else s.package

        stock_str = _format_stock(best.stock)
        print(
            f"{s.reference:<6} {value:<12} {fp:<18} {best.lcsc_part:<12} "
            f"{stock_str:>8} {best.type_str:<6}"
        )

    # Show not found
    if not_found:
        print()
        print("? NO SUGGESTIONS FOUND:")
        for s in not_found:
            value = s.value[:11] if len(s.value) > 11 else s.value
            error = s.error or "No matching parts"
            print(f"  {s.reference}: {value} - {error}")

    # Show already has LCSC (if --all)
    if has_lcsc:
        print()
        print(f"✓ ALREADY HAS LCSC ({len(has_lcsc)} components)")

    # Summary
    print()
    print("-" * 90)
    print(f"Summary: {result.found_suggestions}/{result.missing_lcsc} suggestions found")

    if result.found_suggestions > 0:
        print()
        print("Run with --apply to update schematic with suggested LCSC numbers")


def _format_stock(stock: int) -> str:
    """Format stock number for display."""
    if stock >= 1_000_000:
        return f"{stock / 1_000_000:.1f}M"
    elif stock >= 1000:
        return f"{stock / 1000:.1f}k"
    else:
        return str(stock)


def _apply_suggestions(schematic_path: Path, result) -> int:
    """Apply suggested LCSC numbers to the schematic."""
    from ..schema.hierarchy import build_hierarchy
    from ..schema.schematic import Schematic

    # Build mapping of reference -> suggested LCSC
    lcsc_map = {}
    for s in result.suggestions:
        if s.has_suggestion and s.needs_lcsc:
            lcsc_map[s.reference] = s.best_suggestion.lcsc_part

    if not lcsc_map:
        print("No suggestions to apply")
        return 0

    # Load hierarchy and update all sheets
    hierarchy = build_hierarchy(str(schematic_path))
    updated_count = 0

    for node in hierarchy.all_nodes():
        try:
            sch = Schematic.load(node.path)
            sheet_updated = False

            for symbol in sch.sexp.find_all("symbol"):
                # Get reference
                ref = None
                for prop in symbol.find_all("property"):
                    if prop.get_string(0) == "Reference":
                        ref = prop.get_string(1)
                        break

                if not ref or ref not in lcsc_map:
                    continue

                # Check if already has LCSC property
                has_lcsc = False
                for prop in symbol.find_all("property"):
                    name = prop.get_string(0)
                    if name and name.lower() in ("lcsc", "lcsc_pn", "lcsc part", "jlc", "jlcpcb"):
                        has_lcsc = True
                        break

                if has_lcsc:
                    continue

                # Add LCSC property
                lcsc_value = lcsc_map[ref]
                _add_lcsc_property(symbol, lcsc_value)
                updated_count += 1
                sheet_updated = True

            if sheet_updated:
                sch.save()

        except Exception as e:
            print(f"Warning: Failed to update {node.path}: {e}", file=sys.stderr)

    print(f"\n✓ Applied {updated_count} LCSC part numbers to schematic")
    return 0


def _add_lcsc_property(symbol, lcsc_value: str) -> None:
    """Add LCSC property to a symbol S-expression."""
    from kicad_tools.sexp import SExp

    # Find position from existing properties
    pos = (0.0, 0.0)
    for prop in symbol.find_all("property"):
        if at := prop.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            break

    # Create LCSC property
    prop = SExp("property")
    prop.add("LCSC")
    prop.add(lcsc_value)

    # Add position (offset from reference position)
    at = SExp("at")
    at.add(pos[0])
    at.add(pos[1] + 2.54)  # Offset below
    at.add(0)
    prop.add(at)

    # Add effects (hidden by default)
    effects = SExp("effects")
    font = SExp("font")
    size = SExp("size")
    size.add(1.27)
    size.add(1.27)
    font.add(size)
    effects.add(font)
    effects.add(SExp("hide"))
    prop.add(effects)

    symbol.add(prop)


def _availability_table(result, items, schematic_path: Path, quantity: int) -> None:
    """Output availability as formatted table."""
    from ..cost.availability import AvailabilityStatus

    summary = result.summary()

    print()
    print("=" * 70)
    print("PART AVAILABILITY CHECK (LCSC)")
    print("=" * 70)
    print(f"Schematic: {schematic_path.name}")
    print(f"Quantity:  {quantity} board(s)")
    print()

    # Group by status
    available = [i for i in items if i.status == AvailabilityStatus.AVAILABLE]
    low_stock = [i for i in items if i.status == AvailabilityStatus.LOW_STOCK]
    out_of_stock = [i for i in items if i.status == AvailabilityStatus.OUT_OF_STOCK]
    missing = [
        i for i in items if i.status in (AvailabilityStatus.NO_LCSC, AvailabilityStatus.NOT_FOUND)
    ]

    # Available parts (collapsed)
    if available:
        print(f"✓ AVAILABLE ({len(available)} parts)")
        print()

    # Low stock parts
    if low_stock:
        print(f"⚠ LOW STOCK ({len(low_stock)} parts):")
        for item in low_stock:
            print(f"  {item.reference}: {item.value} ({item.lcsc_part})")
            print(f"    Stock: {item.quantity_available:,} (need {item.quantity_needed:,})")
            if item.alternatives:
                print("    Alternatives:")
                for alt in item.alternatives:
                    price_info = ""
                    if alt.price_diff is not None:
                        if alt.price_diff > 0:
                            price_info = f", +${alt.price_diff:.4f}"
                        elif alt.price_diff < 0:
                            price_info = f", -${abs(alt.price_diff):.4f}"
                    basic = " [Basic]" if alt.is_basic else ""
                    print(f"      • {alt.lcsc_part}: {alt.stock:,} in stock{price_info}{basic}")
        print()

    # Out of stock parts
    if out_of_stock:
        print(f"✗ OUT OF STOCK ({len(out_of_stock)} parts):")
        for item in out_of_stock:
            print(f"  {item.reference}: {item.value} ({item.lcsc_part})")
            if item.lead_time_days:
                print(f"    Lead time: {item.lead_time_days} days")
            if item.alternatives:
                print("    Alternatives:")
                for alt in item.alternatives:
                    price_info = ""
                    if alt.price_diff is not None:
                        if alt.price_diff > 0:
                            price_info = f", +${alt.price_diff:.4f}"
                        elif alt.price_diff < 0:
                            price_info = f", -${abs(alt.price_diff):.4f}"
                    basic = " [Basic]" if alt.is_basic else ""
                    print(f"      • {alt.lcsc_part}: {alt.stock:,} in stock{price_info}{basic}")
            else:
                print("    No alternatives found")
        print()

    # Missing parts (no LCSC number or not found)
    if missing:
        print(f"? MISSING ({len(missing)} parts):")
        for item in missing:
            lcsc_info = f" ({item.lcsc_part})" if item.lcsc_part else ""
            print(f"  {item.reference}: {item.value}{lcsc_info}")
            if item.error:
                print(f"    {item.error}")
        print()

    # Summary
    print("-" * 70)
    print("Summary:")
    print(f"  • {summary['available']}/{summary['total_items']} parts available")
    if summary["low_stock"] > 0:
        print(f"  • {summary['low_stock']} parts low stock")
    if summary["out_of_stock"] > 0:
        print(f"  • {summary['out_of_stock']} parts out of stock")
    if summary["missing"] > 0:
        print(f"  • {summary['missing']} parts missing LCSC number or not found")

    if summary["total_cost"] is not None:
        print(f"  • Estimated component cost: ${summary['total_cost']:.2f}")

    print()
    if summary["all_available"]:
        print("✓ All parts available for ordering")
    elif summary["out_of_stock"] > 0:
        print("✗ Some parts out of stock - check alternatives above")
    else:
        print("⚠ Low stock on some parts - order soon")


if __name__ == "__main__":
    sys.exit(main())
