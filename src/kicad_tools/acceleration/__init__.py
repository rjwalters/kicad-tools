"""GPU acceleration module for kicad-tools.

Provides GPU configuration helpers and backend abstraction for
hardware-accelerated routing and placement operations.
"""

from kicad_tools.acceleration.backend import (
    BackendType,
    check_memory_available,
    detect_backend,
    estimate_memory_bytes,
    get_backend,
    to_numpy,
)
from kicad_tools.acceleration.config import get_effective_backend, should_use_gpu

__all__ = [
    "BackendType",
    "check_memory_available",
    "detect_backend",
    "estimate_memory_bytes",
    "get_backend",
    "get_effective_backend",
    "should_use_gpu",
    "to_numpy",
]
