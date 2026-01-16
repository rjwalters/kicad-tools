"""Pattern validation framework.

Validates instantiated circuit patterns against their specifications,
checking placement rules, routing constraints, and component values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.schematic.blocks.base import CircuitBlock


class ViolationSeverity(str, Enum):
    """Severity levels for pattern violations."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class PatternViolation:
    """Represents a single pattern validation violation.

    Attributes:
        severity: Violation severity level (error, warning, info)
        rule: Name of the rule that was violated
        message: Human-readable description of the violation
        component: Reference designator of the component involved (if any)
        location: (x_mm, y_mm) tuple of the violation location (if applicable)
        fix_suggestion: Suggested action to fix the violation
    """

    severity: Literal["error", "warning", "info"]
    rule: str
    message: str
    component: str | None = None
    location: tuple[float, float] | None = None
    fix_suggestion: str | None = None

    def __post_init__(self) -> None:
        """Validate severity value."""
        if self.severity not in ("error", "warning", "info"):
            raise ValueError(
                f"severity must be 'error', 'warning', or 'info', got {self.severity!r}"
            )

    @property
    def is_error(self) -> bool:
        """Check if this is an error (not a warning or info)."""
        return self.severity == "error"

    @property
    def is_warning(self) -> bool:
        """Check if this is a warning."""
        return self.severity == "warning"

    @property
    def is_info(self) -> bool:
        """Check if this is informational."""
        return self.severity == "info"

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "severity": self.severity,
            "rule": self.rule,
            "message": self.message,
            "component": self.component,
            "location": list(self.location) if self.location else None,
            "fix_suggestion": self.fix_suggestion,
        }


@dataclass
class PatternValidationResult:
    """Aggregates all violations from pattern validation.

    Attributes:
        violations: List of all violations found
        pattern_type: Name of the pattern that was validated
        rules_checked: Number of rules that were checked
    """

    violations: list[PatternViolation] = field(default_factory=list)
    pattern_type: str = ""
    rules_checked: int = 0

    @property
    def error_count(self) -> int:
        """Count of violations with severity='error'."""
        return sum(1 for v in self.violations if v.is_error)

    @property
    def warning_count(self) -> int:
        """Count of violations with severity='warning'."""
        return sum(1 for v in self.violations if v.is_warning)

    @property
    def info_count(self) -> int:
        """Count of violations with severity='info'."""
        return sum(1 for v in self.violations if v.is_info)

    @property
    def passed(self) -> bool:
        """True if no errors (warnings and info are allowed)."""
        return self.error_count == 0

    @property
    def errors(self) -> list[PatternViolation]:
        """List of only error violations."""
        return [v for v in self.violations if v.is_error]

    @property
    def warnings(self) -> list[PatternViolation]:
        """List of only warning violations."""
        return [v for v in self.violations if v.is_warning]

    def add(self, violation: PatternViolation) -> None:
        """Add a violation to the results."""
        self.violations.append(violation)

    def __iter__(self):
        """Iterate over all violations."""
        return iter(self.violations)

    def __len__(self) -> int:
        """Total number of violations."""
        return len(self.violations)

    def __bool__(self) -> bool:
        """True if there are any violations."""
        return len(self.violations) > 0

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "passed": self.passed,
            "pattern_type": self.pattern_type,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "rules_checked": self.rules_checked,
            "violations": [v.to_dict() for v in self.violations],
        }

    def summary(self) -> str:
        """Generate a human-readable summary."""
        status = "PASSED" if self.passed else "FAILED"
        return (
            f"Pattern Validation {status}: {self.error_count} errors, "
            f"{self.warning_count} warnings ({self.rules_checked} rules checked)"
        )


@dataclass
class PlacementRule:
    """Rule for component placement validation.

    Attributes:
        name: Rule identifier
        component: Component reference to validate
        related_component: Component to measure distance from
        max_distance_mm: Maximum allowed distance (if applicable)
        min_distance_mm: Minimum allowed distance (if applicable)
        description: Human-readable description of the rule
    """

    name: str
    component: str
    related_component: str
    max_distance_mm: float | None = None
    min_distance_mm: float | None = None
    description: str = ""


