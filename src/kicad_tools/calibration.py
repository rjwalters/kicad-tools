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
    GpuConfig,
    GpuThresholds,
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
    include_gpu: bool = False,
) -> PerformanceConfig:
    """Run calibration and save results to config file.

    Args:
        output_path: Path to save config, or None for default.
        verbose: If True, print progress.
        quick: If True, run abbreviated benchmarks.
        include_gpu: If True, also run GPU benchmarks.

    Returns:
        PerformanceConfig with calibrated settings.
    """
    result = run_calibration(verbose=verbose, quick=quick)
    config = result.to_performance_config()

    # Run GPU calibration if requested
    if include_gpu:
        if verbose:
            print()
            print("-" * 40)
            print()
        gpu_result = benchmark_gpu_backends(verbose=verbose)
        if gpu_result.has_gpu:
            thresholds = determine_gpu_thresholds(gpu_result, verbose=verbose)
            config.gpu = GpuConfig(
                backend="auto",
                device_id=0,
                memory_limit_mb=0,
                thresholds=thresholds,
            )

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

    if verbose:
        print()
        print(f"Config file: {PERFORMANCE_CONFIG_FILE}")
        print(f"  Exists: {PERFORMANCE_CONFIG_FILE.exists()}")

    return config


# =============================================================================
# GPU Benchmarking Functions
# =============================================================================


@dataclass
class GpuBackendInfo:
    """Information about a GPU backend.

    Attributes:
        backend_type: Backend name (cuda, metal, cpu).
        available: Whether the backend is available.
        device_name: Name of the GPU device.
        memory_mb: GPU memory in MB (if available).
        benchmarks: Dictionary of benchmark results.
    """

    backend_type: str
    available: bool
    device_name: str = ""
    memory_mb: int = 0
    benchmarks: dict[str, float] = field(default_factory=dict)


@dataclass
class GpuBenchmarkResult:
    """Result of GPU benchmarking across all backends.

    Attributes:
        backends: List of backend info with benchmarks.
        has_gpu: Whether any GPU backend is available.
        best_backend: The fastest GPU backend found.
        duration_seconds: Total benchmark time.
    """

    backends: list[GpuBackendInfo] = field(default_factory=list)
    has_gpu: bool = False
    best_backend: str = "cpu"
    duration_seconds: float = 0.0


def _benchmark_array_operations(backend_module: any, size: int = 1_000_000) -> float:
    """Benchmark array creation and element-wise operations.

    Tests memory bandwidth and basic compute.

    Args:
        backend_module: The array backend (numpy, cupy, or MLX wrapper).
        size: Array size to use.

    Returns:
        Duration in milliseconds.
    """
    start = time.perf_counter()

    # Create array
    arr = backend_module.zeros((size,), dtype="float32")

    # Fill with values
    arr = backend_module.ones((size,), dtype="float32") * 2.5

    # Element-wise operations
    arr = arr * arr + arr

    # Sync if GPU (cupy has sync, MLX evaluates lazily)
    if hasattr(backend_module, "cuda"):
        backend_module.cuda.Stream.null.synchronize()
    elif hasattr(backend_module, "_mx"):
        # MLX - force evaluation
        import mlx.core as mx

        mx.eval(arr)

    duration_ms = (time.perf_counter() - start) * 1000
    return duration_ms


