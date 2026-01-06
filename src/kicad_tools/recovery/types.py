"""
Types for failure analysis and resolution strategies.

This module defines the foundational data structures for intelligent failure
recovery, including failure causes, blocking elements, resolution strategies,
and side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FailureCause(Enum):
    """Root causes for routing/placement failures.

    These causes help categorize why an operation failed, enabling
    targeted resolution strategies.
    """

    CONGESTION = "congestion"  # Too many traces in area
    BLOCKED_PATH = "blocked_path"  # Component in the way
    CLEARANCE = "clearance"  # Can't meet DRC clearance
    LAYER_CONFLICT = "layer_conflict"  # Wrong layer or no layer available
    PIN_ACCESS = "pin_access"  # Can't reach pin
    LENGTH_CONSTRAINT = "length_constraint"  # Can't meet length requirements
    DIFFERENTIAL_PAIR = "differential_pair"  # Can't maintain pair constraints
    KEEPOUT = "keepout"  # Path crosses keepout zone


class StrategyType(Enum):
    """Types of resolution strategies.

    Each type represents a different approach to resolving a failure.
    """

    MOVE_COMPONENT = "move_component"  # Move a single component
    MOVE_MULTIPLE = "move_multiple"  # Move multiple components
    ADD_VIA = "add_via"  # Add via to change layers
    CHANGE_LAYER = "change_layer"  # Route on different layer
    REROUTE_NET = "reroute_net"  # Reroute a single net
    REROUTE_MULTIPLE = "reroute_multiple"  # Reroute multiple nets
    WIDEN_CLEARANCE = "widen_clearance"  # Adjust DRC rules
    MANUAL_INTERVENTION = "manual_intervention"  # Requires human judgment


class Difficulty(Enum):
    """Difficulty/risk level of a strategy.

    Higher difficulty strategies have more potential side effects
    and may require more careful verification.
    """

    TRIVIAL = "trivial"  # No side effects
    EASY = "easy"  # Minor side effects
    MEDIUM = "medium"  # Moderate side effects, may need verification
    HARD = "hard"  # Significant changes, high risk
    EXPERT = "expert"  # Requires human judgment


@dataclass
class Rectangle:
    """Axis-aligned bounding box for spatial operations.

    Attributes:
        min_x: Minimum x coordinate in mm.
        min_y: Minimum y coordinate in mm.
        max_x: Maximum x coordinate in mm.
        max_y: Maximum y coordinate in mm.
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

    def contains_point(self, x: float, y: float) -> bool:
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
        """Return a new rectangle expanded by the given margin."""
        return Rectangle(
            self.min_x - margin,
            self.min_y - margin,
            self.max_x + margin,
            self.max_y + margin,
        )

    def to_dict(self) -> dict[str, float]:
        """Convert to dictionary for JSON serialization."""
        return {
            "min_x": self.min_x,
            "min_y": self.min_y,
            "max_x": self.max_x,
            "max_y": self.max_y,
        }


@dataclass
class BlockingElement:
    """Something blocking the desired operation.

    Represents a component, trace, via, zone, or keepout that is
    preventing a routing or placement operation from succeeding.

    Attributes:
        type: Type of blocking element ("component", "trace", "via", "zone", "keepout").
        ref: Component reference designator if applicable.
        net: Net name if applicable.
        bounds: Bounding box of the element.
        movable: Whether this element can be moved to resolve the issue.
    """

    type: str  # "component", "trace", "via", "zone", "keepout"
    ref: str | None
    net: str | None
    bounds: Rectangle
    movable: bool

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.type,
            "ref": self.ref,
            "net": self.net,
            "bounds": self.bounds.to_dict(),
            "movable": self.movable,
        }


@dataclass
class PathAttempt:
    """Record of a routing path attempt.

    Stores information about a failed routing attempt for analysis.

    Attributes:
        start: Starting point (x, y) in mm.
        end: Ending point (x, y) in mm.
        reached: How far the path got before failing (0-1).
        failure_point: Where the path failed (x, y) in mm.
        failure_reason: Brief description of why it failed.
    """

    start: tuple[float, float]
    end: tuple[float, float]
    reached: float  # 0-1, how far the path got
    failure_point: tuple[float, float] | None
    failure_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "start": {"x": self.start[0], "y": self.start[1]},
            "end": {"x": self.end[0], "y": self.end[1]},
            "reached": self.reached,
            "failure_point": (
                {"x": self.failure_point[0], "y": self.failure_point[1]}
                if self.failure_point
                else None
            ),
            "failure_reason": self.failure_reason,
        }


