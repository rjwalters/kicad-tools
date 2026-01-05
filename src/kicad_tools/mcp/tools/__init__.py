"""
Modular tool registration system for MCP.

Provides the MCPTools class for organizing and registering tools
with the FastMCP server in a modular way.

Example usage::

    from kicad_tools.mcp.tools import MCPTools
    from pydantic import BaseModel

    # Create a tools collection
    schematic_tools = MCPTools()

    class SymbolResult(BaseModel):
        success: bool
        symbols: list[str]

    @schematic_tools.register()
    def list_symbols(schematic_path: str) -> SymbolResult:
        '''List all symbols in a schematic.'''
        # Implementation here
        return SymbolResult(success=True, symbols=["U1", "U2"])

    # In server.py:
    # schematic_tools.install(mcp)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Type for MCP decorator functions
MCPDecorator = Callable[["FastMCP"], Callable]


class MCPTools:
    """Modular tool registration for MCP servers.

    Collects tool functions and their decorators, allowing them to be
    registered with an MCP server at startup. This enables organizing
    tools into logical groups (e.g., schematic tools, PCB tools).

    Example::

        # Define tools in a module
        schematic_tools = MCPTools()

        @schematic_tools.register()
        def analyze_schematic(path: str) -> dict:
            '''Analyze a KiCad schematic file.'''
            ...

        # Register with MCP server
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("kicad-tools")
        schematic_tools.install(mcp)

    Custom decorators can be provided for special tool configurations::

        @schematic_tools.register(decorator=lambda mcp: mcp.tool(name="sch_analyze"))
        def analyze_schematic(path: str) -> dict:
            ...
    """

    def __init__(self) -> None:
        """Initialize an empty tools collection."""
        self._tools: dict[Callable, MCPDecorator] = {}

    def register(self, decorator: MCPDecorator | None = None) -> Callable[[Callable], Callable]:
        """Decorator to register a function as an MCP tool.

        Args:
            decorator: Optional custom decorator factory. If not provided,
                      uses the default mcp.tool() decorator.

        Returns:
            Decorator function that registers the tool.

        Example::

            @tools.register()
            def my_tool(arg: str) -> dict:
                '''Tool description shown to AI.'''
                return {"result": arg}

            # With custom decorator
            @tools.register(decorator=lambda mcp: mcp.tool(name="custom_name"))
            def another_tool(arg: str) -> dict:
                ...
        """
        if decorator is None:

            def default_decorator(mcp: FastMCP) -> Callable:
                return mcp.tool()

            decorator = default_decorator

        def decorator_wrapper(func: Callable) -> Callable:
            self._tools[func] = decorator
            return func

        return decorator_wrapper

    def install(self, mcp: FastMCP) -> None:
        """Install all registered tools on an MCP server.

        Args:
            mcp: The FastMCP server instance to register tools with.

        Example::

            from mcp.server.fastmcp import FastMCP

            mcp = FastMCP("kicad-tools")
            schematic_tools.install(mcp)
            pcb_tools.install(mcp)
            mcp.run()
        """
        for func, decorator_factory in self._tools.items():
            logger.debug("Installing MCP tool: %s", func.__name__)
            decorator = decorator_factory(mcp)
            decorator(func)

    @property
    def tool_count(self) -> int:
        """Return the number of registered tools."""
        return len(self._tools)

    @property
    def tool_names(self) -> list[str]:
        """Return list of registered tool function names."""
        return [func.__name__ for func in self._tools]


__all__ = ["MCPTools"]
