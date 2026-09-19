"""Exact-geometry clearance kernel -- pure-Python port (Epic #5509, Phase 1b).

The single exact-geometry clearance kernel that later phases of Epic #5509
move every clearance consumer onto.  It deliberately knows *nothing* about
rules, net classes, net-pair exemptions, grids or routing state: it answers
"how far apart is this copper, edge to edge" and "is that at least
``required_mm``".  Rule resolution -- deciding which number ``required_mm`` is
for a given pair -- belongs to the Phase 2 resolver, not here.

**Nothing in the shipped router calls this yet.**  Phase 1b adds the kernel and
its parity/fixture evidence only; ``tests/router/test_clearance_kernel_parity.py``
asserts that no consumer has been switched.

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

Scope (Phase 1b, PR A)
----------------------
Segment, via and board-edge shapes.  :class:`KPad` (with the ``pad_outline``
port of ``validate/rules/clearance.py:_pad_polygon``) and ``KZonePoly`` (rings
with holes) arrive in the follow-up PR.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from kicad_tools.core.geometry import (
    point_to_segment_distance,
    segment_to_segment_distance,
)

__all__ = [
    "ALL_LAYERS",
    "CLEARANCE_EPSILON_MM",
    "NO_INTERACTION",
    "KEdge",
    "KSegment",
    "KShape",
    "KVia",
    "clear",
    "copper_gap",
    "hole_gap",
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


KShape = KSegment | KVia | KEdge


# ---------------------------------------------------------------------------
# Polyline helpers
# ---------------------------------------------------------------------------


def _point_to_polyline_distance(px: float, py: float, edge: KEdge) -> float:
    """Minimum distance from a point to an open polyline."""
    points = edge.points
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


def _segment_to_polyline_distance(seg: KSegment, edge: KEdge) -> float:
    """Minimum distance from a segment centreline to an open polyline."""
    points = edge.points
    n = len(points)
    if n == 0:
        return NO_INTERACTION
    if n == 1:
        return point_to_segment_distance(points[0][0], points[0][1], seg.x1, seg.y1, seg.x2, seg.y2)

    best = NO_INTERACTION
    for i in range(n - 1):
        d = segment_to_segment_distance(
            seg.x1,
            seg.y1,
            seg.x2,
            seg.y2,
            points[i][0],
            points[i][1],
            points[i + 1][0],
            points[i + 1][1],
        )
        best = min(best, d)
    return best


# ---------------------------------------------------------------------------
# Layer interaction
# ---------------------------------------------------------------------------


def _shape_layer(shape: KShape) -> int:
    """The layer a shape lives on, or :data:`ALL_LAYERS` when it spans all."""
    if isinstance(shape, KSegment):
        return shape.layer
    return ALL_LAYERS


def _layers_interact(a: KShape, b: KShape) -> bool:
    la = _shape_layer(a)
    lb = _shape_layer(b)
    if la == ALL_LAYERS or lb == ALL_LAYERS:
        return True
    return la == lb


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
    centre = _segment_to_polyline_distance(s, e)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - s.width / 2.0


def _copper_gap_via_edge(v: KVia, e: KEdge) -> float:
    centre = _point_to_polyline_distance(v.x, v.y, e)
    if centre == NO_INTERACTION:
        return NO_INTERACTION
    return centre - v.diameter / 2.0


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
    centre = _point_to_polyline_distance(v.x, v.y, e)
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

    if isinstance(a, KSegment):
        if isinstance(b, KSegment):
            return _copper_gap_seg_seg(a, b)
        if isinstance(b, KVia):
            return _copper_gap_seg_via(a, b)
        return _copper_gap_seg_edge(a, b)

    if isinstance(a, KVia):
        if isinstance(b, KSegment):
            return _copper_gap_seg_via(b, a)
        if isinstance(b, KVia):
            return _copper_gap_via_via(a, b)
        return _copper_gap_via_edge(a, b)

    if isinstance(b, KSegment):
        return _copper_gap_seg_edge(b, a)
    if isinstance(b, KVia):
        return _copper_gap_via_edge(b, a)
    # Board outline vs. board outline: not a copper pair.
    return NO_INTERACTION


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
    if isinstance(a, KVia):
        if isinstance(b, KVia):
            return _hole_gap_via_via(a, b)
        if isinstance(b, KSegment):
            return _hole_gap_via_seg(a, b)
        return _hole_gap_via_edge(a, b)

    if isinstance(b, KVia):
        if isinstance(a, KSegment):
            return _hole_gap_via_seg(b, a)
        return _hole_gap_via_edge(b, a)

    # Neither side is drilled.
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
