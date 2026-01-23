"""
A* pathfinding for PCB routing.

This module provides:
- AStarNode: Node for priority queue in A* search
- Router: A* pathfinder with multi-layer support and congestion awareness

The Router accepts a pluggable Heuristic for experimentation with
different routing strategies. See heuristics.py for available options.
"""

import heapq
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .grid import RoutingGrid
from .heuristics import DEFAULT_HEURISTIC, Heuristic, HeuristicContext
from .layers import Layer
from .primitives import Pad, Route, Segment, Via
from .rules import DEFAULT_NET_CLASS_MAP, DesignRules, NetClassRouting


@dataclass(order=True)
class AStarNode:
    """Node for A* priority queue."""

    f_score: float
    g_score: float = field(compare=False)
    x: int = field(compare=False)
    y: int = field(compare=False)
    layer: int = field(compare=False)
    parent: Optional["AStarNode"] = field(compare=False, default=None)
    via_from_parent: bool = field(compare=False, default=False)
    direction: tuple[int, int] = field(compare=False, default=(0, 0))  # (dx, dy) from parent


class Router:
    """A* pathfinder with multi-layer support and congestion awareness.

    The heuristic parameter allows experimentation with different routing
    strategies. Available heuristics include:
    - ManhattanHeuristic: Simple baseline (fast, may explore more nodes)
    - DirectionBiasHeuristic: Prefers straight paths
    - CongestionAwareHeuristic: Avoids congested areas (default)
    - WeightedCongestionHeuristic: Stronger congestion avoidance
    - GreedyHeuristic: Fast but suboptimal
    """

    def __init__(
        self,
        grid: RoutingGrid,
        rules: DesignRules,
        net_class_map: dict[str, NetClassRouting] | None = None,
        heuristic: Heuristic | None = None,
        diagonal_routing: bool = True,
    ):
        """
        Args:
            grid: The routing grid
            rules: Design rules for routing
            net_class_map: Mapping of net names to NetClassRouting
            heuristic: Heuristic for A* search (default: CongestionAwareHeuristic)
            diagonal_routing: Enable 45° diagonal routing (default: True).
                              When True, routes can use diagonal moves for shorter paths.
                              When False, routes use only orthogonal (Manhattan) moves.
        """
        self.grid = grid
        self.rules = rules
        self.net_class_map = net_class_map or DEFAULT_NET_CLASS_MAP
        self.heuristic = heuristic or DEFAULT_HEURISTIC
        self.diagonal_routing = diagonal_routing

        # Neighbor offsets: (dx, dy, dlayer, cost_multiplier)
        # Same layer moves - orthogonal directions
        self.neighbors_2d = [
            (1, 0, 0, 1.0),  # Right
            (-1, 0, 0, 1.0),  # Left
            (0, 1, 0, 1.0),  # Down
            (0, -1, 0, 1.0),  # Up
        ]

        # Add diagonal directions if enabled (45° routing)
        # Diagonal moves travel √2 ≈ 1.414x the distance of orthogonal moves
        if diagonal_routing:
            self.neighbors_2d.extend(
                [
                    (1, 1, 0, 1.414),  # Down-Right
                    (-1, 1, 0, 1.414),  # Down-Left
                    (1, -1, 0, 1.414),  # Up-Right
                    (-1, -1, 0, 1.414),  # Up-Left
                ]
            )

        # Pre-calculate trace clearance radius in grid cells
        # This is the total radius from trace centerline that must be clear:
        # - trace_width/2: half-width of the trace copper
        # - trace_clearance: required clearance from trace edge to obstacles
        # This enforces clearance as a hard constraint during routing.
        # Issue #553: Previously only checked trace_width/2, causing DRC violations
        # when traces were placed too close to obstacles.
        # Issue #864: Use round() before ceil() to avoid floating point errors
        # causing an extra cell of clearance (e.g., 0.30000000000000004 -> 4 cells
        # instead of 3 cells).
        self._trace_half_width_cells = max(
            1,
            math.ceil(
                round(
                    (self.rules.trace_width / 2 + self.rules.trace_clearance)
                    / self.grid.resolution,
                    6,
                )
            ),
        )

        # Pre-calculate via blocking radius in grid cells
        # Via needs diameter/2 + clearance from other objects (pads, traces, vias)
        # Issue #864: Use round() before ceil() to avoid floating point errors.
        self._via_half_cells = max(
            1,
            math.ceil(
                round(
                    (self.rules.via_diameter / 2 + self.rules.via_clearance) / self.grid.resolution,
                    6,
                )
            ),
        )

        # Pre-compute neighbor arrays for batch cost computation (Issue #963)
        # Store offsets and cost multipliers as NumPy arrays for vectorized operations
        self._neighbor_dx = np.array([dx for dx, _, _, _ in self.neighbors_2d], dtype=np.int32)
        self._neighbor_dy = np.array([dy for _, dy, _, _ in self.neighbors_2d], dtype=np.int32)
        self._neighbor_cost_mult = np.array(
            [cost_mult for _, _, _, cost_mult in self.neighbors_2d], dtype=np.float64
        )

        # Pre-compute via checking offsets for vectorized blocking check (Issue #966)
        # Store all (dx, dy) pairs within via radius for batch cell lookup
        via_r = self._via_half_cells
        via_offsets = [
            (dx, dy) for dy in range(-via_r, via_r + 1) for dx in range(-via_r, via_r + 1)
        ]
        self._via_offset_dx = np.array([dx for dx, _ in via_offsets], dtype=np.int32)
        self._via_offset_dy = np.array([dy for _, dy in via_offsets], dtype=np.int32)

        # Layer priority cache for via checks: check most-congested layers first
        # This enables faster rejection when via is blocked on congested layer
        self._layer_priority: list[int] | None = None

        # Via validity cache (Issue #966): caches whether via can be placed at (x, y, net)
        # Key: (gx, gy, net), Value: True if valid, False if blocked
        # Cache is cleared when routes are modified (invalidates blocking state)
        self._via_cache: dict[tuple[int, int, int], bool] = {}
        self._via_cache_enabled: bool = True

        # Issue #1016: Component pitch cache for per-component clearance
        # Computed lazily on first use
        self._component_pitches: dict[str, float] | None = None

        # Issue #1016: Pre-compute trace clearance radii for component-specific clearances
        # Maps clearance value (mm) to grid cell radius
        self._clearance_radii: dict[float, int] = {}
        self._precompute_clearance_radii()

    def _precompute_clearance_radii(self) -> None:
        """Pre-compute grid cell radii for all component-specific clearances.

        Issue #1016: Pre-computes clearance radii for:
        - Default trace clearance
        - Each per-component clearance
        - Fine-pitch clearance (if configured)

        This allows efficient lookup during routing.
        """
        # Always include default clearance
        clearances = {self.rules.trace_clearance}

        # Add per-component clearances
        for clearance in self.rules.component_clearances.values():
            clearances.add(clearance)

        # Add fine-pitch clearance if configured
        if self.rules.fine_pitch_clearance is not None:
            clearances.add(self.rules.fine_pitch_clearance)

        # Compute grid cell radius for each clearance value
        for clearance in clearances:
            radius = max(
                1,
                math.ceil(
                    round(
                        (self.rules.trace_width / 2 + clearance) / self.grid.resolution,
                        6,
                    )
                ),
            )
            self._clearance_radii[clearance] = radius

    def get_clearance_radius_cells(self, clearance_mm: float) -> int:
        """Get the trace clearance radius in grid cells for a given clearance.

        Args:
            clearance_mm: Clearance value in mm

        Returns:
            Radius in grid cells (at least 1)
        """
        # Check cache first
        if clearance_mm in self._clearance_radii:
            return self._clearance_radii[clearance_mm]

        # Compute and cache
        radius = max(
            1,
            math.ceil(
                round(
                    (self.rules.trace_width / 2 + clearance_mm) / self.grid.resolution,
                    6,
                )
            ),
        )
        self._clearance_radii[clearance_mm] = radius
        return radius

    @property
    def component_pitches(self) -> dict[str, float]:
        """Get component pin pitches for automatic fine-pitch detection.

        Issue #1016: Computed lazily on first access and cached.
        Used for per-component clearance validation.

        Returns:
            Dict mapping component reference to minimum pin pitch in mm.
        """
        if self._component_pitches is None:
            self._component_pitches = self.grid.compute_component_pitches()
        return self._component_pitches

    def invalidate_component_pitch_cache(self) -> None:
        """Invalidate the component pitch cache.

        Call this if pads are added or modified after Router initialization.
        """
        self._component_pitches = None

    def _get_net_class(self, net_name: str) -> NetClassRouting | None:
        """Get the net class for a net name."""
        return self.net_class_map.get(net_name)

    def _get_pad_metal_bounds(self, pad: Pad) -> tuple[int, int, int, int]:
        """Calculate the grid coordinate bounds of a pad's metal area.

        This is used to expand goal regions for off-grid pads, ensuring
        routes can reach pads even when their centers don't align with
        the routing grid (Issue #956).

        Returns:
            (gx_min, gy_min, gx_max, gy_max) grid coordinate bounds
        """
        # Calculate effective pad dimensions (same logic as grid._add_pad_unsafe)
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

        # Metal area bounds in world coordinates
        metal_x1 = pad.x - effective_width / 2
        metal_y1 = pad.y - effective_height / 2
        metal_x2 = pad.x + effective_width / 2
        metal_y2 = pad.y + effective_height / 2

        # Convert to grid coordinates using ceil/floor to ensure we only include
        # cells whose CENTER is inside the metal area (Issue #996).
        # Using round() would include cells that are merely nearby.
        resolution = self.grid.resolution
        origin_x = self.grid.origin_x
        origin_y = self.grid.origin_y

        gx1 = max(0, int(math.ceil((metal_x1 - origin_x) / resolution)))
        gy1 = max(0, int(math.ceil((metal_y1 - origin_y) / resolution)))
        gx2 = min(self.grid.cols - 1, int(math.floor((metal_x2 - origin_x) / resolution)))
        gy2 = min(self.grid.rows - 1, int(math.floor((metal_y2 - origin_y) / resolution)))

        return (gx1, gy1, gx2, gy2)

    def _is_trace_blocked(
        self, gx: int, gy: int, layer: int, net: int, allow_sharing: bool = False
    ) -> bool:
        """Check if placing a trace at this position would conflict.

        Unlike is_blocked which checks a single cell, this accounts for
        trace width by checking adjacent cells the trace would occupy.

        Uses vectorized NumPy operations for performance (Issue #962).
        Pre-computed clearance masks enable single-operation blocking checks
        instead of iterating over (2r+1)² cells per neighbor.

        Args:
            allow_sharing: If True (negotiated mode), allow routing through
                          non-obstacle blocked cells (they'll get high cost instead)
        """
        radius = self._trace_half_width_cells

        # Calculate region bounds
        x1 = gx - radius
        y1 = gy - radius
        x2 = gx + radius + 1
        y2 = gy + radius + 1

        # Check if region extends outside grid (any out-of-bounds cell blocks)
        if x1 < 0 or y1 < 0 or x2 > self.grid.cols or y2 > self.grid.rows:
            return True

        # Extract array slices for the region
        blocked_region = self.grid._blocked[layer, y1:y2, x1:x2]
        net_region = self.grid._net[layer, y1:y2, x1:x2]

        if allow_sharing:
            # Negotiated mode: more complex logic
            # Block if any cell is:
            # 1. Blocked AND is_obstacle AND different net
            # 2. Blocked AND NOT is_obstacle AND different net AND usage_count == 0 (static)
            obstacle_region = self.grid._is_obstacle[layer, y1:y2, x1:x2]
            usage_region = self.grid._usage_count[layer, y1:y2, x1:x2]

            # Different net mask (includes net == 0 which are no-net obstacles)
            different_net = net_region != net

            # Case 1: Blocked obstacles with different net
            obstacle_blocks = blocked_region & obstacle_region & different_net

            # Case 2: Blocked non-obstacles with different net AND static (usage == 0)
            static_blocks = blocked_region & ~obstacle_region & different_net & (usage_region == 0)

            return bool(np.any(obstacle_blocks | static_blocks))
        else:
            # Standard mode: block if any cell is blocked AND has different net
            # Issue #864: Same-net cells are passable (even overlapping clearance)
            # but different-net cells and obstacles (net=0 blocked cells) must block.
            blocked_different_net = blocked_region & (net_region != net)
            return bool(np.any(blocked_different_net))

    def _is_diagonal_corner_blocked(
        self, gx: int, gy: int, dx: int, dy: int, layer: int, net: int, allow_sharing: bool = False
    ) -> bool:
        """Check if diagonal move would cut through obstacle corners.

        When moving diagonally from (gx, gy) to (gx+dx, gy+dy), we must verify
        that both adjacent orthogonal cells are clear to prevent corner-cutting:

            B │ D      Moving from A to D diagonally requires
            ──┼──      both B (gx, gy+dy) and C (gx+dx, gy) to be clear
            A │ C

        Args:
            gx, gy: Current grid position
            dx, dy: Diagonal direction (both must be non-zero for diagonal move)
            layer: Current layer
            net: Net ID for same-net checking
            allow_sharing: If True, allow routing through non-obstacle blocked cells

        Returns:
            True if the diagonal move is blocked (would cut corner), False if clear.
        """
        # Only check for actual diagonal moves
        if dx == 0 or dy == 0:
            return False

        # Check the two adjacent orthogonal cells
        # Cell B: same x, new y
        # Cell C: new x, same y
        adjacent_cells = [
            (gx, gy + dy),  # B: vertical neighbor
            (gx + dx, gy),  # C: horizontal neighbor
        ]

        for cx, cy in adjacent_cells:
            # Check bounds
            if not (0 <= cx < self.grid.cols and 0 <= cy < self.grid.rows):
                return True  # Out of bounds = blocked

            cell = self.grid.grid[layer][cy][cx]

            if cell.blocked:
                if allow_sharing and not cell.is_obstacle:
                    # In negotiated mode, non-obstacle cells can be shared
                    # No-net pads (cell.net == 0) must always block other nets
                    # See issue #317: routes incorrectly allowed through no-net pads
                    if cell.net == 0:
                        if cell.usage_count == 0:
                            return True  # Static no-net obstacle (pad) - block
                    elif cell.net != net:
                        # Only allow sharing if this cell has been used by routes
                        # (usage_count > 0). Cells with usage_count == 0 are static
                        # obstacles like pads that should never be shared.
                        # See issue #174: pad clearance zones must block other nets.
                        if cell.usage_count == 0:
                            return True  # Static obstacle (pad) - block
                        continue  # Allow with cost penalty (routed cell)
                else:
                    # Standard mode (same logic as _is_trace_blocked)
                    # Issue #864: Same-net cells are passable, different nets block
                    if cell.net == net:
                        pass  # Same net - passable
                    else:
                        return True  # Different net or obstacle - blocked

        return False

    def _is_via_blocked(
        self, gx: int, gy: int, layer: int, net: int, allow_sharing: bool = False
    ) -> bool:
        """Check if placing a via at this position would conflict.

        Similar to _is_trace_blocked but uses the larger via clearance radius.
        Through-hole vias must be checked on ALL layers.

        Issue #966: Uses vectorized NumPy operations for ~2-3x speedup over
        the original nested loop implementation.

        Args:
            allow_sharing: If True (negotiated mode), allow routing through
                          non-obstacle blocked cells (they'll get high cost instead)
        """
        # Compute all cell coordinates within via radius using pre-computed offsets
        cx_arr = gx + self._via_offset_dx
        cy_arr = gy + self._via_offset_dy

        # Check bounds - if any cell is out of bounds, via is blocked
        in_bounds = (
            (cx_arr >= 0) & (cx_arr < self.grid.cols) & (cy_arr >= 0) & (cy_arr < self.grid.rows)
        )
        if not np.all(in_bounds):
            return True  # Some cells out of bounds

        # Batch lookup cell attributes using fancy indexing
        blocked_arr = self.grid._blocked[layer, cy_arr, cx_arr]

        # Fast path: if no cells are blocked, via is not blocked
        if not np.any(blocked_arr):
            return False

        # Some cells are blocked - need detailed checking
        # Get indices of blocked cells only
        blocked_indices = np.where(blocked_arr)[0]

        # Batch lookup additional attributes for blocked cells
        blocked_cx = cx_arr[blocked_indices]
        blocked_cy = cy_arr[blocked_indices]
        net_arr = self.grid._net[layer, blocked_cy, blocked_cx]

        if allow_sharing:
            # Negotiated mode: allow sharing non-obstacle cells
            is_obstacle_arr = self.grid._is_obstacle[layer, blocked_cy, blocked_cx]
            usage_arr = self.grid._usage_count[layer, blocked_cy, blocked_cx]

            for i in range(len(blocked_indices)):
                cell_net = net_arr[i]
                is_obstacle = is_obstacle_arr[i]
                usage = usage_arr[i]

                if is_obstacle:
                    return True  # Obstacles always block

                # No-net pads must always block
                if cell_net == 0:
                    if usage == 0:
                        return True  # Static no-net obstacle
                elif cell_net != net:
                    # Different net - only allow if cell was used by routes
                    if usage == 0:
                        return True  # Static obstacle (pad)
                # else: same net or routed cell - allow with cost
        else:
            # Standard mode: same-net passable, different nets block
            # Check if any blocked cell has different net
            different_net = net_arr != net
            if np.any(different_net):
                return True

        return False

    def _get_negotiated_cell_cost(
        self, gx: int, gy: int, layer: int, present_factor: float = 1.0
    ) -> float:
        """Get negotiated congestion cost for a cell."""
        return self.grid.get_negotiated_cost(gx, gy, layer, present_factor)

    def _get_layer_priority(self) -> list[int]:
        """Get layer indices sorted by congestion (most congested first).

        Issue #966: When checking if a via is blocked, checking congested
        layers first enables faster rejection since blocked cells are more
        likely on congested layers.

        Returns:
            List of layer indices sorted by decreasing congestion level.
        """
        if self._layer_priority is not None:
            return self._layer_priority

        # Calculate total congestion per layer
        congestion_per_layer = []
        for layer_idx in range(self.grid.num_layers):
            layer_congestion = np.sum(self.grid._congestion[layer_idx])
            congestion_per_layer.append((layer_idx, layer_congestion))

        # Sort by congestion (descending)
        congestion_per_layer.sort(key=lambda x: x[1], reverse=True)

        # Cache and return layer indices
        self._layer_priority = [layer_idx for layer_idx, _ in congestion_per_layer]
        return self._layer_priority

    def _invalidate_layer_priority(self) -> None:
        """Invalidate cached layer priority (call when congestion changes significantly)."""
        self._layer_priority = None

    def _check_via_placement_cached(
        self, gx: int, gy: int, net: int, allow_sharing: bool = False
    ) -> bool:
        """Check if a via can be placed at (gx, gy) for the given net, using cache.

        Issue #966: This method wraps via blocking checks with a cache to avoid
        redundant computation when the same position is checked multiple times
        during A* search.

        Args:
            gx, gy: Grid coordinates for via placement
            net: Net ID for the route
            allow_sharing: If True (negotiated mode), allow sharing

        Returns:
            True if via CAN be placed (all layers clear), False if blocked.
        """
        # Try cache first (only in non-sharing mode since sharing state can change)
        if self._via_cache_enabled and not allow_sharing:
            cache_key = (gx, gy, net)
            if cache_key in self._via_cache:
                return self._via_cache[cache_key]

        # Check all layers using priority ordering
        for check_layer in self._get_layer_priority():
            if self._is_via_blocked(gx, gy, check_layer, net, allow_sharing):
                # Cache the negative result
                if self._via_cache_enabled and not allow_sharing:
                    self._via_cache[(gx, gy, net)] = False
                return False

        # Via is valid on all layers - cache positive result
        if self._via_cache_enabled and not allow_sharing:
            self._via_cache[(gx, gy, net)] = True
        return True

    def clear_via_cache(self) -> None:
        """Clear the via validity cache.

        Call this when grid state changes (routes added/removed) to ensure
        cache doesn't return stale results.
        """
        self._via_cache.clear()

    def set_via_cache_enabled(self, enabled: bool) -> None:
        """Enable or disable via caching.

        Args:
            enabled: True to enable caching, False to disable.
        """
        self._via_cache_enabled = enabled
        if not enabled:
            self._via_cache.clear()

    def _get_congestion_cost(self, gx: int, gy: int, layer: int) -> float:
        """Get additional cost based on congestion at this location."""
        congestion = self.grid.get_congestion(gx, gy, layer)
        if congestion > self.rules.congestion_threshold:
            # Exponential penalty for congested areas
            excess = congestion - self.rules.congestion_threshold
            return self.rules.cost_congestion * (1.0 + excess * 2.0)
        return 0.0

    def _batch_congestion_costs(self, current_x: int, current_y: int, layer: int) -> np.ndarray:
        """Batch compute congestion costs for all 2D neighbors using vectorized NumPy.

        Issue #963: Pre-compute congestion costs for all neighbors in a single
        batch operation to reduce per-neighbor function call overhead.

        Args:
            current_x: Current grid x coordinate
            current_y: Current grid y coordinate
            layer: Current layer index

        Returns:
            Array of congestion costs indexed by neighbor offset index.
            Out-of-bounds neighbors get cost 0 (will be filtered anyway).
        """
        # Compute neighbor coordinates
        nx_arr = current_x + self._neighbor_dx
        ny_arr = current_y + self._neighbor_dy

        # Bounds mask - identify valid neighbors
        valid = (
            (nx_arr >= 0) & (nx_arr < self.grid.cols) & (ny_arr >= 0) & (ny_arr < self.grid.rows)
        )

        # Convert to congestion grid coordinates
        congestion_size = self.grid.congestion_size
        cx_arr = np.minimum(nx_arr // congestion_size, self.grid.congestion_cols - 1)
        cy_arr = np.minimum(ny_arr // congestion_size, self.grid.congestion_rows - 1)

        # Initialize costs array
        costs = np.zeros(len(self.neighbors_2d), dtype=np.float64)

        # Get valid indices
        valid_indices = np.where(valid)[0]
        if len(valid_indices) == 0:
            return costs

        # Batch lookup congestion counts using fancy indexing
        max_cells = congestion_size * congestion_size
        congestion_counts = self.grid._congestion[
            layer, cy_arr[valid_indices], cx_arr[valid_indices]
        ]
        congestion_levels = np.minimum(1.0, congestion_counts / max_cells)

        # Compute costs where congestion exceeds threshold
        threshold = self.rules.congestion_threshold
        exceeds = congestion_levels > threshold
        excess = np.maximum(0, congestion_levels - threshold)
        valid_costs = np.where(exceeds, self.rules.cost_congestion * (1.0 + excess * 2.0), 0.0)
        costs[valid_indices] = valid_costs

        return costs

    def _batch_turn_costs(self, current_direction: tuple[int, int]) -> np.ndarray:
        """Batch compute turn costs for all 2D neighbors using vectorized NumPy.

        Issue #963: Pre-compute turn costs for all neighbors in a single
        batch operation.

        Args:
            current_direction: Current direction as (dx, dy) tuple

        Returns:
            Array of turn costs indexed by neighbor offset index.
        """
        if current_direction == (0, 0):
            # No current direction - no turn penalty
            return np.zeros(len(self.neighbors_2d), dtype=np.float64)

        # Check which neighbors match the current direction
        dx_match = self._neighbor_dx == current_direction[0]
        dy_match = self._neighbor_dy == current_direction[1]
        matches = dx_match & dy_match

        # Turn cost where direction doesn't match
        return np.where(matches, 0.0, self.rules.cost_turn)

    def _batch_negotiated_costs(
        self,
        current_x: int,
        current_y: int,
        layer: int,
        present_cost_factor: float,
        skip_mask: np.ndarray,
    ) -> np.ndarray:
        """Batch compute negotiated costs for all 2D neighbors using vectorized NumPy.

        Issue #963: Pre-compute negotiated costs for all neighbors in a single
        batch operation.

        Args:
            current_x: Current grid x coordinate
            current_y: Current grid y coordinate
            layer: Current layer index
            present_cost_factor: Multiplier for current sharing penalty
            skip_mask: Boolean array indicating neighbors to skip (e.g., near pads)

        Returns:
            Array of negotiated costs indexed by neighbor offset index.
        """
        # Compute neighbor coordinates
        nx_arr = current_x + self._neighbor_dx
        ny_arr = current_y + self._neighbor_dy

        # Bounds mask combined with skip mask
        valid = (
            (nx_arr >= 0)
            & (nx_arr < self.grid.cols)
            & (ny_arr >= 0)
            & (ny_arr < self.grid.rows)
            & ~skip_mask
        )

        # Initialize costs array
        costs = np.zeros(len(self.neighbors_2d), dtype=np.float64)

        # Get valid indices
        valid_indices = np.where(valid)[0]
        if len(valid_indices) == 0:
            return costs

        # Batch lookup usage counts and history costs using fancy indexing
        usage_counts = self.grid._usage_count[layer, ny_arr[valid_indices], nx_arr[valid_indices]]
        history_costs = self.grid._history_cost[layer, ny_arr[valid_indices], nx_arr[valid_indices]]

        # Compute present cost + history cost
        present_costs = present_cost_factor * usage_counts
        costs[valid_indices] = present_costs + history_costs

        return costs

    def _is_zone_cell(self, gx: int, gy: int, layer: int) -> bool:
        """Check if a cell is part of a zone (copper pour)."""
        if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
            return False
        return self.grid.grid[layer][gy][gx].is_zone

    def _get_zone_net(self, gx: int, gy: int, layer: int) -> int:
        """Get the net number of a zone cell, or 0 if not a zone."""
        if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
            return 0
        cell = self.grid.grid[layer][gy][gx]
        if cell.is_zone:
            return cell.net
        return 0

    def _is_zone_blocked(self, gx: int, gy: int, layer: int, net: int) -> bool:
        """Check if routing through this zone cell is blocked.

        Zone cells allow routing through same-net zones but block
        routing through other-net zones.

        Args:
            gx, gy: Grid coordinates
            layer: Grid layer index
            net: Net number of the route being planned

        Returns:
            True if blocked (other-net zone), False if passable
        """
        if not self._is_zone_cell(gx, gy, layer):
            return False  # Not a zone, use normal blocking logic

        zone_net = self._get_zone_net(gx, gy, layer)

        # Same net: passable (can route through own zone copper)
        if zone_net == net:
            return False

        # Different net: blocked (cannot route through other-net zone)
        return True

    def _get_zone_cost(self, gx: int, gy: int, layer: int, net: int) -> float:
        """Get routing cost adjustment for zone cells.

        Same-net zones have reduced cost (encourage using zone copper).
        Different-net zones are blocked (handled elsewhere).

        Args:
            gx, gy: Grid coordinates
            layer: Grid layer index
            net: Net number of the route being planned

        Returns:
            Cost adjustment (0.0 for normal, negative for same-net zone)
        """
        if not self._is_zone_cell(gx, gy, layer):
            return 0.0

        zone_net = self._get_zone_net(gx, gy, layer)

        if zone_net == net:
            # Same net - encourage using zone copper with reduced cost
            return self.rules.cost_zone_same_net - 1.0  # Net reduction
        else:
            # Different net - should be blocked, but return high cost as fallback
            return 100.0

    def _get_layer_preference_cost(self, layer: int, net_class: NetClassRouting | None) -> float:
        """Get routing cost based on layer preferences (Issue #625).

        Applies cost modifiers based on the net class's layer preferences:
        - Preferred layers get a discount (cost multiplier 0.5)
        - Avoided layers get a penalty (cost multiplier from net_class)
        - Neutral layers have no adjustment

        Args:
            layer: Grid layer index
            net_class: NetClassRouting with layer preferences

        Returns:
            Cost multiplier (< 1.0 for preferred, > 1.0 for avoided, 1.0 for neutral)
        """
        if net_class is None:
            return 1.0

        # Check if this is a preferred layer
        if net_class.preferred_layers is not None:
            if layer in net_class.preferred_layers:
                return 0.5  # Discount for preferred layer

        # Check if this is an avoided layer
        if net_class.avoid_layers is not None:
            if layer in net_class.avoid_layers:
                return net_class.layer_cost_multiplier  # Penalty for avoided layer

        return 1.0  # Neutral

    def _is_layer_allowed(self, layer_idx: int) -> bool:
        """Check if routing on this layer is allowed (Issue #715).

        When allowed_layers is set in DesignRules, only those layers
        can be used for routing. This provides a hard constraint for
        single-layer or restricted-layer routing.

        Args:
            layer_idx: Grid layer index

        Returns:
            True if layer is allowed (or no restriction), False if blocked
        """
        if self.rules.allowed_layers is None:
            return True  # No restriction

        # Convert grid index to Layer enum value, then to KiCad name for comparison
        layer_value = self.grid.index_to_layer(layer_idx)
        layer = Layer(layer_value)
        return layer.kicad_name in self.rules.allowed_layers

    def _can_place_via_in_zones(self, gx: int, gy: int, net: int) -> bool:
        """Check if via placement is legal considering zones on all layers.

        A via can be placed if:
        - No zone on any layer, OR
        - All zones are same-net (via connects through same-net zones), OR
        - Via is placed where there's no zone copper

        Args:
            gx, gy: Grid coordinates
            net: Net number of the route being planned

        Returns:
            True if via can be placed, False if blocked by other-net zone
        """
        for layer_idx in range(self.grid.num_layers):
            if self._is_zone_cell(gx, gy, layer_idx):
                zone_net = self._get_zone_net(gx, gy, layer_idx)
                if zone_net != net:
                    # Via would pierce an other-net zone
                    return False
        return True

    def route(
        self,
        start: Pad,
        end: Pad,
        net_class: NetClassRouting | None = None,
        negotiated_mode: bool = False,
        present_cost_factor: float = 0.0,
        weight: float = 1.0,
    ) -> Route | None:
        """Route between two pads using congestion-aware A*.

        Args:
            start: Source pad
            end: Destination pad
            net_class: Optional net class for routing parameters
            negotiated_mode: If True, allow sharing resources with cost penalty
            present_cost_factor: Multiplier for current sharing penalty (increases each iteration)
            weight: A* weight factor (1.0 = optimal A*, >1.0 = faster but suboptimal)
                    Higher values explore fewer nodes but may miss optimal paths.
        """
        # Issue #966: Clear via cache at start of route (grid state may have changed)
        # Keep cache valid within this route call for same-position checks
        self.clear_via_cache()

        # Get net class if not provided
        if net_class is None:
            net_class = self._get_net_class(start.net_name)

        # Net class cost multiplier (lower = prefer this net's route)
        cost_mult = net_class.cost_multiplier if net_class else 1.0

        # In negotiated mode, allow resource sharing
        allow_sharing = negotiated_mode

        # Convert to grid coordinates
        start_gx, start_gy = self.grid.world_to_grid(start.x, start.y)
        end_gx, end_gy = self.grid.world_to_grid(end.x, end.y)

        # Convert Layer enum values to grid indices
        # For PTH pads, we can start/end on any routable layer
        routable_layers = self.grid.get_routable_indices()
        start_layer = self.grid.layer_to_index(start.layer.value)
        end_layer = self.grid.layer_to_index(end.layer.value)

        # Get all valid start/end layers for this pad type
        start_layers = routable_layers if start.through_hole else [start_layer]
        end_layers = routable_layers if end.through_hole else [end_layer]

        # Issue #956/#977: Calculate pad metal area bounds for expanded start/goal regions
        # When pads don't align with the routing grid, we accept reaching any cell
        # within the pad's metal area, not just the grid-snapped center cell.
        # Issue #977: Apply same expansion to START pad - if the grid-snapped center
        # falls on a cell blocked by another net's clearance, we need alternate entry points.
        start_metal_gx1, start_metal_gy1, start_metal_gx2, start_metal_gy2 = (
            self._get_pad_metal_bounds(start)
        )
        end_metal_gx1, end_metal_gy1, end_metal_gx2, end_metal_gy2 = self._get_pad_metal_bounds(end)

        # Filter start/end layers by allowed_layers constraint (Issue #715)
        if self.rules.allowed_layers is not None:
            start_layers = [l for l in start_layers if self._is_layer_allowed(l)]
            end_layers = [l for l in end_layers if self._is_layer_allowed(l)]
            # If no valid layers remain, routing is impossible
            if not start_layers or not end_layers:
                return None

        # A* setup
        open_set: list[AStarNode] = []
        closed_set: set[tuple[int, int, int]] = set()
        g_scores: dict[tuple[int, int, int], float] = {}

        # Create heuristic context - for PTH end pads, use closest routable layer
        # for heuristic estimation (the actual goal check will accept any)
        heuristic_goal_layer = end_layers[0] if end_layers else end_layer
        heuristic_context = HeuristicContext(
            goal_x=end_gx,
            goal_y=end_gy,
            goal_layer=heuristic_goal_layer,
            rules=self.rules,
            cost_multiplier=cost_mult,
            diagonal_routing=self.diagonal_routing,
            get_congestion=self.grid.get_congestion,
            get_congestion_cost=self._get_congestion_cost,
        )

        # Issue #977: Start nodes - add for ALL cells within start pad's metal area
        # This handles off-grid start pads where the grid-snapped center may be blocked
        # by another net's clearance zone. By initializing from all metal area cells,
        # we ensure routing can begin even when some entry points are blocked.
        for sgx in range(start_metal_gx1, start_metal_gx2 + 1):
            for sgy in range(start_metal_gy1, start_metal_gy2 + 1):
                for sl in start_layers:
                    start_h = self.heuristic.estimate(sgx, sgy, sl, (0, 0), heuristic_context)
                    start_node = AStarNode(start_h, 0, sgx, sgy, sl)
                    heapq.heappush(open_set, start_node)
                    g_scores[(sgx, sgy, sl)] = 0

        iterations = 0
        max_iterations = self.grid.cols * self.grid.rows * 4  # Prevent infinite loops

        while open_set and iterations < max_iterations:
            iterations += 1

            current = heapq.heappop(open_set)
            current_key = (current.x, current.y, current.layer)

            if current_key in closed_set:
                continue
            closed_set.add(current_key)

            # Goal check - accept any cell within end pad's metal area (Issue #956)
            # This handles off-grid pads where the center doesn't align with routing grid
            if (
                end_metal_gx1 <= current.x <= end_metal_gx2
                and end_metal_gy1 <= current.y <= end_metal_gy2
                and current.layer in end_layers
            ):
                route = self._reconstruct_route(current, start, end)
                if route is not None:
                    return route
                # Geometric validation failed (Issue #750) - continue A* search
                # This allows finding alternate paths (e.g., B.Cu when F.Cu fails)
                # The node stays in closed_set, preventing re-exploration on this layer
                continue

            # Batch pre-compute costs for all neighbors (Issue #963)
            # This reduces per-neighbor function call overhead by computing all costs
            # in vectorized NumPy operations before the neighbor loop
            batch_congestion_costs = self._batch_congestion_costs(
                current.x, current.y, current.layer
            )
            batch_turn_costs = self._batch_turn_costs(current.direction)

            # Explore neighbors
            for neighbor_idx, (dx, dy, _dlayer, neighbor_cost_mult) in enumerate(self.neighbors_2d):
                nx, ny = current.x + dx, current.y + dy
                nlayer = current.layer

                # Check bounds and obstacles - account for trace width
                # A trace with width W extends W/2 on each side of centerline
                # Must check adjacent cells that trace would occupy
                #
                # EXCEPTION: Allow routing near pad centers if the adjacent cells
                # belong to the SAME NET. This handles TSSOP and other fine-pitch
                # components where pad clearance zones overlap.
                # But we MUST still block cells from OTHER nets (like GND pads).
                pad_approach_radius = 6  # cells
                # For PTH pads, allow approach from any valid layer
                is_start_adjacent = (
                    abs(nx - start_gx) <= pad_approach_radius
                    and abs(ny - start_gy) <= pad_approach_radius
                    and nlayer in start_layers
                )
                is_end_adjacent = (
                    abs(nx - end_gx) <= pad_approach_radius
                    and abs(ny - end_gy) <= pad_approach_radius
                    and nlayer in end_layers
                )

                # Issue #990: Check if CURRENT node is within a pad's metal area
                # When the entire metal area is blocked by other nets' clearance zones,
                # we still need to allow the first step outward from the pad.
                # This enables routing to start even when all metal area cells would
                # normally be blocked by adjacent components' clearance zones.
                is_exiting_start_pad = (
                    start_metal_gx1 <= current.x <= start_metal_gx2
                    and start_metal_gy1 <= current.y <= start_metal_gy2
                    and current.layer in start_layers
                )
                is_exiting_end_pad = (
                    end_metal_gx1 <= current.x <= end_metal_gx2
                    and end_metal_gy1 <= current.y <= end_metal_gy2
                    and current.layer in end_layers
                )

                # Check grid bounds first
                if not (0 <= nx < self.grid.cols and 0 <= ny < self.grid.rows):
                    continue

                # For diagonal moves, check corner clearance to prevent cutting through obstacles
                # This ensures we don't route diagonally through a corner where two obstacles meet
                if dx != 0 and dy != 0:  # Diagonal move
                    if self._is_diagonal_corner_blocked(
                        current.x, current.y, dx, dy, nlayer, start.net, allow_sharing
                    ):
                        continue

                # Check blocked cells carefully
                # Allow routing through blocked cells that belong to OUR net
                # This enables THT pads to be entered/exited on any layer
                cell = self.grid.grid[nlayer][ny][nx]
                if cell.blocked:
                    if cell.net == start.net:
                        # Same-net blocked cell (e.g., our THT pad area)
                        # Allow routing through it - this is key for THT routing
                        pass
                    elif cell.net == 0:
                        # No-net blocked cell - use full check for obstacles
                        if self._is_trace_blocked(nx, ny, nlayer, start.net, allow_sharing):
                            continue
                    else:
                        # Different net's blocked cell
                        # Issue #996: When exiting a pad's metal area, allow entering
                        # clearance zones (not actual pad copper). This enables sub-grid
                        # pad connections where the nearest grid cells are within another
                        # net's clearance zone but not its copper. The geometric validation
                        # during route reconstruction will catch actual DRC violations.
                        is_clearance_only = not cell.pad_blocked  # Not actual pad copper
                        is_pad_exit = is_exiting_start_pad or is_exiting_end_pad
                        if is_clearance_only and is_pad_exit:
                            # Clearance zone cell while exiting pad - allow this move
                            # to enable the first step out of the pad
                            pass
                        else:
                            # Actual pad copper or not exiting a pad - block
                            continue
                else:
                    # Issue #864: Even when center cell is unblocked, check trace clearance
                    # The trace has width and must not violate clearance to other nets
                    # within its radius. Skip this check near pads to allow approach.
                    # Issue #990: Also skip when exiting from within a pad's metal area.
                    # This handles dense layouts where ALL cells in the metal area are
                    # blocked by adjacent nets' clearance zones - we must allow the
                    # first step outward to escape the pad.
                    is_pad_exit_or_approach = (
                        is_start_adjacent
                        or is_end_adjacent
                        or is_exiting_start_pad
                        or is_exiting_end_pad
                    )
                    if not is_pad_exit_or_approach:
                        if self._is_trace_blocked(nx, ny, nlayer, start.net, allow_sharing):
                            continue

                # Check zone blocking (other-net zones block routing)
                if self._is_zone_blocked(nx, ny, nlayer, start.net):
                    continue

                neighbor_key = (nx, ny, nlayer)
                if neighbor_key in closed_set:
                    continue

                # Calculate cost - use batch pre-computed values (Issue #963)
                new_direction = (dx, dy)

                # Use batch-computed turn and congestion costs
                turn_cost = batch_turn_costs[neighbor_idx]
                congestion_cost = batch_congestion_costs[neighbor_idx]

                # Add negotiated congestion cost if in negotiated mode
                # Skip for cells adjacent to start/end pads (they're obstacles)
                negotiated_cost = 0.0
                if negotiated_mode and not (is_start_adjacent or is_end_adjacent):
                    negotiated_cost = self._get_negotiated_cell_cost(
                        nx, ny, nlayer, present_cost_factor
                    )

                # Add zone cost (reduced for same-net zones)
                zone_cost = self._get_zone_cost(nx, ny, nlayer, start.net)

                # Add layer preference cost (Issue #625)
                layer_pref_mult = self._get_layer_preference_cost(nlayer, net_class)

                new_g = (
                    current.g_score
                    + neighbor_cost_mult * self.rules.cost_straight * layer_pref_mult
                    + turn_cost
                    + congestion_cost
                    + negotiated_cost
                    + zone_cost
                ) * cost_mult

                if neighbor_key not in g_scores or new_g < g_scores[neighbor_key]:
                    g_scores[neighbor_key] = new_g
                    h = self.heuristic.estimate(nx, ny, nlayer, new_direction, heuristic_context)
                    f = new_g + weight * h  # Weighted A*

                    neighbor_node = AStarNode(
                        f, new_g, nx, ny, nlayer, current, False, new_direction
                    )
                    heapq.heappush(open_set, neighbor_node)

            # Try layer change (via) - use grid indices, not enum values
            # Only consider routable layers (skip planes)
            for new_layer in self.grid.get_routable_indices():
                if new_layer == current.layer:
                    continue

                # Check layer constraint (Issue #715)
                if not self._is_layer_allowed(new_layer):
                    continue

                # Check if via placement is valid on ALL layers (through-hole via)
                # Issue #966: Use cached via check with layer priority ordering
                if not self._check_via_placement_cached(
                    current.x, current.y, start.net, allow_sharing
                ):
                    continue

                # Check zone blocking for via (would pierce other-net zones)
                if not self._can_place_via_in_zones(current.x, current.y, start.net):
                    continue

                neighbor_key = (current.x, current.y, new_layer)
                if neighbor_key in closed_set:
                    continue

                # Via cost + congestion at new layer
                congestion_cost = self._get_congestion_cost(current.x, current.y, new_layer)

                # Add negotiated congestion cost if in negotiated mode
                negotiated_cost = 0.0
                if negotiated_mode:
                    negotiated_cost = self._get_negotiated_cell_cost(
                        current.x, current.y, new_layer, present_cost_factor
                    )

                # Add layer preference cost for new layer (Issue #625)
                layer_pref_mult = self._get_layer_preference_cost(new_layer, net_class)

                new_g = (
                    current.g_score
                    + self.rules.cost_via * layer_pref_mult
                    + congestion_cost
                    + negotiated_cost
                ) * cost_mult

                if neighbor_key not in g_scores or new_g < g_scores[neighbor_key]:
                    g_scores[neighbor_key] = new_g
                    # Via doesn't change direction, use current direction
                    h = self.heuristic.estimate(
                        current.x, current.y, new_layer, current.direction, heuristic_context
                    )
                    f = new_g + weight * h  # Weighted A*

                    neighbor_node = AStarNode(
                        f, new_g, current.x, current.y, new_layer, current, True
                    )
                    heapq.heappush(open_set, neighbor_node)

        # No path found
        return None

    def find_blocking_nets(
        self,
        start: Pad,
        end: Pad,
        layer: int | None = None,
    ) -> set[int]:
        """Find which nets block the direct path from start to end.

        Uses Bresenham's line algorithm to trace the ideal direct path,
        then identifies which net IDs are blocking cells along that path.
        This is used for targeted rip-up in negotiated routing.

        Args:
            start: Source pad
            end: Destination pad
            layer: Optional layer index (uses pad layer if not specified)

        Returns:
            Set of net IDs that block the path (excluding net 0 and the source net)
        """
        blocking_nets: set[int] = set()
        source_net = start.net

        # Convert to grid coordinates
        start_gx, start_gy = self.grid.world_to_grid(start.x, start.y)
        end_gx, end_gy = self.grid.world_to_grid(end.x, end.y)

        if layer is None:
            layer = self.grid.layer_to_index(start.layer.value)

        # Trace a direct line from start to end using Bresenham's algorithm
        # and collect all blocking nets along the path
        gx1, gy1 = start_gx, start_gy
        gx2, gy2 = end_gx, end_gy

        dx = abs(gx2 - gx1)
        dy = abs(gy2 - gy1)
        sx = 1 if gx1 < gx2 else -1
        sy = 1 if gy1 < gy2 else -1
        err = dx - dy
        gx, gy = gx1, gy1

        while True:
            # Check this cell and nearby cells (accounting for trace width)
            for check_dy in range(-self._trace_half_width_cells, self._trace_half_width_cells + 1):
                for check_dx in range(
                    -self._trace_half_width_cells, self._trace_half_width_cells + 1
                ):
                    cx, cy = gx + check_dx, gy + check_dy
                    if 0 <= cx < self.grid.cols and 0 <= cy < self.grid.rows:
                        cell = self.grid.grid[layer][cy][cx]
                        if cell.blocked and cell.net != source_net and cell.net != 0:
                            # This cell is blocked by another net's route
                            # Check usage_count to ensure it's a routed cell, not a static obstacle
                            if cell.usage_count > 0:
                                blocking_nets.add(cell.net)

            if gx == gx2 and gy == gy2:
                break

            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                gx += sx
            if e2 < dx:
                err += dx
                gy += sy

        return blocking_nets

    def _convert_path_to_route(
        self,
        path: list[tuple[float, float, int, bool]],
        route: Route,
        start_pad: Pad,
        end_pad: Pad,
    ) -> None:
        """Convert path points to route segments and vias.

        This helper method handles the common logic of converting A* path points
        into Via and Segment objects, adding them to the route. Used by both
        unidirectional and bidirectional route reconstruction.

        Issue #972: Performance optimization - merge collinear segments inline
        during reconstruction instead of creating segment-per-cell and merging
        later. This reduces segment count from thousands to tens per net,
        significantly improving routing performance for large boards.

        Issue #1018: Automatic trace neck-down near fine-pitch pads. When
        min_trace_width is configured, traces taper from normal width to
        minimum width as they approach fine-pitch pads.

        Args:
            path: List of (world_x, world_y, layer_idx, is_via) tuples
            route: Route object to populate with segments and vias
            start_pad: Source pad (determines starting position)
            end_pad: Destination pad (determines final segment endpoint)
        """
        if len(path) < 2:
            return

        # Start from pad center on the A* start node's layer
        # Issue #977: With expanded start regions, the A* may start on a different
        # layer than start_pad.layer (e.g., when allowed_layers constrains routing).
        # Use the layer from the first path node, not start_pad.layer.
        # current_layer_idx is a grid index (0, 1, ...), not Layer enum value
        current_layer_idx = path[0][2]  # Layer from first A* node

        # Issue #972: Inline segment merging - track segment start point and direction
        # to merge collinear cells into single segments
        seg_start_x, seg_start_y = start_pad.x, start_pad.y
        current_x, current_y = seg_start_x, seg_start_y
        current_direction: tuple[float, float] | None = None  # (dx_normalized, dy_normalized)

        # Issue #1018: Get pin pitches for neck-down calculation
        start_pitch = self.component_pitches.get(start_pad.ref) if start_pad.ref else None
        end_pitch = self.component_pitches.get(end_pad.ref) if end_pad.ref else None

        # Determine if neck-down applies for each pad
        start_needs_neckdown = self.rules.should_apply_neck_down(start_pad.ref, start_pitch)
        end_needs_neckdown = self.rules.should_apply_neck_down(end_pad.ref, end_pitch)

        def _normalize_direction(dx: float, dy: float) -> tuple[float, float] | None:
            """Normalize direction vector, return None if no movement."""
            length = (dx * dx + dy * dy) ** 0.5
            if length < 0.001:
                return None
            return (dx / length, dy / length)

        def _same_direction(d1: tuple[float, float] | None, d2: tuple[float, float] | None) -> bool:
            """Check if two directions are the same (within tolerance)."""
            if d1 is None or d2 is None:
                return False
            # Check if normalized directions match (collinear)
            return abs(d1[0] - d2[0]) < 0.01 and abs(d1[1] - d2[1]) < 0.01

        def _distance(x1: float, y1: float, x2: float, y2: float) -> float:
            """Calculate Euclidean distance between two points."""
            return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)

        def _calculate_segment_width(x1: float, y1: float, x2: float, y2: float) -> float:
            """Calculate trace width for a segment based on distance to pads.

            Issue #1018: For segments near fine-pitch pads, the width tapers
            from normal trace width to minimum trace width. The width is
            determined by the minimum distance from the segment endpoints
            to either pad that needs neck-down.
            """
            # If no neck-down needed at either end, use normal width
            if not start_needs_neckdown and not end_needs_neckdown:
                return self.rules.trace_width

            # Calculate distances from segment endpoints to pads
            min_width = self.rules.trace_width

            # Check start pad influence
            if start_needs_neckdown:
                dist_to_start_1 = _distance(x1, y1, start_pad.x, start_pad.y)
                dist_to_start_2 = _distance(x2, y2, start_pad.x, start_pad.y)
                # Use minimum distance from either endpoint
                min_dist_start = min(dist_to_start_1, dist_to_start_2)
                width_from_start = self.rules.get_neck_down_width(min_dist_start, start_pitch)
                min_width = min(min_width, width_from_start)

            # Check end pad influence
            if end_needs_neckdown:
                dist_to_end_1 = _distance(x1, y1, end_pad.x, end_pad.y)
                dist_to_end_2 = _distance(x2, y2, end_pad.x, end_pad.y)
                # Use minimum distance from either endpoint
                min_dist_end = min(dist_to_end_1, dist_to_end_2)
                width_from_end = self.rules.get_neck_down_width(min_dist_end, end_pitch)
                min_width = min(min_width, width_from_end)

            return min_width

        def _emit_segment(x1: float, y1: float, x2: float, y2: float, layer_idx: int) -> None:
            """Create and add a segment if there's meaningful distance."""
            if abs(x2 - x1) > 0.01 or abs(y2 - y1) > 0.01:
                # Issue #1018: Calculate width with neck-down support
                width = _calculate_segment_width(x1, y1, x2, y2)
                seg = Segment(
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    width=width,
                    layer=Layer(self.grid.index_to_layer(layer_idx)),
                    net=start_pad.net,
                    net_name=start_pad.net_name,
                )
                route.segments.append(seg)

        for _i, (wx, wy, layer_idx, is_via) in enumerate(path):
            if is_via:
                # Emit pending segment before via
                _emit_segment(seg_start_x, seg_start_y, current_x, current_y, current_layer_idx)

                # Add via - convert grid indices back to Layer enum values
                via = Via(
                    x=current_x,
                    y=current_y,
                    drill=self.rules.via_drill,
                    diameter=self.rules.via_diameter,
                    layers=(
                        Layer(self.grid.index_to_layer(current_layer_idx)),
                        Layer(self.grid.index_to_layer(layer_idx)),
                    ),
                    net=start_pad.net,
                    net_name=start_pad.net_name,
                )
                route.vias.append(via)
                current_layer_idx = layer_idx

                # Reset segment tracking after via
                seg_start_x, seg_start_y = current_x, current_y
                current_direction = None
            else:
                # Check if we've moved
                dx = wx - current_x
                dy = wy - current_y
                new_direction = _normalize_direction(dx, dy)

                if new_direction is not None:
                    # Direction changed - emit current segment and start new one
                    if not _same_direction(current_direction, new_direction):
                        _emit_segment(
                            seg_start_x, seg_start_y, current_x, current_y, current_layer_idx
                        )
                        seg_start_x, seg_start_y = current_x, current_y
                        current_direction = new_direction

                    current_x, current_y = wx, wy
                    current_layer_idx = layer_idx

        # Emit final segment of the path
        _emit_segment(seg_start_x, seg_start_y, current_x, current_y, current_layer_idx)

        # Final segment to end pad (if needed)
        dx = end_pad.x - current_x
        dy = end_pad.y - current_y
        if abs(dx) > 0.01 or abs(dy) > 0.01:
            # Check if final segment is collinear with last emitted segment
            if route.segments:
                last_seg = route.segments[-1]
                last_dx = last_seg.x2 - last_seg.x1
                last_dy = last_seg.y2 - last_seg.y1
                last_dir = _normalize_direction(last_dx, last_dy)
                end_dir = _normalize_direction(dx, dy)

                if _same_direction(last_dir, end_dir):
                    # Extend last segment to end pad
                    # Issue #1018: Recalculate width for the extended segment
                    extended_width = _calculate_segment_width(
                        last_seg.x1, last_seg.y1, end_pad.x, end_pad.y
                    )
                    route.segments[-1] = Segment(
                        x1=last_seg.x1,
                        y1=last_seg.y1,
                        x2=end_pad.x,
                        y2=end_pad.y,
                        width=extended_width,
                        layer=last_seg.layer,
                        net=start_pad.net,
                        net_name=start_pad.net_name,
                    )
                else:
                    _emit_segment(current_x, current_y, end_pad.x, end_pad.y, current_layer_idx)
            else:
                _emit_segment(current_x, current_y, end_pad.x, end_pad.y, current_layer_idx)

    def _validate_route_clearance(
        self,
        route: Route,
        exclude_net: int,
        component_pitches: dict[str, float] | None = None,
    ) -> bool:
        """Validate route segments against geometric clearance constraints.

        Issue #750: Grid-based A* checking is approximate; diagonal segments can
        cut through obstacle corners. This method validates actual geometry to
        catch clearance violations that grid-based checking missed.

        Issue #1016: Now supports per-component clearance validation via
        component_pitches dict for automatic fine-pitch detection.

        Args:
            route: Route to validate
            exclude_net: Net ID to exclude from clearance checks (the route's own net)
            component_pitches: Optional dict mapping component ref to pin pitch in mm

        Returns:
            True if route passes clearance validation, False otherwise.
        """
        for seg in route.segments:
            is_valid, _clearance, _location = self.grid.validate_segment_clearance(
                seg, exclude_net=exclude_net, component_pitches=component_pitches
            )
            if not is_valid:
                return False
        return True

    def _reconstruct_route(self, end_node: AStarNode, start_pad: Pad, end_pad: Pad) -> Route | None:
        """Reconstruct the route from A* result with geometric validation.

        Issue #750: After reconstructing the route from grid coordinates,
        validates each segment against original obstacle geometry to catch
        clearance violations that grid-based checking missed (particularly
        for diagonal segments that can cut through obstacle corners).

        Returns:
            Route if valid, None if geometric clearance validation fails.
        """
        route = Route(net=start_pad.net, net_name=start_pad.net_name)

        # Collect path points
        path: list[tuple[float, float, int, bool]] = []
        node: AStarNode | None = end_node
        while node:
            wx, wy = self.grid.grid_to_world(node.x, node.y)
            path.append((wx, wy, node.layer, node.via_from_parent))
            node = node.parent

        path.reverse()

        # Convert path to segments and vias
        self._convert_path_to_route(path, route, start_pad, end_pad)

        # Validate layer transitions and insert any missing vias
        route.validate_layer_transitions(
            via_drill=self.rules.via_drill,
            via_diameter=self.rules.via_diameter,
        )

        # Geometric clearance validation (Issue #1016: per-component clearance support)
        if not self._validate_route_clearance(
            route, start_pad.net, component_pitches=self.component_pitches
        ):
            # Route has clearance violations - reject it
            # The caller will report "no path found" which is preferable
            # to returning a route with DRC violations
            return None

        return route

    def route_bidirectional(
        self,
        start: Pad,
        end: Pad,
        net_class: NetClassRouting | None = None,
        negotiated_mode: bool = False,
        present_cost_factor: float = 0.0,
        weight: float = 1.0,
    ) -> Route | None:
        """Route between two pads using bidirectional A* search.

        Bidirectional A* runs two simultaneous searches: one from start toward
        end, and one from end toward start. When the frontiers meet, the path
        is reconstructed by combining both directions.

        This can significantly reduce the search space for large paths, as
        the searches meet in the middle rather than one having to traverse
        the entire distance.

        Performance benefits (Issue #964):
        - For paths with N nodes, unidirectional searches O(N)
        - Bidirectional searches O(√N) in best case
        - Typically 50-75% speedup for paths >5000 nodes

        Args:
            start: Source pad
            end: Destination pad
            net_class: Optional net class for routing parameters
            negotiated_mode: If True, allow sharing resources with cost penalty
            present_cost_factor: Multiplier for current sharing penalty
            weight: A* weight factor (1.0 = optimal, >1.0 = faster but suboptimal)

        Returns:
            Route if path found, None otherwise
        """
        # Issue #966: Clear via cache at start of route (grid state may have changed)
        self.clear_via_cache()

        # Get net class if not provided
        if net_class is None:
            net_class = self._get_net_class(start.net_name)

        cost_mult = net_class.cost_multiplier if net_class else 1.0
        allow_sharing = negotiated_mode

        # Convert to grid coordinates
        start_gx, start_gy = self.grid.world_to_grid(start.x, start.y)
        end_gx, end_gy = self.grid.world_to_grid(end.x, end.y)

        # Get valid layers for each pad
        routable_layers = self.grid.get_routable_indices()
        start_layer = self.grid.layer_to_index(start.layer.value)
        end_layer = self.grid.layer_to_index(end.layer.value)
        start_layers = routable_layers if start.through_hole else [start_layer]
        end_layers = routable_layers if end.through_hole else [end_layer]

        # Apply layer constraints
        if self.rules.allowed_layers is not None:
            start_layers = [l for l in start_layers if self._is_layer_allowed(l)]
            end_layers = [l for l in end_layers if self._is_layer_allowed(l)]
            if not start_layers or not end_layers:
                return None

        # Get pad metal bounds for goal checking (Issue #956)
        start_metal_bounds = self._get_pad_metal_bounds(start)
        end_metal_bounds = self._get_pad_metal_bounds(end)

        # Heuristic contexts for both directions
        forward_context = HeuristicContext(
            goal_x=end_gx,
            goal_y=end_gy,
            goal_layer=end_layers[0] if end_layers else end_layer,
            rules=self.rules,
            cost_multiplier=cost_mult,
            diagonal_routing=self.diagonal_routing,
            get_congestion=self.grid.get_congestion,
            get_congestion_cost=self._get_congestion_cost,
        )
        backward_context = HeuristicContext(
            goal_x=start_gx,
            goal_y=start_gy,
            goal_layer=start_layers[0] if start_layers else start_layer,
            rules=self.rules,
            cost_multiplier=cost_mult,
            diagonal_routing=self.diagonal_routing,
            get_congestion=self.grid.get_congestion,
            get_congestion_cost=self._get_congestion_cost,
        )

        # Initialize forward search (start -> end)
        # Issue #977: Initialize from ALL cells within start pad's metal area
        forward_open: list[AStarNode] = []
        forward_closed: set[tuple[int, int, int]] = set()
        forward_g: dict[tuple[int, int, int], float] = {}
        forward_nodes: dict[tuple[int, int, int], AStarNode] = {}

        for sgx in range(start_metal_bounds[0], start_metal_bounds[2] + 1):
            for sgy in range(start_metal_bounds[1], start_metal_bounds[3] + 1):
                for sl in start_layers:
                    h = self.heuristic.estimate(sgx, sgy, sl, (0, 0), forward_context)
                    node = AStarNode(h, 0, sgx, sgy, sl)
                    heapq.heappush(forward_open, node)
                    key = (sgx, sgy, sl)
                    forward_g[key] = 0
                    forward_nodes[key] = node

        # Initialize backward search (end -> start)
        # Issue #977: Initialize from ALL cells within end pad's metal area
        backward_open: list[AStarNode] = []
        backward_closed: set[tuple[int, int, int]] = set()
        backward_g: dict[tuple[int, int, int], float] = {}
        backward_nodes: dict[tuple[int, int, int], AStarNode] = {}

        for egx in range(end_metal_bounds[0], end_metal_bounds[2] + 1):
            for egy in range(end_metal_bounds[1], end_metal_bounds[3] + 1):
                for el in end_layers:
                    h = self.heuristic.estimate(egx, egy, el, (0, 0), backward_context)
                    node = AStarNode(h, 0, egx, egy, el)
                    heapq.heappush(backward_open, node)
                    key = (egx, egy, el)
                    backward_g[key] = 0
                    backward_nodes[key] = node

        # Best meeting point tracking
        best_path_cost = float("inf")
        meeting_point: tuple[int, int, int] | None = None

        iterations = 0
        max_iterations = self.grid.cols * self.grid.rows * 4

        while (forward_open or backward_open) and iterations < max_iterations:
            iterations += 1

            # Alternate between forward and backward search
            # Process forward step
            if forward_open:
                forward_node = heapq.heappop(forward_open)
                fkey = (forward_node.x, forward_node.y, forward_node.layer)

                if fkey not in forward_closed:
                    forward_closed.add(fkey)
                    forward_nodes[fkey] = forward_node

                    # Check if backward search has reached this point
                    if fkey in backward_closed:
                        total_cost = forward_node.g_score + backward_g.get(fkey, float("inf"))
                        if total_cost < best_path_cost:
                            best_path_cost = total_cost
                            meeting_point = fkey

                    # Expand forward neighbors
                    self._expand_bidirectional_neighbors(
                        forward_node,
                        forward_open,
                        forward_closed,
                        forward_g,
                        forward_nodes,
                        forward_context,
                        start,
                        start_layers,
                        end_layers,
                        start_metal_bounds,  # Issue #990: source metal bounds
                        end_metal_bounds,
                        allow_sharing,
                        cost_mult,
                        weight,
                    )

            # Process backward step
            if backward_open:
                backward_node = heapq.heappop(backward_open)
                bkey = (backward_node.x, backward_node.y, backward_node.layer)

                if bkey not in backward_closed:
                    backward_closed.add(bkey)
                    backward_nodes[bkey] = backward_node

                    # Check if forward search has reached this point
                    if bkey in forward_closed:
                        total_cost = backward_node.g_score + forward_g.get(bkey, float("inf"))
                        if total_cost < best_path_cost:
                            best_path_cost = total_cost
                            meeting_point = bkey

                    # Expand backward neighbors
                    self._expand_bidirectional_neighbors(
                        backward_node,
                        backward_open,
                        backward_closed,
                        backward_g,
                        backward_nodes,
                        backward_context,
                        end,  # Backward search uses end pad as "start"
                        end_layers,
                        start_layers,
                        end_metal_bounds,  # Issue #990: source metal bounds
                        start_metal_bounds,
                        allow_sharing,
                        cost_mult,
                        weight,
                    )

            # Early termination: if we have a meeting point and both queues
            # have higher f-scores than the best path, we're done
            if meeting_point is not None:
                min_forward_f = forward_open[0].f_score if forward_open else float("inf")
                min_backward_f = backward_open[0].f_score if backward_open else float("inf")
                if min_forward_f >= best_path_cost and min_backward_f >= best_path_cost:
                    break

        # Reconstruct path if meeting point found
        if meeting_point is not None:
            return self._reconstruct_bidirectional_route(
                meeting_point,
                forward_nodes,
                backward_nodes,
                start,
                end,
            )

        return None

    def _expand_bidirectional_neighbors(
        self,
        current: AStarNode,
        open_set: list[AStarNode],
        closed_set: set[tuple[int, int, int]],
        g_scores: dict[tuple[int, int, int], float],
        nodes: dict[tuple[int, int, int], AStarNode],
        heuristic_context: HeuristicContext,
        source_pad: Pad,
        source_layers: list[int],
        target_layers: list[int],
        source_metal_bounds: tuple[int, int, int, int],
        target_metal_bounds: tuple[int, int, int, int],
        allow_sharing: bool,
        cost_mult: float,
        weight: float,
    ) -> None:
        """Expand neighbors for bidirectional A* search.

        This is a helper method that expands neighbors for either the forward
        or backward search direction. It handles 2D moves and via transitions.
        """
        # Extract bounds (Issue #990: also need source bounds for pad exit check)
        src_gx1, src_gy1, src_gx2, src_gy2 = source_metal_bounds
        tgt_gx1, tgt_gy1, tgt_gx2, tgt_gy2 = target_metal_bounds
        source_gx, source_gy = self.grid.world_to_grid(source_pad.x, source_pad.y)

        # Explore 2D neighbors (same layer moves)
        for dx, dy, _dlayer, neighbor_cost_mult in self.neighbors_2d:
            nx, ny = current.x + dx, current.y + dy
            nlayer = current.layer

            # Check bounds
            if not (0 <= nx < self.grid.cols and 0 <= ny < self.grid.rows):
                continue

            # Check diagonal corner blocking
            if dx != 0 and dy != 0:
                if self._is_diagonal_corner_blocked(
                    current.x, current.y, dx, dy, nlayer, source_pad.net, allow_sharing
                ):
                    continue

            # Pad approach radius for relaxed blocking near pads
            pad_approach_radius = 6
            is_source_adjacent = (
                abs(nx - source_gx) <= pad_approach_radius
                and abs(ny - source_gy) <= pad_approach_radius
                and nlayer in source_layers
            )
            is_target_adjacent = (
                tgt_gx1 - pad_approach_radius <= nx <= tgt_gx2 + pad_approach_radius
                and tgt_gy1 - pad_approach_radius <= ny <= tgt_gy2 + pad_approach_radius
                and nlayer in target_layers
            )

            # Issue #990: Check if CURRENT node is within a pad's metal area
            # When entire metal area is blocked by clearance zones, allow first step out
            is_exiting_source_pad = (
                src_gx1 <= current.x <= src_gx2
                and src_gy1 <= current.y <= src_gy2
                and current.layer in source_layers
            )
            is_exiting_target_pad = (
                tgt_gx1 <= current.x <= tgt_gx2
                and tgt_gy1 <= current.y <= tgt_gy2
                and current.layer in target_layers
            )

            # Check blocking
            cell = self.grid.grid[nlayer][ny][nx]
            if cell.blocked:
                if cell.net == source_pad.net:
                    pass  # Same net - passable
                elif cell.net == 0:
                    if self._is_trace_blocked(nx, ny, nlayer, source_pad.net, allow_sharing):
                        continue
                else:
                    # Different net's blocked cell
                    # Issue #996: When exiting a pad's metal area, allow entering
                    # clearance zones (not actual pad copper). This enables sub-grid
                    # pad connections where the nearest grid cells are within another
                    # net's clearance zone but not its copper.
                    is_clearance_only = not cell.pad_blocked
                    is_pad_exit = is_exiting_source_pad or is_exiting_target_pad
                    if is_clearance_only and is_pad_exit:
                        # Clearance zone cell while exiting pad - allow this move
                        pass
                    else:
                        continue  # Actual pad copper or not exiting a pad - block
            else:
                # Issue #990: Relax blocking check when exiting from pad metal area
                is_pad_exit_or_approach = (
                    is_source_adjacent
                    or is_target_adjacent
                    or is_exiting_source_pad
                    or is_exiting_target_pad
                )
                if not is_pad_exit_or_approach:
                    if self._is_trace_blocked(nx, ny, nlayer, source_pad.net, allow_sharing):
                        continue

            # Check zone blocking
            if self._is_zone_blocked(nx, ny, nlayer, source_pad.net):
                continue

            neighbor_key = (nx, ny, nlayer)
            if neighbor_key in closed_set:
                continue

            # Calculate cost
            new_direction = (dx, dy)
            turn_cost = 0.0
            if current.direction != (0, 0) and current.direction != new_direction:
                turn_cost = self.rules.cost_turn

            congestion_cost = self._get_congestion_cost(nx, ny, nlayer)
            negotiated_cost = 0.0
            if allow_sharing and not (is_source_adjacent or is_target_adjacent):
                negotiated_cost = self._get_negotiated_cell_cost(nx, ny, nlayer, 1.0)

            zone_cost = self._get_zone_cost(nx, ny, nlayer, source_pad.net)
            net_class = self._get_net_class(source_pad.net_name)
            layer_pref_mult = self._get_layer_preference_cost(nlayer, net_class)

            new_g = (
                current.g_score
                + neighbor_cost_mult * self.rules.cost_straight * layer_pref_mult
                + turn_cost
                + congestion_cost
                + negotiated_cost
                + zone_cost
            ) * cost_mult

            if neighbor_key not in g_scores or new_g < g_scores[neighbor_key]:
                g_scores[neighbor_key] = new_g
                h = self.heuristic.estimate(nx, ny, nlayer, new_direction, heuristic_context)
                f = new_g + weight * h

                neighbor_node = AStarNode(f, new_g, nx, ny, nlayer, current, False, new_direction)
                heapq.heappush(open_set, neighbor_node)
                nodes[neighbor_key] = neighbor_node

        # Try layer changes (vias)
        for new_layer in self.grid.get_routable_indices():
            if new_layer == current.layer:
                continue

            if not self._is_layer_allowed(new_layer):
                continue

            # Check via blocking on all layers
            # Issue #966: Use cached via check with layer priority ordering
            if not self._check_via_placement_cached(
                current.x, current.y, source_pad.net, allow_sharing
            ):
                continue

            if not self._can_place_via_in_zones(current.x, current.y, source_pad.net):
                continue

            neighbor_key = (current.x, current.y, new_layer)
            if neighbor_key in closed_set:
                continue

            congestion_cost = self._get_congestion_cost(current.x, current.y, new_layer)
            negotiated_cost = 0.0
            if allow_sharing:
                negotiated_cost = self._get_negotiated_cell_cost(
                    current.x, current.y, new_layer, 1.0
                )

            net_class = self._get_net_class(source_pad.net_name)
            layer_pref_mult = self._get_layer_preference_cost(new_layer, net_class)

            new_g = (
                current.g_score
                + self.rules.cost_via * layer_pref_mult
                + congestion_cost
                + negotiated_cost
            ) * cost_mult

            if neighbor_key not in g_scores or new_g < g_scores[neighbor_key]:
                g_scores[neighbor_key] = new_g
                h = self.heuristic.estimate(
                    current.x, current.y, new_layer, current.direction, heuristic_context
                )
                f = new_g + weight * h

                neighbor_node = AStarNode(f, new_g, current.x, current.y, new_layer, current, True)
                heapq.heappush(open_set, neighbor_node)
                nodes[neighbor_key] = neighbor_node

    def _reconstruct_bidirectional_route(
        self,
        meeting_point: tuple[int, int, int],
        forward_nodes: dict[tuple[int, int, int], AStarNode],
        backward_nodes: dict[tuple[int, int, int], AStarNode],
        start_pad: Pad,
        end_pad: Pad,
    ) -> Route | None:
        """Reconstruct route from bidirectional A* meeting point.

        Combines the forward path (start -> meeting) and reversed backward path
        (meeting -> end) into a complete route.

        Issue #972: Uses inline segment merging for performance.
        """
        route = Route(net=start_pad.net, net_name=start_pad.net_name)

        # Collect forward path (start -> meeting point)
        forward_path: list[tuple[float, float, int, bool]] = []
        forward_node = forward_nodes.get(meeting_point)
        while forward_node:
            wx, wy = self.grid.grid_to_world(forward_node.x, forward_node.y)
            forward_path.append((wx, wy, forward_node.layer, forward_node.via_from_parent))
            forward_node = forward_node.parent
        forward_path.reverse()

        # Collect backward path (end -> meeting point), then reverse
        backward_path: list[tuple[float, float, int, bool]] = []
        backward_node = backward_nodes.get(meeting_point)
        if backward_node:
            backward_node = backward_node.parent  # Skip meeting point (already in forward)
        while backward_node:
            wx, wy = self.grid.grid_to_world(backward_node.x, backward_node.y)
            backward_path.append((wx, wy, backward_node.layer, backward_node.via_from_parent))
            backward_node = backward_node.parent
        # backward_path is now from meeting -> end, which is what we want

        # Combine paths
        full_path = forward_path + backward_path

        # Convert path to segments and vias using shared helper
        # Issue #972: Helper includes inline segment merging optimization
        self._convert_path_to_route(full_path, route, start_pad, end_pad)

        # Validate layer transitions
        route.validate_layer_transitions(
            via_drill=self.rules.via_drill,
            via_diameter=self.rules.via_diameter,
        )

        # Geometric clearance validation (Issue #1016: per-component clearance support)
        if not self._validate_route_clearance(
            route, start_pad.net, component_pitches=self.component_pitches
        ):
            return None

        return route

    def route_auto(
        self,
        start: Pad,
        end: Pad,
        net_class: NetClassRouting | None = None,
        negotiated_mode: bool = False,
        present_cost_factor: float = 0.0,
        weight: float = 1.0,
    ) -> Route | None:
        """Route using automatic algorithm selection.

        Chooses between standard A* and bidirectional A* based on:
        - Grid size (bidirectional for large grids)
        - Distance between pads (bidirectional for long paths)
        - Configuration settings

        This is the recommended entry point for routing, as it automatically
        selects the best algorithm for the task.

        Args:
            start: Source pad
            end: Destination pad
            net_class: Optional net class for routing parameters
            negotiated_mode: If True, allow sharing resources with cost penalty
            present_cost_factor: Multiplier for current sharing penalty
            weight: A* weight factor

        Returns:
            Route if path found, None otherwise
        """
        # Check if bidirectional search is enabled
        if not self.rules.bidirectional_search:
            return self.route(start, end, net_class, negotiated_mode, present_cost_factor, weight)

        # Calculate Manhattan distance in grid cells
        start_gx, start_gy = self.grid.world_to_grid(start.x, start.y)
        end_gx, end_gy = self.grid.world_to_grid(end.x, end.y)
        manhattan_dist = abs(end_gx - start_gx) + abs(end_gy - start_gy)

        # Use bidirectional for paths exceeding threshold
        if manhattan_dist >= self.rules.bidirectional_threshold:
            result = self.route_bidirectional(
                start, end, net_class, negotiated_mode, present_cost_factor, weight
            )
            if result is not None:
                return result
            # Fall back to standard A* if bidirectional fails

        return self.route(start, end, net_class, negotiated_mode, present_cost_factor, weight)
