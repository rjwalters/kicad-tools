"""Intent declaration types for MCP tools.

Provides dataclasses for declaring design intents (interfaces, power rails)
and tracking constraint violations.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ConstraintInfo:
    """Information about a derived constraint.

    Attributes:
        type: Constraint type (e.g., "impedance", "length_match", "min_trace_width")
        params: Constraint parameters specific to the type
        source: Interface type that generated this constraint
        severity: Whether violations are errors or warnings
    """

    type: str
    params: dict
    source: str
    severity: str = "error"

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "type": self.type,
            "params": self.params,
            "source": self.source,
            "severity": self.severity,
        }


@dataclass
class DeclareInterfaceResult:
    """Result of declaring an interface intent.

    Attributes:
        success: Whether the declaration was successful
        declared: Whether the intent was declared (same as success when True)
        interface_type: The interface type that was declared
        nets: The nets included in the declaration
        constraints: Constraints derived from the interface specification
        warnings: Any warnings about the declaration
        error_message: Error message if success is False
    """

    success: bool
    declared: bool = False
    interface_type: str = ""
    nets: list[str] = field(default_factory=list)
    constraints: list[ConstraintInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "declared": self.declared,
            "interface": self.interface_type,
            "nets": self.nets,
            "constraints": [c.to_dict() for c in self.constraints],
            "warnings": self.warnings,
            "error_message": self.error_message,
        }


@dataclass
class DeclarePowerRailResult:
    """Result of declaring a power rail intent.

    Attributes:
        success: Whether the declaration was successful
        declared: Whether the intent was declared
        net: The power net name
        voltage: Rail voltage
        max_current: Maximum expected current
        constraints: Constraints derived from the power specification
        error_message: Error message if success is False
    """

    success: bool
    declared: bool = False
    net: str = ""
    voltage: float | None = None
    max_current: float = 0.5
    constraints: list[ConstraintInfo] = field(default_factory=list)
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "declared": self.declared,
            "net": self.net,
            "voltage": self.voltage,
            "max_current": self.max_current,
            "constraints": [c.to_dict() for c in self.constraints],
            "error_message": self.error_message,
        }


@dataclass
class IntentInfo:
    """Information about a declared intent.

    Attributes:
        interface_type: Interface type name (e.g., "usb2_high_speed", "power_rail")
        nets: List of net names in the interface
        constraint_count: Number of constraints derived from this intent
        metadata: Additional metadata about the declaration
    """

    interface_type: str
    nets: list[str]
    constraint_count: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result: dict = {
            "type": self.interface_type,
            "nets": self.nets,
            "constraint_count": self.constraint_count,
        }
        if self.metadata:
            result["metadata"] = self.metadata
        return result


@dataclass
class ListIntentsResult:
    """Result of listing all intents in a session.

    Attributes:
        success: Whether the operation was successful
        intents: List of declared intents
        constraint_count: Total number of constraints derived from all intents
        error_message: Error message if success is False
    """

    success: bool
    intents: list[IntentInfo] = field(default_factory=list)
    constraint_count: int = 0
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "intents": [i.to_dict() for i in self.intents],
            "constraint_count": self.constraint_count,
            "error_message": self.error_message,
        }


@dataclass
class ClearIntentResult:
    """Result of clearing intent declaration(s).

    Attributes:
        success: Whether the operation was successful
        cleared_count: Number of intents that were cleared
        remaining_count: Number of intents remaining
        error_message: Error message if success is False
    """

    success: bool
    cleared_count: int = 0
    remaining_count: int = 0
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "cleared_count": self.cleared_count,
            "remaining_count": self.remaining_count,
            "error_message": self.error_message,
        }


@dataclass
class IntentViolation:
    """An intent constraint violation.

    Attributes:
        constraint_type: Type of constraint violated
        interface_type: Interface that defined the constraint
        message: Human-readable description of the violation
        severity: Severity level ("error" or "warning")
        net: Net name involved in the violation
    """

    constraint_type: str
    interface_type: str
    message: str
    severity: str = "error"
    net: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "constraint_type": self.constraint_type,
            "interface_type": self.interface_type,
            "message": self.message,
            "severity": self.severity,
            "net": self.net,
        }


@dataclass
class IntentStatus:
    """Intent-aware status for a placement operation.

    Included in apply_move and query_move responses when intents are declared.

    Attributes:
        violations: List of intent constraint violations
        warnings: List of intent-related warnings
        affected_intents: List of interface types affected by the operation
    """

    violations: list[IntentViolation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    affected_intents: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "violations": [v.to_dict() for v in self.violations],
            "warnings": self.warnings,
            "affected_intents": self.affected_intents,
        }
