"""Schematic (sch) subcommand handlers."""

import sys
from pathlib import Path

__all__ = ["run_sch_command"]


def run_sch_command(args) -> int:
    """Handle schematic subcommands."""
    if not args.sch_command:
        print("Usage: kicad-tools sch <command> [options] <file>")
        print("Commands: summary, hierarchy, labels, validate, preflight, wires, info, pins, pin-map,")
        print(
            "          connections, unconnected, set-footprint, set-value, replace,"
            " sync-hierarchy, rename-signal,"
        )
        print(
            "          set-label-direction, add-no-connect, add-component,"
            " add-bypass-cap, add-pull-resistor,"
        )
        print(
            "          add-wire, add-junction,"
            " add-label, cleanup-wires, remove-wire, disconnect, re-annotate"
        )
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

        sub_argv = [str(schematic_path), args.hierarchy_command]
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

    elif args.sch_command == "preflight":
        from ..sch_preflight import main as preflight_main

        sub_argv = [str(schematic_path)]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.strict:
            sub_argv.append("--strict")
        if args.quiet:
            sub_argv.append("--quiet")
        return preflight_main(sub_argv) or 0

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

    elif args.sch_command == "pin-map":
        from ..sch_pin_map import main as pin_map_main

        sub_argv = [str(schematic_path)]
        if args.ref:
            sub_argv.extend(["--ref", args.ref])
        if args.format != "json":
            sub_argv.extend(["--format", args.format])
        return pin_map_main(sub_argv) or 0

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

    elif args.sch_command == "set-footprint":
        from ..sch_set_footprint import run_set_footprint

        map_path = Path(args.map_file) if getattr(args, "map_file", None) else None
        return run_set_footprint(
            schematic_path=schematic_path,
            ref=getattr(args, "ref", None),
            footprint=getattr(args, "footprint", None),
            map_path=map_path,
            dry_run=getattr(args, "dry_run", False),
            backup=getattr(args, "backup", True),
        )

    elif args.sch_command == "set-value":
        from ..sch_set_value import run_set_value

        map_path = Path(args.map_file) if getattr(args, "map_file", None) else None
        return run_set_value(
            schematic_path=schematic_path,
            ref=getattr(args, "ref", None),
            value=getattr(args, "value", None),
            map_path=map_path,
            dry_run=getattr(args, "dry_run", False),
            backup=getattr(args, "backup", True),
        )

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

    elif args.sch_command == "set-label-direction":
        from ..sch_set_label_direction import main as set_label_dir_main

        sub_argv = [str(schematic_path), "--name", args.name, "--shape", args.shape]
        if args.sheet:
            sub_argv.extend(["--sheet", args.sheet])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.backup:
            sub_argv.append("--backup")
        return set_label_dir_main(sub_argv) or 0

    elif args.sch_command == "add-no-connect":
        from ..sch_add_no_connect import main as add_nc_main

        sub_argv = [str(schematic_path)]
        if args.ref:
            sub_argv.extend(["--ref", args.ref])
        if args.pin:
            sub_argv.extend(["--pin", args.pin])
        if args.auto:
            sub_argv.append("--auto")
        if args.lib_paths:
            for path in args.lib_paths:
                sub_argv.extend(["--lib-path", path])
        if args.libs:
            for lib in args.libs:
                sub_argv.extend(["--lib", lib])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.backup:
            sub_argv.append("--backup")
        return add_nc_main(sub_argv) or 0

    elif args.sch_command == "add-component":
        from ..sch_add_component import main as add_comp_main

        sub_argv = [str(schematic_path), "--lib-id", args.lib_id]
        if args.reference:
            sub_argv.extend(["--reference", args.reference])
        if args.value:
            sub_argv.extend(["--value", args.value])
        if args.footprint:
            sub_argv.extend(["--footprint", args.footprint])
        sub_argv.extend(["--at", str(args.at[0]), str(args.at[1])])
        if args.rotation:
            sub_argv.extend(["--rotation", str(args.rotation)])
        if args.mirror:
            sub_argv.extend(["--mirror", args.mirror])
        if args.connects:
            for conn in args.connects:
                sub_argv.extend(["--connect", conn])
        if args.lib_paths:
            for path in args.lib_paths:
                sub_argv.extend(["--lib-path", path])
        if args.libs:
            for lib in args.libs:
                sub_argv.extend(["--lib", lib])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.backup:
            sub_argv.append("--backup")
        return add_comp_main(sub_argv) or 0

    elif args.sch_command == "add-bypass-cap":
        from ..sch_add_bypass_cap import main as add_bypass_main

        sub_argv = [str(schematic_path), "--ref", args.ref, "--pin", args.pin]
        if args.value:
            sub_argv.extend(["--value", args.value])
        if args.ground_net:
            sub_argv.extend(["--ground-net", args.ground_net])
        if args.footprint:
            sub_argv.extend(["--footprint", args.footprint])
        if args.reference:
            sub_argv.extend(["--reference", args.reference])
        sub_argv.extend(["--offset", str(args.offset)])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.backup:
            sub_argv.append("--backup")
        return add_bypass_main(sub_argv) or 0

    elif args.sch_command == "add-pull-resistor":
        from ..sch_add_pull_resistor import main as add_pull_main

        sub_argv = [str(schematic_path), "--ref", args.ref, "--pin", args.pin]
        sub_argv.extend(["--direction", args.direction])
        sub_argv.extend(["--value", args.value])
        if args.power_net:
            sub_argv.extend(["--power-net", args.power_net])
        if args.reference:
            sub_argv.extend(["--reference", args.reference])
        if args.footprint:
            sub_argv.extend(["--footprint", args.footprint])
        if args.offset != 5.08:
            sub_argv.extend(["--offset", str(args.offset)])
        if args.lib_paths:
            for path in args.lib_paths:
                sub_argv.extend(["--lib-path", path])
        if args.libs:
            for lib in args.libs:
                sub_argv.extend(["--lib", lib])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.backup:
            sub_argv.append("--backup")
        if args.force:
            sub_argv.append("--force")
        return add_pull_main(sub_argv) or 0

    elif args.sch_command == "add-wire":
        from ..sch_add_wire import main as add_wire_main

        sub_argv = [str(schematic_path), "--from", str(args.start[0]), str(args.start[1])]
        if args.to:
            for to_pair in args.to:
                sub_argv.extend(["--to", str(to_pair[0]), str(to_pair[1])])
        if args.junction:
            sub_argv.append("--junction")
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.backup:
            sub_argv.append("--backup")
        return add_wire_main(sub_argv) or 0

    elif args.sch_command == "add-junction":
        from ..sch_add_junction import main as add_junc_main

        sub_argv = [str(schematic_path)]
        sub_argv.extend(["--at", str(args.at[0]), str(args.at[1])])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.backup:
            sub_argv.append("--backup")
        return add_junc_main(sub_argv) or 0

    elif args.sch_command == "add-label":
        from ..sch_add_label import main as add_label_main

        sub_argv = [str(schematic_path), "--type", args.type, "--name", args.name]
        sub_argv.extend(["--at", str(args.at[0]), str(args.at[1])])
        if args.shape:
            sub_argv.extend(["--shape", args.shape])
        if args.rotation:
            sub_argv.extend(["--rotation", str(args.rotation)])
        if args.connects:
            for conn in args.connects:
                sub_argv.extend(["--connect", conn])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.backup:
            sub_argv.append("--backup")
        return add_label_main(sub_argv) or 0

    elif args.sch_command == "cleanup-wires":
        from ..sch_cleanup_wires import main as cleanup_main

        sub_argv = [str(schematic_path)]
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.backup:
            sub_argv.append("--backup")
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return cleanup_main(sub_argv) or 0

    elif args.sch_command == "remove-wire":
        from ..sch_remove_wire import main as remove_wire_main

        sub_argv = [str(schematic_path)]
        if args.from_pt:
            sub_argv.extend(["--from", str(args.from_pt[0]), str(args.from_pt[1])])
        if args.to_pt:
            sub_argv.extend(["--to", str(args.to_pt[0]), str(args.to_pt[1])])
        if args.near:
            sub_argv.extend(["--near", str(args.near[0]), str(args.near[1])])
        if args.tolerance != 1.27:
            sub_argv.extend(["--tolerance", str(args.tolerance)])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.backup:
            sub_argv.append("--backup")
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return remove_wire_main(sub_argv) or 0

    elif args.sch_command == "disconnect":
        from ..sch_disconnect import main as disconnect_main

        sub_argv = [str(schematic_path), "--ref", args.ref, "--pin", args.pin]
        if args.lib_paths:
            for path in args.lib_paths:
                sub_argv.extend(["--lib-path", path])
        if args.libs:
            for lib in args.libs:
                sub_argv.extend(["--lib", lib])
        if args.add_nc:
            sub_argv.append("--add-nc")
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.backup:
            sub_argv.append("--backup")
        return disconnect_main(sub_argv) or 0

    elif args.sch_command == "re-annotate":
        from ..sch_re_annotate import run_re_annotate

        prefix_list = None
        if args.prefix:
            prefix_list = [p.strip() for p in args.prefix.split(",")]

        return run_re_annotate(
            schematic_path=schematic_path,
            dry_run=getattr(args, "dry_run", False),
            backup=getattr(args, "backup", False),
            prefixes=prefix_list,
            start_from=getattr(args, "start_from", 1),
            per_sheet=getattr(args, "per_sheet", False),
            format=getattr(args, "format", "text"),
            unannotated_only=getattr(args, "unannotated_only", False),
        )

    return 1
