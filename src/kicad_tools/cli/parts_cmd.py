"""
CLI commands for parts lookup and management.

Usage:
    kct parts lookup C123456              - Look up LCSC part
    kct parts search "100nF 0402"         - Search for parts
    kct parts cache stats                 - Show cache statistics
    kct parts cache clear                 - Clear the cache
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
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
            "prices": [
                {"quantity": p.quantity, "unit_price": p.unit_price}
                for p in part.prices
            ],
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
        print(f"{'LCSC':<10} {'MFR Part':<25} {'Package':<12} {'Stock':>8} {'Price':>8} {'Type':<8}")
        print("-" * 80)
        for part in results.parts:
            price_str = f"${part.best_price:.4f}" if part.best_price else "N/A"
            type_str = "Basic" if part.is_basic else ("Pref" if part.is_preferred else "Ext")
            # Truncate long strings
            mfr = part.mfr_part[:24] if len(part.mfr_part) > 24 else part.mfr_part
            pkg = part.package[:11] if len(part.package) > 11 else part.package
            print(f"{part.lcsc_part:<10} {mfr:<25} {pkg:<12} {part.stock:>8,} {price_str:>8} {type_str:<8}")
    else:  # text
        print(f"Found {results.total_count} results for '{args.query}':")
        print()
        for part in results.parts:
            price_str = f"${part.best_price:.4f}" if part.best_price else "N/A"
            basic_str = " [Basic]" if part.is_basic else ""
            print(f"{part.lcsc_part}: {part.mfr_part} - {part.package} - {part.stock:,} in stock - {price_str}{basic_str}")

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
        if stats['oldest']:
            print(f"  Oldest entry: {stats['oldest']}")
        if stats['newest']:
            print(f"  Newest entry: {stats['newest']}")
        if stats['categories']:
            print("  Categories:")
            for cat, count in sorted(stats['categories'].items()):
                print(f"    {cat}: {count}")

    elif args.cache_action == "clear":
        count = cache.clear()
        print(f"Cleared {count} parts from cache")

    elif args.cache_action == "clear-expired":
        count = cache.clear_expired()
        print(f"Cleared {count} expired entries")

    return 0


if __name__ == "__main__":
    sys.exit(main())
