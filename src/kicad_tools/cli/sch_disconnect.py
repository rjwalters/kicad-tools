#!/usr/bin/env python3
"""
Disconnect a pin from its net by removing wires at the pin position.

Usage:
    kct sch disconnect board.kicad_sch --ref U1 --pin 5 --lib-path lib/
    kct sch disconnect board.kicad_sch --ref U1 --pin 5 --lib-path lib/ --dry-run
    kct sch disconnect board.kicad_sch --ref U1 --pin 5 --lib-path lib/ --add-nc

Options:
    --ref <reference>      Symbol reference (e.g., U1)
    --pin <pin>            Pin number to disconnect
    --lib-path <path>      Path to search for symbol libraries (can be repeated)
    --lib <file>           Specific library file to load (can be repeated)
    --add-nc               Add a no-connect marker after disconnecting
    --dry-run              Show what would change without modifying
    --backup               Create backup before modifying
"""

from __future__ import annotations

import argparse
import shutil
import sys
import uuid as uuid_mod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kicad_tools.schema import LibraryManager, Schematic
from kicad_tools.sexp import SExp

POINT_TOLERANCE = 1.27  # mm - standard KiCad grid


@dataclass
class DisconnectResult:
    """Result of a disconnect operation."""

    pin_position: tuple[float, float]
    wires_removed: int
    no_connect_added: bool


def _find_wires_at_point(
    schematic: Schematic,
    point: tuple[float, float],
    tolerance: float = POINT_TOLERANCE,
) -> list[SExp]:
    """Find all wire S-expression nodes with an endpoint at or near a point."""
    target_key = (int(point[0] * 10), int(point[1] * 10))
    matching = []

    for wire_sexp in schematic.sexp.find_all("wire"):
        pts_node = wire_sexp.find("pts")
        if not pts_node:
            continue

        xy_nodes = pts_node.find_all("xy")
        if len(xy_nodes) < 2:
            continue

        for xy in xy_nodes:
            x = xy.get_float(0) or 0.0
            y = xy.get_float(1) or 0.0
            key = (int(x * 10), int(y * 10))

            # Check within tolerance (grid-snapped neighbor check)
            tol_units = int(tolerance * 10)
            if (
                abs(key[0] - target_key[0]) <= tol_units
                and abs(key[1] - target_key[1]) <= tol_units
            ):
                matching.append(wire_sexp)
                break  # Don't add same wire twice

    return matching


def _build_no_connect_sexp(x: float, y: float) -> SExp:
    """Build an S-expression node for a no_connect marker."""
    return SExp.list(
        "no_connect",
        SExp.list("at", x, y),
        SExp.list("uuid", str(uuid_mod.uuid4())),
    )


def resolve_pin_position(
    schematic: Schematic,
    lib_manager: LibraryManager,
    reference: str,
    pin_number: str,
) -> tuple[float, float] | None:
    """Resolve a pin's absolute position using library data."""
    symbol = schematic.get_symbol(reference)
    if symbol is None:
        return None

    lib_sym = lib_manager.get_symbol(symbol.lib_id)
    if lib_sym is None and ":" in symbol.lib_id:
        sym_name = symbol.lib_id.split(":", 1)[1]
        lib_sym = lib_manager.get_symbol(sym_name)

    if lib_sym is None:
        return None

    pin_positions = lib_sym.get_all_pin_positions(
        instance_pos=symbol.position,
        instance_rot=symbol.rotation,
        mirror=symbol.mirror,
    )

    return pin_positions.get(pin_number)


def disconnect_pin(
    schematic: Schematic,
    pin_position: tuple[float, float],
    add_no_connect: bool = False,
) -> DisconnectResult:
    """Remove wires at a pin position and optionally add a no-connect marker.

    Returns a DisconnectResult describing what was changed.
    """
    wires = _find_wires_at_point(schematic, pin_position)

    removed = 0
    for wire_sexp in wires:
        if schematic.sexp.remove(wire_sexp):
            removed += 1

    nc_added = False
    if add_no_connect and removed > 0:
        idx = schematic._find_insertion_index()
        nc_node = _build_no_connect_sexp(pin_position[0], pin_position[1])
        schematic.sexp.insert(idx, nc_node)
        nc_added = True

    if removed or nc_added:
        schematic.invalidate_cache()

    return DisconnectResult(
        pin_position=pin_position,
        wires_removed=removed,
        no_connect_added=nc_added,
    )


def run_disconnect(args) -> int:
    """Execute the disconnect command."""
    schematic_path = Path(args.schematic)

    if not args.ref or not args.pin:
        print("Error: --ref and --pin are required", file=sys.stderr)
        return 1

    try:
        sch = Schematic.load(schematic_path)
    except FileNotFoundError:
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    # Set up library manager
    lib_manager = LibraryManager()

    if args.lib_paths:
        for lp in args.lib_paths:
            lib_manager.add_search_path(lp)

    if args.libs:
        for lib_file in args.libs:
            lib_manager.load_library(lib_file)

    lib_manager.load_embedded(sch)

    # Resolve pin position
    pos = resolve_pin_position(sch, lib_manager, args.ref, args.pin)
    if pos is None:
        print(
            f"Error: Could not resolve position for {args.ref} pin {args.pin}",
            file=sys.stderr,
        )
        return 1

    # Preview mode
    if args.dry_run:
        wires = _find_wires_at_point(sch, pos)
        print("DRY RUN - No changes will be made")
        print("=" * 60)
        print(f"Pin: {args.ref} pin {args.pin} at ({pos[0]:.2f}, {pos[1]:.2f})")
        print(f"Wires to remove: {len(wires)}")
        if args.add_nc:
            print("No-connect marker: would be added")
        return 0

    # Create backup if requested
    if args.backup:
        backup_path = f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(schematic_path, backup_path)
        print(f"Backup created: {backup_path}")

    # Execute disconnect
    result = disconnect_pin(sch, pos, add_no_connect=args.add_nc)

    if result.wires_removed == 0:
        print(f"No wires found at {args.ref} pin {args.pin} ({pos[0]:.2f}, {pos[1]:.2f})")
        return 0

    sch.save()

    print(f"Disconnected {args.ref} pin {args.pin}")
    print(f"  Wires removed: {result.wires_removed}")
    if result.no_connect_added:
        print("  No-connect marker added")

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Disconnect a pin from its net",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--ref", required=True, help="Symbol reference (e.g., U1)")
    parser.add_argument("--pin", required=True, help="Pin number to disconnect")
    parser.add_argument("--lib-path", action="append", dest="lib_paths", help="Library search path")
    parser.add_argument("--lib", action="append", dest="libs", help="Specific library file")
    parser.add_argument(
        "--add-nc", action="store_true", help="Add no-connect marker after disconnecting"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without modifying")
    parser.add_argument("--backup", action="store_true", help="Create backup before modifying")

    args = parser.parse_args(argv)
    return run_disconnect(args)


if __name__ == "__main__":
    sys.exit(main())