def _benchmark_pairwise_computation(backend_module: any, n: int = 1000) -> float:
    """Benchmark N x N pairwise distance computation.

    Tests parallel compute capability critical for force calculations.

    Args:
        backend_module: The array backend.
        n: Number of points (creates NxN computation).

    Returns:
        Duration in milliseconds.
    """
    import numpy as np

    start = time.perf_counter()

    # Create random point positions (2D)
    if hasattr(backend_module, "random"):
        x = backend_module.random.rand(n).astype("float32")
        y = backend_module.random.rand(n).astype("float32")
    else:
        # MLX wrapper or numpy
        rng = np.random.default_rng(42)
        x_np = rng.random(n).astype(np.float32)
        y_np = rng.random(n).astype(np.float32)
        if hasattr(backend_module, "_mx"):
            import mlx.core as mx

            x = mx.array(x_np)
            y = mx.array(y_np)
        else:
            x = backend_module.array(x_np)
            y = backend_module.array(y_np)

    # Compute pairwise squared distances: (xi - xj)^2 + (yi - yj)^2
    # Using broadcasting: reshape to (n, 1) and (1, n)
    if hasattr(backend_module, "_mx"):
        import mlx.core as mx

        x_diff = mx.reshape(x, (n, 1)) - mx.reshape(x, (1, n))
        y_diff = mx.reshape(y, (n, 1)) - mx.reshape(y, (1, n))
        dist_sq = x_diff * x_diff + y_diff * y_diff
        mx.eval(dist_sq)
    else:
        x_col = x.reshape((n, 1))
        y_col = y.reshape((n, 1))
        x_row = x.reshape((1, n))
        y_row = y.reshape((1, n))
        dist_sq = (x_col - x_row) ** 2 + (y_col - y_row) ** 2

        # Sync GPU
        if hasattr(backend_module, "cuda"):
            backend_module.cuda.Stream.null.synchronize()

    duration_ms = (time.perf_counter() - start) * 1000
    return duration_ms


def _benchmark_reduction(backend_module: any, size: int = 10_000_000) -> float:
    """Benchmark parallel reduction operations.

    Tests sum, max, argmax which are critical for routing decisions.

    Args:
        backend_module: The array backend.
        size: Array size.

    Returns:
        Duration in milliseconds.
    """
    import numpy as np

    start = time.perf_counter()

    # Create test array
    rng = np.random.default_rng(42)
    data_np = rng.random(size).astype(np.float32)

    if hasattr(backend_module, "_mx"):
        import mlx.core as mx

        arr = mx.array(data_np)
        # Reductions
        total = mx.sum(arr)
        maximum = mx.max(arr)
        mx.eval(total, maximum)
    else:
        if hasattr(backend_module, "array"):
            arr = backend_module.array(data_np)
        else:
            arr = data_np

        # Reductions
        _ = arr.sum()
        _ = arr.max()

        # Sync GPU
        if hasattr(backend_module, "cuda"):
            backend_module.cuda.Stream.null.synchronize()

    duration_ms = (time.perf_counter() - start) * 1000
    return duration_ms


def _get_backend_info(backend_type: str) -> GpuBackendInfo:
    """Get information about a specific backend.

    Args:
        backend_type: Backend name (cuda, metal, cpu).

    Returns:
        GpuBackendInfo with availability and device info.
    """
    if backend_type == "cuda":
        try:
            import cupy as cp

            device = cp.cuda.Device(0)
            props = cp.cuda.runtime.getDeviceProperties(device.id)
            device_name = (
                props["name"].decode()
                if isinstance(props["name"], bytes)
                else props["name"]
            )
            memory_mb = props["totalGlobalMem"] // (1024 * 1024)
            return GpuBackendInfo(
                backend_type="cuda",
                available=True,
                device_name=device_name,
                memory_mb=memory_mb,
            )
        except Exception:
            return GpuBackendInfo(
                backend_type="cuda",
                available=False,
            )

    elif backend_type == "metal":
        try:
            import platform

            import mlx.core as mx

            if platform.system() != "Darwin" or platform.machine() != "arm64":
                return GpuBackendInfo(backend_type="metal", available=False)

            # Test MLX is working
            _ = mx.array([1.0, 2.0])
            return GpuBackendInfo(
                backend_type="metal",
                available=True,
                device_name="Apple Silicon GPU",
                memory_mb=0,  # MLX doesn't expose memory info easily
            )
        except Exception:
            return GpuBackendInfo(backend_type="metal", available=False)

    else:  # cpu
        import numpy as np

        return GpuBackendInfo(
            backend_type="cpu",
            available=True,
            device_name=f"CPU (NumPy {np.__version__})",
            memory_mb=int(detect_available_memory_gb() * 1024),
        )


