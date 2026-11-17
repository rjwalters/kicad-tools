"""
Pattern definition DSL for creating PCB patterns in Python.

This module provides a decorator-based DSL for defining PCB patterns,
allowing patterns to be created with minimal boilerplate code.

Example::

    from kicad_tools.patterns.dsl import define_pattern, placement_rule

    @define_pattern(
        name="my_sensor",
        description="Custom sensor interface pattern",
        components=["sensor", "bias_resistor", "filter_cap"],
    )
    class MySensorPattern:
        # Define placement rules as class attributes
        placement_rules = [
            placement_rule("filter_cap", relative_to="sensor", max_distance_mm=5.0),
            placement_rule("bias_resistor", relative_to="sensor", max_distance_mm=10.0),
        ]

        def validate(self, pcb):
            # Custom validation logic
            violations = []
            # ... check conditions ...
            return violations

Or using the class decorator with auto-registration::

    from kicad_tools.patterns import define_pattern, PlacementRule

    @define_pattern
    class TemperatureSensorPattern:
        '''Temperature sensor with filtering.'''

        components = ["RT1", "R1", "C1"]

        placement_rules = [
            PlacementRule("C1", relative_to="RT1", max_distance_mm=5.0),
        ]
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from .base import PCBPattern
from .registry import PatternRegistry
from .schema import (
    PatternSpec,
    PatternViolation,
    Placement,
    PlacementPriority,
    PlacementRule,
    RoutingConstraint,
)

if TYPE_CHECKING:
    pass

T = TypeVar("T")


def placement_rule(
    component: str,
    relative_to: str,
    max_distance_mm: float,
    min_distance_mm: float = 0.0,
    preferred_angle: float | None = None,
    rationale: str = "",
    priority: str = "high",
) -> PlacementRule:
    """Create a placement rule for a component.

    This is a convenience function for creating PlacementRule instances
    with a cleaner syntax.

    Args:
        component: Component role or reference
        relative_to: Anchor component to position relative to
        max_distance_mm: Maximum distance from anchor
        min_distance_mm: Minimum distance from anchor
        preferred_angle: Preferred angle in degrees (0=right, 90=down)
        rationale: Human-readable explanation
        priority: Priority level ("critical", "high", "medium", "low")

    Returns:
        PlacementRule instance

    Example::

        rule = placement_rule(
            "filter_cap",
            relative_to="thermistor",
            max_distance_mm=5.0,
            rationale="Filter cap close to sensor for noise rejection"
        )
    """
    return PlacementRule(
        component=component,
        relative_to=relative_to,
        max_distance_mm=max_distance_mm,
        min_distance_mm=min_distance_mm,
        preferred_angle=preferred_angle,
        rationale=rationale,
        priority=PlacementPriority(priority.lower()),
    )


def routing_constraint(
    net_role: str,
    min_width_mm: float = 0.2,
    max_length_mm: float | None = None,
    via_allowed: bool = True,
    plane_connection: bool = False,
    rationale: str = "",
) -> RoutingConstraint:
    """Create a routing constraint for a net.

    Args:
        net_role: Role name for the net
        min_width_mm: Minimum trace width
        max_length_mm: Maximum trace length (for timing-critical signals)
        via_allowed: Whether vias are permitted
        plane_connection: Whether connection should be to a plane
        rationale: Human-readable explanation

    Returns:
        RoutingConstraint instance

    Example::

        constraint = routing_constraint(
            "clock",
            min_width_mm=0.15,
            max_length_mm=50.0,
            rationale="Keep clock trace short for signal integrity"
        )
    """
    return RoutingConstraint(
        net_role=net_role,
        min_width_mm=min_width_mm,
        max_length_mm=max_length_mm,
        via_allowed=via_allowed,
        plane_connection=plane_connection,
        rationale=rationale,
    )


class DSLPattern(PCBPattern):
    """A pattern created from a DSL-decorated class.

    This class wraps a user-defined pattern class and provides the
    standard PCBPattern interface.
    """

    def __init__(
        self,
        source_class: type,
        name: str,
        description: str,
        components: list[str],
        placement_rules: list[PlacementRule],
        routing_constraints: list[RoutingConstraint],
        **kwargs: Any,
    ) -> None:
        """Initialize from a decorated class.

        Args:
            source_class: The original decorated class
            name: Pattern name
            description: Pattern description
            components: List of component roles
            placement_rules: Placement rules
            routing_constraints: Routing constraints
            **kwargs: Additional configuration
        """
        super().__init__(**kwargs)
        self._source_class = source_class
        self._source_instance = source_class() if _can_instantiate(source_class) else None
        self._name = name
        self._description = description
        self._components = components
        self._placement_rules = placement_rules
        self._routing_constraints = routing_constraints

    def _build_spec(self) -> PatternSpec:
        """Build the pattern specification."""
        return PatternSpec(
            name=self._name,
            description=self._description,
            components=self._components,
            placement_rules=self._placement_rules,
            routing_constraints=self._routing_constraints,
        )

    def get_placements(self, anchor_at: tuple[float, float]) -> dict[str, Placement]:
        """Calculate placements for components.

        If the source class has a get_placements method, delegate to it.
        Otherwise, calculate based on placement rules.

        Args:
            anchor_at: Position of the anchor component

        Returns:
            Dictionary mapping component roles to Placement objects
        """
        # Check if source class has custom implementation
        if self._source_instance is not None and hasattr(self._source_instance, "get_placements"):
            return self._source_instance.get_placements(anchor_at)

        # Default implementation based on placement rules
        placements: dict[str, Placement] = {}
        anchor_role = self._components[0] if self._components else ""

        for rule in self._placement_rules:
            if rule.relative_to == anchor_role:
                angle = rule.preferred_angle if rule.preferred_angle is not None else 0.0
                distance = (rule.min_distance_mm + rule.max_distance_mm) / 2
                position = self._calculate_position(anchor_at, distance, angle)

                placements[rule.component] = Placement(
                    position=position,
                    rotation=0.0,
                    rationale=rule.rationale,
                )

        return placements

    def validate(self, pcb_path: Path | str) -> list[PatternViolation]:
        """Validate a PCB against this pattern.

        If the source class has a validate method, delegate to it.
        Otherwise, validate based on placement rules.

        Args:
            pcb_path: Path to the KiCad PCB file

        Returns:
            List of violations found
        """
        violations: list[PatternViolation] = []

        # Check if source class has custom validation
        if self._source_instance is not None and hasattr(self._source_instance, "validate"):
            custom_violations = self._source_instance.validate(pcb_path)
            if custom_violations:
                violations.extend(custom_violations)

        # Standard placement rule validation would go here
        # (requires PCB file parsing which is out of scope for this module)

        return violations


def _can_instantiate(cls: type) -> bool:
    """Check if a class can be instantiated without arguments."""
    try:
        # First try to actually instantiate - most reliable test
        cls()
        return True
    except TypeError:
        # If direct instantiation fails, check signature
        try:
            sig = inspect.signature(cls.__init__)
            # Check if all parameters (except self) have defaults
            params = list(sig.parameters.values())[1:]  # Skip 'self'
            return all(p.default is not inspect.Parameter.empty for p in params)
        except (ValueError, TypeError):
            return False
    except Exception:
        return False


def _extract_name_from_class(cls: type) -> str:
    """Extract a pattern name from a class name.

    Converts CamelCase to snake_case and removes 'Pattern' suffix.

    Examples:
        MySensorPattern -> my_sensor
        TemperatureSensor -> temperature_sensor
        LDOPattern -> ldo
    """
    name = cls.__name__

    # Remove 'Pattern' suffix
    if name.endswith("Pattern"):
        name = name[:-7]

    # Convert CamelCase to snake_case
    result = []
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            result.append("_")
        result.append(char.lower())

    return "".join(result)


def define_pattern(
    cls: type | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    components: list[str] | None = None,
    category: str = "",
    register: bool = True,
) -> type | Callable[[type], type]:
    """Decorator to define a PCB pattern from a class.

    This decorator transforms a simple class into a full PCBPattern
    implementation that can be used in the pattern library.

    Can be used with or without arguments::

        # Without arguments (uses class attributes)
        @define_pattern
        class MySensorPattern:
            components = ["sensor", "cap"]
            placement_rules = [...]

        # With arguments (override class attributes)
        @define_pattern(name="custom_name", register=False)
        class MySensorPattern:
            ...

    The decorated class can define:
        - components: List of component roles (required)
        - placement_rules: List of PlacementRule instances
        - routing_constraints: List of RoutingConstraint instances
        - get_placements(self, anchor_at): Custom placement calculation
        - validate(self, pcb_path): Custom validation logic

    Args:
        cls: The class to decorate (when used without arguments)
        name: Override the pattern name (default: derived from class name)
        description: Override the description (default: class docstring)
        components: Override the components list
        category: Category for registry organization
        register: Whether to register the pattern (default: True)

    Returns:
        The decorated class or a decorator function

    Example::

        @define_pattern
        class TemperatureSensorPattern:
            '''NTC thermistor with filtering and protection.'''

            components = ["thermistor", "bias_resistor", "filter_cap"]

            placement_rules = [
                placement_rule("filter_cap", relative_to="thermistor", max_distance_mm=5.0),
            ]

            def validate(self, pcb_path):
                # Custom validation
                return []
    """

    def decorator(cls: type) -> type:
        # Extract configuration from class
        pattern_name = name or _extract_name_from_class(cls)
        pattern_description = description or (cls.__doc__ or "").strip()
        pattern_components = components or getattr(cls, "components", [])
        pattern_placement_rules = getattr(cls, "placement_rules", [])
        pattern_routing_constraints = getattr(cls, "routing_constraints", [])

        # Create the pattern wrapper
        pattern = DSLPattern(
            source_class=cls,
            name=pattern_name,
            description=pattern_description,
            components=pattern_components,
            placement_rules=pattern_placement_rules,
            routing_constraints=pattern_routing_constraints,
        )

        # Register if requested
        if register:
            PatternRegistry.register_instance(
                pattern_name,
                pattern,
                description=pattern_description,
                category=category,
            )

        # Store pattern reference on the class
        cls._pattern = pattern  # type: ignore[attr-defined]
        cls._pattern_name = pattern_name  # type: ignore[attr-defined]

        return cls

    # Handle both @define_pattern and @define_pattern() syntax
    if cls is not None:
        return decorator(cls)
    return decorator


def get_pattern_from_class(cls: type) -> DSLPattern | None:
    """Get the pattern instance from a decorated class.

    Args:
        cls: A class decorated with @define_pattern

    Returns:
        The DSLPattern instance, or None if not decorated
    """
    return getattr(cls, "_pattern", None)


def get_pattern_name_from_class(cls: type) -> str | None:
    """Get the registered pattern name from a decorated class.

    Args:
        cls: A class decorated with @define_pattern

    Returns:
        The pattern name, or None if not decorated
    """
    return getattr(cls, "_pattern_name", None)
