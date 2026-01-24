"""GPU acceleration module for kicad-tools.

Provides GPU configuration helpers, backend abstraction, and detection
utilities for hardware-accelerated routing and placement operations.
"""

from kicad_tools.acceleration.backend import (
    ArrayBackend,
    BackendType,
    get_backend,
    get_best_available_backend,
)
from kicad_tools.acceleration.config import should_use_gpu
from kicad_tools.acceleration.detection import (
    GPUBackend,
    GPUInfo,
    detect_gpu,
    get_available_backends,
    suggest_install_command,
)

__all__ = [
    # Backend abstraction
    "ArrayBackend",
    "BackendType",
    "get_backend",
    "get_best_available_backend",
    "should_use_gpu",
    # Detection utilities
    "GPUBackend",
    "GPUInfo",
    "detect_gpu",
    "get_available_backends",
    "suggest_install_command",
]
