"""MCP (Model Context Protocol) server for kicad-tools.

This module provides tools that can be exposed via MCP for AI agents
to analyze and manipulate KiCad PCB designs.

Supports two transport modes:
- stdio: Default mode for Claude Desktop integration
- http: HTTP mode for web-based integrations

Example (stdio):
    kct mcp serve

Example (HTTP):
    kct mcp serve --transport http --port 8080
"""

from kicad_tools.mcp.context import (
    AgentPreferences,
    AnnotateDecisionResult,
    CheckpointResult,
    Decision,
    DecisionHistoryResult,
    RecordDecisionResult,
    SessionContext,
    SessionContextResult,
    SessionSummaryResult,
    StateSnapshot,
)
from kicad_tools.mcp.preference_learner import (
    PatternMatch,
    PreferenceLearner,
)
from kicad_tools.mcp.server import (
    MCPServer,
    create_fastmcp_server,
    create_server,
    run_server,
)
from kicad_tools.mcp.types import (
    AffectedItem,
    BoardAnalysis,
    BoardDimensions,
    ClearanceMeasurement,
    ClearanceResult,
    ComponentSummary,
    DRCResult,
    DRCViolation,
    LayerInfo,
    NetFanout,
    NetSummary,
    RoutingStatus,
    ViolationLocation,
    ZoneInfo,
)

__all__ = [
    # Server
    "MCPServer",
    "create_server",
    "create_fastmcp_server",
    "run_server",
    # Context types
    "AgentPreferences",
    "AnnotateDecisionResult",
    "CheckpointResult",
    "Decision",
    "DecisionHistoryResult",
    "RecordDecisionResult",
    "SessionContext",
    "SessionContextResult",
    "SessionSummaryResult",
    "StateSnapshot",
    # Preference learner
    "PatternMatch",
    "PreferenceLearner",
    # Board analysis types
    "AffectedItem",
    "BoardAnalysis",
    "BoardDimensions",
    "ClearanceMeasurement",
    "ClearanceResult",
    "ComponentSummary",
    "DRCResult",
    "DRCViolation",
    "LayerInfo",
    "NetFanout",
    "NetSummary",
    "RoutingStatus",
    "ViolationLocation",
    "ZoneInfo",
]
