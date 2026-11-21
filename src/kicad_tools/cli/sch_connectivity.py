"""Shared schematic connectivity primitives.

Provides coordinate conversion, snap-tolerance matching, wire-graph
construction, and BFS-based net-name resolution.  Used by
``sch_check_connections``, ``sch_find_unconnected``, and ``sch_pin_map``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

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


def _point_on_segment(point: Coord, seg_start: Coord, seg_end: Coord) -> bool:
    """Check if *point* lies strictly on the interior of a wire segment."""
    if point in (seg_start, seg_end):
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

    Connectivity mirrors ``kicad-cli sch export netlist`` (Issue #9833,
    black-box probed on the pinned KiCad 10.0.6 image):

    * wires join at shared endpoints, or where a junction dot sits on a
      wire (including its interior);
    * labels (local, global, hierarchical) attach to a wire anywhere
      along it, so they split the interior;
    * pins and power symbols attach to a wire only at its endpoints:
      their coordinates are graph nodes but never split an interior.
      A T-connection without a dot, a pin or a power symbol on a wire's
      middle is left open by KiCad's netlist.

    This also subsumes the collinear-overlap rule of Issue #9827: a wire
    end lying inside another wire never splits it, perpendicular or not.

    Returns:
        adjacency: coordinate -> set of connected neighbor coordinates
        net_names: coordinate -> net name (for labels/power symbols)
    """
    net_names: dict[Coord, str] = {}
    special_points: set[Coord] = set()
    # Only labels and junction dots connect to a wire anywhere along it,
    # interior included; kicad-cli joins them there.  Wire endpoints,
    # pins and power symbols connect at wire endpoints only.
    mid_wire_connectors: set[Coord] = set()

    for lbl in schematic.labels:
        coord = to_coord(*lbl.position)
        net_names[coord] = lbl.text
        special_points.add(coord)
        mid_wire_connectors.add(coord)

    for lbl in schematic.global_labels:
        coord = to_coord(*lbl.position)
        net_names[coord] = lbl.text
        special_points.add(coord)
        mid_wire_connectors.add(coord)

    for lbl in schematic.hierarchical_labels:
        coord = to_coord(*lbl.position)
        net_names[coord] = lbl.text
        special_points.add(coord)
        mid_wire_connectors.add(coord)

    for junc in schematic.junctions:
        coord = to_coord(*junc.position)
        special_points.add(coord)
        mid_wire_connectors.add(coord)

    for sym_sexp in schematic.sexp.find_children("symbol"):
        ps = PowerSymbol.from_symbol_sexp(sym_sexp)
        if ps:
            coord = to_coord(*ps.position)
            net_name = ps.value or ps.lib_id.split(":", 1)[-1]
            # PWR_FLAG is an ERC annotation, not a real net name.
            # It must not contribute a net name; like every pin or power
            # symbol it connects at wire endpoints only and never splits
            # a wire's interior.
            if net_name == "PWR_FLAG":
                special_points.add(coord)
                continue
            net_names[coord] = net_name
            special_points.add(coord)

    if extra_points:
        special_points.update(extra_points)

    segments: list[tuple[Coord, Coord]] = []
    for wire in schematic.wires:
        start = to_coord(*wire.start)
        end = to_coord(*wire.end)
        segments.append((start, end))
        special_points.add(start)
        special_points.add(end)

    adjacency: dict[Coord, set[Coord]] = defaultdict(set)

    for start, end in segments:
        # Split the interior at labels and junction dots only.  Another
        # wire's endpoint (T-connection, collinear or perpendicular),
        # a pin or a power symbol on the middle does NOT connect there
        # in KiCad's netlist, so it must not split the wire.
        on_wire = [p for p in mid_wire_connectors if _point_on_segment(p, start, end)]

        if not on_wire:
            adjacency[start].add(end)
            adjacency[end].add(start)
        else:
            on_wire.sort(key=lambda p: (p[0] - start[0]) ** 2 + (p[1] - start[1]) ** 2)
            chain = [start] + on_wire + [end]
            for i in range(len(chain) - 1):
                adjacency[chain[i]].add(chain[i + 1])
                adjacency[chain[i + 1]].add(chain[i])

    for p in special_points:
        if p not in adjacency:
            adjacency[p] = set()

    return dict(adjacency), net_names


