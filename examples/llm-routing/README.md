# LLM-Driven PCB Routing

This directory contains examples demonstrating how to integrate LLMs (Large Language Models) with kicad-tools for semantic PCB layout decisions.

## The Concept

Traditional autorouters are **semantically blind** - they connect pads mechanically without understanding design intent. An LLM can reason about **WHY** decisions matter:

- "Route clock signals on the shortest path"
- "Keep analog section isolated from digital"
- "Use wider traces for power nets"
- "Minimize crosstalk between differential pairs"

The kicad-tools reasoning module provides:
1. **PCB state representation** suitable for LLM prompts
2. **Command vocabulary** for routing actions
3. **Feedback/diagnosis** for failed operations

## Quick Start

### CLI Usage

```bash
# Export current state as JSON for external LLM processing
kct reason board.kicad_pcb --export-state

# Get detailed analysis of current board state
kct reason board.kicad_pcb --analyze

# Auto-route priority nets (without LLM)
kct reason board.kicad_pcb --auto-route --max-nets 5

# Interactive mode (for LLM integration)
kct reason board.kicad_pcb --interactive
```

### Python API

```python
from kicad_tools import PCBReasoningAgent

# Load board
agent = PCBReasoningAgent.from_pcb("board.kicad_pcb")

# Reasoning loop
while not agent.is_complete():
    # Get state as prompt for LLM
    prompt = agent.get_prompt()

    # Call your LLM (OpenAI, Anthropic, local, etc.)
    command = call_your_llm(prompt)

    # Execute and get feedback
    result, diagnosis = agent.execute_dict(command)

    if not result.success:
        # Feed diagnosis back to LLM
        print(diagnosis)

# Save result
agent.save("board_routed.kicad_pcb")
```

## Available Commands

The LLM should return JSON commands:

### Route a Net

```json
{
    "command": "route_net",
    "net": "SCL",
    "minimize_vias": true,
    "avoid_regions": ["analog_corner"]
}
```

### Delete Traces

```json
{
    "command": "delete_trace",
    "net": "GND",
    "near": [50.0, 30.0],
    "radius": 2.0,
    "reason": "fixing short circuit"
}
```

### Place Component

```json
{
    "command": "place_component",
    "ref": "U2",
    "at": [50.0, 30.0],
    "rotation": 90
}
```

## Example Script

See `route_with_llm.py` for a complete example that:
1. Loads a PCB
2. Runs a reasoning loop with LLM calls
3. Handles failures with diagnosis feedback
4. Saves the result and routing history

```bash
# Set your LLM API key
export OPENAI_API_KEY="your-key-here"

# Run the example
python route_with_llm.py board.kicad_pcb
```

## State Format

The `--export-state` option exports:

```json
{
    "pcb_file": "board.kicad_pcb",
    "outline": {"width": 100.0, "height": 80.0},
    "components": {
        "U1": {
            "x": 50.0, "y": 40.0,
            "rotation": 0,
            "footprint": "QFP-48",
            "pads": [
                {"name": "1", "x": 45.0, "y": 35.0, "net": "VCC"},
                ...
            ]
        },
        ...
    },
    "nets": {
        "routed": [{"name": "GND", "pad_count": 12}],
        "unrouted": [{"name": "SCL", "pad_count": 2, "priority": 1}]
    },
    "violations": [...],
    "prompt": "## Progress\nNets routed: 5/20..."
}
```

## Tips for LLM Integration

1. **Use the prompt**: The `prompt` field is pre-formatted for LLM consumption
2. **Handle failures**: Always process the diagnosis on failure
3. **Iterate**: Complex boards may need multiple routing attempts
4. **Prioritize**: Route critical nets (clocks, power) first
5. **Check violations**: Monitor for shorts and clearance issues
