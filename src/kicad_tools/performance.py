"""
Performance configuration for kicad-tools routing operations.

Provides resource-aware defaults and calibrated settings for optimal
routing performance based on the local machine's capabilities.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None  # type: ignore[assignment]

# User config directory for performance calibration
PERFORMANCE_CONFIG_DIR = Path.home() / ".config" / "kicad-tools"
PERFORMANCE_CONFIG_FILE = PERFORMANCE_CONFIG_DIR / "performance.toml"


def detect_cpu_count() -> int:
    """Detect the number of CPU cores available.

    Returns:
        Number of CPU cores, or 4 as a fallback.
    """
    try:
        # Try to get usable CPU count (respects cgroups, affinity)
        if hasattr(os, "sched_getaffinity"):
            return len(os.sched_getaffinity(0))
        return os.cpu_count() or 4
    except Exception:
        return 4


# Type alias for GPU backend selection
GpuBackend = Literal["auto", "cuda", "metal", "cpu"]


@dataclass
class GpuThresholds:
    """Thresholds for GPU usage decisions.

    GPU acceleration has overhead, so it's only worthwhile for problems
    above certain sizes. These thresholds define the minimum problem sizes
    where GPU acceleration is beneficial.

    Attributes:
        min_grid_cells: Minimum grid cells before GPU is worthwhile (~316x316 grid).
        min_components: Minimum components for placement algorithms.
        min_population: Minimum population for evolutionary optimizer.
        min_trace_pairs: Minimum trace pairs for signal integrity analysis.
    """

    min_grid_cells: int = 100_000
    min_components: int = 50
    min_population: int = 20
    min_trace_pairs: int = 100


@dataclass
class GpuConfig:
    """Configuration for GPU acceleration.

    Controls which GPU backend to use and when to use GPU vs CPU
    based on problem size thresholds.

    Attributes:
        backend: GPU backend selection - "auto", "cuda", "metal", or "cpu".
        device_id: GPU device ID for multi-GPU systems.
        memory_limit_mb: Maximum GPU memory to use (0 = no limit).
        thresholds: Problem size thresholds for GPU usage decisions.
    """

    backend: GpuBackend = "auto"
    device_id: int = 0
    memory_limit_mb: int = 0
    thresholds: GpuThresholds = field(default_factory=GpuThresholds)


def detect_available_memory_gb() -> float:
    """Detect available system memory in gigabytes.

    Returns:
        Available memory in GB, or 8.0 as a fallback.
    """
    try:
        # Try psutil first (most accurate)
        import psutil

        mem = psutil.virtual_memory()
        return mem.available / (1024**3)
    except ImportError:
        pass

    # Fallback: try reading from /proc/meminfo on Linux
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb / (1024**2)
    except Exception:
        pass

    # macOS fallback
    try:
        import subprocess

        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            bytes_total = int(result.stdout.strip())
            # Estimate available as 60% of total
            return (bytes_total * 0.6) / (1024**3)
    except Exception:
        pass

    # Default fallback
    return 8.0


@dataclass
class PerformanceConfig:
    """Configuration for routing performance optimization.

    This class provides auto-detected or calibrated settings for optimal
    routing performance. Settings can be loaded from a calibration file
    or use sensible defaults based on detected system resources.

    Attributes:
        cpu_cores: Number of CPU cores available for parallel work.
        available_memory_gb: Available system memory in GB.
        monte_carlo_trials: Number of Monte Carlo trials (0 = auto).
        parallel_workers: Number of parallel routing workers (0 = auto).
        grid_memory_limit_mb: Maximum memory for routing grid.
        negotiated_iterations: Max iterations for negotiated routing.
        partition_rows: Number of partition rows for region-based routing.
        partition_cols: Number of partition columns for region-based routing.
        calibrated: Whether these settings came from calibration.
        calibration_date: ISO date string of last calibration.
    """

    cpu_cores: int = field(default_factory=detect_cpu_count)
    available_memory_gb: float = field(default_factory=detect_available_memory_gb)
    monte_carlo_trials: int = 0  # 0 = auto (2x CPU cores)
    parallel_workers: int = 0  # 0 = auto (CPU cores - 1)
    grid_memory_limit_mb: int = 500
    negotiated_iterations: int = 15
    partition_rows: int = 2
    partition_cols: int = 2
    calibrated: bool = False
    calibration_date: str = ""
    gpu: GpuConfig = field(default_factory=GpuConfig)

    def __post_init__(self):
        """Apply auto-detection for zero values."""
        if self.monte_carlo_trials == 0:
            self.monte_carlo_trials = max(4, self.cpu_cores * 2)
        if self.parallel_workers == 0:
            self.parallel_workers = max(1, self.cpu_cores - 1)

    @property
    def effective_monte_carlo_trials(self) -> int:
        """Get the effective number of Monte Carlo trials."""
        if self.monte_carlo_trials == 0:
            return max(4, self.cpu_cores * 2)
        return self.monte_carlo_trials

    @property
    def effective_parallel_workers(self) -> int:
        """Get the effective number of parallel workers."""
        if self.parallel_workers == 0:
            return max(1, self.cpu_cores - 1)
        return self.parallel_workers

    @classmethod
    def detect(cls) -> PerformanceConfig:
        """Create config with auto-detected system resources.

        Returns:
            PerformanceConfig with detected CPU cores and memory.
        """
        return cls(
            cpu_cores=detect_cpu_count(),
            available_memory_gb=detect_available_memory_gb(),
        )

    @classmethod
    def load_calibrated(cls) -> PerformanceConfig:
        """Load calibrated settings or fall back to auto-detection.

        Returns:
            PerformanceConfig from calibration file, or auto-detected if not available.
        """
        if not PERFORMANCE_CONFIG_FILE.exists():
            return cls.detect()

        if tomllib is None:
            return cls.detect()

        try:
            with open(PERFORMANCE_CONFIG_FILE, "rb") as f:
                data = tomllib.load(f)

            cal = data.get("calibration", {})
            routing = data.get("routing", {})
            grid = data.get("grid", {})
            gpu_data = data.get("gpu", {})
            gpu_thresholds_data = gpu_data.get("thresholds", {})

            # Build GPU thresholds
            gpu_thresholds = GpuThresholds(
                min_grid_cells=gpu_thresholds_data.get("min_grid_cells", 100_000),
                min_components=gpu_thresholds_data.get("min_components", 50),
                min_population=gpu_thresholds_data.get("min_population", 20),
                min_trace_pairs=gpu_thresholds_data.get("min_trace_pairs", 100),
            )

            # Build GPU config
            gpu_config = GpuConfig(
                backend=gpu_data.get("backend", "auto"),
                device_id=gpu_data.get("device_id", 0),
                memory_limit_mb=gpu_data.get("memory_limit_mb", 0),
                thresholds=gpu_thresholds,
            )

            return cls(
                cpu_cores=cal.get("cpu_cores", detect_cpu_count()),
                available_memory_gb=cal.get("memory_gb", detect_available_memory_gb()),
                monte_carlo_trials=routing.get("monte_carlo_trials", 0),
                parallel_workers=routing.get("parallel_workers", 0),
                negotiated_iterations=routing.get("negotiated_iterations", 15),
                partition_rows=routing.get("partition_rows", 2),
                partition_cols=routing.get("partition_cols", 2),
                grid_memory_limit_mb=grid.get("max_memory_mb", 500),
                calibrated=True,
                calibration_date=cal.get("date", ""),
                gpu=gpu_config,
            )
        except Exception:
            return cls.detect()

    @classmethod
    def high_performance(cls) -> PerformanceConfig:
        """Create aggressive settings for maximum throughput.

        Uses all available CPU cores and higher trial counts for
        better routing results at the cost of longer runtime.

        Returns:
            PerformanceConfig optimized for maximum performance.
        """
        cpu_cores = detect_cpu_count()
        memory_gb = detect_available_memory_gb()

        return cls(
            cpu_cores=cpu_cores,
            available_memory_gb=memory_gb,
            # Use all cores for Monte Carlo
            monte_carlo_trials=max(8, cpu_cores * 3),
            # Use all cores minus 1 for parallel routing
            parallel_workers=max(2, cpu_cores),
            # More memory for grid if available
            grid_memory_limit_mb=min(2000, int(memory_gb * 200)),
            # More iterations for better results
            negotiated_iterations=25,
            # More partitions for parallelism
            partition_rows=max(2, int(cpu_cores**0.5)),
            partition_cols=max(2, int(cpu_cores**0.5)),
            calibrated=False,
            calibration_date="",
        )

    def save(self, path: Path | None = None) -> None:
        """Save configuration to TOML file.

        Args:
            path: Path to save to, or None to use default location.
        """
        if path is None:
            path = PERFORMANCE_CONFIG_FILE

        path.parent.mkdir(parents=True, exist_ok=True)

        from datetime import datetime

        date_str = datetime.now().isoformat()

        content = f'''# kicad-tools performance configuration
# Generated by: kicad-tools calibrate
# Machine-specific settings for optimal routing performance

[calibration]
date = "{date_str}"
cpu_cores = {self.cpu_cores}
memory_gb = {self.available_memory_gb:.1f}

[routing]
# Monte Carlo trials for multi-start routing
monte_carlo_trials = {self.monte_carlo_trials}

# Parallel workers for concurrent routing
parallel_workers = {self.parallel_workers}

# Negotiated routing iterations
negotiated_iterations = {self.negotiated_iterations}

# Region partitioning for parallel negotiated routing
partition_rows = {self.partition_rows}
partition_cols = {self.partition_cols}

[grid]
# Maximum memory for routing grid (MB)
max_memory_mb = {self.grid_memory_limit_mb}

[gpu]
# Backend selection: auto | cuda | metal | cpu
backend = "{self.gpu.backend}"

# Device selection (for multi-GPU systems)
device_id = {self.gpu.device_id}

# Memory limit in MB (0 = no limit)
memory_limit_mb = {self.gpu.memory_limit_mb}

[gpu.thresholds]
# Minimum problem sizes before GPU is worthwhile
# Below these, CPU is used regardless of backend setting
min_grid_cells = {self.gpu.thresholds.min_grid_cells}
min_components = {self.gpu.thresholds.min_components}
min_population = {self.gpu.thresholds.min_population}
min_trace_pairs = {self.gpu.thresholds.min_trace_pairs}
'''
        path.write_text(content)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON/TOML output.

        Returns:
            Dictionary representation of the config.
        """
        return {
            "calibration": {
                "cpu_cores": self.cpu_cores,
                "memory_gb": self.available_memory_gb,
                "calibrated": self.calibrated,
                "date": self.calibration_date,
            },
            "routing": {
                "monte_carlo_trials": self.monte_carlo_trials,
                "parallel_workers": self.parallel_workers,
                "negotiated_iterations": self.negotiated_iterations,
                "partition_rows": self.partition_rows,
                "partition_cols": self.partition_cols,
            },
            "grid": {
                "max_memory_mb": self.grid_memory_limit_mb,
            },
            "gpu": {
                "backend": self.gpu.backend,
                "device_id": self.gpu.device_id,
                "memory_limit_mb": self.gpu.memory_limit_mb,
                "thresholds": {
                    "min_grid_cells": self.gpu.thresholds.min_grid_cells,
                    "min_components": self.gpu.thresholds.min_components,
                    "min_population": self.gpu.thresholds.min_population,
                    "min_trace_pairs": self.gpu.thresholds.min_trace_pairs,
                },
            },
        }


def get_performance_config(high_performance: bool = False) -> PerformanceConfig:
    """Get the appropriate performance configuration.

    Args:
        high_performance: If True, use aggressive high-performance settings.

    Returns:
        PerformanceConfig instance with appropriate settings.
    """
    if high_performance:
        return PerformanceConfig.high_performance()
    return PerformanceConfig.load_calibrated()
