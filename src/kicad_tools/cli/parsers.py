"""
Argument parser registration for kicad-tools CLI.

This module contains functions to register all CLI subparsers.
Each function adds a command group to the main parser.
"""

from __future__ import annotations

import argparse


def register_all_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Register all CLI subparsers."""
    register_symbols_parser(subparsers)
    register_nets_parser(subparsers)
    register_netlist_parser(subparsers)
    register_erc_parser(subparsers)
    register_drc_parser(subparsers)
    register_bom_parser(subparsers)
    register_check_parser(subparsers)
    register_sch_parser(subparsers)
    register_pcb_parser(subparsers)
    register_lib_parser(subparsers)
    register_footprint_parser(subparsers)
    register_mfr_parser(subparsers)
    register_zones_parser(subparsers)
    register_route_parser(subparsers)
    register_reason_parser(subparsers)
    register_optimize_parser(subparsers)
    register_validate_footprints_parser(subparsers)
    register_fix_footprints_parser(subparsers)
    register_parts_parser(subparsers)
    register_datasheet_parser(subparsers)
    register_placement_parser(subparsers)
    register_config_parser(subparsers)
    register_interactive_parser(subparsers)
    register_validate_parser(subparsers)


def register_symbols_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the symbols subcommand."""
    parser = subparsers.add_parser("symbols", help="List symbols in a schematic")
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--format", choices=["table", "json", "csv"], default="table")
    parser.add_argument("--filter", dest="pattern", help="Filter by reference")
    parser.add_argument("--lib", dest="lib_id", help="Filter by library ID")
    parser.add_argument("-v", "--verbose", action="store_true")


def register_nets_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the nets subcommand."""
    parser = subparsers.add_parser("nets", help="Trace nets in a schematic")
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--format", choices=["table", "json"], default="table")
    parser.add_argument("--net", help="Trace a specific net by label")
    parser.add_argument("--stats", action="store_true", help="Show statistics only")


def register_netlist_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the netlist subcommand with its subcommands."""
    parser = subparsers.add_parser("netlist", help="Netlist analysis and comparison tools")
    netlist_subparsers = parser.add_subparsers(dest="netlist_command", help="Netlist commands")

    # netlist analyze
    analyze = netlist_subparsers.add_parser("analyze", help="Show connectivity statistics")
    analyze.add_argument("netlist_schematic", help="Path to .kicad_sch file")
    analyze.add_argument(
        "--format", dest="netlist_format", choices=["text", "json"], default="text"
    )

    # netlist list
    lst = netlist_subparsers.add_parser("list", help="List all nets with connection counts")
    lst.add_argument("netlist_schematic", help="Path to .kicad_sch file")
    lst.add_argument("--format", dest="netlist_format", choices=["table", "json"], default="table")
    lst.add_argument(
        "--sort",
        dest="netlist_sort",
        choices=["name", "connections"],
        default="connections",
    )

    # netlist show
    show = netlist_subparsers.add_parser("show", help="Show specific net details")
    show.add_argument("netlist_schematic", help="Path to .kicad_sch file")
    show.add_argument("--net", dest="netlist_net", required=True, help="Net name")
    show.add_argument("--format", dest="netlist_format", choices=["text", "json"], default="text")

    # netlist check
    check = netlist_subparsers.add_parser("check", help="Find connectivity issues")
    check.add_argument("netlist_schematic", help="Path to .kicad_sch file")
    check.add_argument("--format", dest="netlist_format", choices=["text", "json"], default="text")

    # netlist compare
    compare = netlist_subparsers.add_parser("compare", help="Compare two netlists")
    compare.add_argument("netlist_old", help="Path to old .kicad_sch file")
    compare.add_argument("netlist_new", help="Path to new .kicad_sch file")
    compare.add_argument(
        "--format", dest="netlist_format", choices=["text", "json"], default="text"
    )

    # netlist export
    export = netlist_subparsers.add_parser("export", help="Export netlist file")
    export.add_argument("netlist_schematic", help="Path to .kicad_sch file")
    export.add_argument("-o", "--output", dest="netlist_output", help="Output path")
    export.add_argument(
        "--format", dest="netlist_format", choices=["kicad", "json"], default="kicad"
    )


