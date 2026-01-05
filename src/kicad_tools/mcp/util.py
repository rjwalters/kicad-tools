"""Utility classes for MCP tool registration.

Provides a modular tool registration pattern that allows tools to be
defined in separate modules and installed into the MCP server.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


MCP_DECORATOR = Callable[["FastMCP"], Callable]


class MCPTools:
    """Registry for MCP tools with deferred installation.

    Allows tools to be registered with decorators and later installed
    into a FastMCP server instance.

    Usage:
        analysis_tools = MCPTools()

        @analysis_tools.register()
        def my_tool(arg: str) -> str:
            return f"Result: {arg}"

        # Later, when setting up the server:
        analysis_tools.install(mcp)
    """

    def __init__(self) -> None:
        """Initialize an empty tool registry."""
        self._tools: dict[Callable, MCP_DECORATOR] = {}

    def register(self, decorator: MCP_DECORATOR | None = None) -> Callable[[Callable], Callable]:
        """Register a function as an MCP tool.

        Args:
            decorator: Optional custom decorator. If not provided,
                      uses the default mcp.tool() decorator.

        Returns:
            A decorator that registers the function.
        """

        def default_decorator(mcp: FastMCP) -> Callable:
            return mcp.tool()

        if decorator is None:
            decorator = default_decorator

        def decorator_wrapper(func: Callable) -> Callable:
            self._tools[func] = decorator
            return func

        return decorator_wrapper

    def install(self, mcp: FastMCP) -> None:
        """Install all registered tools into the MCP server.

        Args:
            mcp: The FastMCP server instance to install tools into.
        """
        for func, decorator in self._tools.items():
            d = decorator(mcp)
            d(func)
            logger.debug(f"Installed MCP tool: {func.__name__}")
