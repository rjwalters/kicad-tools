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
        print(f"{comp.ref}: ({comp.x:.2f}, {comp.y:.2f}) @ {comp.rotation:.1f} deg")

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

from kicad_tools.optim.clustering import ClusterDetector, detect_functional_clusters
from kicad_tools.optim.components import (
    ClusterType,
    Component,
    FunctionalCluster,
    Keepout,
    Pin,
    Spring,
)
from kicad_tools.optim.config import PlacementConfig
from kicad_tools.optim.evolutionary import (
    EvolutionaryConfig,
    EvolutionaryPlacementOptimizer,
    Individual,
)
from kicad_tools.optim.geometry import Polygon, Vector2D
from kicad_tools.optim.placement import PlacementOptimizer
from kicad_tools.optim.routing import FigureOfMerit, RoutingOptimizer

__all__ = [
    "PlacementOptimizer",
    "EvolutionaryPlacementOptimizer",
    "RoutingOptimizer",
    "FigureOfMerit",
    "Vector2D",
    "Polygon",
    "Component",
    "Spring",
    "Keepout",
    "PlacementConfig",
    "EvolutionaryConfig",
    "Individual",
    "Pin",
    "FunctionalCluster",
    "ClusterType",
    "ClusterDetector",
    "detect_functional_clusters",
]
