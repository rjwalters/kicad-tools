"""Small 2-D geometry primitives for the mesh router (issue #4268).

Kept dependency-free (pure ``math``) and side-effect-free so the navmesh /
funnel / octilinear-fit stages can each be unit-tested in isolation.  All
coordinates are ``(x, y)`` float tuples in board millimetres.
"""

from __future__ import annotations

import math

Pt = tuple[float, float]

# Distances below this (mm) are treated as coincident. Board features live at
# the ~0.1 mm scale; 1e-9 mm is far below any manufacturable tolerance.
EPS = 1e-9


def tri_area2(a: Pt, b: Pt, c: Pt) -> float:
    """Twice the signed area of triangle ``(a, b, c)``.

    Positive when ``a, b, c`` wind counter-clockwise.  This is the sign
    primitive the funnel string-pull turns on.
    """
    return (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])


def dist(a: Pt, b: Pt) -> float:
    """Euclidean distance between two points."""
    return math.hypot(b[0] - a[0], b[1] - a[1])


def point_equal(a: Pt, b: Pt, eps: float = EPS) -> bool:
    """True if two points coincide within ``eps``."""
    return abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps


def centroid(a: Pt, b: Pt, c: Pt) -> Pt:
    """Centroid of a triangle."""
    return ((a[0] + b[0] + c[0]) / 3.0, (a[1] + b[1] + c[1]) / 3.0)


def point_in_triangle(p: Pt, a: Pt, b: Pt, c: Pt) -> bool:
    """True if ``p`` lies inside or on the boundary of triangle ``(a, b, c)``.

    Winding-agnostic: accepts either orientation.
    """
    d1 = tri_area2(p, a, b)
    d2 = tri_area2(p, b, c)
    d3 = tri_area2(p, c, a)
    has_neg = (d1 < -EPS) or (d2 < -EPS) or (d3 < -EPS)
    has_pos = (d1 > EPS) or (d2 > EPS) or (d3 > EPS)
    return not (has_neg and has_pos)


