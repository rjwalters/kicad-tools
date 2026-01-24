"""GPU acceleration module for kicad-tools.

Provides a unified interface for GPU backends with automatic detection
and selection. Supports CUDA (via CuPy), Metal (via MLX), and CPU (NumPy).

Basic Usage:
    >>> from kicad_tools.acceleration import get_backend, BackendType
    >>> backend = get_backend()  # Auto-detect best backend
    >>> arr = backend.zeros((1000, 1000))
    >>> result = backend.to_numpy(arr)

Force Specific Backend:
    >>> backend = get_backend(BackendType.CPU)  # Always use CPU
    >>> backend = get_backend("cuda")  # Request CUDA

Check Available Backends:
    >>> from kicad_tools.acceleration import detect_backends
    >>> available = detect_backends()  # [BackendType.METAL, BackendType.CPU]
"""

from kicad_tools.acceleration.backend import ArrayBackend, BackendType
from kicad_tools.acceleration.cpu import CPUBackend
from kicad_tools.acceleration.cuda import CUDABackend
from kicad_tools.acceleration.detection import (
    detect_backends,
    get_backend,
    get_backend_info,
)
from kicad_tools.acceleration.metal import MetalBackend

__all__ = [
    # Core types
    "BackendType",
    "ArrayBackend",
    # Backend implementations
    "CPUBackend",
    "CUDABackend",
    "MetalBackend",
    # Detection and factory functions
    "detect_backends",
    "get_backend",
    "get_backend_info",
]
