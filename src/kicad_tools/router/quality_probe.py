"""Per-stage routing-quality instrumentation for the ``kct route`` pipeline.

Issue #4732 (slice 1).  ``kct route``'s post-route pipeline is a chain of
copper-mutating stages::

    route -> [optimize] -> [DRC nudge] -> finalize/demote -> write

Issue #4615 measured the *end* of that chain (67% sub-0.25 mm fragments,
42% staircase legs on a board routed with default ``kct route``), and
#4646/#4651 shipped the reporting + opt-in gates on ``kct check`` -- but
nothing says **which stage** leaves (or creates) those artifacts.  The
#4732 hypotheses are mutually exclusive only in their remedy:

1. the optimizer's collision checker vetoes most compressions under
   congestion (then ``post-optimize`` ~= ``pre-optimize``),
2. the optimizer's ``min_segment_length`` (0.05 mm) never targets
   sub-0.25 mm fragments as such (same signature as 1, distinguished by
   the fragment vs staircase split),
3. chain boundaries stop merges (again a small optimize delta),
4. the DRC nudge re-introduces jogs *after* the optimizer with no
   re-consolidation (then ``post-nudge`` is WORSE than ``post-optimize``),
5. residual-overflow mode changes which moves validate.

This module is the measurement that separates them.  It is deliberately
**read-only**: it never touches routes, the grid, or any config, so
enabling it cannot change routed copper.

Metric definitions are not re-implemented here -- the core lives in
:mod:`kicad_tools.analysis.routing_quality` and is shared with the
``kct check`` reporting/gating surface, so a per-stage number is directly
comparable to the number ``kct check`` prints for the same board.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kicad_tools.analysis.routing_quality import (
    QualitySegmentRecord,
    RoutingQualityMetrics,
    compute_routing_quality_from_records,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .primitives import Route

# Canonical stage labels.  Kept as module constants so the CLI wire-in and
# any test assert against one spelling.
STAGE_PRE_OPTIMIZE = "pre-optimize"
STAGE_POST_OPTIMIZE = "post-optimize"
STAGE_POST_NUDGE = "post-nudge"
STAGE_POST_FINALIZE = "post-finalize"

# Metrics whose stage-to-stage movement is the point of the probe.  Both
# are "lower is better", which the delta arrow rendering assumes.
TRACKED_FRACTIONS = ("fragment_fraction", "staircase_fraction")


def segment_records(routes: Iterable[Route]) -> Iterator[QualitySegmentRecord]:
    """Yield neutral quality records for every segment on ``routes``.

    Adapts ``router.primitives.Segment`` (``x1/y1/x2/y2`` floats, a
    ``Layer`` enum, an ``int`` net) to the
    ``(start_xy, end_xy, layer_key, net)`` shape
    :func:`~kicad_tools.analysis.routing_quality.compute_routing_quality_from_records`
    consumes.  The ``Layer`` enum is hashable and only ever compared for
    equality inside the metric core, so it is passed through as the layer
    key without stringification.
    """
    for route in routes:
        for seg in route.segments:
            yield ((seg.x1, seg.y1), (seg.x2, seg.y2), seg.layer, seg.net)


def measure_routes(routes: Iterable[Route]) -> RoutingQualityMetrics:
    """Compute routing-quality metrics over in-memory router routes."""
    return compute_routing_quality_from_records(segment_records(routes))


@dataclass(frozen=True)
class StageQuality:
    """One recorded pipeline stage and its routing-quality metrics."""

    stage: str
    metrics: RoutingQualityMetrics

    def to_dict(self) -> dict[str, Any]:
        """Serialize as ``{"stage": ..., **metrics}``."""
        return {"stage": self.stage, **self.metrics.to_dict()}


class StageQualityRecorder:
    """Records routing-quality metrics at named points of the route pipeline.

    Purely observational: :meth:`record` reads segments and computes
    statistics, and nothing on this class mutates a route, a grid, or a
    config.  Stages that do not run for a given invocation (e.g.
    ``post-optimize`` under ``--no-optimize``, or every geometric stage
    under a non-grid engine) are simply never recorded, so the report
    shows exactly the stages that executed.
    """

    def __init__(self) -> None:
        self._stages: list[StageQuality] = []

    @property
    def stages(self) -> list[StageQuality]:
        """The recorded stages, in the order they were recorded."""
        return list(self._stages)

    def __bool__(self) -> bool:
        return bool(self._stages)

    def record(self, stage: str, routes: Iterable[Route]) -> RoutingQualityMetrics:
        """Measure ``routes`` and append the result under ``stage``."""
        metrics = measure_routes(routes)
        self._stages.append(StageQuality(stage=stage, metrics=metrics))
        return metrics

    def get(self, stage: str) -> RoutingQualityMetrics | None:
        """Return the metrics recorded for ``stage`` (last wins), or None."""
        for entry in reversed(self._stages):
            if entry.stage == stage:
                return entry.metrics
        return None

    def delta(self, metric: str, first: str, last: str) -> float | None:
        """Return ``last[metric] - first[metric]``, or None if either is missing."""
        a = self.get(first)
        b = self.get(last)
        if a is None or b is None:
            return None
        return float(getattr(b, metric)) - float(getattr(a, metric))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the whole recording for machine consumers."""
        return {"stages": [entry.to_dict() for entry in self._stages]}

    def format_report(self) -> str:
        """Render the advisory per-stage table.

        Returns an empty string when nothing was recorded, so the caller
        can print unconditionally without emitting a stray header.
        """
        if not self._stages:
            return ""

        header = (
            "\n--- Routing quality by stage (advisory, issue #4732) ---\n"
            f"  {'stage':<14} {'segs':>7} {'frag%':>7} {'stair%':>7} "
            f"{'45deg%':>7} {'zero':>5} {'median_mm':>10}"
        )
        lines = [header]
        for entry in self._stages:
            m = entry.metrics
            nonzero = m.total_segments - m.zero_length_count
            diag_pct = (m.diagonal_45_count / nonzero * 100.0) if nonzero else 0.0
            lines.append(
                f"  {entry.stage:<14} {m.total_segments:>7d} "
                f"{m.fragment_fraction * 100.0:>7.1f} "
                f"{m.staircase_fraction * 100.0:>7.1f} "
                f"{diag_pct:>7.1f} {m.zero_length_count:>5d} "
                f"{m.median_length_mm:>10.3f}"
            )

        for line in self._format_deltas():
            lines.append(line)
        lines.append(
            "  (advisory only -- these numbers do not change routed copper or any exit code)"
        )
        return "\n".join(lines)

    def _format_deltas(self) -> list[str]:
        """Render one consecutive-stage delta line per tracked fraction."""
        if len(self._stages) < 2:
            return []
        lines: list[str] = []
        for previous, current in zip(self._stages, self._stages[1:], strict=False):
            parts: list[str] = []
            for metric in TRACKED_FRACTIONS:
                before = float(getattr(previous.metrics, metric))
                after = float(getattr(current.metrics, metric))
                change = after - before
                marker = "improved" if change < 0 else ("worse" if change > 0 else "flat")
                parts.append(f"{metric} {change * 100.0:+.1f}pp ({marker})")
            lines.append(f"  {previous.stage} -> {current.stage}: " + ", ".join(parts))
        return lines