@dataclass
class ComponentValueRule:
    """Rule for component value validation.

    Attributes:
        name: Rule identifier
        component: Component reference to validate
        parameter: Parameter to check (e.g., "capacitance", "resistance")
        min_value: Minimum allowed value
        max_value: Maximum allowed value
        unit: Unit of measurement (e.g., "uF", "ohm")
        description: Human-readable description of the rule
    """

    name: str
    component: str
    parameter: str
    min_value: float | None = None
    max_value: float | None = None
    unit: str = ""
    description: str = ""


class PatternValidator:
    """Validates circuit pattern implementations against specifications.

    The PatternValidator checks that instantiated patterns meet their
    design requirements including:
    - Placement rules (component distances, orientation)
    - Routing constraints (trace lengths, impedance)
    - Component values (capacitance, resistance, voltage ratings)

    Example:
        >>> from kicad_tools.patterns import PatternValidator
        >>> from kicad_tools.schema.pcb import PCB
        >>>
        >>> pcb = PCB.load("board.kicad_pcb")
        >>> validator = PatternValidator()
        >>> result = validator.validate_ldo_pattern(
        ...     pcb,
        ...     regulator="U1",
        ...     input_cap="C1",
        ...     output_caps=["C2", "C3"],
        ... )
        >>> for violation in result:
        ...     print(f"{violation.severity}: {violation.message}")
    """

    def __init__(self) -> None:
        """Initialize the pattern validator."""
        self._placement_rules: list[PlacementRule] = []
        self._value_rules: list[ComponentValueRule] = []

    def validate(
        self,
        pcb: PCB,
        pattern: CircuitBlock,
    ) -> PatternValidationResult:
        """Validate a pattern implementation against its specification.

        Args:
            pcb: The PCB containing the pattern implementation
            pattern: The circuit block pattern to validate

        Returns:
            PatternValidationResult with any violations found
        """
        result = PatternValidationResult(
            pattern_type=pattern.__class__.__name__,
        )

        # Check placement rules
        result.violations.extend(self._check_placement_rules(pcb, pattern))
        result.rules_checked += len(self._placement_rules)

        # Check component values
        result.violations.extend(self._check_component_values(pcb, pattern))
        result.rules_checked += len(self._value_rules)

        return result

    def validate_ldo_pattern(
        self,
        pcb: PCB,
        regulator: str,
        input_cap: str,
        output_caps: list[str],
        max_input_cap_distance_mm: float = 3.0,
        max_output_cap_distance_mm: float = 5.0,
        min_input_cap_uf: float = 10.0,
        min_output_cap_uf: float = 10.0,
    ) -> PatternValidationResult:
        """Validate an LDO power supply pattern.

        Checks that:
        - Input capacitor is within specified distance of regulator VIN pin
        - Output capacitors are within specified distance of regulator VOUT pin
        - Input capacitor meets minimum capacitance requirements
        - Output capacitors meet minimum capacitance requirements

        Args:
            pcb: The PCB containing the LDO pattern
            regulator: Reference designator of the LDO (e.g., "U1")
            input_cap: Reference designator of input capacitor (e.g., "C1")
            output_caps: Reference designators of output capacitors
            max_input_cap_distance_mm: Maximum distance from VIN to input cap
            max_output_cap_distance_mm: Maximum distance from VOUT to output caps
            min_input_cap_uf: Minimum input capacitance in microfarads
            min_output_cap_uf: Minimum output capacitance in microfarads

        Returns:
            PatternValidationResult with any violations found

        Example:
            >>> result = validator.validate_ldo_pattern(
            ...     pcb,
            ...     regulator="U1",
            ...     input_cap="C1",
            ...     output_caps=["C2", "C3"],
            ...     max_input_cap_distance_mm=3.0,
            ... )
        """
        result = PatternValidationResult(pattern_type="LDO")
        rules_checked = 0

        # Get component positions
        reg_fp = pcb.get_footprint(regulator)
        if not reg_fp:
            result.add(
                PatternViolation(
                    severity="error",
                    rule="component_exists",
                    message=f"Regulator {regulator} not found on PCB",
                    component=regulator,
                )
            )
            result.rules_checked = 1
            return result

        in_cap_fp = pcb.get_footprint(input_cap)
        if not in_cap_fp:
            result.add(
                PatternViolation(
                    severity="error",
                    rule="component_exists",
                    message=f"Input capacitor {input_cap} not found on PCB",
                    component=input_cap,
                )
            )

        # Check input capacitor distance
        rules_checked += 1
        if reg_fp and in_cap_fp:
            distance = self._calculate_distance(reg_fp.position, in_cap_fp.position)
            if distance > max_input_cap_distance_mm:
                result.add(
                    PatternViolation(
                        severity="error",
                        rule="input_cap_distance",
                        message=(
                            f"{input_cap} is {distance:.1f}mm from {regulator}, "
                            f"max allowed is {max_input_cap_distance_mm}mm"
                        ),
                        component=input_cap,
                        location=in_cap_fp.position,
                        fix_suggestion=f"Move {input_cap} closer to {regulator}",
                    )
                )

        # Check input capacitor value
        rules_checked += 1
        if in_cap_fp:
            cap_value = self._parse_capacitance(in_cap_fp.value)
            if cap_value is not None and cap_value < min_input_cap_uf:
                result.add(
                    PatternViolation(
                        severity="warning",
                        rule="input_cap_value",
                        message=(
                            f"{input_cap} is {cap_value}uF, "
                            f"recommended minimum is {min_input_cap_uf}uF"
                        ),
                        component=input_cap,
                        location=in_cap_fp.position,
                        fix_suggestion=f"Replace with {min_input_cap_uf}uF capacitor",
                    )
                )

        # Check output capacitors
        for out_cap in output_caps:
            out_cap_fp = pcb.get_footprint(out_cap)
            if not out_cap_fp:
                result.add(
                    PatternViolation(
                        severity="error",
                        rule="component_exists",
                        message=f"Output capacitor {out_cap} not found on PCB",
                        component=out_cap,
                    )
                )
                continue

            # Check distance
            rules_checked += 1
            if reg_fp:
                distance = self._calculate_distance(reg_fp.position, out_cap_fp.position)
                if distance > max_output_cap_distance_mm:
                    result.add(
                        PatternViolation(
                            severity="error",
                            rule="output_cap_distance",
                            message=(
                                f"{out_cap} is {distance:.1f}mm from {regulator}, "
                                f"max allowed is {max_output_cap_distance_mm}mm"
                            ),
                            component=out_cap,
                            location=out_cap_fp.position,
                            fix_suggestion=f"Move {out_cap} closer to {regulator}",
                        )
                    )

            # Check value
            rules_checked += 1
            cap_value = self._parse_capacitance(out_cap_fp.value)
            if cap_value is not None and cap_value < min_output_cap_uf:
                result.add(
                    PatternViolation(
                        severity="warning",
                        rule="output_cap_value",
                        message=(
                            f"{out_cap} is {cap_value}uF, "
                            f"recommended minimum is {min_output_cap_uf}uF"
                        ),
                        component=out_cap,
                        location=out_cap_fp.position,
                        fix_suggestion=f"Replace with {min_output_cap_uf}uF capacitor",
                    )
                )

        result.rules_checked = rules_checked
        return result

    def validate_decoupling_pattern(
        self,
        pcb: PCB,
        ic: str,
        capacitors: list[str],
        max_distance_mm: float = 5.0,
        required_values: list[str] | None = None,
    ) -> PatternValidationResult:
        """Validate a decoupling capacitor pattern for an IC.

        Checks that:
        - All capacitors are within specified distance of the IC
        - Required capacitor values are present (if specified)

        Args:
            pcb: The PCB containing the decoupling pattern
            ic: Reference designator of the IC (e.g., "U1")
            capacitors: Reference designators of decoupling capacitors
            max_distance_mm: Maximum distance from IC to capacitors
            required_values: List of required capacitor values (e.g., ["100nF", "10uF"])

        Returns:
            PatternValidationResult with any violations found
        """
        result = PatternValidationResult(pattern_type="Decoupling")
        rules_checked = 0

        # Get IC position
        ic_fp = pcb.get_footprint(ic)
        if not ic_fp:
            result.add(
                PatternViolation(
                    severity="error",
                    rule="component_exists",
                    message=f"IC {ic} not found on PCB",
                    component=ic,
                )
            )
            result.rules_checked = 1
            return result

        found_values: list[str] = []

        # Check each capacitor
        for cap in capacitors:
            cap_fp = pcb.get_footprint(cap)
            if not cap_fp:
                result.add(
                    PatternViolation(
                        severity="error",
                        rule="component_exists",
                        message=f"Capacitor {cap} not found on PCB",
                        component=cap,
                    )
                )
                continue

            # Check distance
            rules_checked += 1
            distance = self._calculate_distance(ic_fp.position, cap_fp.position)
            if distance > max_distance_mm:
                result.add(
                    PatternViolation(
                        severity="error",
                        rule="cap_distance",
                        message=(
                            f"{cap} is {distance:.1f}mm from {ic}, "
                            f"max allowed is {max_distance_mm}mm"
                        ),
                        component=cap,
                        location=cap_fp.position,
                        fix_suggestion=f"Move {cap} closer to {ic}",
                    )
                )

            if cap_fp.value:
                found_values.append(cap_fp.value)

        # Check required values
        if required_values:
            rules_checked += 1
            missing_values = []
            for req_val in required_values:
                if not self._value_present(req_val, found_values):
                    missing_values.append(req_val)

            if missing_values:
                result.add(
                    PatternViolation(
                        severity="warning",
                        rule="required_values",
                        message=(
                            f"Missing recommended decoupling values: {', '.join(missing_values)}"
                        ),
                        component=ic,
                        fix_suggestion=f"Add capacitors with values: {', '.join(missing_values)}",
                    )
                )

        result.rules_checked = rules_checked
        return result

    def validate_buck_converter_pattern(
        self,
        pcb: PCB,
        regulator: str,
        inductor: str,
        input_cap: str,
        output_cap: str,
        diode: str | None = None,
        max_inductor_distance_mm: float = 5.0,
        max_cap_distance_mm: float = 10.0,
    ) -> PatternValidationResult:
        """Validate a buck converter pattern.

        Checks that:
        - Inductor is close to the switching regulator
        - Input and output capacitors are close to the regulator
        - Diode (for async buck) is close to the switch node

        Args:
            pcb: The PCB containing the buck converter pattern
            regulator: Reference designator of the switching regulator
            inductor: Reference designator of the inductor
            input_cap: Reference designator of input capacitor
            output_cap: Reference designator of output capacitor
            diode: Reference designator of catch diode (for async topology)
            max_inductor_distance_mm: Maximum distance from regulator to inductor
            max_cap_distance_mm: Maximum distance from regulator to capacitors

        Returns:
            PatternValidationResult with any violations found
        """
        result = PatternValidationResult(pattern_type="BuckConverter")
        rules_checked = 0

        # Get regulator position
        reg_fp = pcb.get_footprint(regulator)
        if not reg_fp:
            result.add(
                PatternViolation(
                    severity="error",
                    rule="component_exists",
                    message=f"Regulator {regulator} not found on PCB",
                    component=regulator,
                )
            )
            result.rules_checked = 1
            return result

        # Check inductor distance
        ind_fp = pcb.get_footprint(inductor)
        rules_checked += 1
        if ind_fp:
            distance = self._calculate_distance(reg_fp.position, ind_fp.position)
            if distance > max_inductor_distance_mm:
                result.add(
                    PatternViolation(
                        severity="error",
                        rule="inductor_distance",
                        message=(
                            f"{inductor} is {distance:.1f}mm from {regulator}, "
                            f"max allowed is {max_inductor_distance_mm}mm"
                        ),
                        component=inductor,
                        location=ind_fp.position,
                        fix_suggestion=(
                            f"Move {inductor} closer to {regulator} to minimize "
                            "switch node loop area"
                        ),
                    )
                )
        else:
            result.add(
                PatternViolation(
                    severity="error",
                    rule="component_exists",
                    message=f"Inductor {inductor} not found on PCB",
                    component=inductor,
                )
            )

        # Check input capacitor
        in_cap_fp = pcb.get_footprint(input_cap)
        rules_checked += 1
        if in_cap_fp:
            distance = self._calculate_distance(reg_fp.position, in_cap_fp.position)
            if distance > max_cap_distance_mm:
                result.add(
                    PatternViolation(
                        severity="error",
                        rule="input_cap_distance",
                        message=(
                            f"{input_cap} is {distance:.1f}mm from {regulator}, "
                            f"max allowed is {max_cap_distance_mm}mm"
                        ),
                        component=input_cap,
                        location=in_cap_fp.position,
                        fix_suggestion=f"Move {input_cap} closer to {regulator}",
                    )
                )
        else:
            result.add(
                PatternViolation(
                    severity="error",
                    rule="component_exists",
                    message=f"Input capacitor {input_cap} not found on PCB",
                    component=input_cap,
                )
            )

        # Check output capacitor
        out_cap_fp = pcb.get_footprint(output_cap)
        rules_checked += 1
        if out_cap_fp:
            distance = self._calculate_distance(reg_fp.position, out_cap_fp.position)
            if distance > max_cap_distance_mm:
                result.add(
                    PatternViolation(
                        severity="warning",
                        rule="output_cap_distance",
                        message=(
                            f"{output_cap} is {distance:.1f}mm from {regulator}, "
                            f"recommended max is {max_cap_distance_mm}mm"
                        ),
                        component=output_cap,
                        location=out_cap_fp.position,
                        fix_suggestion=f"Move {output_cap} closer to {regulator}",
                    )
                )
        else:
            result.add(
                PatternViolation(
                    severity="error",
                    rule="component_exists",
                    message=f"Output capacitor {output_cap} not found on PCB",
                    component=output_cap,
                )
            )

        # Check diode (for async topology)
        if diode:
            diode_fp = pcb.get_footprint(diode)
            rules_checked += 1
            if diode_fp:
                distance = self._calculate_distance(reg_fp.position, diode_fp.position)
                if distance > max_inductor_distance_mm:
                    result.add(
                        PatternViolation(
                            severity="error",
                            rule="diode_distance",
                            message=(
                                f"{diode} is {distance:.1f}mm from {regulator}, "
                                f"max allowed is {max_inductor_distance_mm}mm"
                            ),
                            component=diode,
                            location=diode_fp.position,
                            fix_suggestion=(
                                f"Move {diode} closer to {regulator} to minimize "
                                "high-current loop area"
                            ),
                        )
                    )
            else:
                result.add(
                    PatternViolation(
                        severity="error",
                        rule="component_exists",
                        message=f"Diode {diode} not found on PCB",
                        component=diode,
                    )
                )

        result.rules_checked = rules_checked
        return result

    def _check_placement_rules(
        self,
        pcb: PCB,
        pattern: CircuitBlock,
    ) -> list[PatternViolation]:
        """Check all placement rules for a pattern."""
        violations = []
        # Placement rules are pattern-specific and validated by dedicated methods
        return violations

    def _check_component_values(
        self,
        pcb: PCB,
        pattern: CircuitBlock,
    ) -> list[PatternViolation]:
        """Check all component value rules for a pattern."""
        violations = []
        # Value rules are pattern-specific and validated by dedicated methods
        return violations

    @staticmethod
    def _calculate_distance(
        pos1: tuple[float, float],
        pos2: tuple[float, float],
    ) -> float:
        """Calculate Euclidean distance between two positions."""
        import math

        return math.sqrt((pos2[0] - pos1[0]) ** 2 + (pos2[1] - pos1[1]) ** 2)

    @staticmethod
    def _parse_capacitance(value: str) -> float | None:
        """Parse a capacitance value string to microfarads.

        Args:
            value: Capacitance string like "10uF", "100nF", "0.1uF"

        Returns:
            Capacitance in microfarads, or None if parsing fails
        """
        if not value:
            return None

        value = value.strip().upper()

        # Handle common formats
        try:
            if "UF" in value or "µF" in value.upper():
                return float(value.replace("UF", "").replace("µF", "").strip())
            elif "NF" in value:
                return float(value.replace("NF", "").strip()) / 1000
            elif "PF" in value:
                return float(value.replace("PF", "").strip()) / 1_000_000
            elif "MF" in value:
                return float(value.replace("MF", "").strip()) * 1000
            else:
                # Try parsing as plain number (assume uF)
                return float(value)
        except ValueError:
            return None

    @staticmethod
    def _value_present(required: str, found: list[str]) -> bool:
        """Check if a required value is present in the found values list.

        Handles value normalization (e.g., "100nF" == "0.1uF").
        """
        req_normalized = PatternValidator._parse_capacitance(required)
        if req_normalized is None:
            return required in found

        for val in found:
            val_normalized = PatternValidator._parse_capacitance(val)
            if val_normalized is not None:
                # Allow 10% tolerance
                if abs(val_normalized - req_normalized) / req_normalized < 0.1:
                    return True

        return False
