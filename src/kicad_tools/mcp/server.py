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
from typing import Any, Callable

from kicad_tools.mcp.tools.analysis import measure_clearance
from kicad_tools.mcp.tools.export import export_assembly, export_bom, export_gerbers
from kicad_tools.mcp.tools.placement import placement_analyze
from kicad_tools.mcp.tools.routing import get_unrouted_nets, route_net
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
        self._register_clearance_tools()
        self._register_routing_tools()

    def _register_export_tools(self) -> None:
        """Register export-related tools."""
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


def main() -> None:
    """Entry point for MCP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )
    server = create_server()
    server.run()


if __name__ == "__main__":
    main()
