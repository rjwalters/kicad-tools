"""
Intent-based pattern constraints for PCB design.

This module provides pattern abstractions that integrate with the intent system
for deriving DRC constraints from high-level circuit specifications. Unlike the
placement-focused patterns in base.py, these patterns focus on design rules
and electrical constraints rather than physical positioning.

Example::

    from kicad_tools.patterns.constraints import SPIPattern

    # Create an SPI pattern for high-speed operation
    spi = SPIPattern(speed="high", cs_count=2)

    # Get placement guidelines
    rules = spi.get_placement_rules()

    # Get constraints for the intent system
    constraints = spi.derive_constraints(
        nets=["SPI_CLK", "SPI_MOSI", "SPI_MISO", "SPI_CS0"]
    )

Classes:
    IntentPattern: Base class for intent-integrated patterns
    ConstraintPlacementRule: Placement guideline for components
    ConstraintRoutingRule: Routing guideline for traces
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from kicad_tools.intent import Constraint, InterfaceCategory


class ConstraintPriority(Enum):
    """Priority levels for constraint rules."""

    CRITICAL = "critical"  # Must be followed for proper operation
    RECOMMENDED = "recommended"  # Should be followed for best performance
    OPTIONAL = "optional"  # Nice to have, but not essential


@dataclass
class ConstraintPlacementRule:
    """A placement guideline for components in a pattern.

    Placement rules describe how components in a pattern should be positioned
    relative to each other and to the PCB for optimal electrical performance.

    Attributes:
        name: Short identifier for the rule.
        description: Human-readable description of the rule.
        priority: How important this rule is to follow.
        component_refs: Component references this rule applies to.
        params: Additional parameters for the rule.
    """

    name: str
    description: str
    priority: ConstraintPriority = ConstraintPriority.RECOMMENDED
    component_refs: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConstraintRoutingRule:
    """A routing guideline for traces in a pattern.

    Routing rules describe how traces in a pattern should be routed,
    including length matching, impedance control, and via restrictions.

    Attributes:
        name: Short identifier for the rule.
        description: Human-readable description of the rule.
        net_pattern: Glob pattern for nets this rule applies to.
        params: Additional parameters for the rule.
    """

    name: str
    description: str
    net_pattern: str = "*"
    params: dict[str, Any] = field(default_factory=dict)


class IntentPattern(ABC):
    """Base class for intent-integrated PCB patterns.

    An IntentPattern represents a common circuit pattern with associated
    placement rules, routing guidelines, and validation logic. Patterns
    can derive constraints for the intent system.

    This differs from PCBPattern (in base.py) which focuses on physical
    placement calculations. IntentPattern focuses on design rules and
    constraint derivation.

    Subclasses must implement:
        - name: Property returning the pattern name
        - category: Property returning the interface category
        - get_placement_rules: Method returning placement rules
        - get_routing_rules: Method returning routing rules
        - validate: Method for pattern validation
        - derive_constraints: Method to generate intent constraints
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the pattern name (e.g., 'spi_high_speed')."""
        ...

    @property
    @abstractmethod
    def category(self) -> InterfaceCategory:
        """Return the interface category for this pattern."""
        ...

    @abstractmethod
    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return placement rules for this pattern.

        Returns:
            List of ConstraintPlacementRule objects describing component placement.
        """
        ...

    @abstractmethod
    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return routing rules for this pattern.

        Returns:
            List of ConstraintRoutingRule objects describing trace routing.
        """
        ...

    @abstractmethod
    def validate(self, **kwargs: Any) -> list[str]:
        """Validate the pattern configuration.

        Args:
            **kwargs: Pattern-specific validation parameters.

        Returns:
            List of validation error messages. Empty if valid.
        """
        ...

    @abstractmethod
    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from this pattern.

        Args:
            nets: List of net names in the pattern.
            params: Additional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        ...

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of this pattern's configuration.

        Returns:
            Dictionary with pattern name, category, and rule counts.
        """
        return {
            "name": self.name,
            "category": self.category.value,
            "placement_rules": len(self.get_placement_rules()),
            "routing_rules": len(self.get_routing_rules()),
        }
