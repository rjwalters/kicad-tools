"""GPU acceleration kernels for kicad-tools.

Contains vectorized kernels for batch operations on GPU.
"""

from kicad_tools.acceleration.kernels.evolutionary import evaluate_population_gpu

__all__ = ["evaluate_population_gpu"]
