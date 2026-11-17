"""
Grouping constraints for component placement optimization.

Provides constraint definitions that control spatial relationships between
components during placement optimization. Constraints can specify:
- Maximum distance from an anchor component
- Alignment along an axis
- Ordering along an axis
- Containment within a bounding box
- Relative position to a reference component
"""

from __future__ import annotations

import fnmatch
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.optim.components import Component


__all__ = [
    "ConstraintType",
    "SpatialConstraint",
    "GroupingConstraint",
    "ConstraintViolation",
    "validate_grouping_constraints",
    "expand_member_patterns",
]


class ConstraintType(Enum):
    """Types of spatial constraints."""

    MAX_DISTANCE = "max_distance"
    ALIGNMENT = "alignment"
    ORDERING = "ordering"
    WITHIN_BOX = "within_box"
    RELATIVE_POSITION = "relative_position"


@dataclass
class SpatialConstraint:
    """
    Individual spatial constraint within a group.

    Defines a specific spatial relationship that components must satisfy.
    """

    constraint_type: ConstraintType
    parameters: dict = field(default_factory=dict)

    @classmethod
    def max_distance(cls, anchor: str, radius_mm: float) -> SpatialConstraint:
        """Create a max distance constraint: all members within radius of anchor."""
        return cls(
            constraint_type=ConstraintType.MAX_DISTANCE,
            parameters={"anchor": anchor, "radius_mm": radius_mm},
        )

    @classmethod
    def alignment(cls, axis: str, tolerance_mm: float = 0.5) -> SpatialConstraint:
        """Create an alignment constraint: members aligned on axis (horizontal/vertical)."""
        return cls(
            constraint_type=ConstraintType.ALIGNMENT,
            parameters={"axis": axis, "tolerance_mm": tolerance_mm},
        )

    @classmethod
    def ordering(cls, axis: str, order: list[str]) -> SpatialConstraint:
        """Create an ordering constraint: members in specific order along axis."""
        return cls(
            constraint_type=ConstraintType.ORDERING,
            parameters={"axis": axis, "order": order},
        )

    @classmethod
    def within_box(cls, x: float, y: float, width: float, height: float) -> SpatialConstraint:
        """Create a bounding box constraint: members contained in box."""
        return cls(
            constraint_type=ConstraintType.WITHIN_BOX,
            parameters={"x": x, "y": y, "width": width, "height": height},
        )

    @classmethod
    def relative_position(
        cls, reference: str, dx: float, dy: float, tolerance_mm: float = 0.5
    ) -> SpatialConstraint:
        """Create a relative position constraint: specific offset from reference."""
        return cls(
            constraint_type=ConstraintType.RELATIVE_POSITION,
            parameters={
                "reference": reference,
                "dx": dx,
                "dy": dy,
                "tolerance_mm": tolerance_mm,
            },
        )


@dataclass
class GroupingConstraint:
    """
    Defines a component group with spatial constraints.

    Members can be specified as exact reference designators or glob patterns
    (e.g., "LED*" for all LEDs, "C1?" for C10-C19).
    """

    name: str
    members: list[str]  # Reference designators or patterns
    constraints: list[SpatialConstraint] = field(default_factory=list)

    def get_resolved_members(self, all_refs: list[str]) -> list[str]:
        """
        Resolve member patterns to actual component references.

        Args:
            all_refs: List of all component reference designators

        Returns:
            List of resolved reference designators
        """
        return expand_member_patterns(self.members, all_refs)


@dataclass
class ConstraintViolation:
    """
    A violation of a grouping constraint.

    Records details about what constraint was violated and by how much.
    """

    group_name: str
    constraint_type: ConstraintType
    message: str
    components: list[str]
    severity: float = 0.0  # How far from satisfying (0 = satisfied)

    def __str__(self) -> str:
        return f"[{self.group_name}] {self.constraint_type.value}: {self.message}"


