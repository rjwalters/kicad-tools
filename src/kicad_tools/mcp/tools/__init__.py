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
from kicad_tools.mcp.tools.design import (
    add_subsystem,
    group_components,
    list_available_subsystem_types,
    plan_subsystem,
    validate_design,
    validate_move,
)
from kicad_tools.mcp.tools.explain import (
    explain_drc_violations,
    explain_net,
    explain_rule,
    list_available_rules,
    search_available_rules,
)
from kicad_tools.mcp.tools.export import export_gerbers
from kicad_tools.mcp.tools.mistakes import (
    detect_mistakes,
    list_mistake_categories,
)
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
    "add_subsystem",
    "analyze_board",
    "annotate_decision",
    "create_checkpoint",
    "detect_mistakes",
    "explain_drc_violations",
    "explain_net",
    "explain_rule",
    "export_gerbers",
    "get_decision_history",
    "get_drc_violations",
    "get_requirements",
    "get_session_context",
    "get_session_summary",
    "get_unrouted_nets",
    "group_components",
    "list_available_components",
    "list_available_rules",
    "list_available_subsystem_types",
    "list_mistake_categories",
    "measure_clearance",
    "plan_subsystem",
    "record_decision",
    "restore_checkpoint",
    "route_net",
    "search_available_rules",
    "validate_design",
    "validate_move",
    "validate_pattern",
]
