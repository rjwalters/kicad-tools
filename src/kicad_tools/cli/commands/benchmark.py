"""Benchmark command handler for routing performance testing."""

from __future__ import annotations

from pathlib import Path

from ..format_options import emit_json


def _wants_json(args) -> bool:
    """True when the canonical ``--format json`` spelling was requested."""
    return getattr(args, "format", "text") == "json"


def _fail(args, message: str) -> int:
    """Report an error and return 1 (single JSON document in JSON mode)."""
    if _wants_json(args):
        emit_json({"error": message})
    else:
        print(message)
    return 1


def run_benchmark_command(args) -> int:
    """Handle benchmark command and subcommands."""
    subcommand = getattr(args, "benchmark_command", None)

    if subcommand == "run":
        return _run_benchmark(args)
    elif subcommand == "compare":
        return _compare_benchmark(args)
    elif subcommand == "report":
        return _generate_report(args)
    elif subcommand == "list":
        return _list_cases(args)
    else:
        print("Usage: kct benchmark <command>")
        print("Commands: run, compare, report, list")
        return 1


def _run_benchmark(args) -> int:
    """Run benchmark suite."""
    from kicad_tools.benchmark import BenchmarkRunner, Difficulty

    verbose = getattr(args, "verbose", False)
    cases = getattr(args, "cases", None)
    strategies = getattr(args, "strategies", None)
    difficulty = getattr(args, "difficulty", None)
    output = getattr(args, "output", None)
    save = getattr(args, "save", False)

    # Parse case filter
    case_names = None
    if cases:
        case_names = [c.strip() for c in cases.split(",")]

    # Parse strategy filter
    strategy_list = None
    if strategies:
        strategy_list = [s.strip() for s in strategies.split(",")]

    json_mode = _wants_json(args)

    # Parse difficulty filter
    difficulty_filter = None
    if difficulty:
        try:
            difficulty_filter = Difficulty(difficulty)
        except ValueError:
            return _fail(
                args, f"Unknown difficulty: {difficulty} (valid options: easy, medium, hard)"
            )

    runner = BenchmarkRunner(base_dir=Path.cwd(), verbose=verbose and not json_mode)

    if not json_mode:
        print("Running routing benchmarks...")
        print()

    results = runner.run_all(
        cases=case_names,
        strategies=strategy_list,
        difficulty=difficulty_filter,
    )

    if not results:
        return _fail(args, "No benchmarks were run.")

    if not json_mode:
        runner.print_summary()

    # Save results if requested
    saved_to = None
    if save or output:
        output_path = runner.save_results(path=output)
        saved_to = str(output_path)
        if not json_mode:
            print(f"\nResults saved to: {output_path}")

    if json_mode:
        emit_json(
            {
                "results": [
                    r.to_dict() for r in sorted(results, key=lambda r: (r.case_name, r.strategy))
                ],
                "saved_to": saved_to,
            }
        )

    return 0


def _compare_benchmark(args) -> int:
    """Compare current results against baseline."""
    from kicad_tools.benchmark import (
        BenchmarkRunner,
        check_regression,
        format_regression_report,
        load_baseline,
    )

    json_mode = _wants_json(args)

    baseline_path = getattr(args, "baseline", None)
    verbose = getattr(args, "verbose", False)
    fail_on_warning = getattr(args, "fail_on_warning", False)

    if not baseline_path:
        return _fail(args, "Error: --baseline is required")

    baseline_path = Path(baseline_path)
    if not baseline_path.exists():
        return _fail(args, f"Error: Baseline file not found: {baseline_path}")

    # Load baseline
    if not json_mode:
        print(f"Loading baseline from: {baseline_path}")
    baseline = load_baseline(baseline_path)
    if not json_mode:
        print(f"  Found {len(baseline)} baseline results")

        # Run current benchmarks
        print("\nRunning current benchmarks...")
    runner = BenchmarkRunner(base_dir=Path.cwd(), verbose=verbose and not json_mode)

    # Get case names from baseline to run same tests
    baseline_cases = list({r.case_name for r in baseline})
    baseline_strategies = list({r.strategy for r in baseline})

    results = runner.run_all(cases=baseline_cases, strategies=baseline_strategies)

    if not results:
        return _fail(args, "No benchmarks were run.")

    # Check for regressions
    if not json_mode:
        print("\nChecking for regressions...")
    regressions = check_regression(results, baseline)

    # Determine exit code
    errors = [r for r in regressions if r.severity == "error"]
    warnings = [r for r in regressions if r.severity == "warning"]

    if json_mode:
        emit_json(
            {
                "baseline": str(baseline_path),
                "baseline_count": len(baseline),
                "regressions": [
                    {
                        "case_name": r.case_name,
                        "strategy": r.strategy,
                        "metric": r.metric,
                        "baseline_value": r.baseline_value,
                        "current_value": r.current_value,
                        "threshold": r.threshold,
                        "severity": r.severity,
                        "change_percent": r.change_percent,
                    }
                    for r in sorted(regressions, key=lambda r: (r.case_name, r.strategy, r.metric))
                ],
                "errors": len(errors),
                "warnings": len(warnings),
                "ok": not errors and not (fail_on_warning and warnings),
            }
        )
    else:
        report = format_regression_report(regressions)
        print()
        print(report)

    if errors:
        return 1
    if fail_on_warning and warnings:
        return 1

    return 0


