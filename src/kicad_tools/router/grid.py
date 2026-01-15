"""
Routing grid for PCB autorouting.

This module provides:
- RoutingGrid: 3D grid for routing with obstacle tracking and congestion awareness

Performance optimizations:
- NumPy arrays for cell attributes (blocked, net, usage_count, etc.)
- Vectorized operations for bulk cell updates
- Pre-computed clearance masks for obstacle marking
- Expanded obstacle mode for coarser grids with pre-computed clearances

Grid Resolution Strategies:
- Fine grid (clearance/2): Maximum accuracy, highest memory/time cost
- Standard grid (trace_width): Good balance for most boards
- Expanded obstacles: Pre-expand obstacles, use coarser grid (~4x faster)

Thread Safety:
- Optional thread-safe mode for parallel routing operations
- RLock-based synchronization to prevent race conditions
- Minimal overhead when disabled (default)
"""

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

import numpy as np

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import Zone

from kicad_tools.exceptions import RoutingError

from .layers import Layer, LayerStack
from .primitives import Obstacle, Pad, Route, Segment, Via
from .rules import DesignRules


class _CellView:
    """Lightweight view into grid arrays, providing GridCell-like interface."""

    __slots__ = ("_grid", "_x", "_y", "_layer")

    def __init__(self, grid: "RoutingGrid", x: int, y: int, layer: int):
        self._grid = grid
        self._x = x
        self._y = y
        self._layer = layer

    @property
    def x(self) -> int:
        return self._x

    @property
    def y(self) -> int:
        return self._y

    @property
    def layer(self) -> int:
        return self._layer

    @property
    def blocked(self) -> bool:
        return bool(self._grid._blocked[self._layer, self._y, self._x])

    @blocked.setter
    def blocked(self, value: bool) -> None:
        self._grid._blocked[self._layer, self._y, self._x] = value

    @property
    def net(self) -> int:
        return int(self._grid._net[self._layer, self._y, self._x])

    @net.setter
    def net(self, value: int) -> None:
        self._grid._net[self._layer, self._y, self._x] = value

    @property
    def cost(self) -> float:
        return 1.0  # Default cost, not stored in arrays

    @property
    def usage_count(self) -> int:
        return int(self._grid._usage_count[self._layer, self._y, self._x])

    @usage_count.setter
    def usage_count(self, value: int) -> None:
        self._grid._usage_count[self._layer, self._y, self._x] = value

    @property
    def history_cost(self) -> float:
        return float(self._grid._history_cost[self._layer, self._y, self._x])

    @history_cost.setter
    def history_cost(self, value: float) -> None:
        self._grid._history_cost[self._layer, self._y, self._x] = value

    @property
    def is_obstacle(self) -> bool:
        return bool(self._grid._is_obstacle[self._layer, self._y, self._x])

    @is_obstacle.setter
    def is_obstacle(self, value: bool) -> None:
        self._grid._is_obstacle[self._layer, self._y, self._x] = value

    @property
    def is_zone(self) -> bool:
        return bool(self._grid._is_zone[self._layer, self._y, self._x])

    @is_zone.setter
    def is_zone(self, value: bool) -> None:
        self._grid._is_zone[self._layer, self._y, self._x] = value

    @property
    def zone_id(self) -> str | None:
        return self._grid._zone_ids.get((self._layer, self._y, self._x))

    @zone_id.setter
    def zone_id(self, value: str | None) -> None:
        key = (self._layer, self._y, self._x)
        if value is None:
            self._grid._zone_ids.pop(key, None)
        else:
            self._grid._zone_ids[key] = value

    @property
    def pad_blocked(self) -> bool:
        return bool(self._grid._pad_blocked[self._layer, self._y, self._x])

    @pad_blocked.setter
    def pad_blocked(self, value: bool) -> None:
        self._grid._pad_blocked[self._layer, self._y, self._x] = value

    @property
    def original_net(self) -> int:
        return int(self._grid._original_net[self._layer, self._y, self._x])

    @original_net.setter
    def original_net(self, value: int) -> None:
        self._grid._original_net[self._layer, self._y, self._x] = value


class _LayerView:
    """View into a single layer of the grid."""

    __slots__ = ("_grid", "_layer")

    def __init__(self, grid: "RoutingGrid", layer: int):
        self._grid = grid
        self._layer = layer

    def __getitem__(self, y: int) -> "_RowView":
        return _RowView(self._grid, self._layer, y)


class _RowView:
    """View into a single row of the grid."""

    __slots__ = ("_grid", "_layer", "_y")

    def __init__(self, grid: "RoutingGrid", layer: int, y: int):
        self._grid = grid
        self._layer = layer
        self._y = y

    def __getitem__(self, x: int) -> _CellView:
        return _CellView(self._grid, x, self._y, self._layer)


class _GridView:
    """Provides backward-compatible grid[layer][y][x] access to NumPy arrays."""

    __slots__ = ("_grid",)

    def __init__(self, grid: "RoutingGrid"):
        self._grid = grid

    def __getitem__(self, layer: int) -> _LayerView:
        return _LayerView(self._grid, layer)


