"""
Error Handling Patterns for kicad-tools Agent Integration.

This module provides error recovery patterns and strategies for AI agents
working with kicad-tools. It helps agents diagnose failures and take
corrective actions.

Usage:
    from error_handlers import ErrorHandler, RecoveryStrategy

    handler = ErrorHandler()

    # When a tool fails
    if not result.success:
        recovery = handler.get_recovery(result)
        print(f"Suggested action: {recovery.action}")
        print(f"Retry with: {recovery.modified_args}")
"""

from dataclasses import dataclass, field
from enum import Enum, auto


class ErrorType(Enum):
    """Types of errors that can occur."""

    # State errors
    NO_SCHEMATIC = auto()
    NO_PCB = auto()
    FILE_NOT_FOUND = auto()

    # Component errors
    COMPONENT_NOT_FOUND = auto()
    PIN_NOT_FOUND = auto()
    NET_NOT_FOUND = auto()
    SYMBOL_NOT_FOUND = auto()

    # Routing errors
    ROUTE_BLOCKED = auto()
    NO_PATH = auto()
    CLEARANCE_VIOLATION = auto()
    TRACE_TOO_THIN = auto()

    # Placement errors
    COLLISION = auto()
    OUT_OF_BOUNDS = auto()
    FOOTPRINT_MISMATCH = auto()

    # DRC errors
    DRC_VIOLATION = auto()
    MANUFACTURER_RULE_VIOLATION = auto()

    # Export errors
    EXPORT_FAILED = auto()
    INVALID_FORMAT = auto()

    # Generic errors
    UNKNOWN = auto()
    INVALID_ARGUMENTS = auto()


class RecoveryAction(Enum):
    """Actions that can be taken to recover from errors."""

    RETRY = "retry"  # Retry with same args
    RETRY_MODIFIED = "retry_modified"  # Retry with modified args
    ALTERNATIVE_TOOL = "alternative_tool"  # Use different tool
    PREREQUISITE = "prerequisite"  # Execute prerequisite first
    USER_INPUT = "user_input"  # Need user decision
    SKIP = "skip"  # Skip this operation
    ABORT = "abort"  # Cannot recover


@dataclass
class RecoveryStrategy:
    """A strategy for recovering from an error."""

    action: RecoveryAction
    description: str
    tool_name: str | None = None
    modified_args: dict = field(default_factory=dict)
    prerequisite_tools: list[tuple[str, dict]] = field(default_factory=list)
    alternatives: list["RecoveryStrategy"] = field(default_factory=list)

    def to_prompt(self) -> str:
        """Generate a prompt-friendly description of the recovery."""
        lines = [f"Recovery: {self.action.value}"]
        lines.append(f"Description: {self.description}")

        if self.modified_args:
            lines.append(f"Modified arguments: {self.modified_args}")

        if self.prerequisite_tools:
            lines.append("Prerequisites:")
            for tool, args in self.prerequisite_tools:
                lines.append(f"  - {tool}({args})")

        if self.alternatives:
            lines.append("Alternatives:")
            for alt in self.alternatives:
                lines.append(f"  - {alt.description}")

        return "\n".join(lines)


@dataclass
class ErrorContext:
    """Context about an error for diagnosis."""

    error_type: ErrorType
    tool_name: str
    arguments: dict
    error_message: str
    state: dict = field(default_factory=dict)


