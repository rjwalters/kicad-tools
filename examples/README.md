# kicad-tools Examples

This directory contains example projects demonstrating common workflows with kicad-tools.

## Examples

| Example | Description | Key Concepts |
|---------|-------------|--------------|
| [01-schematic-analysis](01-schematic-analysis/) | Load and analyze KiCad schematics | Symbols, nets, labels, wires |
| [02-bom-generation](02-bom-generation/) | Extract Bill of Materials | BOM extraction, grouping, export |
| [03-drc-checking](03-drc-checking/) | Parse DRC reports, check manufacturer rules | DRC parsing, manufacturer validation |
| [04-autorouter](04-autorouter/) | PCB autorouting with placement optimization | Routing strategies, force-directed layout |
| [llm-routing](llm-routing/) | LLM-driven PCB layout decisions | Reasoning agent, command vocabulary, feedback loops |

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
