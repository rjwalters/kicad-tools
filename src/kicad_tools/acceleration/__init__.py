"""GPU acceleration module for kicad-tools.

Provides GPU configuration helpers and backend abstraction for
hardware-accelerated routing and placement operations.

Key Components:
- ArrayBackend: Unified interface for CPU/CUDA/Metal array operations
- GPUArrayPool: Reusable array pool to reduce allocation overhead
- scatter_add: GPU-native scatter-add for efficient accumulation

The ArrayBackend.scatter_add() method is the key optimization for
eliminating CPU-GPU memory transfers in inner loops. See issue #1052.
"""

from kicad_tools.acceleration.backend import (
    ArrayBackend,
    BackendType,
    GPUArrayPool,
    check_memory_available,
    detect_backend,
    estimate_memory_bytes,
    get_array_pool,
    get_backend,
    get_best_available_backend,
    to_numpy,
)
from kicad_tools.acceleration.config import get_effective_backend, should_use_gpu

__all__ = [
    "ArrayBackend",
    "BackendType",
    "GPUArrayPool",
    "check_memory_available",
    "detect_backend",
    "estimate_memory_bytes",
    "get_array_pool",
    "get_backend",
    "get_best_available_backend",
    "get_effective_backend",
    "should_use_gpu",
    "to_numpy",
]
