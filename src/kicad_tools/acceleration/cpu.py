"""CPU backend using NumPy.

This is the fallback backend that is always available. It provides a
consistent interface using NumPy arrays.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import ArrayLike, DTypeLike, NDArray

from kicad_tools.acceleration.backend import ArrayBackend, BackendType


class CPUBackend:
    """CPU backend using NumPy arrays.

    This backend is always available and provides a consistent interface
    for array operations using NumPy.
    """

    @property
    def backend_type(self) -> BackendType:
        """Return the backend type."""
        return BackendType.CPU

    def is_available(self) -> bool:
        """CPU backend is always available.

        Returns:
            Always True.
        """
        return True

    def array(self, data: ArrayLike, dtype: DTypeLike | None = None) -> NDArray[Any]:
        """Create a numpy array.

        Args:
            data: Input data (list, tuple, numpy array, etc.).
            dtype: Optional data type for the array.

        Returns:
            NumPy array.
        """
        return np.asarray(data, dtype=dtype)

    def zeros(
        self, shape: tuple[int, ...] | int, dtype: DTypeLike = np.float32
    ) -> NDArray[Any]:
        """Create a zero-filled numpy array.

        Args:
            shape: Shape of the array.
            dtype: Data type for the array.

        Returns:
            Zero-filled NumPy array.
        """
        return np.zeros(shape, dtype=dtype)

    def ones(
        self, shape: tuple[int, ...] | int, dtype: DTypeLike = np.float32
    ) -> NDArray[Any]:
        """Create a numpy array filled with ones.

        Args:
            shape: Shape of the array.
            dtype: Data type for the array.

        Returns:
            NumPy array filled with ones.
        """
        return np.ones(shape, dtype=dtype)

    def empty(
        self, shape: tuple[int, ...] | int, dtype: DTypeLike = np.float32
    ) -> NDArray[Any]:
        """Create an uninitialized numpy array.

        Args:
            shape: Shape of the array.
            dtype: Data type for the array.

        Returns:
            Uninitialized NumPy array.
        """
        return np.empty(shape, dtype=dtype)

    def to_numpy(self, arr: NDArray[Any]) -> NDArray[Any]:
        """Return the array as-is (already numpy).

        Args:
            arr: NumPy array.

        Returns:
            The same NumPy array.
        """
        return np.asarray(arr)

    def from_numpy(self, arr: NDArray[Any]) -> NDArray[Any]:
        """Return the array as-is (already numpy).

        Args:
            arr: NumPy array.

        Returns:
            The same NumPy array.
        """
        return np.asarray(arr)

    def synchronize(self) -> None:
        """No-op for CPU backend.

        CPU operations are synchronous, so no synchronization needed.
        """
        pass


# Verify protocol compliance at module load time
assert isinstance(CPUBackend(), ArrayBackend)
