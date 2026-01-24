"""Backend enum and base protocol for GPU acceleration.

Defines the BackendType enum and ArrayBackend protocol that all backends
(CUDA, Metal, CPU) must implement for unified array operations.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import ArrayLike, DTypeLike, NDArray


class BackendType(Enum):
    """Supported GPU backend types."""

    CUDA = "cuda"
    METAL = "metal"
    CPU = "cpu"


@runtime_checkable
class ArrayBackend(Protocol):
    """Protocol for array operations across backends.

    All backend implementations (CUDA, Metal, CPU) must implement this
    protocol to provide a consistent interface for array operations.
    """

    @property
    def backend_type(self) -> BackendType:
        """Return the backend type."""
        ...

    def is_available(self) -> bool:
        """Check if this backend is usable on the current system.

        Returns:
            True if the backend can be used, False otherwise.
        """
        ...

    def array(self, data: ArrayLike, dtype: DTypeLike | None = None) -> Any:
        """Create an array on this backend.

        Args:
            data: Input data (list, tuple, numpy array, etc.).
            dtype: Optional data type for the array.

        Returns:
            Array on this backend.
        """
        ...

    def zeros(self, shape: tuple[int, ...] | int, dtype: DTypeLike = np.float32) -> Any:
        """Create a zero-filled array.

        Args:
            shape: Shape of the array.
            dtype: Data type for the array.

        Returns:
            Zero-filled array on this backend.
        """
        ...

    def ones(self, shape: tuple[int, ...] | int, dtype: DTypeLike = np.float32) -> Any:
        """Create an array filled with ones.

        Args:
            shape: Shape of the array.
            dtype: Data type for the array.

        Returns:
            Array filled with ones on this backend.
        """
        ...

    def empty(self, shape: tuple[int, ...] | int, dtype: DTypeLike = np.float32) -> Any:
        """Create an uninitialized array.

        Args:
            shape: Shape of the array.
            dtype: Data type for the array.

        Returns:
            Uninitialized array on this backend.
        """
        ...

    def to_numpy(self, arr: Any) -> NDArray[Any]:
        """Transfer array back to CPU as numpy array.

        Args:
            arr: Array on this backend.

        Returns:
            NumPy array with the same data.
        """
        ...

    def from_numpy(self, arr: NDArray[Any]) -> Any:
        """Transfer numpy array to this backend.

        Args:
            arr: NumPy array to transfer.

        Returns:
            Array on this backend.
        """
        ...

    def synchronize(self) -> None:
        """Synchronize the backend (wait for all operations to complete).

        For GPU backends, this ensures all kernel executions are complete.
        For CPU backend, this is a no-op.
        """
        ...
