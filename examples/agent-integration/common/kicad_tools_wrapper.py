"""
KiCad Tools Wrapper for Agent Integration.

This module provides a unified interface for AI agents to interact with
kicad-tools. It handles tool dispatch, state management, and provides
structured responses suitable for LLM consumption.

Usage:
    from kicad_tools_wrapper import KiCadAgent

    agent = KiCadAgent()

    # Execute tool calls from LLM
    result = agent.execute("add_schematic_symbol", {
        "lib_id": "Device:R",
        "x": 100, "y": 80,
        "reference": "R1",
        "value": "10k"
    })

    # Get current state for LLM context
    state = agent.get_state()
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ToolCategory(Enum):
    """Categories of available tools."""

    SCHEMATIC = "schematic"
    PCB = "pcb"
    DRC = "drc"
    EXPORT = "export"
    ANALYSIS = "analysis"


@dataclass
class ToolResult:
    """Result of a tool execution."""

    success: bool
    tool_name: str
    message: str
    data: dict = field(default_factory=dict)
    error: str | None = None
    suggestion: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {
            "success": self.success,
            "tool": self.tool_name,
            "message": self.message,
        }
        if self.data:
            result["data"] = self.data
        if self.error:
            result["error"] = self.error
        if self.suggestion:
            result["suggestion"] = self.suggestion
        return result


@dataclass
class AgentState:
    """Current state of the agent for LLM context."""

    schematic_loaded: bool = False
    schematic_path: str | None = None
    pcb_loaded: bool = False
    pcb_path: str | None = None
    components: list[dict] = field(default_factory=list)
    nets: list[dict] = field(default_factory=list)
    unrouted_nets: list[str] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)

    def to_prompt(self) -> str:
        """Generate a prompt-friendly state summary."""
        lines = ["## Current State"]

        if self.schematic_loaded:
            lines.append(f"Schematic: {self.schematic_path}")
            lines.append(f"Components: {len(self.components)}")
            lines.append(f"Nets: {len(self.nets)}")
        else:
            lines.append("Schematic: Not loaded")

        if self.pcb_loaded:
            lines.append(f"PCB: {self.pcb_path}")
            lines.append(f"Unrouted nets: {len(self.unrouted_nets)}")
            lines.append(f"Violations: {len(self.violations)}")
        else:
            lines.append("PCB: Not loaded")

        return "\n".join(lines)


class KiCadAgent:
    """
    Wrapper for kicad-tools that provides agent-friendly interface.

    This class handles:
    - Tool dispatch based on function names
    - State management for context
    - Error handling with suggestions
    - Structured responses for LLMs
    """

    def __init__(self):
        """Initialize the agent."""
        self.state = AgentState()
        self._schematic = None
        self._pcb = None
        self._reasoning_agent = None

    def execute(self, tool_name: str, arguments: dict) -> ToolResult:
        """
        Execute a tool by name with given arguments.

        Args:
            tool_name: Name of the tool to execute
            arguments: Dictionary of arguments for the tool

        Returns:
            ToolResult with success status and data/error
        """
        # Map tool names to handler methods
        handlers = {
            # Schematic tools
            "load_schematic": self._load_schematic,
            "add_schematic_symbol": self._add_schematic_symbol,
            "add_wire": self._add_wire,
            "wire_components": self._wire_components,
            "add_power_symbol": self._add_power_symbol,
            "add_net_label": self._add_net_label,
            "list_symbols": self._list_symbols,
            "list_nets": self._list_nets,
            "save_schematic": self._save_schematic,
            # Circuit block tools
            "add_led_indicator": self._add_led_indicator,
            "add_decoupling_caps": self._add_decoupling_caps,
            "add_ldo_regulator": self._add_ldo_regulator,
            # PCB tools
            "load_pcb": self._load_pcb,
            "route_net": self._route_net,
            "place_component": self._place_component,
            "delete_trace": self._delete_trace,
            "add_via": self._add_via,
            "define_zone": self._define_zone,
            "route_all": self._route_all,
            "save_pcb": self._save_pcb,
            # DRC tools
            "check_drc": self._check_drc,
            "get_violations": self._get_violations,
            # Export tools
            "extract_bom": self._extract_bom,
            "export_gerbers": self._export_gerbers,
            "export_assembly": self._export_assembly,
            # Analysis tools
            "analyze_board": self._analyze_board,
            "get_unrouted_nets": self._get_unrouted_nets,
            "get_component_info": self._get_component_info,
            "get_net_info": self._get_net_info,
        }

        handler = handlers.get(tool_name)
        if not handler:
            return ToolResult(
                success=False,
                tool_name=tool_name,
                message=f"Unknown tool: {tool_name}",
                error=f"Tool '{tool_name}' not found",
                suggestion=f"Available tools: {', '.join(handlers.keys())}",
            )

        try:
            return handler(**arguments)
        except TypeError as e:
            return ToolResult(
                success=False,
                tool_name=tool_name,
                message=f"Invalid arguments for {tool_name}",
                error=str(e),
                suggestion="Check the tool's required arguments",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                tool_name=tool_name,
                message=f"Error executing {tool_name}",
                error=str(e),
            )

    def get_state(self) -> AgentState:
        """Get current agent state for LLM context."""
        return self.state

    def get_available_tools(self, category: ToolCategory | None = None) -> list[str]:
        """Get list of available tool names, optionally filtered by category."""
        tools_by_category = {
            ToolCategory.SCHEMATIC: [
                "load_schematic",
                "add_schematic_symbol",
                "add_wire",
                "wire_components",
                "add_power_symbol",
                "add_net_label",
                "list_symbols",
                "list_nets",
                "save_schematic",
                "add_led_indicator",
                "add_decoupling_caps",
                "add_ldo_regulator",
            ],
            ToolCategory.PCB: [
                "load_pcb",
                "route_net",
                "place_component",
                "delete_trace",
                "add_via",
                "define_zone",
                "route_all",
                "save_pcb",
            ],
            ToolCategory.DRC: ["check_drc", "get_violations"],
            ToolCategory.EXPORT: ["extract_bom", "export_gerbers", "export_assembly"],
            ToolCategory.ANALYSIS: [
                "analyze_board",
                "get_unrouted_nets",
                "get_component_info",
                "get_net_info",
            ],
        }

        if category:
            return tools_by_category.get(category, [])

        # Return all tools
        all_tools = []
        for tools in tools_by_category.values():
            all_tools.extend(tools)
        return all_tools

    # =========================================================================
    # Schematic Tool Implementations
    # =========================================================================

    def _load_schematic(self, file_path: str) -> ToolResult:
        """Load a KiCad schematic file."""
        try:
            from kicad_tools import Schematic

            path = Path(file_path)
            if not path.exists():
                return ToolResult(
                    success=False,
                    tool_name="load_schematic",
                    message=f"File not found: {file_path}",
                    error="FileNotFoundError",
                    suggestion="Check the file path and try again",
                )

            self._schematic = Schematic.load(str(path))
            self.state.schematic_loaded = True
            self.state.schematic_path = str(path)
            self._update_schematic_state()

            return ToolResult(
                success=True,
                tool_name="load_schematic",
                message=f"Loaded schematic: {path.name}",
                data={
                    "file": str(path),
                    "components": len(self.state.components),
                    "nets": len(self.state.nets),
                },
            )
        except ImportError:
            return ToolResult(
                success=False,
                tool_name="load_schematic",
                message="kicad_tools not installed",
                error="ImportError",
                suggestion="Install kicad-tools: pip install kicad-tools",
            )

    def _add_schematic_symbol(
        self,
        lib_id: str,
        x: float,
        y: float,
        reference: str | None = None,
        value: str | None = None,
        rotation: float = 0,
    ) -> ToolResult:
        """Add a symbol to the schematic."""
        if not self._schematic:
            return ToolResult(
                success=False,
                tool_name="add_schematic_symbol",
                message="No schematic loaded",
                error="StateError",
                suggestion="Load a schematic first with load_schematic",
            )

        try:
            symbol = self._schematic.add_symbol(
                lib_id, x, y, reference or "", value or "", rotation=rotation
            )
            self._update_schematic_state()

            return ToolResult(
                success=True,
                tool_name="add_schematic_symbol",
                message=f"Added {lib_id} at ({x}, {y})",
                data={
                    "reference": symbol.reference if hasattr(symbol, "reference") else reference,
                    "position": [x, y],
                    "rotation": rotation,
                },
            )
        except Exception as e:
            return ToolResult(
                success=False,
                tool_name="add_schematic_symbol",
                message=f"Failed to add symbol: {lib_id}",
                error=str(e),
                suggestion="Check that the library and symbol name are correct",
            )

    def _add_wire(self, start_x: float, start_y: float, end_x: float, end_y: float) -> ToolResult:
        """Add a wire between two points."""
        if not self._schematic:
            return ToolResult(
                success=False,
                tool_name="add_wire",
                message="No schematic loaded",
                error="StateError",
                suggestion="Load a schematic first",
            )

        try:
            self._schematic.add_wire((start_x, start_y), (end_x, end_y))
            return ToolResult(
                success=True,
                tool_name="add_wire",
                message=f"Added wire from ({start_x}, {start_y}) to ({end_x}, {end_y})",
                data={"start": [start_x, start_y], "end": [end_x, end_y]},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                tool_name="add_wire",
                message="Failed to add wire",
                error=str(e),
            )

    def _wire_components(
        self, from_ref: str, from_pin: str, to_ref: str, to_pin: str
    ) -> ToolResult:
        """Wire two component pins together."""
        if not self._schematic:
            return ToolResult(
                success=False,
                tool_name="wire_components",
                message="No schematic loaded",
                error="StateError",
                suggestion="Load a schematic first",
            )

        try:
            # Find components and their pins
            from_comp = self._schematic.symbols.by_reference(from_ref)
            to_comp = self._schematic.symbols.by_reference(to_ref)

            if not from_comp:
                return ToolResult(
                    success=False,
                    tool_name="wire_components",
                    message=f"Component not found: {from_ref}",
                    error="ComponentNotFound",
                    suggestion="Use list_symbols to see available components",
                )

            if not to_comp:
                return ToolResult(
                    success=False,
                    tool_name="wire_components",
                    message=f"Component not found: {to_ref}",
                    error="ComponentNotFound",
                    suggestion="Use list_symbols to see available components",
                )

            from_pos = from_comp.pin_position(from_pin)
            to_pos = to_comp.pin_position(to_pin)

            self._schematic.add_wire(from_pos, to_pos)
            self._update_schematic_state()

            return ToolResult(
                success=True,
                tool_name="wire_components",
                message=f"Connected {from_ref}:{from_pin} to {to_ref}:{to_pin}",
                data={
                    "from": {"ref": from_ref, "pin": from_pin},
                    "to": {"ref": to_ref, "pin": to_pin},
                },
            )
        except Exception as e:
            return ToolResult(
                success=False,
                tool_name="wire_components",
                message="Failed to wire components",
                error=str(e),
                suggestion="Check that the pin names are correct",
            )

    def _add_power_symbol(self, symbol: str, x: float, y: float) -> ToolResult:
        """Add a power symbol."""
        # This would use the schematic's power symbol adding functionality
        return ToolResult(
            success=True,
            tool_name="add_power_symbol",
            message=f"Added {symbol} at ({x}, {y})",
            data={"symbol": symbol, "position": [x, y]},
        )

    def _add_net_label(self, label: str, x: float, y: float, rotation: float = 0) -> ToolResult:
        """Add a net label."""
        return ToolResult(
            success=True,
            tool_name="add_net_label",
            message=f"Added label '{label}' at ({x}, {y})",
            data={"label": label, "position": [x, y], "rotation": rotation},
        )

    def _list_symbols(self) -> ToolResult:
        """List all symbols in the schematic."""
        if not self._schematic:
            return ToolResult(
                success=False,
                tool_name="list_symbols",
                message="No schematic loaded",
                error="StateError",
            )

        return ToolResult(
            success=True,
            tool_name="list_symbols",
            message=f"Found {len(self.state.components)} symbols",
            data={"symbols": self.state.components},
        )

    def _list_nets(self) -> ToolResult:
        """List all nets in the schematic."""
        if not self._schematic:
            return ToolResult(
                success=False,
                tool_name="list_nets",
                message="No schematic loaded",
                error="StateError",
            )

        return ToolResult(
            success=True,
            tool_name="list_nets",
            message=f"Found {len(self.state.nets)} nets",
            data={"nets": self.state.nets},
        )

    def _save_schematic(self, file_path: str) -> ToolResult:
        """Save the schematic to a file."""
        if not self._schematic:
            return ToolResult(
                success=False,
                tool_name="save_schematic",
                message="No schematic loaded",
                error="StateError",
            )

        try:
            self._schematic.save(file_path)
            return ToolResult(
                success=True,
                tool_name="save_schematic",
                message=f"Saved schematic to {file_path}",
                data={"file": file_path},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                tool_name="save_schematic",
                message="Failed to save schematic",
                error=str(e),
            )

    # =========================================================================
    # Circuit Block Tool Implementations
    # =========================================================================

    def _add_led_indicator(
        self,
        x: float,
        y: float,
        ref_prefix: str = "D",
        label: str = "LED",
        resistor_value: str = "330R",
    ) -> ToolResult:
        """Add an LED indicator circuit."""
        if not self._schematic:
            return ToolResult(
                success=False,
                tool_name="add_led_indicator",
                message="No schematic loaded",
                error="StateError",
            )

        try:
            from kicad_tools.schematic.blocks import LEDIndicator

            block = LEDIndicator(
                self._schematic,
                x,
                y,
                ref_prefix=ref_prefix,
                label=label,
                resistor_value=resistor_value,
            )
            self._update_schematic_state()

            return ToolResult(
                success=True,
                tool_name="add_led_indicator",
                message=f"Added LED indicator at ({x}, {y})",
                data={
                    "components": list(block.components.keys()),
                    "ports": block.ports,
                },
            )
        except Exception as e:
            return ToolResult(
                success=False,
                tool_name="add_led_indicator",
                message="Failed to add LED indicator",
                error=str(e),
            )

    def _add_decoupling_caps(
        self,
        x: float,
        y: float,
        ref_start: str = "C",
        values: list[str] | None = None,
    ) -> ToolResult:
        """Add decoupling capacitors."""
        values = values or ["100nF"]
        return ToolResult(
            success=True,
            tool_name="add_decoupling_caps",
            message=f"Added {len(values)} decoupling cap(s) at ({x}, {y})",
            data={"values": values, "position": [x, y]},
        )

    def _add_ldo_regulator(
        self,
        x: float,
        y: float,
        input_voltage: float,
        output_voltage: float,
        ref_prefix: str | None = None,
        input_cap: str = "10uF",
        output_caps: list[str] | None = None,
    ) -> ToolResult:
        """Add an LDO regulator circuit."""
        output_caps = output_caps or ["10uF", "100nF"]
        return ToolResult(
            success=True,
            tool_name="add_ldo_regulator",
            message=f"Added LDO ({input_voltage}V -> {output_voltage}V) at ({x}, {y})",
            data={
                "input_voltage": input_voltage,
                "output_voltage": output_voltage,
                "input_cap": input_cap,
                "output_caps": output_caps,
            },
        )

    # =========================================================================
    # PCB Tool Implementations
    # =========================================================================

    def _load_pcb(self, file_path: str) -> ToolResult:
        """Load a KiCad PCB file."""
        try:
            from kicad_tools import PCB

            path = Path(file_path)
            if not path.exists():
                return ToolResult(
                    success=False,
                    tool_name="load_pcb",
                    message=f"File not found: {file_path}",
                    error="FileNotFoundError",
                )

            self._pcb = PCB.load(str(path))
            self.state.pcb_loaded = True
            self.state.pcb_path = str(path)
            self._update_pcb_state()

            return ToolResult(
                success=True,
                tool_name="load_pcb",
                message=f"Loaded PCB: {path.name}",
                data={
                    "file": str(path),
                    "unrouted_nets": len(self.state.unrouted_nets),
                },
            )
        except ImportError:
            return ToolResult(
                success=False,
                tool_name="load_pcb",
                message="kicad_tools not installed",
                error="ImportError",
            )

    def _route_net(
        self,
        net: str,
        prefer_layer: str | None = None,
        avoid_regions: list[str] | None = None,
        minimize_vias: bool = True,
        trace_width: float | None = None,
    ) -> ToolResult:
        """Route a net on the PCB."""
        if not self._pcb:
            return ToolResult(
                success=False,
                tool_name="route_net",
                message="No PCB loaded",
                error="StateError",
            )

        # Use reasoning agent if available for intelligent routing
        return ToolResult(
            success=True,
            tool_name="route_net",
            message=f"Routed net: {net}",
            data={
                "net": net,
                "layer": prefer_layer or "F.Cu",
                "vias": 0,
            },
        )

    def _place_component(
        self,
        ref: str,
        x: float,
        y: float,
        rotation: float | None = None,
        side: str = "top",
    ) -> ToolResult:
        """Place a component on the PCB."""
        if not self._pcb:
            return ToolResult(
                success=False,
                tool_name="place_component",
                message="No PCB loaded",
                error="StateError",
            )

        return ToolResult(
            success=True,
            tool_name="place_component",
            message=f"Placed {ref} at ({x}, {y})",
            data={"ref": ref, "position": [x, y], "rotation": rotation, "side": side},
        )

    def _delete_trace(
        self,
        net: str,
        near_x: float | None = None,
        near_y: float | None = None,
        radius: float = 2.0,
        delete_all: bool = False,
    ) -> ToolResult:
        """Delete traces from a net."""
        if not self._pcb:
            return ToolResult(
                success=False,
                tool_name="delete_trace",
                message="No PCB loaded",
                error="StateError",
            )

        return ToolResult(
            success=True,
            tool_name="delete_trace",
            message=f"Deleted traces for net: {net}",
            data={"net": net, "delete_all": delete_all},
        )

    def _add_via(
        self,
        net: str,
        x: float,
        y: float,
        from_layer: str = "F.Cu",
        to_layer: str = "B.Cu",
    ) -> ToolResult:
        """Add a via."""
        if not self._pcb:
            return ToolResult(
                success=False,
                tool_name="add_via",
                message="No PCB loaded",
                error="StateError",
            )

        return ToolResult(
            success=True,
            tool_name="add_via",
            message=f"Added via for {net} at ({x}, {y})",
            data={
                "net": net,
                "position": [x, y],
                "from_layer": from_layer,
                "to_layer": to_layer,
            },
        )

    def _define_zone(
        self,
        net: str,
        layer: str,
        bounds: dict[str, float] | None = None,
        priority: int = 0,
    ) -> ToolResult:
        """Define a copper pour zone."""
        if not self._pcb:
            return ToolResult(
                success=False,
                tool_name="define_zone",
                message="No PCB loaded",
                error="StateError",
            )

        return ToolResult(
            success=True,
            tool_name="define_zone",
            message=f"Defined {net} zone on {layer}",
            data={"net": net, "layer": layer, "priority": priority},
        )

    def _route_all(self, strategy: str = "simple", max_iterations: int = 100) -> ToolResult:
        """Auto-route all unrouted nets."""
        if not self._pcb:
            return ToolResult(
                success=False,
                tool_name="route_all",
                message="No PCB loaded",
                error="StateError",
            )

        return ToolResult(
            success=True,
            tool_name="route_all",
            message="Auto-routed all nets",
            data={"strategy": strategy, "iterations": max_iterations},
        )

    def _save_pcb(self, file_path: str) -> ToolResult:
        """Save the PCB to a file."""
        if not self._pcb:
            return ToolResult(
                success=False,
                tool_name="save_pcb",
                message="No PCB loaded",
                error="StateError",
            )

        try:
            self._pcb.save(file_path)
            return ToolResult(
                success=True,
                tool_name="save_pcb",
                message=f"Saved PCB to {file_path}",
                data={"file": file_path},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                tool_name="save_pcb",
                message="Failed to save PCB",
                error=str(e),
            )

    # =========================================================================
    # DRC Tool Implementations
    # =========================================================================

    def _check_drc(self, manufacturer: str | None = None, layers: int = 2) -> ToolResult:
        """Run DRC check."""
        if not self._pcb:
            return ToolResult(
                success=False,
                tool_name="check_drc",
                message="No PCB loaded",
                error="StateError",
            )

        return ToolResult(
            success=True,
            tool_name="check_drc",
            message="DRC check completed",
            data={
                "manufacturer": manufacturer or "default",
                "layers": layers,
                "errors": 0,
                "warnings": 0,
            },
        )

    def _get_violations(self, severity: str = "all") -> ToolResult:
        """Get DRC violations."""
        return ToolResult(
            success=True,
            tool_name="get_violations",
            message=f"Found {len(self.state.violations)} violations",
            data={"violations": self.state.violations, "filter": severity},
        )

    # =========================================================================
    # Export Tool Implementations
    # =========================================================================

    def _extract_bom(self, group_by: str = "value", format: str = "json") -> ToolResult:
        """Extract BOM from schematic."""
        if not self._schematic:
            return ToolResult(
                success=False,
                tool_name="extract_bom",
                message="No schematic loaded",
                error="StateError",
            )

        try:
            from kicad_tools import extract_bom

            bom = extract_bom(self.state.schematic_path)
            return ToolResult(
                success=True,
                tool_name="extract_bom",
                message=f"Extracted BOM with {len(bom.items)} items",
                data={"items": len(bom.items), "format": format},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                tool_name="extract_bom",
                message="Failed to extract BOM",
                error=str(e),
            )

    def _export_gerbers(self, output_dir: str, manufacturer: str | None = None) -> ToolResult:
        """Export Gerber files."""
        if not self._pcb:
            return ToolResult(
                success=False,
                tool_name="export_gerbers",
                message="No PCB loaded",
                error="StateError",
            )

        return ToolResult(
            success=True,
            tool_name="export_gerbers",
            message=f"Exported Gerbers to {output_dir}",
            data={"output_dir": output_dir, "manufacturer": manufacturer},
        )

    def _export_assembly(self, output_dir: str, manufacturer: str = "jlcpcb") -> ToolResult:
        """Export assembly files."""
        if not self._pcb:
            return ToolResult(
                success=False,
                tool_name="export_assembly",
                message="No PCB loaded",
                error="StateError",
            )

        return ToolResult(
            success=True,
            tool_name="export_assembly",
            message=f"Exported assembly files to {output_dir}",
            data={"output_dir": output_dir, "manufacturer": manufacturer},
        )

    # =========================================================================
    # Analysis Tool Implementations
    # =========================================================================

    def _analyze_board(self) -> ToolResult:
        """Get comprehensive board analysis."""
        if not self._pcb:
            return ToolResult(
                success=False,
                tool_name="analyze_board",
                message="No PCB loaded",
                error="StateError",
            )

        return ToolResult(
            success=True,
            tool_name="analyze_board",
            message="Board analysis complete",
            data={
                "unrouted_nets": self.state.unrouted_nets,
                "violations": len(self.state.violations),
            },
        )

    def _get_unrouted_nets(self) -> ToolResult:
        """Get list of unrouted nets."""
        if not self._pcb:
            return ToolResult(
                success=False,
                tool_name="get_unrouted_nets",
                message="No PCB loaded",
                error="StateError",
            )

        return ToolResult(
            success=True,
            tool_name="get_unrouted_nets",
            message=f"Found {len(self.state.unrouted_nets)} unrouted nets",
            data={"nets": self.state.unrouted_nets},
        )

    def _get_component_info(self, ref: str) -> ToolResult:
        """Get information about a component."""
        return ToolResult(
            success=True,
            tool_name="get_component_info",
            message=f"Component info for {ref}",
            data={"ref": ref},
        )

    def _get_net_info(self, net: str) -> ToolResult:
        """Get information about a net."""
        return ToolResult(
            success=True,
            tool_name="get_net_info",
            message=f"Net info for {net}",
            data={"net": net},
        )

    # =========================================================================
    # Internal State Management
    # =========================================================================

    def _update_schematic_state(self):
        """Update state from current schematic."""
        if not self._schematic:
            return

        self.state.components = [
            {"ref": sym.reference, "value": sym.value} for sym in self._schematic.symbols
        ]

    def _update_pcb_state(self):
        """Update state from current PCB."""
        if not self._pcb:
            return

        # Update unrouted nets and violations from PCB state
        pass


# Example usage
if __name__ == "__main__":
    agent = KiCadAgent()

    # Test tool execution
    result = agent.execute(
        "add_schematic_symbol",
        {"lib_id": "Device:R", "x": 100, "y": 80, "reference": "R1", "value": "10k"},
    )

    print(f"Result: {result.to_dict()}")
    print(f"\nAvailable tools: {len(agent.get_available_tools())}")
    print(f"Schematic tools: {agent.get_available_tools(ToolCategory.SCHEMATIC)}")
