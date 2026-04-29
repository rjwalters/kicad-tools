"""
Region graph abstraction for hierarchical routing.

This module provides a coarse-grid representation of the board for global
routing. The board is partitioned into rectangular regions, and a graph
is constructed where edges represent routing capacity between adjacent
regions.

The RegionGraph enables global path planning at a much coarser granularity
than the detailed routing grid, allowing the GlobalRouter to quickly assign
corridors (sequences of regions) to each net before detailed routing begins.

Phase A of hierarchical routing (Issue #1095).
Tile-based capacity estimation and per-layer support (Issue #2276).
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Pad


@dataclass
class Region:
    """A rectangular region on the board for global routing.

    Each region represents a coarse-grid area. Regions track their
    spatial bounds, routing capacity, and current utilization.

    Attributes:
        id: Unique region identifier
        row: Row index in the region grid
        col: Column index in the region grid
        min_x: Minimum X coordinate (mm)
        min_y: Minimum Y coordinate (mm)
        max_x: Maximum X coordinate (mm)
        max_y: Maximum Y coordinate (mm)
        capacity: Maximum number of nets that can traverse this region
        utilization: Current number of nets using this region
        obstacle_count: Number of obstacles (pads, keepouts) in this region
    """

    id: int
    row: int
    col: int
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    capacity: int = 10
    utilization: int = 0
    obstacle_count: int = 0

    @property
    def center_x(self) -> float:
        """X coordinate of the region center."""
        return (self.min_x + self.max_x) / 2.0

    @property
    def center_y(self) -> float:
        """Y coordinate of the region center."""
        return (self.min_y + self.max_y) / 2.0

    @property
    def width(self) -> float:
        """Width of the region in mm."""
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        """Height of the region in mm."""
        return self.max_y - self.min_y

    def contains_point(self, x: float, y: float) -> bool:
        """Check if a point falls within this region.

        Args:
            x: X coordinate in mm
            y: Y coordinate in mm

        Returns:
            True if the point is inside this region
        """
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    @property
    def remaining_capacity(self) -> int:
        """Available routing capacity (capacity minus utilization)."""
        return max(0, self.capacity - self.utilization)


@dataclass
class RegionEdge:
    """An edge between two adjacent regions in the region graph.

    Supports per-layer capacity tracking: ``layer_capacity`` stores the
    maximum number of tracks that can cross the boundary on each layer,
    and ``layer_utilization`` tracks how many are currently assigned.

    When per-layer data is not set (legacy mode), the scalar ``capacity``
    and ``utilization`` fields are used instead.

    Attributes:
        source: Source region ID
        target: Target region ID
        capacity: Maximum number of nets that can cross this boundary
            (sum across all layers when per-layer data is present)
        utilization: Current number of nets crossing this boundary
        distance: Euclidean distance between region centers (mm)
        blockage: Total obstacle blockage length along this edge (mm)
        history_cost: Accumulated history cost from negotiated iterations
        layer_capacity: Per-layer capacity dict (layer_index -> int)
        layer_utilization: Per-layer utilization dict (layer_index -> int)
    """

    source: int
    target: int
    capacity: int = 10
    utilization: int = 0
    distance: float = 0.0
    blockage: float = 0.0
    history_cost: float = 0.0
    layer_capacity: dict[int, int] = field(default_factory=dict)
    layer_utilization: dict[int, int] = field(default_factory=dict)

    @property
    def remaining_capacity(self) -> int:
        """Available crossing capacity."""
        return max(0, self.capacity - self.utilization)

    @property
    def overflow(self) -> int:
        """Number of nets exceeding capacity (0 if not overflowed)."""
        return max(0, self.utilization - self.capacity)

    @property
    def congestion_cost(self) -> float:
        """Cost multiplier based on congestion level.

        Returns higher cost as the edge approaches full utilization,
        discouraging global paths through congested boundaries.
        Includes history cost from negotiated iterations.

        Returns:
            Cost multiplier (1.0 = uncongested, increases with utilization)
        """
        if self.capacity <= 0:
            return 100.0 + self.history_cost
        ratio = self.utilization / self.capacity
        if ratio >= 1.0:
            return 10.0 + self.history_cost
        return 1.0 + 4.0 * ratio * ratio + self.history_cost

    def remaining_capacity_on_layer(self, layer: int) -> int:
        """Available capacity on a specific layer.

        Args:
            layer: Layer index

        Returns:
            Remaining capacity on that layer, or total remaining if
            per-layer data is not available.
        """
        if not self.layer_capacity:
            return self.remaining_capacity
        cap = self.layer_capacity.get(layer, 0)
        util = self.layer_utilization.get(layer, 0)
        return max(0, cap - util)

    def use_layer(self, layer: int) -> None:
        """Record a net crossing this edge on a specific layer.

        Updates both the per-layer utilization and the aggregate
        utilization counter.

        Args:
            layer: Layer index the net is routed on
        """
        self.utilization += 1
        if self.layer_utilization:
            self.layer_utilization[layer] = self.layer_utilization.get(layer, 0) + 1

    def release_layer(self, layer: int) -> None:
        """Release a net crossing on a specific layer (for rip-up).

        Args:
            layer: Layer index to release
        """
        self.utilization = max(0, self.utilization - 1)
        if self.layer_utilization:
            cur = self.layer_utilization.get(layer, 0)
            self.layer_utilization[layer] = max(0, cur - 1)


@dataclass
class _GlobalSearchNode:
    """Node for A* search on the region graph."""

    f_score: float
    g_score: float = field(compare=False)
    region_id: int = field(compare=False)
    parent: _GlobalSearchNode | None = field(compare=False, default=None)

    def __lt__(self, other: _GlobalSearchNode) -> bool:
        return self.f_score < other.f_score


class RegionGraph:
    """Coarse-grid board representation for global routing.

    Partitions the board into a grid of rectangular regions and builds
    a graph of adjacency relationships. This enables fast global path
    planning using Dijkstra/A* on the region graph rather than the
    detailed routing grid.

    The region graph is constructed once from board dimensions and
    component positions, then reused for all nets during global routing.

    Supports two capacity modes:

    1. **Legacy (flat)**: When ``trace_pitch`` is not provided, edges
       use the flat ``base_capacity`` value (default 10). This preserves
       backward compatibility with existing callers.

    2. **Geometry-based**: When ``trace_pitch`` is provided, edge
       capacities are computed from the physical tile-boundary length
       divided by the routing pitch. Per-layer capacity is enabled
       when ``num_layers`` > 1.

    Usage:
        graph = RegionGraph(
            board_width=65.0,
            board_height=56.0,
            origin_x=0.0,
            origin_y=0.0,
            num_cols=10,
            num_rows=10,
            trace_pitch=0.4,
            num_layers=2,
        )
        graph.register_obstacles(pads)
        path = graph.find_path(source_region_id, target_region_id)

    Args:
        board_width: Board width in mm
        board_height: Board height in mm
        origin_x: Board origin X coordinate in mm
        origin_y: Board origin Y coordinate in mm
        num_cols: Number of region columns (default: 10)
        num_rows: Number of region rows (default: 10)
        base_capacity: Base routing capacity per region (default: 10).
            Used only when ``trace_pitch`` is not provided.
        trace_pitch: Routing pitch in mm (trace_width + trace_clearance).
            When provided, edge capacities are computed from geometry.
        num_layers: Number of signal routing layers (default: 1).
            When > 1, per-layer capacity is tracked on each edge.
    """

    def __init__(
        self,
        board_width: float,
        board_height: float,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
        num_cols: int = 10,
        num_rows: int = 10,
        base_capacity: int = 10,
        trace_pitch: float | None = None,
        num_layers: int = 1,
    ):
        self.board_width = board_width
        self.board_height = board_height
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.num_cols = max(1, num_cols)
        self.num_rows = max(1, num_rows)
        self.base_capacity = base_capacity
        self.trace_pitch = trace_pitch
        self.num_layers = max(1, num_layers)

        # Build regions
        self.regions: dict[int, Region] = {}
        self._region_grid: list[list[int]] = []  # [row][col] -> region_id
        self._build_regions()

        # Build edges between adjacent regions
        self.edges: dict[int, list[RegionEdge]] = {}
        self._build_edges()

        # Edge lookup for fast (source, target) access
        self._edge_lookup: dict[tuple[int, int], RegionEdge] = {}
        for edges in self.edges.values():
            for edge in edges:
                self._edge_lookup[(edge.source, edge.target)] = edge

    def _build_regions(self) -> None:
        """Construct the grid of regions covering the board area."""
        region_width = self.board_width / self.num_cols
        region_height = self.board_height / self.num_rows
        region_id = 0

        self._region_grid = []
        for row in range(self.num_rows):
            row_ids: list[int] = []
            for col in range(self.num_cols):
                min_x = self.origin_x + col * region_width
                min_y = self.origin_y + row * region_height
                max_x = min_x + region_width
                max_y = min_y + region_height

                region = Region(
                    id=region_id,
                    row=row,
                    col=col,
                    min_x=min_x,
                    min_y=min_y,
                    max_x=max_x,
                    max_y=max_y,
                    capacity=self.base_capacity,
                )
                self.regions[region_id] = region
                row_ids.append(region_id)
                region_id += 1

            self._region_grid.append(row_ids)

    def _compute_edge_capacity(self, edge_length: float, blockage: float = 0.0) -> int:
        """Compute edge capacity from physical geometry.

        capacity = (edge_length - blockage) / pitch, summed across layers.

        Args:
            edge_length: Length of the tile boundary in mm.
            blockage: Obstacle blockage along the boundary in mm.

        Returns:
            Integer capacity (at least 1).
        """
        if self.trace_pitch is None or self.trace_pitch <= 0:
            return self.base_capacity

        available = max(0.0, edge_length - blockage)
        per_layer = int(available / self.trace_pitch)
        total = per_layer * self.num_layers
        return max(1, total)

    def _make_layer_capacity(self, per_layer_cap: int) -> dict[int, int]:
        """Build per-layer capacity dict.

        Args:
            per_layer_cap: Capacity on each layer.

        Returns:
            Dict mapping layer index to capacity.
        """
        if self.num_layers <= 1:
            return {}
        return {layer: per_layer_cap for layer in range(self.num_layers)}

    def _build_edges(self) -> None:
        """Build edges between adjacent regions (4-connected: up/down/left/right)."""
        for region in self.regions.values():
            self.edges[region.id] = []

        region_width = self.board_width / self.num_cols
        region_height = self.board_height / self.num_rows

        for row in range(self.num_rows):
            for col in range(self.num_cols):
                region_id = self._region_grid[row][col]
                region = self.regions[region_id]

                # Right neighbor -- boundary is vertical, length = region_height
                if col + 1 < self.num_cols:
                    neighbor_id = self._region_grid[row][col + 1]
                    neighbor = self.regions[neighbor_id]
                    dist = math.sqrt(
                        (region.center_x - neighbor.center_x) ** 2
                        + (region.center_y - neighbor.center_y) ** 2
                    )
                    edge_length = region_height
                    edge_capacity = self._compute_edge_capacity(edge_length)

                    if self.trace_pitch is not None and self.trace_pitch > 0:
                        per_layer_cap = max(
                            1,
                            int(max(0.0, edge_length) / self.trace_pitch),
                        )
                    else:
                        per_layer_cap = self.base_capacity

                    layer_cap = self._make_layer_capacity(per_layer_cap)
                    layer_util: dict[int, int] = (
                        {l: 0 for l in layer_cap} if layer_cap else {}
                    )

                    self.edges[region_id].append(
                        RegionEdge(
                            source=region_id,
                            target=neighbor_id,
                            capacity=edge_capacity,
                            distance=dist,
                            layer_capacity=dict(layer_cap),
                            layer_utilization=dict(layer_util),
                        )
                    )
                    self.edges[neighbor_id].append(
                        RegionEdge(
                            source=neighbor_id,
                            target=region_id,
                            capacity=edge_capacity,
                            distance=dist,
                            layer_capacity=dict(layer_cap),
                            layer_utilization=dict(layer_util),
                        )
                    )

                # Bottom neighbor -- boundary is horizontal, length = region_width
                if row + 1 < self.num_rows:
                    neighbor_id = self._region_grid[row + 1][col]
                    neighbor = self.regions[neighbor_id]
                    dist = math.sqrt(
                        (region.center_x - neighbor.center_x) ** 2
                        + (region.center_y - neighbor.center_y) ** 2
                    )
                    edge_length = region_width
                    edge_capacity = self._compute_edge_capacity(edge_length)

                    if self.trace_pitch is not None and self.trace_pitch > 0:
                        per_layer_cap = max(
                            1,
                            int(max(0.0, edge_length) / self.trace_pitch),
                        )
                    else:
                        per_layer_cap = self.base_capacity

                    layer_cap = self._make_layer_capacity(per_layer_cap)
                    layer_util = (
                        {l: 0 for l in layer_cap} if layer_cap else {}
                    )

                    self.edges[region_id].append(
                        RegionEdge(
                            source=region_id,
                            target=neighbor_id,
                            capacity=edge_capacity,
                            distance=dist,
                            layer_capacity=dict(layer_cap),
                            layer_utilization=dict(layer_util),
                        )
                    )
                    self.edges[neighbor_id].append(
                        RegionEdge(
                            source=neighbor_id,
                            target=region_id,
                            capacity=edge_capacity,
                            distance=dist,
                            layer_capacity=dict(layer_cap),
                            layer_utilization=dict(layer_util),
                        )
                    )

    def get_region_at(self, x: float, y: float) -> Region | None:
        """Find the region containing a given board coordinate.

        Args:
            x: X coordinate in mm
            y: Y coordinate in mm

        Returns:
            The Region containing the point, or None if outside the board
        """
        if not (self.origin_x <= x <= self.origin_x + self.board_width):
            return None
        if not (self.origin_y <= y <= self.origin_y + self.board_height):
            return None

        region_width = self.board_width / self.num_cols
        region_height = self.board_height / self.num_rows

        col = min(int((x - self.origin_x) / region_width), self.num_cols - 1)
        row = min(int((y - self.origin_y) / region_height), self.num_rows - 1)

        region_id = self._region_grid[row][col]
        return self.regions[region_id]

    def register_obstacles(self, pads: list[Pad]) -> None:
        """Register component pads as obstacles, reducing region and edge capacity.

        For each pad, the obstacle count on the containing region is
        incremented and blockage is added to edges touching that region.

        When geometry-based capacity is active (``trace_pitch`` set),
        edge capacities are recomputed after blockage is accumulated.
        In legacy mode, region capacity is reduced as before.

        Args:
            pads: List of Pad objects representing component pins
        """
        for pad in pads:
            region = self.get_region_at(pad.x, pad.y)
            if region is not None:
                region.obstacle_count += 1

        if self.trace_pitch is not None and self.trace_pitch > 0:
            # Geometry mode: accumulate blockage per edge and recompute capacity
            self._accumulate_edge_blockage(pads)
        else:
            # Legacy mode: reduce region capacity based on obstacle count
            for region in self.regions.values():
                if region.obstacle_count > 0:
                    reduction = min(region.obstacle_count, region.capacity - 1)
                    region.capacity = max(1, region.capacity - reduction)

    def _accumulate_edge_blockage(self, pads: list[Pad]) -> None:
        """Accumulate obstacle blockage on tile-boundary edges and recompute capacity.

        Each pad contributes blockage to edges of the region it resides in.
        Blockage is estimated as the pad's larger dimension (a conservative
        approximation of how much boundary it blocks).

        After blockage is accumulated, edge capacities are recomputed using
        the geometry formula.

        Args:
            pads: List of Pad objects.
        """
        # Map region_id -> total blockage from obstacles inside it
        region_blockage: dict[int, float] = {}
        for pad in pads:
            region = self.get_region_at(pad.x, pad.y)
            if region is not None:
                pad_size = max(getattr(pad, "width", 0.0), getattr(pad, "height", 0.0))
                region_blockage[region.id] = region_blockage.get(region.id, 0.0) + pad_size

        region_width = self.board_width / self.num_cols
        region_height = self.board_height / self.num_rows

        # Update edges: for each edge, blockage is the average of
        # the two adjacent regions' blockage contributions.
        for edges in self.edges.values():
            for edge in edges:
                src_block = region_blockage.get(edge.source, 0.0)
                tgt_block = region_blockage.get(edge.target, 0.0)
                edge.blockage = (src_block + tgt_block) / 2.0

                # Determine edge length from orientation
                src_region = self.regions[edge.source]
                tgt_region = self.regions[edge.target]
                if src_region.row == tgt_region.row:
                    # Horizontal neighbors -- vertical boundary
                    edge_length = region_height
                else:
                    # Vertical neighbors -- horizontal boundary
                    edge_length = region_width

                new_cap = self._compute_edge_capacity(edge_length, edge.blockage)
                edge.capacity = new_cap

                # Recompute per-layer capacity
                if self.trace_pitch and self.trace_pitch > 0:
                    available = max(0.0, edge_length - edge.blockage)
                    per_layer_cap = max(1, int(available / self.trace_pitch))
                    edge.layer_capacity = self._make_layer_capacity(per_layer_cap)

    def find_path(
        self,
        source_id: int,
        target_id: int,
    ) -> list[int] | None:
        """Find a path between two regions using A* search.

        The search considers edge distances, congestion costs, and
        remaining capacity to find a good global routing path.

        Args:
            source_id: Source region ID
            target_id: Target region ID

        Returns:
            List of region IDs forming the path (inclusive of source and
            target), or None if no path exists
        """
        if source_id not in self.regions or target_id not in self.regions:
            return None

        if source_id == target_id:
            return [source_id]

        target = self.regions[target_id]

        open_set: list[_GlobalSearchNode] = []
        closed_set: set[int] = set()
        g_scores: dict[int, float] = {}

        source = self.regions[source_id]
        h = math.sqrt(
            (source.center_x - target.center_x) ** 2
            + (source.center_y - target.center_y) ** 2
        )
        start_node = _GlobalSearchNode(f_score=h, g_score=0.0, region_id=source_id)
        heapq.heappush(open_set, start_node)
        g_scores[source_id] = 0.0

        while open_set:
            current = heapq.heappop(open_set)

            if current.region_id in closed_set:
                continue
            closed_set.add(current.region_id)

            # Goal check
            if current.region_id == target_id:
                return self._reconstruct_path(current)

            # Explore neighbors
            for edge in self.edges.get(current.region_id, []):
                neighbor_id = edge.target
                if neighbor_id in closed_set:
                    continue

                # Cost = distance * congestion factor
                edge_cost = edge.distance * edge.congestion_cost
                new_g = current.g_score + edge_cost

                if neighbor_id not in g_scores or new_g < g_scores[neighbor_id]:
                    g_scores[neighbor_id] = new_g
                    neighbor = self.regions[neighbor_id]
                    h = math.sqrt(
                        (neighbor.center_x - target.center_x) ** 2
                        + (neighbor.center_y - target.center_y) ** 2
                    )
                    f = new_g + h
                    neighbor_node = _GlobalSearchNode(
                        f_score=f,
                        g_score=new_g,
                        region_id=neighbor_id,
                        parent=current,
                    )
                    heapq.heappush(open_set, neighbor_node)

        return None

    def _reconstruct_path(self, end_node: _GlobalSearchNode) -> list[int]:
        """Reconstruct the region path from the A* search result.

        Args:
            end_node: The goal search node

        Returns:
            List of region IDs from source to target
        """
        path: list[int] = []
        node: _GlobalSearchNode | None = end_node
        while node is not None:
            path.append(node.region_id)
            node = node.parent
        path.reverse()
        return path

    def update_utilization(self, path: list[int], layer: int = 0) -> None:
        """Update region and edge utilization after assigning a path.

        This increases the utilization counters for all regions and edges
        along the path, making future paths prefer less congested areas.

        Args:
            path: List of region IDs forming the assigned path
            layer: Layer index for per-layer tracking (default: 0)
        """
        # Update region utilization
        for region_id in path:
            if region_id in self.regions:
                self.regions[region_id].utilization += 1

        # Update edge utilization
        for i in range(len(path) - 1):
            src = path[i]
            tgt = path[i + 1]
            for edge in self.edges.get(src, []):
                if edge.target == tgt:
                    edge.use_layer(layer)
                    break

    def release_utilization(self, path: list[int], layer: int = 0) -> None:
        """Release utilization for a path (for rip-up during negotiation).

        Decrements utilization counters for all regions and edges along
        the path.

        Args:
            path: List of region IDs forming the path to release
            layer: Layer index for per-layer tracking (default: 0)
        """
        for region_id in path:
            if region_id in self.regions:
                self.regions[region_id].utilization = max(
                    0, self.regions[region_id].utilization - 1
                )

        for i in range(len(path) - 1):
            src = path[i]
            tgt = path[i + 1]
            for edge in self.edges.get(src, []):
                if edge.target == tgt:
                    edge.release_layer(layer)
                    break

    def get_total_overflow(self) -> int:
        """Compute total edge overflow across the graph.

        Overflow on an edge is max(0, utilization - capacity).

        Returns:
            Sum of overflow across all directed edges.
        """
        total = 0
        seen: set[tuple[int, int]] = set()
        for edges in self.edges.values():
            for edge in edges:
                key = (min(edge.source, edge.target), max(edge.source, edge.target))
                if key not in seen:
                    seen.add(key)
                    total += edge.overflow
        return total

    def get_overflowed_edges(self) -> list[RegionEdge]:
        """Return all edges with overflow > 0.

        Returns:
            List of overflowed RegionEdge objects (one per undirected edge).
        """
        result: list[RegionEdge] = []
        seen: set[tuple[int, int]] = set()
        for edges in self.edges.values():
            for edge in edges:
                key = (min(edge.source, edge.target), max(edge.source, edge.target))
                if key not in seen and edge.overflow > 0:
                    seen.add(key)
                    result.append(edge)
        return result

    def update_history_costs(self, increment: float) -> None:
        """Add history cost to overflowed edges (PathFinder-style).

        For each edge with overflow > 0, add ``increment`` to its
        history cost. This makes the A* search progressively avoid
        edges that have been overflowed in previous iterations.

        Args:
            increment: Amount to add to history cost of overflowed edges.
        """
        for edges in self.edges.values():
            for edge in edges:
                if edge.overflow > 0:
                    edge.history_cost += increment

    def reset_utilization(self) -> None:
        """Reset all utilization counters (regions and edges) to zero.

        Used between negotiated iteration rounds to re-route from scratch
        with updated history costs.
        """
        for region in self.regions.values():
            region.utilization = 0
        for edges in self.edges.values():
            for edge in edges:
                edge.utilization = 0
                if edge.layer_utilization:
                    for layer in edge.layer_utilization:
                        edge.layer_utilization[layer] = 0

    def path_to_waypoint_coords(self, path: list[int]) -> list[tuple[float, float]]:
        """Convert a region path to a sequence of waypoint coordinates.

        Each waypoint is placed at the center of its region. This provides
        the centerline for corridor construction.

        Args:
            path: List of region IDs

        Returns:
            List of (x, y) coordinate tuples at region centers
        """
        coords: list[tuple[float, float]] = []
        for region_id in path:
            region = self.regions[region_id]
            coords.append((region.center_x, region.center_y))
        return coords

    def register_block_occupancy(
        self,
        min_x: float,
        min_y: float,
        max_x: float,
        max_y: float,
        trace_count: int = 1,
    ) -> None:
        """Mark regions overlapping a block's area as partially occupied.

        After block-internal routing completes, this increases utilization
        for regions that overlap the block's bounding box. This guides
        inter-block routing away from block interiors.

        Args:
            min_x: Minimum X of the block area (mm).
            min_y: Minimum Y of the block area (mm).
            max_x: Maximum X of the block area (mm).
            max_y: Maximum Y of the block area (mm).
            trace_count: Number of traces placed inside the block.
        """
        for region in self.regions.values():
            # Check if region overlaps with block area
            if (
                region.max_x > min_x
                and region.min_x < max_x
                and region.max_y > min_y
                and region.min_y < max_y
            ):
                region.utilization += trace_count

    def get_region_count(self) -> int:
        """Return the total number of regions."""
        return len(self.regions)

    def get_edge_count(self) -> int:
        """Return the total number of directed edges."""
        return sum(len(edges) for edges in self.edges.values())

    def get_statistics(self) -> dict:
        """Get statistics about the region graph.

        Returns:
            Dictionary with region count, edge count, capacity, and
            utilization statistics
        """
        total_utilization = sum(r.utilization for r in self.regions.values())
        total_capacity = sum(r.capacity for r in self.regions.values())
        max_utilization = (
            max(r.utilization for r in self.regions.values()) if self.regions else 0
        )

        return {
            "num_regions": len(self.regions),
            "num_rows": self.num_rows,
            "num_cols": self.num_cols,
            "num_edges": self.get_edge_count(),
            "total_capacity": total_capacity,
            "total_utilization": total_utilization,
            "max_utilization": max_utilization,
            "regions_with_obstacles": sum(
                1 for r in self.regions.values() if r.obstacle_count > 0
            ),
        }
