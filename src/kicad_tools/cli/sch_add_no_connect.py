#!/usr/bin/env python3
"""
Add no-connect markers to schematic pins.

Supports explicit pin targeting (--ref/--pin) and automatic mode (--auto)
that marks all unconnected pins.

Usage:
    kct sch add-no-connect board.kicad_sch --ref U1 --pin 5 --lib-path lib/
    kct sch add-no-connect board.kicad_sch --auto --lib-path lib/
    kct sch add-no-connect board.kicad_sch --ref U1 --pin 5 --lib-path lib/ --dry-run

Options:
    --ref <reference>      Symbol reference (e.g., U1)
    --pin <pin>            Pin number to mark
    --auto                 Automatically mark all unconnected pins
    --lib-path <path>      Path to search for symbol libraries (can be repeated)
    --lib <file>           Specific library file to load (can be repeated)
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


@dataclass
class NoConnectAction:
    """Represents a no-connect marker to be added."""

    reference: str
    pin_number: str
    pin_name: str
    position: tuple[float, float]


def _build_no_connect_sexp(x: float, y: float) -> SExp:
    """Build an S-expression node for a no_connect marker."""
    return SExp.list(
        "no_connect",
        SExp.list("at", x, y),
        SExp.list("uuid", str(uuid_mod.uuid4())),
    )


def _find_existing_no_connects(schematic: Schematic) -> set[tuple[int, int]]:
    """Return set of positions where no-connect markers already exist."""
    positions = set()
    for nc_node in schematic.sexp.find_all("no_connect"):
        if at := nc_node.find("at"):
            x = at.get_float(0) or 0
            y = at.get_float(1) or 0
            positions.add((int(x * 10), int(y * 10)))
    return positions


def _find_all_connection_points(schematic: Schematic) -> set[tuple[int, int]]:
    """Get all points where connections exist (wire endpoints, labels, junctions)."""
    points = set()

    for wire in schematic.wires:
        points.add((int(wire.start[0] * 10), int(wire.start[1] * 10)))
        points.add((int(wire.end[0] * 10), int(wire.end[1] * 10)))

    for junc in schematic.junctions:
        points.add((int(junc.position[0] * 10), int(junc.position[1] * 10)))

    for lbl in schematic.labels:
        points.add((int(lbl.position[0] * 10), int(lbl.position[1] * 10)))

    for lbl in schematic.global_labels:
        points.add((int(lbl.position[0] * 10), int(lbl.position[1] * 10)))

    for lbl in schematic.hierarchical_labels:
        points.add((int(lbl.position[0] * 10), int(lbl.position[1] * 10)))

    return points


def resolve_pin_position(
    schematic: Schematic,
    lib_manager: LibraryManager,
    reference: str,
    pin_number: str,
) -> tuple[float, float] | None:
    """Resolve a pin's absolute position using library data.

    Returns (x, y) or None if the symbol or pin cannot be found.
    """
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


def find_unconnected_pins(
    schematic: Schematic,
    lib_manager: LibraryManager,
) -> list[NoConnectAction]:
    """Find all pins that are unconnected and lack no-connect markers."""
    connection_points = _find_all_connection_points(schematic)
    existing_nc = _find_existing_no_connects(schematic)
    actions = []

    for symbol in schematic.symbols:
        if symbol.lib_id.startswith("power:"):
            continue

        lib_sym = lib_manager.get_symbol(symbol.lib_id)
        if lib_sym is None and ":" in symbol.lib_id:
            sym_name = symbol.lib_id.split(":", 1)[1]
            lib_sym = lib_manager.get_symbol(sym_name)

        if lib_sym is None:
            continue

        pin_positions = lib_sym.get_all_pin_positions(
            instance_pos=symbol.position,
            instance_rot=symbol.rotation,
            mirror=symbol.mirror,
        )

        for lib_pin in lib_sym.pins:
            if lib_pin.number not in pin_positions:
                continue

            pos = pin_positions[lib_pin.number]
            key = (int(pos[0] * 10), int(pos[1] * 10))

            # Skip if already connected or already has no-connect
            if key in connection_points or key in existing_nc:
                continue

            actions.append(
                NoConnectAction(
                    reference=symbol.reference,
                    pin_number=lib_pin.number,
                    pin_name=lib_pin.name,
                    position=pos,
                )
            )

    return actions


def add_no_connect_markers(
    schematic: Schematic,
    actions: list[NoConnectAction],
) -> int:
    """Insert no-connect markers into the schematic's S-expression tree.

    Returns the number of markers added.
    """
    if not actions:
        return 0

    # Find insertion index (before sheet_instances/symbol_instances)
    idx = schematic._find_insertion_index()

    for action in actions:
        nc_node = _build_no_connect_sexp(action.position[0], action.position[1])
        schematic.sexp.insert(idx, nc_node)
        idx += 1

    schematic.invalidate_cache()
    return len(actions)


def run_add_no_connect(args) -> int:
    """Execute the add-no-connect command."""
    schematic_path = Path(args.schematic)

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

    # Load embedded libraries from the schematic
    lib_manager.load_embedded(sch)

    if args.auto:
        # Auto mode: find all unconnected pins
        actions = find_unconnected_pins(sch, lib_manager)
        if not actions:
            print("No unconnected pins found that need no-connect markers.")
            return 0
    else:
        # Explicit mode: single pin
        if not args.ref or not args.pin:
            print("Error: --ref and --pin are required (or use --auto)", file=sys.stderr)
            return 1

        pos = resolve_pin_position(sch, lib_manager, args.ref, args.pin)
        if pos is None:
            print(
                f"Error: Could not resolve position for {args.ref} pin {args.pin}",
                file=sys.stderr,
            )
            return 1

        # Check if no-connect already exists at this position
        existing_nc = _find_existing_no_connects(sch)
        key = (int(pos[0] * 10), int(pos[1] * 10))
        if key in existing_nc:
            print(f"No-connect marker already exists at ({pos[0]:.2f}, {pos[1]:.2f})")
            return 0

        actions = [
            NoConnectAction(
                reference=args.ref,
                pin_number=args.pin,
                pin_name="",
                position=pos,
            )
        ]

    # Report planned changes
    if args.dry_run:
        print("DRY RUN - No changes will be made")
        print("=" * 60)

    print(f"No-connect markers to add: {len(actions)}")
    for action in actions:
        pin_label = f"{action.reference} pin {action.pin_number}"
        if action.pin_name:
            pin_label += f" ({action.pin_name})"
        print(f"  + {pin_label} at ({action.position[0]:.2f}, {action.position[1]:.2f})")

    if args.dry_run:
        return 0

    # Create backup if requested
    if args.backup:
        backup_path = f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(schematic_path, backup_path)
        print(f"Backup created: {backup_path}")

    # Apply changes
    count = add_no_connect_markers(sch, actions)
    sch.save()

    print(f"\nAdded {count} no-connect marker(s)")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Add no-connect markers to schematic pins",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--ref", help="Symbol reference (e.g., U1)")
    parser.add_argument("--pin", help="Pin number to mark")
    parser.add_argument(
        "--auto", action="store_true", help="Auto-detect and mark all unconnected pins"
    )
    parser.add_argument("--lib-path", action="append", dest="lib_paths", help="Library search path")
    parser.add_argument("--lib", action="append", dest="libs", help="Specific library file")
    parser.add_argument("--dry-run", action="store_true", help="Preview without modifying")
    parser.add_argument("--backup", action="store_true", help="Create backup before modifying")

    args = parser.parse_args(argv)
    return run_add_no_connect(args)


if __name__ == "__main__":
    sys.exit(main())
