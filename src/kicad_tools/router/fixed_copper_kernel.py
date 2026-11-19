"""Fixed copper as kernel shapes, with a spatial index (Epic #5509, Phase 3f).

:mod:`kicad_tools.router.fixed_copper` is how every routing engine sees copper
it must route *around* but may never reuse: filled zones, preserved routes held
fixed across a resumable session, and the board-frame copper of
placement-excluded pads.  This module is the one place where that model meets
the Phase 1b exact-geometry kernel
(:mod:`kicad_tools.router.clearance_kernel`), the way
``router/lattice/kernel_adapter.py`` is for the lattice engine (Phase 3d).

What the adapter owns
---------------------
* **Shape projection.**  A fill is a shapely polygon (or multipolygon) of real
  copper; the kernel's ``KZonePoly`` is a ring set.  :func:`kernel_fill` does
  the conversion once, off the query path, from exactly the rings
  ``FixedFillObstacles.native_polygons`` already hands the C++ grid -- so the
  two halves of consumer group 6 measure literally the same copper.
* **The spatial index.**  ``Grid3D::add_fixed_fill`` bins a pour's boundary
  edges into 1 mm cells (plus a row index for the containment walk) and
  ``Grid3D::fixed_fill_clear`` queries them.  :class:`KernelFill` is that same
  index in Python, built with the same loop, because the alternative is
  unusable: the fixed-copper predicate sits in the pure-Python A* step loop
  (``pathfinder._fixed_step_clear``), and walking a 2000-vertex pour per step
  costs tens of milliseconds where the indexed walk costs microseconds.
* **Nothing else.**  Every number in the verdict comes from the kernel --
  the per-edge gap from
  :func:`~kicad_tools.router.clearance_kernel.copper_gap_ring_edge`, the
  even-odd containment parity from
  :func:`~kicad_tools.router.clearance_kernel.ring_edge_crosses_ray`, the
  comparison slack from
  :data:`~kicad_tools.router.clearance_kernel.CLEARANCE_EPSILON_MM`.  No rule
  value, no net semantics, no heuristic (Epic #5509 scope guards #1/#2).

Why the index cannot move a verdict
-----------------------------------
The bins *select* edges, they never judge one.  An edge outside the query box
expanded by ``required`` cannot be the nearest edge, so the minimum over the
selected edges equals the minimum over all of them whenever that minimum is
below the requirement -- which is the only case a clearance comparison can
distinguish.  The row index holds every edge that can straddle the query
point's own ``y``, which is every edge the ``+x`` ray can cross, so the parity
it produces is the parity over the whole ring set.  Feeding the whole
``KZonePoly`` to :func:`~kicad_tools.router.clearance_kernel.copper_gap`
therefore gives the same answer, and
``tests/router/test_fixed_copper_kernel.py`` asserts exactly that over random
pours and probes.

Why the gaps are edge-to-edge now
---------------------------------
``FixedFillObstacles.segment_clear`` used to compare a **centreline** distance
(shapely's ``LineString.distance(polygon)``) against a composed
``half + max(clearance, fill.clearance)``.  The kernel answers the edge-to-edge
question directly, so the comparison is now against the clearance alone.  The
two forms are algebraically identical -- ``d - half >= clr`` is
``d >= half + clr`` -- so no verdict moves except by floating-point noise far
below the ``1e-4`` slack both forms carry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .clearance_kernel import (
    CLEARANCE_EPSILON_MM,
    KSegment,
    copper_gap_ring_edge,
    ring_edge_crosses_ray,
)

__all__ = [
    "TOUCH_EPSILON_MM",
    "KernelFill",
    "fixed_fill_clear",
    "kernel_fill",
    "polygon_rings",
]

TOUCH_EPSILON_MM = 1e-7
"""Copper the query *touches* is refused however small the requirement.

