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
from typing import Dict, Optional, Tuple

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
    direction: Tuple[int, int] = field(compare=False, default=(0, 0))  # (dx, dy) from parent


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
        net_class_map: Optional[Dict[str, NetClassRouting]] = None,
        heuristic: Optional[Heuristic] = None,
    ):
        """
        Args:
            grid: The routing grid
            rules: Design rules for routing
            net_class_map: Mapping of net names to NetClassRouting
            heuristic: Heuristic for A* search (default: CongestionAwareHeuristic)
        """
        self.grid = grid
        self.rules = rules
        self.net_class_map = net_class_map or DEFAULT_NET_CLASS_MAP
        self.heuristic = heuristic or DEFAULT_HEURISTIC

        # Neighbor offsets: (dx, dy, dlayer, cost_multiplier)
        # Same layer moves
        self.neighbors_2d = [
            (1, 0, 0, 1.0),  # Right
            (-1, 0, 0, 1.0),  # Left
            (0, 1, 0, 1.0),  # Down
            (0, -1, 0, 1.0),  # Up
            # Diagonals (optional, can be disabled for Manhattan routing)
            # (1, 1, 0, 1.414),
            # (-1, 1, 0, 1.414),
            # (1, -1, 0, 1.414),
            # (-1, -1, 0, 1.414),
        ]

        # Pre-calculate trace body radius in grid cells
        # Only trace_width/2 - clearance is already in pad blocking
        # Add 1 cell minimum to ensure blocking check works
        self._trace_half_width_cells = max(
            1, math.ceil((self.rules.trace_width / 2) / self.grid.resolution)
        )

        # Pre-calculate via blocking radius in grid cells
        # Via needs diameter/2 + clearance from other objects (pads, traces, vias)
        self._via_half_cells = max(
            1,
            math.ceil(
                (self.rules.via_diameter / 2 + self.rules.via_clearance) / self.grid.resolution
            ),
        )

    def _get_net_class(self, net_name: str) -> Optional[NetClassRouting]:
        """Get the net class for a net name."""
        return self.net_class_map.get(net_name)

    def _is_trace_blocked(
        self, gx: int, gy: int, layer: int, net: int, allow_sharing: bool = False
    ) -> bool:
        """Check if placing a trace at this position would conflict.

        Unlike is_blocked which checks a single cell, this accounts for
        trace width by checking adjacent cells the trace would occupy.

        Args:
            allow_sharing: If True (negotiated mode), allow routing through
                          non-obstacle blocked cells (they'll get high cost instead)
        """
        # Check cells within trace width radius
        for dy in range(-self._trace_half_width_cells, self._trace_half_width_cells + 1):
            for dx in range(-self._trace_half_width_cells, self._trace_half_width_cells + 1):
                cx, cy = gx + dx, gy + dy
                if not (0 <= cx < self.grid.cols and 0 <= cy < self.grid.rows):
                    return True

                cell = self.grid.grid[layer][cy][cx]

                if cell.blocked:
                    # In negotiated mode, allow sharing non-obstacle cells
                    if allow_sharing and not cell.is_obstacle:
                        # Allow routing through used cells (will get cost penalty)
                        if cell.net != 0 and cell.net != net:
                            continue  # Allow with cost penalty later
                    else:
                        # Standard mode: block if:
                        # - Cell is an obstacle (is_obstacle=True) - always block
                        # - Cell belongs to different net (including net=0 plane nets) - block
                        # Same-net cells (including pad metal) are passable
                        if cell.is_obstacle or cell.net != net:
                            return True
        return False

    def _is_via_blocked(
        self, gx: int, gy: int, layer: int, net: int, allow_sharing: bool = False
    ) -> bool:
        """Check if placing a via at this position would conflict.

        Similar to _is_trace_blocked but uses the larger via clearance radius.
        Through-hole vias must be checked on ALL layers.

        Args:
            allow_sharing: If True (negotiated mode), allow routing through
                          non-obstacle blocked cells (they'll get high cost instead)
        """
        # Check cells within via clearance radius
        for dy in range(-self._via_half_cells, self._via_half_cells + 1):
            for dx in range(-self._via_half_cells, self._via_half_cells + 1):
                cx, cy = gx + dx, gy + dy
                if not (0 <= cx < self.grid.cols and 0 <= cy < self.grid.rows):
                    return True

                cell = self.grid.grid[layer][cy][cx]

                if cell.blocked:
                    # In negotiated mode, allow sharing non-obstacle cells
                    if allow_sharing and not cell.is_obstacle:
                        # Allow routing through used cells (will get cost penalty)
                        if cell.net != 0 and cell.net != net:
                            continue  # Allow with cost penalty later
                    else:
                        # Standard mode: block if obstacle or different net
                        if cell.is_obstacle or cell.net != net:
                            return True
        return False

    def _get_negotiated_cell_cost(
        self, gx: int, gy: int, layer: int, present_factor: float = 1.0
    ) -> float:
        """Get negotiated congestion cost for a cell."""
        return self.grid.get_negotiated_cost(gx, gy, layer, present_factor)

    def _get_congestion_cost(self, gx: int, gy: int, layer: int) -> float:
        """Get additional cost based on congestion at this location."""
        congestion = self.grid.get_congestion(gx, gy, layer)
        if congestion > self.rules.congestion_threshold:
            # Exponential penalty for congested areas
            excess = congestion - self.rules.congestion_threshold
            return self.rules.cost_congestion * (1.0 + excess * 2.0)
        return 0.0

    def route(
        self,
        start: Pad,
        end: Pad,
        net_class: Optional[NetClassRouting] = None,
        negotiated_mode: bool = False,
        present_cost_factor: float = 0.0,
        weight: float = 1.0,
    ) -> Optional[Route]:
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

        # A* setup
        open_set: list[AStarNode] = []
        closed_set: set[Tuple[int, int, int]] = set()
        g_scores: Dict[Tuple[int, int, int], float] = {}

        # Create heuristic context - for PTH end pads, use closest routable layer
        # for heuristic estimation (the actual goal check will accept any)
        heuristic_goal_layer = end_layers[0] if end_layers else end_layer
        heuristic_context = HeuristicContext(
            goal_x=end_gx,
            goal_y=end_gy,
            goal_layer=heuristic_goal_layer,
            rules=self.rules,
            cost_multiplier=cost_mult,
            get_congestion=self.grid.get_congestion,
            get_congestion_cost=self._get_congestion_cost,
        )

        # Start nodes - add one for each valid start layer
        for sl in start_layers:
            start_h = self.heuristic.estimate(start_gx, start_gy, sl, (0, 0), heuristic_context)
            start_node = AStarNode(start_h, 0, start_gx, start_gy, sl)
            heapq.heappush(open_set, start_node)
            g_scores[(start_gx, start_gy, sl)] = 0

        iterations = 0
        max_iterations = self.grid.cols * self.grid.rows * 4  # Prevent infinite loops

        while open_set and iterations < max_iterations:
            iterations += 1

            current = heapq.heappop(open_set)
            current_key = (current.x, current.y, current.layer)

            if current_key in closed_set:
                continue
            closed_set.add(current_key)

            # Goal check - accept any valid end layer for PTH pads
            if current.x == end_gx and current.y == end_gy and current.layer in end_layers:
                return self._reconstruct_route(current, start, end)

            # Explore neighbors
            for dx, dy, dlayer, neighbor_cost_mult in self.neighbors_2d:
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

                # Check grid bounds first
                if not (0 <= nx < self.grid.cols and 0 <= ny < self.grid.rows):
                    continue

                # Check blocked cells carefully
                # Only allow routing through blocked cells if:
                # 1. The cell is our net's pad CENTER (not just clearance zone)
                # 2. We're at the exact start or end pad position
                cell = self.grid.grid[nlayer][ny][nx]
                if cell.blocked:
                    # Check if this is exactly the start or end pad center
                    # For PTH pads, accept any valid layer
                    is_start_center = nx == start_gx and ny == start_gy and nlayer in start_layers
                    is_end_center = nx == end_gx and ny == end_gy and nlayer in end_layers

                    if is_start_center or is_end_center:
                        # Allow routing through our own pad centers
                        if cell.net != start.net:
                            continue  # Block if it's not our net
                    else:
                        # All other blocked cells are obstacles - use full check
                        if self._is_trace_blocked(nx, ny, nlayer, start.net, allow_sharing):
                            continue

                neighbor_key = (nx, ny, nlayer)
                if neighbor_key in closed_set:
                    continue

                # Calculate cost - include turn penalty if direction changes
                new_direction = (dx, dy)
                turn_cost = 0.0
                if current.direction != (0, 0) and current.direction != new_direction:
                    # Direction changed - add turn penalty
                    turn_cost = self.rules.cost_turn

                # Add congestion cost to actual path cost (g_score)
                congestion_cost = self._get_congestion_cost(nx, ny, nlayer)

                # Add negotiated congestion cost if in negotiated mode
                # Skip for cells adjacent to start/end pads (they're obstacles)
                negotiated_cost = 0.0
                if negotiated_mode and not (is_start_adjacent or is_end_adjacent):
                    negotiated_cost = self._get_negotiated_cell_cost(
                        nx, ny, nlayer, present_cost_factor
                    )

                new_g = (
                    current.g_score
                    + neighbor_cost_mult * self.rules.cost_straight
                    + turn_cost
                    + congestion_cost
                    + negotiated_cost
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

                # Check if via placement is valid on ALL layers (through-hole via)
                # Must use via clearance radius, not trace clearance
                via_blocked = False
                for check_layer in range(self.grid.num_layers):
                    if self._is_via_blocked(
                        current.x, current.y, check_layer, start.net, allow_sharing
                    ):
                        via_blocked = True
                        break
                if via_blocked:
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

                new_g = (
                    current.g_score + self.rules.cost_via + congestion_cost + negotiated_cost
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

    def _reconstruct_route(self, end_node: AStarNode, start_pad: Pad, end_pad: Pad) -> Route:
        """Reconstruct the route from A* result."""
        route = Route(net=start_pad.net, net_name=start_pad.net_name)

        # Collect path points
        path: list[Tuple[float, float, int, bool]] = []
        node: Optional[AStarNode] = end_node
        while node:
            wx, wy = self.grid.grid_to_world(node.x, node.y)
            path.append((wx, wy, node.layer, node.via_from_parent))
            node = node.parent

        path.reverse()

        # Convert to segments and vias
        if len(path) < 2:
            return route

        # Start from pad center
        # current_layer_idx is a grid index (0, 1, ...), not Layer enum value
        current_x, current_y = start_pad.x, start_pad.y
        current_layer_idx = self.grid.layer_to_index(start_pad.layer.value)

        for i, (wx, wy, layer_idx, is_via) in enumerate(path):
            if is_via:
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

        return route
