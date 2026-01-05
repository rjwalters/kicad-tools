"""
MCP (Model Context Protocol) server for kicad-tools.

Provides MCP tools for AI agents to interact with KiCad files.
"""

from kicad_tools.mcp.server import MCPServer, create_server

__all__ = ["MCPServer", "create_server"]
