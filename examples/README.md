# kicad-tools Examples

This directory contains example projects demonstrating common workflows with kicad-tools.

## Examples

| Example | Description | Key Concepts |
|---------|-------------|--------------|
| [01-schematic-analysis](01-schematic-analysis/) | Load and analyze KiCad schematics | Symbols, nets, labels, wires |
| [02-bom-generation](02-bom-generation/) | Extract Bill of Materials | BOM extraction, grouping, export |
| [03-drc-checking](03-drc-checking/) | Parse DRC reports, check manufacturer rules | DRC parsing, manufacturer validation |
| [04-autorouter](04-autorouter/) | PCB autorouting with placement optimization | Routing strategies, force-directed layout |
| [05-end-to-end](05-end-to-end/) | Create complete designs programmatically | Circuit blocks, power rails, schematic generation |
| [06-intelligent-placement](06-intelligent-placement/) | v0.6.0 intelligent placement for AI agents | Clustering, edge detection, thermal, sessions |
| [07-design-feedback](07-design-feedback/) | v0.7.0 design feedback for AI agents | Rich errors, congestion, thermal, cost estimation |
| [llm-routing](llm-routing/) | LLM-driven PCB layout decisions | Reasoning agent, command vocabulary, feedback loops |
| [agent-integration](agent-integration/) | AI agent tool definitions and examples | Claude tools, OpenAI functions, error handling |

## Quick Start

Each example includes a Python script and sample KiCad files:

```bash
# Install kicad-tools
pip install kicad-tools

# Run an example
cd examples/01-schematic-analysis
python analyze.py
```

Or with uv:

```bash
uv run python examples/01-schematic-analysis/analyze.py
```

## Example Overview

### 01 - Schematic Analysis

Learn how to load schematics and extract component and net information.

```python
from kicad_tools import Schematic

sch = Schematic.load("design.kicad_sch")
for sym in sch.symbols:
    print(f"{sym.reference}: {sym.value}")
```

### 02 - BOM Generation

Extract and format Bill of Materials for manufacturing.

```python
from kicad_tools import extract_bom

bom = extract_bom("design.kicad_sch")
for group in bom.grouped():
    print(f"{group.quantity}x {group.value}")
```

### 03 - DRC Checking

Parse DRC reports and validate against manufacturer capabilities.

```python
from kicad_tools.drc import DRCReport, check_manufacturer_rules

report = DRCReport.load("design-drc.rpt")
checks = check_manufacturer_rules(report, "jlcpcb")
```

### 04 - Autorouter

Route PCB traces with various strategies.

```python
from kicad_tools.router import load_pcb_for_routing, DesignRules

router, _ = load_pcb_for_routing("board.kicad_pcb", rules=rules)
router.route_all_monte_carlo(num_trials=10)
```

### 05 - End-to-End Design

Create complete schematic designs programmatically using circuit blocks.

```python
from kicad_tools.schematic.models.schematic import Schematic
from kicad_tools.schematic.blocks import CrystalOscillator, DebugHeader

# Create schematic
sch = Schematic(title="My Board", date="2025-01")

# Add power rails
sch.add_rail(y=30, x_start=25, x_end=200, net_label="+3.3V")

# Add circuit blocks
xtal = CrystalOscillator(sch, x=100, y=80, frequency="8MHz")
debug = DebugHeader(sch, x=150, y=100, interface="swd")

# Write output
sch.write("output/board.kicad_sch")
```

### 06 - Intelligent Placement (v0.6.0)

Use intelligent placement features designed for AI agent workflows.

```python
from kicad_tools.optim import (
    PlacementSession,
    detect_functional_clusters,
    detect_edge_components,
    classify_thermal_properties,
)
from kicad_tools.schema.pcb import PCB

# Load PCB and create session
pcb = PCB.load("board.kicad_pcb")
session = PlacementSession(pcb)

# Query a move before applying
result = session.query_move("C1", 45.0, 32.0)
if result.score_delta < 0:  # Improvement
    session.apply_move("C1", 45.0, 32.0)

# Commit changes
session.commit()
pcb.save("optimized.kicad_pcb")
```

### 07 - Design Feedback (v0.7.0)

Get actionable feedback on designs for AI agent iteration.

```python
from kicad_tools.analysis import analyze_congestion, analyze_thermal
from kicad_tools.cost import estimate_manufacturing_cost

# Analyze routing congestion
congestion = analyze_congestion(pcb, grid_size=2.0)
for hotspot in congestion.hotspots:
    print(f"{hotspot.severity}: {hotspot.suggestion}")

# Check thermal issues
thermal = analyze_thermal(pcb)
for source in thermal.heat_sources:
    if source.estimated_temp_rise > 40:
        print(f"Warning: {source.reference} may overheat")

# Estimate manufacturing costs
cost = estimate_manufacturing_cost(pcb, bom, quantity=10)
print(f"Per board: ${cost.per_board:.2f}")
```

### LLM Routing

Integrate LLMs for semantic PCB layout decisions.

```python
from kicad_tools import PCBReasoningAgent

agent = PCBReasoningAgent.from_pcb("board.kicad_pcb")
while not agent.is_complete():
    prompt = agent.get_prompt()
    command = call_your_llm(prompt)
    result, diagnosis = agent.execute_dict(command)
agent.save("board_routed.kicad_pcb")
```

### Agent Integration

Tool definitions and examples for AI agents (Claude, GPT-4, local models).

```python
# Claude integration
from agent_integration.claude.tools import KICAD_TOOLS

# OpenAI integration
import json
with open("agent-integration/openai/tools.json") as f:
    functions = json.load(f)["functions"]

# Common wrapper for any LLM
from agent_integration.common.kicad_tools_wrapper import KiCadAgent
agent = KiCadAgent()
result = agent.execute("add_schematic_symbol", {
    "lib_id": "Device:R",
    "x": 100, "y": 80,
    "reference": "R1"
})
```

## CLI Commands

Most functionality is also available via the command line:

```bash
# Schematic analysis
kct symbols design.kicad_sch
kct nets design.kicad_sch

# BOM generation
kct bom design.kicad_sch --format csv

# DRC checking
kct drc design-drc.rpt --mfr jlcpcb

# Manufacturer comparison
kct mfr compare

# LLM reasoning
kct reason board.kicad_pcb --analyze
kct reason board.kicad_pcb --export-state
```

## Sample Files

Each example includes sample KiCad files:

- `simple_rc.kicad_sch` - Simple RC circuit schematic
- `sample_drc.rpt` - DRC report with sample violations
- `charlieplex_led_grid/` - LED matrix demo board
- `usb_joystick/` - USB game controller demo

## Running All Examples

Verify all examples work:

```bash
cd examples

# Run all Python examples
for script in */analyze.py **/generate_bom.py **/check_drc.py; do
    echo "Running $script..."
    python "$script"
done
```

## Further Reading

- [API Documentation](../docs/)
- [CLI Reference](../README.md#command-line-usage)
- [CHANGELOG](../CHANGELOG.md)
