"""
Command-line interface tools for kicad-tools.

Provides CLI commands for common KiCad operations via the `kicad-tools` or `kct` command:

    kicad-tools symbols <schematic>  - List and query symbols
    kicad-tools nets <schematic>     - Trace and analyze nets
    kicad-tools erc <report>         - Parse ERC report
    kicad-tools drc <report>         - Parse DRC report
    kicad-tools bom <schematic>      - Generate bill of materials

Examples:
    kct symbols design.kicad_sch --filter "U*"
    kct nets design.kicad_sch --net VCC
    kct erc design-erc.json --errors-only
    kct drc design-drc.rpt --mfr jlcpcb
    kct bom design.kicad_sch --format csv
"""

import argparse
import sys
from typing import List, Optional

__all__ = [
    "main",
    "symbols_main",
    "nets_main",
    "erc_main",
    "drc_main",
    "bom_main",
]


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point for kicad-tools CLI."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools",
        description="KiCad automation toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--version", action="version", version="kicad-tools 0.1.0"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Symbols subcommand
    symbols_parser = subparsers.add_parser(
        "symbols", help="List symbols in a schematic"
    )
    symbols_parser.add_argument("schematic", help="Path to .kicad_sch file")
    symbols_parser.add_argument(
        "--format", choices=["table", "json", "csv"], default="table"
    )
    symbols_parser.add_argument("--filter", dest="pattern", help="Filter by reference")
    symbols_parser.add_argument("--lib", dest="lib_id", help="Filter by library ID")
    symbols_parser.add_argument("-v", "--verbose", action="store_true")

    # Nets subcommand
    nets_parser = subparsers.add_parser(
        "nets", help="Trace nets in a schematic"
    )
    nets_parser.add_argument("schematic", help="Path to .kicad_sch file")
    nets_parser.add_argument("--format", choices=["table", "json"], default="table")
    nets_parser.add_argument("--net", help="Trace a specific net by label")
    nets_parser.add_argument("--stats", action="store_true", help="Show statistics only")

    # ERC subcommand
    erc_parser = subparsers.add_parser("erc", help="Parse ERC report")
    erc_parser.add_argument("report", help="Path to ERC report (.json or .rpt)")
    erc_parser.add_argument(
        "--format", choices=["table", "json", "summary"], default="table"
    )
    erc_parser.add_argument("--errors-only", action="store_true")
    erc_parser.add_argument("--type", dest="filter_type", help="Filter by violation type")
    erc_parser.add_argument("--sheet", help="Filter by sheet path")

    # DRC subcommand
    drc_parser = subparsers.add_parser("drc", help="Parse DRC report")
    drc_parser.add_argument("report", help="Path to DRC report (.json or .rpt)")
    drc_parser.add_argument(
        "--format", choices=["table", "json", "summary"], default="table"
    )
    drc_parser.add_argument("--errors-only", action="store_true")
    drc_parser.add_argument("--type", dest="filter_type", help="Filter by violation type")
    drc_parser.add_argument("--net", help="Filter by net name")
    drc_parser.add_argument(
        "--mfr",
        choices=["jlcpcb", "pcbway", "oshpark", "seeed"],
        help="Check against manufacturer design rules",
    )
    drc_parser.add_argument(
        "--layers", type=int, default=2, help="Number of copper layers"
    )

    # BOM subcommand
    bom_parser = subparsers.add_parser("bom", help="Generate bill of materials")
    bom_parser.add_argument("schematic", help="Path to .kicad_sch file")
    bom_parser.add_argument(
        "--format", choices=["table", "csv", "json"], default="table"
    )
    bom_parser.add_argument("--group", action="store_true", help="Group identical components")
    bom_parser.add_argument(
        "--exclude", action="append", default=[], help="Exclude references matching pattern"
    )
    bom_parser.add_argument("--include-dnp", action="store_true")
    bom_parser.add_argument(
        "--sort", choices=["reference", "value", "footprint"], default="reference"
    )

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "symbols":
        from .symbols import main as symbols_cmd
        # Convert args back to argv for the subcommand
        sub_argv = [args.schematic]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.pattern:
            sub_argv.extend(["--filter", args.pattern])
        if args.lib_id:
            sub_argv.extend(["--lib", args.lib_id])
        if args.verbose:
            sub_argv.append("--verbose")
        return symbols_cmd(sub_argv)

    elif args.command == "nets":
        from .nets import main as nets_cmd
        sub_argv = [args.schematic]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.net:
            sub_argv.extend(["--net", args.net])
        if args.stats:
            sub_argv.append("--stats")
        return nets_cmd(sub_argv)

    elif args.command == "erc":
        from .erc_cmd import main as erc_cmd
        sub_argv = [args.report]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.errors_only:
            sub_argv.append("--errors-only")
        if args.filter_type:
            sub_argv.extend(["--type", args.filter_type])
        if args.sheet:
            sub_argv.extend(["--sheet", args.sheet])
        return erc_cmd(sub_argv)

    elif args.command == "drc":
        from .drc_cmd import main as drc_cmd
        sub_argv = [args.report]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.errors_only:
            sub_argv.append("--errors-only")
        if args.filter_type:
            sub_argv.extend(["--type", args.filter_type])
        if args.net:
            sub_argv.extend(["--net", args.net])
        if args.mfr:
            sub_argv.extend(["--mfr", args.mfr])
        if args.layers != 2:
            sub_argv.extend(["--layers", str(args.layers)])
        return drc_cmd(sub_argv)

    elif args.command == "bom":
        from .bom_cmd import main as bom_cmd
        sub_argv = [args.schematic]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.group:
            sub_argv.append("--group")
        for pattern in args.exclude:
            sub_argv.extend(["--exclude", pattern])
        if args.include_dnp:
            sub_argv.append("--include-dnp")
        if args.sort != "reference":
            sub_argv.extend(["--sort", args.sort])
        return bom_cmd(sub_argv)

    return 0


def symbols_main() -> int:
    """Standalone entry point for kicad-symbols command."""
    from .symbols import main
    return main()


def nets_main() -> int:
    """Standalone entry point for kicad-nets command."""
    from .nets import main
    return main()


def erc_main() -> int:
    """Standalone entry point for kicad-erc command."""
    from .erc_cmd import main
    return main()


def drc_main() -> int:
    """Standalone entry point for kicad-drc command."""
    from .drc_cmd import main
    return main()


def bom_main() -> int:
    """Standalone entry point for kicad-bom command."""
    from .bom_cmd import main
    return main()


if __name__ == "__main__":
    sys.exit(main())
