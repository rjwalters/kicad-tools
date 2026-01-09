"""Regression detection for benchmark results."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .result import BenchmarkResult


@dataclass
class Regression:
    """A detected regression between current and baseline results."""

    case_name: str
    strategy: str
    metric: str
    baseline_value: float
    current_value: float
    threshold: float
    severity: str  # 'warning' or 'error'

    @property
    def change_percent(self) -> float:
        """Calculate percentage change from baseline."""
        if self.baseline_value == 0:
            return float("inf") if self.current_value != 0 else 0.0
        return ((self.current_value - self.baseline_value) / self.baseline_value) * 100

    def __str__(self) -> str:
        direction = "decreased" if self.current_value < self.baseline_value else "increased"
        return (
            f"[{self.severity.upper()}] {self.case_name}/{self.strategy}: "
            f"{self.metric} {direction} from {self.baseline_value:.2f} to {self.current_value:.2f} "
            f"({self.change_percent:+.1f}%)"
        )


# Regression thresholds (relative change that triggers a warning/error)
REGRESSION_THRESHOLDS = {
    # (warning_threshold, error_threshold, higher_is_worse)
    "completion_rate": (0.05, 0.10, False),  # 5%/10% drop is warning/error
    "total_vias": (0.20, 0.50, True),  # 20%/50% increase is warning/error
    "total_length_mm": (0.15, 0.30, True),  # 15%/30% increase
    "routing_time_sec": (1.0, 2.0, True),  # 100%/200% increase (perf can vary)
}


def check_regression(
    current: list[BenchmarkResult],
    baseline: list[BenchmarkResult],
    metrics: list[str] | None = None,
) -> list[Regression]:
    """Compare current results against baseline to detect regressions.

    Args:
        current: Current benchmark results
        baseline: Baseline results to compare against
        metrics: Metrics to check (default: all in REGRESSION_THRESHOLDS)

    Returns:
        List of detected regressions
    """
    if metrics is None:
        metrics = list(REGRESSION_THRESHOLDS.keys())

    # Index baseline by (case_name, strategy)
    baseline_map: dict[tuple[str, str], BenchmarkResult] = {}
    for r in baseline:
        key = (r.case_name, r.strategy)
        baseline_map[key] = r

    regressions: list[Regression] = []

    for curr in current:
        key = (curr.case_name, curr.strategy)
        base = baseline_map.get(key)
        if base is None:
            continue  # No baseline to compare against

        for metric in metrics:
            if metric not in REGRESSION_THRESHOLDS:
                continue

            warn_thresh, err_thresh, higher_is_worse = REGRESSION_THRESHOLDS[metric]

            base_val = getattr(base, metric, None)
            curr_val = getattr(curr, metric, None)

            if base_val is None or curr_val is None:
                continue

            # Calculate relative change
            if base_val == 0:
                if curr_val == 0:
                    continue  # No change
                relative_change = float("inf")
            else:
                relative_change = (curr_val - base_val) / abs(base_val)

            # Check for regression based on direction
            if higher_is_worse:
                # Higher values are worse (vias, time, length)
                if relative_change > err_thresh:
                    regressions.append(
                        Regression(
                            case_name=curr.case_name,
                            strategy=curr.strategy,
                            metric=metric,
                            baseline_value=base_val,
                            current_value=curr_val,
                            threshold=err_thresh,
                            severity="error",
                        )
                    )
                elif relative_change > warn_thresh:
                    regressions.append(
                        Regression(
                            case_name=curr.case_name,
                            strategy=curr.strategy,
                            metric=metric,
                            baseline_value=base_val,
                            current_value=curr_val,
                            threshold=warn_thresh,
                            severity="warning",
                        )
                    )
            else:
                # Lower values are worse (completion rate)
                if relative_change < -err_thresh:
                    regressions.append(
                        Regression(
                            case_name=curr.case_name,
                            strategy=curr.strategy,
                            metric=metric,
                            baseline_value=base_val,
                            current_value=curr_val,
                            threshold=err_thresh,
                            severity="error",
                        )
                    )
                elif relative_change < -warn_thresh:
                    regressions.append(
                        Regression(
                            case_name=curr.case_name,
                            strategy=curr.strategy,
                            metric=metric,
                            baseline_value=base_val,
                            current_value=curr_val,
                            threshold=warn_thresh,
                            severity="warning",
                        )
                    )

    return regressions


def load_baseline(path: Path | str) -> list[BenchmarkResult]:
    """Load baseline results from JSON file.

    Args:
        path: Path to baseline JSON file

    Returns:
        List of BenchmarkResult objects
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Baseline file not found: {path}")

    with open(path) as f:
        data = json.load(f)

    results = []
    for r in data.get("results", []):
        results.append(BenchmarkResult.from_dict(r))

    return results


def save_baseline(
    results: list[BenchmarkResult],
    path: Path | str,
    version: str | None = None,
) -> None:
    """Save results as a baseline file.

    Args:
        results: Benchmark results to save
        path: Output path
        version: Optional version tag (e.g., 'v0.8.0')
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "version": version,
        "results": [r.to_dict() for r in results],
    }

    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def format_regression_report(regressions: list[Regression]) -> str:
    """Format regressions as a human-readable report.

    Args:
        regressions: List of detected regressions

    Returns:
        Formatted report string
    """
    if not regressions:
        return "No regressions detected."

    lines = [
        "Regression Report",
        "=" * 50,
        "",
    ]

    # Group by severity
    errors = [r for r in regressions if r.severity == "error"]
    warnings = [r for r in regressions if r.severity == "warning"]

    if errors:
        lines.append(f"ERRORS ({len(errors)}):")
        for r in errors:
            lines.append(f"  {r}")
        lines.append("")

    if warnings:
        lines.append(f"WARNINGS ({len(warnings)}):")
        for r in warnings:
            lines.append(f"  {r}")
        lines.append("")

    lines.append(f"Total: {len(errors)} errors, {len(warnings)} warnings")

    return "\n".join(lines)
