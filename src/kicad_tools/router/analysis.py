"""
Routability analysis and failure diagnostics for PCB routing.

This module provides:
- BlockingObstacle: Represents an obstacle blocking a route
- RoutingFailureDiagnostic: Detailed info about why a route failed
- NetRoutabilityReport: Pre-routing analysis for a single net
- RoutabilityReport: Complete routability analysis for all nets
- RoutabilityAnalyzer: Analyzes routability before routing
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Autorouter
    from .primitives import Pad

from .grid import RoutingGrid
from .rules import DesignRules


class ObstacleType(Enum):
    """Type of obstacle blocking a route."""

    PAD = auto()
    TRACE = auto()
    VIA = auto()
    ZONE = auto()
    COMPONENT = auto()
    KEEPOUT = auto()
    BOARD_EDGE = auto()


class RoutingSeverity(Enum):
    """Severity level for routing problems."""

    LOW = auto()  # Minor issue, likely routable with detour
    MEDIUM = auto()  # Significant congestion, may need via or layer change
    HIGH = auto()  # Severely constrained, routing will be difficult
    CRITICAL = auto()  # Likely unroutable without design changes


@dataclass
class BlockingObstacle:
    """Represents an obstacle blocking a route path."""

    obstacle_type: ObstacleType
    x: float
    y: float
    width: float
    height: float
    net: int = 0
    net_name: str = ""
    ref: str = ""  # Component reference (e.g., "U4")
    layer: str = ""

    @property
    def position(self) -> tuple[float, float]:
        """Return (x, y) position."""
        return (self.x, self.y)

    def __str__(self) -> str:
        if self.ref:
            return f"{self.obstacle_type.name}({self.ref} at {self.x:.2f}, {self.y:.2f})"
        elif self.net_name:
            return f"{self.obstacle_type.name}({self.net_name} at {self.x:.2f}, {self.y:.2f})"
        else:
            return f"{self.obstacle_type.name}(at {self.x:.2f}, {self.y:.2f})"


@dataclass
class RouteAlternative:
    """Represents an alternative routing option."""

    description: str
    via_count: int = 0
    extra_length_mm: float = 0.0
    feasible: bool = True
    reason: str = ""  # Why not feasible if feasible=False

    def __str__(self) -> str:
        if not self.feasible:
            return f"[X] {self.description}: {self.reason}"
        elif self.via_count > 0:
            return f"[+] {self.description} (requires {self.via_count} via(s))"
        elif self.extra_length_mm > 0:
            return f"[+] {self.description} (+{self.extra_length_mm:.1f}mm)"
        else:
            return f"[+] {self.description}"


@dataclass
class RoutingFailureDiagnostic:
    """Detailed diagnostic information about why a route failed."""

    net: int
    net_name: str
    source_pad: tuple[str, str]  # (ref, pin)
    source_position: tuple[float, float]
    target_pad: tuple[str, str]  # (ref, pin)
    target_position: tuple[float, float]
    straight_line_distance: float
    blocking_obstacles: list[BlockingObstacle] = field(default_factory=list)
    blocked_at_position: tuple[float, float] | None = None
    explored_cells: int = 0
    alternatives: list[RouteAlternative] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    @property
    def severity(self) -> RoutingSeverity:
        """Determine severity based on obstacle analysis."""
        if not self.blocking_obstacles:
            return RoutingSeverity.MEDIUM
        # Multiple blocking obstacles = more severe
        if len(self.blocking_obstacles) >= 3:
            return RoutingSeverity.CRITICAL
        elif len(self.blocking_obstacles) >= 2:
            return RoutingSeverity.HIGH
        else:
            return RoutingSeverity.MEDIUM

    def __str__(self) -> str:
        source = f"{self.source_pad[0]}.{self.source_pad[1]}"
        target = f"{self.target_pad[0]}.{self.target_pad[1]}"
        return (
            f"Route failure: {self.net_name} ({source} -> {target})\n"
            f"  Distance: {self.straight_line_distance:.2f}mm\n"
            f"  Blockers: {len(self.blocking_obstacles)}\n"
            f"  Severity: {self.severity.name}"
        )


@dataclass
class CongestionZone:
    """Represents a congested region of the board."""

    x: float
    y: float
    width: float
    height: float
    layer: int
    density: float  # 0.0 to 1.0
    competing_nets: int  # Number of nets trying to use this area
    available_channels: int  # Number of available routing channels

    @property
    def is_bottleneck(self) -> bool:
        """Check if this is a routing bottleneck."""
        return self.competing_nets > self.available_channels

    def __str__(self) -> str:
        status = "BOTTLENECK" if self.is_bottleneck else "congested"
        return (
            f"Congestion at ({self.x:.1f}, {self.y:.1f}): "
            f"{self.competing_nets} nets / {self.available_channels} channels ({status})"
        )


@dataclass
class NetRoutabilityReport:
    """Pre-routing routability analysis for a single net."""

    net: int
    net_name: str
    pad_count: int
    pads: list[tuple[str, str]]  # [(ref, pin), ...]
    total_manhattan_distance: float
    estimated_route_length: float
    blocking_obstacles: list[BlockingObstacle] = field(default_factory=list)
    congestion_zones: list[CongestionZone] = field(default_factory=list)
    severity: RoutingSeverity = RoutingSeverity.LOW
    routable: bool = True
    alternatives: list[RouteAlternative] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    @property
    def difficulty_score(self) -> float:
        """Score from 0-100 indicating routing difficulty (higher = harder)."""
        score = 0.0
        # Base difficulty from obstacle count
        score += len(self.blocking_obstacles) * 15
        # Congestion zones add difficulty
        score += sum(z.density * 20 for z in self.congestion_zones)
        # Bottlenecks are especially bad
        score += sum(25 for z in self.congestion_zones if z.is_bottleneck)
        return min(100.0, score)

    def __str__(self) -> str:
        status = "OK" if self.routable else "PROBLEM"
        return (
            f"Net {self.net_name}: {self.pad_count} pads, "
            f"{self.total_manhattan_distance:.1f}mm manhattan, "
            f"difficulty={self.difficulty_score:.0f} [{status}]"
        )


@dataclass
class RoutabilityReport:
    """Complete pre-routing routability analysis."""

    net_reports: list[NetRoutabilityReport] = field(default_factory=list)
    problem_nets: list[NetRoutabilityReport] = field(default_factory=list)
    congestion_zones: list[CongestionZone] = field(default_factory=list)
    estimated_success_rate: float = 1.0
    layer_utilization: dict[str, float] = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)

    @property
    def total_nets(self) -> int:
        return len(self.net_reports)

    @property
    def expected_routable(self) -> int:
        return len([r for r in self.net_reports if r.routable])

    def __str__(self) -> str:
        lines = [
            "Routability Analysis",
            "=" * 50,
            f"Estimated completion: {self.estimated_success_rate * 100:.0f}% "
            f"({self.expected_routable}/{self.total_nets} nets)",
        ]
        if self.problem_nets:
            lines.append(f"\nProblem Nets ({len(self.problem_nets)}):")
            for net in self.problem_nets[:5]:  # Show first 5
                lines.append(f"  {net}")
        if self.recommendations:
            lines.append("\nRecommendations:")
            for i, rec in enumerate(self.recommendations, 1):
                lines.append(f"  {i}. {rec}")
        return "\n".join(lines)


class RoutabilityAnalyzer:
    """Analyzes PCB routability before actual routing.

    Provides pre-routing analysis to estimate success rate and identify
    potential problems before spending time on routing.

    Example::

        from kicad_tools.router import RoutabilityAnalyzer

        analyzer = RoutabilityAnalyzer(autorouter)
        report = analyzer.analyze()
        print(f"Estimated routability: {report.estimated_success_rate * 100:.0f}%")

        for problem in report.problem_nets:
            print(f"\\n{problem.net_name}:")
            print(f"  Severity: {problem.severity.name}")
            for suggestion in problem.suggestions:
                print(f"  - {suggestion}")
    """

    def __init__(
        self,
        autorouter: Autorouter,
    ):
        """Initialize the analyzer.

        Args:
            autorouter: The Autorouter instance to analyze
        """
        self.autorouter = autorouter
        self.grid: RoutingGrid = autorouter.grid
        self.rules: DesignRules = autorouter.rules
        self.pads = autorouter.pads
        self.nets = autorouter.nets
        self.net_names = autorouter.net_names

    def analyze(self) -> RoutabilityReport:
        """Perform complete routability analysis.

        Returns:
            RoutabilityReport with analysis results
        """
        report = RoutabilityReport()

        # Analyze each net
        for net_id, pad_keys in self.nets.items():
            if net_id == 0:
                continue
            net_report = self._analyze_net(net_id, pad_keys)
            report.net_reports.append(net_report)
            if not net_report.routable or net_report.severity in (
                RoutingSeverity.HIGH,
                RoutingSeverity.CRITICAL,
            ):
                report.problem_nets.append(net_report)

        # Analyze global congestion
        report.congestion_zones = self._find_congestion_zones()

        # Calculate estimated success rate
        total = len(report.net_reports)
        if total > 0:
            easy_nets = len([r for r in report.net_reports if r.severity == RoutingSeverity.LOW])
            medium_nets = len(
                [r for r in report.net_reports if r.severity == RoutingSeverity.MEDIUM]
            )
            hard_nets = len([r for r in report.net_reports if r.severity == RoutingSeverity.HIGH])
            critical_nets = len(
                [r for r in report.net_reports if r.severity == RoutingSeverity.CRITICAL]
            )

            # Estimate success rates per difficulty
            # Easy: ~98%, Medium: ~90%, Hard: ~70%, Critical: ~40%
            expected = (
                easy_nets * 0.98 + medium_nets * 0.90 + hard_nets * 0.70 + critical_nets * 0.40
            )
            report.estimated_success_rate = expected / total

        # Calculate layer utilization
        report.layer_utilization = self._calculate_layer_utilization()

        # Generate recommendations
        report.recommendations = self._generate_recommendations(report)

        return report

    def _analyze_net(self, net_id: int, pad_keys: list[tuple[str, str]]) -> NetRoutabilityReport:
        """Analyze routability of a single net.

        Args:
            net_id: Net ID
            pad_keys: List of (ref, pin) tuples for this net

        Returns:
            NetRoutabilityReport for this net
        """
        net_name = self.net_names.get(net_id, f"Net_{net_id}")
        pad_objs = [self.pads[k] for k in pad_keys if k in self.pads]

        report = NetRoutabilityReport(
            net=net_id,
            net_name=net_name,
            pad_count=len(pad_objs),
            pads=pad_keys,
            total_manhattan_distance=0.0,
            estimated_route_length=0.0,
        )

        if len(pad_objs) < 2:
            return report  # Single-pad net, trivially routable

        # Calculate total manhattan distance (MST approximation)
        report.total_manhattan_distance = self._calculate_mst_length(pad_objs)
        # Estimate actual route length (usually 10-30% longer due to detours)
        report.estimated_route_length = report.total_manhattan_distance * 1.2

        # Analyze path between each pair of pads
        blocking_obstacles: list[BlockingObstacle] = []
        congestion_zones: list[CongestionZone] = []

        for i, pad1 in enumerate(pad_objs):
            for pad2 in pad_objs[i + 1 :]:
                obstacles, zones = self._analyze_path(pad1, pad2, net_id)
                blocking_obstacles.extend(obstacles)
                congestion_zones.extend(zones)

        report.blocking_obstacles = blocking_obstacles
        report.congestion_zones = congestion_zones

        # Determine severity
        if len(blocking_obstacles) == 0:
            report.severity = RoutingSeverity.LOW
        elif len(blocking_obstacles) <= 2:
            report.severity = RoutingSeverity.MEDIUM
        elif len(blocking_obstacles) <= 5:
            report.severity = RoutingSeverity.HIGH
        else:
            report.severity = RoutingSeverity.CRITICAL
            report.routable = False

        # Check for bottlenecks
        if any(z.is_bottleneck for z in congestion_zones):
            if report.severity == RoutingSeverity.LOW:
                report.severity = RoutingSeverity.MEDIUM
            elif report.severity == RoutingSeverity.MEDIUM:
                report.severity = RoutingSeverity.HIGH

        # Generate suggestions
        report.suggestions = self._generate_net_suggestions(report)
        report.alternatives = self._generate_alternatives(blocking_obstacles)

        return report

    def _analyze_path(
        self, pad1: Pad, pad2: Pad, net_id: int
    ) -> tuple[list[BlockingObstacle], list[CongestionZone]]:
        """Analyze the direct path between two pads for obstacles.

        Args:
            pad1: Source pad
            pad2: Target pad
            net_id: Net ID

        Returns:
            Tuple of (blocking_obstacles, congestion_zones)
        """
        obstacles: list[BlockingObstacle] = []
        congestion_zones: list[CongestionZone] = []

        # Get grid coordinates
        gx1, gy1 = self.grid.world_to_grid(pad1.x, pad1.y)
        gx2, gy2 = self.grid.world_to_grid(pad2.x, pad2.y)

        # Sample points along the straight-line path
        dx = gx2 - gx1
        dy = gy2 - gy1
        steps = max(abs(dx), abs(dy), 1)

        layer_idx = self.grid.layer_to_index(pad1.layer.value)
        checked_positions: set[tuple[int, int]] = set()

        for step in range(steps + 1):
            t = step / steps
            gx = int(gx1 + t * dx)
            gy = int(gy1 + t * dy)

            if (gx, gy) in checked_positions:
                continue
            checked_positions.add((gx, gy))

            # Check if blocked
            if 0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows:
                cell = self.grid.grid[layer_idx][gy][gx]

                if cell.blocked and cell.net != net_id:
                    # Found a blocking obstacle
                    wx, wy = self.grid.grid_to_world(gx, gy)
                    obstacle = self._identify_obstacle(gx, gy, layer_idx, wx, wy)
                    if obstacle:
                        obstacles.append(obstacle)

                # Check congestion
                congestion = self.grid.get_congestion(gx, gy, layer_idx)
                if congestion > self.rules.congestion_threshold:
                    wx, wy = self.grid.grid_to_world(gx, gy)
                    zone = CongestionZone(
                        x=wx,
                        y=wy,
                        width=self.grid.resolution * self.grid.congestion_size,
                        height=self.grid.resolution * self.grid.congestion_size,
                        layer=layer_idx,
                        density=congestion,
                        competing_nets=self._count_nets_in_region(gx, gy, layer_idx),
                        available_channels=max(1, int((1.0 - congestion) * 3)),  # Rough estimate
                    )
                    congestion_zones.append(zone)

        return obstacles, congestion_zones

    def _identify_obstacle(
        self, gx: int, gy: int, layer_idx: int, wx: float, wy: float
    ) -> BlockingObstacle | None:
        """Identify what type of obstacle is at a grid position.

        Args:
            gx, gy: Grid coordinates
            layer_idx: Grid layer index
            wx, wy: World coordinates

        Returns:
            BlockingObstacle or None
        """
        cell = self.grid.grid[layer_idx][gy][gx]

        if not cell.blocked:
            return None

        # Try to identify obstacle type
        obs_type = ObstacleType.COMPONENT  # Default
        ref = ""
        net_name = ""

        # Check if it's a zone
        if cell.is_zone:
            obs_type = ObstacleType.ZONE

        # Check if it's near a pad by looking for component refs
        for (pad_ref, pad_pin), pad in self.pads.items():
            pad_gx, pad_gy = self.grid.world_to_grid(pad.x, pad.y)
            if abs(gx - pad_gx) <= 3 and abs(gy - pad_gy) <= 3:
                if pad.net != cell.net:
                    obs_type = ObstacleType.PAD
                    ref = pad_ref
                    net_name = pad.net_name
                    break

        # Check if it's a trace
        if cell.usage_count > 0 and obs_type == ObstacleType.COMPONENT:
            obs_type = ObstacleType.TRACE

        return BlockingObstacle(
            obstacle_type=obs_type,
            x=wx,
            y=wy,
            width=self.grid.resolution,
            height=self.grid.resolution,
            net=cell.net,
            net_name=net_name,
            ref=ref,
            layer=str(layer_idx),
        )

    def _calculate_mst_length(self, pads: list[Pad]) -> float:
        """Calculate minimum spanning tree length for pads.

        Args:
            pads: List of Pad objects

        Returns:
            Total manhattan distance of MST
        """
        if len(pads) < 2:
            return 0.0

        # Prim's algorithm for MST
        n = len(pads)
        connected: set[int] = {0}
        unconnected = set(range(1, n))
        total_length = 0.0

        while unconnected:
            best_dist = float("inf")
            best_edge: tuple[int, int] | None = None

            for i in connected:
                for j in unconnected:
                    dist = abs(pads[i].x - pads[j].x) + abs(pads[i].y - pads[j].y)
                    if dist < best_dist:
                        best_dist = dist
                        best_edge = (i, j)

            if best_edge:
                _, j = best_edge
                total_length += best_dist
                connected.add(j)
                unconnected.remove(j)
            else:
                break

        return total_length

    def _count_nets_in_region(self, gx: int, gy: int, layer_idx: int) -> int:
        """Count unique nets in a region around a grid cell.

        Args:
            gx, gy: Grid coordinates
            layer_idx: Grid layer index

        Returns:
            Number of unique nets
        """
        nets: set[int] = set()
        radius = self.grid.congestion_size // 2

        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                nx, ny = gx + dx, gy + dy
                if 0 <= nx < self.grid.cols and 0 <= ny < self.grid.rows:
                    cell = self.grid.grid[layer_idx][ny][nx]
                    if cell.net != 0:
                        nets.add(cell.net)

        return len(nets)

    def _find_congestion_zones(self) -> list[CongestionZone]:
        """Find all congested regions on the board.

        Returns:
            List of CongestionZone objects
        """
        zones: list[CongestionZone] = []

        for layer_idx in range(self.grid.num_layers):
            for cy in range(self.grid.congestion_rows):
                for cx in range(self.grid.congestion_cols):
                    count = self.grid.congestion[layer_idx][cy][cx]
                    max_cells = self.grid.congestion_size * self.grid.congestion_size
                    density = count / max_cells

                    if density > self.rules.congestion_threshold:
                        # Convert to world coordinates
                        gx = cx * self.grid.congestion_size
                        gy = cy * self.grid.congestion_size
                        wx, wy = self.grid.grid_to_world(gx, gy)

                        zone = CongestionZone(
                            x=wx,
                            y=wy,
                            width=self.grid.resolution * self.grid.congestion_size,
                            height=self.grid.resolution * self.grid.congestion_size,
                            layer=layer_idx,
                            density=density,
                            competing_nets=self._count_nets_in_region(gx, gy, layer_idx),
                            available_channels=max(1, int((1.0 - density) * 3)),
                        )
                        zones.append(zone)

        return zones

    def _calculate_layer_utilization(self) -> dict[str, float]:
        """Calculate utilization percentage for each layer.

        Returns:
            Dict mapping layer name to utilization (0.0 to 1.0)
        """
        utilization: dict[str, float] = {}
        total_cells = self.grid.rows * self.grid.cols

        for layer_def in self.grid.layer_stack.layers:
            layer_idx = layer_def.index
            blocked_count = 0

            for gy in range(self.grid.rows):
                for gx in range(self.grid.cols):
                    if self.grid.grid[layer_idx][gy][gx].blocked:
                        blocked_count += 1

            utilization[layer_def.name] = blocked_count / total_cells if total_cells > 0 else 0.0

        return utilization

    def _generate_net_suggestions(self, report: NetRoutabilityReport) -> list[str]:
        """Generate suggestions for a problem net.

        Args:
            report: Net routability report

        Returns:
            List of suggestion strings
        """
        suggestions: list[str] = []

        if report.severity == RoutingSeverity.CRITICAL:
            suggestions.append("Consider redesigning component placement")

        if len(report.blocking_obstacles) > 0:
            # Group obstacles by type
            pad_obstacles = [
                o for o in report.blocking_obstacles if o.obstacle_type == ObstacleType.PAD
            ]
            if pad_obstacles:
                refs = {o.ref for o in pad_obstacles if o.ref}
                if refs:
                    suggestions.append(f"Move component(s): {', '.join(sorted(refs))}")

        if any(z.is_bottleneck for z in report.congestion_zones):
            suggestions.append("Consider using via(s) to route on different layer")
            if self.grid.num_layers == 2:
                suggestions.append("Consider 4-layer stackup for 100% routability")

        return suggestions

    def _generate_alternatives(self, obstacles: list[BlockingObstacle]) -> list[RouteAlternative]:
        """Generate alternative routing options.

        Args:
            obstacles: Blocking obstacles found

        Returns:
            List of RouteAlternative options
        """
        alternatives: list[RouteAlternative] = []

        # Always suggest layer change if multi-layer
        if self.grid.num_layers > 1:
            alternatives.append(
                RouteAlternative(
                    description="Route on different layer",
                    via_count=2,
                    feasible=True,
                )
            )

        # Suggest going around obstacles
        if obstacles:
            alternatives.append(
                RouteAlternative(
                    description="Route around obstacles",
                    extra_length_mm=5.0,  # Rough estimate
                    feasible=True,
                )
            )

        return alternatives

    def _generate_recommendations(self, report: RoutabilityReport) -> list[str]:
        """Generate overall recommendations.

        Args:
            report: Complete routability report

        Returns:
            List of recommendation strings
        """
        recommendations: list[str] = []

        # Check success rate
        if report.estimated_success_rate < 0.9:
            recommendations.append(
                f"Expected {report.estimated_success_rate * 100:.0f}% routing success - "
                "consider component repositioning"
            )

        # Check for high layer utilization
        for layer_name, util in report.layer_utilization.items():
            if util > 0.6:
                recommendations.append(
                    f"Layer {layer_name} is {util * 100:.0f}% utilized - routing may be constrained"
                )

        # Check for many critical nets
        critical_count = len(
            [r for r in report.net_reports if r.severity == RoutingSeverity.CRITICAL]
        )
        if critical_count > 0:
            recommendations.append(f"{critical_count} net(s) have critical routing difficulty")

        # Suggest layer count if needed
        if self.grid.num_layers == 2 and report.estimated_success_rate < 0.85:
            recommendations.append("Consider 4-layer stackup for improved routability")

        if not recommendations:
            recommendations.append("Board appears routable with current design")

        return recommendations


def analyze_routing_failure(
    router: Autorouter,
    source_pad: Pad,
    target_pad: Pad,
    net_id: int,
) -> RoutingFailureDiagnostic:
    """Analyze why routing failed between two pads.

    This function is called after a routing attempt fails to provide
    detailed diagnostic information.

    Args:
        router: The Autorouter instance
        source_pad: Source pad
        target_pad: Target pad
        net_id: Net ID

    Returns:
        RoutingFailureDiagnostic with failure details
    """
    grid = router.grid
    net_name = router.net_names.get(net_id, f"Net_{net_id}")

    # Find component references for pads
    source_ref, source_pin = "", ""
    target_ref, target_pin = "", ""

    for (ref, pin), pad in router.pads.items():
        if pad.x == source_pad.x and pad.y == source_pad.y:
            source_ref, source_pin = ref, pin
        if pad.x == target_pad.x and pad.y == target_pad.y:
            target_ref, target_pin = ref, pin

    diagnostic = RoutingFailureDiagnostic(
        net=net_id,
        net_name=net_name,
        source_pad=(source_ref, source_pin),
        source_position=(source_pad.x, source_pad.y),
        target_pad=(target_ref, target_pin),
        target_position=(target_pad.x, target_pad.y),
        straight_line_distance=math.sqrt(
            (target_pad.x - source_pad.x) ** 2 + (target_pad.y - source_pad.y) ** 2
        ),
    )

    # Analyze path for obstacles
    gx1, gy1 = grid.world_to_grid(source_pad.x, source_pad.y)
    gx2, gy2 = grid.world_to_grid(target_pad.x, target_pad.y)

    dx = gx2 - gx1
    dy = gy2 - gy1
    steps = max(abs(dx), abs(dy), 1)

    layer_idx = grid.layer_to_index(source_pad.layer.value)

    for step in range(steps + 1):
        t = step / steps
        gx = int(gx1 + t * dx)
        gy = int(gy1 + t * dy)

        if 0 <= gx < grid.cols and 0 <= gy < grid.rows:
            cell = grid.grid[layer_idx][gy][gx]

            if cell.blocked and cell.net != net_id:
                wx, wy = grid.grid_to_world(gx, gy)

                if diagnostic.blocked_at_position is None:
                    diagnostic.blocked_at_position = (wx, wy)

                # Identify obstacle type
                obs_type = ObstacleType.COMPONENT
                ref = ""
                obs_net_name = ""

                if cell.is_zone:
                    obs_type = ObstacleType.ZONE
                elif cell.usage_count > 0:
                    obs_type = ObstacleType.TRACE

                # Find component reference if it's a pad
                for (pad_ref, pad_pin), pad in router.pads.items():
                    pad_gx, pad_gy = grid.world_to_grid(pad.x, pad.y)
                    if abs(gx - pad_gx) <= 3 and abs(gy - pad_gy) <= 3:
                        if pad.net == cell.net:
                            obs_type = ObstacleType.PAD
                            ref = pad_ref
                            obs_net_name = pad.net_name
                            break

                obstacle = BlockingObstacle(
                    obstacle_type=obs_type,
                    x=wx,
                    y=wy,
                    width=grid.resolution,
                    height=grid.resolution,
                    net=cell.net,
                    net_name=obs_net_name,
                    ref=ref,
                    layer=str(layer_idx),
                )
                diagnostic.blocking_obstacles.append(obstacle)

    # Generate suggestions based on obstacles
    if diagnostic.blocking_obstacles:
        pad_refs = {
            o.ref
            for o in diagnostic.blocking_obstacles
            if o.ref and o.obstacle_type == ObstacleType.PAD
        }
        if pad_refs:
            diagnostic.suggestions.append(
                f"Consider moving component(s): {', '.join(sorted(pad_refs))}"
            )

        if grid.num_layers > 1:
            diagnostic.suggestions.append("Try routing on a different layer using vias")
        if grid.num_layers == 2:
            diagnostic.suggestions.append("Consider 4-layer stackup for more routing options")

        trace_obstacles = [
            o for o in diagnostic.blocking_obstacles if o.obstacle_type == ObstacleType.TRACE
        ]
        if trace_obstacles:
            diagnostic.suggestions.append(
                "Try different net ordering (some routes may need to be ripped up)"
            )

    # Generate alternatives
    if grid.num_layers > 1:
        diagnostic.alternatives.append(
            RouteAlternative(
                description="Route on different layer",
                via_count=2,
                feasible=True,
            )
        )

    diagnostic.alternatives.append(
        RouteAlternative(
            description="Route around obstacles",
            extra_length_mm=diagnostic.straight_line_distance * 0.5,
            feasible=len(diagnostic.blocking_obstacles) < 5,
        )
    )

    return diagnostic
