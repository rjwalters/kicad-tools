"""
MCP server for kicad-tools.

Provides a Model Context Protocol server with tools for AI agents
to interact with KiCad files via stdio transport.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

from kicad_tools.mcp.tools.registry import TOOL_REGISTRY, ToolSpec

logger = logging.getLogger(__name__)


@dataclass
class ToolDefinition:
    """Definition of an MCP tool.

    This is kept for backward compatibility with existing code that may
    reference ToolDefinition. New code should use ToolSpec from the registry.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]


@dataclass
class MCPServer:
    """
    MCP server for kicad-tools.

    Implements the Model Context Protocol for tool invocation via stdio.

    Example:
        >>> server = create_server()
        >>> server.run()  # Starts stdio loop
    """

    name: str = "kicad-tools"
    version: str = "0.1.0"
    tools: dict[str, ToolDefinition] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Register tools from the unified registry."""
        self._register_tools_from_registry()

    def _register_tools_from_registry(self) -> None:
        """Auto-register all tools from the unified registry."""
        for tool_name, tool_spec in TOOL_REGISTRY.items():
            self.tools[tool_name] = ToolDefinition(
                name=tool_spec.name,
                description=tool_spec.description,
                parameters=tool_spec.parameters,
                handler=tool_spec.handler,
            )

    def get_tools_list(self) -> list[dict[str, Any]]:
        """Get list of available tools for MCP discovery."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.parameters,
            }
            for tool in self.tools.values()
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """
        Call a tool by name with given arguments.

        Args:
            name: Tool name
            arguments: Tool arguments

        Returns:
            Tool result as dictionary

        Raises:
            ValueError: If tool not found
        """
        if name not in self.tools:
            raise ValueError(f"Unknown tool: {name}")

        tool = self.tools[name]
        return tool.handler(arguments)

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """
        Handle a JSON-RPC request.

        Args:
            request: JSON-RPC request object

        Returns:
            JSON-RPC response object
        """
        method = request.get("method", "")
        params = request.get("params", {})
        request_id = request.get("id")

        try:
            result: dict[str, Any]
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": self.name,
                        "version": self.version,
                    },
                }
            elif method == "tools/list":
                result = {"tools": self.get_tools_list()}
            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                tool_result = self.call_tool(tool_name, arguments)
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(tool_result, indent=2),
                        }
                    ],
                }
            elif method == "notifications/initialized":
                # Client notification, no response needed
                return {}
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                }

            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }

        except Exception as e:
            logger.exception(f"Error handling request: {method}")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": str(e),
                },
            }

    def run(self) -> None:
        """
        Run the MCP server with stdio transport.

        Reads JSON-RPC requests from stdin, processes them,
        and writes responses to stdout.
        """
        logger.info(f"Starting MCP server: {self.name} v{self.version}")

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
                response = self.handle_request(request)

                if response:  # Skip empty responses (notifications)
                    print(json.dumps(response), flush=True)

            except json.JSONDecodeError as e:
                error_response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32700,
                        "message": f"Parse error: {e}",
                    },
                }
                print(json.dumps(error_response), flush=True)


def create_server() -> MCPServer:
    """Create and return an MCP server instance."""
    return MCPServer()


def create_fastmcp_server(http_mode: bool = False) -> FastMCP:
    """Create a FastMCP server with all tools registered from the unified registry.

    Args:
        http_mode: If True, creates server in stateless HTTP mode.

    Returns:
        Configured FastMCP server instance.

    Raises:
        ImportError: If fastmcp is not installed.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise ImportError(
            "FastMCP is required for HTTP transport. Install with: pip install 'kicad-tools[mcp]'"
        ) from e

    mcp = FastMCP("kicad-tools", stateless_http=http_mode)

    # Register all tools from the unified registry
    for tool_name, tool_spec in TOOL_REGISTRY.items():
        _register_fastmcp_tool(mcp, tool_spec)

    return mcp


def _register_fastmcp_tool(mcp: FastMCP, tool_spec: ToolSpec) -> None:
    """Register a single tool from the registry to FastMCP.

    Creates a wrapper function with proper type annotations for FastMCP
    and registers it using the @mcp.tool() decorator pattern.

    Args:
        mcp: The FastMCP server instance
        tool_spec: The tool specification from the registry
    """
    # Create a dynamic wrapper function that FastMCP can introspect
    # The function calls the registry handler with its kwargs as a dict
    def create_handler(spec: ToolSpec) -> Callable:
        """Create a handler function for FastMCP registration."""

        def handler(**kwargs: Any) -> dict:
            """Execute the tool with given parameters."""
            return spec.handler(kwargs)

        # Copy metadata for FastMCP
        handler.__name__ = spec.name
        handler.__doc__ = spec.description

        return handler

    # Register the tool with FastMCP
    handler_func = create_handler(tool_spec)
    mcp.tool()(handler_func)


def run_server(
    transport: str = "stdio",
    host: str = "localhost",
    port: int = 8080,
) -> None:
    """Run the MCP server with the specified transport.

    Args:
        transport: Transport mode - 'stdio' or 'http'
        host: Host address for HTTP mode (default: localhost)
        port: Port for HTTP mode (default: 8080)

    Raises:
        ValueError: If transport is not 'stdio' or 'http'
        ImportError: If fastmcp is not installed for HTTP mode
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )

    if transport == "stdio":
        # Use existing MCPServer for stdio (backward compatible)
        server = create_server()
        server.run()
    elif transport == "http":
        # Use FastMCP for HTTP transport
        mcp = create_fastmcp_server(http_mode=True)
        logger.info(f"Starting HTTP MCP server on {host}:{port}")
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        raise ValueError(f"Unknown transport: {transport}. Use 'stdio' or 'http'.")


def main() -> None:
    """Entry point for MCP server (stdio mode by default)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )
    server = create_server()
    server.run()


if __name__ == "__main__":
    main()
