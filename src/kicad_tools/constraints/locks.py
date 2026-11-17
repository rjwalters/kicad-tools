"""
Constraint lock data classes for multi-stage optimization.

This module defines the data structures for locking components, nets,
and regions during PCB optimization. Locks preserve decisions made by
early optimization stages so later stages respect them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class LockType(Enum):
    """Types of locks that can be applied to components."""

    POSITION = "position"  # Lock x, y coordinates only
    ROTATION = "rotation"  # Lock rotation angle only
    FULL = "full"  # Lock both position and rotation


@dataclass
class ComponentLock:
    """
    A lock on a component's position and/or rotation.

    Attributes:
        ref: Component reference designator (e.g., "FB1", "U3")
        lock_type: What aspects of the component are locked
        reason: Human-readable explanation of why the lock exists
        locked_by: Identifier of the agent/optimizer that created the lock
        timestamp: When the lock was created
        position: Locked position (x, y) in mm, if lock_type includes position
        rotation: Locked rotation in degrees, if lock_type includes rotation
    """

    ref: str
    lock_type: LockType
    reason: str
    locked_by: str
    timestamp: datetime = field(default_factory=datetime.now)
    position: tuple[float, float] | None = None
    rotation: float | None = None

    def locks_position(self) -> bool:
        """Return True if this lock constrains position."""
        return self.lock_type in (LockType.POSITION, LockType.FULL)

    def locks_rotation(self) -> bool:
        """Return True if this lock constrains rotation."""
        return self.lock_type in (LockType.ROTATION, LockType.FULL)


@dataclass
class NetRouteLock:
    """
    A lock on a routed net's trace geometry.

    Preserves the exact routing path of a net so subsequent routing
    passes don't modify it. Useful for timing-critical signals, length-matched
    pairs, and manually optimized routes.

    Attributes:
        net_name: Net name (e.g., "MCLK_MCU", "+3.3V")
        reason: Human-readable explanation of why the route is locked
        locked_by: Identifier of the agent/optimizer that created the lock
        timestamp: When the lock was created
        trace_geometry: List of trace segments as (x1, y1, x2, y2, layer, width)
        via_positions: List of via positions as (x, y, layers)
    """

    net_name: str
    reason: str
    locked_by: str
    timestamp: datetime = field(default_factory=datetime.now)
    trace_geometry: list[tuple[float, float, float, float, str, float]] = field(
        default_factory=list
    )
    via_positions: list[tuple[float, float, tuple[str, str]]] = field(default_factory=list)


@dataclass
class RegionConstraint:
    """
    A constraint defining a region with allowed/disallowed components or nets.

    Used for analog/digital domain separation, power plane regions,
    and keep-out zones.

    Attributes:
        name: Human-readable region name (e.g., "analog_domain")
        bounds: Region bounds as dict with x_min, x_max, y_min, y_max
        allowed_nets: List of net names allowed in this region (empty = all allowed)
        disallowed_nets: List of net names not allowed in this region
        allowed_components: List of component refs allowed in this region
        disallowed_components: List of component refs not allowed in this region
        reason: Human-readable explanation of the region constraint
        locked_by: Identifier of the agent/optimizer that created the constraint
    """

    name: str
    bounds: dict[str, float]
    reason: str
    locked_by: str = ""
    allowed_nets: list[str] = field(default_factory=list)
    disallowed_nets: list[str] = field(default_factory=list)
    allowed_components: list[str] = field(default_factory=list)
    disallowed_components: list[str] = field(default_factory=list)

    def contains_point(self, x: float, y: float) -> bool:
        """Return True if the point is within the region bounds."""
        return self.bounds.get("x_min", float("-inf")) <= x <= self.bounds.get(
            "x_max", float("inf")
        ) and self.bounds.get("y_min", float("-inf")) <= y <= self.bounds.get("y_max", float("inf"))

    def is_net_allowed(self, net_name: str) -> bool:
        """Return True if the net is allowed in this region."""
        if net_name in self.disallowed_nets:
            return False
        if self.allowed_nets and net_name not in self.allowed_nets:
            return False
        return True

    def is_component_allowed(self, ref: str) -> bool:
        """Return True if the component is allowed in this region."""
        if ref in self.disallowed_components:
            return False
        if self.allowed_components and ref not in self.allowed_components:
            return False
        return True


@dataclass
class RelativeConstraint:
    """
    A constraint on the relative position between two components.

    Used for decoupling capacitors near ICs, matched pairs, and
    other proximity requirements.

    Attributes:
        ref1: First component reference designator
        relation: Type of relationship ("near", "aligned", "symmetric")
        ref2: Second component reference designator
        max_distance: Maximum allowed distance in mm (for "near")
        reason: Human-readable explanation
        locked_by: Identifier of the agent/optimizer that created the constraint
    """

    ref1: str
    relation: str
    ref2: str
    max_distance: float | None = None
    reason: str = ""
    locked_by: str = ""

    def check_satisfied(
        self, pos1: tuple[float, float], pos2: tuple[float, float]
    ) -> tuple[bool, str]:
        """
        Check if the constraint is satisfied given component positions.

        Args:
            pos1: Position of first component (x, y)
            pos2: Position of second component (x, y)

        Returns:
            Tuple of (is_satisfied, violation_message)
        """
        import math

        dx = pos2[0] - pos1[0]
        dy = pos2[1] - pos1[1]
        distance = math.sqrt(dx * dx + dy * dy)

        if self.relation == "near" and self.max_distance is not None:
            if distance > self.max_distance:
                return False, (
                    f"{self.ref1} must be within {self.max_distance}mm of {self.ref2}, "
                    f"but distance is {distance:.2f}mm"
                )
            return True, ""

        # Other relations can be added (aligned, symmetric, etc.)
        return True, ""
