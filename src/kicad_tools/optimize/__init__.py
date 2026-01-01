"""Integrated optimization module.

Provides high-level optimization loops that coordinate multiple tools:
- PlaceRouteOptimizer: Iterates between placement, routing, and DRC checking

Example:
    >>> from kicad_tools.optimize import PlaceRouteOptimizer
    >>> from kicad_tools.schema.pcb import PCB
    >>>
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> optimizer = PlaceRouteOptimizer.from_pcb(pcb, manufacturer="jlcpcb")
    >>> result = optimizer.optimize(max_iterations=10)
    >>>
    >>> if result.success:
    ...     print(f"Optimization converged in {result.iterations} iterations")
    ... else:
    ...     print(f"Failed: {result.message}")
"""

from .place_route import OptimizationResult, PlaceRouteOptimizer

__all__ = [
    "OptimizationResult",
    "PlaceRouteOptimizer",
]
