"""
Command Interpreter - Translates strategic commands to geometric operations.

The interpreter bridges the gap between:
- LLM strategic decisions ("route MCLK avoiding analog section")
- Precise PCB modifications (add segment from A to B on layer L)

It uses the existing kicad-tools infrastructure:
- PCBEditor for modifications
- Router for pathfinding
- DRC for verification
"""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..pcb.editor import PCBEditor, Point
from ..router.core import Autorouter
from ..router.primitives import Pad, Route
from ..router.rules import DesignRules
from ..router.layers import Layer

from .state import PCBState, ComponentState, PadState, TraceState
from .vocabulary import SpatialRegion, NetType, RoutingPriority
from .commands import (
    Command,
    CommandResult,
    CommandType,
    PlaceComponentCommand,
    RouteNetCommand,
    DeleteTraceCommand,
    AddViaCommand,
    DefineZoneCommand,
)


@dataclass
class InterpreterConfig:
    """Configuration for the command interpreter."""

    # Design rules
    trace_width: float = 0.2  # mm
    clearance: float = 0.2  # mm
    via_drill: float = 0.3  # mm
    via_size: float = 0.6  # mm

    # Routing preferences
    prefer_orthogonal: bool = True
    grid_size: float = 0.1  # mm
    max_routing_iterations: int = 1000

    # Zone defaults
    zone_min_thickness: float = 0.2
    zone_clearance: float = 0.3


