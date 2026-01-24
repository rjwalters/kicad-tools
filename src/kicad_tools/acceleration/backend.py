"""Backend abstraction for GPU-accelerated array operations.

Provides a unified interface for NumPy (CPU), CuPy (CUDA), and MLX (Metal)
array operations. This allows algorithms to be written once and run on
any available backend.

Example::

    from kicad_tools.acceleration.backend import ArrayBackend

    backend = ArrayBackend.create("auto")  # Auto-detect best backend

    # Use like NumPy
    arr = backend.array([[1, 2], [3, 4]], dtype=backend.float32)
    result = backend.sum(arr, axis=1)
    numpy_result = backend.to_numpy(result)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np
from numpy.typing import NDArray


class BackendType(str, Enum):
    """Available compute backends."""

    CPU = "cpu"
    CUDA = "cuda"
    METAL = "metal"


@dataclass
class ArrayBackend:
    """Unified interface for array operations across CPU/GPU backends.

    Wraps NumPy, CuPy, or MLX to provide consistent array operations.
    The backend is selected at creation time and all operations use
    that backend's array module.

    Attributes:
        backend_type: The compute backend being used.
        xp: The array module (numpy, cupy, or mlx.core).
        float32: Float32 dtype for this backend.
        float64: Float64 dtype for this backend.
        int32: Int32 dtype for this backend.
        int64: Int64 dtype for this backend.
    """

    backend_type: BackendType
    xp: Any  # numpy, cupy, or mlx.core module

    def __post_init__(self):
        """Initialize dtype aliases for the backend."""
        self.float32 = self.xp.float32
        self.float64 = getattr(self.xp, "float64", self.xp.float32)
        self.int32 = self.xp.int32
        self.int64 = getattr(self.xp, "int64", self.xp.int32)

    @classmethod
    def create(cls, backend: BackendType | str) -> ArrayBackend:
        """Create a backend instance.

        Args:
            backend: Backend type to use. "auto" will detect best available.

        Returns:
            ArrayBackend configured for the requested or detected backend.

        Raises:
            ImportError: If requested backend library is not available.
        """
        if isinstance(backend, str):
            backend = BackendType(backend.lower())

        if backend == BackendType.CPU:
            return cls(backend_type=BackendType.CPU, xp=np)

        if backend == BackendType.CUDA:
            try:
                import cupy as cp

                return cls(backend_type=BackendType.CUDA, xp=cp)
            except ImportError as e:
                raise ImportError(
                    "CuPy not installed. Install with: pip install cupy-cuda12x"
                ) from e

        if backend == BackendType.METAL:
            try:
                import mlx.core as mx

                return cls(backend_type=BackendType.METAL, xp=mx)
            except ImportError as e:
                raise ImportError(
                    "MLX not installed. Install with: pip install mlx"
                ) from e

        raise ValueError(f"Unknown backend: {backend}")

    @classmethod
    def auto(cls) -> ArrayBackend:
        """Create backend with auto-detection.

        Tries CUDA first, then Metal, falling back to CPU.

        Returns:
            ArrayBackend with best available backend.
        """
        # Try CUDA first (NVIDIA)
        try:
            import cupy as cp

            # Verify GPU is actually available
            cp.cuda.runtime.getDeviceCount()
            return cls(backend_type=BackendType.CUDA, xp=cp)
        except Exception:
            pass

        # Try Metal (Apple Silicon)
        try:
            import mlx.core as mx

            # MLX should work if importable on Apple Silicon
            import platform

            if platform.machine() == "arm64":
                return cls(backend_type=BackendType.METAL, xp=mx)
        except Exception:
            pass

        # Fallback to CPU (NumPy)
        return cls(backend_type=BackendType.CPU, xp=np)

    @property
    def is_gpu(self) -> bool:
        """Check if this backend uses GPU acceleration."""
        return self.backend_type in (BackendType.CUDA, BackendType.METAL)

    def array(self, data: Any, dtype: Any = None) -> Any:
        """Create an array on this backend.

        Args:
            data: Input data (list, tuple, or numpy array).
            dtype: Optional dtype for the array.

        Returns:
            Array on this backend.
        """
        if dtype is None:
            dtype = self.float32
        return self.xp.array(data, dtype=dtype)

    def zeros(self, shape: tuple[int, ...], dtype: Any = None) -> Any:
        """Create a zero-filled array.

        Args:
            shape: Shape of the array.
            dtype: Optional dtype for the array.

        Returns:
            Zero-filled array on this backend.
        """
        if dtype is None:
            dtype = self.float32
        return self.xp.zeros(shape, dtype=dtype)

    def ones(self, shape: tuple[int, ...], dtype: Any = None) -> Any:
        """Create an array filled with ones.

        Args:
            shape: Shape of the array.
            dtype: Optional dtype for the array.

        Returns:
            Array filled with ones on this backend.
        """
        if dtype is None:
            dtype = self.float32
        return self.xp.ones(shape, dtype=dtype)

    def to_numpy(self, arr: Any) -> NDArray[Any]:
        """Convert array to NumPy.

        Args:
            arr: Array from any backend.

        Returns:
            NumPy array with same data.
        """
        if self.backend_type == BackendType.CPU:
            return arr
        if self.backend_type == BackendType.CUDA:
            return arr.get()  # CuPy uses .get()
        if self.backend_type == BackendType.METAL:
            return np.array(arr)  # MLX converts via np.array()
        return np.asarray(arr)

    def sqrt(self, x: Any) -> Any:
        """Element-wise square root."""
        return self.xp.sqrt(x)

    def sum(self, x: Any, axis: int | tuple[int, ...] | None = None) -> Any:
        """Sum of array elements."""
        return self.xp.sum(x, axis=axis)

    def abs(self, x: Any) -> Any:
        """Element-wise absolute value."""
        return self.xp.abs(x)

    def maximum(self, x1: Any, x2: Any) -> Any:
        """Element-wise maximum."""
        return self.xp.maximum(x1, x2)

    def minimum(self, x1: Any, x2: Any) -> Any:
        """Element-wise minimum."""
        return self.xp.minimum(x1, x2)

    def clip(self, x: Any, a_min: float, a_max: float) -> Any:
        """Clip values to a range."""
        return self.xp.clip(x, a_min, a_max)

    def where(self, condition: Any, x: Any, y: Any) -> Any:
        """Element-wise selection based on condition."""
        return self.xp.where(condition, x, y)

    def broadcast_to(self, arr: Any, shape: tuple[int, ...]) -> Any:
        """Broadcast array to a new shape."""
        return self.xp.broadcast_to(arr, shape)

    def expand_dims(self, arr: Any, axis: int) -> Any:
        """Expand array dimensions."""
        return self.xp.expand_dims(arr, axis=axis)

    def concatenate(self, arrays: list[Any], axis: int = 0) -> Any:
        """Concatenate arrays along an axis."""
        return self.xp.concatenate(arrays, axis=axis)

    def stack(self, arrays: list[Any], axis: int = 0) -> Any:
        """Stack arrays along a new axis."""
        return self.xp.stack(arrays, axis=axis)

    def arange(self, start: int, stop: int | None = None, step: int = 1) -> Any:
        """Create evenly spaced values within an interval."""
        if stop is None:
            return self.xp.arange(start)
        return self.xp.arange(start, stop, step)

    def meshgrid(self, *xi: Any, indexing: str = "xy") -> list[Any]:
        """Create coordinate matrices from coordinate vectors."""
        return self.xp.meshgrid(*xi, indexing=indexing)

    def einsum(self, subscripts: str, *operands: Any) -> Any:
        """Einstein summation convention."""
        return self.xp.einsum(subscripts, *operands)

    def matmul(self, a: Any, b: Any) -> Any:
        """Matrix multiplication."""
        return self.xp.matmul(a, b)

    def logical_and(self, x1: Any, x2: Any) -> Any:
        """Element-wise logical AND."""
        return self.xp.logical_and(x1, x2)

    def logical_or(self, x1: Any, x2: Any) -> Any:
        """Element-wise logical OR."""
        return self.xp.logical_or(x1, x2)

    def logical_not(self, x: Any) -> Any:
        """Element-wise logical NOT."""
        return self.xp.logical_not(x)

    def all(self, x: Any, axis: int | None = None) -> Any:
        """Test whether all elements evaluate to True."""
        return self.xp.all(x, axis=axis)

    def any(self, x: Any, axis: int | None = None) -> Any:
        """Test whether any element evaluates to True."""
        return self.xp.any(x, axis=axis)

    def argmax(self, x: Any, axis: int | None = None) -> Any:
        """Index of maximum value."""
        return self.xp.argmax(x, axis=axis)

    def argmin(self, x: Any, axis: int | None = None) -> Any:
        """Index of minimum value."""
        return self.xp.argmin(x, axis=axis)

    def mean(self, x: Any, axis: int | tuple[int, ...] | None = None) -> Any:
        """Mean of array elements."""
        return self.xp.mean(x, axis=axis)

    def std(self, x: Any, axis: int | tuple[int, ...] | None = None) -> Any:
        """Standard deviation of array elements."""
        return self.xp.std(x, axis=axis)

    def astype(self, x: Any, dtype: Any) -> Any:
        """Cast array to a specified type."""
        return x.astype(dtype)

    def reshape(self, x: Any, shape: tuple[int, ...]) -> Any:
        """Reshape array."""
        return self.xp.reshape(x, shape)

    def transpose(self, x: Any, axes: tuple[int, ...] | None = None) -> Any:
        """Permute array dimensions."""
        return self.xp.transpose(x, axes=axes)

    def cos(self, x: Any) -> Any:
        """Element-wise cosine."""
        return self.xp.cos(x)

    def sin(self, x: Any) -> Any:
        """Element-wise sine."""
        return self.xp.sin(x)

    def radians(self, x: Any) -> Any:
        """Convert angles from degrees to radians."""
        if hasattr(self.xp, "radians"):
            return self.xp.radians(x)
        # MLX fallback
        return x * (self.xp.pi / 180.0)

    @property
    def pi(self) -> float:
        """Value of pi."""
        if hasattr(self.xp, "pi"):
            return self.xp.pi
        return 3.141592653589793

    @property
    def inf(self) -> float:
        """Positive infinity."""
        if hasattr(self.xp, "inf"):
            return self.xp.inf
        return float("inf")
