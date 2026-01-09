"""Benchmark suite for routing performance testing and regression detection.

This module provides:
- Benchmark case definitions for routing strategies
- Performance metrics collection
- Regression detection against baselines
- Synthetic board generators for stress testing

Example::

    from kicad_tools.benchmark import BenchmarkRunner, BenchmarkCase

    runner = BenchmarkRunner()
    results = runner.run_all()
    regressions = runner.check_regression("benchmarks/baseline.json")
"""

from .cases import BENCHMARK_CASES, BenchmarkCase, Difficulty
from .generators import generate_bga_breakout, generate_random_board
from .regression import Regression, check_regression, load_baseline, save_baseline
from .result import BenchmarkResult
from .runner import BenchmarkRunner

__all__ = [
    # Data classes
    "BenchmarkCase",
    "BenchmarkResult",
    "Difficulty",
    "Regression",
    # Runner
    "BenchmarkRunner",
    # Regression detection
    "check_regression",
    "load_baseline",
    "save_baseline",
    # Generators
    "generate_bga_breakout",
    "generate_random_board",
    # Case registry
    "BENCHMARK_CASES",
]
