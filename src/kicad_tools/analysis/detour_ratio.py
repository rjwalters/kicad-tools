"""Per-connection detour ratio: drawn vs alone-on-empty length (issue #9003).

``kct route`` reports completion (nets routed, overflow, DRC) and shape
quality (``--report-stage-quality``), but nothing reports *how far out of its
way each connection went*.  The nearest existing measurement,
``redundant_copper_detour`` (issue #8218), compares a segment chain against
the **straight line between its own ends** -- a denominator that flags
legitimate avoidances (connector cut-outs, length-tuning meanders) while
missing the avoidable detour whose short corridor is blocked only by other
nets' copper.

This module's denominator is **alone-on-empty**: what the same search the
real route uses achieves for the connection when the board carries only
pads, keepouts and fixed copper.  A ratio near 1.0 means the drawn copper is
what the connection costs with nobody else on the board; a large ratio means
the drawn copper is avoidable in principle, which is the only version of the
metric that can drive a repair pass.

Purely computational -- no router, no IO, no CLI -- mirroring
``analysis/routing_quality.py`` so the numbers stay comparable across boards
and over time.  By design it never participates in any pass/fail verdict:
the report is advisory-only and never changes the exit code.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

# A connection is a candidate offender when its drawn copper exceeds this
# multiple of its alone-on-empty cost.  The issue's candidate threshold
# ("copper more than ~1.25x its alone-cost"); pinned as a module constant so
# numbers stay comparable across boards and over time.
DETOUR_RATIO_THRESHOLD = 1.25

# Sidecar schema tag (``<output_stem>.detour_ratio.json``).  Deliberately NOT
# the commit journal's contract: this is a derived quality report, not a
# replay log, and bumping it never breaks a decoder (issue #9003).
DETOUR_RATIO_SCHEMA = "kct-detour-ratio/1"

# How many worst rows the stdout report lists above the threshold.
WORST_ROWS_LIMIT = 10

# Pairing tolerance for matching a settled Route to its probe connection by
# path ends (world mm, sum of both end distances).  Escape-endpoint hops put
# a drawn route's ends a stub-length away from the pads the probe searched
# between -- QFP escape stubs snake several mm out of the package -- so the
# tolerance must cover a full stub length; 4 mm left every escaped net
# unmatched on board 04.  Mis-matching risk stays low: candidates sort
# smallest-cost-first and a swapped pad pairing costs strictly more than the
# true one.
DETOUR_ENDPOINT_MATCH_TOLERANCE_MM = 8.0


def route_path_ends(route: Any) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """A route-like object's two path ends, or ``None`` when it has none.

    Duck-typed on ``.segments`` with ``x1/y1/x2/y2`` floats so both the
    router's in-memory ``Route`` and any neutral record shape work.
    """
    segments = getattr(route, "segments", None)
    if not segments:
        return None
    first = segments[0]
    last = segments[-1]
    return ((first.x1, first.y1), (last.x2, last.y2))


def endpoint_pair_distance(
    a: tuple[tuple[float, float], tuple[float, float]],
    b: tuple[tuple[float, float], tuple[float, float]],
) -> float:
    """Sum of both end distances under the better of the two orientations."""
    (a1, a2), (b1, b2) = a, b

    def d(p: tuple[float, float], q: tuple[float, float]) -> float:
        return math.hypot(p[0] - q[0], p[1] - q[1])

    return min(d(a1, b1) + d(a2, b2), d(a1, b2) + d(a2, b1))


@dataclass
class DetourConnectionRecord:
    """One connection's drawn vs alone-on-empty measurement.

    ``connection_index`` is the connection's position within its net (both
    the probe and the real route emit one Route per connection, in the
    net's RSMT/MST edge order).  ``drawn_mm`` is ``None`` until the real
    route settled and a drawn Route was paired with this row; ``alone_mm``
    is ``None`` when the alone-on-empty probe found no path (reported, not
    dropped -- issue #9003's edge case).  ``unmatched`` marks a probe
    connection the settled route did not reproduce (a partially-routed net,
    or a topology difference between the pristine probe and the congested
    real run); its ratio is excluded from the distribution, not hidden.
    """

    net_name: str
    connection_index: int
    alone_mm: float | None = None
    drawn_mm: float | None = None
    probe_no_path: bool = False
    unmatched: bool = False
    endpoints: tuple[tuple[float, float], tuple[float, float]] | None = None
    """The probe connection's two path ends (world mm), for pairing the
    settled route.  Index pairing is not enough: the real route emits
    infrastructure fragments (escape-stub hops, intra-IC ties) alongside
    full connections, so drawn routes are matched by their path ends
    instead of by position."""

    @property
    def ratio(self) -> float | None:
        """``drawn_mm / alone_mm``, or ``None`` when not computable."""
        if self.drawn_mm is None or self.alone_mm is None or self.alone_mm <= 0.0:
            return None
        return self.drawn_mm / self.alone_mm

    @property
    def is_offender(self) -> bool:
        ratio = self.ratio
        return ratio is not None and ratio > DETOUR_RATIO_THRESHOLD


@dataclass
class DetourRatioSummary:
    """Distribution summary over a board's detour records."""

    probed: int = 0
    matched: int = 0
    no_path: int = 0
    unmatched: int = 0
    median: float | None = None
    p90: float | None = None
    p99: float | None = None
    max: float | None = None
    total_drawn_mm: float = 0.0
    total_alone_mm: float = 0.0
    unattributed_drawn_mm: float = 0.0
    threshold: float = DETOUR_RATIO_THRESHOLD
    worst: list[DetourConnectionRecord] = field(default_factory=list)
    offender_count: int = 0
    """Total connections above the threshold; ``worst`` is its capped head."""


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile of ``q`` in ``[0, 1]`` over a copy-sorted list.

    ``percentile([x], 0.99) == x`` and ``percentile([a, b], 0.99) == b`` --
    the n=1 and n=2 edges the issue's test plan calls out.  ``None`` only for
    an empty input, mirroring how an unrouted board has no distribution.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize_detour_records(
    records: Iterable[DetourConnectionRecord],
    *,
    unattributed_drawn_mm: float = 0.0,
    worst_limit: int = WORST_ROWS_LIMIT,
) -> DetourRatioSummary:
    """Summarise detour records into the distribution the report prints.

    Ratios enter the distribution only when both sides measured and the
    probe found a path; ``no_path`` and ``unmatched`` rows are counted and
    reported but never silently dropped (issue #9003's edge-case rule).
    ``unattributed_drawn_mm`` is settled copper no probe connection claimed
    (escape-stub hops and similar infrastructure fragments) -- reported in
    the totals, never charged to a connection's ratio.
    """
    recs = list(records)
    ratios: list[float] = []
    total_drawn = 0.0
    total_alone = 0.0
    offenders: list[DetourConnectionRecord] = []
    summary = DetourRatioSummary(probed=len(recs))
    for rec in recs:
        if rec.probe_no_path:
            summary.no_path += 1
        elif rec.unmatched:
            summary.unmatched += 1
        ratio = rec.ratio
        if ratio is None:
            continue
        summary.matched += 1
        ratios.append(ratio)
        total_drawn += rec.drawn_mm or 0.0
        total_alone += rec.alone_mm or 0.0
        if rec.is_offender:
            offenders.append(rec)
    summary.median = percentile(ratios, 0.5)
    summary.p90 = percentile(ratios, 0.9)
    summary.p99 = percentile(ratios, 0.99)
    summary.max = percentile(ratios, 1.0)
    summary.total_drawn_mm = total_drawn
    summary.total_alone_mm = total_alone
    summary.unattributed_drawn_mm = unattributed_drawn_mm
    # Worst first; ties break on (net_name, connection_index) so ordering is
    # stable and deterministic run over run.
    summary.offender_count = len(offenders)
    summary.worst = sorted(
        offenders,
        key=lambda r: (-(r.ratio or 0.0), r.net_name, r.connection_index),
    )[:worst_limit]
    return summary


def format_detour_report(summary: DetourRatioSummary) -> str:
    """Render the stdout report.  Advisory-only; never a pass/fail verdict."""
    lines = [
        "=== Detour ratio (drawn vs alone-on-empty, issue #9003) ===",
        (
            f"  connections: {summary.probed} probed, {summary.matched} matched, "
            f"{summary.no_path} probe-no-path, {summary.unmatched} unmatched"
        ),
    ]
    if summary.matched:
        parts = [
            f"median {summary.median:.2f}",
            f"p90 {summary.p90:.2f}",
            f"p99 {summary.p99:.2f}",
            f"max {summary.max:.2f}",
        ]
        lines.append("  ratio: " + ", ".join(parts))
        copper_note = (
            f"  copper: drawn {summary.total_drawn_mm:.1f}mm vs alone "
            f"{summary.total_alone_mm:.1f}mm "
            f"({summary.total_drawn_mm / summary.total_alone_mm:.3f}x pooled)"
            if summary.total_alone_mm > 0.0
            else "  copper: drawn only"
        )
        if summary.unattributed_drawn_mm > 0.0:
            copper_note += (
                f"; +{summary.unattributed_drawn_mm:.1f}mm drawn copper "
                "unattributed to a probed connection (escape-stub hops and "
                "similar infrastructure fragments)"
            )
        lines.append(copper_note)
        lines.append(
            f"  offenders (> {summary.threshold:.2f}x alone-cost): {summary.offender_count}"
        )
        for rec in summary.worst:
            ratio = rec.ratio or 0.0
            lines.append(
                f"    {rec.net_name} #{rec.connection_index}: drawn "
                f"{rec.drawn_mm:.2f}mm alone {rec.alone_mm:.2f}mm "
                f"ratio {ratio:.2f}"
            )
    else:
        lines.append("  ratio: no measurable connections on this board")
    if summary.no_path:
        lines.append(
            f"  note: {summary.no_path} connection(s) found no path on the "
            "empty board and carry no ratio"
        )
    return "\n".join(lines)


def detour_ratio_payload(
    summary: DetourRatioSummary,
    records: Sequence[DetourConnectionRecord],
) -> dict:
    """Machine-readable sidecar payload (``<output_stem>.detour_ratio.json``).

    Aggregating the tail across the fleet boards is the measurement's actual
    verdict, so every row ships -- including the ``no_path``/``unmatched``
    ones -- not just the stdout worst-N.
    """

    def row(rec: DetourConnectionRecord) -> dict:
        return {
            "net": rec.net_name,
            "connection": rec.connection_index,
            "drawn_mm": rec.drawn_mm,
            "alone_mm": rec.alone_mm,
            "ratio": rec.ratio,
            "probe_no_path": rec.probe_no_path,
            "unmatched": rec.unmatched,
            "offender": rec.is_offender,
        }

    return {
        "schema": DETOUR_RATIO_SCHEMA,
        "threshold": summary.threshold,
        "summary": {
            "probed": summary.probed,
            "matched": summary.matched,
            "no_path": summary.no_path,
            "unmatched": summary.unmatched,
            "median": summary.median,
            "p90": summary.p90,
            "p99": summary.p99,
            "max": summary.max,
            "total_drawn_mm": summary.total_drawn_mm,
            "total_alone_mm": summary.total_alone_mm,
            "unattributed_drawn_mm": summary.unattributed_drawn_mm,
            "offenders": summary.offender_count,
        },
        "connections": [row(rec) for rec in records],
    }
