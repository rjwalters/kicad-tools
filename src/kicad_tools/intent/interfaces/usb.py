"""
USB interface specifications.

This module implements USB interface specifications for all common USB variants,
from USB 2.0 Low Speed through USB 3.x Gen2. Each variant defines appropriate
constraints for differential impedance, length matching, and trace routing.

Example::

    from kicad_tools.intent import REGISTRY, create_intent_declaration

    # Create a USB 2.0 High Speed declaration
    declaration = create_intent_declaration(
        interface_type="usb2_high_speed",
        nets=["USB_D+", "USB_D-"],
        metadata={"connector": "J1"},
    )

    # Constraints are automatically derived
    for constraint in declaration.constraints:
        print(f"{constraint.type}: {constraint.params}")

USB Interface Features:

    | Feature                | USB 2.0 LS/FS | USB 2.0 HS | USB 3.x      |
    |------------------------|---------------|------------|--------------|
    | Differential impedance | -             | 90 +/-10%  | 85 +/-10%    |
    | Length matching        | -             | +/-0.5mm   | +/-0.25mm    |
    | Max trace length       | 3m equiv      | 50mm       | 150mm        |
    | Min spacing            | 2x width      | 3x width   | 4x width     |
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ..registry import REGISTRY
from ..types import Constraint, InterfaceCategory


@dataclass
class USBVariant:
    """Configuration for a USB variant.

    Attributes:
        speed: Data rate in bits per second.
        impedance: Target differential impedance in ohms, or None if not specified.
        length_tolerance_mm: Maximum length mismatch in mm, or None if not specified.
        max_trace_length_mm: Maximum recommended trace length in mm.
        min_spacing_multiplier: Minimum spacing as multiple of trace width.
    """

    speed: float
    impedance: float | None
    length_tolerance_mm: float | None
    max_trace_length_mm: float
    min_spacing_multiplier: float


# USB variant specifications
USB_VARIANTS: dict[str, USBVariant] = {
    "usb2_low_speed": USBVariant(
        speed=1.5e6,
        impedance=None,
        length_tolerance_mm=None,
        max_trace_length_mm=3000.0,  # ~3m equivalent
        min_spacing_multiplier=2.0,
    ),
    "usb2_full_speed": USBVariant(
        speed=12e6,
        impedance=None,
        length_tolerance_mm=None,
        max_trace_length_mm=3000.0,  # ~3m equivalent
        min_spacing_multiplier=2.0,
    ),
    "usb2_high_speed": USBVariant(
        speed=480e6,
        impedance=90.0,
        length_tolerance_mm=0.5,
        max_trace_length_mm=50.0,
        min_spacing_multiplier=3.0,
    ),
    "usb3_gen1": USBVariant(
        speed=5e9,
        impedance=85.0,
        length_tolerance_mm=0.25,
        max_trace_length_mm=150.0,
        min_spacing_multiplier=4.0,
    ),
    "usb3_gen2": USBVariant(
        speed=10e9,
        impedance=85.0,
        length_tolerance_mm=0.25,
        max_trace_length_mm=150.0,
        min_spacing_multiplier=4.0,
    ),
}


class USBInterfaceSpec:
    """USB interface specification.

    Implements the InterfaceSpec protocol for USB interfaces. Supports multiple
    USB variants from Low Speed through USB 3.x Gen2.

    The variant is determined by the ``variant`` parameter passed to
    :meth:`derive_constraints`. If not specified, defaults to ``usb2_high_speed``.

    Attributes:
        _variant_name: The USB variant name (e.g., "usb2_high_speed").
        _variant: The variant configuration.
    """

    VARIANTS: ClassVar[dict[str, USBVariant]] = USB_VARIANTS

    def __init__(self, variant_name: str = "usb2_high_speed") -> None:
        """Initialize USB interface spec for a specific variant.

        Args:
            variant_name: USB variant name. One of: usb2_low_speed, usb2_full_speed,
                usb2_high_speed, usb3_gen1, usb3_gen2.

        Raises:
            ValueError: If the variant name is not recognized.
        """
        if variant_name not in self.VARIANTS:
            valid = ", ".join(sorted(self.VARIANTS.keys()))
            raise ValueError(f"Unknown USB variant: '{variant_name}'. Valid variants: {valid}")
        self._variant_name = variant_name
        self._variant = self.VARIANTS[variant_name]

    @property
    def name(self) -> str:
        """Interface type name (e.g., 'usb2_high_speed')."""
        return self._variant_name

    @property
    def category(self) -> InterfaceCategory:
        """Interface category (DIFFERENTIAL for all USB variants)."""
        return InterfaceCategory.DIFFERENTIAL

    def validate_nets(self, nets: list[str]) -> list[str]:
        """Validate net names/count for USB interface.

        USB interfaces require exactly 2 nets for the differential pair (D+ and D-).

        Args:
            nets: List of net names to validate.

        Returns:
            List of validation error messages. Empty list if valid.
        """
        errors: list[str] = []
        if len(nets) != 2:
            errors.append(
                f"USB {self._variant_name} requires exactly 2 nets (D+ and D-), got {len(nets)}"
            )
        return errors

    def derive_constraints(self, nets: list[str], params: dict[str, Any]) -> list[Constraint]:
        """Derive constraints from USB interface declaration.

        Generates constraints based on the USB variant requirements:
        - Differential pair constraint for all variants
        - Length matching for high-speed variants (HS, USB3)
        - Impedance constraints for high-speed variants

        Args:
            nets: List of net names (should be exactly 2 for USB D+/D-).
            params: Additional parameters. Supports:
                - variant: Override variant (default uses instance variant)

        Returns:
            List of constraints derived from the USB specification.
        """
        # Allow runtime variant override via params
        variant_name = params.get("variant", self._variant_name)
        if isinstance(variant_name, str) and variant_name in self.VARIANTS:
            variant = self.VARIANTS[variant_name]
            source = f"usb:{variant_name}"
        else:
            variant = self._variant
            source = f"usb:{self._variant_name}"

        constraints: list[Constraint] = []

        # Differential pair constraint (always added for USB)
        if len(nets) == 2:
            diff_params: dict[str, Any] = {"nets": nets}
            if variant.impedance is not None:
                diff_params["impedance"] = variant.impedance
                diff_params["tolerance"] = 0.1  # 10% tolerance per USB spec

            constraints.append(
                Constraint(
                    type="differential_pair",
                    params=diff_params,
                    source=source,
                    severity="error",
                )
            )

        # Length matching for high-speed variants
        if variant.length_tolerance_mm is not None:
            constraints.append(
                Constraint(
                    type="length_match",
                    params={
                        "nets": nets,
                        "tolerance_mm": variant.length_tolerance_mm,
                    },
                    source=source,
                    severity="error",
                )
            )

        # Trace width for impedance control (warning - may need manual adjustment)
        if variant.impedance is not None:
            constraints.append(
                Constraint(
                    type="trace_width",
                    params={
                        "target_impedance": variant.impedance,
                        "tolerance": 0.1,
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

        # Minimum spacing constraint
        constraints.append(
            Constraint(
                type="clearance",
                params={
                    "nets": nets,
                    "min_spacing_multiplier": variant.min_spacing_multiplier,
                },
                source=source,
                severity="warning",
            )
        )

        return constraints

    def get_validation_message(self, violation: dict[str, Any]) -> str:
        """Convert generic DRC violation to USB-aware message.

        Provides context-aware error messages that explain violations in terms
        of USB signal integrity requirements.

        Args:
            violation: Dictionary containing violation details. Expected keys vary
                by violation type:
                - length_mismatch: {"type": "length_mismatch", "delta": <mm>}
                - impedance: {"type": "impedance", "actual": <ohms>}
                - clearance: {"type": "clearance", "actual": <mm>, "required": <mm>}

        Returns:
            Human-readable message explaining the violation in USB context.
        """
        violation_type = violation.get("type", "")
        variant = self._variant

        if violation_type == "length_mismatch":
            delta = violation.get("delta", "?")
            tolerance = variant.length_tolerance_mm or 0.5
            speed_str = self._format_speed(variant.speed)
            return (
                f"USB differential pair length mismatch: {delta}mm. "
                f"USB {self._variant_name.replace('_', ' ').upper()} requires "
                f"+/-{tolerance}mm matching for signal integrity at {speed_str}."
            )

        if violation_type == "impedance":
            actual = violation.get("actual", "?")
            target = variant.impedance or 90
            return (
                f"USB trace impedance {actual} ohm differs from required {target} ohm "
                f"differential. Adjust trace width or spacing for proper impedance "
                f"matching."
            )

        if violation_type == "clearance":
            actual = violation.get("actual", "?")
            required = violation.get("required", "?")
            return (
                f"USB D+/D- clearance {actual}mm is less than required {required}mm. "
                f"USB {self._variant_name.replace('_', ' ').upper()} requires "
                f"{variant.min_spacing_multiplier}x trace width clearance "
                f"for crosstalk immunity."
            )

        if violation_type == "max_length":
            actual = violation.get("actual", "?")
            max_len = variant.max_trace_length_mm
            return (
                f"USB trace length {actual}mm exceeds maximum {max_len}mm. "
                f"Long traces may cause signal integrity issues at "
                f"{self._format_speed(variant.speed)}."
            )

        # Fallback for unknown violation types
        return violation.get("message", str(violation))

    @staticmethod
    def _format_speed(speed: float) -> str:
        """Format speed in human-readable form.

        Args:
            speed: Speed in bits per second.

        Returns:
            Formatted string (e.g., "480Mbps", "5Gbps").
        """
        if speed >= 1e9:
            return f"{speed / 1e9:.0f}Gbps"
        if speed >= 1e6:
            return f"{speed / 1e6:.0f}Mbps"
        return f"{speed / 1e3:.1f}kbps"


# Register all USB variants in the global registry
def _register_usb_interfaces() -> None:
    """Register all USB interface variants in the global registry."""
    for variant_name in USB_VARIANTS:
        spec = USBInterfaceSpec(variant_name)
        REGISTRY.register(spec)


# Auto-register on module import
_register_usb_interfaces()
