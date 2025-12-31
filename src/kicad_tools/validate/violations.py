"""DRC violation models for pure Python validation.

This module defines the data structures used to represent DRC violations
and results from the pure Python DRC checker.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DRCViolation:
    """Represents a single DRC violation found by the checker.

    This class is hashable (frozen) to support deduplication of violations.

    Attributes:
        rule_id: Unique identifier for the rule, e.g., "clearance_trace_trace"
        severity: Either "error" or "warning"
        message: Human-readable description of the violation
        location: (x_mm, y_mm) tuple of the violation location, or None
        layer: Layer name where violation occurs, e.g., "F.Cu"
        actual_value: The measured value that violated the rule
        required_value: The minimum/maximum value required by the rule
        items: Tuple of item references involved, e.g., ("D1", "C5")
    """

    rule_id: str
    severity: str
    message: str
    location: tuple[float, float] | None = None
    layer: str | None = None
    actual_value: float | None = None
    required_value: float | None = None
    items: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate severity value."""
        if self.severity not in ("error", "warning"):
            raise ValueError(f"severity must be 'error' or 'warning', got {self.severity!r}")

    @property
    def is_error(self) -> bool:
        """Check if this is an error (not a warning)."""
        return self.severity == "error"

    @property
    def is_warning(self) -> bool:
        """Check if this is a warning (not an error)."""
        return self.severity == "warning"

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "location": list(self.location) if self.location else None,
            "layer": self.layer,
            "actual_value": self.actual_value,
            "required_value": self.required_value,
            "items": list(self.items),
        }


@dataclass
class DRCResults:
    """Aggregates all DRC violations from a check run.

    Provides convenient access to violation counts and filtering.

    Attributes:
        violations: List of all violations found
        rules_checked: Number of rules that were checked
    """

    violations: list[DRCViolation] = field(default_factory=list)
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
    def passed(self) -> bool:
        """True if no errors (warnings are allowed)."""
        return self.error_count == 0

    @property
    def errors(self) -> list[DRCViolation]:
        """List of only error violations."""
        return [v for v in self.violations if v.is_error]

    @property
    def warnings(self) -> list[DRCViolation]:
        """List of only warning violations."""
        return [v for v in self.violations if v.is_warning]

    def __iter__(self):
        """Iterate over all violations."""
        return iter(self.violations)

    def __len__(self) -> int:
        """Total number of violations."""
        return len(self.violations)

    def __bool__(self) -> bool:
        """True if there are any violations."""
        return len(self.violations) > 0

    def add(self, violation: DRCViolation) -> None:
        """Add a violation to the results."""
        self.violations.append(violation)

    def merge(self, other: DRCResults) -> None:
        """Merge violations from another DRCResults into this one."""
        self.violations.extend(other.violations)
        self.rules_checked += other.rules_checked

    def filter_by_rule(self, rule_id: str) -> list[DRCViolation]:
        """Get violations for a specific rule."""
        return [v for v in self.violations if v.rule_id == rule_id]

    def filter_by_layer(self, layer: str) -> list[DRCViolation]:
        """Get violations on a specific layer."""
        return [v for v in self.violations if v.layer == layer]

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
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
