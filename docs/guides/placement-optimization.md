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

#### Per-axis breakdown for infeasible placements

`kct optimize-placement` used to print a single `Improvement: 0.0%` line
when the cost landed in the infeasible regime (≈ 1e9 saturates the percentage
arithmetic). It now emits a **per-axis breakdown** instead — separate
contributions for `overlap`, `drc`, `boundary`, `wirelength`, and `area` —
so you can see which constraint is keeping the placement infeasible without
re-running with `-v`. No new flag is required (PR #2829); this is purely a
change to the output format. Treat any non-zero `overlap`/`drc`/`boundary`
component as a feasibility gate trigger (see below).

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

## Anchoring Perimeter Footprints

`kct optimize-placement --anchor-weight FLOAT` biases the CMA-ES cost
function toward keeping nets that touch **locked** footprints short. Each
qualifying net's HPWL contribution is scaled by:

```
HPWL_net * (1 + anchor_weight * (anchored_pins / total_pins))
```

So a net with 1 of 4 pins on a locked footprint at `--anchor-weight 4.0`
contributes `1 + 4 * 0.25 = 2.0x` its normal HPWL — the optimizer prefers
placements that keep that net compact even when overall wirelength is no
better.

The `--help` text quotes a starting range:

```text
--anchor-weight FLOAT
                      Per-net wirelength multiplier boost for nets that
                      touch footprints carrying the KiCad (locked)
                      attribute. Each qualifying net's HPWL is scaled by 1 +
                      anchor_weight * (anchored_pins / total_pins). Default
                      0.0 preserves uniform weighting; recommended starting
                      range is 2.0..5.0 to keep perimeter-anchored signals
                      (connectors, edge sense FETs) from being starved.
```

> **Validated recipe vs. help text.** The help text recommends `2.0 .. 5.0`.
> The validated recipe that took board-05 BLDC from 16/40 (40%) to 24/40 (60%)
> routing completion uses **`--anchor-weight 1.0`** with only the perimeter
> footprints locked plus `--allow-infeasible`. Higher anchor weights with
> more locks (e.g. all sense FETs) over-constrain the optimizer and can make
> things dramatically worse (2.5× total wirelength in the failure case).
> Use the recipe below as the floor; reach for higher weights only when
> perimeter signals are still being starved.

### Validated recipe (from `project_anchor_weight_recipe.md`)

Recipe that lifted board 05 BLDC from 16/40 (40%) baseline to **24/40 (60%)**
completed nets:

```bash
# 1. Lock ONLY perimeter-anchored footprints (connectors, edge ICs). NOT central parts.
python -c "
from kicad_tools.schema.pcb import PCB
pcb = PCB.load('board.kicad_pcb')
for fp in pcb.footprints:
    if fp.reference in {'J3', 'U10'}:  # Hall connector + hall sensor IC only
        fp.locked = True
pcb.save('board.kicad_pcb')  # Requires PR #2830 (2026-05-13) — Footprint.locked roundtrips
"

# 2. Optimize with moderate anchor weight, accept infeasibility (boundary may be > 0)
kct optimize-placement board.kicad_pcb --output optim.kicad_pcb \
  --anchor-weight 1.0 --max-iterations 400 --time-budget 120 --allow-infeasible

# 3. Route with full layer escalation
kct route optim.kicad_pcb --output routed.kicad_pcb \
  --mfr jlcpcb-tier1 --timeout 1500 --auto-layers --auto-fix
```

**Why:**

- `--anchor-weight 3.0` with all sense-FETs locked (5 anchors) **decreased**
  completion to 9/40 — over-constrained the optimizer to push parts apart
  (wirelength 2608 → 6699 = 2.5× worse).
- `--anchor-weight 1.0` with just 2 perimeter parts locked recovered
  HALL_B/C/NRST (which the unweighted optim lost) without distorting topology.
- `--allow-infeasible` needed because the seed placement often has overlap
  > 0 already; the optimizer makes it dramatically better but residual
  boundary violations are usually routable.
- Sense FETs (Q2/Q4/Q6) are central to topology — never lock them; the
  optimizer needs freedom to move them.

**How to apply:**

- Identify perimeter-anchored footprints: connectors (`J*`), edge ICs,
  mounting holes. **Not** central FETs or passives.
- Start with `--anchor-weight 1.0`; only raise if perimeter signals still
  lose.
- If the optimizer reports `boundary=X` but `overlap`/`drc`=0, that's
  usually acceptable for routing.

See [Python API → Footprint attributes](../reference/api.md#footprint-attributes-locked-dnp-exclude_from)
for the locked / dnp / exclude_from_* round-trip semantics that make this
recipe possible.

---

## Feasibility-Gated Convergence

Since issue #2821, the CMA-ES loop **refuses to declare convergence while
the best-known placement is still infeasible**. When the loop times out or
hits `--max-iterations` with the best candidate still showing
overlap / DRC / boundary violations, the command exits **1** with:

```text
FATAL: optimizer exited with infeasible placement (overlap=2, drc=0, boundary=3.4mm)
```

This prevents downstream pipelines from silently handing the router an
illegal placement. Three knobs control the trade-off:

| Flag | Effect |
|------|--------|
| `--allow-infeasible` | Exit 0 anyway. Use when the next step (router) can absorb residual boundary violations. |
| `--time-budget SEC` | Hard wall-clock cap. Bounds the "keep going past plateau while infeasible" loop. |
| `--max-iterations N` | Iteration cap. The loop will still exit cleanly at convergence; this bounds the *infeasible* tail. |

Exit codes for `optimize-placement`:

| Code | Meaning |
|------|---------|
| 0 | Converged to a feasible placement (or `--allow-infeasible` was set) |
| 1 | Final placement infeasible — overlap / DRC / boundary violations remain |
| 2 | Invalid arguments |
| 130 | Interrupted (Ctrl+C) |

See also [CLI Reference → Exit Codes](../reference/cli.md#kct-optimize-placement).

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
