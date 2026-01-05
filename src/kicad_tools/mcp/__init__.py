"""MCP (Model Context Protocol) tools for AI agent integration.

This module provides tools that can be exposed via MCP for AI agents
to analyze and manipulate KiCad PCB designs.
"""

from .types import (
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
    "BoardAnalysis",
    "BoardDimensions",
    "ComponentSummary",
    "LayerInfo",
    "NetFanout",
    "NetSummary",
    "RoutingStatus",
    "ZoneInfo",
]
