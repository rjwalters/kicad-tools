"""Benchmark result data structures."""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def _get_git_commit() -> str:
    """Get the current git commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


@dataclass
class BenchmarkResult:
    """Results from running a single benchmark case."""

    # Identification
    case_name: str
    strategy: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    git_commit: str = field(default_factory=_get_git_commit)

    # Completion metrics
    nets_total: int = 0
    nets_routed: int = 0
    completion_rate: float = 0.0

    # Per-net completeness breakdown (Issue #2611).
    #
    # These three fields partition the multi-pad signal nets in
    # ``nets_total`` into three exclusive buckets so a benchmark can
    # detect regressions in *kind* of failure (a fully routed net
    # becoming partial is materially different from a partial net
    # becoming unrouted).
    #
    # - ``nets_fully_routed``: all pads connected. Equivalent to the
    #   legacy ``nets_routed`` once the runner is connectivity-aware.
    # - ``nets_partial``: at least one pad connected, but at least one
    #   pad still unreachable from the largest connected component.
    # - ``nets_unrouted``: zero pads connected — the structural-floor
    #   candidates. For chorus-test-revA this number is 8 today
    #   (DAC_CLK + 7 small nets in the U5/U7/U9 cluster).
    # - ``unrouteable_nets``: names of the ``nets_unrouted`` set, kept
    #   so regression reports can diff which specific nets regressed.
    nets_fully_routed: int = 0
    nets_partial: int = 0
    nets_unrouted: int = 0
    unrouteable_nets: list[str] = field(default_factory=list)

    # Quality metrics
    total_segments: int = 0
    total_vias: int = 0
    total_length_mm: float = 0.0
    drc_violations: int = 0

    # Congestion metrics
    max_congestion: float = 0.0
    avg_congestion: float = 0.0
    congested_regions: int = 0

    # Performance metrics
    routing_time_sec: float = 0.0
    memory_peak_mb: float = 0.0
    iterations: int = 0

    # Configuration
    grid_resolution: float = 0.0
    trace_width: float = 0.0
    trace_clearance: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkResult:
        """Create from dictionary."""
        return cls(**data)

    def summary_line(self) -> str:
        """Return a single-line summary for display."""
        return (
            f"{self.case_name:<20} {self.strategy:<15} "
            f"{self.nets_routed}/{self.nets_total:<10} "
            f"{self.total_vias:<8} "
            f"{self.total_length_mm:.1f}mm "
            f"{self.routing_time_sec:.2f}s"
        )

    def meets_expectations(
        self,
        expected_completion: float,
        expected_max_vias: int | None = None,
    ) -> tuple[bool, list[str]]:
        """Check if result meets expected thresholds.

        Args:
            expected_completion: Minimum completion rate (0.0-1.0)
            expected_max_vias: Maximum allowed vias (optional)

        Returns:
            Tuple of (passed, list of failure messages)
        """
        failures = []

        if self.completion_rate < expected_completion:
            failures.append(
                f"Completion {self.completion_rate:.1%} < expected {expected_completion:.1%}"
            )

        if expected_max_vias is not None and self.total_vias > expected_max_vias:
            failures.append(f"Vias {self.total_vias} > expected max {expected_max_vias}")

        return len(failures) == 0, failures
