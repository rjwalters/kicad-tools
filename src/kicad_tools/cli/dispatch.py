"""
Command dispatch logic for kicad-tools CLI.

This module contains the main dispatch function and all command handlers.
"""

from __future__ import annotations

import sys
from pathlib import Path


def dispatch_command(args) -> int:
    """Dispatch to the appropriate command handler."""
    if args.command == "symbols":
        from .symbols import main as symbols_cmd

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

    elif args.command == "check":
        return _run_check_command(args)

    elif args.command == "sch":
        return _run_sch_command(args)

    elif args.command == "pcb":
        return _run_pcb_command(args)

    elif args.command == "lib":
        return _run_lib_command(args)

    elif args.command == "footprint":
        return _run_footprint_command(args)

    elif args.command == "mfr":
        return _run_mfr_command(args)

    elif args.command == "parts":
        return _run_parts_command(args)

    elif args.command == "datasheet":
        return _run_datasheet_command(args)

    elif args.command == "zones":
        return _run_zones_command(args)

    elif args.command == "route":
        return _run_route_command(args)

    elif args.command == "reason":
        return _run_reason_command(args)

    elif args.command == "placement":
        return _run_placement_command(args)

    elif args.command == "optimize-traces":
        return _run_optimize_command(args)

    elif args.command == "validate-footprints":
        return _run_validate_footprints_command(args)

    elif args.command == "fix-footprints":
        return _run_fix_footprints_command(args)

    elif args.command == "config":
        return _run_config_command(args)

    elif args.command == "interactive":
        return _run_interactive_command(args)

    elif args.command == "validate":
        return _run_validate_command(args)

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
    if getattr(args, "compare_standard", False):
        sub_argv.append("--compare-standard")
    if getattr(args, "tolerance", 0.05) != 0.05:
        sub_argv.extend(["--tolerance", str(args.tolerance)])
    if getattr(args, "kicad_library_path", None):
        sub_argv.extend(["--kicad-library-path", args.kicad_library_path])
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")
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
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")
    return main_fix(sub_argv)


def _run_check_command(args) -> int:
    """Handle check command (pure Python DRC)."""
    from .check_cmd import main as check_main

    sub_argv = [args.pcb]
    if args.format != "table":
        sub_argv.extend(["--format", args.format])
    if args.errors_only:
        sub_argv.append("--errors-only")
    if args.strict:
        sub_argv.append("--strict")
    if args.mfr != "jlcpcb":
        sub_argv.extend(["--mfr", args.mfr])
    if args.layers != 2:
        sub_argv.extend(["--layers", str(args.layers)])
    if args.copper != 1.0:
        sub_argv.extend(["--copper", str(args.copper)])
    if args.only_checks:
        sub_argv.extend(["--only", args.only_checks])
    if args.skip_checks:
        sub_argv.extend(["--skip", args.skip_checks])
    if args.verbose:
        sub_argv.append("--verbose")
    return check_main(sub_argv)