class ErrorHandler:
    """
    Handles errors from kicad-tools and provides recovery strategies.

    This class analyzes errors and suggests corrective actions that
    an AI agent can take to resolve issues.
    """

    def __init__(self):
        """Initialize the error handler."""
        self._error_patterns = self._build_error_patterns()

    def classify_error(self, tool_name: str, error_message: str, arguments: dict) -> ErrorType:
        """
        Classify an error based on the message and context.

        Args:
            tool_name: Name of the tool that failed
            error_message: Error message returned
            arguments: Arguments that were passed

        Returns:
            ErrorType classification
        """
        error_lower = error_message.lower()

        # State errors
        if "no schematic" in error_lower or "schematic not loaded" in error_lower:
            return ErrorType.NO_SCHEMATIC
        if "no pcb" in error_lower or "pcb not loaded" in error_lower:
            return ErrorType.NO_PCB
        if "file not found" in error_lower or "filenotfounderror" in error_lower:
            return ErrorType.FILE_NOT_FOUND

        # Component errors
        if "component not found" in error_lower:
            return ErrorType.COMPONENT_NOT_FOUND
        if "pin not found" in error_lower or "invalid pin" in error_lower:
            return ErrorType.PIN_NOT_FOUND
        if "net not found" in error_lower:
            return ErrorType.NET_NOT_FOUND
        if "symbol not found" in error_lower:
            return ErrorType.SYMBOL_NOT_FOUND

        # Routing errors
        if "blocked" in error_lower or "cannot route" in error_lower:
            return ErrorType.ROUTE_BLOCKED
        if "no path" in error_lower:
            return ErrorType.NO_PATH
        if "clearance" in error_lower:
            return ErrorType.CLEARANCE_VIOLATION
        if "trace width" in error_lower or "too thin" in error_lower:
            return ErrorType.TRACE_TOO_THIN

        # Placement errors
        if "collision" in error_lower or "overlap" in error_lower:
            return ErrorType.COLLISION
        if "out of bounds" in error_lower or "outside board" in error_lower:
            return ErrorType.OUT_OF_BOUNDS

        # DRC errors
        if "drc" in error_lower or "design rule" in error_lower:
            return ErrorType.DRC_VIOLATION

        # Invalid arguments
        if "invalid" in error_lower or "required" in error_lower:
            return ErrorType.INVALID_ARGUMENTS

        return ErrorType.UNKNOWN

    def get_recovery(
        self,
        tool_name: str,
        error_message: str,
        arguments: dict,
        state: dict | None = None,
    ) -> RecoveryStrategy:
        """
        Get a recovery strategy for an error.

        Args:
            tool_name: Name of the tool that failed
            error_message: Error message returned
            arguments: Arguments that were passed
            state: Current agent state (optional)

        Returns:
            RecoveryStrategy with suggested actions
        """
        error_type = self.classify_error(tool_name, error_message, arguments)
        context = ErrorContext(
            error_type=error_type,
            tool_name=tool_name,
            arguments=arguments,
            error_message=error_message,
            state=state or {},
        )

        return self._get_recovery_for_type(context)

    def _get_recovery_for_type(self, context: ErrorContext) -> RecoveryStrategy:
        """Get recovery strategy based on error type."""

        strategies = {
            ErrorType.NO_SCHEMATIC: self._recover_no_schematic,
            ErrorType.NO_PCB: self._recover_no_pcb,
            ErrorType.FILE_NOT_FOUND: self._recover_file_not_found,
            ErrorType.COMPONENT_NOT_FOUND: self._recover_component_not_found,
            ErrorType.PIN_NOT_FOUND: self._recover_pin_not_found,
            ErrorType.NET_NOT_FOUND: self._recover_net_not_found,
            ErrorType.ROUTE_BLOCKED: self._recover_route_blocked,
            ErrorType.CLEARANCE_VIOLATION: self._recover_clearance_violation,
            ErrorType.COLLISION: self._recover_collision,
            ErrorType.OUT_OF_BOUNDS: self._recover_out_of_bounds,
            ErrorType.DRC_VIOLATION: self._recover_drc_violation,
            ErrorType.INVALID_ARGUMENTS: self._recover_invalid_arguments,
        }

        handler = strategies.get(context.error_type, self._recover_unknown)
        return handler(context)

    # =========================================================================
    # Recovery Strategies
    # =========================================================================

    def _recover_no_schematic(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for no schematic loaded."""
        return RecoveryStrategy(
            action=RecoveryAction.PREREQUISITE,
            description="Load a schematic before performing schematic operations",
            prerequisite_tools=[("load_schematic", {"file_path": "<schematic_path>"})],
        )

    def _recover_no_pcb(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for no PCB loaded."""
        return RecoveryStrategy(
            action=RecoveryAction.PREREQUISITE,
            description="Load a PCB before performing PCB operations",
            prerequisite_tools=[("load_pcb", {"file_path": "<pcb_path>"})],
        )

    def _recover_file_not_found(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for file not found."""
        file_path = context.arguments.get("file_path", "")
        return RecoveryStrategy(
            action=RecoveryAction.USER_INPUT,
            description=f"File not found: {file_path}. Please provide a valid file path.",
            alternatives=[
                RecoveryStrategy(
                    action=RecoveryAction.ALTERNATIVE_TOOL,
                    description="Create a new file instead",
                    tool_name="save_schematic" if "schematic" in context.tool_name else "save_pcb",
                )
            ],
        )

    def _recover_component_not_found(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for component not found."""
        ref = (
            context.arguments.get("ref")
            or context.arguments.get("from_ref")
            or context.arguments.get("to_ref")
        )
        return RecoveryStrategy(
            action=RecoveryAction.PREREQUISITE,
            description=f"Component '{ref}' not found. List available components first.",
            prerequisite_tools=[("list_symbols", {})],
            alternatives=[
                RecoveryStrategy(
                    action=RecoveryAction.ALTERNATIVE_TOOL,
                    description=f"Add the missing component '{ref}'",
                    tool_name="add_schematic_symbol",
                )
            ],
        )

    def _recover_pin_not_found(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for pin not found."""
        ref = context.arguments.get("from_ref") or context.arguments.get("ref")
        return RecoveryStrategy(
            action=RecoveryAction.PREREQUISITE,
            description="Pin not found. Get component info to see available pins.",
            prerequisite_tools=[("get_component_info", {"ref": ref})],
        )

    def _recover_net_not_found(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for net not found."""
        net = context.arguments.get("net", "")
        return RecoveryStrategy(
            action=RecoveryAction.PREREQUISITE,
            description=f"Net '{net}' not found. List available nets first.",
            prerequisite_tools=[("list_nets", {})],
        )

    def _recover_route_blocked(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for blocked route."""
        net = context.arguments.get("net", "")
        current_layer = context.arguments.get("prefer_layer", "F.Cu")
        alt_layer = "B.Cu" if current_layer == "F.Cu" else "F.Cu"

        return RecoveryStrategy(
            action=RecoveryAction.RETRY_MODIFIED,
            description="Route blocked. Try different layer or delete conflicting traces.",
            modified_args={**context.arguments, "prefer_layer": alt_layer},
            alternatives=[
                RecoveryStrategy(
                    action=RecoveryAction.ALTERNATIVE_TOOL,
                    description="Delete conflicting traces first",
                    tool_name="delete_trace",
                    modified_args={"net": net, "delete_all": True},
                ),
                RecoveryStrategy(
                    action=RecoveryAction.ALTERNATIVE_TOOL,
                    description="Move blocking components",
                    tool_name="place_component",
                ),
            ],
        )

    def _recover_clearance_violation(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for clearance violation."""
        return RecoveryStrategy(
            action=RecoveryAction.RETRY_MODIFIED,
            description="Clearance violation. Increase trace clearance or use wider spacing.",
            modified_args={**context.arguments, "clearance": 0.2},
            alternatives=[
                RecoveryStrategy(
                    action=RecoveryAction.ALTERNATIVE_TOOL,
                    description="Delete traces in violation area and reroute",
                    tool_name="delete_trace",
                )
            ],
        )

    def _recover_collision(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for component collision."""
        x = context.arguments.get("x", 0)
        y = context.arguments.get("y", 0)

        return RecoveryStrategy(
            action=RecoveryAction.RETRY_MODIFIED,
            description="Component collision. Try a different position.",
            modified_args={**context.arguments, "x": x + 5, "y": y},
            alternatives=[
                RecoveryStrategy(
                    action=RecoveryAction.RETRY_MODIFIED,
                    description="Try position with offset in Y",
                    modified_args={**context.arguments, "x": x, "y": y + 5},
                ),
                RecoveryStrategy(
                    action=RecoveryAction.ALTERNATIVE_TOOL,
                    description="Get board analysis to find open areas",
                    tool_name="analyze_board",
                ),
            ],
        )

    def _recover_out_of_bounds(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for out of bounds placement."""
        return RecoveryStrategy(
            action=RecoveryAction.PREREQUISITE,
            description="Position is outside board boundaries. Analyze board first.",
            prerequisite_tools=[("analyze_board", {})],
        )

    def _recover_drc_violation(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for DRC violation."""
        return RecoveryStrategy(
            action=RecoveryAction.PREREQUISITE,
            description="DRC violation detected. Get violation details to fix.",
            prerequisite_tools=[("get_violations", {"severity": "error"})],
            alternatives=[
                RecoveryStrategy(
                    action=RecoveryAction.ALTERNATIVE_TOOL,
                    description="Delete traces causing violations and reroute",
                    tool_name="delete_trace",
                )
            ],
        )

    def _recover_invalid_arguments(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for invalid arguments."""
        return RecoveryStrategy(
            action=RecoveryAction.USER_INPUT,
            description=f"Invalid arguments for {context.tool_name}. Check required parameters.",
        )

    def _recover_unknown(self, context: ErrorContext) -> RecoveryStrategy:
        """Recovery for unknown errors."""
        return RecoveryStrategy(
            action=RecoveryAction.USER_INPUT,
            description=f"Unknown error: {context.error_message}",
        )

    def _build_error_patterns(self) -> dict[str, ErrorType]:
        """Build regex patterns for error classification."""
        return {}


class RetryPolicy:
    """
    Policy for automatic retries with backoff.

    Provides strategies for how many times to retry operations
    and with what modifications.
    """

    def __init__(
        self,
        max_retries: int = 3,
        retry_on: list[ErrorType] | None = None,
    ):
        """
        Initialize retry policy.

        Args:
            max_retries: Maximum number of retry attempts
            retry_on: List of error types that should be retried
        """
        self.max_retries = max_retries
        self.retry_on = retry_on or [
            ErrorType.ROUTE_BLOCKED,
            ErrorType.COLLISION,
            ErrorType.CLEARANCE_VIOLATION,
        ]

    def should_retry(self, error_type: ErrorType, attempt: int) -> bool:
        """Check if operation should be retried."""
        if attempt >= self.max_retries:
            return False
        return error_type in self.retry_on

    def get_backoff_args(self, error_type: ErrorType, original_args: dict, attempt: int) -> dict:
        """
        Get modified arguments for retry attempt.

        Args:
            error_type: Type of error encountered
            original_args: Original arguments
            attempt: Current attempt number (0-indexed)

        Returns:
            Modified arguments for retry
        """
        args = original_args.copy()

        if error_type == ErrorType.COLLISION:
            # Offset position by attempt number
            offset = (attempt + 1) * 2.5
            if "x" in args:
                args["x"] = args["x"] + offset
            if "y" in args:
                args["y"] = args["y"] + offset

        elif error_type == ErrorType.ROUTE_BLOCKED:
            # Alternate layers on each attempt
            layers = ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu"]
            if "prefer_layer" in args or attempt > 0:
                args["prefer_layer"] = layers[attempt % len(layers)]

        elif error_type == ErrorType.CLEARANCE_VIOLATION:
            # Increase clearance on each attempt
            base_clearance = args.get("clearance", 0.127)
            args["clearance"] = base_clearance * (1 + 0.5 * attempt)

        return args


# Convenience functions for quick error handling
def get_recovery_suggestion(tool_name: str, error_message: str, arguments: dict) -> str:
    """
    Get a simple recovery suggestion string.

    Args:
        tool_name: Name of the tool that failed
        error_message: Error message
        arguments: Arguments that were passed

    Returns:
        Human-readable recovery suggestion
    """
    handler = ErrorHandler()
    recovery = handler.get_recovery(tool_name, error_message, arguments)
    return recovery.to_prompt()


def classify_and_recover(
    tool_name: str, error_message: str, arguments: dict
) -> tuple[ErrorType, RecoveryStrategy]:
    """
    Classify an error and get recovery strategy.

    Args:
        tool_name: Name of the tool that failed
        error_message: Error message
        arguments: Arguments that were passed

    Returns:
        Tuple of (error_type, recovery_strategy)
    """
    handler = ErrorHandler()
    error_type = handler.classify_error(tool_name, error_message, arguments)
    recovery = handler.get_recovery(tool_name, error_message, arguments)
    return error_type, recovery


# Example usage
if __name__ == "__main__":
    handler = ErrorHandler()

    # Test error classification
    test_cases = [
        ("route_net", "Cannot route net 'SDA' - path blocked by U2", {"net": "SDA"}),
        ("load_schematic", "File not found: design.kicad_sch", {"file_path": "design.kicad_sch"}),
        ("wire_components", "Component 'U2' not found", {"from_ref": "U2", "from_pin": "1"}),
        ("place_component", "Collision with C3 at position", {"ref": "R1", "x": 50, "y": 30}),
    ]

    for tool, error, args in test_cases:
        error_type = handler.classify_error(tool, error, args)
        recovery = handler.get_recovery(tool, error, args)

        print(f"\nTool: {tool}")
        print(f"Error: {error}")
        print(f"Type: {error_type.name}")
        print(f"Recovery:\n{recovery.to_prompt()}")
        print("-" * 50)
