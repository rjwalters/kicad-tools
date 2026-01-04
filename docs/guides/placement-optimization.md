# Placement Optimization Guide

This guide covers using kicad-tools to optimize component placement on PCBs.

---

## Overview

Good component placement is critical for:
- Routability (can traces reach all connections?)
- Signal integrity (short high-speed paths)
- Thermal management (heat dissipation)
- Manufacturing (assembly considerations)

kicad-tools provides placement analysis and optimization tools to help achieve better layouts.

---

## CLI Usage

### Check Placement

```bash
# Check for placement conflicts
kct placement check board.kicad_pcb

# Get detailed report
kct placement check board.kicad_pcb --format json
```

### Get Suggestions

```bash
# Get AI-friendly placement suggestions
kct placement suggestions board.kicad_pcb

# Export for LLM processing
kct placement suggestions board.kicad_pcb --format json > suggestions.json
```

### Optimize Placement

```bash
# Run placement optimization
kct placement optimize board.kicad_pcb -o optimized.kicad_pcb

# With specific focus
kct placement optimize board.kicad_pcb --thermal --grouping
```

---

## Python API

### PlacementSession

Interactive placement refinement:

```python
from kicad_tools.optim import PlacementSession

# Start session
session = PlacementSession.from_pcb("board.kicad_pcb")

# Query a potential move
result = session.query_move("C1", x=10.5, y=20.3)
print(f"Score delta: {result.score_delta}")
print(f"Conflicts: {result.conflicts}")

# Apply if beneficial
if result.score_delta > 0:
    session.apply_move("C1", x=10.5, y=20.3)

# Commit changes
session.commit()
session.save("optimized.kicad_pcb")
```

### PlacementOptimizer

Automatic optimization:

```python
from kicad_tools.optim import PlacementOptimizer

optimizer = PlacementOptimizer.from_pcb("board.kicad_pcb")

# Configure optimization
optimizer.set_weights(
    routing=1.0,      # Prioritize routability
    thermal=0.5,      # Consider thermal
    grouping=0.3,     # Group related components
)

# Run optimization
result = optimizer.optimize(
    iterations=1000,
    temperature=1.0,  # Simulated annealing temperature
)

print(f"Improvement: {result.improvement:.1f}%")
optimizer.save("optimized.kicad_pcb")
```

---

## Placement Scoring

The optimizer uses multiple scoring factors:

| Factor | Description |
|--------|-------------|
| Routing | Estimated routing difficulty (Manhattan distance) |
| Thermal | Heat source proximity to board edges |
| Grouping | Related component clustering |
| Clearance | Component-to-component spacing |
| Alignment | Grid alignment and row/column organization |

### Score Breakdown

```python
# Get current placement score
score = session.get_score()

print(f"Total: {score.total}")
print(f"Routing: {score.routing}")
print(f"Thermal: {score.thermal}")
print(f"Grouping: {score.grouping}")
```

---

## Functional Grouping

Components that work together should be placed together:

```python
# Define groups
optimizer.add_group("power", ["U1", "C1", "C2", "L1"])
optimizer.add_group("usb", ["J1", "R1", "R2", "U2"])

# Optimizer will try to keep groups close
result = optimizer.optimize()
```

---

## Thermal Considerations

Power components should be placed for heat dissipation:

```python
# Mark thermal components
optimizer.set_thermal_components(["U1", "U3", "Q1"])

# Edge preference (heat sinks near board edge)
optimizer.set_thermal_preference("edge")
```

---

## Connector Placement

Connectors typically need specific positions:

```python
# Lock connector positions
session.lock("J1")  # USB connector
session.lock("J2")  # Power jack

# Optimize everything else
session.optimize_unlocked()
```

---

## Integration with Routing

Placement directly affects routing success:

```python
from kicad_tools.optim import PlacementOptimizer
from kicad_tools.router import Autorouter

# Optimize placement
optimizer = PlacementOptimizer.from_pcb("board.kicad_pcb")
optimizer.optimize()
optimizer.save("placed.kicad_pcb")

# Then route
router = Autorouter.from_pcb("placed.kicad_pcb")
result = router.route_all()

print(f"Routed: {result.routed_nets}/{result.total_nets}")
```

---

## Routability Considerations

**IMPORTANT**: Placement optimization focuses on reducing wire length and improving
component grouping, but **may not always improve routability**. A placement that
minimizes total wire length might actually make routing harder by creating congested
areas or blocking routing channels.

### Check Routability Impact

Use `--check-routability` to see how optimization affects routability:

```bash
# See routability before and after optimization
kct placement optimize board.kicad_pcb --check-routability

# Output shows:
#   Before: 95% estimated routability (42 nets, 2 problem nets)
#   After: 87% estimated routability (42 nets, 5 problem nets)
#   WARNING: Routability decreased after placement optimization!
```

### Routing-Aware Mode

For best results, use `--routing-aware` mode which iterates between placement and
routing to find placements that are actually routable:

```bash
# Integrated place-route optimization
kct placement optimize board.kicad_pcb --routing-aware

# This mode:
# 1. Optimizes placement
# 2. Attempts routing
# 3. Adjusts placement if routing fails
# 4. Repeats until convergence
```

### When to Use Each Mode

| Mode | Use When |
|------|----------|
| Default (`--strategy force-directed`) | Quick optimization, will verify routing manually |
| `--check-routability` | Want to see routability impact before/after |
| `--routing-aware` | Need guaranteed routable result, can wait longer |

---

## Best Practices

1. **Place connectors first** - They have physical constraints
2. **Group by function** - Power supply, USB, MCU, etc.
3. **Consider thermal** - Power components near edges or thermal vias
4. **Leave routing channels** - Don't pack too tight
5. **Iterate** - Run placement, try routing, adjust
6. **Check routability** - Use `--check-routability` to verify impact

---

## See Also

- [Routing Guide](routing.md)
- [DRC & Validation Guide](drc-and-validation.md)
- [Example: Intelligent Placement](https://github.com/rjwalters/kicad-tools/tree/main/examples/06-intelligent-placement)
