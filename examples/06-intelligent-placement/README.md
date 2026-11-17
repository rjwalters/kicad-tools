# Intelligent Placement Examples (v0.6.0)

This example demonstrates the intelligent placement features introduced in kicad-tools v0.6.0 for AI agent workflows.

## Overview

The v0.6.0 release adds three key intelligent placement capabilities:

| Feature | Description | Demo Script |
|---------|-------------|-------------|
| **Functional Clustering** | Groups related components (MCU + bypass caps, crystal + load caps) | `clustering_demo.py` |
| **Edge Placement** | Auto-detects connectors/mounting holes for board edge placement | `edge_placement_demo.py` |
| **Thermal Awareness** | Identifies heat sources and sensitive components for proper separation | `thermal_demo.py` |
| **Agent Refinement** | Interactive session API for AI agents to explore placement changes | `agent_session_demo.py` |

## Quick Start

```bash
# Run all demos
uv run python examples/06-intelligent-placement/clustering_demo.py
uv run python examples/06-intelligent-placement/edge_placement_demo.py
uv run python examples/06-intelligent-placement/thermal_demo.py
uv run python examples/06-intelligent-placement/agent_session_demo.py
```

## Functional Clustering

Automatically groups functionally-related components that should be placed together:

```python
from kicad_tools.optim import PlacementOptimizer, detect_functional_clusters
from kicad_tools.schema.pcb import PCB

# Load PCB
pcb = PCB.load("board.kicad_pcb")
optimizer = PlacementOptimizer.from_pcb(pcb)

# Detect functional clusters from netlist analysis
clusters = detect_functional_clusters(optimizer.components)

for cluster in clusters:
    print(f"{cluster.cluster_type.value}: {cluster.anchor} + {cluster.members}")
    # Example: POWER: U1 + ['C1', 'C2']  (MCU with bypass caps)
    # Example: TIMING: U1 + ['Y1', 'C3', 'C4']  (MCU with crystal + load caps)
```

### Cluster Types Detected

- **POWER**: IC + bypass/decoupling capacitors (100nF, 10uF, etc.)
- **TIMING**: Crystal/oscillator + load capacitors
- **INTERFACE**: Connector + ESD protection + series resistors
- **DRIVER**: Driver IC + gate resistors + flyback diodes

## Edge Placement

Automatically identifies components that should be placed at board edges:

```python
from kicad_tools.optim import detect_edge_components, get_board_edges
from kicad_tools.schema.pcb import PCB

pcb = PCB.load("board.kicad_pcb")

# Get board edge geometry
edges = get_board_edges(pcb)
print(f"Board: {edges.top.length:.1f}mm x {edges.left.length:.1f}mm")

# Auto-detect edge components
constraints = detect_edge_components(pcb)
for c in constraints:
    print(f"{c.reference}: edge={c.edge}, slide={c.slide}")
    # Example: J1: edge=any, slide=True  (USB connector)
    # Example: MH1: edge=any, slide=False, corner_priority=True  (mounting hole)
```

### Component Types Detected

- **Connectors**: J*, P*, USB*, DC*, CON* (slide along edge)
- **Mounting Holes**: MH*, H* (corner priority)
- **Test Points**: TP* (edge accessible for probing)
- **Switches**: SW*, BTN* (user-accessible edges)

## Thermal Awareness

Classifies components by thermal behavior to ensure proper separation:

```python
from kicad_tools.optim import (
    classify_thermal_properties,
    detect_thermal_constraints,
    get_thermal_summary,
)
from kicad_tools.schema.pcb import PCB

pcb = PCB.load("board.kicad_pcb")

# Classify all components
thermal_props = classify_thermal_properties(pcb)

# Get summary
summary = get_thermal_summary(thermal_props)
print(f"Heat sources: {summary['heat_sources']}")  # LDO, power MOSFETs
print(f"Heat sensitive: {summary['heat_sensitive']}")  # Crystals, voltage refs

# Generate thermal constraints
constraints = detect_thermal_constraints(pcb, thermal_props)
for c in constraints:
    if c.constraint_type == "min_separation":
        print(f"Keep {c.parameters['heat_source']} away from {c.parameters['sensitive']}")
```

### Thermal Classifications

- **Heat Sources**: LDOs, regulators, MOSFETs, power resistors, power diodes
- **Heat Sensitive**: Crystals, precision voltage references, temperature sensors
- **Neutral**: Most other components

## Agent Refinement Session

Interactive API for AI agents to explore placement changes with query-before-commit semantics:

```python
from kicad_tools.optim import PlacementSession
from kicad_tools.schema.pcb import PCB

pcb = PCB.load("board.kicad_pcb")
session = PlacementSession(pcb)

# Query impact of a hypothetical move (without applying)
result = session.query_move("C1", 45.0, 32.0)
print(f"Score change: {result.score_delta:+.4f}")
print(f"Routing impact: {result.routing_impact.estimated_length_change_mm:+.2f}mm")
print(f"New violations: {len(result.new_violations)}")

# If improvement, apply it
if result.score_delta < 0:  # Negative = improvement
    session.apply_move("C1", 45.0, 32.0)

# Get suggestions for a component
suggestions = session.get_suggestions("C1", num_suggestions=5)
for s in suggestions:
    print(f"  ({s.x:.1f}, {s.y:.1f}): {s.rationale}")

# Commit all changes
session.commit()
pcb.save("optimized.kicad_pcb")
```

### Session API Methods

| Method | Description |
|--------|-------------|
| `query_move(ref, x, y, rot)` | Evaluate move without applying |
| `apply_move(ref, x, y, rot)` | Apply move to pending changes |
| `get_suggestions(ref)` | Get suggested positions |
| `undo()` | Undo last move |
| `commit()` | Apply all pending changes to PCB |
| `rollback()` | Discard all pending changes |

## CLI Commands

The intelligent placement features are also available via CLI:

```bash
# Optimize with all v0.6.0 features
kicad-tools placement optimize board.kicad_pcb \
  --cluster --thermal --edge-detect \
  --output optimized.kicad_pcb

# Get placement suggestions with rationale
kicad-tools placement suggest board.kicad_pcb --format json

# Interactive refinement session
kicad-tools placement refine board.kicad_pcb

# JSON API mode for agent integration
kicad-tools placement refine board.kicad_pcb --json
```

## Fixture Files

The `fixtures/` directory contains a sample PCB demonstrating all features:

- `mcu_board.kicad_pcb` - MCU board with:
  - STM32-style MCU (U1) with VCC/GND pins
  - Bypass capacitors (C1, C2) on power rails
  - Crystal (Y1) with load capacitors (C3, C4)
  - LDO voltage regulator (U2)
  - USB-C connector (J1)
  - Mounting holes (MH1-MH4)

## Release Criteria Verification

This example demonstrates v0.6.0 release criteria:

- [x] Agent can place MCU + bypass caps as a functional group
- [x] Connectors automatically placed at board edges
- [x] Placement rationale available as structured data

## Related Documentation

- [CHANGELOG](../../CHANGELOG.md) - v0.6.0 release notes
- [04-autorouter](../04-autorouter/) - PCB routing examples
- [agent-integration](../agent-integration/) - AI agent tool definitions
