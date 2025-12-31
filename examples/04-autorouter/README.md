# kicad-tools Demo Directory

This directory contains example PCB designs demonstrating the autorouting and placement optimization capabilities of kicad-tools.

## Demos

| Demo | Components | Nets | Description |
|------|------------|------|-------------|
| [charlieplex_led_grid](charlieplex_led_grid/) | 14 | 8 | 3x3 LED matrix with charlieplex driving |
| [usb_joystick](usb_joystick/) | 12 | 13 | USB game controller with analog joystick |

## Quick Start

```bash
# Generate and route charlieplex demo
cd charlieplex_led_grid
python generate_pcb.py
python route_demo.py

# Generate and route USB joystick demo
cd ../usb_joystick
python generate_pcb.py
python route_demo.py
```

## Capabilities Demonstrated

### 1. Placement Optimization (Force-Directed Physics)

The `PlacementOptimizer` uses physics simulation to optimize component placement:

- **Electrostatic repulsion**: Components repel each other to prevent overlap
- **Spring forces**: Connected pins attract to minimize wire length
- **Rotation potential**: Components snap to 90° orientations
- **Boundary forces**: Components stay inside board outline

```python
from kicad_tools.optim import PlacementOptimizer
from kicad_tools.schema.pcb import PCB

pcb = PCB.load("board.kicad_pcb")
optimizer = PlacementOptimizer.from_pcb(pcb)
optimizer.run(iterations=1000, dt=0.02)
optimizer.snap_to_grid(position_grid=0.25, rotation_grid=90.0)
```

### 2. Autorouting Strategies

Multiple routing strategies are available with different trade-offs:

| Strategy | Description | Best For |
|----------|-------------|----------|
| `route_all()` | Basic A* pathfinding | Simple boards, fast routing |
| `route_all_negotiated()` | Rip-up and reroute with congestion awareness | Dense boards with conflicts |
| `route_all_monte_carlo()` | Try multiple net orderings, pick best | Optimal via count and length |

## Benchmark Results

### Routing Strategy Comparison

**Charlieplex LED Grid (8 nets):**

| Strategy | Routed | Vias | Length |
|----------|--------|------|--------|
| Basic | 8/8 | 10 | 291.5mm |
| Negotiated Congestion | 8/8 | 20 | 371.0mm |
| Monte Carlo (5 trials) | 8/8 | 10 | 291.5mm |
| **Monte Carlo (10 trials)** | **8/8** | **8** | **258.5mm** |

**USB Joystick (13 nets):**

| Strategy | Routed | Vias | Length |
|----------|--------|------|--------|
| Basic | 13/13 | 7 | 281.3mm |
| Negotiated Congestion | 13/13 | 11 | 281.3mm |
| **Monte Carlo (5 trials)** | **13/13** | **5** | **279.3mm** |
| Monte Carlo (10 trials) | 13/13 | 5 | 279.3mm |

### Placement Optimization Results

**Charlieplex LED Grid:**
- Random placement wire length: 493mm
- Optimized wire length: 316mm
- **Improvement: 36%**

**USB Joystick:**
- Manual placement was already optimal
- Optimization verified placement quality

## Key Lessons Learned

### Grid Resolution Matters

For dense components like TQFP packages (0.8mm pitch), use fine grid resolution:

```python
# Too coarse - routes fail
rules = DesignRules(grid_resolution=0.25)  # Only 3-4 nets route

# Fine enough for QFP
rules = DesignRules(grid_resolution=0.1)   # All 13 nets route
```

### Pin Assignment Affects Routability

Place signals near their destinations:
- USB signals on MCU side facing USB connector
- Crystal pins near crystal component
- Power pins distributed for decoupling

### Connector Spacing

Dense connectors (USB-C) need adequate row spacing for routing:
```python
# Too tight - traces can't fit between rows
py = 0 if pin.startswith("A") else 0.7  # 0.7mm spacing

# Better - room for traces
py = 0 if pin.startswith("A") else 1.0  # 1.0mm spacing
```

## Test Scripts

| Script | Purpose |
|--------|---------|
| `test_routing_strategies.py` | Compare all routing strategies |
| `test_placement_vs_routing.py` | Compare placement and routing |
| `debug_usb_routing.py` | Debug routing failures |

Run the comparison:
```bash
python test_routing_strategies.py
python test_placement_vs_routing.py
```

## Architecture

```
kicad-tools/
├── src/kicad_tools/
│   ├── optim/          # Placement optimization (force-directed physics)
│   ├── router/         # Autorouting (A*, negotiated, Monte Carlo)
│   │   ├── core.py     # Autorouter class
│   │   ├── pathfinder.py # A* pathfinding
│   │   ├── grid.py     # Routing grid
│   │   └── heuristics.py # Routing cost functions
│   └── schema/         # PCB data models
└── demo/
    ├── charlieplex_led_grid/
    ├── usb_joystick/
    └── test_*.py       # Comparison scripts
```

## API Reference

### PlacementOptimizer

```python
from kicad_tools.optim import PlacementOptimizer, PlacementConfig

config = PlacementConfig(
    charge_density=100.0,      # Repulsion strength
    spring_stiffness=10.0,     # Net attraction
    damping=0.95,              # Velocity decay
    rotation_stiffness=10.0,   # 90° alignment force
)

optimizer = PlacementOptimizer.from_pcb(pcb, config=config)
optimizer.run(iterations=1000, dt=0.02)
wire_length = optimizer.total_wire_length()
```

### Autorouter

```python
from kicad_tools.router import load_pcb_for_routing, DesignRules

rules = DesignRules(
    grid_resolution=0.1,   # Routing grid (mm)
    trace_width=0.2,       # Trace width (mm)
    trace_clearance=0.15,  # Min clearance (mm)
    via_drill=0.3,         # Via hole (mm)
    via_diameter=0.6,      # Via pad (mm)
)

router, net_map = load_pcb_for_routing(
    "board.kicad_pcb",
    skip_nets=["VCC", "GND"],
    rules=rules,
)

# Choose strategy
router.route_all()                          # Basic
router.route_all_negotiated(max_iterations=5)  # Congestion-aware
router.route_all_monte_carlo(num_trials=10)    # Multi-start

stats = router.get_statistics()
print(f"Routed {stats['nets_routed']} nets with {stats['vias']} vias")
```
