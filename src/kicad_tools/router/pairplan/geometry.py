"""Geometry primitives for engine-free pair planning (issue #6837, slice 2c-i).

Verbatim copies of the point/segment helpers in
:mod:`kicad_tools.router.lattice.geometry` (``Pt``, ``Rect``, ``dist``,
``seg_pt_dist``, ``segs_intersect``, ``seg_seg_dist``).  ``pairplan`` must not
import from any routing engine, so it keeps its own copies -- the same
"engines stay independently removable" precedent the lattice module follows.
The expressions are byte-identical to the lattice ones on purpose: any
reordering could change floating-point results and with them the routed
copper.  ``tests/router/pairplan/test_geometry_parity.py`` pins that.
"""

from __future__ import annotations

import math

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
