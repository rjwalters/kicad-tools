"""DRC delta types for continuous validation.

Provides dataclasses for tracking DRC changes during placement operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DRCViolationDetail:
    """Detailed information about a single DRC violation.

    Attributes:
        id: Unique identifier for tracking this violation
        type: Violation type (e.g., "clearance", "courtyard", "silkscreen")
        severity: Severity level ("error", "warning")
        message: Human-readable description
        components: Component references involved
        location: (x, y) position where violation occurs
    """

    id: str
    type: str
    severity: str
    message: str
    components: list[str] = field(default_factory=list)
    location: tuple[float, float] | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result: dict = {
            "id": self.id,
            "type": self.type,
            "severity": self.severity,
            "message": self.message,
            "components": self.components,
        }
        if self.location is not None:
            result["location"] = {"x": round(self.location[0], 3), "y": round(self.location[1], 3)}
        return result


@dataclass
class DRCDeltaInfo:
    """DRC change information after an operation.

    Provides delta information showing what violations were introduced
    or resolved by a move operation.

    Attributes:
        new_violations: Violations introduced by the change
        resolved_violations: Violations fixed by the change
        total_violations: Current total violation count
        delta: Human-readable summary (e.g., "+1 -2 = -1 net change")
        check_time_ms: Time taken for the incremental check
    """

    new_violations: list[DRCViolationDetail] = field(default_factory=list)
    resolved_violations: list[DRCViolationDetail] = field(default_factory=list)
    total_violations: int = 0
    delta: str = ""
    check_time_ms: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "new_violations": [v.to_dict() for v in self.new_violations],
            "resolved_violations": [v.to_dict() for v in self.resolved_violations],
            "total_violations": self.total_violations,
            "delta": self.delta,
            "check_time_ms": round(self.check_time_ms, 2),
        }


@dataclass
class DRCSummary:
    """Summary of current DRC state for a session.

    Provides an overview of the current DRC state including
    violation counts by type and severity, and trend information.

    Attributes:
        total_violations: Total number of active violations
        by_severity: Violation counts by severity level
        by_type: Violation counts by violation type
        trend: Recent trend ("improving", "worsening", "stable")
        session_delta: Change in violations since session start
    """

    total_violations: int = 0
    by_severity: dict[str, int] = field(default_factory=dict)
    by_type: dict[str, int] = field(default_factory=dict)
    trend: str = "stable"
    session_delta: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "total_violations": self.total_violations,
            "by_severity": self.by_severity,
            "by_type": self.by_type,
            "trend": self.trend,
            "session_delta": self.session_delta,
        }
