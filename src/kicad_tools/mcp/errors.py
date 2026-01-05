"""Error types for MCP server operations.

Provides custom exception classes for session management and other MCP operations,
as well as structured error responses for AI agents.
"""

from __future__ import annotations

from pydantic import BaseModel

from kicad_tools.exceptions import (
    FileNotFoundError as KiCadFileNotFoundError,
)
from kicad_tools.exceptions import (
    KiCadToolsError,
    ParseError,
    ValidationError,
)


class MCPError(Exception):
    """Base class for MCP-related errors."""

    pass


class SessionNotFoundError(MCPError):
    """Raised when a session ID does not exist.

    Attributes:
        session_id: The session ID that was not found.
    """

    def __init__(self, session_id: str, message: str | None = None) -> None:
        self.session_id = session_id
        if message is None:
            message = f"Session '{session_id}' not found"
        super().__init__(message)


class SessionExpiredError(MCPError):
    """Raised when a session has expired due to timeout.

    Attributes:
        session_id: The session ID that expired.
    """

    def __init__(self, session_id: str, message: str | None = None) -> None:
        self.session_id = session_id
        if message is None:
            message = f"Session '{session_id}' has expired"
        super().__init__(message)


class SessionOperationError(MCPError):
    """Raised when a session operation fails.

    Attributes:
        session_id: The session ID where the operation failed.
        operation: The operation that failed.
    """

    def __init__(self, session_id: str, operation: str, message: str | None = None) -> None:
        self.session_id = session_id
        self.operation = operation
        if message is None:
            message = f"Operation '{operation}' failed on session '{session_id}'"
        super().__init__(message)


class MCPErrorResponse(BaseModel):
    """Structured error response for MCP tools.

    Provides actionable error information that AI agents can use
    to understand and potentially resolve issues.
    """

    error_type: str
    message: str
    suggestions: list[str] = []
    location: dict | None = None  # file, line, column if applicable


def map_exception_to_mcp_error(exc: Exception) -> MCPErrorResponse:
    """Convert an exception to an MCPErrorResponse.

    Args:
        exc: The exception to convert.

    Returns:
        An MCPErrorResponse with appropriate error type and suggestions.
    """
    if isinstance(exc, KiCadFileNotFoundError):
        return MCPErrorResponse(
            error_type="file_not_found",
            message=str(exc),
            suggestions=[
                "Check that the file path is correct",
                "Ensure the file exists and is accessible",
                "Try using an absolute path",
            ],
        )
    elif isinstance(exc, ParseError):
        return MCPErrorResponse(
            error_type="parse_error",
            message=str(exc),
            suggestions=[
                "Check that the file is a valid KiCad file",
                "Ensure the file is not corrupted",
                "Try opening the file in KiCad to verify it",
            ],
        )
    elif isinstance(exc, ValidationError):
        return MCPErrorResponse(
            error_type="validation_error",
            message=str(exc),
            suggestions=[
                "Review the validation rules that failed",
                "Check the design against manufacturer constraints",
            ],
        )
    elif isinstance(exc, KiCadToolsError):
        return MCPErrorResponse(
            error_type="kicad_tools_error",
            message=str(exc),
            suggestions=[
                "Check the error message for details",
                "Ensure all required files are present",
            ],
        )
    else:
        return MCPErrorResponse(
            error_type="unknown_error",
            message=str(exc),
            suggestions=[
                "An unexpected error occurred",
                "Check the error message for details",
            ],
        )
