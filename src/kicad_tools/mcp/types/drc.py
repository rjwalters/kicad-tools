"""DRC violation types for MCP tools.

Provides dataclasses for Design Rule Check violations and results.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ViolationLocation:
    """Location of a DRC violation on the PCB.

    Attributes:
        x_mm: X coordinate in millimeters
        y_mm: Y coordinate in millimeters
        layer: PCB layer name (e.g., "F.Cu", "B.Cu")
    """

    x_mm: float
    y_mm: float
    layer: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "x_mm": round(self.x_mm, 3),
            "y_mm": round(self.y_mm, 3),
            "layer": self.layer,
        }


@dataclass
class AffectedItem:
    """An item affected by a DRC violation.

    Attributes:
        item_type: Type of item ("pad", "track", "via", "zone", "component")
        reference: Reference designator (e.g., "U1", "R15")
        net: Net name if applicable
    """

    item_type: str
    reference: str | None = None
    net: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "item_type": self.item_type,
            "reference": self.reference,
            "net": self.net,
        }


@dataclass
class DRCViolation:
    """A single DRC violation with location and fix suggestions.

    Attributes:
        id: Unique identifier for this violation
        type: Violation type (clearance, track_width, via_size, etc.)
        severity: Severity level (error, warning)
        message: Human-readable description of the violation
        location: Location on the PCB
        affected_items: Items involved in the violation
        fix_suggestion: Suggested fix for the violation
        required_value_mm: Minimum required value (when applicable)
        actual_value_mm: Measured value that violated the rule
    """

    id: str
    type: str
    severity: str
    message: str
    location: ViolationLocation
    affected_items: list[AffectedItem] = field(default_factory=list)
    fix_suggestion: str | None = None
    required_value_mm: float | None = None
    actual_value_mm: float | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "type": self.type,
            "severity": self.severity,
            "message": self.message,
            "location": self.location.to_dict(),
            "affected_items": [i.to_dict() for i in self.affected_items],
            "fix_suggestion": self.fix_suggestion,
            "required_value_mm": self.required_value_mm,
            "actual_value_mm": self.actual_value_mm,
        }


@dataclass
class DRCResult:
    """Result of running a Design Rule Check on a PCB.

    Attributes:
        passed: Whether the DRC passed (no errors, warnings allowed)
        violation_count: Total number of violations
        error_count: Number of error-severity violations
        warning_count: Number of warning-severity violations
        violations: List of all violations found
        summary_by_type: Count of violations by type
        manufacturer: Manufacturer rules used for the check
        layers: Number of PCB layers checked against
    """

    passed: bool
    violation_count: int
    error_count: int
    warning_count: int
    violations: list[DRCViolation] = field(default_factory=list)
    summary_by_type: dict[str, int] = field(default_factory=dict)
    manufacturer: str = ""
    layers: int = 4

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "passed": self.passed,
            "violation_count": self.violation_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "violations": [v.to_dict() for v in self.violations],
            "summary_by_type": self.summary_by_type,
            "manufacturer": self.manufacturer,
            "layers": self.layers,
        }