def _get_backend_module(backend_type: str) -> any:
    """Get the array module for a backend.

    Args:
        backend_type: Backend name (cuda, metal, cpu).

    Returns:
        Array module (cupy, MLXBackend wrapper, or numpy).
    """
    if backend_type == "cuda":
        import cupy as cp

        return cp
    elif backend_type == "metal":
        from kicad_tools.acceleration.backend import MLXBackend

        return MLXBackend()
    else:
        import numpy as np

        return np


def benchmark_gpu_backends(verbose: bool = False) -> GpuBenchmarkResult:
    """Benchmark all available GPU backends.

    Runs array operations, pairwise computation, and reduction benchmarks
    on each available backend to determine relative performance.

    Args:
        verbose: If True, print detailed progress.

    Returns:
        GpuBenchmarkResult with benchmarks for each backend.
    """
    start_time = time.perf_counter()

    if verbose:
        print("GPU Calibration Results")
        print("=" * 40)
        print()
        print("Detecting backends...")

    backends_to_check = ["cuda", "metal", "cpu"]
    backends: list[GpuBackendInfo] = []
    best_backend = "cpu"
    best_pairwise_time = float("inf")
    has_gpu = False

    for backend_type in backends_to_check:
        info = _get_backend_info(backend_type)
        if verbose:
            status = "available" if info.available else "not available"
            if info.available:
                mem_str = f", {info.memory_mb} MB" if info.memory_mb > 0 else ""
                print(f"  {backend_type.upper()}: {info.device_name}{mem_str}")
            else:
                print(f"  {backend_type.upper()}: {status}")

        if info.available:
            if backend_type in ("cuda", "metal"):
                has_gpu = True

        backends.append(info)

    if verbose:
        print()
        print("Running benchmarks...")

    # Run benchmarks on available backends
    for info in backends:
        if not info.available:
            continue

        if verbose:
            print(f"  Benchmarking {info.backend_type.upper()}...", end=" ", flush=True)

        try:
            backend_module = _get_backend_module(info.backend_type)

            # Array operations (1M elements)
            array_time = _benchmark_array_operations(backend_module, size=1_000_000)
            info.benchmarks["array_ops_1m"] = array_time

            # Pairwise computation (1000 points)
            pairwise_time = _benchmark_pairwise_computation(backend_module, n=1000)
            info.benchmarks["pairwise_1000"] = pairwise_time

            # Reduction (10M elements)
            reduction_time = _benchmark_reduction(backend_module, size=10_000_000)
            info.benchmarks["reduction_10m"] = reduction_time

            if verbose:
                print(
                    f"array={array_time:.1f}ms, "
                    f"pairwise={pairwise_time:.1f}ms, "
                    f"reduction={reduction_time:.1f}ms"
                )

            # Track best GPU backend (by pairwise performance)
            if info.backend_type != "cpu" and pairwise_time < best_pairwise_time:
                best_pairwise_time = pairwise_time
                best_backend = info.backend_type

        except Exception as e:
            if verbose:
                print(f"error: {e}")
            info.benchmarks["error"] = str(e)

    duration_seconds = time.perf_counter() - start_time

    if verbose:
        print()
        _print_benchmark_comparison(backends)

    return GpuBenchmarkResult(
        backends=backends,
        has_gpu=has_gpu,
        best_backend=best_backend if has_gpu else "cpu",
        duration_seconds=duration_seconds,
    )