class CommandInterpreter:
    """Interprets and executes PCB commands.

    This is the core execution engine that translates high-level
    LLM commands into precise PCB modifications.
    """

    def __init__(
        self,
        pcb_path: str,
        state: Optional[PCBState] = None,
        config: Optional[InterpreterConfig] = None,
        regions: Optional[list[SpatialRegion]] = None,
    ):
        self.pcb_path = Path(pcb_path)
        self.editor = PCBEditor(str(pcb_path))
        self.config = config or InterpreterConfig()
        self.regions = regions or []

        # Load or use provided state
        if state:
            self.state = state
        else:
            self.state = PCBState.from_pcb(pcb_path)

        # Build region lookup
        self.region_map = {r.name: r for r in self.regions}

        # Track modifications
        self.modifications: list[str] = []

    def execute(self, command: Command) -> CommandResult:
        """Execute a command and return the result."""
        try:
            if isinstance(command, PlaceComponentCommand):
                return self._execute_place(command)
            elif isinstance(command, RouteNetCommand):
                return self._execute_route(command)
            elif isinstance(command, DeleteTraceCommand):
                return self._execute_delete(command)
            elif isinstance(command, AddViaCommand):
                return self._execute_via(command)
            elif isinstance(command, DefineZoneCommand):
                return self._execute_zone(command)
            else:
                return CommandResult(
                    success=False,
                    command_type=command.command_type,
                    message=f"Unknown command type: {type(command).__name__}",
                )
        except Exception as e:
            return CommandResult(
                success=False,
                command_type=command.command_type,
                message=f"Execution error: {str(e)}",
                details={"exception": str(e)},
            )

    # =========================================================================
    # Placement Execution
    # =========================================================================

    def _execute_place(self, cmd: PlaceComponentCommand) -> CommandResult:
        """Execute a placement command."""
        # Resolve target position
        x, y = self._resolve_position(cmd)

        if x is None or y is None:
            return CommandResult(
                success=False,
                command_type=CommandType.PLACE_COMPONENT,
                message=f"Could not resolve position for {cmd.ref}",
            )

        # Resolve rotation
        rotation = cmd.rotation if cmd.rotation is not None else 0
        if cmd.face:
            rotation = {"north": 0, "east": 90, "south": 180, "west": 270}.get(
                cmd.face, 0
            )

        # Execute placement
        success = self.editor.place_component(cmd.ref, x, y, rotation)

        if success:
            self.modifications.append(f"Placed {cmd.ref} at ({x:.2f}, {y:.2f})")

            # Update state
            if cmd.ref in self.state.components:
                comp = self.state.components[cmd.ref]
                comp.x = x
                comp.y = y
                comp.rotation = rotation
                comp.fixed = cmd.fixed

            return CommandResult(
                success=True,
                command_type=CommandType.PLACE_COMPONENT,
                message=f"Placed {cmd.ref} at ({x:.1f}, {y:.1f}) rotated {rotation}°",
                new_position=(x, y),
                new_rotation=rotation,
            )
        else:
            return CommandResult(
                success=False,
                command_type=CommandType.PLACE_COMPONENT,
                message=f"Failed to place {cmd.ref} - component not found",
            )

    def _resolve_position(
        self, cmd: PlaceComponentCommand
    ) -> tuple[Optional[float], Optional[float]]:
        """Resolve a position specification to coordinates."""
        # Explicit position
        if cmd.at:
            return cmd.at[0] + cmd.offset[0], cmd.at[1] + cmd.offset[1]

        # Near another component
        if cmd.near:
            ref_comp = self.state.components.get(cmd.near)
            if ref_comp:
                return (
                    ref_comp.x + cmd.offset[0],
                    ref_comp.y + cmd.offset[1],
                )
            return None, None

        # In a region
        if cmd.region:
            region = self.region_map.get(cmd.region)
            if region:
                cx, cy = region.center
                return cx + cmd.offset[0], cy + cmd.offset[1]
            return None, None

        return None, None

    # =========================================================================
    # Routing Execution
    # =========================================================================

    def _execute_route(self, cmd: RouteNetCommand) -> CommandResult:
        """Execute a routing command."""
        net = self.state.nets.get(cmd.net)
        if not net:
            return CommandResult(
                success=False,
                command_type=CommandType.ROUTE_NET,
                message=f"Net {cmd.net} not found",
            )

        if net.pad_count < 2:
            return CommandResult(
                success=False,
                command_type=CommandType.ROUTE_NET,
                message=f"Net {cmd.net} has fewer than 2 pads",
            )

        # Collect pads for this net
        pads = self._get_net_pads(cmd.net)
        if len(pads) < 2:
            return CommandResult(
                success=False,
                command_type=CommandType.ROUTE_NET,
                message=f"Could not find pads for net {cmd.net}",
            )

        # Determine trace parameters
        trace_width = cmd.trace_width or self.config.trace_width
        clearance = cmd.clearance or self.config.clearance

        # Adjust based on net type
        net_type = NetType.classify(cmd.net)
        priority = RoutingPriority.for_net_type(net_type)
        if cmd.trace_width is None:
            trace_width = priority.trace_width
        if cmd.clearance is None:
            clearance = priority.clearance

        # Build avoidance polygons from regions
        avoid_bounds = []
        for region_name in cmd.avoid_regions:
            region = self.region_map.get(region_name)
            if region:
                avoid_bounds.append(region.bounds)

        # Set up routing
        routes = []
        total_length = 0.0
        vias_added = 0

        # For simple case: create MST of pads and route each edge
        # More sophisticated routing would use the full autorouter
        pad_positions = [(p.x, p.y, p) for p in pads]

        # Simple greedy MST
        connected = {0}
        unconnected = set(range(1, len(pad_positions)))
        edges = []

        while unconnected:
            best_dist = float("inf")
            best_edge = None

            for i in connected:
                for j in unconnected:
                    p1 = pad_positions[i]
                    p2 = pad_positions[j]
                    dist = abs(p1[0] - p2[0]) + abs(p1[1] - p2[1])
                    if dist < best_dist:
                        best_dist = dist
                        best_edge = (i, j)

            if best_edge:
                edges.append(best_edge)
                connected.add(best_edge[1])
                unconnected.remove(best_edge[1])

        # Route each edge
        successful_routes = 0
        failed_routes = []

        for i, j in edges:
            p1 = pad_positions[i]
            p2 = pad_positions[j]

            # Simple direct routing with optional via
            path, via_used = self._find_path(
                p1[0], p1[1], p2[0], p2[1],
                prefer_layer=cmd.prefer_layer,
                avoid_bounds=avoid_bounds,
                prefer_direction=cmd.prefer_direction,
            )

            if path:
                # Add tracks
                layer = cmd.prefer_layer or "F.Cu"
                tracks = self.editor.add_track(
                    cmd.net,
                    path,
                    width=trace_width,
                    layer=layer,
                )

                if tracks:
                    successful_routes += 1
                    for t in tracks:
                        length = ((t.end.x - t.start.x)**2 + (t.end.y - t.start.y)**2)**0.5
                        total_length += length

                    if via_used:
                        vias_added += 1
            else:
                failed_routes.append((
                    f"{p1[2].ref}.{p1[2].number}",
                    f"{p2[2].ref}.{p2[2].number}",
                ))

        self.modifications.append(
            f"Routed {cmd.net}: {successful_routes}/{len(edges)} connections"
        )

        if failed_routes:
            return CommandResult(
                success=successful_routes > 0,
                command_type=CommandType.ROUTE_NET,
                message=f"Partially routed {cmd.net}: {successful_routes}/{len(edges)} connections",
                details={"failed_routes": failed_routes},
                trace_length=total_length,
                vias_added=vias_added,
            )

        return CommandResult(
            success=True,
            command_type=CommandType.ROUTE_NET,
            message=f"Routed {cmd.net}: {len(edges)} connections, {total_length:.1f}mm total",
            trace_length=total_length,
            vias_added=vias_added,
        )

    def _get_net_pads(self, net_name: str) -> list[PadState]:
        """Get all pads belonging to a net."""
        pads = []
        for comp in self.state.components.values():
            for pad in comp.pads:
                if pad.net == net_name:
                    pads.append(pad)
        return pads

    def _find_path(
        self,
        x1: float, y1: float,
        x2: float, y2: float,
        prefer_layer: Optional[str] = None,
        avoid_bounds: Optional[list[tuple[float, float, float, float]]] = None,
        prefer_direction: Optional[str] = None,
    ) -> tuple[Optional[list[tuple[float, float]]], bool]:
        """Find a path between two points.

        Returns (path, via_used).
        For now, uses simple direct/L-shaped routing.
        Full A* routing would be added for complex cases.
        """
        avoid_bounds = avoid_bounds or []

        # Check if direct path crosses any avoidance zone
        direct_blocked = False
        for bounds in avoid_bounds:
            if self._line_crosses_box(x1, y1, x2, y2, bounds):
                direct_blocked = True
                break

        if not direct_blocked:
            # Direct L-shaped route
            path = self._l_route(x1, y1, x2, y2, prefer_direction)
            return path, False

        # Try routing around avoidance zones
        # Simple approach: route via waypoints at zone corners
        for bounds in avoid_bounds:
            bx1, by1, bx2, by2 = bounds
            margin = 2.0  # mm margin around zone

            # Generate candidate waypoints
            waypoints = [
                (bx1 - margin, (by1 + by2) / 2),  # West
                (bx2 + margin, (by1 + by2) / 2),  # East
                ((bx1 + bx2) / 2, by1 - margin),  # North
                ((bx1 + bx2) / 2, by2 + margin),  # South
            ]

            # Filter by preferred direction
            if prefer_direction == "north":
                waypoints = [w for w in waypoints if w[1] < (by1 + by2) / 2]
            elif prefer_direction == "south":
                waypoints = [w for w in waypoints if w[1] > (by1 + by2) / 2]
            elif prefer_direction == "east":
                waypoints = [w for w in waypoints if w[0] > (bx1 + bx2) / 2]
            elif prefer_direction == "west":
                waypoints = [w for w in waypoints if w[0] < (bx1 + bx2) / 2]

            # Try each waypoint
            for wx, wy in waypoints:
                # Check path start -> waypoint -> end
                path1_ok = not any(
                    self._line_crosses_box(x1, y1, wx, wy, b)
                    for b in avoid_bounds
                )
                path2_ok = not any(
                    self._line_crosses_box(wx, wy, x2, y2, b)
                    for b in avoid_bounds
                )

                if path1_ok and path2_ok:
                    # Build path through waypoint
                    path = [(x1, y1), (wx, y1), (wx, wy), (wx, y2), (x2, y2)]
                    # Simplify redundant points
                    path = self._simplify_path(path)
                    return path, False

        # Fallback: direct route (may cause violations)
        path = self._l_route(x1, y1, x2, y2, prefer_direction)
        return path, False

    def _l_route(
        self,
        x1: float, y1: float,
        x2: float, y2: float,
        prefer_direction: Optional[str] = None,
    ) -> list[tuple[float, float]]:
        """Generate an L-shaped route between two points."""
        # Determine which way to route first
        horizontal_first = True

        if prefer_direction in ["north", "south"]:
            horizontal_first = False
        elif prefer_direction in ["east", "west"]:
            horizontal_first = True
        else:
            # Default: horizontal first if wider than tall
            horizontal_first = abs(x2 - x1) > abs(y2 - y1)

        if horizontal_first:
            return [(x1, y1), (x2, y1), (x2, y2)]
        else:
            return [(x1, y1), (x1, y2), (x2, y2)]

    def _line_crosses_box(
        self,
        x1: float, y1: float,
        x2: float, y2: float,
        bounds: tuple[float, float, float, float],
    ) -> bool:
        """Check if a line segment crosses a bounding box."""
        bx1, by1, bx2, by2 = bounds

        # Quick check: both endpoints on same side of box
        if x1 < bx1 and x2 < bx1:
            return False
        if x1 > bx2 and x2 > bx2:
            return False
        if y1 < by1 and y2 < by1:
            return False
        if y1 > by2 and y2 > by2:
            return False

        # Check if either endpoint is inside
        if bx1 <= x1 <= bx2 and by1 <= y1 <= by2:
            return True
        if bx1 <= x2 <= bx2 and by1 <= y2 <= by2:
            return True

        # Check line intersection with box edges
        # Simplified: if endpoints straddle box, likely crosses
        return True

    def _simplify_path(
        self, path: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        """Remove redundant points from a path."""
        if len(path) <= 2:
            return path

        result = [path[0]]
        for i in range(1, len(path) - 1):
            p0 = result[-1]
            p1 = path[i]
            p2 = path[i + 1]

            # Keep point if direction changes
            d1 = (p1[0] - p0[0], p1[1] - p0[1])
            d2 = (p2[0] - p1[0], p2[1] - p1[1])

            # Normalize directions
            def normalize(d):
                if d[0] == 0 and d[1] == 0:
                    return (0, 0)
                return (
                    1 if d[0] > 0 else (-1 if d[0] < 0 else 0),
                    1 if d[1] > 0 else (-1 if d[1] < 0 else 0),
                )

            if normalize(d1) != normalize(d2):
                result.append(p1)

        result.append(path[-1])
        return result

    # =========================================================================
    # Deletion Execution
    # =========================================================================

    def _execute_delete(self, cmd: DeleteTraceCommand) -> CommandResult:
        """Execute a delete command."""
        deleted_count = 0

        if cmd.delete_all_routing and cmd.net:
            # Delete all routing for a net
            deleted = self._delete_net_routing(cmd.net)
            deleted_count = deleted

            if deleted > 0:
                self.modifications.append(f"Deleted all routing for {cmd.net}")

            return CommandResult(
                success=deleted > 0,
                command_type=CommandType.DELETE_NET_ROUTING,
                message=f"Deleted {deleted} traces/vias for {cmd.net}",
            )

        elif cmd.near:
            # Delete traces near a location
            deleted = self._delete_traces_near(
                cmd.near[0], cmd.near[1],
                radius=cmd.radius,
                net=cmd.net,
                layer=cmd.layer,
            )
            deleted_count = deleted

            self.modifications.append(
                f"Deleted {deleted} traces near ({cmd.near[0]:.1f}, {cmd.near[1]:.1f})"
            )

            return CommandResult(
                success=deleted > 0,
                command_type=CommandType.DELETE_TRACE,
                message=f"Deleted {deleted} traces near location",
            )

        return CommandResult(
            success=False,
            command_type=CommandType.DELETE_TRACE,
            message="No deletion target specified",
        )

    def _delete_net_routing(self, net_name: str) -> int:
        """Delete all traces and vias for a net."""
        if not self.editor.doc:
            return 0

        net_num = self.editor.get_net_number(net_name)
        if net_num == 0:
            return 0

        deleted = 0
        to_remove = []

        for child in self.editor.doc.children:
            # Skip atoms (non-list nodes)
            if child.is_atom:
                continue
            if child.name in ("segment", "via"):
                net_node = child.find("net")
                if net_node and int(net_node.get_first_atom()) == net_num:
                    to_remove.append(child)

        for node in to_remove:
            self.editor.doc.children.remove(node)
            deleted += 1

        return deleted

    def _delete_traces_near(
        self,
        x: float, y: float,
        radius: float,
        net: Optional[str] = None,
        layer: Optional[str] = None,
    ) -> int:
        """Delete traces within radius of a point."""
        if not self.editor.doc:
            return 0

        net_num = self.editor.get_net_number(net) if net else None
        deleted = 0
        to_remove = []

        for child in self.editor.doc.children:
            # Skip atoms
            if child.is_atom:
                continue
            if child.name == "segment":
                # Check net
                if net_num:
                    seg_net = child.find("net")
                    if seg_net and int(seg_net.get_first_atom()) != net_num:
                        continue

                # Check layer
                if layer:
                    seg_layer = child.find("layer")
                    if seg_layer and str(seg_layer.get_first_atom()) != layer:
                        continue

                # Check distance
                start = child.find("start")
                end = child.find("end")
                if start and end:
                    s_atoms = start.get_atoms()
                    e_atoms = end.get_atoms()
                    sx = float(s_atoms[0]) if s_atoms else 0
                    sy = float(s_atoms[1]) if len(s_atoms) > 1 else 0
                    ex = float(e_atoms[0]) if e_atoms else 0
                    ey = float(e_atoms[1]) if len(e_atoms) > 1 else 0

                    # Check if segment passes near point
                    if self._point_near_segment(x, y, sx, sy, ex, ey, radius):
                        to_remove.append(child)

        for node in to_remove:
            self.editor.doc.children.remove(node)
            deleted += 1

        return deleted

    def _point_near_segment(
        self,
        px: float, py: float,
        x1: float, y1: float,
        x2: float, y2: float,
        radius: float,
    ) -> bool:
        """Check if a point is within radius of a line segment."""
        dx = x2 - x1
        dy = y2 - y1
        seg_len_sq = dx * dx + dy * dy

        if seg_len_sq < 1e-10:
            return ((x1 - px) ** 2 + (y1 - py) ** 2) ** 0.5 <= radius

        t = ((px - x1) * dx + (py - y1) * dy) / seg_len_sq
        t = max(0, min(1, t))

        cx = x1 + t * dx
        cy = y1 + t * dy

        return ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5 <= radius

    # =========================================================================
    # Via Execution
    # =========================================================================

    def _execute_via(self, cmd: AddViaCommand) -> CommandResult:
        """Execute a via command."""
        size = cmd.size or self.config.via_size
        drill = cmd.drill or self.config.via_drill

        via = self.editor.add_via(
            position=cmd.position,
            net_name=cmd.net,
            size=size,
            drill=drill,
        )

        self.modifications.append(
            f"Added via for {cmd.net} at ({cmd.position[0]:.1f}, {cmd.position[1]:.1f})"
        )

        return CommandResult(
            success=True,
            command_type=CommandType.ADD_VIA,
            message=f"Added via for {cmd.net}",
            vias_added=1,
        )

    # =========================================================================
    # Zone Execution
    # =========================================================================

    def _execute_zone(self, cmd: DefineZoneCommand) -> CommandResult:
        """Execute a zone command."""
        # Resolve bounds
        bounds = cmd.bounds
        if not bounds and cmd.region:
            region = self.region_map.get(cmd.region)
            if region:
                bounds = region.bounds

        if not bounds:
            return CommandResult(
                success=False,
                command_type=CommandType.DEFINE_ZONE,
                message="Could not determine zone bounds",
            )

        # Convert bounds to points
        x1, y1, x2, y2 = bounds
        boundary = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]

        zone = self.editor.add_zone(
            net_name=cmd.net,
            layer=cmd.layer,
            boundary=boundary,
            priority=cmd.priority,
        )

        self.modifications.append(
            f"Added {cmd.net} zone on {cmd.layer}"
        )

        return CommandResult(
            success=True,
            command_type=CommandType.DEFINE_ZONE,
            message=f"Created {cmd.net} zone on {cmd.layer}",
        )

    # =========================================================================
    # Save
    # =========================================================================

    def save(self, output_path: Optional[str] = None):
        """Save the modified PCB."""
        self.editor.save(output_path)

    def get_modification_log(self) -> list[str]:
        """Get log of all modifications made."""
        return self.modifications.copy()
