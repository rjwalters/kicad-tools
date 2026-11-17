"""
SPI interface specifications.

This module implements SPI interface specifications for common SPI speed variants,
from standard 10MHz through high-speed 100MHz. Each variant defines appropriate
constraints for clock signal routing, length matching, and trace requirements.

Example::

    from kicad_tools.intent import REGISTRY, create_intent_declaration

    # Create a standard SPI declaration
    declaration = create_intent_declaration(
        interface_type="spi_standard",
        nets=["SPI_CLK", "SPI_MOSI", "SPI_MISO", "SPI_CS"],
        metadata={"device": "U1"},
    )

    # Constraints are automatically derived
    for constraint in declaration.constraints:
        print(f"{constraint.type}: {constraint.params}")

SPI Interface Features:

    | Feature            | Standard (≤10MHz) | Fast (≤50MHz) | High-Speed (≤100MHz) |
    |--------------------|-------------------|---------------|----------------------|
    | Max trace length   | 200mm             | 100mm         | 50mm                 |
    | Length matching    | -                 | ±5mm          | ±2mm                 |
    | Termination        | -                 | Optional      | Recommended          |
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ..registry import REGISTRY
from ..types import Constraint, InterfaceCategory


@dataclass
class SPIVariant:
    """Configuration for an SPI variant.

    Attributes:
        max_freq: Maximum clock frequency in Hz.
        max_trace_length_mm: Maximum recommended trace length in mm.
        length_tolerance_mm: Maximum length mismatch in mm, or None if not specified.
        termination_recommended: Whether termination resistors are recommended.
    """

    max_freq: float
    max_trace_length_mm: float
    length_tolerance_mm: float | None
    termination_recommended: bool


# SPI variant specifications
SPI_VARIANTS: dict[str, SPIVariant] = {
    "spi_standard": SPIVariant(
        max_freq=10e6,
        max_trace_length_mm=200.0,
        length_tolerance_mm=None,
        termination_recommended=False,
    ),
    "spi_fast": SPIVariant(
        max_freq=50e6,
        max_trace_length_mm=100.0,
        length_tolerance_mm=5.0,
        termination_recommended=False,
    ),
    "spi_high_speed": SPIVariant(
        max_freq=100e6,
        max_trace_length_mm=50.0,
        length_tolerance_mm=2.0,
        termination_recommended=True,
    ),
}


class SPIInterfaceSpec:
    """SPI interface specification.

    Implements the InterfaceSpec protocol for SPI interfaces. Supports multiple
    SPI variants from standard 10MHz through high-speed 100MHz.

    The variant is determined by the ``variant`` parameter passed to
    :meth:`derive_constraints`. If not specified, defaults to ``spi_standard``.

    Attributes:
        _variant_name: The SPI variant name (e.g., "spi_standard").
        _variant: The variant configuration.
    """

    VARIANTS: ClassVar[dict[str, SPIVariant]] = SPI_VARIANTS

    def __init__(self, variant_name: str = "spi_standard") -> None:
        """Initialize SPI interface spec for a specific variant.

        Args:
            variant_name: SPI variant name. One of: spi_standard, spi_fast,
                spi_high_speed.

        Raises:
            ValueError: If the variant name is not recognized.
        """
        if variant_name not in self.VARIANTS:
            valid = ", ".join(sorted(self.VARIANTS.keys()))
            raise ValueError(f"Unknown SPI variant: '{variant_name}'. Valid variants: {valid}")
        self._variant_name = variant_name
        self._variant = self.VARIANTS[variant_name]

    @property
    def name(self) -> str:
        """Interface type name (e.g., 'spi_standard')."""
        return self._variant_name

    @property
    def category(self) -> InterfaceCategory:
        """Interface category (BUS for all SPI variants)."""
        return InterfaceCategory.BUS

    def validate_nets(self, nets: list[str]) -> list[str]:
        """Validate net names/count for SPI interface.

        SPI interfaces require at least 3 nets (CLK, MOSI/MISO, CS).
        Typical configurations have 4 nets (CLK, MOSI, MISO, CS).

        Args:
            nets: List of net names to validate.

        Returns:
            List of validation error messages. Empty list if valid.
        """
        errors: list[str] = []
        if len(nets) < 3:
            errors.append(
                f"SPI {self._variant_name} requires at least 3 nets "
                f"(CLK, MOSI/MISO, CS), got {len(nets)}"
            )
        return errors

    def derive_constraints(self, nets: list[str], params: dict[str, Any]) -> list[Constraint]:
        """Derive constraints from SPI interface declaration.

        Generates constraints based on the SPI variant requirements:
        - Maximum trace length for all variants
        - Length matching for high-speed variants
        - Termination recommendations for high-speed

        Args:
            nets: List of net names (should be at least 3 for SPI).
            params: Additional parameters. Supports:
                - variant: Override variant (default uses instance variant)

        Returns:
            List of constraints derived from the SPI specification.
        """
        # Allow runtime variant override via params
        variant_name = params.get("variant", self._variant_name)
        if isinstance(variant_name, str) and variant_name in self.VARIANTS:
            variant = self.VARIANTS[variant_name]
            source = f"spi:{variant_name}"
        else:
            variant = self._variant
            source = f"spi:{self._variant_name}"

        constraints: list[Constraint] = []

        # Find clock signal (typically first net or one containing CLK/SCK)
        clk_net = self._find_clk_net(nets)

        # Clock signal maximum length constraint
        if clk_net:
            constraints.append(
                Constraint(
                    type="max_length",
                    params={
                        "net": clk_net,
                        "max_mm": variant.max_trace_length_mm,
                    },
                    source=source,
                    severity="warning",
                )
            )

        # Maximum length for all SPI signals
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

        # Length matching for high-speed SPI variants
        if variant.length_tolerance_mm is not None:
            constraints.append(
                Constraint(
                    type="length_match",
                    params={
                        "nets": nets,
                        "tolerance_mm": variant.length_tolerance_mm,
                    },
                    source=source,
                    severity="warning",
                )
            )

        # Termination recommendation for high-speed
        if variant.termination_recommended:
            constraints.append(
                Constraint(
                    type="termination",
                    params={
                        "nets": nets,
                        "recommended": True,
                    },
                    source=source,
                    severity="warning",
                )
            )

        return constraints

    def get_validation_message(self, violation: dict[str, Any]) -> str:
        """Convert generic DRC violation to SPI-aware message.

        Provides context-aware error messages that explain violations in terms
        of SPI signal integrity requirements.

        Args:
            violation: Dictionary containing violation details. Expected keys vary
                by violation type:
                - length_mismatch: {"type": "length_mismatch", "delta": <mm>}
                - max_length: {"type": "max_length", "actual": <mm>}

        Returns:
            Human-readable message explaining the violation in SPI context.
        """
        violation_type = violation.get("type", "")
        variant = self._variant

        if violation_type == "length_mismatch":
            delta = violation.get("delta", "?")
            tolerance = variant.length_tolerance_mm or 5.0
            freq_str = self._format_freq(variant.max_freq)
            return (
                f"SPI signal length mismatch: {delta}mm. "
                f"SPI {self._variant_name.replace('_', ' ')} requires "
                f"+/-{tolerance}mm matching for signal integrity at {freq_str}."
            )

        if violation_type == "max_length":
            actual = violation.get("actual", "?")
            max_len = variant.max_trace_length_mm
            return (
                f"SPI trace length {actual}mm exceeds maximum {max_len}mm. "
                f"Long traces may cause signal integrity issues at "
                f"{self._format_freq(variant.max_freq)}."
            )

        if violation_type == "termination":
            return (
                f"SPI high-speed signals should have series termination resistors "
                f"(typically 22-33Ω) near the source to reduce reflections at "
                f"{self._format_freq(variant.max_freq)}."
            )

        # Fallback for unknown violation types
        return violation.get("message", str(violation))

    @staticmethod
    def _find_clk_net(nets: list[str]) -> str | None:
        """Find the clock signal net from a list of SPI nets.

        Args:
            nets: List of net names.

        Returns:
            The clock net name, or None if not found.
        """
        clk_patterns = ["CLK", "SCK", "SCLK"]
        for net in nets:
            net_upper = net.upper()
            for pattern in clk_patterns:
                if pattern in net_upper:
                    return net
        # Default to first net if no clock pattern found
        return nets[0] if nets else None

    @staticmethod
    def _format_freq(freq: float) -> str:
        """Format frequency in human-readable form.

        Args:
            freq: Frequency in Hz.

        Returns:
            Formatted string (e.g., "10MHz", "100MHz").
        """
        if freq >= 1e9:
            return f"{freq / 1e9:.0f}GHz"
        if freq >= 1e6:
            return f"{freq / 1e6:.0f}MHz"
        return f"{freq / 1e3:.0f}kHz"


# Register all SPI variants in the global registry
def _register_spi_interfaces() -> None:
    """Register all SPI interface variants in the global registry."""
    for variant_name in SPI_VARIANTS:
        spec = SPIInterfaceSpec(variant_name)
        REGISTRY.register(spec)


# Auto-register on module import
_register_spi_interfaces()
