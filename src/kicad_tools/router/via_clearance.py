"""Shared world-coordinate via-clearance predicate (Issue #2944).

This module hosts the canonical "is it safe to place a via at (x, y)?"
geometry check used by:

* :mod:`kicad_tools.cli.stitch_cmd` -- stitching power/ground planes (the
  ``check_via_clearance`` function in that module is now a thin wrapper).
* :class:`kicad_tools.router.escape.EscapeRouter` -- per-pad escape via
  placement and in-pad via rescue.

Historical context: prior to Issue #2944 the stitcher had a precise
world-coord clearance check at ``cli/stitch_cmd.py:check_via_clearance``
while the router's escape via predicates only checked coarse-grid
obstacle cells.  The mismatch let escape-router-placed vias land
fractions of a millimeter from foreign-net pads/traces, producing DRC
violations the stitcher would never have produced.  The fix shape (per
the curator on #2944) is to lift the precise check into a shared helper
and use it from both call sites.

Design notes:

* The helper accepts duck-typed inputs via :class:`TrackSegmentLike` and
  :class:`FilledPolygonLike` protocols so the existing stitcher data
  classes (``stitch_cmd.TrackSegment`` / ``stitch_cmd.FilledPolygon``)
  work without modification, and the router can supply its own
  segment / polygon representations without a cross-module type
  dependency.
* The check is pure geometry on raw coordinates -- no PCB / SExp state
  is required, which keeps it cheap to call from any pipeline stage.
"""

from __future__ import annotations

import math
from typing import Protocol

from kicad_tools.core.geometry import point_to_segment_distance

# ---------------------------------------------------------------------------
# Duck-typed protocols so the helper works with both stitcher and router
# data classes without importing either module's concrete dataclass.
# ---------------------------------------------------------------------------


class TrackSegmentLike(Protocol):
    """Structural type for a track segment used by :func:`point_clear_of_copper`.

    The router's :class:`Segment` and the stitcher's
    :class:`TrackSegment` are both compatible -- the helper only reads
    ``start_x``, ``start_y``, ``end_x``, ``end_y`` and ``width``.
    """

    start_x: float
    start_y: float
    end_x: float
    end_y: float
    width: float


