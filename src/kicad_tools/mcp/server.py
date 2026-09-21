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
from typing import TYPE_CHECKING, Any, Callable, cast

if TYPE_CHECKING:
    # mcp SDK >= 2.x (#5601): ``FastMCP`` was renamed to ``MCPServer`` and
    # moved from ``mcp.server.fastmcp`` to ``mcp.server.mcpserver``.  Aliased
    # here because this module already defines its own stdio ``MCPServer``
    # dataclass above.
    from mcp.server.mcpserver import MCPServer as SDKMCPServer

from jsonschema import validate  # type: ignore[import-untyped]  # Upstream has no inline types.

from kicad_tools.mcp.observability import record_call
from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

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

        def dispatch(params: dict[str, Any]) -> dict[str, Any]:
            if name not in self.tools:
                raise ValueError(f"Unknown tool: {name}")
            tool = self.tools[name]
            validate(instance=params, schema=tool.parameters)
            return cast(dict[str, Any], tool.handler(params))

        # Include lookup and validation failures; handlers are recorded exactly once.
        return record_call(name, dispatch, arguments)

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

                # If the handler provides pre-built MCP content blocks
                # (e.g. with image data), use those directly.
                if isinstance(tool_result, dict) and "_mcp_content" in tool_result:
                    result = {"content": tool_result["_mcp_content"]}
                else:
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


def create_fastmcp_server(
    http_mode: bool = False,
    host: str | None = None,
    port: int | None = None,
) -> SDKMCPServer:
    """Create an SDK MCP server with all tools registered from the unified registry.

    Args:
        http_mode: If True, the server runs in stateless HTTP mode (the
            ``stateless_http`` run option is recorded for the caller).
        host: Bind address for HTTP transports. The SDK's own default
            (``127.0.0.1``) is used when omitted. Since mcp 2.x (#5601),
            ``host``/``port``/``stateless_http`` are no longer constructor
            settings -- they are ``run(transport=...)`` keyword arguments --
            so they are recorded on the returned instance as
            ``http_run_options`` for whatever call site invokes ``run()``.
        port: Bind port for HTTP transports. The SDK's own default (``8000``)
            is used when omitted.

    Returns:
        Configured SDK ``MCPServer`` instance carrying an
        ``http_run_options`` dict to splat into ``run()``.

    Raises:
        ImportError: If the MCP SDK is not installed, or is a version
            without ``mcp.server.mcpserver.MCPServer`` (mcp 1.x still named
            it ``mcp.server.fastmcp.FastMCP``).
    """
    try:
        from mcp.server.mcpserver import MCPServer as SDKMCPServer
    except ImportError as e:
        # Distinguish "not installed" from "installed but the API moved"
        # (#5601): a missing top-level ``mcp`` package is a genuine install
        # gap; any other import failure means an SDK version without the
        # renamed class -- surfacing that as "not installed" is exactly the
        # misclassification that turned the mcp 2.x CI failures into silent
        # test skips.
        if isinstance(e, ModuleNotFoundError) and e.name == "mcp":
            raise ImportError(
                "The MCP SDK is required for HTTP transport. "
                "Install with: pip install 'kicad-tools[mcp]'"
            ) from e
        raise ImportError(
            "An MCP SDK >= 2 is required (FastMCP was renamed to MCPServer in "
            "mcp 2.x, issue #5601); the installed SDK does not provide "
            "mcp.server.mcpserver.MCPServer."
        ) from e

    class RegistrySDKServer(SDKMCPServer):
        """Use the registry's JSON schemas and dict handlers without a kwargs shim.

        The SDK server registers these public overrides with its low-level
        protocol server (with low-level input validation disabled). Shared
        dispatch owns validation and recording, including failures before a
        handler runs.
        """

        # Set by create_fastmcp_server after construction (see run_server):
        # mcp 2.x host/port/stateless_http are run()-time options.
        http_run_options: dict[str, Any]

        async def list_tools(self) -> list[Any]:
            from mcp.types import Tool

            return [Tool(**item) for item in dispatcher.get_tools_list()]

        async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
            # ``context`` is supplied positionally by the 2.x protocol layer
            # (mcp 1.x never passed it); registry dispatch does not use it.
            from mcp.types import CallToolResult, TextContent

            # mcp 2.x contract: call_tool returns a complete CallToolResult
            # (the 1.x framework used to wrap raw dict returns itself).
            # Registry-dispatch failures (unknown tool, schema validation)
            # are recorded by ``record_call`` before they propagate; surface
            # them as isError results rather than letting an SDK-foreign
            # exception type escape the override.
            try:
                result = dispatcher.call_tool(name, arguments)
            except Exception as e:
                return CallToolResult(
                    content=[TextContent(type="text", text=str(e))],
                    is_error=True,
                )
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result, indent=2))],
                structured_content=result,
                is_error=False,
            )

    dispatcher = MCPServer()
    server = RegistrySDKServer("kicad-tools")
    # mcp 2.x (#5601): host/port/stateless moved from constructor Settings
    # to run(transport=...) kwargs; remember the caller's intent so the run
    # site (run_server below, or any direct caller) can splat them in.
    http_run_options: dict[str, Any] = {"stateless_http": http_mode}
    if host is not None:
        http_run_options["host"] = host
    if port is not None:
        http_run_options["port"] = port
    server.http_run_options = http_run_options
    return server


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
        # Use the SDK server for HTTP transport. Since mcp 2.x (#5601),
        # host/port/stateless_http are run() keyword arguments -- recorded on
        # the instance by create_fastmcp_server as http_run_options.
        mcp = create_fastmcp_server(http_mode=True, host=host, port=port)
        logger.info(f"Starting HTTP MCP server on {host}:{port}")
        # The recorded options are typed via getattr (the declared return type
        # is the SDK base class, which does not carry the attribute). The
        # kwargs all match run_streamable_http_async's signature; the wiring
        # is exercised live by TestHTTPTransportSmoke and the run_server
        # unit tests.
        run_options: dict[str, Any] = getattr(mcp, "http_run_options", {"stateless_http": True})
        mcp.run(transport="streamable-http", **run_options)
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