``Grid3D::fixed_fill_clear``'s long-standing ``distance <= 1e-7`` rule, kept
verbatim; its Python twin expressed the same intent as shapely's ``intersects``
short-circuit.  For any realistic clearance the ordinary
:data:`~kicad_tools.router.clearance_kernel.CLEARANCE_EPSILON_MM` comparison is
stricter and this rule never decides anything -- it only matters when the
requirement itself is at or below 1e-4 mm.
"""

_Edge = tuple[float, float, float, float]


@dataclass(frozen=True)
class KernelFill:
    """One fill's copper as kernel-ready edges, plus the 1 mm index over them.

    The Python twin of ``Grid3D::FixedFill`` (``grid.hpp:30``), field for
    field, so the two implementations of consumer group 6 can be read side by
    side.

    Attributes:
        layer: Grid layer index this copper lives on.
        clearance: The fill's own clearance floor, in mm.
        edges: Every ring edge as ``(ax, ay, bx, by)``.
        rows: ``floor(y)`` -> indices of the edges spanning that row, for the
            even-odd containment walk.
        bins: ``(floor(x), floor(y))`` -> indices of the edges touching that
            1 mm cell, for the distance walk.
        minx: Copper bounding box, for the per-fill reject.
        miny: Copper bounding box, for the per-fill reject.
        maxx: Copper bounding box, for the per-fill reject.
        maxy: Copper bounding box, for the per-fill reject.
    """

    layer: int
    clearance: float
    edges: tuple[_Edge, ...]
    rows: dict[int, tuple[int, ...]]
    bins: dict[tuple[int, int], tuple[int, ...]]
    minx: float
    miny: float
    maxx: float
    maxy: float


def polygon_rings(geometry: Any) -> list[list[list[tuple[float, float]]]]:
    """Every ring of a shapely polygon or multipolygon, one lobe at a time.

    Each lobe yields its exterior ring first, then its interior rings, in the
    ``[exterior, *interiors]`` convention ``Grid3D::add_fixed_fill`` consumes
    and :meth:`FixedFillObstacles.native_polygons` already produced.  Rings are
    closed vertex lists (shapely repeats the first vertex), which is what the
    kernel's ``KRing`` means.

    Args:
        geometry: A shapely ``Polygon`` or ``MultiPolygon``.

    Returns:
        One ring list per lobe; empty when the geometry encloses no copper.
    """
    if geometry.is_empty:
        return []
    geoms = geometry.geoms if geometry.geom_type == "MultiPolygon" else (geometry,)
    return [
        [list(lobe.exterior.coords), *[list(ring.coords) for ring in lobe.interiors]]
        for lobe in geoms
    ]


def kernel_fill(
    layer: int, clearance: float, rings: list[list[tuple[float, float]]]
) -> KernelFill | None:
    """Index one fill's rings, off the query path.

    A line-for-line port of ``Grid3D::add_fixed_fill`` (``grid.cpp:14``),
    including its 1 mm bin size and the ``floor``-based span walk, so an edge
    lands in the same cells on both sides.

    Args:
        layer: Grid layer index.
        clearance: The fill's own clearance floor, in mm.
        rings: ``[exterior, *interiors]`` for one lobe, from
            :func:`polygon_rings`.

    Returns:
        The indexed fill, or ``None`` when the rings enclose no copper (the
        early return ``add_fixed_fill`` makes for empty input).
    """
    if not rings or not rings[0]:
        return None

    edges: list[_Edge] = []
    rows: dict[int, list[int]] = {}
    bins: dict[tuple[int, int], list[int]] = {}
    minx = maxx = rings[0][0][0]
    miny = maxy = rings[0][0][1]

    for ring in rings:
        for i in range(1, len(ring)):
            ax, ay = ring[i - 1]
            bx, by = ring[i]
            minx = min(minx, ax, bx)
            miny = min(miny, ay, by)
            maxx = max(maxx, ax, bx)
            maxy = max(maxy, ay, by)
            index = len(edges)
            edges.append((ax, ay, bx, by))
            for y in range(math.floor(min(ay, by)), math.floor(max(ay, by)) + 1):
                rows.setdefault(y, []).append(index)
                for x in range(math.floor(min(ax, bx)), math.floor(max(ax, bx)) + 1):
                    bins.setdefault((x, y), []).append(index)

    return KernelFill(
        layer=layer,
        clearance=clearance,
        edges=tuple(edges),
        rows={y: tuple(v) for y, v in rows.items()},
        bins={key: tuple(v) for key, v in bins.items()},
        minx=minx,
        miny=miny,
        maxx=maxx,
        maxy=maxy,
    )


def _in_copper(fill: KernelFill, px: float, py: float) -> bool:
    """Is ``(px, py)`` inside this fill's copper?

    The kernel's even-odd walk (``clearance_kernel._point_in_rings``) over the
    edges the row index selects.  Only an edge whose y-span straddles ``py``
    can be crossed by the ``+x`` ray, and every such edge is in
    ``rows[floor(py)]``, so the parity is the same one the whole-ring walk
    computes.
    """
    row = fill.rows.get(math.floor(py))
    if row is None:
        return False
    inside = False
    edges = fill.edges
    for i in row:
        ax, ay, bx, by = edges[i]
        if ring_edge_crosses_ray(px, py, ax, ay, bx, by):
            inside = not inside
    return inside


def fixed_fill_clear(
    fill: KernelFill,
    ax: float,
    ay: float,
    bx: float,
    by: float,
    layer: int,
    half: float,
    reach: float,
) -> bool:
    """Is a candidate trace clear of one fill's copper?

    The Python twin of ``Grid3D::fixed_fill_clear`` (``grid.cpp:41``), with the
    same signature and the same arithmetic, both now expressed through the
    clearance kernel.  A degenerate query (``a == b``) is the via / node form,
    exactly as ``FixedFillObstacles.via_clear`` has always expressed it.

    Args:
        fill: The indexed fill.
        ax: Query centreline start x.
        ay: Query centreline start y.
        bx: Query centreline end x.
        by: Query centreline end y.
        layer: The query's grid layer index.
        half: The query's copper half width, in mm.
        reach: The caller's pre-composed centreline reach
            (``half + clearance``); the fill's own clearance floor can only
            raise it.

    Returns:
        True when the query keeps its distance from this fill.
    """
    if fill.layer != layer:
        return True

    required = max(reach, half + fill.clearance)
    x0, x1 = min(ax, bx) - required, max(ax, bx) + required
    y0, y1 = min(ay, by) - required, max(ay, by) + required
    if x1 < fill.minx or x0 > fill.maxx or y1 < fill.miny or y0 > fill.maxy:
        return True

    if _in_copper(fill, ax, ay) or _in_copper(fill, bx, by):
        return False

    # ``required`` is a centreline reach and the kernel answers edge to edge,
    # so the requirement loses the query's own half width: ``gap >=
    # required_gap`` and ``distance >= required`` are one comparison.
    probe = KSegment(ax, ay, bx, by, 2.0 * half, layer)
    required_gap = required - half
    edges = fill.edges
    bins = fill.bins
    for y in range(math.floor(max(y0, fill.miny)), math.floor(min(y1, fill.maxy)) + 1):
        for x in range(math.floor(max(x0, fill.minx)), math.floor(min(x1, fill.maxx)) + 1):
            for i in bins.get((x, y), ()):
                gap = copper_gap_ring_edge(probe, *edges[i])
                if gap + half <= TOUCH_EPSILON_MM or gap < required_gap - CLEARANCE_EPSILON_MM:
                    return False
    return True
