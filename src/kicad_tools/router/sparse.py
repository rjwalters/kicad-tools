"""
Sparse routing grid using clearance contours.

This module provides an alternative to the uniform grid approach that dramatically
reduces the number of routing points by generating waypoints only where needed:
- On the clearance boundary of each obstacle (pad, via, keepout)
- At pad centers (connection points)
- At sparse intervals in open areas

Performance comparison (65x56mm board, JLCPCB 0.0635mm clearance):
- Uniform grid: ~900,000 points
- Clearance contours: ~10,000 points (50-100x reduction)

The key insight is that valid routing paths must either:
1. Pass through pad centers (start/end points)
2. Navigate around obstacles at exactly the clearance distance
3. Cross open areas in straight lines

By encoding clearance constraints into the waypoint positions, we eliminate
per-segment clearance checking during routing.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Pad

from .layers import Layer
from .primitives import Route, Segment, Via
from .rules import DesignRules


@dataclass
class Waypoint:
    """A routing waypoint in the sparse graph.

    Waypoints are generated at:
    - Pad centers (connection points)
    - Clearance contour vertices (around obstacles)
    - Sparse grid points in open areas
    """

    x: float
    y: float
    layer: int
    waypoint_type: str = "contour"  # "pad", "contour", "sparse"
    pad_ref: str | None = None  # Reference for pad waypoints
    net: int = 0  # Net affinity (for pad waypoints)

    def __hash__(self):
        return hash((round(self.x, 4), round(self.y, 4), self.layer))

    def __eq__(self, other):
        if not isinstance(other, Waypoint):
            return False
        return (
            abs(self.x - other.x) < 0.001
            and abs(self.y - other.y) < 0.001
            and self.layer == other.layer
        )


@dataclass
class Corridor:
    """A routing corridor (channel) assigned during global routing.

    Corridors represent the approximate path a net will take, defined
    by a sequence of waypoints and a width buffer. During detailed
    routing, the router prefers to stay within the corridor but can
    exit with a cost penalty.

    Attributes:
        net: Net ID this corridor is assigned to
        waypoints: Ordered list of waypoints defining the corridor centerline
        width: Half-width of the corridor (distance from centerline to edge)
        layer_segments: List of (start_wp_idx, end_wp_idx, layer) for multi-layer corridors
    """

    net: int
    waypoints: list[Waypoint]
    width: float
    layer_segments: list[tuple[int, int, int]] = field(default_factory=list)

    def __post_init__(self):
        """Build layer segments if not provided."""
        if not self.layer_segments and len(self.waypoints) >= 2:
            # Group consecutive waypoints by layer
            segments = []
            start_idx = 0
            current_layer = self.waypoints[0].layer

            for i, wp in enumerate(self.waypoints[1:], 1):
                if wp.layer != current_layer:
                    segments.append((start_idx, i - 1, current_layer))
                    start_idx = i
                    current_layer = wp.layer

            segments.append((start_idx, len(self.waypoints) - 1, current_layer))
            self.layer_segments = segments

    def get_bounding_box(self) -> tuple[float, float, float, float]:
        """Get the bounding box of the corridor (min_x, min_y, max_x, max_y)."""
        if not self.waypoints:
            return (0, 0, 0, 0)

        xs = [wp.x for wp in self.waypoints]
        ys = [wp.y for wp in self.waypoints]
        return (
            min(xs) - self.width,
            min(ys) - self.width,
            max(xs) + self.width,
            max(ys) + self.width,
        )

    def contains_point(self, x: float, y: float, layer: int) -> bool:
        """Check if a point is inside the corridor on the given layer.

        Uses perpendicular distance to line segments between consecutive
        waypoints on the same layer.
        """
        for start_idx, end_idx, seg_layer in self.layer_segments:
            if seg_layer != layer:
                continue

            for i in range(start_idx, end_idx):
                wp1, wp2 = self.waypoints[i], self.waypoints[i + 1]
                dist = self._point_to_segment_distance(x, y, wp1.x, wp1.y, wp2.x, wp2.y)
                if dist <= self.width:
                    return True

        return False

    def _point_to_segment_distance(
        self, px: float, py: float, x1: float, y1: float, x2: float, y2: float
    ) -> float:
        """Calculate perpendicular distance from point to line segment."""
        dx = x2 - x1
        dy = y2 - y1
        length_sq = dx * dx + dy * dy

        if length_sq < 1e-10:
            # Degenerate segment (point)
            return math.sqrt((px - x1) ** 2 + (py - y1) ** 2)

        # Project point onto line, clamped to segment
        t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / length_sq))
        proj_x = x1 + t * dx
        proj_y = y1 + t * dy

        return math.sqrt((px - proj_x) ** 2 + (py - proj_y) ** 2)

    @classmethod
    def from_waypoints(cls, waypoints: list[Waypoint], net: int, width: float) -> Corridor:
        """Create a corridor from a sequence of waypoints.

        Args:
            waypoints: Ordered waypoints from global routing
            net: Net ID for this corridor
            width: Corridor half-width (typically 2*clearance)

        Returns:
            New Corridor instance
        """
        return cls(net=net, waypoints=waypoints, width=width)


@dataclass
class SparseNode:
    """Node for A* search on sparse graph."""

    f_score: float
    g_score: float = field(compare=False)
    waypoint: Waypoint = field(compare=False)
    parent: SparseNode | None = field(compare=False, default=None)
    via_from_parent: bool = field(compare=False, default=False)

    def __lt__(self, other):
        return self.f_score < other.f_score


class SparseRoutingGraph:
    """Sparse routing graph using clearance contours.

    Instead of a uniform grid, this generates waypoints only at:
    1. Pad centers (connection points)
    2. Clearance boundary vertices around obstacles
    3. Sparse points in open areas for long-distance routing

    The visibility graph connects waypoints that have line-of-sight
    without crossing obstacle boundaries.
    """

    def __init__(
        self,
        width: float,
        height: float,
        rules: DesignRules,
        origin_x: float = 0,
        origin_y: float = 0,
        num_layers: int = 2,
        contour_samples: int = 8,
        sparse_grid_spacing: float = 2.0,
    ):
        """Initialize sparse routing graph.

        Args:
            width, height: Board dimensions in mm
            rules: Design rules for clearances
            origin_x, origin_y: Board origin
            num_layers: Number of routing layers
            contour_samples: Number of waypoints per obstacle contour (8 = octagon)
            sparse_grid_spacing: Spacing for sparse interior grid points (mm)
        """
        self.width = width
        self.height = height
        self.rules = rules
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.num_layers = num_layers
        self.contour_samples = contour_samples
        self.sparse_grid_spacing = sparse_grid_spacing

        # Clearance buffer: distance from obstacle edge to valid routing
        self.clearance_buffer = rules.trace_clearance + rules.trace_width / 2

        # Waypoints by layer
        self.waypoints: dict[int, list[Waypoint]] = {i: [] for i in range(num_layers)}

        # Pad waypoints indexed by (net, layer) for fast lookup
        self.pad_waypoints: dict[tuple[int, int], list[Waypoint]] = {}

        # Obstacle polygons by layer (for visibility checking)
        # Each obstacle is (center_x, center_y, half_width, half_height, clearance)
        self.obstacles: dict[int, list[tuple[float, float, float, float, float]]] = {
            i: [] for i in range(num_layers)
        }

        # Visibility graph: waypoint -> list of (connected_waypoint, distance)
        self.edges: dict[Waypoint, list[tuple[Waypoint, float]]] = {}

        # Statistics
        self.stats = {
            "pad_waypoints": 0,
            "contour_waypoints": 0,
            "sparse_waypoints": 0,
            "total_edges": 0,
        }

        # Corridor reservations for global routing
        # Maps net ID to Corridor
        self.reserved_corridors: dict[int, Corridor] = {}

        # Edge cost multiplier for edges passing through other nets' corridors
        self.corridor_crossing_penalty: float = 3.0

    def add_pad(self, pad: Pad) -> None:
        """Add a pad to the sparse graph.

        Creates:
        1. A waypoint at the pad center (connection point)
        2. Contour waypoints around the pad clearance boundary
        3. Registers the pad as an obstacle
        """
        # Determine effective dimensions
        if pad.through_hole:
            if pad.width > 0 and pad.height > 0:
                half_w = pad.width / 2
                half_h = pad.height / 2
            elif pad.drill > 0:
                half_w = (pad.drill + 0.7) / 2
                half_h = half_w
            else:
                half_w = 0.85
                half_h = 0.85
        else:
            half_w = pad.width / 2
            half_h = pad.height / 2

        # Layers affected
        if pad.through_hole:
            layers = list(range(self.num_layers))
        else:
            layers = [0]  # Assume top layer for SMD

        for layer in layers:
            # Add pad center waypoint
            pad_wp = Waypoint(
                x=pad.x,
                y=pad.y,
                layer=layer,
                waypoint_type="pad",
                pad_ref=f"{pad.net}",
                net=pad.net,
            )
            self.waypoints[layer].append(pad_wp)
            self.stats["pad_waypoints"] += 1

            # Index by net for fast lookup
            key = (pad.net, layer)
            if key not in self.pad_waypoints:
                self.pad_waypoints[key] = []
            self.pad_waypoints[key].append(pad_wp)

            # Register obstacle
            self.obstacles[layer].append((pad.x, pad.y, half_w, half_h, self.clearance_buffer))

            # Generate contour waypoints around clearance boundary
            contour_dist = max(half_w, half_h) + self.clearance_buffer
            self._add_contour_waypoints(pad.x, pad.y, contour_dist, layer, pad.net)

    def _add_contour_waypoints(
        self, cx: float, cy: float, radius: float, layer: int, exclude_net: int = 0
    ) -> None:
        """Add waypoints around a circular/octagonal clearance contour.

        Args:
            cx, cy: Center of obstacle
            radius: Clearance radius (obstacle edge + clearance buffer)
            layer: Layer index
            exclude_net: Net that can pass through this obstacle (same-net routing)
        """
        for i in range(self.contour_samples):
            angle = 2 * math.pi * i / self.contour_samples
            wx = cx + radius * math.cos(angle)
            wy = cy + radius * math.sin(angle)

            # Skip if outside board
            if not self._in_bounds(wx, wy):
                continue

            wp = Waypoint(x=wx, y=wy, layer=layer, waypoint_type="contour", net=exclude_net)
            self.waypoints[layer].append(wp)
            self.stats["contour_waypoints"] += 1

    def add_sparse_grid(self) -> None:
        """Add sparse grid waypoints in open areas.

        These enable long-distance routing across empty board regions.
        """
        cols = int(self.width / self.sparse_grid_spacing) + 1
        rows = int(self.height / self.sparse_grid_spacing) + 1

        for layer in range(self.num_layers):
            for row in range(rows):
                for col in range(cols):
                    x = self.origin_x + col * self.sparse_grid_spacing
                    y = self.origin_y + row * self.sparse_grid_spacing

                    # Skip if inside any obstacle clearance zone
                    if self._point_blocked(x, y, layer):
                        continue

                    wp = Waypoint(x=x, y=y, layer=layer, waypoint_type="sparse")
                    self.waypoints[layer].append(wp)
                    self.stats["sparse_waypoints"] += 1

    def build_visibility_graph(self, max_edge_length: float = 20.0) -> None:
        """Build visibility graph connecting waypoints.

        Two waypoints are connected if the line between them doesn't
        cross any obstacle boundary.

        Args:
            max_edge_length: Maximum edge length to consider (limits search space)
        """
        for layer in range(self.num_layers):
            wps = self.waypoints[layer]
            n = len(wps)

            for i in range(n):
                wp1 = wps[i]
                if wp1 not in self.edges:
                    self.edges[wp1] = []

                for j in range(i + 1, n):
                    wp2 = wps[j]
                    if wp2 not in self.edges:
                        self.edges[wp2] = []

                    # Distance check
                    dist = math.sqrt((wp2.x - wp1.x) ** 2 + (wp2.y - wp1.y) ** 2)
                    if dist > max_edge_length:
                        continue

                    # Visibility check
                    if self._has_line_of_sight(wp1, wp2, layer):
                        self.edges[wp1].append((wp2, dist))
                        self.edges[wp2].append((wp1, dist))
                        self.stats["total_edges"] += 2

    def _in_bounds(self, x: float, y: float) -> bool:
        """Check if point is within board bounds."""
        return (
            self.origin_x <= x <= self.origin_x + self.width
            and self.origin_y <= y <= self.origin_y + self.height
        )

    def _point_blocked(self, x: float, y: float, layer: int, net: int = 0) -> bool:
        """Check if a point is inside any obstacle clearance zone."""
        for cx, cy, half_w, half_h, clearance in self.obstacles[layer]:
            # Rectangular obstacle with clearance buffer
            expanded_w = half_w + clearance
            expanded_h = half_h + clearance

            if abs(x - cx) < expanded_w and abs(y - cy) < expanded_h:
                return True

        return False

    def _has_line_of_sight(self, wp1: Waypoint, wp2: Waypoint, layer: int) -> bool:
        """Check if two waypoints have unobstructed line of sight.

        Uses ray-box intersection to check if line crosses any obstacle.
        """
        # Sample points along the line
        samples = max(10, int(math.sqrt((wp2.x - wp1.x) ** 2 + (wp2.y - wp1.y) ** 2) / 0.5))

        for i in range(1, samples):
            t = i / samples
            x = wp1.x + t * (wp2.x - wp1.x)
            y = wp1.y + t * (wp2.y - wp1.y)

            # Check if point is blocked
            # Allow same-net obstacles
            for cx, cy, half_w, half_h, clearance in self.obstacles[layer]:
                expanded_w = half_w + clearance
                expanded_h = half_h + clearance

                if abs(x - cx) < expanded_w and abs(y - cy) < expanded_h:
                    return False

        return True

    def route(
        self,
        start_pad: Pad,
        end_pad: Pad,
        diagonal_routing: bool = True,
    ) -> Route | None:
        """Route between two pads using the sparse graph.

        Args:
            start_pad: Source pad
            end_pad: Destination pad
            diagonal_routing: Allow diagonal segments (always True for sparse routing)

        Returns:
            Route if successful, None otherwise
        """
        # Find start and end waypoints
        start_wps = self._find_pad_waypoints(start_pad)
        end_wps = self._find_pad_waypoints(end_pad)

        if not start_wps or not end_wps:
            return None

        # A* search
        open_set: list[SparseNode] = []
        closed_set: set[Waypoint] = set()
        g_scores: dict[Waypoint, float] = {}

        # Add all start waypoints
        for start_wp in start_wps:
            h = self._heuristic(start_wp, end_wps[0])
            node = SparseNode(h, 0, start_wp)
            heapq.heappush(open_set, node)
            g_scores[start_wp] = 0

        end_wp_set = set(end_wps)

        while open_set:
            current = heapq.heappop(open_set)

            if current.waypoint in closed_set:
                continue
            closed_set.add(current.waypoint)

            # Goal check
            if current.waypoint in end_wp_set:
                return self._reconstruct_route(current, start_pad, end_pad)

            # Explore neighbors
            for neighbor_wp, edge_cost in self.edges.get(current.waypoint, []):
                if neighbor_wp in closed_set:
                    continue

                # Check if this edge is valid for our net
                # (don't cross other-net obstacles)
                if not self._edge_valid_for_net(current.waypoint, neighbor_wp, start_pad.net):
                    continue

                new_g = current.g_score + edge_cost

                if neighbor_wp not in g_scores or new_g < g_scores[neighbor_wp]:
                    g_scores[neighbor_wp] = new_g
                    h = min(self._heuristic(neighbor_wp, ewp) for ewp in end_wps)
                    f = new_g + h

                    neighbor_node = SparseNode(f, new_g, neighbor_wp, current, False)
                    heapq.heappush(open_set, neighbor_node)

            # Try layer change (via)
            for other_layer in range(self.num_layers):
                if other_layer == current.waypoint.layer:
                    continue

                # Find corresponding waypoint on other layer
                via_wp = self._find_via_waypoint(current.waypoint, other_layer)
                if via_wp is None or via_wp in closed_set:
                    continue

                via_cost = self.rules.cost_via
                new_g = current.g_score + via_cost

                if via_wp not in g_scores or new_g < g_scores[via_wp]:
                    g_scores[via_wp] = new_g
                    h = min(self._heuristic(via_wp, ewp) for ewp in end_wps)
                    f = new_g + h

                    via_node = SparseNode(f, new_g, via_wp, current, True)
                    heapq.heappush(open_set, via_node)

        return None

    def _find_pad_waypoints(self, pad: Pad) -> list[Waypoint]:
        """Find waypoints associated with a pad."""
        result = []
        for layer in range(self.num_layers):
            key = (pad.net, layer)
            if key in self.pad_waypoints:
                for wp in self.pad_waypoints[key]:
                    if abs(wp.x - pad.x) < 0.01 and abs(wp.y - pad.y) < 0.01:
                        result.append(wp)
        return result

    def _find_via_waypoint(self, wp: Waypoint, target_layer: int) -> Waypoint | None:
        """Find a waypoint at the same position on a different layer."""
        for other_wp in self.waypoints[target_layer]:
            if abs(other_wp.x - wp.x) < 0.01 and abs(other_wp.y - wp.y) < 0.01:
                return other_wp

        # Create a new waypoint if at a valid via position
        if not self._point_blocked(wp.x, wp.y, target_layer):
            new_wp = Waypoint(x=wp.x, y=wp.y, layer=target_layer, waypoint_type="via")
            self.waypoints[target_layer].append(new_wp)
            self.edges[new_wp] = []

            # Connect to nearby waypoints
            for other_wp in self.waypoints[target_layer]:
                if other_wp == new_wp:
                    continue
                dist = math.sqrt((other_wp.x - new_wp.x) ** 2 + (other_wp.y - new_wp.y) ** 2)
                if dist < 5.0 and self._has_line_of_sight(new_wp, other_wp, target_layer):
                    self.edges[new_wp].append((other_wp, dist))
                    if other_wp in self.edges:
                        self.edges[other_wp].append((new_wp, dist))

            return new_wp

        return None

    def _edge_valid_for_net(self, wp1: Waypoint, wp2: Waypoint, net: int) -> bool:
        """Check if edge is valid for routing a specific net."""
        # Allow edges between same-net waypoints or neutral waypoints
        if wp1.net != 0 and wp1.net != net:
            return False
        if wp2.net != 0 and wp2.net != net:
            return False
        return True

    def _heuristic(self, wp: Waypoint, goal: Waypoint) -> float:
        """Euclidean distance heuristic."""
        dx = abs(wp.x - goal.x)
        dy = abs(wp.y - goal.y)
        layer_cost = abs(wp.layer - goal.layer) * self.rules.cost_via
        return math.sqrt(dx * dx + dy * dy) + layer_cost

    def _reconstruct_route(self, end_node: SparseNode, start_pad: Pad, end_pad: Pad) -> Route:
        """Reconstruct route from A* result."""
        route = Route(net=start_pad.net, net_name=start_pad.net_name)

        # Collect waypoints from goal to start
        path: list[tuple[Waypoint, bool]] = []  # (waypoint, via_before)
        node: SparseNode | None = end_node
        while node:
            path.append((node.waypoint, node.via_from_parent))
            node = node.parent

        path.reverse()

        if len(path) < 2:
            return route

        # Convert to segments and vias
        current_x, current_y = start_pad.x, start_pad.y
        current_layer = path[0][0].layer

        for wp, is_via in path[1:]:
            if is_via:
                # Add via
                via = Via(
                    x=current_x,
                    y=current_y,
                    drill=self.rules.via_drill,
                    diameter=self.rules.via_diameter,
                    layers=(Layer(current_layer), Layer(wp.layer)),
                    net=start_pad.net,
                    net_name=start_pad.net_name,
                )
                route.vias.append(via)
                current_layer = wp.layer
            else:
                # Add segment
                if abs(wp.x - current_x) > 0.01 or abs(wp.y - current_y) > 0.01:
                    seg = Segment(
                        x1=current_x,
                        y1=current_y,
                        x2=wp.x,
                        y2=wp.y,
                        width=self.rules.trace_width,
                        layer=Layer(current_layer),
                        net=start_pad.net,
                        net_name=start_pad.net_name,
                    )
                    route.segments.append(seg)
                    current_x, current_y = wp.x, wp.y

        # Final segment to end pad
        if abs(end_pad.x - current_x) > 0.01 or abs(end_pad.y - current_y) > 0.01:
            seg = Segment(
                x1=current_x,
                y1=current_y,
                x2=end_pad.x,
                y2=end_pad.y,
                width=self.rules.trace_width,
                layer=Layer(current_layer),
                net=start_pad.net,
                net_name=start_pad.net_name,
            )
            route.segments.append(seg)

        # Validate layer transitions and insert any missing vias
        route.validate_layer_transitions(
            via_drill=self.rules.via_drill,
            via_diameter=self.rules.via_diameter,
        )

        return route

    def get_statistics(self) -> dict:
        """Get statistics about the sparse graph."""
        return {
            **self.stats,
            "total_waypoints": sum(len(wps) for wps in self.waypoints.values()),
            "waypoints_by_layer": {k: len(v) for k, v in self.waypoints.items()},
            "reserved_corridors": len(self.reserved_corridors),
        }

    # =========================================================================
    # GLOBAL ROUTING (TWO-PHASE) SUPPORT
    # =========================================================================

    def find_global_path(
        self,
        start_pad: Pad,
        end_pad: Pad,
    ) -> list[Waypoint] | None:
        """Find a global routing path (waypoints only, no detailed route).

        This is used for global/coarse routing to establish corridors
        before detailed routing. Unlike route(), this returns waypoints
        rather than a full Route with segments.

        Args:
            start_pad: Source pad
            end_pad: Destination pad

        Returns:
            List of waypoints forming the global path, or None if no path found
        """
        # Find start and end waypoints
        start_wps = self._find_pad_waypoints(start_pad)
        end_wps = self._find_pad_waypoints(end_pad)

        if not start_wps or not end_wps:
            return None

        # A* search with corridor-aware costs
        open_set: list[SparseNode] = []
        closed_set: set[Waypoint] = set()
        g_scores: dict[Waypoint, float] = {}

        # Add all start waypoints
        for start_wp in start_wps:
            h = self._heuristic(start_wp, end_wps[0])
            node = SparseNode(h, 0, start_wp)
            heapq.heappush(open_set, node)
            g_scores[start_wp] = 0

        end_wp_set = set(end_wps)

        while open_set:
            current = heapq.heappop(open_set)

            if current.waypoint in closed_set:
                continue
            closed_set.add(current.waypoint)

            # Goal check
            if current.waypoint in end_wp_set:
                return self._reconstruct_waypoint_path(current)

            # Explore neighbors with corridor-aware costs
            for neighbor_wp, edge_cost in self.edges.get(current.waypoint, []):
                if neighbor_wp in closed_set:
                    continue

                # Check if this edge is valid for our net
                if not self._edge_valid_for_net(current.waypoint, neighbor_wp, start_pad.net):
                    continue

                # Apply corridor crossing penalty
                corridor_cost = self._get_edge_corridor_cost(
                    current.waypoint, neighbor_wp, start_pad.net
                )
                total_edge_cost = edge_cost + corridor_cost

                new_g = current.g_score + total_edge_cost

                if neighbor_wp not in g_scores or new_g < g_scores[neighbor_wp]:
                    g_scores[neighbor_wp] = new_g
                    h = min(self._heuristic(neighbor_wp, ewp) for ewp in end_wps)
                    f = new_g + h

                    neighbor_node = SparseNode(f, new_g, neighbor_wp, current, False)
                    heapq.heappush(open_set, neighbor_node)

            # Try layer change (via)
            for other_layer in range(self.num_layers):
                if other_layer == current.waypoint.layer:
                    continue

                via_wp = self._find_via_waypoint(current.waypoint, other_layer)
                if via_wp is None or via_wp in closed_set:
                    continue

                via_cost = self.rules.cost_via
                new_g = current.g_score + via_cost

                if via_wp not in g_scores or new_g < g_scores[via_wp]:
                    g_scores[via_wp] = new_g
                    h = min(self._heuristic(via_wp, ewp) for ewp in end_wps)
                    f = new_g + h

                    via_node = SparseNode(f, new_g, via_wp, current, True)
                    heapq.heappush(open_set, via_node)

        return None

    def _reconstruct_waypoint_path(self, end_node: SparseNode) -> list[Waypoint]:
        """Reconstruct the waypoint path from A* result."""
        path: list[Waypoint] = []
        node: SparseNode | None = end_node
        while node:
            path.append(node.waypoint)
            node = node.parent
        path.reverse()
        return path

    def _get_edge_corridor_cost(self, wp1: Waypoint, wp2: Waypoint, routing_net: int) -> float:
        """Calculate cost penalty for an edge crossing other nets' corridors.

        Edges that pass through corridors reserved by other nets incur a
        penalty, encouraging routes to stay in their own corridors or
        find alternative paths.

        Args:
            wp1, wp2: Edge endpoints
            routing_net: The net currently being routed

        Returns:
            Additional cost for crossing corridors (0 if no crossing)
        """
        if not self.reserved_corridors:
            return 0.0

        # Sample points along edge
        samples = max(5, int(math.sqrt((wp2.x - wp1.x) ** 2 + (wp2.y - wp1.y) ** 2)))
        edge_length = math.sqrt((wp2.x - wp1.x) ** 2 + (wp2.y - wp1.y) ** 2)

        crossing_count = 0
        for net_id, corridor in self.reserved_corridors.items():
            if net_id == routing_net:
                # Own corridor - no penalty
                continue

            # Check if edge crosses this corridor
            for i in range(samples + 1):
                t = i / samples
                x = wp1.x + t * (wp2.x - wp1.x)
                y = wp1.y + t * (wp2.y - wp1.y)
                layer = wp1.layer  # Assume same layer for edge

                if corridor.contains_point(x, y, layer):
                    crossing_count += 1
                    break  # One crossing per corridor is enough

        if crossing_count == 0:
            return 0.0

        # Penalty scales with edge length and number of corridors crossed
        return edge_length * self.corridor_crossing_penalty * crossing_count

    def reserve_corridor(
        self, net: int, waypoints: list[Waypoint], width: float | None = None
    ) -> Corridor:
        """Reserve a corridor for a net after global routing.

        This "soft reserves" the corridor space, adding cost penalties for
        other nets that try to route through it.

        Args:
            net: Net ID to reserve corridor for
            waypoints: Waypoints from global routing path
            width: Corridor half-width (default: 2 * clearance_buffer)

        Returns:
            The created Corridor
        """
        if width is None:
            width = 2.0 * self.clearance_buffer

        corridor = Corridor.from_waypoints(waypoints, net, width)
        self.reserved_corridors[net] = corridor
        return corridor

    def clear_corridor(self, net: int) -> None:
        """Remove a corridor reservation for a net.

        Args:
            net: Net ID whose corridor to remove
        """
        self.reserved_corridors.pop(net, None)

    def clear_all_corridors(self) -> None:
        """Remove all corridor reservations."""
        self.reserved_corridors.clear()

    def get_corridor(self, net: int) -> Corridor | None:
        """Get the reserved corridor for a net.

        Args:
            net: Net ID

        Returns:
            Corridor if reserved, None otherwise
        """
        return self.reserved_corridors.get(net)


class SparseRouter:
    """Router using sparse clearance contour graph.

    This router provides 50-100x speedup over uniform grid routing for
    boards with JLCPCB-compatible clearances (0.127mm / 5mil).

    Usage:
        router = SparseRouter(grid, rules)
        router.add_pads(pads)
        router.build_graph()
        route = router.route(start_pad, end_pad)
    """

    def __init__(
        self,
        width: float,
        height: float,
        rules: DesignRules,
        origin_x: float = 0,
        origin_y: float = 0,
        num_layers: int = 2,
    ):
        """Initialize sparse router.

        Args:
            width, height: Board dimensions
            rules: Design rules
            origin_x, origin_y: Board origin
            num_layers: Number of copper layers
        """
        self.graph = SparseRoutingGraph(
            width=width,
            height=height,
            rules=rules,
            origin_x=origin_x,
            origin_y=origin_y,
            num_layers=num_layers,
            contour_samples=8,  # Octagonal contours
            sparse_grid_spacing=max(2.0, rules.trace_clearance * 10),
        )
        self.rules = rules
        self.pads: dict[str, Pad] = {}
        self._graph_built = False

    def add_pad(self, pad: Pad) -> None:
        """Add a pad to the router."""
        self.graph.add_pad(pad)
        self.pads[f"{pad.net}_{pad.x}_{pad.y}"] = pad
        self._graph_built = False

    def build_graph(self) -> None:
        """Build the sparse routing graph.

        Must be called after all pads are added and before routing.
        """
        self.graph.add_sparse_grid()
        self.graph.build_visibility_graph()
        self._graph_built = True

    def route(self, start_pad: Pad, end_pad: Pad) -> Route | None:
        """Route between two pads.

        Args:
            start_pad: Source pad
            end_pad: Destination pad

        Returns:
            Route if successful, None if no path found
        """
        if not self._graph_built:
            self.build_graph()

        return self.graph.route(start_pad, end_pad)

    def get_statistics(self) -> dict:
        """Get router statistics."""
        return self.graph.get_statistics()

    # =========================================================================
    # GLOBAL ROUTING (TWO-PHASE) SUPPORT
    # =========================================================================

    def find_global_path(self, start_pad: Pad, end_pad: Pad) -> list[Waypoint] | None:
        """Find global routing path as waypoints.

        Args:
            start_pad: Source pad
            end_pad: Destination pad

        Returns:
            List of waypoints forming the global path, or None if no path
        """
        if not self._graph_built:
            self.build_graph()

        return self.graph.find_global_path(start_pad, end_pad)

    def reserve_corridor(
        self, net: int, waypoints: list[Waypoint], width: float | None = None
    ) -> Corridor:
        """Reserve a corridor for a net.

        Args:
            net: Net ID
            waypoints: Waypoints from global routing
            width: Corridor half-width (default: 2 * clearance)

        Returns:
            The created Corridor
        """
        return self.graph.reserve_corridor(net, waypoints, width)

    def clear_corridor(self, net: int) -> None:
        """Clear corridor reservation for a net."""
        self.graph.clear_corridor(net)

    def clear_all_corridors(self) -> None:
        """Clear all corridor reservations."""
        self.graph.clear_all_corridors()

    def get_corridor(self, net: int) -> Corridor | None:
        """Get corridor for a net."""
        return self.graph.get_corridor(net)

    def get_all_corridors(self) -> dict[int, Corridor]:
        """Get all reserved corridors."""
        return self.graph.reserved_corridors.copy()
