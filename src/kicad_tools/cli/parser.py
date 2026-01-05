"""
Argument parser setup for kicad-tools CLI.

This module contains all the argparse configuration for the kicad-tools CLI.
The parser is organized into subparsers for each major command category.
"""

import argparse

from kicad_tools import __version__

__all__ = ["create_parser"]

# Module docstring used as epilog in help
CLI_DOCSTRING = """
Provides CLI commands for common KiCad operations via the `kicad-tools` or `kct` command:

    kicad-tools symbols <schematic>    - List and query symbols
    kicad-tools nets <schematic>       - Trace and analyze nets
    kicad-tools erc <report>           - Parse ERC report
    kicad-tools drc <report>           - Parse DRC report
    kicad-tools bom <schematic>        - Generate bill of materials
    kicad-tools check <pcb>            - Pure Python DRC (no kicad-cli)
    kicad-tools validate --sync        - Check schematic-to-PCB netlist sync
    kicad-tools sch <command> <file>   - Schematic analysis tools
    kicad-tools pcb <command> <file>   - PCB query tools
    kicad-tools lib <command> <file>   - Symbol library tools
    kicad-tools footprint <command>    - Footprint generation tools
    kicad-tools mfr <command>          - Manufacturer tools
    kicad-tools parts <command>        - LCSC parts lookup and search
    kicad-tools datasheet <command>    - Datasheet search, download, and PDF parsing
    kicad-tools route <pcb>            - Autoroute a PCB
    kicad-tools zones <command>        - Add copper pour zones
    kicad-tools reason <pcb>           - LLM-driven PCB layout reasoning
    kicad-tools placement <command>    - Detect and fix placement conflicts
    kicad-tools optimize-traces <pcb>  - Optimize PCB traces
    kicad-tools validate-footprints    - Validate footprint pad spacing
    kicad-tools fix-footprints <pcb>   - Fix footprint pad spacing issues
    kicad-tools analyze <command>      - PCB analysis tools
    kicad-tools audit <project>        - Manufacturing readiness audit
    kicad-tools clean <project>        - Clean up old/orphaned files
    kicad-tools config                 - View/manage configuration
    kicad-tools interactive            - Launch interactive REPL mode

Schematic subcommands (kct sch <command>):
    summary        - Quick schematic overview
    hierarchy      - Show hierarchy tree
    labels         - List labels
    validate       - Run validation checks
    wires          - List wire segments and junctions
    info           - Show symbol details
    pins           - Show symbol pin positions
    connections    - Check pin connections using library positions
    unconnected    - Find unconnected pins and issues
    replace        - Replace a symbol's library ID
    sync-hierarchy - Synchronize sheet pins and hierarchical labels

Examples:
    kct symbols design.kicad_sch --filter "U*"
    kct nets design.kicad_sch --net VCC
    kct erc design-erc.json --errors-only
    kct drc design-drc.rpt --mfr jlcpcb
    kct bom design.kicad_sch --format csv
    kct sch summary design.kicad_sch
    kct sch wires design.kicad_sch --stats
    kct sch info design.kicad_sch U1 --show-pins
    kct sch replace design.kicad_sch U1 "mylib:NewSymbol" --dry-run
    kct pcb summary board.kicad_pcb
    kct mfr compare
    kct parts lookup C123456
    kct parts search "100nF 0402" --in-stock
    kct parts availability design.kicad_sch --quantity 100
    kct datasheet convert STM32F103.pdf --output summary.md
    kct datasheet extract-images STM32F103.pdf --output images/
    kct route board.kicad_pcb --strategy negotiated
    kct reason board.kicad_pcb --export-state
    kct reason board.kicad_pcb --analyze
    kct validate-footprints board.kicad_pcb --min-pad-gap 0.15
    kct fix-footprints board.kicad_pcb --min-pad-gap 0.2 --dry-run
    kct footprint generate soic --pins 8 -o SOIC8.kicad_mod
    kct footprint generate chip --size 0402 --prefix R
    kct footprint generate qfp --pins 48 --pitch 0.5
    kct footprint generate sot --variant SOT-23
    kct footprint generate --list
    kct interactive
    kct interactive --project myboard.kicad_pro
    kct audit project.kicad_pro --mfr jlcpcb
    kct audit board.kicad_pcb --mfr jlcpcb --skip-erc
    kct audit project.kicad_pro --format json --strict
    kct clean project.kicad_pro
    kct clean project.kicad_pro --deep --force
"""


