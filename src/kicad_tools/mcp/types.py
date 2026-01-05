"""
Shared Pydantic models for MCP tool responses.

Provides consistent response structures across all MCP tools,
enabling AI agents to reliably parse tool outputs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from kicad_tools.mcp.errors import MCPError


class MCPResult(BaseModel):
    """Base result model for all MCP tool responses.

    All tool responses should inherit from this class to ensure
    consistent structure with success/error handling.

    Attributes:
        success: Whether the operation completed successfully
        error: Error details if success is False

    Example::

        class SymbolListResult(MCPResult):
            symbols: list[str] = []

        # Success case
        return SymbolListResult(success=True, symbols=["U1", "U2"])

        # Error case
        return SymbolListResult(
            success=False,
            error=MCPError(
                error_type="FILE_NOT_FOUND",
                message="Schematic not found"
            )
        )
    """

    success: bool
    error: MCPError | None = None


class FileResult(MCPResult):
    """Result for file-based operations.

    Extends MCPResult with file path context.

    Attributes:
        file_path: Path to the file that was operated on
    """

    file_path: str | None = None


class ProjectResult(MCPResult):
    """Result for project-wide operations.

    Attributes:
        project_dir: Path to the project directory
    """

    project_dir: str | None = None


class ListResult(MCPResult):
    """Result for operations that return a list of items.

    Attributes:
        items: The list of items
        count: Number of items in the list
    """

    items: list[Any] = []
    count: int = 0


class AnalysisResult(MCPResult):
    """Result for analysis operations.

    Attributes:
        summary: Human-readable summary of the analysis
        details: Detailed analysis data
        metrics: Key metrics from the analysis
    """

    summary: str = ""
    details: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
