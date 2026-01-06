"""
Design intent declaration and constraint derivation.

This module provides infrastructure for declaring design intent and
automatically deriving constraints from interface specifications. It bridges
the gap between high-level design intent (e.g., "these nets form a USB
interface") and low-level DRC constraints (e.g., "differential impedance
must be 90Ω ±10%").

Example::

    from kicad_tools.intent import (
        REGISTRY,
        InterfaceCategory,
        InterfaceSpec,
        Constraint,
        IntentDeclaration,
        create_intent_declaration,
    )

    # Register a custom interface specification
    class USB2HighSpeedSpec:
        @property
        def name(self) -> str:
            return "usb2_high_speed"

        @property
        def category(self) -> InterfaceCategory:
            return InterfaceCategory.DIFFERENTIAL

        def validate_nets(self, nets: list[str]) -> list[str]:
            if len(nets) != 2:
                return ["USB 2.0 High Speed requires exactly 2 nets (D+ and D-)"]
            return []

        def derive_constraints(
            self, nets: list[str], params: dict
        ) -> list[Constraint]:
            return [
                Constraint(
                    type="impedance",
                    params={"target": 90.0, "tolerance": 0.1},
                    source=self.name,
                    severity="error",
                ),
            ]

        def get_validation_message(self, violation: dict) -> str:
            return f"USB 2.0 HS: {violation.get('message', '')}"

    REGISTRY.register(USB2HighSpeedSpec())

    # Create an intent declaration with automatic constraint derivation
    declaration = create_intent_declaration(
        interface_type="usb2_high_speed",
        nets=["USB_DP", "USB_DM"],
        metadata={"connector": "J1"},
    )

    # Access derived constraints
    for constraint in declaration.constraints:
        print(f"Constraint: {constraint.type} - {constraint.params}")

Classes:
    InterfaceCategory: Enum for interface categories (DIFFERENTIAL, BUS, etc.)
    ConstraintSeverity: Enum for constraint violation severity
    Constraint: A constraint derived from design intent
    IntentDeclaration: A declared design intent for a set of nets
    InterfaceSpec: Protocol for interface specifications
    InterfaceRegistry: Registry of available interface types

Functions:
    derive_constraints: Derive constraints from an interface declaration
    validate_intent: Validate an intent declaration
    create_intent_declaration: Create an intent declaration with constraints

Constants:
    REGISTRY: Global interface type registry
"""

from .constraints import (
    create_intent_declaration,
    derive_constraints,
    validate_intent,
)
from .protocol import InterfaceSpec
from .registry import REGISTRY, InterfaceRegistry
from .types import (
    Constraint,
    ConstraintSeverity,
    IntentDeclaration,
    InterfaceCategory,
)

__all__ = [
    # Types
    "InterfaceCategory",
    "ConstraintSeverity",
    "Constraint",
    "IntentDeclaration",
    # Protocol
    "InterfaceSpec",
    # Registry
    "InterfaceRegistry",
    "REGISTRY",
    # Functions
    "derive_constraints",
    "validate_intent",
    "create_intent_declaration",
]

# Import interface specifications to trigger auto-registration
# This must be at the end after REGISTRY is defined
from . import interfaces as _interfaces  # noqa: F401, E402
