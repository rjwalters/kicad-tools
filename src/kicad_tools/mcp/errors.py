"""
MCP-specific error types for structured error responses.

Maps KiCadToolsError hierarchy to actionable MCP responses with
suggestions for recovery.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from kicad_tools.exceptions import (
    ComponentError,
    ConfigurationError,
    ExportError,
    FileFormatError,
    FileNotFoundError,
    KiCadToolsError,
    ParseError,
    RoutingError,
    ValidationError,
)


class MCPError(BaseModel):
    """Structured error response for MCP tools.

    Provides actionable error information that AI agents can use
    to understand and recover from failures.

    Attributes:
        error_type: Machine-readable error type (e.g., "FILE_NOT_FOUND")
        message: Human-readable error description
        suggestions: List of actionable suggestions for fixing the error
        context: Additional context (file path, line number, etc.)

    Example::

        error = MCPError(
            error_type="FILE_NOT_FOUND",
            message="Schematic file not found: design.kicad_sch",
            suggestions=["Check the file path", "Ensure the file exists"],
            context={"file": "design.kicad_sch"}
        )
    """

    error_type: str
    message: str
    suggestions: list[str] = []
    context: dict[str, Any] = {}

    @classmethod
    def from_exception(cls, exc: Exception) -> MCPError:
        """Create MCPError from a Python exception.

        Handles both KiCadToolsError instances (with full context)
        and generic exceptions.

        Args:
            exc: The exception to convert

        Returns:
            MCPError with structured error information

        Example::

            try:
                parse_schematic("missing.kicad_sch")
            except KiCadToolsError as e:
                error = MCPError.from_exception(e)
                return {"success": False, "error": error.model_dump()}
        """
        if isinstance(exc, KiCadToolsError):
            return cls(
                error_type=exc.error_code,
                message=exc.message,
                suggestions=exc.suggestions,
                context=exc.context,
            )

        # Generic exception
        return cls(
            error_type=type(exc).__name__.upper(),
            message=str(exc),
            suggestions=["Check the error message for details"],
            context={},
        )


# Error type constants for common error scenarios
ERROR_FILE_NOT_FOUND = "FILE_NOT_FOUND"
ERROR_PARSE_ERROR = "PARSE_ERROR"
ERROR_VALIDATION_ERROR = "VALIDATION_ERROR"
ERROR_FILE_FORMAT_ERROR = "FILE_FORMAT_ERROR"
ERROR_ROUTING_ERROR = "ROUTING_ERROR"
ERROR_COMPONENT_ERROR = "COMPONENT_ERROR"
ERROR_CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
ERROR_EXPORT_ERROR = "EXPORT_ERROR"
ERROR_INTERNAL_ERROR = "INTERNAL_ERROR"


def map_exception_to_error_type(exc: Exception) -> str:
    """Map exception class to error type string.

    Args:
        exc: The exception to map

    Returns:
        Error type string for MCP response
    """
    error_map = {
        FileNotFoundError: ERROR_FILE_NOT_FOUND,
        ParseError: ERROR_PARSE_ERROR,
        ValidationError: ERROR_VALIDATION_ERROR,
        FileFormatError: ERROR_FILE_FORMAT_ERROR,
        RoutingError: ERROR_ROUTING_ERROR,
        ComponentError: ERROR_COMPONENT_ERROR,
        ConfigurationError: ERROR_CONFIGURATION_ERROR,
        ExportError: ERROR_EXPORT_ERROR,
    }

    for exc_type, error_type in error_map.items():
        if isinstance(exc, exc_type):
            return error_type

    if isinstance(exc, KiCadToolsError):
        return exc.error_code

    return ERROR_INTERNAL_ERROR
