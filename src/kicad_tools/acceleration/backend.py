"""GPU backend abstraction for array operations.

Provides a unified interface for NumPy, CuPy (CUDA), and MLX (Metal)
array operations, enabling transparent GPU acceleration for routing
grid operations.

The backend abstraction uses duck typing - any library that provides
NumPy-compatible array operations can be used as a backend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
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


@dataclass
class GPUArrayPool:
    """Pool of pre-allocated GPU arrays for reuse.

    Reduces allocation overhead by caching arrays of common shapes and dtypes.
    Thread-safe for use in concurrent environments.

    Example::

        pool = GPUArrayPool()
        backend = get_backend(BackendType.CUDA)

        # Get an array from pool (or allocate new)
        arr = pool.get((100, 2), np.float32, backend)

        # Use the array...

        # Return to pool for reuse
        pool.return_array(arr, backend)
    """

    _cache: dict[tuple[tuple[int, ...], str, BackendType], list[Any]] = field(
        default_factory=dict
    )
    _lock: Lock = field(default_factory=Lock)
    max_pool_size: int = 10  # Max arrays per shape/dtype

    def get(
        self,
        shape: tuple[int, ...],
        dtype: type | np.dtype,
        backend: ArrayBackend,
    ) -> Any:
        """Get a zeroed array from pool or allocate new.

        Args:
            shape: Shape of the array to get.
            dtype: NumPy dtype for the array.
            backend: ArrayBackend to use for allocation.

        Returns:
            A zeroed array of the requested shape and dtype.
        """
        dtype_str = str(np.dtype(dtype))
        key = (shape, dtype_str, backend.backend_type)

        with self._lock:
            if key in self._cache and self._cache[key]:
                arr = self._cache[key].pop()
                # Zero the array before returning
                if backend.backend_type == BackendType.CPU:
                    arr.fill(0)
                elif backend.backend_type == BackendType.CUDA:
                    arr.fill(0)
                elif backend.backend_type == BackendType.METAL:
                    # MLX arrays are immutable, return new zeroed array
                    return backend.zeros(shape, dtype=dtype)
                return arr

        # Allocate new array
        return backend.zeros(shape, dtype=dtype)

    def return_array(self, arr: Any, backend: ArrayBackend) -> None:
        """Return array to pool for reuse.

        Args:
            arr: Array to return to the pool.
            backend: ArrayBackend used for the array.
        """
        # MLX arrays are immutable, don't pool them
        if backend.backend_type == BackendType.METAL:
            return

        shape = tuple(arr.shape)
        dtype_str = str(arr.dtype)
        key = (shape, dtype_str, backend.backend_type)

        with self._lock:
            if key not in self._cache:
                self._cache[key] = []

            # Only keep up to max_pool_size arrays
            if len(self._cache[key]) < self.max_pool_size:
                self._cache[key].append(arr)

    def clear(self) -> None:
        """Clear all cached arrays."""
        with self._lock:
            self._cache.clear()


# Global array pool instance
_array_pool: GPUArrayPool | None = None


def get_array_pool() -> GPUArrayPool:
    """Get the global array pool instance."""
    global _array_pool
    if _array_pool is None:
        _array_pool = GPUArrayPool()
    return _array_pool


class ArrayBackend:
    """Unified array backend abstraction for CPU/GPU computation.

    Provides a consistent interface for array operations across NumPy,
    CuPy (CUDA), and MLX (Metal) backends. Supports device-resident
    operations including scatter-add for efficient accumulation.

    Example::

        backend = ArrayBackend(BackendType.CUDA)

        # Create arrays on GPU
        forces = backend.zeros((100, 2))
        indices = backend.array([0, 1, 2, 0, 1])
        values = backend.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [2.0, 0.0], [0.0, 2.0]])

        # GPU-native scatter-add (no CPU transfer)
        backend.scatter_add(forces, indices, values)

        # Transfer result to CPU only when needed
        result = backend.to_numpy(forces)
    """

    def __init__(self, backend_type: BackendType):
        """Initialize the array backend.

        Args:
            backend_type: Type of backend to use.
        """
        self._backend_type = backend_type
        self._xp: Any = None
        self._initialize()

    def _initialize(self) -> None:
        """Initialize the underlying array library."""
        if self._backend_type == BackendType.CPU:
            self._xp = np
        elif self._backend_type == BackendType.CUDA:
            try:
                import cupy as cp

                self._xp = cp
            except ImportError:
                logger.warning("CuPy not available, falling back to NumPy")
                self._xp = np
                self._backend_type = BackendType.CPU
        elif self._backend_type == BackendType.METAL:
            try:
                import mlx.core as mx

                self._xp = mx
            except ImportError:
                logger.warning("MLX not available, falling back to NumPy")
                self._xp = np
                self._backend_type = BackendType.CPU

    @classmethod
    def create(cls, backend_type: BackendType | str) -> ArrayBackend:
        """Create an ArrayBackend instance (alternative to constructor).

        Args:
            backend_type: Type of backend to use. Can be a BackendType enum
                or a string like "cpu", "cuda", or "metal".

        Returns:
            ArrayBackend instance.
        """
        if isinstance(backend_type, str):
            backend_type = BackendType(backend_type)
        return cls(backend_type)

    @property
    def backend_type(self) -> BackendType:
        """Return the backend type."""
        return self._backend_type

    @property
    def is_gpu(self) -> bool:
        """Return True if this is a GPU backend."""
        return self._backend_type in (BackendType.CUDA, BackendType.METAL)

    @property
    def xp(self) -> Any:
        """Return the underlying array library module."""
        return self._xp

    @property
    def float32(self) -> Any:
        """Return the float32 dtype for this backend."""
        if self._backend_type == BackendType.METAL:
            return self._xp.float32
        return np.float32

    @property
    def int32(self) -> Any:
        """Return the int32 dtype for this backend."""
        if self._backend_type == BackendType.METAL:
            return self._xp.int32
        return np.int32

    def array(self, data: Any, dtype: Any = None) -> Any:
        """Create an array from data.

        Args:
            data: Data to convert to array.
            dtype: Optional dtype for the array.

        Returns:
            Array on the appropriate device.
        """
        if self._backend_type == BackendType.METAL:
            if dtype is not None:
                dtype = self._numpy_to_mlx_dtype(dtype)
            return self._xp.array(data, dtype=dtype)
        return self._xp.array(data, dtype=dtype)

    def zeros(self, shape: tuple[int, ...], dtype: Any = None) -> Any:
        """Create a zero-filled array.

        Args:
            shape: Shape of the array.
            dtype: Optional dtype (default: float32).

        Returns:
            Zero-filled array on the appropriate device.
        """
        if dtype is None:
            dtype = np.float32
        if self._backend_type == BackendType.METAL:
            dtype = self._numpy_to_mlx_dtype(dtype)
        return self._xp.zeros(shape, dtype=dtype)

    def ones(self, shape: tuple[int, ...], dtype: Any = None) -> Any:
        """Create a ones-filled array.

        Args:
            shape: Shape of the array.
            dtype: Optional dtype (default: float32).

        Returns:
            Ones-filled array on the appropriate device.
        """
        if dtype is None:
            dtype = np.float32
        if self._backend_type == BackendType.METAL:
            dtype = self._numpy_to_mlx_dtype(dtype)
        return self._xp.ones(shape, dtype=dtype)

    def full(self, shape: tuple[int, ...], fill_value: Any, dtype: Any = None) -> Any:
        """Create an array filled with a value.

        Args:
            shape: Shape of the array.
            fill_value: Value to fill with.
            dtype: Optional dtype (default: float32).

        Returns:
            Filled array on the appropriate device.
        """
        if dtype is None:
            dtype = np.float32
        if self._backend_type == BackendType.METAL:
            dtype = self._numpy_to_mlx_dtype(dtype)
        return self._xp.full(shape, fill_value, dtype=dtype)

    def sum(self, arr: Any, axis: int | tuple[int, ...] | None = None, keepdims: bool = False) -> Any:
        """Sum array elements.

        Args:
            arr: Array to sum.
            axis: Axis or axes along which to sum.
            keepdims: Whether to keep reduced dimensions.

        Returns:
            Sum result.
        """
        if self._backend_type == BackendType.METAL:
            return self._xp.sum(arr, axis=axis, keepdims=keepdims)
        return self._xp.sum(arr, axis=axis, keepdims=keepdims)

    def sqrt(self, arr: Any) -> Any:
        """Compute element-wise square root.

        Args:
            arr: Input array.

        Returns:
            Square root of each element.
        """
        return self._xp.sqrt(arr)

    def maximum(self, arr: Any, value: Any) -> Any:
        """Element-wise maximum with a value or array.

        Args:
            arr: Input array.
            value: Value or array to compare.

        Returns:
            Element-wise maximum.
        """
        return self._xp.maximum(arr, value)

    def minimum(self, arr: Any, value: Any) -> Any:
        """Element-wise minimum with a value or array.

        Args:
            arr: Input array.
            value: Value or array to compare.

        Returns:
            Element-wise minimum.
        """
        return self._xp.minimum(arr, value)

    def clip(self, arr: Any, a_min: float, a_max: float) -> Any:
        """Clip array values to a range.

        Args:
            arr: Input array.
            a_min: Minimum value.
            a_max: Maximum value.

        Returns:
            Clipped array.
        """
        return self._xp.clip(arr, a_min, a_max)

    def abs(self, arr: Any) -> Any:
        """Compute element-wise absolute value.

        Args:
            arr: Input array.

        Returns:
            Absolute value of each element.
        """
        return self._xp.abs(arr)

    def where(self, condition: Any, x: Any = None, y: Any = None) -> Any:
        """Conditional array selection.

        Args:
            condition: Boolean condition array.
            x: Values where condition is True.
            y: Values where condition is False.

        Returns:
            Selected values based on condition.
        """
        if x is None and y is None:
            # Return indices where condition is true
            if self._backend_type == BackendType.METAL:
                return self._xp.argwhere(condition)
            return self._xp.where(condition)
        return self._xp.where(condition, x, y)

    def logical_and(self, a: Any, b: Any) -> Any:
        """Element-wise logical AND.

        Args:
            a: First boolean array.
            b: Second boolean array.

        Returns:
            Logical AND of the arrays.
        """
        return self._xp.logical_and(a, b)

    def logical_or(self, a: Any, b: Any) -> Any:
        """Element-wise logical OR.

        Args:
            a: First boolean array.
            b: Second boolean array.

        Returns:
            Logical OR of the arrays.
        """
        return self._xp.logical_or(a, b)

    def expand_dims(self, arr: Any, axis: int) -> Any:
        """Expand array dimensions.

        Args:
            arr: Input array.
            axis: Position where new axis should be inserted.

        Returns:
            Array with expanded dimensions.
        """
        return self._xp.expand_dims(arr, axis=axis)

    def reshape(self, arr: Any, shape: tuple[int, ...]) -> Any:
        """Reshape array.

        Args:
            arr: Input array.
            shape: New shape.

        Returns:
            Reshaped array.
        """
        return self._xp.reshape(arr, shape)

    def fill_diagonal(self, arr: Any, value: float) -> Any:
        """Fill diagonal of 2D array with a value.

        Args:
            arr: 2D input array.
            value: Value to fill diagonal with.

        Returns:
            Array with filled diagonal.
        """
        if self._backend_type == BackendType.METAL:
            # MLX doesn't have fill_diagonal, use indexing
            n = min(arr.shape[0], arr.shape[1])
            indices = self._xp.arange(n)
            # MLX arrays are immutable, need to create new array
            result = self._xp.array(arr)
            # Use scatter for diagonal assignment
            mask = self._xp.zeros_like(arr)
            for i in range(n):
                mask = mask.at[i, i].add(1.0)
            result = self._xp.where(mask > 0, value, result)
            return result
        else:
            self._xp.fill_diagonal(arr, value)
            return arr

    def scatter_add(
        self,
        target: Any,
        indices: Any,
        values: Any,
        axis: int = 0,
    ) -> Any:
        """Atomically add values at indices to target array (GPU-resident).

        This is the key operation for eliminating CPU-GPU transfers in inner loops.
        The operation target[indices] += values is performed entirely on GPU.

        Args:
            target: Target array to accumulate into (modified in place for CuPy/NumPy).
            indices: 1D array of indices where values should be added.
            values: Values to add at the specified indices.
            axis: Axis along which to scatter (default: 0).

        Returns:
            Updated target array (same array for CuPy/NumPy, new array for MLX).

        Example::

            # Accumulate forces per component from edge contributions
            forces = backend.zeros((n_components, 2))
            edge_forces = backend.array([[1.0, 0.5], [0.2, 0.3], ...])  # (n_edges, 2)
            edge_comp_idx = backend.array([0, 0, 1, 2, 1, ...])  # component index per edge

            # GPU-native scatter-add (no CPU roundtrip!)
            backend.scatter_add(forces, edge_comp_idx, edge_forces)
        """
        if self._backend_type == BackendType.CUDA:
            import cupyx

            cupyx.scatter_add(target, indices, values)
            return target
        elif self._backend_type == BackendType.METAL:
            # MLX doesn't have in-place scatter_add
            # Use indexing with at[] for functional update
            import mlx.core as mx

            # Convert indices to numpy for iteration if needed
            if isinstance(indices, mx.array):
                indices_np = np.array(indices.tolist())
            else:
                indices_np = np.asarray(indices)

            # Group values by index for batched updates
            unique_indices = np.unique(indices_np)
            result = target

            for idx in unique_indices:
                mask = indices_np == idx
                contrib = self._xp.sum(values[mask], axis=0)
                # MLX functional update
                if axis == 0:
                    result = result.at[idx].add(contrib)
                else:
                    # Handle other axes if needed
                    result = result.at[idx].add(contrib)

            return result
        else:
            # NumPy: use np.add.at for scatter-add
            np.add.at(target, indices, values)
            return target

    def to_numpy(self, arr: Any) -> np.ndarray:
        """Convert array to NumPy.

        Args:
            arr: Array from any backend.

        Returns:
            NumPy array.
        """
        if isinstance(arr, np.ndarray):
            return arr

        if self._backend_type == BackendType.CUDA:
            # CuPy array
            return arr.get()
        elif self._backend_type == BackendType.METAL:
            # MLX array
            import mlx.core as mx

            if isinstance(arr, mx.array):
                return np.array(arr.tolist())
            return np.array(arr)

        # Fallback
        return np.array(arr)

    def _numpy_to_mlx_dtype(self, dtype: Any) -> Any:
        """Convert NumPy dtype to MLX dtype.

        Args:
            dtype: NumPy dtype.

        Returns:
            Equivalent MLX dtype.
        """
        if dtype is None:
            return self._xp.float32

        dtype_str = str(np.dtype(dtype))
        dtype_map = {
            "bool": self._xp.bool_,
            "int16": self._xp.int16,
            "int32": self._xp.int32,
            "int64": self._xp.int64,
            "float32": self._xp.float32,
            "float64": self._xp.float32,  # MLX uses float32 by default
        }
        return dtype_map.get(dtype_str, self._xp.float32)


# Cache of ArrayBackend instances
_array_backend_cache: dict[BackendType, ArrayBackend] = {}


def get_backend(
    backend_type: BackendType | None = None,
    config: PerformanceConfig | None = None,
) -> ArrayBackend:
    """Get an ArrayBackend instance.

    Args:
        backend_type: Specific backend to use. If None, auto-detects.
        config: Performance config for backend selection. Used when
            backend_type is None and config.gpu.backend != "auto".

    Returns:
        ArrayBackend instance for the requested or detected backend.
    """
    # Determine which backend to use
    if backend_type is None:
        if config is not None and config.gpu.backend != "auto":
            backend_type = BackendType(config.gpu.backend)
        else:
            backend_type = detect_backend()

    # Return cached backend if available
    if backend_type in _array_backend_cache:
        return _array_backend_cache[backend_type]

    # Create and cache new backend
    backend = ArrayBackend(backend_type)
    _array_backend_cache[backend_type] = backend
    return backend


def get_best_available_backend() -> ArrayBackend:
    """Get the best available backend (GPU if available, else CPU).

    Returns:
        ArrayBackend instance for the best available backend.
    """
    return get_backend(detect_backend())


class MLXBackend:
    """NumPy-compatible wrapper for MLX operations.

    MLX has a different API than NumPy/CuPy, so this wrapper provides
    a compatible interface for the operations used in RoutingGrid.

    DEPRECATED: Use ArrayBackend instead for new code.
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
