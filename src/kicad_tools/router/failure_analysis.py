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
    PIN_ACCESS = "pin_access"  # Can't reach pin
    LENGTH_CONSTRAINT = "length_constraint"  # Can't meet length requirements
    DIFFERENTIAL_PAIR = "differential_pair"  # Can't maintain pair constraints
    KEEPOUT = "keepout"  # Path crosses keepout zone
    UNKNOWN = "unknown"  # Unable to determine root cause


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
class FailureAnalysis:
    """Detailed analysis of why an operation failed."""

    root_cause: FailureCause
    confidence: float  # 0.0-1.0, confidence in the diagnosis

    # Location info
    failure_location: tuple[float, float]
    failure_area: Rectangle

    # What's blocking
    blocking_elements: list[BlockingElement] = field(default_factory=list)

    # Attempted solutions
    attempted_paths: int = 0
    best_attempt: PathAttempt | None = None

    # Metrics
    congestion_score: float = 0.0  # 0-1, how congested the area is
    clearance_margin: float = float("inf")  # How close to DRC limits

    # Suggestions
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "root_cause": self.root_cause.value,
            "confidence": self.confidence,
            "failure_location": list(self.failure_location),
            "failure_area": {
                "min_x": self.failure_area.min_x,
                "min_y": self.failure_area.min_y,
                "max_x": self.failure_area.max_x,
                "max_y": self.failure_area.max_y,
            },
            "blocking_elements": [e.to_dict() for e in self.blocking_elements],
            "attempted_paths": self.attempted_paths,
            "best_attempt": self.best_attempt.to_dict() if self.best_attempt else None,
            "congestion_score": self.congestion_score,
            "clearance_margin": (
                self.clearance_margin if self.clearance_margin != float("inf") else None
            ),
            "suggestions": self.suggestions,
        }

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
    ) -> FailureAnalysis:
        """Analyze why routing failed between two points.

        Args:
            grid: The routing grid
            start: Start point (x, y) in mm
            end: End point (x, y) in mm
            net: Net name being routed
            attempts: List of PathAttempt records (optional)
            layer: Layer index for routing

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

        # Determine root cause
        cause, confidence = self._determine_root_cause(
            grid,
            corridor,
            blocking,
            congestion_score,
            clearance_margin,
            layer,
        )

        # Generate suggestions
        suggestions = self._generate_suggestions(cause, blocking, congestion_score, grid.num_layers)

        return FailureAnalysis(
            root_cause=cause,
            confidence=confidence,
            failure_location=failure_location,
            failure_area=corridor,
            blocking_elements=blocking,
            attempted_paths=len(attempts),
            best_attempt=best_attempt,
            congestion_score=congestion_score,
            clearance_margin=clearance_margin,
            suggestions=suggestions,
        )

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
    ) -> list[str]:
        """Generate actionable suggestions based on root cause.

        Args:
            cause: Determined root cause
            blocking: List of blocking elements
            congestion_score: Congestion score
            num_layers: Number of available layers

        Returns:
            List of suggestion strings
        """
        suggestions: list[str] = []

        if cause == FailureCause.CONGESTION:
            suggestions.append("Area is highly congested")
            if num_layers < 4:
                suggestions.append("Consider adding more routing layers")
            suggestions.append("Try rerouting earlier nets to spread congestion")

        elif cause == FailureCause.BLOCKED_PATH:
            movable_refs = {b.ref for b in blocking if b.movable and b.ref}
            if movable_refs:
                suggestions.append(
                    f"Consider moving component(s): {', '.join(sorted(movable_refs))}"
                )
            if num_layers > 1:
                suggestions.append("Try routing on a different layer using vias")

        elif cause == FailureCause.CLEARANCE:
            suggestions.append("Insufficient clearance to meet DRC requirements")
            suggestions.append("Check design rules and consider wider trace spacing")

        elif cause == FailureCause.KEEPOUT:
            suggestions.append("Route path crosses a keepout zone")
            suggestions.append("Modify keepout boundaries or reroute around")

        elif cause == FailureCause.LAYER_CONFLICT:
            suggestions.append("No available layer for routing")
            if num_layers == 2:
                suggestions.append("Consider 4-layer stackup for better routability")

        elif cause == FailureCause.PIN_ACCESS:
            suggestions.append("Cannot access pin through surrounding obstacles")
            suggestions.append("Move blocking components or use different approach angle")

        # General suggestions based on congestion
        if congestion_score > 0.5:
            suggestions.append(
                f"Congestion score: {congestion_score:.0%} - consider spreading routes"
            )

        return suggestions
