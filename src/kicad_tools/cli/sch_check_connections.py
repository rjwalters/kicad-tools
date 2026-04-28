#!/usr/bin/env python3
"""
Check pin connections using exact pin positions from symbol libraries.

Usage:
    kicad-tools sch connections <schematic.kicad_sch> [options]

Options:
    --lib-path <path>      Path to search for symbol libraries (optional, can be repeated)
    --format {table,json}  Output format (default: table)
    --filter <pattern>     Filter by symbol reference (e.g., "U*")
    --verbose              Show all pins, not just unconnected

Uses embedded lib_symbols from the schematic (no external library files needed).
External --lib-path/--lib flags are supported for backward compatibility.

Examples:
    # Check connections using embedded symbols (no --lib-path needed)
    kicad-tools sch connections clock.kicad_sch

    # Check only ICs
    kicad-tools sch connections clock.kicad_sch --filter "U*"

    # With explicit library path (backward compatible)
    kicad-tools sch connections clock.kicad_sch --lib-path lib/symbols/
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from kicad_tools.cli.sch_connectivity import (
    Coord,
    build_wire_graph,
    is_pin_connected,
    to_coord,
)
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import LibraryManager, Schematic


@dataclass
class PinStatus:
    """Status of a pin connection."""

    reference: str
    pin_number: str
    pin_name: str
    pin_type: str
    position: tuple[float, float]
    connected: bool
    connection_type: str = ""  # "wire", "label", "junction"
    sheet: str = ""  # sheet path for hierarchical schematics


def _resolve_lib_symbol(
    schematic: Schematic,
    lib_id: str,
    lib_manager: LibraryManager | None = None,
):
    """Resolve a library symbol, preferring embedded symbols.

    Tries embedded lib_symbols via ``schematic.get_lib_symbol_resolved()``
    first.  Falls back to *lib_manager* (if provided) for backward
    compatibility with explicit ``--lib-path`` / ``--lib`` usage.
    """
    # Primary path: embedded lib_symbols (works without external files)
    lib_sym = schematic.get_lib_symbol_resolved(lib_id)
    if lib_sym:
        return lib_sym

    # Fallback: external library manager
    if lib_manager is not None:
        lib_sym = lib_manager.get_symbol(lib_id)
        if not lib_sym and ":" in lib_id:
            sym_name = lib_id.split(":", 1)[1]
            lib_sym = lib_manager.get_symbol(sym_name)
        if lib_sym:
            return lib_sym

    return None


def check_symbol_connections(
    schematic: Schematic,
    lib_manager: LibraryManager | None = None,
    pattern: str | None = None,
    sheet_path: str = "",
) -> list[PinStatus]:
    """
    Check all symbol pins for connections.

    Uses the wire-graph BFS approach (same algorithm as ``sch_pin_map``)
    to correctly handle sub-grid pin positions.  Embedded lib_symbols from
    the schematic are used by default; an optional *lib_manager* is
    consulted as a fallback for backward compatibility.

    Returns list of PinStatus for all pins (or filtered pins).
    """
    results = []

    # First pass: collect all pin coordinates so the wire graph is split
    # at pin positions (required for BFS reachability).
    all_pin_coords: set[Coord] = set()
    symbol_data: list[tuple] = []  # (symbol, lib_sym, pin_positions)

    for symbol in schematic.symbols:
        if symbol.lib_id.startswith("power:"):
            continue

        if pattern and not fnmatch.fnmatch(symbol.reference, pattern):
            continue

        lib_sym = _resolve_lib_symbol(schematic, symbol.lib_id, lib_manager)

        if not lib_sym:
            results.append(
                PinStatus(
                    reference=symbol.reference,
                    pin_number="*",
                    pin_name="LIBRARY_NOT_FOUND",
                    pin_type=symbol.lib_id,
                    position=symbol.position,
                    connected=False,
                    sheet=sheet_path,
                )
            )
            continue

        pin_positions = lib_sym.get_all_pin_positions(
            instance_pos=symbol.position,
            instance_rot=symbol.rotation,
            mirror=symbol.mirror,
        )

        for pos in pin_positions.values():
            all_pin_coords.add(to_coord(*pos))

        symbol_data.append((symbol, lib_sym, pin_positions))

    # Build wire graph with pin coordinates as split points
    adjacency, _net_names = build_wire_graph(schematic, extra_points=all_pin_coords)

    # Second pass: check each pin for connectivity via the wire graph
    for symbol, lib_sym, pin_positions in symbol_data:
        for lib_pin in lib_sym.pins:
            if lib_pin.number not in pin_positions:
                continue

            pos = pin_positions[lib_pin.number]
            coord = to_coord(*pos)
            connected = is_pin_connected(coord, adjacency)

            results.append(
                PinStatus(
                    reference=symbol.reference,
                    pin_number=lib_pin.number,
                    pin_name=lib_pin.name,
                    pin_type=lib_pin.type,
                    position=pos,
                    connected=connected,
                    sheet=sheet_path,
                )
            )

    return results


def _collect_all_schematics(
    root_path: Path,
) -> list[tuple[Schematic, str]]:
    """Load the root schematic and all sub-sheets recursively.

    Returns a list of ``(schematic, sheet_path)`` pairs.  The root
    schematic has ``sheet_path=""``.
    """
    result: list[tuple[Schematic, str]] = []

    def _walk(sch_path: Path, path_prefix: str) -> None:
        try:
            sch = Schematic.load(str(sch_path))
        except (FileNotFoundError, KiCadFileNotFoundError, Exception):
            return
        result.append((sch, path_prefix))

        for sheet in sch.sheets:
            child_path = sch_path.parent / sheet.filename
            if child_path.exists():
                child_prefix = f"{path_prefix}/{sheet.name}" if path_prefix else sheet.name
                _walk(child_path, child_prefix)

    _walk(root_path, "")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check pin connections using library pin positions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--lib-path",
        action="append",
        dest="lib_paths",
        help="Path to search for symbol libraries (optional)",
    )
    parser.add_argument(
        "--lib",
        action="append",
        dest="libs",
        help="Specific library file to load (optional)",
    )
    parser.add_argument(
        "--format", choices=["table", "json"], default="table", help="Output format"
    )
    parser.add_argument("--filter", dest="pattern", help="Filter by symbol reference pattern")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show all pins, not just unconnected"
    )

    args = parser.parse_args(argv)

    # Load root schematic
    sch_path = Path(args.schematic)
    try:
        root_sch = Schematic.load(str(sch_path))
    except (FileNotFoundError, KiCadFileNotFoundError):
        print(f"Error: Schematic not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)

    # Set up optional library manager for backward compatibility
    lib_manager: LibraryManager | None = None
    if args.lib_paths or args.libs:
        lib_manager = LibraryManager()

        if args.lib_paths:
            for path in args.lib_paths:
                lib_manager.add_search_path(path)
                for lib_file in Path(path).glob("*.kicad_sym"):
                    try:
                        lib_manager.load_library(str(lib_file))
                    except Exception as e:
                        print(f"Warning: Could not load {lib_file}: {e}", file=sys.stderr)

        if args.libs:
            for lib_path in args.libs:
                try:
                    lib_manager.load_library(lib_path)
                except Exception as e:
                    print(f"Error loading library {lib_path}: {e}", file=sys.stderr)

        # Also load embedded symbols into the library manager
        lib_manager.load_embedded(root_sch)

    # Collect all schematics (root + sub-sheets)
    all_schematics = _collect_all_schematics(sch_path)

    # Check connections across all sheets
    results: list[PinStatus] = []
    for sch, sheet_path in all_schematics:
        results.extend(
            check_symbol_connections(sch, lib_manager, args.pattern, sheet_path)
        )

    # Filter if not verbose
    if not args.verbose:
        results = [r for r in results if not r.connected]

    if args.format == "json":
        output_json(results, args.verbose)
    else:
        output_table(results, args.verbose)


def output_table(results: list[PinStatus], show_all: bool):
    """Output as formatted table."""
    if not results:
        if show_all:
            print("No pins found to check.")
        else:
            print("All pins connected!")
        return

    # Group by symbol
    by_symbol: dict[str, list[PinStatus]] = {}
    for pin in results:
        key = f"{pin.sheet}/{pin.reference}" if pin.sheet else pin.reference
        if key not in by_symbol:
            by_symbol[key] = []
        by_symbol[key].append(pin)

    title = "All Pin Status" if show_all else "Unconnected Pins"
    print(title)
    print("=" * 70)

    for ref in sorted(by_symbol.keys()):
        pins = by_symbol[ref]
        print(f"\n{ref}:")
        print(f"  {'Pin':<5}  {'Name':<15}  {'Type':<12}  {'Position':<20}  {'Status'}")
        print("  " + "-" * 65)

        for pin in sorted(
            pins,
            key=lambda p: (
                not p.pin_number.isdigit(),
                int(p.pin_number) if p.pin_number.isdigit() else 0,
            ),
        ):
            pos_str = f"({pin.position[0]:.1f}, {pin.position[1]:.1f})"
            status = "connected" if pin.connected else "UNCONNECTED"
            print(
                f"  {pin.pin_number:<5}  {pin.pin_name:<15}  {pin.pin_type:<12}  {pos_str:<20}  {status}"
            )

    # Summary
    total = len(results)
    connected = sum(1 for r in results if r.connected)
    unconnected = total - connected

    print(f"\nSummary: {len(by_symbol)} symbols, {total} pins checked")
    if show_all:
        print(f"  Connected: {connected}")
        print(f"  Unconnected: {unconnected}")
    else:
        print(f"  Unconnected: {unconnected}")


def output_json(results: list[PinStatus], show_all: bool):
    """Output as JSON."""
    data = {
        "pins": [
            {
                "reference": p.reference,
                "pin_number": p.pin_number,
                "pin_name": p.pin_name,
                "pin_type": p.pin_type,
                "position": list(p.position),
                "connected": p.connected,
                **({"sheet": p.sheet} if p.sheet else {}),
            }
            for p in results
        ],
        "summary": {
            "total_pins": len(results),
            "connected": sum(1 for r in results if r.connected),
            "unconnected": sum(1 for r in results if not r.connected),
            "symbols_checked": len({p.reference for p in results}),
        },
    }
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
