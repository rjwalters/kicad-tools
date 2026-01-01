"""
CLI commands for datasheet search, download, and PDF parsing.

Provides commands:
    kct datasheet search <part>     - Search for datasheets
    kct datasheet download <part>   - Download a datasheet
    kct datasheet list              - List cached datasheets
    kct datasheet cache             - Cache management
    kct datasheet convert <pdf>     - Convert PDF to markdown
    kct datasheet extract-images <pdf> - Extract images from PDF
    kct datasheet extract-tables <pdf> - Extract tables from PDF
    kct datasheet extract-pins <pdf> - Extract pin definitions from PDF
    kct datasheet info <pdf>        - Show PDF information
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
        description="Datasheet search, download, and PDF parsing tools",
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

    # convert subcommand (PDF parsing)
    convert_parser = subparsers.add_parser("convert", help="Convert PDF to markdown")
    convert_parser.add_argument("pdf", help="Path to PDF file")
    convert_parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    convert_parser.add_argument(
        "--pages",
        help="Page range to convert (e.g., '1-10' or '1,2,5')",
    )

    # extract-images subcommand
    images_parser = subparsers.add_parser("extract-images", help="Extract images from PDF")
    images_parser.add_argument("pdf", help="Path to PDF file")
    images_parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Output directory for images",
    )
    images_parser.add_argument(
        "--pages",
        help="Page range to extract from (e.g., '1-10' or '1,2,5')",
    )
    images_parser.add_argument(
        "--min-size",
        type=int,
        default=100,
        help="Minimum image dimension in pixels (default: 100)",
    )
    images_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for image list",
    )

    # extract-tables subcommand
    tables_parser = subparsers.add_parser("extract-tables", help="Extract tables from PDF")
    tables_parser.add_argument("pdf", help="Path to PDF file")
    tables_parser.add_argument(
        "-o",
        "--output",
        help="Output directory for tables (one file per table)",
    )
    tables_parser.add_argument(
        "--pages",
        help="Page range to extract from (e.g., '1-10' or '1,2,5')",
    )
    tables_parser.add_argument(
        "--format",
        choices=["markdown", "csv", "json"],
        default="markdown",
        help="Output format for tables (default: markdown)",
    )

    # extract-pins subcommand
    pins_parser = subparsers.add_parser("extract-pins", help="Extract pin definitions from PDF")
    pins_parser.add_argument("pdf", help="Path to PDF file")
    pins_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: stdout)",
    )
    pins_parser.add_argument(
        "--pages",
        help="Page range to search (e.g., '1-10' or '1,2,5')",
    )
    pins_parser.add_argument(
        "--package",
        help="Package name to filter (e.g., 'LQFP48')",
    )
    pins_parser.add_argument(
        "--format",
        choices=["json", "csv", "table"],
        default="json",
        help="Output format (default: json)",
    )
    pins_parser.add_argument(
        "--list-packages",
        action="store_true",
        help="List available packages instead of extracting pins",
    )

    # info subcommand
    info_parser = subparsers.add_parser("info", help="Show PDF information")
    info_parser.add_argument("pdf", help="Path to PDF file")
    info_parser.add_argument("--format", choices=["text", "json"], default="text")

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
        elif args.command == "convert":
            return _convert(args)
        elif args.command == "extract-images":
            return _extract_images(args)
        elif args.command == "extract-tables":
            return _extract_tables(args)
        elif args.command == "extract-pins":
            return _extract_pins(args)
        elif args.command == "info":
            return _info(args)
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print(
            "Install with: pip install kicad-tools[parts] or kicad-tools[datasheet]",
            file=sys.stderr,
        )
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def _parse_pages(pages_str: str | None) -> list[int] | None:
    """Parse a page range string into a list of page numbers."""
    if not pages_str:
        return None

    pages: list[int] = []
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            pages.extend(range(int(start.strip()), int(end.strip()) + 1))
        else:
            pages.append(int(part))

    return sorted(set(pages))


def _run_search(args) -> int:
    """Run datasheet search command."""
    from kicad_tools.datasheet.manager import DatasheetManager

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
    from kicad_tools.datasheet.manager import DatasheetManager

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
    from kicad_tools.datasheet.manager import DatasheetManager

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
    from kicad_tools.datasheet.manager import DatasheetManager

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


def _convert(args) -> int:
    """Handle convert command."""
    try:
        from ..datasheet.parser import DatasheetParser
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Install with: pip install kicad-tools[datasheet]", file=sys.stderr)
        return 1

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: File not found: {pdf_path}", file=sys.stderr)
        return 1

    try:
        parser = DatasheetParser(pdf_path)
        pages = _parse_pages(args.pages)
        markdown = parser.to_markdown(pages)

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(markdown)
            print(f"Converted to: {output_path}")
        else:
            print(markdown)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def _extract_images(args) -> int:
    """Handle extract-images command."""
    try:
        from ..datasheet.parser import DatasheetParser
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Install with: pip install kicad-tools[datasheet]", file=sys.stderr)
        return 1

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: File not found: {pdf_path}", file=sys.stderr)
        return 1

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        parser = DatasheetParser(pdf_path)
        pages = _parse_pages(args.pages)
        min_size = args.min_size

        images = parser.extract_images(
            pages=pages,
            min_width=min_size,
            min_height=min_size,
        )

        if args.format == "json":
            data = [
                {
                    "page": img.page,
                    "index": img.index,
                    "width": img.width,
                    "height": img.height,
                    "format": img.format,
                    "classification": img.classification,
                    "caption": img.caption,
                    "filename": img.suggested_filename,
                    "size_kb": round(img.size_kb, 2),
                }
                for img in images
            ]
            print(json.dumps(data, indent=2))
        else:
            print(f"Extracted {len(images)} images from {pdf_path.name}:")

        for img in images:
            output_file = output_dir / img.suggested_filename
            img.save(output_file)
            if args.format == "text":
                print(
                    f"  Page {img.page}: {img.width}x{img.height} {img.format} "
                    f"({img.size_kb:.1f} KB) -> {output_file.name}"
                )

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def _extract_tables(args) -> int:
    """Handle extract-tables command."""
    try:
        from ..datasheet.parser import DatasheetParser
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Install with: pip install kicad-tools[datasheet]", file=sys.stderr)
        return 1

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: File not found: {pdf_path}", file=sys.stderr)
        return 1

    try:
        parser = DatasheetParser(pdf_path)
        pages = _parse_pages(args.pages)
        tables = parser.extract_tables(pages)

        if not tables:
            print("No tables found in document", file=sys.stderr)
            return 0

        if args.output:
            output_dir = Path(args.output)
            output_dir.mkdir(parents=True, exist_ok=True)

            for i, table in enumerate(tables):
                if args.format == "csv":
                    ext = "csv"
                    content = table.to_csv()
                elif args.format == "json":
                    ext = "json"
                    content = table.to_json()
                else:
                    ext = "md"
                    content = table.to_markdown()

                filename = f"table_{i + 1}_page_{table.page}.{ext}"
                output_file = output_dir / filename
                output_file.write_text(content)
                print(f"Saved: {output_file}")

        else:
            # Output to stdout
            if args.format == "json":
                data = [table.to_dict() for table in tables]
                print(json.dumps(data, indent=2))
            else:
                for i, table in enumerate(tables):
                    print(f"\n## Table {i + 1} (Page {table.page})\n")
                    if args.format == "csv":
                        print(table.to_csv())
                    else:
                        print(table.to_markdown())

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def _extract_pins(args) -> int:
    """Handle extract-pins command."""
    try:
        from ..datasheet.parser import DatasheetParser
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Install with: pip install kicad-tools[datasheet]", file=sys.stderr)
        return 1

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: File not found: {pdf_path}", file=sys.stderr)
        return 1

    try:
        parser = DatasheetParser(pdf_path)
        pages = _parse_pages(args.pages)

        # List packages mode
        if args.list_packages:
            packages = parser.list_packages(pages)
            if args.format == "json":
                print(json.dumps({"packages": packages}, indent=2))
            else:
                if packages:
                    print("Available packages:")
                    for pkg in packages:
                        print(f"  {pkg}")
                else:
                    print("No packages found in document")
            return 0

        # Extract pins
        pin_table = parser.extract_pins(pages=pages, package=args.package)

        if not pin_table.pins:
            print("No pin tables found in document", file=sys.stderr)
            return 0

        # Format output
        if args.format == "json":
            content = pin_table.to_json()
        elif args.format == "csv":
            content = pin_table.to_csv()
        else:  # table format
            content = pin_table.to_markdown()

        # Output
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content)
            print(f"Extracted {len(pin_table)} pins to: {output_path}")
        else:
            print(content)

        # Print summary for table/csv formats
        if args.format != "json" and not args.output:
            print(f"\n# Extracted {len(pin_table)} pins")
            if pin_table.package:
                print(f"# Package: {pin_table.package}")
            print(f"# Confidence: {pin_table.confidence:.2f}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def _info(args) -> int:
    """Handle info command."""
    try:
        from ..datasheet.parser import DatasheetParser
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Install with: pip install kicad-tools[datasheet]", file=sys.stderr)
        return 1

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: File not found: {pdf_path}", file=sys.stderr)
        return 1

    try:
        parser = DatasheetParser(pdf_path)

        # Count images and tables for quick summary
        images = parser.extract_images(min_width=100, min_height=100)
        tables = parser.extract_tables()

        if args.format == "json":
            data = {
                "path": str(pdf_path),
                "filename": pdf_path.name,
                "page_count": parser.page_count,
                "image_count": len(images),
                "table_count": len(tables),
            }
            print(json.dumps(data, indent=2))
        else:
            print(f"File:    {pdf_path.name}")
            print(f"Pages:   {parser.page_count}")
            print(f"Images:  {len(images)} (>= 100x100 px)")
            print(f"Tables:  {len(tables)}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
