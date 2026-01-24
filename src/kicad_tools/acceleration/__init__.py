"""GPU acceleration for kicad-tools computations.

Provides backend abstraction for GPU-accelerated operations using either
CUDA (NVIDIA) or Metal (Apple Silicon), with automatic fallback to CPU.

Example::

    from kicad_tools.acceleration import get_backend, should_use_gpu
    from kicad_tools.performance import PerformanceConfig

    config = PerformanceConfig.load_calibrated()

    # Check if GPU should be used for this problem size
    if should_use_gpu(config, n_trace_pairs=500, problem_type="signal_integrity"):
        backend = get_backend(config)
        # Use backend for vectorized operations
        result = backend.array(data)
    else:
        # Use CPU (NumPy)
        import numpy as np
        result = np.array(data)
"""

from __future__ import annotations

from .config import (
    ArrayBackend,
    GpuBackend,
    GpuConfig,
    GpuThresholds,
    ProblemType,
    get_backend,
    should_use_gpu,
)

__all__ = [
    "ArrayBackend",
    "GpuBackend",
    "GpuConfig",
    "GpuThresholds",
    "ProblemType",
    "get_backend",
    "should_use_gpu",
]
