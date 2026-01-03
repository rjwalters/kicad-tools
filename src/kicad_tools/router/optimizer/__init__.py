"""Trace optimizer for post-routing cleanup.

Provides algorithms to optimize routed traces:
- Collinear segment merging (combine same-direction segments)
- Zigzag elimination (remove unnecessary back-and-forth)
- Staircase compression (compress alternating horizontal/diagonal patterns)
- 45-degree corner conversion (smooth 90-degree turns)

Collision detection is supported to prevent optimizations that would
create DRC violations (shorts, track crossings).

Example::

    from kicad_tools.router import TraceOptimizer, OptimizationConfig

    # Optimize a route in memory (no collision checking)
    optimizer = TraceOptimizer()
    optimized_route = optimizer.optimize_route(route)

    # Optimize with collision checking
    from kicad_tools.router import GridCollisionChecker
    checker = GridCollisionChecker(grid)
    optimizer = TraceOptimizer(collision_checker=checker)
    optimized_route = optimizer.optimize_route(route)

    # Optimize traces in a PCB file
    stats = optimizer.optimize_pcb("board.kicad_pcb", output="optimized.kicad_pcb")
    print(f"Reduced segments from {stats['before']} to {stats['after']}")
"""

from .collision import CollisionChecker, GridCollisionChecker
from .config import OptimizationConfig, OptimizationStats
from .trace import TraceOptimizer

__all__ = [
    "CollisionChecker",
    "GridCollisionChecker",
    "OptimizationConfig",
    "OptimizationStats",
    "TraceOptimizer",
]
