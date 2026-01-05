"""Error types for MCP server operations.

Provides custom exception classes for session management and other MCP operations.
"""

from __future__ import annotations


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
