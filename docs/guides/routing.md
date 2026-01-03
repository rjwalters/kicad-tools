# Routing Guide

This guide covers using kicad-tools autorouter for PCB trace routing.

---

## Overview

kicad-tools includes an A* pathfinding-based autorouter that supports:
- Single-layer and multi-layer routing
- Differential pair routing
- Length matching
- Zone (copper pour) awareness
- Obstacle avoidance

---

## CLI Usage

### Basic Routing

```bash
# Route all nets
kct route board.kicad_pcb -o routed.kicad_pcb

# Route specific net
kct route board.kicad_pcb --net CLK -o routed.kicad_pcb

# With custom trace width
kct route board.kicad_pcb --width 0.25 -o routed.kicad_pcb
```

### Design Rules

```bash
# Apply manufacturer rules
kct route board.kicad_pcb --mfr jlcpcb

# Custom clearance
kct route board.kicad_pcb --clearance 0.15 --via-size 0.6
```

---

## Python API

### Basic Routing

```python
from kicad_tools.router import Autorouter, DesignRules

# Load PCB
router = Autorouter.from_pcb("board.kicad_pcb")

# Route all nets
result = router.route_all()

print(f"Routed: {result.routed_nets}/{result.total_nets}")
print(f"Vias used: {result.via_count}")

# Save result
router.save("routed.kicad_pcb")
```

### Custom Design Rules

```python
from kicad_tools.router import DesignRules

rules = DesignRules(
    trace_width=0.2,        # mm
    clearance=0.15,         # mm
    via_drill=0.3,          # mm
    via_diameter=0.6,       # mm
    grid_resolution=0.25,   # Routing grid
)

router = Autorouter.from_pcb("board.kicad_pcb", rules=rules)
result = router.route_all()
```

### Layer Configuration

```python
# 2-layer board
router.set_layers(["F.Cu", "B.Cu"])

# 4-layer with inner layers
router.set_layers(["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])

# Prefer top layer for signals
router.set_layer_preference("F.Cu", weight=2.0)
```

---

## Routing Strategies

### Net Priority

Route critical nets first:

```python
# Set priorities (higher = first)
router.set_priority("CLK", 100)
router.set_priority("DATA*", 50)  # Wildcards supported
router.set_priority("GND", 10)

# Route in priority order
result = router.route_all()
```

### Net Classes

Apply different rules to net classes:

```python
# Power nets: wider traces
router.set_net_class_rules("Power", trace_width=0.5)

# High-speed: tighter clearance
router.set_net_class_rules("HighSpeed", clearance=0.2)
```

---

## Differential Pairs

### Automatic Detection

```python
# Detect pairs by naming convention (DATA_P/DATA_N)
router.auto_detect_diff_pairs()

# Route as pairs
result = router.route_diff_pairs()
```

### Manual Definition

```python
# Define diff pair
router.add_diff_pair("USB_D", "USB_D+", "USB_D-",
    spacing=0.15,           # Pair spacing
    trace_width=0.2,
    impedance=90,           # Target impedance
)

# Route it
router.route_net("USB_D")
```

### Length Matching

```python
# Match lengths within tolerance
router.set_length_match("USB_D+", "USB_D-", tolerance=0.5)  # mm

# Add serpentine if needed
router.enable_serpentine(min_amplitude=0.5, spacing=0.3)
```

---

## Zone Awareness

Handle copper pour zones:

```python
# Respect existing zones
router.set_zone_mode("avoid")  # Route around zones

# Or route through (zones will pour around traces)
router.set_zone_mode("through")

# Thermal relief for pads in zones
router.enable_thermal_relief(spoke_width=0.3, gap=0.3)
```

---

## Via Management

```python
# Limit via count
router.set_max_vias_per_net(4)

# Via-in-pad (for BGA)
router.enable_via_in_pad(components=["U1"])

# Blind/buried vias (4+ layers)
router.enable_blind_vias(top_layer="F.Cu", bottom_layer="In1.Cu")
```

---

## Routing Quality

### Optimization

```python
# After initial routing, optimize
router.optimize_traces(
    straighten=True,        # Remove unnecessary bends
    minimize_length=True,   # Shorten traces
    minimize_vias=True,     # Reduce via count
)
```

### Length Reports

```python
# Get trace lengths
lengths = router.get_trace_lengths()

for net, length in lengths.items():
    print(f"{net}: {length:.2f}mm")

# Check for length violations
violations = router.check_length_constraints()
```

---

## Incremental Routing

Route nets one at a time:

```python
# Route one net
result = router.route_net("CLK")

if result.success:
    print(f"Routed CLK: {result.length:.2f}mm, {result.vias} vias")
else:
    print(f"Failed: {result.failure_reason}")

# Undo if needed
router.unroute_net("CLK")
```

---

## LLM-Driven Routing

Use the reasoning module for AI-assisted routing:

```python
from kicad_tools import PCBReasoningAgent

agent = PCBReasoningAgent.from_pcb("board.kicad_pcb")

while not agent.is_complete():
    # Get state for LLM
    prompt = agent.get_prompt()

    # LLM decides what to route next
    command = your_llm(prompt)  # e.g., "ROUTE CLK"

    # Execute
    result, diagnosis = agent.execute(command)

agent.save("routed.kicad_pcb")
```

See [LLM Routing Example](https://github.com/rjwalters/kicad-tools/tree/main/examples/llm-routing).

---

## Troubleshooting

### Unroutable Nets

```python
result = router.route_all()

for net in result.failed_nets:
    print(f"Failed: {net}")
    diagnosis = router.diagnose_failure(net)
    print(f"  Reason: {diagnosis.reason}")
    print(f"  Suggestion: {diagnosis.suggestion}")
```

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| "No path found" | Blocked by components | Improve placement |
| "Clearance violation" | Too tight | Increase clearance or use smaller traces |
| "Via limit exceeded" | Complex routing | Allow more vias or add layers |
| "Length mismatch" | Serpentine needed | Enable serpentine routing |

---

## Performance

For large boards:

```python
# Use progress callback
from kicad_tools import create_print_callback

result = router.route_all(progress=create_print_callback())

# Route in parallel (experimental)
router.set_parallel(threads=4)
```

---

## See Also

- [Placement Optimization Guide](placement-optimization.md)
- [DRC & Validation Guide](drc-and-validation.md)
- [Example: Autorouter](https://github.com/rjwalters/kicad-tools/tree/main/examples/04-autorouter)
