# Agent Integration Examples

This directory contains examples and tools for integrating AI agents (Claude, GPT-4, local models) with kicad-tools for automated PCB design.

## Overview

kicad-tools is designed to be agent-friendly, providing structured APIs that AI models can use to create schematics, route PCBs, and export manufacturing files.

```
examples/agent-integration/
├── README.md              # This file
├── claude/
│   ├── tools.py           # Claude tool definitions
│   ├── prompts.md         # System prompts for Claude
│   └── example_session.md # Example conversation
├── openai/
│   ├── tools.json         # OpenAI function definitions
│   ├── prompts.md         # System prompts for GPT
│   └── example_session.md # Example conversation
└── common/
    ├── kicad_tools_wrapper.py  # Unified wrapper for all agents
    └── error_handlers.py       # Error recovery patterns
```

## Quick Start

### Claude Integration

```python
import anthropic
from claude.tools import KICAD_TOOLS

client = anthropic.Anthropic()

response = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=1024,
    tools=KICAD_TOOLS,
    messages=[
        {"role": "user", "content": "Create an LED blinker with an ATtiny85"}
    ]
)

# Process tool calls
for block in response.content:
    if block.type == "tool_use":
        print(f"Tool: {block.name}")
        print(f"Args: {block.input}")
```

### OpenAI Integration

```python
import json
from openai import OpenAI

with open("openai/tools.json") as f:
    config = json.load(f)
    functions = config["functions"]

client = OpenAI()

response = client.chat.completions.create(
    model="gpt-4-turbo",
    messages=[
        {"role": "system", "content": "You are a PCB designer using kicad-tools."},
        {"role": "user", "content": "Create a temperature sensor board"}
    ],
    functions=functions,
    function_call="auto"
)
```

### Using the Common Wrapper

```python
from common.kicad_tools_wrapper import KiCadAgent

agent = KiCadAgent()

# Execute tool calls from any LLM
result = agent.execute("add_schematic_symbol", {
    "lib_id": "Device:R",
    "x": 100, "y": 80,
    "reference": "R1",
    "value": "10k"
})

if result.success:
    print(f"Success: {result.message}")
else:
    print(f"Error: {result.error}")
    print(f"Suggestion: {result.suggestion}")
```

## Tool Categories

### Schematic Tools

| Tool | Description |
|------|-------------|
| `load_schematic` | Load a KiCad schematic file |
| `add_schematic_symbol` | Add a component symbol |
| `add_wire` | Connect two points with a wire |
| `wire_components` | Connect component pins directly |
| `add_power_symbol` | Add power rail symbols (VCC, GND) |
| `add_net_label` | Add net labels for clarity |
| `list_symbols` | List all components |
| `list_nets` | List all nets |
| `save_schematic` | Save the schematic |

### Circuit Block Tools

| Tool | Description |
|------|-------------|
| `add_led_indicator` | Add LED + resistor circuit |
| `add_decoupling_caps` | Add capacitor bank |
| `add_ldo_regulator` | Add voltage regulator circuit |
| `add_mcu_block` | Add MCU with support circuits |

### PCB Tools

| Tool | Description |
|------|-------------|
| `load_pcb` | Load a KiCad PCB file |
| `route_net` | Route a specific net |
| `route_all` | Auto-route all nets |
| `place_component` | Move component position |
| `delete_trace` | Remove traces |
| `add_via` | Add layer transition via |
| `define_zone` | Create copper pour zone |
| `save_pcb` | Save the PCB |

### DRC & Validation

| Tool | Description |
|------|-------------|
| `check_drc` | Run design rule check |
| `get_violations` | Get DRC violations |

### Export Tools

| Tool | Description |
|------|-------------|
| `extract_bom` | Extract Bill of Materials |
| `export_gerbers` | Export manufacturing files |
| `export_assembly` | Export BOM/CPL for PCBA |

## Error Handling

The `error_handlers.py` module provides robust error recovery:

```python
from common.error_handlers import ErrorHandler, ErrorType

handler = ErrorHandler()

# When a tool fails
result = agent.execute("route_net", {"net": "SDA"})
if not result.success:
    error_type = handler.classify_error("route_net", result.error, {"net": "SDA"})
    recovery = handler.get_recovery("route_net", result.error, {"net": "SDA"})

    print(f"Error type: {error_type.name}")
    print(f"Recovery: {recovery.to_prompt()}")
```

### Common Error Types

- `NO_SCHEMATIC` / `NO_PCB`: Load file first
- `COMPONENT_NOT_FOUND`: Check component references
- `ROUTE_BLOCKED`: Try different layer or delete conflicts
- `CLEARANCE_VIOLATION`: Increase spacing
- `COLLISION`: Adjust component position
- `DRC_VIOLATION`: Fix design rule issues

## Design Patterns

### 1. Schematic-First Workflow

Always create the schematic before PCB layout:

```
1. Add power symbols (VCC, GND)
2. Add main components (ICs, connectors)
3. Add support components (caps, resistors)
4. Wire power connections
5. Wire signal connections
6. Save schematic
7. Load PCB
8. Place components
9. Route nets
10. Run DRC
11. Export
```

### 2. Iterative Routing

Route in priority order:

```
1. Power nets (VCC, GND) - wider traces
2. Clock signals - short and direct
3. High-speed signals
4. Analog signals - isolated
5. General signals
```

### 3. Error Recovery Loop

```python
max_retries = 3
for attempt in range(max_retries):
    result = agent.execute(tool_name, args)
    if result.success:
        break

    recovery = handler.get_recovery(tool_name, result.error, args)
    if recovery.action == RecoveryAction.RETRY_MODIFIED:
        args = recovery.modified_args
    elif recovery.action == RecoveryAction.PREREQUISITE:
        for prereq_tool, prereq_args in recovery.prerequisite_tools:
            agent.execute(prereq_tool, prereq_args)
    else:
        break
```

## Example Projects

### USB-Powered LED Blinker

See `claude/example_session.md` for a complete walkthrough:
- ATtiny85 microcontroller
- USB-C power input
- Status LED with current limiting
- Full schematic and PCB

### I2C Temperature Sensor

See `openai/example_session.md` for a complete walkthrough:
- TMP102 temperature sensor
- 4-pin I2C connector
- Power LED indicator
- 2-layer PCB for JLCPCB

## Prompt Engineering Tips

1. **Be specific about components**: Use exact library names like `Device:R` not just "resistor"

2. **Include coordinates**: Always specify X/Y positions for placement

3. **Order operations logically**: Power first, then signals

4. **Handle errors gracefully**: Include recovery instructions in system prompts

5. **Validate before export**: Always run DRC before generating manufacturing files

## Testing

Run the wrapper self-test:

```bash
cd examples/agent-integration
python -m common.kicad_tools_wrapper
```

Test error handlers:

```bash
python -m common.error_handlers
```

## Requirements

- Python 3.10+
- kicad-tools (`pip install kicad-tools`)
- For Claude: `anthropic` package
- For OpenAI: `openai` package

## Further Reading

- [LLM Routing Example](../llm-routing/) - Semantic PCB routing with LLMs
- [kicad-tools Documentation](../../docs/)
- [API Reference](../../README.md)
