"""
Intelligent failure recovery: Root cause analysis for routing and placement failures.

This module provides:
- FailureCause: Enum of root causes for routing/placement failures
- BlockingElement: Represents an element blocking a desired operation
- PathAttempt: Records a pathfinding attempt for analysis
- FailureAnalysis: Detailed analysis of why an operation failed
- RootCauseAnalyzer: Analyzes failures to determine root cause
- CongestionMap: Grid-based congestion tracking and heatmap

Example::

    from kicad_tools.router.failure_analysis import RootCauseAnalyzer

    analyzer = RootCauseAnalyzer()
    analysis = analyzer.analyze_routing_failure(
        pcb=pcb,
        net="CLK",
        start=(10.0, 20.0),
        end=(50.0, 60.0),
        attempts=routing_attempts
    )
    print(f"Root cause: {analysis.root_cause.value}")
    print(f"Confidence: {analysis.confidence:.0%}")
    print(f"Blocking elements: {len(analysis.blocking_elements)}")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .grid import RoutingGrid


class FailureCause(Enum):
    """Root causes for routing/placement failures."""

    CONGESTION = "congestion"  # Too many traces in area
    BLOCKED_PATH = "blocked_path"  # Component in the way
    CLEARANCE = "clearance"  # Can't meet DRC clearance
    LAYER_CONFLICT = "layer_conflict"  # Wrong layer or no layer available
    PIN_ACCESS = "pin_access"  # Can't reach pin (pad surrounded)
    VIA_BLOCKED = "via_blocked"  # Cannot place via for layer transition
    LENGTH_CONSTRAINT = "length_constraint"  # Can't meet length requirements
    DIFFERENTIAL_PAIR = "differential_pair"  # Can't maintain pair constraints
    KEEPOUT = "keepout"  # Path crosses keepout zone
    ROUTING_ORDER = "routing_order"  # Earlier net blocking this route
    UNKNOWN = "unknown"  # Unable to determine root cause

    @property
    def description(self) -> str:
        """Human-readable description of the failure cause."""
        descriptions = {
            "congestion": "Area too crowded with traces",
            "blocked_path": "Path blocked by component or trace",
            "clearance": "Cannot meet design rule clearance",
            "layer_conflict": "No available layer for routing",
            "pin_access": "Pin escape blocked by surrounding traces",
            "via_blocked": "Cannot place via for layer transition",
            "length_constraint": "Cannot meet length matching requirements",
            "differential_pair": "Cannot maintain differential pair constraints",
            "keepout": "Route path crosses keepout zone",
            "routing_order": "Blocked by earlier routed net",
            "unknown": "Unable to determine cause",
        }
        return descriptions.get(self.value, self.value)


@dataclass
class Rectangle:
    """Axis-aligned bounding box for failure analysis.

    Note: This is a local definition to avoid circular imports.
    Compatible with placement.conflict.Rectangle.
    """

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        """Width of the rectangle."""
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        """Height of the rectangle."""
        return self.max_y - self.min_y

    @property
    def center(self) -> tuple[float, float]:
        """Center point of the rectangle."""
        return ((self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2)

    @property
    def area(self) -> float:
        """Area of the rectangle."""
        return self.width * self.height

    def contains(self, x: float, y: float) -> bool:
        """Check if a point is inside the rectangle."""
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    def intersects(self, other: Rectangle) -> bool:
        """Check if this rectangle intersects with another."""
        return not (
            self.max_x < other.min_x
            or self.min_x > other.max_x
            or self.max_y < other.min_y
            or self.min_y > other.max_y
        )

    def expand(self, margin: float) -> Rectangle:
        """Return a new rectangle expanded by margin on all sides."""
        return Rectangle(
            self.min_x - margin,
            self.min_y - margin,
            self.max_x + margin,
            self.max_y + margin,
        )

    def __repr__(self) -> str:
        return f"Rectangle({self.min_x:.2f}, {self.min_y:.2f}, {self.max_x:.2f}, {self.max_y:.2f})"


@dataclass
class BlockingElement:
    """Something blocking the desired operation."""

    type: str  # "component", "trace", "via", "zone", "keepout"
    ref: str | None  # Component ref if applicable
    net: str | None  # Net name if applicable
    bounds: Rectangle
    movable: bool  # Can this be moved to resolve the issue?
    layer: int | None = None  # Layer index if applicable

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.type,
            "ref": self.ref,
            "net": self.net,
            "bounds": {
                "min_x": self.bounds.min_x,
                "min_y": self.bounds.min_y,
                "max_x": self.bounds.max_x,
                "max_y": self.bounds.max_y,
            },
            "movable": self.movable,
            "layer": self.layer,
        }

    def __repr__(self) -> str:
        if self.ref:
            return f"BlockingElement({self.type}, ref={self.ref})"
        elif self.net:
            return f"BlockingElement({self.type}, net={self.net})"
        return f"BlockingElement({self.type})"


@dataclass
class PathAttempt:
    """Records a pathfinding attempt for analysis."""

    start: tuple[float, float]
    end: tuple[float, float]
    layer: int
    success: bool
    path: list[tuple[float, float]] | None = None
    blocked_at: tuple[float, float] | None = None
    explored_cells: int = 0
    cost: float = float("inf")

    @property
    def length(self) -> float:
        """Calculate path length if successful."""
        if not self.path or len(self.path) < 2:
            return 0.0
        total = 0.0
        for i in range(len(self.path) - 1):
            dx = self.path[i + 1][0] - self.path[i][0]
            dy = self.path[i + 1][1] - self.path[i][1]
            total += math.sqrt(dx * dx + dy * dy)
        return total

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "start": self.start,
            "end": self.end,
            "layer": self.layer,
            "success": self.success,
            "blocked_at": self.blocked_at,
            "explored_cells": self.explored_cells,
            "cost": self.cost if self.cost != float("inf") else None,
            "path_length": self.length if self.path else None,
        }


@dataclass
class PadAccessBlocker:
    """Information about what's blocking access to a pad.

    Used to provide detailed diagnostics when routing fails due to
    clearance zones blocking pad entry/exit points.
    """

    pad_ref: str  # e.g. "U1.13"
    blocking_net: int  # Net ID of the blocking net
    blocking_net_name: str  # e.g. "SC_POS_PLUS"
    blocking_type: str  # "trace", "via", "pad"
    distance: float  # Distance from pad center to blocking element in mm
    suggested_clearance: float  # Clearance that would allow access in mm

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "pad_ref": self.pad_ref,
            "blocking_net": self.blocking_net,
            "blocking_net_name": self.blocking_net_name,
            "blocking_type": self.blocking_type,
            "distance": self.distance,
            "suggested_clearance": self.suggested_clearance,
        }

    def __str__(self) -> str:
        return (
            f"Pad {self.pad_ref}: blocked by clearance from Net {self.blocking_net} "
            f'"{self.blocking_net_name}" ({self.blocking_type} at {self.distance:.2f}mm distance)'
        )


@dataclass
class ActionableSuggestion:
    """A specific, actionable suggestion for resolving a routing failure.

    Provides detailed recommendations with specific parameters (direction,
    distance, component names) rather than generic advice.

    Example::

        suggestion = ActionableSuggestion(
            category="placement",
            priority=1,
            summary="Move U3 0.5mm east to create routing channel",
            details="The +3.3V trace blocks MCLK_DAC near U3 pin 15",
            affected_component="U3",
            suggested_action="move",
            direction="east",
            distance_mm=0.5,
        )
    """

    category: str  # "routing_order", "placement", "design_rules", "layer_stack"
    priority: int  # 1 = most actionable, higher = less specific
    summary: str  # One-line summary
    details: str = ""  # Additional context
    affected_component: str | None = None  # Component ref if applicable
    affected_net: str | None = None  # Net name if applicable
    suggested_action: str | None = None  # "move", "reroute", "reduce", "add_layer"
    direction: str | None = None  # "north", "south", "east", "west" for moves
    distance_mm: float | None = None  # Distance for moves
    parameter_name: str | None = None  # For design rule changes
    current_value: float | None = None  # Current parameter value
    suggested_value: float | None = None  # Suggested parameter value

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {
            "category": self.category,
            "priority": self.priority,
            "summary": self.summary,
            "details": self.details,
        }
        if self.affected_component:
            result["affected_component"] = self.affected_component
        if self.affected_net:
            result["affected_net"] = self.affected_net
        if self.suggested_action:
            result["suggested_action"] = self.suggested_action
        if self.direction:
            result["direction"] = self.direction
        if self.distance_mm is not None:
            result["distance_mm"] = self.distance_mm
        if self.parameter_name:
            result["parameter_name"] = self.parameter_name
        if self.current_value is not None:
            result["current_value"] = self.current_value
        if self.suggested_value is not None:
            result["suggested_value"] = self.suggested_value
        return result

    def __str__(self) -> str:
        return self.summary


@dataclass
class FailureAnalysis:
    """Detailed analysis of why an operation failed.

    Provides comprehensive diagnostics for routing failures including:
    - Root cause classification with confidence score
    - Blocking element identification (components, traces, vias)
    - Blocked area location and dimensions
    - Actionable suggestions with specific remediation steps

    Example output when formatted:

        MCLK_DAC: Blocked by +3.3V trace near U3 pin 15
                  Suggestion: Try routing MCLK_DAC before +3.3V, or move U3 0.5mm east
    """

    root_cause: FailureCause
    confidence: float  # 0.0-1.0, confidence in the diagnosis

    # Location info
    failure_location: tuple[float, float]
    failure_area: Rectangle

    # What's blocking
    blocking_elements: list[BlockingElement] = field(default_factory=list)

    # Pad access blockers (for PIN_ACCESS failures)
    pad_access_blockers: list[PadAccessBlocker] = field(default_factory=list)

    # Blocking net info (for ROUTING_ORDER failures)
    blocking_net_name: str | None = None

    # Nearby component info for context
    nearby_component: str | None = None
    nearby_pin: str | None = None

    # Attempted solutions
    attempted_paths: int = 0
    best_attempt: PathAttempt | None = None

    # Metrics
    congestion_score: float = 0.0  # 0-1, how congested the area is
    clearance_margin: float = float("inf")  # How close to DRC limits

    # Suggestions - string summaries for display
    suggestions: list[str] = field(default_factory=list)

    # Actionable suggestions - structured for tooling
    actionable_suggestions: list[ActionableSuggestion] = field(default_factory=list)

    # Net that failed (for strategy generation)
    net: str | None = None

    @property
    def has_movable_blockers(self) -> bool:
        """Check if any blocking elements can be moved."""
        return any(el.movable for el in self.blocking_elements)

    @property
    def has_reroutable_nets(self) -> bool:
        """Check if blocking elements include traces that could be rerouted."""
        return any(el.type == "trace" and el.net != self.net for el in self.blocking_elements)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "root_cause": self.root_cause.value,
            "root_cause_description": self.root_cause.description,
            "confidence": self.confidence,
            "failure_location": list(self.failure_location),
            "failure_area": {
                "min_x": self.failure_area.min_x,
                "min_y": self.failure_area.min_y,
                "max_x": self.failure_area.max_x,
                "max_y": self.failure_area.max_y,
                "width": self.failure_area.width,
                "height": self.failure_area.height,
            },
            "blocking_elements": [e.to_dict() for e in self.blocking_elements],
            "pad_access_blockers": [b.to_dict() for b in self.pad_access_blockers],
            "blocking_net_name": self.blocking_net_name,
            "nearby_component": self.nearby_component,
            "nearby_pin": self.nearby_pin,
            "attempted_paths": self.attempted_paths,
            "best_attempt": self.best_attempt.to_dict() if self.best_attempt else None,
            "congestion_score": self.congestion_score,
            "clearance_margin": (
                self.clearance_margin if self.clearance_margin != float("inf") else None
            ),
            "suggestions": self.suggestions,
            "actionable_suggestions": [s.to_dict() for s in self.actionable_suggestions],
            "net": self.net,
        }

    def format_summary(self, net_name: str) -> str:
        """Format a user-friendly summary of the failure.

        Args:
            net_name: Name of the net that failed to route

        Returns:
            Multi-line formatted string suitable for terminal output
        """
        lines = []

        # Build the main failure description
        if self.root_cause == FailureCause.ROUTING_ORDER and self.blocking_net_name:
            desc = f"Blocked by {self.blocking_net_name} trace"
            if self.nearby_component and self.nearby_pin:
                desc += f" near {self.nearby_component} pin {self.nearby_pin}"
            elif self.nearby_component:
                desc += f" near {self.nearby_component}"
        elif self.root_cause == FailureCause.PIN_ACCESS:
            if self.nearby_component and self.nearby_pin:
                desc = f"Pin escape blocked - {self.nearby_component} pin {self.nearby_pin} surrounded by routed traces"
            else:
                desc = "Pin escape blocked - pad surrounded by routed traces"
        elif self.root_cause == FailureCause.CONGESTION:
            area_w = self.failure_area.width
            area_h = self.failure_area.height
            center = self.failure_area.center
            desc = f"No path exists on available layers"
            if area_w > 0 and area_h > 0:
                desc += f"\n            Blocked area: {area_w:.0f}x{area_h:.0f}mm around ({center[0]:.1f}, {center[1]:.1f})"
        elif self.root_cause == FailureCause.VIA_BLOCKED:
            desc = "Cannot place via for layer transition - area blocked"
        elif self.root_cause == FailureCause.CLEARANCE:
            desc = "Cannot meet clearance requirements"
            if self.clearance_margin != float("inf"):
                desc += f" (margin: {self.clearance_margin:.2f}mm)"
        else:
            desc = self.root_cause.description

        lines.append(f"  {net_name}: {desc}")

        # Add the primary suggestion
        if self.suggestions:
            lines.append(f"            Suggestion: {self.suggestions[0]}")

        return "\n".join(lines)

    def __str__(self) -> str:
        return (
            f"FailureAnalysis(cause={self.root_cause.value}, "
            f"confidence={self.confidence:.0%}, "
            f"blockers={len(self.blocking_elements)}, "
            f"congestion={self.congestion_score:.2f})"
        )


class CongestionMap:
    """Grid-based congestion tracking.

    Builds a heatmap of PCB congestion from components, traces, and vias.
    Used to identify routing hotspots and bottlenecks.

    Example::

        from kicad_tools.router.failure_analysis import CongestionMap

        cmap = CongestionMap(grid, cell_size=1.0)
        congestion = cmap.get_congestion(area)
        hotspots = cmap.find_congestion_hotspots(threshold=0.7)
    """

    def __init__(
        self,
        grid: RoutingGrid,
        cell_size: float = 1.0,
        component_weight: float = 1.0,
        trace_weight: float = 0.5,
        via_weight: float = 0.3,
    ):
        """Initialize congestion map.

        Args:
            grid: The routing grid to analyze
            cell_size: Size of each congestion cell in mm
            component_weight: Weight for component footprints
            trace_weight: Weight for routed traces
            via_weight: Weight for vias
        """
        self.grid = grid
        self.cell_size = cell_size
        self.component_weight = component_weight
        self.trace_weight = trace_weight
        self.via_weight = via_weight

        # Calculate grid dimensions
        self.origin_x = grid.origin_x
        self.origin_y = grid.origin_y
        board_width = grid.cols * grid.resolution
        board_height = grid.rows * grid.resolution

        self.cols = max(1, int(board_width / cell_size) + 1)
        self.rows = max(1, int(board_height / cell_size) + 1)

        # Build the congestion grid
        self._grid: np.ndarray = self._build_grid()

    def _build_grid(self) -> np.ndarray:
        """Build congestion grid from routing grid."""
        congestion = np.zeros((self.rows, self.cols), dtype=np.float32)

        # Iterate through all layers and cells
        for layer_idx in range(self.grid.num_layers):
            for gy in range(self.grid.rows):
                for gx in range(self.grid.cols):
                    cell = self.grid.grid[layer_idx][gy][gx]

                    if not cell.blocked and cell.usage_count == 0:
                        continue

                    # Convert grid coords to world coords
                    wx, wy = self.grid.grid_to_world(gx, gy)

                    # Convert to congestion grid coords
                    cx = int((wx - self.origin_x) / self.cell_size)
                    cy = int((wy - self.origin_y) / self.cell_size)

                    if 0 <= cx < self.cols and 0 <= cy < self.rows:
                        # Determine weight based on cell state
                        if cell.is_zone:
                            # Zones have less impact on routing
                            weight = self.trace_weight * 0.5
                        elif cell.blocked:
                            # Components/pads
                            weight = self.component_weight
                        elif cell.usage_count > 0:
                            # Routed traces
                            weight = self.trace_weight * min(cell.usage_count, 3)
                        else:
                            weight = 0.0

                        congestion[cy, cx] += weight

        # Normalize to 0-1 range
        max_val = congestion.max()
        if max_val > 0:
            congestion = congestion / max_val

        return congestion

    def get_congestion(self, area: Rectangle) -> float:
        """Get average congestion in an area.

        Args:
            area: Rectangle defining the area to check

        Returns:
            Average congestion score (0.0-1.0)
        """
        # Convert area bounds to grid indices
        min_cx = max(0, int((area.min_x - self.origin_x) / self.cell_size))
        max_cx = min(self.cols - 1, int((area.max_x - self.origin_x) / self.cell_size))
        min_cy = max(0, int((area.min_y - self.origin_y) / self.cell_size))
        max_cy = min(self.rows - 1, int((area.max_y - self.origin_y) / self.cell_size))

        if min_cx > max_cx or min_cy > max_cy:
            return 0.0

        # Extract the region and compute mean
        region = self._grid[min_cy : max_cy + 1, min_cx : max_cx + 1]
        return float(np.mean(region))

    def get_congestion_at(self, x: float, y: float) -> float:
        """Get congestion at a specific point.

        Args:
            x, y: World coordinates

        Returns:
            Congestion score (0.0-1.0)
        """
        cx = int((x - self.origin_x) / self.cell_size)
        cy = int((y - self.origin_y) / self.cell_size)

        if 0 <= cx < self.cols and 0 <= cy < self.rows:
            return float(self._grid[cy, cx])
        return 0.0

    def find_congestion_hotspots(self, threshold: float = 0.7) -> list[Rectangle]:
        """Find areas with congestion above threshold.

        Uses connected component analysis to find contiguous
        regions of high congestion.

        Args:
            threshold: Minimum congestion score to consider (0.0-1.0)

        Returns:
            List of Rectangle areas representing hotspots
        """
        hotspots: list[Rectangle] = []

        # Create binary mask
        mask = self._grid > threshold

        # Find connected components using simple flood fill
        visited = np.zeros_like(mask, dtype=bool)

        for start_y in range(self.rows):
            for start_x in range(self.cols):
                if mask[start_y, start_x] and not visited[start_y, start_x]:
                    # Flood fill to find connected region
                    min_x, max_x = start_x, start_x
                    min_y, max_y = start_y, start_y

                    stack = [(start_x, start_y)]
                    while stack:
                        cx, cy = stack.pop()
                        if not (0 <= cx < self.cols and 0 <= cy < self.rows):
                            continue
                        if visited[cy, cx] or not mask[cy, cx]:
                            continue

                        visited[cy, cx] = True
                        min_x = min(min_x, cx)
                        max_x = max(max_x, cx)
                        min_y = min(min_y, cy)
                        max_y = max(max_y, cy)

                        # Add neighbors
                        stack.extend([(cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)])

                    # Convert grid coords to world coords
                    world_min_x = self.origin_x + min_x * self.cell_size
                    world_max_x = self.origin_x + (max_x + 1) * self.cell_size
                    world_min_y = self.origin_y + min_y * self.cell_size
                    world_max_y = self.origin_y + (max_y + 1) * self.cell_size

                    hotspots.append(Rectangle(world_min_x, world_min_y, world_max_x, world_max_y))

        return hotspots

    @property
    def shape(self) -> tuple[int, int]:
        """Return grid shape (rows, cols)."""
        return (self.rows, self.cols)

    def to_array(self) -> np.ndarray:
        """Return raw congestion array for visualization."""
        return self._grid.copy()


class RootCauseAnalyzer:
    """Analyzes failures to determine root cause.

    Provides detailed analysis of why routing or placement operations
    failed, with confidence scores and actionable suggestions.

    Example::

        from kicad_tools.router.failure_analysis import RootCauseAnalyzer

        analyzer = RootCauseAnalyzer()

        # Analyze routing failure
        analysis = analyzer.analyze_routing_failure(
            grid=router.grid,
            start=(10.0, 20.0),
            end=(50.0, 60.0),
            net="CLK",
            attempts=[attempt1, attempt2]
        )

        print(f"Root cause: {analysis.root_cause.value}")
        for suggestion in analysis.suggestions:
            print(f"  - {suggestion}")
    """

    # Thresholds for determining root causes
    CONGESTION_THRESHOLD = 0.7  # Above this = congested
    HIGH_CONGESTION_THRESHOLD = 0.9  # Above this = severely congested
    CLEARANCE_MARGIN_THRESHOLD = 0.05  # Below this mm = clearance issue

    def __init__(
        self,
        congestion_threshold: float = 0.7,
        clearance_margin_threshold: float = 0.05,
    ):
        """Initialize analyzer.

        Args:
            congestion_threshold: Congestion level to consider problematic
            clearance_margin_threshold: Clearance margin below which to flag
        """
        self.congestion_threshold = congestion_threshold
        self.clearance_margin_threshold = clearance_margin_threshold

    def analyze_routing_failure(
        self,
        grid: RoutingGrid,
        start: tuple[float, float],
        end: tuple[float, float],
        net: str,
        attempts: list[PathAttempt] | None = None,
        layer: int = 0,
        source_pad_ref: str | None = None,
        source_pin: str | None = None,
        target_pad_ref: str | None = None,
        target_pin: str | None = None,
    ) -> FailureAnalysis:
        """Analyze why routing failed between two points.

        Args:
            grid: The routing grid
            start: Start point (x, y) in mm
            end: End point (x, y) in mm
            net: Net name being routed
            attempts: List of PathAttempt records (optional)
            layer: Layer index for routing
            source_pad_ref: Component reference for source pad (optional)
            source_pin: Pin number/name for source pad (optional)
            target_pad_ref: Component reference for target pad (optional)
            target_pin: Pin number/name for target pad (optional)

        Returns:
            FailureAnalysis with root cause and suggestions
        """
        attempts = attempts or []

        # Build congestion map
        cmap = CongestionMap(grid)

        # Compute routing corridor
        corridor = self._compute_corridor(start, end, margin=2.0)

        # Find blocking elements
        blocking = self._find_blocking_elements(grid, corridor, net, layer)

        # Analyze congestion in corridor
        congestion_score = cmap.get_congestion(corridor)

        # Find best attempt
        best_attempt = self._find_best_attempt(attempts)

        # Determine failure location
        failure_location = self._find_failure_point(start, end, attempts)

        # Compute clearance margin
        clearance_margin = self._compute_clearance_margin(grid, corridor, layer)

        # Find the primary blocking net (for ROUTING_ORDER cause)
        blocking_net_name = None
        nearby_component = None
        nearby_pin = None
        trace_blockers = [b for b in blocking if b.type == "trace" and b.net]
        if trace_blockers:
            # Get the most significant blocking net
            blocking_net_name = trace_blockers[0].net

        # Find nearby component for context
        component_blockers = [b for b in blocking if b.ref]
        if component_blockers:
            nearby_component = component_blockers[0].ref

        # Use source/target info for context if blocking is near them
        if source_pad_ref and self._is_near_point(blocking, start):
            nearby_component = source_pad_ref
            nearby_pin = source_pin
        elif target_pad_ref and self._is_near_point(blocking, end):
            nearby_component = target_pad_ref
            nearby_pin = target_pin

        # Determine root cause with enhanced detection
        cause, confidence = self._determine_root_cause(
            grid,
            corridor,
            blocking,
            congestion_score,
            clearance_margin,
            layer,
        )

        # Check if this is a routing order issue
        if cause == FailureCause.BLOCKED_PATH and trace_blockers:
            cause = FailureCause.ROUTING_ORDER
            confidence = min(confidence + 0.05, 0.95)

        # Generate suggestions with context
        suggestions, actionable = self._generate_suggestions(
            cause,
            blocking,
            congestion_score,
            grid.num_layers,
            corridor=corridor,
            blocking_net=blocking_net_name,
            net_name=net,
        )

        return FailureAnalysis(
            root_cause=cause,
            confidence=confidence,
            failure_location=failure_location,
            failure_area=corridor,
            blocking_elements=blocking,
            blocking_net_name=blocking_net_name,
            nearby_component=nearby_component,
            nearby_pin=nearby_pin,
            attempted_paths=len(attempts),
            best_attempt=best_attempt,
            congestion_score=congestion_score,
            clearance_margin=clearance_margin,
            suggestions=suggestions,
            actionable_suggestions=actionable,
            net=net,
        )

    def _is_near_point(
        self,
        blocking: list[BlockingElement],
        point: tuple[float, float],
        threshold: float = 3.0,
    ) -> bool:
        """Check if any blocking element is near a point.

        Args:
            blocking: List of blocking elements
            point: Point to check (x, y)
            threshold: Distance threshold in mm

        Returns:
            True if any blocker is within threshold of the point
        """
        for b in blocking:
            center = b.bounds.center
            dist = math.sqrt((center[0] - point[0]) ** 2 + (center[1] - point[1]) ** 2)
            if dist < threshold:
                return True
        return False

    def analyze_placement_failure(
        self,
        grid: RoutingGrid,
        ref: str,
        target_pos: tuple[float, float],
        component_bounds: Rectangle,
    ) -> FailureAnalysis:
        """Analyze why placement at target position fails.

        Args:
            grid: The routing grid
            ref: Component reference designator
            target_pos: Target position (x, y) in mm
            component_bounds: Bounding box of the component

        Returns:
            FailureAnalysis with root cause and suggestions
        """
        # Translate bounds to target position
        current_center = component_bounds.center
        dx = target_pos[0] - current_center[0]
        dy = target_pos[1] - current_center[1]

        target_bounds = Rectangle(
            component_bounds.min_x + dx,
            component_bounds.min_y + dy,
            component_bounds.max_x + dx,
            component_bounds.max_y + dy,
        )

        # Build congestion map
        cmap = CongestionMap(grid)

        # Find conflicts at target location
        conflicts = self._find_placement_conflicts(grid, ref, target_bounds)

        # Determine cause
        if any(c.type == "keepout" for c in conflicts):
            cause = FailureCause.KEEPOUT
            confidence = 0.95
        elif any(c.type == "component" for c in conflicts):
            cause = FailureCause.BLOCKED_PATH
            confidence = 0.9
        else:
            cause = FailureCause.CLEARANCE
            confidence = 0.8

        congestion_score = cmap.get_congestion(target_bounds)

        suggestions = []
        if conflicts:
            refs = {c.ref for c in conflicts if c.ref}
            if refs:
                suggestions.append(f"Move conflicting component(s): {', '.join(refs)}")
        if congestion_score > self.congestion_threshold:
            suggestions.append("Area is congested; consider placing elsewhere")

        return FailureAnalysis(
            root_cause=cause,
            confidence=confidence,
            failure_location=target_pos,
            failure_area=target_bounds,
            blocking_elements=conflicts,
            congestion_score=congestion_score,
            suggestions=suggestions,
        )

    def _compute_corridor(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        margin: float = 2.0,
    ) -> Rectangle:
        """Compute the routing corridor between two points.

        Args:
            start: Start point (x, y)
            end: End point (x, y)
            margin: Margin around the direct path in mm

        Returns:
            Rectangle representing the corridor
        """
        min_x = min(start[0], end[0]) - margin
        max_x = max(start[0], end[0]) + margin
        min_y = min(start[1], end[1]) - margin
        max_y = max(start[1], end[1]) + margin

        return Rectangle(min_x, min_y, max_x, max_y)

    def _find_blocking_elements(
        self,
        grid: RoutingGrid,
        corridor: Rectangle,
        net: str,
        layer: int,
    ) -> list[BlockingElement]:
        """Find elements blocking the routing corridor.

        Args:
            grid: Routing grid
            corridor: Area to check
            net: Net being routed (to exclude from blockers)
            layer: Layer index

        Returns:
            List of BlockingElement objects
        """
        blocking: list[BlockingElement] = []
        seen_refs: set[str] = set()

        # Convert corridor to grid coordinates
        min_gx, min_gy = grid.world_to_grid(corridor.min_x, corridor.min_y)
        max_gx, max_gy = grid.world_to_grid(corridor.max_x, corridor.max_y)

        # Clamp to grid bounds
        min_gx = max(0, min_gx)
        min_gy = max(0, min_gy)
        max_gx = min(grid.cols - 1, max_gx)
        max_gy = min(grid.rows - 1, max_gy)

        for gy in range(min_gy, max_gy + 1):
            for gx in range(min_gx, max_gx + 1):
                cell = grid.grid[layer][gy][gx]

                if not cell.blocked:
                    continue

                # Get world coordinates
                wx, wy = grid.grid_to_world(gx, gy)
                cell_bounds = Rectangle(
                    wx - grid.resolution / 2,
                    wy - grid.resolution / 2,
                    wx + grid.resolution / 2,
                    wy + grid.resolution / 2,
                )

                # Determine element type
                if cell.is_zone:
                    element_type = "zone"
                    ref = None
                    movable = False
                elif cell.usage_count > 0:
                    element_type = "trace"
                    ref = None
                    movable = True  # Traces can be ripped up
                else:
                    element_type = "component"
                    ref = cell.ref if hasattr(cell, "ref") else None
                    movable = True

                # Skip if same net
                cell_net = grid.net_names.get(cell.net, "") if hasattr(grid, "net_names") else ""
                if cell_net == net:
                    continue

                # Avoid duplicate component entries
                if ref and ref in seen_refs:
                    continue
                if ref:
                    seen_refs.add(ref)

                blocking.append(
                    BlockingElement(
                        type=element_type,
                        ref=ref,
                        net=cell_net if cell_net else None,
                        bounds=cell_bounds,
                        movable=movable,
                        layer=layer,
                    )
                )

        return blocking

    def _find_placement_conflicts(
        self,
        grid: RoutingGrid,
        ref: str,
        target_bounds: Rectangle,
    ) -> list[BlockingElement]:
        """Find conflicts at a target placement location.

        Args:
            grid: Routing grid
            ref: Reference of component being placed
            target_bounds: Target bounding box

        Returns:
            List of conflicting BlockingElement objects
        """
        conflicts: list[BlockingElement] = []

        # Check all layers for conflicts
        for layer_idx in range(grid.num_layers):
            min_gx, min_gy = grid.world_to_grid(target_bounds.min_x, target_bounds.min_y)
            max_gx, max_gy = grid.world_to_grid(target_bounds.max_x, target_bounds.max_y)

            min_gx = max(0, min_gx)
            min_gy = max(0, min_gy)
            max_gx = min(grid.cols - 1, max_gx)
            max_gy = min(grid.rows - 1, max_gy)

            for gy in range(min_gy, max_gy + 1):
                for gx in range(min_gx, max_gx + 1):
                    cell = grid.grid[layer_idx][gy][gx]

                    if not cell.blocked:
                        continue

                    cell_ref = cell.ref if hasattr(cell, "ref") else None
                    if cell_ref == ref:
                        continue  # Don't conflict with self

                    wx, wy = grid.grid_to_world(gx, gy)
                    cell_bounds = Rectangle(
                        wx - grid.resolution / 2,
                        wy - grid.resolution / 2,
                        wx + grid.resolution / 2,
                        wy + grid.resolution / 2,
                    )

                    element_type = "keepout" if cell.is_zone else "component"

                    conflicts.append(
                        BlockingElement(
                            type=element_type,
                            ref=cell_ref,
                            net=None,
                            bounds=cell_bounds,
                            movable=element_type == "component",
                            layer=layer_idx,
                        )
                    )

        return conflicts

    def _find_failure_point(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        attempts: list[PathAttempt],
    ) -> tuple[float, float]:
        """Find the point where routing failed.

        Args:
            start: Start point
            end: End point
            attempts: List of path attempts

        Returns:
            Point where failure was detected
        """
        # Use blocked_at from attempts if available
        for attempt in attempts:
            if attempt.blocked_at:
                return attempt.blocked_at

        # Use end of partial path if available
        for attempt in attempts:
            if attempt.path and len(attempt.path) > 1:
                return attempt.path[-1]

        # Default to midpoint
        return ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)

    def _find_best_attempt(self, attempts: list[PathAttempt]) -> PathAttempt | None:
        """Find the best path attempt (closest to success).

        Args:
            attempts: List of path attempts

        Returns:
            Best attempt or None
        """
        if not attempts:
            return None

        # Prefer successful attempts
        successful = [a for a in attempts if a.success]
        if successful:
            return min(successful, key=lambda a: a.cost)

        # Otherwise prefer the one that got furthest
        return max(attempts, key=lambda a: len(a.path or []))

    def _compute_clearance_margin(
        self,
        grid: RoutingGrid,
        corridor: Rectangle,
        layer: int,
    ) -> float:
        """Compute minimum clearance margin in the corridor.

        Args:
            grid: Routing grid
            corridor: Area to check
            layer: Layer index

        Returns:
            Minimum clearance margin found (mm)
        """
        min_clearance = float("inf")
        resolution = grid.resolution

        min_gx, min_gy = grid.world_to_grid(corridor.min_x, corridor.min_y)
        max_gx, max_gy = grid.world_to_grid(corridor.max_x, corridor.max_y)

        min_gx = max(0, min_gx)
        min_gy = max(0, min_gy)
        max_gx = min(grid.cols - 1, max_gx)
        max_gy = min(grid.rows - 1, max_gy)

        for gy in range(min_gy, max_gy + 1):
            for gx in range(min_gx, max_gx + 1):
                cell = grid.grid[layer][gy][gx]
                if not cell.blocked:
                    # Check distance to nearest blocked cell
                    for dy in range(-2, 3):
                        for dx in range(-2, 3):
                            nx, ny = gx + dx, gy + dy
                            if 0 <= nx < grid.cols and 0 <= ny < grid.rows:
                                if grid.grid[layer][ny][nx].blocked:
                                    dist = math.sqrt(dx * dx + dy * dy) * resolution
                                    min_clearance = min(min_clearance, dist)

        return min_clearance

    def _determine_root_cause(
        self,
        grid: RoutingGrid,
        corridor: Rectangle,
        blocking: list[BlockingElement],
        congestion_score: float,
        clearance_margin: float,
        layer: int,
    ) -> tuple[FailureCause, float]:
        """Determine the root cause of the failure.

        Args:
            grid: Routing grid
            corridor: Routing corridor
            blocking: List of blocking elements
            congestion_score: Congestion score (0-1)
            clearance_margin: Minimum clearance margin
            layer: Layer index

        Returns:
            Tuple of (FailureCause, confidence)
        """
        # Check for keepout zones
        keepout_blockers = [b for b in blocking if b.type == "keepout"]
        if keepout_blockers:
            return (FailureCause.KEEPOUT, 0.95)

        # Check for severe congestion
        if congestion_score > self.HIGH_CONGESTION_THRESHOLD:
            return (FailureCause.CONGESTION, 0.9)

        # Check for blocking components
        component_blockers = [b for b in blocking if b.type == "component"]
        if component_blockers:
            return (FailureCause.BLOCKED_PATH, 0.85)

        # Check for moderate congestion
        if congestion_score > self.congestion_threshold:
            return (FailureCause.CONGESTION, 0.75)

        # Check for clearance issues
        if clearance_margin < self.clearance_margin_threshold:
            return (FailureCause.CLEARANCE, 0.8)

        # Check for layer conflicts
        if grid.num_layers == 1:
            return (FailureCause.LAYER_CONFLICT, 0.7)

        # Default to unknown with low confidence
        return (FailureCause.UNKNOWN, 0.5)

    def _generate_suggestions(
        self,
        cause: FailureCause,
        blocking: list[BlockingElement],
        congestion_score: float,
        num_layers: int,
        corridor: Rectangle | None = None,
        blocking_net: str | None = None,
        net_name: str | None = None,
    ) -> tuple[list[str], list[ActionableSuggestion]]:
        """Generate actionable suggestions based on root cause.

        Args:
            cause: Determined root cause
            blocking: List of blocking elements
            congestion_score: Congestion score
            num_layers: Number of available layers
            corridor: The routing corridor (optional)
            blocking_net: Name of the net that's blocking (optional)
            net_name: Name of the net that failed to route (optional)

        Returns:
            Tuple of (list of suggestion strings, list of ActionableSuggestion)
        """
        suggestions: list[str] = []
        actionable: list[ActionableSuggestion] = []

        if cause == FailureCause.CONGESTION:
            if num_layers < 4:
                suggestions.append(f"Consider 6-layer stackup or placement adjustment")
                actionable.append(
                    ActionableSuggestion(
                        category="layer_stack",
                        priority=1,
                        summary="Add routing layers for more capacity",
                        details=f"Current {num_layers}-layer stackup is saturated",
                        suggested_action="add_layer",
                        parameter_name="layer_count",
                        current_value=float(num_layers),
                        suggested_value=6.0 if num_layers <= 2 else float(num_layers + 2),
                    )
                )
            else:
                suggestions.append("Increase board area or reduce component density")
                actionable.append(
                    ActionableSuggestion(
                        category="placement",
                        priority=2,
                        summary="Spread components to reduce congestion",
                        details="Routing channels are saturated",
                        suggested_action="spread",
                    )
                )

        elif cause == FailureCause.ROUTING_ORDER and blocking_net:
            suggestions.append(
                f"Try routing {net_name or 'this net'} before {blocking_net}"
            )
            actionable.append(
                ActionableSuggestion(
                    category="routing_order",
                    priority=1,
                    summary=f"Route {net_name or 'this net'} before {blocking_net}",
                    details=f"{blocking_net} trace is blocking the path",
                    affected_net=blocking_net,
                    suggested_action="reorder",
                )
            )

        elif cause == FailureCause.BLOCKED_PATH:
            movable_refs = {b.ref for b in blocking if b.movable and b.ref}
            blocking_nets = {b.net for b in blocking if b.net}

            if movable_refs:
                ref_list = ", ".join(sorted(movable_refs)[:3])
                if len(movable_refs) > 3:
                    ref_list += f" (+{len(movable_refs) - 3} more)"

                # Suggest moving components with direction hint
                if corridor:
                    center = corridor.center
                    direction = self._suggest_move_direction(blocking, center)
                    if direction:
                        suggestions.append(
                            f"Move {ref_list} {direction} to create routing channel"
                        )
                        for ref in sorted(movable_refs)[:2]:
                            actionable.append(
                                ActionableSuggestion(
                                    category="placement",
                                    priority=1,
                                    summary=f"Move {ref} {direction}",
                                    affected_component=ref,
                                    suggested_action="move",
                                    direction=direction,
                                    distance_mm=0.5,
                                )
                            )
                    else:
                        suggestions.append(f"Consider moving component(s): {ref_list}")
                else:
                    suggestions.append(f"Consider moving component(s): {ref_list}")

            if blocking_nets and num_layers > 1:
                suggestions.append("Try routing on a different layer using vias")

        elif cause == FailureCause.VIA_BLOCKED:
            suggestions.append("Via placement blocked in transition area")
            if num_layers > 2:
                suggestions.append("Try alternative via placement location")
            else:
                suggestions.append("Consider 4-layer stackup for more via options")
            actionable.append(
                ActionableSuggestion(
                    category="layer_stack",
                    priority=2,
                    summary="Add layers for more via placement options",
                    suggested_action="add_layer",
                )
            )

        elif cause == FailureCause.CLEARANCE:
            suggestions.append("Reduce trace clearance if manufacturer allows")
            actionable.append(
                ActionableSuggestion(
                    category="design_rules",
                    priority=2,
                    summary="Reduce clearance (check manufacturer limits)",
                    parameter_name="trace_clearance",
                    suggested_action="reduce",
                )
            )

        elif cause == FailureCause.KEEPOUT:
            suggestions.append("Route around keepout zone or adjust boundaries")
            actionable.append(
                ActionableSuggestion(
                    category="placement",
                    priority=3,
                    summary="Modify keepout zone boundaries",
                    suggested_action="reroute",
                )
            )

        elif cause == FailureCause.LAYER_CONFLICT:
            if num_layers == 2:
                suggestions.append("Consider 4-layer stackup for better routability")
                actionable.append(
                    ActionableSuggestion(
                        category="layer_stack",
                        priority=1,
                        summary="Upgrade to 4-layer stackup",
                        parameter_name="layer_count",
                        current_value=2.0,
                        suggested_value=4.0,
                        suggested_action="add_layer",
                    )
                )
            else:
                suggestions.append("No available layer for routing")

        elif cause == FailureCause.PIN_ACCESS:
            suggestions.append("Use finer grid (0.05mm) or neck-down traces")
            actionable.append(
                ActionableSuggestion(
                    category="design_rules",
                    priority=1,
                    summary="Use finer routing grid for pin escape",
                    parameter_name="grid_resolution",
                    suggested_value=0.05,
                    suggested_action="reduce",
                )
            )

        return suggestions, actionable

    def _suggest_move_direction(
        self,
        blocking: list[BlockingElement],
        center: tuple[float, float],
    ) -> str | None:
        """Suggest a direction to move blocking components.

        Args:
            blocking: List of blocking elements
            center: Center of the routing corridor

        Returns:
            Direction string ("north", "south", "east", "west") or None
        """
        if not blocking:
            return None

        # Calculate average position of blockers
        avg_x = sum(b.bounds.center[0] for b in blocking) / len(blocking)
        avg_y = sum(b.bounds.center[1] for b in blocking) / len(blocking)

        # Determine which direction to suggest moving
        dx = avg_x - center[0]
        dy = avg_y - center[1]

        if abs(dx) > abs(dy):
            return "west" if dx > 0 else "east"
        else:
            return "south" if dy > 0 else "north"

    def analyze_pad_access_blockers(
        self,
        grid: RoutingGrid,
        pad_x: float,
        pad_y: float,
        pad_ref: str,
        pad_net: int,
        layer: int,
        net_names: dict[int, str] | None = None,
    ) -> list[PadAccessBlocker]:
        """Analyze what's blocking access to a pad.

        Checks the area around a pad to find which nets' clearance zones
        are blocking routing access to the pad.

        Args:
            grid: The routing grid
            pad_x: Pad center X coordinate in mm
            pad_y: Pad center Y coordinate in mm
            pad_ref: Pad reference (e.g., "U1.13")
            pad_net: Net ID of the pad being accessed
            layer: Layer index to check
            net_names: Optional mapping of net ID to net name

        Returns:
            List of PadAccessBlocker objects describing what's blocking access
        """
        net_names = net_names or {}
        blockers: list[PadAccessBlocker] = []
        seen_nets: set[int] = set()

        # Get grid coordinates of pad center
        center_gx, center_gy = grid.world_to_grid(pad_x, pad_y)

        # Search radius in grid cells - check area around pad for blocking nets
        # Use a radius that covers the trace clearance + trace width
        search_radius_mm = (
            grid.rules.trace_clearance + grid.rules.trace_width + grid.rules.trace_clearance
        )
        search_radius_cells = int(math.ceil(search_radius_mm / grid.resolution)) + 1

        # Track the closest blocking element for each net
        net_closest: dict[int, tuple[float, str]] = {}  # net -> (distance, type)

        for dy in range(-search_radius_cells, search_radius_cells + 1):
            for dx in range(-search_radius_cells, search_radius_cells + 1):
                gx = center_gx + dx
                gy = center_gy + dy

                # Skip out-of-bounds cells
                if not (0 <= gx < grid.cols and 0 <= gy < grid.rows):
                    continue

                cell = grid.grid[layer][gy][gx]

                # Skip unblocked cells or cells belonging to the same net
                if not cell.blocked:
                    continue
                if cell.net == pad_net or cell.net == 0:
                    continue

                # Calculate world distance from pad center to cell center
                cell_wx, cell_wy = grid.grid_to_world(gx, gy)
                distance = math.sqrt((cell_wx - pad_x) ** 2 + (cell_wy - pad_y) ** 2)

                # Determine blocking type
                if cell.pad_blocked:
                    blocking_type = "pad"
                elif cell.usage_count > 0:
                    blocking_type = "trace"
                else:
                    blocking_type = "via"

                # Track closest element for this net
                net_id = cell.net
                if net_id not in net_closest or distance < net_closest[net_id][0]:
                    net_closest[net_id] = (distance, blocking_type)

        # Convert to PadAccessBlocker objects
        for net_id, (distance, blocking_type) in net_closest.items():
            if net_id in seen_nets:
                continue
            seen_nets.add(net_id)

            net_name = net_names.get(net_id, f"Net_{net_id}")

            # Calculate suggested clearance that would allow access
            # The minimum clearance would be distance - trace_width/2
            suggested_clearance = max(0.05, distance - grid.rules.trace_width / 2)

            blockers.append(
                PadAccessBlocker(
                    pad_ref=pad_ref,
                    blocking_net=net_id,
                    blocking_net_name=net_name,
                    blocking_type=blocking_type,
                    distance=distance,
                    suggested_clearance=suggested_clearance,
                )
            )

        # Sort by distance (closest first)
        blockers.sort(key=lambda b: b.distance)

        return blockers
