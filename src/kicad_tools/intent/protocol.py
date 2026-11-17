"""
Interface protocol definition.

This module defines the Protocol that all interface specifications must
implement. The protocol enables a consistent API for validating nets,
deriving constraints, and generating context-aware DRC messages.

Example::

    from kicad_tools.intent.protocol import InterfaceSpec
    from kicad_tools.intent.types import Constraint, InterfaceCategory

    class USB2HighSpeedSpec:
        '''USB 2.0 High Speed interface specification.'''

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
            self, nets: list[str], params: dict[str, object]
        ) -> list[Constraint]:
            return [
                Constraint(
                    type="impedance",
                    params={"target": 90.0, "tolerance": 0.1},
                    source=self.name,
                    severity="error",
                ),
            ]

        def get_validation_message(self, violation: dict[str, object]) -> str:
            return f"USB differential pair: {violation.get('message', '')}"
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .types import Constraint, InterfaceCategory


class InterfaceSpec(Protocol):
    """Protocol for interface specifications.

    Interface specifications define the requirements and constraints for
    specific interface types (e.g., USB, SPI, I2C). Implementations provide:

    - Validation of net configuration
    - Constraint derivation from interface rules
    - Context-aware DRC violation messages
    """

    @property
    def name(self) -> str:
        """Interface type name (e.g., 'usb2_high_speed').

        Returns:
            A unique identifier for this interface type.
        """
        ...

    @property
    def category(self) -> InterfaceCategory:
        """Interface category.

        Returns:
            The high-level category this interface belongs to.
        """
        ...

    def validate_nets(self, nets: list[str]) -> list[str]:
        """Validate net names/count for this interface.

        Checks whether the provided nets are appropriate for this interface
        type. This includes verifying the number of nets and any naming
        conventions.

        Args:
            nets: List of net names to validate.

        Returns:
            List of validation error messages. Empty list if valid.
        """
        ...

    def derive_constraints(self, nets: list[str], params: dict[str, object]) -> list[Constraint]:
        """Derive constraints from interface declaration.

        Generates the set of constraints that apply to the given nets
        based on the interface specification.

        Args:
            nets: List of net names in this interface.
            params: Additional parameters for constraint generation.

        Returns:
            List of constraints derived from the interface specification.
        """
        ...

    def get_validation_message(self, violation: dict[str, object]) -> str:
        """Convert generic DRC violation to intent-aware message.

        Transforms a generic DRC violation into a message that explains
        the violation in terms of the design intent.

        Args:
            violation: Dictionary containing violation details.

        Returns:
            Human-readable message explaining the violation in context.
        """
        ...
