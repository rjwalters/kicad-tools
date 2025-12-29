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

Examples:
    kct symbols design.kicad_sch --filter "U*"
    kct nets design.kicad_sch --net VCC
    kct erc design-erc.json --errors-only
    kct drc design-drc.rpt --mfr jlcpcb
    kct bom design.kicad_sch --format csv
    kct sch summary design.kicad_sch
    kct pcb summary board.kicad_pcb
    kct mfr compare
"""

import argparse
import sys
from pathlib import Path
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
    parser.add_argument("--version", action="version", version="kicad-tools 0.1.0")

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

    return 0


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
        print("Commands: list, info, rules, compare")
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

    return 1


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