def _run_sch_command(args) -> int:
    """Handle schematic subcommands."""
    if not args.sch_command:
        print("Usage: kicad-tools sch <command> [options] <file>")
        print("Commands: summary, hierarchy, labels, validate, wires, info, pins,")
        print("          connections, unconnected, replace")
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

    elif args.sch_command == "wires":
        from .sch_list_wires import main as wires_main

        sub_argv = [str(schematic_path)]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.stats:
            sub_argv.append("--stats")
        if args.junctions:
            sub_argv.append("--junctions")
        return wires_main(sub_argv) or 0

    elif args.sch_command == "info":
        from .sch_symbol_info import main as info_main

        sub_argv = [str(schematic_path), args.reference]
        if args.format == "json":
            sub_argv.append("--json")
        if args.show_pins:
            sub_argv.append("--show-pins")
        if args.show_properties:
            sub_argv.append("--show-properties")
        return info_main(sub_argv) or 0

    elif args.sch_command == "pins":
        from .sch_pin_positions import main as pins_main

        sub_argv = [str(schematic_path), args.reference, "--lib", args.lib]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        return pins_main(sub_argv) or 0

    elif args.sch_command == "connections":
        from .sch_check_connections import main as connections_main

        sub_argv = [str(schematic_path)]
        if args.lib_paths:
            for path in args.lib_paths:
                sub_argv.extend(["--lib-path", path])
        if args.libs:
            for lib in args.libs:
                sub_argv.extend(["--lib", lib])
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.pattern:
            sub_argv.extend(["--filter", args.pattern])
        if args.verbose:
            sub_argv.append("--verbose")
        return connections_main(sub_argv) or 0

    elif args.sch_command == "unconnected":
        from .sch_find_unconnected import main as unconnected_main

        sub_argv = [str(schematic_path)]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.pattern:
            sub_argv.extend(["--filter", args.pattern])
        if args.include_power:
            sub_argv.append("--include-power")
        if args.include_dnp:
            sub_argv.append("--include-dnp")
        return unconnected_main(sub_argv) or 0

    elif args.sch_command == "replace":
        from .sch_replace_symbol import main as replace_main

        sub_argv = [str(schematic_path), args.reference, args.new_lib_id]
        if args.value:
            sub_argv.extend(["--value", args.value])
        if args.footprint:
            sub_argv.extend(["--footprint", args.footprint])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.backup:
            sub_argv.append("--backup")
        return replace_main(sub_argv) or 0

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
        print("Commands: list, symbols, footprints, footprint, symbol-info, footprint-info,")
        print("          create-symbol-lib, create-footprint-lib, generate-footprint, export")
        return 1

    from .lib import (
        create_footprint_library,
        create_symbol_library,
        export_library,
        generate_footprint,
        list_kicad_libraries,
        show_footprint_info,
        show_symbol_info,
    )

    if args.lib_command == "list":
        lib_type = "all"
        if getattr(args, "symbols", False) and not getattr(args, "footprints", False):
            lib_type = "symbols"
        elif getattr(args, "footprints", False) and not getattr(args, "symbols", False):
            lib_type = "footprints"
        return list_kicad_libraries(lib_type, args.format)

    elif args.lib_command == "symbols":
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

    elif args.lib_command == "footprints":
        from .lib_footprints import list_footprints

        directory_path = Path(args.directory)
        if not directory_path.exists():
            print(f"Error: Directory not found: {directory_path}", file=sys.stderr)
            return 1
        return list_footprints(directory_path, args.format)

    elif args.lib_command == "footprint":
        from .lib_footprints import show_footprint

        file_path = Path(args.file)
        if not file_path.exists():
            print(f"Error: File not found: {file_path}", file=sys.stderr)
            return 1
        return show_footprint(file_path, args.format, getattr(args, "pads", False))

    elif args.lib_command == "symbol-info":
        return show_symbol_info(
            args.library,
            args.name,
            args.format,
            getattr(args, "pins", False),
        )

    elif args.lib_command == "footprint-info":
        return show_footprint_info(
            args.library,
            args.name,
            args.format,
            getattr(args, "pads", False),
        )

    elif args.lib_command == "create-symbol-lib":
        return create_symbol_library(args.path)

    elif args.lib_command == "create-footprint-lib":
        return create_footprint_library(args.path)

    elif args.lib_command == "generate-footprint":
        return generate_footprint(
            args.library,
            args.type,
            pins=getattr(args, "pins", None),
            pitch=getattr(args, "pitch", None),
            body_width=getattr(args, "body_width", None),
            body_size=getattr(args, "body_size", None),
            prefix=getattr(args, "prefix", None),
        )

    elif args.lib_command == "export":
        return export_library(args.path, args.format)

    return 1


def _run_footprint_command(args) -> int:
    """Handle footprint subcommands."""
    if not args.footprint_command:
        print("Usage: kicad-tools footprint <command> [options]")
        print("Commands: generate")
        return 1

    if args.footprint_command == "generate":
        from .footprint_generate import main as generate_main

        sub_argv = args.fp_args if hasattr(args, "fp_args") and args.fp_args else []
        return generate_main(sub_argv) or 0

    return 1


