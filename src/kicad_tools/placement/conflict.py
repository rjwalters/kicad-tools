"""Placement conflict data structures."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ConflictType(Enum):
    """Types of placement conflicts."""

    # Clearance violations
    PAD_CLEARANCE = "pad_clearance"  # Pads of different components too close
    COURTYARD_OVERLAP = "courtyard_overlap"  # Component courtyards intersecting
    HOLE_TO_HOLE = "hole_to_hole"  # Drill holes too close or overlapping
    SILKSCREEN_PAD = "silkscreen_pad"  # Silkscreen over copper pads
    EDGE_CLEARANCE = "edge_clearance"  # Components too close to board edge

    @classmethod
    def from_string(cls, s: str) -> "ConflictType":
        """Parse conflict type from string."""
        s_lower = s.lower().strip()
        for ctype in cls:
            if ctype.value == s_lower:
                return ctype
            if ctype.value.replace("_", " ") in s_lower:
                return ctype
        # Fallback mappings
        if "courtyard" in s_lower:
            return cls.COURTYARD_OVERLAP
        if "hole" in s_lower:
            return cls.HOLE_TO_HOLE
        if "pad" in s_lower and "clearance" in s_lower:
            return cls.PAD_CLEARANCE
        if "silk" in s_lower:
            return cls.SILKSCREEN_PAD
        if "edge" in s_lower:
            return cls.EDGE_CLEARANCE
        return cls.PAD_CLEARANCE  # Default


class ConflictSeverity(Enum):
    """Severity level of a conflict."""

    ERROR = "error"  # Must fix before manufacturing
    WARNING = "warning"  # Should fix but may be acceptable
    INFO = "info"  # Informational, may not need fixing


@dataclass
class Point:
    """2D point in mm."""

    x: float
    y: float

    def __add__(self, other: "Point") -> "Point":
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: "Point") -> "Point":
        return Point(self.x - other.x, self.y - other.y)

    def distance_to(self, other: "Point") -> float:
        """Calculate distance to another point."""
        import math

        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2)

    def __repr__(self) -> str:
        return f"Point({self.x:.4f}, {self.y:.4f})"


@dataclass
class Rectangle:
    """Axis-aligned bounding box."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def center(self) -> Point:
        return Point((self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2)

    def contains(self, p: Point) -> bool:
        return self.min_x <= p.x <= self.max_x and self.min_y <= p.y <= self.max_y

    def intersects(self, other: "Rectangle") -> bool:
        """Check if this rectangle intersects with another."""
        return not (
            self.max_x < other.min_x
            or self.min_x > other.max_x
            or self.max_y < other.min_y
            or self.min_y > other.max_y
        )

    def intersection_area(self, other: "Rectangle") -> float:
        """Calculate the area of intersection with another rectangle."""
        if not self.intersects(other):
            return 0.0
        x_overlap = max(0, min(self.max_x, other.max_x) - max(self.min_x, other.min_x))
        y_overlap = max(0, min(self.max_y, other.max_y) - max(self.min_y, other.min_y))
        return x_overlap * y_overlap

    def overlap_vector(self, other: "Rectangle") -> Optional[Point]:
        """Calculate minimum translation to separate from another rectangle.

        Returns None if rectangles don't overlap.
        Returns Point with x,y components indicating the minimum move needed.
        """
        if not self.intersects(other):
            return None

        # Calculate overlap in each direction
        left_overlap = self.max_x - other.min_x
        right_overlap = other.max_x - self.min_x
        top_overlap = self.max_y - other.min_y
        bottom_overlap = other.max_y - self.min_y

        # Find minimum separation
        min_x = left_overlap if left_overlap < right_overlap else -right_overlap
        min_y = top_overlap if top_overlap < bottom_overlap else -bottom_overlap

        # Return the axis with smaller overlap
        if abs(min_x) < abs(min_y):
            return Point(min_x, 0)
        else:
            return Point(0, min_y)

    def expand(self, margin: float) -> "Rectangle":
        """Return expanded rectangle."""
        return Rectangle(
            self.min_x - margin,
            self.min_y - margin,
            self.max_x + margin,
            self.max_y + margin,
        )

    def __repr__(self) -> str:
        return f"Rectangle({self.min_x:.3f}, {self.min_y:.3f}, {self.max_x:.3f}, {self.max_y:.3f})"


@dataclass
class ComponentInfo:
    """Information about a component on the PCB."""

    reference: str  # e.g., "C9", "U3"
    footprint: str  # Footprint name
    position: Point  # Component center position
    rotation: float = 0  # Rotation in degrees
    layer: str = "F.Cu"  # Component layer

    # Bounding boxes (calculated from footprint)
    courtyard: Optional[Rectangle] = None  # Courtyard boundary
    pads_bbox: Optional[Rectangle] = None  # Bounding box of all pads

    # Lists of features
    pads: list["PadInfo"] = field(default_factory=list)
    holes: list["HoleInfo"] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"ComponentInfo({self.reference}, pos={self.position})"


@dataclass
class PadInfo:
    """Information about a pad."""

    name: str  # Pad name/number
    position: Point  # Absolute position on PCB
    size: tuple[float, float]  # Width, height in mm
    shape: str = "rect"  # rect, circle, oval
    net: str = ""  # Net name if assigned

    def bbox(self) -> Rectangle:
        """Get bounding box of the pad."""
        w, h = self.size
        return Rectangle(
            self.position.x - w / 2,
            self.position.y - h / 2,
            self.position.x + w / 2,
            self.position.y + h / 2,
        )


@dataclass
class HoleInfo:
    """Information about a drill hole."""

    position: Point  # Absolute position on PCB
    diameter: float  # Drill diameter in mm
    is_plated: bool = True  # PTH vs NPTH


@dataclass
class Conflict:
    """Represents a placement conflict between components."""

    type: ConflictType
    severity: ConflictSeverity
    component1: str  # Reference designator
    component2: str  # Reference designator (or "edge" for edge conflicts)
    message: str
    location: Point  # Where the conflict occurs

    # Detailed measurements
    actual_clearance: Optional[float] = None  # Current clearance in mm
    required_clearance: Optional[float] = None  # Required clearance in mm
    overlap_amount: Optional[float] = None  # Overlap in mm (for courtyard)

    def __str__(self) -> str:
        if self.actual_clearance is not None and self.required_clearance is not None:
            return (
                f"CONFLICT: {self.component1} and {self.component2} - {self.message} "
                f"({self.actual_clearance:.3f}mm actual, {self.required_clearance:.3f}mm required)"
            )
        elif self.overlap_amount is not None:
            return (
                f"CONFLICT: {self.component1} and {self.component2} - {self.message} "
                f"(overlap: {self.overlap_amount:.3f}mm)"
            )
        return f"CONFLICT: {self.component1} and {self.component2} - {self.message}"

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "type": self.type.value,
            "severity": self.severity.value,
            "component1": self.component1,
            "component2": self.component2,
            "message": self.message,
            "location": {"x": self.location.x, "y": self.location.y},
            "actual_clearance": self.actual_clearance,
            "required_clearance": self.required_clearance,
            "overlap_amount": self.overlap_amount,
        }


@dataclass
class PlacementFix:
    """Suggested fix for a placement conflict."""

    conflict: Conflict
    component: str  # Which component to move
    move_vector: Point  # How much to move (dx, dy) in mm
    confidence: float = 0.0  # 0-1, how confident we are this is a good fix

    # For verification
    new_position: Optional[Point] = None
    expected_clearance: Optional[float] = None
    creates_new_conflicts: bool = False  # If True, this fix might create new issues

    def __str__(self) -> str:
        return (
            f"FIX: Move {self.component} by ({self.move_vector.x:+.3f}, "
            f"{self.move_vector.y:+.3f})mm to resolve {self.conflict.type.value}"
        )

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "component": self.component,
            "move_vector": {"x": self.move_vector.x, "y": self.move_vector.y},
            "confidence": self.confidence,
            "new_position": (
                {"x": self.new_position.x, "y": self.new_position.y}
                if self.new_position
                else None
            ),
            "expected_clearance": self.expected_clearance,
            "creates_new_conflicts": self.creates_new_conflicts,
            "conflict_type": self.conflict.type.value,
        }
