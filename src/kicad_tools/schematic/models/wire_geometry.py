"""Wire-vs-wire geometric union primitives for schematic connectivity.

KiCad unions the nets of *any* two touching or overlapping collinear wire
segments, not just wires that share an exact endpoint.  The schematic
connectivity model historically unioned wires only at their two endpoints,
so a generator using the stub-wire+label idiom could produce mid-segment
overlaps that KiCad merges but kicad-tools kept separate — a silent,
LVS-grade net-merge divergence (issue #4143).

This module provides the single, tested geometric primitive
(:func:`wire_segments_connect`) used by both
``SchematicNetlistMixin._build_connectivity_graph`` and
``SchematicValidationMixin.validate_power_nets`` so the two connectivity
builders cannot drift apart.

Two axis-aligned wire segments A and B are considered electrically
connected (beyond a shared endpoint) when either:

* **Collinear overlap** — A and B lie on the same infinite line and their
  projected parametric ranges share a sub-segment of nonzero length.  This
  covers A containing B, B containing A, and partial overlap where neither
  contains the other (the softstart rev-B repro geometry).
* **Mid-segment T-touch** — one endpoint of A lands in the *interior* of B
  (not at B's endpoints), or vice-versa.

A shared endpoint alone is NOT reported here: that case is already handled
by the endpoint Union-Find in the connectivity builders.  This mirrors, but
is deliberately distinct from, the pin-endpoint-only semantics of
#4020/#4003 — *pins* attach to wires only at endpoints/junctions, while
*wires* union with each other on any overlap/T-touch, matching KiCad.
"""

from __future__ import annotations

# Tolerance (mm) for perpendicular-distance-to-line and endpoint-coincidence
# checks.  Matches ``SchematicElementsMixin.POINT_TOLERANCE`` (0.1mm) — the
# same permissive snap tolerance used elsewhere in the connectivity graph.
# Do NOT use the tighter 0.005mm from ``sch_cleanup_wires``: that value is
# tuned for a *destructive* wire-deletion decision, whereas connectivity
# union is non-destructive and should be at least as permissive as KiCad's
# own snap tolerance.
POINT_TOLERANCE = 0.1

Point = tuple[float, float]


def _points_equal(p1: Point, p2: Point, tolerance: float = POINT_TOLERANCE) -> bool:
    """Return True if two points coincide within *tolerance*."""
    return abs(p1[0] - p2[0]) <= tolerance and abs(p1[1] - p2[1]) <= tolerance


def _point_on_segment_interior(
    point: Point, seg_start: Point, seg_end: Point, tolerance: float = POINT_TOLERANCE
) -> bool:
    """Return True if *point* lies on the interior of the segment.

    "Interior" excludes the two endpoints (within *tolerance*): a point that
    only coincides with an endpoint is handled by endpoint Union-Find, not
    here.  Uses perpendicular distance to the infinite line plus a
    parametric bounds check, so it works for a point anywhere along the
    segment body regardless of orientation.
    """
    px, py = point
    ax, ay = seg_start
    bx, by = seg_end

    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy

    if seg_len_sq <= tolerance * tolerance:
        # Degenerate (zero-length) segment — nothing to be interior to.
        return False

    # Reject points coincident with either endpoint (not "interior").
    if _points_equal(point, seg_start, tolerance) or _points_equal(point, seg_end, tolerance):
        return False

    # Parametric projection of point onto the infinite line through A-B.
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
    seg_len = seg_len_sq**0.5
    eps_t = tolerance / seg_len
    if t <= eps_t or t >= 1.0 - eps_t:
        return False

    # Perpendicular distance from the point to the segment's line.
    cx = ax + t * dx
    cy = ay + t * dy
    dist_sq = (px - cx) ** 2 + (py - cy) ** 2
    return dist_sq <= tolerance * tolerance


def _collinear_overlap(
    a_start: Point,
    a_end: Point,
    b_start: Point,
    b_end: Point,
    tolerance: float = POINT_TOLERANCE,
) -> bool:
    """Return True if segments A and B collinearly overlap on a sub-segment.

    Both segments must lie on the same infinite line (both of B's endpoints
    within *tolerance* of A's line) AND their projected parametric ranges on
    A's line must overlap by more than a single point (nonzero-length shared
    sub-segment).  Handles A⊇B, B⊇A, and partial overlap where neither
    contains the other.  A pure endpoint touch (zero-length overlap) returns
    False — that is the shared-endpoint case handled elsewhere.
    """
    ax, ay = a_start
    bx, by = a_end
    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy

    if seg_len_sq <= tolerance * tolerance:
        return False

    seg_len = seg_len_sq**0.5

    # Both of B's endpoints must lie on A's infinite line.
    for px, py in (b_start, b_end):
        cross = abs((px - ax) * dy - (py - ay) * dx)
        if cross / seg_len > tolerance:
            return False

    # Project B's endpoints onto A's parameterised line.
    t_vals: list[float] = []
    for px, py in (b_start, b_end):
        t_vals.append(((px - ax) * dx + (py - ay) * dy) / seg_len_sq)

    b_min, b_max = min(t_vals), max(t_vals)

    # Intersect [0, 1] (A's range) with [b_min, b_max] (B's range).
    lo = max(0.0, b_min)
    hi = min(1.0, b_max)

    # Require a shared sub-segment of nonzero length, not just a touch.
    eps_t = tolerance / seg_len
    return bool((hi - lo) > eps_t)


def wire_segments_connect(
    a_start: Point,
    a_end: Point,
    b_start: Point,
    b_end: Point,
    tolerance: float = POINT_TOLERANCE,
) -> bool:
    """Return True if two wire segments should union (KiCad wire semantics).

    Reports collinear overlap (nonzero-length shared sub-segment) or a
    mid-segment T-touch (one segment's endpoint on the other's interior).
    A pure shared endpoint returns False — that connection is already made
    by endpoint Union-Find in the connectivity builders, so reporting it
    here would be redundant.
    """
    # Collinear overlap (covers containment and partial overlap).
    if _collinear_overlap(a_start, a_end, b_start, b_end, tolerance):
        return True

    # Mid-segment T-touch: an endpoint of one wire on the other's interior.
    if _point_on_segment_interior(b_start, a_start, a_end, tolerance):
        return True
    if _point_on_segment_interior(b_end, a_start, a_end, tolerance):
        return True
    if _point_on_segment_interior(a_start, b_start, b_end, tolerance):
        return True
    if _point_on_segment_interior(a_end, b_start, b_end, tolerance):
        return True

    return False