def point_in_polygon(p: Pt, poly: list[Pt]) -> bool:
    """Ray-cast point-in-polygon test (boundary counts as inside).

    ``poly`` is a closed ring given as an ordered vertex list WITHOUT a
    repeated final vertex.
    """
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        # On-edge check: treat boundary as inside.
        if _point_on_segment(p, poly[i], poly[j]):
            return True
        intersects = ((yi > p[1]) != (yj > p[1])) and (
            p[0] < (xj - xi) * (p[1] - yi) / (yj - yi + 0.0) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def _point_on_segment(p: Pt, a: Pt, b: Pt, eps: float = 1e-7) -> bool:
    """True if ``p`` lies on segment ``a-b`` within ``eps``."""
    cross = tri_area2(a, b, p)
    if abs(cross) > eps * max(1.0, dist(a, b)):
        return False
    # Within the bounding box of the segment (with slack).
    return (
        min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
        and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps
    )


def _orient(a: Pt, b: Pt, c: Pt) -> int:
    v = tri_area2(a, b, c)
    if v > EPS:
        return 1
    if v < -EPS:
        return -1
    return 0


def segments_intersect(a1: Pt, a2: Pt, b1: Pt, b2: Pt) -> bool:
    """True if segment ``a1-a2`` intersects segment ``b1-b2`` (incl. touching)."""
    o1 = _orient(a1, a2, b1)
    o2 = _orient(a1, a2, b2)
    o3 = _orient(b1, b2, a1)
    o4 = _orient(b1, b2, a2)
    if o1 != o2 and o3 != o4:
        return True
    # Collinear-overlap cases.
    if o1 == 0 and _point_on_segment(b1, a1, a2):
        return True
    if o2 == 0 and _point_on_segment(b2, a1, a2):
        return True
    if o3 == 0 and _point_on_segment(a1, b1, b2):
        return True
    if o4 == 0 and _point_on_segment(a2, b1, b2):
        return True
    return False


def segment_intersects_polygon(p1: Pt, p2: Pt, poly: list[Pt]) -> bool:
    """True if segment ``p1-p2`` intersects (or lies within) a simple polygon.

    Used as the clearance predicate against pour keep-outs (issue #4269): a
    trace leg that enters a filled copper pour is a clearance violation, the
    polygon analogue of :func:`segment_intersects_rect`.  ``poly`` is a closed
    ring given WITHOUT a repeated final vertex.  Any endpoint inside the pour,
    or any leg that crosses a pour boundary edge, counts as a hit.
    """
    n = len(poly)
    if n < 3:
        return False
    # Either endpoint inside the pour (boundary counts as inside).
    if point_in_polygon(p1, poly) or point_in_polygon(p2, poly):
        return True
    # Otherwise the leg must cross a boundary edge to enter/exit.
    return any(segments_intersect(p1, p2, poly[i], poly[(i + 1) % n]) for i in range(n))


def _seg_crossing_param(a: Pt, b: Pt, c: Pt, d: Pt) -> float | None:
    """Parametric ``t`` in ``[0, 1]`` along ``a->b`` where it crosses ``c->d``.

    Returns ``None`` when the two segments are parallel or do not cross within
    both spans.  Used by :func:`segment_polygon_interval` to clip a portal edge
    against a committed-copper capsule (issue #4274 in-corridor lane assignment).
    """
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    denom = rx * sy - ry * sx
    if abs(denom) < 1e-15:
        return None
    qx, qy = c[0] - a[0], c[1] - a[1]
    t = (qx * sy - qy * sx) / denom
    u = (qx * ry - qy * rx) / denom
    if -1e-12 <= t <= 1.0 + 1e-12 and -1e-12 <= u <= 1.0 + 1e-12:
        return min(1.0, max(0.0, t))
    return None


def segment_polygon_interval(a: Pt, b: Pt, poly: list[Pt]) -> tuple[float, float] | None:
    """Parametric ``[t0, t1]`` of the part of segment ``a->b`` inside ``poly``.

    Winding-agnostic; ``poly`` is a closed convex ring (the committed-copper
    capsules are convex quads) given WITHOUT a repeated final vertex.  Returns
    ``None`` when the segment never enters the polygon.  For a convex polygon the
    interior span of a segment is contiguous, so a single ``(t0, t1)`` describes
    the consumed opening a committed trace carves out of a portal edge.
    """
    n = len(poly)
    if n < 3:
        return None
    cuts = {0.0, 1.0}
    for i in range(n):
        t = _seg_crossing_param(a, b, poly[i], poly[(i + 1) % n])
        if t is not None:
            cuts.add(t)
    ts = sorted(cuts)
    lo: float | None = None
    hi = 0.0
    for j in range(len(ts) - 1):
        tm = 0.5 * (ts[j] + ts[j + 1])
        pm = (a[0] + tm * (b[0] - a[0]), a[1] + tm * (b[1] - a[1]))
        if point_in_polygon(pm, poly):
            if lo is None:
                lo = ts[j]
            hi = ts[j + 1]
    if lo is None:
        return None
    return (lo, hi)


def merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Union a set of ``[t0, t1]`` intervals into disjoint, sorted spans."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    out: list[list[float]] = [list(ordered[0])]
    for lo, hi in ordered[1:]:
        if lo <= out[-1][1] + 1e-9:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(lo, hi) for lo, hi in out]


def segment_intersects_rect(
    p1: Pt, p2: Pt, xmin: float, ymin: float, xmax: float, ymax: float
) -> bool:
    """True if segment ``p1-p2`` intersects (or lies within) an AABB.

    Used as the clearance predicate against inflated pad keep-out
    rectangles: a segment leg that enters an inflated keep-out is a
    clearance violation (the #3906 discipline applied per leg).
    """
    # Either endpoint inside the box.
    if xmin <= p1[0] <= xmax and ymin <= p1[1] <= ymax:
        return True
    if xmin <= p2[0] <= xmax and ymin <= p2[1] <= ymax:
        return True
    # Otherwise test against the four box edges.
    corners = [
        (xmin, ymin),
        (xmax, ymin),
        (xmax, ymax),
        (xmin, ymax),
    ]
    return any(segments_intersect(p1, p2, corners[i], corners[(i + 1) % 4]) for i in range(4))
