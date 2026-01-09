"""Differential pair routing integration for the autorouter.

This module provides differential pair-aware routing functionality
that coordinates differential pair routing with the main autorouter.

Key features:
- Coupled A* pathfinding that routes both traces simultaneously
- Maintains constant spacing between P/N traces
- Length matching with serpentine compensation
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Autorouter
    from .grid import RoutingGrid
    from .rules import DesignRules

from .diffpair import (
    DifferentialPair,
    DifferentialPairConfig,
    LengthMismatchWarning,
    analyze_differential_pairs,
    detect_differential_pairs,
)
from .layers import Layer
from .path import calculate_route_length
from .primitives import Pad, Route, Segment, Via


class PairOrientation(Enum):
    """Orientation of the differential pair traces."""

    HORIZONTAL = "horizontal"  # P above N (or vice versa), traces run horizontally
    VERTICAL = "vertical"  # P left of N (or vice versa), traces run vertically


@dataclass
class GridPos:
    """Grid position for coupled routing."""

    x: int
    y: int
    layer: int

    def __hash__(self) -> int:
        return hash((self.x, self.y, self.layer))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GridPos):
            return NotImplemented
        return self.x == other.x and self.y == other.y and self.layer == other.layer

    def __add__(self, other: tuple[int, int, int]) -> GridPos:
        return GridPos(self.x + other[0], self.y + other[1], self.layer + other[2])


@dataclass
class CoupledState:
    """State for coupled differential pair A* search.

    Represents the position of both P and N traces simultaneously.
    Both traces must move together to maintain constant spacing.
    """

    p_pos: GridPos  # Positive trace position
    n_pos: GridPos  # Negative trace position
    direction: tuple[int, int]  # Current routing direction (dx, dy)

    def __hash__(self) -> int:
        return hash((self.p_pos, self.n_pos, self.direction))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CoupledState):
            return NotImplemented
        return (
            self.p_pos == other.p_pos
            and self.n_pos == other.n_pos
            and self.direction == other.direction
        )

    @property
    def spacing(self) -> float:
        """Calculate current spacing between P and N traces."""
        dx = self.p_pos.x - self.n_pos.x
        dy = self.p_pos.y - self.n_pos.y
        return math.sqrt(dx * dx + dy * dy)


@dataclass(order=True)
class CoupledNode:
    """Node for coupled A* priority queue."""

    f_score: float
    g_score: float = field(compare=False)
    state: CoupledState = field(compare=False)
    parent: CoupledNode | None = field(compare=False, default=None)
    via_from_parent: bool = field(compare=False, default=False)


class CoupledPathfinder:
    """A* pathfinder for coupled differential pair routing.

    Routes both P and N traces simultaneously, maintaining constant
    spacing between them throughout the path.
    """

    def __init__(
        self,
        grid: RoutingGrid,
        rules: DesignRules,
        target_spacing_cells: int,
    ):
        """Initialize coupled pathfinder.

        Args:
            grid: The routing grid
            rules: Design rules for routing
            target_spacing_cells: Target spacing between P/N in grid cells
        """
        self.grid = grid
        self.rules = rules
        self.target_spacing_cells = target_spacing_cells

        # Pre-calculate trace clearance radius
        self._trace_half_width_cells = max(
            1,
            math.ceil(
                (self.rules.trace_width / 2 + self.rules.trace_clearance) / self.grid.resolution
            ),
        )

        # Pre-calculate via blocking radius
        self._via_half_cells = max(
            1,
            math.ceil(
                (self.rules.via_diameter / 2 + self.rules.via_clearance) / self.grid.resolution
            ),
        )

        # Orthogonal moves only for differential pairs (diagonal moves
        # would complicate spacing maintenance)
        self.directions = [
            (1, 0),  # Right
            (-1, 0),  # Left
            (0, 1),  # Down
            (0, -1),  # Up
        ]

    def _is_cell_blocked(self, gx: int, gy: int, layer: int, net: int) -> bool:
        """Check if a cell is blocked for this net."""
        if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
            return True
        if layer < 0 or layer >= self.grid.num_layers:
            return True

        cell = self.grid.grid[layer][gy][gx]
        if cell.blocked:
            if cell.is_obstacle or cell.net != net:
                return True
        return False

    def _is_trace_blocked(self, gx: int, gy: int, layer: int, net: int) -> bool:
        """Check if placing a trace at this position would conflict."""
        for dy in range(-self._trace_half_width_cells, self._trace_half_width_cells + 1):
            for dx in range(-self._trace_half_width_cells, self._trace_half_width_cells + 1):
                if self._is_cell_blocked(gx + dx, gy + dy, layer, net):
                    return True
        return False

    def _is_via_blocked(self, gx: int, gy: int, net: int) -> bool:
        """Check if placing a via at this position would conflict on any layer."""
        for layer in range(self.grid.num_layers):
            for dy in range(-self._via_half_cells, self._via_half_cells + 1):
                for dx in range(-self._via_half_cells, self._via_half_cells + 1):
                    if self._is_cell_blocked(gx + dx, gy + dy, layer, net):
                        return True
        return False

    def _get_coupled_neighbors(
        self,
        state: CoupledState,
        p_net: int,
        n_net: int,
    ) -> list[tuple[CoupledState, float, bool]]:
        """Generate valid coupled moves maintaining spacing.

        Returns list of (new_state, cost, is_via) tuples.
        """
        neighbors: list[tuple[CoupledState, float, bool]] = []

        # Try moving both traces in the same direction
        for dx, dy in self.directions:
            new_p = GridPos(
                state.p_pos.x + dx,
                state.p_pos.y + dy,
                state.p_pos.layer,
            )
            new_n = GridPos(
                state.n_pos.x + dx,
                state.n_pos.y + dy,
                state.n_pos.layer,
            )

            # Check if both new positions are valid
            if self._is_trace_blocked(new_p.x, new_p.y, new_p.layer, p_net):
                continue
            if self._is_trace_blocked(new_n.x, new_n.y, new_n.layer, n_net):
                continue

            # Calculate spacing between new positions
            spacing_dx = new_p.x - new_n.x
            spacing_dy = new_p.y - new_n.y
            new_spacing = math.sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy)

            # Only accept moves that maintain target spacing (within tolerance)
            tolerance = 1  # Allow 1 cell tolerance
            if abs(new_spacing - self.target_spacing_cells) > tolerance:
                continue

            # Calculate cost
            new_direction = (dx, dy)
            cost = self.rules.cost_straight

            # Add turn penalty if direction changed
            if state.direction != (0, 0) and state.direction != new_direction:
                cost += self.rules.cost_turn

            new_state = CoupledState(new_p, new_n, new_direction)
            neighbors.append((new_state, cost, False))

        # Try layer change (via) - both traces must change layer together
        routable_layers = self.grid.get_routable_indices()
        for new_layer in routable_layers:
            if new_layer == state.p_pos.layer:
                continue

            # Check if vias can be placed at both positions
            if self._is_via_blocked(state.p_pos.x, state.p_pos.y, p_net):
                continue
            if self._is_via_blocked(state.n_pos.x, state.n_pos.y, n_net):
                continue

            new_p = GridPos(state.p_pos.x, state.p_pos.y, new_layer)
            new_n = GridPos(state.n_pos.x, state.n_pos.y, new_layer)

            # Check if new layer positions are valid
            if self._is_trace_blocked(new_p.x, new_p.y, new_p.layer, p_net):
                continue
            if self._is_trace_blocked(new_n.x, new_n.y, new_n.layer, n_net):
                continue

            # Via cost for both traces
            cost = self.rules.cost_via * 2

            new_state = CoupledState(new_p, new_n, state.direction)
            neighbors.append((new_state, cost, True))

        return neighbors

    def _heuristic(
        self,
        state: CoupledState,
        p_goal: GridPos,
        n_goal: GridPos,
    ) -> float:
        """Calculate heuristic for coupled A* search."""
        # Manhattan distance for both traces
        p_dist = abs(state.p_pos.x - p_goal.x) + abs(state.p_pos.y - p_goal.y)
        n_dist = abs(state.n_pos.x - n_goal.x) + abs(state.n_pos.y - n_goal.y)

        # Layer change cost if needed
        layer_cost = 0.0
        if state.p_pos.layer != p_goal.layer:
            layer_cost += self.rules.cost_via
        if state.n_pos.layer != n_goal.layer:
            layer_cost += self.rules.cost_via

        return (p_dist + n_dist) * self.rules.cost_straight + layer_cost

    def route_coupled(
        self,
        p_start: Pad,
        p_end: Pad,
        n_start: Pad,
        n_end: Pad,
    ) -> tuple[Route, Route] | None:
        """Route a differential pair with coupled pathfinding.

        Args:
            p_start: Positive trace start pad
            p_end: Positive trace end pad
            n_start: Negative trace start pad
            n_end: Negative trace end pad

        Returns:
            Tuple of (p_route, n_route) or None if routing failed
        """
        # Convert to grid coordinates
        p_start_gx, p_start_gy = self.grid.world_to_grid(p_start.x, p_start.y)
        p_end_gx, p_end_gy = self.grid.world_to_grid(p_end.x, p_end.y)
        n_start_gx, n_start_gy = self.grid.world_to_grid(n_start.x, n_start.y)
        n_end_gx, n_end_gy = self.grid.world_to_grid(n_end.x, n_end.y)

        # Determine start layer
        start_layer = self.grid.layer_to_index(p_start.layer.value)
        end_layer = self.grid.layer_to_index(p_end.layer.value)

        # Create start and goal states
        p_start_pos = GridPos(p_start_gx, p_start_gy, start_layer)
        n_start_pos = GridPos(n_start_gx, n_start_gy, start_layer)
        p_goal_pos = GridPos(p_end_gx, p_end_gy, end_layer)
        n_goal_pos = GridPos(n_end_gx, n_end_gy, end_layer)

        start_state = CoupledState(p_start_pos, n_start_pos, (0, 0))

        # A* setup
        open_set: list[CoupledNode] = []
        closed_set: set[tuple[GridPos, GridPos]] = set()
        g_scores: dict[tuple[GridPos, GridPos], float] = {}

        start_h = self._heuristic(start_state, p_goal_pos, n_goal_pos)
        start_node = CoupledNode(start_h, 0.0, start_state)
        heapq.heappush(open_set, start_node)
        g_scores[(p_start_pos, n_start_pos)] = 0.0

        max_iterations = self.grid.cols * self.grid.rows * 4
        iterations = 0

        while open_set and iterations < max_iterations:
            iterations += 1

            current = heapq.heappop(open_set)
            current_key = (current.state.p_pos, current.state.n_pos)

            if current_key in closed_set:
                continue
            closed_set.add(current_key)

            # Goal check - both traces must reach their goals
            p_at_goal = (
                current.state.p_pos.x == p_goal_pos.x and current.state.p_pos.y == p_goal_pos.y
            )
            n_at_goal = (
                current.state.n_pos.x == n_goal_pos.x and current.state.n_pos.y == n_goal_pos.y
            )

            if p_at_goal and n_at_goal:
                return self._reconstruct_coupled_routes(current, p_start, p_end, n_start, n_end)

            # Explore neighbors
            for new_state, cost, is_via in self._get_coupled_neighbors(
                current.state, p_start.net, n_start.net
            ):
                neighbor_key = (new_state.p_pos, new_state.n_pos)
                if neighbor_key in closed_set:
                    continue

                new_g = current.g_score + cost

                if neighbor_key not in g_scores or new_g < g_scores[neighbor_key]:
                    g_scores[neighbor_key] = new_g
                    h = self._heuristic(new_state, p_goal_pos, n_goal_pos)
                    f = new_g + h

                    neighbor_node = CoupledNode(f, new_g, new_state, current, is_via)
                    heapq.heappush(open_set, neighbor_node)

        # No path found
        return None

    def _reconstruct_coupled_routes(
        self,
        end_node: CoupledNode,
        p_start: Pad,
        p_end: Pad,
        n_start: Pad,
        n_end: Pad,
    ) -> tuple[Route, Route]:
        """Reconstruct both routes from A* result."""
        p_route = Route(net=p_start.net, net_name=p_start.net_name)
        n_route = Route(net=n_start.net, net_name=n_start.net_name)

        # Collect path points
        p_path: list[tuple[float, float, int, bool]] = []
        n_path: list[tuple[float, float, int, bool]] = []

        node: CoupledNode | None = end_node
        while node:
            p_wx, p_wy = self.grid.grid_to_world(node.state.p_pos.x, node.state.p_pos.y)
            n_wx, n_wy = self.grid.grid_to_world(node.state.n_pos.x, node.state.n_pos.y)

            p_path.append((p_wx, p_wy, node.state.p_pos.layer, node.via_from_parent))
            n_path.append((n_wx, n_wy, node.state.n_pos.layer, node.via_from_parent))

            node = node.parent

        p_path.reverse()
        n_path.reverse()

        # Convert to segments and vias for P trace
        self._build_route_from_path(p_route, p_path, p_start, p_end)

        # Convert to segments and vias for N trace
        self._build_route_from_path(n_route, n_path, n_start, n_end)

        return p_route, n_route

    def _build_route_from_path(
        self,
        route: Route,
        path: list[tuple[float, float, int, bool]],
        start_pad: Pad,
        end_pad: Pad,
    ) -> None:
        """Build route segments and vias from path points."""
        if len(path) < 2:
            return

        current_x, current_y = start_pad.x, start_pad.y
        current_layer_idx = self.grid.layer_to_index(start_pad.layer.value)

        for wx, wy, layer_idx, is_via in path:
            if is_via:
                # Add via
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
            else:
                # Add segment if we've moved
                if abs(wx - current_x) > 0.01 or abs(wy - current_y) > 0.01:
                    seg = Segment(
                        x1=current_x,
                        y1=current_y,
                        x2=wx,
                        y2=wy,
                        width=self.rules.trace_width,
                        layer=Layer(self.grid.index_to_layer(layer_idx)),
                        net=start_pad.net,
                        net_name=start_pad.net_name,
                    )
                    route.segments.append(seg)
                    current_x, current_y = wx, wy
                    current_layer_idx = layer_idx

        # Final segment to end pad
        if abs(end_pad.x - current_x) > 0.01 or abs(end_pad.y - current_y) > 0.01:
            seg = Segment(
                x1=current_x,
                y1=current_y,
                x2=end_pad.x,
                y2=end_pad.y,
                width=self.rules.trace_width,
                layer=Layer(self.grid.index_to_layer(current_layer_idx)),
                net=start_pad.net,
                net_name=start_pad.net_name,
            )
            route.segments.append(seg)


def create_serpentine(
    route: Route,
    length_to_add: float,
    min_amplitude: float = 0.3,
    min_segment_length: float = 1.0,
) -> bool:
    """Add serpentine meander to a route to increase its length.

    Finds a suitable straight segment and replaces it with a serpentine
    pattern to add the required length.

    Args:
        route: The route to modify
        length_to_add: Additional length needed in mm
        min_amplitude: Minimum serpentine amplitude in mm
        min_segment_length: Minimum segment length for serpentine in mm

    Returns:
        True if serpentine was added, False if no suitable segment found
    """
    if length_to_add <= 0:
        return False

    # Find the longest straight horizontal or vertical segment
    best_segment = None
    best_segment_idx = -1
    best_length = 0.0

    for i, seg in enumerate(route.segments):
        seg_dx = seg.x2 - seg.x1
        seg_dy = seg.y2 - seg.y1
        seg_length = math.sqrt(seg_dx * seg_dx + seg_dy * seg_dy)

        # Only consider segments long enough for serpentine
        if seg_length < min_segment_length:
            continue

        # Prefer horizontal or vertical segments
        is_horizontal = abs(seg_dy) < 0.01
        is_vertical = abs(seg_dx) < 0.01

        if (is_horizontal or is_vertical) and seg_length > best_length:
            best_length = seg_length
            best_segment = seg
            best_segment_idx = i

    if best_segment is None:
        return False

    # Calculate serpentine parameters
    # Serpentine adds length = 2 * num_bends * amplitude
    # We want to add length_to_add, so:
    # amplitude = length_to_add / (2 * num_bends)
    # Use 4 bends as default
    num_bends = 4
    amplitude = max(min_amplitude, length_to_add / (2 * num_bends))

    # Determine serpentine direction (perpendicular to segment)
    seg_dx = best_segment.x2 - best_segment.x1
    seg_dy = best_segment.y2 - best_segment.y1
    seg_length = math.sqrt(seg_dx * seg_dx + seg_dy * seg_dy)

    # Normalize direction
    dir_x = seg_dx / seg_length
    dir_y = seg_dy / seg_length

    # Perpendicular direction for serpentine waves
    perp_x = -dir_y
    perp_y = dir_x

    # Create serpentine segments
    new_segments: list[Segment] = []
    step_length = seg_length / (num_bends + 1)

    current_x = best_segment.x1
    current_y = best_segment.y1
    current_side = 1  # Alternates between +1 and -1

    for bend in range(num_bends + 1):
        # Move to next point along the segment direction
        next_x = best_segment.x1 + dir_x * step_length * (bend + 1)
        next_y = best_segment.y1 + dir_y * step_length * (bend + 1)

        if bend < num_bends:
            # Add serpentine bulge
            bulge_x = current_x + dir_x * step_length / 2 + perp_x * amplitude * current_side
            bulge_y = current_y + dir_y * step_length / 2 + perp_y * amplitude * current_side

            # Segment to bulge
            new_segments.append(
                Segment(
                    x1=current_x,
                    y1=current_y,
                    x2=bulge_x,
                    y2=bulge_y,
                    width=best_segment.width,
                    layer=best_segment.layer,
                    net=best_segment.net,
                    net_name=best_segment.net_name,
                )
            )

            # Segment from bulge to next point
            new_segments.append(
                Segment(
                    x1=bulge_x,
                    y1=bulge_y,
                    x2=next_x,
                    y2=next_y,
                    width=best_segment.width,
                    layer=best_segment.layer,
                    net=best_segment.net,
                    net_name=best_segment.net_name,
                )
            )

            current_side *= -1  # Flip side for next bend
        else:
            # Final segment to end point
            new_segments.append(
                Segment(
                    x1=current_x,
                    y1=current_y,
                    x2=best_segment.x2,
                    y2=best_segment.y2,
                    width=best_segment.width,
                    layer=best_segment.layer,
                    net=best_segment.net,
                    net_name=best_segment.net_name,
                )
            )

        current_x = next_x
        current_y = next_y

    # Replace the original segment with serpentine segments
    route.segments = (
        route.segments[:best_segment_idx] + new_segments + route.segments[best_segment_idx + 1 :]
    )

    return True


def match_pair_lengths(
    p_route: Route,
    n_route: Route,
    max_delta: float,
    add_serpentines: bool = True,
) -> bool:
    """Match lengths of differential pair traces.

    Adds serpentine meander to the shorter trace to match lengths.

    Args:
        p_route: Positive trace route
        n_route: Negative trace route
        max_delta: Maximum allowed length difference in mm
        add_serpentines: Whether to add serpentines (if False, just check)

    Returns:
        True if lengths are matched (within tolerance), False otherwise
    """
    p_length = calculate_route_length([p_route])
    n_length = calculate_route_length([n_route])
    delta = abs(p_length - n_length)

    if delta <= max_delta:
        return True  # Already matched

    if not add_serpentines:
        return False  # Cannot match without serpentines

    # Add serpentine to shorter trace
    length_to_add = delta - max_delta * 0.5  # Leave some margin

    if p_length < n_length:
        return create_serpentine(p_route, length_to_add)
    else:
        return create_serpentine(n_route, length_to_add)


class DiffPairRouter:
    """Differential pair routing coordinator for the autorouter.

    Supports two routing modes:
    1. Coupled routing: Both traces routed simultaneously maintaining spacing
    2. Independent routing: Traces routed separately (fallback)
    """

    def __init__(self, autorouter: Autorouter):
        """Initialize differential pair router.

        Args:
            autorouter: Parent autorouter instance
        """
        self.autorouter = autorouter

    def detect_differential_pairs(self) -> list[DifferentialPair]:
        """Detect differential pairs from net names."""
        return detect_differential_pairs(self.autorouter.net_names)

    def analyze_differential_pairs(self) -> dict[str, any]:
        """Analyze net names for differential pairs."""
        return analyze_differential_pairs(self.autorouter.net_names)

    def _get_pair_pads(self, pair: DifferentialPair) -> tuple[list[Pad], list[Pad]] | None:
        """Get pads for P and N nets of a differential pair.

        Returns:
            Tuple of (p_pads, n_pads) or None if pads not found
        """
        p_net_id = pair.positive.net_id
        n_net_id = pair.negative.net_id

        if p_net_id not in self.autorouter.nets:
            return None
        if n_net_id not in self.autorouter.nets:
            return None

        p_pad_keys = self.autorouter.nets[p_net_id]
        n_pad_keys = self.autorouter.nets[n_net_id]

        if len(p_pad_keys) < 2 or len(n_pad_keys) < 2:
            return None

        p_pads = [self.autorouter.pads[k] for k in p_pad_keys]
        n_pads = [self.autorouter.pads[k] for k in n_pad_keys]

        return p_pads, n_pads

    def _pair_pads_for_coupled_routing(
        self, p_pads: list[Pad], n_pads: list[Pad]
    ) -> list[tuple[Pad, Pad, Pad, Pad]]:
        """Pair up P and N pads for coupled routing.

        Matches P/N pads that are closest together as start/end pairs.

        Returns:
            List of (p_start, p_end, n_start, n_end) tuples
        """
        if len(p_pads) != 2 or len(n_pads) != 2:
            # For now, only support simple 2-pad pairs
            return []

        # Find which P pad is closer to which N pad
        p0, p1 = p_pads[0], p_pads[1]
        n0, n1 = n_pads[0], n_pads[1]

        # Distance from p0 to both n pads
        d_p0_n0 = math.sqrt((p0.x - n0.x) ** 2 + (p0.y - n0.y) ** 2)
        d_p0_n1 = math.sqrt((p0.x - n1.x) ** 2 + (p0.y - n1.y) ** 2)

        # Match closest pads together
        if d_p0_n0 < d_p0_n1:
            # p0 pairs with n0, p1 pairs with n1
            return [(p0, p1, n0, n1)]
        else:
            # p0 pairs with n1, p1 pairs with n0
            return [(p0, p1, n1, n0)]

    def route_differential_pair_coupled(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair using coupled pathfinding.

        Routes both P and N traces simultaneously while maintaining
        constant spacing between them.
        """
        if pair.rules is None:
            return [], None

        if spacing is None:
            spacing = pair.rules.spacing

        print(f"\n  Routing differential pair {pair} (coupled mode)")
        print(f"    Type: {pair.pair_type.value}")
        print(f"    Spacing: {spacing}mm, Max delta: {pair.rules.max_length_delta}mm")

        # Get pads
        pad_result = self._get_pair_pads(pair)
        if pad_result is None:
            print("    ERROR: Could not find pads for differential pair")
            return [], None

        p_pads, n_pads = pad_result

        # Pair pads for routing
        pad_pairs = self._pair_pads_for_coupled_routing(p_pads, n_pads)
        if not pad_pairs:
            print("    WARNING: Complex pad configuration, falling back to independent routing")
            return self.route_differential_pair_independent(pair, spacing)

        # Calculate spacing in grid cells
        spacing_cells = int(spacing / self.autorouter.grid.resolution)

        # Create coupled pathfinder
        pathfinder = CoupledPathfinder(
            self.autorouter.grid,
            self.autorouter.rules,
            spacing_cells,
        )

        routes: list[Route] = []
        p_routes: list[Route] = []
        n_routes: list[Route] = []

        for p_start, p_end, n_start, n_end in pad_pairs:
            print(f"    Routing {pair.positive.net_name}/{pair.negative.net_name}...")

            result = pathfinder.route_coupled(p_start, p_end, n_start, n_end)

            if result is None:
                print("    WARNING: Coupled routing failed, falling back to independent routing")
                return self.route_differential_pair_independent(pair, spacing)

            p_route, n_route = result

            # Mark routes on grid
            self.autorouter.grid.mark_route(p_route)
            self.autorouter.grid.mark_route(n_route)
            self.autorouter.routes.append(p_route)
            self.autorouter.routes.append(n_route)

            p_routes.append(p_route)
            n_routes.append(n_route)
            routes.extend([p_route, n_route])

        # Calculate lengths
        p_length = calculate_route_length(p_routes)
        n_length = calculate_route_length(n_routes)
        pair.routed_length_p = p_length
        pair.routed_length_n = n_length

        print(f"      P length: {p_length:.3f}mm")
        print(f"      N length: {n_length:.3f}mm")

        # Check and apply length matching
        delta = pair.length_delta
        warning = None

        if delta > pair.rules.max_length_delta:
            print(f"    Length mismatch: {delta:.3f}mm, attempting serpentine...")

            # Try to add serpentine to shorter route
            if p_routes and n_routes:
                matched = match_pair_lengths(
                    p_routes[0],
                    n_routes[0],
                    pair.rules.max_length_delta,
                    add_serpentines=True,
                )

                if matched:
                    # Recalculate lengths
                    p_length = calculate_route_length(p_routes)
                    n_length = calculate_route_length(n_routes)
                    pair.routed_length_p = p_length
                    pair.routed_length_n = n_length
                    delta = pair.length_delta
                    print(f"    After serpentine: delta={delta:.3f}mm")

        if delta > pair.rules.max_length_delta:
            warning = LengthMismatchWarning(
                pair=pair,
                delta=delta,
                max_allowed=pair.rules.max_length_delta,
            )
            print(f"    WARNING: {warning}")
        else:
            print(f"    Length matched: delta={delta:.3f}mm (within tolerance)")

        return routes, warning

    def route_differential_pair_independent(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair with independent routing (fallback).

        Routes P and N traces separately using the standard router.
        """
        if pair.rules is None:
            return [], None

        if spacing is None:
            spacing = pair.rules.spacing

        routes: list[Route] = []
        print(f"\n  Routing differential pair {pair} (independent mode)")
        print(f"    Type: {pair.pair_type.value}")
        print(f"    Spacing: {spacing}mm, Max delta: {pair.rules.max_length_delta}mm")

        p_net_id = pair.positive.net_id
        n_net_id = pair.negative.net_id

        print(f"    Routing {pair.positive.net_name} (P)...")
        p_routes = self.autorouter.route_net(p_net_id)
        routes.extend(p_routes)

        p_length = calculate_route_length(p_routes)
        pair.routed_length_p = p_length
        print(f"      Length: {p_length:.3f}mm")

        print(f"    Routing {pair.negative.net_name} (N)...")
        n_routes = self.autorouter.route_net(n_net_id)
        routes.extend(n_routes)

        n_length = calculate_route_length(n_routes)
        pair.routed_length_n = n_length
        print(f"      Length: {n_length:.3f}mm")

        delta = pair.length_delta
        warning = None
        if delta > pair.rules.max_length_delta:
            warning = LengthMismatchWarning(
                pair=pair,
                delta=delta,
                max_allowed=pair.rules.max_length_delta,
            )
            print(f"    WARNING: {warning}")
        else:
            print(f"    Length matched: delta={delta:.3f}mm (within tolerance)")

        return routes, warning

    def route_differential_pair(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
        use_coupled_routing: bool = True,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair.

        Args:
            pair: The differential pair to route
            spacing: Override spacing (uses pair rules if None)
            use_coupled_routing: If True, use coupled A* routing.
                                If False, use independent routing.

        Returns:
            Tuple of (routes, warning) where warning is set if
            length matching failed.
        """
        if use_coupled_routing:
            return self.route_differential_pair_coupled(pair, spacing)
        else:
            return self.route_differential_pair_independent(pair, spacing)

    def route_all_with_diffpairs(
        self,
        diffpair_config: DifferentialPairConfig | None = None,
        net_order: list[int] | None = None,
    ) -> tuple[list[Route], list[LengthMismatchWarning]]:
        """Route all nets with differential pair-aware routing.

        Differential pairs are routed first (they're most constrained),
        then remaining nets are routed using the standard router.
        """
        if diffpair_config is None or not diffpair_config.enabled:
            return self.autorouter.route_all(net_order), []

        print("\n=== Differential Pair Routing ===")

        diff_pairs = self.detect_differential_pairs()
        diff_net_ids: set[int] = set()

        if diff_pairs:
            print(f"  Detected {len(diff_pairs)} differential pairs:")
            for pair in diff_pairs:
                print(f"    - {pair}: {pair.pair_type.value}")
                p_id, n_id = pair.get_net_ids()
                diff_net_ids.add(p_id)
                diff_net_ids.add(n_id)
        else:
            print("  No differential pairs detected")
            return self.autorouter.route_all(net_order), []

        for pair in diff_pairs:
            if pair.rules is not None:
                pair.rules = diffpair_config.get_rules(pair.pair_type)

        print("\n--- Routing differential pairs first (most constrained) ---")
        all_routes: list[Route] = []
        warnings: list[LengthMismatchWarning] = []

        for pair in diff_pairs:
            pair_routes, warning = self.route_differential_pair(
                pair,
                diffpair_config.spacing,
                use_coupled_routing=True,  # Use coupled routing by default
            )
            all_routes.extend(pair_routes)
            if warning:
                warnings.append(warning)

        non_diff_nets = [n for n in self.autorouter.nets if n not in diff_net_ids and n != 0]
        if non_diff_nets:
            print(f"\n--- Routing {len(non_diff_nets)} non-differential nets ---")
            if net_order:
                non_diff_order = [n for n in net_order if n in non_diff_nets]
            else:
                non_diff_order = sorted(
                    non_diff_nets, key=lambda n: self.autorouter._get_net_priority(n)
                )

            for net in non_diff_order:
                routes = self.autorouter.route_net(net)
                all_routes.extend(routes)
                if routes:
                    print(
                        f"  Net {net}: {len(routes)} routes, "
                        f"{sum(len(r.segments) for r in routes)} segments"
                    )

        print("\n=== Differential Pair Routing Complete ===")
        print(f"  Total routes: {len(all_routes)}")
        print(f"  Differential pair nets: {len(diff_net_ids)}")
        print(f"  Other nets: {len(non_diff_nets)}")
        if warnings:
            print(f"  Length mismatch warnings: {len(warnings)}")
            for w in warnings:
                print(f"    - {w}")

        return all_routes, warnings
