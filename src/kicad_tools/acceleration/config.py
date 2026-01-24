"""GPU configuration helpers for kicad-tools.

Provides utilities for determining when GPU acceleration should be used
based on configuration and problem characteristics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from kicad_tools.performance import PerformanceConfig

# Problem types that can be GPU accelerated
ProblemType = Literal["grid", "placement", "evolutionary", "signal_integrity"]


def should_use_gpu(
    config: PerformanceConfig,
    problem_size: int,
    problem_type: ProblemType,
) -> bool:
    """Determine if GPU should be used based on config and problem size.

    GPU acceleration has overhead from kernel launches and memory transfers,
    so it's only beneficial for problems above certain size thresholds.

    Args:
        config: Performance configuration with GPU settings.
        problem_size: Size of the problem (interpretation depends on problem_type).
        problem_type: Type of problem being solved:
            - "grid": problem_size is number of routing grid cells
            - "placement": problem_size is number of components
            - "evolutionary": problem_size is population size
            - "signal_integrity": problem_size is number of trace pairs

    Returns:
        True if GPU should be used, False if CPU is preferred.

    Examples:
        >>> config = PerformanceConfig()
        >>> should_use_gpu(config, 500_000, "grid")  # Large grid
        True
        >>> should_use_gpu(config, 1000, "grid")  # Small grid
        False
        >>> config.gpu.backend = "cpu"
        >>> should_use_gpu(config, 500_000, "grid")  # Forced CPU
        False
    """
    # If backend is explicitly set to CPU, never use GPU
    if config.gpu.backend == "cpu":
        return False

    # Map problem types to their threshold fields
    thresholds = {
        "grid": config.gpu.thresholds.min_grid_cells,
        "placement": config.gpu.thresholds.min_components,
        "evolutionary": config.gpu.thresholds.min_population,
        "signal_integrity": config.gpu.thresholds.min_trace_pairs,
    }

    # Get threshold for this problem type (default to 0 = always use GPU)
    threshold = thresholds.get(problem_type, 0)

    return problem_size >= threshold


def get_effective_backend(config: PerformanceConfig) -> str:
    """Get the effective GPU backend based on config and system detection.

    When backend is "auto", this function detects available GPU backends
    and returns the best available option.

    Args:
        config: Performance configuration with GPU settings.

    Returns:
        Effective backend string: "cuda", "metal", or "cpu".
    """
    if config.gpu.backend != "auto":
        return config.gpu.backend

    # Auto-detect best backend
    # Try CUDA first (NVIDIA)
    try:
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

    # Try Metal (Apple Silicon)
    try:
        import platform

        if platform.system() == "Darwin":
            # Check for Apple Silicon or AMD GPU
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                output = result.stdout.lower()
                if "apple m" in output or "amd" in output:
                    return "metal"
    except Exception:
        pass

    # Fallback to CPU
    return "cpu"
