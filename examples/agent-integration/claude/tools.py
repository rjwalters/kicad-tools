"""
Claude Tool Definitions for kicad-tools.

This module provides tool definitions formatted for Claude's tool use API.
Each tool corresponds to a kicad-tools operation and includes proper input
schemas for reliable function calling.

Usage:
    import anthropic
    from tools import KICAD_TOOLS

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        tools=KICAD_TOOLS,
        messages=[{"role": "user", "content": "Create a simple LED blinker circuit"}]
    )
"""

# =============================================================================
# Schematic Tools
# =============================================================================

SCHEMATIC_TOOLS = [
    {
        "name": "load_schematic",
        "description": "Load a KiCad schematic file (.kicad_sch) for analysis or modification.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the KiCad schematic file (.kicad_sch)",
                }
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "add_schematic_symbol",
        "description": "Add a component symbol to the schematic at specified coordinates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lib_id": {
                    "type": "string",
                    "description": "Library:SymbolName (e.g., 'Device:R', 'Device:C', 'Device:LED')",
                },
                "x": {
                    "type": "number",
                    "description": "X coordinate in mm",
                },
                "y": {
                    "type": "number",
                    "description": "Y coordinate in mm",
                },
                "reference": {
                    "type": "string",
                    "description": "Reference designator (e.g., 'R1', 'C1', 'U1')",
                },
                "value": {
                    "type": "string",
                    "description": "Component value (e.g., '10k', '100nF', 'ATtiny85')",
                },
                "rotation": {
                    "type": "number",
                    "description": "Rotation in degrees (0, 90, 180, 270)",
                    "default": 0,
                },
            },
            "required": ["lib_id", "x", "y"],
        },
    },
    {
        "name": "add_wire",
        "description": "Add a wire connecting two points on the schematic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_x": {"type": "number", "description": "Start X coordinate in mm"},
                "start_y": {"type": "number", "description": "Start Y coordinate in mm"},
                "end_x": {"type": "number", "description": "End X coordinate in mm"},
                "end_y": {"type": "number", "description": "End Y coordinate in mm"},
            },
            "required": ["start_x", "start_y", "end_x", "end_y"],
        },
    },
    {
        "name": "wire_components",
        "description": "Connect two component pins with a wire, automatically routing between them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_ref": {
                    "type": "string",
                    "description": "Source component reference (e.g., 'R1')",
                },
                "from_pin": {
                    "type": "string",
                    "description": "Source pin name or number (e.g., '1', 'VCC')",
                },
                "to_ref": {
                    "type": "string",
                    "description": "Target component reference (e.g., 'U1')",
                },
                "to_pin": {
                    "type": "string",
                    "description": "Target pin name or number (e.g., 'GND', 'PA0')",
                },
            },
            "required": ["from_ref", "from_pin", "to_ref", "to_pin"],
        },
    },
    {
        "name": "add_power_symbol",
        "description": "Add a power symbol (VCC, GND, +3V3, etc.) to the schematic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Power symbol name (e.g., 'VCC', 'GND', '+3V3', '+5V')",
                },
                "x": {"type": "number", "description": "X coordinate in mm"},
                "y": {"type": "number", "description": "Y coordinate in mm"},
            },
            "required": ["symbol", "x", "y"],
        },
    },
    {
        "name": "add_net_label",
        "description": "Add a net label to identify a connection.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Net label text (e.g., 'SCL', 'MOSI', 'LED_OUT')",
                },
                "x": {"type": "number", "description": "X coordinate in mm"},
                "y": {"type": "number", "description": "Y coordinate in mm"},
                "rotation": {
                    "type": "number",
                    "description": "Rotation in degrees",
                    "default": 0,
                },
            },
            "required": ["label", "x", "y"],
        },
    },
    {
        "name": "list_symbols",
        "description": "List all component symbols in the current schematic.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_nets",
        "description": "List all nets (connections) in the current schematic.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "save_schematic",
        "description": "Save the current schematic to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Output file path (.kicad_sch)",
                }
            },
            "required": ["file_path"],
        },
    },
]