def _print_benchmark_comparison(backends: list[GpuBackendInfo]) -> None:
    """Print benchmark comparison table.

    Args:
        backends: List of backend info with benchmarks.
    """
    # Find CPU times for comparison
    cpu_info = next((b for b in backends if b.backend_type == "cpu"), None)
    if not cpu_info or not cpu_info.benchmarks:
        return

    cpu_array = cpu_info.benchmarks.get("array_ops_1m", 0)
    cpu_pairwise = cpu_info.benchmarks.get("pairwise_1000", 0)
    cpu_reduction = cpu_info.benchmarks.get("reduction_10m", 0)

    print("Benchmark Results:")
    print("-" * 60)
    print(f"{'Operation':<20} {'CPU':>10} ", end="")

    gpu_backends = [b for b in backends if b.backend_type != "cpu" and b.available]
    for gpu in gpu_backends:
        print(f"{gpu.backend_type.upper():>10} {'Speedup':>8}", end="")
    print()

    # Array ops row
    print(f"{'Array ops (1M)':<20} {cpu_array:>9.1f}ms", end="")
    for gpu in gpu_backends:
        gpu_time = gpu.benchmarks.get("array_ops_1m", 0)
        if gpu_time > 0:
            speedup = cpu_array / gpu_time
            print(f" {gpu_time:>9.1f}ms {speedup:>7.1f}x", end="")
    print()

    # Pairwise row
    print(f"{'Pairwise (1000)':<20} {cpu_pairwise:>9.1f}ms", end="")
    for gpu in gpu_backends:
        gpu_time = gpu.benchmarks.get("pairwise_1000", 0)
        if gpu_time > 0:
            speedup = cpu_pairwise / gpu_time
            print(f" {gpu_time:>9.1f}ms {speedup:>7.1f}x", end="")
    print()

    # Reduction row
    print(f"{'Reduction (10M)':<20} {cpu_reduction:>9.1f}ms", end="")
    for gpu in gpu_backends:
        gpu_time = gpu.benchmarks.get("reduction_10m", 0)
        if gpu_time > 0:
            speedup = cpu_reduction / gpu_time
            print(f" {gpu_time:>9.1f}ms {speedup:>7.1f}x", end="")
    print()
    print("-" * 60)


def determine_gpu_thresholds(
    benchmark_result: GpuBenchmarkResult,
    verbose: bool = False,
) -> GpuThresholds:
    """Determine optimal GPU thresholds from benchmark results.

    Analyzes benchmark data to find the crossover points where GPU
    becomes faster than CPU for each operation type.

    Args:
        benchmark_result: Results from benchmark_gpu_backends().
        verbose: If True, print analysis.

    Returns:
        GpuThresholds with calibrated values.
    """
    # Get CPU and best GPU benchmarks
    cpu_info = next(
        (b for b in benchmark_result.backends if b.backend_type == "cpu"), None
    )
    gpu_info = next(
        (b for b in benchmark_result.backends if b.backend_type == benchmark_result.best_backend),
        None,
    )

    if not cpu_info or not gpu_info or not gpu_info.benchmarks:
        # No GPU or benchmarks failed - use defaults
        return GpuThresholds()

    # Calculate speedup ratios
    cpu_pairwise = cpu_info.benchmarks.get("pairwise_1000", 1)
    gpu_pairwise = gpu_info.benchmarks.get("pairwise_1000", 1)
    pairwise_speedup = cpu_pairwise / gpu_pairwise if gpu_pairwise > 0 else 1

    cpu_array = cpu_info.benchmarks.get("array_ops_1m", 1)
    gpu_array = gpu_info.benchmarks.get("array_ops_1m", 1)
    array_speedup = cpu_array / gpu_array if gpu_array > 0 else 1

    # Estimate crossover points based on speedup and GPU overhead
    # GPU has ~1-5ms launch overhead, so need enough work to amortize

    # For grid operations (array-like): GPU wins when array_speedup > 1
    # Base threshold is 100k cells, scale inversely with speedup
    if array_speedup > 1.5:
        min_grid_cells = max(10_000, int(100_000 / array_speedup))
    else:
        min_grid_cells = 500_000  # Only use GPU for very large grids

    # For placement (pairwise-like): GPU wins faster due to O(n^2)
    # 1000 components = 1M pairwise ops
    if pairwise_speedup > 5:
        min_components = max(20, int(100 / (pairwise_speedup / 10)))
    elif pairwise_speedup > 2:
        min_components = 50
    else:
        min_components = 200

    # For evolutionary (population-based): similar to pairwise
    if pairwise_speedup > 5:
        min_population = max(10, int(50 / (pairwise_speedup / 10)))
    else:
        min_population = 30

    # For signal integrity (trace pairs): depends on pair count
    min_trace_pairs = max(50, int(200 / max(1, pairwise_speedup / 5)))

    thresholds = GpuThresholds(
        min_grid_cells=min_grid_cells,
        min_components=min_components,
        min_population=min_population,
        min_trace_pairs=min_trace_pairs,
    )

    if verbose:
        print()
        print("Recommended Thresholds:")
        print(f"  min_grid_cells: {thresholds.min_grid_cells:,}  (GPU faster above this)")
        print(f"  min_components: {thresholds.min_components}  (GPU faster above this)")
        print(f"  min_population: {thresholds.min_population}  (GPU faster above this)")
        print(f"  min_trace_pairs: {thresholds.min_trace_pairs}  (GPU faster above this)")

    return thresholds


