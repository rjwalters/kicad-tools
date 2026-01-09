"""Benchmark runner for routing performance testing."""

from __future__ import annotations

import json
import time
import tracemalloc
from datetime import datetime
from pathlib import Path

from .cases import BENCHMARK_CASES, BenchmarkCase, Difficulty
from .result import BenchmarkResult


class BenchmarkRunner:
    """Runner for executing routing benchmarks and collecting results."""

    # Available routing strategies
    STRATEGIES = ["basic", "negotiated", "monte_carlo"]

    def __init__(
        self,
        base_dir: Path | None = None,
        verbose: bool = False,
    ):
        """Initialize the benchmark runner.

        Args:
            base_dir: Base directory for resolving PCB paths
            verbose: Enable verbose output
        """
        self.base_dir = base_dir or Path.cwd()
        self.verbose = verbose
        self.results: list[BenchmarkResult] = []

    def run_single(
        self,
        case: BenchmarkCase,
        strategy: str = "negotiated",
    ) -> BenchmarkResult:
        """Run a single benchmark case with specified strategy.

        Args:
            case: Benchmark case to run
            strategy: Routing strategy ('basic', 'negotiated', 'monte_carlo')

        Returns:
            BenchmarkResult with timing and quality metrics
        """
        from kicad_tools.router import DesignRules, load_pcb_for_routing

        if self.verbose:
            print(f"  Running {case.name} with {strategy}...")

        # Create design rules
        rules = DesignRules(
            grid_resolution=case.grid_resolution,
            trace_width=case.trace_width,
            trace_clearance=case.trace_clearance,
        )

        # Load or generate the router
        if case.is_synthetic():
            if case.generator is None:
                raise ValueError(f"Synthetic case {case.name} has no generator")
            router = case.generator()
        else:
            pcb_path = case.get_pcb_path(self.base_dir)
            if pcb_path is None or not pcb_path.exists():
                raise FileNotFoundError(f"PCB file not found: {pcb_path}")
            router, _ = load_pcb_for_routing(
                str(pcb_path),
                skip_nets=case.skip_nets,
                rules=rules,
            )

        total_nets = len([n for n in router.nets if n > 0])

        # Start memory tracking
        tracemalloc.start()
        start_time = time.perf_counter()
        iterations = 0

        # Run routing based on strategy
        if strategy == "basic":
            router.route_all()
        elif strategy == "negotiated":
            router.route_all_negotiated(max_iterations=5)
            iterations = 5
        elif strategy == "monte_carlo":
            router.route_all_monte_carlo(num_trials=10, verbose=False)
            iterations = 10
        else:
            raise ValueError(f"Unknown strategy: {strategy}")

        # Collect timing and memory stats
        routing_time = time.perf_counter() - start_time
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        memory_peak_mb = peak / (1024 * 1024)

        # Get router statistics
        stats = router.get_statistics()

        # Build result
        result = BenchmarkResult(
            case_name=case.name,
            strategy=strategy,
            nets_total=total_nets,
            nets_routed=stats["nets_routed"],
            completion_rate=stats["nets_routed"] / total_nets if total_nets > 0 else 0.0,
            total_segments=stats["segments"],
            total_vias=stats["vias"],
            total_length_mm=stats["total_length_mm"],
            max_congestion=stats.get("max_congestion", 0.0),
            avg_congestion=stats.get("avg_congestion", 0.0),
            congested_regions=stats.get("congested_regions", 0),
            routing_time_sec=routing_time,
            memory_peak_mb=memory_peak_mb,
            iterations=iterations,
            grid_resolution=case.grid_resolution,
            trace_width=case.trace_width,
            trace_clearance=case.trace_clearance,
        )

        # Check against expectations
        passed, failures = result.meets_expectations(
            case.expected_completion,
            case.expected_max_vias,
        )

        if self.verbose:
            status = "PASS" if passed else "FAIL"
            print(
                f"    [{status}] {result.nets_routed}/{result.nets_total} nets, {result.total_vias} vias, {routing_time:.2f}s"
            )
            for failure in failures:
                print(f"      WARNING: {failure}")

        return result

    def run_case(
        self,
        case: BenchmarkCase,
        strategies: list[str] | None = None,
    ) -> list[BenchmarkResult]:
        """Run all strategies for a single benchmark case.

        Args:
            case: Benchmark case to run
            strategies: List of strategies (default: all)

        Returns:
            List of results for each strategy
        """
        if strategies is None:
            strategies = self.STRATEGIES

        results = []
        for strategy in strategies:
            try:
                result = self.run_single(case, strategy)
                results.append(result)
                self.results.append(result)
            except Exception as e:
                if self.verbose:
                    print(f"    ERROR: {e}")

        return results

    def run_all(
        self,
        cases: list[str] | None = None,
        strategies: list[str] | None = None,
        difficulty: Difficulty | None = None,
    ) -> list[BenchmarkResult]:
        """Run benchmark suite.

        Args:
            cases: List of case names to run (default: all)
            strategies: List of strategies (default: all)
            difficulty: Filter by difficulty level

        Returns:
            List of all benchmark results
        """
        if strategies is None:
            strategies = self.STRATEGIES

        self.results = []

        for case in BENCHMARK_CASES:
            # Filter by name
            if cases is not None and case.name not in cases:
                continue

            # Filter by difficulty
            if difficulty is not None and case.difficulty != difficulty:
                continue

            if self.verbose:
                print(f"\nBenchmarking: {case.name} ({case.difficulty.value})")

            self.run_case(case, strategies)

        return self.results

    def save_results(
        self,
        path: Path | str | None = None,
    ) -> Path:
        """Save benchmark results to JSON file.

        Args:
            path: Output file path (default: benchmarks/{timestamp}.json)

        Returns:
            Path to saved file
        """
        if path is None:
            benchmarks_dir = self.base_dir / "benchmarks"
            benchmarks_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = benchmarks_dir / f"benchmark_{timestamp}.json"
        else:
            path = Path(path)

        data = {
            "timestamp": datetime.now().isoformat(),
            "results": [r.to_dict() for r in self.results],
        }

        with open(path, "w") as f:
            json.dump(data, f, indent=2)

        return path

    def print_summary(self) -> None:
        """Print summary table of results."""
        if not self.results:
            print("No benchmark results to display.")
            return

        # Header
        print()
        print(
            f"{'Case':<20} {'Strategy':<15} {'Routed':<12} {'Vias':<8} {'Length':<12} {'Time':<10}"
        )
        print("-" * 77)

        # Group by case for better readability
        current_case = None
        for r in self.results:
            if r.case_name != current_case:
                if current_case is not None:
                    print()  # Blank line between cases
                current_case = r.case_name

            routed_str = f"{r.nets_routed}/{r.nets_total}"
            print(
                f"{r.case_name:<20} {r.strategy:<15} {routed_str:<12} "
                f"{r.total_vias:<8} {r.total_length_mm:.1f}mm{'':<6} {r.routing_time_sec:.2f}s"
            )

        # Summary
        print()
        print("-" * 77)
        total_cases = len({r.case_name for r in self.results})
        total_runs = len(self.results)
        avg_completion = sum(r.completion_rate for r in self.results) / len(self.results)
        print(f"Total: {total_cases} cases, {total_runs} runs, {avg_completion:.1%} avg completion")

    def get_best_strategy(self, case_name: str) -> BenchmarkResult | None:
        """Find the best result for a given case.

        Best is defined as: highest completion rate, then lowest vias.
        """
        case_results = [r for r in self.results if r.case_name == case_name]
        if not case_results:
            return None

        return max(case_results, key=lambda r: (r.completion_rate, -r.total_vias))
