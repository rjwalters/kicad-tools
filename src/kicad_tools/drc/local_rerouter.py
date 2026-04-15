"""Local rerouting for infeasible DRC clearance violations.

When nudge-based repair cannot resolve a clearance violation (e.g., both
segment endpoints sit at vias), this module rips up the offending segment
and routes a new path around the obstacle using A* on a small scratch grid.

The scratch grid covers only the bounding box of the segment plus padding,
keeping the search space small (typically 20x60 cells at 0.05mm resolution).

Usage:
    from kicad_tools.drc.local_rerouter import LocalRerouter

    rerouter = LocalRerouter(pcb_doc, nets_dict)
    success = rerouter.reroute_segment(
        segment_node, obstacle_x, obstacle_y, obstacle_radius,
        trace_width=0.25, trace_clearance=0.2,
    )
"""

from __future__ import annotations

import heapq
import math
import uuid
from dataclasses import dataclass, field

from ..sexp import SExp


@dataclass(order=True)
class _AStarNode:
    """Lightweight A* node for local grid search."""

    f_score: float
    g_score: float = field(compare=False)
    x: int = field(compare=False)
    y: int = field(compare=False)
    parent: _AStarNode | None = field(compare=False, default=None)


@dataclass
class RerouteResult:
    """Result of a single local reroute attempt."""

    success: bool
    new_segments: int = 0  # Number of replacement segments created
    path_length_mm: float = 0.0  # Total path length


