"""MCP (Model Context Protocol) server for kicad-tools.

This module provides tools that can be exposed via MCP for AI agents
to analyze and manipulate KiCad PCB designs.
"""

from kicad_tools.mcp.server import MCPServer, create_server
from kicad_tools.mcp.types import (
    BoardAnalysis,
    BoardDimensions,
    ComponentSummary,
    LayerInfo,
    NetFanout,
    NetSummary,
    RoutingStatus,
    ZoneInfo,
)

__all__ = [
    "MCPServer",
    "create_server",
    "BoardAnalysis",
    "BoardDimensions",
    "ComponentSummary",
    "LayerInfo",
    "NetFanout",
    "NetSummary",
    "RoutingStatus",
    "ZoneInfo",
]
