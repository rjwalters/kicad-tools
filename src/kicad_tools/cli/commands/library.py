"""Library (lib) subcommand handlers."""

import sys
from pathlib import Path

__all__ = ["run_lib_command"]


def run_lib_command(args) -> int:
    """Handle library subcommands."""
    if not args.lib_command:
        print("Usage: kicad-tools lib <command> [options] <file>")
        print("Commands: list, symbols, footprints, footprint, symbol-info, footprint-info,")
        print("          create-symbol-lib, create-footprint-lib, generate-footprint, export")
        return 1

    # Use the new unified lib module for new commands
    from ..lib import (
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

        from ..lib_list_symbols import main as lib_main

        sub_argv = [str(library_path)]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.pins:
            sub_argv.append("--pins")
        return lib_main(sub_argv) or 0

    elif args.lib_command == "footprints":
        from ..lib_footprints import list_footprints

        directory_path = Path(args.directory)
        if not directory_path.exists():
            print(f"Error: Directory not found: {directory_path}", file=sys.stderr)
            return 1
        return list_footprints(directory_path, args.format)

    elif args.lib_command == "footprint":
        from ..lib_footprints import show_footprint

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