def expand_member_patterns(patterns: list[str], all_refs: list[str]) -> list[str]:
    """
    Expand glob patterns to actual reference designators.

    Supports:
    - Exact matches: "LED1" -> ["LED1"]
    - Glob patterns: "LED*" -> ["LED1", "LED2", ...]
    - Character classes: "C1?" -> ["C10", "C11", ...]
    - Ranges: "R[1-5]" -> ["R1", "R2", "R3", "R4", "R5"]

    Args:
        patterns: List of patterns or exact references
        all_refs: List of all available reference designators

    Returns:
        List of matched reference designators (deduplicated, order preserved)
    """
    result = []
    seen = set()

    for pattern in patterns:
        # Check if pattern contains glob characters
        if any(c in pattern for c in "*?["):
            # Use fnmatch for glob matching
            for ref in all_refs:
                if fnmatch.fnmatch(ref, pattern):
                    if ref not in seen:
                        result.append(ref)
                        seen.add(ref)
        else:
            # Exact match
            if pattern in all_refs and pattern not in seen:
                result.append(pattern)
                seen.add(pattern)

    return result


def validate_grouping_constraints(
    components: list[Component],
    constraints: list[GroupingConstraint],
) -> list[ConstraintViolation]:
    """
    Check if current placement satisfies constraints.

    Args:
        components: List of all components with current positions
        constraints: List of grouping constraints to validate

    Returns:
        List of constraint violations (empty if all satisfied)
    """
    violations = []
    comp_map = {comp.ref: comp for comp in components}
    all_refs = list(comp_map.keys())

    for group in constraints:
        members = group.get_resolved_members(all_refs)
        member_comps = [comp_map[ref] for ref in members if ref in comp_map]

        if not member_comps:
            continue

        for constraint in group.constraints:
            violation = _validate_single_constraint(group.name, constraint, member_comps, comp_map)
            if violation:
                violations.append(violation)

    return violations


def _validate_single_constraint(
    group_name: str,
    constraint: SpatialConstraint,
    members: list[Component],
    comp_map: dict[str, Component],
) -> ConstraintViolation | None:
    """Validate a single constraint against member components."""
    if constraint.constraint_type == ConstraintType.MAX_DISTANCE:
        return _validate_max_distance(group_name, constraint, members, comp_map)
    elif constraint.constraint_type == ConstraintType.ALIGNMENT:
        return _validate_alignment(group_name, constraint, members)
    elif constraint.constraint_type == ConstraintType.ORDERING:
        return _validate_ordering(group_name, constraint, members)
    elif constraint.constraint_type == ConstraintType.WITHIN_BOX:
        return _validate_within_box(group_name, constraint, members)
    elif constraint.constraint_type == ConstraintType.RELATIVE_POSITION:
        return _validate_relative_position(group_name, constraint, members, comp_map)
    return None


def _validate_max_distance(
    group_name: str,
    constraint: SpatialConstraint,
    members: list[Component],
    comp_map: dict[str, Component],
) -> ConstraintViolation | None:
    """Validate max_distance constraint."""
    params = constraint.parameters
    anchor_ref = params["anchor"]
    radius = params["radius_mm"]

    anchor = comp_map.get(anchor_ref)
    if not anchor:
        return ConstraintViolation(
            group_name=group_name,
            constraint_type=constraint.constraint_type,
            message=f"Anchor component {anchor_ref} not found",
            components=[anchor_ref],
            severity=float("inf"),
        )

    violators = []
    max_violation = 0.0

    for comp in members:
        if comp.ref == anchor_ref:
            continue
        dist = math.sqrt((comp.x - anchor.x) ** 2 + (comp.y - anchor.y) ** 2)
        if dist > radius:
            violators.append(comp.ref)
            max_violation = max(max_violation, dist - radius)

    if violators:
        return ConstraintViolation(
            group_name=group_name,
            constraint_type=constraint.constraint_type,
            message=f"Components {violators} exceed max distance {radius}mm from {anchor_ref}",
            components=violators,
            severity=max_violation,
        )
    return None


def _validate_alignment(
    group_name: str,
    constraint: SpatialConstraint,
    members: list[Component],
) -> ConstraintViolation | None:
    """Validate alignment constraint."""
    params = constraint.parameters
    axis = params["axis"]
    tolerance = params.get("tolerance_mm", 0.5)

    if len(members) < 2:
        return None

    # Get positions along the alignment axis
    if axis == "horizontal":
        positions = [comp.y for comp in members]
    else:  # vertical
        positions = [comp.x for comp in members]

    # Check if all positions are within tolerance of each other
    min_pos = min(positions)
    max_pos = max(positions)
    spread = max_pos - min_pos

    if spread > tolerance:
        return ConstraintViolation(
            group_name=group_name,
            constraint_type=constraint.constraint_type,
            message=f"Components spread {spread:.2f}mm along {axis} axis (tolerance: {tolerance}mm)",
            components=[comp.ref for comp in members],
            severity=spread - tolerance,
        )
    return None


