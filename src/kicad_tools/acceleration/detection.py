"""Hardware detection utilities for GPU backends.

Provides functions to detect available GPU backends and select
the best backend based on system capabilities.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kicad_tools.acceleration.backend import ArrayBackend, BackendType

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def detect_backends() -> list[BackendType]:
    """Return list of available backends in priority order.

    Checks for available GPU backends (CUDA, Metal) and always includes
    CPU as a fallback. The list is ordered by preference: GPU backends
    first, then CPU.

    Returns:
        List of available BackendType values in priority order.

    Examples:
        >>> backends = detect_backends()
        >>> BackendType.CPU in backends  # Always available
        True
        >>> backends[0] if len(backends) > 1 else None  # Preferred GPU if available
    """
    available: list[BackendType] = []

    # Check CUDA (CuPy)
    if _check_cuda_available():
        available.append(BackendType.CUDA)
        logger.debug("CUDA backend available")

    # Check Metal (MLX)
    if _check_metal_available():
        available.append(BackendType.METAL)
        logger.debug("Metal backend available")

    # CPU is always available
    available.append(BackendType.CPU)
    logger.debug("CPU backend available (fallback)")

    return available


def _check_cuda_available() -> bool:
    """Check if CUDA is available via CuPy.

    Returns:
        True if CuPy is installed and CUDA devices are available.
    """
    try:
        import cupy

        device_count = cupy.cuda.runtime.getDeviceCount()
        return device_count > 0
    except ImportError:
        return False
    except Exception as e:
        logger.debug(f"CUDA check failed: {e}")
        return False


def _check_metal_available() -> bool:
    """Check if Metal is available via MLX.

    Returns:
        True if MLX is installed and running on macOS.
    """
    try:
        import platform

        if platform.system() != "Darwin":
            return False

        import mlx.core as mx

        # Verify we can get the default device
        mx.default_device()
        return True
    except ImportError:
        return False
    except Exception as e:
        logger.debug(f"Metal check failed: {e}")
        return False


def get_backend(
    preferred: BackendType | str | None = None,
    device_id: int = 0,
) -> ArrayBackend:
    """Get the best available backend, or preferred if available.

    If a preferred backend is specified and available, it will be used.
    Otherwise, the best available backend is returned (GPU before CPU).

    Args:
        preferred: Preferred backend type (BackendType enum or string).
            If None or "auto", the best available backend is used.
        device_id: Device ID for multi-GPU systems (CUDA only).

    Returns:
        An ArrayBackend instance.

    Raises:
        ValueError: If the preferred backend is not available.

    Examples:
        >>> backend = get_backend()  # Best available
        >>> backend = get_backend(BackendType.CPU)  # Force CPU
        >>> backend = get_backend("cuda")  # Request CUDA
    """
    from kicad_tools.acceleration.cpu import CPUBackend
    from kicad_tools.acceleration.cuda import CUDABackend
    from kicad_tools.acceleration.metal import MetalBackend

    # Normalize preferred to BackendType
    if preferred is None or preferred == "auto":
        preferred_type = None
    elif isinstance(preferred, str):
        try:
            preferred_type = BackendType(preferred.lower())
        except ValueError:
            raise ValueError(
                f"Unknown backend type: {preferred}. "
                f"Valid options: {[b.value for b in BackendType]}"
            )
    else:
        preferred_type = preferred

    # If specific backend requested, try to use it
    if preferred_type is not None:
        if preferred_type == BackendType.CPU:
            return CPUBackend()

        if preferred_type == BackendType.CUDA:
            backend = CUDABackend(device_id=device_id)
            if backend.is_available():
                logger.info(f"Using CUDA backend (device {device_id})")
                return backend
            raise ValueError(
                "CUDA backend requested but not available. "
                "Ensure CuPy is installed and NVIDIA GPU is present."
            )

        if preferred_type == BackendType.METAL:
            backend = MetalBackend()
            if backend.is_available():
                logger.info("Using Metal backend")
                return backend
            raise ValueError(
                "Metal backend requested but not available. "
                "Ensure MLX is installed and running on macOS with Apple Silicon."
            )

    # Auto-select best available backend
    available = detect_backends()

    if not available:
        # Should never happen since CPU is always available
        logger.warning("No backends available, falling back to CPU")
        return CPUBackend()

    best = available[0]

    if best == BackendType.CUDA:
        logger.info(f"Auto-selected CUDA backend (device {device_id})")
        return CUDABackend(device_id=device_id)

    if best == BackendType.METAL:
        logger.info("Auto-selected Metal backend")
        return MetalBackend()

    logger.info("Using CPU backend")
    return CPUBackend()


def get_backend_info() -> dict[str, bool | str]:
    """Get information about available backends.

    Returns:
        Dictionary with backend availability and system info.

    Examples:
        >>> info = get_backend_info()
        >>> info["cuda_available"]
        False
        >>> info["metal_available"]
        True
    """
    import platform

    available = detect_backends()

    info: dict[str, bool | str] = {
        "platform": platform.system(),
        "machine": platform.machine(),
        "cuda_available": BackendType.CUDA in available,
        "metal_available": BackendType.METAL in available,
        "cpu_available": BackendType.CPU in available,  # Always True
        "preferred_backend": available[0].value if available else "cpu",
    }

    # Add GPU-specific info if available
    if BackendType.CUDA in available:
        try:
            import cupy

            info["cuda_device_count"] = cupy.cuda.runtime.getDeviceCount()
            info["cuda_device_name"] = cupy.cuda.Device(0).name
        except Exception:
            pass

    if BackendType.METAL in available:
        try:
            import mlx.core as mx

            info["metal_device"] = str(mx.default_device())
        except Exception:
            pass

    return info
