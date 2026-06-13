"""Shapely-based board geometry engine.

Provides :class:`BoardGeometry` -- a unified wrapper around Edge.Cuts
board outline data backed by Shapely for geometric operations like
containment checks, clearance buffers, and boolean operations.

Shapely is an *optional* dependency.  When it is not installed the
module still loads but ``BoardGeometry.from_pcb()`` will raise
``ImportError`` with a helpful message.  Call-sites that previously used
AABB-based logic can fall back transparently via :func:`has_shapely`.

Usage::

    from kicad_tools.pcb.board_geometry import BoardGeometry, has_shapely

    if has_shapely():
        geom = BoardGeometry.from_pcb(pcb)
        inside = geom.contains_point(10.0, 15.0)
        clearance_zone = geom.buffer(-0.3)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

# ---------------------------------------------------------------------------
# Optional Shapely import
# ---------------------------------------------------------------------------

_SHAPELY_AVAILABLE = False
try:
    from shapely.geometry import LineString, Point  # noqa: F811
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.ops import polygonize, unary_union

    _SHAPELY_AVAILABLE = True
except ImportError:
    pass


def has_shapely() -> bool:
    """Return True if Shapely is available at runtime."""
    return _SHAPELY_AVAILABLE


# ---------------------------------------------------------------------------
# Arc / curve linearisation helpers
# ---------------------------------------------------------------------------

_DEFAULT_ARC_SEGMENTS = 32


def _linearize_arc(
    sx: float,
    sy: float,
    mx: float,
    my: float,
    ex: float,
    ey: float,
    num_segments: int = _DEFAULT_ARC_SEGMENTS,
) -> list[tuple[float, float]]:
    """Approximate a KiCad three-point arc as a polyline.

    KiCad ``gr_arc`` is specified by *start*, *mid*, and *end* points.
    We recover the circumscribed circle centre and radius, then emit
    evenly-spaced points along the arc.

    Returns a list of (x, y) points *including* start and end.
    """
    # Circumscribed-circle of three points
    ax, ay = sx, sy
    bx, by = mx, my
    cx, cy = ex, ey

    D = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(D) < 1e-12:
        # Degenerate / collinear -- straight line
        return [(sx, sy), (ex, ey)]

    ux = (
        (ax * ax + ay * ay) * (by - cy)
        + (bx * bx + by * by) * (cy - ay)
        + (cx * cx + cy * cy) * (ay - by)
    ) / D
    uy = (
        (ax * ax + ay * ay) * (cx - bx)
        + (bx * bx + by * by) * (ax - cx)
        + (cx * cx + cy * cy) * (bx - ax)
    ) / D

    radius = math.sqrt((sx - ux) ** 2 + (sy - uy) ** 2)

    angle_start = math.atan2(sy - uy, sx - ux)
    angle_mid = math.atan2(my - uy, mx - ux)
    angle_end = math.atan2(ey - uy, ex - ux)

    def _norm(a: float) -> float:
        while a < 0:
            a += 2.0 * math.pi
        while a >= 2.0 * math.pi:
            a -= 2.0 * math.pi
        return a

    a_s = _norm(angle_start)
    a_m = _norm(angle_mid)
    a_e = _norm(angle_end)

    sweep_ccw = _norm(a_e - a_s)
    mid_in_ccw = _norm(a_m - a_s)
    if mid_in_ccw <= sweep_ccw:
        sweep = sweep_ccw
    else:
        sweep = -(2.0 * math.pi - sweep_ccw)

    points: list[tuple[float, float]] = []
    for i in range(num_segments + 1):
        t = i / num_segments
        a = angle_start + sweep * t
        points.append((ux + radius * math.cos(a), uy + radius * math.sin(a)))

    return points


def _linearize_bezier(
    control_points: list[tuple[float, float]],
    num_segments: int = _DEFAULT_ARC_SEGMENTS,
) -> list[tuple[float, float]]:
    """Approximate a cubic or quadratic Bezier curve as a polyline.

    Supports both quadratic (3 control points) and cubic (4 control
    points) Bezier curves via De Casteljau's algorithm.

    Returns a list of (x, y) points *including* start and end.
    """
    n = len(control_points)
    if n < 2:
        return list(control_points)

    points: list[tuple[float, float]] = []
    for i in range(num_segments + 1):
        t = i / num_segments
        # De Casteljau
        work = list(control_points)
        for level in range(n - 1):
            work = [
                (
                    (1.0 - t) * work[j][0] + t * work[j + 1][0],
                    (1.0 - t) * work[j][1] + t * work[j + 1][1],
                )
                for j in range(len(work) - 1)
            ]
        points.append(work[0])
    return points


# ---------------------------------------------------------------------------
# Outline repair
# ---------------------------------------------------------------------------


def _repair_outline_polygon(poly: Any) -> Any:
    """Repair an invalid board-outline polygon, keeping the largest piece.

    Self-intersecting outlines (bowties) split into multiple lobes when
    repaired.  ``buffer(0)`` silently drops the negatively-wound lobe,
    which *shrinks* the keep-in area -- a conservative failure (routing
    refuses space that exists) but an invisible one (Issue #3614).
    ``shapely.make_valid`` keeps every lobe, so we can make a deliberate
    choice: ``BoardGeometry`` is single-Polygon by design (its Edge.Cuts
    path also keeps only the largest piece), so keep the largest lobe
    and log a WARNING naming the area that was discarded.

    ``make_valid`` may return a GeometryCollection when part of the
    outline collapses to zero-area linework; only polygonal components
    contribute board area, so extract those (mirrors
    ``_repair_fill_polygon`` in ``validate/rules/clearance.py``,
    PR #3613).

    Raises:
        ValueError: If no polygonal area survives the repair (fully
            degenerate outline).
    """
    from shapely import make_valid
    from shapely.geometry import GeometryCollection
    from shapely.geometry import MultiPolygon as ShapelyMultiPolygon

    repaired = make_valid(poly)

    pieces: list[Any] = []
    if isinstance(repaired, ShapelyPolygon):
        pieces = [repaired]
    elif isinstance(repaired, ShapelyMultiPolygon):
        pieces = list(repaired.geoms)
    elif isinstance(repaired, GeometryCollection):
        for geom in repaired.geoms:
            if isinstance(geom, ShapelyPolygon):
                pieces.append(geom)
            elif isinstance(geom, ShapelyMultiPolygon):
                pieces.extend(geom.geoms)

    pieces = [p for p in pieces if not p.is_empty]
    if not pieces:
        raise ValueError(
            "Board outline is degenerate: repairing the invalid polygon produced no polygonal area."
        )

    if len(pieces) == 1:
        return pieces[0]

    pieces.sort(key=lambda p: p.area, reverse=True)
    largest = pieces[0]
    dropped_area = sum(p.area for p in pieces[1:])
    logger.warning(
        "Invalid board outline split into %d pieces after repair; "
        "keeping the largest (%.4f mm^2) and dropping %d piece(s) "
        "totalling %.4f mm^2.  The keep-in area shrank -- check the "
        "outline for self-intersections.",
        len(pieces),
        largest.area,
        len(pieces) - 1,
        dropped_area,
    )
    return largest


# ---------------------------------------------------------------------------
# BoardGeometry
# ---------------------------------------------------------------------------


@dataclass
class BoardGeometry:
    """Shapely-backed board outline geometry.

    Wraps the Edge.Cuts layer of a KiCad PCB into a Shapely ``Polygon``
    (or ``MultiPolygon`` for boards with cutouts) and exposes geometric
    queries commonly needed by placement optimisation, routing edge
    clearance, and keepout zone computation.

    Attributes:
        polygon: The Shapely polygon representing the board outline.
        origin: The board origin ``(ox, oy)`` used to convert from
            sheet-absolute to board-relative coordinates.
    """

    polygon: Any  # shapely.geometry.Polygon | MultiPolygon
    origin: tuple[float, float] = (0.0, 0.0)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_pcb(cls, pcb: PCB) -> BoardGeometry:
        """Build a ``BoardGeometry`` from a parsed PCB.

        Parses Edge.Cuts elements (``gr_line``, ``gr_arc``, ``gr_rect``,
        ``gr_circle``, ``gr_bezier``) and constructs a Shapely polygon.

        Args:
            pcb: A parsed :class:`~kicad_tools.schema.pcb.PCB` instance.

        Returns:
            A new :class:`BoardGeometry`.

        Raises:
            ImportError: If Shapely is not installed.
            ValueError: If no valid board outline can be constructed.
        """
        if not _SHAPELY_AVAILABLE:
            raise ImportError(
                "Shapely is required for BoardGeometry.  "
                "Install it with:  pip install kicad-tools[geometry]"
            )

        origin = pcb.board_origin
        ox, oy = origin

        # Collect line-string segments from Edge.Cuts
        lines: list[list[tuple[float, float]]] = []

        # --- gr_line ---
        for line in pcb.graphic_lines:
            if line.layer == "Edge.Cuts":
                s = (line.start[0] - ox, line.start[1] - oy)
                e = (line.end[0] - ox, line.end[1] - oy)
                lines.append([s, e])

        # --- gr_arc ---
        for arc in pcb.graphic_arcs:
            if arc.layer == "Edge.Cuts":
                sx, sy = arc.start[0] - ox, arc.start[1] - oy
                mx, my = arc.mid[0] - ox, arc.mid[1] - oy
                ex, ey = arc.end[0] - ox, arc.end[1] - oy
                pts = _linearize_arc(sx, sy, mx, my, ex, ey)
                lines.append(pts)

        # --- gr_rect, gr_circle, gr_bezier from raw graphics ---
        for g in pcb.graphics:
            if g.layer != "Edge.Cuts":
                continue

            if g.graphic_type == "rect":
                x1, y1 = g.start[0] - ox, g.start[1] - oy
                x2, y2 = g.end[0] - ox, g.end[1] - oy
                # Four edges of the rectangle
                lines.append([(x1, y1), (x2, y1)])
                lines.append([(x2, y1), (x2, y2)])
                lines.append([(x2, y2), (x1, y2)])
                lines.append([(x1, y2), (x1, y1)])

            elif g.graphic_type == "circle":
                cx_raw, cy_raw = g.start[0] - ox, g.start[1] - oy
                ex_raw, ey_raw = g.end[0] - ox, g.end[1] - oy
                radius = math.sqrt((ex_raw - cx_raw) ** 2 + (ey_raw - cy_raw) ** 2)
                circle_poly = Point(cx_raw, cy_raw).buffer(radius, resolution=_DEFAULT_ARC_SEGMENTS)
                return cls(polygon=circle_poly, origin=origin)

            elif g.graphic_type == "bezier":
                # gr_bezier stores control points in pts attribute
                if hasattr(g, "pts") and g.pts:
                    ctrl = [(p[0] - ox, p[1] - oy) for p in g.pts]
                    pts = _linearize_bezier(ctrl)
                    lines.append(pts)

        if not lines:
            raise ValueError("No Edge.Cuts elements found -- cannot construct board outline.")

        # Build Shapely LineStrings and polygonize
        shapely_lines = [LineString(seg) for seg in lines if len(seg) >= 2]
        result = list(polygonize(unary_union(shapely_lines)))

        if not result:
            # Fall back: try to chain endpoints manually
            all_coords = _chain_segments(lines)
            if all_coords and len(all_coords) >= 3:
                poly = ShapelyPolygon(all_coords)
                if poly.is_valid:
                    return cls(polygon=poly, origin=origin)
            raise ValueError("Could not form a closed polygon from Edge.Cuts segments.")

        if len(result) == 1:
            polygon = result[0]
        else:
            # Multiple polygons -- use the largest as the board outline
            # (smaller ones are likely mounting-hole cutouts)
            result.sort(key=lambda p: p.area, reverse=True)
            polygon = result[0]

        return cls(polygon=polygon, origin=origin)

    @classmethod
    def from_outline_points(
        cls,
        points: list[tuple[float, float]],
        origin: tuple[float, float] = (0.0, 0.0),
    ) -> BoardGeometry:
        """Build from a list of polygon vertices (already board-relative).

        Useful for testing or when outline points are already extracted.

        Invalid (self-intersecting) outlines are repaired via
        ``shapely.make_valid``; if the repair splits the outline into
        multiple pieces, the largest is kept and a WARNING is logged
        naming the dropped area (see :func:`_repair_outline_polygon`).

        Raises:
            ImportError: If Shapely is not installed.
            ValueError: If fewer than 3 points are provided, or the
                outline is fully degenerate (no polygonal area survives
                repair).
        """
        if not _SHAPELY_AVAILABLE:
            raise ImportError(
                "Shapely is required for BoardGeometry.  "
                "Install it with:  pip install kicad-tools[geometry]"
            )

        if len(points) < 3:
            raise ValueError("Need at least 3 points for a polygon.")

        poly = ShapelyPolygon(points)
        if not poly.is_valid:
            # make_valid + keep-largest (NOT buffer(0), which silently
            # drops bowtie lobes and shrinks the keep-in -- Issue #3614).
            poly = _repair_outline_polygon(poly)
        return cls(polygon=poly, origin=origin)

    @classmethod
    def from_bounds(
        cls,
        min_x: float,
        min_y: float,
        max_x: float,
        max_y: float,
        origin: tuple[float, float] = (0.0, 0.0),
    ) -> BoardGeometry:
        """Build a rectangular board geometry from bounding-box limits.

        Raises:
            ImportError: If Shapely is not installed.
        """
        if not _SHAPELY_AVAILABLE:
            raise ImportError(
                "Shapely is required for BoardGeometry.  "
                "Install it with:  pip install kicad-tools[geometry]"
            )

        from shapely.geometry import box

        return cls(polygon=box(min_x, min_y, max_x, max_y), origin=origin)

    # ------------------------------------------------------------------
    # Geometric queries
    # ------------------------------------------------------------------

    def contains_point(self, x: float, y: float) -> bool:
        """Return True if (*x*, *y*) is inside the board outline."""
        return self.polygon.contains(Point(x, y))

    def distance_to_edge(self, x: float, y: float) -> float:
        """Return the minimum distance from (*x*, *y*) to the board edge.

        Positive values mean the point is *inside* the board; negative
        values mean it is *outside*.  (Signed distance.)
        """
        pt = Point(x, y)
        dist = self.polygon.exterior.distance(pt)
        if self.polygon.contains(pt):
            return dist
        return -dist

    def buffer(self, distance: float) -> BoardGeometry:
        """Return a new geometry inset (negative) or expanded (positive).

        A negative *distance* shrinks the outline inward -- useful for
        computing edge-clearance zones.  A positive *distance* expands
        outward.
        """
        new_poly = self.polygon.buffer(distance)
        return BoardGeometry(polygon=new_poly, origin=self.origin)

    def intersection(self, other: BoardGeometry) -> BoardGeometry:
        """Return the geometric intersection with *other*."""
        result = self.polygon.intersection(other.polygon)
        return BoardGeometry(polygon=result, origin=self.origin)

    def difference(self, other: BoardGeometry) -> BoardGeometry:
        """Return the geometric difference (self minus *other*)."""
        result = self.polygon.difference(other.polygon)
        return BoardGeometry(polygon=result, origin=self.origin)

    def union(self, other: BoardGeometry) -> BoardGeometry:
        """Return the geometric union with *other*."""
        result = self.polygon.union(other.polygon)
        return BoardGeometry(polygon=result, origin=self.origin)

    # ------------------------------------------------------------------
    # Compatibility helpers
    # ------------------------------------------------------------------

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Return ``(min_x, min_y, max_x, max_y)`` bounding box."""
        return self.polygon.bounds  # type: ignore[return-value]

    @property
    def area(self) -> float:
        """Return the area of the board outline in mm^2."""
        return float(self.polygon.area)

    @property
    def exterior_coords(self) -> list[tuple[float, float]]:
        """Return exterior ring coordinates as ``[(x, y), ...]``."""
        if hasattr(self.polygon, "exterior"):
            return list(self.polygon.exterior.coords)
        return []

    def to_board_outline(self) -> Any:
        """Convert to a ``BoardOutline`` (AABB) for backward compatibility.

        Returns a ``BoardOutline`` dataclass from
        ``kicad_tools.placement.cost``.
        """
        from kicad_tools.placement.cost import BoardOutline

        min_x, min_y, max_x, max_y = self.bounds
        return BoardOutline(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y)

    def to_optim_polygon(self) -> Any:
        """Convert to an ``optim.geometry.Polygon`` for backward compatibility."""
        from kicad_tools.optim.geometry import Polygon as OptimPolygon
        from kicad_tools.optim.geometry import Vector2D

        coords = self.exterior_coords
        if coords and coords[-1] == coords[0]:
            coords = coords[:-1]  # Remove closing duplicate
        return OptimPolygon(vertices=[Vector2D(x, y) for x, y in coords])

    def compute_boundary_violation(
        self,
        box_min_x: float,
        box_min_y: float,
        box_max_x: float,
        box_max_y: float,
    ) -> float:
        """Compute out-of-bounds area for a component AABB.

        Uses Shapely polygon containment for accurate non-rectangular
        board shapes rather than simple AABB clamping.

        Returns the area (mm^2) of the component box that falls outside
        the board outline.
        """
        from shapely.geometry import box

        comp_box = box(box_min_x, box_min_y, box_max_x, box_max_y)
        outside = comp_box.difference(self.polygon)
        return float(outside.area)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _chain_segments(
    segments: list[list[tuple[float, float]]],
    tolerance: float = 0.01,
) -> list[tuple[float, float]]:
    """Chain line segments into an ordered polygon vertex list.

    Each segment is a list of points ``[(x1, y1), (x2, y2), ...]``.
    We treat the first and last point of each segment as endpoints
    and chain them by proximity.
    """
    if not segments:
        return []

    # Normalise to (start, end, full_points)
    segs: list[tuple[tuple[float, float], tuple[float, float], list[tuple[float, float]]]] = []
    for pts in segments:
        if len(pts) < 2:
            continue
        segs.append((pts[0], pts[-1], pts))

    if not segs:
        return []

    result = list(segs[0][2])
    used = {0}

    while len(used) < len(segs):
        current_end = result[-1]
        found = False
        for i, (s, e, pts) in enumerate(segs):
            if i in used:
                continue
            ds = math.sqrt((s[0] - current_end[0]) ** 2 + (s[1] - current_end[1]) ** 2)
            de = math.sqrt((e[0] - current_end[0]) ** 2 + (e[1] - current_end[1]) ** 2)
            if ds < tolerance:
                result.extend(pts[1:])
                used.add(i)
                found = True
                break
            elif de < tolerance:
                result.extend(reversed(pts[:-1]))
                used.add(i)
                found = True
                break
        if not found:
            break

    return result
