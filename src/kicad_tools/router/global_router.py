"""
Global router for hierarchical routing.

This module provides the GlobalRouter class that assigns nets to routing
corridors using the RegionGraph abstraction. The global router operates
at a coarse granularity, planning approximate paths through board regions
before detailed routing fills in exact trace geometry.

The global router builds on the existing Corridor class from sparse.py,
converting region-level paths into corridor assignments that guide the
detailed router.

Phase A of hierarchical routing (Issue #1095).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Pad

from .region_graph import RegionGraph
from .sparse import Corridor, Waypoint


@dataclass
class CorridorAssignment:
    """Assignment of a net to a routing corridor via the global router.

    Attributes:
        net: Net ID
        region_path: Sequence of region IDs the net traverses
        corridor: The Corridor object for detailed routing guidance
        waypoint_coords: Waypoint coordinates along the corridor centerline
    """

    net: int
    region_path: list[int]
    corridor: Corridor
    waypoint_coords: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class GlobalRoutingResult:
    """Result of global routing for all nets.

    Attributes:
        assignments: Dictionary mapping net ID to CorridorAssignment
        failed_nets: List of net IDs that could not be globally routed
        region_graph: The RegionGraph used for planning
    """

    assignments: dict[int, CorridorAssignment] = field(default_factory=dict)
    failed_nets: list[int] = field(default_factory=list)
    region_graph: RegionGraph | None = None


class GlobalRouter:
    """Assigns nets to routing corridors using coarse-grid path planning.

    The GlobalRouter takes a RegionGraph and a set of nets (with their pad
    positions) and produces a corridor assignment for each net. These corridors
    guide the detailed router to stay within planned routing channels, reducing
    congestion and improving routability.

    The router processes nets in the provided order, updating region/edge
    utilization after each assignment so that later nets route around earlier
    ones. This sequential greedy approach is simple and effective for Phase A;
    min-cost flow optimization can be added in future phases.

    Usage:
        region_graph = RegionGraph(board_width=65, board_height=56)
        region_graph.register_obstacles(all_pads)
        global_router = GlobalRouter(
            region_graph=region_graph,
            corridor_width=0.5,
        )
        result = global_router.route_all(nets, pads)

    Args:
        region_graph: The coarse-grid board representation
        corridor_width: Half-width of corridors in mm (default: 2x clearance)
        default_layer: Default routing layer index (default: 0)
    """

    def __init__(
        self,
        region_graph: RegionGraph,
        corridor_width: float = 0.5,
        default_layer: int = 0,
    ):
        self.region_graph = region_graph
        self.corridor_width = corridor_width
        self.default_layer = default_layer

    def route_net(
        self,
        net: int,
        pad_positions: list[tuple[float, float]],
    ) -> CorridorAssignment | None:
        """Assign a corridor for a single net.

        Finds the source and target regions from pad positions, routes
        through the region graph, and constructs a Corridor from the
        resulting path.

        For multi-pad nets, routes between the two most distant pads.
        The corridor will cover intermediate pads as well.

        Args:
            net: Net ID
            pad_positions: List of (x, y) coordinates for the net's pads

        Returns:
            CorridorAssignment if successful, None if routing fails
        """
        if len(pad_positions) < 2:
            return None

        # Find source and target regions
        # For multi-pad nets, pick the two most distant pads to define
        # the corridor spanning the full extent of the net.
        src_pos, tgt_pos = self._pick_endpoints(pad_positions)

        src_region = self.region_graph.get_region_at(src_pos[0], src_pos[1])
        tgt_region = self.region_graph.get_region_at(tgt_pos[0], tgt_pos[1])

        if src_region is None or tgt_region is None:
            return None

        # Find path through region graph
        region_path = self.region_graph.find_path(src_region.id, tgt_region.id)
        if region_path is None:
            return None

        # Update utilization
        self.region_graph.update_utilization(region_path)

        # Convert region path to waypoints
        waypoint_coords = self._build_waypoint_coords(
            region_path, src_pos, tgt_pos
        )

        # Build Corridor from waypoints
        waypoints = [
            Waypoint(x=x, y=y, layer=self.default_layer, waypoint_type="global")
            for x, y in waypoint_coords
        ]

        corridor = Corridor.from_waypoints(
            waypoints=waypoints,
            net=net,
            width=self.corridor_width,
        )

        return CorridorAssignment(
            net=net,
            region_path=region_path,
            corridor=corridor,
            waypoint_coords=waypoint_coords,
        )

    def route_all(
        self,
        nets: dict[int, list[tuple[str, str]]],
        pad_dict: dict[tuple[str, str], Pad],
        net_order: list[int] | None = None,
    ) -> GlobalRoutingResult:
        """Assign corridors for all nets.

        Processes nets in order, updating utilization after each assignment
        so later nets avoid congested regions. Nets that cannot be globally
        routed are recorded as failures but do not block other nets.

        Args:
            nets: Dictionary mapping net ID to list of (ref, pin) tuples
            pad_dict: Dictionary mapping (ref, pin) to Pad objects
            net_order: Optional ordering of net IDs (default: sorted by ID)

        Returns:
            GlobalRoutingResult with corridor assignments and failures
        """
        result = GlobalRoutingResult(region_graph=self.region_graph)

        if net_order is None:
            net_order = sorted(n for n in nets.keys() if n != 0)
        else:
            net_order = [n for n in net_order if n != 0]

        for net in net_order:
            if net not in nets:
                continue

            pad_keys = nets[net]
            if len(pad_keys) < 2:
                continue

            # Collect pad positions
            pad_positions: list[tuple[float, float]] = []
            for key in pad_keys:
                pad = pad_dict.get(key)
                if pad is not None:
                    pad_positions.append((pad.x, pad.y))

            if len(pad_positions) < 2:
                continue

            assignment = self.route_net(net, pad_positions)
            if assignment is not None:
                result.assignments[net] = assignment
            else:
                result.failed_nets.append(net)

        return result

    def _pick_endpoints(
        self,
        pad_positions: list[tuple[float, float]],
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Pick the two most distant pads as corridor endpoints.

        For 2-pad nets this is trivial. For multi-pad nets, using the
        most distant pair ensures the corridor spans the full net extent.

        Args:
            pad_positions: List of (x, y) pad coordinates

        Returns:
            Tuple of (source_pos, target_pos)
        """
        if len(pad_positions) == 2:
            return pad_positions[0], pad_positions[1]

        # Find the two most distant pads (diameter of the point set)
        max_dist = -1.0
        best_pair = (pad_positions[0], pad_positions[1])

        for i, p1 in enumerate(pad_positions):
            for p2 in pad_positions[i + 1 :]:
                dx = p1[0] - p2[0]
                dy = p1[1] - p2[1]
                dist = dx * dx + dy * dy  # Skip sqrt for comparison
                if dist > max_dist:
                    max_dist = dist
                    best_pair = (p1, p2)

        return best_pair

    def _build_waypoint_coords(
        self,
        region_path: list[int],
        src_pos: tuple[float, float],
        tgt_pos: tuple[float, float],
    ) -> list[tuple[float, float]]:
        """Build waypoint coordinates from a region path.

        The waypoints start at the source pad position, pass through
        region centers, and end at the target pad position. This gives
        the corridor a smooth centerline anchored to the actual pads.

        Args:
            region_path: List of region IDs
            src_pos: Source pad (x, y) position
            tgt_pos: Target pad (x, y) position

        Returns:
            List of (x, y) waypoint coordinates
        """
        coords: list[tuple[float, float]] = []

        # Start at source pad
        coords.append(src_pos)

        # Add region centers (skip first/last if they're close to pad positions)
        region_centers = self.region_graph.path_to_waypoint_coords(region_path)
        for center in region_centers:
            # Skip if too close to source or target (within half a region width)
            min_dim = min(
                self.region_graph.board_width / self.region_graph.num_cols,
                self.region_graph.board_height / self.region_graph.num_rows,
            ) / 2.0
            dx_src = center[0] - src_pos[0]
            dy_src = center[1] - src_pos[1]
            dx_tgt = center[0] - tgt_pos[0]
            dy_tgt = center[1] - tgt_pos[1]

            dist_src = (dx_src * dx_src + dy_src * dy_src) ** 0.5
            dist_tgt = (dx_tgt * dx_tgt + dy_tgt * dy_tgt) ** 0.5

            if dist_src > min_dim and dist_tgt > min_dim:
                coords.append(center)

        # End at target pad
        coords.append(tgt_pos)

        return coords

    def get_statistics(self) -> dict:
        """Get global routing statistics.

        Returns:
            Dictionary with routing statistics
        """
        return self.region_graph.get_statistics()
