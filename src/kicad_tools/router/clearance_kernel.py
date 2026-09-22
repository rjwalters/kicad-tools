"""Exact-geometry clearance kernel -- pure-Python port (Epic #5509, Phase 1b).

The single exact-geometry clearance kernel that later phases of Epic #5509
move every clearance consumer onto.  It deliberately knows *nothing* about
rules, net classes, net-pair exemptions, grids or routing state: it answers
"how far apart is this copper, edge to edge" and "is that at least
``required_mm``".  Rule resolution -- deciding which number ``required_mm`` is
for a given pair -- belongs to the Phase 2 resolver, not here.

**Consumers arrive one epic phase at a time.**  Phase 1b added the kernel and
its parity/fixture evidence with nothing wired to it; Phase 3a (#5660) switched
the first consumer -- ``router/grid.py``'s route-copper halo marking, and its
C++ sibling in ``cpp/src/grid.cpp``.  ``tests/router/test_clearance_kernel_parity.py``
keeps the ledger of who is on the kernel (``SWITCHED_PY_CONSUMERS`` /
``SWITCHED_CPP_CONSUMERS``) and fails on an import that appears without one, so
each phase's before/after measurement stays attributable to that phase.

Port contract
-------------
This module is a line-for-line port of
``src/kicad_tools/router/cpp/src/clearance_kernel.cpp``.
``tests/router/test_clearance_kernel_parity.py`` drives both and asserts
identical :func:`clear` verdicts and ``|gap_py - gap_cpp| <= 1e-7`` mm.  A
divergence is a bug on whichever side disagrees with kicad-cli -- never
something to paper over by loosening the bound.  **Edit both sides together.**

Two deliberate non-choices:

* **Not shapely.**  Shapely's ``buffer`` approximates arcs at its own
  resolution, so a shapely-backed port would disagree with the C++ side at
  every rounded corner.  The geometry here is closed-form.
* **No ``router_cpp`` import.**  This module is importable with no compiled
  extension present; only the tests reach for the extension.

Scope (Phase 1b, complete)
--------------------------
All five shape classes -- :class:`KSegment`, :class:`KVia`, :class:`KEdge`
(PR A) plus :class:`KPad` and :class:`KZonePoly` (PR B) -- and all fifteen
unordered pair kinds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from kicad_tools.core.geometry import (
    point_to_segment_distance,
    rotate_pad_offset,
    segment_to_segment_distance,
)

__all__ = [
    "ALL_LAYERS",
    "ARC_CHORD_ERROR_MM",
    "CLEARANCE_EPSILON_MM",
    "NO_INTERACTION",
    "KEdge",
    "KPad",
    "KRing",
    "KSegment",
    "KShape",
    "KVia",
    "KZonePoly",
    "clear",
    "copper_gap",
    "hole_gap",
    "make_pad",
    "pad_outline",
]

CLEARANCE_EPSILON_MM = 1e-4
"""Tolerance for "is this gap at least the requirement" comparisons.

Deliberately a private copy of ``grid.cpp``'s file-local
``CLEARANCE_EPSILON_MM`` and of ``router/io.py``'s ``_CLEARANCE_EPSILON_MM``.
Phase 4 of Epic #5509 collapses the copies once every consumer is on this
kernel.
"""

ALL_LAYERS = -1
"""Sentinel layer meaning "present on every copper layer".

Vias and the board outline are modelled that way implicitly (neither carries a
layer field); a segment may use it when the caller has no layer information.
"""

NO_INTERACTION = math.inf
"""Returned instead of a distance when a query is not applicable.

Different layers for a copper query, no drilled hole on either side for a hole
query.  Callers compare a gap against a requirement, and ``+inf`` is always
clear.
"""

ARC_CHORD_ERROR_MM = 0.0005
"""Maximum sagitta (chord error) when tessellating a pad arc, in mm.

Only :func:`pad_outline` uses it -- pad *distances* go through the exact
Minkowski core, so tessellation can never move a verdict.
"""


# ---------------------------------------------------------------------------
# Shape value types
# ---------------------------------------------------------------------------
#
# Plain value objects: no net, no rule, no grid coordinates.  Coordinates and
# sizes are millimetres in the board frame.


@dataclass(frozen=True)
class KSegment:
    """A routed track segment: a centreline with a copper width, on one layer."""

    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: int = ALL_LAYERS


@dataclass(frozen=True)
class KVia:
    """A via: copper annulus of ``diameter`` around a hole of ``drill``.

    Present on every copper layer.  ``drill <= 0`` models a via with no hole;
    hole queries against it return :data:`NO_INTERACTION`.
    """

    x: float
    y: float
    diameter: float
    drill: float = 0.0


@dataclass(frozen=True)
class KEdge:
    """The board outline, as an open polyline of vertices.

    Consecutive vertices form the outline segments -- the same model
    ``validate/rules/edge.py:_min_distance_to_outline`` consumes (arcs are
    flattened before they reach it).  Repeat the first vertex at the end to
    close a loop.  Applies to every copper layer.
    """

    points: tuple[tuple[float, float], ...] = field(default=())


KRing = tuple[tuple[float, float], ...]
"""A polygon ring: a *closed* vertex list (first vertex repeated at the end).

