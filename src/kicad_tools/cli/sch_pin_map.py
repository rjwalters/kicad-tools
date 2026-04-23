#!/usr/bin/env python3
"""
Resolve pin-to-net assignments for schematic symbols.

Usage:
    kicad-tools sch pin-map <schematic.kicad_sch> [--ref <REF>] [--format json|table]

Uses embedded lib_symbols from the schematic (no external library files needed).
For each component, traces wires from pin positions to labels/power symbols
to determine the net name.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import Schematic
from kicad_tools.schema.label import PowerSymbol
from kicad_tools.schema.library import LibrarySymbol

# Use integer-scaled coordinates for fast set lookup (matches sch_check_connections.py)
Coord = tuple[int, int]


def _to_coord(x: float, y: float) -> Coord:
    """Convert float coordinates to integer-scaled for set lookup."""
    return (int(round(x * 10)), int(round(y * 10)))


def _point_on_segment(
    point: Coord, seg_start: Coord, seg_end: Coord
) -> bool:
    """Check if *point* lies strictly on the interior of the segment (not at endpoints).

    All coordinates are integer-scaled (x*10, y*10).
    Only axis-aligned (horizontal/vertical) and 45-degree wires are common in KiCad;
    we handle the general case with a collinearity + bounding-box check.
    """
    if point == seg_start or point == seg_end:
        return False

    # Collinearity: cross product must be zero (or near-zero for int coords)
    dx1 = seg_end[0] - seg_start[0]
    dy1 = seg_end[1] - seg_start[1]
    dx2 = point[0] - seg_start[0]
    dy2 = point[1] - seg_start[1]
    cross = dx1 * dy2 - dy1 * dx2
    if abs(cross) > 1:  # allow rounding tolerance of 1 unit (0.1mm)
        return False

    # Bounding box check (point must be between start and end)
    min_x = min(seg_start[0], seg_end[0])
    max_x = max(seg_start[0], seg_end[0])
    min_y = min(seg_start[1], seg_end[1])
    max_y = max(seg_start[1], seg_end[1])
    return min_x <= point[0] <= max_x and min_y <= point[1] <= max_y


def _build_wire_graph(
    schematic: Schematic,
) -> tuple[dict[Coord, set[Coord]], dict[Coord, str]]:
    """Build a connectivity graph from wires, labels, and power symbols.

    Labels and other connection points may sit on the interior of a wire
    segment (not just at endpoints). We split wire edges at such points
    so that flood-fill can reach them.

    Returns:
        adjacency: Maps each coordinate node to its connected neighbors
        net_names: Maps label/power-symbol coordinates to their net name
    """
    net_names: dict[Coord, str] = {}

    # Collect all "special" coordinates that need to be graph nodes:
    # labels, junctions, power symbols.
    special_points: set[Coord] = set()

    for lbl in schematic.labels:
        coord = _to_coord(*lbl.position)
        net_names[coord] = lbl.text
        special_points.add(coord)

    for lbl in schematic.global_labels:
        coord = _to_coord(*lbl.position)
        net_names[coord] = lbl.text
        special_points.add(coord)

    for lbl in schematic.hierarchical_labels:
        coord = _to_coord(*lbl.position)
        net_names[coord] = lbl.text
        special_points.add(coord)

    for junc in schematic.junctions:
        special_points.add(_to_coord(*junc.position))

    for sym_sexp in schematic.sexp.find_children("symbol"):
        ps = PowerSymbol.from_symbol_sexp(sym_sexp)
        if ps:
            coord = _to_coord(*ps.position)
            net_name = ps.value or ps.lib_id.split(":", 1)[-1]
            net_names[coord] = net_name
            special_points.add(coord)

    # Build adjacency by processing each wire segment.
    # If any special point lies on the interior of a wire, split the wire at that point.
    adjacency: dict[Coord, set[Coord]] = defaultdict(set)

    for wire in schematic.wires:
        start = _to_coord(*wire.start)
        end = _to_coord(*wire.end)

        # Find special points on this wire's interior
        on_wire = [p for p in special_points if _point_on_segment(p, start, end)]

        if not on_wire:
            # Simple case: no splits needed
            adjacency[start].add(end)
            adjacency[end].add(start)
        else:
            # Sort split points by distance from start
            on_wire.sort(
                key=lambda p: (p[0] - start[0]) ** 2 + (p[1] - start[1]) ** 2
            )
            # Create chain: start -> p1 -> p2 -> ... -> end
            chain = [start] + on_wire + [end]
            for i in range(len(chain) - 1):
                adjacency[chain[i]].add(chain[i + 1])
                adjacency[chain[i + 1]].add(chain[i])

    # Ensure all special points exist as nodes even if no wire touches them
    for p in special_points:
        if p not in adjacency:
            adjacency[p] = set()

    return dict(adjacency), net_names


def _flood_fill_net(
    start: Coord,
    adjacency: dict[Coord, set[Coord]],
    net_names: dict[Coord, str],
) -> str | None:
    """BFS from a coordinate to find the first net name reachable along wires.

    Returns the net name, or None if no label/power symbol is reachable.
    """
    visited: set[Coord] = set()
    queue = [start]
    visited.add(start)

    while queue:
        current = queue.pop(0)

        # Check if this node has a net name
        if current in net_names:
            return net_names[current]

        # Traverse neighbors
        for neighbor in adjacency.get(current, set()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    return None


def resolve_pin_map(
    schematic: Schematic,
    ref_filter: str | None = None,
) -> dict[str, dict]:
    """Resolve pin-to-net assignments for all symbols in a schematic.

    Args:
        schematic: Loaded schematic
        ref_filter: If provided, only include this reference designator

    Returns:
        Dict mapping reference -> {"lib_id": str, "pins": {pin_num: {name, net, type}}}
    """
    adjacency, net_names = _build_wire_graph(schematic)
    result: dict[str, dict] = {}

    for symbol in schematic.symbols:
        # Skip power symbols
        if symbol.lib_id.startswith("power:"):
            continue

        # Apply reference filter
        if ref_filter and symbol.reference != ref_filter:
            continue

        # Look up embedded library symbol
        lib_sexp = schematic.get_lib_symbol(symbol.lib_id)
        if not lib_sexp:
            continue

        lib_sym = LibrarySymbol.from_sexp(lib_sexp)

        # Get transformed pin positions
        pin_positions = lib_sym.get_all_pin_positions(
            instance_pos=symbol.position,
            instance_rot=symbol.rotation,
            mirror=symbol.mirror,
        )

        pins_data: dict[str, dict] = {}
        for lib_pin in lib_sym.pins:
            if lib_pin.number not in pin_positions:
                continue

            pos = pin_positions[lib_pin.number]
            coord = _to_coord(*pos)

            # Trace from pin position to find net name
            net = _flood_fill_net(coord, adjacency, net_names)

            pins_data[lib_pin.number] = {
                "name": lib_pin.name,
                "net": net,
                "type": lib_pin.type,
                "position": [round(pos[0], 4), round(pos[1], 4)],
            }

        # Handle multi-unit: if the same reference already exists, merge pins
        if symbol.reference in result:
            result[symbol.reference]["pins"].update(pins_data)
        else:
            result[symbol.reference] = {
                "lib_id": symbol.lib_id,
                "pins": pins_data,
            }

    return result


def output_json(pin_map: dict[str, dict]) -> None:
    """Output pin map as JSON."""
    print(json.dumps(pin_map, indent=2))


def output_table(pin_map: dict[str, dict]) -> None:
    """Output pin map as formatted table."""
    if not pin_map:
        print("No symbols found.")
        return

    for ref in sorted(pin_map.keys()):
        entry = pin_map[ref]
        print(f"\n{ref} ({entry['lib_id']})")
        print(f"  {'Pin':<6} {'Name':<20} {'Type':<15} {'Position':<22} {'Net'}")
        print("  " + "-" * 80)

        pins = entry["pins"]
        for pin_num in sorted(
            pins.keys(),
            key=lambda p: (not p.isdigit(), int(p) if p.isdigit() else 0, p),
        ):
            pin = pins[pin_num]
            net_str = pin["net"] if pin["net"] else "(unconnected)"
            pos = pin.get("position")
            pos_str = f"({pos[0]:.2f}, {pos[1]:.2f})" if pos else ""
            print(f"  {pin_num:<6} {pin['name']:<20} {pin['type']:<15} {pos_str:<22} {net_str}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Show resolved pin-to-net assignments for schematic symbols",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--ref", help="Filter by symbol reference (e.g., U1)")
    parser.add_argument(
        "--format", choices=["table", "json"], default="json", help="Output format (default: json)"
    )

    args = parser.parse_args(argv)

    # Load schematic
    try:
        sch = Schematic.load(args.schematic)
    except (FileNotFoundError, KiCadFileNotFoundError):
        print(f"Error: Schematic not found: {args.schematic}", file=sys.stderr)
        return 1

    # Resolve pin map
    pin_map = resolve_pin_map(sch, ref_filter=args.ref)

    if args.format == "json":
        output_json(pin_map)
    else:
        output_table(pin_map)

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