# =============================================================================
# Circuit Block Tools
# =============================================================================

CIRCUIT_BLOCK_TOOLS = [
    {
        "name": "add_led_indicator",
        "description": "Add an LED with current-limiting resistor. Creates LED, resistor, and wiring.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X coordinate in mm"},
                "y": {"type": "number", "description": "Y coordinate in mm"},
                "ref_prefix": {
                    "type": "string",
                    "description": "Reference prefix (e.g., 'D1')",
                    "default": "D",
                },
                "label": {
                    "type": "string",
                    "description": "LED label (e.g., 'PWR', 'STATUS')",
                    "default": "LED",
                },
                "resistor_value": {
                    "type": "string",
                    "description": "Current limiting resistor value",
                    "default": "330R",
                },
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "add_decoupling_caps",
        "description": "Add a bank of decoupling capacitors for power filtering.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X coordinate in mm"},
                "y": {"type": "number", "description": "Y coordinate in mm"},
                "ref_start": {
                    "type": "string",
                    "description": "Starting reference (e.g., 'C1')",
                    "default": "C",
                },
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Capacitor values (e.g., ['10uF', '100nF'])",
                    "default": ["100nF"],
                },
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "add_ldo_regulator",
        "description": "Add a voltage regulator circuit with input/output capacitors.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X coordinate in mm"},
                "y": {"type": "number", "description": "Y coordinate in mm"},
                "ref_prefix": {
                    "type": "string",
                    "description": "Reference prefix (e.g., 'U1')",
                },
                "input_voltage": {
                    "type": "number",
                    "description": "Input voltage (e.g., 5.0)",
                },
                "output_voltage": {
                    "type": "number",
                    "description": "Output voltage (e.g., 3.3)",
                },
                "input_cap": {
                    "type": "string",
                    "description": "Input capacitor value",
                    "default": "10uF",
                },
                "output_caps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Output capacitor values",
                    "default": ["10uF", "100nF"],
                },
            },
            "required": ["x", "y", "input_voltage", "output_voltage"],
        },
    },
    {
        "name": "add_mcu_block",
        "description": "Add a microcontroller with typical support circuitry (decoupling, reset, crystal).",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X coordinate in mm"},
                "y": {"type": "number", "description": "Y coordinate in mm"},
                "mcu_symbol": {
                    "type": "string",
                    "description": "MCU symbol (e.g., 'MCU_Microchip_ATtiny:ATtiny85-20PU')",
                },
                "ref": {
                    "type": "string",
                    "description": "Reference designator (e.g., 'U1')",
                },
                "add_crystal": {
                    "type": "boolean",
                    "description": "Add external crystal oscillator",
                    "default": False,
                },
                "add_reset": {
                    "type": "boolean",
                    "description": "Add reset circuit with pull-up",
                    "default": True,
                },
            },
            "required": ["x", "y", "mcu_symbol"],
        },
    },
]

# =============================================================================
# PCB Layout Tools
# =============================================================================

