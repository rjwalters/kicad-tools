"""Exact board outlines for panel copies (Issue #6158).

The panel's Edge.Cuts is the boundary of a Shapely union: every board
copy, every tab and the frame rail.  Shapely only knows polygons, so a
copy's rounded corner has to be faceted to take part in the union.  The
fab must still rout the arc the designer drew, not a chord polygon, so
arcs are recovered *after* the union:

1. Each source Edge.Cuts primitive (``gr_line``, ``gr_arc``, ``gr_rect``,
   ``gr_poly``, ``gr_circle``, ``gr_curve``) is parsed into
   :class:`OutlinePrimitive` lines and three-point arcs.
2. Per copy, the primitives are moved rigidly, every arc is faceted onto
   KiCad's 1 nm grid, and each facet edge is registered in an
   :class:`ArcRegistry` under its two snapped endpoints.
3. After the union, :func:`render_ring` walks each boundary ring.  A run
   of consecutive edges that are facets of one registered arc becomes a
   single ``gr_arc``: the source arc verbatim when the run covers it
   whole, or a sub-arc of the same circle when a join clipped it.  Every
   other edge is a ``gr_line``.

Recovery is exact rather than fitted: a facet edge is identified by its
endpoints, which the union leaves untouched (it only *adds* vertices
where outlines cross), so a corner nothing touched comes back as the
identical arc.  Tabs are kept off curves by :func:`flat_intervals`, so in
practice every arc survives whole.

Beziers (``gr_curve``) are the one exception: they are faceted finely
and emitted as line segments, since a clipped Bezier has no exact
three-point form.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from kicad_tools.sexp.parser import SExp

Point = tuple[float, float]
PointMapper = Callable[[float, float], Point]

# KiCad's internal unit is 1 nm.
GRID_MM = 1e-6
# Source endpoints closer than this are the same outline vertex (KiCad's
# own outline chaining is similarly forgiving of hand-drawn outlines).
WELD_TOLERANCE_MM = 1e-3
# Chord-to-arc deviation used when faceting an arc for the union.  It
# only affects the union's polygon, never the emitted arcs.
_FACET_SAGITTA_MM = 1e-3
_MIN_ARC_FACETS = 4
_MAX_ARC_FACETS = 256
_BEZIER_FACETS = 64

_EDGE_CUTS = "Edge.Cuts"


@dataclass(frozen=True)
class OutlinePrimitive:
    """One Edge.Cuts outline element.

    Attributes:
        kind: ``"line"`` or ``"arc"``.
        points: ``(start, end)`` for a line, ``(start, mid, end)`` for an
            arc (KiCad's three-point form).
    """

    kind: str
    points: tuple[Point, ...]

    @property
    def start(self) -> Point:
        return self.points[0]

    @property
    def end(self) -> Point:
        return self.points[-1]

    def mapped(self, mapper: PointMapper) -> OutlinePrimitive:
        return OutlinePrimitive(self.kind, tuple(mapper(x, y) for x, y in self.points))


@dataclass(frozen=True)
class ArcGeometry:
    """Circle-parametrised form of a three-point arc."""

    cx: float
    cy: float
    radius: float
    start_angle: float
    sweep: float  # signed, radians

    def point_at(self, t: float) -> Point:
        a = self.start_angle + self.sweep * t
        return (self.cx + self.radius * math.cos(a), self.cy + self.radius * math.sin(a))


def arc_geometry(start: Point, mid: Point, end: Point) -> ArcGeometry | None:
    """Circle through *start*, *mid*, *end*; ``None`` when collinear."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
          + (cx * cx + cy * cy) * (ay - by)) / d  # fmt: skip
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
          + (cx * cx + cy * cy) * (bx - ax)) / d  # fmt: skip
    radius = math.hypot(ax - ux, ay - uy)
    a_s = math.atan2(ay - uy, ax - ux)
    a_m = math.atan2(by - uy, bx - ux)
    a_e = math.atan2(cy - uy, cx - ux)
    two_pi = 2.0 * math.pi
    ccw = (a_e - a_s) % two_pi
    if ccw == 0.0:
        ccw = two_pi  # closed circle given as one arc
    if (a_m - a_s) % two_pi <= ccw:
        sweep = ccw
    else:
        sweep = ccw - two_pi
    return ArcGeometry(ux, uy, radius, a_s, sweep)


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------