class RoutingGrid:
    """3D grid for routing with obstacle tracking and congestion awareness.

    Uses NumPy arrays for high-performance cell access and vectorized operations.

    Grid Modes:
    - Standard: Uses rules.grid_resolution, adds clearance during routing
    - Expanded: Pre-expands obstacles by full clearance, allows coarser grid

    The expanded mode achieves ~4x speedup by:
    1. Using trace_width as grid resolution instead of clearance/2
    2. Pre-expanding all obstacles to include clearance zones
    3. Eliminating per-segment clearance checks during routing
    """

    def __init__(
        self,
        width: float,
        height: float,
        rules: DesignRules,
        origin_x: float = 0,
        origin_y: float = 0,
        layer_stack: LayerStack | None = None,
        expanded_obstacles: bool = False,
        resolution_override: float | None = None,
        thread_safe: bool = False,
    ):
        """Initialize routing grid.

        Args:
            width, height: Board dimensions in mm
            rules: Design rules for routing
            origin_x, origin_y: Board origin
            layer_stack: Layer configuration
            expanded_obstacles: If True, pre-expand obstacles by clearance
                               and allow coarser grid resolution
            resolution_override: Override grid resolution (None = auto from rules)
            thread_safe: If True, enable thread-safe mode with locking for
                        concurrent access. Disabled by default for performance.
        """
        self.width = width
        self.height = height
        self.rules = rules
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.expanded_obstacles = expanded_obstacles

        # Calculate effective resolution
        if resolution_override is not None:
            self.resolution = resolution_override
        elif expanded_obstacles:
            # In expanded mode, we can use trace_width as resolution
            # since clearances are pre-computed in obstacle expansion
            self.resolution = max(rules.trace_width, rules.grid_resolution)
        else:
            self.resolution = rules.grid_resolution

        # Layer stack (default to 2-layer for backward compatibility)
        self.layer_stack = layer_stack or LayerStack.two_layer()
        self.num_layers = self.layer_stack.num_layers

        # Build layer enum to grid index mapping
        self._layer_to_index: dict[int, int] = {}
        self._index_to_layer: dict[int, int] = {}
        for layer_def in self.layer_stack.layers:
            for layer_enum in Layer:
                if layer_enum.kicad_name == layer_def.name:
                    self._layer_to_index[layer_enum.value] = layer_def.index
                    self._index_to_layer[layer_def.index] = layer_enum.value
                    break

        # Grid dimensions
        self.cols = int(width / self.resolution) + 1
        self.rows = int(height / self.resolution) + 1

        # NumPy arrays for cell attributes: [layer, y, x]
        grid_shape = (self.num_layers, self.rows, self.cols)
        self._blocked = np.zeros(grid_shape, dtype=np.bool_)
        self._net = np.zeros(grid_shape, dtype=np.int32)
        self._usage_count = np.zeros(grid_shape, dtype=np.int16)
        self._history_cost = np.zeros(grid_shape, dtype=np.float32)
        self._is_obstacle = np.zeros(grid_shape, dtype=np.bool_)
        self._is_zone = np.zeros(grid_shape, dtype=np.bool_)
        self._pad_blocked = np.zeros(grid_shape, dtype=np.bool_)
        self._original_net = np.zeros(grid_shape, dtype=np.int32)

        # Sparse storage for zone IDs (most cells don't have zones)
        self._zone_ids: dict[tuple[int, int, int], str] = {}

        # Backward-compatible grid accessor
        self.grid = _GridView(self)

        # Congestion tracking: coarser grid for density
        self.congestion_size = rules.congestion_grid_size
        self.congestion_cols = max(1, self.cols // self.congestion_size)
        self.congestion_rows = max(1, self.rows // self.congestion_size)

        # Congestion counts using NumPy: [layer, cy, cx]
        self._congestion = np.zeros(
            (self.num_layers, self.congestion_rows, self.congestion_cols), dtype=np.int32
        )

        # Track placed routes for net assignment
        self.routes: list[Route] = []

        # Alias for backward compatibility
        self.layers = self.num_layers

        # Pre-computed clearance masks for common radii
        self._clearance_masks: dict[int, np.ndarray] = {}

        # Thread safety support
        self._thread_safe = thread_safe
        self._lock: threading.RLock | None = threading.RLock() if thread_safe else None

        # Corridor preference tracking for two-phase routing
        # Maps net ID to Corridor object (from sparse.py)
        # Use Any type hint to avoid circular import; actual type checked at runtime
        self._corridor_preferences: dict[int, any] = {}
        self._corridor_penalty: float = 5.0  # Default penalty for leaving corridor

        # Store original pad geometry for geometric clearance validation
        # Issue #750: Grid-based checking is approximate; we need precise geometry
        # for post-route validation to catch diagonal segment violations
        self._pads: list[Pad] = []

    @property
    def congestion(self) -> np.ndarray:
        """Return congestion array (backward compatible)."""
        return self._congestion

    @property
    def thread_safe(self) -> bool:
        """Return whether thread-safe mode is enabled."""
        return self._thread_safe

    @contextmanager
    def locked(self) -> Iterator["RoutingGrid"]:
        """Context manager for exclusive grid access.

        Use this when performing multiple grid operations that must be atomic.
        In non-thread-safe mode, this is a no-op that yields immediately.

        Example:
            with grid.locked():
                grid.mark_route(route1)
                grid.mark_route(route2)

        Yields:
            self: The grid instance for method chaining
        """
        if self._lock is not None:
            with self._lock:
                yield self
        else:
            yield self

    @contextmanager
    def _acquire_lock(self) -> Iterator[None]:
        """Internal context manager for acquiring lock if thread-safe mode is enabled.

        This is used internally by grid methods that modify state.
        """
        if self._lock is not None:
            with self._lock:
                yield
        else:
            yield

    def _get_clearance_mask(self, radius: int) -> np.ndarray:
        """Get or create a circular clearance mask for given radius."""
        if radius not in self._clearance_masks:
            y, x = np.ogrid[-radius : radius + 1, -radius : radius + 1]
            mask = x * x + y * y <= radius * radius
            self._clearance_masks[radius] = mask
        return self._clearance_masks[radius]

    def layer_to_index(self, layer_enum_value: int) -> int:
        """Map Layer enum value to grid index."""
        if layer_enum_value in self._layer_to_index:
            return self._layer_to_index[layer_enum_value]
        raise RoutingError(
            "Layer value not in stack",
            context={
                "layer_value": layer_enum_value,
                "available": list(self._layer_to_index.keys()),
            },
        )

    def index_to_layer(self, index: int) -> int:
        """Map grid index to Layer enum value."""
        if index in self._index_to_layer:
            return self._index_to_layer[index]
        raise RoutingError(
            "Grid index not in stack",
            context={"index": index, "available": list(self._index_to_layer.keys())},
        )

    def get_routable_indices(self) -> list[int]:
        """Get grid indices of routable signal layers."""
        return self.layer_stack.get_routable_indices()

    def is_plane_layer(self, index: int) -> bool:
        """Check if grid index is a plane layer (no routing)."""
        return self.layer_stack.is_plane_layer(index)

    def _update_congestion(self, gx: int, gy: int, layer: int, delta: int = 1) -> None:
        """Update congestion count for the region containing (gx, gy)."""
        cx = min(gx // self.congestion_size, self.congestion_cols - 1)
        cy = min(gy // self.congestion_size, self.congestion_rows - 1)
        self._congestion[layer, cy, cx] += delta

    def get_congestion(self, gx: int, gy: int, layer: int) -> float:
        """Get congestion level [0, 1] for a grid cell's region."""
        cx = min(gx // self.congestion_size, self.congestion_cols - 1)
        cy = min(gy // self.congestion_size, self.congestion_rows - 1)
        count = self._congestion[layer, cy, cx]
        max_cells = self.congestion_size * self.congestion_size
        return min(1.0, count / max_cells)

    def get_congestion_map(self) -> dict[str, float]:
        """Get congestion statistics for all regions using vectorized operations."""
        max_cells = self.congestion_size * self.congestion_size
        density = self._congestion / max_cells

        return {
            "max_congestion": float(np.max(density)),
            "avg_congestion": float(np.mean(density)),
            "congested_regions": int(np.sum(density > self.rules.congestion_threshold)),
        }

    def world_to_grid(self, x: float, y: float) -> tuple[int, int]:
        """Convert world coordinates to grid indices.

        Uses round() instead of int() to avoid floating point precision errors.
        For example, (112.6 - 75.0) / 0.1 = 375.9999999999999 should map to 376,
        but int() would truncate to 375, causing off-by-one grid cell errors.
        """
        gx = round((x - self.origin_x) / self.resolution)
        gy = round((y - self.origin_y) / self.resolution)
        return (max(0, min(gx, self.cols - 1)), max(0, min(gy, self.rows - 1)))

    def grid_to_world(self, gx: int, gy: int) -> tuple[float, float]:
        """Convert grid indices to world coordinates."""
        return (
            self.origin_x + gx * self.resolution,
            self.origin_y + gy * self.resolution,
        )

    def add_obstacle(self, obs: Obstacle) -> None:
        """Mark grid cells as blocked by an obstacle.

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            # Include trace half-width so trace edges maintain clearance from obstacle
            clearance = obs.clearance + self.rules.trace_clearance + self.rules.trace_width / 2

            # Calculate affected grid region
            x1 = obs.x - obs.width / 2 - clearance
            y1 = obs.y - obs.height / 2 - clearance
            x2 = obs.x + obs.width / 2 + clearance
            y2 = obs.y + obs.height / 2 + clearance

            gx1, gy1 = self.world_to_grid(x1, y1)
            gx2, gy2 = self.world_to_grid(x2, y2)

            layer_idx = self.layer_to_index(obs.layer.value)

            for gy in range(gy1, gy2 + 1):
                for gx in range(gx1, gx2 + 1):
                    if 0 <= gx < self.cols and 0 <= gy < self.rows:
                        self.grid[layer_idx][gy][gx].blocked = True

    def add_pad(self, pad: Pad) -> None:
        """Add a pad as an obstacle (except for its own net).

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            self._add_pad_unsafe(pad)

    def _add_pad_unsafe(self, pad: Pad) -> None:
        """Internal pad addition without locking."""
        # Store pad geometry for geometric clearance validation (Issue #750)
        self._pads.append(pad)

        # Clearance model: trace clearance + trace half-width from pad edge.
        # The pathfinder checks if the trace CENTER can be placed at a cell,
        # so we must block cells where the trace edge would violate clearance.
        # If we only blocked trace_clearance, a trace center placed at the boundary
        # would have its edge at (trace_clearance - trace_width/2) from the pad,
        # violating the required clearance.
        clearance = self.rules.trace_clearance + self.rules.trace_width / 2

        if pad.through_hole:
            if pad.width > 0 and pad.height > 0:
                effective_width = pad.width
                effective_height = pad.height
            elif pad.drill > 0:
                effective_width = pad.drill + 0.7
                effective_height = effective_width
            else:
                effective_width = 1.7
                effective_height = 1.7
        else:
            effective_width = pad.width
            effective_height = pad.height

        x1 = pad.x - effective_width / 2 - clearance
        y1 = pad.y - effective_height / 2 - clearance
        x2 = pad.x + effective_width / 2 + clearance
        y2 = pad.y + effective_height / 2 + clearance

        gx1, gy1 = self.world_to_grid(x1, y1)
        gx2, gy2 = self.world_to_grid(x2, y2)

        # PTH pads block all layers, SMD pads block only their layer
        if pad.through_hole:
            layers_to_block = list(range(self.num_layers))
        else:
            layers_to_block = [self.layer_to_index(pad.layer.value)]

        # Get center cell coordinates
        center_gx, center_gy = self.world_to_grid(pad.x, pad.y)

        # Calculate pad metal area bounds (without clearance)
        metal_x1 = pad.x - effective_width / 2
        metal_y1 = pad.y - effective_height / 2
        metal_x2 = pad.x + effective_width / 2
        metal_y2 = pad.y + effective_height / 2
        metal_gx1, metal_gy1 = self.world_to_grid(metal_x1, metal_y1)
        metal_gx2, metal_gy2 = self.world_to_grid(metal_x2, metal_y2)

        for layer_idx in layers_to_block:
            for gy in range(gy1, gy2 + 1):
                for gx in range(gx1, gx2 + 1):
                    if 0 <= gx < self.cols and 0 <= gy < self.rows:
                        cell = self.grid[layer_idx][gy][gx]
                        cell.blocked = True
                        # Mark as pad-blocked so route rip-up won't corrupt it
                        cell.pad_blocked = True
                        cell.original_net = pad.net

                        is_metal_area = (
                            metal_gx1 <= gx <= metal_gx2 and metal_gy1 <= gy <= metal_gy2
                        )

                        if is_metal_area:
                            if cell.net == 0:
                                cell.net = pad.net
                            elif cell.net != pad.net and pad.net != 0:
                                cell.is_obstacle = True
                        else:
                            if pad.net == 0:
                                if cell.net != 0:
                                    cell.is_obstacle = True
                            elif cell.net == 0:
                                cell.net = pad.net
                            elif cell.net != pad.net:
                                cell.is_obstacle = True

            # Always mark the center cell with this pad's net
            if 0 <= center_gx < self.cols and 0 <= center_gy < self.rows:
                center_cell = self.grid[layer_idx][center_gy][center_gx]
                center_cell.net = pad.net
                center_cell.original_net = pad.net

    def add_keepout(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layers: list[Layer] | None = None,
    ) -> None:
        """Add a keepout region.

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            if layers is None:
                layer_indices = self.get_routable_indices()
            else:
                layer_indices = [self.layer_to_index(layer.value) for layer in layers]

            gx1, gy1 = self.world_to_grid(x1, y1)
            gx2, gy2 = self.world_to_grid(x2, y2)

            for layer_idx in layer_indices:
                for gy in range(gy1, gy2 + 1):
                    for gx in range(gx1, gx2 + 1):
                        if 0 <= gx < self.cols and 0 <= gy < self.rows:
                            self.grid[layer_idx][gy][gx].blocked = True

    def is_blocked(self, gx: int, gy: int, layer: Layer, net: int = 0) -> bool:
        """Check if a cell is blocked for routing."""
        if not (0 <= gx < self.cols and 0 <= gy < self.rows):
            return True
        layer_idx = self.layer_to_index(layer.value)
        cell = self.grid[layer_idx][gy][gx]
        if cell.blocked:
            return cell.net == 0 or cell.net != net
        return False

    def validate_segment_clearance(
        self,
        seg: Segment,
        exclude_net: int,
        min_clearance: float | None = None,
    ) -> tuple[bool, float, tuple[float, float] | None]:
        """Validate geometric clearance of a segment against all obstacles.

        This performs precise geometric distance calculations to catch violations
        that grid-based checking misses, particularly for diagonal segments.
        Issue #750: Grid discretization causes diagonal segments to pass through
        obstacle corners that weren't detected during A* search.

        Args:
            seg: The segment to validate
            exclude_net: Net ID to exclude (same-net elements don't violate clearance)
            min_clearance: Minimum required clearance (default: rules.trace_clearance)

        Returns:
            Tuple of (is_valid, actual_clearance, violation_location)
            - is_valid: True if segment meets clearance requirements
            - actual_clearance: Minimum clearance found (negative if overlapping)
            - violation_location: (x, y) of worst violation, or None if valid
        """
        import math

        if min_clearance is None:
            min_clearance = self.rules.trace_clearance

        # Segment half-width for edge-to-edge distance calculation
        seg_half_width = seg.width / 2

        min_actual_clearance = float("inf")
        violation_loc: tuple[float, float] | None = None

        # Check against all stored pads
        for pad in self._pads:
            # Skip same-net pads (clearance not required within same net)
            if pad.net == exclude_net:
                continue

            # Skip pads on different layers (unless PTH)
            if not pad.through_hole:
                # Convert layer for comparison
                pad_layer_idx = self.layer_to_index(pad.layer.value)
                seg_layer_idx = self.layer_to_index(seg.layer.value)
                if pad_layer_idx != seg_layer_idx:
                    continue

            # Calculate distance from segment to pad center
            # Use the pad's larger dimension as radius for conservative check
            pad_radius = max(pad.width, pad.height) / 2

            # Point-to-segment distance calculation
            dist = self._point_to_segment_distance(pad.x, pad.y, seg.x1, seg.y1, seg.x2, seg.y2)

            # Edge-to-edge clearance
            clearance = dist - seg_half_width - pad_radius

            if clearance < min_actual_clearance:
                min_actual_clearance = clearance
                if clearance < min_clearance:
                    violation_loc = (pad.x, pad.y)

        # Check against segments from existing routes
        for route in self.routes:
            # Skip same-net routes
            if route.net == exclude_net:
                continue

            for other_seg in route.segments:
                # Skip segments on different layers
                if other_seg.layer != seg.layer:
                    continue

                # Segment-to-segment distance
                dist = self._segment_to_segment_distance(
                    seg.x1,
                    seg.y1,
                    seg.x2,
                    seg.y2,
                    other_seg.x1,
                    other_seg.y1,
                    other_seg.x2,
                    other_seg.y2,
                )

                # Edge-to-edge clearance (both segment half-widths)
                clearance = dist - seg_half_width - other_seg.width / 2

                if clearance < min_actual_clearance:
                    min_actual_clearance = clearance
                    if clearance < min_clearance:
                        # Violation location at midpoint
                        violation_loc = (
                            (seg.x1 + seg.x2 + other_seg.x1 + other_seg.x2) / 4,
                            (seg.y1 + seg.y2 + other_seg.y1 + other_seg.y2) / 4,
                        )

            # Check against vias from existing routes
            for via in route.vias:
                via_radius = via.diameter / 2

                # Point-to-segment distance for via
                dist = self._point_to_segment_distance(via.x, via.y, seg.x1, seg.y1, seg.x2, seg.y2)

                clearance = dist - seg_half_width - via_radius

                if clearance < min_actual_clearance:
                    min_actual_clearance = clearance
                    if clearance < min_clearance:
                        violation_loc = (via.x, via.y)

        is_valid = min_actual_clearance >= min_clearance
        return is_valid, min_actual_clearance, violation_loc

    def _point_to_segment_distance(
        self,
        px: float,
        py: float,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> float:
        """Calculate the distance from a point to a line segment."""
        import math

        # Vector from p1 to p2
        dx = x2 - x1
        dy = y2 - y1

        # Length squared of segment
        len_sq = dx * dx + dy * dy

        if len_sq == 0:
            # Segment is a point
            return math.sqrt((px - x1) ** 2 + (py - y1) ** 2)

        # Parameter t for the closest point on the line
        t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / len_sq))

        # Closest point on segment
        closest_x = x1 + t * dx
        closest_y = y1 + t * dy

        # Distance from point to closest point
        return math.sqrt((px - closest_x) ** 2 + (py - closest_y) ** 2)

    def _segment_to_segment_distance(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        x3: float,
        y3: float,
        x4: float,
        y4: float,
    ) -> float:
        """Calculate minimum distance between two line segments."""
        # Check all four endpoint-to-segment distances
        d1 = self._point_to_segment_distance(x1, y1, x3, y3, x4, y4)
        d2 = self._point_to_segment_distance(x2, y2, x3, y3, x4, y4)
        d3 = self._point_to_segment_distance(x3, y3, x1, y1, x2, y2)
        d4 = self._point_to_segment_distance(x4, y4, x1, y1, x2, y2)

        return min(d1, d2, d3, d4)

    def mark_route(self, route: Route) -> None:
        """Mark a route's cells as used.

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            total_clearance = self.rules.trace_width / 2 + self.rules.trace_clearance
            clearance_cells = int(total_clearance / self.resolution) + 1

            for seg in route.segments:
                self._mark_segment(seg, clearance_cells=clearance_cells)
            for via in route.vias:
                self._mark_via(via)
            self.routes.append(route)

    def _mark_segment(self, seg: Segment, clearance_cells: int = 1) -> None:
        """Mark cells along a segment as blocked (with clearance buffer)."""
        gx1, gy1 = self.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = self.world_to_grid(seg.x2, seg.y2)

        layer_idx = self.layer_to_index(seg.layer.value)
        marked_cells: set[tuple[int, int]] = set()

        def mark_with_clearance(gx: int, gy: int) -> None:
            for dy in range(-clearance_cells, clearance_cells + 1):
                for dx in range(-clearance_cells, clearance_cells + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        cell = self.grid[layer_idx][ny][nx]
                        if not cell.blocked:
                            # First time blocking - this is a route cell
                            marked_cells.add((nx, ny))
                            cell.net = seg.net
                        # else: cell already blocked (by pad), don't change net
                        cell.blocked = True

        # Simple line marking
        if gx1 == gx2:  # Vertical
            for gy in range(min(gy1, gy2), max(gy1, gy2) + 1):
                mark_with_clearance(gx1, gy)
        elif gy1 == gy2:  # Horizontal
            for gx in range(min(gx1, gx2), max(gx1, gx2) + 1):
                mark_with_clearance(gx, gy1)
        else:  # Diagonal - use Bresenham
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                mark_with_clearance(gx, gy)
                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy

        # Update congestion for all newly marked cells
        for nx, ny in marked_cells:
            self._update_congestion(nx, ny, layer_idx)

    def _mark_via(self, via: Via) -> None:
        """Mark cells around a via as blocked on ALL layers (through-hole via)."""
        gx, gy = self.world_to_grid(via.x, via.y)
        # Include trace half-width so trace edges maintain via_clearance from via edge
        radius = int(
            (via.diameter / 2 + self.rules.via_clearance + self.rules.trace_width / 2)
            / self.resolution
        )

        for layer_idx in range(self.num_layers):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        cell = self.grid[layer_idx][ny][nx]
                        if not cell.blocked:
                            self._update_congestion(nx, ny, layer_idx)
                            cell.net = via.net
                        cell.blocked = True

    def unmark_route(self, route: Route) -> None:
        """Unmark a route's cells (rip-up). Reverses mark_route().

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            total_clearance = self.rules.trace_width / 2 + self.rules.trace_clearance
            clearance_cells = int(total_clearance / self.resolution) + 1

            for seg in route.segments:
                self._unmark_segment(seg, clearance_cells=clearance_cells)
            for via in route.vias:
                self._unmark_via(via)

            if route in self.routes:
                self.routes.remove(route)

    def _unmark_segment(self, seg: Segment, clearance_cells: int = 1) -> None:
        """Unmark cells along a segment (clear blocked status and net)."""
        gx1, gy1 = self.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = self.world_to_grid(seg.x2, seg.y2)

        layer_idx = self.layer_to_index(seg.layer.value)

        def unmark_with_clearance(gx: int, gy: int) -> None:
            for dy in range(-clearance_cells, clearance_cells + 1):
                for dx in range(-clearance_cells, clearance_cells + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        cell = self.grid[layer_idx][ny][nx]
                        if cell.pad_blocked:
                            # Don't unblock pad cells, just restore original net
                            cell.net = cell.original_net
                        elif cell.net == seg.net:
                            cell.blocked = False
                            cell.net = 0

        if gx1 == gx2:
            for gy in range(min(gy1, gy2), max(gy1, gy2) + 1):
                unmark_with_clearance(gx1, gy)
        elif gy1 == gy2:
            for gx in range(min(gx1, gx2), max(gx1, gx2) + 1):
                unmark_with_clearance(gx, gy1)
        else:
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                unmark_with_clearance(gx, gy)
                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy

    def _unmark_via(self, via: Via) -> None:
        """Unmark cells around a via on ALL layers."""
        gx, gy = self.world_to_grid(via.x, via.y)
        # Include trace half-width to match _mark_via calculation
        radius = int(
            (via.diameter / 2 + self.rules.via_clearance + self.rules.trace_width / 2)
            / self.resolution
        )

        for layer_idx in range(self.num_layers):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        cell = self.grid[layer_idx][ny][nx]
                        if cell.pad_blocked:
                            # Don't unblock pad cells, just restore original net
                            cell.net = cell.original_net
                        elif cell.net == via.net:
                            cell.blocked = False
                            cell.net = 0

    # =========================================================================
    # NEGOTIATED CONGESTION ROUTING SUPPORT
    # =========================================================================

    def reset_route_usage(self) -> None:
        """Reset all usage counts (start of new negotiation iteration).

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            self._usage_count.fill(0)

    def mark_route_usage(
        self, route: Route, net_cells: dict[int, set] | None = None
    ) -> set[tuple[int, int, int]]:
        """Mark cells used by a route, incrementing usage count.

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            cells_used: set[tuple[int, int, int]] = set()

            for seg in route.segments:
                seg_cells = self._get_segment_cells(seg)
                cells_used.update(seg_cells)

            for via in route.vias:
                via_cells = self._get_via_cells(via)
                cells_used.update(via_cells)

            for gx, gy, layer_idx in cells_used:
                if 0 <= gx < self.cols and 0 <= gy < self.rows:
                    self.grid[layer_idx][gy][gx].usage_count += 1

            if net_cells is not None:
                if route.net not in net_cells:
                    net_cells[route.net] = set()
                net_cells[route.net].update(cells_used)

            return cells_used

    def unmark_route_usage(self, route: Route, net_cells: dict[int, set] | None = None) -> None:
        """Remove a route's usage (rip-up), decrementing usage count.

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            cells_used: set[tuple[int, int, int]] = set()

            for seg in route.segments:
                seg_cells = self._get_segment_cells(seg)
                cells_used.update(seg_cells)

            for via in route.vias:
                via_cells = self._get_via_cells(via)
                cells_used.update(via_cells)

            for gx, gy, layer_idx in cells_used:
                if 0 <= gx < self.cols and 0 <= gy < self.rows:
                    cell = self.grid[layer_idx][gy][gx]
                    cell.usage_count = max(0, cell.usage_count - 1)

            if net_cells is not None and route.net in net_cells:
                net_cells[route.net] -= cells_used

    def _get_segment_cells(self, seg: Segment) -> set[tuple[int, int, int]]:
        """Get all grid cells occupied by a segment."""
        cells: set[tuple[int, int, int]] = set()
        gx1, gy1 = self.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = self.world_to_grid(seg.x2, seg.y2)
        layer_idx = self.layer_to_index(seg.layer.value)

        if gx1 == gx2:
            for gy in range(min(gy1, gy2), max(gy1, gy2) + 1):
                cells.add((gx1, gy, layer_idx))
        elif gy1 == gy2:
            for gx in range(min(gx1, gx2), max(gx1, gx2) + 1):
                cells.add((gx, gy1, layer_idx))
        else:
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                cells.add((gx, gy, layer_idx))
                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy
        return cells

    def _get_via_cells(self, via: Via) -> set[tuple[int, int, int]]:
        """Get all grid cells occupied by a via (all layers for through-hole)."""
        cells: set[tuple[int, int, int]] = set()
        gx, gy = self.world_to_grid(via.x, via.y)
        for layer_idx in range(self.num_layers):
            cells.add((gx, gy, layer_idx))
        return cells

    def find_overused_cells(self) -> list[tuple[int, int, int, int]]:
        """Find cells with usage_count > 1 (resource conflicts)."""
        # Find all overused cells using NumPy
        overused_mask = self._usage_count > 1
        layer_indices, y_indices, x_indices = np.where(overused_mask)

        # Build result list with usage counts
        overused = []
        for layer_idx, gy, gx in zip(layer_indices, y_indices, x_indices, strict=True):
            usage = int(self._usage_count[layer_idx, gy, gx])
            overused.append((int(gx), int(gy), int(layer_idx), usage))
        return overused

    def update_history_costs(self, history_increment: float = 1.0) -> None:
        """Increase history cost for overused cells (PathFinder-style).

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            # Vectorized update: add increment * (usage_count - 1) where usage_count > 1
            overused_mask = self._usage_count > 1
            increment = history_increment * (self._usage_count.astype(np.float32) - 1)
            self._history_cost += np.where(overused_mask, increment, 0)

    def get_negotiated_cost(
        self, gx: int, gy: int, layer: int, present_cost_factor: float = 1.0
    ) -> float:
        """Get the negotiated congestion cost for a cell."""
        if not (0 <= gx < self.cols and 0 <= gy < self.rows):
            return float("inf")

        cell = self.grid[layer][gy][gx]

        if cell.is_obstacle:
            return float("inf")

        present_cost = present_cost_factor * cell.usage_count
        history_cost = cell.history_cost

        return present_cost + history_cost

    def get_total_overflow(self) -> int:
        """Get total overflow (sum of usage_count - 1 for overused cells)."""
        # Vectorized calculation: sum of (usage - 1) where usage > 1
        overused = self._usage_count > 1
        return int(np.sum(np.where(overused, self._usage_count - 1, 0)))

    # =========================================================================
    # ZONE (COPPER POUR) SUPPORT
    # =========================================================================

    def add_zone_cells(
        self,
        zone: "Zone",
        filled_cells: set[tuple[int, int]],
        layer_index: int,
    ) -> None:
        """Mark grid cells as belonging to a zone.

        Thread-safe when thread_safe=True.

        Args:
            zone: Zone definition (for net and uuid)
            filled_cells: Set of (gx, gy) grid coordinates to mark
            layer_index: Grid layer index
        """
        from kicad_tools.schema.pcb import Zone as ZoneType  # noqa: F401

        with self._acquire_lock():
            for gx, gy in filled_cells:
                if 0 <= gx < self.cols and 0 <= gy < self.rows:
                    cell = self.grid[layer_index][gy][gx]
                    cell.is_zone = True
                    cell.zone_id = zone.uuid
                    cell.net = zone.net_number
                    # Zone copper is not an obstacle - routes can pass through same-net zones

    def clear_zones(self, layer_index: int | None = None) -> None:
        """Remove all zone markings from the grid.

        Thread-safe when thread_safe=True.

        Args:
            layer_index: If specified, only clear this layer. Otherwise clear all.
        """
        with self._acquire_lock():
            if layer_index is not None:
                layers_to_clear = [layer_index]
            else:
                layers_to_clear = list(range(self.num_layers))

            for layer_idx in layers_to_clear:
                # Find zone cells that should have net cleared
                zone_mask = self._is_zone[layer_idx]
                clear_net_mask = (
                    zone_mask & ~self._is_obstacle[layer_idx] & ~self._blocked[layer_idx]
                )

                # Clear nets where applicable
                self._net[layer_idx] = np.where(clear_net_mask, 0, self._net[layer_idx])

                # Clear zone flags
                self._is_zone[layer_idx] = False

                # Clear zone IDs for this layer from sparse storage
                keys_to_remove = [k for k in self._zone_ids if k[0] == layer_idx]
                for key in keys_to_remove:
                    del self._zone_ids[key]

    def get_zone_cells(self, layer_index: int, zone_id: str | None = None) -> set[tuple[int, int]]:
        """Get all cells belonging to zones on a layer.

        Args:
            layer_index: Grid layer index
            zone_id: If specified, only return cells for this zone

        Returns:
            Set of (gx, gy) coordinates
        """
        if zone_id is None:
            # Get all zone cells using NumPy
            y_indices, x_indices = np.where(self._is_zone[layer_index])
            return {(int(x), int(y)) for x, y in zip(x_indices, y_indices, strict=True)}
        else:
            # Filter by zone_id using sparse storage
            return {
                (k[2], k[1])
                for k, v in self._zone_ids.items()
                if k[0] == layer_index and v == zone_id
            }

    def is_zone_cell(self, gx: int, gy: int, layer_index: int) -> bool:
        """Check if a cell is part of a zone.

        Args:
            gx, gy: Grid coordinates
            layer_index: Grid layer index

        Returns:
            True if cell is marked as zone copper
        """
        if not (0 <= gx < self.cols and 0 <= gy < self.rows):
            return False
        return bool(self._is_zone[layer_index, gy, gx])

    # =========================================================================
    # CORRIDOR PREFERENCE SUPPORT (TWO-PHASE ROUTING)
    # =========================================================================

    def set_corridor_preference(
        self, corridor: any, net: int, penalty: float | None = None
    ) -> None:
        """Set a corridor preference for a net during two-phase routing.

        The pathfinder will add a cost penalty when routing this net
        outside its assigned corridor.

        Thread-safe when thread_safe=True.

        Args:
            corridor: The Corridor from global routing (sparse.Corridor)
            net: Net ID this corridor is assigned to
            penalty: Cost penalty multiplier for leaving corridor (default: 5.0)
        """
        with self._acquire_lock():
            self._corridor_preferences[net] = corridor
            if penalty is not None:
                self._corridor_penalty = penalty

    def clear_corridor_preference(self, net: int) -> None:
        """Remove corridor preference for a net.

        Thread-safe when thread_safe=True.

        Args:
            net: Net ID whose corridor preference to remove
        """
        with self._acquire_lock():
            self._corridor_preferences.pop(net, None)

    def clear_all_corridor_preferences(self) -> None:
        """Remove all corridor preferences.

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            self._corridor_preferences.clear()

    def get_corridor_cost(self, gx: int, gy: int, layer: int, net: int) -> float:
        """Get corridor cost penalty for a cell.

        Returns additional cost if the cell is outside the net's assigned
        corridor (if any). This guides detailed routing to stay within
        the corridor established during global routing.

        Args:
            gx, gy: Grid coordinates
            layer: Grid layer index
            net: Net being routed

        Returns:
            Additional cost (0 if inside corridor or no corridor assigned)
        """
        corridor = self._corridor_preferences.get(net)
        if corridor is None:
            return 0.0

        # Convert grid to world coordinates
        x, y = self.grid_to_world(gx, gy)

        # Check if point is inside corridor
        if corridor.contains_point(x, y, layer):
            return 0.0

        # Outside corridor - apply penalty
        return self._corridor_penalty

    def has_corridor_preference(self, net: int) -> bool:
        """Check if a net has an assigned corridor.

        Args:
            net: Net ID to check

        Returns:
            True if net has a corridor preference set
        """
        return net in self._corridor_preferences

    def get_corridor_statistics(self) -> dict:
        """Get statistics about corridor preferences.

        Returns:
            Dictionary with corridor stats
        """
        return {
            "corridors_assigned": len(self._corridor_preferences),
            "corridor_penalty": self._corridor_penalty,
            "nets_with_corridors": list(self._corridor_preferences.keys()),
        }

    # =========================================================================
    # BOARD EDGE CLEARANCE SUPPORT
    # =========================================================================

    def add_edge_keepout(
        self,
        edge_segments: list[tuple[tuple[float, float], tuple[float, float]]],
        clearance: float,
    ) -> int:
        """Block cells within clearance distance of board edge segments.

        This prevents routes from being placed too close to the board edge,
        which would violate copper-to-edge clearance DRC rules.

        Thread-safe when thread_safe=True.

        Args:
            edge_segments: List of (start, end) tuples defining edge line segments.
                          Each segment is ((x1, y1), (x2, y2)) in world coordinates.
            clearance: Edge clearance distance in mm.

        Returns:
            Number of cells blocked.
        """
        with self._acquire_lock():
            if clearance <= 0 or not edge_segments:
                return 0

            blocked_count = 0
            clearance_cells = int(clearance / self.resolution) + 1

            # Get all routable layer indices
            layer_indices = self.get_routable_indices()

            for (x1, y1), (x2, y2) in edge_segments:
                # Mark cells along each edge segment with clearance buffer
                blocked_count += self._mark_edge_segment_keepout(
                    x1, y1, x2, y2, clearance_cells, layer_indices
                )

            return blocked_count

    def _mark_edge_segment_keepout(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        clearance_cells: int,
        layer_indices: list[int],
    ) -> int:
        """Mark cells within clearance of a single edge segment as blocked.

        Uses Bresenham's algorithm to walk along the segment and blocks all
        cells within the clearance distance on all routable layers.

        Args:
            x1, y1: Start point in world coordinates
            x2, y2: End point in world coordinates
            clearance_cells: Number of grid cells for clearance buffer
            layer_indices: Grid indices of layers to block

        Returns:
            Number of cells blocked.
        """
        gx1, gy1 = self.world_to_grid(x1, y1)
        gx2, gy2 = self.world_to_grid(x2, y2)

        blocked_count = 0
        blocked_cells: set[tuple[int, int]] = set()

        def mark_with_clearance(gx: int, gy: int) -> None:
            """Mark cells within clearance radius of a point."""
            nonlocal blocked_count
            for dy in range(-clearance_cells, clearance_cells + 1):
                for dx in range(-clearance_cells, clearance_cells + 1):
                    nx, ny = gx + dx, gy + dy
                    if (nx, ny) in blocked_cells:
                        continue
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        # Check if within circular clearance (not square)
                        if dx * dx + dy * dy <= clearance_cells * clearance_cells:
                            blocked_cells.add((nx, ny))
                            for layer_idx in layer_indices:
                                cell = self.grid[layer_idx][ny][nx]
                                if not cell.blocked:
                                    cell.blocked = True
                                    cell.is_obstacle = True
                                    blocked_count += 1

        # Walk along the segment using Bresenham's algorithm
        if gx1 == gx2:  # Vertical line
            for gy in range(min(gy1, gy2), max(gy1, gy2) + 1):
                mark_with_clearance(gx1, gy)
        elif gy1 == gy2:  # Horizontal line
            for gx in range(min(gx1, gx2), max(gx1, gx2) + 1):
                mark_with_clearance(gx, gy1)
        else:  # Diagonal - use Bresenham
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                mark_with_clearance(gx, gy)
                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy

        return blocked_count

    # =========================================================================
    # FACTORY METHODS FOR OPTIMIZED GRID CONFIGURATIONS
    # =========================================================================

    @classmethod
    def create_expanded(
        cls,
        width: float,
        height: float,
        rules: DesignRules,
        origin_x: float = 0,
        origin_y: float = 0,
        layer_stack: "LayerStack | None" = None,
    ) -> "RoutingGrid":
        """Create a grid with expanded obstacles for faster routing.

        This factory method creates a grid optimized for performance:
        - Uses trace_width as grid resolution (coarser than clearance-based)
        - Pre-expands all obstacles to include clearance zones
        - Suitable for JLCPCB and similar tight-clearance designs

        Performance comparison (65x56mm board, 0.127mm clearance):
        - Standard grid (0.0635mm): ~900,000 cells, ~120s routing
        - Expanded grid (0.127mm): ~225,000 cells, ~30s routing

        Args:
            width, height: Board dimensions
            rules: Design rules
            origin_x, origin_y: Board origin
            layer_stack: Layer configuration

        Returns:
            RoutingGrid with expanded obstacle mode enabled
        """
        return cls(
            width=width,
            height=height,
            rules=rules,
            origin_x=origin_x,
            origin_y=origin_y,
            layer_stack=layer_stack,
            expanded_obstacles=True,
            resolution_override=max(rules.trace_width, rules.trace_clearance),
        )

    @classmethod
    def create_adaptive(
        cls,
        width: float,
        height: float,
        rules: DesignRules,
        origin_x: float = 0,
        origin_y: float = 0,
        layer_stack: "LayerStack | None" = None,
        target_cells: int = 500000,
    ) -> "RoutingGrid":
        """Create a grid with adaptive resolution based on board size.

        Automatically calculates resolution to keep total cells near target,
        balancing routing accuracy against performance.

        For JLCPCB-compatible boards (5mil clearance):
        - Small boards (<50mm): Fine resolution for accuracy
        - Large boards (>100mm): Coarser resolution for performance

        Args:
            width, height: Board dimensions
            rules: Design rules
            origin_x, origin_y: Board origin
            layer_stack: Layer configuration
            target_cells: Target number of grid cells (default: 500k)

        Returns:
            RoutingGrid with adaptive resolution
        """
        # Calculate resolution needed to achieve target cell count
        # cells = (width / res) * (height / res) * layers
        num_layers = (layer_stack or LayerStack.two_layer()).num_layers
        area = width * height

        # Solve for resolution: res = sqrt(area * layers / target_cells)
        optimal_res = (area * num_layers / target_cells) ** 0.5

        # Clamp to reasonable bounds
        min_res = rules.trace_clearance / 2  # Never finer than clearance/2
        max_res = rules.trace_width * 2  # Never coarser than 2x trace width

        resolution = max(min_res, min(max_res, optimal_res))

        # Use expanded obstacles if resolution is coarser than clearance
        use_expanded = resolution > rules.trace_clearance

        return cls(
            width=width,
            height=height,
            rules=rules,
            origin_x=origin_x,
            origin_y=origin_y,
            layer_stack=layer_stack,
            expanded_obstacles=use_expanded,
            resolution_override=resolution,
        )

    def add_pad_vectorized(self, pad: Pad) -> None:
        """Add a pad using vectorized NumPy operations for better performance.

        This method uses pre-computed circular masks and array slicing
        instead of per-cell loops, providing ~5x speedup for pad addition.

        Thread-safe when thread_safe=True.

        Args:
            pad: Pad to add to the grid
        """
        with self._acquire_lock():
            self._add_pad_vectorized_unsafe(pad)

    def _add_pad_vectorized_unsafe(self, pad: Pad) -> None:
        """Internal vectorized pad addition without locking."""
        # Clearance model: trace clearance + trace half-width from pad edge.
        # The pathfinder checks if the trace CENTER can be placed at a cell,
        # so we must block cells where the trace edge would violate clearance.
        clearance = self.rules.trace_clearance + self.rules.trace_width / 2

        # Determine effective dimensions
        if pad.through_hole:
            if pad.width > 0 and pad.height > 0:
                effective_width = pad.width
                effective_height = pad.height
            elif pad.drill > 0:
                effective_width = pad.drill + 0.7
                effective_height = effective_width
            else:
                effective_width = 1.7
                effective_height = 1.7
        else:
            effective_width = pad.width
            effective_height = pad.height

        # Calculate affected region in grid coordinates
        half_w = effective_width / 2 + clearance
        half_h = effective_height / 2 + clearance

        x1, y1 = pad.x - half_w, pad.y - half_h
        x2, y2 = pad.x + half_w, pad.y + half_h

        gx1, gy1 = self.world_to_grid(x1, y1)
        gx2, gy2 = self.world_to_grid(x2, y2)

        # Clamp to grid bounds
        gx1 = max(0, gx1)
        gy1 = max(0, gy1)
        gx2 = min(self.cols - 1, gx2)
        gy2 = min(self.rows - 1, gy2)

        # Determine affected layers
        if pad.through_hole:
            layers = list(range(self.num_layers))
        else:
            layers = [self.layer_to_index(pad.layer.value)]

        # Calculate pad metal area bounds (without clearance)
        metal_half_w = effective_width / 2
        metal_half_h = effective_height / 2
        metal_x1, metal_y1 = pad.x - metal_half_w, pad.y - metal_half_h
        metal_x2, metal_y2 = pad.x + metal_half_w, pad.y + metal_half_h
        metal_gx1, metal_gy1 = self.world_to_grid(metal_x1, metal_y1)
        metal_gx2, metal_gy2 = self.world_to_grid(metal_x2, metal_y2)

        # Get center coordinates
        center_gx, center_gy = self.world_to_grid(pad.x, pad.y)

        # Vectorized update for each layer
        for layer_idx in layers:
            # Block the entire clearance zone
            self._blocked[layer_idx, gy1 : gy2 + 1, gx1 : gx2 + 1] = True
            self._pad_blocked[layer_idx, gy1 : gy2 + 1, gx1 : gx2 + 1] = True
            self._original_net[layer_idx, gy1 : gy2 + 1, gx1 : gx2 + 1] = pad.net

            # Set net for metal area
            metal_gy1_clamped = max(0, metal_gy1)
            metal_gy2_clamped = min(self.rows - 1, metal_gy2)
            metal_gx1_clamped = max(0, metal_gx1)
            metal_gx2_clamped = min(self.cols - 1, metal_gx2)

            # Only set net where it's currently 0 (avoid overwriting other pads)
            metal_slice = (
                layer_idx,
                slice(metal_gy1_clamped, metal_gy2_clamped + 1),
                slice(metal_gx1_clamped, metal_gx2_clamped + 1),
            )
            net_slice = self._net[metal_slice]
            self._net[metal_slice] = np.where(net_slice == 0, pad.net, net_slice)

            # Mark center cell with this pad's net
            if 0 <= center_gx < self.cols and 0 <= center_gy < self.rows:
                self._net[layer_idx, center_gy, center_gx] = pad.net
                self._original_net[layer_idx, center_gy, center_gx] = pad.net

    def get_grid_statistics(self) -> dict:
        """Get statistics about grid usage and memory.

        Returns:
            Dict with grid statistics for performance analysis
        """
        total_cells = self.cols * self.rows * self.num_layers
        blocked_cells = int(np.sum(self._blocked))
        pad_cells = int(np.sum(self._pad_blocked))

        return {
            "resolution_mm": self.resolution,
            "cols": self.cols,
            "rows": self.rows,
            "layers": self.num_layers,
            "total_cells": total_cells,
            "blocked_cells": blocked_cells,
            "blocked_percent": round(100 * blocked_cells / total_cells, 1),
            "pad_cells": pad_cells,
            "expanded_obstacles": self.expanded_obstacles,
            "thread_safe": self._thread_safe,
            "memory_mb": round(
                (
                    self._blocked.nbytes
                    + self._net.nbytes
                    + self._usage_count.nbytes
                    + self._history_cost.nbytes
                    + self._is_obstacle.nbytes
                    + self._is_zone.nbytes
                    + self._pad_blocked.nbytes
                    + self._original_net.nbytes
                )
                / (1024 * 1024),
                2,
            ),
        }
