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
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

from kicad_tools.mcp.tools.analysis import measure_clearance
from kicad_tools.mcp.tools.context import (
    annotate_decision,
    create_checkpoint,
    get_decision_history,
    get_session_context,
    get_session_summary,
    record_decision,
    restore_checkpoint,
)
from kicad_tools.mcp.tools.export import (
    export_assembly,
    export_bom,
    export_gerbers,
    validate_assembly_bom,
)
from kicad_tools.mcp.tools.mistakes import (
    detect_mistakes,
    list_mistake_categories,
)
from kicad_tools.mcp.tools.placement import placement_analyze
from kicad_tools.mcp.tools.routing import get_unrouted_nets, route_net
from kicad_tools.mcp.tools.patterns import (
    adapt_pattern,
    get_requirements,
    list_available_components,
    validate_pattern,
)
from kicad_tools.mcp.tools.session import (
    apply_move,
    commit_session,
    query_move,
    rollback_session,
    start_session,
    undo_move,
)

logger = logging.getLogger(__name__)


@dataclass
class ToolDefinition:
    """Definition of an MCP tool."""

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
        """Register default tools."""
        self._register_export_tools()
        self._register_assembly_tools()
        self._register_placement_tools()
        self._register_session_tools()
        self._register_context_tools()
        self._register_clearance_tools()
        self._register_routing_tools()
        self._register_pattern_tools()
        self._register_mistake_tools()

    def _register_export_tools(self) -> None:
        """Register export-related tools."""
        self.tools["validate_assembly_bom"] = ToolDefinition(
            name="validate_assembly_bom",
            description=(
                "Validate a BOM for JLCPCB assembly. Checks all components against the "
                "LCSC/JLCPCB parts library and categorizes them by tier (Basic/Extended), "
                "stock status, and availability. Returns a summary of parts that are "
                "available, out of stock, or missing LCSC part numbers. Use this to "
                "verify assembly readiness before ordering."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["schematic_path"],
            },
            handler=self._handle_validate_assembly_bom,
        )

        self.tools["export_gerbers"] = ToolDefinition(
            name="export_gerbers",
            description=(
                "Export Gerber files for PCB manufacturing. Generates all required "
                "Gerber layers (copper, soldermask, silkscreen, outline) and optionally "
                "drill files. Supports manufacturer presets for JLCPCB, OSHPark, PCBWay, and Seeed."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["pcb_path", "output_dir"],
            },
            handler=self._handle_export_gerbers,
        )

        self.tools["export_bom"] = ToolDefinition(
            name="export_bom",
            description=(
                "Export Bill of Materials (BOM) from a KiCad schematic file. "
                "Generates a component list with quantities, values, footprints, and "
                "part numbers. Supports multiple output formats including CSV, JSON, "
                "and manufacturer-specific formats (JLCPCB, PCBWay, Seeed). "
                "Automatically extracts LCSC part numbers from component fields."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["schematic_path"],
            },
            handler=self._handle_export_bom,
        )

    def _handle_validate_assembly_bom(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle validate_assembly_bom tool call."""
        return validate_assembly_bom(
            schematic_path=params["schematic_path"],
            quantity=params.get("quantity", 1),
        )

    def _handle_export_bom(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle export_bom tool call."""
        result = export_bom(
            schematic_path=params["schematic_path"],
            output_path=params.get("output_path"),
            format=params.get("format", "csv"),
            group_by=params.get("group_by", "value+footprint"),
            include_dnp=params.get("include_dnp", False),
        )
        return result.to_dict()

    def _handle_export_gerbers(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle export_gerbers tool call."""
        result = export_gerbers(
            pcb_path=params["pcb_path"],
            output_dir=params["output_dir"],
            manufacturer=params.get("manufacturer", "generic"),
            include_drill=params.get("include_drill", True),
            zip_output=params.get("zip_output", True),
        )
        return result.to_dict()

    def _register_assembly_tools(self) -> None:
        """Register assembly-related tools."""
        self.tools["export_assembly"] = ToolDefinition(
            name="export_assembly",
            description=(
                "Generate complete assembly package for manufacturing. Creates Gerber files, "
                "bill of materials (BOM), and pick-and-place (PnP/CPL) files tailored to "
                "specific manufacturers. Outputs a single zip file ready for upload to "
                "JLCPCB, PCBWay, Seeed, or generic assembly services."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["pcb_path", "schematic_path", "output_dir"],
            },
            handler=self._handle_export_assembly,
        )

    def _handle_export_assembly(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle export_assembly tool call."""
        result = export_assembly(
            pcb_path=params["pcb_path"],
            schematic_path=params["schematic_path"],
            output_dir=params["output_dir"],
            manufacturer=params.get("manufacturer", "jlcpcb"),
        )
        return result.to_dict()

    def _register_placement_tools(self) -> None:
        """Register placement analysis tools."""
        self.tools["placement_analyze"] = ToolDefinition(
            name="placement_analyze",
            description=(
                "Analyze current component placement quality. Evaluates placement with "
                "metrics for wire length, congestion, thermal characteristics, signal "
                "integrity, and manufacturing concerns. Returns an overall score, "
                "category scores, identified issues with suggestions, detected functional "
                "clusters, and routing difficulty estimates."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["pcb_path"],
            },
            handler=self._handle_placement_analyze,
        )

    def _handle_placement_analyze(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle placement_analyze tool call."""
        result = placement_analyze(
            pcb_path=params["pcb_path"],
            check_thermal=params.get("check_thermal", True),
            check_signal_integrity=params.get("check_signal_integrity", True),
            check_manufacturing=params.get("check_manufacturing", True),
        )
        return result.to_dict()

    def _register_session_tools(self) -> None:
        """Register session management tools for placement refinement."""
        self.tools["start_session"] = ToolDefinition(
            name="start_session",
            description=(
                "Start a new placement refinement session. Creates a stateful session "
                "for interactively refining component placement through query-before-commit "
                "operations. Returns a session ID used for subsequent operations."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["pcb_path"],
            },
            handler=self._handle_start_session,
        )

        self.tools["query_move"] = ToolDefinition(
            name="query_move",
            description=(
                "Query the impact of a hypothetical component move without applying it. "
                "Returns score changes, new/resolved violations, and routing impact. "
                "Use this to evaluate moves before applying them."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["session_id", "ref", "x", "y"],
            },
            handler=self._handle_query_move,
        )

        self.tools["apply_move"] = ToolDefinition(
            name="apply_move",
            description=(
                "Apply a component move within the session. The move can be undone with "
                "undo_move and is not written to disk until commit_session is called. "
                "Returns updated component position and score delta."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["session_id", "ref", "x", "y"],
            },
            handler=self._handle_apply_move,
        )

        self.tools["undo_move"] = ToolDefinition(
            name="undo_move",
            description=(
                "Undo the last applied move in the session. Restores the component "
                "to its previous position. Can be called multiple times to undo "
                "multiple moves."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID from start_session",
                    },
                },
                "required": ["session_id"],
            },
            handler=self._handle_undo_move,
        )

        self.tools["commit_session"] = ToolDefinition(
            name="commit_session",
            description=(
                "Commit all pending moves to the PCB file and close the session. "
                "Writes changes to disk. Optionally specify output_path to save to "
                "a different file instead of overwriting the original."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID from start_session",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Output file path (optional, overwrites original if not specified)",
                    },
                },
                "required": ["session_id"],
            },
            handler=self._handle_commit_session,
        )

        self.tools["rollback_session"] = ToolDefinition(
            name="rollback_session",
            description=(
                "Discard all pending moves and close the session. No changes are "
                "written to disk. Use this to abandon a session without saving."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID from start_session",
                    },
                },
                "required": ["session_id"],
            },
            handler=self._handle_rollback_session,
        )

    def _handle_start_session(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle start_session tool call."""
        result = start_session(
            pcb_path=params["pcb_path"],
            fixed_refs=params.get("fixed_refs"),
        )
        return result.to_dict()

    def _handle_query_move(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle query_move tool call."""
        result = query_move(
            session_id=params["session_id"],
            ref=params["ref"],
            x=params["x"],
            y=params["y"],
            rotation=params.get("rotation"),
        )
        return result.to_dict()

    def _handle_apply_move(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle apply_move tool call."""
        result = apply_move(
            session_id=params["session_id"],
            ref=params["ref"],
            x=params["x"],
            y=params["y"],
            rotation=params.get("rotation"),
        )
        return result.to_dict()

    def _handle_undo_move(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle undo_move tool call."""
        result = undo_move(session_id=params["session_id"])
        return result.to_dict()

    def _handle_commit_session(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle commit_session tool call."""
        result = commit_session(
            session_id=params["session_id"],
            output_path=params.get("output_path"),
        )
        return result.to_dict()

    def _handle_rollback_session(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle rollback_session tool call."""
        result = rollback_session(session_id=params["session_id"])
        return result.to_dict()

    def _register_context_tools(self) -> None:
        """Register context persistence and decision tracking tools."""
        self.tools["record_decision"] = ToolDefinition(
            name="record_decision",
            description=(
                "Record a design decision with rationale. Creates a permanent record "
                "of a design decision, including reasoning and alternatives considered. "
                "Use this to build a decision history for learning and context."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["session_id", "action", "target"],
            },
            handler=self._handle_record_decision,
        )

        self.tools["get_decision_history"] = ToolDefinition(
            name="get_decision_history",
            description=(
                "Get recent decisions in session. Returns decision history with "
                "detected patterns. Optionally filter by action type."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["session_id"],
            },
            handler=self._handle_get_decision_history,
        )

        self.tools["annotate_decision"] = ToolDefinition(
            name="annotate_decision",
            description=(
                "Add feedback to a past decision. Updates the decision record "
                "with feedback and optionally changes its outcome status."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["session_id", "decision_id", "feedback"],
            },
            handler=self._handle_annotate_decision,
        )

        self.tools["get_session_context"] = ToolDefinition(
            name="get_session_context",
            description=(
                "Get session context at specified detail level. Returns session "
                "state including decisions, preferences, and patterns. Detail levels: "
                "'summary' for quick overview, 'detailed' for recent decisions and "
                "patterns, 'full' for complete state dump."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["session_id"],
            },
            handler=self._handle_get_session_context,
        )

        self.tools["create_checkpoint"] = ToolDefinition(
            name="create_checkpoint",
            description=(
                "Create named checkpoint for potential rollback. Saves current "
                "session state that can be restored later using restore_checkpoint."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["session_id"],
            },
            handler=self._handle_create_checkpoint,
        )

        self.tools["restore_checkpoint"] = ToolDefinition(
            name="restore_checkpoint",
            description=(
                "Restore session to checkpoint state. Retrieves the saved state "
                "associated with a checkpoint for restoration."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID from start_session",
                    },
                    "checkpoint_id": {
                        "type": "string",
                        "description": "ID of the checkpoint to restore",
                    },
                },
                "required": ["session_id", "checkpoint_id"],
            },
            handler=self._handle_restore_checkpoint,
        )

        self.tools["get_session_summary"] = ToolDefinition(
            name="get_session_summary",
            description=(
                "Get token-efficient session summary for LLM context. Returns "
                "a compact representation focusing on recent changes and "
                "important decisions, optimized to fit within token budgets."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["session_id"],
            },
            handler=self._handle_get_session_summary,
        )

    def _handle_record_decision(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle record_decision tool call."""
        result = record_decision(
            session_id=params["session_id"],
            action=params["action"],
            target=params["target"],
            rationale=params.get("rationale"),
            alternatives=params.get("alternatives"),
            confidence=params.get("confidence", 0.8),
        )
        return result.to_dict()

    def _handle_get_decision_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle get_decision_history tool call."""
        result = get_decision_history(
            session_id=params["session_id"],
            limit=params.get("limit", 20),
            filter_action=params.get("filter_action"),
        )
        return result.to_dict()

    def _handle_annotate_decision(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle annotate_decision tool call."""
        result = annotate_decision(
            session_id=params["session_id"],
            decision_id=params["decision_id"],
            feedback=params["feedback"],
            outcome=params.get("outcome"),
        )
        return result.to_dict()

    def _handle_get_session_context(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle get_session_context tool call."""
        result = get_session_context(
            session_id=params["session_id"],
            detail_level=params.get("detail_level", "summary"),
        )
        return result.to_dict()

    def _handle_create_checkpoint(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle create_checkpoint tool call."""
        result = create_checkpoint(
            session_id=params["session_id"],
            name=params.get("name"),
            drc_violation_count=params.get("drc_violation_count", 0),
            score=params.get("score", 0.0),
        )
        return result.to_dict()

    def _handle_restore_checkpoint(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle restore_checkpoint tool call."""
        result = restore_checkpoint(
            session_id=params["session_id"],
            checkpoint_id=params["checkpoint_id"],
        )
        return result.to_dict()

    def _handle_get_session_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle get_session_summary tool call."""
        result = get_session_summary(
            session_id=params["session_id"],
            max_tokens=params.get("max_tokens", 500),
        )
        return result.to_dict()

    def _register_clearance_tools(self) -> None:
        """Register clearance measurement tools."""
        self.tools["measure_clearance"] = ToolDefinition(
            name="measure_clearance",
            description=(
                "Measure clearance between items on the PCB. Measures the minimum "
                "edge-to-edge clearance between two items (components or nets) on the PCB. "
                "If item2 is not specified, finds the nearest neighbor to item1. "
                "Returns detailed measurements and design rule pass/fail status."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["pcb_path", "item1"],
            },
            handler=self._handle_measure_clearance,
        )

    def _handle_measure_clearance(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle measure_clearance tool call."""
        result = measure_clearance(
            pcb_path=params["pcb_path"],
            item1=params["item1"],
            item2=params.get("item2"),
            layer=params.get("layer"),
        )
        return result.to_dict()

    def _register_routing_tools(self) -> None:
        """Register routing-related tools."""
        self.tools["get_unrouted_nets"] = ToolDefinition(
            name="get_unrouted_nets",
            description=(
                "List nets that need routing. Analyzes a PCB file to identify nets "
                "that are unrouted or partially routed. Provides difficulty estimates "
                "and routing recommendations for AI-driven routing workflows."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["pcb_path"],
            },
            handler=self._handle_get_unrouted_nets,
        )

        self.tools["route_net"] = ToolDefinition(
            name="route_net",
            description=(
                "Route a specific net. Attempts to route all unconnected pads on the "
                "specified net using the autorouter. Returns routing details including "
                "success status, trace length, vias used, and suggestions if routing failed."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["pcb_path", "net_name"],
            },
            handler=self._handle_route_net,
        )

    def _handle_get_unrouted_nets(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle get_unrouted_nets tool call."""
        result = get_unrouted_nets(
            pcb_path=params["pcb_path"],
            include_partial=params.get("include_partial", True),
        )
        return result.to_dict()

    def _handle_route_net(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle route_net tool call."""
        result = route_net(
            pcb_path=params["pcb_path"],
            net_name=params["net_name"],
            output_path=params.get("output_path"),
            strategy=params.get("strategy", "auto"),
            layer_preference=params.get("layer_preference"),
        )
        return result.to_dict()

    def _register_pattern_tools(self) -> None:
        """Register pattern validation and adaptation tools."""
        self.tools["validate_pattern"] = ToolDefinition(
            name="validate_pattern",
            description=(
                "Validate a circuit pattern implementation against design requirements. "
                "Checks placement rules, routing constraints, and component values for "
                "patterns like LDO power supplies, decoupling networks, and buck converters. "
                "Returns violations with severity levels, locations, and fix suggestions."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["pcb_path", "pattern_type", "components"],
            },
            handler=self._handle_validate_pattern,
        )

        self.tools["adapt_pattern"] = ToolDefinition(
            name="adapt_pattern",
            description=(
                "Get adapted parameters for a circuit pattern based on component requirements. "
                "Loads component specifications from the database and generates appropriate "
                "capacitor values, thermal requirements, and other parameters for the pattern."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["pattern_type", "component_mpn"],
            },
            handler=self._handle_adapt_pattern,
        )

        self.tools["get_component_requirements"] = ToolDefinition(
            name="get_component_requirements",
            description=(
                "Get design requirements for a specific component from the database. "
                "Returns specifications like capacitor requirements, thermal needs, "
                "dropout voltage, and application notes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "component_mpn": {
                        "type": "string",
                        "description": "Manufacturer part number (e.g., 'AMS1117-3.3')",
                    },
                },
                "required": ["component_mpn"],
            },
            handler=self._handle_get_requirements,
        )

        self.tools["list_pattern_components"] = ToolDefinition(
            name="list_pattern_components",
            description=(
                "List components available in the pattern database. "
                "Optionally filter by component type (LDO, BuckConverter, IC)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "component_type": {
                        "type": "string",
                        "description": "Optional filter by type",
                        "enum": ["LDO", "BuckConverter", "IC", "LinearRegulator"],
                    },
                },
                "required": [],
            },
            handler=self._handle_list_components,
        )

    def _handle_validate_pattern(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle validate_pattern tool call."""
        return validate_pattern(
            pcb_path=params["pcb_path"],
            pattern_type=params["pattern_type"],
            components=params["components"],
        )

    def _handle_adapt_pattern(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle adapt_pattern tool call."""
        return adapt_pattern(
            pattern_type=params["pattern_type"],
            component_mpn=params["component_mpn"],
            overrides=params.get("overrides"),
        )

    def _handle_get_requirements(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle get_component_requirements tool call."""
        return get_requirements(params["component_mpn"])

    def _handle_list_components(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle list_pattern_components tool call."""
        return list_available_components(params.get("component_type"))

    def _register_mistake_tools(self) -> None:
        """Register PCB design mistake detection tools."""
        self.tools["detect_mistakes"] = ToolDefinition(
            name="detect_mistakes",
            description=(
                "Detect common PCB design mistakes with educational explanations. "
                "Analyzes a PCB file and identifies issues like bypass capacitor "
                "placement, crystal trace length, differential pair skew, power "
                "trace width, thermal pad connections, via-in-pad, acid traps, "
                "and tombstoning risks. Each mistake includes an explanation "
                "of why it's a problem and how to fix it."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "required": ["pcb_path"],
            },
            handler=self._handle_detect_mistakes,
        )

        self.tools["list_mistake_categories"] = ToolDefinition(
            name="list_mistake_categories",
            description=(
                "List all available mistake detection categories. "
                "Returns information about each category of design mistakes "
                "that can be detected, along with the number of checks."
            ),
            parameters={
                "type": "object",
                "properties": {},
            },
            handler=self._handle_list_mistake_categories,
        )

    def _handle_detect_mistakes(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle detect_mistakes tool call."""
        return detect_mistakes(
            pcb_path=params["pcb_path"],
            category=params.get("category"),
            severity=params.get("severity"),
        )

    def _handle_list_mistake_categories(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle list_mistake_categories tool call."""
        return list_mistake_categories()

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
        if name not in self.tools:
            raise ValueError(f"Unknown tool: {name}")

        tool = self.tools[name]
        return tool.handler(arguments)

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


def create_fastmcp_server(http_mode: bool = False) -> FastMCP:
    """Create a FastMCP server with all tools registered.

    Args:
        http_mode: If True, creates server in stateless HTTP mode.

    Returns:
        Configured FastMCP server instance.

    Raises:
        ImportError: If fastmcp is not installed.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise ImportError(
            "FastMCP is required for HTTP transport. Install with: pip install 'kicad-tools[mcp]'"
        ) from e

    mcp = FastMCP("kicad-tools", stateless_http=http_mode)

    # Register export tools
    @mcp.tool()
    def export_gerbers(
        pcb_path: str,
        output_dir: str,
        manufacturer: str = "generic",
        include_drill: bool = True,
        zip_output: bool = True,
    ) -> dict:
        """Export Gerber files for PCB manufacturing.

        Generates all required Gerber layers (copper, soldermask, silkscreen, outline)
        and optionally drill files. Supports manufacturer presets for JLCPCB, OSHPark,
        PCBWay, and Seeed.

        Args:
            pcb_path: Path to .kicad_pcb file
            output_dir: Directory for output files
            manufacturer: Manufacturer preset (generic, jlcpcb, pcbway, oshpark, seeed)
            include_drill: Include drill files (Excellon format)
            zip_output: Create zip archive of all files

        Returns:
            Export result with file paths and status.
        """
        from kicad_tools.mcp.tools.export import export_gerbers as _export_gerbers

        result = _export_gerbers(
            pcb_path=pcb_path,
            output_dir=output_dir,
            manufacturer=manufacturer,
            include_drill=include_drill,
            zip_output=zip_output,
        )
        return result.to_dict()

    @mcp.tool()
    def export_bom(
        schematic_path: str,
        output_path: str | None = None,
        format: str = "csv",
        group_by: str = "value+footprint",
        include_dnp: bool = False,
    ) -> dict:
        """Export Bill of Materials (BOM) from a KiCad schematic file.

        Generates a component list with quantities, values, footprints, and
        part numbers. Supports multiple output formats including CSV, JSON,
        and manufacturer-specific formats (JLCPCB, PCBWay, Seeed).

        Args:
            schematic_path: Path to .kicad_sch file
            output_path: Output file path (optional)
            format: Output format (csv, json, jlcpcb, pcbway, seeed)
            group_by: Component grouping strategy
            include_dnp: Include Do Not Place components

        Returns:
            BOM export result with components and file path.
        """
        from kicad_tools.mcp.tools.export import export_bom as _export_bom

        result = _export_bom(
            schematic_path=schematic_path,
            output_path=output_path,
            format=format,
            group_by=group_by,
            include_dnp=include_dnp,
        )
        return result.to_dict()

    @mcp.tool()
    def export_assembly(
        pcb_path: str,
        schematic_path: str,
        output_dir: str,
        manufacturer: str = "jlcpcb",
    ) -> dict:
        """Generate complete assembly package for manufacturing.

        Creates Gerber files, bill of materials (BOM), and pick-and-place (PnP/CPL)
        files tailored to specific manufacturers.

        Args:
            pcb_path: Path to .kicad_pcb file
            schematic_path: Path to .kicad_sch file
            output_dir: Directory for output files
            manufacturer: Target manufacturer (jlcpcb, pcbway, seeed, generic)

        Returns:
            Assembly export result with file paths.
        """
        from kicad_tools.mcp.tools.export import export_assembly as _export_assembly

        result = _export_assembly(
            pcb_path=pcb_path,
            schematic_path=schematic_path,
            output_dir=output_dir,
            manufacturer=manufacturer,
        )
        return result.to_dict()

    # Register placement tools
    @mcp.tool()
    def placement_analyze(
        pcb_path: str,
        check_thermal: bool = True,
        check_signal_integrity: bool = True,
        check_manufacturing: bool = True,
    ) -> dict:
        """Analyze current component placement quality.

        Evaluates placement with metrics for wire length, congestion, thermal
        characteristics, signal integrity, and manufacturing concerns.

        Args:
            pcb_path: Path to .kicad_pcb file
            check_thermal: Include thermal analysis
            check_signal_integrity: Include signal integrity hints
            check_manufacturing: Include DFM checks

        Returns:
            Analysis result with scores and suggestions.
        """
        from kicad_tools.mcp.tools.placement import (
            placement_analyze as _placement_analyze,
        )

        result = _placement_analyze(
            pcb_path=pcb_path,
            check_thermal=check_thermal,
            check_signal_integrity=check_signal_integrity,
            check_manufacturing=check_manufacturing,
        )
        return result.to_dict()

    @mcp.tool()
    def placement_suggestions(
        pcb_path: str,
        component: str | None = None,
        max_suggestions: int = 10,
        strategy: str = "balanced",
    ) -> dict:
        """Get AI-friendly placement improvement suggestions for a PCB.

        Analyzes component placement and returns actionable recommendations
        ranked by priority.

        Args:
            pcb_path: Path to .kicad_pcb file
            component: Specific component reference to analyze (optional)
            max_suggestions: Maximum number of suggestions (1-50)
            strategy: Optimization strategy (balanced, wire_length, thermal, si)

        Returns:
            Suggestions with rankings and expected improvements.
        """
        from kicad_tools.mcp.tools.placement import (
            placement_suggestions as _placement_suggestions,
        )

        result = _placement_suggestions(
            pcb_path=pcb_path,
            component=component,
            max_suggestions=max_suggestions,
            strategy=strategy,
        )
        return result.to_dict()

    # Register session tools
    @mcp.tool()
    def start_session(
        pcb_path: str,
        fixed_refs: list[str] | None = None,
    ) -> dict:
        """Start a new placement refinement session.

        Creates a stateful session for interactively refining component placement
        through query-before-commit operations.

        Args:
            pcb_path: Absolute path to .kicad_pcb file
            fixed_refs: Component references to keep fixed (optional)

        Returns:
            Session info with session_id for subsequent operations.
        """
        from kicad_tools.mcp.tools.session import start_session as _start_session

        result = _start_session(
            pcb_path=pcb_path,
            fixed_refs=fixed_refs,
        )
        return result.to_dict()

    @mcp.tool()
    def query_move(
        session_id: str,
        ref: str,
        x: float,
        y: float,
        rotation: float | None = None,
    ) -> dict:
        """Query the impact of a hypothetical component move without applying it.

        Returns score changes, new/resolved violations, and routing impact.
        Use this to evaluate moves before applying them.

        Args:
            session_id: Session ID from start_session
            ref: Component reference designator (e.g., 'C1', 'R5')
            x: Target X position in millimeters
            y: Target Y position in millimeters
            rotation: Target rotation in degrees (optional)

        Returns:
            Move impact analysis with score delta and violations.
        """
        from kicad_tools.mcp.tools.session import query_move as _query_move

        result = _query_move(
            session_id=session_id,
            ref=ref,
            x=x,
            y=y,
            rotation=rotation,
        )
        return result.to_dict()

    @mcp.tool()
    def apply_move(
        session_id: str,
        ref: str,
        x: float,
        y: float,
        rotation: float | None = None,
    ) -> dict:
        """Apply a component move within the session.

        The move can be undone with undo_move and is not written to disk
        until commit_session is called.

        Args:
            session_id: Session ID from start_session
            ref: Component reference designator
            x: New X position in millimeters
            y: New Y position in millimeters
            rotation: New rotation in degrees (optional)

        Returns:
            Updated component position and score delta.
        """
        from kicad_tools.mcp.tools.session import apply_move as _apply_move

        result = _apply_move(
            session_id=session_id,
            ref=ref,
            x=x,
            y=y,
            rotation=rotation,
        )
        return result.to_dict()

    @mcp.tool()
    def undo_move(session_id: str) -> dict:
        """Undo the last applied move in the session.

        Restores the component to its previous position.

        Args:
            session_id: Session ID from start_session

        Returns:
            Undo result with restored position.
        """
        from kicad_tools.mcp.tools.session import undo_move as _undo_move

        result = _undo_move(session_id=session_id)
        return result.to_dict()

    @mcp.tool()
    def commit_session(
        session_id: str,
        output_path: str | None = None,
    ) -> dict:
        """Commit all pending moves to the PCB file and close the session.

        Writes changes to disk. Optionally specify output_path to save to
        a different file instead of overwriting the original.

        Args:
            session_id: Session ID from start_session
            output_path: Output file path (optional)

        Returns:
            Commit result with file path and statistics.
        """
        from kicad_tools.mcp.tools.session import commit_session as _commit_session

        result = _commit_session(
            session_id=session_id,
            output_path=output_path,
        )
        return result.to_dict()

    @mcp.tool()
    def rollback_session(session_id: str) -> dict:
        """Discard all pending moves and close the session.

        No changes are written to disk.

        Args:
            session_id: Session ID from start_session

        Returns:
            Rollback confirmation.
        """
        from kicad_tools.mcp.tools.session import rollback_session as _rollback_session

        result = _rollback_session(session_id=session_id)
        return result.to_dict()

    # Register clearance tool
    @mcp.tool()
    def measure_clearance(
        pcb_path: str,
        item1: str,
        item2: str | None = None,
        layer: str | None = None,
    ) -> dict:
        """Measure clearance between items on the PCB.

        Measures the minimum edge-to-edge clearance between two items
        (components or nets) on the PCB. If item2 is not specified,
        finds the nearest neighbor to item1.

        Args:
            pcb_path: Path to .kicad_pcb file
            item1: Component reference (e.g., 'U1') or net name (e.g., 'GND')
            item2: Second item, or omit for nearest neighbor search
            layer: Specific layer to check (e.g., 'F.Cu'), or omit for all

        Returns:
            Clearance measurement with distance and pass/fail status.
        """
        from kicad_tools.mcp.tools.analysis import measure_clearance as _measure_clearance

        result = _measure_clearance(
            pcb_path=pcb_path,
            item1=item1,
            item2=item2,
            layer=layer,
        )
        return result.to_dict()

    return mcp


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
        # Use FastMCP for HTTP transport
        mcp = create_fastmcp_server(http_mode=True)
        logger.info(f"Starting HTTP MCP server on {host}:{port}")
        mcp.run(transport="streamable-http", host=host, port=port)
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
