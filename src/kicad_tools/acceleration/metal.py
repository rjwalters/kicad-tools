"""Metal backend using MLX.

This backend provides GPU acceleration on Apple Silicon using MLX.
MLX must be installed for this backend to be available.
"""

from __future__ import annotations

import platform
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, DTypeLike, NDArray

from kicad_tools.acceleration.backend import ArrayBackend, BackendType

# Lazy import flag
_mlx = None
_mlx_import_attempted = False


def _get_mlx():
    """Lazy import MLX to avoid startup overhead."""
    global _mlx, _mlx_import_attempted
    if not _mlx_import_attempted:
        _mlx_import_attempted = True
        # MLX only works on macOS
        if platform.system() != "Darwin":
            _mlx = None
        else:
            try:
                import mlx.core as mx

                _mlx = mx
            except ImportError:
                _mlx = None
    return _mlx


def _numpy_dtype_to_mlx(dtype: DTypeLike) -> Any:
    """Convert numpy dtype to MLX dtype.

    Args:
        dtype: NumPy dtype.

    Returns:
        MLX dtype.
    """
    mx = _get_mlx()
    if mx is None:
        return dtype

    dtype = np.dtype(dtype)
    dtype_map = {
        np.float16: mx.float16,
        np.float32: mx.float32,
        np.float64: mx.float32,  # MLX doesn't support float64, downcast
        np.int8: mx.int8,
        np.int16: mx.int16,
        np.int32: mx.int32,
        np.int64: mx.int64,
        np.uint8: mx.uint8,
        np.uint16: mx.uint16,
        np.uint32: mx.uint32,
        np.uint64: mx.uint64,
        np.bool_: mx.bool_,
    }
    return dtype_map.get(dtype.type, mx.float32)


class MetalBackend:
    """Metal backend using MLX arrays.

    This backend provides GPU acceleration on Apple Silicon.
    Requires MLX to be installed and macOS to be running.
    """

    @property
    def backend_type(self) -> BackendType:
        """Return the backend type."""
        return BackendType.METAL

    def is_available(self) -> bool:
        """Check if Metal is available.

        Returns:
            True if MLX is installed and running on macOS.
        """
        if platform.system() != "Darwin":
            return False
        mx = _get_mlx()
        if mx is None:
            return False
        try:
            # Verify we can get the default device
            mx.default_device()
            return True
        except Exception:
            return False

    def array(self, data: ArrayLike, dtype: DTypeLike | None = None) -> Any:
        """Create an MLX array.

        Args:
            data: Input data (list, tuple, numpy array, etc.).
            dtype: Optional data type for the array.

        Returns:
            MLX array.

        Raises:
            RuntimeError: If MLX is not available.
        """
        mx = _get_mlx()
        if mx is None:
            raise RuntimeError(
                "MLX is not installed or not available on this system. "
                "Install with: pip install mlx (macOS only)"
            )
        # Convert to numpy first for consistent handling
        np_arr = np.asarray(data)
        if dtype is not None:
            mlx_dtype = _numpy_dtype_to_mlx(dtype)
            return mx.array(np_arr, dtype=mlx_dtype)
        return mx.array(np_arr)

    def zeros(
        self, shape: tuple[int, ...] | int, dtype: DTypeLike = np.float32
    ) -> Any:
        """Create a zero-filled MLX array.

        Args:
            shape: Shape of the array.
            dtype: Data type for the array.

        Returns:
            Zero-filled MLX array.

        Raises:
            RuntimeError: If MLX is not available.
        """
        mx = _get_mlx()
        if mx is None:
            raise RuntimeError(
                "MLX is not installed or not available on this system. "
                "Install with: pip install mlx (macOS only)"
            )
        if isinstance(shape, int):
            shape = (shape,)
        mlx_dtype = _numpy_dtype_to_mlx(dtype)
        return mx.zeros(shape, dtype=mlx_dtype)

    def ones(self, shape: tuple[int, ...] | int, dtype: DTypeLike = np.float32) -> Any:
        """Create an MLX array filled with ones.

        Args:
            shape: Shape of the array.
            dtype: Data type for the array.

        Returns:
            MLX array filled with ones.

        Raises:
            RuntimeError: If MLX is not available.
        """
        mx = _get_mlx()
        if mx is None:
            raise RuntimeError(
                "MLX is not installed or not available on this system. "
                "Install with: pip install mlx (macOS only)"
            )
        if isinstance(shape, int):
            shape = (shape,)
        mlx_dtype = _numpy_dtype_to_mlx(dtype)
        return mx.ones(shape, dtype=mlx_dtype)

    def empty(
        self, shape: tuple[int, ...] | int, dtype: DTypeLike = np.float32
    ) -> Any:
        """Create an uninitialized MLX array.

        Note: MLX doesn't have a true empty(), so we use zeros().

        Args:
            shape: Shape of the array.
            dtype: Data type for the array.

        Returns:
            MLX array (zero-filled).

        Raises:
            RuntimeError: If MLX is not available.
        """
        # MLX doesn't have empty, use zeros instead
        return self.zeros(shape, dtype)

    def to_numpy(self, arr: Any) -> NDArray[Any]:
        """Transfer MLX array back to CPU.

        Args:
            arr: MLX array.

        Returns:
            NumPy array with the same data.
        """
        mx = _get_mlx()
        if mx is None:
            return np.asarray(arr)
        if hasattr(arr, "__array__"):
            # MLX arrays support numpy conversion via __array__
            return np.array(arr)
        return np.asarray(arr)

    def from_numpy(self, arr: NDArray[Any]) -> Any:
        """Transfer numpy array to MLX.

        Args:
            arr: NumPy array to transfer.

        Returns:
            MLX array.

        Raises:
            RuntimeError: If MLX is not available.
        """
        mx = _get_mlx()
        if mx is None:
            raise RuntimeError(
                "MLX is not installed or not available on this system. "
                "Install with: pip install mlx (macOS only)"
            )
        return mx.array(arr)

    def synchronize(self) -> None:
        """Synchronize MLX operations.

        Ensures all operations are complete and evaluated.
        """
        mx = _get_mlx()
        if mx is not None:
            mx.eval()  # Force evaluation of lazy operations


# Protocol compliance check (only if MLX is available)
def _check_protocol():
    backend = MetalBackend()
    if backend.is_available():
        assert isinstance(backend, ArrayBackend)
