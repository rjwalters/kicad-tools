"""
Argument parser setup for kicad-tools CLI.

This module contains all the argparse configuration for the kicad-tools CLI.
The parser is organized into subparsers for each major command category.
"""

import argparse

from kicad_tools import __version__
from kicad_tools.manufacturers import get_all_manufacturer_names

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
    kicad-tools stitch <pcb>           - Auto-add stitching vias for plane connections
    kicad-tools reason <pcb>           - LLM-driven PCB layout reasoning
    kicad-tools placement <command>    - Detect and fix placement conflicts
    kicad-tools optimize-placement     - Run CMA-ES placement optimization
    kicad-tools optimize-traces <pcb>  - Optimize PCB traces
    kicad-tools validate-footprints    - Validate footprint pad spacing
    kicad-tools fix-footprints <pcb>   - Fix footprint pad spacing issues
    kicad-tools analyze <command>      - PCB analysis tools
    kicad-tools audit <project>        - Manufacturing readiness audit
    kicad-tools pipeline <pcb>         - End-to-end repair pipeline for existing PCBs
    kicad-tools create-pcb <schematic>  - Create PCB from schematic
    kicad-tools clean <project>        - Clean up old/orphaned files
    kicad-tools init <project>         - Initialize project with manufacturer rules
    kicad-tools run <script>           - Run Python script with kicad-tools interpreter
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
    kct init myproject --mfr jlcpcb --layers 4
    kct init existing.kicad_pro --mfr seeed
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
    parser.add_argument(
        "--units",
        choices=["mm", "mils"],
        default=None,
        help="Unit system for output (mm or mils). Overrides config and env var.",
        dest="global_units",
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
    _add_creepage_parser(subparsers)
    _add_sch_parser(subparsers)
    _add_pcb_parser(subparsers)
    _add_lib_parser(subparsers)
    _add_footprint_parser(subparsers)
    _add_mfr_parser(subparsers)
    _add_zones_parser(subparsers)
    _add_stitch_parser(subparsers)
    _add_route_parser(subparsers)
    _add_route_auto_parser(subparsers)
    _add_reason_parser(subparsers)
    _add_optimize_parser(subparsers)
    _add_validate_footprints_parser(subparsers)
    _add_fix_footprints_parser(subparsers)
    _add_fix_vias_parser(subparsers)
    _add_fix_silkscreen_parser(subparsers)
    _add_repair_clearance_parser(subparsers)
    _add_fix_drc_parser(subparsers)
    _add_fix_erc_parser(subparsers)
    _add_parts_parser(subparsers)
    _add_datasheet_parser(subparsers)
    _add_decisions_parser(subparsers)
    _add_placement_parser(subparsers)
    _add_optimize_placement_parser(subparsers)
    _add_config_parser(subparsers)
    _add_interactive_parser(subparsers)
    _add_validate_parser(subparsers)
    _add_analyze_parser(subparsers)
    _add_constraints_parser(subparsers)
    _add_estimate_parser(subparsers)
    _add_audit_parser(subparsers)
    _add_suggest_parser(subparsers)
    _add_net_status_parser(subparsers)
    _add_fleet_parser(subparsers)
    _add_render_parser(subparsers)
    _add_board_metrics_parser(subparsers)
    _add_clean_parser(subparsers)
    _add_impedance_parser(subparsers)
    _add_mcp_parser(subparsers)
    _add_ipc_parser(subparsers)
    _add_init_parser(subparsers)
    _add_panel_parser(subparsers)
    _add_pipeline_parser(subparsers)
    _add_create_pcb_parser(subparsers)
    _add_build_parser(subparsers)
    _add_build_native_parser(subparsers)
    _add_doctor_parser(subparsers)
    _add_spec_parser(subparsers)
    _add_benchmark_parser(subparsers)
    _add_sync_parser(subparsers)
    _add_run_parser(subparsers)
    _add_explain_parser(subparsers)
    _add_detect_mistakes_parser(subparsers)
    _add_calibrate_parser(subparsers)
    _add_screenshot_parser(subparsers)
    _add_report_parser(subparsers)
    _add_export_parser(subparsers)
    _add_optim_parser(subparsers)

    return parser


def _add_optim_parser(subparsers) -> None:
    """Add ``optim`` subcommand parser (issue #3186 hybrid FOM).

    ``optim`` has nested subcommands:

    * ``optim fom-debug <pcb> [--weights weights.yaml] [--format text|json]``
    """
    optim_parser = subparsers.add_parser(
        "optim",
        help="Placement / routing FOM tools (issue #3186)",
    )
    optim_subs = optim_parser.add_subparsers(dest="optim_command", help="Optim subcommand")

    fom_debug = optim_subs.add_parser(
        "fom-debug",
        help="Print the per-term FOM breakdown for an existing placement",
    )
    fom_debug.add_argument("pcb", help="Path to .kicad_pcb file (placement or routed)")
    fom_debug.add_argument(
        "--weights",
        default=None,
        help="Path to weights YAML (default: uniform 1.0)",
    )
    fom_debug.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        dest="output_format",
        help="Output format (default: text)",
    )
    fom_debug.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Include feature-extraction stats above the FOM table",
    )


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
        choices=get_all_manufacturer_names(),
        help="Check against manufacturer design rules",
    )
    drc_parser.add_argument("--layers", type=int, default=2, help="Number of copper layers")


def _add_bom_parser(subparsers) -> None:
    """Add BOM subcommand parser."""
    bom_parser = subparsers.add_parser("bom", help="Generate bill of materials")
    bom_parser.add_argument("schematic", help="Path to .kicad_sch file")
    bom_parser.add_argument(
        "--format",
        choices=["table", "csv", "json", "jlcpcb"],
        default="table",
        help="Output format (jlcpcb: JLCPCB assembly BOM)",
    )
    bom_parser.add_argument("--group", action="store_true", help="Group identical components")
    bom_parser.add_argument(
        "--exclude", action="append", default=[], help="Exclude references matching pattern"
    )
    bom_parser.add_argument("--include-dnp", action="store_true")
    bom_parser.add_argument(
        "--sort", choices=["reference", "value", "footprint"], default="reference"
    )


def _add_creepage_parser(subparsers) -> None:
    """Add creepage subcommand parser (HV surface-path audit, Issue #4327)."""
    creepage_parser = subparsers.add_parser(
        "creepage",
        help="HV creepage/clearance census (surface-path distance)",
    )
    creepage_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    creepage_parser.add_argument(
        "--net-class",
        dest="net_class",
        default="HV",
        help="Net class to treat as the HV group (default: HV)",
    )
    creepage_parser.add_argument(
        "--net-class-map",
        dest="net_class_map",
        default=None,
        help=(
            "Path to a JSON sidecar mapping net names to NetClassRouting "
            "fields (parsed by net_class_map_from_dict); nets whose class "
            "name matches --net-class are the HV group.  Falls back to "
            "name-pattern classification for unmapped nets."
        ),
    )
    creepage_parser.add_argument(
        "--min",
        type=float,
        default=None,
        help=(
            "Required minimum creepage (surface-path) distance in mm.  Manual "
            "override / phase-1 mode.  Optional when --standard is supplied; "
            "when BOTH are given the stricter (larger) of {manual, derived} "
            "governs per pair.  Provide either --standard or --min."
        ),
    )
    # Phase 2 (#4332): derive the required creepage/clearance from an IEC table.
    creepage_parser.add_argument(
        "--standard",
        choices=["iec60664", "iec62368"],
        default=None,
        help=(
            "Derive the required creepage (and clearance) from an IEC "
            "standard table instead of --min: iec60664 (IEC 60664-1 Table F.4 "
            "creepage / Table F.2 clearance) or iec62368 (IEC 62368-1 Table 17 "
            "/ Table 14).  Requires --working-voltage and --pollution-degree.  "
            "Engineering aid, NOT a certification."
        ),
    )
    creepage_parser.add_argument(
        "--pollution-degree",
        dest="pollution_degree",
        type=int,
        choices=[1, 2, 3],
        default=None,
        help=(
            "IEC pollution degree (1=sealed, 2=typical indoor/office FR-4, "
            "3=conductive pollution).  Required with --standard."
        ),
    )
    creepage_parser.add_argument(
        "--working-voltage",
        dest="working_voltage",
        type=float,
        default=None,
        help=(
            "RMS working voltage in volts.  Creepage keys on this RMS value "
            "directly (step-up to the next-higher tabulated row, never "
            "interpolated); clearance keys on its peak (RMS x sqrt(2), "
            "sinusoidal assumption).  Required with --standard unless "
            "--voltage-map is supplied."
        ),
    )
    # Per-net voltage model (#4371): derive the requirement per pair from |ΔV|.
    creepage_parser.add_argument(
        "--voltage-map",
        dest="voltage_map",
        default=None,
        help=(
            "Path to a JSON sidecar mapping net names to their RMS working "
            'potential (volts) about a common reference, e.g. {"/AC_LINE": 150, '
            '"/AC_NEUTRAL": 0, "/SCAP_POS": 90}.  With --standard, the required '
            "creepage/clearance is derived PER PAIR from |V_a - V_b| instead of "
            "one global --working-voltage: same-potential nets require ~0 and "
            "cross-domain pairs use their real difference.  Unmapped nets default "
            "to 0 V; the reserved key _edge_voltage sets the board-edge/earth "
            "reference (default 0 V); other _-prefixed keys (e.g. _comment) are "
            "ignored.  Potentials are worst-case DC-equivalent magnitudes -- AC "
            "phase is not modelled, so |ΔV| is conservative for in-phase nets.  "
            "Requires --standard (and --pollution-degree)."
        ),
    )
    creepage_parser.add_argument(
        "--material-group",
        dest="material_group",
        choices=["I", "II", "IIIa", "IIIb"],
        default="IIIa",
        help=(
            "Insulation material group by CTI (I: CTI>=600, II: 400<=CTI<600, "
            "IIIa: 175<=CTI<400, IIIb: 100<=CTI<175).  Default IIIa -- the "
            "conservative assumption for common FR-4."
        ),
    )
    creepage_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )


