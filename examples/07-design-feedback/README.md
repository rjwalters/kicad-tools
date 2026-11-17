# Design Feedback Examples (v0.7.0)

This directory contains examples demonstrating v0.7.0's design feedback features.
These tools help agents understand failures and iterate on designs with actionable guidance.

## What's New in v0.7.0

v0.7.0 focuses on **actionable feedback**. Instead of just error codes, agents get
specific suggestions like "Move C1 0.5mm left to clear U1 pad" or "Route congested
around U1 pins 4-7, consider moving bypass caps".

## Examples

### Congestion Analysis Demo (`congestion_demo.py`)

Analyzes routing density to identify hotspots:
- Grid-based density heatmap
- Track length per unit area
- Via clustering detection
- Actionable suggestions (move components, use inner layers)

```bash
python congestion_demo.py
# Or via CLI:
kct analyze congestion board.kicad_pcb
```

### Thermal Analysis Demo (`thermal_demo.py`)

Identifies thermal issues before they become problems:
- Heat source identification
- Power dissipation estimation
- Temperature rise prediction
- Thermal via effectiveness

```bash
python thermal_demo.py
# Or via CLI:
kct analyze thermal board.kicad_pcb
```

### Cost Estimation Demo (`cost_demo.py`)

Manufacturing cost visibility:
- PCB fabrication costs by manufacturer
- Component pricing from LCSC
- Assembly costs
- Alternative part suggestions

```bash
python cost_demo.py
# Or via CLI:
kct estimate cost board.kicad_pcb --bom schematic.kicad_sch
kct parts availability schematic.kicad_sch
kct suggest alternatives schematic.kicad_sch
```

## CLI Commands Summary

All v0.7.0 features are accessible via CLI:

```bash
# Error analysis
kct erc explain schematic.kicad_sch       # Root cause analysis for ERC errors

# Design quality metrics
kct analyze congestion board.kicad_pcb    # Routing hotspots
kct analyze trace-lengths board.kicad_pcb # Timing-critical traces
kct analyze thermal board.kicad_pcb       # Thermal issues
kct analyze signal-integrity board.kicad_pcb # Crosstalk/impedance

# Constraint checking
kct constraints check board.kicad_pcb     # Detect conflicts

# Cross-domain validation
kct validate --consistency                # Schematic↔PCB sync
kct validate --connectivity               # Net connectivity

# Cost awareness
kct estimate cost board.kicad_pcb         # Manufacturing costs
kct parts availability schematic.kicad_sch # LCSC stock levels
kct suggest alternatives schematic.kicad_sch # Part alternatives
```

## Fixtures

No sample KiCad files ship with these demos, and the `fixtures/` directory
the scripts look in does not exist until you create it. Each demo falls back to
a built-in synthetic example when its input is missing. To analyze a real
design, create `fixtures/` next to the scripts and copy your own project files
(or the examples from other directories) in under the names the scripts load:
`fixtures/dense_board.kicad_pcb` (`congestion_demo.py`),
`fixtures/power_board.kicad_pcb` (`thermal_demo.py`), and
`fixtures/project.kicad_pcb` + `fixtures/project.kicad_sch` (`cost_demo.py`).

## Agent Integration

These features are designed for AI agent workflows. Example integration:

```python
from kicad_tools.analysis import analyze_congestion, analyze_thermal
from kicad_tools.cost import estimate_manufacturing_cost

# Analyze design before routing
congestion = analyze_congestion(pcb, grid_size=2.0)
for hotspot in congestion.hotspots:
    if hotspot.severity >= Severity.HIGH:
        print(f"Warning: {hotspot.suggestion}")

# Check thermal after placement
thermal = analyze_thermal(pcb)
for source in thermal.heat_sources:
    if source.estimated_temp_rise > 40:
        print(f"Critical: {source.reference} may overheat")

# Estimate costs before ordering
cost = estimate_manufacturing_cost(pcb, bom, quantity=10)
print(f"Total per board: ${cost.total_per_board:.2f}")
```
