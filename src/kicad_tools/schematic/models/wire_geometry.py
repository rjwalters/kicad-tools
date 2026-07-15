"""Wire-vs-wire geometric union primitives for schematic connectivity.

KiCad unions the nets of two touching or overlapping collinear wire segments
**only where a junction dot is present** — not on pure geometry.  A generator
using the stub-wire+label idiom can legitimately place two wires so their
bodies graze (a mid-segment T-touch) or share a collinear sub-segment without
any junction dot there; KiCad renders these as *crossing, unconnected* wires
and keeps their nets separate.  KiCad only merges the nets at a touch/overlap
point when a junction dot sits at that point.

Historically the connectivity model unioned wires only at their two exact
endpoints, so a generator that placed a *dotted* mid-segment overlap that
KiCad merges was kept separate — a silent, LVS-grade net-merge divergence
(issue #4143).  #4157 then unioned on *pure geometry* (any collinear overlap
or mid-segment T-touch), which over-corrected: it merged incidental,
dot-less grazes that KiCad does NOT union, collapsing distinct nets into one
(issue #4226 — board-05's stub-label schematic reported 16 false copper-LVS
shorts, ``+24V`` carrying 85/205 pins).

This module provides the single, tested geometric primitive
(:func:`wire_segments_connect`) used by both
``SchematicNetlistMixin._build_connectivity_graph`` and
``SchematicValidationMixin.validate_power_nets`` so the two connectivity
builders cannot drift apart.

Two axis-aligned wire segments A and B are considered electrically
connected (beyond a shared endpoint) when either:

* **Collinear overlap** — A and B lie on the same infinite line and their
  projected parametric ranges share a sub-segment of nonzero length, AND a
  junction dot lies *within that shared sub-segment* (issue #4226).  This
  covers A containing B, B containing A, and partial overlap where neither
  contains the other (the softstart rev-B repro geometry — which has a
  junction dot at the intended join).
* **Mid-segment T-touch** — one endpoint of A lands in the *interior* of B
  (not at B's endpoints), or vice-versa, AND a junction dot lies *at that
  touch point* (issue #4226).

A shared endpoint alone is NOT reported here: that case is already handled
by the endpoint Union-Find in the connectivity builders — endpoints connect
natively in KiCad, no junction dot required.  This mirrors, but is
deliberately distinct from, the pin-endpoint-only semantics of #4020/#4003 —
*pins* attach to wires only at endpoints/junctions, while *wires* union with
each other on a *dotted* overlap/T-touch, matching KiCad.

Junction-gating semantics
--------------------------
:func:`wire_segments_connect` accepts an optional ``junction_points`` set:

* When ``junction_points is None`` (the default), the predicate is **pure
  geometry** — it returns True for any collinear overlap or T-touch
  regardless of junction dots.  This preserves the ungated behaviour used by
  ``_check_collinear_net_conflicts`` (issue #4226): a differently-labelled
  dot-less graze is still a *suspicious* geometry worth flagging as a lint
  finding even though KiCad would not merge it.
* When ``junction_points`` is provided, the predicate is **junction-gated**:
  a T-touch or collinear overlap unions only if a junction dot is present at
  the qualifying point (the touch coordinate for a T-touch; anywhere inside
  the shared sub-segment for a collinear overlap).  This matches KiCad's real
  connectivity and is what the connectivity builders pass.
"""

from __future__ import annotations

from collections.abc import Iterable

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


def _junction_at(
    point: Point,
    junction_points: Iterable[Point],
    tolerance: float = POINT_TOLERANCE,
) -> bool:
    """Return True if a junction dot coincides with *point* within *tolerance*."""
    return any(_points_equal(point, jp, tolerance) for jp in junction_points)


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


def _collinear_overlap_range(
    a_start: Point,
    a_end: Point,
    b_start: Point,
    b_end: Point,
    tolerance: float = POINT_TOLERANCE,
) -> tuple[float, float] | None:
    """Return the shared collinear-overlap parameter range on A's line.

    If segments A and B lie on the same infinite line and their projected
    parametric ranges (on A's ``[0, 1]`` parameterisation) share a
    sub-segment of nonzero length, return that ``(lo, hi)`` range; otherwise
    return ``None``.  Handles A⊇B, B⊇A, and partial overlap where neither
    contains the other.  A pure endpoint touch (zero-length overlap) returns
    ``None`` — that is the shared-endpoint case handled elsewhere.
    """
    ax, ay = a_start
    bx, by = a_end
    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy

    if seg_len_sq <= tolerance * tolerance:
        return None

    seg_len = seg_len_sq**0.5

    # Both of B's endpoints must lie on A's infinite line.
    for px, py in (b_start, b_end):
        cross = abs((px - ax) * dy - (py - ay) * dx)
        if cross / seg_len > tolerance:
            return None

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
    if (hi - lo) > eps_t:
        return (lo, hi)
    return None