@dataclass
class FailureAnalysis:
    """Detailed analysis of why an operation failed.

    This is the output of root cause analysis and serves as input
    to the strategy generator.

    Attributes:
        root_cause: The determined root cause of the failure.
        confidence: How confident we are in the root cause (0-1).
        failure_location: Point where the failure occurred (x, y) in mm.
        failure_area: Bounding box of the affected area.
        blocking_elements: Elements contributing to the failure.
        attempted_paths: Number of routing paths attempted.
        best_attempt: The most successful path attempt.
        congestion_score: Congestion level in the area (0-1).
        clearance_margin: How close to DRC limits we got in mm.
        net: Name of the net that failed (if routing failure).
    """

    root_cause: FailureCause
    confidence: float
    failure_location: tuple[float, float]
    failure_area: Rectangle
    blocking_elements: list[BlockingElement] = field(default_factory=list)
    attempted_paths: int = 0
    best_attempt: PathAttempt | None = None
    congestion_score: float = 0.0
    clearance_margin: float = 0.0
    net: str | None = None

    @property
    def has_movable_blockers(self) -> bool:
        """Check if any blocking elements can be moved."""
        return any(el.movable for el in self.blocking_elements)

    @property
    def has_reroutable_nets(self) -> bool:
        """Check if blocking elements include traces that could be rerouted."""
        return any(el.type == "trace" and el.net != self.net for el in self.blocking_elements)

    @property
    def near_connector(self) -> bool:
        """Check if the failure is near a connector component."""
        connector_prefixes = ("J", "CN", "CONN", "USB", "HDMI", "ETH")
        for el in self.blocking_elements:
            if el.ref and el.ref.upper().startswith(connector_prefixes):
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "root_cause": self.root_cause.value,
            "confidence": self.confidence,
            "failure_location": {
                "x": self.failure_location[0],
                "y": self.failure_location[1],
            },
            "failure_area": self.failure_area.to_dict(),
            "blocking_elements": [el.to_dict() for el in self.blocking_elements],
            "attempted_paths": self.attempted_paths,
            "best_attempt": self.best_attempt.to_dict() if self.best_attempt else None,
            "congestion_score": self.congestion_score,
            "clearance_margin": self.clearance_margin,
            "net": self.net,
        }


@dataclass
class SideEffect:
    """A potential side effect of a strategy.

    Describes what might happen as a consequence of applying
    a resolution strategy.

    Attributes:
        description: Human-readable description of the side effect.
        severity: Severity level ("info", "warning", "risk").
        mitigatable: Whether this side effect can be mitigated.
    """

    description: str
    severity: str  # "info", "warning", "risk"
    mitigatable: bool

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "description": self.description,
            "severity": self.severity,
            "mitigatable": self.mitigatable,
        }


@dataclass
class Action:
    """A single action in a strategy.

    Represents a concrete operation to be performed as part
    of a resolution strategy.

    Attributes:
        type: Action type ("move", "add_via", "reroute", "change_layer").
        target: Target of the action (component ref, net name, etc.).
        params: Action-specific parameters.
    """

    type: str  # "move", "add_via", "reroute", "change_layer"
    target: str  # Component ref, net name, etc.
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.type,
            "target": self.target,
            "params": self.params,
        }


@dataclass
class ResolutionStrategy:
    """A concrete strategy to resolve a failure.

    Contains all information needed to execute a resolution,
    including actions, side effects, and confidence estimates.

    Attributes:
        type: Type of resolution strategy.
        difficulty: Difficulty/risk level.
        confidence: How likely this strategy will work (0-1).
        actions: List of actions to perform.
        side_effects: Potential side effects.
        affected_components: Component refs affected by this strategy.
        affected_nets: Net names affected by this strategy.
        estimated_improvement: How much this helps (0-1).
    """

    type: StrategyType
    difficulty: Difficulty
    confidence: float
    actions: list[Action] = field(default_factory=list)
    side_effects: list[SideEffect] = field(default_factory=list)
    affected_components: list[str] = field(default_factory=list)
    affected_nets: list[str] = field(default_factory=list)
    estimated_improvement: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.type.value,
            "difficulty": self.difficulty.value,
            "confidence": self.confidence,
            "actions": [a.to_dict() for a in self.actions],
            "side_effects": [e.to_dict() for e in self.side_effects],
            "affected_components": self.affected_components,
            "affected_nets": self.affected_nets,
            "estimated_improvement": self.estimated_improvement,
        }
