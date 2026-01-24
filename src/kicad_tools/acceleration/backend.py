"""GPU backend abstraction for array operations.

Provides a unified interface for NumPy, CuPy (CUDA), and MLX (Metal)
array operations, enabling transparent GPU acceleration for routing
grid operations.

The backend abstraction uses duck typing - any library that provides
NumPy-compatible array operations can be used as a backend.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from kicad_tools.performance import PerformanceConfig

logger = logging.getLogger(__name__)


class BackendType(Enum):
    """Available array computation backends."""

    CPU = "cpu"
    CUDA = "cuda"
    METAL = "metal"


# Cached backend modules
_backend_cache: dict[BackendType, Any] = {}
_detected_backend: BackendType | None = None


def detect_backend() -> BackendType:
    """Detect the best available backend.

    Checks for CUDA (CuPy) first, then Metal (MLX), falling back to CPU.

    Returns:
        The detected BackendType.
    """
    global _detected_backend

    if _detected_backend is not None:
        return _detected_backend

    # Try CUDA via CuPy
    try:
        import cupy as cp

        # Verify CUDA is actually available
        cp.cuda.runtime.getDeviceCount()
        _detected_backend = BackendType.CUDA
        logger.info("GPU backend detected: CUDA (CuPy)")
        return _detected_backend
    except Exception:
        pass

    # Try Metal via MLX
    try:
        import mlx.core as mx

        # MLX always uses Metal on Apple Silicon
        import platform

        if platform.system() == "Darwin" and platform.machine() == "arm64":
            _detected_backend = BackendType.METAL
            logger.info("GPU backend detected: Metal (MLX)")
            return _detected_backend
    except Exception:
        pass

    # Fallback to CPU
    _detected_backend = BackendType.CPU
    logger.info("GPU backend: CPU (NumPy)")
    return _detected_backend


def get_backend(
    backend_type: BackendType | None = None,
    config: PerformanceConfig | None = None,
) -> Any:
    """Get the array backend module.

    Args:
        backend_type: Specific backend to use. If None, auto-detects.
        config: Performance config for backend selection. Used when
            backend_type is None and config.gpu.backend != "auto".

    Returns:
        The backend module (numpy, cupy, or mlx.core).

    Raises:
        ImportError: If the requested backend is not available.
    """
    # Determine which backend to use
    if backend_type is None:
        if config is not None and config.gpu.backend != "auto":
            backend_type = BackendType(config.gpu.backend)
        else:
            backend_type = detect_backend()

    # Return cached backend if available
    if backend_type in _backend_cache:
        return _backend_cache[backend_type]

    # Load and cache the backend
    if backend_type == BackendType.CPU:
        _backend_cache[backend_type] = np
        return np

    elif backend_type == BackendType.CUDA:
        try:
            import cupy as cp

            _backend_cache[backend_type] = cp
            return cp
        except ImportError as e:
            logger.warning(f"CuPy not available, falling back to CPU: {e}")
            _backend_cache[backend_type] = np
            return np

    elif backend_type == BackendType.METAL:
        try:
            import mlx.core as mx

            # Create a numpy-compatible wrapper for MLX
            _backend_cache[backend_type] = MLXBackend()
            return _backend_cache[backend_type]
        except ImportError as e:
            logger.warning(f"MLX not available, falling back to CPU: {e}")
            _backend_cache[backend_type] = np
            return np

    # Unknown backend, use CPU
    return np


class MLXBackend:
    """NumPy-compatible wrapper for MLX operations.

    MLX has a different API than NumPy/CuPy, so this wrapper provides
    a compatible interface for the operations used in RoutingGrid.
    """

    def __init__(self):
        import mlx.core as mx

        self._mx = mx

    def zeros(self, shape: tuple, dtype: Any = None) -> Any:
        """Create a zero-filled array."""
        mx_dtype = self._numpy_to_mlx_dtype(dtype)
        return self._mx.zeros(shape, dtype=mx_dtype)

    def ones(self, shape: tuple, dtype: Any = None) -> Any:
        """Create a ones-filled array."""
        mx_dtype = self._numpy_to_mlx_dtype(dtype)
        return self._mx.ones(shape, dtype=mx_dtype)

    def full(self, shape: tuple, fill_value: Any, dtype: Any = None) -> Any:
        """Create an array filled with a value."""
        mx_dtype = self._numpy_to_mlx_dtype(dtype)
        return self._mx.full(shape, fill_value, dtype=mx_dtype)

    def where(self, condition: Any, x: Any = None, y: Any = None) -> Any:
        """Conditional array selection."""
        if x is None and y is None:
            # Return indices where condition is true
            return self._mx.argwhere(condition)
        return self._mx.where(condition, x, y)

    def sum(self, arr: Any, axis: int | None = None) -> Any:
        """Sum array elements."""
        return self._mx.sum(arr, axis=axis)

    def max(self, arr: Any, axis: int | None = None) -> Any:
        """Maximum array element."""
        return self._mx.max(arr, axis=axis)

    def mean(self, arr: Any, axis: int | None = None) -> Any:
        """Mean of array elements."""
        return self._mx.mean(arr, axis=axis)

    def _numpy_to_mlx_dtype(self, dtype: Any) -> Any:
        """Convert NumPy dtype to MLX dtype."""
        if dtype is None:
            return self._mx.float32

        dtype_str = str(np.dtype(dtype))
        dtype_map = {
            "bool": self._mx.bool_,
            "int16": self._mx.int16,
            "int32": self._mx.int32,
            "int64": self._mx.int64,
            "float32": self._mx.float32,
            "float64": self._mx.float32,  # MLX uses float32 by default
        }
        return dtype_map.get(dtype_str, self._mx.float32)


def to_numpy(arr: Any) -> np.ndarray:
    """Convert any backend array to NumPy.

    Args:
        arr: Array from any backend (NumPy, CuPy, MLX).

    Returns:
        NumPy array.
    """
    if isinstance(arr, np.ndarray):
        return arr

    # CuPy array
    if hasattr(arr, "get"):
        return arr.get()

    # MLX array
    if hasattr(arr, "tolist"):
        try:
            import mlx.core as mx

            if isinstance(arr, mx.array):
                return np.array(arr.tolist())
        except ImportError:
            pass

    # Fallback: try np.array()
    return np.array(arr)


def estimate_memory_bytes(
    cols: int,
    rows: int,
    layers: int,
) -> int:
    """Estimate GPU memory needed for grid arrays.

    Based on actual RoutingGrid array allocations:
    - _blocked: bool (1 byte)
    - _net: int32 (4 bytes)
    - _usage_count: int16 (2 bytes)
    - _history_cost: float32 (4 bytes)
    - _is_obstacle: bool (1 byte)
    - _is_zone: bool (1 byte)
    - _pad_blocked: bool (1 byte)
    - _original_net: int32 (4 bytes)
    Total: 18 bytes per cell

    Args:
        cols: Grid columns.
        rows: Grid rows.
        layers: Number of layers.

    Returns:
        Estimated memory in bytes.
    """
    cells = cols * rows * layers
    bytes_per_cell = 18  # See docstring for breakdown
    return cells * bytes_per_cell


def check_memory_available(
    required_bytes: int,
    config: PerformanceConfig | None = None,
) -> bool:
    """Check if sufficient GPU memory is available.

    Args:
        required_bytes: Memory needed in bytes.
        config: Performance config with memory limits.

    Returns:
        True if memory is available, False otherwise.
    """
    # Check config limit first
    if config is not None and config.gpu.memory_limit_mb > 0:
        limit_bytes = config.gpu.memory_limit_mb * 1024 * 1024
        if required_bytes > limit_bytes:
            logger.debug(
                f"Memory limit exceeded: {required_bytes / 1e6:.1f}MB > "
                f"{config.gpu.memory_limit_mb}MB limit"
            )
            return False

    backend_type = detect_backend()

    if backend_type == BackendType.CUDA:
        try:
            import cupy as cp

            meminfo = cp.cuda.runtime.memGetInfo()
            free_bytes = meminfo[0]
            return required_bytes < free_bytes * 0.9  # Leave 10% headroom
        except Exception:
            return False

    elif backend_type == BackendType.METAL:
        # MLX doesn't expose memory info directly
        # Use system memory as proxy (Apple unified memory)
        try:
            import subprocess

            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                total_bytes = int(result.stdout.strip())
                # Assume 50% available for GPU
                return required_bytes < total_bytes * 0.5
        except Exception:
            pass

    # CPU always has "enough" memory (let numpy handle OOM)
    return True
