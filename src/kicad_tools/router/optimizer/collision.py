"""Collision checking for trace optimization."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..layers import Layer

if TYPE_CHECKING:
    from ..grid import RoutingGrid


class CollisionChecker(Protocol):
    """Protocol for checking if a path is clear of obstacles.

    Implementations can use different strategies:
    - Grid-based: Use RoutingGrid obstacle data
    - Segment intersection: Check for crossings with other nets
    - Quadtree: Spatial indexing for efficient queries

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
    """

    def __init__(self, grid: RoutingGrid):
        """Initialize with a routing grid.

        Args:
            grid: The routing grid with obstacle and net data.
        """
        self.grid = grid

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
                # Cell is blocked - check if it's our net or another net
                if cell.net != 0 and cell.net != exclude_net:
                    return False  # Blocked by another net
                if cell.is_obstacle:
                    return False  # Hard obstacle (pad, keepout)

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
