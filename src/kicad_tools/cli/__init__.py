"""
Command-line interface tools for kicad-tools.

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
    kicad-tools route-auto <pcb>       - Orchestrator-based smart routing for a net
    kicad-tools zones <command>        - Add and fill copper pour zones
    kicad-tools reason <pcb>           - LLM-driven PCB layout reasoning
    kicad-tools placement <command>    - Detect and fix placement conflicts
    kicad-tools optimize-traces <pcb>  - Optimize PCB traces
    kicad-tools validate-footprints    - Validate footprint pad spacing
    kicad-tools fix-footprints <pcb>   - Fix footprint pad spacing issues
    kicad-tools analyze <command>      - PCB analysis tools (congestion, etc.)
    kicad-tools suggest <command>      - Part suggestions (alternatives, etc.)
    kicad-tools config                 - View/manage configuration
    kicad-tools interactive            - Launch interactive REPL mode
    kicad-tools net-status <pcb>       - Report net connectivity status
    kicad-tools pipeline <pcb>          - End-to-end repair pipeline for existing PCBs
    kicad-tools init <project>         - Initialize project with manufacturer rules
    kicad-tools run <script>           - Run Python script with kicad-tools interpreter

See `kicad-tools --help` for complete documentation.
"""

import sys

from kicad_tools.config import Config
from kicad_tools.exceptions import KiCadToolsError
from kicad_tools.units import get_unit_formatter, set_current_formatter

from .commands import (
    run_analyze_command,
    run_audit_command,
    run_benchmark_command,
    run_build_command,
    run_build_native_command,
    run_check_command,
    run_clean_command,
    run_config_command,
    run_constraints_command,
    run_datasheet_command,
    run_decisions_command,
    run_estimate_command,
    run_fix_footprints_command,
    run_footprint_command,
    run_impedance_command,
    run_init_command,
    run_interactive_command,
    run_lib_command,
    run_mcp_command,
    run_mfr_command,
    run_optimize_command,
    run_optimize_placement_command,
    run_parts_command,
    run_pcb_command,
    run_pipeline_command,
    run_placement_command,
    run_reason_command,
    run_route_auto_command,
    run_route_command,
    run_run_command,
    run_sch_command,
    run_spec_command,
    run_suggest_command,
    run_validate_command,
    run_validate_footprints_command,
    run_zones_command,
)
from .parser import create_parser
from .utils import format_error, print_error

