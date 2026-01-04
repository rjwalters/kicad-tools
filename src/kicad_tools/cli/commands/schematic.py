"""Schematic (sch) subcommand handlers."""

import sys
from pathlib import Path

__all__ = ["run_sch_command"]


def run_sch_command(args) -> int:
    """Handle schematic subcommands."""
    if not args.sch_command:
        print("Usage: kicad-tools sch <command> [options] <file>")
        print("Commands: summary, hierarchy, labels, validate, wires, info, pins,")
        print("          connections, unconnected, replace, sync-hierarchy, rename-signal")
        return 1

    schematic_path = Path(args.schematic)
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    if args.sch_command == "summary":
        from ..sch_summary import run_summary

        return run_summary(schematic_path, args.format, args.verbose)

    elif args.sch_command == "hierarchy":
        from ..sch_hierarchy import main as hierarchy_main

        sub_argv = [str(schematic_path), "tree"]
        if args.format != "tree":
            sub_argv.extend(["--format", args.format])
        if args.depth:
            sub_argv.extend(["--depth", str(args.depth)])
        return hierarchy_main(sub_argv) or 0

    elif args.sch_command == "labels":
        from ..sch_list_labels import main as labels_main

        sub_argv = [str(schematic_path)]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.type != "all":
            sub_argv.extend(["--type", args.type])
        if args.pattern:
            sub_argv.extend(["--filter", args.pattern])
        return labels_main(sub_argv) or 0

    elif args.sch_command == "validate":
        from ..sch_validate import main as validate_main

        sub_argv = [str(schematic_path)]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.strict:
            sub_argv.append("--strict")
        if args.quiet:
            sub_argv.append("--quiet")
        return validate_main(sub_argv) or 0

    elif args.sch_command == "wires":
        from ..sch_list_wires import main as wires_main

        sub_argv = [str(schematic_path)]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.stats:
            sub_argv.append("--stats")
        if args.junctions:
            sub_argv.append("--junctions")
        return wires_main(sub_argv) or 0

    elif args.sch_command == "info":
        from ..sch_symbol_info import main as info_main

        sub_argv = [str(schematic_path), args.reference]
        if args.format == "json":
            sub_argv.append("--json")
        if args.show_pins:
            sub_argv.append("--show-pins")
        if args.show_properties:
            sub_argv.append("--show-properties")
        return info_main(sub_argv) or 0

    elif args.sch_command == "pins":
        from ..sch_pin_positions import main as pins_main

        sub_argv = [str(schematic_path), args.reference, "--lib", args.lib]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        return pins_main(sub_argv) or 0

    elif args.sch_command == "connections":
        from ..sch_check_connections import main as connections_main

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
        from ..sch_find_unconnected import main as unconnected_main

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
        from ..sch_replace_symbol import main as replace_main

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

    elif args.sch_command == "sync-hierarchy":
        from ..sch_sync_hierarchy import main as sync_main

        sub_argv = [str(schematic_path)]
        if args.add_labels:
            sub_argv.append("--add-labels")
        if args.remove_orphan_pins:
            sub_argv.append("--remove-orphan-pins")
        if args.interactive:
            sub_argv.append("--interactive")
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.sheet:
            sub_argv.extend(["--sheet", args.sheet])
        return sync_main(sub_argv) or 0

    elif args.sch_command == "rename-signal":
        from ..sch_rename_signal import main as rename_signal_main

        sub_argv = [str(schematic_path), "--from", args.old_name, "--to", args.new_name]
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.yes:
            sub_argv.append("--yes")
        if args.include_nets:
            sub_argv.append("--include-nets")
        if args.include_globals:
            sub_argv.append("--include-globals")
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return rename_signal_main(sub_argv) or 0

    return 1