def create_parser() -> argparse.ArgumentParser:
    """Create and configure the main argument parser."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools",
        description="KiCad automation toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=CLI_DOCSTRING,
    )
    parser.add_argument("--version", action="version", version=f"kicad-tools {__version__}")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show full stack traces on errors",
        dest="global_verbose",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
        dest="global_quiet",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Add all subparsers
    _add_symbols_parser(subparsers)
    _add_nets_parser(subparsers)
    _add_netlist_parser(subparsers)
    _add_erc_parser(subparsers)
    _add_drc_parser(subparsers)
    _add_bom_parser(subparsers)
    _add_check_parser(subparsers)
    _add_sch_parser(subparsers)
    _add_pcb_parser(subparsers)
    _add_lib_parser(subparsers)
    _add_footprint_parser(subparsers)
    _add_mfr_parser(subparsers)
    _add_zones_parser(subparsers)
    _add_route_parser(subparsers)
    _add_reason_parser(subparsers)
    _add_optimize_parser(subparsers)
    _add_validate_footprints_parser(subparsers)
    _add_fix_footprints_parser(subparsers)
    _add_parts_parser(subparsers)
    _add_datasheet_parser(subparsers)
    _add_placement_parser(subparsers)
    _add_config_parser(subparsers)
    _add_interactive_parser(subparsers)
    _add_validate_parser(subparsers)
    _add_analyze_parser(subparsers)
    _add_constraints_parser(subparsers)
    _add_estimate_parser(subparsers)
    _add_audit_parser(subparsers)
    _add_suggest_parser(subparsers)
    _add_net_status_parser(subparsers)
    _add_clean_parser(subparsers)
    _add_impedance_parser(subparsers)

    return parser


def _add_symbols_parser(subparsers) -> None:
    """Add symbols subcommand parser."""
    symbols_parser = subparsers.add_parser("symbols", help="List symbols in a schematic")
    symbols_parser.add_argument("schematic", help="Path to .kicad_sch file")
    symbols_parser.add_argument("--format", choices=["table", "json", "csv"], default="table")
    symbols_parser.add_argument("--filter", dest="pattern", help="Filter by reference")
    symbols_parser.add_argument("--lib", dest="lib_id", help="Filter by library ID")
    symbols_parser.add_argument("-v", "--verbose", action="store_true")


def _add_nets_parser(subparsers) -> None:
    """Add nets subcommand parser."""
    nets_parser = subparsers.add_parser("nets", help="Trace nets in a schematic")
    nets_parser.add_argument("schematic", help="Path to .kicad_sch file")
    nets_parser.add_argument("--format", choices=["table", "json"], default="table")
    nets_parser.add_argument("--net", help="Trace a specific net by label")
    nets_parser.add_argument("--stats", action="store_true", help="Show statistics only")


def _add_netlist_parser(subparsers) -> None:
    """Add netlist subcommand parser with its subcommands."""
    netlist_parser = subparsers.add_parser("netlist", help="Netlist analysis and comparison tools")
    netlist_subparsers = netlist_parser.add_subparsers(
        dest="netlist_command", help="Netlist commands"
    )

    # netlist analyze
    netlist_analyze = netlist_subparsers.add_parser("analyze", help="Show connectivity statistics")
    netlist_analyze.add_argument("netlist_schematic", help="Path to .kicad_sch file")
    netlist_analyze.add_argument(
        "--format", dest="netlist_format", choices=["text", "json"], default="text"
    )

    # netlist list
    netlist_list = netlist_subparsers.add_parser(
        "list", help="List all nets with connection counts"
    )
    netlist_list.add_argument("netlist_schematic", help="Path to .kicad_sch file")
    netlist_list.add_argument(
        "--format", dest="netlist_format", choices=["table", "json"], default="table"
    )
    netlist_list.add_argument(
        "--sort",
        dest="netlist_sort",
        choices=["name", "connections"],
        default="connections",
    )

    # netlist show
    netlist_show = netlist_subparsers.add_parser("show", help="Show specific net details")
    netlist_show.add_argument("netlist_schematic", help="Path to .kicad_sch file")
    netlist_show.add_argument("--net", dest="netlist_net", required=True, help="Net name")
    netlist_show.add_argument(
        "--format", dest="netlist_format", choices=["text", "json"], default="text"
    )

    # netlist check
    netlist_check = netlist_subparsers.add_parser("check", help="Find connectivity issues")
    netlist_check.add_argument("netlist_schematic", help="Path to .kicad_sch file")
    netlist_check.add_argument(
        "--format", dest="netlist_format", choices=["text", "json"], default="text"
    )

    # netlist compare
    netlist_compare = netlist_subparsers.add_parser("compare", help="Compare two netlists")
    netlist_compare.add_argument("netlist_old", help="Path to old .kicad_sch file")
    netlist_compare.add_argument("netlist_new", help="Path to new .kicad_sch file")
    netlist_compare.add_argument(
        "--format", dest="netlist_format", choices=["text", "json"], default="text"
    )

    # netlist export
    netlist_export = netlist_subparsers.add_parser("export", help="Export netlist file")
    netlist_export.add_argument("netlist_schematic", help="Path to .kicad_sch file")
    netlist_export.add_argument("-o", "--output", dest="netlist_output", help="Output path")
    netlist_export.add_argument(
        "--format", dest="netlist_format", choices=["kicad", "json"], default="kicad"
    )


def _add_erc_parser(subparsers) -> None:
    """Add ERC subcommand parser with subcommands."""
    erc_parser = subparsers.add_parser(
        "erc",
        help="ERC validation and analysis",
        description="Run ERC on schematics or analyze ERC reports",
    )

    erc_subparsers = erc_parser.add_subparsers(
        dest="erc_command", help="ERC commands", required=True
    )

    # erc parse subcommand (default behavior for backwards compatibility)
    # Usage: kct erc parse <file> OR kct erc <file> (auto-inserted by main)
    erc_parse = erc_subparsers.add_parser(
        "parse",
        help="Parse ERC report (default command)",
        description="Parse and display ERC report from schematic or JSON file",
    )
    erc_parse.add_argument(
        "report",
        help="Path to schematic (.kicad_sch) or ERC report (.json or .rpt)",
    )
    erc_parse.add_argument("--format", choices=["table", "json", "summary"], default="table")
    erc_parse.add_argument("--errors-only", action="store_true")
    erc_parse.add_argument("--type", dest="filter_type", help="Filter by violation type")
    erc_parse.add_argument("--sheet", help="Filter by sheet path")

    # erc explain subcommand
    erc_explain = erc_subparsers.add_parser(
        "explain",
        help="Detailed ERC error analysis with root cause and fixes",
        description="Analyze ERC errors with detailed explanations, root causes, and fix suggestions",
    )
    erc_explain.add_argument(
        "explain_input",
        help="Path to schematic (.kicad_sch) or ERC report (.json/.rpt)",
    )
    erc_explain.add_argument(
        "--format",
        dest="explain_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    erc_explain.add_argument(
        "--errors-only",
        dest="explain_errors_only",
        action="store_true",
        help="Show only errors, not warnings",
    )
    erc_explain.add_argument(
        "--type",
        "-t",
        dest="explain_filter_type",
        help="Filter by violation type",
    )
    erc_explain.add_argument(
        "--keep-report",
        dest="explain_keep_report",
        action="store_true",
        help="Keep the ERC report file after running",
    )


def _add_drc_parser(subparsers) -> None:
    """Add DRC subcommand parser."""
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


def _add_bom_parser(subparsers) -> None:
    """Add BOM subcommand parser."""
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


def _add_check_parser(subparsers) -> None:
    """Add check subcommand parser (pure Python DRC)."""
    check_parser = subparsers.add_parser("check", help="Pure Python DRC (no kicad-cli)")
    check_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    check_parser.add_argument("--format", choices=["table", "json", "summary"], default="table")
    check_parser.add_argument("--errors-only", action="store_true")
    check_parser.add_argument("--strict", action="store_true", help="Exit with code 2 on warnings")
    check_parser.add_argument(
        "--mfr",
        "-m",
        choices=["jlcpcb", "pcbway", "oshpark", "seeed"],
        default="jlcpcb",
        help="Target manufacturer (default: jlcpcb)",
    )
    check_parser.add_argument("--layers", "-l", type=int, default=2, help="Number of layers")
    check_parser.add_argument("--copper", "-c", type=float, default=1.0, help="Copper weight (oz)")
    check_parser.add_argument(
        "--only",
        dest="only_checks",
        help="Run only specific checks (comma-separated: clearance, dimensions, edge, silkscreen)",
    )
    check_parser.add_argument(
        "--skip",
        dest="skip_checks",
        help="Skip specific checks (comma-separated)",
    )
    check_parser.add_argument("-v", "--verbose", action="store_true")


def _add_sch_parser(subparsers) -> None:
    """Add schematic subcommand parser with its subcommands."""
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

    # sch wires
    sch_wires = sch_subparsers.add_parser("wires", help="List wire segments and junctions")
    sch_wires.add_argument("schematic", help="Path to .kicad_sch file")
    sch_wires.add_argument("--format", choices=["table", "json", "csv"], default="table")
    sch_wires.add_argument("--stats", action="store_true", help="Show statistics only")
    sch_wires.add_argument("--junctions", action="store_true", help="Include junction points")

    # sch info
    sch_info = sch_subparsers.add_parser("info", help="Show symbol details")
    sch_info.add_argument("schematic", help="Path to .kicad_sch file")
    sch_info.add_argument("reference", help="Symbol reference (e.g., U1)")
    sch_info.add_argument("--format", choices=["text", "json"], default="text")
    sch_info.add_argument("--show-pins", action="store_true", help="Show pin details")
    sch_info.add_argument("--show-properties", action="store_true", help="Show all properties")

    # sch pins
    sch_pins = sch_subparsers.add_parser("pins", help="Show symbol pin positions")
    sch_pins.add_argument("schematic", help="Path to .kicad_sch file")
    sch_pins.add_argument("reference", help="Symbol reference (e.g., U1)")
    sch_pins.add_argument("--lib", required=True, help="Path to symbol library file")
    sch_pins.add_argument("--format", choices=["table", "json"], default="table")

    # sch connections
    sch_connections = sch_subparsers.add_parser(
        "connections", help="Check pin connections using library positions"
    )
    sch_connections.add_argument("schematic", help="Path to .kicad_sch file")
    sch_connections.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    sch_connections.add_argument(
        "--lib", action="append", dest="libs", help="Specific library file"
    )
    sch_connections.add_argument("--format", choices=["table", "json"], default="table")
    sch_connections.add_argument("--filter", dest="pattern", help="Filter by symbol reference")
    sch_connections.add_argument(
        "-v", "--verbose", action="store_true", help="Show all pins, not just unconnected"
    )

    # sch unconnected
    sch_unconnected = sch_subparsers.add_parser(
        "unconnected", help="Find unconnected pins and issues"
    )
    sch_unconnected.add_argument("schematic", help="Path to .kicad_sch file")
    sch_unconnected.add_argument("--format", choices=["table", "json"], default="table")
    sch_unconnected.add_argument("--filter", dest="pattern", help="Filter by symbol reference")
    sch_unconnected.add_argument(
        "--include-power", action="store_true", help="Include power symbols"
    )
    sch_unconnected.add_argument("--include-dnp", action="store_true", help="Include DNP symbols")

    # sch replace
    sch_replace = sch_subparsers.add_parser("replace", help="Replace a symbol's library ID")
    sch_replace.add_argument("schematic", help="Path to .kicad_sch file")
    sch_replace.add_argument("reference", help="Symbol reference to replace (e.g., U1)")
    sch_replace.add_argument("new_lib_id", help="New library ID (e.g., 'mylib:NewSymbol')")
    sch_replace.add_argument("--value", help="New value for the symbol")
    sch_replace.add_argument("--footprint", help="New footprint")
    sch_replace.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    sch_replace.add_argument("--backup", action="store_true", help="Create backup before modifying")

    # sch sync-hierarchy
    sch_sync = sch_subparsers.add_parser(
        "sync-hierarchy", help="Synchronize sheet pins and hierarchical labels"
    )
    sch_sync.add_argument("schematic", help="Path to root .kicad_sch file")
    sch_sync.add_argument(
        "--add-labels",
        action="store_true",
        help="Add missing hierarchical labels to child sheets",
    )
    sch_sync.add_argument(
        "--remove-orphan-pins",
        action="store_true",
        help="Remove sheet pins that have no matching labels",
    )
    sch_sync.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Interactive mode - prompt for each action",
    )
    sch_sync.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview changes without modifying files",
    )
    sch_sync.add_argument("--format", choices=["text", "json"], default="text")
    sch_sync.add_argument("--sheet", help="Focus on a specific sheet")

    # sch rename-signal
    sch_rename_signal = sch_subparsers.add_parser(
        "rename-signal", help="Rename a signal across the hierarchy"
    )
    sch_rename_signal.add_argument("schematic", help="Path to root .kicad_sch file")
    sch_rename_signal.add_argument(
        "--from", dest="old_name", required=True, help="Current signal name to rename"
    )
    sch_rename_signal.add_argument("--to", dest="new_name", required=True, help="New signal name")
    sch_rename_signal.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying files"
    )
    sch_rename_signal.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )
    sch_rename_signal.add_argument(
        "--include-nets", action="store_true", help="Also rename matching net labels"
    )
    sch_rename_signal.add_argument(
        "--include-globals", action="store_true", help="Also rename matching global labels"
    )
    sch_rename_signal.add_argument("--format", choices=["text", "json"], default="text")


def _add_pcb_parser(subparsers) -> None:
    """Add PCB subcommand parser with its subcommands."""
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


def _add_lib_parser(subparsers) -> None:
    """Add library subcommand parser with its subcommands."""
    lib_parser = subparsers.add_parser("lib", help="Symbol and footprint library tools")
    lib_subparsers = lib_parser.add_subparsers(dest="lib_command", help="Library commands")

    # lib list
    lib_list = lib_subparsers.add_parser("list", help="List available KiCad libraries")
    lib_list.add_argument("--symbols", action="store_true", help="List only symbol libraries")
    lib_list.add_argument("--footprints", action="store_true", help="List only footprint libraries")
    lib_list.add_argument("--format", choices=["table", "json"], default="table")

    # lib symbols
    lib_symbols = lib_subparsers.add_parser("symbols", help="List symbols in library")
    lib_symbols.add_argument("library", help="Path to .kicad_sym file")
    lib_symbols.add_argument("--format", choices=["table", "json"], default="table")
    lib_symbols.add_argument("--pins", action="store_true", help="Show pin details")

    # lib footprints
    lib_footprints = lib_subparsers.add_parser(
        "footprints", help="List footprints in a .pretty library directory"
    )
    lib_footprints.add_argument("directory", help="Path to .pretty directory")
    lib_footprints.add_argument("--format", choices=["table", "json"], default="table")

    # lib footprint
    lib_footprint = lib_subparsers.add_parser("footprint", help="Show details of a footprint file")
    lib_footprint.add_argument("file", help="Path to .kicad_mod file")
    lib_footprint.add_argument("--format", choices=["text", "json"], default="text")
    lib_footprint.add_argument("--pads", action="store_true", help="Show pad details")

    # lib symbol-info
    lib_symbol_info = lib_subparsers.add_parser(
        "symbol-info", help="Show details of a symbol by name"
    )
    lib_symbol_info.add_argument("library", help="Library name or path to .kicad_sym file")
    lib_symbol_info.add_argument("name", help="Symbol name")
    lib_symbol_info.add_argument("--format", choices=["text", "json"], default="text")
    lib_symbol_info.add_argument("--pins", action="store_true", help="Show pin details")

    # lib footprint-info
    lib_footprint_info = lib_subparsers.add_parser(
        "footprint-info", help="Show details of a footprint by name"
    )
    lib_footprint_info.add_argument("library", help="Library name or path to .pretty directory")
    lib_footprint_info.add_argument("name", help="Footprint name")
    lib_footprint_info.add_argument("--format", choices=["text", "json"], default="text")
    lib_footprint_info.add_argument("--pads", action="store_true", help="Show pad details")

    # lib create-symbol-lib
    lib_create_sym = lib_subparsers.add_parser(
        "create-symbol-lib", help="Create new symbol library (not yet implemented)"
    )
    lib_create_sym.add_argument("path", help="Path for new .kicad_sym file")

    # lib create-footprint-lib
    lib_create_fp = lib_subparsers.add_parser(
        "create-footprint-lib", help="Create new footprint library (not yet implemented)"
    )
    lib_create_fp.add_argument("path", help="Path for new .pretty directory")

    # lib generate-footprint
    lib_generate = lib_subparsers.add_parser(
        "generate-footprint", help="Generate parametric footprint (not yet implemented)"
    )
    lib_generate.add_argument("library", help="Target library path")
    lib_generate.add_argument(
        "type", choices=["soic", "qfp", "qfn", "dfn", "chip", "sot"], help="Footprint type"
    )
    lib_generate.add_argument("--pins", type=int, help="Number of pins")
    lib_generate.add_argument("--pitch", type=float, help="Pin pitch in mm")
    lib_generate.add_argument("--body-width", type=float, help="Body width in mm")
    lib_generate.add_argument("--body-size", type=float, help="Body size (square) in mm")
    lib_generate.add_argument("--prefix", help="Footprint name prefix")

    # lib export
    lib_export = lib_subparsers.add_parser("export", help="Export library to JSON")
    lib_export.add_argument("path", help="Path to library file or item")
    lib_export.add_argument("--format", choices=["json"], default="json")


def _add_footprint_parser(subparsers) -> None:
    """Add footprint subcommand parser."""
    footprint_parser = subparsers.add_parser("footprint", help="Footprint generation and tools")
    footprint_subparsers = footprint_parser.add_subparsers(
        dest="footprint_command", help="Footprint commands"
    )

    # footprint generate - delegates to footprint_generate.py for subcommands
    footprint_subparsers.add_parser(
        "generate",
        help="Generate parametric footprints",
        add_help=False,  # Let footprint_generate handle help
    )


def _add_mfr_parser(subparsers) -> None:
    """Add manufacturer subcommand parser with its subcommands."""
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

    # mfr export-dru
    mfr_export_dru = mfr_subparsers.add_parser(
        "export-dru", help="Export manufacturer rules as KiCad DRU file"
    )
    mfr_export_dru.add_argument("manufacturer", help="Manufacturer ID (jlcpcb, seeed, etc.)")
    mfr_export_dru.add_argument("-l", "--layers", type=int, default=4, help="Layer count")
    mfr_export_dru.add_argument(
        "-c", "--copper", type=float, default=1.0, help="Copper weight (oz)"
    )
    mfr_export_dru.add_argument("-o", "--output", type=str, help="Output file path")

    # mfr import-dru
    mfr_import_dru = mfr_subparsers.add_parser(
        "import-dru", help="Parse and display a KiCad design rules file"
    )
    mfr_import_dru.add_argument("file", help="Path to .kicad_dru file")
    mfr_import_dru.add_argument("--format", choices=["text", "json"], default="text")


def _add_zones_parser(subparsers) -> None:
    """Add zones subcommand parser with its subcommands."""
    zones_parser = subparsers.add_parser("zones", help="Add copper pour zones to PCB")
    zones_subparsers = zones_parser.add_subparsers(dest="zones_command", help="Zone commands")

    # zones add
    zones_add = zones_subparsers.add_parser("add", help="Add a copper zone")
    zones_add.add_argument("pcb", help="Path to .kicad_pcb file")
    zones_add.add_argument("-o", "--output", help="Output file path")
    zones_add.add_argument("--net", required=True, help="Net name (e.g., GND, +3.3V)")
    zones_add.add_argument("--layer", required=True, help="Copper layer (e.g., B.Cu, F.Cu)")
    zones_add.add_argument("--priority", type=int, default=0, help="Zone fill priority")
    zones_add.add_argument("--clearance", type=float, default=0.3, help="Clearance in mm")
    zones_add.add_argument("--thermal-gap", type=float, default=0.3, help="Thermal gap in mm")
    zones_add.add_argument(
        "--thermal-bridge", type=float, default=0.4, help="Thermal bridge width in mm"
    )
    zones_add.add_argument("--min-thickness", type=float, default=0.25, help="Min thickness in mm")
    zones_add.add_argument("-v", "--verbose", action="store_true")
    zones_add.add_argument("--dry-run", action="store_true")

    # zones list
    zones_list = zones_subparsers.add_parser("list", help="List existing zones")
    zones_list.add_argument("pcb", help="Path to .kicad_pcb file")
    zones_list.add_argument("--format", choices=["text", "json"], default="text")

    # zones batch
    zones_batch = zones_subparsers.add_parser("batch", help="Add multiple zones from spec")
    zones_batch.add_argument("pcb", help="Path to .kicad_pcb file")
    zones_batch.add_argument("-o", "--output", help="Output file path")
    zones_batch.add_argument(
        "--power-nets",
        required=True,
        help="Power nets spec: 'NET:LAYER,...' (e.g., 'GND:B.Cu,+3.3V:F.Cu')",
    )
    zones_batch.add_argument("--clearance", type=float, default=0.3, help="Clearance in mm")
    zones_batch.add_argument("-v", "--verbose", action="store_true")
    zones_batch.add_argument("--dry-run", action="store_true")


def _add_route_parser(subparsers) -> None:
    """Add route subcommand parser."""
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
    route_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")
    route_parser.add_argument(
        "--power-nets",
        help="Generate zones: 'NET:LAYER,...' (e.g., 'GND:B.Cu,+3.3V:F.Cu')",
    )


def _add_reason_parser(subparsers) -> None:
    """Add reason subcommand parser."""
    reason_parser = subparsers.add_parser("reason", help="LLM-driven PCB layout reasoning")
    reason_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    reason_parser.add_argument("-o", "--output", help="Output file path")
    reason_parser.add_argument(
        "--export-state",
        action="store_true",
        help="Export state as JSON for external LLM",
    )
    reason_parser.add_argument("--state-output", help="Output path for state JSON")
    reason_parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run interactive reasoning loop",
    )
    reason_parser.add_argument(
        "--analyze",
        action="store_true",
        help="Print detailed analysis of PCB state",
    )
    reason_parser.add_argument(
        "--auto-route",
        action="store_true",
        help="Auto-route priority nets without LLM",
    )
    reason_parser.add_argument(
        "--max-nets",
        type=int,
        default=10,
        help="Maximum nets to auto-route (default: 10)",
    )
    reason_parser.add_argument("--drc", help="Path to DRC report file")
    reason_parser.add_argument("-v", "--verbose", action="store_true")
    reason_parser.add_argument("--dry-run", action="store_true", help="Don't write output")


def _add_optimize_parser(subparsers) -> None:
    """Add optimize-traces subcommand parser."""
    optimize_parser = subparsers.add_parser("optimize-traces", help="Optimize PCB traces")
    optimize_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    optimize_parser.add_argument("-o", "--output", help="Output file (default: modify in place)")
    optimize_parser.add_argument("--net", help="Only optimize traces matching this net pattern")
    optimize_parser.add_argument(
        "--no-merge", action="store_true", help="Disable collinear merging"
    )
    optimize_parser.add_argument(
        "--no-zigzag", action="store_true", help="Disable zigzag elimination"
    )
    optimize_parser.add_argument("--no-45", action="store_true", help="Disable 45-degree corners")
    optimize_parser.add_argument(
        "--chamfer-size",
        type=float,
        default=0.5,
        help="45-degree chamfer size in mm (default: 0.5)",
    )
    optimize_parser.add_argument("-v", "--verbose", action="store_true")
    optimize_parser.add_argument(
        "--dry-run", action="store_true", help="Show results without writing"
    )
    optimize_parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )


def _add_validate_footprints_parser(subparsers) -> None:
    """Add validate-footprints subcommand parser."""
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
    # Standard library comparison options
    validate_fp_parser.add_argument(
        "--compare-standard",
        action="store_true",
        help="Compare footprints against KiCad standard library",
    )
    validate_fp_parser.add_argument(
        "--tolerance",
        type=float,
        default=0.05,
        help="Tolerance for standard comparison in mm (default: 0.05)",
    )
    validate_fp_parser.add_argument(
        "--kicad-library-path",
        type=str,
        default=None,
        help="Override path to KiCad footprint libraries",
    )


def _add_fix_footprints_parser(subparsers) -> None:
    """Add fix-footprints subcommand parser."""
    fix_fp_parser = subparsers.add_parser("fix-footprints", help="Fix footprint pad spacing issues")
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


def _add_parts_parser(subparsers) -> None:
    """Add parts subcommand parser with its subcommands."""
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

    # parts availability
    parts_avail = parts_subparsers.add_parser(
        "availability", help="Check BOM part availability on LCSC"
    )
    parts_avail.add_argument("schematic", help="Path to .kicad_sch file")
    parts_avail.add_argument(
        "--quantity", "-q", type=int, default=1, help="Number of boards to manufacture (default: 1)"
    )
    parts_avail.add_argument("--format", choices=["table", "json", "summary"], default="table")
    parts_avail.add_argument(
        "--no-alternatives", action="store_true", help="Don't search for alternative parts"
    )
    parts_avail.add_argument(
        "--issues-only", action="store_true", help="Only show parts with availability issues"
    )

    # parts cache
    parts_cache = parts_subparsers.add_parser("cache", help="Cache management")
    parts_cache.add_argument(
        "cache_action",
        nargs="?",
        choices=["stats", "clear", "clear-expired"],
        default="stats",
        help="Cache action (default: stats)",
    )


def _add_datasheet_parser(subparsers) -> None:
    """Add datasheet subcommand parser with its subcommands."""
    datasheet_parser = subparsers.add_parser(
        "datasheet", help="Datasheet search, download, and PDF parsing"
    )
    datasheet_subparsers = datasheet_parser.add_subparsers(
        dest="datasheet_command", help="Datasheet commands"
    )

    # datasheet search
    datasheet_search = datasheet_subparsers.add_parser("search", help="Search for datasheets")
    datasheet_search.add_argument("part", help="Part number to search for")
    datasheet_search.add_argument("--format", choices=["text", "json"], default="text")
    datasheet_search.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")

    # datasheet download
    datasheet_download = datasheet_subparsers.add_parser("download", help="Download a datasheet")
    datasheet_download.add_argument("part", help="Part number to download")
    datasheet_download.add_argument("-o", "--output", help="Output directory (default: cache)")
    datasheet_download.add_argument(
        "--force", action="store_true", help="Force download even if cached"
    )

    # datasheet list
    datasheet_list = datasheet_subparsers.add_parser("list", help="List cached datasheets")
    datasheet_list.add_argument("--format", choices=["text", "json"], default="text")

    # datasheet cache
    datasheet_cache = datasheet_subparsers.add_parser("cache", help="Cache management")
    datasheet_cache.add_argument(
        "cache_action",
        nargs="?",
        choices=["stats", "clear", "clear-expired"],
        default="stats",
        help="Cache action (default: stats)",
    )
    datasheet_cache.add_argument(
        "--older-than", type=int, help="For clear: only clear entries older than N days"
    )

    # datasheet convert
    ds_convert = datasheet_subparsers.add_parser("convert", help="Convert PDF to markdown")
    ds_convert.add_argument("pdf", help="Path to PDF file")
    ds_convert.add_argument("-o", "--output", help="Output file path (default: stdout)")
    ds_convert.add_argument("--pages", help="Page range (e.g., '1-10' or '1,2,5')")

    # datasheet extract-images
    ds_images = datasheet_subparsers.add_parser("extract-images", help="Extract images from PDF")
    ds_images.add_argument("pdf", help="Path to PDF file")
    ds_images.add_argument("-o", "--output", required=True, help="Output directory for images")
    ds_images.add_argument("--pages", help="Page range (e.g., '1-10' or '1,2,5')")
    ds_images.add_argument(
        "--min-size", type=int, default=100, help="Minimum image dimension (default: 100)"
    )
    ds_images.add_argument("--format", choices=["text", "json"], default="text")

    # datasheet extract-tables
    ds_tables = datasheet_subparsers.add_parser("extract-tables", help="Extract tables from PDF")
    ds_tables.add_argument("pdf", help="Path to PDF file")
    ds_tables.add_argument("-o", "--output", help="Output directory for tables")
    ds_tables.add_argument("--pages", help="Page range (e.g., '1-10' or '1,2,5')")
    ds_tables.add_argument("--format", choices=["markdown", "csv", "json"], default="markdown")

    # datasheet info
    ds_info = datasheet_subparsers.add_parser("info", help="Show PDF information")
    ds_info.add_argument("pdf", help="Path to PDF file")
    ds_info.add_argument("--format", choices=["text", "json"], default="text")


def _add_placement_parser(subparsers) -> None:
    """Add placement subcommand parser with its subcommands."""
    placement_parser = subparsers.add_parser("placement", help="Detect and fix placement conflicts")
    placement_subparsers = placement_parser.add_subparsers(
        dest="placement_command", help="Placement commands"
    )

    # placement check
    placement_check = placement_subparsers.add_parser(
        "check", help="Check PCB for placement conflicts"
    )
    placement_check.add_argument("pcb", help="Path to .kicad_pcb file")
    placement_check.add_argument("--format", choices=["table", "json", "summary"], default="table")
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
    placement_check.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )
    placement_check.add_argument(
        "--signal-integrity",
        action="store_true",
        help="Analyze signal integrity and show placement hints",
    )

    # placement fix
    placement_fix = placement_subparsers.add_parser("fix", help="Suggest and apply placement fixes")
    placement_fix.add_argument("pcb", help="Path to .kicad_pcb file")
    placement_fix.add_argument("-o", "--output", help="Output file path")
    placement_fix.add_argument(
        "--strategy",
        choices=["spread", "compact", "anchor"],
        default="spread",
        help="Fix strategy",
    )
    placement_fix.add_argument("--anchor", help="Comma-separated components to keep fixed")
    placement_fix.add_argument("--dry-run", action="store_true")
    placement_fix.add_argument("-v", "--verbose", action="store_true")
    placement_fix.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )

    # placement optimize
    placement_optimize = placement_subparsers.add_parser(
        "optimize", help="Optimize placement for routability"
    )
    placement_optimize.add_argument("pcb", help="Path to .kicad_pcb file")
    placement_optimize.add_argument("-o", "--output", help="Output file path")
    placement_optimize.add_argument(
        "--strategy",
        choices=["force-directed", "evolutionary", "hybrid"],
        default="force-directed",
        help="Optimization strategy (default: force-directed)",
    )
    placement_optimize.add_argument(
        "--iterations",
        type=int,
        default=1000,
        help="Max iterations for physics simulation (default: 1000)",
    )
    placement_optimize.add_argument(
        "--generations",
        type=int,
        default=100,
        help="Generations for evolutionary/hybrid mode (default: 100)",
    )
    placement_optimize.add_argument(
        "--population",
        type=int,
        default=50,
        help="Population size for evolutionary/hybrid mode (default: 50)",
    )
    placement_optimize.add_argument(
        "--grid",
        type=float,
        default=0.0,
        help="Position grid snap in mm (0 to disable, default: 0)",
    )
    placement_optimize.add_argument(
        "--fixed",
        help="Comma-separated component refs to keep fixed (e.g., J1,J2,H1)",
    )
    placement_optimize.add_argument(
        "--cluster",
        action="store_true",
        help="Enable functional clustering (groups bypass caps near ICs, etc.)",
    )
    placement_optimize.add_argument(
        "--constraints",
        help="Path to YAML file with grouping constraints",
    )
    placement_optimize.add_argument(
        "--keepout",
        metavar="FILE",
        help="YAML file defining keepout zones",
    )
    placement_optimize.add_argument(
        "--auto-keepout",
        action="store_true",
        dest="auto_keepout",
        help="Auto-detect keepout zones from mounting holes and connectors",
    )
    placement_optimize.add_argument(
        "--edge-detect",
        action="store_true",
        help="Auto-detect edge components (connectors, mounting holes, etc.)",
    )
    placement_optimize.add_argument(
        "--thermal",
        action="store_true",
        help="Enable thermal-aware placement (keeps heat sources away from sensitive components)",
    )
    placement_optimize.add_argument("--dry-run", action="store_true", help="Preview only")
    placement_optimize.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    placement_optimize.add_argument("-v", "--verbose", action="store_true")
    placement_optimize.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )

    # placement snap
    placement_snap = placement_subparsers.add_parser("snap", help="Snap components to grid")
    placement_snap.add_argument("pcb", help="Path to .kicad_pcb file")
    placement_snap.add_argument(
        "-o", "--output", help="Output file path (default: modify in place)"
    )
    placement_snap.add_argument(
        "--grid",
        type=float,
        default=0.5,
        help="Grid size in mm (default: 0.5)",
    )
    placement_snap.add_argument(
        "--rotation",
        type=int,
        default=90,
        help="Rotation snap in degrees (0 to disable, default: 90)",
    )
    placement_snap.add_argument("--dry-run", action="store_true", help="Preview without saving")
    placement_snap.add_argument("-v", "--verbose", action="store_true")
    placement_snap.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )

    # placement align
    placement_align = placement_subparsers.add_parser(
        "align", help="Align components in row or column"
    )
    placement_align.add_argument("pcb", help="Path to .kicad_pcb file")
    placement_align.add_argument(
        "-o", "--output", help="Output file path (default: modify in place)"
    )
    placement_align.add_argument(
        "--components",
        "-c",
        required=True,
        help="Comma-separated component refs to align (e.g., R1,R2,R3)",
    )
    placement_align.add_argument(
        "--axis",
        choices=["row", "column"],
        default="row",
        help="Alignment axis: row (horizontal) or column (vertical) (default: row)",
    )
    placement_align.add_argument(
        "--reference",
        choices=["center", "top", "bottom", "left", "right"],
        default="center",
        help="Alignment reference point (default: center)",
    )
    placement_align.add_argument(
        "--tolerance",
        type=float,
        default=0.1,
        help="Tolerance for already-aligned components in mm (default: 0.1)",
    )
    placement_align.add_argument("--dry-run", action="store_true", help="Preview without saving")
    placement_align.add_argument("-v", "--verbose", action="store_true")
    placement_align.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )

    # placement distribute
    placement_distribute = placement_subparsers.add_parser(
        "distribute", help="Distribute components evenly"
    )
    placement_distribute.add_argument("pcb", help="Path to .kicad_pcb file")
    placement_distribute.add_argument(
        "-o", "--output", help="Output file path (default: modify in place)"
    )
    placement_distribute.add_argument(
        "--components",
        "-c",
        required=True,
        help="Comma-separated component refs to distribute (e.g., LED1,LED2,LED3,LED4)",
    )
    placement_distribute.add_argument(
        "--axis",
        choices=["horizontal", "vertical"],
        default="horizontal",
        help="Distribution axis (default: horizontal)",
    )
    placement_distribute.add_argument(
        "--spacing",
        type=float,
        default=0.0,
        help="Fixed spacing in mm (0 for automatic even distribution, default: 0)",
    )
    placement_distribute.add_argument(
        "--dry-run", action="store_true", help="Preview without saving"
    )
    placement_distribute.add_argument("-v", "--verbose", action="store_true")
    placement_distribute.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )

    # placement suggest
    placement_suggest = placement_subparsers.add_parser(
        "suggest", help="Generate placement suggestions with rationale"
    )
    placement_suggest.add_argument("pcb", help="Path to .kicad_pcb file")
    placement_suggest.add_argument(
        "--component",
        "-c",
        help="Explain placement for specific component reference",
    )
    placement_suggest.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    placement_suggest.add_argument("-v", "--verbose", action="store_true")
    placement_suggest.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )

    # placement refine
    placement_refine = placement_subparsers.add_parser(
        "refine", help="Interactive placement refinement session"
    )
    placement_refine.add_argument("pcb", help="Path to .kicad_pcb file")
    placement_refine.add_argument(
        "-o", "--output", help="Output file path (default: modify in place)"
    )
    placement_refine.add_argument(
        "--fixed",
        help="Comma-separated component refs to keep fixed (e.g., J1,J2,H1)",
    )
    placement_refine.add_argument(
        "--json",
        action="store_true",
        help="JSON API mode (read commands from stdin, write responses to stdout)",
    )
    placement_refine.add_argument("-v", "--verbose", action="store_true")
    placement_refine.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )


def _add_config_parser(subparsers) -> None:
    """Add config subcommand parser."""
    config_parser = subparsers.add_parser("config", help="View and manage configuration")
    config_parser.add_argument(
        "--show",
        action="store_true",
        help="Show effective configuration with sources",
    )
    config_parser.add_argument(
        "--init",
        action="store_true",
        help="Create template config file",
    )
    config_parser.add_argument(
        "--paths",
        action="store_true",
        help="Show config file paths",
    )
    config_parser.add_argument(
        "--user",
        action="store_true",
        help="Use user config for --init",
    )
    config_parser.add_argument(
        "config_action",
        nargs="?",
        choices=["get", "set"],
        help="Config action",
    )
    config_parser.add_argument(
        "config_key",
        nargs="?",
        help="Config key (e.g., defaults.format)",
    )
    config_parser.add_argument(
        "config_value",
        nargs="?",
        help="Value to set",
    )


def _add_interactive_parser(subparsers) -> None:
    """Add interactive subcommand parser."""
    interactive_parser = subparsers.add_parser("interactive", help="Launch interactive REPL mode")
    interactive_parser.add_argument(
        "--project",
        help="Auto-load a project on startup",
    )


def _add_validate_parser(subparsers) -> None:
    """Add validate subcommand parser."""
    validate_parser = subparsers.add_parser("validate", help="Validation tools")
    validate_parser.add_argument(
        "validate_files",
        nargs="*",
        help="Path(s) to project/schematic/PCB files. Accepts: .kicad_pro, .kicad_sch, .kicad_pcb",
    )
    validate_parser.add_argument(
        "--sync",
        action="store_true",
        help="Check schematic-to-PCB netlist synchronization",
    )
    validate_parser.add_argument(
        "--connectivity",
        action="store_true",
        help="Check net connectivity on PCB (detect unrouted nets)",
    )
    validate_parser.add_argument(
        "--consistency",
        action="store_true",
        help="Check schematic-to-PCB consistency (components, nets, properties)",
    )
    validate_parser.add_argument(
        "--placement",
        action="store_true",
        help="Check BOM components are placed on PCB",
    )
    validate_parser.add_argument(
        "--schematic",
        "-s",
        dest="validate_schematic",
        help="Path to .kicad_sch file (if not using project file)",
    )
    validate_parser.add_argument(
        "--pcb",
        "-p",
        dest="validate_pcb",
        help="Path to .kicad_pcb file (if not using project file)",
    )
    validate_parser.add_argument(
        "--format",
        dest="validate_format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format (default: table)",
    )
    validate_parser.add_argument(
        "--errors-only",
        dest="validate_errors_only",
        action="store_true",
        help="Show only errors, not warnings",
    )
    validate_parser.add_argument(
        "--strict",
        dest="validate_strict",
        action="store_true",
        help="Exit with error code on warnings",
    )
    validate_parser.add_argument(
        "-v",
        "--verbose",
        dest="validate_verbose",
        action="store_true",
        help="Show detailed issue information",
    )


def _add_analyze_parser(subparsers) -> None:
    """Add analyze subcommand parser with its subcommands."""
    analyze_parser = subparsers.add_parser("analyze", help="PCB analysis tools")
    analyze_subparsers = analyze_parser.add_subparsers(
        dest="analyze_command", help="Analysis commands"
    )

    # analyze congestion
    congestion_parser = analyze_subparsers.add_parser(
        "congestion",
        help="Analyze routing congestion",
        description="Identify congested areas and suggest solutions",
    )
    congestion_parser.add_argument("pcb", help="PCB file to analyze (.kicad_pcb)")
    congestion_parser.add_argument(
        "--format",
        "-f",
        dest="analyze_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    congestion_parser.add_argument(
        "--grid-size",
        dest="analyze_grid_size",
        type=float,
        default=2.0,
        help="Grid cell size in mm (default: 2.0)",
    )
    congestion_parser.add_argument(
        "--min-severity",
        dest="analyze_min_severity",
        choices=["low", "medium", "high", "critical"],
        default="low",
        help="Minimum severity to report (default: low)",
    )

    # analyze trace-lengths
    trace_parser = analyze_subparsers.add_parser(
        "trace-lengths",
        help="Analyze trace lengths for timing-critical nets",
        description="Calculate trace lengths, identify differential pairs, and check skew",
    )
    trace_parser.add_argument("pcb", help="PCB file to analyze (.kicad_pcb)")
    trace_parser.add_argument(
        "--format",
        "-f",
        dest="analyze_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    trace_parser.add_argument(
        "--net",
        "-n",
        action="append",
        dest="analyze_nets",
        help="Specific net(s) to analyze (can be used multiple times)",
    )
    trace_parser.add_argument(
        "--all",
        "-a",
        dest="analyze_all",
        action="store_true",
        help="Analyze all nets, not just timing-critical ones",
    )
    trace_parser.add_argument(
        "--diff-pairs",
        "-d",
        dest="analyze_diff_pairs",
        action="store_true",
        default=True,
        help="Include differential pair analysis (default: True)",
    )
    trace_parser.add_argument(
        "--no-diff-pairs",
        action="store_false",
        dest="analyze_diff_pairs",
        help="Disable differential pair analysis",
    )

    # analyze signal-integrity
    si_parser = analyze_subparsers.add_parser(
        "signal-integrity",
        help="Analyze signal integrity (crosstalk and impedance)",
        description="Identify crosstalk risks and impedance discontinuities",
    )
    si_parser.add_argument("pcb", help="PCB file to analyze (.kicad_pcb)")
    si_parser.add_argument(
        "--format",
        "-f",
        dest="analyze_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    si_parser.add_argument(
        "--min-risk",
        dest="analyze_min_risk",
        choices=["low", "medium", "high"],
        default="medium",
        help="Minimum risk level to report for crosstalk (default: medium)",
    )
    si_parser.add_argument(
        "--crosstalk-only",
        dest="analyze_crosstalk_only",
        action="store_true",
        help="Only analyze crosstalk, skip impedance analysis",
    )
    si_parser.add_argument(
        "--impedance-only",
        dest="analyze_impedance_only",
        action="store_true",
        help="Only analyze impedance, skip crosstalk analysis",
    )

    # analyze thermal
    thermal_parser = analyze_subparsers.add_parser(
        "thermal",
        help="Analyze thermal characteristics and hotspots",
        description="Identify heat sources, estimate power dissipation, and suggest thermal improvements",
    )
    thermal_parser.add_argument("pcb", help="PCB file to analyze (.kicad_pcb)")
    thermal_parser.add_argument(
        "--format",
        "-f",
        dest="analyze_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    thermal_parser.add_argument(
        "--cluster-radius",
        dest="analyze_cluster_radius",
        type=float,
        default=10.0,
        help="Radius for clustering heat sources in mm (default: 10.0)",
    )
    thermal_parser.add_argument(
        "--min-power",
        dest="analyze_min_power",
        type=float,
        default=0.05,
        help="Minimum power threshold in Watts (default: 0.05)",
    )


def _add_constraints_parser(subparsers) -> None:
    """Add constraints subcommand parser with its subcommands."""
    constraints_parser = subparsers.add_parser(
        "constraints", help="Constraint conflict detection and management"
    )
    constraints_subparsers = constraints_parser.add_subparsers(
        dest="constraints_command", help="Constraints commands"
    )

    # constraints check
    constraints_check = constraints_subparsers.add_parser(
        "check", help="Detect conflicts between constraints"
    )
    constraints_check.add_argument("pcb", help="Path to .kicad_pcb file")
    constraints_check.add_argument(
        "--format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format (default: table)",
    )
    constraints_check.add_argument(
        "--keepout",
        metavar="FILE",
        help="YAML file defining keepout zones",
    )
    constraints_check.add_argument(
        "--constraints",
        metavar="FILE",
        dest="constraints_file",
        help="YAML file with grouping constraints",
    )
    constraints_check.add_argument(
        "--auto-keepout",
        action="store_true",
        dest="auto_keepout",
        help="Auto-detect keepout zones from mounting holes and connectors",
    )
    constraints_check.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed conflict information",
    )


def _add_estimate_parser(subparsers) -> None:
    """Add estimate subcommand parser with its subcommands."""
    estimate_parser = subparsers.add_parser("estimate", help="Manufacturing cost estimation")
    estimate_subparsers = estimate_parser.add_subparsers(
        dest="estimate_command", help="Estimate commands"
    )

    # estimate cost
    estimate_cost = estimate_subparsers.add_parser("cost", help="Estimate manufacturing costs")
    estimate_cost.add_argument("pcb", help="Path to .kicad_pcb file")
    estimate_cost.add_argument(
        "--bom",
        help="Path to BOM file (.csv) or schematic (.kicad_sch) for component costs",
    )
    estimate_cost.add_argument(
        "--quantity",
        "-q",
        type=int,
        default=10,
        help="Number of boards to manufacture (default: 10)",
    )
    estimate_cost.add_argument(
        "--mfr",
        "-m",
        choices=["jlcpcb", "pcbway", "seeed", "oshpark"],
        default="jlcpcb",
        help="Target manufacturer (default: jlcpcb)",
    )
    estimate_cost.add_argument(
        "--format",
        "-f",
        dest="estimate_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    estimate_cost.add_argument(
        "--finish",
        choices=["hasl", "hasl_lead_free", "enig", "osp"],
        default="hasl",
        help="Surface finish (default: hasl)",
    )
    estimate_cost.add_argument(
        "--color",
        choices=["green", "red", "blue", "black", "white", "yellow"],
        default="green",
        help="Solder mask color (default: green)",
    )
    estimate_cost.add_argument(
        "--layers",
        "-l",
        type=int,
        help="Layer count override (auto-detected from PCB if not specified)",
    )
    estimate_cost.add_argument(
        "--thickness",
        type=float,
        default=1.6,
        help="Board thickness in mm (default: 1.6)",
    )
    estimate_cost.add_argument(
        "-v", "--verbose", action="store_true", help="Show detailed breakdown"
    )


def _add_audit_parser(subparsers) -> None:
    """Add audit subcommand parser for manufacturing readiness audit."""
    audit_parser = subparsers.add_parser(
        "audit",
        help="Manufacturing readiness audit (ERC, DRC, connectivity, compatibility)",
    )
    audit_parser.add_argument(
        "audit_project",
        help="Path to .kicad_pro or .kicad_pcb file",
    )
    audit_parser.add_argument(
        "--format",
        dest="audit_format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format (default: table)",
    )
    audit_parser.add_argument(
        "--mfr",
        "-m",
        dest="audit_mfr",
        choices=["jlcpcb", "pcbway", "oshpark", "seeed"],
        default="jlcpcb",
        help="Target manufacturer (default: jlcpcb)",
    )
    audit_parser.add_argument(
        "--layers",
        "-l",
        dest="audit_layers",
        type=int,
        help="Layer count (auto-detected if not specified)",
    )
    audit_parser.add_argument(
        "--copper",
        "-c",
        dest="audit_copper",
        type=float,
        default=1.0,
        help="Copper weight in oz (default: 1.0)",
    )
    audit_parser.add_argument(
        "--quantity",
        "-q",
        dest="audit_quantity",
        type=int,
        default=5,
        help="Quantity for cost estimate (default: 5)",
    )
    audit_parser.add_argument(
        "--skip-erc",
        dest="audit_skip_erc",
        action="store_true",
        help="Skip ERC check (for PCB-only audits)",
    )
    audit_parser.add_argument(
        "--strict",
        dest="audit_strict",
        action="store_true",
        help="Exit with code 2 on warnings",
    )
    audit_parser.add_argument(
        "-v",
        "--verbose",
        dest="audit_verbose",
        action="store_true",
        help="Show detailed information",
    )


def _add_suggest_parser(subparsers) -> None:
    """Add suggest subcommand parser with its subcommands."""
    suggest_parser = subparsers.add_parser("suggest", help="Part suggestions and recommendations")
    suggest_subparsers = suggest_parser.add_subparsers(
        dest="suggest_command", help="Suggest commands"
    )

    # suggest alternatives
    suggest_alt = suggest_subparsers.add_parser(
        "alternatives", help="Suggest alternative parts for unavailable or expensive components"
    )
    suggest_alt.add_argument(
        "schematic",
        help="Path to .kicad_sch file (or BOM CSV with --bom flag)",
    )
    suggest_alt.add_argument(
        "--bom",
        action="store_true",
        help="Treat input as CSV BOM file instead of schematic",
    )
    suggest_alt.add_argument(
        "--max-alternatives",
        "-n",
        type=int,
        default=3,
        help="Maximum alternatives per part (default: 3)",
    )
    suggest_alt.add_argument(
        "--format",
        "-f",
        dest="suggest_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    suggest_alt.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass parts cache (fetch fresh data)",
    )
    suggest_alt.add_argument(
        "--show-all",
        action="store_true",
        help="Show alternatives for all parts, not just problematic ones",
    )
    suggest_alt.add_argument(
        "-v", "--verbose", action="store_true", help="Show detailed information"
    )


def _add_net_status_parser(subparsers) -> None:
    """Add net-status subcommand parser."""
    net_status_parser = subparsers.add_parser(
        "net-status",
        help="Report net connectivity status for a PCB",
        description="Show which nets are complete, incomplete, or unrouted with details on unconnected pads",
    )
    net_status_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    net_status_parser.add_argument(
        "--format",
        dest="net_status_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    net_status_parser.add_argument(
        "--incomplete",
        dest="net_status_incomplete",
        action="store_true",
        help="Show only incomplete nets",
    )
    net_status_parser.add_argument(
        "--net",
        dest="net_status_net",
        help="Show status for a specific net by name",
    )
    net_status_parser.add_argument(
        "--by-class",
        dest="net_status_by_class",
        action="store_true",
        help="Group output by net class",
    )
    net_status_parser.add_argument(
        "-v",
        "--verbose",
        dest="net_status_verbose",
        action="store_true",
        help="Show all pads with coordinates",
    )


def _add_clean_parser(subparsers) -> None:
    """Add clean subcommand parser for project cleanup."""
    clean_parser = subparsers.add_parser(
        "clean",
        help="Clean up old/orphaned files from KiCad projects",
    )
    clean_parser.add_argument(
        "clean_project",
        metavar="project",
        help="Path to .kicad_pro file",
    )
    clean_parser.add_argument(
        "--dry-run",
        dest="clean_dry_run",
        action="store_true",
        help="Show what would be cleaned without deleting (default behavior)",
    )
    clean_parser.add_argument(
        "--deep",
        dest="clean_deep",
        action="store_true",
        help="Also delete generated output files (gerbers, BOM exports, etc.)",
    )
    clean_parser.add_argument(
        "--force",
        "-f",
        dest="clean_force",
        action="store_true",
        help="Delete files without confirmation (for CI/automation)",
    )
    clean_parser.add_argument(
        "--format",
        dest="clean_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    clean_parser.add_argument(
        "-v",
        "--verbose",
        dest="clean_verbose",
        action="store_true",
        help="Show detailed output",
    )


def _add_impedance_parser(subparsers) -> None:
    """Add impedance subcommand parser with its subcommands."""
    impedance_parser = subparsers.add_parser(
        "impedance",
        help="Transmission line impedance calculations",
        description=(
            "Calculate trace impedance, width for target impedance, "
            "differential pair parameters, and crosstalk estimation"
        ),
    )
    impedance_subparsers = impedance_parser.add_subparsers(
        dest="impedance_command", help="Impedance commands"
    )

    # Common arguments function
    def add_common_args(parser, require_board=False):
        """Add common arguments shared by impedance subcommands."""
        if require_board:
            parser.add_argument("pcb", nargs="?", help="Path to .kicad_pcb file")
        else:
            parser.add_argument(
                "pcb", nargs="?", help="Path to .kicad_pcb file (optional if --preset used)"
            )
        parser.add_argument(
            "--preset",
            "-p",
            dest="impedance_preset",
            choices=["jlcpcb-4", "oshpark-4", "generic-2", "generic-4", "generic-6"],
            help="Use a preset stackup instead of reading from PCB",
        )
        parser.add_argument(
            "--format",
            "-f",
            dest="impedance_format",
            choices=["text", "json"],
            default="text",
            help="Output format (default: text)",
        )

    # impedance stackup - Display board stackup
    stackup_parser = impedance_subparsers.add_parser(
        "stackup",
        help="Display parsed board stackup",
        description="Show layer stackup extracted from PCB or from preset",
    )
    add_common_args(stackup_parser)

    # impedance width - Calculate trace width for target impedance
    width_parser = impedance_subparsers.add_parser(
        "width",
        help="Calculate trace width for target impedance",
        description="Find trace width needed to achieve a target characteristic impedance",
    )
    add_common_args(width_parser)
    width_parser.add_argument(
        "--target",
        "-z",
        dest="impedance_target",
        type=float,
        required=True,
        help="Target characteristic impedance in ohms (e.g., 50)",
    )
    width_parser.add_argument(
        "--layer",
        "-l",
        dest="impedance_layer",
        required=True,
        help="Layer name (e.g., F.Cu, In1.Cu)",
    )
    width_parser.add_argument(
        "--mode",
        "-m",
        dest="impedance_mode",
        choices=["auto", "microstrip", "stripline"],
        default="auto",
        help="Transmission line mode (default: auto-detect from layer position)",
    )

    # impedance calculate - Forward impedance calculation
    calc_parser = impedance_subparsers.add_parser(
        "calculate",
        help="Calculate impedance for given geometry",
        description="Forward calculation: given trace width, compute impedance",
    )
    add_common_args(calc_parser)
    calc_parser.add_argument(
        "--width",
        "-w",
        dest="impedance_width",
        type=float,
        required=True,
        help="Trace width in mm",
    )
    calc_parser.add_argument(
        "--layer",
        "-l",
        dest="impedance_layer",
        required=True,
        help="Layer name (e.g., F.Cu, In1.Cu)",
    )
    calc_parser.add_argument(
        "--mode",
        "-m",
        dest="impedance_mode",
        choices=["auto", "microstrip", "stripline", "cpwg"],
        default="auto",
        help="Transmission line mode (default: auto-detect)",
    )
    calc_parser.add_argument(
        "--gap",
        "-g",
        dest="impedance_gap",
        type=float,
        help="Gap to ground for CPWG mode in mm",
    )
    calc_parser.add_argument(
        "--frequency",
        dest="impedance_frequency",
        type=float,
        default=1.0,
        help="Signal frequency in GHz for loss calculation (default: 1.0)",
    )

    # impedance diffpair - Differential pair analysis
    diffpair_parser = impedance_subparsers.add_parser(
        "diffpair",
        help="Analyze differential pair",
        description="Calculate differential and common mode impedances for coupled lines",
    )
    add_common_args(diffpair_parser)
    diffpair_parser.add_argument(
        "--width",
        "-w",
        dest="impedance_width",
        type=float,
        required=True,
        help="Trace width in mm (each trace)",
    )
    diffpair_parser.add_argument(
        "--gap",
        "-g",
        dest="impedance_gap",
        type=float,
        required=True,
        help="Edge-to-edge gap between traces in mm",
    )
    diffpair_parser.add_argument(
        "--layer",
        "-l",
        dest="impedance_layer",
        required=True,
        help="Layer name (e.g., F.Cu, In1.Cu)",
    )
    diffpair_parser.add_argument(
        "--target",
        "-z",
        dest="impedance_target",
        type=float,
        help="Target differential impedance (e.g., 90, 100) for verification",
    )

    # impedance crosstalk - Crosstalk estimation
    crosstalk_parser = impedance_subparsers.add_parser(
        "crosstalk",
        help="Estimate crosstalk between parallel traces",
        description="Calculate NEXT/FEXT crosstalk, or find spacing for budget",
    )
    add_common_args(crosstalk_parser)
    crosstalk_parser.add_argument(
        "--layer",
        "-l",
        dest="impedance_layer",
        required=True,
        help="Layer name (e.g., F.Cu, In1.Cu)",
    )
    crosstalk_parser.add_argument(
        "--width",
        "-w",
        dest="impedance_width",
        type=float,
        help="Trace width in mm (both traces)",
    )
    crosstalk_parser.add_argument(
        "--spacing",
        "-s",
        dest="impedance_spacing",
        type=float,
        help="Edge-to-edge spacing in mm (for analysis mode)",
    )
    crosstalk_parser.add_argument(
        "--length",
        dest="impedance_length",
        type=float,
        help="Parallel run length in mm",
    )
    crosstalk_parser.add_argument(
        "--rise-time",
        dest="impedance_rise_time",
        type=float,
        default=1.0,
        help="Signal rise time in nanoseconds (default: 1.0)",
    )
    crosstalk_parser.add_argument(
        "--max-percent",
        dest="impedance_max_percent",
        type=float,
        help="Maximum crosstalk %% (for spacing calculation mode)",
    )
