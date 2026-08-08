"""Advisory routing-quality metrics over PCB copper segments (issue #4623).

``kct check``'s gates are all binary (connectivity, DRC, clearance, ERC/LVS
rollup), so a board can be DRC-clean while carrying copper that is visibly
poor: sub-0.25 mm fragments, H/V staircases instead of true 45° geometry,
off-axis stubs, zero-length segments.  This module computes *descriptive*
statistics over ``pcb.segments`` so that drift in routing quality becomes
observable.  It is purely computational -- no CLI, no IO, and by design it
never participates in any pass/fail verdict (the wire-in in
``cli/check_cmd.py`` is advisory-only).

Metric definitions are pinned in issue #4623; the thresholds below are
module constants chosen to match the #4615 measurement so numbers stay
comparable across boards and over time.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.schema.pcb import PCB, Segment

# Issue #4732: the metric core is expressed over this neutral record shape
# -- ``(start_xy, end_xy, layer_key, net_number)`` -- so producers other
# than a schema ``PCB`` (notably the router's in-memory
# ``router.primitives.Segment``, whose layer is a ``Layer`` enum and whose
# endpoints are ``x1/y1/x2/y2`` floats) can be measured with the SAME
# definitions the ``kct check`` gates consume.  ``layer_key`` only has to
# be hashable and comparable for equality within a single call; the
# staircase join never interprets it.  Keeping the core here (rather than
# duplicating it router-side) is what makes per-stage router numbers
# directly comparable to the numbers #4646/#4651 report on a board.
QualitySegmentRecord = tuple[tuple[float, float], tuple[float, float], Hashable, int]

# Segments shorter than this are counted as "fragments" -- copper slivers
# that almost always indicate routing churn rather than intent.  Matches
# the #4615 measurement (67% of that board's segments were sub-0.25 mm).
FRAGMENT_LENGTH_MM = 0.25

# An orthogonal segment shorter than this that meets a perpendicular
# orthogonal segment (same net, same layer, also shorter than this) at a
# shared endpoint is counted as a "staircase step" -- an H/V zigzag that a
# clean router would express as a single 45° segment.  Matches the #4615
# measurement basis (42% staircase steps at 0.6 mm legs).
STAIRCASE_STEP_MM = 0.6

# Angular tolerance (degrees) for classifying a segment as orthogonal
# (0°/90°) or true diagonal (45°).  Everything else is off-axis.
ANGLE_TOLERANCE_DEG = 0.5

# Coordinate tolerance (mm) for zero-length detection and endpoint
# sharing.  KiCad coordinates are nm-quantized, so exact equality is the
# common case; the epsilon only absorbs float noise.
COORD_EPSILON_MM = 1e-6

# Rule ids emitted by the opt-in threshold gate (issue #4651).  Defined
# here (next to the metric definitions) so the CLI wire-in and any JSON
# consumer share a single source for the identifiers.
RULE_FRAGMENT_FRACTION = "routing_quality_fragment_fraction"
RULE_STAIRCASE_FRACTION = "routing_quality_staircase_fraction"


@dataclass(frozen=True)
class RoutingQualityMetrics:
    """Descriptive routing-quality statistics for one board.

    All counts are over ``pcb.segments`` (copper trace segments).  The
    length/direction statistics exclude zero-length segments; the
    fractions use the same zero-length-excluded population as their
    denominator (and are ``0.0`` when that population is empty).
    """

    total_segments: int
    nets_with_copper: int
    segments_per_net: float
    zero_length_count: int
    median_length_mm: float
    fragment_count: int
    fragment_fraction: float
    orthogonal_count: int
    diagonal_45_count: int
    off_axis_count: int
    staircase_step_count: int
    staircase_fraction: float

    def to_dict(self) -> dict[str, int | float]:
        """Serialize to the stable JSON contract emitted by ``kct check``."""
        return {
            "total_segments": self.total_segments,
            "nets_with_copper": self.nets_with_copper,
            "segments_per_net": self.segments_per_net,
            "zero_length_count": self.zero_length_count,
            "median_length_mm": self.median_length_mm,
            "fragment_count": self.fragment_count,
            "fragment_fraction": self.fragment_fraction,
            "orthogonal_count": self.orthogonal_count,
            "diagonal_45_count": self.diagonal_45_count,
            "off_axis_count": self.off_axis_count,
            "staircase_step_count": self.staircase_step_count,
            "staircase_fraction": self.staircase_fraction,
        }


@dataclass(frozen=True)
class ThresholdBreach:
    """One routing-quality metric that exceeded its opt-in ceiling.

    Produced by :func:`evaluate_routing_quality_thresholds` (issue
    #4651).  The CLI converts each breach into a real ``DRCViolation``
    so the gate participates in the normal verdict / exit-code path;
    this dataclass stays free of any ``validate`` dependency so the
    analysis package remains purely computational.

    Attributes:
        rule_id: Stable violation rule id
            (:data:`RULE_FRAGMENT_FRACTION` /
            :data:`RULE_STAIRCASE_FRACTION`).
        metric: The ``RoutingQualityMetrics`` field name that breached
            (e.g. ``"fragment_fraction"``).
        actual: The measured value.
        limit: The user-supplied ceiling it exceeded.
        message: Human-readable description of the breach.
    """

    rule_id: str
    metric: str
    actual: float
    limit: float
    message: str


def evaluate_routing_quality_thresholds(
    metrics: RoutingQualityMetrics,
    *,
    max_fragment_fraction: float | None = None,
    max_staircase_fraction: float | None = None,
) -> list[ThresholdBreach]:
    """Evaluate opt-in routing-quality ceilings over computed metrics.

    Pure function (issue #4651): compares each supplied ceiling against
    the corresponding metric and returns one :class:`ThresholdBreach`
    per exceeded threshold.  A ``None`` ceiling means "not gated on this
    metric" -- with both ceilings ``None`` the result is always empty,
    which is exactly today's advisory-only behavior.

    Comparison is strictly-greater-than: a metric exactly **equal** to
    its ceiling passes (so ``--max-fragment-fraction 0.0`` fails only
    when at least one fragment exists).
    """
    breaches: list[ThresholdBreach] = []
    if max_fragment_fraction is not None and metrics.fragment_fraction > max_fragment_fraction:
        breaches.append(
            ThresholdBreach(
                rule_id=RULE_FRAGMENT_FRACTION,
                metric="fragment_fraction",
                actual=metrics.fragment_fraction,
                limit=max_fragment_fraction,
                message=(
                    f"Routing-quality gate: fragment_fraction "
                    f"{metrics.fragment_fraction:.4f} exceeds the "
                    f"--max-fragment-fraction ceiling {max_fragment_fraction:.4f} "
                    f"({metrics.fragment_count} segment(s) shorter than "
                    f"{FRAGMENT_LENGTH_MM} mm)"
                ),
            )
        )
    if max_staircase_fraction is not None and metrics.staircase_fraction > max_staircase_fraction:
        breaches.append(
            ThresholdBreach(
                rule_id=RULE_STAIRCASE_FRACTION,
                metric="staircase_fraction",
                actual=metrics.staircase_fraction,
                limit=max_staircase_fraction,
                message=(
                    f"Routing-quality gate: staircase_fraction "
                    f"{metrics.staircase_fraction:.4f} exceeds the "
                    f"--max-staircase-fraction ceiling {max_staircase_fraction:.4f} "
                    f"({metrics.staircase_step_count} H/V staircase step(s) with "
                    f"legs shorter than {STAIRCASE_STEP_MM} mm)"
                ),
            )
        )
    return breaches


def routing_quality_gate_dict(
    breaches: list[ThresholdBreach],
    *,
    max_fragment_fraction: float | None,
    max_staircase_fraction: float | None,
) -> dict[str, object]:
    """Serialize the threshold-gate outcome for the ``routing_quality`` JSON.

    Issue #4651: when gating is enabled, the ``routing_quality`` object
    gains the applied thresholds and a pass/fail verdict so CI consumers
    can see *why* the gate fired.  Emitted only when at least one
    ceiling was supplied (the no-flag JSON contract stays byte-identical
    to the advisory-only #4646 shape).
    """
    return {
        "thresholds": {
            "max_fragment_fraction": max_fragment_fraction,
            "max_staircase_fraction": max_staircase_fraction,
        },
        "gate_passed": not breaches,
        "gate_breaches": [
            {
                "rule_id": b.rule_id,
                "metric": b.metric,
                "actual": b.actual,
                "limit": b.limit,
            }
            for b in breaches
        ],
    }


def _quantize(point: tuple[float, float]) -> tuple[int, int]:
    """Quantize a coordinate to the COORD_EPSILON_MM grid for hashing."""
    return (round(point[0] / COORD_EPSILON_MM), round(point[1] / COORD_EPSILON_MM))


def _classify_direction(dx: float, dy: float) -> str:
    """Classify a non-zero-length segment direction.

    Returns ``"horizontal"``, ``"vertical"``, ``"diagonal_45"``, or
    ``"off_axis"``.  Horizontal/vertical together form the *orthogonal*
    class; they are kept distinct so the staircase join can require
    perpendicularity.
    """
    angle = math.degrees(math.atan2(abs(dy), abs(dx)))
    if angle <= ANGLE_TOLERANCE_DEG:
        return "horizontal"
    if angle >= 90.0 - ANGLE_TOLERANCE_DEG:
        return "vertical"
    if abs(angle - 45.0) <= ANGLE_TOLERANCE_DEG:
        return "diagonal_45"
    return "off_axis"


def compute_routing_quality(pcb: PCB) -> RoutingQualityMetrics:
    """Compute advisory routing-quality metrics over ``pcb.segments``.

    Pure and side-effect free.  Gracefully handles a board with zero
    copper segments (all counts 0, fractions 0.0 -- never divides by
    zero).

    Thin adapter over :func:`compute_routing_quality_from_records`; the
    metric definitions live there so non-``PCB`` producers (issue #4732's
    per-stage router instrumentation) measure identically.
    """
    segments: list[Segment] = list(pcb.segments)
    return compute_routing_quality_from_records(
        (seg.start, seg.end, seg.layer, seg.net_number) for seg in segments
    )


def compute_routing_quality_from_records(
    records: Iterable[QualitySegmentRecord],
) -> RoutingQualityMetrics:
    """Compute routing-quality metrics over neutral segment records.

    Issue #4732: the metric core, expressed over
    ``(start_xy, end_xy, layer_key, net_number)`` tuples instead of a
    schema ``PCB``.  :func:`compute_routing_quality` delegates here, so a
    board measured through either entry point yields identical numbers.

    Pure and side-effect free; ``records`` is consumed exactly once.
    """
    materialized: list[QualitySegmentRecord] = list(records)
    total_segments = len(materialized)
    net_numbers = {net for _s, _e, _layer, net in materialized if net != 0}
    nets_with_copper = len(net_numbers)
    segments_per_net = total_segments / nets_with_copper if nets_with_copper else 0.0

    zero_length_count = 0
    lengths: list[float] = []
    fragment_count = 0
    orthogonal_count = 0
    diagonal_45_count = 0
    off_axis_count = 0

    # (net, layer, quantized endpoint) -> set of orientations of *short*
    # orthogonal segments touching that point.  Only a perpendicular
    # partner makes a segment a staircase step, so a segment's own entry
    # can never make itself count.
    short_orth_endpoints: dict[tuple[int, Hashable, tuple[int, int]], set[str]] = {}
    # (segment orientation, net, layer, endpoints) for each short orthogonal
    # segment, revisited in the second pass.
    short_orth_segments: list[tuple[str, int, Hashable, tuple[int, int], tuple[int, int]]] = []

    for start, end, layer, net_number in materialized:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.hypot(dx, dy)
        if length <= COORD_EPSILON_MM:
            zero_length_count += 1
            continue

        lengths.append(length)
        if length < FRAGMENT_LENGTH_MM:
            fragment_count += 1

        direction = _classify_direction(dx, dy)
        if direction in ("horizontal", "vertical"):
            orthogonal_count += 1
            if length < STAIRCASE_STEP_MM:
                p1 = _quantize(start)
                p2 = _quantize(end)
                short_orth_segments.append((direction, net_number, layer, p1, p2))
                for point in (p1, p2):
                    key = (net_number, layer, point)
                    short_orth_endpoints.setdefault(key, set()).add(direction)
        elif direction == "diagonal_45":
            diagonal_45_count += 1
        else:
            off_axis_count += 1

    # Second pass: a short orthogonal segment is a staircase step when a
    # perpendicular short orthogonal segment of the same net and layer
    # shares either endpoint.  Counts segments (not pairs).
    staircase_step_count = 0
    for direction, net, layer, p1, p2 in short_orth_segments:
        perpendicular = "vertical" if direction == "horizontal" else "horizontal"
        for point in (p1, p2):
            if perpendicular in short_orth_endpoints.get((net, layer, point), ()):
                staircase_step_count += 1
                break

    nonzero_count = len(lengths)
    median_length_mm = float(median(lengths)) if lengths else 0.0
    fragment_fraction = fragment_count / nonzero_count if nonzero_count else 0.0
    staircase_fraction = staircase_step_count / nonzero_count if nonzero_count else 0.0

    return RoutingQualityMetrics(
        total_segments=total_segments,
        nets_with_copper=nets_with_copper,
        segments_per_net=segments_per_net,
        zero_length_count=zero_length_count,
        median_length_mm=median_length_mm,
        fragment_count=fragment_count,
        fragment_fraction=fragment_fraction,
        orthogonal_count=orthogonal_count,
        diagonal_45_count=diagonal_45_count,
        off_axis_count=off_axis_count,
        staircase_step_count=staircase_step_count,
        staircase_fraction=staircase_fraction,
    )
