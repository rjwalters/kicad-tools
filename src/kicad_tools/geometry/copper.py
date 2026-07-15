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
