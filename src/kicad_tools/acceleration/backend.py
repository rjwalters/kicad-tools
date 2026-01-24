"""Backend abstraction for CPU and GPU array operations.

Provides a unified interface for array operations that can run on CPU (NumPy)
or GPU (CuPy for CUDA, MLX for Metal). The backend automatically falls back
to CPU if GPU libraries are unavailable.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray


class BackendType(Enum):
    """Available backend types for array operations."""

    CPU = "cpu"
    CUDA = "cuda"
    METAL = "metal"


@runtime_checkable
class ArrayModule(Protocol):
    """Protocol for array module (numpy-like interface)."""

    def array(self, data: Any, dtype: Any = None) -> Any: ...
    def zeros(self, shape: tuple[int, ...], dtype: Any = None) -> Any: ...
    def ones(self, shape: tuple[int, ...], dtype: Any = None) -> Any: ...
    def sqrt(self, x: Any) -> Any: ...
    def sum(self, x: Any, axis: int | None = None) -> Any: ...
    def maximum(self, x: Any, y: Any) -> Any: ...
    def clip(self, x: Any, a_min: Any, a_max: Any) -> Any: ...


@dataclass
class ArrayBackend:
    """Backend abstraction for array operations.

    Provides a unified interface for CPU (NumPy) and GPU (CuPy/MLX) operations.
    Automatically handles data transfer between CPU and GPU.

    Attributes:
        backend_type: The type of backend (CPU, CUDA, or METAL).
        xp: The array module (numpy, cupy, or mlx.core).
    """

    backend_type: BackendType
    xp: Any  # numpy, cupy, or mlx.core module

    @classmethod
    def create(cls, backend_type: BackendType | str = BackendType.CPU) -> ArrayBackend:
        """Create a backend instance.

        Args:
            backend_type: Desired backend type. Falls back to CPU if unavailable.

        Returns:
            ArrayBackend configured for the requested (or fallback) backend.
        """
        if isinstance(backend_type, str):
            backend_type = BackendType(backend_type)

        if backend_type == BackendType.CUDA:
            try:
                import cupy as cp

                return cls(backend_type=BackendType.CUDA, xp=cp)
            except ImportError:
                warnings.warn(
                    "CuPy not available, falling back to CPU. "
                    "Install with: pip install cupy-cuda12x",
                    stacklevel=2,
                )
                return cls(backend_type=BackendType.CPU, xp=np)

        elif backend_type == BackendType.METAL:
            try:
                import mlx.core as mx

                return cls(backend_type=BackendType.METAL, xp=mx)
            except ImportError:
                warnings.warn(
                    "MLX not available, falling back to CPU. "
                    "Install with: pip install mlx",
                    stacklevel=2,
                )
                return cls(backend_type=BackendType.CPU, xp=np)

        return cls(backend_type=BackendType.CPU, xp=np)

    @property
    def is_gpu(self) -> bool:
        """Return True if this backend uses GPU acceleration."""
        return self.backend_type in (BackendType.CUDA, BackendType.METAL)

    def array(self, data: Any, dtype: Any = None) -> Any:
        """Create array on the backend device."""
        if dtype is None:
            dtype = self.xp.float32
        return self.xp.array(data, dtype=dtype)

    def zeros(self, shape: tuple[int, ...], dtype: Any = None) -> Any:
        """Create zero-filled array on the backend device."""
        if dtype is None:
            dtype = self.xp.float32
        return self.xp.zeros(shape, dtype=dtype)

    def ones(self, shape: tuple[int, ...], dtype: Any = None) -> Any:
        """Create one-filled array on the backend device."""
        if dtype is None:
            dtype = self.xp.float32
        return self.xp.ones(shape, dtype=dtype)

    def to_numpy(self, arr: Any) -> NDArray[np.float32]:
        """Transfer array to CPU as numpy array."""
        if self.backend_type == BackendType.CPU:
            return arr
        elif self.backend_type == BackendType.CUDA:
            return arr.get()  # CuPy's method to transfer to CPU
        elif self.backend_type == BackendType.METAL:
            return np.array(arr)  # MLX converts via np.array()
        return arr

    def sqrt(self, x: Any) -> Any:
        """Element-wise square root."""
        return self.xp.sqrt(x)

    def sum(self, x: Any, axis: int | None = None, keepdims: bool = False) -> Any:
        """Sum array elements along axis."""
        return self.xp.sum(x, axis=axis, keepdims=keepdims)

    def maximum(self, x: Any, y: Any) -> Any:
        """Element-wise maximum."""
        return self.xp.maximum(x, y)

    def clip(self, x: Any, a_min: Any, a_max: Any) -> Any:
        """Clip array values to range."""
        return self.xp.clip(x, a_min, a_max)

    def fill_diagonal(self, arr: Any, value: float) -> Any:
        """Fill diagonal of 2D array with value (returns new array)."""
        if self.backend_type == BackendType.METAL:
            # MLX doesn't have fill_diagonal, use mask approach
            n = arr.shape[0]
            # Create identity mask
            eye = self.xp.eye(n, dtype=arr.dtype)
            # Zero out diagonal and add value
            return arr * (1 - eye) + value * eye
        else:
            # NumPy and CuPy have fill_diagonal (modifies in place)
            result = arr.copy()
            self.xp.fill_diagonal(result, value)
            return result

    def norm(self, x: Any, axis: int | None = None, keepdims: bool = False) -> Any:
        """Compute L2 norm along axis."""
        if self.backend_type == BackendType.METAL:
            # MLX uses linalg.norm
            return self.xp.linalg.norm(x, axis=axis, keepdims=keepdims)
        else:
            # NumPy and CuPy
            return self.xp.linalg.norm(x, axis=axis, keepdims=keepdims)

    def expand_dims(self, x: Any, axis: int) -> Any:
        """Expand array dimensions."""
        return self.xp.expand_dims(x, axis=axis)

    def tile(self, x: Any, reps: tuple[int, ...]) -> Any:
        """Tile array."""
        return self.xp.tile(x, reps)

    def abs(self, x: Any) -> Any:
        """Element-wise absolute value."""
        return self.xp.abs(x)

    def minimum(self, x: Any, y: Any) -> Any:
        """Element-wise minimum."""
        return self.xp.minimum(x, y)

    def where(self, condition: Any, x: Any, y: Any) -> Any:
        """Element-wise selection based on condition."""
        return self.xp.where(condition, x, y)

    def logical_and(self, x: Any, y: Any) -> Any:
        """Element-wise logical AND."""
        return self.xp.logical_and(x, y)

    def logical_or(self, x: Any, y: Any) -> Any:
        """Element-wise logical OR."""
        return self.xp.logical_or(x, y)

    def reshape(self, x: Any, shape: tuple[int, ...]) -> Any:
        """Reshape array."""
        return self.xp.reshape(x, shape)

    @property
    def float32(self) -> Any:
        """Float32 dtype for this backend."""
        return self.xp.float32

    @property
    def int32(self) -> Any:
        """Int32 dtype for this backend."""
        return self.xp.int32

    @classmethod
    def auto(cls) -> ArrayBackend:
        """Create backend with auto-detection (alias for get_best_available_backend)."""
        return get_best_available_backend()


def get_backend(backend_type: BackendType | str = BackendType.CPU) -> ArrayBackend:
    """Get an array backend instance.

    Args:
        backend_type: Desired backend type (cpu, cuda, or metal).

    Returns:
        ArrayBackend configured for the requested type.
    """
    return ArrayBackend.create(backend_type)


def get_best_available_backend() -> ArrayBackend:
    """Get the best available backend for this system.

    Tries CUDA first, then Metal, then falls back to CPU.

    Returns:
        ArrayBackend configured for the best available backend.
    """
    # Try CUDA first (NVIDIA GPUs)
    try:
        import cupy as cp  # noqa: F401

        return ArrayBackend.create(BackendType.CUDA)
    except ImportError:
        pass

    # Try Metal (Apple Silicon)
    try:
        import mlx.core as mx  # noqa: F401

        return ArrayBackend.create(BackendType.METAL)
    except ImportError:
        pass

    # Fall back to CPU
    return ArrayBackend.create(BackendType.CPU)