def _collinear_overlap(
    a_start: Point,
    a_end: Point,
    b_start: Point,
    b_end: Point,
    tolerance: float = POINT_TOLERANCE,
) -> bool:
    """Return True if segments A and B collinearly overlap on a sub-segment.

    Thin boolean wrapper over :func:`_collinear_overlap_range` (kept for the
    ungated call sites and existing unit coverage).
    """
    return _collinear_overlap_range(a_start, a_end, b_start, b_end, tolerance) is not None


def _junction_in_overlap_range(
    a_start: Point,
    a_end: Point,
    overlap_range: tuple[float, float],
    junction_points: Iterable[Point],
    tolerance: float = POINT_TOLERANCE,
) -> bool:
    """Return True if a junction dot projects inside the overlap sub-segment.

    A junction qualifies only when it lies on A's line (within *tolerance*)
    *and* its projected parameter ``t`` falls inside the shared overlap range
    ``[lo, hi]`` (with a small ``eps_t`` slack).  A junction sitting merely at
    a wire's own far endpoint (outside ``[lo, hi]``) does NOT qualify — this
    is the critical refinement from issue #4226: every rail-drop stub has some
    junction at its own far end, which is unrelated to whether the *overlap
    itself* was an intentional connection.
    """
    ax, ay = a_start
    bx, by = a_end
    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq <= tolerance * tolerance:
        return False

    seg_len = seg_len_sq**0.5
    lo, hi = overlap_range
    eps_t = tolerance / seg_len

    for jx, jy in junction_points:
        # Junction must lie on A's infinite line.
        cross = abs((jx - ax) * dy - (jy - ay) * dx)
        if cross / seg_len > tolerance:
            continue
        t = ((jx - ax) * dx + (jy - ay) * dy) / seg_len_sq
        if (lo - eps_t) <= t <= (hi + eps_t):
            return True
    return False


def wire_segments_connect(
    a_start: Point,
    a_end: Point,
    b_start: Point,
    b_end: Point,
    tolerance: float = POINT_TOLERANCE,
    junction_points: Iterable[Point] | None = None,
) -> bool:
    """Return True if two wire segments should union (KiCad wire semantics).

    Reports collinear overlap (nonzero-length shared sub-segment) or a
    mid-segment T-touch (one segment's endpoint on the other's interior).
    A pure shared endpoint returns False — that connection is already made
    by endpoint Union-Find in the connectivity builders, so reporting it
    here would be redundant.

    ``junction_points`` gates the union to match KiCad (issue #4226):

    * ``None`` (default): **pure-geometry** predicate — any collinear overlap
      or T-touch returns True regardless of junction dots.  Used by
      ``_check_collinear_net_conflicts`` to still *flag* a dot-less graze
      between differently-labelled nets as a suspicious geometry.
    * a set/iterable of junction ``(x, y)`` points: **junction-gated**
      predicate — a T-touch unions only if a junction dot sits at the touch
      point; a collinear overlap unions only if a junction dot projects
      *inside* the shared sub-segment.  This matches KiCad's real
      connectivity (a wire-to-wire merge in KiCad requires a junction dot)
      and is what the connectivity builders pass.
    """
    juncs = None if junction_points is None else list(junction_points)

    # Collinear overlap (covers containment and partial overlap).
    overlap_range = _collinear_overlap_range(a_start, a_end, b_start, b_end, tolerance)
    if overlap_range is not None:
        if juncs is None:
            return True
        if _junction_in_overlap_range(a_start, a_end, overlap_range, juncs, tolerance):
            return True
        # Collinear overlap without a qualifying junction dot: KiCad does not
        # merge these (issue #4226).  Fall through — a T-touch check below
        # cannot also apply to a genuinely collinear pair, so this returns
        # False for the gated case.
        return False

    # Mid-segment T-touch: an endpoint of one wire on the other's interior.
    # The touch point is the endpoint that lands in the interior.
    touch_candidates: list[Point] = []
    if _point_on_segment_interior(b_start, a_start, a_end, tolerance):
        touch_candidates.append(b_start)
    if _point_on_segment_interior(b_end, a_start, a_end, tolerance):
        touch_candidates.append(b_end)
    if _point_on_segment_interior(a_start, b_start, b_end, tolerance):
        touch_candidates.append(a_start)
    if _point_on_segment_interior(a_end, b_start, b_end, tolerance):
        touch_candidates.append(a_end)

    if not touch_candidates:
        return False

    if juncs is None:
        return True

    # Junction-gated: union only if a junction dot sits at the touch point.
    return any(_junction_at(pt, juncs, tolerance) for pt in touch_candidates)
