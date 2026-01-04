"""
Edge placement constraints for connectors and interfaces.

Provides constraints that keep components (connectors, mounting holes, test points)
at board edges during placement optimization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from kicad_tools.optim.geometry import Polygon, Vector2D

if TYPE_CHECKING:
    from kicad_tools.optim.components import Component
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "Edge",
    "EdgeSide",
    "EdgeConstraint",
    "BoardEdges",
    "detect_edge_components",
    "get_board_edges",
]


class EdgeSide(Enum):
    """Which edge of the board a component should be placed at."""

    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"
    ANY = "any"  # Optimizer chooses best edge


@dataclass
class Edge:
    """A board edge segment."""

    start: Vector2D
    end: Vector2D
    side: EdgeSide

    @property
    def length(self) -> float:
        """Length of this edge segment."""
        return (self.end - self.start).magnitude()

    @property
    def direction(self) -> Vector2D:
        """Unit vector along the edge."""
        return (self.end - self.start).normalized()

    @property
    def normal(self) -> Vector2D:
        """Outward-facing normal (perpendicular to edge, pointing out of board)."""
        # For a CCW polygon, perpendicular rotated 90 deg CW points outward
        d = self.direction
        return Vector2D(d.y, -d.x)

    def project_point(self, point: Vector2D) -> tuple[float, float]:
        """
        Project a point onto this edge.

        Returns:
            Tuple of (position along edge in mm from start, perpendicular distance)
        """
        to_point = point - self.start
        edge_vec = self.end - self.start
        edge_len = edge_vec.magnitude()

        if edge_len < 1e-10:
            return 0.0, to_point.magnitude()

        # Position along edge
        t = to_point.dot(edge_vec) / (edge_len * edge_len)
        position_mm = t * edge_len

        # Perpendicular distance
        closest = self.start + edge_vec * t
        distance = (point - closest).magnitude()

        return position_mm, distance


@dataclass
class EdgeConstraint:
    """
    Constraint that keeps a component at a board edge.

    Attributes:
        reference: Component reference designator (e.g., "USB1", "J1")
        edge: Which edge to constrain to ("top", "bottom", "left", "right", "any")
        position: Position along edge (mm from corner, or "center")
        slide: If True, component can slide along edge during optimization
        corner_priority: If True, prefer corner positions (for mounting holes)
        offset_mm: Inset from edge in mm (e.g., for clearance)
    """

    reference: str
    edge: str = "any"  # "top", "bottom", "left", "right", "any"
    position: float | str | None = None  # mm from corner, or "center"
    slide: bool = True
    corner_priority: bool = False
    offset_mm: float = 0.0

    def __post_init__(self):
        """Validate edge value."""
        valid_edges = {"top", "bottom", "left", "right", "any"}
        if self.edge not in valid_edges:
            raise ValueError(f"Invalid edge '{self.edge}'. Must be one of {valid_edges}")

    @property
    def edge_side(self) -> EdgeSide:
        """Get EdgeSide enum from string edge value."""
        return EdgeSide(self.edge)


@dataclass
class BoardEdges:
    """
    Extracted board edges with methods for edge constraint calculations.

    Represents the four primary edges of a rectangular board.
    For non-rectangular boards, edges are approximated from the bounding box.
    """

    top: Edge
    bottom: Edge
    left: Edge
    right: Edge
    outline: Polygon

    @classmethod
    def from_polygon(cls, polygon: Polygon) -> BoardEdges:
        """
        Extract board edges from a polygon outline.

        For rectangular boards, identifies the four edges directly.
        For non-rectangular boards, uses bounding box approximation.
        """
        if not polygon.vertices:
            # Default 100x80mm board
            return cls.from_bounds(0, 0, 100, 80)

        # Get bounding box
        min_x = min(v.x for v in polygon.vertices)
        max_x = max(v.x for v in polygon.vertices)
        min_y = min(v.y for v in polygon.vertices)
        max_y = max(v.y for v in polygon.vertices)

        return cls.from_bounds(min_x, min_y, max_x - min_x, max_y - min_y, polygon)

    @classmethod
    def from_bounds(
        cls,
        x: float,
        y: float,
        width: float,
        height: float,
        outline: Polygon | None = None,
    ) -> BoardEdges:
        """Create board edges from rectangular bounds."""
        # KiCad uses Y-down coordinate system, so:
        # - Top edge is at min_y
        # - Bottom edge is at max_y
        min_x, min_y = x, y
        max_x, max_y = x + width, y + height

        top = Edge(
            start=Vector2D(min_x, min_y),
            end=Vector2D(max_x, min_y),
            side=EdgeSide.TOP,
        )
        bottom = Edge(
            start=Vector2D(max_x, max_y),
            end=Vector2D(min_x, max_y),
            side=EdgeSide.BOTTOM,
        )
        left = Edge(
            start=Vector2D(min_x, max_y),
            end=Vector2D(min_x, min_y),
            side=EdgeSide.LEFT,
        )
        right = Edge(
            start=Vector2D(max_x, min_y),
            end=Vector2D(max_x, max_y),
            side=EdgeSide.RIGHT,
        )

        if outline is None:
            outline = Polygon.rectangle(x + width / 2, y + height / 2, width, height)

        return cls(top=top, bottom=bottom, left=left, right=right, outline=outline)

    def get_edge(self, side: EdgeSide | str) -> Edge:
        """Get edge by side."""
        if isinstance(side, str):
            side = EdgeSide(side)

        if side == EdgeSide.TOP:
            return self.top
        elif side == EdgeSide.BOTTOM:
            return self.bottom
        elif side == EdgeSide.LEFT:
            return self.left
        elif side == EdgeSide.RIGHT:
            return self.right
        else:
            raise ValueError(f"Cannot get edge for side={side}")

    def all_edges(self) -> list[Edge]:
        """Get all four edges."""
        return [self.top, self.bottom, self.left, self.right]

    def nearest_edge(self, point: Vector2D) -> Edge:
        """Find the nearest edge to a point."""
        best_edge = self.top
        best_distance = float("inf")

        for edge in self.all_edges():
            _, distance = edge.project_point(point)
            if distance < best_distance:
                best_distance = distance
                best_edge = edge

        return best_edge

    def corners(self) -> list[Vector2D]:
        """Get the four corner positions."""
        return [
            self.top.start,  # Top-left
            self.top.end,  # Top-right
            self.bottom.start,  # Bottom-right
            self.bottom.end,  # Bottom-left
        ]

    def nearest_corner(self, point: Vector2D) -> Vector2D:
        """Find the nearest corner to a point."""
        best_corner = self.corners()[0]
        best_distance = float("inf")

        for corner in self.corners():
            distance = (point - corner).magnitude()
            if distance < best_distance:
                best_distance = distance
                best_corner = corner

        return best_corner


def get_board_edges(pcb: PCB) -> BoardEdges:
    """
    Extract board edges from a PCB.

    Parses Edge.Cuts layer to find the board outline and extracts
    the four primary edges.

    Args:
        pcb: Loaded PCB object

    Returns:
        BoardEdges with the four primary edges
    """
    from kicad_tools.optim.placement import PlacementOptimizer

    # Use existing outline extraction logic
    outline = PlacementOptimizer._extract_board_outline(pcb)

    if outline is None:
        # Fall back to estimating from footprints
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")

        for fp in pcb.footprints:
            x, y = fp.position
            min_x = min(min_x, x - 10)
            max_x = max(max_x, x + 10)
            min_y = min(min_y, y - 10)
            max_y = max(max_y, y + 10)

        if min_x == float("inf"):
            return BoardEdges.from_bounds(50, 40, 100, 80)

        return BoardEdges.from_bounds(min_x, min_y, max_x - min_x, max_y - min_y)

    return BoardEdges.from_polygon(outline)


# Component type detection patterns
_CONNECTOR_PATTERNS = [
    re.compile(r"^J\d+", re.IGNORECASE),  # J1, J2, etc.
    re.compile(r"^P\d+", re.IGNORECASE),  # P1, P2, etc.
    re.compile(r"^CON\d*", re.IGNORECASE),  # CON1, CON, etc.
    re.compile(r"^USB\d*", re.IGNORECASE),  # USB1, USB, etc.
    re.compile(r"^DC\d*", re.IGNORECASE),  # DC1 (barrel jacks)
    re.compile(r"^PWR\d*", re.IGNORECASE),  # PWR1
]

_MOUNTING_HOLE_PATTERNS = [
    re.compile(r"^MH\d*", re.IGNORECASE),  # MH1, MH2, etc.
    re.compile(r"^H\d+", re.IGNORECASE),  # H1, H2, etc.
    re.compile(r"^MOUNT", re.IGNORECASE),  # MOUNT1, MOUNTING, etc.
]

_TEST_POINT_PATTERNS = [
    re.compile(r"^TP\d*", re.IGNORECASE),  # TP1, TP2, etc.
    re.compile(r"^TEST\d*", re.IGNORECASE),  # TEST1, etc.
]

_SWITCH_PATTERNS = [
    re.compile(r"^SW\d*", re.IGNORECASE),  # SW1, SW2, etc.
    re.compile(r"^BTN\d*", re.IGNORECASE),  # BTN1, etc.
    re.compile(r"^S\d+$", re.IGNORECASE),  # S1, S2, etc.
]

_LED_PATTERNS = [
    re.compile(r"^LED\d*", re.IGNORECASE),  # LED1, LED2, etc.
    re.compile(r"^D\d+$", re.IGNORECASE),  # D1, D2 (often LEDs)
]


def _matches_patterns(ref: str, patterns: list[re.Pattern]) -> bool:
    """Check if reference matches any pattern."""
    return any(p.match(ref) for p in patterns)


def _is_connector(ref: str, footprint_name: str = "") -> bool:
    """Check if component is a connector based on reference or footprint."""
    if _matches_patterns(ref, _CONNECTOR_PATTERNS):
        return True

    # Check footprint name for connector keywords
    fp_lower = footprint_name.lower()
    connector_keywords = ["usb", "barrel", "jack", "header", "connector", "socket"]
    return any(kw in fp_lower for kw in connector_keywords)


def _is_mounting_hole(ref: str, footprint_name: str = "") -> bool:
    """Check if component is a mounting hole."""
    if _matches_patterns(ref, _MOUNTING_HOLE_PATTERNS):
        return True

    fp_lower = footprint_name.lower()
    return "mounting" in fp_lower or "hole" in fp_lower


def _is_test_point(ref: str, footprint_name: str = "") -> bool:
    """Check if component is a test point."""
    if _matches_patterns(ref, _TEST_POINT_PATTERNS):
        return True

    fp_lower = footprint_name.lower()
    return "testpoint" in fp_lower or "test_point" in fp_lower


def _is_switch(ref: str, footprint_name: str = "") -> bool:
    """Check if component is a switch or button."""
    if _matches_patterns(ref, _SWITCH_PATTERNS):
        return True

    fp_lower = footprint_name.lower()
    return "switch" in fp_lower or "button" in fp_lower or "tactile" in fp_lower


def _is_led(ref: str, footprint_name: str = "") -> bool:
    """Check if component is an LED."""
    if _matches_patterns(ref, _LED_PATTERNS):
        return True

    fp_lower = footprint_name.lower()
    return "led" in fp_lower


def detect_edge_components(
    pcb: PCB,
    include_connectors: bool = True,
    include_mounting_holes: bool = True,
    include_test_points: bool = True,
    include_switches: bool = True,
    include_leds: bool = False,
) -> list[EdgeConstraint]:
    """
    Auto-detect components that should be placed at board edges.

    Detection heuristics:
    - Connectors (USB, barrel jack, headers): board edge for accessibility
    - Mounting holes: corners preferred for mechanical stability
    - Test points: edge accessible for probing
    - Switches/buttons: user-accessible edges
    - LEDs: visible edges (optional, disabled by default)

    Args:
        pcb: Loaded PCB object
        include_connectors: Include connectors (J*, USB*, etc.)
        include_mounting_holes: Include mounting holes (MH*, H*)
        include_test_points: Include test points (TP*)
        include_switches: Include switches (SW*, BTN*)
        include_leds: Include LEDs (LED*, D*)

    Returns:
        List of EdgeConstraint objects for detected edge components
    """
    constraints: list[EdgeConstraint] = []

    for fp in pcb.footprints:
        ref = fp.reference
        fp_name = fp.footprint_name if hasattr(fp, "footprint_name") else ""

        # Check each component type
        if include_connectors and _is_connector(ref, fp_name):
            constraints.append(
                EdgeConstraint(
                    reference=ref,
                    edge="any",
                    slide=True,
                    corner_priority=False,
                )
            )
        elif include_mounting_holes and _is_mounting_hole(ref, fp_name):
            constraints.append(
                EdgeConstraint(
                    reference=ref,
                    edge="any",
                    slide=False,  # Mounting holes usually have fixed positions
                    corner_priority=True,  # Prefer corners
                )
            )
        elif include_test_points and _is_test_point(ref, fp_name):
            constraints.append(
                EdgeConstraint(
                    reference=ref,
                    edge="any",
                    slide=True,
                )
            )
        elif include_switches and _is_switch(ref, fp_name):
            constraints.append(
                EdgeConstraint(
                    reference=ref,
                    edge="any",  # Usually top or side
                    slide=True,
                )
            )
        elif include_leds and _is_led(ref, fp_name):
            constraints.append(
                EdgeConstraint(
                    reference=ref,
                    edge="any",
                    slide=True,
                )
            )

    return constraints


def compute_edge_force(
    component: Component,
    constraint: EdgeConstraint,
    board_edges: BoardEdges,
    stiffness: float = 50.0,
) -> tuple[Vector2D, bool]:
    """
    Compute force to keep component at edge.

    The force pulls the component toward its assigned edge while allowing
    sliding along the edge if constraint.slide is True.

    Args:
        component: The component to constrain
        constraint: Edge constraint for this component
        board_edges: Board edge definitions
        stiffness: Spring stiffness for edge attraction

    Returns:
        Tuple of (force vector, is_at_edge boolean)
    """
    pos = component.position()

    # Determine target edge
    if constraint.edge == "any":
        # Find nearest edge
        target_edge = board_edges.nearest_edge(pos)
    else:
        target_edge = board_edges.get_edge(constraint.edge)

    # Find closest point on edge to component
    edge_vec = target_edge.end - target_edge.start
    edge_len = edge_vec.magnitude()
    to_point = pos - target_edge.start

    if edge_len > 1e-10:
        t = max(0.0, min(1.0, to_point.dot(edge_vec) / (edge_len * edge_len)))
    else:
        t = 0.0

    closest = target_edge.start + edge_vec * t
    position_along = t * edge_len

    # Vector from component to closest point on edge
    to_edge = closest - pos
    distance_from = to_edge.magnitude()

    # Force toward edge
    force = Vector2D(0.0, 0.0)

    # Account for offset
    effective_distance = distance_from - constraint.offset_mm

    if effective_distance > 0.1:  # Not yet at edge
        # Pull toward edge - force points from component toward edge
        force = to_edge.normalized() * (stiffness * effective_distance)

    # If not sliding, also constrain position along edge
    if not constraint.slide and constraint.position is not None:
        if isinstance(constraint.position, str) and constraint.position == "center":
            target_pos = target_edge.length / 2
        else:
            target_pos = float(constraint.position)

        # Add force along edge direction
        pos_error = target_pos - position_along
        if abs(pos_error) > 0.1:
            force = force + target_edge.direction * (stiffness * pos_error * 0.5)

    # Corner priority: add weak force toward nearest corner
    if constraint.corner_priority:
        nearest = board_edges.nearest_corner(pos)
        to_corner = nearest - pos
        dist_to_corner = to_corner.magnitude()
        if dist_to_corner > 1.0:
            force = force + to_corner.normalized() * (stiffness * 0.3)

    is_at_edge = effective_distance < 1.0  # Within 1mm of edge

    return force, is_at_edge
