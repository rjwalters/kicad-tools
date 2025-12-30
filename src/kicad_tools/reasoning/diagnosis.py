"""
Diagnosis Engine - Analyzes failures and suggests alternatives.

When routing or placement fails, the diagnosis engine:
1. Identifies WHY it failed (blocked, clearance, congestion)
2. Analyzes the local area for context
3. Suggests alternative approaches

This provides the LLM with actionable feedback to revise its strategy.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .state import PCBState, ComponentState, TraceState, ViolationState
from .vocabulary import SpatialRegion, describe_position, describe_distance
from .commands import CommandResult, CommandType


class FailureReason(Enum):
    """Categories of routing/placement failures."""

    # Routing failures
    PATH_BLOCKED = "path_blocked"
    CLEARANCE_VIOLATION = "clearance_violation"
    NO_LAYER_AVAILABLE = "no_layer_available"
    CONGESTION = "congestion"

    # Placement failures
    COMPONENT_NOT_FOUND = "component_not_found"
    POSITION_OCCUPIED = "position_occupied"
    OUT_OF_BOUNDS = "out_of_bounds"
    FIXED_COMPONENT = "fixed_component"

    # General
    NET_NOT_FOUND = "net_not_found"
    INVALID_PARAMETERS = "invalid_parameters"
    UNKNOWN = "unknown"


@dataclass
class Obstacle:
    """Something blocking a path."""

    type: str  # "component", "trace", "via", "zone", "keepout"
    name: str  # Reference or description
    position: tuple[float, float]
    bounds: Optional[tuple[float, float, float, float]] = None


@dataclass
class Alternative:
    """A suggested alternative approach."""

    description: str
    direction: Optional[str] = None  # "north", "south", "east", "west"
    layer: Optional[str] = None
    detour_length: Optional[float] = None  # mm
    via_count: int = 0
    trade_offs: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        """Generate prompt-friendly description."""
        parts = [self.description]
        if self.detour_length:
            parts.append(f"adds {self.detour_length:.1f}mm")
        if self.via_count > 0:
            parts.append(f"requires {self.via_count} via(s)")
        if self.trade_offs:
            parts.append(f"trade-offs: {', '.join(self.trade_offs)}")
        return " - ".join(parts)


@dataclass
class RoutingDiagnosis:
    """Diagnosis of a routing attempt."""

    success: bool
    net: str
    start_position: tuple[float, float]
    end_position: tuple[float, float]

    # Failure information
    failure_reason: Optional[FailureReason] = None
    failure_location: Optional[tuple[float, float]] = None
    failure_description: str = ""

    # Obstacles
    blocking_obstacles: list[Obstacle] = field(default_factory=list)

    # Alternatives
    alternatives: list[Alternative] = field(default_factory=list)

    # Context
    congestion_level: float = 0.0  # 0-1, how crowded the area is
    nearby_nets: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        """Generate prompt-friendly diagnosis."""
        lines = []

        if self.success:
            lines.append(f"✓ Successfully routed {self.net}")
            return "\n".join(lines)

        lines.append(f"✗ Failed to route {self.net}")
        lines.append(f"  From: ({self.start_position[0]:.1f}, {self.start_position[1]:.1f})")
        lines.append(f"  To: ({self.end_position[0]:.1f}, {self.end_position[1]:.1f})")
        lines.append("")

        if self.failure_reason:
            lines.append(f"Reason: {self.failure_reason.value}")

        if self.failure_description:
            lines.append(f"Details: {self.failure_description}")

        if self.failure_location:
            lines.append(
                f"Failure at: ({self.failure_location[0]:.1f}, {self.failure_location[1]:.1f})"
            )

        if self.blocking_obstacles:
            lines.append("")
            lines.append("Blocking obstacles:")
            for obs in self.blocking_obstacles[:5]:
                lines.append(f"  - {obs.type}: {obs.name} at ({obs.position[0]:.1f}, {obs.position[1]:.1f})")

        if self.alternatives:
            lines.append("")
            lines.append("Alternatives:")
            for i, alt in enumerate(self.alternatives[:3], 1):
                lines.append(f"  {i}. {alt.to_prompt()}")

        return "\n".join(lines)


@dataclass
class PlacementDiagnosis:
    """Diagnosis of a placement attempt."""

    success: bool
    ref: str
    target_position: tuple[float, float]

    failure_reason: Optional[FailureReason] = None
    failure_description: str = ""

    blocking_components: list[str] = field(default_factory=list)
    suggested_positions: list[tuple[float, float]] = field(default_factory=list)

    def to_prompt(self) -> str:
        """Generate prompt-friendly diagnosis."""
        lines = []

        if self.success:
            lines.append(f"✓ Successfully placed {self.ref}")
            return "\n".join(lines)

        lines.append(f"✗ Failed to place {self.ref}")
        lines.append(f"  Target: ({self.target_position[0]:.1f}, {self.target_position[1]:.1f})")

        if self.failure_reason:
            lines.append(f"Reason: {self.failure_reason.value}")

        if self.failure_description:
            lines.append(f"Details: {self.failure_description}")

        if self.blocking_components:
            lines.append(f"Blocked by: {', '.join(self.blocking_components)}")

        if self.suggested_positions:
            lines.append("Suggested alternatives:")
            for i, pos in enumerate(self.suggested_positions[:3], 1):
                lines.append(f"  {i}. ({pos[0]:.1f}, {pos[1]:.1f})")

        return "\n".join(lines)


class DiagnosisEngine:
    """Engine for analyzing failures and suggesting alternatives."""

    def __init__(self, state: PCBState, regions: Optional[list[SpatialRegion]] = None):
        self.state = state
        self.regions = regions or []
        self.region_map = {r.name: r for r in self.regions}

    def diagnose_routing(
        self,
        result: CommandResult,
        net: str,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> RoutingDiagnosis:
        """Diagnose a routing attempt."""
        if result.success:
            return RoutingDiagnosis(
                success=True,
                net=net,
                start_position=start,
                end_position=end,
            )

        # Analyze failure
        failure_reason = self._determine_routing_failure(result, start, end)
        obstacles = self._find_obstacles_on_path(start, end)
        alternatives = self._generate_routing_alternatives(start, end, obstacles)
        congestion = self._calculate_congestion(start, end)
        nearby = self._find_nearby_nets(start, end)

        return RoutingDiagnosis(
            success=False,
            net=net,
            start_position=start,
            end_position=end,
            failure_reason=failure_reason,
            failure_location=self._find_failure_point(start, end, obstacles),
            failure_description=result.message,
            blocking_obstacles=obstacles,
            alternatives=alternatives,
            congestion_level=congestion,
            nearby_nets=nearby,
        )

    def diagnose_placement(
        self,
        result: CommandResult,
        ref: str,
        target: tuple[float, float],
    ) -> PlacementDiagnosis:
        """Diagnose a placement attempt."""
        if result.success:
            return PlacementDiagnosis(
                success=True,
                ref=ref,
                target_position=target,
            )

        failure_reason = self._determine_placement_failure(result)
        blocking = self._find_blocking_components(target)
        suggested = self._suggest_alternative_positions(ref, target)

        return PlacementDiagnosis(
            success=False,
            ref=ref,
            target_position=target,
            failure_reason=failure_reason,
            failure_description=result.message,
            blocking_components=blocking,
            suggested_positions=suggested,
        )

    def analyze_violations(self) -> str:
        """Generate a summary of current violations with suggested fixes."""
        lines = []

        if not self.state.violations:
            return "No DRC violations."

        lines.append(f"## DRC Violations: {len(self.state.violations)}")
        lines.append("")

        # Group by type
        by_type: dict[str, list[ViolationState]] = {}
        for v in self.state.violations:
            if v.type not in by_type:
                by_type[v.type] = []
            by_type[v.type].append(v)

        for vtype, violations in sorted(
            by_type.items(), key=lambda x: len(x[1]), reverse=True
        ):
            lines.append(f"### {vtype}: {len(violations)}")

            # Analyze first few of each type
            for v in violations[:3]:
                lines.append(f"  Location: ({v.x:.1f}, {v.y:.1f})")
                if v.nets:
                    lines.append(f"  Nets: {', '.join(v.nets)}")

                # Suggest fix
                fix = self._suggest_violation_fix(v)
                if fix:
                    lines.append(f"  Suggested fix: {fix}")
                lines.append("")

            if len(violations) > 3:
                lines.append(f"  ... and {len(violations) - 3} more")
            lines.append("")

        return "\n".join(lines)

    # =========================================================================
    # Internal Analysis Methods
    # =========================================================================

    def _determine_routing_failure(
        self,
        result: CommandResult,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> FailureReason:
        """Determine why routing failed."""
        msg = result.message.lower()

        if "blocked" in msg or "obstacle" in msg:
            return FailureReason.PATH_BLOCKED
        if "clearance" in msg:
            return FailureReason.CLEARANCE_VIOLATION
        if "layer" in msg:
            return FailureReason.NO_LAYER_AVAILABLE
        if "congestion" in msg or "congested" in msg:
            return FailureReason.CONGESTION
        if "not found" in msg:
            return FailureReason.NET_NOT_FOUND

        return FailureReason.UNKNOWN

    def _determine_placement_failure(self, result: CommandResult) -> FailureReason:
        """Determine why placement failed."""
        msg = result.message.lower()

        if "not found" in msg:
            return FailureReason.COMPONENT_NOT_FOUND
        if "occupied" in msg or "overlap" in msg:
            return FailureReason.POSITION_OCCUPIED
        if "out of" in msg or "bounds" in msg:
            return FailureReason.OUT_OF_BOUNDS
        if "fixed" in msg:
            return FailureReason.FIXED_COMPONENT

        return FailureReason.UNKNOWN

    def _find_obstacles_on_path(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> list[Obstacle]:
        """Find obstacles along a path."""
        obstacles = []

        x1, y1 = start
        x2, y2 = end

        # Check components
        for ref, comp in self.state.components.items():
            bounds = comp.bounds
            if self._line_intersects_box(x1, y1, x2, y2, bounds):
                obstacles.append(Obstacle(
                    type="component",
                    name=ref,
                    position=(comp.x, comp.y),
                    bounds=bounds,
                ))

        # Check traces
        for trace in self.state.traces:
            if self._segments_intersect(
                x1, y1, x2, y2,
                trace.x1, trace.y1, trace.x2, trace.y2,
            ):
                obstacles.append(Obstacle(
                    type="trace",
                    name=trace.net,
                    position=((trace.x1 + trace.x2) / 2, (trace.y1 + trace.y2) / 2),
                ))

        # Check keepout regions
        for region in self.regions:
            if region.is_keepout:
                if self._line_intersects_box(x1, y1, x2, y2, region.bounds):
                    obstacles.append(Obstacle(
                        type="keepout",
                        name=region.name,
                        position=region.center,
                        bounds=region.bounds,
                    ))

        return obstacles

    def _generate_routing_alternatives(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        obstacles: list[Obstacle],
    ) -> list[Alternative]:
        """Generate alternative routing strategies."""
        alternatives = []

        if not obstacles:
            return alternatives

        x1, y1 = start
        x2, y2 = end
        direct_length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5

        # Find bounding box of obstacles
        all_bounds = [o.bounds for o in obstacles if o.bounds]
        if not all_bounds:
            return alternatives

        min_x = min(b[0] for b in all_bounds)
        min_y = min(b[1] for b in all_bounds)
        max_x = max(b[2] for b in all_bounds)
        max_y = max(b[3] for b in all_bounds)

        margin = 3.0  # mm clearance

        # Northern route
        if min_y > min(y1, y2):
            north_y = min_y - margin
            detour = abs(y1 - north_y) + abs(north_y - y2)
            alternatives.append(Alternative(
                description="Route north around obstacle",
                direction="north",
                detour_length=detour - abs(y2 - y1) if detour > abs(y2 - y1) else 0,
                trade_offs=["longer path"],
            ))

        # Southern route
        if max_y < max(y1, y2):
            south_y = max_y + margin
            detour = abs(y1 - south_y) + abs(south_y - y2)
            alternatives.append(Alternative(
                description="Route south around obstacle",
                direction="south",
                detour_length=detour - abs(y2 - y1) if detour > abs(y2 - y1) else 0,
                trade_offs=["longer path"],
            ))

        # Eastern route
        if max_x < max(x1, x2):
            east_x = max_x + margin
            detour = abs(x1 - east_x) + abs(east_x - x2)
            alternatives.append(Alternative(
                description="Route east around obstacle",
                direction="east",
                detour_length=detour - abs(x2 - x1) if detour > abs(x2 - x1) else 0,
                trade_offs=["longer path"],
            ))

        # Western route
        if min_x > min(x1, x2):
            west_x = min_x - margin
            detour = abs(x1 - west_x) + abs(west_x - x2)
            alternatives.append(Alternative(
                description="Route west around obstacle",
                direction="west",
                detour_length=detour - abs(x2 - x1) if detour > abs(x2 - x1) else 0,
                trade_offs=["longer path"],
            ))

        # Layer change
        alternatives.append(Alternative(
            description="Change layer using via",
            layer="B.Cu",
            via_count=2,
            trade_offs=["adds vias", "uses back copper"],
        ))

        return alternatives

    def _find_failure_point(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        obstacles: list[Obstacle],
    ) -> Optional[tuple[float, float]]:
        """Estimate where routing failed."""
        if not obstacles:
            return None

        # Return position of first obstacle
        return obstacles[0].position

    def _calculate_congestion(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> float:
        """Calculate congestion level along path (0-1)."""
        # Count traces and components in the path area
        x1, y1 = start
        x2, y2 = end

        # Define a corridor
        min_x = min(x1, x2) - 5
        max_x = max(x1, x2) + 5
        min_y = min(y1, y2) - 5
        max_y = max(y1, y2) + 5

        area = (max_x - min_x) * (max_y - min_y)
        if area == 0:
            return 0

        # Count items in corridor
        trace_count = sum(
            1 for t in self.state.traces
            if min_x <= t.x1 <= max_x and min_y <= t.y1 <= max_y
        )
        comp_count = sum(
            1 for c in self.state.components.values()
            if min_x <= c.x <= max_x and min_y <= c.y <= max_y
        )

        # Normalize to 0-1 (empirical formula)
        density = (trace_count * 0.5 + comp_count * 2) / (area / 100)
        return min(1.0, density)

    def _find_nearby_nets(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> list[str]:
        """Find nets routed near the path."""
        nearby = set()
        x1, y1 = start
        x2, y2 = end

        for trace in self.state.traces:
            # Check if trace is within 3mm of path
            dist = self._segment_distance(
                x1, y1, x2, y2,
                trace.x1, trace.y1, trace.x2, trace.y2,
            )
            if dist < 3.0:
                nearby.add(trace.net)

        return list(nearby)[:10]

    def _find_blocking_components(
        self, position: tuple[float, float]
    ) -> list[str]:
        """Find components that would block a placement."""
        blocking = []
        x, y = position

        for ref, comp in self.state.components.items():
            bx1, by1, bx2, by2 = comp.bounds
            if bx1 <= x <= bx2 and by1 <= y <= by2:
                blocking.append(ref)

        return blocking

    def _suggest_alternative_positions(
        self,
        ref: str,
        target: tuple[float, float],
    ) -> list[tuple[float, float]]:
        """Suggest alternative placement positions."""
        suggestions = []
        x, y = target

        # Try offsets in each direction
        for dx, dy in [(5, 0), (-5, 0), (0, 5), (0, -5), (5, 5), (-5, -5)]:
            new_x, new_y = x + dx, y + dy
            # Check if position is clear
            blocking = self._find_blocking_components((new_x, new_y))
            if not blocking:
                suggestions.append((new_x, new_y))
                if len(suggestions) >= 3:
                    break

        return suggestions

    def _suggest_violation_fix(self, violation: ViolationState) -> Optional[str]:
        """Suggest a fix for a violation."""
        vtype = violation.type

        if vtype == "shorting_items":
            if violation.nets:
                return f"Delete traces for {violation.nets[0]} near violation and reroute"
            return "Delete one of the shorting traces and reroute"

        if vtype == "clearance":
            return "Widen trace spacing or reroute one of the traces"

        if vtype == "unconnected_items":
            if violation.nets:
                return f"Route net {violation.nets[0]}"
            return "Complete the connection"

        if vtype == "track_width":
            return "Increase trace width to meet minimum"

        return None

    # =========================================================================
    # Geometry Helpers
    # =========================================================================

    def _line_intersects_box(
        self,
        x1: float, y1: float,
        x2: float, y2: float,
        bounds: tuple[float, float, float, float],
    ) -> bool:
        """Check if a line intersects a bounding box."""
        bx1, by1, bx2, by2 = bounds

        # Quick rejection
        if max(x1, x2) < bx1 or min(x1, x2) > bx2:
            return False
        if max(y1, y2) < by1 or min(y1, y2) > by2:
            return False

        # Check if either endpoint is inside
        if bx1 <= x1 <= bx2 and by1 <= y1 <= by2:
            return True
        if bx1 <= x2 <= bx2 and by1 <= y2 <= by2:
            return True

        # More thorough check would test line against box edges
        return True  # Conservative: assume intersection

    def _segments_intersect(
        self,
        x1: float, y1: float, x2: float, y2: float,
        x3: float, y3: float, x4: float, y4: float,
    ) -> bool:
        """Check if two line segments intersect."""
        # Simplified: check if bounding boxes overlap and segments are close
        if max(x1, x2) < min(x3, x4) or max(x3, x4) < min(x1, x2):
            return False
        if max(y1, y2) < min(y3, y4) or max(y3, y4) < min(y1, y2):
            return False

        # Check minimum distance
        dist = self._segment_distance(x1, y1, x2, y2, x3, y3, x4, y4)
        return dist < 0.2  # Traces closer than 0.2mm

    def _segment_distance(
        self,
        x1: float, y1: float, x2: float, y2: float,
        x3: float, y3: float, x4: float, y4: float,
    ) -> float:
        """Calculate minimum distance between two line segments."""
        # Simplified: use center-to-center distance
        c1x, c1y = (x1 + x2) / 2, (y1 + y2) / 2
        c2x, c2y = (x3 + x4) / 2, (y3 + y4) / 2
        return ((c1x - c2x) ** 2 + (c1y - c2y) ** 2) ** 0.5
