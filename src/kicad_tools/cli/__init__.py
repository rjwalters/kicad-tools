"""
Command-line interface tools for kicad-tools.

Provides CLI commands for common KiCad operations via the `kicad-tools` or `kct` command:

    kicad-tools symbols <schematic>    - List and query symbols
    kicad-tools nets <schematic>       - Trace and analyze nets
    kicad-tools erc <report>           - Parse ERC report
    kicad-tools drc <report>           - Parse DRC report
    kicad-tools bom <schematic>        - Generate bill of materials
    kicad-tools sch <command> <file>   - Schematic analysis tools
    kicad-tools pcb <command> <file>   - PCB query tools
    kicad-tools lib <command> <file>   - Symbol library tools
    kicad-tools mfr <command>          - Manufacturer tools
    kicad-tools parts <command>        - LCSC parts lookup and search
    kicad-tools route <pcb>            - Autoroute a PCB
    kicad-tools placement <command>    - Detect and fix placement conflicts
    kicad-tools optimize-traces <pcb>  - Optimize PCB traces
    kicad-tools validate-footprints    - Validate footprint pad spacing
    kicad-tools fix-footprints <pcb>   - Fix footprint pad spacing issues

Examples:
    kct symbols design.kicad_sch --filter "U*"
    kct nets design.kicad_sch --net VCC
    kct erc design-erc.json --errors-only
    kct drc design-drc.rpt --mfr jlcpcb
    kct bom design.kicad_sch --format csv
    kct sch summary design.kicad_sch
    kct pcb summary board.kicad_pcb
    kct mfr compare
    kct parts lookup C123456
    kct parts search "100nF 0402" --in-stock
    kct route board.kicad_pcb --strategy negotiated
    kct validate-footprints board.kicad_pcb --min-pad-gap 0.15
    kct fix-footprints board.kicad_pcb --min-pad-gap 0.2 --dry-run
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from kicad_tools import __version__

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
    parser.add_argument("--version", action="version", version=f"kicad-tools {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Symbols subcommand
    symbols_parser = subparsers.add_parser("symbols", help="List symbols in a schematic")
    symbols_parser.add_argument("schematic", help="Path to .kicad_sch file")
    symbols_parser.add_argument("--format", choices=["table", "json", "csv"], default="table")
    symbols_parser.add_argument("--filter", dest="pattern", help="Filter by reference")
    symbols_parser.add_argument("--lib", dest="lib_id", help="Filter by library ID")
    symbols_parser.add_argument("-v", "--verbose", action="store_true")

    # Nets subcommand
    nets_parser = subparsers.add_parser("nets", help="Trace nets in a schematic")
    nets_parser.add_argument("schematic", help="Path to .kicad_sch file")
    nets_parser.add_argument("--format", choices=["table", "json"], default="table")
    nets_parser.add_argument("--net", help="Trace a specific net by label")
    nets_parser.add_argument("--stats", action="store_true", help="Show statistics only")

    # ERC subcommand
    erc_parser = subparsers.add_parser("erc", help="Parse ERC report")
    erc_parser.add_argument("report", help="Path to ERC report (.json or .rpt)")
    erc_parser.add_argument("--format", choices=["table", "json", "summary"], default="table")
    erc_parser.add_argument("--errors-only", action="store_true")
    erc_parser.add_argument("--type", dest="filter_type", help="Filter by violation type")
    erc_parser.add_argument("--sheet", help="Filter by sheet path")

    # DRC subcommand
    drc_parser = subparsers.add_parser("drc", help="Parse DRC report")
    drc_parser.add_argument("report", help="Path to DRC report (.json or .rpt)")
    drc_parser.add_argument("--format", choices=["table", "json", "summary"], default="table")
    drc_parser.add_argument("--errors-only", action="store_true")
    drc_parser.add_argument("--type", dest="filter_type", help="Filter by violation type")
    drc_parser.add_argument("--net", help="Filter by net name")
    drc_parser.add_argument(
        "--mfr",
        choices=["jlcpcb", "pcbway", "oshpark", "seeed"],
        help="Check against manufacturer design rules",
    )
    drc_parser.add_argument("--layers", type=int, default=2, help="Number of copper layers")

    # BOM subcommand
    bom_parser = subparsers.add_parser("bom", help="Generate bill of materials")
    bom_parser.add_argument("schematic", help="Path to .kicad_sch file")
    bom_parser.add_argument("--format", choices=["table", "csv", "json"], default="table")
    bom_parser.add_argument("--group", action="store_true", help="Group identical components")
    bom_parser.add_argument(
        "--exclude", action="append", default=[], help="Exclude references matching pattern"
    )
    bom_parser.add_argument("--include-dnp", action="store_true")
    bom_parser.add_argument(
        "--sort", choices=["reference", "value", "footprint"], default="reference"
    )

    # SCH subcommand - schematic tools
    sch_parser = subparsers.add_parser("sch", help="Schematic analysis tools")
    sch_subparsers = sch_parser.add_subparsers(dest="sch_command", help="Schematic commands")

    # sch summary
    sch_summary = sch_subparsers.add_parser("summary", help="Quick schematic overview")
    sch_summary.add_argument("schematic", help="Path to .kicad_sch file")
    sch_summary.add_argument("--format", choices=["text", "json"], default="text")
    sch_summary.add_argument("-v", "--verbose", action="store_true")

    # sch hierarchy
    sch_hierarchy = sch_subparsers.add_parser("hierarchy", help="Show hierarchy tree")
    sch_hierarchy.add_argument("schematic", help="Path to root .kicad_sch file")
    sch_hierarchy.add_argument("--format", choices=["tree", "json"], default="tree")
    sch_hierarchy.add_argument("--depth", type=int, help="Maximum depth to show")

    # sch labels
    sch_labels = sch_subparsers.add_parser("labels", help="List labels")
    sch_labels.add_argument("schematic", help="Path to .kicad_sch file")
    sch_labels.add_argument("--format", choices=["table", "json", "csv"], default="table")
    sch_labels.add_argument(
        "--type", choices=["all", "local", "global", "hierarchical", "power"], default="all"
    )
    sch_labels.add_argument("--filter", dest="pattern", help="Filter by label text pattern")

    # sch validate
    sch_validate = sch_subparsers.add_parser("validate", help="Run validation checks")
    sch_validate.add_argument("schematic", help="Path to .kicad_sch file")
    sch_validate.add_argument("--format", choices=["text", "json"], default="text")
    sch_validate.add_argument("--strict", action="store_true", help="Exit with error on warnings")
    sch_validate.add_argument("-q", "--quiet", action="store_true", help="Only show errors")

    # PCB subcommand - PCB tools
    pcb_parser = subparsers.add_parser("pcb", help="PCB query tools")
    pcb_subparsers = pcb_parser.add_subparsers(dest="pcb_command", help="PCB commands")

    # pcb summary
    pcb_summary = pcb_subparsers.add_parser("summary", help="Board summary")
    pcb_summary.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_summary.add_argument("--format", choices=["text", "json"], default="text")

    # pcb footprints
    pcb_footprints = pcb_subparsers.add_parser("footprints", help="List footprints")
    pcb_footprints.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_footprints.add_argument("--format", choices=["text", "json"], default="text")
    pcb_footprints.add_argument("--filter", dest="pattern", help="Filter by reference pattern")
    pcb_footprints.add_argument("--sorted", action="store_true", help="Sort output")

    # pcb nets
    pcb_nets = pcb_subparsers.add_parser("nets", help="List nets")
    pcb_nets.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_nets.add_argument("--format", choices=["text", "json"], default="text")
    pcb_nets.add_argument("--filter", dest="pattern", help="Filter by net pattern")
    pcb_nets.add_argument("--sorted", action="store_true", help="Sort output")

    # pcb traces
    pcb_traces = pcb_subparsers.add_parser("traces", help="Show trace statistics")
    pcb_traces.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_traces.add_argument("--format", choices=["text", "json"], default="text")
    pcb_traces.add_argument("--layer", help="Filter by layer (e.g., F.Cu)")

    # pcb stackup
    pcb_stackup = pcb_subparsers.add_parser("stackup", help="Show layer stackup")
    pcb_stackup.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_stackup.add_argument("--format", choices=["text", "json"], default="text")

    # LIB subcommand - library tools
    lib_parser = subparsers.add_parser("lib", help="Symbol library tools")
    lib_subparsers = lib_parser.add_subparsers(dest="lib_command", help="Library commands")

    # lib symbols
    lib_symbols = lib_subparsers.add_parser("symbols", help="List symbols in library")
    lib_symbols.add_argument("library", help="Path to .kicad_sym file")
    lib_symbols.add_argument("--format", choices=["table", "json"], default="table")
    lib_symbols.add_argument("--pins", action="store_true", help="Show pin details")

    # MFR subcommand - manufacturer tools
    mfr_parser = subparsers.add_parser("mfr", help="Manufacturer tools")
    mfr_subparsers = mfr_parser.add_subparsers(dest="mfr_command", help="Manufacturer commands")

    # mfr list
    mfr_subparsers.add_parser("list", help="List available manufacturers")

    # mfr info
    mfr_info = mfr_subparsers.add_parser("info", help="Show manufacturer details")
    mfr_info.add_argument("manufacturer", help="Manufacturer ID (jlcpcb, seeed, etc.)")

    # mfr rules
    mfr_rules = mfr_subparsers.add_parser("rules", help="Show design rules")
    mfr_rules.add_argument("manufacturer", help="Manufacturer ID")
    mfr_rules.add_argument("-l", "--layers", type=int, default=4, help="Layer count")
    mfr_rules.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")

    # mfr compare
    mfr_compare = mfr_subparsers.add_parser("compare", help="Compare manufacturers")
    mfr_compare.add_argument("-l", "--layers", type=int, default=4, help="Layer count")
    mfr_compare.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")

    # mfr apply-rules
    mfr_apply = mfr_subparsers.add_parser(
        "apply-rules", help="Apply manufacturer design rules to project/PCB"
    )
    mfr_apply.add_argument("file", help="Path to .kicad_pro or .kicad_pcb file")
    mfr_apply.add_argument("manufacturer", help="Manufacturer ID (jlcpcb, seeed, etc.)")
    mfr_apply.add_argument("-l", "--layers", type=int, default=2, help="Layer count")
    mfr_apply.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")
    mfr_apply.add_argument("-o", "--output", help="Output file (default: modify in place)")
    mfr_apply.add_argument("--dry-run", action="store_true", help="Show changes without applying")

    # mfr validate
    mfr_validate = mfr_subparsers.add_parser(
        "validate", help="Validate PCB against manufacturer rules"
    )
    mfr_validate.add_argument("file", help="Path to .kicad_pcb file")
    mfr_validate.add_argument("manufacturer", help="Manufacturer ID (jlcpcb, seeed, etc.)")
    mfr_validate.add_argument("-l", "--layers", type=int, default=2, help="Layer count")
    mfr_validate.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")

    # ROUTE subcommand - PCB autorouting
    route_parser = subparsers.add_parser("route", help="Autoroute a PCB")
    route_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    route_parser.add_argument("-o", "--output", help="Output file path")
    route_parser.add_argument(
        "--strategy",
        choices=["basic", "negotiated", "monte-carlo"],
        default="negotiated",
        help="Routing strategy (default: negotiated)",
    )
    route_parser.add_argument("--skip-nets", help="Comma-separated nets to skip")
    route_parser.add_argument("--grid", type=float, default=0.25, help="Grid resolution in mm")
    route_parser.add_argument("--trace-width", type=float, default=0.2, help="Trace width in mm")
    route_parser.add_argument("--clearance", type=float, default=0.15, help="Clearance in mm")
    route_parser.add_argument("--via-drill", type=float, default=0.3, help="Via drill size in mm")
    route_parser.add_argument("--via-diameter", type=float, default=0.6, help="Via diameter in mm")
    route_parser.add_argument("--mc-trials", type=int, default=10, help="Monte Carlo trials")
    route_parser.add_argument("--iterations", type=int, default=15, help="Max iterations")
    route_parser.add_argument("-v", "--verbose", action="store_true")
    route_parser.add_argument("--dry-run", action="store_true", help="Don't write output")

    # OPTIMIZE-TRACES subcommand - Trace optimization
    optimize_parser = subparsers.add_parser("optimize-traces", help="Optimize PCB traces")
    optimize_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    optimize_parser.add_argument("-o", "--output", help="Output file (default: modify in place)")
    optimize_parser.add_argument("--net", help="Only optimize traces matching this net pattern")
    optimize_parser.add_argument("--no-merge", action="store_true", help="Disable collinear merging")
    optimize_parser.add_argument("--no-zigzag", action="store_true", help="Disable zigzag elimination")
    optimize_parser.add_argument("--no-45", action="store_true", help="Disable 45-degree corners")
    optimize_parser.add_argument(
        "--chamfer-size", type=float, default=0.5, help="45-degree chamfer size in mm (default: 0.5)"
    )
    optimize_parser.add_argument("-v", "--verbose", action="store_true")
    optimize_parser.add_argument("--dry-run", action="store_true", help="Show results without writing")

    # VALIDATE-FOOTPRINTS subcommand
    validate_fp_parser = subparsers.add_parser(
        "validate-footprints", help="Validate footprints for pad spacing issues"
    )
    validate_fp_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    validate_fp_parser.add_argument(
        "--min-pad-gap",
        type=float,
        default=0.15,
        help="Minimum required gap between pads in mm (default: 0.15)",
    )
    validate_fp_parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    validate_fp_parser.add_argument(
        "--errors-only",
        action="store_true",
        help="Only show errors, not warnings",
    )

    # FIX-FOOTPRINTS subcommand
    fix_fp_parser = subparsers.add_parser(
        "fix-footprints", help="Fix footprint pad spacing issues"
    )
    fix_fp_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    fix_fp_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: modify in place)",
    )
    fix_fp_parser.add_argument(
        "--min-pad-gap",
        type=float,
        default=0.2,
        help="Target gap between pads in mm (default: 0.2)",
    )
    fix_fp_parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    fix_fp_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without applying",
    )

    # PARTS subcommand - LCSC parts lookup
    parts_parser = subparsers.add_parser("parts", help="LCSC parts lookup and search")
    parts_subparsers = parts_parser.add_subparsers(dest="parts_command", help="Parts commands")

    # parts lookup
    parts_lookup = parts_subparsers.add_parser("lookup", help="Look up part by LCSC number")
    parts_lookup.add_argument("part", help="LCSC part number (e.g., C123456)")
    parts_lookup.add_argument("--format", choices=["text", "json"], default="text")
    parts_lookup.add_argument("--no-cache", action="store_true", help="Bypass cache")

    # parts search
    parts_search = parts_subparsers.add_parser("search", help="Search for parts")
    parts_search.add_argument("query", help="Search query")
    parts_search.add_argument("--format", choices=["text", "json", "table"], default="table")
    parts_search.add_argument("--limit", type=int, default=20, help="Max results")
    parts_search.add_argument("--in-stock", action="store_true", help="Only in-stock parts")
    parts_search.add_argument("--basic", action="store_true", help="Only JLCPCB basic parts")

    # parts cache
    parts_cache = parts_subparsers.add_parser("cache", help="Cache management")
    parts_cache.add_argument(
        "cache_action",
        nargs="?",
        choices=["stats", "clear", "clear-expired"],
        default="stats",
        help="Cache action (default: stats)",
    )

    # PLACEMENT subcommand - placement conflict detection and resolution
    placement_parser = subparsers.add_parser(
        "placement", help="Detect and fix placement conflicts"
    )
    placement_subparsers = placement_parser.add_subparsers(
        dest="placement_command", help="Placement commands"
    )

    # placement check
    placement_check = placement_subparsers.add_parser(
        "check", help="Check PCB for placement conflicts"
    )
    placement_check.add_argument("pcb", help="Path to .kicad_pcb file")
    placement_check.add_argument(
        "--format", choices=["table", "json", "summary"], default="table"
    )
    placement_check.add_argument(
        "--pad-clearance", type=float, default=0.1, help="Min pad clearance (mm)"
    )
    placement_check.add_argument(
        "--hole-clearance", type=float, default=0.5, help="Min hole clearance (mm)"
    )
    placement_check.add_argument(
        "--edge-clearance", type=float, default=0.3, help="Min edge clearance (mm)"
    )
    placement_check.add_argument("-v", "--verbose", action="store_true")

    # placement fix
    placement_fix = placement_subparsers.add_parser(
        "fix", help="Suggest and apply placement fixes"
    )
    placement_fix.add_argument("pcb", help="Path to .kicad_pcb file")
    placement_fix.add_argument("-o", "--output", help="Output file path")
    placement_fix.add_argument(
        "--strategy",
        choices=["spread", "compact", "anchor"],
        default="spread",
        help="Fix strategy",
    )
    placement_fix.add_argument(
        "--anchor", help="Comma-separated components to keep fixed"
    )
    placement_fix.add_argument("--dry-run", action="store_true")
    placement_fix.add_argument("-v", "--verbose", action="store_true")

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

    elif args.command == "sch":
        return _run_sch_command(args)

    elif args.command == "pcb":
        return _run_pcb_command(args)

    elif args.command == "lib":
        return _run_lib_command(args)

    elif args.command == "mfr":
        return _run_mfr_command(args)

    elif args.command == "parts":
        return _run_parts_command(args)

    elif args.command == "route":
        return _run_route_command(args)

    elif args.command == "placement":
        return _run_placement_command(args)

    elif args.command == "optimize-traces":
        return _run_optimize_command(args)

    elif args.command == "validate-footprints":
        return _run_validate_footprints_command(args)

    elif args.command == "fix-footprints":
        return _run_fix_footprints_command(args)

    return 0


def _run_validate_footprints_command(args) -> int:
    """Handle validate-footprints command."""
    from .footprint_cmd import main_validate

    sub_argv = [args.pcb]
    if args.min_pad_gap != 0.15:
        sub_argv.extend(["--min-pad-gap", str(args.min_pad_gap)])
    if args.format != "text":
        sub_argv.extend(["--format", args.format])
    if args.errors_only:
        sub_argv.append("--errors-only")
    return main_validate(sub_argv)


def _run_fix_footprints_command(args) -> int:
    """Handle fix-footprints command."""
    from .footprint_cmd import main_fix

    sub_argv = [args.pcb]
    if args.output:
        sub_argv.extend(["-o", args.output])
    if args.min_pad_gap != 0.2:
        sub_argv.extend(["--min-pad-gap", str(args.min_pad_gap)])
    if args.format != "text":
        sub_argv.extend(["--format", args.format])
    if args.dry_run:
        sub_argv.append("--dry-run")
    return main_fix(sub_argv)


def _run_sch_command(args) -> int:
    """Handle schematic subcommands."""
    if not args.sch_command:
        print("Usage: kicad-tools sch <command> [options] <file>")
        print("Commands: summary, hierarchy, labels, validate")
        return 1

    schematic_path = Path(args.schematic)
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    if args.sch_command == "summary":
        from .sch_summary import run_summary

        return run_summary(schematic_path, args.format, args.verbose)

    elif args.sch_command == "hierarchy":
        from .sch_hierarchy import main as hierarchy_main

        sub_argv = [str(schematic_path), "tree"]
        if args.format != "tree":
            sub_argv.extend(["--format", args.format])
        if args.depth:
            sub_argv.extend(["--depth", str(args.depth)])
        return hierarchy_main(sub_argv) or 0

    elif args.sch_command == "labels":
        from .sch_list_labels import main as labels_main

        sub_argv = [str(schematic_path)]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.type != "all":
            sub_argv.extend(["--type", args.type])
        if args.pattern:
            sub_argv.extend(["--filter", args.pattern])
        return labels_main(sub_argv) or 0

    elif args.sch_command == "validate":
        from .sch_validate import main as validate_main

        sub_argv = [str(schematic_path)]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.strict:
            sub_argv.append("--strict")
        if args.quiet:
            sub_argv.append("--quiet")
        return validate_main(sub_argv) or 0

    return 1


def _run_pcb_command(args) -> int:
    """Handle PCB subcommands."""
    if not args.pcb_command:
        print("Usage: kicad-tools pcb <command> [options] <file>")
        print("Commands: summary, footprints, nets, traces, stackup")
        return 1

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    from .pcb_query import main as pcb_main

    if args.pcb_command == "summary":
        sub_argv = [str(pcb_path), "summary"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return pcb_main(sub_argv) or 0

    elif args.pcb_command == "footprints":
        sub_argv = [str(pcb_path), "footprints"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.pattern:
            sub_argv.extend(["--filter", args.pattern])
        if args.sorted:
            sub_argv.append("--sorted")
        return pcb_main(sub_argv) or 0

    elif args.pcb_command == "nets":
        sub_argv = [str(pcb_path), "nets"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.pattern:
            sub_argv.extend(["--filter", args.pattern])
        if args.sorted:
            sub_argv.append("--sorted")
        return pcb_main(sub_argv) or 0

    elif args.pcb_command == "traces":
        sub_argv = [str(pcb_path), "traces"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.layer:
            sub_argv.extend(["--layer", args.layer])
        return pcb_main(sub_argv) or 0

    elif args.pcb_command == "stackup":
        sub_argv = [str(pcb_path), "stackup"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return pcb_main(sub_argv) or 0

    return 1


def _run_lib_command(args) -> int:
    """Handle library subcommands."""
    if not args.lib_command:
        print("Usage: kicad-tools lib <command> [options] <file>")
        print("Commands: symbols")
        return 1

    if args.lib_command == "symbols":
        library_path = Path(args.library)
        if not library_path.exists():
            print(f"Error: File not found: {library_path}", file=sys.stderr)
            return 1

        from .lib_list_symbols import main as lib_main

        sub_argv = [str(library_path)]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.pins:
            sub_argv.append("--pins")
        return lib_main(sub_argv) or 0

    return 1


def _run_mfr_command(args) -> int:
    """Handle manufacturer subcommands."""
    if not args.mfr_command:
        print("Usage: kicad-tools mfr <command> [options]")
        print("Commands: list, info, rules, compare, apply-rules, validate")
        return 1

    from .mfr import main as mfr_main

    if args.mfr_command == "list":
        return mfr_main(["list"]) or 0

    elif args.mfr_command == "info":
        return mfr_main(["info", args.manufacturer]) or 0

    elif args.mfr_command == "rules":
        sub_argv = ["rules", args.manufacturer]
        if args.layers != 4:
            sub_argv.extend(["--layers", str(args.layers)])
        if args.copper != 1.0:
            sub_argv.extend(["--copper", str(args.copper)])
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "compare":
        sub_argv = ["compare"]
        if args.layers != 4:
            sub_argv.extend(["--layers", str(args.layers)])
        if args.copper != 1.0:
            sub_argv.extend(["--copper", str(args.copper)])
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "apply-rules":
        sub_argv = ["apply-rules", args.file, args.manufacturer]
        if args.layers != 2:
            sub_argv.extend(["--layers", str(args.layers)])
        if args.copper != 1.0:
            sub_argv.extend(["--copper", str(args.copper)])
        if args.output:
            sub_argv.extend(["--output", args.output])
        if args.dry_run:
            sub_argv.append("--dry-run")
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "validate":
        sub_argv = ["validate", args.file, args.manufacturer]
        if args.layers != 2:
            sub_argv.extend(["--layers", str(args.layers)])
        if args.copper != 1.0:
            sub_argv.extend(["--copper", str(args.copper)])
        return mfr_main(sub_argv) or 0

    return 1


def _run_parts_command(args) -> int:
    """Handle parts subcommands."""
    if not args.parts_command:
        print("Usage: kicad-tools parts <command> [options]")
        print("Commands: lookup, search, cache")
        return 1

    from .parts_cmd import main as parts_main

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

    elif args.parts_command == "cache":
        sub_argv = ["cache", args.cache_action]
        return parts_main(sub_argv) or 0

    return 1


def _run_route_command(args) -> int:
    """Handle route command."""
    from .route_cmd import main as route_main

    sub_argv = [args.pcb]
    if args.output:
        sub_argv.extend(["-o", args.output])
    if args.strategy != "negotiated":
        sub_argv.extend(["--strategy", args.strategy])
    if args.skip_nets:
        sub_argv.extend(["--skip-nets", args.skip_nets])
    if args.grid != 0.25:
        sub_argv.extend(["--grid", str(args.grid)])
    if args.trace_width != 0.2:
        sub_argv.extend(["--trace-width", str(args.trace_width)])
    if args.clearance != 0.15:
        sub_argv.extend(["--clearance", str(args.clearance)])
    if args.via_drill != 0.3:
        sub_argv.extend(["--via-drill", str(args.via_drill)])
    if args.via_diameter != 0.6:
        sub_argv.extend(["--via-diameter", str(args.via_diameter)])
    if args.mc_trials != 10:
        sub_argv.extend(["--mc-trials", str(args.mc_trials)])
    if args.iterations != 15:
        sub_argv.extend(["--iterations", str(args.iterations)])
    if args.verbose:
        sub_argv.append("--verbose")
    if args.dry_run:
        sub_argv.append("--dry-run")
    return route_main(sub_argv)


def _run_placement_command(args) -> int:
    """Handle placement command."""
    if not args.placement_command:
        print("Usage: kicad-tools placement <command> [options] <file>")
        print("Commands: check, fix")
        return 1

    from .placement_cmd import main as placement_main

    if args.placement_command == "check":
        sub_argv = ["check", args.pcb]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.pad_clearance != 0.1:
            sub_argv.extend(["--pad-clearance", str(args.pad_clearance)])
        if args.hole_clearance != 0.5:
            sub_argv.extend(["--hole-clearance", str(args.hole_clearance)])
        if args.edge_clearance != 0.3:
            sub_argv.extend(["--edge-clearance", str(args.edge_clearance)])
        if args.verbose:
            sub_argv.append("--verbose")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "fix":
        sub_argv = ["fix", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.strategy != "spread":
            sub_argv.extend(["--strategy", args.strategy])
        if args.anchor:
            sub_argv.extend(["--anchor", args.anchor])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.verbose:
            sub_argv.append("--verbose")
        return placement_main(sub_argv) or 0

    return 1


def _run_optimize_command(args) -> int:
    """Handle optimize-traces command."""
    from .optimize_cmd import main as optimize_main

    sub_argv = [args.pcb]
    if args.output:
        sub_argv.extend(["-o", args.output])
    if args.net:
        sub_argv.extend(["--net", args.net])
    if args.no_merge:
        sub_argv.append("--no-merge")
    if args.no_zigzag:
        sub_argv.append("--no-zigzag")
    if args.no_45:
        sub_argv.append("--no-45")
    if args.chamfer_size != 0.5:
        sub_argv.extend(["--chamfer-size", str(args.chamfer_size)])
    if args.verbose:
        sub_argv.append("--verbose")
    if args.dry_run:
        sub_argv.append("--dry-run")
    return optimize_main(sub_argv)


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
