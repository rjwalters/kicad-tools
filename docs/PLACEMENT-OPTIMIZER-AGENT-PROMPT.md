# Physics-Based Placement Optimizer - Continuation Prompt

## Context

You are continuing development of a physics-based component placement optimizer in the `kicad-tools` Python package. The foundation has been implemented in `/src/kicad_tools/optim/__init__.py`.

## What Has Been Built

A force-directed placement simulator using:

1. **Edge-to-edge charge repulsion** - All component outlines and board edges have linear charge density (λ). Forces computed by sampling along edges with 1/r falloff. Components and board edges repel each other.

2. **Spring attraction for nets** - Net connections modeled as springs between connected pins using Hooke's law (F = -kx). Different stiffness for power/clock/signal nets.

3. **Rotation torsion potential** - Energy function E(θ) = -k·cos(4θ) creates energy wells at 0°, 90°, 180°, 270°. Components naturally settle into cardinal orientations.

4. **Proper pin tracking** - Pin positions stored as relative offsets and updated correctly when components translate and rotate.

### Current API

```python
from kicad_tools.optim import PlacementOptimizer, PlacementConfig, Polygon, Component, Pin

# Define board outline
board = Polygon.rectangle(center_x, center_y, width, height)

# Configure physics parameters
config = PlacementConfig(
    charge_density=30.0,       # Edge-to-edge repulsion strength
    spring_stiffness=10.0,     # Net attraction (Hooke's law k)
    boundary_charge=80.0,      # Board edge repulsion multiplier
    rotation_stiffness=100.0,  # Torsion spring for 90° alignment
    edge_samples=3,            # Samples per edge for force integration
    damping=0.92,              # Linear velocity damping
    angular_damping=0.75,      # Rotational velocity damping
)

optimizer = PlacementOptimizer(board, config)
optimizer.add_component(Component(...))
optimizer.create_springs_from_nets()
optimizer.run(iterations=2000, dt=0.015)
optimizer.snap_rotations_to_90()  # Force exact cardinal alignment
```

### Test Results

- Wire length reduction: ~50% in synthetic tests
- Components cluster based on net connectivity
- Rotations converge toward 90° slots
- Board edges successfully contain components

## What Needs To Be Built

### Priority 1: KiCad PCB File Integration

The optimizer currently works with synthetic Component objects. It needs to:

1. **Read from .kicad_pcb files** - Extract footprint positions, outlines (courtyards), pad locations, and net assignments. The `kicad_tools.schema.pcb.PCB` class already parses these files.

2. **Write back to .kicad_pcb files** - Update footprint positions and rotations after optimization. Need to modify the S-expression AST and write it back.

3. **Extract actual component outlines** - Use courtyard or fab layer polygons instead of bounding boxes. Handle non-rectangular components.

```python
# Target API
from kicad_tools.optim import PlacementOptimizer
from kicad_tools.schema.pcb import PCB

pcb = PCB.load("board.kicad_pcb")
optimizer = PlacementOptimizer.from_pcb(pcb)  # Already partially implemented
optimizer.run(iterations=2000)
optimizer.write_to_pcb(pcb)  # Needs implementation
pcb.save("board-optimized.kicad_pcb")
```

### Priority 2: Fixed Components and Keepout Zones

Real PCBs have placement constraints:

1. **Fixed components** - Connectors, mounting holes, and reference components that cannot move. Set `component.fixed = True`.

2. **Keepout zones** - Areas where components cannot be placed (mounting holes, board edge clearances, specific exclusion zones). Add as charged polygons with infinite/very high charge.

3. **Edge-locked components** - Connectors that must remain at board edges but can slide along the edge.

```python
# Mark GPIO header as fixed
gpio_header = optimizer.get_component("J1")
gpio_header.fixed = True

# Add keepout for mounting hole
mounting_hole = Polygon.circle(x, y, radius=3.5)
optimizer.add_keepout(mounting_hole, charge_multiplier=10.0)
```

### Priority 3: Grid Snapping

PCB placement typically uses a grid (0.5mm, 0.25mm, or 0.1mm):

1. **Post-optimization snap** - Round final positions to nearest grid point
2. **Grid-aware physics** - Optional: quantize forces or positions during simulation
3. **Rotation grid** - Already have 90° snapping; may want 45° option

### Priority 4: Design Rule Awareness

1. **Courtyard clearances** - Use actual courtyard polygons for collision, not bounding boxes
2. **Component-specific rules** - Decoupling caps within Xmm of IC power pins
3. **Thermal considerations** - Keep hot components (regulators) away from sensitive parts

### Priority 5: Performance Optimization

Current implementation is O(n² × e²) where n=components and e=edges per component:

1. **Spatial indexing** - Use quadtree/R-tree for nearby component queries
2. **Reduced sampling** - Adaptive edge sampling based on distance
3. **NumPy vectorization** - Batch force calculations for speed

## Files to Understand

```
/src/kicad_tools/optim/__init__.py    # Main implementation (900+ lines)
/src/kicad_tools/schema/pcb.py        # PCB file parsing (Footprint, Pad, etc.)
/src/kicad_tools/router/primitives.py # Point, Pad, Obstacle classes
/src/kicad_tools/router/io.py         # load_pcb_for_routing() for reference
```

## Physics Model Details

### Charge Force (edge to point)
```
F = (λ × L / r) × r̂
```
Where λ = charge density, L = edge length, r = distance to nearest point on edge, r̂ = unit vector away from edge.

### Edge-to-Edge Force
Discretize receiving edge into samples, compute force on each sample from source edge, sum forces. Also computes torque about edge center.

### Spring Force (Hooke's Law)
```
F = k × (|p2 - p1| - rest_length) × (p2 - p1)/|p2 - p1|
```
Where k = spring stiffness, rest_length typically 0 for nets.

### Rotation Potential (Torsion Spring)
```
E(θ) = k × (1 - cos(4θ))
τ = -dE/dθ = -4k × sin(4θ)
```
Creates energy wells at 0°, 90°, 180°, 270°.

## Testing

Run the existing test:
```bash
cd /path/to/kicad-tools
PYTHONPATH=src python3 -c "
from kicad_tools.optim import PlacementOptimizer, PlacementConfig, Polygon, Component, Pin

board = Polygon.rectangle(100, 100, 65, 56)
config = PlacementConfig(charge_density=30, spring_stiffness=10, rotation_stiffness=100)
opt = PlacementOptimizer(board, config)

# Add test components and run
# ... (see examples in optim module docstring)
"
```

## Success Criteria

1. Can load a real .kicad_pcb, optimize placement, and write back valid PCB file
2. Respects fixed components (connectors stay in place)
3. Respects keepout zones (no components in mounting holes)
4. Produces DRC-clean placement (no courtyard overlaps)
5. Reduces total wire length compared to initial placement
6. All rotations at cardinal angles (0°, 90°, 180°, 270°)

## Repository

- Main repo: `/Users/rwalters/GitHub/kicad-tools`
- This is a PyPI package (`kicad-tools`) for KiCad automation
- Uses S-expression parsing via `kicad_tools.sexp` module
- Follow existing code style (dataclasses, type hints, docstrings)
