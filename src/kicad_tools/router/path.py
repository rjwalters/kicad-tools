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

from .primitives import Route, Segment


def create_intra_ic_routes(
    net: int,
    pads: list[tuple[str, str]],
    pad_lookup: dict[tuple[str, str], Pad],
    rules,
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
            route = Route(net=net, net_name=net_name)
            seg = Segment(
                x1=pad1.x,
                y1=pad1.y,
                x2=pad2.x,
                y2=pad2.y,
                width=rules.trace_width,
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
) -> list[tuple[str, str]]:
    """Build reduced pad list after intra-IC routing.

    Groups connected pads by component and returns one representative
    per group plus all unconnected pads.

    Args:
        pads: Original list of (ref, pin) tuples
        connected_indices: Set of pad indices connected by intra-IC routing

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

    # Create reduced pads list: one representative per connected group + unconnected pads
    reduced_pad_indices: list[int] = []
    for indices in ref_to_indices.values():
        # Use first pad from each intra-IC group as representative
        reduced_pad_indices.append(indices[0])

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


def calculate_total_wirelength(routes: list[Route]) -> float:
    """Calculate total wirelength including all segments.

    Args:
        routes: List of Route objects

    Returns:
        Total wirelength in mm
    """
    return sum(
        math.sqrt((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) for r in routes for s in r.segments
    )


def count_vias(routes: list[Route]) -> int:
    """Count total number of vias in routes.

    Args:
        routes: List of Route objects

    Returns:
        Total via count
    """
    return sum(len(r.vias) for r in routes)
