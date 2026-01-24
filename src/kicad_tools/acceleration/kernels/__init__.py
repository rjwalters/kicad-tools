"""GPU kernels for kicad-tools acceleration.

This package contains GPU-accelerated implementations of computationally
intensive algorithms.
"""

from kicad_tools.acceleration.kernels.placement import (
    compute_pairwise_repulsion_gpu,
    PlacementGPUAccelerator,
)

__all__ = [
    "compute_pairwise_repulsion_gpu",
    "PlacementGPUAccelerator",
]
