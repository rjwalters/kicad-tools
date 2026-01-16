"""MCP tools for kicad-tools.

Each tool module provides specific functionality for AI agents.
"""

from kicad_tools.mcp.tools.analysis import (
    analyze_board,
    get_drc_violations,
    measure_clearance,
)
from kicad_tools.mcp.tools.context import (
    annotate_decision,
    create_checkpoint,
    get_decision_history,
    get_session_context,
    get_session_summary,
    record_decision,
    restore_checkpoint,
)
from kicad_tools.mcp.tools.export import export_gerbers
from kicad_tools.mcp.tools.patterns import (
    adapt_pattern,
    get_requirements,
    list_available_components,
    validate_pattern,
)
from kicad_tools.mcp.tools.routing import (
    get_unrouted_nets,
    route_net,
)

__all__ = [
    "adapt_pattern",
    "analyze_board",
    "annotate_decision",
    "create_checkpoint",
    "export_gerbers",
    "get_decision_history",
    "get_drc_violations",
    "get_requirements",
    "get_session_context",
    "get_session_summary",
    "get_unrouted_nets",
    "list_available_components",
    "measure_clearance",
    "record_decision",
    "restore_checkpoint",
    "route_net",
    "validate_pattern",
]
