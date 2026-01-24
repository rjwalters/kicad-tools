"""GPU configuration and backend abstraction.

Provides a unified interface for GPU computations that works with CUDA,
Metal, or falls back to CPU (NumPy) when no GPU is available.

Example::

    from kicad_tools.acceleration.config import get_backend, should_use_gpu
    from kicad_tools.performance import PerformanceConfig

    config = PerformanceConfig.load_calibrated()

    if should_use_gpu(config, 500, "signal_integrity"):
        backend = get_backend(config)
        arr = backend.array([1, 2, 3])
        result = backend.sqrt(arr)
        numpy_result = backend.to_numpy(result)
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    pass

# Type aliases
GpuBackend = Literal["auto", "cuda", "metal", "cpu"]
ProblemType = Literal["grid", "placement", "evolutionary", "signal_integrity"]


@dataclass
class GpuThresholds:
    """Minimum problem sizes for GPU acceleration to be beneficial.

    Below these thresholds, CPU is typically faster due to GPU transfer overhead.

    Attributes:
        min_grid_cells: Minimum routing grid cells (default: 100,000).
        min_components: Minimum components for placement optimization (default: 50).
        min_population: Minimum evolutionary algorithm population (default: 20).
        min_trace_pairs: Minimum trace pairs for signal integrity analysis (default: 100).
    """

    min_grid_cells: int = 100_000
    min_components: int = 50
    min_population: int = 20
    min_trace_pairs: int = 100


@dataclass
class GpuConfig:
    """Configuration for GPU acceleration.

    Attributes:
        backend: GPU backend to use ("auto", "cuda", "metal", or "cpu").
        device_id: GPU device ID (0 for primary).
        memory_limit_mb: Maximum GPU memory to use (0 = unlimited).
        thresholds: Problem size thresholds for GPU usage.
        enabled: Whether GPU acceleration is enabled.
    """

    backend: GpuBackend = "auto"
    device_id: int = 0
    memory_limit_mb: int = 0
    thresholds: GpuThresholds = field(default_factory=GpuThresholds)
    enabled: bool = True

    def __post_init__(self) -> None:
        """Detect GPU backend if set to auto."""
        if self.backend == "auto":
            self.backend = detect_gpu_backend()


def detect_gpu_backend() -> GpuBackend:
    """Auto-detect the best available GPU backend.

    Returns:
        "cuda" if NVIDIA GPU with CUDA is available,
        "metal" if Apple Silicon with Metal is available,
        "cpu" otherwise.
    """
    # Check for CUDA (NVIDIA)
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Verify CuPy is available
            try:
                import cupy  # noqa: F401

                return "cuda"
            except ImportError:
                pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Check for Metal (Apple Silicon)
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and "Apple" in result.stdout:
                # Metal is available on Apple Silicon
                # Check for MLX (Apple's ML framework)
                try:
                    import mlx.core  # noqa: F401

                    return "metal"
                except ImportError:
                    pass
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return "cpu"


def should_use_gpu(
    config: Any,  # PerformanceConfig, but avoiding circular import
    problem_size: int,
    problem_type: ProblemType,
) -> bool:
    """Determine if GPU should be used for a given problem.

    Compares the problem size against configured thresholds.

    Args:
        config: PerformanceConfig instance (must have gpu attribute).
        problem_size: Size of the problem (interpretation depends on problem_type).
        problem_type: Type of problem being solved.

    Returns:
        True if GPU should be used, False for CPU.

    Problem size interpretation by type:
        - "grid": Number of routing grid cells
        - "placement": Number of components
        - "evolutionary": Population size
        - "signal_integrity": Number of trace pairs
    """
    # Check if config has GPU settings
    gpu_config = getattr(config, "gpu", None)
    if gpu_config is None:
        return False

    if not gpu_config.enabled:
        return False

    if gpu_config.backend == "cpu":
        return False

    thresholds = gpu_config.thresholds

    # Get threshold for problem type
    threshold_map = {
        "grid": thresholds.min_grid_cells,
        "placement": thresholds.min_components,
        "evolutionary": thresholds.min_population,
        "signal_integrity": thresholds.min_trace_pairs,
    }

    threshold = threshold_map.get(problem_type, 0)
    return problem_size >= threshold


@runtime_checkable
class ArrayBackend(Protocol):
    """Protocol for array backend operations.

    This protocol defines the interface that all backends (CUDA, Metal, CPU)
    must implement for vectorized array operations.
    """

    def array(self, data: Any, dtype: Any = None) -> Any:
        """Create an array from data."""
        ...

    def zeros(self, shape: tuple[int, ...], dtype: Any = None) -> Any:
        """Create a zero-filled array."""
        ...

    def ones(self, shape: tuple[int, ...], dtype: Any = None) -> Any:
        """Create a one-filled array."""
        ...

    def sqrt(self, arr: Any) -> Any:
        """Element-wise square root."""
        ...

    def log(self, arr: Any) -> Any:
        """Element-wise natural logarithm."""
        ...

    def exp(self, arr: Any) -> Any:
        """Element-wise exponential."""
        ...

    def sum(self, arr: Any, axis: int | None = None) -> Any:
        """Sum of array elements."""
        ...

    def max(self, arr: Any, axis: int | None = None) -> Any:
        """Maximum of array elements."""
        ...

    def min(self, arr: Any, axis: int | None = None) -> Any:
        """Minimum of array elements."""
        ...

    def fill_diagonal(self, arr: Any, value: float) -> Any:
        """Fill diagonal of a 2D array with a value."""
        ...

    def to_numpy(self, arr: Any) -> NDArray[Any]:
        """Convert array to NumPy array."""
        ...


class NumpyBackend:
    """CPU backend using NumPy.

    This is the fallback backend when no GPU is available.
    """

    def __init__(self) -> None:
        """Initialize the NumPy backend."""
        self._default_dtype = np.float64

    def array(self, data: Any, dtype: Any = None) -> NDArray[Any]:
        """Create a NumPy array from data."""
        return np.array(data, dtype=dtype or self._default_dtype)

    def zeros(self, shape: tuple[int, ...], dtype: Any = None) -> NDArray[Any]:
        """Create a zero-filled array."""
        return np.zeros(shape, dtype=dtype or self._default_dtype)

    def ones(self, shape: tuple[int, ...], dtype: Any = None) -> NDArray[Any]:
        """Create a one-filled array."""
        return np.ones(shape, dtype=dtype or self._default_dtype)

    def sqrt(self, arr: NDArray[Any]) -> NDArray[Any]:
        """Element-wise square root."""
        return np.sqrt(arr)

    def log(self, arr: NDArray[Any]) -> NDArray[Any]:
        """Element-wise natural logarithm."""
        return np.log(arr)

    def exp(self, arr: NDArray[Any]) -> NDArray[Any]:
        """Element-wise exponential."""
        return np.exp(arr)

    def sum(self, arr: NDArray[Any], axis: int | None = None) -> NDArray[Any]:
        """Sum of array elements."""
        return np.sum(arr, axis=axis)

    def max(self, arr: NDArray[Any], axis: int | None = None) -> NDArray[Any]:
        """Maximum of array elements."""
        return np.max(arr, axis=axis)

    def min(self, arr: NDArray[Any], axis: int | None = None) -> NDArray[Any]:
        """Minimum of array elements."""
        return np.min(arr, axis=axis)

    def fill_diagonal(self, arr: NDArray[Any], value: float) -> NDArray[Any]:
        """Fill diagonal of a 2D array with a value."""
        result = arr.copy()
        np.fill_diagonal(result, value)
        return result

    def to_numpy(self, arr: NDArray[Any]) -> NDArray[Any]:
        """Return the array as-is (already NumPy)."""
        return arr


class CupyBackend:
    """CUDA backend using CuPy.

    Provides GPU acceleration on NVIDIA GPUs.
    """

    def __init__(self, device_id: int = 0) -> None:
        """Initialize the CuPy backend.

        Args:
            device_id: CUDA device ID to use.
        """
        import cupy as cp

        self._cp = cp
        self._device_id = device_id
        self._default_dtype = cp.float64

        # Set the device
        cp.cuda.Device(device_id).use()

    def array(self, data: Any, dtype: Any = None) -> Any:
        """Create a CuPy array from data."""
        return self._cp.array(data, dtype=dtype or self._default_dtype)

    def zeros(self, shape: tuple[int, ...], dtype: Any = None) -> Any:
        """Create a zero-filled array."""
        return self._cp.zeros(shape, dtype=dtype or self._default_dtype)

    def ones(self, shape: tuple[int, ...], dtype: Any = None) -> Any:
        """Create a one-filled array."""
        return self._cp.ones(shape, dtype=dtype or self._default_dtype)

    def sqrt(self, arr: Any) -> Any:
        """Element-wise square root."""
        return self._cp.sqrt(arr)

    def log(self, arr: Any) -> Any:
        """Element-wise natural logarithm."""
        return self._cp.log(arr)

    def exp(self, arr: Any) -> Any:
        """Element-wise exponential."""
        return self._cp.exp(arr)

    def sum(self, arr: Any, axis: int | None = None) -> Any:
        """Sum of array elements."""
        return self._cp.sum(arr, axis=axis)

    def max(self, arr: Any, axis: int | None = None) -> Any:
        """Maximum of array elements."""
        return self._cp.max(arr, axis=axis)

    def min(self, arr: Any, axis: int | None = None) -> Any:
        """Minimum of array elements."""
        return self._cp.min(arr, axis=axis)

    def fill_diagonal(self, arr: Any, value: float) -> Any:
        """Fill diagonal of a 2D array with a value."""
        result = arr.copy()
        self._cp.fill_diagonal(result, value)
        return result

    def to_numpy(self, arr: Any) -> NDArray[Any]:
        """Convert CuPy array to NumPy array."""
        return self._cp.asnumpy(arr)


class MlxBackend:
    """Metal backend using MLX.

    Provides GPU acceleration on Apple Silicon.
    """

    def __init__(self) -> None:
        """Initialize the MLX backend."""
        import mlx.core as mx

        self._mx = mx
        self._default_dtype = mx.float32  # MLX prefers float32

    def array(self, data: Any, dtype: Any = None) -> Any:
        """Create an MLX array from data."""
        # MLX doesn't accept dtype directly in array()
        arr = self._mx.array(data)
        if dtype is not None:
            arr = arr.astype(dtype)
        return arr

    def zeros(self, shape: tuple[int, ...], dtype: Any = None) -> Any:
        """Create a zero-filled array."""
        return self._mx.zeros(shape, dtype=dtype or self._default_dtype)

    def ones(self, shape: tuple[int, ...], dtype: Any = None) -> Any:
        """Create a one-filled array."""
        return self._mx.ones(shape, dtype=dtype or self._default_dtype)

    def sqrt(self, arr: Any) -> Any:
        """Element-wise square root."""
        return self._mx.sqrt(arr)

    def log(self, arr: Any) -> Any:
        """Element-wise natural logarithm."""
        return self._mx.log(arr)

    def exp(self, arr: Any) -> Any:
        """Element-wise exponential."""
        return self._mx.exp(arr)

    def sum(self, arr: Any, axis: int | None = None) -> Any:
        """Sum of array elements."""
        if axis is None:
            return self._mx.sum(arr)
        return self._mx.sum(arr, axis=axis)

    def max(self, arr: Any, axis: int | None = None) -> Any:
        """Maximum of array elements."""
        if axis is None:
            return self._mx.max(arr)
        return self._mx.max(arr, axis=axis)

    def min(self, arr: Any, axis: int | None = None) -> Any:
        """Minimum of array elements."""
        if axis is None:
            return self._mx.min(arr)
        return self._mx.min(arr, axis=axis)

    def fill_diagonal(self, arr: Any, value: float) -> Any:
        """Fill diagonal of a 2D array with a value.

        MLX doesn't have a direct fill_diagonal, so we use NumPy conversion.
        """
        # Convert to numpy, fill diagonal, convert back
        np_arr = np.array(arr)
        np.fill_diagonal(np_arr, value)
        return self._mx.array(np_arr)

    def to_numpy(self, arr: Any) -> NDArray[Any]:
        """Convert MLX array to NumPy array."""
        return np.array(arr)


# Global backend cache
_backend_cache: dict[str, ArrayBackend] = {}


def get_backend(config: Any = None) -> ArrayBackend:
    """Get the appropriate array backend based on configuration.

    Args:
        config: PerformanceConfig instance (optional). If None, uses CPU backend.

    Returns:
        ArrayBackend instance for the configured GPU backend.
    """
    global _backend_cache

    # Determine backend type
    if config is None:
        backend_type = "cpu"
    else:
        gpu_config = getattr(config, "gpu", None)
        if gpu_config is None:
            backend_type = "cpu"
        else:
            backend_type = gpu_config.backend
            if backend_type == "auto":
                backend_type = detect_gpu_backend()

    # Check cache
    if backend_type in _backend_cache:
        return _backend_cache[backend_type]

    # Create backend
    if backend_type == "cuda":
        try:
            device_id = 0
            if config is not None:
                gpu_config = getattr(config, "gpu", None)
                if gpu_config is not None:
                    device_id = gpu_config.device_id
            backend = CupyBackend(device_id)
        except ImportError:
            backend = NumpyBackend()
            backend_type = "cpu"
    elif backend_type == "metal":
        try:
            backend = MlxBackend()
        except ImportError:
            backend = NumpyBackend()
            backend_type = "cpu"
    else:
        backend = NumpyBackend()
        backend_type = "cpu"

    _backend_cache[backend_type] = backend
    return backend


def clear_backend_cache() -> None:
    """Clear the backend cache.

    Useful for testing or when GPU configuration changes.
    """
    global _backend_cache
    _backend_cache.clear()
