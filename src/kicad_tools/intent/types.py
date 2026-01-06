"""
Base types for the intent module.

This module defines the foundational data structures for declaring design
intent and deriving constraints from interface specifications.

Example::

    from kicad_tools.intent.types import (
        InterfaceCategory,
        Constraint,
        IntentDeclaration,
    )

    # Create a constraint for USB differential impedance
    constraint = Constraint(
        type="impedance",
        params={"target": 90.0, "tolerance": 0.1},
        source="usb2_high_speed",
        severity="error",
    )

    # Declare intent for a USB interface
    declaration = IntentDeclaration(
        interface_type="usb2_high_speed",
        nets=["USB_DP", "USB_DM"],
        constraints=[constraint],
        metadata={"connector": "J1"},
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class InterfaceCategory(Enum):
    """High-level interface categories.

    Categories group related interface types for organization and filtering.
    Each category shares common characteristics in terms of signal integrity
    requirements and routing considerations.
    """

    DIFFERENTIAL = "differential"  # USB, LVDS, Ethernet
    BUS = "bus"  # SPI, I2C, parallel
    SINGLE_ENDED = "single_ended"  # GPIO, analog
    POWER = "power"  # Power rails


class ConstraintSeverity(Enum):
    """Severity level for constraint violations."""

    ERROR = "error"  # Must be fixed before manufacturing
    WARNING = "warning"  # Should be reviewed, may be acceptable


@dataclass
class Constraint:
    """A constraint derived from design intent.

    Constraints are generated from interface specifications and represent
    concrete design rules that must be satisfied. They link back to their
    source interface for context-aware error messages.

    Attributes:
        type: Constraint type identifier (e.g., "impedance", "length_match").
        params: Constraint parameters specific to the type.
        source: Interface type that generated this constraint.
        severity: Whether violations are errors or warnings.
    """

    type: str
    params: dict[str, object]
    source: str
    severity: ConstraintSeverity | str

    def __post_init__(self) -> None:
        """Convert string severity to enum if needed."""
        if isinstance(self.severity, str):
            self.severity = ConstraintSeverity(self.severity)


@dataclass
class IntentDeclaration:
    """A declared design intent for a set of nets.

    An intent declaration associates a group of nets with an interface type,
    along with the constraints derived from that interface specification.

    Attributes:
        interface_type: Interface type name (e.g., "usb2_high_speed").
        nets: List of net names affected by this intent.
        constraints: Constraints derived from the interface specification.
        metadata: Additional context about the declaration.
    """

    interface_type: str
    nets: list[str]
    constraints: list[Constraint] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