The convention ``Grid3D::add_fixed_fill`` already uses for zone fills.
"""


@dataclass(frozen=True)
class KPad:
    """A footprint pad, as a Minkowski sum: a convex ``core`` dilated by ``r``.

    Every branch of the reference model
    ``validate/rules/clearance.py:_pad_polygon`` is exactly such a sum -- see
    :func:`make_pad`, which is the port of it.  Carrying the core (1, 2 or 4
    board-frame vertices) instead of a tessellated outline is what makes pad
    distances *exact*: ``copper_gap(pad, X) = dist(core, X) - corner_radius``
    has no vertex list for the two ports to disagree about and no chord error
    to leak into a verdict.  :func:`pad_outline` still produces the tessellated
    polygon, for export and for the Phase 1c oracle adapter.

    Attributes:
        core: 1, 2 or 4 vertices, already rotated and translated into the
            board frame.  Empty for a degenerate (non-positive) pad size.
        corner_radius: Minkowski dilation radius, 0 for a plain rectangle.
        cx: Pad centre x -- also where the drilled hole sits.
        cy: Pad centre y.
        drill: Hole diameter; ``<= 0`` models an SMD pad (hole queries return
            :data:`NO_INTERACTION`).
        layer: :data:`ALL_LAYERS` for a through-hole pad.
    """

    core: KRing = field(default=())
    corner_radius: float = 0.0
    cx: float = 0.0
    cy: float = 0.0
    drill: float = 0.0
    layer: int = ALL_LAYERS


@dataclass(frozen=True)
class KZonePoly:
    """A filled copper pour on one layer: an outer ring plus interior rings.

    Mirrors ``grid.hpp``'s ``FixedFill`` and the ``_ZoneFill`` model at
    ``validate/rules/clearance.py:1485``, where one zone fill is a shapely
    polygon **with holes** and ``SegmentZoneClearanceRule`` measures
    ``line.distance(poly)`` minus the half width.

    Containment is even-odd across every ring (the
    ``grid.cpp:fixed_fill_clear`` walk, without its binning), so a point inside
    a hole is correctly *outside* the copper.  A zone carries no width: the
    pour *is* the copper.
    """

    rings: tuple[KRing, ...] = field(default=())
    layer: int = ALL_LAYERS


KShape = KSegment | KVia | KEdge | KPad | KZonePoly


# ---------------------------------------------------------------------------
# Pad construction (the ``_pad_polygon`` port)
# ---------------------------------------------------------------------------


def _local_rect(w: float, h: float) -> KRing:
    """An axis-aligned rectangle core centred on the origin, counter-clockwise.

    A degenerate dimension collapses to a segment or a point.  That matters for
    ``roundrect`` with ``rratio`` at KiCad's maximum 0.5, where the inner box is
    exactly ``0 x 0`` (``2 * 0.5 * min`` is exact in binary) and the pad is a
    true disc.  Keeping four coincident vertices would give the right
    *distance* but a degenerate :func:`pad_outline`, whose zero-length edges
    have no normal to rotate an arc between.
    """
    if w <= 0.0 and h <= 0.0:
        return ((0.0, 0.0),)
    if h <= 0.0:
        return ((-w / 2.0, 0.0), (w / 2.0, 0.0))
    if w <= 0.0:
        return ((0.0, -h / 2.0), (0.0, h / 2.0))
    return (
        (-w / 2.0, -h / 2.0),
        (w / 2.0, -h / 2.0),
        (w / 2.0, h / 2.0),
        (-w / 2.0, h / 2.0),
    )


def _pad_core_local(shape: str, w: float, h: float, rratio: float) -> tuple[KRing, float]:
    """Minkowski decomposition of ``_pad_polygon``, in the pad's local frame.

    Branch for branch with ``validate/rules/clearance.py:263 _pad_polygon``,
    **including its fallback**: ``rect`` and any unknown shape -- ``custom``,
    ``trapezoid``, ``chamfered`` -- is the exact ``w x h`` rectangle with no
    over-approximation.  That is deliberate, not an oversight: the kernel must
    not invent primitives the reference model does not have.  A custom pad
    whose copper reaches outside its ``size`` box is under-modelled here
    exactly as it is in ``kct check`` today; Epic #5509 unifies that model, it
    does not change it.

    Returns:
        ``(core vertices, dilation radius)``; an empty core for a
        non-positive size.
    """
    if w <= 0 or h <= 0:
        return (), 0.0

    oval = shape in ("oval", "obround")

    if shape == "circle" or (oval and abs(w - h) < 1e-6):
        return ((0.0, 0.0),), min(w, h) / 2.0

    if oval:
        r = min(w, h) / 2.0
        if w >= h:
            return ((-(w / 2.0 - r), 0.0), (w / 2.0 - r, 0.0)), r
        return ((0.0, -(h / 2.0 - r)), (0.0, h / 2.0 - r)), r

    if shape == "roundrect":
        r = rratio * min(w, h)
        if r <= 0:
            return _local_rect(w, h), 0.0
        return _local_rect(w - 2.0 * r, h - 2.0 * r), r

    # "rect" and every other shape keyword.
    return _local_rect(w, h), 0.0


def make_pad(
    shape: str,
    w: float,
    h: float,
    rratio: float = 0.25,
    rotation_deg: float = 0.0,
    cx: float = 0.0,
    cy: float = 0.0,
    layer: int = ALL_LAYERS,
    drill: float = 0.0,
) -> KPad:
    """Build a :class:`KPad` from KiCad pad parameters.

    The port of ``validate/rules/clearance.py:263 _pad_polygon``, expressed as
    a Minkowski core plus a radius (see :func:`_pad_core_local` for the
    branch-by-branch correspondence, including the ``rect`` fallback for
    ``custom`` / ``trapezoid`` / ``chamfered`` pads).

    Args:
        shape: KiCad pad-shape keyword.
        w: Pad width in its own *local* frame.
        h: Pad height in its own *local* frame.
        rratio: Roundrect corner ratio (KiCad default 0.25).
        rotation_deg: The pad's ABSOLUTE board-frame angle.  ``Pad.rotation``
            already includes the footprint rotation per KiCad's file
            convention (issue #3902), so pass it directly.
        cx: Absolute pad centre x.
        cy: Absolute pad centre y.
        layer: Copper layer index, :data:`ALL_LAYERS` for through-hole.
        drill: Hole diameter, 0 for SMD.

    Returns:
        The pad, with its core rotated into the board frame by the **negated**
        angle -- KiCad's forward transform
        (:func:`kicad_tools.core.geometry.rotate_pad_offset`, pcbnew-verified
        in #3739; the sign error #5227 fixed).
    """
    core_local, radius = _pad_core_local(shape, w, h, rratio)
    core: list[tuple[float, float]] = []
    for vx, vy in core_local:
        rx, ry = rotate_pad_offset(vx, vy, rotation_deg)
        core.append((cx + rx, cy + ry))
    return KPad(
        core=tuple(core),
        corner_radius=radius,
        cx=cx,
        cy=cy,
        drill=drill,
        layer=layer,
    )


def _arc_segment_count(radius: float, sweep: float) -> int:
    """Segments for a ``sweep``-radian arc within :data:`ARC_CHORD_ERROR_MM`.

    A chord subtending ``a`` radians on radius ``r`` has sagitta
    ``r * (1 - cos(a / 2))``; inverting for ``a`` gives the largest admissible
    step, and the count is its ceiling.  An *integer* count computed
    identically on both sides is the whole point: letting each port pick its
    own resolution is how a tessellated outline drifts.
    """
    if radius <= ARC_CHORD_ERROR_MM or sweep <= 0.0:
        return 1
    max_step = 2.0 * math.acos(1.0 - ARC_CHORD_ERROR_MM / radius)
    if not max_step > 0.0:
        return 1
    return max(1, math.ceil(sweep / max_step))


def _outward_normal(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    """Unit outward normal of the directed edge ``a -> b`` of a CCW polygon.

    The right-hand normal, since a CCW polygon keeps its interior to the left.
    """
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    length = math.sqrt(dx * dx + dy * dy)
    if length == 0.0:
        return (0.0, 0.0)
    return (dy / length, -dx / length)


def pad_outline(
    shape: str,
    w: float,
    h: float,
    rratio: float = 0.25,
    rotation_deg: float = 0.0,
    cx: float = 0.0,
    cy: float = 0.0,
) -> KRing:
    """The pad's true copper outline as a closed polygon.

    For export and for the Phase 1c oracle adapter.  Arcs are tessellated with
    a fixed segment *count* per arc derived from the
    :data:`ARC_CHORD_ERROR_MM` sagitta rule (:func:`_arc_segment_count`),
    computed identically in both ports -- never a per-side choice.

    **Not used by** :func:`copper_gap`: pad distances go through the exact
    Minkowski core (see :class:`KPad`), so tessellation can never move a
    verdict.

    Args:
        shape: KiCad pad-shape keyword.
        w: Local pad width.
        h: Local pad height.
        rratio: Roundrect corner ratio.
        rotation_deg: Absolute board-frame pad angle.
        cx: Absolute pad centre x.
        cy: Absolute pad centre y.

    Returns:
        A closed ring of board-frame vertices, empty for a degenerate size.
    """
    pad = make_pad(shape, w, h, rratio, rotation_deg, cx, cy)
    core = pad.core
    r = pad.corner_radius

    if not core:
        return ()

    # Radius 0 -- a bare rectangle.  Return the core itself, closed.
    if r <= 0.0:
        return (*core, core[0])

    # A point core dilates to a full circle.
    if len(core) == 1:
        n = _arc_segment_count(r, 2.0 * math.pi)
        ring = [
            (
                core[0][0] + r * math.cos(2.0 * math.pi * k / n),
                core[0][1] + r * math.sin(2.0 * math.pi * k / n),
            )
            for k in range(n)
        ]
        return (*ring, ring[0])

    # Convex core (a segment traversed both ways, or a CCW quad): walk the
    # directed edges and emit a rounding arc at each vertex, between the
    # outward normals of the incoming and outgoing edges.
    m = len(core)
    if m == 2:
        edges = [(0, 1), (1, 0)]
    else:
        edges = [(i, (i + 1) % m) for i in range(m)]

    ring = []
    for k, (i, j) in enumerate(edges):
        pi_, pj_ = edges[k - 1]
        n_in = _outward_normal(core[pi_], core[pj_])
        n_out = _outward_normal(core[i], core[j])
        a0 = math.atan2(n_in[1], n_in[0])
        a1 = math.atan2(n_out[1], n_out[0])
        sweep = a1 - a0
        while sweep < 0.0:
            sweep += 2.0 * math.pi
        n = _arc_segment_count(r, sweep)
        pivot = core[i]
        for t in range(n + 1):
            ang = a0 + sweep * t / n
            ring.append((pivot[0] + r * math.cos(ang), pivot[1] + r * math.sin(ang)))
    return (*ring, ring[0])


# ---------------------------------------------------------------------------
# Polyline helpers
# ---------------------------------------------------------------------------


def _point_to_polyline_distance(px: float, py: float, points: KRing) -> float:
    """Minimum distance from a point to an open polyline."""
    n = len(points)
    if n == 0:
        return NO_INTERACTION
    if n == 1:
        dx = px - points[0][0]
        dy = py - points[0][1]
        return math.sqrt(dx * dx + dy * dy)

    best = NO_INTERACTION
    for i in range(n - 1):
        d = point_to_segment_distance(
            px, py, points[i][0], points[i][1], points[i + 1][0], points[i + 1][1]
        )
        best = min(best, d)
    return best


def _segment_to_polyline_distance(
    x1: float, y1: float, x2: float, y2: float, points: KRing
) -> float:
    """Minimum distance from a line segment to an open polyline."""
    n = len(points)
    if n == 0:
        return NO_INTERACTION
    if n == 1:
        return point_to_segment_distance(points[0][0], points[0][1], x1, y1, x2, y2)

    best = NO_INTERACTION
    for i in range(n - 1):
        d = segment_to_segment_distance(
            x1,
            y1,
            x2,
            y2,
            points[i][0],
            points[i][1],
            points[i + 1][0],
            points[i + 1][1],
        )
        best = min(best, d)
    return best


def _polyline_to_polyline_distance(a: KRing, b: KRing) -> float:
    """Minimum distance between two open polylines."""
    if not a or not b:
        return NO_INTERACTION
    if len(a) == 1:
        return _point_to_polyline_distance(a[0][0], a[0][1], b)

    best = NO_INTERACTION
    for i in range(len(a) - 1):
        d = _segment_to_polyline_distance(a[i][0], a[i][1], a[i + 1][0], a[i + 1][1], b)
        best = min(best, d)
    return best


# ---------------------------------------------------------------------------
# Ring-set (region) helpers
# ---------------------------------------------------------------------------
#
# A *region* is a list of closed rings: ring 0 the outer boundary, the rest
# interior holes.  Containment is even-odd across every ring, so a point in a
# hole is outside the copper -- the same walk ``grid.cpp:fixed_fill_clear``
# performs, with its 1 mm binning dropped (the kernel answers one pair at a
# time; there is nothing to accelerate).


def _point_in_rings(rings: tuple[KRing, ...], px: float, py: float) -> bool:
    """Even-odd containment across every ring of a region."""
    inside = False
    for ring in rings:
        for i in range(1, len(ring)):
            ax, ay = ring[i - 1]
            bx, by = ring[i]
            if (ay > py) != (by > py) and px < (bx - ax) * (py - ay) / (by - ay) + ax:
                inside = not inside
    return inside


def _rings_empty(rings: tuple[KRing, ...]) -> bool:
    return all(not ring for ring in rings)


def _point_to_rings_distance(rings: tuple[KRing, ...], px: float, py: float) -> float:
    """Distance from a point to the region *boundary* (every ring)."""
    best = NO_INTERACTION
    for ring in rings:
        best = min(best, _point_to_polyline_distance(px, py, ring))
    return best


def _segment_to_rings_distance(
    rings: tuple[KRing, ...], x1: float, y1: float, x2: float, y2: float
) -> float:
    """Distance from a segment to the region boundary."""
    best = NO_INTERACTION
    for ring in rings:
        best = min(best, _segment_to_polyline_distance(x1, y1, x2, y2, ring))
    return best


def _polyline_to_rings_distance(rings: tuple[KRing, ...], poly: KRing) -> float:
    """Distance from an open polyline to the region boundary."""
    best = NO_INTERACTION
    for ring in rings:
        best = min(best, _polyline_to_polyline_distance(poly, ring))
    return best


def _rings_to_rings_distance(a: tuple[KRing, ...], b: tuple[KRing, ...]) -> float:
    """Distance between two region boundaries."""
    best = NO_INTERACTION
    for ring in a:
        best = min(best, _polyline_to_rings_distance(b, ring))
    return best


# --- region distances (0 inside the copper, like shapely's ``distance``) ----


def _region_point_distance(rings: tuple[KRing, ...], px: float, py: float) -> float:
    if _rings_empty(rings):
        return NO_INTERACTION
    if _point_in_rings(rings, px, py):
        return 0.0
    return _point_to_rings_distance(rings, px, py)


def _region_segment_distance(
    rings: tuple[KRing, ...], x1: float, y1: float, x2: float, y2: float
) -> float:
    if _rings_empty(rings):
        return NO_INTERACTION
    if _point_in_rings(rings, x1, y1) or _point_in_rings(rings, x2, y2):
        return 0.0
    return _segment_to_rings_distance(rings, x1, y1, x2, y2)


def _region_polyline_distance(rings: tuple[KRing, ...], poly: KRing) -> float:
    if _rings_empty(rings) or not poly:
        return NO_INTERACTION
    for px, py in poly:
        if _point_in_rings(rings, px, py):
            return 0.0
    return _polyline_to_rings_distance(rings, poly)


def _region_region_distance(a: tuple[KRing, ...], b: tuple[KRing, ...]) -> float:
    if _rings_empty(a) or _rings_empty(b):
        return NO_INTERACTION
    for ring in a:
        for px, py in ring:
            if _point_in_rings(b, px, py):
                return 0.0
    for ring in b:
        for px, py in ring:
            if _point_in_rings(a, px, py):
                return 0.0
    return _rings_to_rings_distance(a, b)


def _pad_region(pad: KPad) -> tuple[KRing, ...]:
    """The pad core closed into a ring, so every region helper serves pads.

    A 1- or 2-vertex core closes to a degenerate ring with no interior, which
    is exactly right: a point and a segment enclose nothing, and the even-odd
    walk counts each crossing twice and cancels.
    """
    if not pad.core:
        return ()
    return ((*pad.core, pad.core[0]),)


# ---------------------------------------------------------------------------
# Layer interaction
# ---------------------------------------------------------------------------


def _shape_layer(shape: KShape) -> int:
    """The layer a shape lives on, or :data:`ALL_LAYERS` when it spans all."""
    if isinstance(shape, (KSegment, KPad, KZonePoly)):
        return shape.layer
    return ALL_LAYERS


def _layers_interact(a: KShape, b: KShape) -> bool:
    la = _shape_layer(a)
    lb = _shape_layer(b)
    if la == ALL_LAYERS or lb == ALL_LAYERS:
        return True
    return la == lb


# ---------------------------------------------------------------------------
# Pair dispatch
# ---------------------------------------------------------------------------
#
# Canonical shape ordering, so each of the fifteen unordered pair kinds is
# implemented exactly once and argument order provably cannot matter.  Must
# match the C++ ``KShape`` variant alternative order and its ``RANK_*``
# constants.

_RANK_SEGMENT = 0
_RANK_VIA = 1
_RANK_EDGE = 2
_RANK_PAD = 3
_RANK_ZONE = 4

_SHAPE_RANK: dict[type, int] = {
    KSegment: _RANK_SEGMENT,
    KVia: _RANK_VIA,
    KEdge: _RANK_EDGE,
    KPad: _RANK_PAD,
    KZonePoly: _RANK_ZONE,
}


def _shape_rank(shape: KShape) -> int:
    return _SHAPE_RANK[type(shape)]


# ---------------------------------------------------------------------------
# Per-pair copper distances
# ---------------------------------------------------------------------------


def _copper_gap_seg_seg(a: KSegment, b: KSegment) -> float:
    centre = segment_to_segment_distance(a.x1, a.y1, a.x2, a.y2, b.x1, b.y1, b.x2, b.y2)
    return centre - a.width / 2.0 - b.width / 2.0


def _copper_gap_seg_via(s: KSegment, v: KVia) -> float:
    centre = point_to_segment_distance(v.x, v.y, s.x1, s.y1, s.x2, s.y2)
    return centre - s.width / 2.0 - v.diameter / 2.0


def _copper_gap_via_via(a: KVia, b: KVia) -> float:
    dx = b.x - a.x
    dy = b.y - a.y
    centre = math.sqrt(dx * dx + dy * dy)
    return centre - a.diameter / 2.0 - b.diameter / 2.0


def _copper_gap_seg_edge(s: KSegment, e: KEdge) -> float:
    centre = _segment_to_polyline_distance(s.x1, s.y1, s.x2, s.y2, e.points)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - s.width / 2.0


def _copper_gap_via_edge(v: KVia, e: KEdge) -> float:
    centre = _point_to_polyline_distance(v.x, v.y, e.points)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - v.diameter / 2.0


# --- pad copper distances --------------------------------------------------
#
# Every one of these is ``dist(core, other) - corner_radius - the other side's
# own half-extent``: the Minkowski identity, so no pad tessellation is involved
# and the answer is exact.


def _copper_gap_pad_seg(p: KPad, s: KSegment) -> float:
    centre = _region_segment_distance(_pad_region(p), s.x1, s.y1, s.x2, s.y2)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - p.corner_radius - s.width / 2.0


def _copper_gap_pad_via(p: KPad, v: KVia) -> float:
    centre = _region_point_distance(_pad_region(p), v.x, v.y)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - p.corner_radius - v.diameter / 2.0


def _copper_gap_pad_pad(a: KPad, b: KPad) -> float:
    centre = _region_region_distance(_pad_region(a), _pad_region(b))
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - a.corner_radius - b.corner_radius


def _copper_gap_pad_edge(p: KPad, e: KEdge) -> float:
    centre = _region_polyline_distance(_pad_region(p), e.points)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - p.corner_radius


def _copper_gap_pad_zone(p: KPad, z: KZonePoly) -> float:
    centre = _region_region_distance(_pad_region(p), z.rings)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - p.corner_radius


# --- zone copper distances -------------------------------------------------
#
# A pour carries no width of its own -- the filled polygon *is* the copper --
# so only the partner's half-extent is subtracted.  This is the
# ``SegmentZoneClearanceRule`` reading (``line.distance(poly)`` minus the half
# width) with shapely replaced by the even-odd walk.


def _copper_gap_zone_seg(z: KZonePoly, s: KSegment) -> float:
    centre = _region_segment_distance(z.rings, s.x1, s.y1, s.x2, s.y2)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - s.width / 2.0


def _copper_gap_zone_via(z: KZonePoly, v: KVia) -> float:
    centre = _region_point_distance(z.rings, v.x, v.y)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - v.diameter / 2.0


def _copper_gap_zone_edge(z: KZonePoly, e: KEdge) -> float:
    return _region_polyline_distance(z.rings, e.points)


def _copper_gap_zone_zone(a: KZonePoly, b: KZonePoly) -> float:
    return _region_region_distance(a.rings, b.rings)


# ---------------------------------------------------------------------------
# Per-pair hole distances
# ---------------------------------------------------------------------------


def _hole_gap_via_via(a: KVia, b: KVia) -> float:
    if a.drill <= 0.0 or b.drill <= 0.0:
        # Only one hole (or none): fall back to the drill-to-copper reading
        # for whichever side is drilled.
        if a.drill > 0.0:
            dx = b.x - a.x
            dy = b.y - a.y
            return math.sqrt(dx * dx + dy * dy) - a.drill / 2.0 - b.diameter / 2.0
        if b.drill > 0.0:
            dx = b.x - a.x
            dy = b.y - a.y
            return math.sqrt(dx * dx + dy * dy) - b.drill / 2.0 - a.diameter / 2.0
        return NO_INTERACTION
    dx = b.x - a.x
    dy = b.y - a.y
    return math.sqrt(dx * dx + dy * dy) - a.drill / 2.0 - b.drill / 2.0


def _hole_gap_via_seg(v: KVia, s: KSegment) -> float:
    if v.drill <= 0.0:
        return NO_INTERACTION
    centre = point_to_segment_distance(v.x, v.y, s.x1, s.y1, s.x2, s.y2)
    return centre - v.drill / 2.0 - s.width / 2.0


def _hole_gap_via_edge(v: KVia, e: KEdge) -> float:
    if v.drill <= 0.0:
        return NO_INTERACTION
    centre = _point_to_polyline_distance(v.x, v.y, e.points)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - v.drill / 2.0


# --- pad / zone hole distances ---------------------------------------------
#
# A pad's hole is a disc of ``drill`` at the pad centre; a zone is never
# drilled.  Where only one side is drilled the reading is drill-to-copper --
# the *hole* against the other object's copper -- exactly the one-sided branch
# :func:`_hole_gap_via_via` already takes.  There is no in-repo reference model
# for drill-to-copper (``git grep hole_to_copper`` finds nothing under
# ``validate/``), so the kernel measures the geometry and leaves the threshold
# entirely to the caller.


def _hole_gap_pad_via(p: KPad, v: KVia) -> float:
    if p.drill > 0.0 and v.drill > 0.0:
        dx = v.x - p.cx
        dy = v.y - p.cy
        return math.sqrt(dx * dx + dy * dy) - p.drill / 2.0 - v.drill / 2.0
    if p.drill > 0.0:
        dx = v.x - p.cx
        dy = v.y - p.cy
        return math.sqrt(dx * dx + dy * dy) - p.drill / 2.0 - v.diameter / 2.0
    if v.drill > 0.0:
        centre = _region_point_distance(_pad_region(p), v.x, v.y)
        if centre == NO_INTERACTION:
            return NO_INTERACTION
        return centre - p.corner_radius - v.drill / 2.0
    return NO_INTERACTION


def _hole_gap_pad_pad(a: KPad, b: KPad) -> float:
    if a.drill > 0.0 and b.drill > 0.0:
        dx = b.cx - a.cx
        dy = b.cy - a.cy
        return math.sqrt(dx * dx + dy * dy) - a.drill / 2.0 - b.drill / 2.0
    if a.drill > 0.0:
        centre = _region_point_distance(_pad_region(b), a.cx, a.cy)
        if centre == NO_INTERACTION:
            return NO_INTERACTION
        return centre - b.corner_radius - a.drill / 2.0
    if b.drill > 0.0:
        centre = _region_point_distance(_pad_region(a), b.cx, b.cy)
        if centre == NO_INTERACTION:
            return NO_INTERACTION
        return centre - a.corner_radius - b.drill / 2.0
    return NO_INTERACTION


def _hole_gap_pad_seg(p: KPad, s: KSegment) -> float:
    if p.drill <= 0.0:
        return NO_INTERACTION
    centre = point_to_segment_distance(p.cx, p.cy, s.x1, s.y1, s.x2, s.y2)
    return centre - p.drill / 2.0 - s.width / 2.0


def _hole_gap_pad_edge(p: KPad, e: KEdge) -> float:
    if p.drill <= 0.0:
        return NO_INTERACTION
    centre = _point_to_polyline_distance(p.cx, p.cy, e.points)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - p.drill / 2.0


def _hole_gap_pad_zone(p: KPad, z: KZonePoly) -> float:
    if p.drill <= 0.0:
        return NO_INTERACTION
    centre = _region_point_distance(z.rings, p.cx, p.cy)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - p.drill / 2.0


def _hole_gap_via_zone(v: KVia, z: KZonePoly) -> float:
    if v.drill <= 0.0:
        return NO_INTERACTION
    centre = _region_point_distance(z.rings, v.x, v.y)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - v.drill / 2.0


# ---------------------------------------------------------------------------
# Public predicates
# ---------------------------------------------------------------------------


def copper_gap(a: KShape, b: KShape) -> float:
    """Edge-to-edge copper distance in mm; negative when the coppers overlap.

    Returns :data:`NO_INTERACTION` when the pair cannot interact at all --
    two segments on different layers, or two board outlines (a meaningless
    pair).

    Args:
        a: First shape.
        b: Second shape.

    Returns:
        Signed edge-to-edge distance in mm, or :data:`NO_INTERACTION`.
    """
    if not _layers_interact(a, b):
        return NO_INTERACTION

    # Normalise the pair into canonical rank order (segment, via, edge, pad,
    # zone) so each of the fifteen unordered kinds is written exactly once.
    # The kernel has no notion of "which existed first" -- that absence is the
    # fix for #5398 -- so sorting the arguments cannot change the answer.
    p, q = (a, b) if _shape_rank(a) <= _shape_rank(b) else (b, a)

    if isinstance(p, KSegment):
        if isinstance(q, KSegment):
            return _copper_gap_seg_seg(p, q)
        if isinstance(q, KVia):
            return _copper_gap_seg_via(p, q)
        if isinstance(q, KEdge):
            return _copper_gap_seg_edge(p, q)
        if isinstance(q, KPad):
            return _copper_gap_pad_seg(q, p)
        return _copper_gap_zone_seg(q, p)

    if isinstance(p, KVia):
        if isinstance(q, KVia):
            return _copper_gap_via_via(p, q)
        if isinstance(q, KEdge):
            return _copper_gap_via_edge(p, q)
        if isinstance(q, KPad):
            return _copper_gap_pad_via(q, p)
        # Rank order puts zone last, so this is the only alternative left.
        assert isinstance(q, KZonePoly)
        return _copper_gap_zone_via(q, p)

    if isinstance(p, KEdge):
        if isinstance(q, KPad):
            return _copper_gap_pad_edge(q, p)
        if isinstance(q, KZonePoly):
            return _copper_gap_zone_edge(q, p)
        # Board outline vs. board outline: not a copper pair.
        return NO_INTERACTION

    if isinstance(p, KPad):
        if isinstance(q, KPad):
            return _copper_gap_pad_pad(p, q)
        assert isinstance(q, KZonePoly)
        return _copper_gap_pad_zone(p, q)

    assert isinstance(q, KZonePoly)
    return _copper_gap_zone_zone(p, q)


def hole_gap(a: KShape, b: KShape) -> float:
    """Edge-to-edge distance in mm involving at least one drilled hole.

    Drill-to-drill when both sides are drilled, drill-to-copper when only one
    is.  Returns :data:`NO_INTERACTION` when neither side has a hole.

    Holes pass through the whole board, so this is layer-independent by
    construction.

    Args:
        a: First shape.
        b: Second shape.

    Returns:
        Signed edge-to-edge distance in mm, or :data:`NO_INTERACTION`.
    """
    # Holes pass through the whole board, so -- unlike :func:`copper_gap` --
    # there is deliberately no layer gate here.
    p, q = (a, b) if _shape_rank(a) <= _shape_rank(b) else (b, a)

    if isinstance(p, KSegment):
        if isinstance(q, KVia):
            return _hole_gap_via_seg(q, p)
        if isinstance(q, KPad):
            return _hole_gap_pad_seg(q, p)
        return NO_INTERACTION

    if isinstance(p, KVia):
        if isinstance(q, KVia):
            return _hole_gap_via_via(p, q)
        if isinstance(q, KEdge):
            return _hole_gap_via_edge(p, q)
        if isinstance(q, KPad):
            return _hole_gap_pad_via(q, p)
        # Rank order puts zone last, so this is the only alternative left.
        assert isinstance(q, KZonePoly)
        return _hole_gap_via_zone(p, q)

    if isinstance(p, KEdge):
        if isinstance(q, KPad):
            return _hole_gap_pad_edge(q, p)
        return NO_INTERACTION

    if isinstance(p, KPad):
        if isinstance(q, KPad):
            return _hole_gap_pad_pad(p, q)
        assert isinstance(q, KZonePoly)
        return _hole_gap_pad_zone(p, q)

    # Neither side is drilled (or can be).
    return NO_INTERACTION


def clear(a: KShape, b: KShape, required_mm: float) -> bool:
    """Copper clearance verdict: ``copper_gap(a, b) >= required_mm - EPSILON``.

    Hole requirements (hole-to-hole, hole-to-copper) carry a *different* rule
    value, so they are not folded in here -- compare :func:`hole_gap` against
    the hole requirement using the same :data:`CLEARANCE_EPSILON_MM`.

    Args:
        a: First shape.
        b: Second shape.
        required_mm: The clearance requirement, resolved by the caller.

    Returns:
        True when the pair satisfies ``required_mm``.
    """
    return copper_gap(a, b) >= required_mm - CLEARANCE_EPSILON_MM
