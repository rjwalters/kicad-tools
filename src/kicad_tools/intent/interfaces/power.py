"""
Power interface specifications.

This module implements power rail interface specifications for common power
delivery scenarios. It provides constraints for trace width based on current
requirements and decoupling capacitor recommendations.

Example::

    from kicad_tools.intent import REGISTRY, create_intent_declaration

    # Create a 3.3V power rail declaration
    declaration = create_intent_declaration(
        interface_type="power_rail",
        nets=["VCC_3V3"],
        params={"voltage": 3.3, "max_current": 0.5},
        metadata={"source": "U1"},
    )

    # Constraints are automatically derived
    for constraint in declaration.constraints:
        print(f"{constraint.type}: {constraint.params}")

Power Rail Constraints:

    | Current | Min Width (1oz Cu) | Decoupling                  |
    |---------|--------------------|-----------------------------|
    | ≤100mA  | 0.15mm             | 100nF                       |
    | ≤500mA  | 0.25mm             | 100nF + 10µF                |
    | ≤1A     | 0.4mm              | 100nF + 10µF + 47µF         |
    | ≤2A     | 0.7mm              | Bulk + ceramic array        |

Note:
    Trace width calculations use IPC-2221 approximations for 1oz copper
    with 10°C temperature rise. Actual requirements depend on copper weight,
    ambient temperature, and acceptable temperature rise.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..registry import REGISTRY
from ..types import Constraint, InterfaceCategory


class PowerInterfaceSpec:
    """Power rail interface specification.

    Implements the InterfaceSpec protocol for power rail interfaces. Generates
    constraints based on voltage and current requirements including trace width
    and decoupling capacitors.

    Unlike other interface specs, PowerInterfaceSpec doesn't have predefined
    variants. Instead, constraints are derived dynamically from parameters like
    voltage and max_current.
    """

    # Current thresholds for decoupling recommendations (in amps)
    _DECOUPLING_THRESHOLDS: ClassVar[list[tuple[float, list[dict[str, Any]]]]] = [
        (0.1, [{"value": "100nF", "count": 1}]),
        (0.5, [{"value": "100nF", "count": 1}, {"value": "10uF", "count": 1}]),
        (
            1.0,
            [
                {"value": "100nF", "count": 1},
                {"value": "10uF", "count": 1},
                {"value": "47uF", "count": 1},
            ],
        ),
        (
            2.0,
            [
                {"value": "100nF", "count": 2},
                {"value": "10uF", "count": 2},
                {"value": "47uF", "count": 1},
                {"value": "100uF", "count": 1},
            ],
        ),
    ]

    def __init__(self) -> None:
        """Initialize power rail interface spec."""

    @property
    def name(self) -> str:
        """Interface type name."""
        return "power_rail"

    @property
    def category(self) -> InterfaceCategory:
        """Interface category (POWER for power rails)."""
        return InterfaceCategory.POWER

    def validate_nets(self, nets: list[str]) -> list[str]:
        """Validate net names/count for power rail interface.

        Power rail declarations require exactly 1 net (the power rail).

        Args:
            nets: List of net names to validate.

        Returns:
            List of validation error messages. Empty list if valid.
        """
        errors: list[str] = []
        if len(nets) != 1:
            errors.append(f"Power rail declaration requires exactly 1 net, got {len(nets)}")
        return errors

    def derive_constraints(self, nets: list[str], params: dict[str, Any]) -> list[Constraint]:
        """Derive constraints from power rail declaration.

        Generates constraints based on the power rail requirements:
        - Minimum trace width based on current
        - Decoupling capacitor requirements based on current

        Args:
            nets: List of net names (should be exactly 1 for power rail).
            params: Parameters for constraint generation:
                - voltage: Rail voltage in volts (optional, for context)
                - max_current: Maximum current in amps (default: 0.5A)

        Returns:
            List of constraints derived from the power specification.
        """
        source = "power_rail"
        constraints: list[Constraint] = []

        voltage = params.get("voltage")
        current = params.get("max_current", 0.5)

        # Trace width for current capacity
        min_width = self._width_for_current(current)
        constraints.append(
            Constraint(
                type="min_trace_width",
                params={
                    "net": nets[0] if nets else None,
                    "min_mm": min_width,
                    "current": current,
                },
                source=source,
                severity="error",
            )
        )

        # Decoupling capacitor requirements
        decoupling_spec = self._decoupling_spec(current)
        decoupling_params: dict[str, Any] = {
            "net": nets[0] if nets else None,
            "capacitors": decoupling_spec,
        }
        if voltage is not None:
            decoupling_params["voltage"] = voltage

        constraints.append(
            Constraint(
                type="requires_decoupling",
                params=decoupling_params,
                source=source,
                severity="warning",
            )
        )

        return constraints

    def get_validation_message(self, violation: dict[str, Any]) -> str:
        """Convert generic DRC violation to power-aware message.

        Provides context-aware error messages that explain violations in terms
        of power delivery requirements.

        Args:
            violation: Dictionary containing violation details. Expected keys vary
                by violation type:
                - trace_width: {"type": "trace_width", "actual": <mm>, "required": <mm>}
                - decoupling: {"type": "decoupling", "missing": <list>}

        Returns:
            Human-readable message explaining the violation in power context.
        """
        violation_type = violation.get("type", "")

        if violation_type == "trace_width":
            actual = violation.get("actual", "?")
            required = violation.get("required", "?")
            current = violation.get("current", "?")
            return (
                f"Power trace width {actual}mm is less than required {required}mm "
                f"for {current}A. Insufficient trace width may cause excessive "
                f"voltage drop and overheating."
            )

        if violation_type == "decoupling":
            missing = violation.get("missing", [])
            if missing:
                caps_str = ", ".join(str(c) for c in missing)
                return (
                    f"Power rail missing recommended decoupling capacitors: {caps_str}. "
                    f"Add capacitors near power pins for proper voltage regulation."
                )
            return "Power rail requires decoupling capacitors near load."

        if violation_type == "voltage_drop":
            drop = violation.get("drop", "?")
            max_drop = violation.get("max_allowed", "?")
            return (
                f"Power rail voltage drop {drop}V exceeds maximum {max_drop}V. "
                f"Increase trace width or use power planes."
            )

        # Fallback for unknown violation types
        return violation.get("message", str(violation))

    @staticmethod
    def _width_for_current(amps: float) -> float:
        """Calculate minimum trace width for given current.

        Uses IPC-2221 approximation for 1oz copper with 10°C temperature rise.
        This is a simplified calculation; actual requirements depend on copper
        weight, ambient temperature, and acceptable temperature rise.

        Args:
            amps: Current in amperes.

        Returns:
            Minimum trace width in mm.
        """
        # IPC-2221 approximation for external traces, 1oz copper, 10°C rise
        # Width (mm) ≈ 0.1 + (current * 0.3)
        # This is conservative for typical PCB applications
        return round(0.1 + (amps * 0.3), 2)

    def _decoupling_spec(self, current: float) -> list[dict[str, Any]]:
        """Recommend decoupling capacitors based on current.

        Args:
            current: Maximum current in amperes.

        Returns:
            List of recommended capacitors with values and counts.
        """
        # Find the appropriate threshold
        for threshold, caps in reversed(self._DECOUPLING_THRESHOLDS):
            if current >= threshold:
                return caps

        # Default for very low current
        return [{"value": "100nF", "count": 1}]


# Register power interface in the global registry
def _register_power_interface() -> None:
    """Register power interface in the global registry."""
    spec = PowerInterfaceSpec()
    REGISTRY.register(spec)


# Auto-register on module import
_register_power_interface()