PCB_TOOLS = [
    {
        "name": "load_pcb",
        "description": "Load a KiCad PCB file (.kicad_pcb) for layout or routing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the KiCad PCB file (.kicad_pcb)",
                }
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "route_net",
        "description": "Route a net on the PCB, connecting all pads of the specified net.",
        "input_schema": {
            "type": "object",
            "properties": {
                "net": {
                    "type": "string",
                    "description": "Net name to route (e.g., 'GND', 'SCL', 'VCC')",
                },
                "prefer_layer": {
                    "type": "string",
                    "description": "Preferred copper layer (e.g., 'F.Cu', 'B.Cu')",
                },
                "avoid_regions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Region names to avoid",
                },
                "minimize_vias": {
                    "type": "boolean",
                    "description": "Try to minimize layer transitions",
                    "default": True,
                },
                "trace_width": {
                    "type": "number",
                    "description": "Trace width in mm (uses default if not specified)",
                },
            },
            "required": ["net"],
        },
    },
    {
        "name": "place_component",
        "description": "Move a component to a new position on the PCB.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Component reference (e.g., 'U1', 'R1')",
                },
                "x": {"type": "number", "description": "X coordinate in mm"},
                "y": {"type": "number", "description": "Y coordinate in mm"},
                "rotation": {
                    "type": "number",
                    "description": "Rotation in degrees",
                },
                "side": {
                    "type": "string",
                    "enum": ["top", "bottom"],
                    "description": "Board side for component placement",
                    "default": "top",
                },
            },
            "required": ["ref", "x", "y"],
        },
    },
    {
        "name": "delete_trace",
        "description": "Delete traces of a net, optionally near a specific location.",
        "input_schema": {
            "type": "object",
            "properties": {
                "net": {
                    "type": "string",
                    "description": "Net name to delete traces from",
                },
                "near_x": {
                    "type": "number",
                    "description": "X coordinate to search near",
                },
                "near_y": {
                    "type": "number",
                    "description": "Y coordinate to search near",
                },
                "radius": {
                    "type": "number",
                    "description": "Search radius in mm",
                    "default": 2.0,
                },
                "delete_all": {
                    "type": "boolean",
                    "description": "Delete all routing for this net",
                    "default": False,
                },
            },
            "required": ["net"],
        },
    },
    {
        "name": "add_via",
        "description": "Add a via for layer transition.",
        "input_schema": {
            "type": "object",
            "properties": {
                "net": {"type": "string", "description": "Net name for the via"},
                "x": {"type": "number", "description": "X coordinate in mm"},
                "y": {"type": "number", "description": "Y coordinate in mm"},
                "from_layer": {
                    "type": "string",
                    "description": "Starting layer",
                    "default": "F.Cu",
                },
                "to_layer": {
                    "type": "string",
                    "description": "Ending layer",
                    "default": "B.Cu",
                },
            },
            "required": ["net", "x", "y"],
        },
    },
    {
        "name": "define_zone",
        "description": "Define a copper pour zone for power or ground planes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "net": {
                    "type": "string",
                    "description": "Net for the zone (e.g., 'GND', 'VCC')",
                },
                "layer": {
                    "type": "string",
                    "description": "Copper layer (e.g., 'F.Cu', 'B.Cu')",
                },
                "bounds": {
                    "type": "object",
                    "properties": {
                        "x1": {"type": "number"},
                        "y1": {"type": "number"},
                        "x2": {"type": "number"},
                        "y2": {"type": "number"},
                    },
                    "description": "Zone bounding box coordinates",
                },
                "priority": {
                    "type": "integer",
                    "description": "Zone priority (higher = fills first)",
                    "default": 0,
                },
            },
            "required": ["net", "layer"],
        },
    },
    {
        "name": "route_all",
        "description": "Auto-route all unrouted nets on the PCB.",
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {
                    "type": "string",
                    "enum": ["simple", "monte_carlo", "iterative"],
                    "description": "Routing strategy to use",
                    "default": "simple",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Maximum routing iterations",
                    "default": 100,
                },
            },
        },
    },
    {
        "name": "save_pcb",
        "description": "Save the current PCB to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Output file path (.kicad_pcb)",
                }
            },
            "required": ["file_path"],
        },
    },
]

# =============================================================================
# DRC and Validation Tools
# =============================================================================

DRC_TOOLS = [
    {
        "name": "check_drc",
        "description": "Run design rule check on the current PCB.",
        "input_schema": {
            "type": "object",
            "properties": {
                "manufacturer": {
                    "type": "string",
                    "description": "Manufacturer rules to check against (e.g., 'jlcpcb', 'oshpark', 'pcbway')",
                },
                "layers": {
                    "type": "integer",
                    "description": "Number of PCB layers",
                    "default": 2,
                },
            },
        },
    },
    {
        "name": "parse_drc_report",
        "description": "Parse an existing KiCad DRC report file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to DRC report file (.rpt or .json)",
                }
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "get_violations",
        "description": "Get current DRC violations on the board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "severity": {
                    "type": "string",
                    "enum": ["error", "warning", "all"],
                    "description": "Filter by severity level",
                    "default": "all",
                },
            },
        },
    },
]

