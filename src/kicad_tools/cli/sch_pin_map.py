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
import os
import sys
from collections import defaultdict

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import Schematic
from kicad_tools.schema.hierarchy import build_hierarchy
from kicad_tools.schema.label import PowerSymbol

# Use integer-scaled coordinates for fast set lookup (matches sch_check_connections.py)
Coord = tuple[int, int]


def _to_coord(x: float, y: float) -> Coord:
    """Convert float coordinates to integer-scaled for set lookup."""
    return (int(round(x * 10)), int(round(y * 10)))


def _snap_coord(coord: Coord, known: set[Coord], tolerance: int = 1) -> Coord:
    """Return the nearest node in *known* if within *tolerance*, else *coord* unchanged.

    Sub-grid component placement can cause pin positions computed from
    ``instance_pos + lib_pin_offset`` to round to a different integer than
    the wire endpoint parsed directly from the schematic.  This function
    bridges a gap of up to *tolerance* units (default 1 = 0.1 mm) so that
    the pin still matches the wire graph.
    """
    if coord in known:
        return coord
    cx, cy = coord
    best = coord
    best_dist = tolerance + 1  # sentinel > tolerance
    for dx in range(-tolerance, tolerance + 1):
        for dy in range(-tolerance, tolerance + 1):
            if dx == 0 and dy == 0:
                continue
            candidate = (cx + dx, cy + dy)
            if candidate in known:
                dist = abs(dx) + abs(dy)  # Manhattan distance
                if dist < best_dist:
                    best = candidate
                    best_dist = dist
    return best


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
    extra_points: set[Coord] | None = None,
) -> tuple[dict[Coord, set[Coord]], dict[Coord, str]]:
    """Build a connectivity graph from wires, labels, and power symbols.

    Labels and other connection points may sit on the interior of a wire
    segment (not just at endpoints). We split wire edges at such points
    so that flood-fill can reach them.

    Args:
        schematic: Loaded schematic
        extra_points: Additional coordinates (e.g., component pin positions)
            that should be treated as graph nodes. Wire segments passing
            through these points will be split so BFS can traverse them.

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

    # Include caller-supplied points (e.g., component pin positions) so that
    # wire segments are split at those locations and BFS can start there.
    if extra_points:
        special_points.update(extra_points)

    # Also include all wire endpoints as split points.  When one wire's
    # endpoint lands on the interior of another wire (e.g., a T-junction
    # without an explicit junction symbol), the second wire must be split
    # at that point so BFS can traverse between them.
    for wire in schematic.wires:
        special_points.add(_to_coord(*wire.start))
        special_points.add(_to_coord(*wire.end))

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


def _propagate_net_names(
    adjacency: dict[Coord, set[Coord]],
    net_names: dict[Coord, str],
) -> None:
    """Propagate net names from labelled nodes to all wire-connected nodes.

    For each coordinate that already has a net name (from a label or power
    symbol), run an unrestricted BFS across the wire graph and assign the
    same net name to every reachable node that does not yet have one.

    This is safe because KiCad wires form disjoint nets -- two different
    net labels connected by a wire would be an ERC error in the schematic.
    The function mutates *net_names* in place.
    """
    # Snapshot the set of seed coordinates; we will add entries to net_names
    # during iteration, so iterate over a copy.
    seeds = list(net_names.items())

    for seed_coord, name in seeds:
        # BFS from seed_coord through the full wire graph (no barriers).
        visited: set[Coord] = set()
        queue = [seed_coord]
        visited.add(seed_coord)

        while queue:
            current = queue.pop(0)
            # Assign net name if this node doesn't have one yet.
            if current not in net_names:
                net_names[current] = name
            for neighbor in adjacency.get(current, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)


def _flood_fill_net(
    start: Coord,
    adjacency: dict[Coord, set[Coord]],
    net_names: dict[Coord, str],
    barrier_pins: set[Coord] | None = None,
) -> str | None:
    """BFS from a coordinate to find the first net name reachable along wires.

    Args:
        start: Starting coordinate (a pin position of the component being traced).
        adjacency: Wire connectivity graph.
        net_names: Map of coordinates to net names (labels/power symbols).
        barrier_pins: Pin coordinates of *other* components.  When the BFS
            reaches a barrier pin it records any net name but does **not**
            continue traversal through that node.  This prevents the flood
            fill from crossing through another component's body (e.g. tracing
            from one pin of a resistor through a wire that happens to
            connect its other pin to a different net).

    Returns the net name, or None if no label/power symbol is reachable.
    """
    if barrier_pins is None:
        barrier_pins = set()

    # Snap start to the nearest graph node if sub-grid rounding caused a
    # 1-unit mismatch between pin position and wire endpoint.
    graph_nodes = set(adjacency.keys())
    start = _snap_coord(start, graph_nodes)

    visited: set[Coord] = set()
    queue = [start]
    visited.add(start)

    while queue:
        current = queue.pop(0)

        # Check if this node has a net name
        if current in net_names:
            return net_names[current]

        # If this node is a pin of another component, do not traverse further
        # from it.  The pin is a terminal -- the net stops here.  We already
        # checked for a net name above, so unnamed junctions at foreign pins
        # simply become dead-ends for BFS.
        if current != start and current in barrier_pins:
            continue

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
        Dict mapping reference -> {"lib_id": str, "pins": {pin_num: {name, net, type, position}}}
    """
    result: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # First pass: collect all component pin positions.
    # We need these BEFORE building the wire graph so that wire segments
    # are split at pin coordinates, making them reachable by BFS.
    # We also build per-component barrier sets: a barrier set for symbol S
    # contains every pin coordinate that does NOT belong to S.  This
    # prevents the BFS from traversing *through* another component's
    # body (i.e. hopping from pin 1 to pin 2 of an intermediate
    # resistor/diode via a wire that connects them in the schematic
    # drawing).
    # ------------------------------------------------------------------
    # Map: reference -> set of Coord for that symbol's pins
    ref_pin_coords: dict[str, set[Coord]] = defaultdict(set)
    # All non-power pin coordinates across the whole schematic
    all_pin_coords: set[Coord] = set()

    # We also cache resolved LibrarySymbol + pin_positions per symbol
    # instance so we don't have to compute them twice.
    _sym_cache: list[tuple] = []  # (symbol, lib_sym, pin_positions)

    for symbol in schematic.symbols:
        if symbol.lib_id.startswith("power:"):
            continue

        lib_sym = schematic.get_lib_symbol_resolved(symbol.lib_id)
        if not lib_sym:
            continue
        pin_positions = lib_sym.get_all_pin_positions(
            instance_pos=symbol.position,
            instance_rot=symbol.rotation,
            mirror=symbol.mirror,
        )

        coords: set[Coord] = set()
        for pin_num, pos in pin_positions.items():
            coords.add(_to_coord(*pos))
        ref_pin_coords[symbol.reference].update(coords)
        all_pin_coords.update(coords)
        _sym_cache.append((symbol, lib_sym, pin_positions))

    # --- Build wire graph with pin coordinates as split points ---
    adjacency, net_names = _build_wire_graph(schematic, extra_points=all_pin_coords)

    # ------------------------------------------------------------------
    # Pre-pass: propagate net names to all wire-connected nodes.
    #
    # Labels/power symbols define net names at specific coordinates, but
    # barrier pins may sit between a component's pin and the label.  When
    # the per-component BFS reaches a barrier pin it checks for a net name
    # at that coordinate but does NOT continue traversal.  If the label is
    # on the far side of the barrier, the net name is never found.
    #
    # Fix: run an unrestricted BFS from every labelled node and assign the
    # net name to every reachable node in the wire graph.  This ensures
    # barrier pins that are electrically on the same wire as a label will
    # already have the net name recorded before per-component BFS runs.
    # ------------------------------------------------------------------
    _propagate_net_names(adjacency, net_names)

    # ------------------------------------------------------------------
    # Second pass: resolve nets for each symbol using per-component barrier sets.
    # ------------------------------------------------------------------
    for symbol, lib_sym, pin_positions in _sym_cache:
        # Apply reference filter
        if ref_filter and symbol.reference != ref_filter:
            continue

        # Barrier = all pin coords except this symbol's own pins
        barrier_pins = all_pin_coords - ref_pin_coords[symbol.reference]

        pins_data: dict[str, dict] = {}
        for lib_pin in lib_sym.pins:
            if lib_pin.number not in pin_positions:
                continue

            pos = pin_positions[lib_pin.number]
            coord = _to_coord(*pos)

            # Trace from pin position to find net name
            net = _flood_fill_net(coord, adjacency, net_names, barrier_pins)

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
                "value": symbol.value,
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
        "--sheet", help="Restrict to a specific sheet (name or path substring)"
    )
    parser.add_argument(
        "--format", choices=["table", "json"], default="json", help="Output format (default: json)"
    )

    args = parser.parse_args(argv)

    # Verify root schematic exists before building hierarchy
    if not os.path.isfile(args.schematic):
        print(f"Error: Schematic not found: {args.schematic}", file=sys.stderr)
        return 1

    # Build hierarchy to iterate all sheets (root + children)
    hierarchy = build_hierarchy(args.schematic)

    # Iterate all sheets, merging pin maps
    pin_map: dict[str, dict] = {}
    for node in hierarchy.all_nodes():
        # Apply --sheet filter: skip nodes whose name or path don't match
        if args.sheet:
            sheet_filter = args.sheet.lower()
            node_name = (node.name or "").lower()
            node_path = (node.path or "").lower()
            if sheet_filter not in node_name and sheet_filter not in node_path:
                continue

        try:
            sch = Schematic.load(node.path)
        except (FileNotFoundError, KiCadFileNotFoundError):
            continue

        sheet_map = resolve_pin_map(sch, ref_filter=args.ref)

        # Merge results -- multi-unit symbols may span sheets
        for ref, entry in sheet_map.items():
            if ref in pin_map:
                pin_map[ref]["pins"].update(entry["pins"])
            else:
                pin_map[ref] = entry

    if args.format == "json":
        output_json(pin_map)
    else:
        output_table(pin_map)

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
