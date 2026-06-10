"""Path manipulation utilities for routing.

This module provides utilities for:
- Intra-IC routing (connecting same-component pins on the same net)
- Route length calculation
- Path analysis
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Pad, Route
    from .rules import NetClassRouting

from .primitives import Route, Segment


def _get_trace_width_for_net(
    net_name: str,
    rules,
    net_class_map: dict[str, NetClassRouting] | None = None,
) -> float:
    """Get the trace width for a net based on its net class.

    Args:
        net_name: Name of the net
        rules: Design rules with trace_width default
        net_class_map: Optional mapping of net names to NetClassRouting

    Returns:
        Trace width in mm
    """
    if net_class_map and net_name in net_class_map:
        return net_class_map[net_name].trace_width
    return rules.trace_width


def create_intra_ic_routes(
    net: int,
    pads: list[tuple[str, str]],
    pad_lookup: dict[tuple[str, str], Pad],
    rules,
    net_class_map: dict[str, NetClassRouting] | None = None,
) -> tuple[list[Route], set[int]]:
    """Create direct routes for same-IC pins on the same net.

    For pins on the same IC that share a net (e.g., U10 pins 1,3,4 on SYNC_L),
    create direct short segments connecting them. This bypasses the A* router
    for these tight connections where blocking areas overlap.

    Args:
        net: Net ID
        pads: List of (ref, pin) tuples for this net
        pad_lookup: Dictionary mapping (ref, pin) to Pad objects
        rules: Design rules with trace_width
        net_class_map: Optional net class map for per-net trace widths

    Returns:
        Tuple of (routes created, set of pad indices that were connected)
    """
    routes: list[Route] = []
    connected_indices: set[int] = set()

    # Group pads by component reference
    by_ref: dict[str, list[int]] = {}
    for i, (ref, _pin) in enumerate(pads):
        if ref not in by_ref:
            by_ref[ref] = []
        by_ref[ref].append(i)

    # For each component with multiple same-net pins, create direct connections
    for ref, indices in by_ref.items():
        if len(indices) < 2:
            continue

        # Get pad objects
        pad_objs = [pad_lookup[pads[i]] for i in indices]
        net_name = pad_objs[0].net_name

        # Connect all pads on this component with short stubs
        # Use chain topology: pad0 -> pad1 -> pad2 -> ...
        # Sort by position to get sensible ordering
        sorted_pairs = sorted(zip(indices, pad_objs, strict=False), key=lambda p: (p[1].x, p[1].y))

        for j in range(len(sorted_pairs) - 1):
            idx1, pad1 = sorted_pairs[j][:2]
            idx2, pad2 = sorted_pairs[j + 1][:2]

            # Create a direct segment between these pads
            # Check distance - only do this for close pins (< 3mm)
            # SOT-23-5 is ~2.5mm wide, TSSOP pins can be ~2mm apart
            dist = math.sqrt((pad2.x - pad1.x) ** 2 + (pad2.y - pad1.y) ** 2)
            if dist > 3.0:
                continue  # Too far apart, let normal router handle it

            # Create route with single segment
            # Issue #1543: Use net-class-aware trace width
            trace_width = _get_trace_width_for_net(net_name, rules, net_class_map)
            route = Route(net=net, net_name=net_name)
            seg = Segment(
                x1=pad1.x,
                y1=pad1.y,
                x2=pad2.x,
                y2=pad2.y,
                width=trace_width,
                layer=pad1.layer,  # Use pad layer (typically F.Cu for SMD)
                net=net,
                net_name=net_name,
            )
            route.segments.append(seg)
            routes.append(route)

            # Mark these pads as connected
            connected_indices.add(idx1)
            connected_indices.add(idx2)

            print(f"  Intra-IC route: {ref} pins {pads[idx1][1]}->{pads[idx2][1]} ({dist:.2f}mm)")

    return routes, connected_indices


def reduce_pads_after_intra_ic(
    pads: list[tuple[str, str]],
    connected_indices: set[int],
    pad_lookup: dict[tuple[str, str], Pad] | None = None,
) -> list[tuple[str, str]]:
    """Build reduced pad list after intra-IC routing.

    Groups connected pads by component and returns one representative
    per group plus all unconnected pads.

    Representative selection (issue #3410): when ``pad_lookup`` is
    provided, the representative of each intra-IC group is the member
    CLOSEST to the centroid of the net's pads OUTSIDE that component.
    The previous "first pad in the group" choice was arbitrary and could
    hand the MST an edge endpoint that faces AWAY from every other pad
    on the net.  Board 03's USB-C made this concrete: J1's USB_D- tie
    group {A7 (front row, boxed in by neighboring escape lanes), B7
    (south row, open escape)} was represented by A7, so the A* had to
    thread the J1 escape belt from the worst possible start cell and
    failed with BLOCKED_BY_COMPONENT even on an empty board.  Picking
    the externally-facing member (B7) gives the pathfinder a start pad
    whose escape direction already points at the rest of the net.

    Args:
        pads: Original list of (ref, pin) tuples
        connected_indices: Set of pad indices connected by intra-IC routing
        pad_lookup: Optional mapping of (ref, pin) -> Pad used for the
            target-aware representative choice.  When omitted (legacy
            callers), the first group member is used as before.

    Returns:
        Reduced list of pads for inter-IC routing
    """
    if not connected_indices:
        return pads

    # Group connected pads by their component reference
    ref_to_indices: dict[str, list[int]] = {}
    for i in connected_indices:
        ref = pads[i][0]
        if ref not in ref_to_indices:
            ref_to_indices[ref] = []
        ref_to_indices[ref].append(i)

    def _pick_representative(ref: str, indices: list[int]) -> int:
        """Pick the group member closest to the net's external centroid."""
        if pad_lookup is None or len(indices) < 2:
            return indices[0]
        # Centroid of all pads NOT on this component (the places the
        # MST will have to route to from this group).
        external = [pad_lookup[p] for p in pads if p[0] != ref and p in pad_lookup]
        if not external:
            return indices[0]
        cx = sum(p.x for p in external) / len(external)
        cy = sum(p.y for p in external) / len(external)
        best = indices[0]
        best_d = float("inf")
        for i in indices:
            pad = pad_lookup.get(pads[i])
            if pad is None:
                continue
            d = (pad.x - cx) ** 2 + (pad.y - cy) ** 2
            if d < best_d:
                best_d = d
                best = i
        return best

    # Create reduced pads list: one representative per connected group + unconnected pads
    reduced_pad_indices: list[int] = []
    for ref, indices in ref_to_indices.items():
        reduced_pad_indices.append(_pick_representative(ref, indices))

    # Add pads that weren't connected intra-IC
    for i in range(len(pads)):
        if i not in connected_indices:
            reduced_pad_indices.append(i)

    return [pads[i] for i in reduced_pad_indices]


def calculate_route_length(routes: list[Route]) -> float:
    """Calculate total length of all segments in routes.

    Args:
        routes: List of Route objects

    Returns:
        Total length in mm
    """
    total_length = 0.0
    for route in routes:
        for seg in route.segments:
            dx = seg.x2 - seg.x1
            dy = seg.y2 - seg.y1
            total_length += math.sqrt(dx * dx + dy * dy)
    return total_length


def count_vias(routes: list[Route]) -> int:
    """Count total number of vias in routes.

    Args:
        routes: List of Route objects

    Returns:
        Total via count
    """
    return sum(len(r.vias) for r in routes)