def run_gpu_calibration(
    output_path: Path | None = None,
    verbose: bool = False,
) -> PerformanceConfig:
    """Run GPU-specific calibration and update config.

    Args:
        output_path: Path to save config, or None for default.
        verbose: If True, print progress.

    Returns:
        PerformanceConfig with updated GPU thresholds.
    """
    # Load existing config or create new one
    config = PerformanceConfig.load_calibrated()

    # Run GPU benchmarks
    benchmark_result = benchmark_gpu_backends(verbose=verbose)

    if not benchmark_result.has_gpu:
        if verbose:
            print()
            print("No GPU acceleration available.")
            print("Using CPU-only configuration.")
        config.gpu = GpuConfig(backend="cpu")
    else:
        # Determine thresholds
        thresholds = determine_gpu_thresholds(benchmark_result, verbose=verbose)
        config.gpu = GpuConfig(
            backend="auto",
            device_id=0,
            memory_limit_mb=0,
            thresholds=thresholds,
        )

    # Save updated config
    save_path = output_path or PERFORMANCE_CONFIG_FILE
    config.save(save_path)

    if verbose:
        print()
        print(f"Configuration saved to: {save_path}")

    return config


def show_gpu_config(verbose: bool = False) -> None:
    """Display GPU capabilities and current configuration.

    Args:
        verbose: If True, show detailed information.
    """
    print("GPU Configuration")
    print("=" * 40)
    print()

    # Check available backends
    print("Available Backends:")
    for backend_type in ["cuda", "metal"]:
        info = _get_backend_info(backend_type)
        if info.available:
            mem_str = f" ({info.memory_mb} MB)" if info.memory_mb > 0 else ""
            print(f"  {backend_type.upper()}: {info.device_name}{mem_str}")
        else:
            print(f"  {backend_type.upper()}: not available")

    # Show current config
    config = PerformanceConfig.load_calibrated()
    print()
    print("Current Configuration:")
    print(f"  Backend: {config.gpu.backend}")
    print(f"  Device ID: {config.gpu.device_id}")
    if config.gpu.memory_limit_mb > 0:
        print(f"  Memory limit: {config.gpu.memory_limit_mb} MB")
    else:
        print("  Memory limit: none")

    print()
    print("Thresholds (GPU used above these sizes):")
    print(f"  Grid cells: {config.gpu.thresholds.min_grid_cells:,}")
    print(f"  Components: {config.gpu.thresholds.min_components}")
    print(f"  Population: {config.gpu.thresholds.min_population}")
    print(f"  Trace pairs: {config.gpu.thresholds.min_trace_pairs}")

    if verbose:
        print()
        print("Suggested installation:")
        from kicad_tools.acceleration.detection import suggest_install_command

        print(f"  {suggest_install_command()}")

    print()
    print("To run GPU benchmarks and calibrate thresholds:")
    print("  kicad-tools calibrate --gpu")
