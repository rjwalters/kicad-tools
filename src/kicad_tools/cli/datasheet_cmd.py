"""
CLI commands for datasheet search and download.

Provides commands:
    kct datasheet search <part>     - Search for datasheets
    kct datasheet download <part>   - Download a datasheet
    kct datasheet list              - List cached datasheets
    kct datasheet cache             - Cache management
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Main entry point for datasheet CLI."""
    parser = argparse.ArgumentParser(
        prog="kct datasheet",
        description="Search for and download component datasheets",
    )

    subparsers = parser.add_subparsers(dest="command", help="Datasheet commands")

    # search subcommand
    search_parser = subparsers.add_parser("search", help="Search for datasheets")
    search_parser.add_argument("part", help="Part number to search for")
    search_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format",
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum results to show (default: 10)",
    )

    # download subcommand
    download_parser = subparsers.add_parser("download", help="Download a datasheet")
    download_parser.add_argument("part", help="Part number to download")
    download_parser.add_argument(
        "-o",
        "--output",
        help="Output directory (default: cache)",
    )
    download_parser.add_argument(
        "--force",
        action="store_true",
        help="Force download even if cached",
    )

    # list subcommand
    list_parser = subparsers.add_parser("list", help="List cached datasheets")
    list_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format",
    )

    # cache subcommand
    cache_parser = subparsers.add_parser("cache", help="Cache management")
    cache_parser.add_argument(
        "action",
        nargs="?",
        choices=["stats", "clear", "clear-expired"],
        default="stats",
        help="Cache action (default: stats)",
    )
    cache_parser.add_argument(
        "--older-than",
        type=int,
        help="For clear: only clear entries older than N days",
    )

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    try:
        if args.command == "search":
            return _run_search(args)
        elif args.command == "download":
            return _run_download(args)
        elif args.command == "list":
            return _run_list(args)
        elif args.command == "cache":
            return _run_cache(args)
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Install with: pip install kicad-tools[parts]", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def _run_search(args) -> int:
    """Run datasheet search command."""
    from kicad_tools.datasheet import DatasheetManager

    manager = DatasheetManager()
    results = manager.search(args.part)

    if args.format == "json":
        output = {
            "query": results.query,
            "results": [
                {
                    "part_number": r.part_number,
                    "manufacturer": r.manufacturer,
                    "description": r.description,
                    "datasheet_url": r.datasheet_url,
                    "source": r.source,
                    "confidence": r.confidence,
                }
                for r in results.results[: args.limit]
            ],
            "errors": results.errors,
        }
        print(json.dumps(output, indent=2))
    else:
        if not results.has_results:
            print(f"No datasheets found for '{args.part}'")
            if results.errors:
                print("\nSource errors:")
                for source, error in results.errors.items():
                    print(f"  {source}: {error}")
            return 1

        print(f"Found {len(results)} datasheets for '{args.part}':\n")

        for i, result in enumerate(results.results[: args.limit], 1):
            print(f"{i}. {result.part_number} - {result.manufacturer}")
            print(
                f"   Description: {result.description[:60]}..."
                if len(result.description) > 60
                else f"   Description: {result.description}"
            )
            print(f"   Source: {result.source}")
            print(f"   URL: {result.datasheet_url}")
            print()

        if results.errors:
            print("Note: Some sources failed:")
            for source, error in results.errors.items():
                print(f"  {source}: {error}")

    return 0


def _run_download(args) -> int:
    """Run datasheet download command."""
    from kicad_tools.datasheet import DatasheetManager

    manager = DatasheetManager()

    output_dir = Path(args.output) if args.output else None

    try:
        datasheet = manager.download_by_part(
            args.part,
            output_dir=output_dir,
            force=args.force,
        )

        print(f"Downloaded datasheet for {args.part}")
        print(f"  Path: {datasheet.local_path}")
        print(f"  Size: {datasheet.file_size_mb:.2f} MB")
        print(f"  Source: {datasheet.source}")

        return 0

    except Exception as e:
        print(f"Failed to download datasheet: {e}", file=sys.stderr)
        return 1


def _run_list(args) -> int:
    """Run list cached datasheets command."""
    from kicad_tools.datasheet import DatasheetManager

    manager = DatasheetManager()
    datasheets = manager.list_cached()

    if args.format == "json":
        output = [
            {
                "part_number": ds.part_number,
                "manufacturer": ds.manufacturer,
                "local_path": str(ds.local_path),
                "source_url": ds.source_url,
                "source": ds.source,
                "downloaded_at": ds.downloaded_at.isoformat(),
                "file_size": ds.file_size,
            }
            for ds in datasheets
        ]
        print(json.dumps(output, indent=2))
    else:
        if not datasheets:
            print("No cached datasheets")
            return 0

        print(f"Cached datasheets ({len(datasheets)}):\n")

        # Calculate column widths
        max_part = max(len(ds.part_number) for ds in datasheets)
        max_mfr = max(len(ds.manufacturer) for ds in datasheets)

        for ds in datasheets:
            size_mb = ds.file_size_mb
            print(
                f"{ds.part_number:<{max_part}}  {ds.manufacturer:<{max_mfr}}  {size_mb:>6.2f} MB  {ds.source}"
            )

    return 0


def _run_cache(args) -> int:
    """Run cache management command."""
    from kicad_tools.datasheet import DatasheetManager

    manager = DatasheetManager()

    if args.action == "stats":
        stats = manager.cache_stats()

        print("Datasheet Cache Statistics")
        print("=" * 40)
        print(f"Cache directory: {stats['cache_dir']}")
        print(f"Total datasheets: {stats['total_count']}")
        print(f"Valid entries: {stats['valid_count']}")
        print(f"Expired entries: {stats['expired_count']}")
        print(f"Total size: {stats['total_size_mb']:.2f} MB")
        print(f"TTL: {stats['ttl_days']} days")

        if stats["sources"]:
            print("\nBy source:")
            for source, count in stats["sources"].items():
                print(f"  {source}: {count}")

    elif args.action == "clear":
        if args.older_than:
            count = manager.clear_cache(older_than_days=args.older_than)
            print(f"Cleared {count} entries older than {args.older_than} days")
        else:
            count = manager.clear_cache()
            print(f"Cleared {count} cached datasheets")

    elif args.action == "clear-expired":
        count = manager.cache.clear_expired()
        print(f"Cleared {count} expired entries")

    return 0


if __name__ == "__main__":
    sys.exit(main())