class FilledPolygonLike(Protocol):
    """Structural type for a zone-fill polygon used by clearance checks.

    Matches ``stitch_cmd.FilledPolygon`` and any future router-side polygon
    representation that exposes ``points`` and the bounding box used for
    the fast pre-filter.
    """

    points: list[tuple[float, float]]
    min_x: float
    min_y: float
    max_x: float
    max_y: float


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test.

    A duplicate of :func:`kicad_tools.cli.stitch_cmd.point_in_polygon` kept
    private to this module so the shared helper has no circular
    dependency on the stitch CLI.
    """
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _point_clear_of_filled_polygons(
    px: float,
    py: float,
    radius: float,
    filled_polygons: list[FilledPolygonLike],
    clearance: float,
) -> bool:
    """Return True iff a circular copper object at (px, py) clears all
    filled polygons.

    Args:
        px, py: Center of the circular copper object (e.g. via center).
        radius: Radius of the copper object in mm.
        filled_polygons: Other-net filled polygons to check against.
        clearance: Required clearance from polygon copper.

    Returns:
        True if the object clears every polygon, False on any violation.
    """
    required = radius + clearance
    for fp in filled_polygons:
        # Bounding-box pre-filter
        if (
            px + required < fp.min_x
            or px - required > fp.max_x
            or py + required < fp.min_y
            or py - required > fp.max_y
        ):
            continue

        if _point_in_polygon(px, py, fp.points):
            return False

        n = len(fp.points)
        for i in range(n):
            j = (i + 1) % n
            dist = point_to_segment_distance(
                px, py, fp.points[i][0], fp.points[i][1], fp.points[j][0], fp.points[j][1]
            )
            if dist < required:
                return False

    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


#: Foreign-net pad tuple type.  Either:
#:
#: * 4-tuple ``(x, y, effective_radius, net_num)`` -- legacy "disc-bound"
#:   shape used by the stitcher's pre-#2944 callers.  The effective radius
#:   conservatively encodes pad geometry as ``max(width, height) / 2``.
#: * 5-tuple ``(x, y, width, height, net_num)`` -- rect-aware shape
#:   introduced in Issue #2951.  The helper computes axis-separated
#:   distance to the pad rectangle (closest-point distance), avoiding the
#:   disc-bound over-conservatism for oblong fine-pitch pads (e.g. 0.3 x
#:   1.4mm LQFP fingers at 0.5mm pitch where the disc bound of 0.7mm
#:   produces a 1.05mm minimum centre-to-centre vs the actual 0.35mm
#:   rect-distance requirement).
ForeignPadTuple = (
    tuple[float, float, float, int]  # (x, y, radius, net)
    | tuple[float, float, float, float, int]  # (x, y, width, height, net)
)


def point_clear_of_copper(
    x: float,
    y: float,
    via_size: float,
    clearance: float,
    other_net_tracks: list[TrackSegmentLike] | None = None,
    other_net_vias: list[tuple[float, float, float, int]] | None = None,
    other_net_pads: list[ForeignPadTuple] | None = None,
    same_net_vias: list[tuple[float, float]] | None = None,
    other_net_filled_polygons: list[FilledPolygonLike] | None = None,
) -> bool:
    """Check if a via at (x, y) clears all surrounding copper.

    This is the canonical world-coordinate via-clearance predicate.
    It checks the proposed via center against:

    * Same-net vias (prevents stacking; threshold = ``via_size + clearance``).
    * Other-net track segments (threshold = ``via_radius + seg.width/2 + clearance``).
    * Other-net vias (threshold = ``via_radius + other_radius + clearance``).
    * Other-net pads (threshold = ``via_radius + pad_radius + clearance`` for
      4-tuple discs, or rect-distance >= ``via_radius + clearance`` for
      5-tuple width/height pads -- see :data:`ForeignPadTuple`).
    * Other-net filled polygons (zone fills).

    Returns True only if every check passes.  Any single violation
    short-circuits with False -- callers that want to know which
    obstacle blocked the placement should use the structured-diagnostic
    paths in :mod:`stitch_cmd` (this predicate is intentionally
    boolean-only for cheap use on hot paths).

    All distances are in millimeters and the inputs are world-coordinate
    floats (NOT grid indices).  This is the key contract the function
    enforces over coarse grid-cell obstacle checks: a via that clears
    every grid cell may still violate copper clearance if the cell
    resolution is coarser than the manufacturer's minimum clearance
    rule.

    Args:
        x: Proposed via X (mm, world coordinates).
        y: Proposed via Y (mm, world coordinates).
        via_size: Via pad diameter in mm.
        clearance: Minimum copper-to-copper clearance in mm
            (e.g. ``manufacturer.min_via_to_track_clearance`` from the
            manufacturer profile).
        other_net_tracks: Foreign-net track segments (any structural
            type with ``start_x/start_y/end_x/end_y/width``).
        other_net_vias: Foreign-net vias as
            ``(x, y, size_mm, net_num)`` tuples.
        other_net_pads: Foreign-net pads.  Each element may be either a
            4-tuple ``(x, y, effective_radius_mm, net_num)`` (legacy
            disc-bound) or a 5-tuple
            ``(x, y, width_mm, height_mm, net_num)`` (rect-aware --
            preferred for oblong fine-pitch pads; see Issue #2951).
            Mixing tuple shapes within the same list is supported.
        same_net_vias: Existing same-net via centers as ``(x, y)``
            tuples used to reject via stacking.
        other_net_filled_polygons: Foreign-net filled polygons from
            zone fills (any structural type with
            ``points/min_x/min_y/max_x/max_y``).

    Returns:
        True if the position is clear for via placement; False if any
        clearance threshold is violated.
    """
    via_radius = via_size / 2

    if same_net_vias:
        for vx, vy in same_net_vias:
            dist = math.sqrt((vx - x) ** 2 + (vy - y) ** 2)
            if dist < via_size + clearance:
                return False

    if other_net_tracks:
        for seg in other_net_tracks:
            dist = point_to_segment_distance(
                x, y, seg.start_x, seg.start_y, seg.end_x, seg.end_y
            )
            min_dist = via_radius + seg.width / 2 + clearance
            if dist < min_dist:
                return False

    if other_net_vias:
        for ovx, ovy, ov_size, _onet in other_net_vias:
            dist = math.sqrt((ovx - x) ** 2 + (ovy - y) ** 2)
            min_dist = via_radius + ov_size / 2 + clearance
            if dist < min_dist:
                return False

    if other_net_pads:
        # Required clearance from any pad edge -- same for disc and rect
        # forms below (the disc form folds the pad's effective radius
        # into the threshold, while the rect form measures distance to
        # the pad's edge directly).
        required_from_edge = via_radius + clearance
        for pad in other_net_pads:
            if len(pad) == 5:
                # Rect-aware: (x, y, width, height, net).
                # Compute axis-separated distance from via center to the
                # axis-aligned pad rectangle -- mirrors
                # ``EscapeRouter._via_clears_other_pads`` (Issue #2946).
                px, py, p_w, p_h, _pnet = pad
                half_w = p_w / 2
                half_h = p_h / 2
                dx_abs = abs(x - px)
                dy_abs = abs(y - py)
                outside_x = max(0.0, dx_abs - half_w)
                outside_y = max(0.0, dy_abs - half_h)
                if outside_x == 0.0 and outside_y == 0.0:
                    # Via center inside the foreign pad rectangle -- an
                    # immediate violation.  (Same-net "via in pad" is
                    # legitimate but must be filtered by the caller; the
                    # helper does not consult the via's own net.)
                    return False
                rect_dist = math.sqrt(outside_x * outside_x + outside_y * outside_y)
                if rect_dist < required_from_edge - 1e-9:
                    return False
            else:
                # Disc-bound legacy: (x, y, radius, net).
                px, py, p_radius, _pnet = pad
                dist = math.sqrt((px - x) ** 2 + (py - y) ** 2)
                if dist < via_radius + p_radius + clearance:
                    return False

    if other_net_filled_polygons:
        if not _point_clear_of_filled_polygons(
            x, y, via_radius, other_net_filled_polygons, clearance
        ):
            return False

    return True


# ---------------------------------------------------------------------------
# Shared segment-vs-foreign-via predicate (Issue #2998 / #3002)
# ---------------------------------------------------------------------------


class _SegmentLike(Protocol):
    """Structural type for a routed segment passed to
    :func:`segment_clears_foreign_via`.

    The router's :class:`Segment` is compatible -- the helper reads the
    centerline endpoints, the per-layer placement, and the trace width.
    Layer is exposed via the ``layer`` attribute (an enum-like with a
    ``value`` int).
    """

    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    # ``layer`` exposes ``.value`` -- intentionally not typed as a
    # concrete enum so test fixtures can mock with simple objects.


class _ViaLike(Protocol):
    """Structural type for a routed via passed to
    :func:`segment_clears_foreign_via`.

    Mirrors :class:`kicad_tools.router.primitives.Via` -- reads
    centre coordinates, diameter, and the two-element ``layers`` range.
    ``layers[0]`` and ``layers[1]`` each expose ``.value`` (an int).
    """

    x: float
    y: float
    diameter: float
    # ``layers`` is a 2-tuple whose elements expose ``.value``.


def segment_clears_foreign_via(
    seg: "_SegmentLike",
    via: "_ViaLike",
    trace_clearance: float,
    hard_intersection_only: bool = False,
) -> bool:
    """Return True iff a segment clears a foreign-net via.

    Issue #2998 / #3002: Shared sibling of :func:`point_clear_of_copper`
    for the segment-vs-via direction.  Where ``point_clear_of_copper``
    protects a NEW via from foreign segments/pads, this predicate
    protects a NEW segment from a foreign-net via.

    Originally introduced as a private static helper inside
    :class:`kicad_tools.router.escape.EscapeRouter` by PR #2999 (the
    escape-commit gate for issue #2998).  Lifted to this module by PR
    for issue #3002 so the main-router commit gate can consume the same
    predicate without importing :mod:`escape`.

    Layer-awareness: the segment occupies one copper layer; the via
    spans a contiguous layer range ``via.layers[0]..[1]``.  A segment
    must clear a via only when the via spans the segment's layer.

    Same-net filtering is the CALLER's responsibility (mirrors the
    ``point_clear_of_copper`` boundary convention; the caller has more
    context to enforce diff-pair / split-net policies).

    Two thresholds (parameterised by ``hard_intersection_only``):

    * STANDARD (``hard_intersection_only=False``)::

        dist(via_center, segment_centerline)
          >= via.diameter/2 + seg.width/2 + trace_clearance

      Full manufacturer clearance.  Rejects both hard intersections
      AND marginal sub-clearance violations.  This is the predicate
      the C++ post-route validator uses at
      ``cpp/src/grid.cpp:510-536`` (block 1c) and the predicate used
      by :meth:`Autorouter._update_router_segment_foreign_context`
      (Issue #3002 main-router gate).

    * HARD-INTERSECTION (``hard_intersection_only=True``)::

        dist(via_center, segment_centerline)
          >= via.diameter/2 + seg.width/2

      Drops the ``trace_clearance`` term: only flags cases where
      copper physically overlaps copper (negative edge-to-edge
      clearance).  Used by ``EscapeRouter.apply_escape_routes`` to drop
      only the unrecoverable-by-routing escapes; preserves the in-pad
      rescue last-resort policy (PR #2945 / Issue #2944) for marginal
      sub-clearance escapes whose alternate path would regress net
      completion (e.g. board-04 NRST cluster).

    Args:
        seg: The candidate segment.  Reads ``x1/y1/x2/y2/width/layer``.
        via: A foreign-net via to validate against.  Reads
            ``x/y/diameter/layers``.
        trace_clearance: Manufacturer minimum copper-to-copper
            clearance in mm.
        hard_intersection_only: When True, ignore ``trace_clearance``
            in the threshold (see HARD-INTERSECTION mode above).

    Returns:
        True if the segment clears the via, False on violation.
    """
    # Layer overlap check: vias span layers[0]..layers[1] inclusive.
    v_lo = min(via.layers[0].value, via.layers[1].value)
    v_hi = max(via.layers[0].value, via.layers[1].value)
    if not (v_lo <= seg.layer.value <= v_hi):
        return True  # Via doesn't reach the segment's layer.

    dist = point_to_segment_distance(
        via.x, via.y, seg.x1, seg.y1, seg.x2, seg.y2
    )
    required = via.diameter / 2 + seg.width / 2
    if not hard_intersection_only:
        required += trace_clearance
    # 1e-9 epsilon mirrors ``point_clear_of_copper``'s convention so a
    # segment exactly at the clearance threshold is admitted.
    return dist >= required - 1e-9


__all__ = [
    "FilledPolygonLike",
    "ForeignPadTuple",
    "TrackSegmentLike",
    "point_clear_of_copper",
    "segment_clears_foreign_via",
]