class LocalRerouter:
    """Reroutes individual segments around obstacles using A* on a local grid.

    Builds a small scratch grid around the segment being rerouted, marks
    obstacles (vias, other segments) as blocked, and runs A* to find
    an alternate path from one endpoint to the other.

    The grid uses a dict[tuple[int,int], bool] representation where True
    means blocked. This avoids the overhead of NumPy array allocation for
    the tiny grids used here.
    """

    def __init__(
        self,
        doc: SExp,
        nets: dict[int, str],
        resolution: float = 0.05,
        padding: float = 0.5,
    ):
        """Initialize the local rerouter.

        Args:
            doc: Parsed PCB S-expression document
            nets: Mapping of net number to net name
            resolution: Grid resolution in mm (default: 0.05mm)
            padding: Extra space around segment bounding box in mm (default: 0.5mm)
        """
        self.doc = doc
        self.nets = nets
        self.resolution = resolution
        self.padding = padding

    def reroute_segment(
        self,
        seg_node: SExp,
        obstacle_x: float,
        obstacle_y: float,
        obstacle_radius: float,
        trace_width: float = 0.25,
        trace_clearance: float = 0.2,
        dry_run: bool = False,
    ) -> RerouteResult:
        """Attempt to reroute a segment around an obstacle.

        Rips up the segment and finds a new path from start to end that
        clears the obstacle by at least trace_clearance.

        Args:
            seg_node: The segment S-expression node to reroute
            obstacle_x: X position of the obstacle center (mm)
            obstacle_y: Y position of the obstacle center (mm)
            obstacle_radius: Radius of the obstacle including annular ring (mm)
            trace_width: Width of the trace being rerouted (mm)
            trace_clearance: Required clearance from trace edge to obstacle (mm)
            dry_run: If True, find path but don't modify the PCB

        Returns:
            RerouteResult indicating success/failure and statistics
        """
        # Extract segment endpoints
        start_node = seg_node.find("start")
        end_node = seg_node.find("end")
        if not (start_node and end_node):
            return RerouteResult(success=False)

        start_atoms = start_node.get_atoms()
        end_atoms = end_node.get_atoms()
        sx = float(start_atoms[0]) if start_atoms else 0.0
        sy = float(start_atoms[1]) if len(start_atoms) > 1 else 0.0
        ex = float(end_atoms[0]) if end_atoms else 0.0
        ey = float(end_atoms[1]) if len(end_atoms) > 1 else 0.0

        # Extract segment metadata
        layer_node = seg_node.find("layer")
        seg_layer = layer_node.get_first_atom() if layer_node else "F.Cu"

        net_node = seg_node.find("net")
        seg_net = int(net_node.get_first_atom()) if net_node else 0

        width_node = seg_node.find("width")
        seg_width = float(width_node.get_first_atom()) if width_node else trace_width

        # Build the local grid bounding box
        min_x = min(sx, ex) - self.padding
        min_y = min(sy, ey) - self.padding
        max_x = max(sx, ex) + self.padding
        max_y = max(sy, ey) + self.padding

        # Ensure obstacle is within bounds
        min_x = min(min_x, obstacle_x - obstacle_radius - self.padding)
        min_y = min(min_y, obstacle_y - obstacle_radius - self.padding)
        max_x = max(max_x, obstacle_x + obstacle_radius + self.padding)
        max_y = max(max_y, obstacle_y + obstacle_radius + self.padding)

        cols = int((max_x - min_x) / self.resolution) + 1
        rows = int((max_y - min_y) / self.resolution) + 1

        # Build blocked set -- use a set for fast lookup
        blocked: set[tuple[int, int]] = set()

        # Total blocking radius: obstacle_radius + trace half-width + clearance
        block_radius = obstacle_radius + seg_width / 2 + trace_clearance

        # Mark the primary obstacle (the via/object causing the violation)
        self._mark_circle_blocked(
            blocked, obstacle_x, obstacle_y, block_radius, min_x, min_y, cols, rows
        )

        # Mark other obstacles in the local area
        self._mark_local_obstacles(
            blocked,
            min_x,
            min_y,
            max_x,
            max_y,
            cols,
            rows,
            seg_layer,
            seg_net,
            seg_node,
            seg_width,
            trace_clearance,
        )

        # Convert endpoints to grid coordinates
        start_gx, start_gy = self._world_to_grid(sx, sy, min_x, min_y, cols, rows)
        end_gx, end_gy = self._world_to_grid(ex, ey, min_x, min_y, cols, rows)

        # Ensure start and end are not blocked (they are pinned positions)
        blocked.discard((start_gx, start_gy))
        blocked.discard((end_gx, end_gy))

        # Run A*
        path = self._astar(start_gx, start_gy, end_gx, end_gy, blocked, cols, rows)

        if path is None:
            return RerouteResult(success=False)

        # Convert grid path to world coordinates
        world_path = [self._grid_to_world(gx, gy, min_x, min_y) for gx, gy in path]

        # Simplify path: remove collinear intermediate points
        world_path = self._simplify_path(world_path)

        if len(world_path) < 2:
            return RerouteResult(success=False)

        # Calculate total path length
        total_length = 0.0
        for i in range(len(world_path) - 1):
            dx = world_path[i + 1][0] - world_path[i][0]
            dy = world_path[i + 1][1] - world_path[i][1]
            total_length += math.sqrt(dx * dx + dy * dy)

        if not dry_run:
            # Replace the old segment with new segments
            self._replace_segment(seg_node, world_path, str(seg_layer), seg_net, seg_width)

        return RerouteResult(
            success=True,
            new_segments=len(world_path) - 1,
            path_length_mm=round(total_length, 4),
        )

    def _world_to_grid(
        self,
        x: float,
        y: float,
        origin_x: float,
        origin_y: float,
        cols: int,
        rows: int,
    ) -> tuple[int, int]:
        """Convert world coordinates to local grid indices."""
        gx = round((x - origin_x) / self.resolution)
        gy = round((y - origin_y) / self.resolution)
        return (max(0, min(gx, cols - 1)), max(0, min(gy, rows - 1)))

    def _grid_to_world(
        self,
        gx: int,
        gy: int,
        origin_x: float,
        origin_y: float,
    ) -> tuple[float, float]:
        """Convert local grid indices to world coordinates."""
        return (
            round(origin_x + gx * self.resolution, 4),
            round(origin_y + gy * self.resolution, 4),
        )

    def _mark_circle_blocked(
        self,
        blocked: set[tuple[int, int]],
        cx: float,
        cy: float,
        radius: float,
        origin_x: float,
        origin_y: float,
        cols: int,
        rows: int,
    ) -> None:
        """Mark all grid cells within a circle as blocked."""
        # Convert to grid coordinates
        gx_min = max(0, int((cx - radius - origin_x) / self.resolution))
        gx_max = min(cols - 1, int((cx + radius - origin_x) / self.resolution) + 1)
        gy_min = max(0, int((cy - radius - origin_y) / self.resolution))
        gy_max = min(rows - 1, int((cy + radius - origin_y) / self.resolution) + 1)

        radius_sq = radius * radius

        for gy in range(gy_min, gy_max + 1):
            wy = origin_y + gy * self.resolution
            for gx in range(gx_min, gx_max + 1):
                wx = origin_x + gx * self.resolution
                dist_sq = (wx - cx) ** 2 + (wy - cy) ** 2
                if dist_sq <= radius_sq:
                    blocked.add((gx, gy))

    def _mark_segment_blocked(
        self,
        blocked: set[tuple[int, int]],
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        half_width: float,
        origin_x: float,
        origin_y: float,
        cols: int,
        rows: int,
    ) -> None:
        """Mark grid cells along a segment (with half-width buffer) as blocked.

        Uses the perpendicular distance from each cell to the segment centerline.
        """
        # Bounding box of the segment including half-width
        bx_min = min(x1, x2) - half_width
        bx_max = max(x1, x2) + half_width
        by_min = min(y1, y2) - half_width
        by_max = max(y1, y2) + half_width

        gx_min = max(0, int((bx_min - origin_x) / self.resolution))
        gx_max = min(cols - 1, int((bx_max - origin_x) / self.resolution) + 1)
        gy_min = max(0, int((by_min - origin_y) / self.resolution))
        gy_max = min(rows - 1, int((by_max - origin_y) / self.resolution) + 1)

        dx = x2 - x1
        dy = y2 - y1
        seg_len_sq = dx * dx + dy * dy

        half_width_sq = half_width * half_width

        for gy in range(gy_min, gy_max + 1):
            wy = origin_y + gy * self.resolution
            for gx in range(gx_min, gx_max + 1):
                wx = origin_x + gx * self.resolution

                # Distance from point to segment
                if seg_len_sq < 1e-10:
                    dist_sq = (wx - x1) ** 2 + (wy - y1) ** 2
                else:
                    t = max(0.0, min(1.0, ((wx - x1) * dx + (wy - y1) * dy) / seg_len_sq))
                    cx = x1 + t * dx
                    cy = y1 + t * dy
                    dist_sq = (wx - cx) ** 2 + (wy - cy) ** 2

                if dist_sq <= half_width_sq:
                    blocked.add((gx, gy))

    def _mark_local_obstacles(
        self,
        blocked: set[tuple[int, int]],
        min_x: float,
        min_y: float,
        max_x: float,
        max_y: float,
        cols: int,
        rows: int,
        layer: str | object,
        net: int,
        exclude_seg: SExp,
        trace_width: float,
        trace_clearance: float,
    ) -> None:
        """Mark all PCB objects in the local area as obstacles.

        Marks vias and segments (on the same layer, different net).
        The segment being rerouted (exclude_seg) is excluded.
        """
        layer_str = str(layer)
        # Half-width buffer for blocking other traces:
        # other_trace_half_width + our_trace_half_width + clearance
        our_half_width = trace_width / 2

        # Mark vias as obstacles
        for via_node in self.doc.find_all("via"):
            at_node = via_node.find("at")
            if not at_node:
                continue
            at_atoms = at_node.get_atoms()
            vx = float(at_atoms[0]) if at_atoms else 0.0
            vy = float(at_atoms[1]) if len(at_atoms) > 1 else 0.0

            # Skip vias outside local area (with generous margin)
            size_node = via_node.find("size")
            via_diameter = float(size_node.get_first_atom()) if size_node else 0.6
            via_radius = via_diameter / 2

            if vx + via_radius < min_x or vx - via_radius > max_x:
                continue
            if vy + via_radius < min_y or vy - via_radius > max_y:
                continue

            # Check if via is on a relevant layer
            layers_node = via_node.find("layers")
            if layers_node:
                layer_atoms = layers_node.get_atoms()
                via_layers = [str(a) for a in layer_atoms]
                if layer_str not in via_layers:
                    continue

            # Same-net vias: don't block (our trace can touch our own via)
            via_net_node = via_node.find("net")
            via_net = int(via_net_node.get_first_atom()) if via_net_node else 0
            if via_net == net and net != 0:
                continue

            # Block radius: via_radius + our trace half-width + clearance
            block_r = via_radius + our_half_width + trace_clearance
            self._mark_circle_blocked(blocked, vx, vy, block_r, min_x, min_y, cols, rows)

        # Mark other segments as obstacles
        for other_seg in self.doc.find_all("segment"):
            if other_seg is exclude_seg:
                continue

            other_layer_node = other_seg.find("layer")
            other_layer = other_layer_node.get_first_atom() if other_layer_node else ""
            if str(other_layer) != layer_str:
                continue

            other_net_node = other_seg.find("net")
            other_net = int(other_net_node.get_first_atom()) if other_net_node else 0
            if other_net == net and net != 0:
                continue

            other_start = other_seg.find("start")
            other_end = other_seg.find("end")
            if not (other_start and other_end):
                continue
            os_atoms = other_start.get_atoms()
            oe_atoms = other_end.get_atoms()
            osx = float(os_atoms[0]) if os_atoms else 0.0
            osy = float(os_atoms[1]) if len(os_atoms) > 1 else 0.0
            oex = float(oe_atoms[0]) if oe_atoms else 0.0
            oey = float(oe_atoms[1]) if len(oe_atoms) > 1 else 0.0

            # Skip segments fully outside local area
            if max(osx, oex) < min_x or min(osx, oex) > max_x:
                continue
            if max(osy, oey) < min_y or min(osy, oey) > max_y:
                continue

            other_width_node = other_seg.find("width")
            other_width = float(other_width_node.get_first_atom()) if other_width_node else 0.25

            # Block half-width: other half-width + our half-width + clearance
            block_half = other_width / 2 + our_half_width + trace_clearance
            self._mark_segment_blocked(
                blocked, osx, osy, oex, oey, block_half, min_x, min_y, cols, rows
            )

    def _astar(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        blocked: set[tuple[int, int]],
        cols: int,
        rows: int,
    ) -> list[tuple[int, int]] | None:
        """Run A* search on the local grid.

        Supports orthogonal and diagonal (45-degree) movement.

        Returns:
            List of (gx, gy) grid coordinates from start to end, or None if
            no path exists.
        """
        # Neighbor offsets: (dx, dy, cost_multiplier)
        neighbors = [
            (1, 0, 1.0),
            (-1, 0, 1.0),
            (0, 1, 1.0),
            (0, -1, 1.0),
            (1, 1, 1.414),
            (-1, 1, 1.414),
            (1, -1, 1.414),
            (-1, -1, 1.414),
        ]

        def heuristic(x: int, y: int) -> float:
            # Octile distance (accounts for diagonal moves)
            dx = abs(x - end_x)
            dy = abs(y - end_y)
            return max(dx, dy) + 0.414 * min(dx, dy)

        start_h = heuristic(start_x, start_y)
        start_node = _AStarNode(f_score=start_h, g_score=0.0, x=start_x, y=start_y)

        open_set: list[_AStarNode] = [start_node]
        g_scores: dict[tuple[int, int], float] = {(start_x, start_y): 0.0}
        closed: set[tuple[int, int]] = set()

        while open_set:
            current = heapq.heappop(open_set)
            cx, cy = current.x, current.y

            if cx == end_x and cy == end_y:
                # Reconstruct path
                path: list[tuple[int, int]] = []
                node: _AStarNode | None = current
                while node is not None:
                    path.append((node.x, node.y))
                    node = node.parent
                path.reverse()
                return path

            pos = (cx, cy)
            if pos in closed:
                continue
            closed.add(pos)

            for dx, dy, cost_mult in neighbors:
                nx, ny = cx + dx, cy + dy
                if nx < 0 or nx >= cols or ny < 0 or ny >= rows:
                    continue
                npos = (nx, ny)
                if npos in closed or npos in blocked:
                    continue

                new_g = current.g_score + cost_mult
                if new_g < g_scores.get(npos, float("inf")):
                    g_scores[npos] = new_g
                    new_f = new_g + heuristic(nx, ny)
                    new_node = _AStarNode(f_score=new_f, g_score=new_g, x=nx, y=ny, parent=current)
                    heapq.heappush(open_set, new_node)

        return None  # No path found

    def _simplify_path(self, path: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """Remove collinear intermediate points from a path.

        Consecutive points that lie on the same line are reduced to just
        the start and end of that line segment.
        """
        if len(path) <= 2:
            return path

        simplified: list[tuple[float, float]] = [path[0]]

        for i in range(1, len(path) - 1):
            px, py = simplified[-1]
            cx, cy = path[i]
            nx, ny = path[i + 1]

            # Check if (prev, current, next) are collinear
            # using cross product
            cross = (cx - px) * (ny - py) - (cy - py) * (nx - px)
            if abs(cross) > 1e-10:
                simplified.append(path[i])

        simplified.append(path[-1])
        return simplified

    def _replace_segment(
        self,
        old_seg: SExp,
        path: list[tuple[float, float]],
        layer: str,
        net: int,
        width: float,
    ) -> None:
        """Replace a segment node with new segments along the given path.

        Removes the old segment from the PCB document and inserts new
        segment nodes for each leg of the rerouted path, preserving the
        net number and layer.
        """
        # Find the parent (should be the document root / kicad_pcb node)
        parent = self.doc

        # Find position of old segment in parent's children
        old_index = None
        for i, child in enumerate(parent.children):
            if child is old_seg:
                old_index = i
                break

        if old_index is None:
            # Segment not found as direct child -- shouldn't happen
            return

        # Build new segment nodes
        new_segments: list[SExp] = []
        for i in range(len(path) - 1):
            x1, y1 = path[i]
            x2, y2 = path[i + 1]
            seg = self._make_segment_node(x1, y1, x2, y2, width, layer, net)
            new_segments.append(seg)

        # Remove old segment and insert new ones at the same position
        parent.children.pop(old_index)
        for j, new_seg in enumerate(new_segments):
            parent.children.insert(old_index + j, new_seg)

    def _make_segment_node(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        width: float,
        layer: str,
        net: int,
    ) -> SExp:
        """Create a KiCad segment S-expression node."""
        seg = SExp(name="segment")
        seg.append(SExp.list("start", round(x1, 4), round(y1, 4)))
        seg.append(SExp.list("end", round(x2, 4), round(y2, 4)))
        seg.append(SExp.list("width", round(width, 4)))
        seg.append(SExp.list("layer", layer))
        seg.append(SExp.list("net", net))
        seg.append(SExp.list("uuid", str(uuid.uuid4())))
        return seg
