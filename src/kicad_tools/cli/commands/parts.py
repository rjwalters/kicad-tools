"""Parts (LCSC) command handlers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

__all__ = ["run_parts_command"]


def run_parts_command(args) -> int:
    """Handle parts subcommands."""
    if not args.parts_command:
        print("Usage: kicad-tools parts <command> [options]")
        print("Commands: lookup, search, availability, cache, suggest")
        return 1

    from ..parts_cmd import main as parts_main

    if args.parts_command == "lookup":
        sub_argv = ["lookup", args.part]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.no_cache:
            sub_argv.append("--no-cache")
        return parts_main(sub_argv) or 0

    elif args.parts_command == "search":
        sub_argv = ["search", args.query]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.limit != 20:
            sub_argv.extend(["--limit", str(args.limit)])
        if args.in_stock:
            sub_argv.append("--in-stock")
        if args.basic:
            sub_argv.append("--basic")
        return parts_main(sub_argv) or 0

    elif args.parts_command == "availability":
        sub_argv = ["availability", args.schematic]
        if args.quantity != 1:
            sub_argv.extend(["--quantity", str(args.quantity)])
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.no_alternatives:
            sub_argv.append("--no-alternatives")
        if args.issues_only:
            sub_argv.append("--issues-only")
        return parts_main(sub_argv) or 0

    elif args.parts_command == "cache":
        sub_argv = ["cache", args.cache_action]
        return parts_main(sub_argv) or 0

    elif args.parts_command == "suggest":
        return _run_suggest_command(args)

    return 1


def _run_suggest_command(args) -> int:
    """Suggest LCSC part numbers for components without them."""
    from kicad_tools.cost.suggest import PartSuggester
    from kicad_tools.schema.bom import extract_bom

    input_path = Path(args.schematic)
    if not input_path.exists():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        return 1

    # Load BOM from schematic
    try:
        bom = extract_bom(str(input_path))
    except Exception as e:
        print(f"Error loading schematic: {e}", file=sys.stderr)
        return 1

    if not bom.items:
        print("No components found in schematic.", file=sys.stderr)
        return 1

    # Filter to non-DNP items
    active_items = [item for item in bom.items if not item.dnp]

    if not active_items:
        print("No active (non-DNP) components found.", file=sys.stderr)
        return 1

    # Count items needing LCSC numbers
    missing_lcsc = [item for item in active_items if not item.lcsc]

    if not missing_lcsc and not args.show_all:
        print("All components already have LCSC part numbers.")
        return 0

    print(
        f"Analyzing {len(active_items)} components ({len(missing_lcsc)} missing LCSC numbers)...",
        file=sys.stderr,
    )

    # Create suggester with options
    try:
        with PartSuggester(
            prefer_basic=not args.no_basic_preference,
            min_stock=args.min_stock,
            max_suggestions=args.max_suggestions,
        ) as suggester:
            result = suggester.suggest_for_bom(bom)
    except ImportError:
        print(
            "Error: The 'requests' library is required for this feature.",
            file=sys.stderr,
        )
        print("Install with: pip install kicad-tools[parts]", file=sys.stderr)
        return 1

    # Filter results if not showing all
    if not args.show_all:
        result.suggestions = [s for s in result.suggestions if s.needs_lcsc]

    if not result.suggestions:
        print("No components need suggestions.")
        return 0

    # Output results
    if args.format == "json":
        _print_json_result(result)
    else:
        _print_table_result(result)

    return 0


def _print_json_result(result) -> None:
    """Print suggestions as JSON."""
    output = {
        "summary": {
            "total_components": result.total_components,
            "missing_lcsc": result.missing_lcsc,
            "found_suggestions": result.found_suggestions,
            "no_suggestions": result.no_suggestions,
        },
        "suggestions": [],
    }

    for suggestion in result.suggestions:
        item = {
            "reference": suggestion.reference,
            "value": suggestion.value,
            "footprint": suggestion.footprint,
            "package": suggestion.package,
            "existing_lcsc": suggestion.existing_lcsc,
            "search_query": suggestion.search_query,
            "error": suggestion.error,
            "suggestions": [],
        }

        for s in suggestion.suggestions:
            item["suggestions"].append(
                {
                    "lcsc_part": s.lcsc_part,
                    "mfr_part": s.mfr_part,
                    "description": s.description,
                    "package": s.package,
                    "stock": s.stock,
                    "is_basic": s.is_basic,
                    "is_preferred": s.is_preferred,
                    "unit_price": s.unit_price,
                    "confidence": s.confidence,
                }
            )

        output["suggestions"].append(item)

    print(json.dumps(output, indent=2))


def _print_table_result(result) -> None:
    """Print suggestions in table format."""
    print()
    print(
        f"{'Component':<12} {'Value':<15} {'Footprint':<15} {'Suggested LCSC':<12} {'Stock':<10} {'Type':<6}"
    )
    print("-" * 82)

    for suggestion in result.suggestions:
        if suggestion.has_suggestion:
            best = suggestion.best_suggestion
            print(
                f"{suggestion.reference:<12} "
                f"{suggestion.value[:14]:<15} "
                f"{suggestion.package or '-':<15} "
                f"{best.lcsc_part:<12} "
                f"{best.stock:>9,} "
                f"{best.type_str:<6}"
            )

            # Print additional suggestions if any
            for alt in suggestion.suggestions[1:]:
                print(
                    f"{'':12} {'':15} {'':15} {alt.lcsc_part:<12} {alt.stock:>9,} {alt.type_str:<6}"
                )
        else:
            error_msg = suggestion.error or "No matches found"
            print(
                f"{suggestion.reference:<12} "
                f"{suggestion.value[:14]:<15} "
                f"{suggestion.package or '-':<15} "
                f"{'(none)':<12} "
                f"{'':<10} "
                f"{error_msg}"
            )

    print()
    print(
        f"Summary: {result.found_suggestions}/{result.missing_lcsc} components with suggestions found"
    )
    if result.no_suggestions > 0:
        print(f"         {result.no_suggestions} components could not be matched")
