"""Shared schematic connectivity primitives.

Provides coordinate conversion, snap-tolerance matching, wire-graph
construction, and BFS-based net-name resolution.  Used by
``sch_check_connections``, ``sch_find_unconnected``, and ``sch_pin_map``.
"""

from __future__ import annotations

from collections import defaultdict

from kicad_tools.schema import Schematic
from kicad_tools.schema.label import PowerSymbol

# ---------------------------------------------------------------------------
# Integer-scaled coordinate helpers
# ---------------------------------------------------------------------------

Coord = tuple[int, int]


def to_coord(x: float, y: float) -> Coord:
    """Convert float mm coordinates to integer-scaled (x*10, y*10)."""
    return (int(round(x * 10)), int(round(y * 10)))


def snap_coord(coord: Coord, known: set[Coord], tolerance: int = 1) -> Coord:
    """Return the nearest node in *known* if within *tolerance*, else *coord*.

    Sub-grid component placement can cause pin positions to round to a
    different integer than the wire endpoint.  This bridges gaps of up to
    *tolerance* units (default 1 = 0.1 mm).
    """
    if coord in known:
        return coord
    cx, cy = coord
    best = coord
    best_dist = tolerance + 1
    for dx in range(-tolerance, tolerance + 1):
        for dy in range(-tolerance, tolerance + 1):
            if dx == 0 and dy == 0:
                continue
            candidate = (cx + dx, cy + dy)
            if candidate in known:
                dist = abs(dx) + abs(dy)
                if dist < best_dist:
                    best = candidate
                    best_dist = dist
    return best


# ---------------------------------------------------------------------------
# Wire-graph construction
# ---------------------------------------------------------------------------


def _point_on_segment(
    point: Coord, seg_start: Coord, seg_end: Coord
) -> bool:
    """Check if *point* lies strictly on the interior of a wire segment."""
    if point == seg_start or point == seg_end:
        return False

    dx1 = seg_end[0] - seg_start[0]
    dy1 = seg_end[1] - seg_start[1]
    dx2 = point[0] - seg_start[0]
    dy2 = point[1] - seg_start[1]
    cross = dx1 * dy2 - dy1 * dx2
    if abs(cross) > 1:
        return False

    min_x = min(seg_start[0], seg_end[0])
    max_x = max(seg_start[0], seg_end[0])
    min_y = min(seg_start[1], seg_end[1])
    max_y = max(seg_start[1], seg_end[1])
    return min_x <= point[0] <= max_x and min_y <= point[1] <= max_y


def build_wire_graph(
    schematic: Schematic,
    extra_points: set[Coord] | None = None,
) -> tuple[dict[Coord, set[Coord]], dict[Coord, str]]:
    """Build a connectivity graph from wires, labels, and power symbols.

    Wire segments are split at label positions, junctions, power-symbol
    positions, wire endpoints of other wires, and any *extra_points* so
    that BFS can traverse through them.

    Returns:
        adjacency: coordinate -> set of connected neighbor coordinates
        net_names: coordinate -> net name (for labels/power symbols)
    """
    net_names: dict[Coord, str] = {}
    special_points: set[Coord] = set()

    for lbl in schematic.labels:
        coord = to_coord(*lbl.position)
        net_names[coord] = lbl.text
        special_points.add(coord)

    for lbl in schematic.global_labels:
        coord = to_coord(*lbl.position)
        net_names[coord] = lbl.text
        special_points.add(coord)

    for lbl in schematic.hierarchical_labels:
        coord = to_coord(*lbl.position)
        net_names[coord] = lbl.text
        special_points.add(coord)

    for junc in schematic.junctions:
        special_points.add(to_coord(*junc.position))

    for sym_sexp in schematic.sexp.find_children("symbol"):
        ps = PowerSymbol.from_symbol_sexp(sym_sexp)
        if ps:
            coord = to_coord(*ps.position)
            net_name = ps.value or ps.lib_id.split(":", 1)[-1]
            net_names[coord] = net_name
            special_points.add(coord)

    if extra_points:
        special_points.update(extra_points)

    for wire in schematic.wires:
        special_points.add(to_coord(*wire.start))
        special_points.add(to_coord(*wire.end))

    adjacency: dict[Coord, set[Coord]] = defaultdict(set)

    for wire in schematic.wires:
        start = to_coord(*wire.start)
        end = to_coord(*wire.end)

        on_wire = [p for p in special_points if _point_on_segment(p, start, end)]

        if not on_wire:
            adjacency[start].add(end)
            adjacency[end].add(start)
        else:
            on_wire.sort(
                key=lambda p: (p[0] - start[0]) ** 2 + (p[1] - start[1]) ** 2
            )
            chain = [start] + on_wire + [end]
            for i in range(len(chain) - 1):
                adjacency[chain[i]].add(chain[i + 1])
                adjacency[chain[i + 1]].add(chain[i])

    for p in special_points:
        if p not in adjacency:
            adjacency[p] = set()

    return dict(adjacency), net_names


def propagate_net_names(
    adjacency: dict[Coord, set[Coord]],
    net_names: dict[Coord, str],
) -> None:
    """Propagate net names from labelled nodes to all wire-connected nodes.

    Mutates *net_names* in place.
    """
    seeds = list(net_names.items())
    for seed_coord, name in seeds:
        visited: set[Coord] = set()
        queue = [seed_coord]
        visited.add(seed_coord)
        while queue:
            current = queue.pop(0)
            if current not in net_names:
                net_names[current] = name
            for neighbor in adjacency.get(current, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)


def flood_fill_net(
    start: Coord,
    adjacency: dict[Coord, set[Coord]],
    net_names: dict[Coord, str],
    barrier_pins: set[Coord] | None = None,
) -> str | None:
    """BFS from *start* to find the first reachable net name.

    *barrier_pins* are coordinates of other components' pins -- BFS
    checks for a net name there but does not continue traversal through
    them.
    """
    if barrier_pins is None:
        barrier_pins = set()

    graph_nodes = set(adjacency.keys())
    start = snap_coord(start, graph_nodes)

    visited: set[Coord] = set()
    queue = [start]
    visited.add(start)

    while queue:
        current = queue.pop(0)
        if current in net_names:
            return net_names[current]
        if current != start and current in barrier_pins:
            continue
        for neighbor in adjacency.get(current, set()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    return None


def is_pin_connected(
    pin_coord: Coord,
    adjacency: dict[Coord, set[Coord]],
) -> bool:
    """Return True if *pin_coord* is reachable from the wire graph.

    A pin is considered connected if its coordinate (after snap) exists
    as a node in the adjacency graph AND has at least one neighbor, OR
    if it has a direct edge in the graph.
    """
    graph_nodes = set(adjacency.keys())
    snapped = snap_coord(pin_coord, graph_nodes)
    if snapped not in adjacency:
        return False
    # The node must have at least one neighbor (i.e. something connects to it)
    return len(adjacency[snapped]) > 0