def _validate_ordering(
    group_name: str,
    constraint: SpatialConstraint,
    members: list[Component],
) -> ConstraintViolation | None:
    """Validate ordering constraint."""
    params = constraint.parameters
    axis = params["axis"]
    expected_order = params["order"]

    # Build map of ref -> component
    comp_by_ref = {comp.ref: comp for comp in members}

    # Get positions in expected order
    if axis == "horizontal":
        # For horizontal ordering, check x positions (left to right)
        ordered_comps = [(ref, comp_by_ref[ref].x) for ref in expected_order if ref in comp_by_ref]
    else:  # vertical
        # For vertical ordering, check y positions (top to bottom or bottom to top)
        ordered_comps = [(ref, comp_by_ref[ref].y) for ref in expected_order if ref in comp_by_ref]

    if len(ordered_comps) < 2:
        return None

    # Check if positions follow expected order
    violations = []
    for i in range(len(ordered_comps) - 1):
        ref1, pos1 = ordered_comps[i]
        ref2, pos2 = ordered_comps[i + 1]
        if pos1 >= pos2:
            violations.append((ref1, ref2))

    if violations:
        return ConstraintViolation(
            group_name=group_name,
            constraint_type=constraint.constraint_type,
            message=f"Ordering violated: {violations}",
            components=[ref for v in violations for ref in v],
            severity=float(len(violations)),
        )
    return None


def _validate_within_box(
    group_name: str,
    constraint: SpatialConstraint,
    members: list[Component],
) -> ConstraintViolation | None:
    """Validate within_box constraint."""
    params = constraint.parameters
    box_x = params["x"]
    box_y = params["y"]
    box_width = params["width"]
    box_height = params["height"]

    # Box boundaries
    x_min = box_x
    x_max = box_x + box_width
    y_min = box_y
    y_max = box_y + box_height

    violators = []
    max_violation = 0.0

    for comp in members:
        # Check if component center is within box
        in_x = x_min <= comp.x <= x_max
        in_y = y_min <= comp.y <= y_max

        if not (in_x and in_y):
            violators.append(comp.ref)
            # Calculate how far outside
            dx = max(0, x_min - comp.x, comp.x - x_max)
            dy = max(0, y_min - comp.y, comp.y - y_max)
            max_violation = max(max_violation, math.sqrt(dx * dx + dy * dy))

    if violators:
        return ConstraintViolation(
            group_name=group_name,
            constraint_type=constraint.constraint_type,
            message=f"Components {violators} are outside bounding box",
            components=violators,
            severity=max_violation,
        )
    return None


def _validate_relative_position(
    group_name: str,
    constraint: SpatialConstraint,
    members: list[Component],
    comp_map: dict[str, Component],
) -> ConstraintViolation | None:
    """Validate relative_position constraint."""
    params = constraint.parameters
    reference_ref = params["reference"]
    dx = params["dx"]
    dy = params["dy"]
    tolerance = params.get("tolerance_mm", 0.5)

    reference = comp_map.get(reference_ref)
    if not reference:
        return ConstraintViolation(
            group_name=group_name,
            constraint_type=constraint.constraint_type,
            message=f"Reference component {reference_ref} not found",
            components=[reference_ref],
            severity=float("inf"),
        )

    # Expected position
    expected_x = reference.x + dx
    expected_y = reference.y + dy

    violators = []
    max_violation = 0.0

    for comp in members:
        if comp.ref == reference_ref:
            continue
        actual_dx = comp.x - expected_x
        actual_dy = comp.y - expected_y
        error = math.sqrt(actual_dx * actual_dx + actual_dy * actual_dy)

        if error > tolerance:
            violators.append(comp.ref)
            max_violation = max(max_violation, error - tolerance)

    if violators:
        return ConstraintViolation(
            group_name=group_name,
            constraint_type=constraint.constraint_type,
            message=f"Components {violators} not at expected offset ({dx}, {dy}) from {reference_ref}",
            components=violators,
            severity=max_violation,
        )
    return None