def _add_check_parser(subparsers) -> None:
    """Add check subcommand parser (pure Python DRC)."""
    check_parser = subparsers.add_parser("check", help="Pure Python DRC (no kicad-cli)")
    check_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    check_parser.add_argument("--format", choices=["table", "json", "summary"], default="table")
    check_parser.add_argument("--errors-only", action="store_true")
    check_parser.add_argument("--strict", action="store_true", help="Exit with code 2 on warnings")
    check_parser.add_argument(
        "--strict-connectivity",
        dest="strict_connectivity",
        action="store_true",
        help=(
            "Decide the connectivity DRC rule by REAL geometric copper contact "
            "(shapely polygon intersection) instead of the default 0.01mm "
            "endpoint-proximity tolerance, matching KiCad (issue #4176). "
            "Requires shapely. Distinct from --strict (warnings fatal)."
        ),
    )
    check_parser.add_argument(
        "--mfr",
        "-m",
        choices=get_all_manufacturer_names(),
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
    check_parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Write JSON report to file (implies --format json for file output)",
    )
    check_parser.add_argument(
        "--suppress-library",
        action="store_true",
        help="Suppress silkscreen warnings from standard KiCad library footprints",
    )
    check_parser.add_argument(
        "--drc-only",
        dest="drc_only",
        action="store_true",
        help=(
            "Legacy DRC-only mode (issue #3750): skip the ERC / LVS / "
            "Manifest meta sub-checks and preserve the pre-#3750 stdout "
            "and exit-code contract.  Use this in CI scripts and recipes "
            "that depend on the historical 'kct check' semantics."
        ),
    )
    check_parser.add_argument(
        "--allow-incomplete",
        dest="allow_incomplete",
        action="store_true",
        help=(
            "Treat Overall: INCOMPLETE (any sub-check NOT RUN) as exit 0 "
            "(issue #3750).  By default INCOMPLETE exits non-zero so "
            "consumers that read the exit code do not silently accept a "
            "partially verified board.  Use this for boards / recipes that "
            "legitimately lack a sub-check input (no schematic, or "
            "kct check runs before kct export produces the manifest)."
        ),
    )
    check_parser.add_argument(
        "--netlist-sync",
        action="store_true",
        help=(
            "Run a blocking schematic/PCB netlist-sync gate (issue #3154): "
            "compares the schematic component set against the PCB footprint "
            "set and exits with code 2 when a schematic component is missing "
            "from the PCB (unbuildable BOM) or a matched component's value or "
            "footprint diverges (wrong part / wrong package). Benign "
            "rating-suffix value diffs (issue #4351) do not fail the gate; "
            "PCB-only extras stay a warning unless --strict. Skips silently "
            "if no schematic is found."
        ),
    )
    check_parser.add_argument(
        "--schematic",
        default=None,
        help=(
            "Explicit .kicad_sch path for --netlist-sync / the advisory drift "
            "banner (default: auto-discover from project.kct or sibling file)."
        ),
    )
    check_parser.add_argument(
        "--net-class-map",
        dest="net_class_map",
        default=None,
        help=(
            "Path to a JSON sidecar mapping net names to NetClassRouting "
            "fields.  When supplied, enables the diff-pair DRC rules "
            "(routing_continuity, length_skew) to fire on routed boards "
            "(Issue #2684)."
        ),
    )
    # Issue #3061: per-board auto-derive of the pad_grid tolerance is the
    # default for the CLI.  --pad-grid-strict opts back into the PR #3057
    # fixed-0.05mm constant; --pad-grid-tolerance pins a custom value.
    pad_grid_group = check_parser.add_mutually_exclusive_group()
    pad_grid_group.add_argument(
        "--pad-grid-strict",
        action="store_true",
        help=(
            "Use the fixed 0.05mm pad_grid tolerance (PR #3057 default) "
            "instead of auto-deriving per-board from the pad-offset "
            "histogram (issue #3061).  Default: auto-derive."
        ),
    )
    pad_grid_group.add_argument(
        "--pad-grid-tolerance",
        type=float,
        default=None,
        metavar="MM",
        help=(
            "Override the pad_grid L2 tolerance with an explicit value "
            "in mm (e.g. ``--pad-grid-tolerance 0.02``).  Disables "
            "auto-derivation."
        ),
    )


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
    sch_hierarchy.add_argument(
        "hierarchy_command",
        nargs="?",
        default="tree",
        choices=["tree", "list", "labels", "path", "stats", "validate"],
        help="Hierarchy subcommand (default: tree)",
    )
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

    # sch preflight
    sch_preflight = sch_subparsers.add_parser(
        "preflight", help="Pre-layout validation (footprint resolution, pin/pad, nets)"
    )
    sch_preflight.add_argument("schematic", help="Path to .kicad_sch file")
    sch_preflight.add_argument("--format", choices=["text", "json"], default="text")
    sch_preflight.add_argument("--strict", action="store_true", help="Exit with error on warnings")
    sch_preflight.add_argument("-q", "--quiet", action="store_true", help="Only show errors")

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
    sch_pins.add_argument(
        "--lib",
        required=False,
        default=None,
        help="Path to symbol library file (uses embedded lib_symbols when omitted)",
    )
    sch_pins.add_argument("--format", choices=["table", "json"], default="table")

    # sch pin-map
    sch_pin_map = sch_subparsers.add_parser("pin-map", help="Show resolved pin-to-net assignments")
    sch_pin_map.add_argument("schematic", help="Path to .kicad_sch file")
    sch_pin_map.add_argument("--ref", help="Filter by symbol reference (e.g., U1)")
    sch_pin_map.add_argument(
        "--sheet", help="Restrict to a specific sheet (name or path substring)"
    )
    sch_pin_map.add_argument(
        "--format", choices=["table", "json"], default="json", help="Output format (default: json)"
    )

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
    sch_unconnected.add_argument(
        "--no-check-netlist-export",
        dest="check_netlist_export",
        action="store_false",
        default=True,
        help="Skip the netlist-export cross-check (default: enabled)",
    )

    # sch replace
    sch_replace = sch_subparsers.add_parser("replace", help="Replace a symbol's library ID")
    sch_replace.add_argument("schematic", help="Path to .kicad_sch file")
    sch_replace.add_argument("reference", help="Symbol reference to replace (e.g., U1)")
    sch_replace.add_argument("new_lib_id", help="New library ID (e.g., 'mylib:NewSymbol')")
    sch_replace.add_argument("--value", help="New value for the symbol")
    sch_replace.add_argument("--footprint", help="New footprint")
    sch_replace.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    sch_replace.add_argument("--backup", action="store_true", help="Create backup before modifying")

    # sch set-footprint
    sch_set_fp = sch_subparsers.add_parser(
        "set-footprint", help="Set footprint assignments for symbols"
    )
    sch_set_fp.add_argument("schematic", help="Path to .kicad_sch file")
    sch_set_fp.add_argument("--ref", help="Symbol reference (e.g., U2, R1)")
    sch_set_fp.add_argument(
        "--footprint", help="Footprint to assign (e.g., Package_TO_SOT_SMD:SOT-23-5)"
    )
    sch_set_fp.add_argument(
        "--map",
        dest="map_file",
        help="Path to JSON or CSV mapping file (ref -> footprint)",
    )
    sch_set_fp.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying files"
    )
    sch_set_fp.add_argument("--backup", action="store_true", help="Create backup before modifying")
    sch_set_fp.add_argument(
        "--no-validate",
        dest="validate",
        action="store_false",
        default=True,
        help="Skip pin-count validation against the footprint library",
    )
    sch_set_fp.add_argument(
        "--strict",
        action="store_true",
        help="Fail on any pin-count mismatch, even in batch mode",
    )

    # sch assign-footprints
    sch_assign_fp = sch_subparsers.add_parser(
        "assign-footprints",
        help="Bulk-assign footprints to symbols missing one (auto + dry-run + json)",
    )
    sch_assign_fp.add_argument("schematic", help="Path to .kicad_sch file")
    sch_assign_fp.add_argument(
        "--auto",
        action="store_true",
        default=True,
        help=("Assign only unambiguous candidates (default; only mode currently supported)."),
    )
    sch_assign_fp.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview the proposed mapping without modifying files",
    )
    sch_assign_fp.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    sch_assign_fp.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max candidates considered per symbol (default: 20)",
    )
    sch_assign_fp.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip backup files on write",
    )
    sch_assign_fp.add_argument(
        "--no-validate",
        dest="validate",
        action="store_false",
        default=True,
        help="Skip pin-count validation on the resolved mapping",
    )
    sch_assign_fp.add_argument(
        "--include-power",
        action="store_true",
        help="Also consider power: symbols (default: skip)",
    )
    sch_assign_fp.add_argument(
        "--include-dnp",
        action="store_true",
        help="Also consider DNP symbols (default: skip)",
    )
    sch_assign_fp.add_argument(
        "--assign-missing",
        action="store_true",
        help=(
            "Resolve missing footprints with a deterministic value+package "
            "heuristic for standard SMD passives (no installed library "
            "required); unknown parts fail loud with a non-zero exit"
        ),
    )
    sch_assign_fp.add_argument(
        "--force",
        action="store_true",
        help="Reconsider symbols that already have a non-empty footprint",
    )
    sch_assign_fp.add_argument(
        "--no-project-lib",
        action="store_true",
        help=(
            "Ignore the project's fp-lib-table and only use global "
            "footprint libraries (CI / reproducibility opt-out)"
        ),
    )

    # sch suggest-footprint
    sch_suggest_fp = sch_subparsers.add_parser(
        "suggest-footprint",
        help="Suggest library footprints for a symbol by pin count / package",
    )
    sch_suggest_fp.add_argument("schematic", help="Path to .kicad_sch file")
    sch_suggest_fp.add_argument("--ref", required=True, help="Symbol reference (e.g., U7, R1)")
    sch_suggest_fp.add_argument(
        "--package",
        help="Package keyword hint to filter/rank candidates (e.g., SOT-23, R_0603)",
    )
    sch_suggest_fp.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format"
    )
    sch_suggest_fp.add_argument(
        "--limit", type=int, default=20, help="Maximum number of suggestions (default: 20)"
    )
    sch_suggest_fp.add_argument(
        "--no-project-lib",
        action="store_true",
        help=(
            "Ignore the project's fp-lib-table and only use global "
            "footprint libraries (CI / reproducibility opt-out)."
        ),
    )

    # sch set-value
    sch_set_val = sch_subparsers.add_parser("set-value", help="Set value property for symbols")
    sch_set_val.add_argument("schematic", help="Path to .kicad_sch file")
    sch_set_val.add_argument("--ref", help="Symbol reference (e.g., U4, R1)")
    sch_set_val.add_argument("--value", help="Value to assign (e.g., AP2204K-3.3TRG1)")
    sch_set_val.add_argument(
        "--map",
        dest="map_file",
        help="Path to JSON or CSV mapping file (ref -> value)",
    )
    sch_set_val.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying files"
    )
    sch_set_val.add_argument("--backup", action="store_true", help="Create backup before modifying")

    # sch set-reference
    sch_set_ref = sch_subparsers.add_parser(
        "set-reference", help="Rename component reference designators"
    )
    sch_set_ref.add_argument("schematic", help="Path to .kicad_sch file")
    sch_set_ref.add_argument("--ref", help="Current reference designator (e.g., LED3)")
    sch_set_ref.add_argument("--new-ref", help="New reference designator (e.g., D3)")
    sch_set_ref.add_argument(
        "--map",
        dest="map_file",
        help="Path to JSON or CSV mapping file (old_ref -> new_ref)",
    )
    sch_set_ref.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying files"
    )
    sch_set_ref.add_argument("--backup", action="store_true", help="Create backup before modifying")

    # sch set-symbol-property
    sch_set_prop = sch_subparsers.add_parser(
        "set-symbol-property",
        help="Set symbol-level boolean flags (on_board, in_bom, dnp, exclude_from_sim)",
    )
    sch_set_prop.add_argument("schematic", help="Path to .kicad_sch file")
    sch_set_prop.add_argument("--ref", required=True, help="Symbol reference (e.g., #PWR052, U1)")
    sch_set_prop.add_argument(
        "--property",
        dest="property_name",
        required=True,
        help="Flag to modify (on_board, in_bom, dnp, exclude_from_sim)",
    )
    sch_set_prop.add_argument("--value", required=True, help="Value to set (yes/no/true/false/1/0)")
    sch_set_prop.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying files"
    )
    sch_set_prop.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

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

    # sch set-label-direction
    sch_set_label_dir = sch_subparsers.add_parser(
        "set-label-direction", help="Change shape (direction) of global/hierarchical labels"
    )
    sch_set_label_dir.add_argument("schematic", help="Path to root .kicad_sch file")
    sch_set_label_dir.add_argument("--name", required=True, help="Label name to match")
    sch_set_label_dir.add_argument(
        "--shape",
        required=True,
        choices=("input", "output", "bidirectional", "tri_state", "passive"),
        help="New shape value",
    )
    sch_set_label_dir.add_argument(
        "--sheet", help="Restrict to a specific sheet (name or path substring)"
    )
    sch_set_label_dir.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying files"
    )
    sch_set_label_dir.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )
    # sch add-no-connect
    sch_add_nc = sch_subparsers.add_parser("add-no-connect", help="Add no-connect markers to pins")
    sch_add_nc.add_argument("schematic", help="Path to .kicad_sch file")
    sch_add_nc.add_argument("--ref", help="Symbol reference (e.g., U1)")
    sch_add_nc.add_argument("--pin", help="Pin number to mark")
    sch_add_nc.add_argument(
        "--auto", action="store_true", help="Auto-detect and mark all unconnected pins"
    )
    sch_add_nc.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    sch_add_nc.add_argument("--lib", action="append", dest="libs", help="Specific library file")
    sch_add_nc.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_add_nc.add_argument("--backup", action="store_true", help="Create backup before modifying")

    # sch add-component
    sch_add_comp = sch_subparsers.add_parser(
        "add-component", help="Add a component symbol to the schematic"
    )
    sch_add_comp.add_argument("schematic", help="Path to .kicad_sch file")
    sch_add_comp.add_argument(
        "--lib-id", required=True, help="Library symbol ID (e.g., Device:R, power:GND)"
    )
    sch_add_comp.add_argument("--reference", help="Symbol reference (e.g., R1, U1)")
    sch_add_comp.add_argument("--value", help="Component value (e.g., 10k, 100nF)")
    sch_add_comp.add_argument(
        "--footprint", help="Footprint name (e.g., Resistor_SMD:R_0402_1005Metric)"
    )
    sch_add_comp.add_argument(
        "--at",
        nargs=2,
        type=float,
        required=True,
        metavar=("X", "Y"),
        help="Placement coordinates",
    )
    sch_add_comp.add_argument(
        "--rotation", type=float, default=0, help="Rotation in degrees (default: 0)"
    )
    sch_add_comp.add_argument(
        "--mirror", choices=["x", "y"], default="", help="Mirror mode (x or y)"
    )
    sch_add_comp.add_argument(
        "--connect",
        action="append",
        dest="connects",
        metavar="PIN:X,Y",
        help="Connect pin to coordinates (e.g., 1:120,80). Repeatable.",
    )
    sch_add_comp.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    sch_add_comp.add_argument("--lib", action="append", dest="libs", help="Specific library file")
    sch_add_comp.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_add_comp.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    # sch add-bypass-cap
    sch_add_bypass = sch_subparsers.add_parser(
        "add-bypass-cap",
        help="Add a bypass/decoupling capacitor to an IC power pin",
    )
    sch_add_bypass.add_argument("schematic", help="Path to .kicad_sch file")
    sch_add_bypass.add_argument(
        "--ref", required=True, help="Target IC reference designator (e.g., U8)"
    )
    sch_add_bypass.add_argument(
        "--pin", required=True, help="Target pin number on that IC (e.g., 4)"
    )
    sch_add_bypass.add_argument("--value", default="100nF", help="Capacitor value (default: 100nF)")
    sch_add_bypass.add_argument(
        "--ground-net",
        default="GND",
        help="Ground power symbol name (default: GND)",
    )
    sch_add_bypass.add_argument(
        "--footprint",
        default="Capacitor_SMD:C_0402_1005Metric",
        help="Capacitor footprint (default: Capacitor_SMD:C_0402_1005Metric)",
    )
    sch_add_bypass.add_argument(
        "--reference", default=None, help="Capacitor reference (auto-assigned if omitted)"
    )
    sch_add_bypass.add_argument(
        "--offset",
        type=float,
        default=5.08,
        help="Distance from pin to cap body centre in mm (default: 5.08)",
    )
    sch_add_bypass.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_add_bypass.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    # sch add-pull-resistor
    sch_add_pull = sch_subparsers.add_parser(
        "add-pull-resistor",
        help="Add a pull-up or pull-down resistor to a schematic pin",
    )
    sch_add_pull.add_argument("schematic", help="Path to .kicad_sch file")
    sch_add_pull.add_argument(
        "--ref", required=True, help="Symbol reference of target IC (e.g., U5)"
    )
    sch_add_pull.add_argument("--pin", required=True, help="Pin number on target IC (e.g., 25)")
    sch_add_pull.add_argument(
        "--direction",
        required=True,
        choices=["up", "down"],
        help="Pull-up (power) or pull-down (ground)",
    )
    sch_add_pull.add_argument("--value", required=True, help="Resistor value (e.g., 10k)")
    sch_add_pull.add_argument(
        "--power-net",
        dest="power_net",
        help="Power/ground net name (default: +3.3V for up, GND for down)",
    )
    sch_add_pull.add_argument(
        "--reference",
        help="Reference designator for new resistor (default: R? auto-assign)",
    )
    sch_add_pull.add_argument(
        "--footprint",
        default="Resistor_SMD:R_0402_1005Metric",
        help="Resistor footprint (default: Resistor_SMD:R_0402_1005Metric)",
    )
    sch_add_pull.add_argument(
        "--offset",
        type=float,
        default=5.08,
        help="Grid distance from IC pin to resistor center in mm (default: 5.08)",
    )
    sch_add_pull.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    sch_add_pull.add_argument("--lib", action="append", dest="libs", help="Specific library file")
    sch_add_pull.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_add_pull.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )
    sch_add_pull.add_argument(
        "--force", action="store_true", help="Place even if collision detected"
    )

    # sch add-wire
    sch_add_wire = sch_subparsers.add_parser("add-wire", help="Add wire segments to the schematic")
    sch_add_wire.add_argument("schematic", help="Path to .kicad_sch file")
    sch_add_wire.add_argument(
        "--from",
        nargs=2,
        type=float,
        required=True,
        dest="start",
        metavar=("X", "Y"),
        help="Start coordinate",
    )
    sch_add_wire.add_argument(
        "--to",
        nargs=2,
        type=float,
        action="append",
        required=True,
        metavar=("X", "Y"),
        help="End coordinate (repeatable for multi-segment wires)",
    )
    sch_add_wire.add_argument(
        "--junction",
        action="store_true",
        help="Auto-insert junctions where endpoints land on existing wire midpoints",
    )
    sch_add_wire.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_add_wire.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    # sch add-junction
    sch_add_junc = sch_subparsers.add_parser("add-junction", help="Add a junction to the schematic")
    sch_add_junc.add_argument("schematic", help="Path to .kicad_sch file")
    sch_add_junc.add_argument(
        "--at",
        nargs=2,
        type=float,
        required=True,
        metavar=("X", "Y"),
        help="Junction coordinates",
    )
    sch_add_junc.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_add_junc.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    # sch add-label
    sch_add_label = sch_subparsers.add_parser(
        "add-label", help="Add a label (local, global, or hierarchical) to the schematic"
    )
    sch_add_label.add_argument("schematic", help="Path to .kicad_sch file")
    sch_add_label.add_argument(
        "--type",
        required=True,
        choices=["global", "local", "hierarchical"],
        help="Label type",
    )
    sch_add_label.add_argument("--name", required=True, help="Label text / net name")
    sch_add_label.add_argument(
        "--at",
        nargs=2,
        type=float,
        required=True,
        metavar=("X", "Y"),
        help="Placement coordinates",
    )
    sch_add_label.add_argument(
        "--shape",
        choices=["input", "output", "bidirectional", "tri_state", "passive"],
        default=None,
        help="Label shape (global and hierarchical only)",
    )
    sch_add_label.add_argument(
        "--rotation", type=float, default=0, help="Rotation in degrees (default: 0)"
    )
    sch_add_label.add_argument(
        "--connect",
        action="append",
        dest="connects",
        metavar="X,Y",
        help="Draw wire from label to target coordinates (e.g., 120,80). Repeatable.",
    )
    sch_add_label.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_add_label.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    # sch cleanup-wires
    sch_cleanup = sch_subparsers.add_parser(
        "cleanup-wires", help="Remove zero-length and dangling wires"
    )
    sch_cleanup.add_argument("schematic", help="Path to .kicad_sch file")
    sch_cleanup.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_cleanup.add_argument("--backup", action="store_true", help="Create backup before modifying")
    sch_cleanup.add_argument("--format", choices=["text", "json"], default="text")
    sch_cleanup.add_argument(
        "--stub-threshold",
        type=float,
        default=1.27,
        dest="stub_threshold",
        help="Max length (mm) for single-end-dangling stubs to remove (default: 1.27, 0 to disable)",
    )

    # sch remove-wire
    sch_remove_wire = sch_subparsers.add_parser(
        "remove-wire", help="Remove a specific wire segment"
    )
    sch_remove_wire.add_argument("schematic", help="Path to .kicad_sch file")
    sch_remove_wire.add_argument(
        "--from",
        nargs=2,
        type=float,
        dest="from_pt",
        metavar=("X", "Y"),
        help="Start endpoint of wire to remove",
    )
    sch_remove_wire.add_argument(
        "--to",
        nargs=2,
        type=float,
        dest="to_pt",
        metavar=("X", "Y"),
        help="End endpoint of wire to remove",
    )
    sch_remove_wire.add_argument(
        "--near",
        nargs=2,
        type=float,
        metavar=("X", "Y"),
        help="Find and remove wire nearest to this point",
    )
    sch_remove_wire.add_argument(
        "--tolerance",
        type=float,
        default=1.27,
        help="Coordinate matching tolerance in mm (default: 1.27)",
    )
    sch_remove_wire.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_remove_wire.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )
    sch_remove_wire.add_argument("--format", choices=["text", "json"], default="text")

    # sch insert-inline
    sch_insert_inline = sch_subparsers.add_parser(
        "insert-inline",
        help="Break a wire and insert a component inline in a signal path",
    )
    sch_insert_inline.add_argument("schematic", help="Path to .kicad_sch file")
    sch_insert_inline.add_argument(
        "--lib-id", required=True, help="Library symbol ID (e.g., Device:D)"
    )
    sch_insert_inline.add_argument("--reference", help="Symbol reference (e.g., D1)")
    sch_insert_inline.add_argument("--value", default="", help="Component value (e.g., BAT54)")
    sch_insert_inline.add_argument("--footprint", default="", help="Footprint name")
    sch_insert_inline.add_argument(
        "--from",
        nargs=2,
        type=float,
        dest="from_pt",
        metavar=("X", "Y"),
        help="Start endpoint of the target wire",
    )
    sch_insert_inline.add_argument(
        "--to",
        nargs=2,
        type=float,
        dest="to_pt",
        metavar=("X", "Y"),
        help="End endpoint of the target wire",
    )
    sch_insert_inline.add_argument(
        "--near",
        nargs=2,
        type=float,
        metavar=("X", "Y"),
        help="Find target wire nearest to this point",
    )
    sch_insert_inline.add_argument("--pin-a", default="1", help="Upstream pin number (default: 1)")
    sch_insert_inline.add_argument(
        "--pin-b", default="2", help="Downstream pin number (default: 2)"
    )
    sch_insert_inline.add_argument(
        "--rotation",
        type=float,
        default=None,
        help="Symbol rotation in degrees (auto-detected if omitted)",
    )
    sch_insert_inline.add_argument(
        "--expand-gap",
        action="store_true",
        help="Shift downstream geometry if wire is too short for the component",
    )
    sch_insert_inline.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    sch_insert_inline.add_argument(
        "--lib", action="append", dest="libs", help="Specific library file"
    )
    sch_insert_inline.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_insert_inline.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    # sch disconnect
    sch_disconnect = sch_subparsers.add_parser("disconnect", help="Disconnect a pin from its net")
    sch_disconnect.add_argument("schematic", help="Path to .kicad_sch file")
    sch_disconnect.add_argument("--ref", required=True, help="Symbol reference (e.g., U1)")
    sch_disconnect.add_argument("--pin", required=True, help="Pin number to disconnect")
    sch_disconnect.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    sch_disconnect.add_argument("--lib", action="append", dest="libs", help="Specific library file")
    sch_disconnect.add_argument(
        "--add-nc", action="store_true", help="Add no-connect marker after disconnecting"
    )
    sch_disconnect.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_disconnect.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    # sch reconnect-pin
    sch_reconnect_pin = sch_subparsers.add_parser(
        "reconnect-pin", help="Reconnect a pin from one net to another"
    )
    sch_reconnect_pin.add_argument("schematic", help="Path to .kicad_sch file")
    sch_reconnect_pin.add_argument("--ref", required=True, help="Symbol reference (e.g., C41)")
    sch_reconnect_pin.add_argument("--pin", required=True, help="Pin number to reconnect")
    sch_reconnect_pin.add_argument("--to-net", required=True, help="Target net name (e.g., GNDD)")
    sch_reconnect_pin.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    sch_reconnect_pin.add_argument(
        "--lib", action="append", dest="libs", help="Specific library file"
    )
    sch_reconnect_pin.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_reconnect_pin.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    # sch move-component
    sch_move_component = sch_subparsers.add_parser(
        "move-component", help="Move a symbol to a new position"
    )
    sch_move_component.add_argument("schematic", help="Path to .kicad_sch file")
    sch_move_component.add_argument(
        "--ref", required=True, help="Symbol reference designator (e.g., U1)"
    )
    sch_move_component.add_argument(
        "--to",
        nargs=2,
        type=float,
        required=True,
        metavar=("X", "Y"),
        help="New position coordinates",
    )
    sch_move_component.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    sch_move_component.add_argument(
        "--lib", action="append", dest="libs", help="Specific library file"
    )
    sch_move_component.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_move_component.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )
    sch_move_component.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format"
    )

    # sch remove-component
    sch_remove_component = sch_subparsers.add_parser(
        "remove-component", help="Remove a symbol from a schematic"
    )
    sch_remove_component.add_argument("schematic", help="Path to .kicad_sch file")
    sch_remove_component.add_argument(
        "--ref", required=True, help="Symbol reference designator (e.g., U1)"
    )
    sch_remove_component.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    sch_remove_component.add_argument(
        "--lib", action="append", dest="libs", help="Specific library file"
    )
    sch_remove_component.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    sch_remove_component.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )
    sch_remove_component.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format"
    )

    # sch re-annotate
    sch_reannotate = sch_subparsers.add_parser(
        "re-annotate", help="Renumber reference designators sequentially"
    )
    sch_reannotate.add_argument("schematic", help="Path to .kicad_sch file")
    sch_reannotate.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying"
    )
    sch_reannotate.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )
    sch_reannotate.add_argument(
        "--prefix",
        help="Comma-separated list of prefixes to renumber (e.g., R,C,U)",
    )
    sch_reannotate.add_argument(
        "--start-from",
        type=int,
        default=1,
        help="Starting number for each prefix (default: 1)",
    )
    sch_reannotate.add_argument(
        "--per-sheet",
        action="store_true",
        help="Restart numbering per sheet instead of continuous",
    )
    sch_reannotate.add_argument(
        "--unannotated-only",
        action="store_true",
        help="Only assign numbers to unannotated (?) references",
    )
    sch_reannotate.add_argument("--format", choices=["text", "json"], default="text")

    # sch repair-instances
    sch_repair_instances = sch_subparsers.add_parser(
        "repair-instances",
        help="Fix symbols missing project instances blocks",
    )
    sch_repair_instances.add_argument("schematic", help="Path to .kicad_sch file")
    sch_repair_instances.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview changes without modifying files",
    )
    sch_repair_instances.add_argument(
        "--backup",
        action="store_true",
        help="Create backup before modifying",
    )
    sch_repair_instances.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
    )

    # sch fix-annotation
    sch_fix_annotation = sch_subparsers.add_parser(
        "fix-annotation",
        help=("Hierarchy-aware power/flag annotation repair (net-neutrality gated)"),
    )
    sch_fix_annotation.add_argument("schematic", help="Path to root .kicad_sch file")
    sch_fix_annotation.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview changes without modifying files",
    )
    sch_fix_annotation.add_argument(
        "--backup",
        action="store_true",
        help="Create backup before modifying",
    )
    sch_fix_annotation.add_argument(
        "--skip-net-check",
        action="store_true",
        help="Skip the net-neutrality gate (unsafe; e.g. no kicad-cli)",
    )
    sch_fix_annotation.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
    )


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
    pcb_nets.add_argument(
        "--check-connectivity",
        action="store_true",
        default=False,
        help="Run island detection to find disconnected segments within each net",
    )

    # pcb padmap
    pcb_padmap = pcb_subparsers.add_parser("padmap", help="Show per-footprint pad-to-net bindings")
    pcb_padmap.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_padmap.add_argument("--format", choices=["text", "json"], default="text")
    pcb_padmap.add_argument("--ref", help="Scope to a single footprint reference")
    pcb_padmap.add_argument("--net", help="Invert to net-owning-pads mode for this net")

    # pcb traces
    pcb_traces = pcb_subparsers.add_parser("traces", help="Show trace statistics")
    pcb_traces.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_traces.add_argument("--format", choices=["text", "json"], default="text")
    pcb_traces.add_argument("--layer", help="Filter by layer (e.g., F.Cu)")

    # pcb stackup
    pcb_stackup = pcb_subparsers.add_parser("stackup", help="Show layer stackup")
    pcb_stackup.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_stackup.add_argument("--format", choices=["text", "json"], default="text")

    # pcb strip
    pcb_strip = pcb_subparsers.add_parser(
        "strip",
        help="Remove traces while preserving placement",
        description="Remove trace segments and vias from a PCB while preserving "
        "component placement, zones, and other board elements. Useful for "
        "re-routing a board from scratch with different routing strategies.",
    )
    pcb_strip.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_strip.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: overwrite input or add -stripped suffix)",
    )
    pcb_strip.add_argument(
        "--nets",
        help="Comma-separated list of net names to strip (default: all nets)",
    )
    pcb_strip.add_argument(
        "--layers",
        help="Comma-separated list of layer names to strip "
        "(e.g. In1.Cu,In2.Cu). Only segments on these layers are removed. "
        "Vias are removed only when ALL their layers are in the set.",
    )
    pcb_strip.add_argument(
        "--include-power",
        action="store_true",
        default=False,
        help="Include power/ground nets in stripping (default: exclude them)",
    )
    pcb_strip.add_argument(
        "--power-pattern",
        help="Regex pattern for power net names (overrides built-in heuristic)",
    )
    pcb_strip.add_argument(
        "--remove-orphan-vias",
        action="store_true",
        default=False,
        help="Remove vias that no longer connect to any remaining segment "
        "on either of their layers (only meaningful with --layers)",
    )
    pcb_strip.add_argument(
        "--region",
        help="Spatially bound the strip to an axis-aligned box "
        "'x1,y1,x2,y2' (board-relative mm). Only geometry inside the box is "
        "removed, ANDed with --nets/--layers. Vias are stripped when their "
        "point is inside; segments fully inside are removed; segments crossing "
        "the boundary are clipped to the outside portion; segments spanning the "
        "box with both endpoints outside are left untouched and reported as "
        "'boundary_skipped'. Zones are only removed (with --no-keep-zones) when "
        "their whole polygon is inside the box (no polygon clipping).",
    )
    pcb_strip.add_argument(
        "--no-keep-zones",
        dest="keep_zones",
        action="store_false",
        default=True,
        help="Also remove copper pour zones (default: keep zones)",
    )
    pcb_strip.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_strip.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without modifying files",
    )

    # pcb reinforce (issue #4220 -- Unit A of #4218)
    pcb_reinforce = pcb_subparsers.add_parser(
        "reinforce",
        help="Emit a spaced same-net PTH anchor row along a routed net",
        description="Post-route buttress-wire reinforcement (Unit A of #4218). "
        "Walks a routed net's segments into ordered polylines and emits a "
        "spaced row of same-net plated through-hole (PTH) anchor vias along the "
        "longest run, sized to a wire gauge and respecting the annular-ring "
        "floor. A solid-core wire is later soldered through the anchor row to "
        "carry additional current. Anchors are same-net (no shorts); a candidate "
        "that would collide with a DIFFERENT net is refused and reported, not "
        "placed. NOTE: anchors are same-net vias -- existing zone pours must be "
        "refilled afterward (e.g. `kicad-cli pcb drc --refill-zones`) so the "
        "pours knock out around them. In-place by default; use -o to write "
        "elsewhere.",
    )
    pcb_reinforce.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_reinforce.add_argument(
        "--net",
        required=True,
        help="Net name to reinforce (must have routed copper)",
    )
    pcb_reinforce.add_argument(
        "--wire-gauge",
        type=int,
        default=16,
        help="Buttress-wire gauge in AWG (default: 16; also supports 14, 12)",
    )
    pcb_reinforce.add_argument(
        "--spacing",
        type=float,
        default=15.0,
        help="Arc-length spacing between mid-run anchors, in mm (default: 15.0)",
    )
    pcb_reinforce.add_argument(
        "--layer",
        help="Restrict to a copper layer (default: layer with the most "
        "cumulative routed length for the net)",
    )
    pcb_reinforce.add_argument(
        "--all-runs",
        dest="all_runs",
        action="store_true",
        help="Anchor EVERY chained run on the target layer (multi-branch HV "
        "nets), not just the single longest run. Default anchors only the "
        "longest run.",
    )
    pcb_reinforce.add_argument(
        "--min-run-length",
        dest="min_run_length",
        type=float,
        default=None,
        help="Only anchor runs at least this long, in mm. Shorter runs are "
        "reported but not anchored. Composes with --all-runs.",
    )
    pcb_reinforce.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: modify in place)",
    )
    pcb_reinforce.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_reinforce.add_argument(
        "--dry-run",
        action="store_true",
        help="Report anchors that would be placed/refused without writing files",
    )

    # pcb dedupe (issue #4175)
    pcb_dedupe = pcb_subparsers.add_parser(
        "dedupe",
        help="Remove exact-duplicate copper (segments + vias)",
        description="Remove pre-existing exact-duplicate copper from a board: "
        "segments and vias that share the same net, geometry, and layer(s) "
        "(differing only in uuid). Such duplicates are invisible to netlist "
        "connectivity but silently bloat the file and slow later DRC/optimize "
        "passes. One instance of each distinct geometry is kept, so net "
        "connectivity is unchanged. In-place by default; use -o to write "
        "elsewhere.",
    )
    pcb_dedupe.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_dedupe.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: overwrite input in place)",
    )
    pcb_dedupe.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_dedupe.add_argument(
        "--dry-run",
        action="store_true",
        help="Report duplicates without modifying files",
    )

    # pcb reannotate
    pcb_reannotate = pcb_subparsers.add_parser(
        "reannotate",
        help="Batch rename reference designators",
        description="Rename reference designators in a PCB using a JSON mapping file. "
        "Handles collision chains (e.g., C6->C10 while C10->C15) safely using "
        "temporary intermediate references.",
    )
    pcb_reannotate.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_reannotate.add_argument(
        "--map",
        required=True,
        help='Path to JSON mapping file (e.g., {"C1": "C10", "C10": "C15"})',
    )
    pcb_reannotate.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: overwrite input)",
    )
    pcb_reannotate.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_reannotate.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview renames without modifying the PCB file",
    )

    # pcb sync-netlist
    pcb_sync_netlist = pcb_subparsers.add_parser(
        "sync-netlist",
        help="Sync PCB footprints from schematic netlist",
        description="Compare schematic components against PCB footprints. "
        "Adds missing footprints (placed at board edge), detects renamed "
        "references, and reports orphaned footprints.",
    )
    pcb_sync_netlist.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_sync_netlist.add_argument(
        "--schematic",
        required=True,
        help="Path to root .kicad_sch file",
    )
    pcb_sync_netlist.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: overwrite input PCB)",
    )
    pcb_sync_netlist.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_sync_netlist.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview sync actions without modifying the PCB file",
    )
    pcb_sync_netlist.add_argument(
        "--remove-orphans",
        action="store_true",
        help="Delete orphaned footprints (in PCB but not in schematic)",
    )
    pcb_sync_netlist.add_argument(
        "--force",
        action="store_true",
        help="Remove orphans even if they have routed traces",
    )
    pcb_sync_netlist.add_argument(
        "--auto-rename",
        action="store_true",
        help="Apply reference renames without interactive confirmation",
    )
    pcb_sync_netlist.add_argument(
        "--remove-orphan-nets",
        action="store_true",
        help="Remove nets with no pad references after net assignment",
    )

    # pcb zones
    pcb_zones = pcb_subparsers.add_parser("zones", help="List copper pour zones")
    pcb_zones.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_zones.add_argument("--format", choices=["text", "json"], default="text")

    # pcb add-3d-models
    pcb_add_models = pcb_subparsers.add_parser(
        "add-3d-models",
        help="Add missing (model ...) 3D refs from the installed KiCad libraries",
        description="Patch missing (model ...) 3D model references into PCB "
        "footprints by copying them from the installed KiCad footprint "
        "libraries (.kicad_mod sources). Pure metadata insertion: copper, "
        "placement, zones and nets are untouched, so DRC results are "
        "identical. Makes kicad-cli pcb render / the KiCad 3D viewer show "
        "component bodies instead of a bare board.",
    )
    pcb_add_models.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_add_models.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: patch input in place)",
    )
    pcb_add_models.add_argument(
        "--lib-path",
        help="Explicit KiCad footprints directory (default: auto-detect; "
        "KICAD_FOOTPRINT_DIR is also honored)",
    )
    pcb_add_models.add_argument(
        "--exact",
        action="store_true",
        help="Require exact footprint-name matches (disable the same-library "
        "name-variant fallback used for visual model lookup)",
    )
    pcb_add_models.add_argument(
        "--lcsc-models",
        dest="lcsc_models",
        help="Path to an lcsc_models.json sidecar mapping lib_id -> LCSC "
        "C-number, enabling the LCSC/EasyEDA fetch-on-demand tier. Bodies are "
        "resolved from a cache dir (${KCT_LCSC_3D_DIR}, default "
        "~/.cache/kicad-tools/lcsc-3d/) and emitted as portable "
        "${KCT_LCSC_3D_DIR}/C#####.step refs",
    )
    pcb_add_models.add_argument(
        "--fetch-lcsc",
        action="store_true",
        help="Fetch missing LCSC STEP bodies from EasyEDA on a cache miss "
        "(default: cache-only, no network; also enabled by KCT_LCSC_FETCH=1)",
    )
    pcb_add_models.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_add_models.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be inserted without modifying the PCB file",
    )

    # pcb remove-footprint
    pcb_remove_fp = pcb_subparsers.add_parser(
        "remove-footprint",
        help="Remove a footprint from the PCB by reference designator",
        description="Remove a specific footprint from a PCB file. "
        "By default, refuses to remove footprints with routed traces "
        "unless --force is specified.",
    )
    pcb_remove_fp.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_remove_fp.add_argument(
        "--ref",
        required=True,
        help="Reference designator of footprint to remove (e.g., C1)",
    )
    pcb_remove_fp.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: overwrite input PCB)",
    )
    pcb_remove_fp.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_remove_fp.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview removal without modifying the PCB file",
    )
    pcb_remove_fp.add_argument(
        "--force",
        action="store_true",
        help="Remove footprint even if it has routed traces",
    )

    # pcb move-footprint
    pcb_move_fp = pcb_subparsers.add_parser(
        "move-footprint",
        help="Move a footprint to new coordinates (board-relative by default)",
        description="Relocate a footprint to a new position (and optionally "
        "rotation) by reference designator.  Supports batch mode via --map.  "
        "Coordinates are board-relative by default (measured from the "
        "Edge.Cuts top-left origin); pass --absolute to use KiCad page "
        "coordinates instead.",
    )
    pcb_move_fp.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_move_fp.add_argument(
        "--ref",
        help="Reference designator of footprint to move (e.g., J2)",
    )
    pcb_move_fp.add_argument(
        "--to",
        nargs=2,
        type=float,
        metavar=("X", "Y"),
        help="New position in mm. Board-relative by default (measured from the "
        "Edge.Cuts top-left origin); the board-origin offset is added "
        "automatically. Pass --absolute to give KiCad page coordinates "
        "directly. Example (board-relative): --to 10 10",
    )
    pcb_move_fp.add_argument(
        "--rotation",
        type=float,
        help="New rotation in degrees (optional)",
    )
    pcb_move_fp.add_argument(
        "--absolute",
        action="store_true",
        help="Interpret --to / --map coordinates as absolute KiCad page "
        "coordinates (the same space as a footprint's raw (at ...) node) "
        "instead of board-relative. Default: board-relative.",
    )
    pcb_move_fp.add_argument(
        "--map",
        dest="batch_map",
        help="JSON map for batch moves; x/y are board-relative by default "
        "(or absolute page coords with --absolute), "
        'e.g. \'{"J2": {"x": 10, "y": 10}}\'',
    )
    pcb_move_fp.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: overwrite input PCB)",
    )
    pcb_move_fp.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_move_fp.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview moves without modifying the PCB file",
    )

    # pcb page-fit
    pcb_page_fit = pcb_subparsers.add_parser(
        "page-fit",
        help="Resize the drawing sheet to fit the board and center it",
        description='Rewrite the (paper ...) node to a tight (paper "User" W H) '
        "sized to the Edge.Cuts bounding box plus a uniform margin, and "
        "translate all board items so the board is centered with that margin. "
        "Pure geometric transform -- routing/DRC preserved, no re-route needed.",
    )
    pcb_page_fit.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_page_fit.add_argument(
        "--margin",
        type=float,
        default=5.0,
        help="Margin around the board in mm (default: 5.0)",
    )
    pcb_page_fit.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: overwrite input PCB)",
    )
    pcb_page_fit.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_page_fit.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the new page size without modifying the PCB file",
    )

    # pcb center-on-sheet
    pcb_center_sheet = pcb_subparsers.add_parser(
        "center-on-sheet",
        help="Center the board in the drawing sheet's usable area "
        "(inside the frame, above the title block)",
        description="Rigidly translate ALL board geometry by a single "
        "grid-snapped (dx, dy) so the Edge.Cuts bounding box is centered in "
        "the sheet's usable drawing area: inside the 10 mm frame border and "
        "above the 35 mm title-block band (KiCad default worksheet). "
        '(paper "User" W H) sheets are upgraded to the smallest standard '
        "landscape size that fits the board with 15 mm slack per side. "
        "Pure text transform with exact decimal arithmetic: only coordinate "
        "atoms change, so routing, 45-degree copper and DRC results are "
        "preserved exactly.",
    )
    pcb_center_sheet.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_center_sheet.add_argument(
        "--paper",
        default="auto",
        help="Sheet handling: 'auto' (default: keep a fitting standard sheet, "
        "upgrade User/too-small sheets), 'keep' (never change the sheet), or "
        "an explicit landscape size (A4, A3, A2, A1, A0)",
    )
    pcb_center_sheet.add_argument(
        "--margin",
        type=float,
        default=None,
        help="Frame border inset per side in mm (default: 10)",
    )
    pcb_center_sheet.add_argument(
        "--title-block",
        dest="title_block",
        type=float,
        default=None,
        help="Reserved title-block band height above the bottom frame line "
        "in mm (default: 35 -- KiCad default worksheet block is 34 mm)",
    )
    pcb_center_sheet.add_argument(
        "--grid",
        type=float,
        default=None,
        help="Grid to snap the translation delta to in mm (default: 0.05)",
    )
    pcb_center_sheet.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: modify input PCB in place)",
    )
    pcb_center_sheet.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_center_sheet.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the would-be transform without modifying the PCB file",
    )

    # pcb lock-footprints / unlock-footprints
    for _cmd_name, _help_verb in (
        ("lock-footprints", "Lock"),
        ("unlock-footprints", "Unlock"),
    ):
        _lock_fp = pcb_subparsers.add_parser(
            _cmd_name,
            help=f"{_help_verb} footprints by reference (for anchor-weight recipe)",
            description=(
                f"{_help_verb} footprints in a PCB so the anchor-weight "
                "recipe (kct optimize-placement --anchor-weight 1.0 "
                "--allow-infeasible) can identify perimeter anchors. "
                "Use --refs for explicit selection or --all-perimeter to "
                "target every footprint whose bounding box touches the "
                "Edge.Cuts outline.  Idempotent."
            ),
        )
        _lock_fp.add_argument("pcb", help="Path to .kicad_pcb file")
        _lock_fp.add_argument(
            "--refs",
            help="Comma-separated reference designators (e.g. J1,J2,MH1)",
        )
        _lock_fp.add_argument(
            "--all-perimeter",
            action="store_true",
            help="Target all footprints whose bbox touches the board edge",
        )
        _lock_fp.add_argument(
            "--perimeter-margin",
            type=float,
            default=None,
            help=(
                "Tolerance in mm for the --all-perimeter test "
                "(default: 2.0). Set higher to include footprints "
                "set further inboard."
            ),
        )
        _lock_fp.add_argument(
            "-o",
            "--output",
            dest="output",
            help="Output file path (default: overwrite input PCB)",
        )
        _lock_fp.add_argument(
            "--format",
            choices=["text", "json"],
            default="text",
            help="Output format for results",
        )
        _lock_fp.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without modifying the PCB file",
        )

    # pcb add-zone
    pcb_add_zone = pcb_subparsers.add_parser(
        "add-zone",
        help="Add a copper pour zone to the PCB",
        description="Create a copper pour zone on a specified layer and net. "
        "By default the zone boundary follows the board outline (--fill-board). "
        "Use --rect with --origin and --size to specify a rectangular boundary.",
    )
    pcb_add_zone.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_add_zone.add_argument(
        "--net",
        required=True,
        help="Net name for the zone (e.g., GND, +3.3V)",
    )
    pcb_add_zone.add_argument(
        "--layer",
        required=True,
        help="Copper layer (e.g., F.Cu, B.Cu, In1.Cu)",
    )
    pcb_add_zone.add_argument(
        "--priority",
        type=int,
        default=0,
        help="Zone fill priority (higher = fills later, default: 0)",
    )
    pcb_add_zone.add_argument(
        "--min-clearance",
        type=float,
        default=0.3,
        help="Clearance to other nets in mm (default: 0.3)",
    )
    pcb_add_zone.add_argument(
        "--thermal-relief-gap",
        type=float,
        default=0.3,
        help="Thermal relief gap in mm (default: 0.3)",
    )
    pcb_add_zone.add_argument(
        "--thermal-relief-width",
        type=float,
        default=0.4,
        help="Thermal relief spoke width in mm (default: 0.4)",
    )
    pcb_add_zone.add_argument(
        "--min-thickness",
        type=float,
        default=0.25,
        help="Minimum copper thickness in mm (default: 0.25)",
    )
    pcb_add_zone.add_argument(
        "--fill-board",
        action="store_true",
        default=False,
        help="Use board outline as zone boundary (this is the default behavior)",
    )
    pcb_add_zone.add_argument(
        "--rect",
        action="store_true",
        default=False,
        help="Use a rectangular zone boundary (requires --origin and --size)",
    )
    pcb_add_zone.add_argument(
        "--origin",
        type=float,
        nargs=2,
        metavar=("X", "Y"),
        help="Rectangle origin in mm (bottom-left corner)",
    )
    pcb_add_zone.add_argument(
        "--size",
        type=float,
        nargs=2,
        metavar=("W", "H"),
        help="Rectangle size in mm (width height)",
    )
    pcb_add_zone.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: overwrite input, consistent with 'zones add')",
    )
    pcb_add_zone.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing output",
    )
    pcb_add_zone.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )

    # pcb snap-rotation
    pcb_snap_rot = pcb_subparsers.add_parser(
        "snap-rotation",
        help="Normalize component rotation angles to cardinal directions",
        description="Snap footprint rotations to the nearest multiple of a grid angle "
        "(default 90 degrees). Useful for cleaning up arbitrary rotations left "
        "by placement optimization.",
    )
    pcb_snap_rot.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_snap_rot.add_argument(
        "--grid",
        type=float,
        default=90.0,
        help="Rotation grid in degrees (default: 90). Use 45 for 45-degree snapping.",
    )
    pcb_snap_rot.add_argument(
        "--tolerance",
        type=float,
        default=None,
        help="Maximum angular distance (degrees) from the nearest grid angle to snap. "
        "Footprints further from the grid than this are left unchanged.",
    )
    pcb_snap_rot.add_argument(
        "--exclude",
        default=None,
        help="Comma-separated reference designators to skip (e.g., --exclude U8,J1)",
    )
    pcb_snap_rot.add_argument(
        "--only",
        default=None,
        help="Comma-separated reference designators to snap (only these are modified)",
    )
    pcb_snap_rot.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: overwrite input)",
    )
    pcb_snap_rot.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_snap_rot.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview rotation changes without modifying the PCB file",
    )

    # pcb edit-outline
    pcb_edit_outline = pcb_subparsers.add_parser(
        "edit-outline",
        help="List, remove, or replace Edge.Cuts board outlines",
        description="Manage Edge.Cuts contours in a PCB file. "
        "Supports listing contours, removing specific contours by index, "
        "keeping only a specific contour, and replacing all outlines with "
        "a new rectangle. Mounting hole circles are preserved by default.",
    )
    pcb_edit_outline.add_argument("pcb", help="Path to .kicad_pcb file")

    outline_action = pcb_edit_outline.add_mutually_exclusive_group(required=True)
    outline_action.add_argument(
        "--list",
        dest="list_contours",
        action="store_true",
        help="List all Edge.Cuts contours with bounding boxes",
    )
    outline_action.add_argument(
        "--remove-outline",
        type=int,
        metavar="INDEX",
        help="Remove a contour by its index",
    )
    outline_action.add_argument(
        "--keep-only",
        type=int,
        metavar="INDEX",
        help="Remove all contours except the one at INDEX (preserves mounting holes)",
    )
    outline_action.add_argument(
        "--set-outline",
        choices=["rect"],
        help="Replace all outlines with a new shape (preserves mounting holes)",
    )

    pcb_edit_outline.add_argument(
        "--origin",
        nargs=2,
        type=float,
        metavar=("X", "Y"),
        help="Origin (top-left) for --set-outline in mm",
    )
    pcb_edit_outline.add_argument(
        "--size",
        nargs=2,
        type=float,
        metavar=("W", "H"),
        help="Width and height for --set-outline in mm",
    )
    pcb_edit_outline.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: overwrite input PCB)",
    )
    pcb_edit_outline.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_edit_outline.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying the PCB file",
    )

    # pcb net-audit
    pcb_net_audit = pcb_subparsers.add_parser(
        "net-audit",
        help="Detect stale/duplicate net names from KiCad version changes",
        description="Detect and optionally fix stale net names that arise when "
        "a PCB is round-tripped through different KiCad versions. "
        "Old-style Net-(REF-PadN) and new-style Net-(REF-N) names "
        "for the same logical net are detected as duplicates.",
    )
    pcb_net_audit.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_net_audit.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for results",
    )
    pcb_net_audit.add_argument(
        "--fix",
        action="store_true",
        help="Reassign pads from stale nets to active nets",
    )
    pcb_net_audit.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview fixes without modifying the PCB file (implies --fix)",
    )
    pcb_net_audit.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output file path (default: overwrite input PCB)",
    )

    # pcb export-dsn
    pcb_export_dsn = pcb_subparsers.add_parser(
        "export-dsn",
        help="Export PCB to Specctra DSN format for Freerouting",
        description="Export a .kicad_pcb file to Specctra DSN format, suitable "
        "for routing with Freerouting or other DSN-compatible autorouters.",
    )
    pcb_export_dsn.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_export_dsn.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output DSN file path (default: <pcb-stem>.dsn next to input)",
    )

    # pcb import-ses
    pcb_import_ses = pcb_subparsers.add_parser(
        "import-ses",
        help="Import Specctra SES routes into a PCB",
        description="Import a Freerouting .ses session file and merge the "
        "routed traces back into a KiCad .kicad_pcb file.",
    )
    pcb_import_ses.add_argument("pcb", help="Path to .kicad_pcb file")
    pcb_import_ses.add_argument("ses", help="Path to .ses file from Freerouting")
    pcb_import_ses.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output PCB file path (default: overwrite input PCB)",
    )


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

    # lib purge
    lib_purge = lib_subparsers.add_parser(
        "purge", help="List unused symbols/footprints in project-local libraries"
    )
    lib_purge.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        help="Path to KiCad project directory (default: current directory)",
    )
    lib_purge.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )


def _add_footprint_parser(subparsers) -> None:
    """Add footprint subcommand parser."""
    footprint_parser = subparsers.add_parser("footprint", help="Footprint generation and tools")
    footprint_subparsers = footprint_parser.add_subparsers(
        dest="footprint_command", help="Footprint commands"
    )

    # footprint generate - dispatched in cli/__init__.py to footprint_generate.main,
    # which owns its own subparser tree (soic/qfp/qfn/chip/sot/dip/pin-header).
    footprint_subparsers.add_parser(
        "generate",
        help="Generate parametric footprints",
        add_help=False,  # Let footprint_generate handle --help itself
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
    mfr_compare.add_argument("-l", "--layers", type=int, default=2, help="Layer count (default: 2)")
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
    zones_add.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input, consistent with 'zones fill')",
    )
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
    zones_batch.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input, consistent with 'zones fill')",
    )
    zones_batch.add_argument(
        "--power-nets",
        required=True,
        help="Power nets spec: 'NET:LAYER,...' (e.g., 'GND:B.Cu,+3.3V:F.Cu')",
    )
    zones_batch.add_argument("--clearance", type=float, default=0.3, help="Clearance in mm")
    zones_batch.add_argument("-v", "--verbose", action="store_true")
    zones_batch.add_argument("--dry-run", action="store_true")

    # zones hv-keepout
    zones_hv = zones_subparsers.add_parser(
        "hv-keepout",
        help="Generate plane pour-keepouts so inner pours clear HV nets",
    )
    zones_hv.add_argument("pcb", help="Path to .kicad_pcb file")
    zones_hv.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input, consistent with 'zones fill')",
    )
    zones_hv.add_argument(
        "--net-class", default="HV", help="Net class naming the HV group (default: HV)"
    )
    zones_hv.add_argument(
        "--net-class-map",
        help="Path to a net-class-map JSON sidecar classifying the HV nets",
    )
    zones_hv.add_argument(
        "--clearance",
        type=float,
        required=True,
        help="Required clearance from HV copper in mm (the void distance)",
    )
    zones_hv.add_argument(
        "--plane-layers",
        help="Comma-separated copper layers whose pours must void "
        "(default: all layers carrying a plane pour)",
    )
    zones_hv.add_argument(
        "--refill",
        action="store_true",
        help="Run 'kicad-cli pcb drc --refill-zones' after writing the keepouts",
    )
    zones_hv.add_argument("-v", "--verbose", action="store_true")
    zones_hv.add_argument("--dry-run", action="store_true")

    # zones fill
    zones_fill = zones_subparsers.add_parser("fill", help="Fill all zones in a PCB")
    zones_fill.add_argument("pcb", help="Path to .kicad_pcb file")
    zones_fill.add_argument("-o", "--output", help="Output file path (default: overwrites input)")
    zones_fill.add_argument("--net", help="Fill only zones for this net (e.g., GND)")
    zones_fill.add_argument("-v", "--verbose", action="store_true")
    zones_fill.add_argument(
        "--dry-run", action="store_true", help="Show what would be done, no output"
    )


def _add_stitch_parser(subparsers) -> None:
    """Add stitch subcommand parser for stitching vias."""
    stitch_parser = subparsers.add_parser(
        "stitch", help="Auto-add stitching vias for plane connections"
    )
    stitch_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    stitch_parser.add_argument(
        "--net",
        "-n",
        action="append",
        dest="stitch_nets",
        help="Net name to add vias for (can be repeated). "
        "If not specified, auto-detects all power plane nets from zones.",
    )
    stitch_parser.add_argument(
        "--via-size",
        type=float,
        default=0.45,
        dest="stitch_via_size",
        help="Via pad diameter in mm (default: 0.45)",
    )
    stitch_parser.add_argument(
        "--drill",
        type=float,
        default=0.2,
        dest="stitch_drill",
        help="Via drill size in mm (default: 0.2)",
    )
    stitch_parser.add_argument(
        "--clearance",
        type=float,
        default=0.2,
        dest="stitch_clearance",
        help="Minimum clearance from existing copper in mm (default: 0.2)",
    )
    stitch_parser.add_argument(
        "--offset",
        type=float,
        default=0.5,
        dest="stitch_offset",
        help="Max distance from pad center for via placement in mm (default: 0.5)",
    )
    stitch_parser.add_argument(
        "--target-layer",
        "-t",
        dest="stitch_target_layer",
        help="Target plane layer (e.g., In1.Cu). Default: auto-detect",
    )
    stitch_parser.add_argument(
        "--trace-width",
        type=float,
        default=0.2,
        dest="stitch_trace_width",
        help="Width of pad-to-via trace segments in mm (default: 0.2)",
    )
    stitch_parser.add_argument(
        "--escape-distance",
        type=float,
        default=3.0,
        dest="stitch_escape_distance",
        help="Maximum escape trace length in mm for dense IC pads (default: 3.0)",
    )
    stitch_parser.add_argument(
        "--blanket",
        "-b",
        action="store_true",
        dest="stitch_blanket",
        help="Place vias on a grid pattern across zone polygons (blanket stitching)",
    )
    stitch_parser.add_argument(
        "--micro-via",
        action="store_true",
        dest="stitch_micro_via",
        help=(
            "Retry stitch-failed pads with smaller micro-vias "
            "(0.3mm pad / 0.15mm drill defaults). Micro-vias span only "
            "adjacent copper layers per KiCad's stack-up rules and help "
            "fine-pitch IC corner pads (e.g. LQFP-48 GND pins boxed by "
            "neighbour signal traces) that the standard 0.6mm via cannot "
            "fit. See issue #3033 for the board-04 U2.23 use case."
        ),
    )
    stitch_parser.add_argument(
        "--micro-via-size",
        type=float,
        default=0.3,
        dest="stitch_micro_via_size",
        help="Micro-via pad diameter in mm (default: 0.3). Only used with --micro-via.",
    )
    stitch_parser.add_argument(
        "--micro-via-drill",
        type=float,
        default=0.15,
        dest="stitch_micro_via_drill",
        help="Micro-via drill diameter in mm (default: 0.15). Only used with --micro-via.",
    )
    stitch_parser.add_argument(
        "--avoid-pad-overlap",
        action="store_true",
        dest="stitch_avoid_pad_overlap",
        help=(
            "Issue #3271: drop via placements whose drill circle would be "
            "fully contained inside an SMD pad on the same net.  Required "
            "on manufacturer profiles that forbid via-in-pad processing "
            "(e.g. JLCPCB standard tier).  Has no effect under --blanket "
            "or --thermal."
        ),
    )
    stitch_parser.add_argument(
        "--spacing",
        type=float,
        default=3.0,
        dest="stitch_spacing",
        help="Grid spacing for blanket stitching in mm (default: 3.0)",
    )
    stitch_parser.add_argument(
        "--thermal",
        action="store_true",
        dest="stitch_thermal",
        help=(
            "Place thermal vias under / around MOSFET heat-sink pads. "
            "Selects pads using footprint-name (TO-220, DPAK, QFN-EP, ...), "
            "reference-prefix (Q*), and pad-size heuristics, AND-ed with "
            "target-net membership."
        ),
    )
    stitch_parser.add_argument(
        "--vias-per-pad",
        type=int,
        default=4,
        dest="stitch_vias_per_pad",
        help="Number of thermal vias to place per qualifying pad (default: 4)",
    )
    stitch_parser.add_argument(
        "--thermal-radius",
        type=float,
        default=2.5,
        dest="stitch_thermal_radius",
        help=(
            "Halo-mode ring radius in mm for thermal vias placed AROUND "
            "(not under) smaller pads (default: 2.5)"
        ),
    )
    stitch_parser.add_argument(
        "--thermal-min-pad-size",
        type=float,
        default=2.0,
        dest="stitch_thermal_min_pad_size",
        help=(
            "Minimum pad width AND height (mm) for the 'large pad' thermal "
            "signal (default: 2.0). Pads bigger than this on both axes are "
            "flagged as heat-sink pads even without a footprint-name match."
        ),
    )
    stitch_parser.add_argument(
        "--thermal-component-prefix",
        action="append",
        dest="stitch_thermal_component_prefixes",
        help=(
            "Reference prefix (case-sensitive) for components that should "
            "be considered thermal-pad candidates. Can be repeated. "
            "Defaults to 'Q' (transistors / MOSFETs)."
        ),
    )
    stitch_parser.add_argument(
        "--dry-run",
        "-d",
        action="store_true",
        dest="stitch_dry_run",
        help="Show changes without applying",
    )
    stitch_parser.add_argument(
        "-o",
        "--output",
        dest="stitch_output",
        help="Output file (default: modify in place)",
    )
    stitch_parser.add_argument(
        "--drc",
        action="store_true",
        dest="stitch_drc",
        help="Run DRC after stitching (fills zones automatically via kicad-cli)",
    )
    stitch_parser.add_argument(
        "--mfr",
        "--manufacturer",
        dest="stitch_mfr",
        default=None,
        help=(
            "Manufacturer profile (e.g., 'jlcpcb', 'jlcpcb-tier1'). When set, "
            "stitch via dimensions are resolved from the manufacturer's YAML "
            "design rules using the board's actual copper layer count, "
            "overriding --via-size and --drill defaults. When omitted, the "
            "existing CLI defaults are used."
        ),
    )
    stitch_parser.add_argument(
        "--copper",
        type=float,
        default=1.0,
        dest="stitch_copper",
        help=(
            "Outer copper weight in oz (default: 1.0). Used together with --mfr "
            "to select the correct design-rules row from the manufacturer YAML."
        ),
    )