def _run_mfr_command(args) -> int:
    """Handle manufacturer subcommands."""
    if not args.mfr_command:
        print("Usage: kicad-tools mfr <command> [options]")
        print("Commands: list, info, rules, compare, apply-rules, validate, export-dru, import-dru")
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

    elif args.mfr_command == "export-dru":
        sub_argv = ["export-dru", args.manufacturer]
        if args.layers != 4:
            sub_argv.extend(["--layers", str(args.layers)])
        if args.copper != 1.0:
            sub_argv.extend(["--copper", str(args.copper)])
        if args.output:
            sub_argv.extend(["--output", args.output])
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "import-dru":
        from .mfr_dru import import_dru

        file_path = Path(args.file)
        if not file_path.exists():
            print(f"Error: File not found: {file_path}", file=sys.stderr)
            return 1
        return import_dru(file_path, args.format)

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


def _run_datasheet_command(args) -> int:
    """Handle datasheet subcommands."""
    if not args.datasheet_command:
        print("Usage: kicad-tools datasheet <command> [options]")
        print(
            "Commands: search, download, list, cache, convert, extract-images, extract-tables, info"
        )
        return 1

    from .datasheet_cmd import main as datasheet_main

    if args.datasheet_command == "search":
        sub_argv = ["search", args.part]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.limit != 10:
            sub_argv.extend(["--limit", str(args.limit)])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "download":
        sub_argv = ["download", args.part]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.force:
            sub_argv.append("--force")
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "list":
        sub_argv = ["list"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "cache":
        sub_argv = ["cache", args.cache_action]
        if getattr(args, "older_than", None):
            sub_argv.extend(["--older-than", str(args.older_than)])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "convert":
        sub_argv = ["convert", args.pdf]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.pages:
            sub_argv.extend(["--pages", args.pages])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "extract-images":
        sub_argv = ["extract-images", args.pdf, "-o", args.output]
        if args.pages:
            sub_argv.extend(["--pages", args.pages])
        if args.min_size != 100:
            sub_argv.extend(["--min-size", str(args.min_size)])
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "extract-tables":
        sub_argv = ["extract-tables", args.pdf]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.pages:
            sub_argv.extend(["--pages", args.pages])
        if args.format != "markdown":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "info":
        sub_argv = ["info", args.pdf]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    return 1


def _run_zones_command(args) -> int:
    """Handle zones command."""
    if not args.zones_command:
        print("Usage: kicad-tools zones <command> [options] <file>")
        print("Commands: add, list, batch")
        return 1

    from .zones_cmd import main as zones_main

    if args.zones_command == "add":
        sub_argv = ["add", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        sub_argv.extend(["--net", args.net])
        sub_argv.extend(["--layer", args.layer])
        if args.priority != 0:
            sub_argv.extend(["--priority", str(args.priority)])
        if args.clearance != 0.3:
            sub_argv.extend(["--clearance", str(args.clearance)])
        if getattr(args, "thermal_gap", 0.3) != 0.3:
            sub_argv.extend(["--thermal-gap", str(args.thermal_gap)])
        if getattr(args, "thermal_bridge", 0.4) != 0.4:
            sub_argv.extend(["--thermal-bridge", str(args.thermal_bridge)])
        if getattr(args, "min_thickness", 0.25) != 0.25:
            sub_argv.extend(["--min-thickness", str(args.min_thickness)])
        if args.verbose:
            sub_argv.append("--verbose")
        if args.dry_run:
            sub_argv.append("--dry-run")
        if getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return zones_main(sub_argv) or 0

    elif args.zones_command == "list":
        sub_argv = ["list", args.pcb]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return zones_main(sub_argv) or 0

    elif args.zones_command == "batch":
        sub_argv = ["batch", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        sub_argv.extend(["--power-nets", args.power_nets])
        if args.clearance != 0.3:
            sub_argv.extend(["--clearance", str(args.clearance)])
        if args.verbose:
            sub_argv.append("--verbose")
        if args.dry_run:
            sub_argv.append("--dry-run")
        if getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return zones_main(sub_argv) or 0

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
    if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")
    if getattr(args, "power_nets", None):
        sub_argv.extend(["--power-nets", args.power_nets])
    return route_main(sub_argv)


def _run_reason_command(args) -> int:
    """Handle reason command."""
    from .reason_cmd import main as reason_main

    sub_argv = [args.pcb]
    if args.output:
        sub_argv.extend(["-o", args.output])
    if args.export_state:
        sub_argv.append("--export-state")
    if args.state_output:
        sub_argv.extend(["--state-output", args.state_output])
    if args.interactive:
        sub_argv.append("--interactive")
    if args.analyze:
        sub_argv.append("--analyze")
    if args.auto_route:
        sub_argv.append("--auto-route")
    if args.max_nets != 10:
        sub_argv.extend(["--max-nets", str(args.max_nets)])
    if args.drc:
        sub_argv.extend(["--drc", args.drc])
    if args.verbose:
        sub_argv.append("--verbose")
    if args.dry_run:
        sub_argv.append("--dry-run")
    return reason_main(sub_argv)


def _run_placement_command(args) -> int:
    """Handle placement command."""
    if not args.placement_command:
        print("Usage: kicad-tools placement <command> [options] <file>")
        print("Commands: check, fix, optimize")
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
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
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
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "optimize":
        sub_argv = ["optimize", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.strategy != "force-directed":
            sub_argv.extend(["--strategy", args.strategy])
        if args.iterations != 1000:
            sub_argv.extend(["--iterations", str(args.iterations)])
        if args.generations != 100:
            sub_argv.extend(["--generations", str(args.generations)])
        if args.population != 50:
            sub_argv.extend(["--population", str(args.population)])
        if args.grid != 0.0:
            sub_argv.extend(["--grid", str(args.grid)])
        if args.fixed:
            sub_argv.extend(["--fixed", args.fixed])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.verbose:
            sub_argv.append("--verbose")
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
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
    if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")
    return optimize_main(sub_argv)


def _run_config_command(args) -> int:
    """Handle config command."""
    from .config_cmd import main as config_main

    sub_argv = []
    if args.show:
        sub_argv.append("--show")
    if args.init:
        sub_argv.append("--init")
    if args.paths:
        sub_argv.append("--paths")
    if args.user:
        sub_argv.append("--user")
    if args.config_action:
        sub_argv.append(args.config_action)
    if args.config_key:
        sub_argv.append(args.config_key)
    if args.config_value:
        sub_argv.append(args.config_value)
    return config_main(sub_argv) or 0


def _run_interactive_command(args) -> int:
    """Handle interactive command."""
    from .interactive import main as interactive_main

    sub_argv = []
    if args.project:
        sub_argv.extend(["--project", args.project])
    return interactive_main(sub_argv)


def _run_validate_command(args) -> int:
    """Handle validate command."""
    if not args.sync:
        print("Usage: kicad-tools validate --sync [options] <project>")
        print("Currently only --sync is supported.")
        return 1

    from .validate_sync_cmd import main as validate_sync_main

    sub_argv = []
    if args.validate_project:
        sub_argv.append(args.validate_project)
    if args.validate_schematic:
        sub_argv.extend(["--schematic", args.validate_schematic])
    if args.validate_pcb:
        sub_argv.extend(["--pcb", args.validate_pcb])
    if args.validate_format != "table":
        sub_argv.extend(["--format", args.validate_format])
    if args.validate_errors_only:
        sub_argv.append("--errors-only")
    if args.validate_strict:
        sub_argv.append("--strict")
    if args.validate_verbose:
        sub_argv.append("--verbose")
    return validate_sync_main(sub_argv)