# =============================================================================
# BOM and Export Tools
# =============================================================================

EXPORT_TOOLS = [
    {
        "name": "extract_bom",
        "description": "Extract Bill of Materials from the schematic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_by": {
                    "type": "string",
                    "enum": ["value", "footprint", "none"],
                    "description": "How to group components",
                    "default": "value",
                },
                "format": {
                    "type": "string",
                    "enum": ["json", "csv", "markdown"],
                    "description": "Output format",
                    "default": "json",
                },
            },
        },
    },
    {
        "name": "export_gerbers",
        "description": "Export Gerber files for PCB manufacturing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "description": "Output directory for Gerber files",
                },
                "manufacturer": {
                    "type": "string",
                    "description": "Manufacturer preset (e.g., 'jlcpcb', 'oshpark')",
                },
            },
            "required": ["output_dir"],
        },
    },
    {
        "name": "export_assembly",
        "description": "Export assembly files (BOM, CPL) for PCBA services.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "description": "Output directory for assembly files",
                },
                "manufacturer": {
                    "type": "string",
                    "description": "Manufacturer format (e.g., 'jlcpcb')",
                    "default": "jlcpcb",
                },
            },
            "required": ["output_dir"],
        },
    },
]

# =============================================================================
# Analysis Tools
# =============================================================================

ANALYSIS_TOOLS = [
    {
        "name": "analyze_board",
        "description": "Get comprehensive analysis of current PCB state.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_unrouted_nets",
        "description": "List all nets that haven't been routed yet.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_component_info",
        "description": "Get detailed information about a specific component.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Component reference (e.g., 'U1')",
                }
            },
            "required": ["ref"],
        },
    },
    {
        "name": "get_net_info",
        "description": "Get detailed information about a specific net.",
        "input_schema": {
            "type": "object",
            "properties": {
                "net": {
                    "type": "string",
                    "description": "Net name",
                }
            },
            "required": ["net"],
        },
    },
    {
        "name": "measure_clearance",
        "description": "Measure clearance between two points or objects.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_ref": {
                    "type": "string",
                    "description": "Starting component reference",
                },
                "to_ref": {
                    "type": "string",
                    "description": "Ending component reference",
                },
            },
            "required": ["from_ref", "to_ref"],
        },
    },
]

# =============================================================================
# Combined Tool List
# =============================================================================

KICAD_TOOLS = (
    SCHEMATIC_TOOLS + CIRCUIT_BLOCK_TOOLS + PCB_TOOLS + DRC_TOOLS + EXPORT_TOOLS + ANALYSIS_TOOLS
)

# Organized by category for selective use
TOOL_CATEGORIES = {
    "schematic": SCHEMATIC_TOOLS,
    "circuit_blocks": CIRCUIT_BLOCK_TOOLS,
    "pcb": PCB_TOOLS,
    "drc": DRC_TOOLS,
    "export": EXPORT_TOOLS,
    "analysis": ANALYSIS_TOOLS,
}


def get_tools(categories: list[str] | None = None) -> list[dict]:
    """Get tools for specific categories.

    Args:
        categories: List of category names. If None, returns all tools.

    Returns:
        List of tool definitions for the specified categories.

    Example:
        # Get only PCB and DRC tools
        tools = get_tools(["pcb", "drc"])
    """
    if categories is None:
        return KICAD_TOOLS

    result = []
    for cat in categories:
        if cat in TOOL_CATEGORIES:
            result.extend(TOOL_CATEGORIES[cat])
    return result


if __name__ == "__main__":
    # Print tool summary
    print("kicad-tools Claude Tool Definitions")
    print("=" * 50)
    for category, tools in TOOL_CATEGORIES.items():
        print(f"\n{category.upper()} ({len(tools)} tools):")
        for tool in tools:
            print(f"  - {tool['name']}: {tool['description'][:60]}...")
    print(f"\nTotal: {len(KICAD_TOOLS)} tools")
