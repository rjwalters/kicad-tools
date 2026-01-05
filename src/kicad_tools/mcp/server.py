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

from kicad_tools.mcp.tools.export import export_assembly, export_gerbers
from kicad_tools.mcp.tools.placement import placement_analyze

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
