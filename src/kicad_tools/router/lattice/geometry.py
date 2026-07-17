"""Geometry helpers for the octilinear lattice engine (issue #4278).

Small, dependency-free primitives: point/segment distances, segment
intersection, rectangle tests, and a spatial hash for committed copper.
The lattice package keeps its own copies (rather than importing from
``router.mesh``) so the two engines stay independently removable --
supersession of either substrate must not orphan the other's helpers.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator

Pt = tuple[float, float]
Rect = tuple[float, float, float, float]  # (xmin, ymin, xmax, ymax)


def dist(a: Pt, b: Pt) -> float:
    """Euclidean distance between two points."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def seg_pt_dist(a: Pt, b: Pt, p: Pt) -> float:
    """Minimum distance from point ``p`` to segment ``a-b``."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 1e-18:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / length2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _orient(a: Pt, b: Pt, c: Pt) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segs_intersect(a: Pt, b: Pt, c: Pt, d: Pt) -> bool:
    """True if open segments ``a-b`` and ``c-d`` properly cross."""
    o1, o2 = _orient(a, b, c), _orient(a, b, d)
    o3, o4 = _orient(c, d, a), _orient(c, d, b)
    return ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0))


def seg_seg_dist(a: Pt, b: Pt, c: Pt, d: Pt) -> float:
    """Minimum distance between segments ``a-b`` and ``c-d`` (0 if crossing)."""
    if segs_intersect(a, b, c, d):
        return 0.0
    return min(
        seg_pt_dist(a, b, c),
        seg_pt_dist(a, b, d),
        seg_pt_dist(c, d, a),
        seg_pt_dist(c, d, b),
    )


def pt_in_rect(p: Pt, rect: Rect) -> bool:
    """True if ``p`` lies inside axis-aligned ``rect`` (inclusive)."""
    x0, y0, x1, y1 = rect
    return x0 <= p[0] <= x1 and y0 <= p[1] <= y1


def rects_overlap(a: Rect, b: Rect) -> bool:
    """True if two AABBs overlap (touching edges count)."""
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def seg_rect_intersect(a: Pt, b: Pt, rect: Rect) -> bool:
    """True if segment ``a-b`` enters axis-aligned ``rect``.

    Covers the three cases: an endpoint inside, a proper crossing of a
    rectangle side, and a collinear graze (distance-zero touch).
    """
    if pt_in_rect(a, rect) or pt_in_rect(b, rect):
        return True
    x0, y0, x1, y1 = rect
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    for c1, c2 in zip(corners, corners[1:] + corners[:1], strict=False):
        if segs_intersect(a, b, c1, c2):
            return True
        if seg_seg_dist(a, b, c1, c2) < 1e-12:
            return True
    return False


class SegHash:
    """Uniform-bucket spatial hash over committed copper segments.

    Buckets are keyed by integer cells of ``cell`` mm.  Each stored item is
    ``(a, b, net, half_width)``; queries return every item whose inflated
    bounding box touches the query segment's cells, deduplicated by identity.
    """

    def __init__(self, cell: float = 2.0) -> None:
        self.cell = cell
        self.buckets: dict[tuple[int, int], list[tuple[Pt, Pt, int, float]]] = defaultdict(list)

    def _cells_for_seg(self, a: Pt, b: Pt, pad: float = 0.0) -> Iterator[tuple[int, int]]:
        x0 = min(a[0], b[0]) - pad
        x1 = max(a[0], b[0]) + pad
        y0 = min(a[1], b[1]) - pad
        y1 = max(a[1], b[1]) + pad
        c = self.cell
        for ix in range(int(math.floor(x0 / c)), int(math.floor(x1 / c)) + 1):
            for iy in range(int(math.floor(y0 / c)), int(math.floor(y1 / c)) + 1):
                yield (ix, iy)

    def add(self, a: Pt, b: Pt, net: int, half_width: float) -> None:
        """Insert segment ``a-b`` of net ``net`` with copper ``half_width``."""
        for key in self._cells_for_seg(a, b, pad=half_width + 0.5):
            self.buckets[key].append((a, b, net, half_width))

    def query_seg(self, a: Pt, b: Pt, pad: float = 1.0) -> Iterator[tuple[Pt, Pt, int, float]]:
        """Yield stored segments near ``a-b`` (each item exactly once)."""
        seen: set[int] = set()
        for key in self._cells_for_seg(a, b, pad=pad):
            for item in self.buckets[key]:
                iid = id(item)
                if iid not in seen:
                    seen.add(iid)
                    yield item