def _xy(node: SExp | None) -> Point | None:
    if node is None or len(node.children) < 2:
        return None
    vals = []
    for child in node.children[:2]:
        if not child.is_atom or isinstance(child.value, bool):
            return None
        if not isinstance(child.value, (int, float)):
            return None
        vals.append(float(child.value))
    return (vals[0], vals[1])


def _on_edge_cuts(node: SExp) -> bool:
    layer = node.find_child("layer")
    return layer is not None and layer.get_string(0) == _EDGE_CUTS


def _float_child(node: SExp, name: str) -> float | None:
    child = node.find_child(name)
    if child is None or not child.children:
        return None
    atom = child.children[0]
    if atom.is_atom and isinstance(atom.value, (int, float)) and not isinstance(atom.value, bool):
        return float(atom.value)
    return None


def _rect_primitives(p1: Point, p2: Point, radius: float) -> list[OutlinePrimitive]:
    x0, x1 = sorted((p1[0], p2[0]))
    y0, y1 = sorted((p1[1], p2[1]))
    r = max(0.0, min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    if r <= GRID_MM:
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return [OutlinePrimitive("line", (corners[i], corners[(i + 1) % 4])) for i in range(4)]
    k = r * (1.0 - math.sqrt(0.5))  # corner inset of a 45-degree arc midpoint
    prims = [
        OutlinePrimitive("line", ((x0 + r, y0), (x1 - r, y0))),
        OutlinePrimitive("arc", ((x1 - r, y0), (x1 - k, y0 + k), (x1, y0 + r))),
        OutlinePrimitive("line", ((x1, y0 + r), (x1, y1 - r))),
        OutlinePrimitive("arc", ((x1, y1 - r), (x1 - k, y1 - k), (x1 - r, y1))),
        OutlinePrimitive("line", ((x1 - r, y1), (x0 + r, y1))),
        OutlinePrimitive("arc", ((x0 + r, y1), (x0 + k, y1 - k), (x0, y1 - r))),
        OutlinePrimitive("line", ((x0, y1 - r), (x0, y0 + r))),
        OutlinePrimitive("arc", ((x0, y0 + r), (x0 + k, y0 + k), (x0 + r, y0))),
    ]
    return [p for p in prims if p.kind == "arc" or _dist(p.start, p.end) > GRID_MM]


def _bezier_points(ctrl: list[Point], n: int = _BEZIER_FACETS) -> list[Point]:
    out = []
    for i in range(n + 1):
        t = i / n
        work = list(ctrl)
        while len(work) > 1:
            work = [
                ((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1])
                for a, b in zip(work[:-1], work[1:], strict=True)
            ]
        out.append(work[0])
    return out


def parse_edge_cuts(root: SExp) -> list[OutlinePrimitive]:
    """Board-level Edge.Cuts graphics of *root* as lines and arcs.

    Coordinates are sheet-absolute, the frame of the raw S-expression.
    """
    prims: list[OutlinePrimitive] = []
    for node in root.children:
        if node.is_atom or not node.name or not node.name.startswith("gr_"):
            continue
        if not _on_edge_cuts(node):
            continue
        name = node.name
        if name == "gr_line":
            s, e = _xy(node.find_child("start")), _xy(node.find_child("end"))
            if s and e:
                prims.append(OutlinePrimitive("line", (s, e)))
        elif name == "gr_arc":
            s, m, e = (_xy(node.find_child(k)) for k in ("start", "mid", "end"))
            if s and m and e:
                prims.append(OutlinePrimitive("arc", (s, m, e)))
        elif name == "gr_rect":
            s, e = _xy(node.find_child("start")), _xy(node.find_child("end"))
            if s and e:
                prims.extend(_rect_primitives(s, e, _float_child(node, "radius") or 0.0))
        elif name == "gr_circle":
            c, e = _xy(node.find_child("center")), _xy(node.find_child("end"))
            if c and e:
                r = _dist(c, e)
                if r > GRID_MM:
                    east, west = (c[0] + r, c[1]), (c[0] - r, c[1])
                    prims.append(OutlinePrimitive("arc", (east, (c[0], c[1] + r), west)))
                    prims.append(OutlinePrimitive("arc", (west, (c[0], c[1] - r), east)))
        elif name == "gr_poly":
            prims.extend(_poly_primitives(node))
        elif name in ("gr_curve", "gr_bezier"):
            pts_node = node.find_child("pts")
            ctrl = [p for p in (_xy(c) for c in _children(pts_node, "xy")) if p]
            if len(ctrl) >= 2:
                pts = _bezier_points(ctrl)
                prims.extend(
                    OutlinePrimitive("line", (a, b))
                    for a, b in zip(pts[:-1], pts[1:], strict=True)
                    if _dist(a, b) > GRID_MM
                )
    return prims


def _children(node: SExp | None, name: str) -> list[SExp]:
    if node is None:
        return []
    return [c for c in node.children if not c.is_atom and c.name == name]


def _poly_primitives(node: SExp) -> list[OutlinePrimitive]:
    """A closed ``gr_poly``; its ``pts`` may mix ``xy`` and ``arc`` entries."""
    pts_node = node.find_child("pts")
    if pts_node is None:
        return []
    # Each entry contributes a path piece; consecutive pieces join.
    pieces: list[OutlinePrimitive | Point] = []
    for child in pts_node.children:
        if child.is_atom:
            continue
        if child.name == "xy":
            p = _xy(child)
            if p:
                pieces.append(p)
        elif child.name == "arc":
            s, m, e = (_xy(child.find_child(k)) for k in ("start", "mid", "end"))
            if s and m and e:
                pieces.append(OutlinePrimitive("arc", (s, m, e)))
    prims: list[OutlinePrimitive] = []
    cursor: Point | None = None
    first: Point | None = None
    for piece in pieces:
        start = piece.start if isinstance(piece, OutlinePrimitive) else piece
        if cursor is not None and _dist(cursor, start) > GRID_MM:
            prims.append(OutlinePrimitive("line", (cursor, start)))
        if first is None:
            first = start
        if isinstance(piece, OutlinePrimitive):
            prims.append(piece)
            cursor = piece.end
        else:
            cursor = piece
    if cursor is not None and first is not None and _dist(cursor, first) > GRID_MM:
        prims.append(OutlinePrimitive("line", (cursor, first)))
    return prims


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def weld_endpoints(
    prims: list[OutlinePrimitive], tolerance: float = WELD_TOLERANCE_MM
) -> list[OutlinePrimitive]:
    """Snap nearly coincident endpoints onto one shared vertex.

    Polygonising needs exactly shared endpoints; hand-drawn outlines are
    often a few nanometres apart at a joint.
    """
    anchors: list[Point] = []

    def anchor(p: Point) -> Point:
        for a in anchors:
            if _dist(a, p) <= tolerance:
                return a
        anchors.append(p)
        return p

    out = []
    for prim in prims:
        pts = list(prim.points)
        pts[0] = anchor(pts[0])
        pts[-1] = anchor(pts[-1])
        if prim.kind == "line" and pts[0] == pts[-1]:
            continue
        out.append(OutlinePrimitive(prim.kind, tuple(pts)))
    return out


def primitive_bounds(prims: Iterable[OutlinePrimitive]) -> tuple[float, float, float, float]:
    """Exact ``(min_x, min_y, max_x, max_y)``, including arc extrema."""
    xs: list[float] = []
    ys: list[float] = []
    for prim in prims:
        xs.extend(p[0] for p in (prim.start, prim.end))
        ys.extend(p[1] for p in (prim.start, prim.end))
        if prim.kind != "arc":
            continue
        geo = arc_geometry(*prim.points)
        if geo is None:
            continue
        for k in range(-8, 9):  # every axis crossing within +-4 turns
            a = k * math.pi / 2.0
            t = (a - geo.start_angle) / geo.sweep
            if 0.0 < t < 1.0:
                x, y = geo.point_at(t)
                xs.append(x)
                ys.append(y)
    if not xs:
        raise ValueError("empty outline")
    return (min(xs), min(ys), max(xs), max(ys))


# ----------------------------------------------------------------------
# Faceting and arc registry
# ----------------------------------------------------------------------


def snap(v: float) -> int:
    """Coordinate in mm -> integer nanometres."""
    return round(v / GRID_MM)


def _key(p: Point) -> tuple[int, int]:
    return (snap(p[0]), snap(p[1]))


def _grid(p: Point) -> Point:
    return (snap(p[0]) * GRID_MM, snap(p[1]) * GRID_MM)


def _facet_count(geo: ArcGeometry) -> int:
    r = geo.radius
    if r <= _FACET_SAGITTA_MM:
        step = math.pi / 2.0
    else:
        step = 2.0 * math.acos(1.0 - _FACET_SAGITTA_MM / r)
    n = math.ceil(abs(geo.sweep) / step)
    return max(_MIN_ARC_FACETS, min(_MAX_ARC_FACETS, n))


@dataclass
class _RegisteredArc:
    geo: ArcGeometry
    start: Point
    mid: Point
    end: Point
    facets: int


class ArcRegistry:
    """Remembers which faceted edges stand in for which arcs."""

    def __init__(self) -> None:
        self._arcs: list[_RegisteredArc] = []
        # frozenset of two snapped endpoints -> (arc id, facet index)
        self._edges: dict[frozenset[tuple[int, int]], tuple[int, int]] = {}

    def facet(self, prim: OutlinePrimitive) -> list[Point]:
        """Grid-snapped polyline for *prim*; arcs are registered."""
        if prim.kind == "line":
            return [_grid(prim.start), _grid(prim.end)]
        geo = arc_geometry(*prim.points)
        if geo is None:
            return [_grid(prim.start), _grid(prim.end)]
        n = _facet_count(geo)
        pts = [_grid(prim.start)]
        pts.extend(_grid(geo.point_at(i / n)) for i in range(1, n))
        pts.append(_grid(prim.end))
        # Facets that snap to nothing would make keys ambiguous.
        dedup = [pts[0]]
        for p in pts[1:]:
            if _key(p) != _key(dedup[-1]):
                dedup.append(p)
        if len(dedup) < 2:
            return dedup
        arc_id = len(self._arcs)
        self._arcs.append(
            _RegisteredArc(
                geo=geo,
                start=dedup[0],
                mid=_grid(prim.points[1]),
                end=dedup[-1],
                facets=len(dedup) - 1,
            )
        )
        for i, (a, b) in enumerate(zip(dedup[:-1], dedup[1:], strict=True)):
            self._edges[frozenset((_key(a), _key(b)))] = (arc_id, i)
        return dedup

    def lookup(self, a: Point, b: Point) -> tuple[int, int] | None:
        return self._edges.get(frozenset((_key(a), _key(b))))

    def arc(self, arc_id: int) -> _RegisteredArc:
        return self._arcs[arc_id]


def copy_region(prims: list[OutlinePrimitive], registry: ArcRegistry) -> Any:
    """Shapely region enclosed by one copy's (already placed) outline.

    Outer rings and cutouts both come out right: the region is the
    even-odd combination of every closed face's exterior ring.
    """
    from shapely import set_precision  # type: ignore[import-untyped]
    from shapely.geometry import LineString, Polygon  # type: ignore[import-untyped]
    from shapely.ops import polygonize, unary_union  # type: ignore[import-untyped]

    lines = []
    for prim in prims:
        pts = registry.facet(prim)
        if len(pts) >= 2:
            lines.append(LineString(pts))
    if not lines:
        return Polygon()
    faces = list(polygonize(unary_union(lines)))
    rings: dict[frozenset[tuple[int, int]], Any] = {}
    for face in faces:
        shell = Polygon(face.exterior)
        rings.setdefault(frozenset(_key(c) for c in face.exterior.coords), shell)
    region = Polygon()
    for shell in rings.values():
        region = region.symmetric_difference(shell)
    return set_precision(region, GRID_MM)


# ----------------------------------------------------------------------
# Flat edges (where a tab may attach, and whether a side can be butted)
# ----------------------------------------------------------------------


def flat_intervals(
    prims: Iterable[OutlinePrimitive],
    axis: str,
    coord: float,
    tolerance: float = 1e-6,
) -> list[tuple[float, float]]:
    """Merged straight outline runs lying on the line ``axis = coord``.

    *axis* is ``"y"`` for a horizontal side (``y == coord``; intervals are
    along X) or ``"x"`` for a vertical side.
    """
    raw: list[tuple[float, float]] = []
    for prim in prims:
        if prim.kind != "line":
            continue
        (sx, sy), (ex, ey) = prim.start, prim.end
        if axis == "y" and abs(sy - coord) <= tolerance and abs(ey - coord) <= tolerance:
            raw.append((min(sx, ex), max(sx, ex)))
        elif axis == "x" and abs(sx - coord) <= tolerance and abs(ex - coord) <= tolerance:
            raw.append((min(sy, ey), max(sy, ey)))
    raw.sort()
    merged: list[tuple[float, float]] = []
    for lo, hi in raw:
        if merged and lo <= merged[-1][1] + tolerance:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def covers(intervals: list[tuple[float, float]], lo: float, hi: float, tol: float = 1e-6) -> bool:
    """True if one interval contains ``[lo, hi]``."""
    return any(a - tol <= lo and hi <= b + tol for a, b in intervals)


def intersect_intervals(
    a: list[tuple[float, float]], b: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    out = []
    for a0, a1 in a:
        for b0, b1 in b:
            lo, hi = max(a0, b0), min(a1, b1)
            if hi > lo:
                out.append((lo, hi))
    return sorted(out)


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def _mm(value: float) -> SExp:
    text = f"{round(value, 6):.6f}".rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return SExp(value=float(text), _original_str=text)


def _pt(name: str, p: Point) -> SExp:
    return SExp.list(name, _mm(p[0]), _mm(p[1]))


def _graphic(name: str, points: list[tuple[str, Point]], width: float) -> SExp:
    node = SExp.list(name)
    for tag, p in points:
        node.append(_pt(tag, p))
    node.append(SExp.list("stroke", SExp.list("width", _mm(width)), SExp.list("type", "default")))
    node.append(SExp.list("layer", SExp.quoted_atom(_EDGE_CUTS)))
    node.append(SExp.list("uuid", SExp.quoted_atom(str(uuid.uuid4()))))
    return node


def _collinear(a: Point, b: Point, c: Point) -> bool:
    """``a -> b -> c`` continues straight on (in the same direction)."""
    ux, uy = b[0] - a[0], b[1] - a[1]
    vx, vy = c[0] - b[0], c[1] - b[1]
    cross = ux * vy - uy * vx
    # |cross| / |u| is c's offset from line ab; allow one grid step.
    return ux * vx + uy * vy > 0 and abs(cross) <= GRID_MM * max(math.hypot(ux, uy), GRID_MM)


def render_ring(ring: list[Point], registry: ArcRegistry, width: float = 0.1) -> list[SExp]:
    """Edge.Cuts graphics for one closed boundary ring (first == last).

    Consecutive facets of one registered arc collapse back into a
    ``gr_arc`` -- the source arc verbatim when the run covers all of it, a
    sub-arc of the same circle otherwise.  Every other edge is a
    ``gr_line``, with collinear runs (a board edge the union split where
    a tab joins) merged into one segment.  Endpoints are the ring's own
    grid-snapped vertices, so neighbouring graphics share them exactly.
    """
    pts = [_grid(p) for p in ring]
    if len(pts) >= 2 and _key(pts[0]) == _key(pts[-1]):
        pts = pts[:-1]
    n = len(pts)
    if n < 2:
        return []
    tags = [registry.lookup(pts[i], pts[(i + 1) % n]) for i in range(n)]

    def same_run(prev: tuple[int, int] | None, cur: tuple[int, int] | None) -> bool:
        return (
            prev is not None
            and cur is not None
            and prev[0] == cur[0]
            and abs(prev[1] - cur[1]) == 1
        )

    def breaks_before(i: int) -> bool:
        """True if a graphic must start at vertex *i*."""
        prev, cur = tags[i - 1], tags[i]
        if prev is None and cur is None:
            return not _collinear(pts[i - 1], pts[i], pts[(i + 1) % n])
        return not same_run(prev, cur)

    starts = [i for i in range(n) if breaks_before(i)]
    if not starts:  # a lone closed arc or a degenerate ring: nothing to merge
        starts = [0]

    # Each graphic spans the edges from one start to the next.
    items: list[tuple[str, list[tuple[str, Point]]]] = []
    for s_idx, i in enumerate(starts):
        j = starts[(s_idx + 1) % len(starts)]
        span = (j - i) % n or n
        first, last = pts[i], pts[(i + span) % n]
        tag = tags[i]
        if tag is None:
            items.append(("gr_line", [("start", first), ("end", last)]))
            continue
        arc = registry.arc(tag[0])
        idx = [tags[(i + k) % n][1] for k in range(span)]  # type: ignore[index]
        lo, hi = min(idx), max(idx) + 1
        if lo == 0 and hi == arc.facets:
            mid = arc.mid
        else:
            mid = _grid(arc.geo.point_at((lo + hi) / 2.0 / arc.facets))
        items.append(("gr_arc", [("start", first), ("mid", mid), ("end", last)]))
    return [_graphic(name, points, width) for name, points in items]
