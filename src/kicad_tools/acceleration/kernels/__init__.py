"""GPU kernels for kicad-tools acceleration.

This package contains GPU-accelerated implementations of computationally
intensive algorithms.
"""

from kicad_tools.acceleration.kernels.evolutionary import evaluate_population_gpu
from kicad_tools.acceleration.kernels.placement import (
    PlacementGPUAccelerator,
    compute_pairwise_repulsion_gpu,
)

__all__ = [
    "compute_pairwise_repulsion_gpu",
    "evaluate_population_gpu",
    "PlacementGPUAccelerator",
]
