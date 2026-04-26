#!/usr/bin/env python3
"""
Remove a symbol (component) from a KiCad schematic.

Removes the symbol S-expression node, any wires exclusively connected to it,
orphaned junctions at former wire endpoints, the ``symbol_instances`` path
entry, and the ``lib_symbols`` entry when the last instance is removed.

Usage:
    kct sch remove-component board.kicad_sch --ref PWR1
    kct sch remove-component board.kicad_sch --ref PWR1 --dry-run
    kct sch remove-component board.kicad_sch --ref PWR1 --backup
    kct sch remove-component board.kicad_sch --ref PWR1 --format json

Options:
    --ref <reference>      Symbol reference designator (e.g., PWR1, U1)
    --lib-path <path>      Path to search for symbol libraries (can be repeated)
    --lib <file>           Specific library file to load (can be repeated)
    --dry-run              Show what would be removed without modifying
    --backup               Create backup before modifying
    --format {text,json}   Output format (default: text)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kicad_tools.schema import LibraryManager, Schematic
from kicad_tools.sexp import SExp

POINT_TOLERANCE = 1.27  # mm - standard KiCad grid


@dataclass
class RemoveComponentResult:
    """Result of a remove-component operation."""

    reference: str
    symbol_removed: bool
    wires_removed: int
    junctions_removed: int
    lib_symbol_removed: bool
    instance_path_removed: bool


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

            tol_units = int(tolerance * 10)
            if (
                abs(key[0] - target_key[0]) <= tol_units
                and abs(key[1] - target_key[1]) <= tol_units
            ):
                matching.append(wire_sexp)
                break  # Don't add same wire twice

    return matching


def _wire_start_end(wire_sexp: SExp) -> tuple[tuple[float, float], tuple[float, float]]:
    """Extract start and end points from a wire S-expression node."""
    pts_node = wire_sexp.find("pts")
    if not pts_node:
        return (0.0, 0.0), (0.0, 0.0)

    xy_nodes = pts_node.find_all("xy")
    if len(xy_nodes) < 2:
        return (0.0, 0.0), (0.0, 0.0)

    x1 = xy_nodes[0].get_float(0) or 0.0
    y1 = xy_nodes[0].get_float(1) or 0.0
    x2 = xy_nodes[1].get_float(0) or 0.0
    y2 = xy_nodes[1].get_float(1) or 0.0

    return (x1, y1), (x2, y2)


def _wire_endpoint_counts(
    wire_sexps: list[SExp],
) -> dict[tuple[int, int], int]:
    """Count how many wires touch each endpoint."""
    counts: dict[tuple[int, int], int] = {}
    for ws in wire_sexps:
        start, end = _wire_start_end(ws)
        for pt in [start, end]:
            key = (int(pt[0] * 10), int(pt[1] * 10))
            counts[key] = counts.get(key, 0) + 1
    return counts


def _collect_all_connectable_points(
    schematic: Schematic,
    lib_manager: LibraryManager,
    exclude_reference: str,
) -> set[tuple[int, int]]:
    """Collect all connectable points in the schematic *except* for the symbol
    identified by ``exclude_reference``.

    Connectable points include:
    - Pin positions of all other symbols (resolved via library)
    - Label positions (label, global_label, hierarchical_label)
    - Power flag / power symbol pin positions (already covered by symbols)
    - No-connect positions
    """
    points: set[tuple[int, int]] = set()

    # Other symbol pin positions
    for sym in schematic.symbols:
        if sym.reference == exclude_reference:
            continue

        lib_sym = lib_manager.get_symbol(sym.lib_id)
        if lib_sym is None and ":" in sym.lib_id:
            sym_name = sym.lib_id.split(":", 1)[1]
            lib_sym = lib_manager.get_symbol(sym_name)

        if lib_sym is not None:
            pin_positions = lib_sym.get_all_pin_positions(
                instance_pos=sym.position,
                instance_rot=sym.rotation,
                mirror=sym.mirror,
            )
            for pos in pin_positions.values():
                points.add((int(pos[0] * 10), int(pos[1] * 10)))
        else:
            # Fallback: use symbol origin as a connectable point
            points.add((int(sym.position[0] * 10), int(sym.position[1] * 10)))

    # Labels (local, global, hierarchical)
    for tag in ("label", "global_label", "hierarchical_label"):
        for lbl_sexp in schematic.sexp.find_all(tag):
            at_node = lbl_sexp.find("at")
            if at_node:
                x = at_node.get_float(0) or 0.0
                y = at_node.get_float(1) or 0.0
                points.add((int(x * 10), int(y * 10)))

    # No-connect markers (wire endpoints touching these are NOT exclusive)
    for nc_sexp in schematic.sexp.find_all("no_connect"):
        at_node = nc_sexp.find("at")
        if at_node:
            x = at_node.get_float(0) or 0.0
            y = at_node.get_float(1) or 0.0
            points.add((int(x * 10), int(y * 10)))

    return points


def _is_wire_exclusive(
    wire_sexp: SExp,
    pin_keys: set[tuple[int, int]],
    other_connectable: set[tuple[int, int]],
    tolerance: float = POINT_TOLERANCE,
) -> bool:
    """Determine if a wire is exclusively connected to the removed symbol.

    A wire is exclusive if *every* endpoint either:
    - Touches one of the removed symbol's pin positions, OR
    - Does NOT touch any other connectable point (dangling end)

    In other words, a wire is shared (not exclusive) if at least one of its
    endpoints touches a connectable point belonging to another element.
    """
    tol_units = int(tolerance * 10)
    start, end = _wire_start_end(wire_sexp)

    for pt in [start, end]:
        pt_key = (int(pt[0] * 10), int(pt[1] * 10))

        # If this endpoint is one of the removed symbol's pins, it's fine
        is_own_pin = False
        for pk in pin_keys:
            if abs(pt_key[0] - pk[0]) <= tol_units and abs(pt_key[1] - pk[1]) <= tol_units:
                is_own_pin = True
                break
        if is_own_pin:
            continue

        # If this endpoint touches another connectable element, wire is shared
        for cp in other_connectable:
            if abs(pt_key[0] - cp[0]) <= tol_units and abs(pt_key[1] - cp[1]) <= tol_units:
                return False

        # Check if another wire also has an endpoint here (T-junction / continuation)
        # If so, the wire is shared infrastructure
        # This is handled implicitly: if the other end doesn't touch any
        # connectable point, the wire endpoint is dangling and exclusive

    return True


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


def _get_symbol_uuid(sym_sexp: SExp) -> str | None:
    """Extract UUID from a symbol S-expression node."""
    uuid_node = sym_sexp.find("uuid")
    if uuid_node:
        return uuid_node.get_string(0)
    return None


def _remove_symbol_instance_path(schematic: Schematic, sym_uuid: str) -> bool:
    """Remove the symbol_instances path entry for the given UUID."""
    si_section = schematic.sexp.find("symbol_instances")
    if si_section is None:
        return False

    for path_node in list(si_section.find_all("path")):
        path_str = path_node.get_string(0)
        if path_str and sym_uuid in path_str:
            if si_section.remove(path_node):
                return True
    return False


def _count_lib_id_usage(schematic: Schematic, lib_id: str) -> int:
    """Count how many symbol instances use the given lib_id."""
    count = 0
    for sym in schematic.symbols:
        if sym.lib_id == lib_id:
            count += 1
    return count


def _remove_lib_symbol(schematic: Schematic, lib_id: str) -> bool:
    """Remove the lib_symbols entry for the given lib_id."""
    lib_syms = schematic.lib_symbols
    if lib_syms is None:
        return False

    for sym_sexp in list(lib_syms.find_all("symbol")):
        sym_name = sym_sexp.get_string(0)
        if sym_name == lib_id:
            if lib_syms.remove(sym_sexp):
                return True
    return False


def remove_component(
    schematic: Schematic,
    lib_manager: LibraryManager,
    reference: str,
) -> RemoveComponentResult:
    """Remove a symbol and its exclusive wires from the schematic.

    Returns a RemoveComponentResult describing what was changed.
    """
    result = RemoveComponentResult(
        reference=reference,
        symbol_removed=False,
        wires_removed=0,
        junctions_removed=0,
        lib_symbol_removed=False,
        instance_path_removed=False,
    )

    # Find symbol instance
    symbol = schematic.get_symbol(reference)
    if symbol is None:
        return result

    sym_sexp = _find_symbol_sexp(schematic, reference)
    if sym_sexp is None:
        return result

    sym_uuid = _get_symbol_uuid(sym_sexp) or ""
    lib_id = symbol.lib_id

    # Resolve pin positions
    pin_positions: dict[str, tuple[float, float]] = {}
    lib_sym = lib_manager.get_symbol(symbol.lib_id)
    if lib_sym is None and ":" in symbol.lib_id:
        sym_name = symbol.lib_id.split(":", 1)[1]
        lib_sym = lib_manager.get_symbol(sym_name)

    if lib_sym is not None:
        pin_positions = lib_sym.get_all_pin_positions(
            instance_pos=symbol.position,
            instance_rot=symbol.rotation,
            mirror=symbol.mirror,
        )

    pin_keys = {(int(p[0] * 10), int(p[1] * 10)) for p in pin_positions.values()}

    # If no pin positions resolved, use symbol origin as fallback
    if not pin_keys:
        pin_keys = {(int(symbol.position[0] * 10), int(symbol.position[1] * 10))}

    # Collect connectable points from other elements
    other_connectable = _collect_all_connectable_points(
        schematic, lib_manager, reference,
    )

    # Find wires connected to this symbol
    all_connected_wires: list[SExp] = []
    seen_ids: set[int] = set()
    for pos in pin_positions.values():
        for wire in _find_wires_at_point(schematic, pos):
            wire_id = id(wire)
            if wire_id not in seen_ids:
                seen_ids.add(wire_id)
                all_connected_wires.append(wire)

    # Also check symbol origin for power symbols with no resolved pins
    if not pin_positions:
        for wire in _find_wires_at_point(schematic, symbol.position):
            wire_id = id(wire)
            if wire_id not in seen_ids:
                seen_ids.add(wire_id)
                all_connected_wires.append(wire)

    # Determine which wires are exclusive
    exclusive_wires: list[SExp] = []
    for wire in all_connected_wires:
        if _is_wire_exclusive(wire, pin_keys, other_connectable):
            exclusive_wires.append(wire)

    # Remove exclusive wires and collect their endpoints for junction cleanup
    wire_endpoints: list[tuple[float, float]] = []
    for wire in exclusive_wires:
        start, end = _wire_start_end(wire)
        wire_endpoints.extend([start, end])
        if schematic.sexp.remove(wire):
            result.wires_removed += 1

    # Clean up orphaned junctions at former wire endpoints
    if wire_endpoints:
        remaining_wires = list(schematic.sexp.find_all("wire"))
        endpoint_counts = _wire_endpoint_counts(remaining_wires)

        checked_keys: set[tuple[int, int]] = set()
        for pt in wire_endpoints:
            pt_key = (int(pt[0] * 10), int(pt[1] * 10))
            if pt_key in checked_keys:
                continue
            checked_keys.add(pt_key)

            wire_count = endpoint_counts.get(pt_key, 0)
            if wire_count >= 3:
                continue

            for junc_sexp in list(schematic.sexp.find_all("junction")):
                at_node = junc_sexp.find("at")
                if not at_node:
                    continue
                jx = at_node.get_float(0) or 0.0
                jy = at_node.get_float(1) or 0.0
                junc_key = (int(jx * 10), int(jy * 10))

                if junc_key == pt_key:
                    if schematic.sexp.remove(junc_sexp):
                        result.junctions_removed += 1

    # Remove the symbol node itself
    if schematic.sexp.remove(sym_sexp):
        result.symbol_removed = True

    # Remove symbol_instances path entry
    if sym_uuid:
        result.instance_path_removed = _remove_symbol_instance_path(schematic, sym_uuid)

    # Clean up lib_symbols if this was the last instance
    # After removing the symbol, count remaining instances with this lib_id
    # (invalidate cache first so symbols list is refreshed)
    schematic.invalidate_cache()
    remaining_count = _count_lib_id_usage(schematic, lib_id)
    if remaining_count == 0:
        result.lib_symbol_removed = _remove_lib_symbol(schematic, lib_id)
        schematic.invalidate_cache()

    return result


def preview_remove_component(
    schematic: Schematic,
    lib_manager: LibraryManager,
    reference: str,
) -> dict:
    """Preview what would be removed without modifying the schematic.

    Returns a dict with preview information.
    """
    symbol = schematic.get_symbol(reference)
    if symbol is None:
        return {"error": f"Symbol '{reference}' not found"}

    # Resolve pin positions
    pin_positions: dict[str, tuple[float, float]] = {}
    lib_sym = lib_manager.get_symbol(symbol.lib_id)
    if lib_sym is None and ":" in symbol.lib_id:
        sym_name = symbol.lib_id.split(":", 1)[1]
        lib_sym = lib_manager.get_symbol(sym_name)

    if lib_sym is not None:
        pin_positions = lib_sym.get_all_pin_positions(
            instance_pos=symbol.position,
            instance_rot=symbol.rotation,
            mirror=symbol.mirror,
        )

    pin_keys = {(int(p[0] * 10), int(p[1] * 10)) for p in pin_positions.values()}
    if not pin_keys:
        pin_keys = {(int(symbol.position[0] * 10), int(symbol.position[1] * 10))}

    other_connectable = _collect_all_connectable_points(
        schematic, lib_manager, reference,
    )

    # Find connected wires
    all_connected_wires: list[SExp] = []
    seen_ids: set[int] = set()
    for pos in pin_positions.values():
        for wire in _find_wires_at_point(schematic, pos):
            wire_id = id(wire)
            if wire_id not in seen_ids:
                seen_ids.add(wire_id)
                all_connected_wires.append(wire)

    if not pin_positions:
        for wire in _find_wires_at_point(schematic, symbol.position):
            wire_id = id(wire)
            if wire_id not in seen_ids:
                seen_ids.add(wire_id)
                all_connected_wires.append(wire)

    exclusive_count = sum(
        1 for w in all_connected_wires
        if _is_wire_exclusive(w, pin_keys, other_connectable)
    )
    shared_count = len(all_connected_wires) - exclusive_count

    lib_id = symbol.lib_id
    remaining_after = _count_lib_id_usage(schematic, lib_id) - 1
    will_remove_lib = remaining_after <= 0

    return {
        "reference": reference,
        "lib_id": lib_id,
        "position": list(symbol.position),
        "pins": len(pin_positions),
        "wires_connected": len(all_connected_wires),
        "wires_exclusive": exclusive_count,
        "wires_shared": shared_count,
        "will_remove_lib_symbol": will_remove_lib,
    }


def run_remove_component(args) -> int:
    """Execute the remove-component command."""
    schematic_path = Path(args.schematic)

    if not args.ref:
        print("Error: --ref is required", file=sys.stderr)
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

    # Check symbol exists
    symbol = sch.get_symbol(args.ref)
    if symbol is None:
        msg = f"Symbol '{args.ref}' not found in schematic"
        if args.format == "json":
            print(json.dumps({"error": msg, "removed": False}, indent=2))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    # Dry run
    if args.dry_run:
        preview = preview_remove_component(sch, lib_manager, args.ref)

        if args.format == "json":
            preview["dry_run"] = True
            preview["removed"] = False
            print(json.dumps(preview, indent=2))
        else:
            print("DRY RUN - No changes will be made")
            print("=" * 60)
            print(f"Symbol: {args.ref} ({preview.get('lib_id', '?')})")
            pos = preview.get("position", [0, 0])
            print(f"Position: ({pos[0]:.2f}, {pos[1]:.2f})")
            print(f"Pins: {preview.get('pins', 0)}")
            print(f"Wires connected: {preview.get('wires_connected', 0)}")
            print(f"  Exclusive (would remove): {preview.get('wires_exclusive', 0)}")
            print(f"  Shared (would preserve): {preview.get('wires_shared', 0)}")
            if preview.get("will_remove_lib_symbol"):
                print(f"Library symbol '{preview.get('lib_id')}': would be removed (last instance)")
        return 0

    # Create backup if requested
    if args.backup:
        backup_path = f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(schematic_path, backup_path)
        if args.format != "json":
            print(f"Backup created: {backup_path}")

    # Execute removal
    result = remove_component(sch, lib_manager, args.ref)

    if not result.symbol_removed:
        msg = f"Failed to remove symbol '{args.ref}'"
        if args.format == "json":
            print(json.dumps({"error": msg, "removed": False}, indent=2))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    sch.save()

    if args.format == "json":
        data = {
            "removed": True,
            "reference": result.reference,
            "wires_removed": result.wires_removed,
            "junctions_removed": result.junctions_removed,
            "lib_symbol_removed": result.lib_symbol_removed,
            "instance_path_removed": result.instance_path_removed,
        }
        print(json.dumps(data, indent=2))
    else:
        print(f"Removed symbol: {result.reference}")
        print(f"  Wires removed: {result.wires_removed}")
        if result.junctions_removed:
            print(f"  Junctions removed: {result.junctions_removed}")
        if result.lib_symbol_removed:
            print("  Library symbol removed (last instance)")
        if result.instance_path_removed:
            print("  Instance path entry removed")

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Remove a symbol from a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--ref", required=True, help="Symbol reference designator (e.g., U1)")
    parser.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    parser.add_argument("--lib", action="append", dest="libs", help="Specific library file")
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    parser.add_argument("--backup", action="store_true", help="Create backup before modifying")
    parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format"
    )

    args = parser.parse_args(argv)
    return run_remove_component(args)


if __name__ == "__main__":
    sys.exit(main())
