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

from kicad_tools.cli.sch_connectivity import (
    Coord,
    _point_on_segment,  # noqa: F401  # re-exported for tests/test_sch_pin_map.py
    build_wire_graph,
    flood_fill_net,
    propagate_net_names,
    snap_coord,
    to_coord,
)
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import Schematic
from kicad_tools.schema.hierarchy import build_hierarchy

# Keep private aliases for backward compatibility (tests import these names).
_to_coord = to_coord
_snap_coord = snap_coord
_build_wire_graph = build_wire_graph
_propagate_net_names = propagate_net_names
_flood_fill_net = flood_fill_net


def resolve_pin_map(
    schematic: Schematic,
    ref_filter: str | None = None,
) -> dict[str, dict]:
    """Resolve pin-to-net assignments for all symbols in a schematic.

    Args:
        schematic: Loaded schematic
        ref_filter: If provided, only include this reference designator

    Returns:
        Dict mapping reference -> {"lib_id": str, "pins": {pin_num: {name, net, connected, type, position}}}

        The ``connected`` field is True when the pin touches at least one wire
        in the schematic, False when the pin is truly floating (no wire at all).

        Pins on unnamed local nets (wired together but no label) receive a
        synthetic net name like ``_local_1``, ``_local_2``, etc.  Pins that are
        truly floating have ``net: None`` and ``connected: False``.
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
    # Pin coordinates belonging to net-tie symbols (Device:NetTie_2,
    # Device:NetTie_3, etc.).  Net-ties are transparent wire bridges --
    # their pins should NOT act as BFS barriers so that tracing can
    # cross through them to reach the net label on the other side.
    # NOTE: This covers the standard KiCad library net-ties whose lib_id
    # starts with "Device:NetTie_".  Custom net-tie symbols in user
    # libraries would need a different detection heuristic.
    nettie_pin_coords: set[Coord] = set()
    # Virtual edges to add between all pins of each net-tie symbol.
    # A net-tie's body bridges its pins electrically, but there is no
    # explicit wire segment between them in the schematic.  We add
    # adjacency edges so BFS can traverse through the net-tie body.
    nettie_edges: list[tuple[Coord, Coord]] = []

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

        # Detect net-tie symbols by lib_id prefix
        if symbol.lib_id.startswith("Device:NetTie_"):
            nettie_pin_coords.update(coords)
            # Create virtual edges between all pin pairs of this net-tie
            coord_list = list(coords)
            for i in range(len(coord_list)):
                for j in range(i + 1, len(coord_list)):
                    nettie_edges.append((coord_list[i], coord_list[j]))

        _sym_cache.append((symbol, lib_sym, pin_positions))

    # --- Build wire graph with pin coordinates as split points ---
    adjacency, net_names = _build_wire_graph(schematic, extra_points=all_pin_coords)

    # Inject virtual edges for net-tie pin pairs so BFS can traverse
    # through the net-tie body (which has no explicit wire in the schematic).
    for a, b in nettie_edges:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

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

        # Barrier = all pin coords except this symbol's own pins and net-tie pins.
        # Net-tie pins are excluded so BFS can traverse through net-tie bodies,
        # which act as transparent wire bridges between nets/zones.
        barrier_pins = all_pin_coords - ref_pin_coords[symbol.reference] - nettie_pin_coords

        pins_data: dict[str, dict] = {}
        for lib_pin in lib_sym.pins:
            if lib_pin.number not in pin_positions:
                continue

            pos = pin_positions[lib_pin.number]
            coord = _to_coord(*pos)

            # Trace from pin position to find net name
            net = _flood_fill_net(coord, adjacency, net_names, barrier_pins)

            # Determine connectivity: a pin is "connected" if it has any
            # neighbors in the wire adjacency graph (i.e., a wire touches it).
            snapped = _snap_coord(coord, set(adjacency.keys()))
            has_neighbors = bool(adjacency.get(snapped, set()))
            connected = net is not None or has_neighbors

            pins_data[lib_pin.number] = {
                "name": lib_pin.name,
                "net": net,
                "connected": connected,
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

    # ------------------------------------------------------------------
    # Post-pass: assign synthetic net names to unnamed local nets.
    #
    # Pins with net=None but connected=True are on unnamed local nets.
    # Group them by connected component using BFS on the adjacency graph,
    # then assign _local_1, _local_2, etc. to each group.
    # ------------------------------------------------------------------
    # Collect all (ref, pin_num, coord) tuples for unresolved-but-connected pins
    unresolved_coords: dict[Coord, list[tuple[str, str]]] = defaultdict(list)
    for ref, entry in result.items():
        for pin_num, pin_data in entry["pins"].items():
            if pin_data["net"] is None and pin_data["connected"]:
                pos = pin_data["position"]
                coord = _to_coord(pos[0], pos[1])
                snapped = _snap_coord(coord, set(adjacency.keys()))
                unresolved_coords[snapped].append((ref, pin_num))

    # BFS to find connected components among unresolved pin coordinates
    visited: set[Coord] = set()
    local_counter = 0

    for start_coord in unresolved_coords:
        if start_coord in visited:
            continue

        # BFS from this coordinate through the wire graph
        component_coords: set[Coord] = set()
        queue = [start_coord]
        visited.add(start_coord)

        while queue:
            current = queue.pop(0)
            if current in unresolved_coords:
                component_coords.add(current)
            for neighbor in adjacency.get(current, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)

        # Only assign a synthetic name if there are pins in this component
        if component_coords:
            local_counter += 1
            synthetic_name = f"_local_{local_counter}"
            for coord in component_coords:
                for ref, pin_num in unresolved_coords[coord]:
                    result[ref]["pins"][pin_num]["net"] = synthetic_name

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
            if pin["net"]:
                net_str = pin["net"]
            elif pin.get("connected"):
                net_str = "(unnamed)"
            else:
                net_str = "(floating)"
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
    parser.add_argument("--sheet", help="Restrict to a specific sheet (name or path substring)")
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