def _add_route_parser(subparsers) -> None:
    """Add route subcommand parser."""
    route_parser = subparsers.add_parser(
        "route",
        help="Autoroute a PCB",
        epilog=(
            "PERFORMANCE: The C++ backend provides 10-100x faster routing. "
            "Check status with 'kct build-native --check' and build with "
            "'kct build-native'. The --backend flag defaults to 'auto' which "
            "uses C++ when available and falls back to Python otherwise."
        ),
    )
    route_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    route_parser.add_argument("-o", "--output", help="Output file path")
    route_parser.add_argument(
        "--strategy",
        choices=["basic", "negotiated", "monte-carlo", "evolutionary"],
        default="negotiated",
        help="Routing strategy (default: negotiated)",
    )
    route_parser.add_argument("--skip-nets", help="Comma-separated nets to skip")
    route_parser.add_argument(
        "--nets",
        metavar="NET[,NET...]",
        help=(
            "Comma-separated nets to route EXCLUSIVELY (Issue #4322): route "
            "only the listed nets and treat every OTHER board net as a fixed "
            "obstacle -- the inverse of --skip-nets. Mutually exclusive with "
            "--skip-nets (passing both is an error). Names must match the "
            "board's net names exactly (e.g. 'GND', '/SPI_CLK'); an unknown "
            "name is an error that names the missing net. Whitespace around "
            "each name is trimmed."
        ),
    )
    route_parser.add_argument(
        "--preserve-existing",
        action="store_true",
        default=False,
        help=(
            "Incremental routing: load existing (segment ...)/(via ...) copper "
            "as immovable obstacles and re-emit it unchanged, so only "
            "unconnected nets are routed. Preserves manually-routed nets, "
            "skipped nets' geometry, and standalone stitch vias across a "
            "route pass. Default off (full re-route, existing copper is "
            "replaced by freshly routed nets)."
        ),
    )
    route_parser.add_argument(
        "--region",
        metavar="X1,Y1,X2,Y2",
        help=(
            "SPATIAL routing bound (Issue #4148): confine all new routing to "
            "the axis-aligned box 'x1,y1,x2,y2' (board-relative mm, same "
            "convention as 'pcb strip --region'). Every cell outside the box "
            "is treated as a fixed obstacle, and existing copper outside is "
            "preserved unchanged (implies --preserve-existing semantics). "
            "Composable with --nets (route only the listed nets) or "
            "--skip-nets (route all but the listed nets); --nets and "
            "--skip-nets are themselves mutually exclusive. NOTE: unrelated to "
            "--region-parallel (which partitions the grid for PARALLEL "
            "routing); --region is a spatial bound, not a parallelism knob. "
            "Nets whose pads all lie inside the box route normally; a net "
            "with an endpoint outside fails with a clear per-net message. "
            "Reconnecting to bare mid-trace stubs left by 'pcb strip --region' "
            "is NOT supported here (deferred to a Phase 2b follow-up); stubs "
            "that coincide with a pad/via still work."
        ),
    )
    route_parser.add_argument(
        "--grid",
        type=str,
        default="auto",
        help="Grid resolution in mm or 'auto' for automatic selection (default: auto)",
    )
    route_parser.add_argument(
        "--max-cells",
        type=int,
        default=500_000,
        help=(
            "Maximum grid cells to allow for --grid auto (default: 500,000). "
            "Raise this on large boards when auto-grid selects a coarse, unsafe "
            "grid because of the memory budget cap (the caller-facing override "
            "for the budget named in the 'Increase max_cells' warning/error). "
            "Threaded to both the uniform and adaptive grid-selection paths; no "
            "effect when --grid is an explicit value."
        ),
    )
    route_parser.add_argument("--trace-width", type=float, default=0.2, help="Trace width in mm")
    route_parser.add_argument("--clearance", type=float, default=0.15, help="Clearance in mm")
    route_parser.add_argument(
        "--fine-pitch-clearance",
        type=float,
        default=None,
        help=(
            "Clearance for fine-pitch components (pitch < 0.8mm) in mm. "
            "Allows escape routing between SSOP/QFP/QFN pins. "
            "Example: --fine-pitch-clearance 0.08"
        ),
    )
    route_parser.add_argument("--via-drill", type=float, default=0.3, help="Via drill size in mm")
    route_parser.add_argument("--via-diameter", type=float, default=0.6, help="Via diameter in mm")
    route_parser.add_argument("--mc-trials", type=int, default=10, help="Monte Carlo trials")
    route_parser.add_argument(
        "--pop-size", type=int, default=20, help="Evolutionary optimizer population size"
    )
    route_parser.add_argument(
        "--generations", type=int, default=10, help="Evolutionary optimizer generations"
    )
    route_parser.add_argument("--iterations", type=int, default=15, help="Max iterations")
    # Issue #3101: Best-metric early-stop patience.  Mirror of the inner
    # parser flag at route_cmd.py; both sites must stay in sync per
    # ``tests/test_cli_parser_drift.py``.
    route_parser.add_argument(
        "--early-stop-patience",
        type=int,
        default=2,
        help=(
            "Consecutive non-improving negotiated iterations before the "
            "outer loop breaks early (Issue #3101). Default: 2. Use 0 to "
            "disable. Iteration-0 result is preserved by the existing "
            "best-state restore."
        ),
    )
    # Issue #3438 / #3414: targeted rip-up.  Mirror of the inner parser flag
    # at route_cmd.py; both sites must stay in sync per
    # ``tests/test_cli_parser_drift.py``.
    route_parser.add_argument(
        "--targeted-ripup",
        action="store_true",
        default=False,
        help=(
            "Enable targeted rip-up in the negotiated routing loop "
            "(Issue #3438). Instead of ripping up every net sharing an "
            "overused cell, displace only the specific nets blocking each "
            "failed net. Helps parallel pad-array bundles (DDR byte lanes, "
            "facing QFN pin columns) where the last-routed member finds "
            "its escape corridor consumed by siblings."
        ),
    )
    route_parser.add_argument(
        "--max-ripups-per-net",
        type=int,
        default=None,
        help=(
            "Per-net displacement budget for rip-up recovery (default: 3 "
            "for --targeted-ripup and the two-phase stall path, 2 for the "
            "standard route_all flow).  Issue #3470: previously this flag "
            "only fed --targeted-ripup; it now also governs the "
            "BLOCKED_BY_COMPONENT destructive rip-up budget in route_all "
            "and the two-phase initial-pass stall recovery."
        ),
    )
    route_parser.add_argument(
        "--bundle-river-planner",
        action="store_true",
        default=False,
        help=(
            "Enable the scoped bundle river planner for mirrored byte-lane "
            "bus reversals (Issue #4053, epic #4049).  Board 07's DDR data "
            "byte is a FULL bus reversal between two facing QFN-48 pin "
            "columns (all C(11,2)=55 pairs cross), which planar same-layer "
            "lane ordering cannot solve (every ordering-only approach "
            "capped at <=10/11).  Resolves both facing rows, diffs their "
            "permutation, and reserves one inner-layer via-hop corridor per "
            "inverted (crossing) pair so the losing net can dip under its "
            "partner.  Default OFF (byte-identical when absent); DDR-bundle "
            "scoped in v1."
        ),
    )
    route_parser.add_argument(
        "--monotone-certificate-order",
        action="store_true",
        default=False,
        help=(
            "Enable the monotone-certificate escape order for byte-lane "
            "buses (Issue #4089, epic #4049).  Board 07's DDR byte is proven "
            "feasible and routes 11/11 in isolation (#4089) but has not been "
            "validated end-to-end on the assembled board; when the "
            "certificate finds the bundle infeasible as-pinned, order is left "
            "at IDENTITY (no regression vs. flag-off).  Default OFF "
            "(byte-identical when absent)."
        ),
    )
    route_parser.add_argument(
        "--no-rescue-pass",
        action="store_true",
        default=False,
        help=(
            "Disable the post-negotiation rescue sweep (Issue #4159).  ON by "
            "default: after the negotiated batch loop converges/stalls/times "
            "out, each still-stranded net is re-attempted SOLO on the live "
            "grid, recovering long-haul nets the batch loop starved on per-net "
            "search budget.  Bounded and strictly additive (failed attempts "
            "roll back), so it can only raise the routed count.  Pass this "
            "flag for the raw negotiated result (e.g. A/B comparison)."
        ),
    )
    route_parser.add_argument(
        "--cross-package-pair-corridor",
        action="store_true",
        default=False,
        help=(
            "Enable the cross-package pair corridor for diff/matched pairs "
            "whose members escape from facing packages (Issue #4090, epic "
            "#4049).  Reserves a shared corridor so the pair's two escapes "
            "stay coupled across the package gap.  Default OFF "
            "(byte-identical when absent)."
        ),
    )
    route_parser.add_argument(
        "--slack-corridor-widening",
        action="store_true",
        default=False,
        help=(
            "Enable slack-corridor widening (Issue #4092, epic #4049).  "
            "Prefers slack-reserved corridors and threads the reservation "
            "into the escape router and diff-pair length tuning so tuned "
            "nets can widen into reserved slack.  Default OFF "
            "(byte-identical when absent)."
        ),
    )
    # Issue #3054 (Phase 2 of #3045): wire region-based parallelism through to
    # ``route_all_negotiated``.  Opt-in (default off) so existing scripts and
    # CI runs see byte-identical routes; when set, the negotiated loop
    # partitions the grid into ``--partition-rows`` x ``--partition-cols``
    # regions and routes non-adjacent regions concurrently across
    # ``--max-parallel-workers`` workers per region group.  Expected 2-3x
    # speedup on congested boards (e.g. board 07) per the
    # ``route_all_negotiated`` docstring.
    route_parser.add_argument(
        "--region-parallel",
        action="store_true",
        default=False,
        help=(
            "Enable region-based parallel routing (Issue #965). Partitions "
            "the routing grid into regions and routes non-adjacent regions "
            "in parallel during each negotiated-congestion iteration. "
            "Expected 2-3x speedup on dense multi-net boards. Default off "
            "preserves single-threaded routing (deterministic with --seed). "
            "Auto-disabled on small / dense workloads where nets-per-region "
            "< 16 (Issue #3100); recommended for >= 64-net boards with a "
            "2x2 partition."
        ),
    )
    route_parser.add_argument(
        "--partition-rows",
        type=int,
        default=2,
        metavar="N",
        help=(
            "Number of region rows for --region-parallel partitioning "
            "(default: 2). Only effective when --region-parallel is set."
        ),
    )
    route_parser.add_argument(
        "--partition-cols",
        type=int,
        default=2,
        metavar="N",
        help=(
            "Number of region columns for --region-parallel partitioning "
            "(default: 2). Only effective when --region-parallel is set."
        ),
    )
    route_parser.add_argument(
        "--max-parallel-workers",
        type=int,
        default=4,
        metavar="N",
        help=(
            "Maximum parallel workers per region group for --region-parallel "
            "(default: 4). Only effective when --region-parallel is set."
        ),
    )
    route_parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Timeout in seconds for routing (default: no timeout). Returns best partial result if reached.",
    )
    route_parser.add_argument(
        "--per-net-timeout",
        type=float,
        default=30.0,
        help="Wall-clock timeout in seconds for each per-net A* search (default: 30). "
        "Prevents individual nets from monopolizing the router. Use 0 to disable.",
    )
    # Issue #2817: forward --checkpoint-interval through the outer parser so
    # users can disable (``0``) or tune the best-so-far checkpoint cadence
    # introduced by #2812.  The inner parser at route_cmd.py also declares
    # this flag (default 30.0); both sites must stay in sync, enforced by
    # ``tests/test_cli_parser_drift.py``.
    route_parser.add_argument(
        "--checkpoint-interval",
        type=float,
        default=30.0,
        help=(
            "Interval in seconds between best-so-far checkpoint writes to "
            "--output. Default: 30. Use 0 to disable."
        ),
    )
    # Issue #2610: --max-search-iterations override for the C++ A* memory
    # backstop (default 0 = use the historical ``cols * rows * 4`` heuristic).
    # Documented as an escape hatch for dense boards where the cap fires
    # before the wall-clock deadline; not normally needed.
    route_parser.add_argument(
        "--max-search-iterations",
        type=int,
        default=0,
        help=(
            "Override the C++ A* iteration backstop (default: 0 = use "
            "cols*rows*4, which is ~1M for a 500x500 grid). Positive values "
            "let dense boards trade memory for completeness. Iteration-cap "
            "aborts are logged distinctly from --per-net-timeout (wall-clock) "
            "aborts so you can tell which limit fired."
        ),
    )
    # Issue #3881: tuned per-net iteration cap, distinct from the 12M memory
    # backstop above.  Forwarded to the inner route_cmd parser by
    # ``commands/routing.py``; both sites declare it (enforced by
    # ``tests/test_cli_parser_drift.py``).
    route_parser.add_argument(
        "--per-net-iterations",
        type=int,
        default=0,
        help=(
            "Tuned per-net C++ A* iteration cap (default: 0 = unset). When set, "
            "each net gives up DETERMINISTICALLY after N node expansions so a "
            "hard net cannot monopolise the budget and more nets get a turn. "
            "Distinct from --max-search-iterations (the memory backstop): the "
            "effective cap is min(N, max-search-iterations) and a capped net's "
            "Python fallback is skipped. Load-independent, so reproducible. "
            "--deterministic-budget defaults this to a sensible value."
        ),
    )
    # Issue #3538: bound routing work by an iteration budget instead of
    # wall-clock time so the routed output (and its DRC count) is reproducible
    # across machines.  Forwarded to the inner route_cmd parser by
    # ``commands/routing.py``; both sites declare it (enforced by
    # ``tests/test_cli_parser_drift.py``).
    route_parser.add_argument(
        "--deterministic-budget",
        action="store_true",
        help=(
            "Bound routing work by an ITERATION budget instead of wall-clock "
            "time so the routed output is reproducible across machines "
            "regardless of runner speed/load (Issue #3538). Disables the "
            "per-net wall-clock cutoff (--per-net-timeout 0) and pins the C++ "
            "A* iteration backstop (--max-search-iterations) to a fixed "
            "node-expansion count. --timeout is kept only as a safety "
            "backstop. Combine with --seed for byte-stable re-routes."
        ),
    )
    route_parser.add_argument("-v", "--verbose", action="store_true")
    route_parser.add_argument("--dry-run", action="store_true", help="Don't write output")
    route_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")
    route_parser.add_argument(
        "--power-nets",
        help="Generate zones: 'NET:LAYER,...' (e.g., 'GND:B.Cu,+3.3V:F.Cu')",
    )
    route_parser.add_argument(
        "--layers",
        choices=["auto", "2", "4", "4-sig", "4-all", "6"],
        default="auto",
        help=(
            "Layer stack configuration: "
            "'auto' = auto-detect (default); "
            "'2' = 2-layer; "
            "'4' = 4-layer with GND/PWR planes; "
            "'4-sig' = 4-layer with 2 signal layers; "
            "'4-all' = 4-layer with all 4 signal layers (no planes); "
            "'6' = 6-layer. "
            "Pass '4' when your net-class-map declares is_pour_net / plane "
            "nets so inner layers are reserved for planes: 'auto' infers "
            "planes only from zones already in the input PCB and cannot see "
            "pour nets added post-route."
        ),
    )
    route_parser.add_argument(
        "--force",
        action="store_true",
        help="Force routing even when grid > clearance (may cause DRC violations)",
    )
    route_parser.add_argument(
        "--allow-unsafe-grid",
        action="store_true",
        help=(
            "Allow --grid auto to route on a grid coarser than clearance/2 when "
            "the memory budget cap forces it (issue #3911). Refused by default "
            "because it reliably produces cross-net clearance shorts."
        ),
    )
    route_parser.add_argument(
        "--no-optimize",
        action="store_true",
        help="Skip trace optimization (keep raw grid-step segments)",
    )
    route_parser.add_argument(
        "--raw",
        action="store_true",
        dest="no_optimize",
        help="Alias for --no-optimize (keep raw grid-step segments for debugging)",
    )
    route_parser.add_argument(
        "--auto-layers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Automatically escalate layer count on routing failure "
            "(default: enabled). Tries 2 -> 4 -> 6 layers until routing "
            "succeeds or --max-layers is reached. Use --no-auto-layers "
            "to disable and route at a fixed layer count."
        ),
    )
    route_parser.add_argument(
        "--max-layers",
        type=int,
        default=6,
        choices=[2, 4, 6],
        help="Maximum layer count for auto-escalation (default: 6)",
    )
    # Issue #3400: ``--starting-layers`` lets boards opt out of the 2L tax.
    # CLI flag > project.kct EscalationPolicy.starting_layers > default 2.
    route_parser.add_argument(
        "--starting-layers",
        type=int,
        default=None,
        choices=[2, 4, 6],
        help=(
            "Lower rung of the auto-escalation ladder (default: 2). "
            "Use --starting-layers 4 to skip the 2L probe (Issue #3400). "
            "Must be <= --max-layers."
        ),
    )
    route_parser.add_argument(
        "--min-completion",
        type=float,
        default=0.95,
        help="Minimum routing completion rate for success (default: 0.95 = 95%%)",
    )
    route_parser.add_argument(
        "--adaptive-rules",
        action="store_true",
        help=(
            "Automatically relax design rules on routing failure. "
            "Tries progressively relaxed trace widths and clearances "
            "until routing succeeds or manufacturer limits are reached."
        ),
    )
    # Issue #2881: Manufacturer-tier escalation -- opt-in (default off).
    route_parser.add_argument(
        "--auto-mfr-tier",
        action="store_true",
        default=False,
        help=(
            "Automatically escalate to a tighter manufacturer tier when "
            "geometric infeasibility blocks routing on the current tier "
            "(default: disabled). E.g. jlcpcb -> jlcpcb-tier1 to gain "
            "via-in-pad for fine-pitch QFP escape."
        ),
    )
    # Issue #3352 (P_AS5): Auto-pcb-size escalation -- opt-in (default off).
    # The outer parser registers and forwards the flag; the inner route_cmd
    # parser (route_cmd.py) is the canonical owner of the help text and
    # behaviour.  Per Q5 the flag IMPLIES --auto-layers in the inner parser.
    route_parser.add_argument(
        "--auto-pcb-size",
        action="store_true",
        default=False,
        help=(
            "Automatically escalate PCB envelope to the next manufacturer "
            "size tier when routing reach + DRC density indicate the envelope "
            "is the bottleneck (default: disabled).  Per Issue #3352 Q5, "
            "--auto-pcb-size implies --auto-layers; pass --no-auto-layers to "
            "opt out of the layers axis.  Honours the project.kct "
            "MechanicalRequirements.envelope_hard + "
            "ManufacturingRequirements.escalation policy when present."
        ),
    )
    # Issue #3403: --packing-overhead controls the sum-of-clearances pre-
    # route area estimator used by --auto-pcb-size.  Default None means
    # "use the recipe's EscalationPolicy.packing_overhead (default 2.5)".
    # Setting to 0 disables the pre-route check; the reactive DRC-density
    # backstop still applies.
    route_parser.add_argument(
        "--packing-overhead",
        type=float,
        default=None,
        help=(
            "Packing-density multiplier for the --auto-pcb-size pre-route "
            "area estimator (Issue #3403).  Default: use the recipe's "
            "EscalationPolicy.packing_overhead (default 2.5).  Bump to 3.0+ "
            "for tight layouts, down to 1.8 for loose ones.  Set to 0 to "
            "disable the pre-route check (reactive DRC-density backstop "
            "still applies)."
        ),
    )
    route_parser.add_argument(
        "--mfr-tier-ladder",
        type=str,
        default=None,
        help=(
            "Explicit comma-separated manufacturer tier ladder for "
            "--auto-mfr-tier (e.g. 'jlcpcb,jlcpcb-tier1'). Overrides the "
            "default ladder registered for the current --mfr."
        ),
    )
    route_parser.add_argument(
        "--min-trace",
        type=float,
        help="Minimum trace width floor for adaptive rules (mm)",
    )
    route_parser.add_argument(
        "--min-clearance-floor",
        type=float,
        help="Minimum clearance floor for adaptive rules (mm)",
    )
    route_parser.add_argument(
        "--manufacturer",
        "--mfr",
        default="jlcpcb",
        help="Manufacturer for DRC validation and adaptive rules (default: jlcpcb)",
    )
    route_parser.add_argument(
        "--high-performance",
        action="store_true",
        help=(
            "Use high-performance mode with aggressive parallelization and more trials. "
            "Uses calibrated settings if available (run 'kicad-tools calibrate' first)."
        ),
    )
    route_parser.add_argument(
        "--skip-drc",
        action="store_true",
        help=(
            "Skip post-routing DRC validation. By default, the router runs "
            "a DRC check after routing and warns about violations. Use this "
            "flag for performance-critical use or when running separate validation."
        ),
    )
    # Issue #3154: advisory schematic/PCB drift banner.  Mirror of the inner
    # route_cmd.py flags; both sites must stay in sync per
    # ``tests/test_cli_parser_drift.py``.
    route_parser.add_argument(
        "--sync-check",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Print an advisory banner when the PCB footprint set has drifted "
            "from the schematic netlist (default: enabled, non-blocking). Use "
            "--no-sync-check to suppress. See 'kct check --netlist-sync' for a "
            "blocking gate."
        ),
    )
    route_parser.add_argument(
        "--schematic",
        default=None,
        help=(
            "Explicit .kicad_sch path for the advisory drift banner "
            "(default: auto-discover from project.kct or sibling file)."
        ),
    )
    # Issue #4156: off-board placement preflight escape hatch.  Mirror of the
    # inner route_cmd.py flag; both sites must stay in sync per
    # ``tests/test_cli_parser_drift.py``.
    route_parser.add_argument(
        "--allow-offboard",
        action="store_true",
        default=False,
        help=(
            "Skip the off-board placement preflight. By default kct route "
            "aborts (exit 2) when any footprint's courtyard falls outside the "
            "Edge.Cuts outline, since routing an off-board net always fails. "
            "Use this to proceed anyway (e.g. intentional staging/reference "
            "footprints)."
        ),
    )
    # Issue #4178: hard-gate on native (kicad-cli) DRC actually running.
    # Mirror of the inner route_cmd.py flag; both sites must stay in sync per
    # ``tests/test_cli_parser_drift.py``.
    route_parser.add_argument(
        "--strict-drc",
        action="store_true",
        default=False,
        help=(
            "Treat 'native kicad-cli DRC did not run' (kicad-cli absent, "
            "timed out, crashed, or produced no report) as a HARD FAILURE "
            "(non-zero exit) instead of a soft NOTE. By default the post-route "
            "gate degrades gracefully to an internal-engine-only PASS when "
            "kicad-cli is unavailable, which is not authoritative. Use this in "
            "CI / manufacturing pipelines that require the native DRC to have "
            "actually run and passed."
        ),
    )
    route_parser.add_argument(
        "--auto-fix",
        action="store_true",
        help=(
            "Automatically run 'kct fix-drc' after routing if DRC violations are "
            "detected. Suppressed by --dry-run and --skip-drc. "
            "Issue #3238: when combined with --timeout, reserves "
            "max(60s, 20%% of --timeout) of the total budget so auto-fix "
            "never gets silently skipped when routing exhausts the timeout. "
            "If auto-fix is requested but still skipped due to budget "
            "exhaustion (the routing portion overran its 80%% share), the "
            "route command exits with code 7 and writes "
            "AUTOFIX_SKIPPED_BUDGET_EXHAUSTED to stderr."
        ),
    )
    route_parser.add_argument(
        "--auto-fix-passes",
        type=int,
        default=None,
        metavar="N",
        help=("Number of repair passes for --auto-fix (default: 3). Implies --auto-fix."),
    )
    # Issue #2595: placement-feedback opt-in flags.
    route_parser.add_argument(
        "--placement-feedback",
        action="store_true",
        default=False,
        help=(
            "After the initial routing pass, if any nets failed with "
            "BLOCKED_BY_COMPONENT root cause, invoke the placement-routing "
            "feedback loop to nudge non-anchored components and re-route. "
            "Connectors (J*, P*) and locked footprints are auto-anchored. "
            "Issue #2595."
        ),
    )
    route_parser.add_argument(
        "--no-placement-feedback",
        dest="placement_feedback",
        action="store_false",
        help="Explicitly disable placement-routing feedback (default).",
    )
    route_parser.add_argument(
        "--placement-feedback-budget",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Maximum number of placement adjustments to attempt when "
            "--placement-feedback is set (default: 3)."
        ),
    )
    route_parser.add_argument(
        "--placement-feedback-max-movement",
        type=float,
        default=5.0,
        metavar="MM",
        help=(
            "Hard cap on per-component movement distance for the placement "
            "feedback loop, in mm (default: 5.0)."
        ),
    )
    route_parser.add_argument(
        "--placement-feedback-anchor",
        default=None,
        metavar="REFS",
        help=(
            "Additional component references to anchor (never move) during "
            "placement feedback, comma-separated. Example: --placement-feedback-anchor U5,U7"
        ),
    )
    route_parser.add_argument(
        "--placement-feedback-no-anchor",
        default=None,
        metavar="REFS",
        help=(
            "Component references to remove from the anchor set, comma-separated. "
            "Example: --placement-feedback-no-anchor J3"
        ),
    )
    # Issue #2606: stagnation + outer-timeout guards on the
    # PlacementFeedbackLoop.  Defaults preserve today's behavior (the
    # detector is enabled but only triggers when the routed-net count
    # has plateaued; the outer timeout is off unless explicitly set).
    route_parser.add_argument(
        "--placement-feedback-stagnation-patience",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Number of consecutive outer placement-feedback iterations with "
            "no fully-routed-net-count improvement before the loop exits "
            "early with exit_reason=pf_stagnated. Default 3. Set to 0 to "
            "disable stagnation detection. Issue #2606."
        ),
    )
    route_parser.add_argument(
        "--placement-feedback-outer-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Hard wall-clock budget for the entire outer placement-feedback "
            "loop, in seconds. When exceeded between iterations the loop "
            "exits with exit_reason=pf_timeout. Default: no outer cap "
            "(only the per-iteration --timeout applies). Issue #2606."
        ),
    )
    route_parser.add_argument(
        "--export-failed-nets",
        metavar="PATH",
        help=(
            "Export failed nets (fully-unrouted and partial) to a file for "
            "triage or manual completion in KiCad. A '.json' path emits "
            "structured per-net detail (status, connected/total pads, and the "
            "stranded pad names for partial nets); any other extension emits "
            "one net name per line. The file is always written when routing "
            "is incomplete."
        ),
    )
    route_parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable routing cache (force fresh routing)",
    )
    route_parser.add_argument(
        "--backend",
        choices=["auto", "cpp", "python"],
        default="auto",
        help=(
            "Router backend: 'auto' = C++ if available, Python fallback (default); "
            "'cpp' = require C++ (fails if not built); 'python' = force Python. "
            "The C++ backend is 10-100x faster; build with 'kct build-native'."
        ),
    )
    route_parser.add_argument(
        "--route-engine",
        choices=["grid", "mesh", "lattice"],
        default="grid",
        help=(
            "Routing substrate (issues #4268/#4278), orthogonal to --backend: "
            "'grid' = uniform-grid A* (default, unchanged); "
            "'mesh' = navmesh router (poly2tri CDT + funnel + clearance-aware "
            "45deg fit, multi-net portal negotiation); 'lattice' = adaptive "
            "octilinear lattice (balanced quadtree, paths are 45deg-legal by "
            "construction, negotiated multi-net, through-vias at free-space "
            "lattice nodes). Mesh and lattice are experimental engines that "
            "run their own whole-netset negotiation and REQUIRE --strategy "
            "basic; combining them with the default negotiated strategy, "
            "monte-carlo, evolutionary, --two-phase, --multi-resolution, or "
            "--escape-routing is rejected (issue #4280). Grid remains the "
            "production default and works with every strategy. (The name "
            "'strategy' was already taken by the negotiation-algorithm flag "
            "above, so the substrate selector is --route-engine.)"
        ),
    )
    route_parser.add_argument(
        "--lattice-optimize",
        action="store_true",
        help=(
            "Opt in to the geometric optimize/nudge post-passes on lattice "
            "(and mesh) copper (issue #4318). By default these passes are "
            "SKIPPED for non-grid engines (#4281): lattice copper is committed "
            "as-is, so the byte output of a plain '--route-engine lattice' run "
            "is unchanged. With this flag the optimize/DRC-nudge post-passes "
            "are allowed to run so 'repair-clearance'/'fix-drc'-style cleanup "
            "can operate on lattice output. Off by default; has no effect for "
            "'--route-engine grid' (grid always runs the post-passes)."
        ),
    )
    route_parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Seed the global random module for reproducible routing "
            "(Issue #2589). The python backend's escape strategies "
            "(_escape_shuffle_order, _escape_random_subset, "
            "_escape_full_reorder) and MST fine-grid trial shuffle call "
            "random.shuffle/random.sample without per-instance seeding, "
            "so two runs with identical inputs but no --seed produce "
            "different byte output. When --seed N is set the router is "
            "deterministic for the same input (modulo per-element UUID "
            "generation, which is intentional). Recommended for CI."
        ),
    )
    route_parser.add_argument(
        "--order-method",
        choices=["greedy", "critical_first", "congestion", "hybrid"],
        default=None,
        help=(
            "Compute the net routing order with a named heuristic (Issue #3897) "
            "instead of the default priority-based sort. Overrides the internal "
            "_get_net_priority ordering. Choices: 'greedy' (fewest pads first), "
            "'critical_first' (power/clock nets first), 'congestion' (most "
            "congested nets first), 'hybrid' (critical_first + congestion). "
            "'congestion' and 'hybrid' require a congestion map; if one cannot "
            "be obtained the command warns and falls back to 'greedy'. When "
            "omitted, ordering is byte-identical to the default behaviour."
        ),
    )
    route_parser.add_argument(
        "--no-auto-build-native",
        action="store_true",
        help=(
            "Disable silent auto-build of the C++ router extension on first use "
            "(Issue #2549). When --backend is 'auto' (the default) and the compiled "
            "router_cpp.*.so is missing, kct route normally invokes 'kct build-native' "
            "once and uses C++ for the rest of the session. Pass this flag (or set "
            "KICAD_TOOLS_NO_AUTO_BUILD=1) to skip the build attempt and fall straight "
            "through to pure Python."
        ),
    )
    route_parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Fail with non-zero exit code if output connectivity verification "
            "detects any disconnected net in the written PCB file."
        ),
    )
    route_parser.add_argument(
        "--strict-in-pad-clearance",
        action="store_true",
        dest="route_strict_in_pad_clearance",
        help=(
            "Issue #3033 / #3062: refuse to commit in-pad rescue vias that "
            "would clip a neighbouring foreign-net pad on a fine-pitch "
            "QFP/SSOP.  Forwarded to the inner 'kct route' command, which "
            "stamps KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE=1 so EscapeRouter "
            "inherits the opt-in.  Default off preserves the legacy "
            "'proceed anyway' behaviour bit-for-bit."
        ),
    )
    route_parser.add_argument(
        "--micro-via-in-pad-fallback",
        action="store_true",
        dest="route_micro_via_in_pad_fallback",
        help=(
            "Issue #3118: retry in-pad escape vias with smaller micro-via "
            "dimensions (default 0.3 mm OD / 0.15 mm drill) when the "
            "standard manufacturer-floor via clips a neighbouring "
            "foreign-net pad on a fine-pitch QFP/SSOP.  Resolves the "
            "board-04 OSC_OUT LQFP-48 0.5 mm-pitch cluster where the "
            "0.6 mm minimum jlcpcb-tier1 via cannot fit.  Forwarded to "
            "the inner 'kct route' command, which stamps "
            "KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK=1 so EscapeRouter "
            "inherits the opt-in.  Default off preserves legacy "
            "behaviour bit-for-bit.  Only effective on manufacturers "
            "that support via-in-pad (e.g. jlcpcb-tier1)."
        ),
    )
    route_parser.add_argument(
        "--micro-via-size",
        type=float,
        default=0.3,
        dest="route_micro_via_size",
        help=(
            "Micro-via pad diameter in mm for the in-pad fallback "
            "(default: 0.3).  Only used with "
            "--micro-via-in-pad-fallback."
        ),
    )
    route_parser.add_argument(
        "--micro-via-drill",
        type=float,
        default=0.15,
        dest="route_micro_via_drill",
        help=(
            "Micro-via drill diameter in mm for the in-pad fallback "
            "(default: 0.15).  Only used with "
            "--micro-via-in-pad-fallback."
        ),
    )
    route_parser.add_argument(
        "--differential-pairs",
        action="store_true",
        help=(
            "Enable differential pair routing. Detects diff pair net names "
            "(e.g., USB_D+/USB_D-, ETH_TX_P/ETH_TX_N) and routes them together "
            "with the CoupledPathfinder. Compatible with the 'negotiated' and "
            "'basic' strategies. Issue #2464: makes diff-pair detection a "
            "routing-time consumer."
        ),
    )
    route_parser.add_argument(
        "--diffpair-spacing",
        type=float,
        help=(
            "Override differential pair trace spacing in mm. "
            "Default: auto based on detected pair type "
            "(USB2: 0.20mm, USB3/HDMI/LVDS: 0.15mm, Ethernet: 0.20mm)."
        ),
    )
    route_parser.add_argument(
        "--diffpair-max-delta",
        type=float,
        help=(
            "Override maximum length mismatch for differential pairs in mm. "
            "Default: auto based on detected pair type."
        ),
    )
    route_parser.add_argument(
        "--diffpair-per-pair-timeout",
        type=float,
        default=None,
        help=(
            "Per-pair wall-clock budget for the CoupledPathfinder in "
            "seconds (Issue #3089).  When set, each diff-pair coupled A* "
            "search abandons after this many seconds and the pair is "
            "deferred to the main strategy (single-ended A*).  Required "
            "for boards with dense BGA/QFN escape geometry where the "
            "unbounded coupled search can hang for many minutes per "
            "pair (e.g. board 07's MIPI lanes, Issue #3275).  Default: "
            "no per-pair budget."
        ),
    )
    route_parser.add_argument(
        "--length-match-diffpairs",
        action="store_true",
        help=(
            "Enable per-pair differential length-match (skew) tuning (Epic "
            "#2556 Phase 3I). Inserts serpentines on the shorter half of "
            "each length-critical diff pair until skew is within the "
            "per-class tolerance. Requires --differential-pairs (otherwise "
            "warns and short-circuits)."
        ),
    )
    route_parser.add_argument(
        "--length-match-groups",
        action="store_true",
        help=(
            "Enable N-trace match-group length-match tuning (Epic #2661 "
            "Phase 3H). Detects parallel-bus groups (DDR, MIPI, HDMI TMDS) "
            "declared via NetClassRouting.length_match_group, then inserts "
            "serpentines on shorter group members until the per-group skew "
            "is within tolerance. Compatible with --length-match-diffpairs; "
            "groups whose members are diff pairs (MIPI/HDMI lane groups) "
            "engage the Phase 2F symmetric-serpentine path."
        ),
    )
    route_parser.add_argument(
        "--net-class-map",
        dest="net_class_map",
        default=None,
        help=(
            "Path to a JSON sidecar mapping net names to NetClassRouting "
            "fields (Issue #2996).  Merged into the autorouter's "
            "name-pattern-classified net_class_map so per-pair / per-group "
            "fields (intra_pair_clearance, coupled_routing, "
            "coupled_continuity_threshold, target_diff_impedance, "
            "length_match_group) project through to the routing-time "
            "pathfinder.  Without this flag, --differential-pairs falls "
            "back to NetClassRouting defaults and can produce overlapping "
            "diff-pair sibling traces under tight mfr profiles.  Keys are "
            "matched against the board net's sheet-local suffix (the "
            "segment after the last '/'), so a bare key FUSED_LINE matches "
            "KiCad's '/'-prefixed label net /FUSED_LINE while global power "
            "nets (GND, +3.3V) stay bare (Issue #4149)."
        ),
    )
    route_parser.add_argument(
        "--analog-nets",
        dest="analog_nets",
        default=None,
        help=(
            "Comma-separated list of analog net names (e.g. "
            '"AUDIO_L,AUDIO_R") to route with a boosted analog class '
            "(Issue #3171, Phase 3).  Selected nets get priority=2 (route "
            "before digital signals) and cost_multiplier=0.85 (shorter-path "
            "bias).  Pour/ground nets (e.g. GNDA) are never forced into the "
            "pathfinder.  NOTE: guard-trace / shield-copper generation is NOT "
            "implemented and is deferred to a follow-up (Phase 4)."
        ),
    )
    route_parser.add_argument(
        "--auto-analog",
        dest="auto_analog",
        action="store_true",
        help=(
            "Auto-detect analog nets via the Phase 2 detector "
            "(detect_analog_nets) and route them with the boosted analog "
            "class (Issue #3171).  May be combined with --analog-nets (the "
            "two sets are unioned)."
        ),
    )


