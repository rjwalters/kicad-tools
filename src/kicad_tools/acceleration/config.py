"""
GPU configuration helpers for kicad-tools acceleration.

Provides utilities for determining when GPU acceleration should be used
based on problem size and configuration thresholds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from kicad_tools.performance import PerformanceConfig

# Problem types that support GPU acceleration
ProblemType = Literal["grid", "placement", "evolutionary", "signal_integrity"]


def should_use_gpu(
    config: PerformanceConfig,
    problem_size: int,
    problem_type: ProblemType,
) -> bool:
    """Determine if GPU should be used based on config and problem size.

    GPU acceleration has overhead from memory transfers, so it's only
    worthwhile for problems above certain sizes. This function checks
    the problem size against the configured thresholds to decide whether
    to use GPU or CPU.

    Args:
        config: Performance configuration with GPU settings.
        problem_size: Size of the problem (units depend on problem_type).
        problem_type: Type of problem being solved:
            - "grid": Number of grid cells for routing
            - "placement": Number of components for placement
            - "evolutionary": Population size for evolutionary optimizer
            - "signal_integrity": Number of trace pairs for SI analysis

    Returns:
        True if GPU should be used, False if CPU should be used.

    Examples:
        >>> from kicad_tools.performance import PerformanceConfig
        >>> config = PerformanceConfig.detect()
        >>> should_use_gpu(config, 50_000, "grid")  # Below threshold
        False
        >>> should_use_gpu(config, 200_000, "grid")  # Above threshold
        True
    """
    # CPU mode always uses CPU
    if config.gpu.backend == "cpu":
        return False

    # Map problem types to their threshold values
    thresholds = {
        "grid": config.gpu.thresholds.min_grid_cells,
        "placement": config.gpu.thresholds.min_components,
        "evolutionary": config.gpu.thresholds.min_population,
        "signal_integrity": config.gpu.thresholds.min_trace_pairs,
    }

    threshold = thresholds.get(problem_type, 0)
    return problem_size >= threshold


def get_effective_backend(config: PerformanceConfig) -> str:
    """Get the effective GPU backend based on configuration and system.

    When backend is "auto", this function detects the available GPU
    hardware and returns the appropriate backend. Otherwise, it returns
    the configured backend.

    Args:
        config: Performance configuration with GPU settings.

    Returns:
        The effective backend to use: "cuda", "metal", or "cpu".
    """
    if config.gpu.backend != "auto":
        return config.gpu.backend

    # Auto-detect based on platform
    import sys

    if sys.platform == "darwin":
        # macOS: try Metal
        try:
            # Check for Metal availability (placeholder for actual detection)
            return "metal"
        except Exception:
            return "cpu"
    else:
        # Linux/Windows: try CUDA
        try:
            # Check for CUDA availability (placeholder for actual detection)
            import subprocess

            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return "cuda"
        except Exception:
            pass

        return "cpu"


def validate_backend(backend: str) -> bool:
    """Validate that a backend string is valid.

    Args:
        backend: Backend string to validate.

    Returns:
        True if valid, False otherwise.
    """
    return backend in ("auto", "cuda", "metal", "cpu")
