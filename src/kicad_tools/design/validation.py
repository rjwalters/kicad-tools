"""
Cross-level validation for multi-resolution design abstraction.

This module ensures consistency between high-level subsystem definitions
and low-level component placements. It detects when manual moves break
subsystem constraints and provides warnings.

Example::

    from kicad_tools.design.validation import AbstractionValidator

    validator = AbstractionValidator()
    issues = validator.validate(design)

    for issue in issues:
        print(f"{issue.severity}: {issue.message}")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from kicad_tools.design.subsystems import (
    OptimizationGoal,
    SubsystemType,
)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


class ValidationSeverity(Enum):
    """Severity levels for validation issues."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    """A validation issue detected during cross-level checking.

    Attributes:
        severity: How serious the issue is
        message: Human-readable description
        subsystem: Subsystem affected (if any)
        component: Component causing the issue (if any)
        rule_violated: Name of the rule that was violated
        actual_value: The actual measured value
        expected_value: The expected/limit value
        suggestion: How to fix the issue
    """

    severity: ValidationSeverity
    message: str
    subsystem: str = ""
    component: str = ""
    rule_violated: str = ""
    actual_value: float | None = None
    expected_value: float | None = None
    suggestion: str = ""


@dataclass
class Subsystem:
    """A defined subsystem with its components and constraints.

    Attributes:
        name: Unique name for this subsystem instance
        subsystem_type: Type of subsystem
        components: List of component references in this subsystem
        anchor: The anchor component
        anchor_position: Position of the anchor when subsystem was created
        constraints: Placement constraints for this subsystem
        optimization_goal: Goal used when placing
    """

    name: str
    subsystem_type: SubsystemType
    components: list[str]
    anchor: str
    anchor_position: tuple[float, float]
    constraints: dict[str, float] = field(default_factory=dict)
    optimization_goal: OptimizationGoal = OptimizationGoal.ROUTING


@dataclass
class SubsystemConstraint:
    """A constraint within a subsystem.

    Attributes:
        component: Component this constraint applies to
        relative_to: Reference component
        max_distance_mm: Maximum allowed distance
        min_distance_mm: Minimum required distance
        rationale: Why this constraint exists
    """

    component: str
    relative_to: str
    max_distance_mm: float
    min_distance_mm: float = 0.0
    rationale: str = ""


# Default constraints for subsystem types
DEFAULT_CONSTRAINTS: dict[SubsystemType, list[SubsystemConstraint]] = {
    SubsystemType.POWER_SUPPLY: [
        SubsystemConstraint(
            component="input_cap",
            relative_to="regulator",
            max_distance_mm=3.0,
            rationale="Input capacitor must be within 3mm of VIN for filtering",
        ),
        SubsystemConstraint(
            component="output_cap",
            relative_to="regulator",
            max_distance_mm=2.0,
            rationale="Output capacitor must be within 2mm of VOUT for stability",
        ),
        SubsystemConstraint(
            component="inductor",
            relative_to="regulator",
            max_distance_mm=5.0,
            rationale="Inductor must be within 5mm of switch node",
        ),
    ],
    SubsystemType.MCU_CORE: [
        SubsystemConstraint(
            component="bypass_cap",
            relative_to="mcu",
            max_distance_mm=4.0,
            rationale="Bypass capacitors must be within 4mm of MCU power pins",
        ),
        SubsystemConstraint(
            component="crystal",
            relative_to="mcu",
            max_distance_mm=5.0,
            rationale="Crystal must be within 5mm of OSC pins",
        ),
        SubsystemConstraint(
            component="load_cap",
            relative_to="crystal",
            max_distance_mm=2.0,
            rationale="Load capacitors must be within 2mm of crystal",
        ),
    ],
    SubsystemType.CONNECTOR: [
        SubsystemConstraint(
            component="esd_protection",
            relative_to="connector",
            max_distance_mm=5.0,
            rationale="ESD protection must be within 5mm of connector",
        ),
        SubsystemConstraint(
            component="filter",
            relative_to="connector",
            max_distance_mm=10.0,
            rationale="Filters should be within 10mm of connector",
        ),
    ],
}


class AbstractionValidator:
    """Validates consistency across abstraction levels.

    This class checks that manual component moves don't violate
    subsystem constraints, and that subsystem definitions are
    internally consistent.
    """

    def __init__(self) -> None:
        """Initialize the validator."""
        self._subsystems: list[Subsystem] = []

    def register_subsystem(self, subsystem: Subsystem) -> None:
        """Register a subsystem for validation.

        Args:
            subsystem: Subsystem to register
        """
        self._subsystems.append(subsystem)

    def clear_subsystems(self) -> None:
        """Clear all registered subsystems."""
        self._subsystems = []

    def validate(self, pcb: PCB) -> list[ValidationIssue]:
        """Validate all registered subsystems against the PCB.

        Args:
            pcb: The PCB to validate against

        Returns:
            List of validation issues found
        """
        issues: list[ValidationIssue] = []

        for subsystem in self._subsystems:
            subsystem_issues = self._validate_subsystem(subsystem, pcb)
            issues.extend(subsystem_issues)

        return issues

    def validate_move(
        self,
        ref: str,
        new_x: float,
        new_y: float,
        pcb: PCB,
    ) -> list[ValidationIssue]:
        """Check if a proposed move would violate subsystem constraints.

        This is called before applying a low-level move to warn about
        potential subsystem constraint violations.

        Args:
            ref: Component reference being moved
            new_x: New X position
            new_y: New Y position
            pcb: Current PCB state

        Returns:
            List of validation issues (warnings about constraint violations)
        """
        issues: list[ValidationIssue] = []

        # Find subsystems containing this component
        for subsystem in self._subsystems:
            if ref in subsystem.components:
                # Check if move violates constraints
                move_issues = self._check_move_constraints(subsystem, ref, new_x, new_y, pcb)
                issues.extend(move_issues)

        return issues

    def _validate_subsystem(
        self,
        subsystem: Subsystem,
        pcb: PCB,
    ) -> list[ValidationIssue]:
        """Validate a single subsystem.

        Args:
            subsystem: Subsystem to validate
            pcb: PCB to check against

        Returns:
            List of validation issues
        """
        issues: list[ValidationIssue] = []

        # Get component positions from PCB
        positions = self._get_component_positions(subsystem.components, pcb)

        # Check that anchor is present
        if subsystem.anchor not in positions:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    message=f"Anchor component '{subsystem.anchor}' not found in PCB",
                    subsystem=subsystem.name,
                    component=subsystem.anchor,
                )
            )
            return issues

        anchor_pos = positions[subsystem.anchor]

        # Check that anchor hasn't moved too far from original position
        anchor_drift = self._distance(anchor_pos, subsystem.anchor_position)
        if anchor_drift > 1.0:  # More than 1mm drift
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.INFO,
                    message=f"Anchor '{subsystem.anchor}' has moved {anchor_drift:.1f}mm from original position",
                    subsystem=subsystem.name,
                    component=subsystem.anchor,
                    actual_value=anchor_drift,
                    expected_value=0.0,
                )
            )

        # Get default constraints for this subsystem type
        constraints = DEFAULT_CONSTRAINTS.get(subsystem.subsystem_type, [])

        # Check each constraint
        for constraint in constraints:
            constraint_issues = self._check_constraint(subsystem, constraint, positions, pcb)
            issues.extend(constraint_issues)

        return issues

    def _check_constraint(
        self,
        subsystem: Subsystem,
        constraint: SubsystemConstraint,
        positions: dict[str, tuple[float, float]],
        pcb: PCB,
    ) -> list[ValidationIssue]:
        """Check a single constraint.

        Args:
            subsystem: Subsystem being checked
            constraint: Constraint to check
            positions: Component positions
            pcb: PCB for context

        Returns:
            List of validation issues
        """
        issues: list[ValidationIssue] = []

        # Find components matching the constraint roles
        # For now, use simple heuristics based on reference prefixes
        component_ref = self._find_component_by_role(constraint.component, subsystem.components)
        relative_ref = self._find_component_by_role(constraint.relative_to, subsystem.components)

        if not component_ref or component_ref not in positions:
            return issues  # Component not found, skip

        if not relative_ref or relative_ref not in positions:
            return issues  # Reference not found, skip

        component_pos = positions[component_ref]
        relative_pos = positions[relative_ref]

        distance = self._distance(component_pos, relative_pos)

        if distance > constraint.max_distance_mm:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"{component_ref} is {distance:.1f}mm from {relative_ref}, "
                        f"should be within {constraint.max_distance_mm:.1f}mm. "
                        f"{constraint.rationale}"
                    ),
                    subsystem=subsystem.name,
                    component=component_ref,
                    rule_violated=f"{constraint.component}_distance",
                    actual_value=distance,
                    expected_value=constraint.max_distance_mm,
                    suggestion=f"Move {component_ref} closer to {relative_ref}",
                )
            )

        if constraint.min_distance_mm > 0 and distance < constraint.min_distance_mm:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"{component_ref} is only {distance:.1f}mm from {relative_ref}, "
                        f"should be at least {constraint.min_distance_mm:.1f}mm"
                    ),
                    subsystem=subsystem.name,
                    component=component_ref,
                    rule_violated=f"{constraint.component}_min_distance",
                    actual_value=distance,
                    expected_value=constraint.min_distance_mm,
                    suggestion=f"Move {component_ref} further from {relative_ref}",
                )
            )

        return issues

    def _check_move_constraints(
        self,
        subsystem: Subsystem,
        ref: str,
        new_x: float,
        new_y: float,
        pcb: PCB,
    ) -> list[ValidationIssue]:
        """Check if a move violates subsystem constraints.

        Args:
            subsystem: Subsystem containing the component
            ref: Component being moved
            new_x: New X position
            new_y: New Y position
            pcb: Current PCB

        Returns:
            List of warning issues
        """
        issues: list[ValidationIssue] = []

        # Get current positions
        positions = self._get_component_positions(subsystem.components, pcb)

        # Update with proposed new position
        positions[ref] = (new_x, new_y)

        # Check constraints with new position
        constraints = DEFAULT_CONSTRAINTS.get(subsystem.subsystem_type, [])

        for constraint in constraints:
            # Find which component role matches the moved component
            component_ref = self._find_component_by_role(constraint.component, subsystem.components)
            relative_ref = self._find_component_by_role(
                constraint.relative_to, subsystem.components
            )

            if component_ref not in (ref, None) and relative_ref not in (ref, None):
                continue  # This constraint doesn't involve the moved component

            if not component_ref or component_ref not in positions:
                continue
            if not relative_ref or relative_ref not in positions:
                continue

            distance = self._distance(positions[component_ref], positions[relative_ref])

            if distance > constraint.max_distance_mm:
                issues.append(
                    ValidationIssue(
                        severity=ValidationSeverity.WARNING,
                        message=(
                            f"Moving {ref} would place {component_ref} {distance:.1f}mm "
                            f"from {relative_ref}, violating the {constraint.max_distance_mm:.1f}mm "
                            f"maximum distance constraint for {subsystem.name}"
                        ),
                        subsystem=subsystem.name,
                        component=ref,
                        rule_violated=f"{constraint.component}_distance",
                        actual_value=distance,
                        expected_value=constraint.max_distance_mm,
                        suggestion=f"Consider keeping {ref} within subsystem bounds",
                    )
                )

        return issues

    def _get_component_positions(
        self,
        refs: list[str],
        pcb: PCB,
    ) -> dict[str, tuple[float, float]]:
        """Get positions of components from PCB.

        Args:
            refs: Component references to find
            pcb: PCB to search

        Returns:
            Dictionary mapping refs to positions
        """
        positions: dict[str, tuple[float, float]] = {}

        for fp in pcb.footprints:
            if fp.reference in refs:
                positions[fp.reference] = fp.position

        return positions

    def _find_component_by_role(
        self,
        role: str,
        components: list[str],
    ) -> str | None:
        """Find a component matching a role name.

        Uses simple heuristics based on reference designator prefixes.

        Args:
            role: Role name (e.g., "regulator", "input_cap")
            components: List of component references

        Returns:
            Component reference or None if not found
        """
        # Role to prefix mapping
        role_prefixes = {
            "regulator": ["U", "IC"],
            "mcu": ["U", "IC"],
            "input_cap": ["C"],
            "output_cap": ["C"],
            "bypass_cap": ["C"],
            "load_cap": ["C"],
            "cap": ["C"],
            "inductor": ["L"],
            "crystal": ["Y", "X"],
            "resistor": ["R"],
            "connector": ["J", "P"],
            "esd_protection": ["D", "TVS"],
            "filter": ["FB", "L", "C"],
        }

        prefixes = role_prefixes.get(role.lower(), [])

        for comp in components:
            for prefix in prefixes:
                if comp.upper().startswith(prefix):
                    return comp

        return None

    def _distance(
        self,
        pos1: tuple[float, float],
        pos2: tuple[float, float],
    ) -> float:
        """Calculate distance between two positions.

        Args:
            pos1: First position
            pos2: Second position

        Returns:
            Distance in mm
        """
        dx = pos2[0] - pos1[0]
        dy = pos2[1] - pos1[1]
        return math.sqrt(dx * dx + dy * dy)


__all__ = [
    "ValidationSeverity",
    "ValidationIssue",
    "Subsystem",
    "SubsystemConstraint",
    "AbstractionValidator",
    "DEFAULT_CONSTRAINTS",
]
