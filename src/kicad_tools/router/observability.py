"""Routing observability and statistics aggregation.

Collects and aggregates routing metrics including congestion,
layer usage, and overall routing quality statistics.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .grid import RoutingGrid
    from .primitives import Pad, Route


# ---------------------------------------------------------------------------
# Union-Find for connectivity validation
# ---------------------------------------------------------------------------


class _UnionFind:
    """Lightweight union-find (disjoint set) for connectivity checks."""

    def __init__(self) -> None:
        self._parent: dict[tuple[float, float], tuple[float, float]] = {}
        self._rank: dict[tuple[float, float], int] = {}

    def _ensure(self, p: tuple[float, float]) -> None:
        if p not in self._parent:
            self._parent[p] = p
            self._rank[p] = 0

    def find(self, p: tuple[float, float]) -> tuple[float, float]:
        self._ensure(p)
        while self._parent[p] != p:
            self._parent[p] = self._parent[self._parent[p]]
            p = self._parent[p]
        return p

    def union(self, a: tuple[float, float], b: tuple[float, float]) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1


def _snap(val: float, tolerance: float = 0.01) -> float:
    """Round a coordinate to avoid floating-point mismatch."""
    return round(val / tolerance) * tolerance


def _pt(x: float, y: float, tol: float = 0.01) -> tuple[float, float]:
    return (_snap(x, tol), _snap(y, tol))


def validate_net_connectivity(
    routes: list[Route],
    net_pads: dict[int, list[Pad]],
    tolerance: float = 0.01,
) -> dict[int, dict]:
    """Check whether routed segments actually connect all pads in each net.

    For every net that has routes **and** pad information, this builds a
    union-find over segment endpoints, then checks how many pads belong to the
    same connected component.

    Args:
        routes: All routed ``Route`` objects.
        net_pads: Mapping of net ID to the list of ``Pad`` objects that belong
            to that net.  Only nets present in this dict are validated.
        tolerance: Coordinate tolerance for matching endpoints (mm).

    Returns:
        Dict mapping net ID -> connectivity info dict with keys:

        - ``total_pads``: number of pads in the net
        - ``connected_pads``: number of pads reachable from the largest
          connected component that includes at least one pad
        - ``connected``: ``True`` when all pads are in one component
    """
    # Group routes by net
    routes_by_net: dict[int, list[Route]] = {}
    for r in routes:
        routes_by_net.setdefault(r.net, []).append(r)

    result: dict[int, dict] = {}

    for net_id, pads in net_pads.items():
        if len(pads) < 2:
            # Single-pad nets are trivially connected
            result[net_id] = {
                "total_pads": len(pads),
                "connected_pads": len(pads),
                "connected": True,
            }
            continue

        net_routes = routes_by_net.get(net_id, [])
        if not net_routes:
            # No routes at all for this net
            result[net_id] = {
                "total_pads": len(pads),
                "connected_pads": 0,
                "connected": False,
            }
            continue

        uf = _UnionFind()

        # Union segment endpoints
        for route in net_routes:
            for seg in route.segments:
                p1 = _pt(seg.x1, seg.y1, tolerance)
                p2 = _pt(seg.x2, seg.y2, tolerance)
                uf.union(p1, p2)
            for via in route.vias:
                via_pt = _pt(via.x, via.y, tolerance)
                # A via connects to any segment endpoint at the same position
                # (already handled because the segment endpoints are unioned
                # with each other; the via shares the same coordinate as an
                # endpoint, so find() will merge them).
                uf._ensure(via_pt)
                # Explicitly union via point with nearby segment endpoints
                for seg in route.segments:
                    for sp in [_pt(seg.x1, seg.y1, tolerance), _pt(seg.x2, seg.y2, tolerance)]:
                        if sp == via_pt:
                            uf.union(via_pt, sp)

        # Map each pad to its nearest segment endpoint
        pad_points: list[tuple[float, float]] = []
        for pad in pads:
            pad_pt = _pt(pad.x, pad.y, tolerance)
            # Check if this pad position is already in the union-find
            # (i.e. a segment starts/ends at this pad).  If not, find
            # the closest segment endpoint and union with it.
            best_dist = float("inf")
            best_pt = pad_pt
            for route in net_routes:
                for seg in route.segments:
                    for sp in [_pt(seg.x1, seg.y1, tolerance), _pt(seg.x2, seg.y2, tolerance)]:
                        dx = sp[0] - pad_pt[0]
                        dy = sp[1] - pad_pt[1]
                        d = dx * dx + dy * dy
                        if d < best_dist:
                            best_dist = d
                            best_pt = sp
            # Only link pad to segment if within a reasonable proximity
            # (2mm -- escape stubs are typically short).
            if best_dist <= 4.0:  # 2mm squared
                uf.union(pad_pt, best_pt)
            else:
                uf._ensure(pad_pt)
            pad_points.append(pad_pt)

        # Count pads in the largest connected component
        component_pads: dict[tuple[float, float], int] = {}
        for pp in pad_points:
            root = uf.find(pp)
            component_pads[root] = component_pads.get(root, 0) + 1

        max_component = max(component_pads.values()) if component_pads else 0
        total = len(pads)

        result[net_id] = {
            "total_pads": total,
            "connected_pads": max_component,
            "connected": max_component == total,
        }

    return result


def compute_routing_statistics(
    routes: list[Route],
    grid: RoutingGrid,
    layer_stats: dict,
    nets_to_route_ids: set[int] | None = None,
    net_pads: dict[int, list[Pad]] | None = None,
) -> dict:
    """Compute routing statistics including congestion metrics.

    Args:
        routes: List of completed routes
        grid: The routing grid (for congestion data)
        layer_stats: Pre-computed layer usage statistics
        nets_to_route_ids: Optional set of net IDs that were targeted for
            routing (multi-pad signal nets).  When provided, ``nets_routed``
            only counts nets present in this set so the numerator and
            denominator use the same population.
        net_pads: Optional mapping of net ID to list of Pad objects.
            When provided, connectivity validation is performed and
            ``nets_routed`` reflects actual pad-to-pad connectivity
            rather than mere segment existence.

    Returns:
        Dictionary with routing statistics.  When *net_pads* is supplied
        the result also contains:

        - ``connectivity``: per-net connectivity dict from
          :func:`validate_net_connectivity`
        - ``nets_fully_connected``: count of nets where all pads are in
          one connected component
        - ``has_disconnected_islands``: ``True`` when any targeted net
          has pads that are not connected
    """
    total_length = sum(
        math.sqrt((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2)
        for r in routes
        for s in r.segments
    )
    congestion_stats = grid.get_congestion_map()

    all_routed_nets = {r.net for r in routes}

    # --- Connectivity-aware routing count ---
    connectivity: dict[int, dict] | None = None
    nets_fully_connected = 0
    has_disconnected_islands = False

    if net_pads is not None:
        # Validate actual connectivity
        target_pads = (
            {nid: pads for nid, pads in net_pads.items() if nid in nets_to_route_ids}
            if nets_to_route_ids is not None
            else net_pads
        )
        connectivity = validate_net_connectivity(routes, target_pads)
        nets_fully_connected = sum(
            1 for info in connectivity.values() if info["connected"]
        )
        has_disconnected_islands = any(
            not info["connected"] for info in connectivity.values()
            if info["total_pads"] >= 2
        )
        # nets_routed = only nets that are fully connected
        nets_routed = nets_fully_connected
    else:
        # Legacy path: count any net with at least one route
        if nets_to_route_ids is not None:
            nets_routed = len(all_routed_nets & nets_to_route_ids)
        else:
            nets_routed = len(all_routed_nets)

    result = {
        "routes": len(routes),
        "segments": sum(len(r.segments) for r in routes),
        "vias": sum(len(r.vias) for r in routes),
        "total_length_mm": total_length,
        "nets_routed": nets_routed,
        "max_congestion": congestion_stats["max_congestion"],
        "avg_congestion": congestion_stats["avg_congestion"],
        "congested_regions": congestion_stats["congested_regions"],
        "layer_usage": layer_stats,
    }

    if connectivity is not None:
        result["connectivity"] = connectivity
        result["nets_fully_connected"] = nets_fully_connected
        result["has_disconnected_islands"] = has_disconnected_islands

    return result


def compute_layer_usage_statistics(
    routes: list[Route],
    grid: RoutingGrid,
    layer_stack: object | None,
) -> dict:
    """Compute layer utilization statistics from routed segments.

    Args:
        routes: List of completed routes
        grid: The routing grid (for layer index mapping)
        layer_stack: Optional layer stack for index conversion

    Returns:
        Dictionary with per-layer usage statistics including:
        - per_layer: Dict mapping layer index to usage statistics
        - total_length: Total trace length across all layers
        - most_used_layer: Layer index with highest usage
        - least_used_layer: Layer index with lowest usage (among used layers)
        - balance_ratio: Ratio of min/max usage (1.0 = perfectly balanced)
    """
    # Count segments and length per layer
    layer_stats: dict[int, dict] = {}

    for route in routes:
        for seg in route.segments:
            # Get layer index from segment
            layer_idx = (
                grid.layer_to_index(seg.layer.value)
                if layer_stack
                else seg.layer.value
            )

            if layer_idx not in layer_stats:
                layer_stats[layer_idx] = {
                    "segments": 0,
                    "length_mm": 0.0,
                    "nets": set(),
                }

            seg_length = math.sqrt((seg.x2 - seg.x1) ** 2 + (seg.y2 - seg.y1) ** 2)
            layer_stats[layer_idx]["segments"] += 1
            layer_stats[layer_idx]["length_mm"] += seg_length
            layer_stats[layer_idx]["nets"].add(route.net)

    # Convert sets to counts for JSON serialization
    for layer_idx, stats in layer_stats.items():
        stats["net_count"] = len(stats["nets"])
        del stats["nets"]

    # Calculate summary statistics
    total_length = sum(s["length_mm"] for s in layer_stats.values())
    lengths = [s["length_mm"] for s in layer_stats.values()] if layer_stats else [0]

    most_used = (
        max(layer_stats.keys(), key=lambda k: layer_stats[k]["length_mm"]) if layer_stats else 0
    )
    least_used = (
        min(layer_stats.keys(), key=lambda k: layer_stats[k]["length_mm"]) if layer_stats else 0
    )

    # Balance ratio: min/max (1.0 = perfectly balanced)
    max_length = max(lengths) if lengths else 0
    min_length = min(lengths) if lengths else 0
    balance_ratio = min_length / max_length if max_length > 0 else 1.0

    return {
        "per_layer": layer_stats,
        "total_length": total_length,
        "most_used_layer": most_used,
        "least_used_layer": least_used,
        "balance_ratio": balance_ratio,
    }