def register_erc_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ERC subcommand."""
    parser = subparsers.add_parser("erc", help="Parse ERC report")
    parser.add_argument("report", help="Path to ERC report (.json or .rpt)")
    parser.add_argument("--format", choices=["table", "json", "summary"], default="table")
    parser.add_argument("--errors-only", action="store_true")
    parser.add_argument("--type", dest="filter_type", help="Filter by violation type")
    parser.add_argument("--sheet", help="Filter by sheet path")


def register_drc_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the DRC subcommand."""
    parser = subparsers.add_parser("drc", help="Parse DRC report")
    parser.add_argument("report", help="Path to DRC report (.json or .rpt)")
    parser.add_argument("--format", choices=["table", "json", "summary"], default="table")
    parser.add_argument("--errors-only", action="store_true")
    parser.add_argument("--type", dest="filter_type", help="Filter by violation type")
    parser.add_argument("--net", help="Filter by net name")
    parser.add_argument(
        "--mfr",
        choices=["jlcpcb", "pcbway", "oshpark", "seeed"],
        help="Check against manufacturer design rules",
    )
    parser.add_argument("--layers", type=int, default=2, help="Number of copper layers")


def register_bom_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the BOM subcommand."""
    parser = subparsers.add_parser("bom", help="Generate bill of materials")
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--format", choices=["table", "csv", "json"], default="table")
    parser.add_argument("--group", action="store_true", help="Group identical components")
    parser.add_argument(
        "--exclude", action="append", default=[], help="Exclude references matching pattern"
    )
    parser.add_argument("--include-dnp", action="store_true")
    parser.add_argument("--sort", choices=["reference", "value", "footprint"], default="reference")


def register_check_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the check subcommand (pure Python DRC)."""
    parser = subparsers.add_parser("check", help="Pure Python DRC (no kicad-cli)")
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument("--format", choices=["table", "json", "summary"], default="table")
    parser.add_argument("--errors-only", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Exit with code 2 on warnings")
    parser.add_argument(
        "--mfr",
        "-m",
        choices=["jlcpcb", "pcbway", "oshpark", "seeed"],
        default="jlcpcb",
        help="Target manufacturer (default: jlcpcb)",
    )
    parser.add_argument("--layers", "-l", type=int, default=2, help="Number of layers")
    parser.add_argument("--copper", "-c", type=float, default=1.0, help="Copper weight (oz)")
    parser.add_argument(
        "--only",
        dest="only_checks",
        help="Run only specific checks (comma-separated: clearance, dimensions, edge, silkscreen)",
    )
    parser.add_argument(
        "--skip",
        dest="skip_checks",
        help="Skip specific checks (comma-separated)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")


def register_sch_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the sch subcommand with its subcommands."""
    parser = subparsers.add_parser("sch", help="Schematic analysis tools")
    sch_subparsers = parser.add_subparsers(dest="sch_command", help="Schematic commands")

    # sch summary
    summary = sch_subparsers.add_parser("summary", help="Quick schematic overview")
    summary.add_argument("schematic", help="Path to .kicad_sch file")
    summary.add_argument("--format", choices=["text", "json"], default="text")
    summary.add_argument("-v", "--verbose", action="store_true")

    # sch hierarchy
    hierarchy = sch_subparsers.add_parser("hierarchy", help="Show hierarchy tree")
    hierarchy.add_argument("schematic", help="Path to root .kicad_sch file")
    hierarchy.add_argument("--format", choices=["tree", "json"], default="tree")
    hierarchy.add_argument("--depth", type=int, help="Maximum depth to show")

    # sch labels
    labels = sch_subparsers.add_parser("labels", help="List labels")
    labels.add_argument("schematic", help="Path to .kicad_sch file")
    labels.add_argument("--format", choices=["table", "json", "csv"], default="table")
    labels.add_argument(
        "--type", choices=["all", "local", "global", "hierarchical", "power"], default="all"
    )
    labels.add_argument("--filter", dest="pattern", help="Filter by label text pattern")

    # sch validate
    validate = sch_subparsers.add_parser("validate", help="Run validation checks")
    validate.add_argument("schematic", help="Path to .kicad_sch file")
    validate.add_argument("--format", choices=["text", "json"], default="text")
    validate.add_argument("--strict", action="store_true", help="Exit with error on warnings")
    validate.add_argument("-q", "--quiet", action="store_true", help="Only show errors")

    # sch wires
    wires = sch_subparsers.add_parser("wires", help="List wire segments and junctions")
    wires.add_argument("schematic", help="Path to .kicad_sch file")
    wires.add_argument("--format", choices=["table", "json", "csv"], default="table")
    wires.add_argument("--stats", action="store_true", help="Show statistics only")
    wires.add_argument("--junctions", action="store_true", help="Include junction points")

    # sch info
    info = sch_subparsers.add_parser("info", help="Show symbol details")
    info.add_argument("schematic", help="Path to .kicad_sch file")
    info.add_argument("reference", help="Symbol reference (e.g., U1)")
    info.add_argument("--format", choices=["text", "json"], default="text")
    info.add_argument("--show-pins", action="store_true", help="Show pin details")
    info.add_argument("--show-properties", action="store_true", help="Show all properties")

    # sch pins
    pins = sch_subparsers.add_parser("pins", help="Show symbol pin positions")
    pins.add_argument("schematic", help="Path to .kicad_sch file")
    pins.add_argument("reference", help="Symbol reference (e.g., U1)")
    pins.add_argument("--lib", required=True, help="Path to symbol library file")
    pins.add_argument("--format", choices=["table", "json"], default="table")

    # sch connections
    connections = sch_subparsers.add_parser(
        "connections", help="Check pin connections using library positions"
    )
    connections.add_argument("schematic", help="Path to .kicad_sch file")
    connections.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    connections.add_argument("--lib", action="append", dest="libs", help="Specific library file")
    connections.add_argument("--format", choices=["table", "json"], default="table")
    connections.add_argument("--filter", dest="pattern", help="Filter by symbol reference")
    connections.add_argument(
        "-v", "--verbose", action="store_true", help="Show all pins, not just unconnected"
    )

    # sch unconnected
    unconnected = sch_subparsers.add_parser("unconnected", help="Find unconnected pins and issues")
    unconnected.add_argument("schematic", help="Path to .kicad_sch file")
    unconnected.add_argument("--format", choices=["table", "json"], default="table")
    unconnected.add_argument("--filter", dest="pattern", help="Filter by symbol reference")
    unconnected.add_argument("--include-power", action="store_true", help="Include power symbols")
    unconnected.add_argument("--include-dnp", action="store_true", help="Include DNP symbols")

    # sch replace
    replace = sch_subparsers.add_parser("replace", help="Replace a symbol's library ID")
    replace.add_argument("schematic", help="Path to .kicad_sch file")
    replace.add_argument("reference", help="Symbol reference to replace (e.g., U1)")
    replace.add_argument("new_lib_id", help="New library ID (e.g., 'mylib:NewSymbol')")
    replace.add_argument("--value", help="New value for the symbol")
    replace.add_argument("--footprint", help="New footprint")
    replace.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    replace.add_argument("--backup", action="store_true", help="Create backup before modifying")


def register_pcb_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the pcb subcommand with its subcommands."""
    parser = subparsers.add_parser("pcb", help="PCB query tools")
    pcb_subparsers = parser.add_subparsers(dest="pcb_command", help="PCB commands")

    # pcb summary
    summary = pcb_subparsers.add_parser("summary", help="Board summary")
    summary.add_argument("pcb", help="Path to .kicad_pcb file")
    summary.add_argument("--format", choices=["text", "json"], default="text")

    # pcb footprints
    footprints = pcb_subparsers.add_parser("footprints", help="List footprints")
    footprints.add_argument("pcb", help="Path to .kicad_pcb file")
    footprints.add_argument("--format", choices=["text", "json"], default="text")
    footprints.add_argument("--filter", dest="pattern", help="Filter by reference pattern")
    footprints.add_argument("--sorted", action="store_true", help="Sort output")

    # pcb nets
    nets = pcb_subparsers.add_parser("nets", help="List nets")
    nets.add_argument("pcb", help="Path to .kicad_pcb file")
    nets.add_argument("--format", choices=["text", "json"], default="text")
    nets.add_argument("--filter", dest="pattern", help="Filter by net pattern")
    nets.add_argument("--sorted", action="store_true", help="Sort output")

    # pcb traces
    traces = pcb_subparsers.add_parser("traces", help="Show trace statistics")
    traces.add_argument("pcb", help="Path to .kicad_pcb file")
    traces.add_argument("--format", choices=["text", "json"], default="text")
    traces.add_argument("--layer", help="Filter by layer (e.g., F.Cu)")

    # pcb stackup
    stackup = pcb_subparsers.add_parser("stackup", help="Show layer stackup")
    stackup.add_argument("pcb", help="Path to .kicad_pcb file")
    stackup.add_argument("--format", choices=["text", "json"], default="text")


def register_lib_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the lib subcommand with its subcommands."""
    parser = subparsers.add_parser("lib", help="Symbol and footprint library tools")
    lib_subparsers = parser.add_subparsers(dest="lib_command", help="Library commands")

    # lib list
    lst = lib_subparsers.add_parser("list", help="List available KiCad libraries")
    lst.add_argument("--symbols", action="store_true", help="List only symbol libraries")
    lst.add_argument("--footprints", action="store_true", help="List only footprint libraries")
    lst.add_argument("--format", choices=["table", "json"], default="table")

    # lib symbols
    symbols = lib_subparsers.add_parser("symbols", help="List symbols in library")
    symbols.add_argument("library", help="Path to .kicad_sym file")
    symbols.add_argument("--format", choices=["table", "json"], default="table")
    symbols.add_argument("--pins", action="store_true", help="Show pin details")

    # lib footprints
    footprints = lib_subparsers.add_parser(
        "footprints", help="List footprints in a .pretty library directory"
    )
    footprints.add_argument("directory", help="Path to .pretty directory")
    footprints.add_argument("--format", choices=["table", "json"], default="table")

    # lib footprint
    footprint = lib_subparsers.add_parser("footprint", help="Show details of a footprint file")
    footprint.add_argument("file", help="Path to .kicad_mod file")
    footprint.add_argument("--format", choices=["text", "json"], default="text")
    footprint.add_argument("--pads", action="store_true", help="Show pad details")

    # lib symbol-info
    symbol_info = lib_subparsers.add_parser("symbol-info", help="Show details of a symbol by name")
    symbol_info.add_argument("library", help="Library name or path to .kicad_sym file")
    symbol_info.add_argument("name", help="Symbol name")
    symbol_info.add_argument("--format", choices=["text", "json"], default="text")
    symbol_info.add_argument("--pins", action="store_true", help="Show pin details")

    # lib footprint-info
    footprint_info = lib_subparsers.add_parser(
        "footprint-info", help="Show details of a footprint by name"
    )
    footprint_info.add_argument("library", help="Library name or path to .pretty directory")
    footprint_info.add_argument("name", help="Footprint name")
    footprint_info.add_argument("--format", choices=["text", "json"], default="text")
    footprint_info.add_argument("--pads", action="store_true", help="Show pad details")

    # lib create-symbol-lib (placeholder)
    create_sym = lib_subparsers.add_parser(
        "create-symbol-lib", help="Create new symbol library (not yet implemented)"
    )
    create_sym.add_argument("path", help="Path for new .kicad_sym file")

    # lib create-footprint-lib (placeholder)
    create_fp = lib_subparsers.add_parser(
        "create-footprint-lib", help="Create new footprint library (not yet implemented)"
    )
    create_fp.add_argument("path", help="Path for new .pretty directory")

    # lib generate-footprint (placeholder)
    generate = lib_subparsers.add_parser(
        "generate-footprint", help="Generate parametric footprint (not yet implemented)"
    )
    generate.add_argument("library", help="Target library path")
    generate.add_argument(
        "type", choices=["soic", "qfp", "qfn", "dfn", "chip", "sot"], help="Footprint type"
    )
    generate.add_argument("--pins", type=int, help="Number of pins")
    generate.add_argument("--pitch", type=float, help="Pin pitch in mm")
    generate.add_argument("--body-width", type=float, help="Body width in mm")
    generate.add_argument("--body-size", type=float, help="Body size (square) in mm")
    generate.add_argument("--prefix", help="Footprint name prefix")

    # lib export
    export = lib_subparsers.add_parser("export", help="Export library to JSON")
    export.add_argument("path", help="Path to library file or item")
    export.add_argument("--format", choices=["json"], default="json")


def register_footprint_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the footprint subcommand."""
    parser = subparsers.add_parser("footprint", help="Footprint generation and tools")
    fp_subparsers = parser.add_subparsers(dest="footprint_command", help="Footprint commands")

    # footprint generate - delegates to footprint_generate.py
    fp_subparsers.add_parser(
        "generate",
        help="Generate parametric footprints",
        add_help=False,
    )


def register_mfr_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the mfr subcommand with its subcommands."""
    parser = subparsers.add_parser("mfr", help="Manufacturer tools")
    mfr_subparsers = parser.add_subparsers(dest="mfr_command", help="Manufacturer commands")

    # mfr list
    mfr_subparsers.add_parser("list", help="List available manufacturers")

    # mfr info
    info = mfr_subparsers.add_parser("info", help="Show manufacturer details")
    info.add_argument("manufacturer", help="Manufacturer ID (jlcpcb, seeed, etc.)")

    # mfr rules
    rules = mfr_subparsers.add_parser("rules", help="Show design rules")
    rules.add_argument("manufacturer", help="Manufacturer ID")
    rules.add_argument("-l", "--layers", type=int, default=4, help="Layer count")
    rules.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")

    # mfr compare
    compare = mfr_subparsers.add_parser("compare", help="Compare manufacturers")
    compare.add_argument("-l", "--layers", type=int, default=4, help="Layer count")
    compare.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")

    # mfr apply-rules
    apply_rules = mfr_subparsers.add_parser(
        "apply-rules", help="Apply manufacturer design rules to project/PCB"
    )
    apply_rules.add_argument("file", help="Path to .kicad_pro or .kicad_pcb file")
    apply_rules.add_argument("manufacturer", help="Manufacturer ID (jlcpcb, seeed, etc.)")
    apply_rules.add_argument("-l", "--layers", type=int, default=2, help="Layer count")
    apply_rules.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")
    apply_rules.add_argument("-o", "--output", help="Output file (default: modify in place)")
    apply_rules.add_argument("--dry-run", action="store_true", help="Show changes without applying")

    # mfr validate
    validate = mfr_subparsers.add_parser("validate", help="Validate PCB against manufacturer rules")
    validate.add_argument("file", help="Path to .kicad_pcb file")
    validate.add_argument("manufacturer", help="Manufacturer ID (jlcpcb, seeed, etc.)")
    validate.add_argument("-l", "--layers", type=int, default=2, help="Layer count")
    validate.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")

    # mfr export-dru
    export_dru = mfr_subparsers.add_parser(
        "export-dru", help="Export manufacturer rules as KiCad DRU file"
    )
    export_dru.add_argument("manufacturer", help="Manufacturer ID (jlcpcb, seeed, etc.)")
    export_dru.add_argument("-l", "--layers", type=int, default=4, help="Layer count")
    export_dru.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")
    export_dru.add_argument("-o", "--output", type=str, help="Output file path")

    # mfr import-dru
    import_dru = mfr_subparsers.add_parser(
        "import-dru", help="Parse and display a KiCad design rules file"
    )
    import_dru.add_argument("file", help="Path to .kicad_dru file")
    import_dru.add_argument("--format", choices=["text", "json"], default="text")


def register_zones_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the zones subcommand with its subcommands."""
    parser = subparsers.add_parser("zones", help="Add copper pour zones to PCB")
    zones_subparsers = parser.add_subparsers(dest="zones_command", help="Zone commands")

    # zones add
    add = zones_subparsers.add_parser("add", help="Add a copper zone")
    add.add_argument("pcb", help="Path to .kicad_pcb file")
    add.add_argument("-o", "--output", help="Output file path")
    add.add_argument("--net", required=True, help="Net name (e.g., GND, +3.3V)")
    add.add_argument("--layer", required=True, help="Copper layer (e.g., B.Cu, F.Cu)")
    add.add_argument("--priority", type=int, default=0, help="Zone fill priority")
    add.add_argument("--clearance", type=float, default=0.3, help="Clearance in mm")
    add.add_argument("--thermal-gap", type=float, default=0.3, help="Thermal gap in mm")
    add.add_argument("--thermal-bridge", type=float, default=0.4, help="Thermal bridge width in mm")
    add.add_argument("--min-thickness", type=float, default=0.25, help="Min thickness in mm")
    add.add_argument("-v", "--verbose", action="store_true")
    add.add_argument("--dry-run", action="store_true")

    # zones list
    lst = zones_subparsers.add_parser("list", help="List existing zones")
    lst.add_argument("pcb", help="Path to .kicad_pcb file")
    lst.add_argument("--format", choices=["text", "json"], default="text")

    # zones batch
    batch = zones_subparsers.add_parser("batch", help="Add multiple zones from spec")
    batch.add_argument("pcb", help="Path to .kicad_pcb file")
    batch.add_argument("-o", "--output", help="Output file path")
    batch.add_argument(
        "--power-nets",
        required=True,
        help="Power nets spec: 'NET:LAYER,...' (e.g., 'GND:B.Cu,+3.3V:F.Cu')",
    )
    batch.add_argument("--clearance", type=float, default=0.3, help="Clearance in mm")
    batch.add_argument("-v", "--verbose", action="store_true")
    batch.add_argument("--dry-run", action="store_true")


def register_route_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the route subcommand."""
    parser = subparsers.add_parser("route", help="Autoroute a PCB")
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument("-o", "--output", help="Output file path")
    parser.add_argument(
        "--strategy",
        choices=["basic", "negotiated", "monte-carlo"],
        default="negotiated",
        help="Routing strategy (default: negotiated)",
    )
    parser.add_argument("--skip-nets", help="Comma-separated nets to skip")
    parser.add_argument("--grid", type=float, default=0.25, help="Grid resolution in mm")
    parser.add_argument("--trace-width", type=float, default=0.2, help="Trace width in mm")
    parser.add_argument("--clearance", type=float, default=0.15, help="Clearance in mm")
    parser.add_argument("--via-drill", type=float, default=0.3, help="Via drill size in mm")
    parser.add_argument("--via-diameter", type=float, default=0.6, help="Via diameter in mm")
    parser.add_argument("--mc-trials", type=int, default=10, help="Monte Carlo trials")
    parser.add_argument("--iterations", type=int, default=15, help="Max iterations")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Don't write output")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")
    parser.add_argument(
        "--power-nets",
        help="Generate zones: 'NET:LAYER,...' (e.g., 'GND:B.Cu,+3.3V:F.Cu')",
    )


def register_reason_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the reason subcommand."""
    parser = subparsers.add_parser("reason", help="LLM-driven PCB layout reasoning")
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument("-o", "--output", help="Output file path")
    parser.add_argument(
        "--export-state",
        action="store_true",
        help="Export state as JSON for external LLM",
    )
    parser.add_argument("--state-output", help="Output path for state JSON")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run interactive reasoning loop",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Print detailed analysis of PCB state",
    )
    parser.add_argument(
        "--auto-route",
        action="store_true",
        help="Auto-route priority nets without LLM",
    )
    parser.add_argument(
        "--max-nets",
        type=int,
        default=10,
        help="Maximum nets to auto-route (default: 10)",
    )
    parser.add_argument("--drc", help="Path to DRC report file")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Don't write output")


