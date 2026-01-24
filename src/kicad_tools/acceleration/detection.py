"""
GPU detection and installation suggestion utilities.

Provides platform-aware GPU detection and recommends the appropriate
pip install command for GPU acceleration.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class GPUBackend(Enum):
    """Available GPU acceleration backends."""

    NONE = "none"
    CUDA = "cuda"
    METAL = "metal"


@dataclass
class GPUInfo:
    """Information about detected GPU capabilities."""

    backend: GPUBackend
    available: bool
    device_name: str | None = None
    reason: str | None = None

    def __str__(self) -> str:
        if self.available:
            return f"{self.backend.value}: {self.device_name}"
        return f"{self.backend.value}: not available ({self.reason})"


def _check_cuda() -> GPUInfo:
    """Check for CUDA availability via CuPy."""
    try:
        import cupy as cp

        device = cp.cuda.Device(0)
        props = cp.cuda.runtime.getDeviceProperties(device.id)
        device_name = props["name"].decode() if isinstance(props["name"], bytes) else props["name"]
        return GPUInfo(
            backend=GPUBackend.CUDA,
            available=True,
            device_name=device_name,
        )
    except ImportError:
        return GPUInfo(
            backend=GPUBackend.CUDA,
            available=False,
            reason="cupy not installed",
        )
    except Exception as e:
        return GPUInfo(
            backend=GPUBackend.CUDA,
            available=False,
            reason=str(e),
        )


def _check_metal() -> GPUInfo:
    """Check for Metal availability via MLX."""
    try:
        import mlx.core as mx

        # MLX automatically uses Metal on Apple Silicon
        # Check if we can create a simple array (validates Metal is working)
        _ = mx.array([1.0, 2.0, 3.0])
        return GPUInfo(
            backend=GPUBackend.METAL,
            available=True,
            device_name="Apple Silicon GPU",
        )
    except ImportError:
        return GPUInfo(
            backend=GPUBackend.METAL,
            available=False,
            reason="mlx not installed",
        )
    except Exception as e:
        return GPUInfo(
            backend=GPUBackend.METAL,
            available=False,
            reason=str(e),
        )


def detect_gpu() -> GPUInfo:
    """Detect the best available GPU backend for the current platform.

    Returns:
        GPUInfo with details about the detected GPU backend.
    """
    system = platform.system()
    machine = platform.machine()

    # On macOS with Apple Silicon, prefer Metal
    if system == "Darwin" and machine == "arm64":
        metal_info = _check_metal()
        if metal_info.available:
            return metal_info
        # Fall through to check CUDA (unlikely but possible via external GPU)

    # On Linux/Windows or Intel Mac, try CUDA
    if system in ("Linux", "Windows") or (system == "Darwin" and machine != "arm64"):
        cuda_info = _check_cuda()
        if cuda_info.available:
            return cuda_info

    # Also check Metal on macOS even if not arm64 (in case of future support)
    if system == "Darwin":
        metal_info = _check_metal()
        if metal_info.available:
            return metal_info

    return GPUInfo(
        backend=GPUBackend.NONE,
        available=False,
        reason="no GPU acceleration available",
    )


def get_available_backends() -> list[GPUInfo]:
    """Get information about all potentially available GPU backends.

    Returns:
        List of GPUInfo for each backend (CUDA, Metal).
    """
    backends = []

    # Check CUDA
    backends.append(_check_cuda())

    # Check Metal (only on macOS)
    if platform.system() == "Darwin":
        backends.append(_check_metal())

    return backends


def suggest_install_command() -> str:
    """Suggest the right pip install command for GPU acceleration on this platform.

    Returns:
        A pip install command string appropriate for the current platform.
    """
    system = platform.system()
    machine = platform.machine()

    if system == "Darwin":
        if machine == "arm64":
            return "pip install kicad-tools[metal]"
        return "pip install kicad-tools  # No GPU acceleration for Intel Mac"
    elif system == "Linux":
        return "pip install kicad-tools[cuda]"
    else:
        # Windows or other
        return "pip install kicad-tools[cuda]  # Requires NVIDIA GPU"


def show_gpu_status(verbose: bool = False) -> None:
    """Print GPU acceleration status to stdout.

    Args:
        verbose: If True, show detailed information about all backends.
    """
    print("GPU Acceleration Status")
    print("=" * 40)
    print()

    # Current detection
    gpu = detect_gpu()
    if gpu.available:
        print(f"Active backend: {gpu.backend.value.upper()}")
        print(f"Device: {gpu.device_name}")
    else:
        print("No GPU acceleration available")

    print()

    if verbose:
        print("Backend Details:")
        print("-" * 40)
        for backend in get_available_backends():
            status = "available" if backend.available else "not available"
            print(f"  {backend.backend.value.upper()}: {status}")
            if backend.available:
                print(f"    Device: {backend.device_name}")
            elif backend.reason:
                print(f"    Reason: {backend.reason}")
        print()

    # Installation suggestion
    print("Suggested installation:")
    print(f"  {suggest_install_command()}")
    print()
