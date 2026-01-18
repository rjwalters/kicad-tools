"""
Zone-based component placement for PCB layout.

Provides a high-level API for defining placement zones and assigning
components to them. Components within a zone are constrained to stay
within the zone's bounding box during optimization.

Example::

    from kicad_tools.optim import PlacementOptimizer
    from kicad_tools.optim.zones import PlacementZone, assign_zone

    optimizer = PlacementOptimizer.from_pcb(pcb)

    # Define zones
    zone = PlacementZone("supercaps", x=10, y=10, width=120, height=50)

    # Assign components using regex pattern
    assign_zone(optimizer, zone, pattern=r"C1[0-6][0-9]")  # C100-C169

    # Or assign by reference list
    assign_zone(optimizer, zone, references=["C101", "C102", "C103"])

    optimizer.run()
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.optim.constraints import GroupingConstraint, SpatialConstraint

if TYPE_CHECKING:
    from kicad_tools.optim.placement import PlacementOptimizer

__all__ = [
    "PlacementZone",
    "assign_zone",
    "expand_regex_pattern",
]


@dataclass
class PlacementZone:
    """
    A named rectangular zone for component placement.

    Components assigned to this zone will be constrained to stay within
    the zone's bounding box during optimization.

    Attributes:
        name: Unique identifier for the zone
        x: X coordinate of the zone's bottom-left corner (mm)
        y: Y coordinate of the zone's bottom-left corner (mm)
        width: Zone width (mm)
        height: Zone height (mm)
        assigned_components: List of component references assigned to this zone
    """

    name: str
    x: float
    y: float
    width: float
    height: float
    assigned_components: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.width <= 0:
            raise ValueError(f"Zone width must be positive, got {self.width}")
        if self.height <= 0:
            raise ValueError(f"Zone height must be positive, got {self.height}")

    @property
    def x_max(self) -> float:
        """Right edge X coordinate."""
        return self.x + self.width

    @property
    def y_max(self) -> float:
        """Top edge Y coordinate."""
        return self.y + self.height

    @property
    def center(self) -> tuple[float, float]:
        """Zone center coordinates."""
        return (self.x + self.width / 2, self.y + self.height / 2)

    def contains_point(self, x: float, y: float) -> bool:
        """Check if a point is within the zone."""
        return self.x <= x <= self.x_max and self.y <= y <= self.y_max

    def to_constraint(self) -> SpatialConstraint:
        """Convert zone to a WITHIN_BOX spatial constraint."""
        return SpatialConstraint.within_box(
            x=self.x,
            y=self.y,
            width=self.width,
            height=self.height,
        )


def expand_regex_pattern(pattern: str, all_refs: list[str]) -> list[str]:
    """
    Expand a regex pattern to match component references.

    Supports full Python regex syntax for complex patterns like:
    - "C1[0-6][0-9]" matches C100-C169
    - "R[0-9]+" matches R1, R12, R123, etc.
    - "U[1-3]" matches U1, U2, U3
    - "Q[1-4]" matches Q1, Q2, Q3, Q4

    Args:
        pattern: Regular expression pattern to match
        all_refs: List of all component reference designators

    Returns:
        List of matched reference designators (order preserved)
    """
    try:
        regex = re.compile(f"^{pattern}$")
    except re.error as e:
        raise ValueError(f"Invalid regex pattern '{pattern}': {e}") from e

    return [ref for ref in all_refs if regex.match(ref)]


def assign_zone(
    optimizer: PlacementOptimizer,
    zone: PlacementZone,
    *,
    pattern: str | None = None,
    references: list[str] | None = None,
) -> list[str]:
    """
    Assign components to a placement zone.

    Components can be assigned by regex pattern or explicit reference list.
    The zone is converted to a WITHIN_BOX grouping constraint that will
    keep assigned components within the zone during optimization.

    Args:
        optimizer: PlacementOptimizer instance to add zone constraint to
        zone: PlacementZone defining the target area
        pattern: Regex pattern to match component references (e.g., "C1[0-6][0-9]")
        references: Explicit list of component references to assign

    Returns:
        List of component references that were assigned to the zone

    Raises:
        ValueError: If neither pattern nor references is specified,
                   or if the pattern is invalid
    """
    if pattern is None and references is None:
        raise ValueError("Must specify either 'pattern' or 'references'")

    # Get all component references
    all_refs = [comp.ref for comp in optimizer.components]

    # Resolve components to assign
    assigned: list[str] = []

    if references:
        # Use explicit references (filter to those that exist)
        assigned.extend(ref for ref in references if ref in all_refs)

    if pattern:
        # Use regex pattern matching
        matched = expand_regex_pattern(pattern, all_refs)
        # Add matched refs that aren't already assigned
        for ref in matched:
            if ref not in assigned:
                assigned.append(ref)

    if not assigned:
        return []

    # Update zone's assigned components list
    zone.assigned_components.extend(ref for ref in assigned if ref not in zone.assigned_components)

    # Create grouping constraint with WITHIN_BOX
    constraint = GroupingConstraint(
        name=f"zone_{zone.name}",
        members=assigned,
        constraints=[zone.to_constraint()],
    )

    # Add constraint to optimizer
    optimizer.add_grouping_constraint(constraint)

    return assigned
