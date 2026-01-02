"""
Placement and routing optimization module.

Provides algorithms for component placement optimization:

**Physics-based (force-directed):**
- Charge-based repulsion from board/component outlines
- Spring-based attraction between net-connected pins
- Converges to local minima quickly

**Evolutionary (genetic algorithm):**
- Population-based global search
- Crossover and mutation operators
- Escapes local minima through exploration
- Hybrid mode combines evolutionary + physics

Example (physics-based)::

    from kicad_tools.optim import PlacementOptimizer
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load("board.kicad_pcb")
    optimizer = PlacementOptimizer.from_pcb(pcb)

    # Run simulation
    optimizer.run(iterations=1000, dt=0.01)

    # Get optimized placements
    for comp in optimizer.components:
        print(f"{comp.ref}: ({comp.x:.2f}, {comp.y:.2f}) @ {comp.rotation:.1f}")

Example (evolutionary)::

    from kicad_tools.optim import EvolutionaryPlacementOptimizer
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load("board.kicad_pcb")
    optimizer = EvolutionaryPlacementOptimizer.from_pcb(pcb)

    # Run evolutionary optimization
    best = optimizer.optimize(generations=100, population_size=50)

    # Or use hybrid: evolutionary global search + physics refinement
    physics_opt = optimizer.optimize_hybrid(generations=50)
    physics_opt.write_to_pcb(pcb)
    pcb.save("optimized.kicad_pcb")
"""

from __future__ import annotations

# Geometry primitives
from kicad_tools.optim.geometry import Polygon, Vector2D

# Core data models
from kicad_tools.optim.models import Component, Keepout, Pin, PlacementConfig, Spring

# Physics-based placement optimizer
from kicad_tools.optim.placement import PlacementOptimizer

# Routing optimizer and metrics
from kicad_tools.optim.routing import FigureOfMerit, RoutingOptimizer

# Evolutionary optimizer (in its own module)
from kicad_tools.optim.evolutionary import (
    EvolutionaryConfig,
    EvolutionaryPlacementOptimizer,
    Individual,
)

__all__ = [
    # Geometry
    "Vector2D",
    "Polygon",
    # Models
    "Pin",
    "Component",
    "Spring",
    "Keepout",
    "PlacementConfig",
    # Physics-based placement
    "PlacementOptimizer",
    # Routing
    "FigureOfMerit",
    "RoutingOptimizer",
    # Evolutionary
    "EvolutionaryConfig",
    "EvolutionaryPlacementOptimizer",
    "Individual",
]
