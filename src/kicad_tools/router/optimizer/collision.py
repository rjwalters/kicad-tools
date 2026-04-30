"""Collision checking for trace optimization."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from ..geometry import point_to_segment_distance, segment_to_segment_distance
from ..layers import Layer
from ..primitives import Segment

if TYPE_CHECKING:
    from ..grid import RoutingGrid


class CollisionChecker(Protocol):
    """Protocol for checking if a path is clear of obstacles.

    Implementations can use different strategies:
    - Grid-based: Use RoutingGrid obstacle data
    - Segment intersection: Check for crossings with other nets
    - R-tree: Spatial indexing for efficient segment clearance queries
      (implemented in RoutingGrid via per-layer rtree.index.Index, Issue #1249)

    The collision checker should return True if the path is clear,
    False if it would cross obstacles or other nets.
    """

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check if a path from (x1, y1) to (x2, y2) is clear of obstacles.

        Args:
            x1, y1: Start point coordinates.
            x2, y2: End point coordinates.
            layer: The layer the path is on.
            width: The trace width.
            exclude_net: Net ID to exclude from collision checks (own net).

        Returns:
            True if the path is clear, False if it would cross obstacles.
        """
        ...


class GridCollisionChecker:
    """Collision checker using the routing grid.

    Uses the RoutingGrid's obstacle data to check if paths are clear.
    This reuses the same collision detection logic as the autorouter.

    When ``ignore_overflow=True``, cells that are blocked by route
    occupation (another net passing through) but are *not* hard obstacles
    (pads, keepouts) are treated as clear.  This prevents the trace
    optimizer from fragmenting routes that pass through cells with minor
    overflow from negotiated routing.  Hard obstacles are always respected
    regardless of this flag.
    """

    def __init__(self, grid: RoutingGrid, ignore_overflow: bool = False):
        """Initialize with a routing grid.

        Args:
            grid: The routing grid with obstacle and net data.
            ignore_overflow: When True, treat cells blocked by route
                occupation (not hard obstacles) as clear.  This is used
                after negotiated routing with residual overflow so that
                the optimizer preserves connectivity instead of
                destroying segments that pass through overused cells.
        """
        self.grid = grid
        self.ignore_overflow = ignore_overflow

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check if a path is clear using grid-based collision detection.

        Uses Bresenham's line algorithm to check all grid cells along the path,
        including a buffer for trace width and clearance.

        Args:
            x1, y1: Start point coordinates.
            x2, y2: End point coordinates.
            layer: The layer the path is on.
            width: The trace width.
            exclude_net: Net ID to exclude from collision checks.

        Returns:
            True if the path is clear, False if it would cross obstacles.
        """
        # Convert to grid coordinates
        gx1, gy1 = self.grid.world_to_grid(x1, y1)
        gx2, gy2 = self.grid.world_to_grid(x2, y2)

        # Calculate clearance buffer in grid cells
        total_clearance = width / 2 + self.grid.rules.trace_clearance
        clearance_cells = int(total_clearance / self.grid.resolution) + 1

        # Get layer index
        try:
            layer_idx = self.grid.layer_to_index(layer.value)
        except Exception:
            return False  # Invalid layer

        # Check all cells along the path using Bresenham's algorithm
        cells_to_check = self._get_path_cells(gx1, gy1, gx2, gy2, clearance_cells)

        for gx, gy in cells_to_check:
            if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
                continue  # Out of bounds - skip but don't fail

            cell = self.grid.grid[layer_idx][gy][gx]

            # Check if blocked by another net
            if cell.blocked:
                if cell.is_obstacle:
                    return False  # Hard obstacle (pad, keepout) -- always block

                # Cell is occupied by another net's route (soft block).
                # When ignore_overflow is set, skip this check so the
                # optimizer does not fragment routes through overused cells.
                if cell.net != 0 and cell.net != exclude_net:
                    if not self.ignore_overflow:
                        return False  # Blocked by another net

        return True

    def _get_path_cells(
        self, gx1: int, gy1: int, gx2: int, gy2: int, clearance: int
    ) -> list[tuple[int, int]]:
        """Get all grid cells along a path with clearance buffer.

        Uses Bresenham's line algorithm with clearance expansion.

        Args:
            gx1, gy1: Start grid coordinates.
            gx2, gy2: End grid coordinates.
            clearance: Clearance buffer in grid cells.

        Returns:
            List of (gx, gy) grid coordinates to check.
        """
        cells: set[tuple[int, int]] = set()

        # Bresenham's line algorithm
        dx = abs(gx2 - gx1)
        dy = abs(gy2 - gy1)
        sx = 1 if gx1 < gx2 else -1
        sy = 1 if gy1 < gy2 else -1
        err = dx - dy

        gx, gy = gx1, gy1
        while True:
            # Add cell and clearance buffer
            for cy in range(-clearance, clearance + 1):
                for cx in range(-clearance, clearance + 1):
                    cells.add((gx + cx, gy + cy))

            if gx == gx2 and gy == gy2:
                break

            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                gx += sx
            if e2 < dx:
                err += dx
                gy += sy

        return list(cells)


class VectorCollisionChecker:
    """Collision checker using exact vector math and R-tree spatial indexing.

    Instead of discretizing the path onto a grid (O(L * W) cells), this
    checker performs broad-phase candidate filtering via the R-tree spatial
    index already maintained by ``RoutingGrid``, then applies exact
    segment-to-segment distance calculations for narrow-phase clearance
    checks.

    This is typically 10x+ faster than ``GridCollisionChecker`` for boards
    with many routed nets, because the R-tree query returns only nearby
    candidates and the analytical distance check is O(1) per candidate pair.

    Falls back to ``GridCollisionChecker`` when the R-tree is unavailable
    (e.g. rtree package not installed) or the segment count is below the
    R-tree activation threshold.
    """

    def __init__(
        self,
        grid: RoutingGrid,
        ignore_overflow: bool = False,
    ):
        """Initialize with a routing grid that has R-tree spatial index data.

        Args:
            grid: The routing grid with R-tree index and obstacle data.
            ignore_overflow: When True, treat cells blocked by route
                occupation (not hard obstacles) as clear.
        """
        self.grid = grid
        self.ignore_overflow = ignore_overflow
        # Lazy-initialized fallback for when R-tree is unavailable on a layer
        self._grid_fallback: GridCollisionChecker | None = None

    def _get_grid_fallback(self) -> GridCollisionChecker:
        """Get or create the grid-based fallback checker."""
        if self._grid_fallback is None:
            self._grid_fallback = GridCollisionChecker(
                self.grid, ignore_overflow=self.ignore_overflow
            )
        return self._grid_fallback

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check if a path is clear using R-tree + exact vector math.

        Broad phase: queries the per-layer R-tree for candidate segments
        whose bounding boxes overlap the query path's envelope (expanded
        by half-width + trace clearance).

        Narrow phase: computes exact segment-to-segment distance for each
        candidate and checks edge-to-edge clearance against the design
        rule minimum.

        Falls back to ``GridCollisionChecker`` when R-tree data is not
        available for the requested layer.

        Args:
            x1, y1: Start point coordinates.
            x2, y2: End point coordinates.
            layer: The layer the path is on.
            width: The trace width.
            exclude_net: Net ID to exclude from collision checks.

        Returns:
            True if the path is clear, False if it would cross obstacles.
        """
        # Resolve layer index
        try:
            layer_idx = self.grid.layer_to_index(layer.value)
        except Exception:
            return False

        # Check if R-tree is available and populated for this layer
        if (
            not self.grid._rtree_available
            or layer_idx not in self.grid._seg_rtree
        ):
            return self._get_grid_fallback().path_is_clear(
                x1, y1, x2, y2, layer, width, exclude_net
            )

        min_clearance = self.grid.rules.trace_clearance
        half_width = width / 2
        search_radius = half_width + min_clearance

        # Broad phase: query R-tree with expanded envelope
        query_envelope = (
            min(x1, x2) - search_radius,
            min(y1, y2) - search_radius,
            max(x1, x2) + search_radius,
            max(y1, y2) + search_radius,
        )
        candidate_ids = list(
            self.grid._seg_rtree[layer_idx].intersection(query_envelope)
        )
        layer_items: dict[int, Any] = self.grid._seg_rtree_items.get(
            layer_idx, {}
        )

        # Narrow phase: exact distance check for each candidate
        for cand_id in candidate_ids:
            other_seg: Segment | None = layer_items.get(cand_id)
            if other_seg is None:
                continue

            # Skip own-net segments
            if other_seg.net == exclude_net:
                continue

            # Exact center-to-center distance
            dist = segment_to_segment_distance(
                x1, y1, x2, y2,
                other_seg.x1, other_seg.y1, other_seg.x2, other_seg.y2,
            )

            # Edge-to-edge clearance
            clearance = dist - half_width - other_seg.width / 2

            if clearance < min_clearance:
                return False

        # Also check hard obstacles (pads, keepouts) via the grid
        # The R-tree only indexes routed segments, not static obstacles,
        # so we use the grid's obstacle layer for pad/keepout checks.
        if not self._check_obstacles_clear(
            x1, y1, x2, y2, layer_idx, width, exclude_net
        ):
            return False

        return True

    def _check_obstacles_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer_idx: int,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check that a path does not cross hard obstacles (pads, keepouts).

        Samples the path at grid resolution and checks each cell for hard
        obstacles.  This is lighter than a full Bresenham sweep because we
        only check obstacle status, not soft net occupation.

        Args:
            x1, y1: Start point coordinates (world).
            x2, y2: End point coordinates (world).
            layer_idx: Layer index.
            width: Trace width.
            exclude_net: Net ID to exclude.

        Returns:
            True if clear of hard obstacles.
        """
        gx1, gy1 = self.grid.world_to_grid(x1, y1)
        gx2, gy2 = self.grid.world_to_grid(x2, y2)

        total_clearance = width / 2 + self.grid.rules.trace_clearance
        clearance_cells = int(total_clearance / self.grid.resolution) + 1

        # Bresenham walk -- only check hard obstacles
        dx = abs(gx2 - gx1)
        dy = abs(gy2 - gy1)
        sx = 1 if gx1 < gx2 else -1
        sy = 1 if gy1 < gy2 else -1
        err = dx - dy

        gx, gy = gx1, gy1
        while True:
            for cy in range(-clearance_cells, clearance_cells + 1):
                for cx in range(-clearance_cells, clearance_cells + 1):
                    check_x = gx + cx
                    check_y = gy + cy
                    if not (
                        0 <= check_x < self.grid.cols
                        and 0 <= check_y < self.grid.rows
                    ):
                        continue
                    cell = self.grid.grid[layer_idx][check_y][check_x]
                    if cell.blocked and cell.is_obstacle:
                        # Hard obstacle -- check if it belongs to our net
                        if cell.net != 0 and cell.net == exclude_net:
                            continue  # Own-net pad is OK
                        return False

            if gx == gx2 and gy == gy2:
                break

            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                gx += sx
            if e2 < dx:
                err += dx
                gy += sy

        return True


def make_collision_checker(
    grid: RoutingGrid,
    ignore_overflow: bool = False,
) -> GridCollisionChecker | VectorCollisionChecker:
    """Select the best collision checker for the given grid.

    Returns a ``VectorCollisionChecker`` when the grid has an R-tree
    spatial index available and populated, otherwise falls back to
    ``GridCollisionChecker``.

    Args:
        grid: The routing grid with obstacle and net data.
        ignore_overflow: When True, treat cells blocked by route
            occupation (not hard obstacles) as clear.

    Returns:
        The most efficient collision checker for the given grid state.
    """
    if grid._rtree_available and grid._seg_rtree_count > 0:
        return VectorCollisionChecker(grid, ignore_overflow=ignore_overflow)
    return GridCollisionChecker(grid, ignore_overflow=ignore_overflow)