@dataclass(frozen=True)
class MidWireAttachment:
    """An attachment KiCad's netlist leaves open (Issue #9833).

    kicad-cli joins a wire's interior only at a junction dot or a label.
    A wire endpoint meeting another wire's middle (a T-connection
    without a dot), a pin or a power symbol on a wire's middle is open
    in the exported netlist, so :func:`build_wire_graph` no longer
    connects it; report it so ``sch_check_connections`` can warn.
    """

    kind: str  # "undotted_t" | "pin_mid_wire" | "power_mid_wire"
    coord: Coord
    description: str


def find_open_mid_wire_attachments(
    schematic: Schematic,
    extra_points: set[Coord] | None = None,
) -> list[MidWireAttachment]:
    """Find attachments on a wire's interior that KiCad's netlist leaves open.

    *extra_points* are pin coordinates (as passed to
    :func:`build_wire_graph`).  A junction dot or a label at the same
    point anchors the connection in KiCad, so such points are skipped.
    """
    segments = [(to_coord(*w.start), to_coord(*w.end)) for w in schematic.wires]

    # Labels (all three kinds) and junctions anchor a mid-wire point.
    anchored: set[Coord] = set()
    for lbl in schematic.labels:
        anchored.add(to_coord(*lbl.position))
    for glbl in schematic.global_labels:
        anchored.add(to_coord(*glbl.position))
    for hlbl in schematic.hierarchical_labels:
        anchored.add(to_coord(*hlbl.position))
    for junc in schematic.junctions:
        anchored.add(to_coord(*junc.position))

    def _on_interior(point: Coord) -> bool:
        return any(_point_on_segment(point, s, e) for s, e in segments)

    def _fmt(point: Coord) -> str:
        return f"({point[0] / 10:g}, {point[1] / 10:g})"

    wire_endpoints: set[Coord] = set()
    for start, end in segments:
        wire_endpoints.add(start)
        wire_endpoints.add(end)

    # A point within snap tolerance of a wire endpoint is joined by the
    # connectivity tools (sub-grid placement, e.g. a pin at 69.8500000000001
    # against a wire end serialized as 69.85); KiCad stores full precision
    # and joins them too, so it must not be reported as open.
    def _snaps_to_wire_endpoint(point: Coord) -> bool:
        if point in wire_endpoints:
            # snap_coord returns an exact member unchanged, so look for a
            # *different* endpoint within tolerance.
            return any(
                (point[0] + dx, point[1] + dy) in wire_endpoints
                for dx in range(-1, 2)
                for dy in range(-1, 2)
                if (dx, dy) != (0, 0)
            )
        return snap_coord(point, wire_endpoints) != point

    found: dict[tuple[str, Coord], MidWireAttachment] = {}

    for p in wire_endpoints:
        if p in anchored or _snaps_to_wire_endpoint(p) or not _on_interior(p):
            continue
        found[("undotted_t", p)] = MidWireAttachment(
            kind="undotted_t",
            coord=p,
            description=(
                f"wire endpoint {_fmt(p)} meets another wire's middle "
                "with no junction dot; the KiCad netlist treats it as open"
            ),
        )

    for p in extra_points or ():
        if p in anchored or p in wire_endpoints or _snaps_to_wire_endpoint(p):
            continue
        if not _on_interior(p):
            continue
        found[("pin_mid_wire", p)] = MidWireAttachment(
            kind="pin_mid_wire",
            coord=p,
            description=(
                f"pin {_fmt(p)} lies on a wire's middle with no junction "
                "dot; the KiCad netlist leaves it unconnected"
            ),
        )

    for sym_sexp in schematic.sexp.find_children("symbol"):
        ps = PowerSymbol.from_symbol_sexp(sym_sexp)
        if not ps:
            continue
        p = to_coord(*ps.position)
        if p in anchored or p in wire_endpoints or _snaps_to_wire_endpoint(p):
            continue
        if not _on_interior(p):
            continue
        found[("power_mid_wire", p)] = MidWireAttachment(
            kind="power_mid_wire",
            coord=p,
            description=(
                f"power symbol {_fmt(p)} lies on a wire's middle with no "
                "junction dot; the KiCad netlist leaves it unconnected"
            ),
        )

    return list(found.values())


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