def register_optimize_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the optimize-traces subcommand."""
    parser = subparsers.add_parser("optimize-traces", help="Optimize PCB traces")
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument("-o", "--output", help="Output file (default: modify in place)")
    parser.add_argument("--net", help="Only optimize traces matching this net pattern")
    parser.add_argument("--no-merge", action="store_true", help="Disable collinear merging")
    parser.add_argument("--no-zigzag", action="store_true", help="Disable zigzag elimination")
    parser.add_argument("--no-45", action="store_true", help="Disable 45-degree corners")
    parser.add_argument(
        "--chamfer-size",
        type=float,
        default=0.5,
        help="45-degree chamfer size in mm (default: 0.5)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Show results without writing")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")


def register_validate_footprints_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the validate-footprints subcommand."""
    parser = subparsers.add_parser(
        "validate-footprints", help="Validate footprints for pad spacing issues"
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "--min-pad-gap",
        type=float,
        default=0.15,
        help="Minimum required gap between pads in mm (default: 0.15)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument("--errors-only", action="store_true", help="Only show errors, not warnings")
    parser.add_argument(
        "--compare-standard",
        action="store_true",
        help="Compare footprints against KiCad standard library",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.05,
        help="Tolerance for standard comparison in mm (default: 0.05)",
    )
    parser.add_argument(
        "--kicad-library-path",
        type=str,
        default=None,
        help="Override path to KiCad footprint libraries",
    )


def register_fix_footprints_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the fix-footprints subcommand."""
    parser = subparsers.add_parser("fix-footprints", help="Fix footprint pad spacing issues")
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument("-o", "--output", help="Output file path (default: modify in place)")
    parser.add_argument(
        "--min-pad-gap",
        type=float,
        default=0.2,
        help="Target gap between pads in mm (default: 0.2)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without applying",
    )


def register_parts_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the parts subcommand with its subcommands."""
    parser = subparsers.add_parser("parts", help="LCSC parts lookup and search")
    parts_subparsers = parser.add_subparsers(dest="parts_command", help="Parts commands")

    # parts lookup
    lookup = parts_subparsers.add_parser("lookup", help="Look up part by LCSC number")
    lookup.add_argument("part", help="LCSC part number (e.g., C123456)")
    lookup.add_argument("--format", choices=["text", "json"], default="text")
    lookup.add_argument("--no-cache", action="store_true", help="Bypass cache")

    # parts search
    search = parts_subparsers.add_parser("search", help="Search for parts")
    search.add_argument("query", help="Search query")
    search.add_argument("--format", choices=["text", "json", "table"], default="table")
    search.add_argument("--limit", type=int, default=20, help="Max results")
    search.add_argument("--in-stock", action="store_true", help="Only in-stock parts")
    search.add_argument("--basic", action="store_true", help="Only JLCPCB basic parts")

    # parts cache
    cache = parts_subparsers.add_parser("cache", help="Cache management")
    cache.add_argument(
        "cache_action",
        nargs="?",
        choices=["stats", "clear", "clear-expired"],
        default="stats",
        help="Cache action (default: stats)",
    )


def register_datasheet_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the datasheet subcommand with its subcommands."""
    parser = subparsers.add_parser("datasheet", help="Datasheet search, download, and PDF parsing")
    ds_subparsers = parser.add_subparsers(dest="datasheet_command", help="Datasheet commands")

    # datasheet search
    search = ds_subparsers.add_parser("search", help="Search for datasheets")
    search.add_argument("part", help="Part number to search for")
    search.add_argument("--format", choices=["text", "json"], default="text")
    search.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")

    # datasheet download
    download = ds_subparsers.add_parser("download", help="Download a datasheet")
    download.add_argument("part", help="Part number to download")
    download.add_argument("-o", "--output", help="Output directory (default: cache)")
    download.add_argument("--force", action="store_true", help="Force download even if cached")

    # datasheet list
    lst = ds_subparsers.add_parser("list", help="List cached datasheets")
    lst.add_argument("--format", choices=["text", "json"], default="text")

    # datasheet cache
    cache = ds_subparsers.add_parser("cache", help="Cache management")
    cache.add_argument(
        "cache_action",
        nargs="?",
        choices=["stats", "clear", "clear-expired"],
        default="stats",
        help="Cache action (default: stats)",
    )
    cache.add_argument(
        "--older-than", type=int, help="For clear: only clear entries older than N days"
    )

    # datasheet convert
    convert = ds_subparsers.add_parser("convert", help="Convert PDF to markdown")
    convert.add_argument("pdf", help="Path to PDF file")
    convert.add_argument("-o", "--output", help="Output file path (default: stdout)")
    convert.add_argument("--pages", help="Page range (e.g., '1-10' or '1,2,5')")

    # datasheet extract-images
    images = ds_subparsers.add_parser("extract-images", help="Extract images from PDF")
    images.add_argument("pdf", help="Path to PDF file")
    images.add_argument("-o", "--output", required=True, help="Output directory for images")
    images.add_argument("--pages", help="Page range (e.g., '1-10' or '1,2,5')")
    images.add_argument(
        "--min-size", type=int, default=100, help="Minimum image dimension (default: 100)"
    )
    images.add_argument("--format", choices=["text", "json"], default="text")

    # datasheet extract-tables
    tables = ds_subparsers.add_parser("extract-tables", help="Extract tables from PDF")
    tables.add_argument("pdf", help="Path to PDF file")
    tables.add_argument("-o", "--output", help="Output directory for tables")
    tables.add_argument("--pages", help="Page range (e.g., '1-10' or '1,2,5')")
    tables.add_argument("--format", choices=["markdown", "csv", "json"], default="markdown")

    # datasheet info
    info = ds_subparsers.add_parser("info", help="Show PDF information")
    info.add_argument("pdf", help="Path to PDF file")
    info.add_argument("--format", choices=["text", "json"], default="text")


def register_placement_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the placement subcommand with its subcommands."""
    parser = subparsers.add_parser("placement", help="Detect and fix placement conflicts")
    placement_subparsers = parser.add_subparsers(
        dest="placement_command", help="Placement commands"
    )

    # placement check
    check = placement_subparsers.add_parser("check", help="Check PCB for placement conflicts")
    check.add_argument("pcb", help="Path to .kicad_pcb file")
    check.add_argument("--format", choices=["table", "json", "summary"], default="table")
    check.add_argument("--pad-clearance", type=float, default=0.1, help="Min pad clearance (mm)")
    check.add_argument("--hole-clearance", type=float, default=0.5, help="Min hole clearance (mm)")
    check.add_argument("--edge-clearance", type=float, default=0.3, help="Min edge clearance (mm)")
    check.add_argument("-v", "--verbose", action="store_true")
    check.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")

    # placement fix
    fix = placement_subparsers.add_parser("fix", help="Suggest and apply placement fixes")
    fix.add_argument("pcb", help="Path to .kicad_pcb file")
    fix.add_argument("-o", "--output", help="Output file path")
    fix.add_argument(
        "--strategy", choices=["spread", "compact", "anchor"], default="spread", help="Fix strategy"
    )
    fix.add_argument("--anchor", help="Comma-separated components to keep fixed")
    fix.add_argument("--dry-run", action="store_true")
    fix.add_argument("-v", "--verbose", action="store_true")
    fix.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")

    # placement optimize
    optimize = placement_subparsers.add_parser(
        "optimize", help="Optimize placement for routability"
    )
    optimize.add_argument("pcb", help="Path to .kicad_pcb file")
    optimize.add_argument("-o", "--output", help="Output file path")
    optimize.add_argument(
        "--strategy",
        choices=["force-directed", "evolutionary", "hybrid"],
        default="force-directed",
        help="Optimization strategy (default: force-directed)",
    )
    optimize.add_argument(
        "--iterations",
        type=int,
        default=1000,
        help="Max iterations for physics simulation (default: 1000)",
    )
    optimize.add_argument(
        "--generations",
        type=int,
        default=100,
        help="Generations for evolutionary/hybrid mode (default: 100)",
    )
    optimize.add_argument(
        "--population",
        type=int,
        default=50,
        help="Population size for evolutionary/hybrid mode (default: 50)",
    )
    optimize.add_argument(
        "--grid",
        type=float,
        default=0.0,
        help="Position grid snap in mm (0 to disable, default: 0)",
    )
    optimize.add_argument(
        "--fixed", help="Comma-separated component refs to keep fixed (e.g., J1,J2,H1)"
    )
    optimize.add_argument("--dry-run", action="store_true", help="Preview only")
    optimize.add_argument("-v", "--verbose", action="store_true")
    optimize.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")


def register_config_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the config subcommand."""
    parser = subparsers.add_parser("config", help="View and manage configuration")
    parser.add_argument(
        "--show", action="store_true", help="Show effective configuration with sources"
    )
    parser.add_argument("--init", action="store_true", help="Create template config file")
    parser.add_argument("--paths", action="store_true", help="Show config file paths")
    parser.add_argument("--user", action="store_true", help="Use user config for --init")
    parser.add_argument("config_action", nargs="?", choices=["get", "set"], help="Config action")
    parser.add_argument("config_key", nargs="?", help="Config key (e.g., defaults.format)")
    parser.add_argument("config_value", nargs="?", help="Value to set")


def register_interactive_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the interactive subcommand."""
    parser = subparsers.add_parser("interactive", help="Launch interactive REPL mode")
    parser.add_argument("--project", help="Auto-load a project on startup")


def register_validate_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the validate subcommand."""
    parser = subparsers.add_parser("validate", help="Validation tools")
    parser.add_argument("validate_project", nargs="?", help="Path to .kicad_pro file")
    parser.add_argument(
        "--sync", action="store_true", help="Check schematic-to-PCB netlist synchronization"
    )
    parser.add_argument(
        "--schematic",
        "-s",
        dest="validate_schematic",
        help="Path to .kicad_sch file (if not using project file)",
    )
    parser.add_argument(
        "--pcb",
        "-p",
        dest="validate_pcb",
        help="Path to .kicad_pcb file (if not using project file)",
    )
    parser.add_argument(
        "--format",
        dest="validate_format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--errors-only",
        dest="validate_errors_only",
        action="store_true",
        help="Show only errors, not warnings",
    )
    parser.add_argument(
        "--strict",
        dest="validate_strict",
        action="store_true",
        help="Exit with error code on warnings",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        dest="validate_verbose",
        action="store_true",
        help="Show detailed issue information",
    )