def _add_route_auto_parser(subparsers) -> None:
    """Add route-auto subcommand parser."""
    route_auto_parser = subparsers.add_parser(
        "route-auto",
        help="Route a net using RoutingOrchestrator smart strategy selection",
    )
    route_auto_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    # Issue #4322: --net is no longer unconditionally required because --nets
    # (route several nets in sequence) is an alternative.  Exactly one of
    # --net / --nets must be given; the handler enforces this and errors if
    # both (or neither) are supplied.
    route_auto_parser.add_argument(
        "--net",
        required=False,
        metavar="NAME",
        help="Net to route (e.g., 'GND', 'SPI_CLK'). Mutually exclusive with --nets.",
    )
    route_auto_parser.add_argument(
        "--nets",
        metavar="NAME[,NAME...]",
        help=(
            "Comma-separated nets to route in sequence (Issue #4322). Each net "
            "is routed independently via RoutingOrchestrator, accumulating "
            "copper into the output; the exit code is non-zero if ANY net "
            "fails or is left partial. Whitespace around each name is trimmed. "
            "Mutually exclusive with --net."
        ),
    )
    route_auto_parser.add_argument(
        "--region",
        metavar="X1,Y1,X2,Y2",
        help=(
            "SPATIAL routing bound (Issue #4148): confine routing of --net to "
            "the axis-aligned box 'x1,y1,x2,y2' (board-relative mm, same "
            "convention as 'pcb strip --region' and 'route --region'). Every "
            "cell outside the box is a fixed obstacle and existing copper "
            "outside is preserved unchanged. If --net has an endpoint outside "
            "the box the route fails with a clear message. Bare mid-trace "
            "stub reconnection is deferred to a Phase 2b follow-up."
        ),
    )
    route_auto_parser.add_argument(
        "--strategy",
        choices=["auto", "global", "escape", "hierarchical", "subgrid", "via_resolution"],
        default="auto",
        help=(
            "Strategy override (default: auto). "
            "'auto' lets the orchestrator select the best strategy and, on a "
            "partially-connected multi-pad net, automatically ATTEMPTS the "
            "'hierarchical' fallback to complete it (Issue #4165); the fallback "
            "improves completion when it can but does not guarantee it, so a "
            "hard net may stay partial and route-auto still exits non-zero. "
            "'global'/'escape'/'subgrid' route a SINGLE two-terminal corridor "
            "and may leave intermediate pads of a multi-pad net unconnected "
            "even when they report success (route-auto then reports "
            "'partially routed: k/n pads connected' and exits non-zero). "
            "'hierarchical' iterates toward full net completion but may still "
            "leave a congested net partial."
        ),
    )
    route_auto_parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Persist a partially-routed multi-pad net's copper instead of "
            "refusing to write incomplete copper. Exit code stays non-zero and "
            "the k/n partial report is still printed (Issue #4165)."
        ),
    )
    route_auto_parser.add_argument(
        "--no-repair",
        action="store_true",
        help="Disable automatic clearance repair after routing (repair enabled by default)",
    )
    route_auto_parser.add_argument(
        "--no-via-resolution",
        action="store_true",
        help="Disable via conflict resolution (enabled by default)",
    )
    route_auto_parser.add_argument(
        "--via-drill",
        type=float,
        default=None,
        metavar="MM",
        help=(
            "Via drill size in mm. Overrides the board-derived via drill "
            "(Issue #4247). When omitted, route-auto derives the drill from "
            "the board's net-class via constraints."
        ),
    )
    route_auto_parser.add_argument(
        "--via-diameter",
        type=float,
        default=None,
        metavar="MM",
        help=(
            "Via pad diameter in mm. Overrides the board-derived via diameter "
            "(Issue #4247). When omitted, route-auto derives the diameter from "
            "the board's net-class via constraints."
        ),
    )
    route_auto_parser.add_argument("-o", "--output", help="Output PCB file path")
    route_auto_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview strategy selection without routing",
    )
    route_auto_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show full traceback on error",
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
    # DRC-aware mode arguments
    optimize_parser.add_argument(
        "--drc-aware",
        action="store_true",
        help="Enable DRC-aware mode: roll back per-net optimizations that increase violations",
    )
    optimize_parser.add_argument(
        "--mfr",
        help="Target manufacturer for DRC rules (e.g., jlcpcb, oshpark). Required with --drc-aware",
    )
    optimize_parser.add_argument(
        "--layers",
        type=int,
        default=2,
        help="Number of copper layers for DRC checks (default: 2)",
    )
    optimize_parser.add_argument(
        "--copper",
        type=float,
        default=1.0,
        help="Copper weight in oz for DRC checks (default: 1.0)",
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


def _add_fix_vias_parser(subparsers) -> None:
    """Add fix-vias subcommand parser."""
    fix_vias_parser = subparsers.add_parser(
        "fix-vias", help="Fix vias to meet manufacturer specifications"
    )
    fix_vias_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    fix_vias_parser.add_argument(
        "--mfr",
        choices=get_all_manufacturer_names(),
        default="jlcpcb",
        help="Manufacturer to use for design rules (default: jlcpcb)",
    )
    fix_vias_parser.add_argument(
        "--layers",
        type=int,
        default=None,
        help="Number of PCB layers (auto-detected from board if not specified)",
    )
    fix_vias_parser.add_argument(
        "--copper",
        type=float,
        default=1.0,
        help="Outer copper weight in oz (default: 1.0)",
    )
    fix_vias_parser.add_argument(
        "--drill",
        type=float,
        help="Target drill diameter in mm (overrides manufacturer rules)",
    )
    fix_vias_parser.add_argument(
        "--diameter",
        type=float,
        help="Target via diameter in mm (overrides manufacturer rules)",
    )
    fix_vias_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input)",
    )
    fix_vias_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files",
    )
    fix_vias_parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    fix_vias_parser.add_argument(
        "--skip-if-clearance-violation",
        action="store_true",
        help=(
            "Skip resizing vias that would cause clearance violations. "
            "Keeps the original via size when enlargement would violate "
            "minimum clearance to nearby tracks, pads, or other vias."
        ),
    )
    fix_vias_parser.add_argument(
        "--relocate-in-pad",
        action="store_true",
        help=(
            "Relocate via-in-pad vias off-pad (connectivity-preserving) so the "
            "board can move to a manufacturer profile that disallows via-in-pad "
            "(e.g. standard jlcpcb). Distinct pass from via resizing; slides "
            "each in-pad signal via just outside the pad along its escape track "
            "and adds stub segments to preserve the net."
        ),
    )
    fix_vias_parser.add_argument(
        "--net",
        action="append",
        dest="nets",
        metavar="NET",
        help=("Restrict --relocate-in-pad to the given net name (repeatable). Default: all nets."),
    )


def _add_fix_silkscreen_parser(subparsers) -> None:
    """Add fix-silkscreen subcommand parser."""
    fix_silk_parser = subparsers.add_parser(
        "fix-silkscreen", help="Fix silkscreen line widths to meet manufacturer specifications"
    )
    fix_silk_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    fix_silk_parser.add_argument(
        "--mfr",
        choices=get_all_manufacturer_names(),
        default="jlcpcb",
        help="Manufacturer to use for design rules (default: jlcpcb)",
    )
    fix_silk_parser.add_argument(
        "--layers",
        type=int,
        default=2,
        help="Number of PCB layers (default: 2)",
    )
    fix_silk_parser.add_argument(
        "--copper",
        type=float,
        default=1.0,
        help="Outer copper weight in oz (default: 1.0)",
    )
    fix_silk_parser.add_argument(
        "--min-width",
        type=float,
        help="Minimum silkscreen line width in mm (overrides manufacturer rules)",
    )
    fix_silk_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input)",
    )
    fix_silk_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files",
    )
    fix_silk_parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )


def _add_repair_clearance_parser(subparsers) -> None:
    """Add repair-clearance subcommand parser."""
    repair_parser = subparsers.add_parser(
        "repair-clearance", help="Repair clearance violations by nudging traces"
    )
    repair_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    repair_parser.add_argument(
        "--drc-report",
        help="Path to existing DRC report (.rpt or .json)",
    )
    repair_parser.add_argument(
        "--mfr",
        choices=get_all_manufacturer_names(),
        help="Target manufacturer (for context)",
    )
    repair_parser.add_argument(
        "--max-displacement",
        type=float,
        default=0.1,
        help="Maximum nudge distance in mm (default: 0.1)",
    )
    repair_parser.add_argument(
        "--margin",
        type=float,
        default=0.01,
        help="Extra clearance margin beyond minimum in mm (default: 0.01)",
    )
    repair_parser.add_argument(
        "--prefer",
        choices=["move-trace", "move-via"],
        default="move-trace",
        help="Which object to move (default: move-trace)",
    )
    repair_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input)",
    )
    repair_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files",
    )
    repair_parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )


def _add_fix_drc_parser(subparsers) -> None:
    """Add fix-drc subcommand parser."""
    fix_drc_parser = subparsers.add_parser(
        "fix-drc", help="Automated DRC violation repair (clearance + drill)"
    )
    fix_drc_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    fix_drc_parser.add_argument(
        "--drc-report",
        help="Path to existing DRC report (.rpt or .json)",
    )
    fix_drc_parser.add_argument(
        "--mfr",
        "-m",
        choices=get_all_manufacturer_names(),
        default="jlcpcb",
        help=(
            "Target manufacturer for design rules (default: jlcpcb). "
            "Only used when fix-drc generates a DRC report internally "
            "(no --drc-report given, or with --verify); ignored when "
            "--drc-report is supplied since clearances come from the report."
        ),
    )
    fix_drc_parser.add_argument(
        "--layers",
        "-l",
        type=int,
        default=2,
        help=(
            "Number of PCB layers used to derive clearance rules "
            "(default: 2). Only used when generating a DRC report "
            "internally; ignored when --drc-report is supplied."
        ),
    )
    fix_drc_parser.add_argument(
        "--max-displacement",
        type=float,
        default=0.5,
        help="Maximum nudge/slide distance in mm (default: 0.5)",
    )
    fix_drc_parser.add_argument(
        "--margin",
        type=float,
        default=0.01,
        help="Extra clearance margin beyond minimum in mm (default: 0.01)",
    )
    fix_drc_parser.add_argument(
        "--only",
        choices=["clearance", "drill-clearance"],
        help="Only fix a specific violation type",
    )
    fix_drc_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input)",
    )
    fix_drc_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files",
    )
    fix_drc_parser.add_argument(
        "--max-passes",
        type=int,
        default=1,
        help=(
            "Maximum number of detect-repair cycles (default: 1). "
            "Each pass re-runs DRC detection on the modified PCB. "
            "Iteration stops early when no violations are repaired in a pass."
        ),
    )
    fix_drc_parser.add_argument(
        "--local-reroute",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Attempt local A* rerouting for infeasible violations "
            "(segments with both endpoints at vias). On by default; "
            "use --no-local-reroute to disable."
        ),
    )
    fix_drc_parser.add_argument(
        "--no-connectivity-check",
        action="store_true",
        help=(
            "Skip post-pass connectivity check and rollback. "
            "Use for boards with no footprints where connectivity is meaningless."
        ),
    )
    fix_drc_parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Run pure-Python DRC before and after repair and report a before/after violation delta."
        ),
    )
    fix_drc_parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
    )
    fix_drc_parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )


