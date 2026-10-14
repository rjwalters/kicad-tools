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

It also owns the single pad-outline primitive, :func:`pad_copper_outline`
(Issues #6452, #6453): the exact rect / roundrect / circle / oval copper of a
pad, never eroded, with contact decided by a positive intersection area
(:data:`CONTACT_AREA_EPSILON`).  Every connectivity model builds its pad
geometry from it via :mod:`kicad_tools.validate.copper_contact`.

``shapely`` is a core dependency (see :mod:`kicad_tools._shapely`); these
helpers return ``None`` when it is unavailable so callers on a real-geometry
path can fail loud rather than silently degrade.
"""

from __future__ import annotations

import math
from typing import Any

from kicad_tools._shapely import has_shapely as _has_shapely

if _has_shapely():  # pragma: no cover - import guard exercised by environment
    from shapely.geometry import LineString as _ShapelyLineString  # type: ignore[import-untyped]
    from shapely.geometry import Point as _ShapelyPoint
    from shapely.geometry import Polygon as _ShapelyPolygon


# Minimum intersection area (mm^2) that counts as copper contact between a
# pad outline and other copper (fill, pad, via annulus).  Far below any real
# thermal spoke or solid tie (a 0.05 mm deep contact on a 0.3 mm pad is
# ~0.015 mm^2) and far above floating-point noise on a shared edge, so a
# tangent outline is never contact and any real overlap always is
# (Issues #6452, #6453).
CONTACT_AREA_EPSILON: float = 1e-6

# Inward erosion (mm) for pad shapes this module does NOT model exactly
# (``trapezoid``, chamfered ``roundrect``, ``custom`` primitives).  Their
# size box over-reaches the real copper at the corners, so the box is shrunk
# by this margin to stay a conservative under-approximation: such a pad can
# only bond *less* readily than its real copper, never across a clearance
# moat.  Exact outlines (rect / roundrect / circle / oval) need no erosion --
# the shrink existed only to cancel the box's corner over-reach, and applying
# it to real copper hid shallow thermal-spoke contact (Issue #6453).
PAD_FALLBACK_ERODE: float = 0.1

# Pad shapes whose copper outline :func:`pad_copper_outline` builds exactly.
EXACT_PAD_SHAPES: frozenset[str] = frozenset({"rect", "roundrect", "circle", "oval", "obround"})


def _pad_has_chamfer(pad: Any) -> bool:
    """True when a ``roundrect`` pad carries a non-zero chamfer.

    The schema does not model chamfers, so look at the raw ``(pad ...)``
    S-expression when it is linked: ``(chamfer_ratio R)`` with ``R > 0`` and a
    ``(chamfer ...)`` corner list means some corners are cut, not rounded.
    """
    node = getattr(pad, "_sexp_node", None)
    if node is None:
        return False
    ratio = node.find("chamfer_ratio")
    corners = node.find("chamfer")
    if ratio is None or corners is None:
        return False
    value = ratio.get_float(0)
    return bool(value) and value > 0 and len(getattr(corners, "children", []) or []) > 1


def pad_outline_is_exact(pad: Any) -> bool:
    """Whether :func:`pad_copper_outline` models ``pad``'s copper exactly."""
    shape = (getattr(pad, "shape", "") or "").lower()
    if shape not in EXACT_PAD_SHAPES:
        return False
    return not (shape == "roundrect" and _pad_has_chamfer(pad))


def pad_board_center(
    pad: Any,
    fp_position: tuple[float, float],
    fp_rotation: float,
) -> tuple[float, float]:
    """Board-frame position of a pad's anchor (its ``(at x y)``).

    Pad coordinates are footprint-local; they rotate with the footprint
    (KiCad's negated-angle convention, see
    :func:`kicad_tools.core.geometry.rotate_pad_offset`).
    """
    from kicad_tools.core.geometry import rotate_pad_offset

    px, py = pad.position
    rx, ry = rotate_pad_offset(px, py, fp_rotation)
    return (fp_position[0] + rx, fp_position[1] + ry)


def pad_size_box(
    pad: Any,
    fp_position: tuple[float, float],
    fp_rotation: float,
    erode: float = 0.0,
) -> Any | None:
    """A pad's sharp ``size`` box in the board frame, optionally eroded.

    The pre-#6453 pad approximation: correct only for ``rect`` pads, and an
    over-reach at the corners of every rounded shape.  Kept for shapes
    :func:`pad_copper_outline` does not model exactly and for legacy
    (``strict=False``) net-status, whose historical behaviour is pinned.
    Erosion that would empty the box returns the un-eroded box.
    """
    if not _has_shapely():
        return None
    cx, cy = pad_board_center(pad, fp_position, fp_rotation)
    w, h = pad.size
    if w <= 0 or h <= 0:
        return _ShapelyPoint((cx, cy))
    angle = math.radians(-(getattr(pad, "rotation", 0.0) or 0.0))
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    box = _ShapelyPolygon(
        [(cx + x * cos_a - y * sin_a, cy + x * sin_a + y * cos_a) for x, y in corners]
    )
    if erode > 0:
        eroded = box.buffer(-erode)
        if not eroded.is_empty:
            return eroded
    return box


def pad_copper_outline(
    pad: Any,
    fp_position: tuple[float, float],
    fp_rotation: float,
) -> Any | None:
    """Board-frame shapely geometry of a pad's copper (Issues #6452, #6453).

    The single pad-outline primitive every connectivity model shares:

    * ``rect`` -> the exact rectangle;
    * ``roundrect`` -> rectangle with corner radius
      ``roundrect_rratio * min(w, h)`` (KiCad's default ratio 0.25);
    * ``circle`` -> disk of diameter ``min(w, h)``;
    * ``oval`` / ``obround`` -> stadium (a circle when ``w == h``).

    The outline is oriented by the pad's **absolute** angle (``pad.rotation``
    already includes the footprint rotation; KiCad's negated-angle
    convention, Issue #6460), centred on the pad anchor plus its rotated
    ``(offset ...)``, and is **not** eroded: contact is decided by a positive
    intersection area (:data:`CONTACT_AREA_EPSILON`) against the other
    copper, exactly as KiCad collides a pad's effective shape with a zone
    fill.  With the true rounded corners there is no over-reach to cancel --
    the old 0.1 mm erosion of a sharp box both hid real shallow contact
    (#6453) and was load-bearing only because the box's corners grazed
    foreign pours.

    Shapes not modelled exactly (``trapezoid``, chamfered ``roundrect``,
    ``custom``) fall back to the size box eroded by
    :data:`PAD_FALLBACK_ERODE` (un-eroded if the erosion empties it), which
    can only under-report contact.  A pad with a non-positive size is its
    bare anchor point.  Returns ``None`` when shapely is unavailable.
    """
    if not _has_shapely():
        return None
    cx, cy = pad_board_center(pad, fp_position, fp_rotation)
    w, h = pad.size
    if w <= 0 or h <= 0:
        return _ShapelyPoint((cx, cy))
    angle = math.radians(-(getattr(pad, "rotation", 0.0) or 0.0))
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    ox, oy = getattr(pad, "drill_offset", (0.0, 0.0)) or (0.0, 0.0)

    def _to_board(lx: float, ly: float) -> tuple[float, float]:
        lx += ox
        ly += oy
        return (cx + lx * cos_a - ly * sin_a, cy + lx * sin_a + ly * cos_a)

    shape = (getattr(pad, "shape", "") or "").lower()
    exact = pad_outline_is_exact(pad)
    radius = 0.0
    if exact and shape in ("circle", "oval", "obround"):
        radius = min(w, h) / 2.0
    elif exact and shape == "roundrect":
        ratio = getattr(pad, "roundrect_rratio", 0.25)
        ratio = 0.25 if ratio is None else max(0.0, min(float(ratio), 0.5))
        radius = ratio * min(w, h)
    if exact and shape == "circle":
        core: Any = _ShapelyPoint(_to_board(0.0, 0.0))
    elif radius > 0.0:
        hx, hy = w / 2.0 - radius, h / 2.0 - radius
        corners = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
        if hx <= 1e-12 and hy <= 1e-12:
            core = _ShapelyPoint(_to_board(0.0, 0.0))
        elif hx <= 1e-12 or hy <= 1e-12:
            ends = [(-hx, -hy), (hx, hy)]
            core = _ShapelyLineString([_to_board(*e) for e in ends])
        else:
            core = _ShapelyPolygon([_to_board(*c) for c in corners])
    else:
        corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
        core = _ShapelyPolygon([_to_board(*c) for c in corners])
    if radius > 0.0:
        return core.buffer(radius, quad_segs=32)
    if not exact and PAD_FALLBACK_ERODE > 0:
        eroded = core.buffer(-PAD_FALLBACK_ERODE, join_style="mitre")
        if not eroded.is_empty:
            return eroded
    return core


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
