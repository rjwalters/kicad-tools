"""CUDA backend using CuPy.

This backend provides GPU acceleration on NVIDIA GPUs using CuPy.
CuPy must be installed for this backend to be available.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import ArrayLike, DTypeLike, NDArray

from kicad_tools.acceleration.backend import ArrayBackend, BackendType

# Lazy import flag
_cupy = None
_cupy_import_attempted = False


def _get_cupy():
    """Lazy import CuPy to avoid startup overhead."""
    global _cupy, _cupy_import_attempted
    if not _cupy_import_attempted:
        _cupy_import_attempted = True
        try:
            import cupy

            _cupy = cupy
        except ImportError:
            _cupy = None
    return _cupy


class CUDABackend:
    """CUDA backend using CuPy arrays.

    This backend provides GPU acceleration on NVIDIA GPUs.
    Requires CuPy to be installed.
    """

    def __init__(self, device_id: int = 0):
        """Initialize CUDA backend.

        Args:
            device_id: CUDA device ID to use (default: 0).
        """
        self._device_id = device_id

    @property
    def backend_type(self) -> BackendType:
        """Return the backend type."""
        return BackendType.CUDA

    def is_available(self) -> bool:
        """Check if CUDA is available.

        Returns:
            True if CuPy is installed and CUDA devices are available.
        """
        cp = _get_cupy()
        if cp is None:
            return False
        try:
            device_count = cp.cuda.runtime.getDeviceCount()
            return device_count > 0 and self._device_id < device_count
        except Exception:
            return False

    def array(self, data: ArrayLike, dtype: DTypeLike | None = None) -> Any:
        """Create a CuPy array on GPU.

        Args:
            data: Input data (list, tuple, numpy array, etc.).
            dtype: Optional data type for the array.

        Returns:
            CuPy array on GPU.

        Raises:
            RuntimeError: If CUDA is not available.
        """
        cp = _get_cupy()
        if cp is None:
            raise RuntimeError("CuPy is not installed. Install with: pip install cupy")
        with cp.cuda.Device(self._device_id):
            return cp.asarray(data, dtype=dtype)

    def zeros(
        self, shape: tuple[int, ...] | int, dtype: DTypeLike = np.float32
    ) -> Any:
        """Create a zero-filled CuPy array on GPU.

        Args:
            shape: Shape of the array.
            dtype: Data type for the array.

        Returns:
            Zero-filled CuPy array on GPU.

        Raises:
            RuntimeError: If CUDA is not available.
        """
        cp = _get_cupy()
        if cp is None:
            raise RuntimeError("CuPy is not installed. Install with: pip install cupy")
        with cp.cuda.Device(self._device_id):
            return cp.zeros(shape, dtype=dtype)

    def ones(self, shape: tuple[int, ...] | int, dtype: DTypeLike = np.float32) -> Any:
        """Create a CuPy array filled with ones on GPU.

        Args:
            shape: Shape of the array.
            dtype: Data type for the array.

        Returns:
            CuPy array filled with ones on GPU.

        Raises:
            RuntimeError: If CUDA is not available.
        """
        cp = _get_cupy()
        if cp is None:
            raise RuntimeError("CuPy is not installed. Install with: pip install cupy")
        with cp.cuda.Device(self._device_id):
            return cp.ones(shape, dtype=dtype)

    def empty(
        self, shape: tuple[int, ...] | int, dtype: DTypeLike = np.float32
    ) -> Any:
        """Create an uninitialized CuPy array on GPU.

        Args:
            shape: Shape of the array.
            dtype: Data type for the array.

        Returns:
            Uninitialized CuPy array on GPU.

        Raises:
            RuntimeError: If CUDA is not available.
        """
        cp = _get_cupy()
        if cp is None:
            raise RuntimeError("CuPy is not installed. Install with: pip install cupy")
        with cp.cuda.Device(self._device_id):
            return cp.empty(shape, dtype=dtype)

    def to_numpy(self, arr: Any) -> NDArray[Any]:
        """Transfer CuPy array back to CPU.

        Args:
            arr: CuPy array on GPU.

        Returns:
            NumPy array with the same data.
        """
        cp = _get_cupy()
        if cp is None:
            # Assume it's already a numpy array
            return np.asarray(arr)
        if isinstance(arr, cp.ndarray):
            return cp.asnumpy(arr)
        return np.asarray(arr)

    def from_numpy(self, arr: NDArray[Any]) -> Any:
        """Transfer numpy array to GPU.

        Args:
            arr: NumPy array to transfer.

        Returns:
            CuPy array on GPU.

        Raises:
            RuntimeError: If CUDA is not available.
        """
        cp = _get_cupy()
        if cp is None:
            raise RuntimeError("CuPy is not installed. Install with: pip install cupy")
        with cp.cuda.Device(self._device_id):
            return cp.asarray(arr)

    def synchronize(self) -> None:
        """Synchronize CUDA stream (wait for all operations to complete).

        Ensures all kernel executions on this device are complete.
        """
        cp = _get_cupy()
        if cp is not None:
            with cp.cuda.Device(self._device_id):
                cp.cuda.Stream.null.synchronize()


# Protocol compliance check (only if CuPy is available)
# This is deferred to avoid import errors
def _check_protocol():
    backend = CUDABackend()
    if backend.is_available():
        assert isinstance(backend, ArrayBackend)
