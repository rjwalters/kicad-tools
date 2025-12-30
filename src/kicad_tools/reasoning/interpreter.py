"""
Command Interpreter - Translates strategic commands to geometric operations.

The interpreter bridges the gap between:
- LLM strategic decisions ("route MCLK avoiding analog section")
- Precise PCB modifications (add segment from A to B on layer L)

It uses the existing kicad-tools infrastructure:
- PCBEditor for modifications
- Router for pathfinding (A* with obstacle avoidance)
- DRC for verification
"""

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..pcb.editor import PCBEditor, Point
from ..router.core import Autorouter
from ..router.grid import RoutingGrid
from ..router.pathfinder import Router
from ..router.primitives import Pad, Route, Segment
from ..router.rules import DesignRules
from ..router.layers import Layer, LayerStack

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
class RoutingDiagnostic:
    """Diagnostic information when routing fails."""

    source_pad: str  # "U1.1"
    target_pad: str  # "U2.3"
    reason: str  # "no_path", "blocked_by_obstacle", "clearance_violation"
    blocked_at: Optional[tuple[float, float]] = None  # Position where routing was blocked
    blocking_net: Optional[str] = None  # Net that blocked the path
    suggestions: list[str] = field(default_factory=list)


class ObstacleGridBuilder:
    """Builds a RoutingGrid from PCBState for A* routing.

    This populates the grid with:
    - All component pads as obstacles (except for routing net)
    - Existing traces as obstacles
    - Zones/keepouts as blocked regions
    """

    def __init__(self, state: PCBState, rules: DesignRules, layer_stack: Optional[LayerStack] = None):
        self.state = state
        self.rules = rules
        self.layer_stack = layer_stack or LayerStack.two_layer()

    def build(self) -> RoutingGrid:
        """Build and return the routing grid."""
        # Determine board dimensions from outline
        width = self.state.outline.width or 100.0
        height = self.state.outline.height or 100.0

        # Find origin from outline
        if self.state.outline.points:
            xs = [p[0] for p in self.state.outline.points]
            ys = [p[1] for p in self.state.outline.points]
            origin_x = min(xs)
            origin_y = min(ys)
        else:
            # Fallback: use component positions
            all_pads = []
            for comp in self.state.components.values():
                all_pads.extend(comp.pads)
            if all_pads:
                origin_x = min(p.x for p in all_pads) - 5.0
                origin_y = min(p.y for p in all_pads) - 5.0
                max_x = max(p.x for p in all_pads) + 5.0
                max_y = max(p.y for p in all_pads) + 5.0
                width = max_x - origin_x
                height = max_y - origin_y
            else:
                origin_x, origin_y = 0.0, 0.0

        # Create the grid
        grid = RoutingGrid(
            width=width,
            height=height,
            rules=self.rules,
            origin_x=origin_x,
            origin_y=origin_y,
            layer_stack=self.layer_stack,
        )

        # Add all pads as obstacles
        for comp in self.state.components.values():
            for pad_state in comp.pads:
                pad = self._pad_state_to_pad(pad_state)
                grid.add_pad(pad)

        # Add existing traces as obstacles
        for trace in self.state.traces:
            self._add_trace_to_grid(grid, trace)

        # Add zones as keepouts (except for their own net)
        for zone in self.state.zones:
            if zone.net:
                # Copper pour - add as blocked for other nets
                self._add_zone_to_grid(grid, zone)

        return grid

    def _pad_state_to_pad(self, pad_state: PadState) -> Pad:
        """Convert PadState to router Pad."""
        # Map layer string to Layer enum
        layer = self._layer_from_string(pad_state.layer)

        return Pad(
            x=pad_state.x,
            y=pad_state.y,
            width=pad_state.width,
            height=pad_state.height,
            net=pad_state.net_id,
            net_name=pad_state.net,
            layer=layer,
            ref=pad_state.ref,
            through_hole=pad_state.through_hole,
        )

    def _layer_from_string(self, layer_str: str) -> Layer:
        """Convert layer string to Layer enum."""
        layer_map = {
            "F.Cu": Layer.F_CU,
            "B.Cu": Layer.B_CU,
            "In1.Cu": Layer.IN1_CU,
            "In2.Cu": Layer.IN2_CU,
            "In3.Cu": Layer.IN3_CU,
            "In4.Cu": Layer.IN4_CU,
        }
        return layer_map.get(layer_str, Layer.F_CU)

    def _add_trace_to_grid(self, grid: RoutingGrid, trace: TraceState) -> None:
        """Add an existing trace to the grid as an obstacle."""
        layer = self._layer_from_string(trace.layer)

        # Create a segment and mark it on the grid
        seg = Segment(
            x1=trace.x1,
            y1=trace.y1,
            x2=trace.x2,
            y2=trace.y2,
            width=trace.width,
            layer=layer,
            net=trace.net_id,
            net_name=trace.net,
        )

        # Create a minimal route to use the grid's mark_route method
        route = Route(net=trace.net_id, net_name=trace.net)
        route.segments.append(seg)
        grid.mark_route(route)

    def _add_zone_to_grid(self, grid: RoutingGrid, zone) -> None:
        """Add a zone as a keepout for other nets."""
        layer = self._layer_from_string(zone.layer)
        x1, y1, x2, y2 = zone.bounds
        grid.add_keepout(x1, y1, x2, y2, [layer])


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

    # A* routing options
    use_astar: bool = True  # Use A* pathfinding instead of simple L-routing
    astar_weight: float = 1.0  # A* weight (1.0=optimal, >1.0=faster)
    use_negotiated: bool = False  # Use negotiated congestion routing
    layer_count: int = 2  # Number of copper layers

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

        # A* routing infrastructure (lazy initialization)
        self._routing_grid: Optional[RoutingGrid] = None
        self._router: Optional[Router] = None
        self._design_rules: Optional[DesignRules] = None

    def _get_design_rules(self) -> DesignRules:
        """Get or create design rules from config."""
        if self._design_rules is None:
            self._design_rules = DesignRules(
                trace_width=self.config.trace_width,
                trace_clearance=self.config.clearance,
                via_drill=self.config.via_drill,
                via_diameter=self.config.via_size,
                grid_resolution=self.config.grid_size,
            )
        return self._design_rules

    def _get_layer_stack(self) -> LayerStack:
        """Get layer stack based on config."""
        if self.config.layer_count == 2:
            return LayerStack.two_layer()
        elif self.config.layer_count == 4:
            return LayerStack.four_layer_sig_gnd_pwr_sig()
        elif self.config.layer_count >= 6:
            return LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()
        return LayerStack.two_layer()

    def _get_routing_grid(self) -> RoutingGrid:
        """Get or create the routing grid."""
        if self._routing_grid is None:
            rules = self._get_design_rules()
            layer_stack = self._get_layer_stack()
            builder = ObstacleGridBuilder(self.state, rules, layer_stack)
            self._routing_grid = builder.build()
        return self._routing_grid

    def _get_router(self) -> Router:
        """Get or create the A* router."""
        if self._router is None:
            grid = self._get_routing_grid()
            rules = self._get_design_rules()
            self._router = Router(grid, rules)
        return self._router

    def _invalidate_routing_cache(self) -> None:
        """Invalidate routing grid cache (after modifications)."""
        self._routing_grid = None
        self._router = None

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
        """Execute a routing command using A* pathfinding.

        Uses the A* router with obstacle avoidance to find paths that:
        - Avoid existing traces with configurable clearance
        - Avoid component pads (except target net)
        - Prefer orthogonal and 45° angles
        - Use vias for layer changes when beneficial
        """
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

        # Build avoidance polygons from regions (for A* routing, add as keepouts)
        avoid_bounds = []
        for region_name in cmd.avoid_regions:
            region = self.region_map.get(region_name)
            if region:
                avoid_bounds.append(region.bounds)

        # Set up routing
        total_length = 0.0
        vias_added = 0
        successful_routes = 0
        failed_routes: list[RoutingDiagnostic] = []

        # Build MST of pads and route each edge
        pad_positions = [(p.x, p.y, p) for p in pads]

        # Simple greedy MST (Prim's algorithm)
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

        # Route each MST edge using A* or simple routing
        if self.config.use_astar:
            # Use A* pathfinding
            successful_routes, failed_routes, total_length, vias_added = self._route_with_astar(
                cmd, pads, pad_positions, edges, trace_width, avoid_bounds
            )
        else:
            # Fallback to simple L-routing
            successful_routes, failed_routes, total_length, vias_added = self._route_simple(
                cmd, pads, pad_positions, edges, trace_width, avoid_bounds
            )

        self.modifications.append(
            f"Routed {cmd.net}: {successful_routes}/{len(edges)} connections"
        )

        # Invalidate routing cache after modifications
        self._invalidate_routing_cache()

        if failed_routes:
            # Build diagnostic details
            details = {
                "failed_routes": [
                    {
                        "source": d.source_pad,
                        "target": d.target_pad,
                        "reason": d.reason,
                        "blocking_net": d.blocking_net,
                        "suggestions": d.suggestions,
                    }
                    for d in failed_routes
                ]
            }
            return CommandResult(
                success=successful_routes > 0,
                command_type=CommandType.ROUTE_NET,
                message=f"Partially routed {cmd.net}: {successful_routes}/{len(edges)} connections",
                details=details,
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

    def _route_with_astar(
        self,
        cmd: RouteNetCommand,
        pads: list[PadState],
        pad_positions: list[tuple[float, float, PadState]],
        edges: list[tuple[int, int]],
        trace_width: float,
        avoid_bounds: list[tuple[float, float, float, float]],
    ) -> tuple[int, list[RoutingDiagnostic], float, int]:
        """Route MST edges using A* pathfinding.

        Returns:
            (successful_routes, failed_diagnostics, total_length, vias_added)
        """
        successful_routes = 0
        failed_routes: list[RoutingDiagnostic] = []
        total_length = 0.0
        vias_added = 0

        # Get or create the A* router
        router = self._get_router()
        grid = self._get_routing_grid()

        # Add avoid_bounds as temporary keepouts
        for bounds in avoid_bounds:
            x1, y1, x2, y2 = bounds
            grid.add_keepout(x1, y1, x2, y2)

        # Route each edge
        for i, j in edges:
            p1 = pad_positions[i]
            p2 = pad_positions[j]
            pad1 = p1[2]
            pad2 = p2[2]

            # Convert PadState to router Pad
            source_pad = self._pad_state_to_router_pad(pad1)
            target_pad = self._pad_state_to_router_pad(pad2)

            # Use A* to find path
            route = router.route(
                source_pad,
                target_pad,
                negotiated_mode=self.config.use_negotiated,
                weight=self.config.astar_weight,
            )

            if route and route.segments:
                # Route found - add tracks to PCB
                for seg in route.segments:
                    path = [(seg.x1, seg.y1), (seg.x2, seg.y2)]
                    tracks = self.editor.add_track(
                        cmd.net,
                        path,
                        width=trace_width,
                        layer=seg.layer.kicad_name,
                    )
                    if tracks:
                        for t in tracks:
                            length = ((t.end.x - t.start.x)**2 + (t.end.y - t.start.y)**2)**0.5
                            total_length += length

                # Add vias
                for via in route.vias:
                    self.editor.add_via(
                        position=(via.x, via.y),
                        net_name=cmd.net,
                        size=self.config.via_size,
                        drill=self.config.via_drill,
                    )
                    vias_added += 1

                # Mark route on grid for subsequent routing
                grid.mark_route(route)
                successful_routes += 1
            else:
                # Route failed - generate diagnostic
                diagnostic = self._generate_routing_diagnostic(
                    pad1, pad2, grid, source_pad, target_pad
                )
                failed_routes.append(diagnostic)

        return successful_routes, failed_routes, total_length, vias_added

    def _route_simple(
        self,
        cmd: RouteNetCommand,
        pads: list[PadState],
        pad_positions: list[tuple[float, float, PadState]],
        edges: list[tuple[int, int]],
        trace_width: float,
        avoid_bounds: list[tuple[float, float, float, float]],
    ) -> tuple[int, list[RoutingDiagnostic], float, int]:
        """Route MST edges using simple L-shaped routing (fallback).

        Returns:
            (successful_routes, failed_diagnostics, total_length, vias_added)
        """
        successful_routes = 0
        failed_routes: list[RoutingDiagnostic] = []
        total_length = 0.0
        vias_added = 0

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
                failed_routes.append(RoutingDiagnostic(
                    source_pad=f"{p1[2].ref}.{p1[2].number}",
                    target_pad=f"{p2[2].ref}.{p2[2].number}",
                    reason="no_path_found",
                    suggestions=["Try clearing obstructing traces", "Consider layer change"],
                ))

        return successful_routes, failed_routes, total_length, vias_added

    def _pad_state_to_router_pad(self, pad_state: PadState) -> Pad:
        """Convert PadState to router Pad."""
        layer_map = {
            "F.Cu": Layer.F_CU,
            "B.Cu": Layer.B_CU,
            "In1.Cu": Layer.IN1_CU,
            "In2.Cu": Layer.IN2_CU,
            "In3.Cu": Layer.IN3_CU,
            "In4.Cu": Layer.IN4_CU,
        }
        layer = layer_map.get(pad_state.layer, Layer.F_CU)

        return Pad(
            x=pad_state.x,
            y=pad_state.y,
            width=pad_state.width,
            height=pad_state.height,
            net=pad_state.net_id,
            net_name=pad_state.net,
            layer=layer,
            ref=pad_state.ref,
            through_hole=pad_state.through_hole,
        )

    def _generate_routing_diagnostic(
        self,
        pad1: PadState,
        pad2: PadState,
        grid: RoutingGrid,
        source_pad: Pad,
        target_pad: Pad,
    ) -> RoutingDiagnostic:
        """Generate diagnostic information for a failed route.

        Analyzes the grid to determine why routing failed and suggest fixes.
        """
        source_str = f"{pad1.ref}.{pad1.number}"
        target_str = f"{pad2.ref}.{pad2.number}"

        # Check if source or target is completely blocked
        source_gx, source_gy = grid.world_to_grid(source_pad.x, source_pad.y)
        target_gx, target_gy = grid.world_to_grid(target_pad.x, target_pad.y)

        blocking_net = None
        reason = "no_path"
        suggestions: list[str] = []

        # Check cells around source
        source_blocked = True
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                gx, gy = source_gx + dx, source_gy + dy
                if 0 <= gx < grid.cols and 0 <= gy < grid.rows:
                    # Check all routable layers
                    for layer_idx in grid.get_routable_indices():
                        cell = grid.grid[layer_idx][gy][gx]
                        if not cell.blocked or cell.net == source_pad.net:
                            source_blocked = False
                            break
                        elif cell.blocked and cell.net != 0 and cell.net != source_pad.net:
                            # Find the blocking net name
                            for net_name, net_state in self.state.nets.items():
                                if net_state.net_id == cell.net:
                                    blocking_net = net_name
                                    break

        # Check cells around target
        target_blocked = True
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                gx, gy = target_gx + dx, target_gy + dy
                if 0 <= gx < grid.cols and 0 <= gy < grid.rows:
                    for layer_idx in grid.get_routable_indices():
                        cell = grid.grid[layer_idx][gy][gx]
                        if not cell.blocked or cell.net == target_pad.net:
                            target_blocked = False
                            break
                        elif cell.blocked and cell.net != 0 and cell.net != target_pad.net:
                            for net_name, net_state in self.state.nets.items():
                                if net_state.net_id == cell.net:
                                    blocking_net = net_name
                                    break

        # Determine reason and suggestions
        if source_blocked:
            reason = "source_blocked"
            suggestions.append(f"Source pad {source_str} is surrounded by obstacles")
            if blocking_net:
                suggestions.append(f"Blocked by net '{blocking_net}' - consider rerouting it first")
        elif target_blocked:
            reason = "target_blocked"
            suggestions.append(f"Target pad {target_str} is surrounded by obstacles")
            if blocking_net:
                suggestions.append(f"Blocked by net '{blocking_net}' - consider rerouting it first")
        else:
            reason = "path_congested"
            suggestions.append("Path between pads is congested")
            suggestions.append("Try routing on a different layer")
            if self.config.layer_count == 2:
                suggestions.append("Consider using a 4-layer board for more routing flexibility")

        # Check congestion between source and target
        congestion = grid.get_congestion(
            (source_gx + target_gx) // 2,
            (source_gy + target_gy) // 2,
            0,  # Check layer 0
        )
        if congestion > 0.5:
            suggestions.insert(0, f"High congestion ({congestion:.0%}) in routing area")

        return RoutingDiagnostic(
            source_pad=source_str,
            target_pad=target_str,
            reason=reason,
            blocked_at=((source_pad.x + target_pad.x) / 2, (source_pad.y + target_pad.y) / 2),
            blocking_net=blocking_net,
            suggestions=suggestions,
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
