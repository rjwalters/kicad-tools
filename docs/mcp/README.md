# kicad-tools MCP Server

The kicad-tools MCP (Model Context Protocol) server enables AI assistants like Claude to interact with KiCad PCB designs through a standardized protocol.

## What is MCP?

The [Model Context Protocol](https://modelcontextprotocol.io/) is a standard for connecting AI assistants to external tools and data sources. The kicad-tools MCP server exposes PCB analysis and manipulation capabilities to AI assistants.

## Quick Start

### Prerequisites

- Python 3.10 or higher
- kicad-tools installed with MCP support

### Installation

```bash
# Install kicad-tools with MCP dependencies
pip install "kicad-tools[mcp]"
```

### Configure Claude Desktop

1. Open Claude Desktop settings
2. Navigate to the MCP servers configuration
3. Add the following configuration to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "kicad-tools": {
      "command": "python",
      "args": ["-m", "kicad_tools.mcp.server"]
    }
  }
}
```

4. Restart Claude Desktop
5. Verify by asking Claude: "What PCB tools are available?"

## Available Tools

The MCP server provides 16 tools organized into five categories:

### Analysis Tools
- **analyze_board** - Get board summary (layers, components, nets, dimensions)
- **get_drc_violations** - Run DRC and return violations with locations
- **placement_analyze** - Analyze component placement quality
- **placement_suggestions** - Get AI-friendly placement recommendations
- **measure_clearance** - Measure clearance between items

### Export Tools
- **export_gerbers** - Export Gerber files for PCB manufacturing
- **export_bom** - Export Bill of Materials from schematic
- **export_assembly** - Generate complete assembly package (Gerbers + BOM + PnP)

### Session Tools (Interactive Placement)
- **start_session** - Start a placement refinement session
- **query_move** - Query impact of hypothetical component move
- **apply_move** - Apply a component move
- **undo_move** - Undo the last applied move
- **commit_session** - Write changes to file
- **rollback_session** - Discard all changes

### Routing Tools
- **route_net** - Route a specific net
- **get_unrouted_nets** - List nets that need routing

## Example Conversations

### Design Review
```
User: "Review my PCB design at ~/projects/board.kicad_pcb"

Claude: [calls placement_analyze]
"Your 4-layer 50x40mm board scores 78/100:
- Wire length: 85/100
- Congestion: 72/100 (2 hotspots near U1)
- Thermal: 90/100
- Signal integrity: 75/100 (crosstalk risk on SPI bus)
- Manufacturing: 80/100

Recommendations:
1. Move C3 closer to U1 to reduce bypass capacitor distance
2. Increase spacing between SPI_CLK and SPI_MISO traces"
```

### Manufacturing Preparation
```
User: "Prepare this board for JLCPCB"

Claude: [calls export_assembly]
"Generated manufacturing package:
- Gerber files: 8 layers
- BOM: 45 unique parts (3 missing LCSC numbers)
- Pick-and-place: 127 placements

Output: ~/projects/board-jlcpcb-assembly.zip

Note: Missing LCSC part numbers for: U3, J2, SW1"
```

### Interactive Placement Refinement
```
User: "Help me optimize bypass capacitor placement"

Claude: [calls start_session]
"Started session. Let me analyze capacitor positions..."

Claude: [calls query_move for C1]
"Moving C1 to (45.2, 32.1) would:
- Reduce wire length by 2.3mm
- Improve decoupling for U1

Shall I apply this change?"

User: "Yes, apply it"

Claude: [calls apply_move]
"Applied. Score improved from 78 to 81."
```

## Documentation

- [Setup Guide](setup.md) - Detailed installation and configuration
- [Tool Reference](tools.md) - Complete tool documentation
- [Example Workflows](workflows.md) - Common usage patterns
- [Configuration](configuration.md) - Server configuration options
- [Troubleshooting](troubleshooting.md) - Common issues and solutions

## Support

For issues with the MCP server:
- [GitHub Issues](https://github.com/rjwalters/kicad-tools/issues)
- [MCP Protocol Documentation](https://modelcontextprotocol.io/)
