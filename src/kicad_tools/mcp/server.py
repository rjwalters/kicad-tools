"""
FastMCP server entry point for kicad-tools.

Provides the main MCP server that exposes kicad-tools functionality
to AI agents via the Model Context Protocol.

Usage:
    # Via CLI
    kct mcp serve                    # stdio transport (Claude Desktop)
    kct mcp serve --http             # HTTP transport (web)
    kct mcp serve --http --port 8080 # Custom port

    # Programmatically
    from kicad_tools.mcp.server import run_mcp
    run_mcp(http=False, debug=False)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Debug mode can be enabled via environment variable
DEBUG_ENV_VAR = "KICAD_TOOLS_MCP_DEBUG"


def _setup_debug_logging(enable: bool = False) -> None:
    """Configure debug logging to file.

    When enabled, logs MCP server activity to a file for debugging.

    Args:
        enable: Whether to enable debug logging
    """
    if not enable:
        return

    log_path = Path(__file__).parent / "mcp_debug.log"
    handler = logging.FileHandler(log_path)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)

    # Add handler to the kicad_tools.mcp logger
    mcp_logger = logging.getLogger("kicad_tools.mcp")
    mcp_logger.addHandler(handler)
    mcp_logger.setLevel(logging.DEBUG)

    logger.info("Debug logging enabled, writing to %s", log_path)


def _check_mcp_available() -> bool:
    """Check if the MCP package is installed.

    Returns:
        True if mcp is available, False otherwise.
    """
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401

        return True
    except ImportError:
        return False


def create_server() -> FastMCP:
    """Create and configure the MCP server.

    Creates a FastMCP server instance and registers all available tools.
    Currently returns an empty server - tools will be added in future PRs.

    Returns:
        Configured FastMCP server instance.

    Raises:
        ImportError: If the mcp package is not installed.
    """
    if not _check_mcp_available():
        raise ImportError(
            "MCP support requires the 'mcp' optional dependency. "
            "Install with: pip install kicad-tools[mcp]"
        )

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("kicad-tools")

    # Tools will be registered here as they are implemented
    # For now, this is an empty server that responds to list_tools

    logger.info("Created MCP server with %d tools", 0)

    return mcp


def run_mcp(
    http: bool = False,
    port: int = 8000,
    debug: bool = False,
) -> None:
    """Run the MCP server.

    Main entry point for the MCP server. Supports both stdio transport
    (for Claude Desktop) and HTTP transport (for web applications).

    Args:
        http: If True, use HTTP transport. Otherwise, use stdio.
        port: Port number for HTTP transport (default: 8000).
        debug: If True, enable debug logging to file.

    Raises:
        ImportError: If the mcp package is not installed.

    Example::

        # For Claude Desktop integration
        run_mcp()

        # For HTTP server
        run_mcp(http=True, port=8080)

        # With debug logging
        run_mcp(debug=True)
    """
    import os

    # Check for debug environment variable
    env_debug = os.environ.get(DEBUG_ENV_VAR, "").lower() in ("1", "true", "yes")
    _setup_debug_logging(enable=debug or env_debug)

    mcp = create_server()

    transport = "streamable-http" if http else "stdio"
    logger.info("Starting kicad-tools MCP server with %s transport...", transport)

    if http:
        # For HTTP transport, we need to configure the port
        # FastMCP's run() method handles this internally
        mcp.run(transport=transport)
    else:
        mcp.run(transport=transport)


__all__ = ["run_mcp", "create_server"]
