"""Unified validation models using Pydantic.

This module provides type-safe base models for validation results and violations
across all validators (DRC, connectivity, consistency, netlist, etc.).

The generic `ValidationResult[V]` pattern enables type-safe specialization
while sharing common aggregation and filtering logic.
"""

from __future__ import annotations

from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class Severity(str, Enum):
    """Severity levels for validation violations."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ViolationCategory(str, Enum):
    """Categories of validation violations for filtering and reporting."""

    DRC = "drc"
    CONNECTIVITY = "connectivity"
    CONSISTENCY = "consistency"
    NETLIST = "netlist"
    THERMAL = "thermal"  # Future
    IMPEDANCE = "impedance"  # Future
    SIGNAL_INTEGRITY = "signal_integrity"  # Future


class Location(BaseModel):
    """Standardized location reference for violations.

    Supports various location types: coordinates, layer, component reference, net.
    """

    model_config = ConfigDict(frozen=True)

    x: float | None = None
    y: float | None = None
    layer: str | None = None
    ref: str | None = None  # Component reference (e.g., "R1", "U3")
    net: str | None = None  # Net name

    def to_tuple(self) -> tuple[float, float] | None:
        """Return (x, y) tuple if both coordinates are set."""
        if self.x is not None and self.y is not None:
            return (self.x, self.y)
        return None


class BaseViolation(BaseModel):
    """Base class for all validation violations.

    Provides common fields and properties shared by all violation types.
    Subclasses can add domain-specific fields while inheriting the base behavior.
    """

    model_config = ConfigDict(frozen=True)

    severity: Severity
    category: ViolationCategory
    message: str
    location: Location | None = None

    @property
    def is_error(self) -> bool:
        """Check if this is an error severity violation."""
        return self.severity == Severity.ERROR

    @property
    def is_warning(self) -> bool:
        """Check if this is a warning severity violation."""
        return self.severity == Severity.WARNING

    @property
    def is_info(self) -> bool:
        """Check if this is an info severity violation."""
        return self.severity == Severity.INFO

    def to_dict(self) -> dict:
        """Convert to dictionary for backwards-compatible serialization."""
        return self.model_dump(mode="json")


V = TypeVar("V", bound=BaseViolation)


class ValidationResult(BaseModel, Generic[V]):
    """Generic validation result container.

    Aggregates violations of a specific type and provides common
    filtering, counting, and iteration methods.

    Type parameter V must be a subclass of BaseViolation.
    """

    violations: list[V] = Field(default_factory=list)

    @property
    def error_count(self) -> int:
        """Count of violations with ERROR severity."""
        return sum(1 for v in self.violations if v.is_error)

    @property
    def warning_count(self) -> int:
        """Count of violations with WARNING severity."""
        return sum(1 for v in self.violations if v.is_warning)

    @property
    def info_count(self) -> int:
        """Count of violations with INFO severity."""
        return sum(1 for v in self.violations if v.is_info)

    @property
    def passed(self) -> bool:
        """True if no errors (warnings and info are allowed)."""
        return self.error_count == 0

    def errors(self) -> list[V]:
        """List of only error violations."""
        return [v for v in self.violations if v.is_error]

    def warnings(self) -> list[V]:
        """List of only warning violations."""
        return [v for v in self.violations if v.is_warning]

    def infos(self) -> list[V]:
        """List of only info violations."""
        return [v for v in self.violations if v.is_info]

    def filter_by_category(self, category: ViolationCategory) -> list[V]:
        """Get violations for a specific category."""
        return [v for v in self.violations if v.category == category]

    def __iter__(self):
        """Iterate over all violations."""
        return iter(self.violations)

    def __len__(self) -> int:
        """Total number of violations."""
        return len(self.violations)

    def __bool__(self) -> bool:
        """True if there are any violations."""
        return len(self.violations) > 0

    def add(self, violation: V) -> None:
        """Add a violation to the results."""
        self.violations.append(violation)

    def merge(self, other: ValidationResult[V]) -> None:
        """Merge violations from another result into this one."""
        self.violations.extend(other.violations)

    def to_dict(self) -> dict:
        """Convert to dictionary for backwards-compatible serialization."""
        return {
            "passed": self.passed,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "violations": [v.to_dict() for v in self.violations],
        }


# DRC-specific models


class DRCViolation(BaseViolation):
    """DRC-specific violation with rule and measurement details.

    Extends BaseViolation with fields specific to design rule checks:
    - rule_id: Identifies which DRC rule was violated
    - actual_value/required_value: Measured vs required values
    - items: Component references involved in the violation
    """

    model_config = ConfigDict(frozen=True)

    category: ViolationCategory = ViolationCategory.DRC
    rule_id: str
    layer: str | None = None
    actual_value: float | None = None
    required_value: float | None = None
    items: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """Convert to dictionary matching legacy DRCViolation format."""
        result = {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "message": self.message,
            "location": (
                [self.location.x, self.location.y]
                if self.location and self.location.x is not None
                else None
            ),
            "layer": self.layer,
            "actual_value": self.actual_value,
            "required_value": self.required_value,
            "items": list(self.items),
        }
        return result


class DRCResult(ValidationResult[DRCViolation]):
    """DRC-specific result with additional metadata.

    Extends ValidationResult with DRC-specific fields and methods.
    """

    rules_checked: int = 0

    def merge(self, other: DRCResult) -> None:  # type: ignore[override]
        """Merge violations from another DRCResult into this one."""
        super().merge(other)
        self.rules_checked += other.rules_checked

    def filter_by_rule(self, rule_id: str) -> list[DRCViolation]:
        """Get violations for a specific rule."""
        return [v for v in self.violations if v.rule_id == rule_id]

    def filter_by_layer(self, layer: str) -> list[DRCViolation]:
        """Get violations on a specific layer."""
        return [v for v in self.violations if v.layer == layer]

    def to_dict(self) -> dict:
        """Convert to dictionary matching legacy DRCResults format."""
        return {
            "passed": self.passed,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "rules_checked": self.rules_checked,
            "violations": [v.to_dict() for v in self.violations],
        }

    def summary(self) -> str:
        """Generate a human-readable summary."""
        status = "PASSED" if self.passed else "FAILED"
        return (
            f"DRC {status}: {self.error_count} errors, "
            f"{self.warning_count} warnings ({self.rules_checked} rules checked)"
        )