def _add_fix_erc_parser(subparsers) -> None:
    """Add fix-erc subcommand parser."""
    fix_erc_parser = subparsers.add_parser(
        "fix-erc", help="Automated ERC violation repair (PWR_FLAG + no-connect)"
    )
    fix_erc_parser.add_argument("schematic", help="Path to .kicad_sch file")
    fix_erc_parser.add_argument(
        "--erc-report",
        help="Path to existing ERC report (.rpt or .json)",
    )
    fix_erc_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files",
    )
    fix_erc_parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
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

    # parts sync-catalog
    parts_sync = parts_subparsers.add_parser(
        "sync-catalog",
        help="Download the offline jlcparts catalog for offline/rate-limited lookups",
    )
    parts_sync.add_argument(
        "--force", action="store_true", help="Re-download even if a catalog already exists"
    )
    parts_sync.add_argument(
        "--base-url", default=None, help="Override dataset base URL (advanced/testing)"
    )

    # parts suggest
    parts_suggest = parts_subparsers.add_parser(
        "suggest", help="Suggest LCSC part numbers for components without them"
    )
    parts_suggest.add_argument("schematic", help="Path to .kicad_sch file")
    parts_suggest.add_argument(
        "--format", choices=["table", "json"], default="table", help="Output format"
    )
    parts_suggest.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Show suggestions for all components (including those with LCSC numbers)",
    )
    parts_suggest.add_argument(
        "--no-basic-preference",
        action="store_true",
        help="Don't prefer JLCPCB basic parts",
    )
    parts_suggest.add_argument(
        "--min-stock",
        type=int,
        default=100,
        help="Minimum stock level to consider (default: 100)",
    )
    parts_suggest.add_argument(
        "--max-suggestions",
        type=int,
        default=3,
        help="Maximum suggestions per component (default: 3)",
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


def _add_decisions_parser(subparsers) -> None:
    """Add decisions subcommand parser with its subcommands."""
    decisions_parser = subparsers.add_parser(
        "decisions", help="Query design decisions (placement and routing rationale)"
    )
    decisions_subparsers = decisions_parser.add_subparsers(
        dest="decisions_command", help="Decision commands"
    )

    # decisions show
    decisions_show = decisions_subparsers.add_parser("show", help="Show decisions matching filters")
    decisions_show.add_argument("pcb", help="Path to .kicad_pcb file")
    decisions_show.add_argument(
        "-c", "--component", help="Filter by component reference (e.g., U1)"
    )
    decisions_show.add_argument("-n", "--net", help="Filter by net name (e.g., USB_D+)")
    decisions_show.add_argument(
        "-a",
        "--action",
        choices=["place", "route", "move", "reroute", "delete"],
        help="Filter by action type",
    )
    decisions_show.add_argument("-f", "--format", choices=["text", "json", "tree"], default="text")
    decisions_show.add_argument(
        "-l", "--limit", type=int, default=20, help="Max decisions to show (default: 20)"
    )

    # decisions list
    decisions_list = decisions_subparsers.add_parser("list", help="List summary of all decisions")
    decisions_list.add_argument("pcb", help="Path to .kicad_pcb file")
    decisions_list.add_argument("-f", "--format", choices=["text", "json"], default="text")

    # decisions explain-placement
    decisions_explain_place = decisions_subparsers.add_parser(
        "explain-placement", help="Explain why a component is placed where it is"
    )
    decisions_explain_place.add_argument("pcb", help="Path to .kicad_pcb file")
    decisions_explain_place.add_argument("component", help="Component reference (e.g., U1)")
    decisions_explain_place.add_argument("-f", "--format", choices=["text", "json"], default="text")

    # decisions explain-route
    decisions_explain_route = decisions_subparsers.add_parser(
        "explain-route", help="Explain why a net was routed the way it was"
    )
    decisions_explain_route.add_argument("pcb", help="Path to .kicad_pcb file")
    decisions_explain_route.add_argument("net", help="Net name (e.g., USB_D+)")
    decisions_explain_route.add_argument("-f", "--format", choices=["text", "json"], default="text")


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
    placement_fix.add_argument(
        "--only",
        choices=["pad_clearance", "courtyard_overlap", "hole_to_hole", "edge_clearance"],
        default=None,
        help="Fix only a specific conflict type (e.g., pad_clearance for fast targeted repair)",
    )
    placement_fix.add_argument("--dry-run", action="store_true")
    placement_fix.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Maximum wall-clock seconds; returns best result so far on expiry",
    )
    placement_fix.add_argument("-v", "--verbose", action="store_true")
    placement_fix.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )

    # placement nudge (fast pad clearance repair)
    placement_nudge = placement_subparsers.add_parser(
        "nudge", help="Fast targeted pad clearance violation repair"
    )
    placement_nudge.add_argument("pcb", help="Path to .kicad_pcb file")
    placement_nudge.add_argument("-o", "--output", help="Output file path")
    placement_nudge.add_argument("--anchor", help="Comma-separated components to keep fixed")
    placement_nudge.add_argument(
        "--pad-clearance", type=float, default=0.1, help="Min pad clearance (mm)"
    )
    placement_nudge.add_argument("--dry-run", action="store_true", help="Show proposed moves only")
    placement_nudge.add_argument("-v", "--verbose", action="store_true")
    placement_nudge.add_argument(
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
    placement_optimize.add_argument(
        "--routing-aware",
        action="store_true",
        dest="routing_aware",
        help="Use integrated place-route optimization (iterates between placement and routing)",
    )
    placement_optimize.add_argument(
        "--use-routing-fitness",
        action="store_true",
        dest="use_routing_fitness",
        help=(
            "(Issue #2720) Replace the evolutionary GA's average-pairwise-spacing "
            "routability proxy with actual routing completion rate from the C++ A* "
            "router (CppAstarRoutingEvaluator). Only effective with --strategy "
            "evolutionary or hybrid. Default off."
        ),
    )
    placement_optimize.add_argument(
        "--check-routability",
        action="store_true",
        dest="check_routability",
        help="Check routability before and after optimization to show impact",
    )
    placement_optimize.add_argument(
        "--boundary-margin",
        "--board-margin",
        type=float,
        default=None,
        dest="boundary_margin",
        help="Extra margin in mm between component courtyards and board edge (default: 1.0)",
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


def _add_optimize_placement_parser(subparsers) -> None:
    """Add optimize-placement subcommand parser."""
    op_parser = subparsers.add_parser(
        "optimize-placement",
        help="Run CMA-ES placement optimization on a KiCad PCB",
        epilog=(
            "PERFORMANCE: A C++ backend is available for faster placement "
            "evaluation. Check status with 'kct build-native --check' and "
            "build with 'kct build-native'."
        ),
    )
    op_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    op_parser.add_argument(
        "--strategy",
        choices=["cmaes"],
        default="cmaes",
        help="Optimization strategy (default: cmaes)",
    )
    op_parser.add_argument(
        "--max-iterations",
        type=int,
        default=1000,
        help="Maximum number of optimizer iterations (default: 1000)",
    )
    op_parser.add_argument(
        "-o",
        "--output",
        help="Output PCB file path (default: overwrite input)",
    )
    op_parser.add_argument(
        "--seed",
        choices=["force-directed", "random"],
        default="force-directed",
        dest="seed_method",
        help="Seed placement method (default: force-directed)",
    )
    op_parser.add_argument(
        "--weights",
        metavar="JSON",
        help=(
            'Custom cost weights as JSON, e.g. \'{"wirelength": 2.0, "overlap": 1e6}\'. '
            "Keys: overlap, drc, boundary, wirelength, area, creepage"
        ),
    )
    op_parser.add_argument(
        "--voltage-map",
        metavar="FILE",
        dest="voltage_map",
        help=(
            "Path to a JSON voltage map {net_name: volts} (reuses the #4371 "
            "format). Enables HV-aware placement (issue #4373): footprints are "
            "grouped into voltage domains and cross-domain footprints are pushed "
            "apart to their required creepage. Absent, the objective is "
            "byte-identical to the voltage-blind default. Mutually exclusive "
            "with --hv-domains."
        ),
    )
    op_parser.add_argument(
        "--hv-domains",
        metavar="FILE",
        dest="hv_domains",
        help=(
            "Path to a JSON HV-domains declaration "
            '{domain_id: {"refs": [globs], "voltage": v}} -- the manual fallback '
            "for HV-aware placement when no voltage map is available. Mutually "
            "exclusive with --voltage-map."
        ),
    )
    op_parser.add_argument(
        "--creepage-standard",
        dest="creepage_standard",
        choices=["iec60664", "iec62368"],
        default="iec60664",
        help="Creepage standard for the required-distance lookup (default: iec60664)",
    )
    op_parser.add_argument(
        "--pollution-degree",
        dest="pollution_degree",
        type=int,
        choices=[1, 2, 3],
        default=2,
        help="IEC pollution degree for the creepage lookup (default: 2)",
    )
    op_parser.add_argument(
        "--material-group",
        dest="material_group",
        default="IIIa",
        help="Insulation material group I/II/IIIa/IIIb for the creepage lookup (default: IIIa)",
    )
    op_parser.add_argument(
        "--hv-threshold",
        dest="hv_threshold",
        type=float,
        default=30.0,
        metavar="VOLTS",
        help=(
            "Minimum cross-domain |ΔV| (volts) that triggers a creepage keepout; "
            "lower-difference domain pairs rely on normal DRC clearance to avoid "
            "over-segregating low-voltage nets (default: 30.0)"
        ),
    )
    op_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate current placement without optimizing",
    )
    op_parser.add_argument(
        "--progress",
        type=int,
        default=0,
        metavar="N",
        help="Print score every N iterations (0 = no progress output)",
    )
    op_parser.add_argument(
        "--checkpoint",
        metavar="DIR",
        help="Directory for checkpoint save/resume of optimizer state",
    )
    op_parser.add_argument(
        "--no-slide-off",
        action="store_true",
        default=False,
        help="Disable slide-off overlap pre-processing on the seed placement",
    )
    op_parser.add_argument(
        "--anchor-weight",
        type=float,
        default=0.0,
        metavar="FLOAT",
        help=(
            "Per-net wirelength multiplier boost for nets that touch "
            "footprints carrying the KiCad (locked) attribute. Each "
            "qualifying net's HPWL is scaled by "
            "1 + anchor_weight * (anchored_pins / total_pins). "
            "Default 0.0 preserves uniform weighting; recommended "
            "starting range is 2.0..5.0 to keep perimeter-anchored "
            "signals (connectors, edge sense FETs) from being starved."
        ),
    )
    op_parser.add_argument(
        "--time-budget",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Wall-clock budget in seconds. The optimization loop exits "
            "once this many seconds have elapsed (after the current "
            "generation finishes). Use this to bound the new "
            "feasibility-gated convergence behaviour (issue #2821), "
            "where the loop refuses to declare convergence while the "
            "best-known placement is still infeasible. Default: no cap."
        ),
    )
    op_parser.add_argument(
        "--allow-infeasible",
        action="store_true",
        default=False,
        help=(
            "Return exit code 0 even when the final placement is "
            "infeasible (overlap/DRC/boundary violations remain). "
            "Default behaviour (issue #2821) is to exit 1 with a "
            "FATAL: message on stderr so pipelines do not silently "
            "hand illegal placements to the router."
        ),
    )
    op_parser.add_argument("-v", "--verbose", action="store_true")
    op_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")


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
        "--lvs",
        action="store_true",
        help="Layout-vs-schematic check with hierarchical support and fuzzy matching",
    )
    validate_parser.add_argument(
        "--min-confidence",
        dest="validate_min_confidence",
        type=float,
        default=0.0,
        help="Minimum match confidence to include in LVS results (0.0-1.0, default: 0.0)",
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

    # analyze complexity
    complexity_parser = analyze_subparsers.add_parser(
        "complexity",
        help="Analyze routing complexity and predict layer requirements",
        description="Estimate routing complexity, predict layer count, and identify bottlenecks",
    )
    complexity_parser.add_argument("pcb", help="PCB file to analyze (.kicad_pcb)")
    complexity_parser.add_argument(
        "--format",
        "-f",
        dest="analyze_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    complexity_parser.add_argument(
        "--grid-size",
        dest="analyze_grid_size",
        type=float,
        default=5.0,
        help="Grid cell size for density analysis in mm (default: 5.0)",
    )

    # analyze current-sense
    current_sense_parser = analyze_subparsers.add_parser(
        "current-sense",
        help="Analyze sense-net vs. high-current-net parallel coupling",
        description=(
            "Flag sense/analog nets that run parallel and close to "
            "high-current/switching nets on the same layer (advisory)"
        ),
    )
    current_sense_parser.add_argument("pcb", help="PCB file to analyze (.kicad_pcb)")
    current_sense_parser.add_argument(
        "--format",
        "-f",
        dest="analyze_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    current_sense_parser.add_argument(
        "--sense-net",
        action="append",
        dest="analyze_sense_nets",
        default=[],
        metavar="NAME",
        help="Explicitly tag NAME as a sense net (repeatable)",
    )
    current_sense_parser.add_argument(
        "--hicur-net",
        action="append",
        dest="analyze_hicur_nets",
        default=[],
        metavar="NAME",
        help="Explicitly tag NAME as a high-current net (repeatable)",
    )
    current_sense_parser.add_argument(
        "--max-parallel",
        dest="analyze_max_parallel",
        type=float,
        default=10.0,
        help="Parallel-run FAIL threshold in mm (default: 10.0)",
    )
    current_sense_parser.add_argument(
        "--min-gap",
        dest="analyze_min_gap",
        type=float,
        default=0.5,
        help="Edge-to-edge gap FAIL threshold in mm (default: 0.5)",
    )
    current_sense_parser.add_argument(
        "--max-loop-area",
        dest="analyze_max_loop_area",
        type=float,
        default=10.0,
        help=(
            "Enclosed copper sense-loop area FAIL threshold in mm^2 (default: 10.0; EE-confirmable)"
        ),
    )
    current_sense_parser.add_argument(
        "--sense-pair",
        action="append",
        nargs=2,
        dest="analyze_sense_pairs",
        default=[],
        metavar=("SENSE", "RETURN"),
        help=(
            "Kelvin loop pairing: close SENSE's loop with RETURN conductor "
            "(repeatable). Nets ending _P/_N, +/-, _H/_L auto-pair."
        ),
    )
    current_sense_parser.add_argument(
        "--sense-return",
        dest="analyze_sense_return",
        default=None,
        metavar="NAME",
        help="Shared return conductor (e.g. Kelvin ground) to close sense loops",
    )
    current_sense_parser.add_argument(
        "--kelvin-tol",
        dest="analyze_kelvin_tol",
        type=float,
        default=0.05,
        help="Kelvin-tap coincidence tolerance in mm (default: 0.05)",
    )
    current_sense_parser.add_argument(
        "--kelvin-pair",
        action="append",
        dest="analyze_kelvin_pairs",
        default=[],
        metavar="SENSE:FORCE",
        help=(
            "Kelvin force pairing: check SENSE taps FORCE at a pad "
            "(repeatable). Nets ending _SENSE/_FORCE, SNS/FRC auto-pair."
        ),
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
        choices=get_all_manufacturer_names(),
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
    estimate_cost.add_argument(
        "--no-lcsc",
        action="store_true",
        default=False,
        help="Disable LCSC pricing lookup (use category-based estimates only)",
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
        choices=get_all_manufacturer_names(),
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
        "--pcb",
        dest="audit_pcb",
        type=str,
        default=None,
        help="Override auto-detected PCB path (takes precedence over project.kct)",
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
    audit_parser.add_argument(
        "--net-class-map",
        dest="audit_net_class_map",
        default=None,
        help=(
            "Path to a JSON sidecar mapping net names to NetClassRouting "
            "fields.  When supplied, enables the diff-pair DRC rules to "
            "fire on routed boards (Issue #2684)."
        ),
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
    net_status_parser.add_argument(
        "--why",
        dest="net_status_why",
        action="store_true",
        help=(
            "Classify each incomplete signal net by WHY it is stuck "
            "(ESCAPE_BLOCKED / CONGESTION_SATURATED / BUDGET_STARVED / "
            "PLACEMENT_BOUND) with supporting evidence. Read-only diagnostic "
            "(issue #3863)."
        ),
    )
    net_status_parser.add_argument(
        "--strict",
        dest="net_status_strict",
        action="store_true",
        help=(
            "Decide connectivity by REAL geometric copper contact (shapely "
            "polygon intersection) instead of the default 0.01mm endpoint "
            "proximity tolerance. The default model unions a segment endpoint "
            "with a pad/via/segment whenever their reference points land "
            "within 0.01mm even if the actual copper (segment width, pad "
            "shape) does not touch -- so it can report a net 'complete' that "
            "'kicad-cli pcb drc' reports as unconnected. --strict matches "
            "KiCad's connectivity semantics (issue #4176). Requires shapely."
        ),
    )


def _add_board_metrics_parser(subparsers) -> None:
    """Add the ``board-metrics`` parser (normalized board.json per board, #3676).

    Emits a stable ``board.json`` data contract per demo board by aggregating
    artifacts that already exist under ``output/manufacturing/`` (report.md,
    manifest.json, BOM, kicad_project.zip) plus render images from ``kct render``.
    """
    bm_parser = subparsers.add_parser(
        "board-metrics",
        help="Emit a normalized board.json per board from existing artifacts",
        description=(
            "Aggregate already-computed manufacturing artifacts (report.md, "
            "manifest.json, BOM, kicad_project.zip) and render images into a "
            "normalized board.json per board. Sources existing output; never "
            "recomputes from KiCad. Default output: boards/<id>/output/board.json."
        ),
    )
    bm_parser.add_argument(
        "board_metrics_board",
        nargs="?",
        metavar="board",
        help="Path to a board directory (e.g. boards/05-bldc-motor-controller)",
    )
    bm_parser.add_argument(
        "--all",
        dest="board_metrics_all",
        action="store_true",
        help="Process every board under --boards-dir instead of a single board",
    )
    bm_parser.add_argument(
        "--boards-dir",
        dest="board_metrics_boards_dir",
        default="boards",
        help="Root directory containing per-board subdirs (default: boards)",
    )
    bm_parser.add_argument(
        "--output",
        "-o",
        dest="board_metrics_output",
        default=None,
        help="Override output path (single-board mode only)",
    )
    bm_parser.add_argument(
        "--dry-run",
        dest="board_metrics_dry_run",
        action="store_true",
        help="Print board.json to stdout without writing any file",
    )


def _add_fleet_parser(subparsers) -> None:
    """Add fleet parent-subaction parser (fleet status; future fleet route-all)."""
    fleet_parser = subparsers.add_parser(
        "fleet",
        help="Fleet-wide PCB status and operations",
        description=(
            "Survey every board under a fleet root in one shot. Reports routing "
            "completion (via NetStatusAnalyzer) and manufacturing-artifact "
            "readiness (gerbers, BOM, CPL, manifest) with staleness detection."
        ),
    )
    fleet_subparsers = fleet_parser.add_subparsers(dest="fleet_command", help="Fleet commands")

    # fleet status
    fleet_status = fleet_subparsers.add_parser(
        "status",
        help="Survey routing + manufacturing status for all boards",
    )
    fleet_status.add_argument(
        "--boards-dir",
        dest="fleet_boards_dir",
        default="boards",
        help="Root directory containing per-board subdirs (default: boards)",
    )
    fleet_status.add_argument(
        "--format",
        dest="fleet_format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    fleet_status.add_argument(
        "--ship-only",
        dest="fleet_ship_only",
        action="store_true",
        help="Show only ship-ready boards in table output",
    )
    fleet_status.add_argument(
        "--include-stale",
        dest="fleet_include_stale",
        action="store_true",
        help="(Reserved) treat stale artifacts as not-shippable (already default)",
    )
    fleet_status.add_argument(
        "--pattern",
        dest="fleet_pattern",
        default="*_routed.kicad_pcb",
        help="Glob to identify routed PCB inside output/ (default: *_routed.kicad_pcb)",
    )
    fleet_status.add_argument(
        "--drc-tolerance-file",
        dest="fleet_drc_tolerance_file",
        default=".github/routed-drc-tolerance.yml",
        help=(
            "Path to the per-board DRC tolerance allowlist (default: "
            ".github/routed-drc-tolerance.yml). Boards exceeding the listed "
            "tolerance -- or any board not listed with errors > 0 -- block "
            "ship-ready. Missing file is treated as a strict 0-error gate."
        ),
    )

    # ------------------------------------------------------------------
    # fleet ship-ready (issue #3099)
    # ------------------------------------------------------------------
    fleet_ship_ready = fleet_subparsers.add_parser(
        "ship-ready",
        help=(
            "Per-board PASS/FAIL gate across routing + DRC + ERC + manufacturing. "
            "Warn-only by default; pass --strict for non-zero exit on failure."
        ),
        description=(
            "Aggregate ship-readiness gate. Reads the routed PCB, "
            "drc_report.json, manifest.json, and any erc_report.json under "
            "each board's output/ directory and emits a PASS/FAIL row per "
            "board. Designed for nightly CI use in warn-only mode: --strict "
            "opts into non-zero exits for humans."
        ),
    )
    fleet_ship_ready.add_argument(
        "--boards-dir",
        dest="fleet_ship_boards_dir",
        default="boards",
        help="Root directory containing per-board subdirs (default: boards)",
    )
    fleet_ship_ready.add_argument(
        "--format",
        dest="fleet_ship_format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    fleet_ship_ready.add_argument(
        "--pattern",
        dest="fleet_ship_pattern",
        default="*_routed.kicad_pcb",
        help="Glob to identify routed PCB inside output/ (default: *_routed.kicad_pcb)",
    )
    fleet_ship_ready.add_argument(
        "--drc-tolerance-file",
        dest="fleet_ship_drc_tolerance_file",
        default=".github/routed-drc-tolerance.yml",
        help=(
            "Path to the per-board DRC tolerance allowlist (default: "
            ".github/routed-drc-tolerance.yml)."
        ),
    )
    fleet_ship_ready.add_argument(
        "--strict",
        dest="fleet_ship_strict",
        action="store_true",
        help=("Exit non-zero (2) if any board fails. Default is warn-only (always exit 0)."),
    )


def _add_render_parser(subparsers) -> None:
    """Add the ``render`` subcommand parser (Epic #3674, Phase 1).

    Generates per-board 2D layer plots (SVGs) and 3D ray-traced PNGs into
    ``boards/<id>/output/renders/`` for downstream gallery consumption.
    """
    render_parser = subparsers.add_parser(
        "render",
        help="Render per-board 2D SVGs + 3D PNGs into output/renders/",
        description=(
            "Generate visual artifacts for one board or every board under a "
            "root: 2D front/back layer plots (copper + silkscreen + edge cuts) "
            "via 'kicad-cli pcb export svg', and 3D front/back ray-traced PNGs "
            "via 'kicad-cli pcb render'. Outputs go to a fixed, documented path "
            "(boards/<id>/output/renders/{pcb-front,pcb-back}.svg and "
            "{3d-front,3d-back}.png). "
            "A bare .kicad_pcb file may also be given: it is rendered directly "
            "to <pcb-dir>/renders/ (or -o/--output), skipping directory scanning. "
            "The routed PCB is preferred with graceful fallback to the unrouted "
            "PCB. 3D render requires KiCad 8.0.4+ and a display (xvfb on CI)."
        ),
    )
    render_parser.add_argument(
        "render_path",
        metavar="path",
        help="Board directory, a root containing board directories, or a .kicad_pcb file",
    )
    render_parser.add_argument(
        "-o",
        "--output",
        dest="render_output",
        default=None,
        help=(
            "Output directory for a single .kicad_pcb file "
            "(default: <pcb-dir>/renders/). Ignored in directory/root scanning mode."
        ),
    )
    render_parser.add_argument(
        "--no-3d",
        dest="render_no_3d",
        action="store_true",
        help="Skip 3D ray-traced renders (for headless CI without a display)",
    )
    render_parser.add_argument(
        "--format",
        dest="render_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
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


def _add_mcp_parser(subparsers) -> None:
    """Add MCP server subcommand parser."""
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="MCP (Model Context Protocol) server for AI agents",
        description="Start an MCP server for AI agent integration with KiCad tools.",
    )
    mcp_subparsers = mcp_parser.add_subparsers(dest="mcp_command", help="MCP subcommands")

    # mcp serve subcommand
    serve_parser = mcp_subparsers.add_parser(
        "serve",
        help="Start the MCP server",
        description=(
            "Start the MCP server with the specified transport. "
            "Use stdio (default) for Claude Desktop integration, "
            "or http for web-based integrations."
        ),
    )
    serve_parser.add_argument(
        "--transport",
        "-t",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    serve_parser.add_argument(
        "--host",
        default="localhost",
        help="Host address for HTTP mode (default: localhost)",
    )
    serve_parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=8080,
        help="Port for HTTP mode (default: 8080)",
    )

    # mcp setup subcommand
    setup_parser = mcp_subparsers.add_parser(
        "setup",
        help="Configure MCP client integration",
        description=(
            "Auto-detect the kct binary and write the MCP server config "
            "for Claude Code or Claude Desktop."
        ),
    )
    setup_parser.add_argument(
        "--client",
        "-c",
        choices=["claude-code", "claude-desktop"],
        default="claude-code",
        help="MCP client to configure (default: claude-code)",
    )
    setup_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be written without making changes",
    )


def _add_ipc_parser(subparsers) -> None:
    """Add IPC subcommand parser for live KiCad instance interaction."""
    ipc_parser = subparsers.add_parser(
        "ipc",
        help="Interact with a running KiCad instance via IPC API (KiCad 9.0+)",
        description=(
            "Connect to a running KiCad instance via its IPC API (protobuf over NNG). "
            "Requires KiCad 9.0+ and the ipc optional dependency: pip install 'kicad-tools[ipc]'"
        ),
    )
    ipc_subparsers = ipc_parser.add_subparsers(dest="ipc_command", help="IPC subcommands")

    # ipc status subcommand
    status_parser = ipc_subparsers.add_parser(
        "status",
        help="Show KiCad IPC connection status",
        description="Discover running KiCad instances and report their IPC status.",
    )
    status_parser.add_argument(
        "--socket",
        "-s",
        help="Explicit path to KiCad IPC socket (auto-discovered if not provided)",
    )

    # ipc connect subcommand
    connect_parser = ipc_subparsers.add_parser(
        "connect",
        help="Test connection to a running KiCad instance",
        description="Attempt to connect to a running KiCad instance and report the result.",
    )
    connect_parser.add_argument(
        "--socket",
        "-s",
        help="Explicit path to KiCad IPC socket (auto-discovered if not provided)",
    )

    # ipc push-routes subcommand
    push_parser = ipc_subparsers.add_parser(
        "push-routes",
        help="Push routed tracks from a PCB file to a running KiCad instance",
        description=(
            "Read tracks and vias from a .kicad_pcb file and push them to "
            "a running KiCad instance via IPC. All items are created in a "
            "single undo transaction."
        ),
    )
    push_parser.add_argument(
        "pcb",
        help="Path to .kicad_pcb file containing the routing solution",
    )
    push_parser.add_argument(
        "--socket",
        "-s",
        help="Explicit path to KiCad IPC socket (auto-discovered if not provided)",
    )
    push_parser.add_argument(
        "--net",
        "-n",
        help="Only push tracks/vias for a specific net name",
    )
    push_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be pushed without actually connecting to KiCad",
    )


def _add_init_parser(subparsers) -> None:
    """Add init subcommand parser for project initialization."""
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize a KiCad project with manufacturer design rules",
        description=(
            "Create or configure a KiCad project with manufacturer-specific design rules. "
            "This prevents DRC issues by setting appropriate rules from the start."
        ),
    )
    init_parser.add_argument(
        "init_project",
        metavar="PROJECT",
        help="Project name or path to .kicad_pro file (use '.' for current directory)",
    )
    init_parser.add_argument(
        "-m",
        "--mfr",
        dest="init_mfr",
        required=True,
        metavar="MANUFACTURER",
        help="Manufacturer ID (jlcpcb, seeed, pcbway, oshpark)",
    )
    init_parser.add_argument(
        "-l",
        "--layers",
        dest="init_layers",
        type=int,
        default=2,
        help="Number of copper layers (default: 2)",
    )
    init_parser.add_argument(
        "-c",
        "--copper",
        dest="init_copper",
        type=float,
        default=1.0,
        help="Copper weight in oz (default: 1.0)",
    )
    init_parser.add_argument(
        "-t",
        "--design-type",
        dest="init_design_type",
        choices=["audio", "power_supply", "digital", "mixed_signal", "rf"],
        default=None,
        metavar="TYPE",
        help="Design type for netclass configuration (audio, power_supply, digital, mixed_signal, rf)",
    )
    init_parser.add_argument(
        "--dry-run",
        dest="init_dry_run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    init_parser.add_argument(
        "--format",
        dest="init_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )


def _add_panel_parser(subparsers) -> None:
    """Add panel subcommand parser for board panelization."""
    panel_parser = subparsers.add_parser(
        "panel",
        help="Create manufacturing panels from board PCBs",
        description=(
            "Panelize a board into a manufacturing panel with configurable "
            "grid layout, breakaway tabs, mousebite/V-cut separation, "
            "tooling holes, and fiducial marks."
        ),
    )
    panel_parser.add_argument(
        "panel_input",
        metavar="INPUT",
        help="Path to source .kicad_pcb file",
    )
    panel_parser.add_argument(
        "-o",
        "--output",
        dest="panel_output",
        default=None,
        help="Output panel PCB file path (default: <input>_panel.kicad_pcb)",
    )
    panel_parser.add_argument(
        "--rows",
        dest="panel_rows",
        type=int,
        default=2,
        help="Number of board rows (default: 2)",
    )
    panel_parser.add_argument(
        "--cols",
        dest="panel_cols",
        type=int,
        default=2,
        help="Number of board columns (default: 2)",
    )
    panel_parser.add_argument(
        "--spacing",
        dest="panel_spacing",
        type=float,
        default=2.0,
        help="Gap between boards in mm (default: 2.0)",
    )
    panel_parser.add_argument(
        "--cut",
        dest="panel_cut",
        choices=["mousebite", "vcut"],
        default="mousebite",
        help="Separation method (default: mousebite)",
    )
    panel_parser.add_argument(
        "--tab-width",
        dest="panel_tab_width",
        type=float,
        default=3.0,
        help="Tab width in mm (default: 3.0)",
    )
    panel_parser.add_argument(
        "--tab-count",
        dest="panel_tab_count",
        type=int,
        default=3,
        help="Number of tabs per edge (default: 3)",
    )
    panel_parser.add_argument(
        "--mousebite-diameter",
        dest="panel_mousebite_diameter",
        type=float,
        default=0.5,
        help="Mousebite hole diameter in mm (default: 0.5)",
    )
    panel_parser.add_argument(
        "--mousebite-spacing",
        dest="panel_mousebite_spacing",
        type=float,
        default=0.8,
        help="Mousebite hole spacing in mm (default: 0.8)",
    )
    panel_parser.add_argument(
        "--frame",
        dest="panel_frame",
        action="store_true",
        help="Add a frame (rail) around the panel",
    )
    panel_parser.add_argument(
        "--frame-width",
        dest="panel_frame_width",
        type=float,
        default=5.0,
        help="Frame rail width in mm (default: 5.0)",
    )
    panel_parser.add_argument(
        "--frame-space",
        dest="panel_frame_space",
        type=float,
        default=2.0,
        help="Gap between board and frame in mm (default: 2.0)",
    )
    panel_parser.add_argument(
        "--tooling-holes",
        dest="panel_tooling_holes",
        action="store_true",
        help="Add tooling holes to the panel frame",
    )
    panel_parser.add_argument(
        "--fiducials",
        dest="panel_fiducials",
        action="store_true",
        help="Add fiducial marks to the panel frame",
    )


def _add_pipeline_parser(subparsers) -> None:
    """Add pipeline subcommand parser for existing PCB repair workflow."""
    pipeline_parser = subparsers.add_parser(
        "pipeline",
        help="End-to-end repair pipeline for existing PCBs",
        description=(
            "Orchestrate the full repair pipeline on an existing PCB: "
            "erc, fix-silkscreen, fix-vias, route (if needed), fix-drc, optimize-traces, "
            "zone fill, audit. Auto-detects board state to skip unnecessary steps."
        ),
    )
    pipeline_parser.add_argument(
        "pipeline_input",
        metavar="INPUT",
        help="Path to .kicad_pcb or .kicad_pro file",
    )
    pipeline_parser.add_argument(
        "--step",
        "-s",
        dest="pipeline_step",
        choices=[
            "erc",
            "fix-erc",
            "sync",
            "fix-silkscreen",
            "route",
            "stitch",
            "fix-vias",
            "fix-drc",
            "optimize",
            "zones",
            "zones-refill",
            "audit",
            "report",
            "export",
        ],
        default=None,
        help="Run only this step (default: run all steps in order)",
    )
    pipeline_parser.add_argument(
        "--mfr",
        "-m",
        dest="pipeline_mfr",
        choices=get_all_manufacturer_names(),
        default="jlcpcb",
        help="Target manufacturer (default: jlcpcb)",
    )
    pipeline_parser.add_argument(
        "--layers",
        "-l",
        dest="pipeline_layers",
        choices=["auto", "2", "4", "4-sig", "4-all", "6"],
        default=None,
        help=(
            "Layer stack configuration: "
            "'auto' = auto-detect from PCB (default when omitted); "
            "'2' = 2-layer; '4' = 4-layer with GND/PWR planes; "
            "'4-sig' = 4-layer with 2 signal + 1 ground plane; "
            "'4-all' = 4-layer all-signal; '6' = 6-layer. "
            "Pass '4' when your net-class-map declares is_pour_net / plane "
            "nets so inner layers are reserved for planes ('auto' cannot see "
            "pour nets added post-route)."
        ),
    )
    pipeline_parser.add_argument(
        "--dry-run",
        dest="pipeline_dry_run",
        action="store_true",
        help="Preview pipeline steps without modifying files",
    )
    pipeline_parser.add_argument(
        "-v",
        "--verbose",
        dest="pipeline_verbose",
        action="store_true",
        help="Show detailed output from each step",
    )
    pipeline_parser.add_argument(
        "-f",
        "--force",
        dest="pipeline_force",
        action="store_true",
        help="Force all steps (e.g., re-route even if already routed)",
    )
    pipeline_parser.add_argument(
        "--commit",
        dest="pipeline_commit",
        action="store_true",
        default=False,
        help="Create a git commit with modified files after a successful pipeline run",
    )
    pipeline_parser.add_argument(
        "--max-displacement",
        dest="pipeline_max_displacement",
        type=float,
        default=2.0,
        help=(
            "Maximum nudge/slide distance in mm for fix-drc step (default: 2.0). "
            "Increase when enlarged vias cause segment-to-via violations that "
            "exceed the displacement budget."
        ),
    )
    pipeline_parser.add_argument(
        "--route-skip-threshold",
        dest="pipeline_route_skip_threshold",
        type=float,
        default=95.0,
        metavar="PERCENT",
        help=(
            "Minimum signal-net completion percentage required to skip the "
            "route step (default: 95.0). Single-pad nets and zone-fillable "
            "plane nets are excluded from the signal-net subset and never "
            "block the skip."
        ),
    )
    pipeline_parser.add_argument(
        "--best-effort",
        dest="pipeline_best_effort",
        action="store_true",
        default=False,
        help="Continue past routing failures to zone fill, audit, and export",
    )
    pipeline_parser.add_argument(
        "--no-cache",
        dest="pipeline_no_cache",
        action="store_true",
        default=False,
        help="Bypass routing cache (force fresh routing in the route step)",
    )
    pipeline_parser.add_argument(
        "--clear-cache",
        dest="pipeline_clear_cache",
        action="store_true",
        default=False,
        help="Clear routing cache before the route step runs",
    )
    pipeline_parser.add_argument(
        "--sch",
        "--schematic",
        dest="pipeline_sch",
        default=None,
        help="Path to root .kicad_sch file (overrides auto-discovery)",
    )
    pipeline_parser.add_argument(
        "--apply-sync",
        dest="pipeline_apply_sync",
        action="store_true",
        default=False,
        help=(
            "In the sync step, auto-add missing footprints and apply"
            " high-confidence value/footprint corrections in place."
        ),
    )


def _add_create_pcb_parser(subparsers) -> None:
    """Add create-pcb subcommand parser for generating PCBs from schematics."""
    create_pcb_parser = subparsers.add_parser(
        "create-pcb",
        help="Create a PCB from a KiCad schematic",
        description=(
            "Generate a PCB file from a KiCad schematic. Extracts netlist data, "
            "creates a blank PCB with specified dimensions, places footprints for "
            "all components, and assigns nets based on schematic connectivity."
        ),
    )
    create_pcb_parser.add_argument(
        "create_pcb_schematic",
        metavar="SCHEMATIC",
        help="Path to .kicad_sch schematic file",
    )
    create_pcb_parser.add_argument(
        "-o",
        "--output",
        dest="create_pcb_output",
        help="Output .kicad_pcb file path (default: <schematic-stem>.kicad_pcb)",
    )
    create_pcb_parser.add_argument(
        "--width",
        dest="create_pcb_width",
        type=float,
        default=100.0,
        help="Board width in mm (default: 100.0)",
    )
    create_pcb_parser.add_argument(
        "--height",
        dest="create_pcb_height",
        type=float,
        default=100.0,
        help="Board height in mm (default: 100.0)",
    )
    create_pcb_parser.add_argument(
        "--layers",
        dest="create_pcb_layers",
        type=int,
        choices=[2, 4],
        default=2,
        help="Number of copper layers (default: 2)",
    )
    create_pcb_parser.add_argument(
        "--title",
        dest="create_pcb_title",
        default="",
        help="Board title for title block (default: schematic filename)",
    )
    create_pcb_parser.add_argument(
        "--revision",
        dest="create_pcb_revision",
        default="1.0",
        help="Board revision (default: 1.0)",
    )
    create_pcb_parser.add_argument(
        "--company",
        dest="create_pcb_company",
        default="",
        help="Company name for title block",
    )
    create_pcb_parser.add_argument(
        "--no-place",
        dest="create_pcb_no_place",
        action="store_true",
        help="Skip automatic component placement",
    )
    create_pcb_parser.add_argument(
        "--spacing",
        dest="create_pcb_spacing",
        type=float,
        default=15.0,
        help="Spacing between auto-placed components in mm (default: 15.0)",
    )
    create_pcb_parser.add_argument(
        "--columns",
        dest="create_pcb_columns",
        type=int,
        default=10,
        help="Number of columns for auto-placement grid (default: 10)",
    )
    create_pcb_parser.add_argument(
        "--dry-run",
        dest="create_pcb_dry_run",
        action="store_true",
        help="Show what would be done without saving",
    )


def _add_build_parser(subparsers) -> None:
    """Add build subcommand parser for end-to-end workflow."""
    build_parser = subparsers.add_parser(
        "build",
        help="Build from spec to manufacturable design",
        description=(
            "Orchestrate the full build workflow from .kct specification to routed, verified PCB. "
            "Runs schematic generation, PCB generation, autorouting, and verification in sequence."
        ),
    )
    build_parser.add_argument(
        "build_spec",
        metavar="SPEC",
        nargs="?",
        help="Path to .kct file or project directory (default: current directory)",
    )
    build_parser.add_argument(
        "--step",
        "-s",
        dest="build_step",
        choices=[
            "schematic",
            "erc",
            "pcb",
            "sync",
            "outline",
            "placement",
            "zones",
            "silkscreen",
            "route",
            "stitch",
            "page-fit",
            "preflight-routing",
            "verify",
            "export",
            "all",
        ],
        default="all",
        help="Run specific step or all (default: all)",
    )
    build_parser.add_argument(
        "--mfr",
        "-m",
        dest="build_mfr",
        choices=get_all_manufacturer_names(),
        default=None,
        help=(
            "Target manufacturer for verification. When omitted, the "
            "project spec's manufacturing.target_fab is used (falling back "
            "to jlcpcb). An explicit --mfr always overrides the spec."
        ),
    )
    build_parser.add_argument(
        "--dry-run",
        dest="build_dry_run",
        action="store_true",
        help="Preview build steps without executing",
    )
    build_parser.add_argument(
        "-v",
        "--verbose",
        dest="build_verbose",
        action="store_true",
        help="Show detailed output",
    )
    build_parser.add_argument(
        "-q",
        "--quiet",
        dest="build_quiet",
        action="store_true",
        help="Suppress progress output",
    )
    build_parser.add_argument(
        "-f",
        "--force",
        dest="build_force",
        action="store_true",
        help="Force rebuild, ignoring existing outputs and timestamp checks",
    )
    build_parser.add_argument(
        "-o",
        "--output",
        dest="build_output",
        help="Output directory for generated files (default: project directory)",
    )
    build_parser.add_argument(
        "--optimize-placement",
        dest="build_optimize_placement",
        action="store_true",
        help="Run CMA-ES placement optimization before routing (opt-in)",
    )
    build_parser.add_argument(
        "--no-smoke-check",
        dest="build_no_smoke_check",
        action="store_true",
        help=(
            "Disable the per-step kicad-cli load smoke check that runs "
            "after each PCB-write step.  Use to restore prior behaviour "
            "when kicad-cli is misbehaving or pipeline speed matters."
        ),
    )
    build_parser.add_argument(
        "--allow-incomplete",
        dest="build_allow_incomplete",
        action="store_true",
        help=(
            "Skip the routing-completeness preflight "
            "(advertised in failure messages; CI greppable)."
        ),
    )


def _add_build_native_parser(subparsers) -> None:
    """Add build-native subcommand parser for building C++ router backend."""
    build_native_parser = subparsers.add_parser(
        "build-native",
        help="Build C++ router backend for 10-100x faster routing",
        description=(
            "Build and install the C++ router extension for significantly faster routing. "
            "This command handles all the build steps automatically: checking prerequisites, "
            "installing nanobind, configuring cmake, and building the extension."
        ),
    )
    build_native_parser.add_argument(
        "-v",
        "--verbose",
        dest="build_native_verbose",
        action="store_true",
        help="Show detailed build output",
    )
    build_native_parser.add_argument(
        "-f",
        "--force",
        dest="build_native_force",
        action="store_true",
        help="Force rebuild even if already installed",
    )
    build_native_parser.add_argument(
        "-j",
        "--jobs",
        dest="build_native_jobs",
        type=int,
        default=None,
        help="Number of parallel build jobs (default: auto)",
    )
    build_native_parser.add_argument(
        "--format",
        dest="build_native_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    build_native_parser.add_argument(
        "--check",
        dest="build_native_check",
        action="store_true",
        help="Just check if C++ backend is available, don't build",
    )


def _add_doctor_parser(subparsers) -> None:
    """Add doctor subcommand parser for environment/installation health checks."""
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Diagnose kicad-tools installation health (version-record drift)",
        description=(
            "Check the health of a kicad-tools installation. The first check is "
            "version-record drift: the installed package version (ground truth) is "
            "compared against the records the installer stamps into a consumer repo "
            "(pyproject.toml dependency pin, .kct/install-metadata.json, and the "
            "CLAUDE.md marker block). Advisory by default; use --strict to exit "
            "non-zero on drift so it can gate CI / pre-commit hooks."
        ),
    )
    doctor_parser.add_argument(
        "--root",
        dest="doctor_root",
        default=".",
        metavar="DIR",
        help="Directory to look for the version records in (default: current dir)",
    )
    doctor_parser.add_argument(
        "--format",
        dest="doctor_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    doctor_parser.add_argument(
        "--strict",
        dest="doctor_strict",
        action="store_true",
        help="Exit 1 when any version record has drifted (default: advisory exit 0)",
    )


def _add_spec_parser(subparsers) -> None:
    """Add spec subcommand parser for .kct project specification files."""
    spec_parser = subparsers.add_parser(
        "spec",
        help="Project specification (.kct) management",
        description=(
            "Manage .kct project specification files that capture design intent, "
            "requirements, decisions, and progress for PCB projects."
        ),
    )
    spec_subparsers = spec_parser.add_subparsers(
        dest="spec_command",
        help="Spec commands",
    )

    # spec init
    spec_init = spec_subparsers.add_parser(
        "init",
        help="Initialize a new .kct specification file",
        description="Create a new project specification file from a template.",
    )
    spec_init.add_argument(
        "spec_name",
        metavar="NAME",
        help="Project name",
    )
    spec_init.add_argument(
        "-t",
        "--template",
        dest="spec_template",
        choices=["minimal", "power_supply", "sensor_board", "mcu_breakout"],
        default="minimal",
        help="Template to use (default: minimal)",
    )
    spec_init.add_argument(
        "-o",
        "--output",
        dest="spec_output",
        metavar="FILE",
        help="Output file path (default: project.kct)",
    )
    spec_init.add_argument(
        "-f",
        "--force",
        dest="spec_force",
        action="store_true",
        help="Overwrite existing file",
    )

    # spec validate
    spec_validate = spec_subparsers.add_parser(
        "validate",
        help="Validate a .kct specification file",
        description="Check a spec file for schema and semantic errors.",
    )
    spec_validate.add_argument(
        "spec_file",
        metavar="FILE",
        help="Path to .kct file",
    )

    # spec status
    spec_status = spec_subparsers.add_parser(
        "status",
        help="Show project status and progress",
        description="Display progress, phase status, and recent decisions from a spec file.",
    )
    spec_status.add_argument(
        "spec_file",
        metavar="FILE",
        help="Path to .kct file",
    )

    # spec decide
    spec_decide = spec_subparsers.add_parser(
        "decide",
        help="Record a design decision",
        description="Add a design decision with rationale to the spec file.",
    )
    spec_decide.add_argument(
        "spec_file",
        metavar="FILE",
        help="Path to .kct file",
    )
    spec_decide.add_argument(
        "--topic",
        dest="decide_topic",
        required=True,
        help="Decision topic (e.g., 'Buck Converter Selection')",
    )
    spec_decide.add_argument(
        "--choice",
        dest="decide_choice",
        required=True,
        help="Chosen option",
    )
    spec_decide.add_argument(
        "--rationale",
        dest="decide_rationale",
        required=True,
        help="Reasoning for the decision",
    )
    spec_decide.add_argument(
        "--alternatives",
        dest="decide_alternatives",
        help="Comma-separated alternative options considered",
    )

    # spec check
    spec_check = spec_subparsers.add_parser(
        "check",
        help="Mark a checklist item as complete",
        description="Mark a progress checklist item as completed.",
    )
    spec_check.add_argument(
        "spec_file",
        metavar="FILE",
        help="Path to .kct file",
    )
    spec_check.add_argument(
        "check_item",
        metavar="ITEM",
        help="Checklist item to mark complete (format: 'phase.item' or 'item')",
    )


def _add_benchmark_parser(subparsers) -> None:
    """Add benchmark subcommand parser."""
    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="Run routing benchmarks and regression tests",
        description="Benchmark routing performance and detect regressions.",
    )
    benchmark_subparsers = benchmark_parser.add_subparsers(
        dest="benchmark_command",
        help="Benchmark commands",
    )

    # benchmark run
    benchmark_run = benchmark_subparsers.add_parser(
        "run",
        help="Run benchmark suite",
        description="Execute routing benchmarks and collect metrics.",
    )
    benchmark_run.add_argument(
        "--cases",
        help="Comma-separated list of cases to run (default: all)",
    )
    benchmark_run.add_argument(
        "--strategies",
        help="Comma-separated strategies: basic,negotiated,monte_carlo (default: all)",
    )
    benchmark_run.add_argument(
        "--difficulty",
        choices=["easy", "medium", "hard"],
        help="Filter by difficulty level",
    )
    benchmark_run.add_argument(
        "-o",
        "--output",
        help="Output file path for results",
    )
    benchmark_run.add_argument(
        "--save",
        action="store_true",
        help="Save results to benchmarks/ directory",
    )
    benchmark_run.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed progress",
    )

    # benchmark compare
    benchmark_compare = benchmark_subparsers.add_parser(
        "compare",
        help="Compare against baseline",
        description="Run benchmarks and check for regressions against a baseline.",
    )
    benchmark_compare.add_argument(
        "--baseline",
        required=True,
        help="Path to baseline results JSON file",
    )
    benchmark_compare.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Exit with error on warnings (not just errors)",
    )
    benchmark_compare.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed progress",
    )

    # benchmark report
    benchmark_report = benchmark_subparsers.add_parser(
        "report",
        help="Generate benchmark report",
        description="Generate human-readable report from benchmark results.",
    )
    benchmark_report.add_argument(
        "input",
        help="Path to benchmark results JSON file",
    )
    benchmark_report.add_argument(
        "--format",
        choices=["text", "markdown"],
        default="text",
        help="Output format (default: text)",
    )

    # benchmark list
    benchmark_list = benchmark_subparsers.add_parser(
        "list",
        help="List available benchmark cases",
        description="Show all registered benchmark test cases.",
    )
    benchmark_list.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )


def _add_sync_parser(subparsers) -> None:
    """Add sync subcommand parser for schematic/PCB reconciliation."""
    sync_parser = subparsers.add_parser(
        "sync",
        help="Reconcile schematic and PCB references",
        description="Analyze and fix mismatches between schematic and PCB",
    )

    # Mode selection
    mode = sync_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--analyze",
        dest="sync_analyze",
        action="store_true",
        help="Analyze mismatches and report proposed changes",
    )
    mode.add_argument(
        "--apply",
        dest="sync_apply",
        action="store_true",
        help="Apply proposed changes (requires --dry-run or --confirm)",
    )

    # File arguments
    sync_parser.add_argument(
        "sync_project",
        nargs="?",
        help="Path to .kicad_pro file (auto-finds schematic and PCB)",
    )
    sync_parser.add_argument(
        "--schematic",
        "-s",
        dest="sync_schematic",
        help="Path to .kicad_sch file (required if no project file)",
    )
    sync_parser.add_argument(
        "--pcb",
        "-p",
        dest="sync_pcb",
        help="Path to .kicad_pcb file (required if no project file)",
    )

    # Output options
    sync_parser.add_argument(
        "--format",
        dest="sync_format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    sync_parser.add_argument(
        "--output-mapping",
        "-m",
        dest="sync_output_mapping",
        help="Save analysis mapping to JSON file",
    )
    sync_parser.add_argument(
        "-o",
        "--output",
        dest="sync_output",
        help="Write modified PCB to this file instead of overwriting",
    )

    # Apply options
    sync_parser.add_argument(
        "--dry-run",
        dest="sync_dry_run",
        action="store_true",
        help="Show what would change without modifying files",
    )
    sync_parser.add_argument(
        "--confirm",
        dest="sync_confirm",
        action="store_true",
        help="Actually apply changes (required with --apply)",
    )
    sync_parser.add_argument(
        "--min-confidence",
        dest="sync_min_confidence",
        choices=["high", "medium", "low"],
        default="high",
        help="Minimum confidence level to apply (default: high)",
    )
    sync_parser.add_argument(
        "--remove-orphans",
        dest="sync_remove_orphans",
        action="store_true",
        help="Remove PCB footprints not present in schematic (with --apply)",
    )
    sync_parser.add_argument(
        "--force",
        dest="sync_force",
        action="store_true",
        help="Force removal of orphans even if they have routed traces",
    )


def _add_run_parser(subparsers) -> None:
    """Add run subcommand parser for executing Python scripts.

    This command solves the common issue where users install kicad-tools
    via pipx but cannot run board generation scripts with `python3 script.py`
    because the kicad_tools module is not available in the system Python.
    """
    run_parser = subparsers.add_parser(
        "run",
        help="Run a Python script using the kicad-tools interpreter",
        description=(
            "Execute a Python script using the same Python interpreter that runs kicad-tools. "
            "This is useful when kicad-tools is installed via pipx, as board generation scripts "
            "can import kicad_tools modules that would otherwise be unavailable.\n\n"
            "Example:\n"
            "  kct run generate_design.py           # Run script\n"
            "  kct run generate_design.py output/   # Pass arguments to script"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_parser.add_argument(
        "run_script",
        metavar="SCRIPT",
        help="Path to Python script to run",
    )
    run_parser.add_argument(
        "run_args",
        nargs="*",
        metavar="ARGS",
        help="Arguments to pass to the script",
    )


def _add_explain_parser(subparsers) -> None:
    """Add explain subcommand parser for design rule explanations."""
    explain_parser = subparsers.add_parser(
        "explain",
        help="Explain design rules and DRC violations",
        description=(
            "Explain design rules with spec references and fix suggestions. "
            "Can explain individual rules, search for rules, or explain violations "
            "from a DRC report."
        ),
    )

    # Main argument - rule ID
    explain_parser.add_argument(
        "explain_rule",
        nargs="?",
        metavar="RULE",
        help="Rule ID to explain (e.g., trace_clearance, via_drill)",
    )

    # Discovery options
    explain_parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        dest="explain_list",
        help="List all available rule IDs",
    )
    explain_parser.add_argument(
        "--search",
        "-s",
        metavar="QUERY",
        dest="explain_search",
        help="Search for rules matching a query",
    )

    # Context values
    explain_parser.add_argument(
        "--value",
        "-v",
        type=float,
        dest="explain_value",
        help="Current/actual value for contextualized explanation",
    )
    explain_parser.add_argument(
        "--required",
        "-r",
        type=float,
        dest="explain_required",
        help="Required/minimum value",
    )
    explain_parser.add_argument(
        "--unit",
        "-u",
        default="mm",
        dest="explain_unit",
        help="Unit of measurement (default: mm)",
    )
    explain_parser.add_argument(
        "--net1",
        dest="explain_net1",
        help="First net name for context",
    )
    explain_parser.add_argument(
        "--net2",
        dest="explain_net2",
        help="Second net name for context",
    )

    # DRC report integration
    explain_parser.add_argument(
        "--drc-report",
        "-d",
        metavar="FILE",
        dest="explain_drc_report",
        help="Path to DRC report file to explain all violations",
    )

    # Output format
    explain_parser.add_argument(
        "--format",
        "-f",
        choices=["text", "tree", "json", "markdown"],
        default="text",
        dest="explain_format",
        help="Output format (default: text)",
    )

    # Interface/net explanation
    explain_parser.add_argument(
        "--net",
        "-n",
        metavar="NAME",
        dest="explain_net",
        help="Explain constraints for a specific net",
    )
    explain_parser.add_argument(
        "--interface",
        "-i",
        dest="explain_interface",
        help="Specify interface type for net (usb, i2c, spi)",
    )


def _add_detect_mistakes_parser(subparsers) -> None:
    """Add detect-mistakes subcommand parser for PCB design mistake detection."""
    mistakes_parser = subparsers.add_parser(
        "detect-mistakes",
        help="Detect common PCB design mistakes with educational explanations",
        description=(
            "Detect common PCB design mistakes like bypass capacitor placement, "
            "crystal trace length, differential pair skew, and more. Each mistake "
            "includes an explanation and fix suggestion."
        ),
    )

    # Main argument - PCB file
    mistakes_parser.add_argument(
        "mistakes_pcb",
        nargs="?",
        metavar="PCB",
        help="Path to .kicad_pcb file to analyze",
    )

    # Category filter
    mistakes_parser.add_argument(
        "--category",
        "-c",
        dest="mistakes_category",
        choices=[
            "bypass_capacitor",
            "crystal_oscillator",
            "differential_pair",
            "power_trace",
            "thermal_management",
            "emi_shielding",
            "decoupling",
            "grounding",
            "via_placement",
            "manufacturability",
        ],
        help="Only check specific category",
    )

    # Severity filter
    mistakes_parser.add_argument(
        "--severity",
        "-s",
        dest="mistakes_severity",
        choices=["error", "warning", "info"],
        help="Only show issues of this severity or higher",
    )

    # Output format
    mistakes_parser.add_argument(
        "--format",
        "-f",
        dest="mistakes_format",
        choices=["table", "json", "tree", "summary"],
        default="table",
        help="Output format (default: table)",
    )

    # Strict mode
    mistakes_parser.add_argument(
        "--strict",
        action="store_true",
        dest="mistakes_strict",
        help="Exit with error code on warnings",
    )

    # List categories
    mistakes_parser.add_argument(
        "--list-categories",
        action="store_true",
        dest="mistakes_list_categories",
        help="List available check categories and exit",
    )

    # Verbose output
    mistakes_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        dest="mistakes_verbose",
        help="Show detailed information",
    )


def _add_calibrate_parser(subparsers) -> None:
    """Add calibrate subcommand parser."""
    calibrate_parser = subparsers.add_parser(
        "calibrate",
        help="Calibrate routing performance settings for your machine",
    )
    calibrate_parser.add_argument(
        "--show",
        action="store_true",
        dest="calibrate_show",
        help="Show current performance configuration without running calibration",
    )
    calibrate_parser.add_argument(
        "--show-gpu",
        action="store_true",
        dest="calibrate_show_gpu",
        help="Show GPU capabilities and current configuration",
    )
    calibrate_parser.add_argument(
        "--gpu",
        action="store_true",
        dest="calibrate_gpu",
        help="Run GPU-specific benchmarks and determine optimal thresholds",
    )
    calibrate_parser.add_argument(
        "--all",
        action="store_true",
        dest="calibrate_all",
        help="Run full calibration including GPU benchmarks",
    )
    calibrate_parser.add_argument(
        "--benchmark",
        action="store_true",
        dest="calibrate_benchmark",
        help="Run full benchmarks with detailed output",
    )
    calibrate_parser.add_argument(
        "--quick",
        action="store_true",
        dest="calibrate_quick",
        help="Run abbreviated calibration (faster but less accurate)",
    )
    calibrate_parser.add_argument(
        "-o",
        "--output",
        dest="calibrate_output",
        help="Output path for configuration file",
    )
    calibrate_parser.add_argument(
        "--json",
        action="store_true",
        dest="calibrate_json",
        help="Output configuration as JSON",
    )
    calibrate_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        dest="calibrate_verbose",
        help="Show detailed progress information",
    )


def _add_screenshot_parser(subparsers) -> None:
    """Add screenshot subcommand parser."""
    screenshot_parser = subparsers.add_parser(
        "screenshot",
        help="Capture a PNG screenshot of a KiCad board or schematic",
    )
    screenshot_parser.add_argument(
        "screenshot_input",
        help="Path to .kicad_pcb or .kicad_sch file",
    )
    screenshot_parser.add_argument(
        "-o",
        "--output",
        dest="screenshot_output",
        help="Output PNG file path (default: <input>.png)",
    )
    screenshot_parser.add_argument(
        "--layers",
        dest="screenshot_layers",
        default=None,
        help=(
            "Layer specification for PCB screenshots. "
            "Preset name (default, copper, assembly, front, back) "
            "or comma-separated layer list"
        ),
    )
    screenshot_parser.add_argument(
        "--max-size",
        type=int,
        dest="screenshot_max_size",
        default=1568,
        help="Maximum image dimension in pixels (default: 1568)",
    )
    screenshot_parser.add_argument(
        "--bw",
        "--black-and-white",
        action="store_true",
        dest="screenshot_bw",
        help="Use black and white rendering",
    )
    screenshot_parser.add_argument(
        "--theme",
        dest="screenshot_theme",
        default=None,
        help="KiCad color theme name",
    )


def _add_report_parser(subparsers) -> None:
    """Add report subcommand parser."""
    report_parser = subparsers.add_parser(
        "report",
        help="Generate a Markdown design report",
    )
    report_sub = report_parser.add_subparsers(dest="report_command")

    gen_parser = report_sub.add_parser("generate", help="Generate a design report")
    gen_parser.add_argument(
        "report_input",
        help="Path to .kicad_pro or .kicad_pcb file",
    )
    gen_parser.add_argument(
        "--mfr",
        dest="report_mfr",
        default="unknown",
        help="Target manufacturer (default: unknown)",
    )
    gen_parser.add_argument(
        "-o",
        "--output",
        dest="report_output",
        default="reports",
        help="Output directory for versioned reports (default: reports/)",
    )
    gen_parser.add_argument(
        "--data-dir",
        dest="report_data_dir",
        default=None,
        help="Directory containing pre-collected data/ and figures/ snapshots",
    )
    gen_parser.add_argument(
        "--template",
        dest="report_template",
        default=None,
        help="Path to a custom Jinja2 template file",
    )
    gen_parser.add_argument(
        "--sch",
        dest="report_sch",
        default=None,
        help="Path to root .kicad_sch file (inferred from input if omitted)",
    )
    gen_parser.add_argument(
        "--no-figures",
        dest="report_no_figures",
        action="store_true",
        default=False,
        help="Skip figure generation (useful when kicad-cli/cairosvg are unavailable)",
    )
    gen_parser.add_argument(
        "--quantity",
        dest="report_quantity",
        type=int,
        default=5,
        help="Quantity for cost estimation (default: 5)",
    )
    gen_parser.add_argument(
        "--skip-erc",
        dest="report_skip_erc",
        action="store_true",
        help="Skip ERC during auto-collection",
    )
    gen_parser.add_argument(
        "--skip-collect",
        dest="report_skip_collect",
        action="store_true",
        help="Skip auto-collection; generate skeleton report (legacy behavior)",
    )


def _add_export_parser(subparsers) -> None:
    """Add export subcommand parser for manufacturing packages."""
    export_parser = subparsers.add_parser(
        "export",
        help="Generate a complete manufacturing package (BOM, CPL, Gerbers, project ZIP, manifest)",
    )
    export_parser.add_argument(
        "export_pcb",
        help="Path to .kicad_pcb file",
    )
    export_parser.add_argument(
        "--mfr",
        "-m",
        dest="export_mfr",
        default="jlcpcb",
        choices=[*get_all_manufacturer_names(), "generic"],
        help="Target manufacturer (default: jlcpcb)",
    )
    export_parser.add_argument(
        "-o",
        "--output",
        dest="export_output",
        default=None,
        help="Output directory (default: <pcb-dir>/manufacturing/)",
    )
    export_parser.add_argument(
        "--sch",
        dest="export_sch",
        default=None,
        help="Path to .kicad_sch file (auto-detected by default)",
    )
    export_parser.add_argument(
        "--dry-run",
        dest="export_dry_run",
        action="store_true",
        help="Show what would be generated without writing files",
    )
    export_parser.add_argument(
        "--no-report",
        dest="export_no_report",
        action="store_true",
        help="Skip report generation",
    )
    export_parser.add_argument(
        "--no-gerbers",
        dest="export_no_gerbers",
        action="store_true",
        help="Skip Gerber export",
    )
    export_parser.add_argument(
        "--no-bom",
        dest="export_no_bom",
        action="store_true",
        help="Skip BOM generation",
    )
    export_parser.add_argument(
        "--no-cpl",
        dest="export_no_cpl",
        action="store_true",
        help="Skip CPL/pick-and-place generation",
    )
    export_parser.add_argument(
        "--no-project-zip",
        dest="export_no_project_zip",
        action="store_true",
        help="Skip KiCad project ZIP creation",
    )
    export_parser.add_argument(
        "--auto-lcsc",
        dest="export_auto_lcsc",
        action="store_true",
        default=True,
        help="Auto-match LCSC part numbers for JLCPCB BOMs (default: enabled)",
    )
    export_parser.add_argument(
        "--no-auto-lcsc",
        dest="export_no_auto_lcsc",
        action="store_true",
        help="Disable LCSC auto-matching",
    )
    export_parser.add_argument(
        "--skip-preflight",
        dest="export_skip_preflight",
        action="store_true",
        help=(
            "Skip BOM/ERC/LCSC/cosmetic pre-flight validation checks. Does NOT "
            "suppress the hard connectivity safety floor (net shorts). Use "
            "--skip-drc-floor to override that too."
        ),
    )
    export_parser.add_argument(
        "--skip-drc-floor",
        dest="export_skip_drc_floor",
        action="store_true",
        help=(
            "Disable the connectivity safety floor that blocks export on net "
            "shorts in a pre-existing DRC report. Only for known-safe workarounds."
        ),
    )
    export_parser.add_argument(
        "--strict-preflight",
        dest="export_strict_preflight",
        action="store_true",
        help="Block export when preflight checks fail (for CI; default: export proceeds with warnings)",
    )
    export_parser.add_argument(
        "--skip-drc",
        dest="export_skip_drc",
        action="store_true",
        help="Skip DRC check in pre-flight validation",
    )
    export_parser.add_argument(
        "--skip-erc",
        dest="export_skip_erc",
        action="store_true",
        help="Skip ERC check in pre-flight validation",
    )
    export_parser.add_argument(
        "--drc-report",
        dest="export_drc_report",
        default=None,
        help="Path to pre-existing DRC report file",
    )
    export_parser.add_argument(
        "--erc-report",
        dest="export_erc_report",
        default=None,
        help="Path to pre-existing ERC report file",
    )
    export_parser.add_argument(
        "--keep-versions",
        dest="export_keep_versions",
        action="store_true",
        help="Preserve versioned vN/ report directories (default: flat report/ directory)",
    )
    export_parser.add_argument(
        "--keep-build-artifacts",
        dest="export_keep_build_artifacts",
        action="store_true",
        help="Preserve intermediate report files (markdown, figures, data) in .build/ directory",
    )
    export_parser.add_argument(
        "--keep-gerber-files",
        dest="export_keep_gerber_files",
        action="store_true",
        help="Keep individual gerber/drill files alongside the zip archive",
    )
    export_parser.add_argument(
        "--include-tht",
        dest="export_include_tht",
        action="store_true",
        help="Include through-hole components in CPL (they are excluded by default for JLCPCB)",
    )
    export_parser.add_argument(
        "--format",
        dest="export_format",
        default="text",
        choices=["text", "json"],
        help="Output format for preflight results (default: text)",
    )
