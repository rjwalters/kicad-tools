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


def _get_clearance_for_net(
    net_name: str,
    rules,
    net_class_map: dict[str, NetClassRouting] | None = None,
) -> float:
    """Get the active copper clearance for a net (net class override or rules).

    Args:
        net_name: Name of the net
        rules: Design rules with trace_clearance default
        net_class_map: Optional mapping of net names to NetClassRouting

    Returns:
        Clearance in mm
    """
    if net_class_map and net_name in net_class_map:
        return net_class_map[net_name].clearance
    return getattr(rules, "trace_clearance", 0.2)


def _point_segment_distance(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    """Distance from point (px, py) to segment (x1,y1)-(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _segments_intersect(
    ax1: float,
    ay1: float,
    ax2: float,
    ay2: float,
    bx1: float,
    by1: float,
    bx2: float,
    by2: float,
) -> bool:
    """True if segments A and B intersect (including touching)."""

    def orient(ox: float, oy: float, px: float, py: float, qx: float, qy: float) -> float:
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)

    d1 = orient(bx1, by1, bx2, by2, ax1, ay1)
    d2 = orient(bx1, by1, bx2, by2, ax2, ay2)
    d3 = orient(ax1, ay1, ax2, ay2, bx1, by1)
    d4 = orient(ax1, ay1, ax2, ay2, bx2, by2)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True

    def on_seg(ox: float, oy: float, px: float, py: float, qx: float, qy: float) -> bool:
        return min(ox, px) <= qx <= max(ox, px) and min(oy, py) <= qy <= max(oy, py)

    if d1 == 0 and on_seg(bx1, by1, bx2, by2, ax1, ay1):
        return True
    if d2 == 0 and on_seg(bx1, by1, bx2, by2, ax2, ay2):
        return True
    if d3 == 0 and on_seg(ax1, ay1, ax2, ay2, bx1, by1):
        return True
    return bool(d4 == 0 and on_seg(ax1, ay1, ax2, ay2, bx2, by2))


def _segment_segment_distance(
    ax1: float,
    ay1: float,
    ax2: float,
    ay2: float,
    bx1: float,
    by1: float,
    bx2: float,
    by2: float,
) -> float:
    """Minimum distance between two segments (0 if they intersect)."""
    if _segments_intersect(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2):
        return 0.0
    return min(
        _point_segment_distance(ax1, ay1, bx1, by1, bx2, by2),
        _point_segment_distance(ax2, ay2, bx1, by1, bx2, by2),
        _point_segment_distance(bx1, by1, ax1, ay1, ax2, ay2),
        _point_segment_distance(bx2, by2, ax1, ay1, ax2, ay2),
    )


def _segment_rect_distance(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    rminx: float,
    rminy: float,
    rmaxx: float,
    rmaxy: float,
) -> float:
    """Minimum distance from a segment centerline to an axis-aligned rect.

    Returns 0 if the segment touches or passes through the rectangle.
    """
    # Endpoint inside the rect -> distance 0
    if rminx <= x1 <= rmaxx and rminy <= y1 <= rmaxy:
        return 0.0
    if rminx <= x2 <= rmaxx and rminy <= y2 <= rmaxy:
        return 0.0
    edges = (
        (rminx, rminy, rmaxx, rminy),
        (rmaxx, rminy, rmaxx, rmaxy),
        (rmaxx, rmaxy, rminx, rmaxy),
        (rminx, rmaxy, rminx, rminy),
    )
    return min(
        _segment_segment_distance(x1, y1, x2, y2, ex1, ey1, ex2, ey2)
        for ex1, ey1, ex2, ey2 in edges
    )


def _pad_blocks_layer(pad: Pad, layer) -> bool:
    """True if ``pad`` occupies copper on ``layer`` (PTH blocks all layers)."""
    return pad.through_hole or pad.layer == layer


def _path_violates(
    points: list[tuple[float, float]],
    width: float,
    layer,
    clearance: float,
    foreign_pads: list[Pad],
    obstacle_segments: list[Segment] | None = None,
) -> bool:
    """Check a polyline (trace centerline) against foreign copper.

    Issue #3480: the intra-IC primitive used to emit segments blindly;
    on a SOT-23-5 the pin1->pin4 diagonal passed 0.027 mm from the GND
    pad.  This validator enforces edge-to-edge clearance against
    foreign pads (and, for fallback wrap paths, previously routed
    segments of other nets).

    Args:
        points: Polyline vertices (trace centerline)
        width: Trace width in mm
        layer: Router layer of the candidate trace
        clearance: Required edge-to-edge clearance in mm
        foreign_pads: Pads on OTHER nets to keep clear of
        obstacle_segments: Optional previously routed segments of other
            nets (same-layer) to keep clear of

    Returns:
        True if any candidate segment violates the clearance
    """
    eps = 1e-6
    need_pad = clearance + width / 2.0 - eps
    for (px1, py1), (px2, py2) in zip(points, points[1:], strict=False):
        for pad in foreign_pads:
            if not _pad_blocks_layer(pad, layer):
                continue
            dist = _segment_rect_distance(
                px1,
                py1,
                px2,
                py2,
                pad.x - pad.width / 2.0,
                pad.y - pad.height / 2.0,
                pad.x + pad.width / 2.0,
                pad.y + pad.height / 2.0,
            )
            if dist < need_pad:
                return True
        if obstacle_segments:
            for seg in obstacle_segments:
                if seg.layer != layer:
                    continue
                need_seg = clearance + width / 2.0 + seg.width / 2.0 - eps
                dist = _segment_segment_distance(px1, py1, px2, py2, seg.x1, seg.y1, seg.x2, seg.y2)
                if dist < need_seg:
                    return True
    return False


def _perimeter_wrap_candidates(
    pad1: Pad,
    pad2: Pad,
    component_pads: list[Pad],
    width: float,
    clearance: float,
) -> list[list[tuple[float, float]]]:
    """Generate package-perimeter wrap paths between two same-package pads.

    Issue #3480: when the direct intra-IC segment violates clearance to a
    foreign pad in the same package (e.g. SOT-23-5 pin1->pin4 wrapping past
    the GND pad), the legal alternative is to wrap AROUND the package pad
    field.  This builds candidate polylines that exit each pad
    perpendicular to the expanded pad-field bounding box and travel along
    the box perimeter (both directions), shortest first.

    Args:
        pad1: Start pad
        pad2: End pad
        component_pads: All pads of the component (for the bounding box)
        width: Trace width in mm
        clearance: Required clearance in mm

    Returns:
        Candidate polylines sorted by length (shortest first)
    """
    margin = clearance + width / 2.0 + 0.05
    minx = min(p.x - p.width / 2.0 for p in component_pads) - margin
    maxx = max(p.x + p.width / 2.0 for p in component_pads) + margin
    miny = min(p.y - p.height / 2.0 for p in component_pads) - margin
    maxy = max(p.y + p.height / 2.0 for p in component_pads) + margin
    w = maxx - minx
    h = maxy - miny
    perimeter = 2.0 * (w + h)

    def perim_t(x: float, y: float) -> float:
        """Perimeter arclength parameter, clockwise from (minx, miny)."""
        if abs(y - miny) < 1e-9:
            return x - minx
        if abs(x - maxx) < 1e-9:
            return w + (y - miny)
        if abs(y - maxy) < 1e-9:
            return w + h + (maxx - x)
        return 2.0 * w + h + (maxy - y)

    corners = [
        (0.0, (minx, miny)),
        (w, (maxx, miny)),
        (w + h, (maxx, maxy)),
        (2.0 * w + h, (minx, maxy)),
    ]

    def walk(t1: float, t2: float, forward: bool) -> list[tuple[float, float]]:
        """Corner waypoints strictly between t1 and t2 along the perimeter."""
        pts: list[tuple[float, float]] = []
        span = (t2 - t1) % perimeter if forward else (t1 - t2) % perimeter
        order = sorted(
            corners,
            key=lambda c: ((c[0] - t1) % perimeter) if forward else ((t1 - c[0]) % perimeter),
        )
        for t, pt in order:
            delta = ((t - t1) % perimeter) if forward else ((t1 - t) % perimeter)
            if 1e-9 < delta < span - 1e-9:
                pts.append(pt)
        return pts

    def exits(pad: Pad) -> list[tuple[float, float]]:
        return [
            (pad.x, miny),
            (pad.x, maxy),
            (minx, pad.y),
            (maxx, pad.y),
        ]

    candidates: list[tuple[float, list[tuple[float, float]]]] = []
    for e1 in exits(pad1):
        for e2 in exits(pad2):
            t1 = perim_t(*e1)
            t2 = perim_t(*e2)
            for forward in (True, False):
                raw = [(pad1.x, pad1.y), e1, *walk(t1, t2, forward), e2, (pad2.x, pad2.y)]
                # Drop consecutive duplicates
                path: list[tuple[float, float]] = []
                for pt in raw:
                    if not path or math.hypot(pt[0] - path[-1][0], pt[1] - path[-1][1]) > 1e-9:
                        path.append(pt)
                if len(path) < 2:
                    continue
                length = sum(
                    math.hypot(b[0] - a[0], b[1] - a[1])
                    for a, b in zip(path, path[1:], strict=False)
                )
                candidates.append((length, path))

    candidates.sort(key=lambda c: c[0])
    # De-duplicate identical paths (forward/backward walks can coincide)
    seen: set[tuple[tuple[float, float], ...]] = set()
    unique: list[list[tuple[float, float]]] = []
    for _length, path in candidates:
        key = tuple(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def create_intra_ic_routes(
    net: int,
    pads: list[tuple[str, str]],
    pad_lookup: dict[tuple[str, str], Pad],
    rules,
    net_class_map: dict[str, NetClassRouting] | None = None,
    obstacle_segments: list[Segment] | None = None,
) -> tuple[list[Route], set[int]]:
    """Create direct routes for same-IC pins on the same net.

    For pins on the same IC that share a net (e.g., U10 pins 1,3,4 on SYNC_L),
    create direct short segments connecting them. This bypasses the A* router
    for these tight connections where blocking areas overlap.

    Issue #3480: emitted segments are validated against the same-package
    foreign pads at the active clearance.  When the direct path violates
    (e.g. the SOT-23-5 pin1->pin4 diagonal passing 0.027 mm from the GND
    pad), a perimeter wrap around the package pad field is tried; if no
    legal wrap exists either, the pair is left for the main A* router.

    Args:
        net: Net ID
        pads: List of (ref, pin) tuples for this net
        pad_lookup: Dictionary mapping (ref, pin) to Pad objects
        rules: Design rules with trace_width
        net_class_map: Optional net class map for per-net trace widths
        obstacle_segments: Optional previously routed segments of other
            nets; perimeter-wrap fallbacks are validated against these so
            the wrap cannot collide with existing copper

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

        # Issue #3480: collect the component's full pad field once --
        # foreign pads (other nets) are clearance obstacles for the
        # direct segment AND any wrap fallback.
        component_pads = [p for (r, _pin), p in pad_lookup.items() if r == ref]
        same_pkg_foreign = [p for p in component_pads if p.net != net]

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

            # Issue #1543: Use net-class-aware trace width
            trace_width = _get_trace_width_for_net(net_name, rules, net_class_map)
            clearance = _get_clearance_for_net(net_name, rules, net_class_map)
            layer = pad1.layer  # Use pad layer (typically F.Cu for SMD)

            direct = [(pad1.x, pad1.y), (pad2.x, pad2.y)]
            path: list[tuple[float, float]] | None = None
            kind = "route"
            if not _path_violates(direct, trace_width, layer, clearance, same_pkg_foreign):
                path = direct
            else:
                # Issue #3480: direct path violates same-package pad
                # clearance -- try a perimeter wrap around the pad field.
                # Wraps leave the package envelope, so validate against
                # ALL foreign pads near the wrap region plus previously
                # routed copper, not just the same package.
                margin = clearance + trace_width / 2.0 + 0.1
                wminx = min(p.x - p.width / 2.0 for p in component_pads) - margin
                wmaxx = max(p.x + p.width / 2.0 for p in component_pads) + margin
                wminy = min(p.y - p.height / 2.0 for p in component_pads) - margin
                wmaxy = max(p.y + p.height / 2.0 for p in component_pads) + margin
                nearby_foreign = [
                    p
                    for p in pad_lookup.values()
                    if p.net != net
                    and p.x + p.width / 2.0 >= wminx - margin
                    and p.x - p.width / 2.0 <= wmaxx + margin
                    and p.y + p.height / 2.0 >= wminy - margin
                    and p.y - p.height / 2.0 <= wmaxy + margin
                ]
                for candidate in _perimeter_wrap_candidates(
                    pad1, pad2, component_pads, trace_width, clearance
                ):
                    if not _path_violates(
                        candidate,
                        trace_width,
                        layer,
                        clearance,
                        nearby_foreign,
                        obstacle_segments=obstacle_segments,
                    ):
                        path = candidate
                        kind = "wrap"
                        break

            if path is None:
                # No legal direct or wrap path: defer this pair to the
                # main A* router (do NOT mark the pads connected).
                print(
                    f"  Intra-IC defer: {ref} pins {pads[idx1][1]}->{pads[idx2][1]} "
                    f"(direct path violates {clearance:.3f}mm pad clearance, "
                    "no legal perimeter wrap)"
                )
                continue

            # Create route from the validated polyline
            route = Route(net=net, net_name=net_name)
            for (sx, sy), (ex, ey) in zip(path, path[1:], strict=False):
                route.segments.append(
                    Segment(
                        x1=sx,
                        y1=sy,
                        x2=ex,
                        y2=ey,
                        width=trace_width,
                        layer=layer,
                        net=net,
                        net_name=net_name,
                    )
                )
            routes.append(route)

            # Mark these pads as connected
            connected_indices.add(idx1)
            connected_indices.add(idx2)

            label = "Intra-IC route" if kind == "route" else "Intra-IC wrap"
            length = sum(
                math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:], strict=False)
            )
            print(f"  {label}: {ref} pins {pads[idx1][1]}->{pads[idx2][1]} ({length:.2f}mm)")

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
