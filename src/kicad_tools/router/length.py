"""
Length constraint tracking and tuning for timing-critical nets.

This module provides:
- LengthViolation: Represents a length constraint violation
- LengthTracker: Tracks route lengths and validates against constraints
- LengthTuner: Adjusts routes to meet length constraints

Use cases:
- DDR memory buses: Data lines must match clock ±50mil
- Differential pairs: P/N must match within 5mil
- Parallel buses: All bits should be similar length
- Clock distribution: Equal path lengths to all loads
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Route
    from .rules import LengthConstraint


class ViolationType(Enum):
    """Type of length constraint violation."""

    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    MISMATCH = "mismatch"


@dataclass
class LengthViolation:
    """Represents a length constraint violation.

    Attributes:
        net_id: Net ID or match group name that has the violation
        violation_type: Type of violation (too_short, too_long, mismatch)
        actual_length: Actual measured length(s) in mm
        target_length: Target or expected length in mm (for min/max violations)
        delta: Amount by which the constraint is violated in mm
        match_group: Match group name if this is a mismatch violation
    """

    net_id: int | str
    violation_type: ViolationType
    actual_length: float | list[float]
    target_length: float | None = None
    delta: float = 0.0
    match_group: str | None = None

    def __str__(self) -> str:
        if self.violation_type == ViolationType.TOO_SHORT:
            return (
                f"Net {self.net_id}: too short ({self.actual_length:.3f}mm, "
                f"min: {self.target_length:.3f}mm, delta: {self.delta:.3f}mm)"
            )
        elif self.violation_type == ViolationType.TOO_LONG:
            return (
                f"Net {self.net_id}: too long ({self.actual_length:.3f}mm, "
                f"max: {self.target_length:.3f}mm, delta: {self.delta:.3f}mm)"
            )
        else:  # MISMATCH
            lengths = (
                self.actual_length if isinstance(self.actual_length, list) else [self.actual_length]
            )
            return (
                f"Match group '{self.match_group}': length mismatch "
                f"(range: {min(lengths):.3f}-{max(lengths):.3f}mm, delta: {self.delta:.3f}mm)"
            )


@dataclass
class LengthTracker:
    """Tracks route lengths and validates against constraints.

    This class maintains a record of routed net lengths and validates
    them against length constraints including min/max limits and
    match group requirements.

    Attributes:
        constraints: List of length constraints to track
        lengths: Dictionary mapping net ID to measured length in mm
        match_groups: Dictionary mapping group name to list of net IDs
    """

    constraints: list[LengthConstraint] = field(default_factory=list)
    lengths: dict[int, float] = field(default_factory=dict)
    match_groups: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))

    def __post_init__(self):
        """Build match groups from constraints."""
        self._constraint_map: dict[int, LengthConstraint] = {}
        for c in self.constraints:
            self._constraint_map[c.net_id] = c
            if c.match_group:
                self.match_groups[c.match_group].append(c.net_id)

    def add_constraint(self, constraint: LengthConstraint) -> None:
        """Add a length constraint.

        Args:
            constraint: LengthConstraint to add
        """
        self.constraints.append(constraint)
        self._constraint_map[constraint.net_id] = constraint
        if constraint.match_group:
            self.match_groups[constraint.match_group].append(constraint.net_id)

    def record_route(self, net_id: int, route: Route) -> float:
        """Record the length of a routed net.

        Args:
            net_id: Net ID
            route: Route object containing segments

        Returns:
            Total route length in mm
        """
        length = self.calculate_route_length(route)
        self.lengths[net_id] = length
        return length

    def record_length(self, net_id: int, length: float) -> None:
        """Record a pre-calculated length for a net.

        Args:
            net_id: Net ID
            length: Route length in mm
        """
        self.lengths[net_id] = length

    @staticmethod
    def calculate_route_length(route: Route) -> float:
        """Calculate the total length of a route.

        Args:
            route: Route object containing segments

        Returns:
            Total route length in mm
        """
        total = 0.0
        for seg in route.segments:
            dx = seg.x2 - seg.x1
            dy = seg.y2 - seg.y1
            total += math.sqrt(dx * dx + dy * dy)
        return total

    def get_length(self, net_id: int) -> float | None:
        """Get the recorded length for a net.

        Args:
            net_id: Net ID

        Returns:
            Route length in mm, or None if not recorded
        """
        return self.lengths.get(net_id)

    def get_constraint(self, net_id: int) -> LengthConstraint | None:
        """Get the constraint for a net.

        Args:
            net_id: Net ID

        Returns:
            LengthConstraint if one exists, None otherwise
        """
        return self._constraint_map.get(net_id)

    def get_violations(self) -> list[LengthViolation]:
        """Check all constraints and return violations.

        Returns:
            List of LengthViolation objects for any violated constraints
        """
        violations: list[LengthViolation] = []

        # Check min/max constraints
        for net_id, length in self.lengths.items():
            constraint = self._constraint_map.get(net_id)
            if not constraint:
                continue

            if constraint.min_length is not None and length < constraint.min_length:
                violations.append(
                    LengthViolation(
                        net_id=net_id,
                        violation_type=ViolationType.TOO_SHORT,
                        actual_length=length,
                        target_length=constraint.min_length,
                        delta=constraint.min_length - length,
                    )
                )

            if constraint.max_length is not None and length > constraint.max_length:
                violations.append(
                    LengthViolation(
                        net_id=net_id,
                        violation_type=ViolationType.TOO_LONG,
                        actual_length=length,
                        target_length=constraint.max_length,
                        delta=length - constraint.max_length,
                    )
                )

        # Check match groups
        for group_name, net_ids in self.match_groups.items():
            group_lengths = [self.lengths.get(n, 0.0) for n in net_ids if n in self.lengths]
            if len(group_lengths) < 2:
                continue

            max_len = max(group_lengths)
            min_len = min(group_lengths)
            delta = max_len - min_len

            # Get tolerance from first net's constraint
            tolerance = 0.5  # Default
            if net_ids and net_ids[0] in self._constraint_map:
                tolerance = self._constraint_map[net_ids[0]].match_tolerance

            if delta > tolerance:
                violations.append(
                    LengthViolation(
                        net_id=group_name,
                        violation_type=ViolationType.MISMATCH,
                        actual_length=group_lengths,
                        delta=delta,
                        match_group=group_name,
                    )
                )

        return violations

    def get_match_group_target(self, group_name: str) -> float | None:
        """Get the target length for a match group (longest net).

        For match groups, we can only add length (serpentines), not remove it.
        So the target length is the longest net in the group.

        Args:
            group_name: Name of the match group

        Returns:
            Target length in mm (longest net's length), or None if group not found
        """
        net_ids = self.match_groups.get(group_name, [])
        if not net_ids:
            return None

        lengths = [self.lengths.get(n, 0.0) for n in net_ids if n in self.lengths]
        return max(lengths) if lengths else None

    def get_length_needed(self, net_id: int) -> float:
        """Get additional length needed to meet constraints.

        Args:
            net_id: Net ID

        Returns:
            Additional length needed in mm (positive), or 0.0 if constraint met
        """
        current = self.lengths.get(net_id, 0.0)
        constraint = self._constraint_map.get(net_id)

        if not constraint:
            return 0.0

        needed = 0.0

        # Check min length
        if constraint.min_length is not None and current < constraint.min_length:
            needed = max(needed, constraint.min_length - current)

        # Check match group
        if constraint.match_group:
            target = self.get_match_group_target(constraint.match_group)
            if target is not None and current < target - constraint.match_tolerance:
                needed = max(needed, target - current)

        return needed

    def get_statistics(self) -> dict:
        """Get statistics about tracked lengths.

        Returns:
            Dictionary with length statistics
        """
        if not self.lengths:
            return {
                "total_nets": 0,
                "constrained_nets": 0,
                "match_groups": 0,
                "violations": 0,
            }

        violations = self.get_violations()

        return {
            "total_nets": len(self.lengths),
            "constrained_nets": len(self._constraint_map),
            "match_groups": len(self.match_groups),
            "violations": len(violations),
            "min_length": min(self.lengths.values()),
            "max_length": max(self.lengths.values()),
            "avg_length": sum(self.lengths.values()) / len(self.lengths),
        }

    def clear(self) -> None:
        """Clear all recorded lengths (keeps constraints)."""
        self.lengths.clear()


def create_match_group(
    name: str,
    net_ids: list[int],
    tolerance: float = 0.5,
    min_length: float | None = None,
    max_length: float | None = None,
) -> list[LengthConstraint]:
    """Create length constraints for a match group.

    This is a convenience function for creating multiple constraints
    that all belong to the same match group.

    Args:
        name: Match group name
        net_ids: List of net IDs in the group
        tolerance: Length match tolerance in mm
        min_length: Minimum length for all nets (optional)
        max_length: Maximum length for all nets (optional)

    Returns:
        List of LengthConstraint objects

    Example:
        >>> constraints = create_match_group(
        ...     "DDR_DATA",
        ...     [100, 101, 102, 103],
        ...     tolerance=0.5,
        ... )
        >>> tracker = LengthTracker(constraints)
    """
    from .rules import LengthConstraint

    return [
        LengthConstraint(
            net_id=net_id,
            min_length=min_length,
            max_length=max_length,
            match_group=name,
            match_tolerance=tolerance,
        )
        for net_id in net_ids
    ]
