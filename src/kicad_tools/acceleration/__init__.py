"""GPU acceleration module for kicad-tools.

Provides GPU configuration helpers and backend abstraction for
hardware-accelerated routing and placement operations.
"""

from kicad_tools.acceleration.backend import (
    ArrayBackend,
    BackendType,
    get_backend,
    get_best_available_backend,
)
from kicad_tools.acceleration.config import should_use_gpu

__all__ = [
    "ArrayBackend",
    "BackendType",
    "get_backend",
    "get_best_available_backend",
    "should_use_gpu",
]
