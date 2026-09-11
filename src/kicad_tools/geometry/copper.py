"""Shared, checker-agnostic trace-copper geometry (Issue #4176).

This module is the single source of truth for "what copper does a PCB trace
segment actually occupy" — the real shapely polygon of a segment's copper,
built as the segment centerline (``LineString``) buffered outward by half its
``width``.  It mirrors the courtyard-polygon precedent (#4182,
:mod:`kicad_tools.geometry.courtyard`) that swapped a bbox/tolerance
approximation for real shapely polygon intersection.

Why this exists
---------------

``NetStatusAnalyzer`` (and ``kct check``'s connectivity rule, which defers to
it) historically unioned trace copper on *endpoint proximity* — a
``POSITION_TOLERANCE = 0.01`` mm Euclidean radius between segment endpoints,
pad centers, and via centers — never testing whether the copper *shapes*
actually touch.  A segment endpoint landing near-but-not-on a pad's copper
therefore blessed an electrically-open net (kct reported "complete" while
KiCad's ``pcb drc`` reported the pad unconnected; see Issue #4176).

KiCad's connectivity engine bonds two conductors only when their copper
geometry physically overlaps or touches.  The pad/via/zone copper polygons
already exist in :mod:`kicad_tools.validate.connectivity`
(``_pad_copper_polygon`` / ``_fill_solid_region`` / ``_via_copper_geom``); the
only missing primitive was the trace-segment polygon this module provides.

``shapely`` is a core dependency (see :mod:`kicad_tools._shapely`); this
helper returns ``None`` when it is unavailable so callers on a real-geometry
path can fail loud rather than silently degrade.
"""

from __future__ import annotations

import math
from typing import Any

from kicad_tools._shapely import has_shapely as _has_shapely

if _has_shapely():  # pragma: no cover - import guard exercised by environment
    from shapely.geometry import LineString as _ShapelyLineString  # type: ignore[import-untyped]
    from shapely.geometry import Point as _ShapelyPoint


def segment_copper_polygon(
    start: tuple[float, float],
    end: tuple[float, float],
    width: float,
) -> Any | None:
    """Build a board-frame shapely polygon approximating a trace's copper.

    A KiCad trace segment is a rectangle of length ``|end - start|`` and
    thickness ``width`` with semicircular end caps (round line caps).  That is
    exactly ``LineString([start, end]).buffer(width / 2)``, matching the
    ``buffer()`` idiom :meth:`ConnectivityValidator._via_copper_geom` already
    uses for via circles.

    Two trace copper polygons are electrically bonded iff they
    ``intersects()`` — including the near-miss case Issue #4176 reports, where
    an endpoint sits just outside neighboring copper.

    Degenerate inputs are handled defensively (mirroring the ``w <= 0 or
    h <= 0`` guard in :meth:`ConnectivityValidator._pad_copper_polygon`):

    * ``width <= 0`` — a zero/negative-width segment has no copper thickness;
      return the bare centerline geometry (a ``LineString``, or a ``Point``
      when the segment is also zero-length) so an exact endpoint coincidence
      still registers as contact without manufacturing spurious area.
    * ``start == end`` (zero length) with positive width — return the round
      copper pad (a disk of radius ``width / 2``) the trace's end cap would
      occupy.

    Returns ``None`` when shapely is unavailable.
    """
    if not _has_shapely():
        return None

    if start == end:
        point = _ShapelyPoint(start)
        if width <= 0:
            return point
        return point.buffer(width / 2)

    line = _ShapelyLineString([start, end])
    if width <= 0:
        return line
    return line.buffer(width / 2)


def point_segment_distance(
    point: tuple[float, float],
    seg_start: tuple[float, float],
    seg_end: tuple[float, float],
) -> float:
    """Shortest distance from ``point`` to the closed segment ``start-end``."""
    px, py = point
    ax, ay = seg_start
    bx, by = seg_end
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _orientation(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> float:
    """Signed area of the triangle ``p, q, r`` (positive = counter-clockwise)."""
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def segment_centerline_distance(
    a_start: tuple[float, float],
    a_end: tuple[float, float],
    b_start: tuple[float, float],
    b_end: tuple[float, float],
) -> float:
    """Exact minimum distance between two closed 2-D segments.

    Segments that cross (or touch) return ``0.0``; otherwise the minimum is
    always attained at one of the four endpoint-to-segment distances, which
    is the standard closed-form result for convex sets.

    This is the analytic equivalent of
    ``LineString(a).distance(LineString(b))``, evaluated without constructing
    shapely objects — the connectivity extractor calls it on every segment
    pair, and it is also exact for the degenerate zero-length case (where a
    "segment" is a point).
    """
    # A *proper* crossing (each segment strictly straddles the other's
    # supporting line) is the one configuration whose minimum distance is
    # NOT attained at an endpoint, so it needs its own test.  Every
    # improper contact — touching, T-shaped, collinear overlap — puts an
    # endpoint of one segment on the other, which the endpoint minimum
    # below already reports as 0.
    o1 = _orientation(a_start, a_end, b_start)
    o2 = _orientation(a_start, a_end, b_end)
    o3 = _orientation(b_start, b_end, a_start)
    o4 = _orientation(b_start, b_end, a_end)
    if o1 * o2 < 0 and o3 * o4 < 0:
        return 0.0
    return min(
        point_segment_distance(b_start, a_start, a_end),
        point_segment_distance(b_end, a_start, a_end),
        point_segment_distance(a_start, b_start, b_end),
        point_segment_distance(a_end, b_start, b_end),
    )


def segments_copper_touch(
    a_start: tuple[float, float],
    a_end: tuple[float, float],
    a_width: float,
    b_start: tuple[float, float],
    b_end: tuple[float, float],
    b_width: float,
) -> bool:
    """True iff two same-layer trace segments' swept copper touches.

    A trace's copper is its centerline buffered by ``width / 2`` (see
    :func:`segment_copper_polygon`), i.e. a capsule.  Two capsules intersect
    **iff** the distance between their centerlines is at most the sum of
    their radii — so this is exactly
    ``segment_copper_polygon(a...).intersects(segment_copper_polygon(b...))``
    but exact (shapely's ``buffer`` approximates the round caps with a
    finite-resolution polygon, which *under*-covers the true copper) and far
    cheaper.

    Two consequences matter for connectivity (issue #5060):

    * contact is decided over the **full** swept copper, so a T-junction
      landing on a track's interior — or a side-on width overlap with no
      shared endpoint at all — is contact, exactly as KiCad's own
      connectivity engine sees it; and
    * the predicate is **invariant** under splitting a segment into
      collinear subsegments with the same copper union: the centerline
      distance minimum is attained at some point of the centerline, which
      lies on one of the subsegments, and no subsegment centerline reaches
      anywhere the whole centerline did not.

    Widths are clamped at zero, so a width-less segment contributes only its
    bare centerline (never negative reach).
    """
    reach = max(a_width or 0.0, 0.0) / 2.0 + max(b_width or 0.0, 0.0) / 2.0
    # Cheap axis-aligned reject before the exact test: the connectivity
    # extractor evaluates this over every segment pair on the board.
    for axis in (0, 1):
        if (
            min(a_start[axis], a_end[axis]) > max(b_start[axis], b_end[axis]) + reach
            or min(b_start[axis], b_end[axis]) > max(a_start[axis], a_end[axis]) + reach
        ):
            return False
    return segment_centerline_distance(a_start, a_end, b_start, b_end) <= reach
