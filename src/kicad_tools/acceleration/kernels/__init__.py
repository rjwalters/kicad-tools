"""GPU kernels for kicad-tools acceleration.

This package contains GPU-accelerated implementations of computationally
intensive algorithms.
"""

from kicad_tools.acceleration.kernels.evolutionary import evaluate_population_gpu
from kicad_tools.acceleration.kernels.placement import (
    PlacementGPUAccelerator,
    compute_pairwise_repulsion_gpu,
)
from kicad_tools.acceleration.kernels.routing import (
    BatchPathfinder,
    BatchRouteRequest,
    BatchRouteResult,
    batch_heuristic_gpu,
    compute_batch_costs_gpu,
)

__all__ = [
    "BatchPathfinder",
    "BatchRouteRequest",
    "BatchRouteResult",
    "batch_heuristic_gpu",
    "compute_batch_costs_gpu",
    "compute_pairwise_repulsion_gpu",
    "evaluate_population_gpu",
    "PlacementGPUAccelerator",
]
