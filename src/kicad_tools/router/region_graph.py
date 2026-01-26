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

    Attributes:
        source: Source region ID
        target: Target region ID
        capacity: Maximum number of nets that can cross this boundary
        utilization: Current number of nets crossing this boundary
        distance: Euclidean distance between region centers (mm)
    """

    source: int
    target: int
    capacity: int = 10
    utilization: int = 0
    distance: float = 0.0

    @property
    def remaining_capacity(self) -> int:
        """Available crossing capacity."""
        return max(0, self.capacity - self.utilization)

    @property
    def congestion_cost(self) -> float:
        """Cost multiplier based on congestion level.

        Returns higher cost as the edge approaches full utilization,
        discouraging global paths through congested boundaries.

        Returns:
            Cost multiplier (1.0 = uncongested, increases with utilization)
        """
        if self.capacity <= 0:
            return 100.0
        ratio = self.utilization / self.capacity
        if ratio >= 1.0:
            return 10.0
        return 1.0 + 4.0 * ratio * ratio


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

    Usage:
        graph = RegionGraph(
            board_width=65.0,
            board_height=56.0,
            origin_x=0.0,
            origin_y=0.0,
            num_cols=10,
            num_rows=10,
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
        base_capacity: Base routing capacity per region (default: 10)
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
    ):
        self.board_width = board_width
        self.board_height = board_height
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.num_cols = max(1, num_cols)
        self.num_rows = max(1, num_rows)
        self.base_capacity = base_capacity

        # Build regions
        self.regions: dict[int, Region] = {}
        self._region_grid: list[list[int]] = []  # [row][col] -> region_id
        self._build_regions()

        # Build edges between adjacent regions
        self.edges: dict[int, list[RegionEdge]] = {}
        self._build_edges()

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

    def _build_edges(self) -> None:
        """Build edges between adjacent regions (4-connected: up/down/left/right)."""
        for region in self.regions.values():
            self.edges[region.id] = []

        for row in range(self.num_rows):
            for col in range(self.num_cols):
                region_id = self._region_grid[row][col]
                region = self.regions[region_id]

                # Right neighbor
                if col + 1 < self.num_cols:
                    neighbor_id = self._region_grid[row][col + 1]
                    neighbor = self.regions[neighbor_id]
                    dist = math.sqrt(
                        (region.center_x - neighbor.center_x) ** 2
                        + (region.center_y - neighbor.center_y) ** 2
                    )
                    edge_capacity = self.base_capacity
                    self.edges[region_id].append(
                        RegionEdge(
                            source=region_id,
                            target=neighbor_id,
                            capacity=edge_capacity,
                            distance=dist,
                        )
                    )
                    self.edges[neighbor_id].append(
                        RegionEdge(
                            source=neighbor_id,
                            target=region_id,
                            capacity=edge_capacity,
                            distance=dist,
                        )
                    )

                # Bottom neighbor
                if row + 1 < self.num_rows:
                    neighbor_id = self._region_grid[row + 1][col]
                    neighbor = self.regions[neighbor_id]
                    dist = math.sqrt(
                        (region.center_x - neighbor.center_x) ** 2
                        + (region.center_y - neighbor.center_y) ** 2
                    )
                    edge_capacity = self.base_capacity
                    self.edges[region_id].append(
                        RegionEdge(
                            source=region_id,
                            target=neighbor_id,
                            capacity=edge_capacity,
                            distance=dist,
                        )
                    )
                    self.edges[neighbor_id].append(
                        RegionEdge(
                            source=neighbor_id,
                            target=region_id,
                            capacity=edge_capacity,
                            distance=dist,
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
        """Register component pads as obstacles, reducing region capacity.

        Regions with more obstacles have reduced routing capacity, which
        guides global routing paths around congested areas.

        Args:
            pads: List of Pad objects representing component pins
        """
        for pad in pads:
            region = self.get_region_at(pad.x, pad.y)
            if region is not None:
                region.obstacle_count += 1

        # Reduce capacity of regions with many obstacles
        for region in self.regions.values():
            if region.obstacle_count > 0:
                # Each obstacle reduces capacity by 1, but keep minimum of 1
                reduction = min(region.obstacle_count, region.capacity - 1)
                region.capacity = max(1, region.capacity - reduction)

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

    def update_utilization(self, path: list[int]) -> None:
        """Update region and edge utilization after assigning a path.

        This increases the utilization counters for all regions and edges
        along the path, making future paths prefer less congested areas.

        Args:
            path: List of region IDs forming the assigned path
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
                    edge.utilization += 1
                    break

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
