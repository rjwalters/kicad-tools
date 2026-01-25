"""Unified tool registry for MCP server.

Provides a single source of truth for tool definitions used by both
stdio and HTTP transports. This eliminates duplication between MCPServer
and create_fastmcp_server() implementations.

Example:
    >>> from kicad_tools.mcp.tools.registry import TOOL_REGISTRY, get_tool
    >>> tool = get_tool("export_gerbers")
    >>> result = tool.handler({"pcb_path": "/path/to/board.kicad_pcb", ...})
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolSpec:
    """Specification for an MCP tool.

    Attributes:
        name: Unique tool identifier
        description: Human-readable description for LLM context
        parameters: JSON Schema for tool input validation
        handler: Function that executes the tool
        category: Optional category for grouping tools
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    category: str = "general"


# Global tool registry
TOOL_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    handler: Callable[..., Any],
    category: str = "general",
) -> ToolSpec:
    """Register a tool in the global registry.

    Args:
        name: Unique tool identifier
        description: Human-readable description
        parameters: JSON Schema for input validation
        handler: Function that executes the tool
        category: Optional category for grouping

    Returns:
        The registered ToolSpec

    Raises:
        ValueError: If tool with same name already registered
    """
    if name in TOOL_REGISTRY:
        raise ValueError(f"Tool already registered: {name}")

    spec = ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        category=category,
    )
    TOOL_REGISTRY[name] = spec
    return spec


def get_tool(name: str) -> ToolSpec | None:
    """Get a tool specification by name.

    Args:
        name: Tool identifier

    Returns:
        ToolSpec or None if not found
    """
    return TOOL_REGISTRY.get(name)


def list_tools(category: str | None = None) -> list[ToolSpec]:
    """List all registered tools.

    Args:
        category: Optional filter by category

    Returns:
        List of ToolSpec objects
    """
    if category is None:
        return list(TOOL_REGISTRY.values())
    return [t for t in TOOL_REGISTRY.values() if t.category == category]


def clear_registry() -> None:
    """Clear all registered tools (for testing)."""
    TOOL_REGISTRY.clear()


# =============================================================================
# Tool Definitions
# =============================================================================

# Helper to create JSON Schema for tool parameters


