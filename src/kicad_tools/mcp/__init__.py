"""
MCP (Model Context Protocol) server for kicad-tools.

Provides AI agent integration through the MCP protocol, allowing AI agents
to interact with KiCad projects via standardized tools.

Usage:
    kct mcp serve              # Start stdio MCP server (for Claude Desktop)
    kct mcp serve --http       # Start HTTP MCP server (for web)

Example Claude Desktop configuration (~/.config/claude/claude_desktop_config.json):
    {
        "mcpServers": {
            "kicad-tools": {
                "command": "kct",
                "args": ["mcp", "serve"]
            }
        }
    }

This module provides:
- MCPTools: Modular tool registration system
- MCPError: Structured error responses for MCP
- run_mcp(): Main entry point for the MCP server
"""

from kicad_tools.mcp.errors import MCPError
from kicad_tools.mcp.server import run_mcp
from kicad_tools.mcp.tools import MCPTools

__all__ = [
    "MCPTools",
    "MCPError",
    "run_mcp",
]