__all__ = [
    "main",
    "symbols_main",
    "nets_main",
    "erc_main",
    "drc_main",
    "drc_summary_main",
    "bom_main",
    "net_status_main",
    "format_error",
    "print_error",
]


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kicad-tools CLI."""
    parser = create_parser()

    # Handle footprint generate specially - it has its own subcommand parser
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) >= 2 and argv[0] == "footprint" and argv[1] == "generate":
        from .footprint_generate import main as generate_main

        return generate_main(argv[2:]) or 0

    # Handle erc backwards compatibility: kct erc <file> -> kct erc parse <file>
    # Insert "parse" subcommand if the first arg after "erc" isn't a known subcommand
    argv = list(argv)  # Make a copy to avoid mutating the original
    if len(argv) >= 2 and argv[0] == "erc" and argv[1] not in ("parse", "explain", "-h", "--help"):
        argv.insert(1, "parse")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    # Get global verbose flag (use getattr for backwards compatibility)
    verbose = getattr(args, "global_verbose", False)

    # Initialize unit formatter with CLI > env > config precedence
    cli_units = getattr(args, "global_units", None)
    config = Config.load()
    formatter = get_unit_formatter(cli_units=cli_units, config=config)
    set_current_formatter(formatter)

    try:
        return _dispatch_command(args)
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130
    except KiCadToolsError as e:
        print_error(e, verbose)
        return 1
    except Exception as e:
        print_error(e, verbose)
        return 1


def _dispatch_command(args) -> int:
    """Dispatch to the appropriate command handler."""
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

    elif args.command == "netlist":
        from .netlist_cmd import main as netlist_cmd

        if not args.netlist_command:
            # No subcommand, show help
            return netlist_cmd(["--help"])

        sub_argv = [args.netlist_command]

        if args.netlist_command == "compare":
            sub_argv.extend([args.netlist_old, args.netlist_new])
        elif args.netlist_command == "export":
            sub_argv.append(args.netlist_schematic)
            if hasattr(args, "netlist_output") and args.netlist_output:
                sub_argv.extend(["-o", args.netlist_output])
        else:
            sub_argv.append(args.netlist_schematic)

        if hasattr(args, "netlist_format") and args.netlist_format:
            sub_argv.extend(["--format", args.netlist_format])
        if hasattr(args, "netlist_sort") and args.netlist_sort != "connections":
            sub_argv.extend(["--sort", args.netlist_sort])
        if hasattr(args, "netlist_net") and args.netlist_net:
            sub_argv.extend(["--net", args.netlist_net])

        return netlist_cmd(sub_argv)

    elif args.command == "erc":
        # Check for subcommand
        erc_command = getattr(args, "erc_command", None)

        if erc_command == "explain":
            from .erc_explain_cmd import main as erc_explain_cmd

            sub_argv = [args.explain_input]
            if hasattr(args, "explain_format") and args.explain_format != "text":
                sub_argv.extend(["--format", args.explain_format])
            if hasattr(args, "explain_errors_only") and args.explain_errors_only:
                sub_argv.append("--errors-only")
            if hasattr(args, "explain_filter_type") and args.explain_filter_type:
                sub_argv.extend(["--type", args.explain_filter_type])
            if hasattr(args, "explain_keep_report") and args.explain_keep_report:
                sub_argv.append("--keep-report")
            return erc_explain_cmd(sub_argv)

        elif erc_command == "parse":
            # Parse subcommand (default behavior)
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

        else:
            # No subcommand provided, show help
            from .parser import create_parser

            parser = create_parser()
            parser.parse_args(["erc", "--help"])
            return 0

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

    elif args.command == "check":
        return run_check_command(args)

    elif args.command == "sch":
        return run_sch_command(args)

    elif args.command == "pcb":
        return run_pcb_command(args)

    elif args.command == "lib":
        return run_lib_command(args)

    elif args.command == "footprint":
        return run_footprint_command(args)

    elif args.command == "mfr":
        return run_mfr_command(args)

    elif args.command == "parts":
        return run_parts_command(args)

    elif args.command == "datasheet":
        return run_datasheet_command(args)

    elif args.command == "decisions":
        return run_decisions_command(args)

    elif args.command == "zones":
        return run_zones_command(args)

    elif args.command == "route":
        return run_route_command(args)

    elif args.command == "route-auto":
        return run_route_auto_command(args)

    elif args.command == "reason":
        return run_reason_command(args)

    elif args.command == "placement":
        return run_placement_command(args)

    elif args.command == "optimize-placement":
        return run_optimize_placement_command(args)

    elif args.command == "optimize-traces":
        return run_optimize_command(args)

    elif args.command == "validate-footprints":
        return run_validate_footprints_command(args)

    elif args.command == "fix-footprints":
        return run_fix_footprints_command(args)

    elif args.command == "fix-vias":
        from .commands import run_fix_vias_command

        return run_fix_vias_command(args)

    elif args.command == "fix-silkscreen":
        from .commands import run_fix_silkscreen_command

        return run_fix_silkscreen_command(args)

    elif args.command == "repair-clearance":
        from .commands import run_repair_clearance_command

        return run_repair_clearance_command(args)

    elif args.command == "fix-drc":
        from .commands import run_fix_drc_command

        return run_fix_drc_command(args)

    elif args.command == "fix-erc":
        from .commands import run_fix_erc_command

        return run_fix_erc_command(args)

    elif args.command == "config":
        return run_config_command(args)

    elif args.command == "interactive":
        return run_interactive_command(args)

    elif args.command == "validate":
        return run_validate_command(args)

    elif args.command == "analyze":
        return run_analyze_command(args)

    elif args.command == "constraints":
        return run_constraints_command(args)

    elif args.command == "estimate":
        return run_estimate_command(args)

    elif args.command == "audit":
        return run_audit_command(args)

    elif args.command == "suggest":
        return run_suggest_command(args)

    elif args.command == "net-status":
        from .net_status_cmd import main as net_status_cmd

        sub_argv = [args.pcb]
        if hasattr(args, "net_status_format") and args.net_status_format != "text":
            sub_argv.extend(["--format", args.net_status_format])
        if hasattr(args, "net_status_incomplete") and args.net_status_incomplete:
            sub_argv.append("--incomplete")
        if hasattr(args, "net_status_net") and args.net_status_net:
            sub_argv.extend(["--net", args.net_status_net])
        if hasattr(args, "net_status_by_class") and args.net_status_by_class:
            sub_argv.append("--by-class")
        if hasattr(args, "net_status_verbose") and args.net_status_verbose:
            sub_argv.append("--verbose")
        return net_status_cmd(sub_argv)

    elif args.command == "clean":
        return run_clean_command(args)

    elif args.command == "impedance":
        return run_impedance_command(args)

    elif args.command == "mcp":
        return run_mcp_command(args)

    elif args.command == "init":
        return run_init_command(args)

    elif args.command == "pipeline":
        return run_pipeline_command(args)

    elif args.command == "build":
        return run_build_command(args)

    elif args.command == "build-native":
        return run_build_native_command(args)

    elif args.command == "spec":
        return run_spec_command(args)

    elif args.command == "benchmark":
        return run_benchmark_command(args)

    elif args.command == "run":
        return run_run_command(args)

    elif args.command == "explain":
        return _run_explain_command(args)

    elif args.command == "detect-mistakes":
        return _run_detect_mistakes_command(args)

    elif args.command == "calibrate":
        return _run_calibrate_command(args)

    elif args.command == "screenshot":
        return _run_screenshot_command(args)

    elif args.command == "report":
        return _run_report_command(args)

    elif args.command == "export":
        return _run_export_command(args)

    return 0


def _run_explain_command(args) -> int:
    """Run the explain command."""
    from .explain_cmd import main as explain_cmd

    sub_argv = []

    # Add rule if provided
    if hasattr(args, "explain_rule") and args.explain_rule:
        sub_argv.append(args.explain_rule)

    # Handle flags
    if hasattr(args, "explain_list") and args.explain_list:
        sub_argv.append("--list")
    if hasattr(args, "explain_search") and args.explain_search:
        sub_argv.extend(["--search", args.explain_search])
    if hasattr(args, "explain_value") and args.explain_value is not None:
        sub_argv.extend(["--value", str(args.explain_value)])
    if hasattr(args, "explain_required") and args.explain_required is not None:
        sub_argv.extend(["--required", str(args.explain_required)])
    if hasattr(args, "explain_unit") and args.explain_unit != "mm":
        sub_argv.extend(["--unit", args.explain_unit])
    if hasattr(args, "explain_net1") and args.explain_net1:
        sub_argv.extend(["--net1", args.explain_net1])
    if hasattr(args, "explain_net2") and args.explain_net2:
        sub_argv.extend(["--net2", args.explain_net2])
    if hasattr(args, "explain_drc_report") and args.explain_drc_report:
        sub_argv.extend(["--drc-report", args.explain_drc_report])
    if hasattr(args, "explain_format") and args.explain_format != "text":
        sub_argv.extend(["--format", args.explain_format])
    if hasattr(args, "explain_net") and args.explain_net:
        sub_argv.extend(["--net", args.explain_net])
    if hasattr(args, "explain_interface") and args.explain_interface:
        sub_argv.extend(["--interface", args.explain_interface])

    return explain_cmd(sub_argv)


def _run_detect_mistakes_command(args) -> int:
    """Run the detect-mistakes command."""
    from .mistakes_cmd import main as mistakes_cmd

    sub_argv = []

    # Add PCB file if provided
    if hasattr(args, "mistakes_pcb") and args.mistakes_pcb:
        sub_argv.append(args.mistakes_pcb)

    # Handle flags
    if hasattr(args, "mistakes_list_categories") and args.mistakes_list_categories:
        sub_argv.append("--list-categories")
    if hasattr(args, "mistakes_category") and args.mistakes_category:
        sub_argv.extend(["--category", args.mistakes_category])
    if hasattr(args, "mistakes_severity") and args.mistakes_severity:
        sub_argv.extend(["--severity", args.mistakes_severity])
    if hasattr(args, "mistakes_format") and args.mistakes_format != "table":
        sub_argv.extend(["--format", args.mistakes_format])
    if hasattr(args, "mistakes_strict") and args.mistakes_strict:
        sub_argv.append("--strict")
    if hasattr(args, "mistakes_verbose") and args.mistakes_verbose:
        sub_argv.append("--verbose")

    return mistakes_cmd(sub_argv)


def _run_calibrate_command(args) -> int:
    """Run the calibrate command."""
    from .calibrate_cmd import main as calibrate_cmd

    sub_argv = []

    # Handle flags
    if hasattr(args, "calibrate_show") and args.calibrate_show:
        sub_argv.append("--show")
    if hasattr(args, "calibrate_show_gpu") and args.calibrate_show_gpu:
        sub_argv.append("--show-gpu")
    if hasattr(args, "calibrate_gpu") and args.calibrate_gpu:
        sub_argv.append("--gpu")
    if hasattr(args, "calibrate_all") and args.calibrate_all:
        sub_argv.append("--all")
    if hasattr(args, "calibrate_benchmark") and args.calibrate_benchmark:
        sub_argv.append("--benchmark")
    if hasattr(args, "calibrate_quick") and args.calibrate_quick:
        sub_argv.append("--quick")
    if hasattr(args, "calibrate_output") and args.calibrate_output:
        sub_argv.extend(["-o", args.calibrate_output])
    if hasattr(args, "calibrate_json") and args.calibrate_json:
        sub_argv.append("--json")
    if hasattr(args, "calibrate_verbose") and args.calibrate_verbose:
        sub_argv.append("--verbose")

    return calibrate_cmd(sub_argv)


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


def erc_explain_main() -> int:
    """Standalone entry point for kicad-erc-explain command."""
    from .erc_explain_cmd import main

    return main()


def drc_main() -> int:
    """Standalone entry point for kicad-drc command."""
    from .drc_cmd import main

    return main()


def bom_main() -> int:
    """Standalone entry point for kicad-bom command."""
    from .bom_cmd import main

    return main()


def drc_summary_main() -> int:
    """Standalone entry point for kicad-drc-summary command."""
    from .drc_summary import main

    return main()


def net_status_main() -> int:
    """Standalone entry point for kicad-net-status command."""
    from .net_status_cmd import main

    return main()


def _run_screenshot_command(args) -> int:
    """Run the screenshot command."""
    from .screenshot_cmd import main as screenshot_cmd

    sub_argv = [args.screenshot_input]

    if hasattr(args, "screenshot_output") and args.screenshot_output:
        sub_argv.extend(["-o", args.screenshot_output])
    if hasattr(args, "screenshot_layers") and args.screenshot_layers:
        sub_argv.extend(["--layers", args.screenshot_layers])
    if hasattr(args, "screenshot_max_size") and args.screenshot_max_size != 1568:
        sub_argv.extend(["--max-size", str(args.screenshot_max_size)])
    if hasattr(args, "screenshot_bw") and args.screenshot_bw:
        sub_argv.append("--bw")
    if hasattr(args, "screenshot_theme") and args.screenshot_theme:
        sub_argv.extend(["--theme", args.screenshot_theme])

    return screenshot_cmd(sub_argv)


def _run_report_command(args) -> int:
    """Run the report command."""
    from .report_cmd import main as report_cmd

    sub_argv: list[str] = []

    report_command = getattr(args, "report_command", None)
    if not report_command:
        return report_cmd(["--help"])

    sub_argv.append(report_command)

    if report_command == "generate":
        sub_argv.append(args.report_input)
        if hasattr(args, "report_mfr") and args.report_mfr:
            sub_argv.extend(["--mfr", args.report_mfr])
        if hasattr(args, "report_output") and args.report_output:
            sub_argv.extend(["-o", args.report_output])
        if hasattr(args, "report_data_dir") and args.report_data_dir:
            sub_argv.extend(["--data-dir", args.report_data_dir])
        if hasattr(args, "report_template") and args.report_template:
            sub_argv.extend(["--template", args.report_template])
        if hasattr(args, "report_sch") and args.report_sch:
            sub_argv.extend(["--sch", args.report_sch])
        if getattr(args, "report_no_figures", False):
            sub_argv.append("--no-figures")
        if hasattr(args, "report_quantity") and args.report_quantity is not None:
            sub_argv.extend(["--quantity", str(args.report_quantity)])
        if getattr(args, "report_skip_erc", False):
            sub_argv.append("--skip-erc")
        if getattr(args, "report_skip_collect", False):
            sub_argv.append("--skip-collect")

    return report_cmd(sub_argv)


def _run_export_command(args) -> int:
    """Run the export command."""
    from .export_cmd import main as export_cmd

    sub_argv = [args.export_pcb]

    if hasattr(args, "export_mfr") and args.export_mfr:
        sub_argv.extend(["--mfr", args.export_mfr])
    if hasattr(args, "export_output") and args.export_output:
        sub_argv.extend(["-o", args.export_output])
    if hasattr(args, "export_sch") and args.export_sch:
        sub_argv.extend(["--sch", args.export_sch])
    if getattr(args, "export_dry_run", False):
        sub_argv.append("--dry-run")
    if getattr(args, "export_no_report", False):
        sub_argv.append("--no-report")
    if getattr(args, "export_no_gerbers", False):
        sub_argv.append("--no-gerbers")
    if getattr(args, "export_no_bom", False):
        sub_argv.append("--no-bom")
    if getattr(args, "export_no_cpl", False):
        sub_argv.append("--no-cpl")
    if getattr(args, "export_no_project_zip", False):
        sub_argv.append("--no-project-zip")
    if getattr(args, "export_no_auto_lcsc", False):
        sub_argv.append("--no-auto-lcsc")
    if getattr(args, "export_skip_preflight", False):
        sub_argv.append("--skip-preflight")
    if getattr(args, "export_strict_preflight", False):
        sub_argv.append("--strict-preflight")
    if getattr(args, "export_skip_drc", False):
        sub_argv.append("--skip-drc")
    if getattr(args, "export_skip_erc", False):
        sub_argv.append("--skip-erc")
    if hasattr(args, "export_drc_report") and args.export_drc_report:
        sub_argv.extend(["--drc-report", args.export_drc_report])
    if hasattr(args, "export_erc_report") and args.export_erc_report:
        sub_argv.extend(["--erc-report", args.export_erc_report])
    if getattr(args, "export_include_tht", False):
        sub_argv.append("--include-tht")
    if hasattr(args, "export_format") and args.export_format != "text":
        sub_argv.extend(["--format", args.export_format])

    return export_cmd(sub_argv)


if __name__ == "__main__":
    sys.exit(main())
