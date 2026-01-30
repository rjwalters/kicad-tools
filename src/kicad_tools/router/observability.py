"""Routing observability and statistics aggregation.

Collects and aggregates routing metrics including congestion,
layer usage, and overall routing quality statistics.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .grid import RoutingGrid
    from .primitives import Route


def compute_routing_statistics(
    routes: list[Route],
    grid: RoutingGrid,
    layer_stats: dict,
) -> dict:
    """Compute routing statistics including congestion metrics.

    Args:
        routes: List of completed routes
        grid: The routing grid (for congestion data)
        layer_stats: Pre-computed layer usage statistics

    Returns:
        Dictionary with routing statistics
    """
    total_length = sum(
        math.sqrt((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2)
        for r in routes
        for s in r.segments
    )
    congestion_stats = grid.get_congestion_map()

    return {
        "routes": len(routes),
        "segments": sum(len(r.segments) for r in routes),
        "vias": sum(len(r.vias) for r in routes),
        "total_length_mm": total_length,
        "nets_routed": len({r.net for r in routes}),
        "max_congestion": congestion_stats["max_congestion"],
        "avg_congestion": congestion_stats["avg_congestion"],
        "congested_regions": congestion_stats["congested_regions"],
        "layer_usage": layer_stats,
    }


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
