"""
Performance calibration for kicad-tools routing operations.

Provides benchmarking and calibration utilities to determine optimal
routing parameters for the local machine.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from kicad_tools.performance import (
    PERFORMANCE_CONFIG_FILE,
    PerformanceConfig,
    detect_available_memory_gb,
    detect_cpu_count,
)


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run.

    Attributes:
        worker_count: Number of workers used.
        trial_count: Number of trials used.
        duration_ms: Total duration in milliseconds.
        throughput: Operations per second.
    """

    worker_count: int
    trial_count: int
    duration_ms: float
    throughput: float


@dataclass
class CalibrationResult:
    """Result of the full calibration process.

    Attributes:
        cpu_cores: Detected CPU cores.
        memory_gb: Available memory in GB.
        optimal_workers: Optimal worker count from benchmark.
        optimal_trials: Optimal trial count from benchmark.
        benchmarks: List of individual benchmark results.
        duration_seconds: Total calibration time.
    """

    cpu_cores: int
    memory_gb: float
    optimal_workers: int
    optimal_trials: int
    benchmarks: list[BenchmarkResult] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_performance_config(self) -> PerformanceConfig:
        """Convert calibration result to PerformanceConfig.

        Returns:
            PerformanceConfig with calibrated settings.
        """
        return PerformanceConfig(
            cpu_cores=self.cpu_cores,
            available_memory_gb=self.memory_gb,
            monte_carlo_trials=self.optimal_trials,
            parallel_workers=self.optimal_workers,
            grid_memory_limit_mb=min(2000, int(self.memory_gb * 150)),
            negotiated_iterations=15 + min(10, self.cpu_cores // 2),
            partition_rows=max(2, int(self.cpu_cores**0.5)),
            partition_cols=max(2, int(self.cpu_cores**0.5)),
            calibrated=True,
        )


def _run_parallel_benchmark(worker_count: int, iterations: int = 100) -> float:
    """Run a parallel computation benchmark.

    Args:
        worker_count: Number of workers to use.
        iterations: Number of iterations to run.

    Returns:
        Duration in milliseconds.
    """
    from concurrent.futures import ThreadPoolExecutor

    def work_unit(n: int) -> float:
        """Simple CPU-bound work unit."""
        total = 0.0
        for i in range(1000):
            total += (i * n) ** 0.5
        return total

    start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        list(executor.map(work_unit, range(iterations)))

    duration_ms = (time.perf_counter() - start) * 1000
    return duration_ms


def _run_memory_benchmark(size_mb: int) -> tuple[float, bool]:
    """Run a memory allocation benchmark.

    Args:
        size_mb: Size to allocate in MB.

    Returns:
        Tuple of (duration_ms, success).
    """
    try:
        start = time.perf_counter()

        # Allocate a numpy array if available, otherwise use list
        try:
            import numpy as np

            arr = np.zeros((size_mb * 1024 * 1024 // 8,), dtype=np.float64)
            _ = arr.sum()  # Touch the memory
            del arr
        except ImportError:
            # Fallback: allocate a large list
            arr = [0.0] * (size_mb * 1024 * 128)  # Approximate MB
            _ = sum(arr[:1000])
            del arr

        duration_ms = (time.perf_counter() - start) * 1000
        return duration_ms, True
    except MemoryError:
        return 0.0, False


def run_calibration(
    verbose: bool = False,
    quick: bool = False,
) -> CalibrationResult:
    """Run performance calibration benchmarks.

    Benchmarks parallel execution and memory allocation to determine
    optimal settings for the current machine.

    Args:
        verbose: If True, print progress information.
        quick: If True, run abbreviated benchmarks.

    Returns:
        CalibrationResult with optimal settings.
    """
    start_time = time.perf_counter()

    cpu_cores = detect_cpu_count()
    memory_gb = detect_available_memory_gb()

    if verbose:
        print(f"Detected {cpu_cores} CPU cores")
        print(f"Available memory: {memory_gb:.1f} GB")
        print()

    benchmarks: list[BenchmarkResult] = []
    best_throughput = 0.0
    optimal_workers = max(1, cpu_cores - 1)

    # Benchmark different worker counts
    worker_counts = [1, 2, 4, cpu_cores // 2, cpu_cores - 1, cpu_cores]
    worker_counts = sorted({w for w in worker_counts if w >= 1 and w <= cpu_cores * 2})

    if quick:
        # Quick mode: fewer worker counts, fewer iterations
        worker_counts = [cpu_cores // 2, cpu_cores - 1, cpu_cores]
        worker_counts = sorted({w for w in worker_counts if w >= 1})
        iterations = 50
    else:
        iterations = 100

    if verbose:
        print("Benchmarking parallel execution...")

    for worker_count in worker_counts:
        if verbose:
            print(f"  Testing {worker_count} workers...", end=" ", flush=True)

        duration_ms = _run_parallel_benchmark(worker_count, iterations)
        throughput = iterations / (duration_ms / 1000)

        benchmark = BenchmarkResult(
            worker_count=worker_count,
            trial_count=iterations,
            duration_ms=duration_ms,
            throughput=throughput,
        )
        benchmarks.append(benchmark)

        if verbose:
            print(f"{throughput:.1f} ops/sec")

        if throughput > best_throughput:
            best_throughput = throughput
            optimal_workers = worker_count

    # Determine optimal trial count based on throughput
    # More cores = more trials make sense
    optimal_trials = max(4, optimal_workers * 2)

    # Benchmark memory allocation if not quick mode
    if not quick and verbose:
        print()
        print("Benchmarking memory allocation...")

        for size_mb in [100, 250, 500, 1000]:
            duration_ms, success = _run_memory_benchmark(size_mb)
            if success:
                if verbose:
                    print(f"  {size_mb} MB: {duration_ms:.1f} ms")
            else:
                if verbose:
                    print(f"  {size_mb} MB: allocation failed")
                break

    duration_seconds = time.perf_counter() - start_time

    if verbose:
        print()
        print(f"Calibration complete in {duration_seconds:.1f}s")
        print(f"  Optimal workers: {optimal_workers}")
        print(f"  Optimal trials: {optimal_trials}")

    return CalibrationResult(
        cpu_cores=cpu_cores,
        memory_gb=memory_gb,
        optimal_workers=optimal_workers,
        optimal_trials=optimal_trials,
        benchmarks=benchmarks,
        duration_seconds=duration_seconds,
    )


def calibrate_and_save(
    output_path: Path | None = None,
    verbose: bool = False,
    quick: bool = False,
) -> PerformanceConfig:
    """Run calibration and save results to config file.

    Args:
        output_path: Path to save config, or None for default.
        verbose: If True, print progress.
        quick: If True, run abbreviated benchmarks.

    Returns:
        PerformanceConfig with calibrated settings.
    """
    result = run_calibration(verbose=verbose, quick=quick)
    config = result.to_performance_config()

    save_path = output_path or PERFORMANCE_CONFIG_FILE
    config.save(save_path)

    if verbose:
        print()
        print(f"Configuration saved to: {save_path}")

    return config


def show_current_config(verbose: bool = False) -> PerformanceConfig:
    """Display the current performance configuration.

    Args:
        verbose: If True, show detailed information.

    Returns:
        Current PerformanceConfig.
    """
    config = PerformanceConfig.load_calibrated()

    print("Performance Configuration")
    print("=" * 40)

    if config.calibrated:
        print(f"Status: Calibrated ({config.calibration_date})")
    else:
        print("Status: Using auto-detected defaults")

    print()
    print("System Resources:")
    print(f"  CPU cores:         {config.cpu_cores}")
    print(f"  Available memory:  {config.available_memory_gb:.1f} GB")

    print()
    print("Routing Settings:")
    print(f"  Monte Carlo trials:    {config.monte_carlo_trials}")
    print(f"  Parallel workers:      {config.parallel_workers}")
    print(f"  Negotiated iterations: {config.negotiated_iterations}")
    print(f"  Partition grid:        {config.partition_rows}x{config.partition_cols}")

    print()
    print("Grid Settings:")
    print(f"  Memory limit: {config.grid_memory_limit_mb} MB")

    print()
    print("GPU Settings:")
    print(f"  Backend:      {config.gpu.backend}")
    print(f"  Device ID:    {config.gpu.device_id}")
    print(f"  Memory limit: {config.gpu.memory_limit_mb} MB (0 = no limit)")

    print()
    print("GPU Thresholds (min problem size for GPU usage):")
    print(f"  Grid cells:     {config.gpu.thresholds.min_grid_cells:,}")
    print(f"  Components:     {config.gpu.thresholds.min_components}")
    print(f"  Population:     {config.gpu.thresholds.min_population}")
    print(f"  Trace pairs:    {config.gpu.thresholds.min_trace_pairs}")

    if verbose:
        print()
        print(f"Config file: {PERFORMANCE_CONFIG_FILE}")
        print(f"  Exists: {PERFORMANCE_CONFIG_FILE.exists()}")

    return config
