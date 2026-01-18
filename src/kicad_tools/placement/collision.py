"""Collision detection for placement validation.

Provides data structures and utilities for checking placement collisions
before committing positions to a PCB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .conflict import ConflictSeverity, ConflictType, Point

if TYPE_CHECKING:
    from .conflict import Conflict


@dataclass
class CollisionResult:
    """Result of checking a single placement for collisions.

    Attributes:
        has_collision: True if the placement would cause a collision
        other_ref: Reference designator of the component that would collide (if any)
        conflict_type: Type of conflict detected (if any)
        required_clearance: Minimum clearance required by design rules (mm)
        actual_clearance: Actual clearance that would result (mm)
        location: Location of the conflict (if any)
        message: Human-readable description of the collision
    """

    has_collision: bool = False
    other_ref: str | None = None
    conflict_type: ConflictType | None = None
    required_clearance: float | None = None
    actual_clearance: float | None = None
    location: Point | None = None
    message: str = ""

    @classmethod
    def no_collision(cls) -> CollisionResult:
        """Create a result indicating no collision."""
        return cls(has_collision=False, message="No collision detected")

    @classmethod
    def from_conflict(cls, conflict: Conflict) -> CollisionResult:
        """Create a collision result from a Conflict object."""
        return cls(
            has_collision=True,
            other_ref=conflict.component2,
            conflict_type=conflict.type,
            required_clearance=conflict.required_clearance,
            actual_clearance=conflict.actual_clearance,
            location=conflict.location,
            message=conflict.message,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "has_collision": self.has_collision,
            "other_ref": self.other_ref,
            "conflict_type": self.conflict_type.value if self.conflict_type else None,
            "required_clearance": self.required_clearance,
            "actual_clearance": self.actual_clearance,
            "location": (
                {"x": self.location.x, "y": self.location.y} if self.location else None
            ),
            "message": self.message,
        }


@dataclass
class PlacementValidationResult:
    """Result of validating a batch of placements.

    Attributes:
        is_valid: True if all placements are valid (no collisions)
        total_placements: Number of placements validated
        collision_count: Number of placements that would cause collisions
        collisions: List of collisions detected
    """

    is_valid: bool = True
    total_placements: int = 0
    collision_count: int = 0
    collisions: list[PlacementCollision] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "is_valid": self.is_valid,
            "total_placements": self.total_placements,
            "collision_count": self.collision_count,
            "collisions": [c.to_dict() for c in self.collisions],
        }


@dataclass
class PlacementCollision:
    """A collision between two placements.

    Attributes:
        ref1: Reference of first component
        ref2: Reference of second component
        violation_type: Type of violation detected
        location: Location of the collision
        actual_clearance: Actual clearance (mm)
        required_clearance: Required clearance (mm)
        message: Human-readable description
    """

    ref1: str
    ref2: str
    violation_type: ConflictType
    location: Point | None = None
    actual_clearance: float | None = None
    required_clearance: float | None = None
    message: str = ""

    @classmethod
    def from_conflict(cls, conflict: Conflict) -> PlacementCollision:
        """Create from a Conflict object."""
        return cls(
            ref1=conflict.component1,
            ref2=conflict.component2,
            violation_type=conflict.type,
            location=conflict.location,
            actual_clearance=conflict.actual_clearance,
            required_clearance=conflict.required_clearance,
            message=conflict.message,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "ref1": self.ref1,
            "ref2": self.ref2,
            "violation_type": self.violation_type.value,
            "location": (
                {"x": self.location.x, "y": self.location.y} if self.location else None
            ),
            "actual_clearance": self.actual_clearance,
            "required_clearance": self.required_clearance,
            "message": self.message,
        }


@dataclass
class DRCResult:
    """Result of running design rule check.

    Attributes:
        passed: True if no DRC violations found
        violation_count: Total number of violations
        clearance_count: Number of clearance violations
        courtyard_count: Number of courtyard overlap violations
        violations: List of all violations
    """

    passed: bool = True
    violation_count: int = 0
    clearance_count: int = 0
    courtyard_count: int = 0
    edge_clearance_count: int = 0
    hole_to_hole_count: int = 0
    violations: list[DRCViolation] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "passed": self.passed,
            "violation_count": self.violation_count,
            "clearance_count": self.clearance_count,
            "courtyard_count": self.courtyard_count,
            "edge_clearance_count": self.edge_clearance_count,
            "hole_to_hole_count": self.hole_to_hole_count,
            "violations": [v.to_dict() for v in self.violations],
        }


@dataclass
class DRCViolation:
    """A single DRC violation.

    Attributes:
        type: Type of violation
        severity: Severity level
        description: Human-readable description
        location: Location on the board (x, y in mm)
        components: Components involved
        actual_value: Actual measured value (e.g., clearance)
        required_value: Required value per design rules
    """

    type: ConflictType
    severity: ConflictSeverity
    description: str
    location: Point | None = None
    components: tuple[str, ...] = ()
    actual_value: float | None = None
    required_value: float | None = None

    @classmethod
    def from_conflict(cls, conflict: Conflict) -> DRCViolation:
        """Create from a Conflict object."""
        return cls(
            type=conflict.type,
            severity=conflict.severity,
            description=conflict.message,
            location=conflict.location,
            components=(conflict.component1, conflict.component2),
            actual_value=conflict.actual_clearance or conflict.overlap_amount,
            required_value=conflict.required_clearance,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.type.value,
            "severity": self.severity.value,
            "description": self.description,
            "location": (
                {"x": self.location.x, "y": self.location.y} if self.location else None
            ),
            "components": list(self.components),
            "actual_value": self.actual_value,
            "required_value": self.required_value,
        }
