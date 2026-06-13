#!/usr/bin/env python3
"""
Move (reposition) a symbol on a KiCad schematic.

Moves the symbol to a new position, shifts all property labels by the same
delta, and reconnects wire endpoints that were attached to the symbol's pins.

Usage:
    kct sch move-component board.kicad_sch --ref R1 --to 120 80
    kct sch move-component board.kicad_sch --ref R1 --to 120 80 --dry-run
    kct sch move-component board.kicad_sch --ref R1 --to 120 80 --backup
    kct sch move-component board.kicad_sch --ref R1 --to 120 80 --format json

Options:
    --ref <reference>      Symbol reference designator (e.g., R1, U1)
    --to <x> <y>           New position in schematic coordinates
    --lib-path <path>      Path to search for symbol libraries (can be repeated)
    --lib <file>           Specific library file to load (can be repeated)
    --dry-run              Show what would be changed without modifying
    --backup               Create backup before modifying
    --format {text,json}   Output format (default: text)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import LibraryManager, Schematic
from kicad_tools.sexp import SExp

GRID_STEP = 1.27  # mm - standard KiCad schematic grid
POINT_TOLERANCE = 0.127  # mm - tolerance for endpoint matching (1/10 grid)


def _snap(value: float, grid: float = GRID_STEP) -> float:
    """Snap a coordinate to the nearest grid point."""
    return round(value / grid) * grid


@dataclass
class MoveComponentResult:
    """Result of a move-component operation."""

    reference: str
    old_position: tuple[float, float] = (0.0, 0.0)
    new_position: tuple[float, float] = (0.0, 0.0)
    properties_shifted: int = 0
    wires_adjusted: int = 0
    warnings: list[str] = field(default_factory=list)


def _find_symbol_sexp(schematic: Schematic, reference: str) -> SExp | None:
    """Find the top-level symbol S-expression node by reference designator."""
    for sym_sexp in schematic.sexp.find_all("symbol"):
        for prop in sym_sexp.find_all("property"):
            prop_name = prop.get_string(0)
            if prop_name == "Reference":
                prop_value = prop.get_string(1)
                if prop_value == reference:
                    return sym_sexp
    return None


def _find_wires_at_point(
    schematic: Schematic,
    point: tuple[float, float],
    tolerance: float = POINT_TOLERANCE,
) -> list[tuple[SExp, int]]:
    """Find all wire S-expression nodes with an endpoint at or near a point.

    Returns list of (wire_sexp, endpoint_index) tuples where endpoint_index
    is 0 for start or 1 for end.
    """
    target_x, target_y = point
    matching: list[tuple[SExp, int]] = []

    for wire_sexp in schematic.sexp.find_all("wire"):
        pts_node = wire_sexp.find("pts")
        if not pts_node:
            continue

        xy_nodes = pts_node.find_all("xy")
        if len(xy_nodes) < 2:
            continue

        for idx, xy in enumerate(xy_nodes):
            x = xy.get_float(0) or 0.0
            y = xy.get_float(1) or 0.0

            if abs(x - target_x) <= tolerance and abs(y - target_y) <= tolerance:
                matching.append((wire_sexp, idx))
                break  # Don't add same wire twice for both endpoints

    return matching


def move_component(
    schematic: Schematic,
    lib_manager: LibraryManager,
    reference: str,
    new_position: tuple[float, float],
) -> MoveComponentResult:
    """Move a symbol to a new position, adjusting properties and wires.

    Returns a MoveComponentResult describing what was changed.
    """
    result = MoveComponentResult(reference=reference)

    # Find symbol instance
    symbol = schematic.get_symbol(reference)
    if symbol is None:
        result.warnings.append(f"Symbol '{reference}' not found")
        return result

    sym_sexp = _find_symbol_sexp(schematic, reference)
    if sym_sexp is None:
        result.warnings.append(f"Symbol S-expression for '{reference}' not found")
        return result

    old_pos = symbol.position
    result.old_position = old_pos
    result.new_position = new_position

    # No-op check
    if abs(new_position[0] - old_pos[0]) < 0.001 and abs(new_position[1] - old_pos[1]) < 0.001:
        return result

    # Compute delta
    dx = new_position[0] - old_pos[0]
    dy = new_position[1] - old_pos[1]

    # Resolve old pin positions
    lib_sym = lib_manager.get_symbol(symbol.lib_id)
    if lib_sym is None and ":" in symbol.lib_id:
        sym_name = symbol.lib_id.split(":", 1)[1]
        lib_sym = lib_manager.get_symbol(sym_name)

    old_pin_positions: dict[str, tuple[float, float]] = {}
    new_pin_positions: dict[str, tuple[float, float]] = {}

    if lib_sym is not None:
        old_pin_positions = lib_sym.get_all_pin_positions(
            instance_pos=old_pos,
            instance_rot=symbol.rotation,
            mirror=symbol.mirror,
        )
        new_pin_positions = lib_sym.get_all_pin_positions(
            instance_pos=new_position,
            instance_rot=symbol.rotation,
            mirror=symbol.mirror,
        )

    # Step 1: Update symbol (at X Y ROT) node
    at_node = sym_sexp.find("at")
    if at_node:
        at_node.set_value(0, new_position[0])
        at_node.set_value(1, new_position[1])
        # Rotation (index 2) stays the same

    # Step 2: Update property positions by the same delta
    for prop_sexp in sym_sexp.find_all("property"):
        prop_at = prop_sexp.find("at")
        if prop_at:
            old_x = prop_at.get_float(0) or 0.0
            old_y = prop_at.get_float(1) or 0.0
            prop_at.set_value(0, old_x + dx)
            prop_at.set_value(1, old_y + dy)
            result.properties_shifted += 1

    # Step 3: Reconnect wires - for each pin, find wires with an endpoint
    # matching the old pin position and update to new pin position
    adjusted_wire_ids: set[int] = set()

    for pin_num, old_pin_pos in old_pin_positions.items():
        new_pin_pos = new_pin_positions.get(pin_num)
        if new_pin_pos is None:
            continue

        # Skip if pin didn't actually move (shouldn't happen with translation, but safe)
        if (
            abs(new_pin_pos[0] - old_pin_pos[0]) < 0.001
            and abs(new_pin_pos[1] - old_pin_pos[1]) < 0.001
        ):
            continue

        wire_hits = _find_wires_at_point(schematic, old_pin_pos)
        for wire_sexp, endpoint_idx in wire_hits:
            wire_id = id(wire_sexp)
            if wire_id in adjusted_wire_ids:
                # Already adjusted this wire (shared endpoint between two pins
                # of the same symbol). Warn about potential degenerate geometry.
                result.warnings.append(
                    f"Wire already adjusted for another pin; "
                    f"pin {pin_num} endpoint may create degenerate geometry"
                )
                continue

            pts_node = wire_sexp.find("pts")
            if not pts_node:
                continue

            xy_nodes = pts_node.find_all("xy")
            if endpoint_idx >= len(xy_nodes):
                continue

            xy_node = xy_nodes[endpoint_idx]
            xy_node.set_value(0, new_pin_pos[0])
            xy_node.set_value(1, new_pin_pos[1])

            adjusted_wire_ids.add(wire_id)
            result.wires_adjusted += 1

    # Invalidate cached data since we modified the S-expression tree
    schematic.invalidate_cache()

    return result


def preview_move_component(
    schematic: Schematic,
    lib_manager: LibraryManager,
    reference: str,
    new_position: tuple[float, float],
) -> dict:
    """Preview what would change without modifying the schematic."""
    symbol = schematic.get_symbol(reference)
    if symbol is None:
        return {"error": f"Symbol '{reference}' not found"}

    old_pos = symbol.position

    # Compute delta
    dx = new_position[0] - old_pos[0]
    dy = new_position[1] - old_pos[1]

    # Resolve pin positions
    lib_sym = lib_manager.get_symbol(symbol.lib_id)
    if lib_sym is None and ":" in symbol.lib_id:
        sym_name = symbol.lib_id.split(":", 1)[1]
        lib_sym = lib_manager.get_symbol(sym_name)

    old_pin_positions: dict[str, tuple[float, float]] = {}
    if lib_sym is not None:
        old_pin_positions = lib_sym.get_all_pin_positions(
            instance_pos=old_pos,
            instance_rot=symbol.rotation,
            mirror=symbol.mirror,
        )

    # Count wires that would be adjusted
    wires_to_adjust = 0
    seen_wire_ids: set[int] = set()
    for pin_num, old_pin_pos in old_pin_positions.items():
        wire_hits = _find_wires_at_point(schematic, old_pin_pos)
        for wire_sexp, _ in wire_hits:
            wire_id = id(wire_sexp)
            if wire_id not in seen_wire_ids:
                seen_wire_ids.add(wire_id)
                wires_to_adjust += 1

    # Count properties
    sym_sexp = _find_symbol_sexp(schematic, reference)
    prop_count = 0
    if sym_sexp:
        for prop_sexp in sym_sexp.find_all("property"):
            if prop_sexp.find("at"):
                prop_count += 1

    is_noop = abs(dx) < 0.001 and abs(dy) < 0.001

    return {
        "reference": reference,
        "lib_id": symbol.lib_id,
        "old_position": list(old_pos),
        "new_position": list(new_position),
        "delta": [round(dx, 4), round(dy, 4)],
        "pins": len(old_pin_positions),
        "properties_to_shift": prop_count,
        "wires_to_adjust": wires_to_adjust,
        "is_noop": is_noop,
    }


def run_move_component(args) -> int:
    """Execute the move-component command."""
    schematic_path = Path(args.schematic)

    if not args.ref:
        print("Error: --ref is required", file=sys.stderr)
        return 1

    if not args.to:
        print("Error: --to is required", file=sys.stderr)
        return 1

    try:
        sch = Schematic.load(schematic_path)
    except (FileNotFoundError, KiCadFileNotFoundError):
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

    # Snap target to grid
    new_x = _snap(args.to[0])
    new_y = _snap(args.to[1])
    new_position = (new_x, new_y)

    # Check symbol exists
    symbol = sch.get_symbol(args.ref)
    if symbol is None:
        msg = f"Symbol '{args.ref}' not found in schematic"
        if args.format == "json":
            print(json.dumps({"error": msg, "moved": False}, indent=2))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    # Dry run
    if args.dry_run:
        preview = preview_move_component(sch, lib_manager, args.ref, new_position)

        if args.format == "json":
            preview["dry_run"] = True
            preview["moved"] = False
            print(json.dumps(preview, indent=2))
        else:
            print("DRY RUN - No changes will be made")
            print("=" * 60)
            print(f"Symbol: {args.ref} ({preview.get('lib_id', '?')})")
            old = preview.get("old_position", [0, 0])
            new = preview.get("new_position", [0, 0])
            delta = preview.get("delta", [0, 0])
            print(f"Old position: ({old[0]:.2f}, {old[1]:.2f})")
            print(f"New position: ({new[0]:.2f}, {new[1]:.2f})")
            print(f"Delta: ({delta[0]:.2f}, {delta[1]:.2f})")
            print(f"Pins: {preview.get('pins', 0)}")
            print(f"Properties to shift: {preview.get('properties_to_shift', 0)}")
            print(f"Wires to adjust: {preview.get('wires_to_adjust', 0)}")
            if preview.get("is_noop"):
                print("NOTE: No movement needed (same position)")
        return 0

    # Create backup if requested
    if args.backup:
        backup_path = f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(schematic_path, backup_path)
        if args.format != "json":
            print(f"Backup created: {backup_path}")

    # Execute move
    result = move_component(sch, lib_manager, args.ref, new_position)

    # Check for failure
    if result.warnings and not result.wires_adjusted and result.old_position == (0.0, 0.0):
        # Symbol not found case
        msg = result.warnings[0] if result.warnings else f"Failed to move symbol '{args.ref}'"
        if args.format == "json":
            print(json.dumps({"error": msg, "moved": False}, indent=2))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    sch.save()

    if args.format == "json":
        data = {
            "moved": True,
            "reference": result.reference,
            "old_position": list(result.old_position),
            "new_position": list(result.new_position),
            "properties_shifted": result.properties_shifted,
            "wires_adjusted": result.wires_adjusted,
            "warnings": result.warnings,
        }
        print(json.dumps(data, indent=2))
    else:
        print(f"Moved symbol: {result.reference}")
        print(f"  From: ({result.old_position[0]:.2f}, {result.old_position[1]:.2f})")
        print(f"  To:   ({result.new_position[0]:.2f}, {result.new_position[1]:.2f})")
        print(f"  Properties shifted: {result.properties_shifted}")
        print(f"  Wires adjusted: {result.wires_adjusted}")
        if result.warnings:
            for w in result.warnings:
                print(f"  Warning: {w}")

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Move a symbol to a new position on a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--ref", required=True, help="Symbol reference designator (e.g., U1)")
    parser.add_argument(
        "--to",
        nargs=2,
        type=float,
        required=True,
        metavar=("X", "Y"),
        help="New position coordinates",
    )
    parser.add_argument("--lib-path", action="append", dest="lib_paths", help="Library search path")
    parser.add_argument("--lib", action="append", dest="libs", help="Specific library file")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Preview without modifying")
    parser.add_argument("--backup", action="store_true", help="Create backup before modifying")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")

    args = parser.parse_args(argv)
    return run_move_component(args)


if __name__ == "__main__":
    sys.exit(main())
