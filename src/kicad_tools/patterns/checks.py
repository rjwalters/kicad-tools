"""
Validation checks for PCB patterns.

This module provides a registry of validation checks that can be used in
both Python-defined and YAML-defined patterns. Each check implements a
specific validation rule that can be configured via parameters.

Example::

    from kicad_tools.patterns.checks import (
        ComponentDistanceCheck,
        ValueMatchCheck,
        get_check,
    )

    # Use a check directly
    check = ComponentDistanceCheck(from_component="thermistor", to_component="filter_cap", max_mm=5.0)
    violation = check.validate(pcb, component_positions)

    # Get a check by name (for YAML patterns)
    check_class = get_check("component_distance")
    check = check_class(from_component="thermistor", to_component="filter_cap", max_mm=5.0)
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .schema import PatternViolation, PlacementPriority

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class CheckContext:
    """Context for validation checks.

    Provides access to component positions, net information, and other
    PCB data needed for validation.

    Attributes:
        component_positions: Dict mapping component references to (x, y) positions
        component_values: Dict mapping component references to their values
        component_footprints: Dict mapping component references to footprint names
        net_lengths: Dict mapping net names to trace lengths in mm
        pcb_path: Path to the PCB file being validated
    """

    component_positions: dict[str, tuple[float, float]]
    component_values: dict[str, str]
    component_footprints: dict[str, str]
    net_lengths: dict[str, float]
    pcb_path: Path | None = None


class ValidationCheck(ABC):
    """Base class for validation checks.

    Validation checks are reusable rules that can be applied to PCB layouts
    to verify they meet pattern requirements. Each check has a name that can
    be referenced in YAML pattern definitions.

    Subclasses must implement:
        - name: Class attribute with the check name
        - validate(): Method to perform the validation

    Example::

        class MyCustomCheck(ValidationCheck):
            name = "my_custom_check"

            def __init__(self, param1: str, param2: float):
                self.param1 = param1
                self.param2 = param2

            def validate(self, context: CheckContext) -> PatternViolation | None:
                # Validation logic here
                ...
    """

    name: str = ""  # Override in subclasses

    @abstractmethod
    def validate(self, context: CheckContext) -> PatternViolation | None:
        """Perform the validation check.

        Args:
            context: CheckContext with PCB data

        Returns:
            PatternViolation if the check fails, None if it passes
        """


class ComponentDistanceCheck(ValidationCheck):
    """Check that two components are within a maximum distance.

    This is commonly used for decoupling capacitors that must be placed
    close to their associated IC pins.

    Args:
        from_component: First component reference or role
        to_component: Second component reference or role
        max_mm: Maximum allowed distance in mm
        min_mm: Minimum required distance in mm (optional)
        rationale: Explanation for the constraint

    Example::

        check = ComponentDistanceCheck(
            from_component="thermistor",
            to_component="filter_cap",
            max_mm=5.0,
            rationale="Filter cap close to sensor for noise rejection"
        )
    """

    name = "component_distance"

    def __init__(
        self,
        from_component: str,
        to_component: str,
        max_mm: float,
        min_mm: float = 0.0,
        rationale: str = "",
    ) -> None:
        self.from_component = from_component
        self.to_component = to_component
        self.max_mm = max_mm
        self.min_mm = min_mm
        self.rationale = rationale

    def validate(self, context: CheckContext) -> PatternViolation | None:
        """Check distance between two components."""
        pos1 = context.component_positions.get(self.from_component)
        pos2 = context.component_positions.get(self.to_component)

        if pos1 is None:
            return PatternViolation(
                rule=None,
                component=self.from_component,
                message=f"Component '{self.from_component}' not found in layout",
                severity=PlacementPriority.CRITICAL,
            )

        if pos2 is None:
            return PatternViolation(
                rule=None,
                component=self.to_component,
                message=f"Component '{self.to_component}' not found in layout",
                severity=PlacementPriority.CRITICAL,
            )

        distance = math.sqrt((pos2[0] - pos1[0]) ** 2 + (pos2[1] - pos1[1]) ** 2)

        if distance > self.max_mm:
            return PatternViolation(
                rule=None,
                component=self.to_component,
                message=(
                    f"{self.to_component} is too far from {self.from_component}: "
                    f"{distance:.2f}mm > {self.max_mm:.2f}mm. {self.rationale}"
                ),
                severity=PlacementPriority.HIGH,
                actual_value=distance,
                expected_value=self.max_mm,
            )

        if distance < self.min_mm:
            return PatternViolation(
                rule=None,
                component=self.to_component,
                message=(
                    f"{self.to_component} is too close to {self.from_component}: "
                    f"{distance:.2f}mm < {self.min_mm:.2f}mm"
                ),
                severity=PlacementPriority.MEDIUM,
                actual_value=distance,
                expected_value=self.min_mm,
            )

        return None


class ValueMatchCheck(ValidationCheck):
    """Check that a component value matches another component's value.

    Used for matched components like bias resistors that should equal
    the sensor resistance.

    Args:
        component: Component to check
        equals: Component whose value should match
        tolerance_percent: Allowed tolerance (default 0 for exact match)
        rationale: Explanation for the constraint

    Example::

        check = ValueMatchCheck(
            component="bias_resistor",
            equals="thermistor",
            tolerance_percent=5.0,
            rationale="Bias resistor should match thermistor for voltage divider"
        )
    """

    name = "value_match"

    def __init__(
        self,
        component: str,
        equals: str,
        tolerance_percent: float = 0.0,
        rationale: str = "",
    ) -> None:
        self.component = component
        self.equals = equals
        self.tolerance_percent = tolerance_percent
        self.rationale = rationale

    def validate(self, context: CheckContext) -> PatternViolation | None:
        """Check that component values match."""
        value1 = context.component_values.get(self.component)
        value2 = context.component_values.get(self.equals)

        if value1 is None:
            return PatternViolation(
                rule=None,
                component=self.component,
                message=f"Component '{self.component}' not found",
                severity=PlacementPriority.CRITICAL,
            )

        if value2 is None:
            return PatternViolation(
                rule=None,
                component=self.equals,
                message=f"Component '{self.equals}' not found",
                severity=PlacementPriority.CRITICAL,
            )

        # Parse numeric values (handle common suffixes)
        num1 = self._parse_value(value1)
        num2 = self._parse_value(value2)

        if num1 is None or num2 is None:
            # Can't compare non-numeric values, just check string equality
            if value1 != value2:
                return PatternViolation(
                    rule=None,
                    component=self.component,
                    message=(
                        f"{self.component} value '{value1}' does not match "
                        f"{self.equals} value '{value2}'. {self.rationale}"
                    ),
                    severity=PlacementPriority.HIGH,
                )
            return None

        # Calculate tolerance
        if self.tolerance_percent > 0 and num2 != 0:
            tolerance = abs(num2 * self.tolerance_percent / 100)
            if abs(num1 - num2) > tolerance:
                return PatternViolation(
                    rule=None,
                    component=self.component,
                    message=(
                        f"{self.component} value {value1} is outside "
                        f"{self.tolerance_percent}% tolerance of {self.equals} "
                        f"value {value2}. {self.rationale}"
                    ),
                    severity=PlacementPriority.HIGH,
                    actual_value=num1,
                    expected_value=num2,
                )
        elif num1 != num2:
            return PatternViolation(
                rule=None,
                component=self.component,
                message=(
                    f"{self.component} value {value1} does not match "
                    f"{self.equals} value {value2}. {self.rationale}"
                ),
                severity=PlacementPriority.HIGH,
                actual_value=num1,
                expected_value=num2,
            )

        return None

    def _parse_value(self, value: str) -> float | None:
        """Parse a component value string to a number."""
        multipliers = {
            "p": 1e-12,
            "n": 1e-9,
            "u": 1e-6,
            "µ": 1e-6,
            "m": 1e-3,
            "k": 1e3,
            "K": 1e3,
            "M": 1e6,
            "G": 1e9,
        }

        value = value.strip()
        if not value:
            return None

        # Remove common units
        for unit in ["F", "H", "R", "Ohm", "ohm", "V", "A"]:
            value = value.replace(unit, "")

        value = value.strip()
        if not value:
            return None

        # Check for multiplier suffix
        multiplier = 1.0
        if value[-1] in multipliers:
            multiplier = multipliers[value[-1]]
            value = value[:-1]

        try:
            return float(value) * multiplier
        except ValueError:
            return None


class TraceLengthCheck(ValidationCheck):
    """Check that a net's trace length is within limits.

    Used for timing-critical signals that require matched or limited
    trace lengths.

    Args:
        net: Net name to check
        max_mm: Maximum allowed trace length
        min_mm: Minimum required trace length (optional)
        rationale: Explanation for the constraint

    Example::

        check = TraceLengthCheck(
            net="CLK",
            max_mm=50.0,
            rationale="Clock trace must be short to minimize delay"
        )
    """

    name = "trace_length"

    def __init__(
        self,
        net: str,
        max_mm: float | None = None,
        min_mm: float | None = None,
        rationale: str = "",
    ) -> None:
        self.net = net
        self.max_mm = max_mm
        self.min_mm = min_mm
        self.rationale = rationale

    def validate(self, context: CheckContext) -> PatternViolation | None:
        """Check trace length is within limits."""
        length = context.net_lengths.get(self.net)

        if length is None:
            return PatternViolation(
                rule=None,
                component=self.net,
                message=f"Net '{self.net}' not found or has no routed trace",
                severity=PlacementPriority.MEDIUM,
            )

        if self.max_mm is not None and length > self.max_mm:
            return PatternViolation(
                rule=None,
                component=self.net,
                message=(
                    f"Net '{self.net}' trace is too long: "
                    f"{length:.2f}mm > {self.max_mm:.2f}mm. {self.rationale}"
                ),
                severity=PlacementPriority.HIGH,
                actual_value=length,
                expected_value=self.max_mm,
            )

        if self.min_mm is not None and length < self.min_mm:
            return PatternViolation(
                rule=None,
                component=self.net,
                message=(
                    f"Net '{self.net}' trace is too short: "
                    f"{length:.2f}mm < {self.min_mm:.2f}mm. {self.rationale}"
                ),
                severity=PlacementPriority.MEDIUM,
                actual_value=length,
                expected_value=self.min_mm,
            )

        return None


class ValueRangeCheck(ValidationCheck):
    """Check that a component value is within an allowed range.

    Used to verify components meet specification requirements.

    Args:
        component: Component to check
        min_value: Minimum allowed value (with unit suffix)
        max_value: Maximum allowed value (with unit suffix)
        rationale: Explanation for the constraint

    Example::

        check = ValueRangeCheck(
            component="filter_cap",
            min_value="100n",
            max_value="1u",
            rationale="Filter cap must be 100nF-1uF for proper filtering"
        )
    """

    name = "value_range"

    def __init__(
        self,
        component: str,
        min_value: str | None = None,
        max_value: str | None = None,
        rationale: str = "",
    ) -> None:
        self.component = component
        self.min_value = min_value
        self.max_value = max_value
        self.rationale = rationale

    def validate(self, context: CheckContext) -> PatternViolation | None:
        """Check component value is within range."""
        value_str = context.component_values.get(self.component)

        if value_str is None:
            return PatternViolation(
                rule=None,
                component=self.component,
                message=f"Component '{self.component}' not found",
                severity=PlacementPriority.CRITICAL,
            )

        value = self._parse_value(value_str)
        if value is None:
            return PatternViolation(
                rule=None,
                component=self.component,
                message=f"Cannot parse value '{value_str}' for {self.component}",
                severity=PlacementPriority.MEDIUM,
            )

        if self.min_value is not None:
            min_val = self._parse_value(self.min_value)
            if min_val is not None and value < min_val:
                return PatternViolation(
                    rule=None,
                    component=self.component,
                    message=(
                        f"{self.component} value {value_str} is below minimum "
                        f"{self.min_value}. {self.rationale}"
                    ),
                    severity=PlacementPriority.HIGH,
                    actual_value=value,
                    expected_value=min_val,
                )

        if self.max_value is not None:
            max_val = self._parse_value(self.max_value)
            if max_val is not None and value > max_val:
                return PatternViolation(
                    rule=None,
                    component=self.component,
                    message=(
                        f"{self.component} value {value_str} is above maximum "
                        f"{self.max_value}. {self.rationale}"
                    ),
                    severity=PlacementPriority.HIGH,
                    actual_value=value,
                    expected_value=max_val,
                )

        return None

    def _parse_value(self, value: str) -> float | None:
        """Parse a component value string to a number."""
        # Reuse ValueMatchCheck's parsing logic
        return ValueMatchCheck._parse_value(self, value)


class ComponentPresentCheck(ValidationCheck):
    """Check that a required component is present in the layout.

    Args:
        component: Component reference or role that must be present
        optional: If True, only warn if missing; if False, it's an error
        rationale: Explanation for why this component is needed

    Example::

        check = ComponentPresentCheck(
            component="protection_diode",
            optional=True,
            rationale="ESD protection diode recommended for USB data lines"
        )
    """

    name = "component_present"

    def __init__(
        self,
        component: str,
        optional: bool = False,
        rationale: str = "",
    ) -> None:
        self.component = component
        self.optional = optional
        self.rationale = rationale

    def validate(self, context: CheckContext) -> PatternViolation | None:
        """Check that component is present."""
        if self.component not in context.component_positions:
            severity = PlacementPriority.LOW if self.optional else PlacementPriority.CRITICAL
            return PatternViolation(
                rule=None,
                component=self.component,
                message=(
                    f"{'Optional component' if self.optional else 'Required component'} "
                    f"'{self.component}' not found. {self.rationale}"
                ),
                severity=severity,
            )
        return None


# Registry of available validation checks
VALIDATION_CHECKS: dict[str, type[ValidationCheck]] = {
    "component_distance": ComponentDistanceCheck,
    "value_match": ValueMatchCheck,
    "trace_length": TraceLengthCheck,
    "value_range": ValueRangeCheck,
    "component_present": ComponentPresentCheck,
}


def get_check(name: str) -> type[ValidationCheck]:
    """Get a validation check class by name.

    Args:
        name: Name of the check (e.g., "component_distance")

    Returns:
        The validation check class

    Raises:
        KeyError: If no check with that name exists
    """
    if name not in VALIDATION_CHECKS:
        available = ", ".join(sorted(VALIDATION_CHECKS.keys()))
        raise KeyError(f"Unknown validation check '{name}'. Available: {available}")
    return VALIDATION_CHECKS[name]


def register_check(check_class: type[ValidationCheck]) -> type[ValidationCheck]:
    """Register a custom validation check.

    Can be used as a decorator::

        @register_check
        class MyCustomCheck(ValidationCheck):
            name = "my_custom_check"
            ...

    Args:
        check_class: The validation check class to register

    Returns:
        The same class (for use as decorator)

    Raises:
        ValueError: If the check has no name or name is already registered
    """
    if not check_class.name:
        raise ValueError(f"Check class {check_class.__name__} has no 'name' attribute")

    if check_class.name in VALIDATION_CHECKS:
        raise ValueError(f"Check '{check_class.name}' is already registered")

    VALIDATION_CHECKS[check_class.name] = check_class
    return check_class


def create_check(name: str, params: dict[str, Any]) -> ValidationCheck:
    """Create a validation check instance from name and parameters.

    This is used by the YAML loader to instantiate checks from
    YAML definitions.

    Args:
        name: Name of the check type
        params: Parameters to pass to the check constructor

    Returns:
        Instantiated validation check

    Example::

        check = create_check("component_distance", {
            "from_component": "thermistor",
            "to_component": "filter_cap",
            "max_mm": 5.0
        })
    """
    check_class = get_check(name)
    return check_class(**params)
