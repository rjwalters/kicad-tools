"""
Pattern schema definitions for PCB placement rules.

This module defines the data structures for specifying PCB placement patterns,
including placement rules and routing constraints that can be used to guide
automated PCB layout.

Example::

    from kicad_tools.patterns.schema import PlacementRule, PatternSpec

    # Define a placement rule for an input capacitor
    rule = PlacementRule(
        component="input_cap",
        relative_to="regulator",
        max_distance_mm=3.0,
        preferred_angle=180.0,
        rationale="Input cap within 3mm of VIN pin",
    )

    # Define a complete pattern specification
    spec = PatternSpec(
        name="ldo_regulator",
        components=["regulator", "input_cap", "output_cap_1", "output_cap_2"],
        placement_rules=[rule],
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class PlacementPriority(Enum):
    """Priority level for placement rules."""

    CRITICAL = "critical"  # Must be satisfied (e.g., thermal, EMI)
    HIGH = "high"  # Strongly recommended
    MEDIUM = "medium"  # Preferred but flexible
    LOW = "low"  # Nice to have


@dataclass
class Placement:
    """A computed placement for a component.

    Attributes:
        position: (x, y) coordinates in mm
        rotation: Rotation angle in degrees (0, 90, 180, 270)
        rationale: Human-readable explanation for this placement
        layer: PCB layer (e.g., "F.Cu", "B.Cu")
    """

    position: tuple[float, float]
    rotation: float = 0.0
    rationale: str = ""
    layer: str = "F.Cu"


@dataclass
class PlacementRule:
    """Rule for placing a component relative to an anchor.

    Defines how a component should be positioned relative to another
    component (the anchor). Rules can specify distance constraints,
    preferred angles, and routing considerations.

    Attributes:
        component: Reference or role name (e.g., "input_cap", "C_IN")
        relative_to: Anchor component to position relative to
        max_distance_mm: Maximum allowed distance from anchor
        min_distance_mm: Minimum required distance from anchor
        preferred_angle: Preferred angle in degrees (0=right, 90=down, 180=left, 270=up)
        angle_tolerance: Allowed deviation from preferred angle
        rationale: Human-readable explanation for this rule
        priority: How important this rule is
        same_layer: Whether component must be on same layer as anchor
    """

    component: str
    relative_to: str
    max_distance_mm: float
    min_distance_mm: float = 0.0
    preferred_angle: float | None = None
    angle_tolerance: float = 45.0
    rationale: str = ""
    priority: PlacementPriority = PlacementPriority.HIGH
    same_layer: bool = True


@dataclass
class RoutingConstraint:
    """Routing constraint for pattern connections.

    Defines constraints for routing traces between components in a pattern,
    such as trace width, via requirements, and keep-out zones.

    Attributes:
        net_role: Role name for the net (e.g., "vin_power", "gnd_return")
        min_width_mm: Minimum trace width
        max_length_mm: Maximum trace length (for timing-critical signals)
        via_allowed: Whether vias are permitted
        plane_connection: Whether connection should be to a plane
        rationale: Human-readable explanation
    """

    net_role: str
    min_width_mm: float = 0.2
    max_length_mm: float | None = None
    via_allowed: bool = True
    plane_connection: bool = False
    rationale: str = ""


@dataclass
class PatternSpec:
    """Complete pattern specification for PCB placement.

    A PatternSpec defines all the components, placement rules, routing
    constraints, and validation checks for a common circuit pattern
    (like an LDO with decoupling capacitors).

    Attributes:
        name: Pattern identifier (e.g., "ldo_regulator", "buck_converter")
        description: Human-readable description of the pattern
        components: List of component roles in the pattern
        placement_rules: Rules for positioning components
        routing_constraints: Constraints for trace routing
        validation_checks: Optional validation functions
    """

    name: str
    description: str = ""
    components: list[str] = field(default_factory=list)
    placement_rules: list[PlacementRule] = field(default_factory=list)
    routing_constraints: list[RoutingConstraint] = field(default_factory=list)
    validation_checks: list[Callable] = field(default_factory=list)

    def get_rules_for_component(self, component: str) -> list[PlacementRule]:
        """Get all placement rules for a specific component.

        Args:
            component: Component role name

        Returns:
            List of placement rules applicable to this component
        """
        return [r for r in self.placement_rules if r.component == component]

    def get_routing_for_net(self, net_role: str) -> RoutingConstraint | None:
        """Get routing constraint for a specific net role.

        Args:
            net_role: Net role name

        Returns:
            Routing constraint if defined, None otherwise
        """
        for constraint in self.routing_constraints:
            if constraint.net_role == net_role:
                return constraint
        return None


@dataclass
class PatternViolation:
    """A violation detected during pattern validation.

    Attributes:
        rule: The rule that was violated (or None for general violations)
        component: Component that caused the violation
        message: Human-readable description of the violation
        severity: How serious the violation is
        actual_value: The actual measured value (if applicable)
        expected_value: The expected/limit value (if applicable)
    """

    rule: PlacementRule | RoutingConstraint | None
    component: str
    message: str
    severity: PlacementPriority = PlacementPriority.HIGH
    actual_value: float | None = None
    expected_value: float | None = None
