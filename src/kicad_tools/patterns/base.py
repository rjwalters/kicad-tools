"""
Base class for PCB design patterns.

This module provides the abstract base class for PCB design patterns that
encapsulate placement rules, routing constraints, and validation logic
for common circuit topologies.

Example::

    from kicad_tools.patterns.base import PCBPattern
    from kicad_tools.patterns.schema import Placement, PlacementRule

    class LDOPattern(PCBPattern):
        def get_placements(self, anchor_at: tuple[float, float]) -> dict[str, Placement]:
            # Calculate positions for input cap, output caps, etc.
            ...

        def validate(self, pcb) -> list[PatternViolation]:
            # Check if pattern is correctly implemented
            ...
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .schema import (
    PatternSpec,
    PatternViolation,
    Placement,
    PlacementRule,
    RoutingConstraint,
)

if TYPE_CHECKING:
    from pathlib import Path


class PCBPattern(ABC):
    """Abstract base class for PCB design patterns.

    PCB patterns encapsulate the placement rules and routing constraints
    for common circuit topologies. Each pattern knows how to:
    - Calculate optimal component placements relative to an anchor
    - Validate that a PCB layout follows the pattern rules
    - Provide routing constraints for the pattern's nets

    Subclasses must implement:
    - get_placements(): Calculate component positions
    - validate(): Check if a PCB follows the pattern

    Attributes:
        spec: The pattern specification with rules and constraints
        component_map: Mapping from role names to actual component references
    """

    def __init__(self, **kwargs: object) -> None:
        """Initialize the pattern with configuration.

        Args:
            **kwargs: Pattern-specific configuration parameters
        """
        self._config = kwargs
        self._spec: PatternSpec | None = None
        self.component_map: dict[str, str] = {}

    @property
    def spec(self) -> PatternSpec:
        """Get the pattern specification.

        Override this in subclasses to define the pattern's rules.
        """
        if self._spec is None:
            self._spec = self._build_spec()
        return self._spec

    @abstractmethod
    def _build_spec(self) -> PatternSpec:
        """Build the pattern specification.

        Subclasses must implement this to define:
        - Component roles
        - Placement rules
        - Routing constraints

        Returns:
            PatternSpec defining the pattern
        """

    @abstractmethod
    def get_placements(self, anchor_at: tuple[float, float]) -> dict[str, Placement]:
        """Calculate optimal placements for all components.

        Given an anchor position (typically the main component like a
        regulator IC), calculate the recommended positions for all
        other components in the pattern.

        Args:
            anchor_at: (x, y) position of the anchor component in mm

        Returns:
            Dictionary mapping component roles to Placement objects
        """

    @abstractmethod
    def validate(self, pcb_path: Path | str) -> list[PatternViolation]:
        """Validate that a PCB layout follows this pattern.

        Checks component positions, distances, and routing against
        the pattern's rules and constraints.

        Args:
            pcb_path: Path to the KiCad PCB file

        Returns:
            List of violations found. Empty list if pattern is valid.
        """

    def get_routing_constraints(self) -> list[RoutingConstraint]:
        """Get routing constraints for this pattern.

        Returns:
            List of routing constraints from the pattern spec
        """
        return self.spec.routing_constraints

    def set_component_map(self, mapping: dict[str, str]) -> None:
        """Set the mapping from role names to actual references.

        Args:
            mapping: Dict mapping role names (e.g., "input_cap") to
                    actual references (e.g., "C1")
        """
        self.component_map = mapping

    def _calculate_position(
        self,
        anchor: tuple[float, float],
        distance_mm: float,
        angle_degrees: float,
    ) -> tuple[float, float]:
        """Calculate position at given distance and angle from anchor.

        Args:
            anchor: (x, y) anchor position
            distance_mm: Distance from anchor in mm
            angle_degrees: Angle in degrees (0=right, 90=down, 180=left, 270=up)

        Returns:
            (x, y) calculated position
        """
        angle_rad = math.radians(angle_degrees)
        x = anchor[0] + distance_mm * math.cos(angle_rad)
        y = anchor[1] + distance_mm * math.sin(angle_rad)
        return (x, y)

    def _measure_distance(
        self,
        pos1: tuple[float, float],
        pos2: tuple[float, float],
    ) -> float:
        """Calculate distance between two positions.

        Args:
            pos1: First position (x, y)
            pos2: Second position (x, y)

        Returns:
            Distance in mm
        """
        dx = pos2[0] - pos1[0]
        dy = pos2[1] - pos1[1]
        return math.sqrt(dx * dx + dy * dy)

    def _validate_placement_rule(
        self,
        rule: PlacementRule,
        component_pos: tuple[float, float],
        anchor_pos: tuple[float, float],
    ) -> PatternViolation | None:
        """Validate a single placement rule.

        Args:
            rule: The placement rule to validate
            component_pos: Position of the component
            anchor_pos: Position of the anchor

        Returns:
            PatternViolation if rule is violated, None otherwise
        """
        distance = self._measure_distance(component_pos, anchor_pos)

        if distance > rule.max_distance_mm:
            return PatternViolation(
                rule=rule,
                component=rule.component,
                message=(
                    f"{rule.component} is too far from {rule.relative_to}: "
                    f"{distance:.2f}mm > {rule.max_distance_mm:.2f}mm. "
                    f"{rule.rationale}"
                ),
                severity=rule.priority,
                actual_value=distance,
                expected_value=rule.max_distance_mm,
            )

        if distance < rule.min_distance_mm:
            return PatternViolation(
                rule=rule,
                component=rule.component,
                message=(
                    f"{rule.component} is too close to {rule.relative_to}: "
                    f"{distance:.2f}mm < {rule.min_distance_mm:.2f}mm"
                ),
                severity=rule.priority,
                actual_value=distance,
                expected_value=rule.min_distance_mm,
            )

        return None
