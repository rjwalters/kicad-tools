"""
CLI commands for library management.

Provides unified commands for symbol and footprint library operations:
    kct lib symbols <library>           - List symbols in library
    kct lib footprints <library>        - List footprints in library
    kct lib list [--symbols|--footprints]  - List available libraries
    kct lib symbol-info <lib> <name>    - Show symbol details
    kct lib footprint-info <lib> <name> - Show footprint details
    kct lib create-symbol-lib <path>    - Create new symbol library (NYI)
    kct lib create-footprint-lib <path> - Create new footprint library (NYI)
    kct lib generate-footprint <lib> <type> [options]  - Generate parametric footprint (NYI)
    kct lib export <path> [--format]    - Export to JSON (NYI)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kicad_tools.footprints.library_path import (
    detect_kicad_library_path,
)
from kicad_tools.footprints.library_path import (
    list_available_libraries as list_footprint_libraries,
)
from kicad_tools.schema import SymbolLibrary
from kicad_tools.schematic.grid import KICAD_SYMBOL_PATHS
from kicad_tools.schematic.library import list_libraries as list_symbol_libraries


def list_kicad_libraries(
    library_type: str = "all",
    output_format: str = "table",
) -> int:
    """List available KiCad standard libraries.

    Args:
        library_type: "symbols", "footprints", or "all"
        output_format: "table" or "json"

    Returns:
        Exit code (0 for success)
    """
    result: dict[str, Any] = {}

    if library_type in ("symbols", "all"):
        symbol_libs = list_symbol_libraries(KICAD_SYMBOL_PATHS)
        result["symbols"] = {
            "count": len(symbol_libs),
            "libraries": symbol_libs,
        }

    if library_type in ("footprints", "all"):
        fp_paths = detect_kicad_library_path()
        footprint_libs = list_footprint_libraries(fp_paths) if fp_paths.found else []
        result["footprints"] = {
            "count": len(footprint_libs),
            "libraries": footprint_libs,
            "path": str(fp_paths.footprints_path) if fp_paths.footprints_path else None,
        }

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        _print_library_list(result, library_type)

    return 0


def _print_library_list(result: dict[str, Any], library_type: str) -> None:
    """Print library list in table format."""
    if "symbols" in result:
        symbols = result["symbols"]
        print(f"\nSymbol Libraries ({symbols['count']}):")
        print("=" * 60)
        if symbols["libraries"]:
            for lib in symbols["libraries"]:
                print(f"  {lib}")
        else:
            print("  No symbol libraries found.")
            print("  Check KICAD_SYMBOL_DIR environment variable or KiCad installation.")

    if "footprints" in result:
        footprints = result["footprints"]
        print(f"\nFootprint Libraries ({footprints['count']}):")
        print("=" * 60)
        if footprints["path"]:
            print(f"  Path: {footprints['path']}")
        if footprints["libraries"]:
            for lib in footprints["libraries"]:
                print(f"  {lib}")
        else:
            print("  No footprint libraries found.")
            print("  Check KICAD_FOOTPRINT_DIR environment variable or KiCad installation.")


def show_symbol_info(
    library: str,
    symbol_name: str,
    output_format: str = "text",
    show_pins: bool = False,
) -> int:
    """Show details of a specific symbol in a library.

    Args:
        library: Library name or path to .kicad_sym file
        symbol_name: Name of the symbol to show
        output_format: "text" or "json"
        show_pins: Whether to show pin details

    Returns:
        Exit code (0 for success)
    """
    # Try to find the library file
    lib_path = _find_symbol_library(library)
    if not lib_path:
        print(f"Error: Symbol library not found: {library}", file=sys.stderr)
        return 1

    try:
        lib = SymbolLibrary.load(str(lib_path))
    except Exception as e:
        print(f"Error loading library: {e}", file=sys.stderr)
        return 1

    symbol = lib.get_symbol(symbol_name)
    if not symbol:
        print(f"Error: Symbol '{symbol_name}' not found in {library}", file=sys.stderr)
        print(f"Available symbols: {', '.join(sorted(lib.symbols.keys())[:10])}...")
        return 1

    if output_format == "json":
        _print_symbol_json(symbol, lib_path, show_pins)
    else:
        _print_symbol_text(symbol, lib_path, show_pins)

    return 0


def _find_symbol_library(library: str) -> Path | None:
    """Find a symbol library by name or path."""
    # Check if it's a direct path
    path = Path(library)
    if path.exists() and path.suffix == ".kicad_sym":
        return path

    # Add extension if needed
    lib_name = library if library.endswith(".kicad_sym") else f"{library}.kicad_sym"

    # Search in KiCad paths
    for search_path in KICAD_SYMBOL_PATHS:
        candidate = search_path / lib_name
        if candidate.exists():
            return candidate

    return None


def _print_symbol_text(symbol, lib_path: Path, show_pins: bool) -> None:
    """Print symbol info in text format."""
    print(f"\nSymbol: {symbol.name}")
    print("=" * 60)
    print(f"Library: {lib_path}")

    if symbol.properties:
        print("\nProperties:")
        for key, value in sorted(symbol.properties.items()):
            if value:  # Only show non-empty properties
                print(f"  {key}: {value}")

    print(f"\nPin count: {symbol.pin_count}")

    if show_pins and symbol.pins:
        print("\nPins:")
        print(f"  {'#':<6} {'Name':<15} {'Type':<12} Position")
        print("  " + "-" * 55)
        for pin in sorted(
            symbol.pins,
            key=lambda p: (
                p.number.isdigit(),
                int(p.number) if p.number.isdigit() else 0,
                p.number,
            ),
        ):
            pos_str = f"({pin.position[0]:.2f}, {pin.position[1]:.2f})"
            print(f"  {pin.number:<6} {pin.name:<15} {pin.type:<12} {pos_str}")


def _print_symbol_json(symbol, lib_path: Path, show_pins: bool) -> None:
    """Print symbol info in JSON format."""
    data: dict[str, Any] = {
        "name": symbol.name,
        "library": str(lib_path),
        "properties": symbol.properties,
        "pin_count": symbol.pin_count,
    }

    if show_pins:
        data["pins"] = [
            {
                "number": p.number,
                "name": p.name,
                "type": p.type,
                "position": list(p.position),
                "rotation": p.rotation,
                "length": p.length,
            }
            for p in symbol.pins
        ]

    print(json.dumps(data, indent=2))


def show_footprint_info(
    library: str,
    footprint_name: str,
    output_format: str = "text",
    show_pads: bool = False,
) -> int:
    """Show details of a specific footprint in a library.

    Args:
        library: Library name (without .pretty) or path to .pretty directory
        footprint_name: Name of the footprint to show
        output_format: "text" or "json"
        show_pads: Whether to show pad details

    Returns:
        Exit code (0 for success)
    """
    # Find the footprint file
    fp_path = _find_footprint(library, footprint_name)
    if not fp_path:
        print(
            f"Error: Footprint '{footprint_name}' not found in {library}",
            file=sys.stderr,
        )
        return 1

    # Use existing show_footprint function
    from kicad_tools.cli.lib_footprints import show_footprint

    return show_footprint(fp_path, output_format, show_pads)


def _find_footprint(library: str, footprint_name: str) -> Path | None:
    """Find a footprint file by library and name."""
    # Ensure extensions
    lib_name = library if library.endswith(".pretty") else f"{library}.pretty"
    fp_name = (
        footprint_name if footprint_name.endswith(".kicad_mod") else f"{footprint_name}.kicad_mod"
    )

    # Check if library is a direct path
    lib_path = Path(library)
    if lib_path.is_dir():
        fp_path = lib_path / fp_name
        if fp_path.exists():
            return fp_path

    # Search in KiCad footprint paths
    kicad_paths = detect_kicad_library_path()
    if kicad_paths.footprints_path:
        fp_path = kicad_paths.footprints_path / lib_name / fp_name
        if fp_path.exists():
            return fp_path

    return None


def create_symbol_library(path: str) -> int:
    """Create a new empty symbol library.

    Note: This feature requires issue #85 (Add symbol library creation and save support).

    Args:
        path: Path for the new .kicad_sym file

    Returns:
        Exit code (0 for success)
    """
    print("Error: create-symbol-lib is not yet implemented.", file=sys.stderr)
    print("This feature requires: https://github.com/rjwalters/kicad-tools/issues/85")
    return 2  # Exit code 2 for "not implemented"


def create_footprint_library(path: str) -> int:
    """Create a new empty footprint library.

    Note: This feature requires issue #87 (Add FootprintLibrary class).

    Args:
        path: Path for the new .pretty directory

    Returns:
        Exit code (0 for success)
    """
    print("Error: create-footprint-lib is not yet implemented.", file=sys.stderr)
    print("This feature requires: https://github.com/rjwalters/kicad-tools/issues/87")
    return 2


def generate_footprint(
    library: str,
    footprint_type: str,
    **kwargs,
) -> int:
    """Generate a parametric footprint.

    Note: This feature requires issue #88 (Add footprint creation and save support).

    Args:
        library: Target library path
        footprint_type: Footprint type (soic, qfp, chip, etc.)
        **kwargs: Type-specific options (pins, pitch, body-width, etc.)

    Returns:
        Exit code (0 for success)
    """
    print("Error: generate-footprint is not yet implemented.", file=sys.stderr)
    print("This feature requires: https://github.com/rjwalters/kicad-tools/issues/88")
    return 2


def export_library(
    path: str,
    output_format: str = "json",
) -> int:
    """Export library or item to JSON.

    Args:
        path: Path to library file or item
        output_format: Output format (only json supported currently)

    Returns:
        Exit code (0 for success)
    """
    path_obj = Path(path)

    if not path_obj.exists():
        print(f"Error: Path not found: {path}", file=sys.stderr)
        return 1

    # Handle symbol libraries
    if path_obj.suffix == ".kicad_sym":
        try:
            lib = SymbolLibrary.load(str(path_obj))
            data = {
                "type": "symbol_library",
                "path": str(path_obj),
                "symbol_count": len(lib.symbols),
                "symbols": [
                    {
                        "name": name,
                        "properties": sym.properties,
                        "pin_count": sym.pin_count,
                    }
                    for name, sym in sorted(lib.symbols.items())
                ],
            }
            print(json.dumps(data, indent=2))
            return 0
        except Exception as e:
            print(f"Error exporting library: {e}", file=sys.stderr)
            return 1

    # Handle footprint libraries (.pretty directories)
    if path_obj.is_dir() and path_obj.suffix == ".pretty":
        from kicad_tools.cli.lib_footprints import list_footprints

        return list_footprints(path_obj, "json")

    # Handle individual footprint files
    if path_obj.suffix == ".kicad_mod":
        from kicad_tools.cli.lib_footprints import show_footprint

        return show_footprint(path_obj, "json", show_pads=True)

    print(f"Error: Unsupported file type: {path_obj.suffix}", file=sys.stderr)
    print("Supported: .kicad_sym, .pretty directories, .kicad_mod files")
    return 1


def main(argv: list[str] | None = None) -> int:
    """Main entry point for lib subcommand."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools lib",
        description="Symbol and footprint library tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="lib_command", help="Library commands")

    # lib list
    list_parser = subparsers.add_parser("list", help="List available KiCad libraries")
    list_parser.add_argument(
        "--symbols",
        action="store_true",
        help="List only symbol libraries",
    )
    list_parser.add_argument(
        "--footprints",
        action="store_true",
        help="List only footprint libraries",
    )
    list_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )

    # lib symbols (existing)
    symbols_parser = subparsers.add_parser("symbols", help="List symbols in a library")
    symbols_parser.add_argument("library", help="Path to .kicad_sym file")
    symbols_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format",
    )
    symbols_parser.add_argument("--pins", action="store_true", help="Show pin details")

    # lib footprints (existing)
    footprints_parser = subparsers.add_parser(
        "footprints",
        help="List footprints in a .pretty directory",
    )
    footprints_parser.add_argument("directory", help="Path to .pretty directory")
    footprints_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format",
    )

    # lib footprint (existing - show single footprint)
    footprint_parser = subparsers.add_parser(
        "footprint",
        help="Show details of a footprint file",
    )
    footprint_parser.add_argument("file", help="Path to .kicad_mod file")
    footprint_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format",
    )
    footprint_parser.add_argument("--pads", action="store_true", help="Show pad details")

    # lib symbol-info (new)
    symbol_info_parser = subparsers.add_parser(
        "symbol-info",
        help="Show details of a symbol by name",
    )
    symbol_info_parser.add_argument(
        "library",
        help="Library name or path to .kicad_sym file",
    )
    symbol_info_parser.add_argument("name", help="Symbol name")
    symbol_info_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format",
    )
    symbol_info_parser.add_argument("--pins", action="store_true", help="Show pin details")

    # lib footprint-info (new)
    footprint_info_parser = subparsers.add_parser(
        "footprint-info",
        help="Show details of a footprint by name",
    )
    footprint_info_parser.add_argument(
        "library",
        help="Library name or path to .pretty directory",
    )
    footprint_info_parser.add_argument("name", help="Footprint name")
    footprint_info_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format",
    )
    footprint_info_parser.add_argument("--pads", action="store_true", help="Show pad details")

    # lib create-symbol-lib (placeholder)
    create_sym_parser = subparsers.add_parser(
        "create-symbol-lib",
        help="Create new symbol library (not yet implemented)",
    )
    create_sym_parser.add_argument("path", help="Path for new .kicad_sym file")

    # lib create-footprint-lib (placeholder)
    create_fp_parser = subparsers.add_parser(
        "create-footprint-lib",
        help="Create new footprint library (not yet implemented)",
    )
    create_fp_parser.add_argument("path", help="Path for new .pretty directory")

    # lib generate-footprint (placeholder)
    generate_parser = subparsers.add_parser(
        "generate-footprint",
        help="Generate parametric footprint (not yet implemented)",
    )
    generate_parser.add_argument("library", help="Target library path")
    generate_parser.add_argument(
        "type",
        choices=["soic", "qfp", "qfn", "dfn", "chip", "sot"],
        help="Footprint type",
    )
    generate_parser.add_argument("--pins", type=int, help="Number of pins")
    generate_parser.add_argument("--pitch", type=float, help="Pin pitch in mm")
    generate_parser.add_argument("--body-width", type=float, help="Body width in mm")
    generate_parser.add_argument("--body-size", type=float, help="Body size (square) in mm")
    generate_parser.add_argument("--prefix", help="Footprint name prefix")

    # lib export
    export_parser = subparsers.add_parser("export", help="Export library to JSON")
    export_parser.add_argument(
        "path",
        help="Path to library file or item",
    )
    export_parser.add_argument(
        "--format",
        choices=["json"],
        default="json",
        help="Output format (default: json)",
    )

    args = parser.parse_args(argv)

    if not args.lib_command:
        parser.print_help()
        return 0

    # Dispatch to handlers
    if args.lib_command == "list":
        lib_type = "all"
        if args.symbols and not args.footprints:
            lib_type = "symbols"
        elif args.footprints and not args.symbols:
            lib_type = "footprints"
        return list_kicad_libraries(lib_type, args.format)

    elif args.lib_command == "symbols":
        from kicad_tools.cli.lib_list_symbols import main as symbols_main

        sub_argv = [args.library]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.pins:
            sub_argv.append("--pins")
        return symbols_main(sub_argv) or 0

    elif args.lib_command == "footprints":
        from kicad_tools.cli.lib_footprints import list_footprints

        return list_footprints(Path(args.directory), args.format)

    elif args.lib_command == "footprint":
        from kicad_tools.cli.lib_footprints import show_footprint

        return show_footprint(Path(args.file), args.format, args.pads)

    elif args.lib_command == "symbol-info":
        return show_symbol_info(
            args.library,
            args.name,
            args.format,
            args.pins,
        )

    elif args.lib_command == "footprint-info":
        return show_footprint_info(
            args.library,
            args.name,
            args.format,
            args.pads,
        )

    elif args.lib_command == "create-symbol-lib":
        return create_symbol_library(args.path)

    elif args.lib_command == "create-footprint-lib":
        return create_footprint_library(args.path)

    elif args.lib_command == "generate-footprint":
        return generate_footprint(
            args.library,
            args.type,
            pins=args.pins,
            pitch=args.pitch,
            body_width=args.body_width,
            body_size=args.body_size,
            prefix=args.prefix,
        )

    elif args.lib_command == "export":
        return export_library(args.path, args.format)

    return 0


if __name__ == "__main__":
    sys.exit(main())