def _make_params(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    """Create a JSON Schema object for tool parameters."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


# -----------------------------------------------------------------------------
# Export Tools
# -----------------------------------------------------------------------------


def _handler_validate_assembly_bom(params: dict[str, Any]) -> dict[str, Any]:
    """Handle validate_assembly_bom tool call."""
    from kicad_tools.mcp.tools.export import validate_assembly_bom

    return validate_assembly_bom(
        schematic_path=params["schematic_path"],
        quantity=params.get("quantity", 1),
    )


register_tool(
    name="validate_assembly_bom",
    description=(
        "Validate a BOM for JLCPCB assembly. Checks all components against the "
        "LCSC/JLCPCB parts library and categorizes them by tier (Basic/Extended), "
        "stock status, and availability. Returns a summary of parts that are "
        "available, out of stock, or missing LCSC part numbers. Use this to "
        "verify assembly readiness before ordering."
    ),
    parameters=_make_params(
        properties={
            "schematic_path": {
                "type": "string",
                "description": "Path to .kicad_sch file",
            },
            "quantity": {
                "type": "integer",
                "description": "Number of boards (multiplies component quantities)",
                "default": 1,
            },
        },
        required=["schematic_path"],
    ),
    handler=_handler_validate_assembly_bom,
    category="export",
)


def _handler_export_gerbers(params: dict[str, Any]) -> dict[str, Any]:
    """Handle export_gerbers tool call."""
    from kicad_tools.mcp.tools.export import export_gerbers

    result = export_gerbers(
        pcb_path=params["pcb_path"],
        output_dir=params["output_dir"],
        manufacturer=params.get("manufacturer", "generic"),
        include_drill=params.get("include_drill", True),
        zip_output=params.get("zip_output", True),
    )
    return result.to_dict()


register_tool(
    name="export_gerbers",
    description=(
        "Export Gerber files for PCB manufacturing. Generates all required "
        "Gerber layers (copper, soldermask, silkscreen, outline) and optionally "
        "drill files. Supports manufacturer presets for JLCPCB, OSHPark, PCBWay, and Seeed."
    ),
    parameters=_make_params(
        properties={
            "pcb_path": {
                "type": "string",
                "description": "Path to .kicad_pcb file",
            },
            "output_dir": {
                "type": "string",
                "description": "Directory for output files",
            },
            "manufacturer": {
                "type": "string",
                "description": "Manufacturer preset",
                "enum": ["generic", "jlcpcb", "pcbway", "oshpark", "seeed"],
                "default": "generic",
            },
            "include_drill": {
                "type": "boolean",
                "description": "Include drill files (Excellon format)",
                "default": True,
            },
            "zip_output": {
                "type": "boolean",
                "description": "Create zip archive of all files",
                "default": True,
            },
        },
        required=["pcb_path", "output_dir"],
    ),
    handler=_handler_export_gerbers,
    category="export",
)


def _handler_export_bom(params: dict[str, Any]) -> dict[str, Any]:
    """Handle export_bom tool call."""
    from kicad_tools.mcp.tools.export import export_bom

    result = export_bom(
        schematic_path=params["schematic_path"],
        output_path=params.get("output_path"),
        format=params.get("format", "csv"),
        group_by=params.get("group_by", "value+footprint"),
        include_dnp=params.get("include_dnp", False),
    )
    return result.to_dict()


register_tool(
    name="export_bom",
    description=(
        "Export Bill of Materials (BOM) from a KiCad schematic file. "
        "Generates a component list with quantities, values, footprints, and "
        "part numbers. Supports multiple output formats including CSV, JSON, "
        "and manufacturer-specific formats (JLCPCB, PCBWay, Seeed). "
        "Automatically extracts LCSC part numbers from component fields."
    ),
    parameters=_make_params(
        properties={
            "schematic_path": {
                "type": "string",
                "description": "Path to .kicad_sch file",
            },
            "output_path": {
                "type": "string",
                "description": "Output file path (optional - omit for data-only response)",
            },
            "format": {
                "type": "string",
                "description": "Output format",
                "enum": ["csv", "json", "jlcpcb", "pcbway", "seeed"],
                "default": "csv",
            },
            "group_by": {
                "type": "string",
                "description": "Component grouping strategy",
                "enum": ["value", "footprint", "value+footprint", "mpn", "none"],
                "default": "value+footprint",
            },
            "include_dnp": {
                "type": "boolean",
                "description": "Include Do Not Place components",
                "default": False,
            },
        },
        required=["schematic_path"],
    ),
    handler=_handler_export_bom,
    category="export",
)


def _handler_export_assembly(params: dict[str, Any]) -> dict[str, Any]:
    """Handle export_assembly tool call."""
    from kicad_tools.mcp.tools.export import export_assembly

    result = export_assembly(
        pcb_path=params["pcb_path"],
        schematic_path=params["schematic_path"],
        output_dir=params["output_dir"],
        manufacturer=params.get("manufacturer", "jlcpcb"),
    )
    return result.to_dict()


register_tool(
    name="export_assembly",
    description=(
        "Generate complete assembly package for manufacturing. Creates Gerber files, "
        "bill of materials (BOM), and pick-and-place (PnP/CPL) files tailored to "
        "specific manufacturers. Outputs a single zip file ready for upload to "
        "JLCPCB, PCBWay, Seeed, or generic assembly services."
    ),
    parameters=_make_params(
        properties={
            "pcb_path": {
                "type": "string",
                "description": "Path to .kicad_pcb file",
            },
            "schematic_path": {
                "type": "string",
                "description": "Path to .kicad_sch file",
            },
            "output_dir": {
                "type": "string",
                "description": "Directory for output files",
            },
            "manufacturer": {
                "type": "string",
                "description": "Target manufacturer for assembly",
                "enum": ["jlcpcb", "pcbway", "seeed", "generic"],
                "default": "jlcpcb",
            },
        },
        required=["pcb_path", "schematic_path", "output_dir"],
    ),
    handler=_handler_export_assembly,
    category="export",
)


# -----------------------------------------------------------------------------
# Placement Tools
# -----------------------------------------------------------------------------


def _handler_placement_analyze(params: dict[str, Any]) -> dict[str, Any]:
    """Handle placement_analyze tool call."""
    from kicad_tools.mcp.tools.placement import placement_analyze

    result = placement_analyze(
        pcb_path=params["pcb_path"],
        check_thermal=params.get("check_thermal", True),
        check_signal_integrity=params.get("check_signal_integrity", True),
        check_manufacturing=params.get("check_manufacturing", True),
    )
    return result.to_dict()


register_tool(
    name="placement_analyze",
    description=(
        "Analyze current component placement quality. Evaluates placement with "
        "metrics for wire length, congestion, thermal characteristics, signal "
        "integrity, and manufacturing concerns. Returns an overall score, "
        "category scores, identified issues with suggestions, detected functional "
        "clusters, and routing difficulty estimates."
    ),
    parameters=_make_params(
        properties={
            "pcb_path": {
                "type": "string",
                "description": "Path to .kicad_pcb file",
            },
            "check_thermal": {
                "type": "boolean",
                "description": "Include thermal analysis (power components, heat spreading)",
                "default": True,
            },
            "check_signal_integrity": {
                "type": "boolean",
                "description": "Include signal integrity hints (high-speed nets, crosstalk)",
                "default": True,
            },
            "check_manufacturing": {
                "type": "boolean",
                "description": "Include DFM checks (clearances, assembly)",
                "default": True,
            },
        },
        required=["pcb_path"],
    ),
    handler=_handler_placement_analyze,
    category="placement",
)


def _handler_placement_suggestions(params: dict[str, Any]) -> dict[str, Any]:
    """Handle placement_suggestions tool call."""
    from kicad_tools.mcp.tools.placement import placement_suggestions

    result = placement_suggestions(
        pcb_path=params["pcb_path"],
        component=params.get("component"),
        max_suggestions=params.get("max_suggestions", 10),
        strategy=params.get("strategy", "balanced"),
    )
    return result.to_dict()


register_tool(
    name="placement_suggestions",
    description=(
        "Get AI-friendly placement improvement suggestions for a PCB. "
        "Analyzes component placement and returns actionable recommendations "
        "ranked by priority."
    ),
    parameters=_make_params(
        properties={
            "pcb_path": {
                "type": "string",
                "description": "Path to .kicad_pcb file",
            },
            "component": {
                "type": "string",
                "description": "Specific component reference to analyze (optional)",
            },
            "max_suggestions": {
                "type": "integer",
                "description": "Maximum number of suggestions (1-50)",
                "default": 10,
            },
            "strategy": {
                "type": "string",
                "description": "Optimization strategy (balanced, wire_length, thermal, si)",
                "enum": ["balanced", "wire_length", "thermal", "si"],
                "default": "balanced",
            },
        },
        required=["pcb_path"],
    ),
    handler=_handler_placement_suggestions,
    category="placement",
)


# -----------------------------------------------------------------------------
# Session Tools
# -----------------------------------------------------------------------------


def _handler_start_session(params: dict[str, Any]) -> dict[str, Any]:
    """Handle start_session tool call."""
    from kicad_tools.mcp.tools.session import start_session

    result = start_session(
        pcb_path=params["pcb_path"],
        fixed_refs=params.get("fixed_refs"),
    )
    return result.to_dict()


register_tool(
    name="start_session",
    description=(
        "Start a new placement refinement session. Creates a stateful session "
        "for interactively refining component placement through query-before-commit "
        "operations. Returns a session ID used for subsequent operations."
    ),
    parameters=_make_params(
        properties={
            "pcb_path": {
                "type": "string",
                "description": "Absolute path to .kicad_pcb file",
            },
            "fixed_refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Component references to keep fixed (optional)",
            },
        },
        required=["pcb_path"],
    ),
    handler=_handler_start_session,
    category="session",
)


def _handler_query_move(params: dict[str, Any]) -> dict[str, Any]:
    """Handle query_move tool call."""
    from kicad_tools.mcp.tools.session import query_move

    result = query_move(
        session_id=params["session_id"],
        ref=params["ref"],
        x=params["x"],
        y=params["y"],
        rotation=params.get("rotation"),
    )
    return result.to_dict()


register_tool(
    name="query_move",
    description=(
        "Query the impact of a hypothetical component move without applying it. "
        "Returns score changes, new/resolved violations, and routing impact. "
        "Use this to evaluate moves before applying them."
    ),
    parameters=_make_params(
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID from start_session",
            },
            "ref": {
                "type": "string",
                "description": "Component reference designator (e.g., 'C1', 'R5')",
            },
            "x": {
                "type": "number",
                "description": "Target X position in millimeters",
            },
            "y": {
                "type": "number",
                "description": "Target Y position in millimeters",
            },
            "rotation": {
                "type": "number",
                "description": "Target rotation in degrees (optional, keeps current if not specified)",
            },
        },
        required=["session_id", "ref", "x", "y"],
    ),
    handler=_handler_query_move,
    category="session",
)


def _handler_apply_move(params: dict[str, Any]) -> dict[str, Any]:
    """Handle apply_move tool call."""
    from kicad_tools.mcp.tools.session import apply_move

    result = apply_move(
        session_id=params["session_id"],
        ref=params["ref"],
        x=params["x"],
        y=params["y"],
        rotation=params.get("rotation"),
    )
    return result.to_dict()


register_tool(
    name="apply_move",
    description=(
        "Apply a component move within the session. The move can be undone with "
        "undo_move and is not written to disk until commit_session is called. "
        "Returns updated component position and score delta."
    ),
    parameters=_make_params(
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID from start_session",
            },
            "ref": {
                "type": "string",
                "description": "Component reference designator",
            },
            "x": {
                "type": "number",
                "description": "New X position in millimeters",
            },
            "y": {
                "type": "number",
                "description": "New Y position in millimeters",
            },
            "rotation": {
                "type": "number",
                "description": "New rotation in degrees (optional)",
            },
        },
        required=["session_id", "ref", "x", "y"],
    ),
    handler=_handler_apply_move,
    category="session",
)


def _handler_undo_move(params: dict[str, Any]) -> dict[str, Any]:
    """Handle undo_move tool call."""
    from kicad_tools.mcp.tools.session import undo_move

    result = undo_move(session_id=params["session_id"])
    return result.to_dict()


register_tool(
    name="undo_move",
    description=(
        "Undo the last applied move in the session. Restores the component "
        "to its previous position. Can be called multiple times to undo "
        "multiple moves."
    ),
    parameters=_make_params(
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID from start_session",
            },
        },
        required=["session_id"],
    ),
    handler=_handler_undo_move,
    category="session",
)


def _handler_commit_session(params: dict[str, Any]) -> dict[str, Any]:
    """Handle commit_session tool call."""
    from kicad_tools.mcp.tools.session import commit_session

    result = commit_session(
        session_id=params["session_id"],
        output_path=params.get("output_path"),
    )
    return result.to_dict()


register_tool(
    name="commit_session",
    description=(
        "Commit all pending moves to the PCB file and close the session. "
        "Writes changes to disk. Optionally specify output_path to save to "
        "a different file instead of overwriting the original."
    ),
    parameters=_make_params(
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID from start_session",
            },
            "output_path": {
                "type": "string",
                "description": "Output file path (optional, overwrites original if not specified)",
            },
        },
        required=["session_id"],
    ),
    handler=_handler_commit_session,
    category="session",
)


def _handler_rollback_session(params: dict[str, Any]) -> dict[str, Any]:
    """Handle rollback_session tool call."""
    from kicad_tools.mcp.tools.session import rollback_session

    result = rollback_session(session_id=params["session_id"])
    return result.to_dict()


register_tool(
    name="rollback_session",
    description=(
        "Discard all pending moves and close the session. No changes are "
        "written to disk. Use this to abandon a session without saving."
    ),
    parameters=_make_params(
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID from start_session",
            },
        },
        required=["session_id"],
    ),
    handler=_handler_rollback_session,
    category="session",
)


# -----------------------------------------------------------------------------
# Context Tools
# -----------------------------------------------------------------------------


def _handler_record_decision(params: dict[str, Any]) -> dict[str, Any]:
    """Handle record_decision tool call."""
    from kicad_tools.mcp.tools.context import record_decision

    result = record_decision(
        session_id=params["session_id"],
        action=params["action"],
        target=params["target"],
        rationale=params.get("rationale"),
        alternatives=params.get("alternatives"),
        confidence=params.get("confidence", 0.8),
    )
    return result.to_dict()


register_tool(
    name="record_decision",
    description=(
        "Record a design decision with rationale. Creates a permanent record "
        "of a design decision, including reasoning and alternatives considered. "
        "Use this to build a decision history for learning and context."
    ),
    parameters=_make_params(
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID from start_session",
            },
            "action": {
                "type": "string",
                "description": "Action type (e.g., 'move', 'route', 'place')",
            },
            "target": {
                "type": "string",
                "description": "Target of action (component ref, net name, etc.)",
            },
            "rationale": {
                "type": "string",
                "description": "Why this decision was made (optional)",
            },
            "alternatives": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Other options considered (optional)",
            },
            "confidence": {
                "type": "number",
                "description": "Agent confidence in decision (0.0-1.0)",
                "default": 0.8,
            },
        },
        required=["session_id", "action", "target"],
    ),
    handler=_handler_record_decision,
    category="context",
)


def _handler_get_decision_history(params: dict[str, Any]) -> dict[str, Any]:
    """Handle get_decision_history tool call."""
    from kicad_tools.mcp.tools.context import get_decision_history

    result = get_decision_history(
        session_id=params["session_id"],
        limit=params.get("limit", 20),
        filter_action=params.get("filter_action"),
    )
    return result.to_dict()


register_tool(
    name="get_decision_history",
    description=(
        "Get recent decisions in session. Returns decision history with "
        "detected patterns. Optionally filter by action type."
    ),
    parameters=_make_params(
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID from start_session",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum decisions to return",
                "default": 20,
            },
            "filter_action": {
                "type": "string",
                "description": "Filter by action type (optional)",
            },
        },
        required=["session_id"],
    ),
    handler=_handler_get_decision_history,
    category="context",
)


def _handler_annotate_decision(params: dict[str, Any]) -> dict[str, Any]:
    """Handle annotate_decision tool call."""
    from kicad_tools.mcp.tools.context import annotate_decision

    result = annotate_decision(
        session_id=params["session_id"],
        decision_id=params["decision_id"],
        feedback=params["feedback"],
        outcome=params.get("outcome"),
    )
    return result.to_dict()


register_tool(
    name="annotate_decision",
    description=(
        "Add feedback to a past decision. Updates the decision record "
        "with feedback and optionally changes its outcome status."
    ),
    parameters=_make_params(
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID from start_session",
            },
            "decision_id": {
                "type": "string",
                "description": "ID of the decision to annotate",
            },
            "feedback": {
                "type": "string",
                "description": "Feedback text to add",
            },
            "outcome": {
                "type": "string",
                "enum": ["success", "partial", "reverted", "pending"],
                "description": "New outcome status (optional)",
            },
        },
        required=["session_id", "decision_id", "feedback"],
    ),
    handler=_handler_annotate_decision,
    category="context",
)


def _handler_get_session_context(params: dict[str, Any]) -> dict[str, Any]:
    """Handle get_session_context tool call."""
    from kicad_tools.mcp.tools.context import get_session_context

    result = get_session_context(
        session_id=params["session_id"],
        detail_level=params.get("detail_level", "summary"),
    )
    return result.to_dict()


register_tool(
    name="get_session_context",
    description=(
        "Get session context at specified detail level. Returns session "
        "state including decisions, preferences, and patterns. Detail levels: "
        "'summary' for quick overview, 'detailed' for recent decisions and "
        "patterns, 'full' for complete state dump."
    ),
    parameters=_make_params(
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID from start_session",
            },
            "detail_level": {
                "type": "string",
                "enum": ["summary", "detailed", "full"],
                "description": "Level of detail",
                "default": "summary",
            },
        },
        required=["session_id"],
    ),
    handler=_handler_get_session_context,
    category="context",
)


def _handler_create_checkpoint(params: dict[str, Any]) -> dict[str, Any]:
    """Handle create_checkpoint tool call."""
    from kicad_tools.mcp.tools.context import create_checkpoint

    result = create_checkpoint(
        session_id=params["session_id"],
        name=params.get("name"),
        drc_violation_count=params.get("drc_violation_count", 0),
        score=params.get("score", 0.0),
    )
    return result.to_dict()


register_tool(
    name="create_checkpoint",
    description=(
        "Create named checkpoint for potential rollback. Saves current "
        "session state that can be restored later using restore_checkpoint."
    ),
    parameters=_make_params(
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID from start_session",
            },
            "name": {
                "type": "string",
                "description": "Human-readable checkpoint name (optional)",
            },
            "drc_violation_count": {
                "type": "integer",
                "description": "Current DRC violation count",
                "default": 0,
            },
            "score": {
                "type": "number",
                "description": "Current placement score",
                "default": 0.0,
            },
        },
        required=["session_id"],
    ),
    handler=_handler_create_checkpoint,
    category="context",
)


def _handler_restore_checkpoint(params: dict[str, Any]) -> dict[str, Any]:
    """Handle restore_checkpoint tool call."""
    from kicad_tools.mcp.tools.context import restore_checkpoint

    result = restore_checkpoint(
        session_id=params["session_id"],
        checkpoint_id=params["checkpoint_id"],
    )
    return result.to_dict()


register_tool(
    name="restore_checkpoint",
    description=(
        "Restore session to checkpoint state. Retrieves the saved state "
        "associated with a checkpoint for restoration."
    ),
    parameters=_make_params(
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID from start_session",
            },
            "checkpoint_id": {
                "type": "string",
                "description": "ID of the checkpoint to restore",
            },
        },
        required=["session_id", "checkpoint_id"],
    ),
    handler=_handler_restore_checkpoint,
    category="context",
)


def _handler_get_session_summary(params: dict[str, Any]) -> dict[str, Any]:
    """Handle get_session_summary tool call."""
    from kicad_tools.mcp.tools.context import get_session_summary

    result = get_session_summary(
        session_id=params["session_id"],
        max_tokens=params.get("max_tokens", 500),
    )
    return result.to_dict()


register_tool(
    name="get_session_summary",
    description=(
        "Get token-efficient session summary for LLM context. Returns "
        "a compact representation focusing on recent changes and "
        "important decisions, optimized to fit within token budgets."
    ),
    parameters=_make_params(
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID from start_session",
            },
            "max_tokens": {
                "type": "integer",
                "description": "Approximate maximum tokens for summary",
                "default": 500,
            },
        },
        required=["session_id"],
    ),
    handler=_handler_get_session_summary,
    category="context",
)


# -----------------------------------------------------------------------------
# Analysis Tools
# -----------------------------------------------------------------------------


def _handler_measure_clearance(params: dict[str, Any]) -> dict[str, Any]:
    """Handle measure_clearance tool call."""
    from kicad_tools.mcp.tools.analysis import measure_clearance

    result = measure_clearance(
        pcb_path=params["pcb_path"],
        item1=params["item1"],
        item2=params.get("item2"),
        layer=params.get("layer"),
    )
    return result.to_dict()


register_tool(
    name="measure_clearance",
    description=(
        "Measure clearance between items on the PCB. Measures the minimum "
        "edge-to-edge clearance between two items (components or nets) on the PCB. "
        "If item2 is not specified, finds the nearest neighbor to item1. "
        "Returns detailed measurements and design rule pass/fail status."
    ),
    parameters=_make_params(
        properties={
            "pcb_path": {
                "type": "string",
                "description": "Path to .kicad_pcb file",
            },
            "item1": {
                "type": "string",
                "description": "Component reference (e.g., 'U1') or net name (e.g., 'GND')",
            },
            "item2": {
                "type": "string",
                "description": "Second item, or omit for nearest neighbor search",
            },
            "layer": {
                "type": "string",
                "description": "Specific layer to check (e.g., 'F.Cu'), or omit for all layers",
            },
        },
        required=["pcb_path", "item1"],
    ),
    handler=_handler_measure_clearance,
    category="analysis",
)


# -----------------------------------------------------------------------------
# Routing Tools
# -----------------------------------------------------------------------------


def _handler_get_unrouted_nets(params: dict[str, Any]) -> dict[str, Any]:
    """Handle get_unrouted_nets tool call."""
    from kicad_tools.mcp.tools.routing import get_unrouted_nets

    result = get_unrouted_nets(
        pcb_path=params["pcb_path"],
        include_partial=params.get("include_partial", True),
    )
    return result.to_dict()


register_tool(
    name="get_unrouted_nets",
    description=(
        "List nets that need routing. Analyzes a PCB file to identify nets "
        "that are unrouted or partially routed. Provides difficulty estimates "
        "and routing recommendations for AI-driven routing workflows."
    ),
    parameters=_make_params(
        properties={
            "pcb_path": {
                "type": "string",
                "description": "Path to .kicad_pcb file",
            },
            "include_partial": {
                "type": "boolean",
                "description": "Include partially routed nets in results",
                "default": True,
            },
        },
        required=["pcb_path"],
    ),
    handler=_handler_get_unrouted_nets,
    category="routing",
)


def _handler_route_net(params: dict[str, Any]) -> dict[str, Any]:
    """Handle route_net tool call."""
    from kicad_tools.mcp.tools.routing import route_net

    result = route_net(
        pcb_path=params["pcb_path"],
        net_name=params["net_name"],
        output_path=params.get("output_path"),
        strategy=params.get("strategy", "auto"),
        layer_preference=params.get("layer_preference"),
    )
    return result.to_dict()


register_tool(
    name="route_net",
    description=(
        "Route a specific net. Attempts to route all unconnected pads on the "
        "specified net using the autorouter. Returns routing details including "
        "success status, trace length, vias used, and suggestions if routing failed."
    ),
    parameters=_make_params(
        properties={
            "pcb_path": {
                "type": "string",
                "description": "Path to .kicad_pcb file",
            },
            "net_name": {
                "type": "string",
                "description": "Name of the net to route (e.g., 'GND', 'SPI_CLK')",
            },
            "output_path": {
                "type": "string",
                "description": "Output file path (optional, overwrites original if not specified)",
            },
            "strategy": {
                "type": "string",
                "description": "Routing strategy",
                "enum": ["auto", "shortest", "avoid_vias"],
                "default": "auto",
            },
            "layer_preference": {
                "type": "string",
                "description": "Preferred layer for routing (e.g., 'F.Cu', 'B.Cu')",
            },
        },
        required=["pcb_path", "net_name"],
    ),
    handler=_handler_route_net,
    category="routing",
)


# -----------------------------------------------------------------------------
# Pattern Tools
# -----------------------------------------------------------------------------


def _handler_validate_pattern(params: dict[str, Any]) -> dict[str, Any]:
    """Handle validate_pattern tool call."""
    from kicad_tools.mcp.tools.patterns import validate_pattern

    return validate_pattern(
        pcb_path=params["pcb_path"],
        pattern_type=params["pattern_type"],
        components=params["components"],
    )


register_tool(
    name="validate_pattern",
    description=(
        "Validate a circuit pattern implementation against design requirements. "
        "Checks placement rules, routing constraints, and component values for "
        "patterns like LDO power supplies, decoupling networks, and buck converters. "
        "Returns violations with severity levels, locations, and fix suggestions."
    ),
    parameters=_make_params(
        properties={
            "pcb_path": {
                "type": "string",
                "description": "Absolute path to .kicad_pcb file",
            },
            "pattern_type": {
                "type": "string",
                "description": "Type of pattern to validate",
                "enum": ["ldo", "decoupling", "buck"],
            },
            "components": {
                "type": "object",
                "description": (
                    "Pattern-specific component references. "
                    "For LDO: {regulator, input_cap, output_caps}. "
                    "For Decoupling: {ic, capacitors}. "
                    "For Buck: {regulator, inductor, input_cap, output_cap, diode}."
                ),
            },
        },
        required=["pcb_path", "pattern_type", "components"],
    ),
    handler=_handler_validate_pattern,
    category="patterns",
)


def _handler_adapt_pattern(params: dict[str, Any]) -> dict[str, Any]:
    """Handle adapt_pattern tool call."""
    from kicad_tools.mcp.tools.patterns import adapt_pattern

    return adapt_pattern(
        pattern_type=params["pattern_type"],
        component_mpn=params["component_mpn"],
        overrides=params.get("overrides"),
    )


register_tool(
    name="adapt_pattern",
    description=(
        "Get adapted parameters for a circuit pattern based on component requirements. "
        "Loads component specifications from the database and generates appropriate "
        "capacitor values, thermal requirements, and other parameters for the pattern."
    ),
    parameters=_make_params(
        properties={
            "pattern_type": {
                "type": "string",
                "description": "Type of pattern (LDO, BuckConverter, Decoupling)",
            },
            "component_mpn": {
                "type": "string",
                "description": "Manufacturer part number of the main component",
            },
            "overrides": {
                "type": "object",
                "description": "Optional parameter overrides",
            },
        },
        required=["pattern_type", "component_mpn"],
    ),
    handler=_handler_adapt_pattern,
    category="patterns",
)


def _handler_get_requirements(params: dict[str, Any]) -> dict[str, Any]:
    """Handle get_component_requirements tool call."""
    from kicad_tools.mcp.tools.patterns import get_requirements

    return get_requirements(params["component_mpn"])


register_tool(
    name="get_component_requirements",
    description=(
        "Get design requirements for a specific component from the database. "
        "Returns specifications like capacitor requirements, thermal needs, "
        "dropout voltage, and application notes."
    ),
    parameters=_make_params(
        properties={
            "component_mpn": {
                "type": "string",
                "description": "Manufacturer part number (e.g., 'AMS1117-3.3')",
            },
        },
        required=["component_mpn"],
    ),
    handler=_handler_get_requirements,
    category="patterns",
)


def _handler_list_components(params: dict[str, Any]) -> dict[str, Any]:
    """Handle list_pattern_components tool call."""
    from kicad_tools.mcp.tools.patterns import list_available_components

    return list_available_components(params.get("component_type"))


register_tool(
    name="list_pattern_components",
    description=(
        "List components available in the pattern database. "
        "Optionally filter by component type (LDO, BuckConverter, IC)."
    ),
    parameters=_make_params(
        properties={
            "component_type": {
                "type": "string",
                "description": "Optional filter by type",
                "enum": ["LDO", "BuckConverter", "IC", "LinearRegulator"],
            },
        },
        required=[],
    ),
    handler=_handler_list_components,
    category="patterns",
)


# -----------------------------------------------------------------------------
# Mistake Detection Tools
# -----------------------------------------------------------------------------


def _handler_detect_mistakes(params: dict[str, Any]) -> dict[str, Any]:
    """Handle detect_mistakes tool call."""
    from kicad_tools.mcp.tools.mistakes import detect_mistakes

    return detect_mistakes(
        pcb_path=params["pcb_path"],
        category=params.get("category"),
        severity=params.get("severity"),
    )


register_tool(
    name="detect_mistakes",
    description=(
        "Detect common PCB design mistakes with educational explanations. "
        "Analyzes a PCB file and identifies issues like bypass capacitor "
        "placement, crystal trace length, differential pair skew, power "
        "trace width, thermal pad connections, via-in-pad, acid traps, "
        "and tombstoning risks. Each mistake includes an explanation "
        "of why it's a problem and how to fix it."
    ),
    parameters=_make_params(
        properties={
            "pcb_path": {
                "type": "string",
                "description": "Path to .kicad_pcb file to analyze",
            },
            "category": {
                "type": "string",
                "description": "Only check specific category",
                "enum": [
                    "bypass_capacitor",
                    "crystal_oscillator",
                    "differential_pair",
                    "power_trace",
                    "thermal_management",
                    "via_placement",
                    "manufacturability",
                ],
            },
            "severity": {
                "type": "string",
                "description": "Only show issues of this severity or higher",
                "enum": ["error", "warning", "info"],
            },
        },
        required=["pcb_path"],
    ),
    handler=_handler_detect_mistakes,
    category="mistakes",
)


def _handler_list_mistake_categories(params: dict[str, Any]) -> dict[str, Any]:
    """Handle list_mistake_categories tool call."""
    from kicad_tools.mcp.tools.mistakes import list_mistake_categories

    return list_mistake_categories()


register_tool(
    name="list_mistake_categories",
    description=(
        "List all available mistake detection categories. "
        "Returns information about each category of design mistakes "
        "that can be detected, along with the number of checks."
    ),
    parameters=_make_params(
        properties={},
        required=[],
    ),
    handler=_handler_list_mistake_categories,
    category="mistakes",
)