def _generate_report(args) -> int:
    """Generate benchmark report."""
    from kicad_tools.benchmark import load_baseline

    input_path = getattr(args, "input", None)
    format_type = getattr(args, "format", "text")

    if not input_path:
        return _fail(args, "Error: Input file is required")

    input_path = Path(input_path)
    if not input_path.exists():
        return _fail(args, f"Error: File not found: {input_path}")

    results = load_baseline(input_path)

    if format_type == "json":
        by_case: dict[str, list[dict]] = {}
        for r in sorted(results, key=lambda r: (r.case_name, r.strategy)):
            by_case.setdefault(r.case_name, []).append(r.to_dict())
        emit_json({"input": str(input_path), "cases": by_case})
    elif format_type == "markdown":
        _print_markdown_report(results)
    else:
        _print_text_report(results)

    return 0


def _print_text_report(results) -> None:
    """Print plain text report."""
    print("Routing Benchmark Report")
    print("=" * 60)
    print()

    # Group by case
    by_case = {}
    for r in results:
        if r.case_name not in by_case:
            by_case[r.case_name] = []
        by_case[r.case_name].append(r)

    for case_name, case_results in sorted(by_case.items()):
        print(f"## {case_name}")
        print()
        print(f"{'Strategy':<15} {'Routed':<12} {'Vias':<8} {'Length':<12} {'Time':<10}")
        print("-" * 57)

        for r in case_results:
            routed_str = f"{r.nets_routed}/{r.nets_total}"
            print(
                f"{r.strategy:<15} {routed_str:<12} {r.total_vias:<8} "
                f"{r.total_length_mm:.1f}mm{'':<6} {r.routing_time_sec:.2f}s"
            )
        print()


def _print_markdown_report(results) -> None:
    """Print markdown report."""
    print("# Routing Benchmark Report")
    print()
    print(f"Generated from: {results[0].git_commit if results else 'unknown'}")
    print()

    # Group by case
    by_case = {}
    for r in results:
        if r.case_name not in by_case:
            by_case[r.case_name] = []
        by_case[r.case_name].append(r)

    for case_name, case_results in sorted(by_case.items()):
        print(f"## {case_name}")
        print()
        print("| Strategy | Routed | Vias | Length | Time |")
        print("|----------|--------|------|--------|------|")

        for r in case_results:
            completion = f"{r.completion_rate:.0%}"
            print(
                f"| {r.strategy} | {r.nets_routed}/{r.nets_total} ({completion}) | "
                f"{r.total_vias} | {r.total_length_mm:.1f}mm | {r.routing_time_sec:.2f}s |"
            )
        print()


def _list_cases(args) -> int:
    """List available benchmark cases."""
    from kicad_tools.benchmark import BENCHMARK_CASES

    format_type = getattr(args, "format", "text")

    if format_type == "json":
        import json

        cases_data = []
        for case in BENCHMARK_CASES:
            cases_data.append(
                {
                    "name": case.name,
                    "difficulty": case.difficulty.value,
                    "expected_completion": case.expected_completion,
                    "expected_max_vias": case.expected_max_vias,
                    "pcb_path": case.pcb_path,
                    "is_synthetic": case.is_synthetic(),
                }
            )
        print(json.dumps(cases_data, indent=2))
    else:
        print("Available Benchmark Cases")
        print("=" * 60)
        print()
        print(f"{'Name':<25} {'Difficulty':<10} {'Completion':<12} {'Max Vias':<10}")
        print("-" * 57)

        for case in BENCHMARK_CASES:
            max_vias = str(case.expected_max_vias) if case.expected_max_vias else "-"
            print(
                f"{case.name:<25} {case.difficulty.value:<10} "
                f"{case.expected_completion:.0%}{'':<9} {max_vias:<10}"
            )

        print()
        print(f"Total: {len(BENCHMARK_CASES)} cases")

    return 0
