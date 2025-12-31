#!/usr/bin/env python3
"""
Check pin connections using exact pin positions from symbol libraries.

Usage:
    python3 sch-check-connections.py <schematic.kicad_sch> --lib-path <path> [options]

Options:
    --lib-path <path>      Path to search for symbol libraries (can be repeated)
    --format {table,json}  Output format (default: table)
    --filter <pattern>     Filter by symbol reference (e.g., "U*")
    --verbose              Show all pins, not just unconnected

Examples:
    # Check connections with library path
    python3 sch-check-connections.py clock.kicad_sch --lib-path lib/symbols/

    # Check only ICs
    python3 sch-check-connections.py clock.kicad_sch --lib-path lib/ --filter "U*"
"""

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from kicad_tools.schema import LibraryManager, Schematic

POINT_TOLERANCE = 1.27  # mm - standard KiCad grid


@dataclass
class PinStatus:
    """Status of a pin connection."""

    reference: str
    pin_number: str
    pin_name: str
    pin_type: str
    position: Tuple[float, float]
    connected: bool
    connection_type: str = ""  # "wire", "label", "junction"


def find_all_connection_points(schematic: Schematic) -> Set[Tuple[int, int]]:
    """
    Get all points where connections exist.

    Returns set of (x*10, y*10) integer tuples for fast lookup.
    """
    points = set()

    # Wire endpoints
    for wire in schematic.wires:
        points.add((int(wire.start[0] * 10), int(wire.start[1] * 10)))
        points.add((int(wire.end[0] * 10), int(wire.end[1] * 10)))

    # Junctions
    for junc in schematic.junctions:
        points.add((int(junc.position[0] * 10), int(junc.position[1] * 10)))

    # Labels
    for lbl in schematic.labels:
        points.add((int(lbl.position[0] * 10), int(lbl.position[1] * 10)))

    for lbl in schematic.global_labels:
        points.add((int(lbl.position[0] * 10), int(lbl.position[1] * 10)))

    for lbl in schematic.hierarchical_labels:
        points.add((int(lbl.position[0] * 10), int(lbl.position[1] * 10)))

    return points


def point_is_connected(
    point: Tuple[float, float],
    connection_points: Set[Tuple[int, int]],
    tolerance: float = POINT_TOLERANCE,
) -> bool:
    """Check if a point is connected to anything."""
    # Check exact match first
    key = (int(point[0] * 10), int(point[1] * 10))
    if key in connection_points:
        return True

    # Check with tolerance (check grid-snapped neighbors)
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            if (key[0] + dx * 10, key[1] + dy * 10) in connection_points:
                return True

    return False


def check_symbol_connections(
    schematic: Schematic,
    lib_manager: LibraryManager,
    pattern: Optional[str] = None,
) -> List[PinStatus]:
    """
    Check all symbol pins for connections.

    Returns list of PinStatus for all pins (or filtered pins).
    """
    connection_points = find_all_connection_points(schematic)
    results = []

    for symbol in schematic.symbols:
        # Skip power symbols
        if symbol.lib_id.startswith("power:"):
            continue

        # Apply filter
        if pattern and not fnmatch.fnmatch(symbol.reference, pattern):
            continue

        # Get library symbol
        lib_sym = lib_manager.get_symbol(symbol.lib_id)
        if not lib_sym:
            # Try without library prefix
            if ":" in symbol.lib_id:
                sym_name = symbol.lib_id.split(":", 1)[1]
                lib_sym = lib_manager.get_symbol(sym_name)

        if not lib_sym:
            # Can't check - library not found
            # Add a result indicating library not found
            results.append(
                PinStatus(
                    reference=symbol.reference,
                    pin_number="*",
                    pin_name="LIBRARY_NOT_FOUND",
                    pin_type=symbol.lib_id,
                    position=symbol.position,
                    connected=False,
                )
            )
            continue

        # Get pin positions
        pin_positions = lib_sym.get_all_pin_positions(
            instance_pos=symbol.position,
            instance_rot=symbol.rotation,
            mirror=symbol.mirror,
        )

        # Check each pin
        for lib_pin in lib_sym.pins:
            if lib_pin.number not in pin_positions:
                continue

            pos = pin_positions[lib_pin.number]
            connected = point_is_connected(pos, connection_points)

            results.append(
                PinStatus(
                    reference=symbol.reference,
                    pin_number=lib_pin.number,
                    pin_name=lib_pin.name,
                    pin_type=lib_pin.type,
                    position=pos,
                    connected=connected,
                )
            )

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Check pin connections using library pin positions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Path to search for symbol libraries"
    )
    parser.add_argument("--lib", action="append", dest="libs", help="Specific library file to load")
    parser.add_argument(
        "--format", choices=["table", "json"], default="table", help="Output format"
    )
    parser.add_argument("--filter", dest="pattern", help="Filter by symbol reference pattern")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show all pins, not just unconnected"
    )

    args = parser.parse_args()

    # Load schematic
    try:
        sch = Schematic.load(args.schematic)
    except FileNotFoundError:
        print(f"Error: Schematic not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)

    # Set up library manager
    lib_manager = LibraryManager()

    # Add search paths
    if args.lib_paths:
        for path in args.lib_paths:
            lib_manager.add_search_path(path)
            # Also load any .kicad_sym files found there
            for lib_file in Path(path).glob("*.kicad_sym"):
                try:
                    lib_manager.load_library(str(lib_file))
                except Exception as e:
                    print(f"Warning: Could not load {lib_file}: {e}", file=sys.stderr)

    # Load specific libraries
    if args.libs:
        for lib_path in args.libs:
            try:
                lib_manager.load_library(lib_path)
            except Exception as e:
                print(f"Error loading library {lib_path}: {e}", file=sys.stderr)

    if not lib_manager.libraries:
        print(
            "Warning: No symbol libraries loaded. Use --lib-path or --lib to specify libraries.",
            file=sys.stderr,
        )
        print("Without libraries, pin positions cannot be determined.", file=sys.stderr)
        sys.exit(1)

    # Check connections
    results = check_symbol_connections(sch, lib_manager, args.pattern)

    # Filter if not verbose
    if not args.verbose:
        results = [r for r in results if not r.connected]

    if args.format == "json":
        output_json(results, args.verbose)
    else:
        output_table(results, args.verbose)


def output_table(results: List[PinStatus], show_all: bool):
    """Output as formatted table."""
    if not results:
        if show_all:
            print("No pins found to check.")
        else:
            print("✓ All pins connected!")
        return

    # Group by symbol
    by_symbol: Dict[str, List[PinStatus]] = {}
    for pin in results:
        if pin.reference not in by_symbol:
            by_symbol[pin.reference] = []
        by_symbol[pin.reference].append(pin)

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
            status = "✓" if pin.connected else "✗ UNCONNECTED"
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


def output_json(results: List[PinStatus], show_all: bool):
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
            }
            for p in results
        ],
        "summary": {
            "total_pins": len(results),
            "connected": sum(1 for r in results if r.connected),
            "unconnected": sum(1 for r in results if not r.connected),
            "symbols_checked": len(set(p.reference for p in results)),
        },
    }
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
