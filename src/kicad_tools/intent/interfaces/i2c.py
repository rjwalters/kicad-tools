"""
I2C interface specifications.

This module implements I2C interface specifications for all common I2C variants,
from Standard Mode (100kHz) through Fast Mode Plus (1MHz). Each variant defines
appropriate constraints for bus capacitance, pull-up resistors, and routing.

Example::

    from kicad_tools.intent import REGISTRY, create_intent_declaration

    # Create an I2C Fast Mode declaration
    declaration = create_intent_declaration(
        interface_type="i2c_fast",
        nets=["I2C_SDA", "I2C_SCL"],
        metadata={"device": "U1"},
    )

    # Constraints are automatically derived
    for constraint in declaration.constraints:
        print(f"{constraint.type}: {constraint.params}")

I2C Interface Features:

    | Feature           | Standard (100kHz) | Fast (400kHz)  | Fast+ (1MHz)   |
    |-------------------|-------------------|----------------|----------------|
    | Max capacitance   | 400pF             | 400pF          | 550pF          |
    | Rise time         | 1000ns            | 300ns          | 120ns          |
    | Pull-up resistor  | 4.7kΩ             | 2.2kΩ          | 1kΩ            |
    | Max trace length  | ~1m               | ~0.5m          | ~0.3m          |
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ..registry import REGISTRY
from ..types import Constraint, InterfaceCategory


@dataclass
class I2CVariant:
    """Configuration for an I2C variant.

    Attributes:
        freq: Clock frequency in Hz.
        max_capacitance_pf: Maximum bus capacitance in pF.
        rise_time_ns: Maximum rise time in nanoseconds.
        pullup_ohms: Typical pull-up resistor value in ohms.
        max_trace_length_mm: Maximum recommended trace length in mm.
    """

    freq: float
    max_capacitance_pf: float
    rise_time_ns: float
    pullup_ohms: float
    max_trace_length_mm: float


# I2C variant specifications
I2C_VARIANTS: dict[str, I2CVariant] = {
    "i2c_standard": I2CVariant(
        freq=100e3,
        max_capacitance_pf=400.0,
        rise_time_ns=1000.0,
        pullup_ohms=4700.0,
        max_trace_length_mm=1000.0,  # ~1m
    ),
    "i2c_fast": I2CVariant(
        freq=400e3,
        max_capacitance_pf=400.0,
        rise_time_ns=300.0,
        pullup_ohms=2200.0,
        max_trace_length_mm=500.0,  # ~0.5m
    ),
    "i2c_fast_plus": I2CVariant(
        freq=1e6,
        max_capacitance_pf=550.0,
        rise_time_ns=120.0,
        pullup_ohms=1000.0,
        max_trace_length_mm=300.0,  # ~0.3m
    ),
}


class I2CInterfaceSpec:
    """I2C interface specification.

    Implements the InterfaceSpec protocol for I2C interfaces. Supports
    Standard Mode (100kHz), Fast Mode (400kHz), and Fast Mode Plus (1MHz).

    The variant is determined by the ``variant`` parameter passed to
    :meth:`derive_constraints`. If not specified, defaults to ``i2c_standard``.

    Attributes:
        _variant_name: The I2C variant name (e.g., "i2c_standard").
        _variant: The variant configuration.
    """

    VARIANTS: ClassVar[dict[str, I2CVariant]] = I2C_VARIANTS

    def __init__(self, variant_name: str = "i2c_standard") -> None:
        """Initialize I2C interface spec for a specific variant.

        Args:
            variant_name: I2C variant name. One of: i2c_standard, i2c_fast,
                i2c_fast_plus.

        Raises:
            ValueError: If the variant name is not recognized.
        """
        if variant_name not in self.VARIANTS:
            valid = ", ".join(sorted(self.VARIANTS.keys()))
            raise ValueError(f"Unknown I2C variant: '{variant_name}'. Valid variants: {valid}")
        self._variant_name = variant_name
        self._variant = self.VARIANTS[variant_name]

    @property
    def name(self) -> str:
        """Interface type name (e.g., 'i2c_standard')."""
        return self._variant_name

    @property
    def category(self) -> InterfaceCategory:
        """Interface category (BUS for all I2C variants)."""
        return InterfaceCategory.BUS

    def validate_nets(self, nets: list[str]) -> list[str]:
        """Validate net names/count for I2C interface.

        I2C interfaces require exactly 2 nets (SDA and SCL).

        Args:
            nets: List of net names to validate.

        Returns:
            List of validation error messages. Empty list if valid.
        """
        errors: list[str] = []
        if len(nets) != 2:
            errors.append(
                f"I2C {self._variant_name} requires exactly 2 nets (SDA and SCL), got {len(nets)}"
            )
        return errors

    def derive_constraints(self, nets: list[str], params: dict[str, Any]) -> list[Constraint]:
        """Derive constraints from I2C interface declaration.

        Generates constraints based on the I2C variant requirements:
        - Bus capacitance limit (affects trace length)
        - Pull-up resistor requirements
        - Maximum trace length

        Args:
            nets: List of net names (should be exactly 2 for I2C SDA/SCL).
            params: Additional parameters. Supports:
                - variant: Override variant (default uses instance variant)

        Returns:
            List of constraints derived from the I2C specification.
        """
        # Allow runtime variant override via params
        variant_name = params.get("variant", self._variant_name)
        if isinstance(variant_name, str) and variant_name in self.VARIANTS:
            variant = self.VARIANTS[variant_name]
            source = f"i2c:{variant_name}"
        else:
            variant = self._variant
            source = f"i2c:{self._variant_name}"

        constraints: list[Constraint] = []

        # Bus capacitance constraint
        constraints.append(
            Constraint(
                type="max_capacitance",
                params={
                    "nets": nets,
                    "max_pf": variant.max_capacitance_pf,
                },
                source=source,
                severity="warning",
            )
        )

        # Pull-up resistor requirement
        constraints.append(
            Constraint(
                type="requires_pullup",
                params={
                    "nets": nets,
                    "typical_ohms": variant.pullup_ohms,
                },
                source=source,
                severity="warning",
            )
        )

        # Maximum trace length constraint
        constraints.append(
            Constraint(
                type="max_length",
                params={
                    "nets": nets,
                    "max_mm": variant.max_trace_length_mm,
                },
                source=source,
                severity="warning",
            )
        )

        return constraints

    def get_validation_message(self, violation: dict[str, Any]) -> str:
        """Convert generic DRC violation to I2C-aware message.

        Provides context-aware error messages that explain violations in terms
        of I2C bus requirements.

        Args:
            violation: Dictionary containing violation details. Expected keys vary
                by violation type:
                - capacitance: {"type": "capacitance", "actual": <pF>}
                - max_length: {"type": "max_length", "actual": <mm>}
                - pullup: {"type": "pullup", "missing": True}

        Returns:
            Human-readable message explaining the violation in I2C context.
        """
        violation_type = violation.get("type", "")
        variant = self._variant

        if violation_type == "capacitance":
            actual = violation.get("actual", "?")
            max_cap = variant.max_capacitance_pf
            freq_str = self._format_freq(variant.freq)
            return (
                f"I2C bus capacitance {actual}pF exceeds maximum {max_cap}pF. "
                f"At {freq_str}, excessive capacitance will slow rise times "
                f"and may cause communication failures."
            )

        if violation_type == "max_length":
            actual = violation.get("actual", "?")
            max_len = variant.max_trace_length_mm
            return (
                f"I2C trace length {actual}mm exceeds recommended maximum {max_len}mm. "
                f"Long traces increase capacitance and may require stronger pull-ups."
            )

        if violation_type == "pullup":
            pullup = variant.pullup_ohms
            pullup_str = self._format_resistance(pullup)
            return (
                f"I2C bus requires pull-up resistors on SDA and SCL. "
                f"For {self._variant_name.replace('_', ' ')}, typical value is {pullup_str}."
            )

        if violation_type == "rise_time":
            actual = violation.get("actual", "?")
            max_rise = variant.rise_time_ns
            return (
                f"I2C rise time {actual}ns exceeds specification {max_rise}ns. "
                f"Use lower pull-up resistor values or reduce bus capacitance."
            )

        # Fallback for unknown violation types
        return violation.get("message", str(violation))

    @staticmethod
    def _format_freq(freq: float) -> str:
        """Format frequency in human-readable form.

        Args:
            freq: Frequency in Hz.

        Returns:
            Formatted string (e.g., "100kHz", "1MHz").
        """
        if freq >= 1e6:
            return f"{freq / 1e6:.0f}MHz"
        return f"{freq / 1e3:.0f}kHz"

    @staticmethod
    def _format_resistance(ohms: float) -> str:
        """Format resistance in human-readable form.

        Args:
            ohms: Resistance in ohms.

        Returns:
            Formatted string (e.g., "4.7kΩ", "1kΩ").
        """
        if ohms >= 1000:
            value = ohms / 1000
            if value == int(value):
                return f"{int(value)}kΩ"
            return f"{value:.1f}kΩ"
        return f"{int(ohms)}Ω"


# Register all I2C variants in the global registry
def _register_i2c_interfaces() -> None:
    """Register all I2C interface variants in the global registry."""
    for variant_name in I2C_VARIANTS:
        spec = I2CInterfaceSpec(variant_name)
        REGISTRY.register(spec)


# Auto-register on module import
_register_i2c_interfaces()
