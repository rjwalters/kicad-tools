"""``schema.pcb`` primitives -> clearance-kernel shapes (Epic #5509, Phase 4d).

The validate-side sibling of :mod:`kicad_tools.router.clearance_shapes`.  That
module translates **router** primitives (``router.primitives.Pad`` / ``Segment``
/ ``Via``) into the kernel's value objects; this one translates the **board
file** objects :mod:`kicad_tools.schema.pcb` parses, which is what every
``kct check`` rule consumes.

Why a second translation rather than one
----------------------------------------
The two sides speak different primitives, not different *geometry*.  A router
``Pad`` carries a pre-swapped ``width``/``height`` plus a residual rotation
(issue #4910); a schema ``Pad`` carries KiCad's own local ``size`` plus the
absolute board-frame ``rotation`` that already folds in the footprint's own
angle (issue #3902), and its board position must still be derived from the
owning footprint.  Both end up calling the *same*
:func:`kicad_tools.router.clearance_kernel.make_pad` with the same meaning for
each argument, so there is exactly one pad model -- the one Phase 1b ported
from ``validate/rules/clearance.py:_pad_polygon``, i.e. from this side.

**Rule resolution stays with the caller.**  Nothing here decides what
``required_mm`` is; Epic #5509 scope guard #1 forbids this phase from touching
any rule value.  These helpers answer *gaps*.

Layer convention
----------------
Every helper here emits :data:`~kicad_tools.router.clearance_kernel.ALL_LAYERS`.
``kct check``'s clearance rules resolve layer interaction *before* they ask for
a distance -- :meth:`ClearanceRule._check_layer` scans one copper layer at a
time and :meth:`ClearanceRule._collect_elements` only hands it copper present on
that layer -- so a second layer test inside the kernel would be dead weight at
best and a second, disagreeing opinion at worst.  Passing ``ALL_LAYERS`` makes
the kernel answer the geometry question it was asked and nothing else.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from kicad_tools.core.geometry import point_in_polygon as _core_point_in_polygon
from kicad_tools.core.geometry import rotate_pad_offset as _rotate_pad_offset
from kicad_tools.router.clearance_kernel import (
    ALL_LAYERS,
    CLEARANCE_EPSILON_MM,
    NO_INTERACTION,
    KEdge,
    KPad,
    KRing,
    KSegment,
    KShape,
    KVia,
    KZonePoly,
    clear,
    copper_gap,
    hole_gap,
    make_pad,
    pad_outline,
    ring_edge_crosses_ray,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

    from kicad_tools.schema.pcb import FilledOutlineRegion, Footprint, Pad, Segment, Via

__all__ = [
    "ALL_LAYERS",
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
    "filled_region_contains",
    "hole_gap",
    "inside_filled_outline_region",
    "min_gap_to_outline",
    "outline_shapes",
    "pad_center",
    "pad_outline",
    "pad_shape",
    "ring_edge_crosses_ray",
    "segment_shape",
    "via_shape",
    "zone_shape",
]


def segment_shape(seg: Segment) -> KSegment:
    """A routed track :class:`~kicad_tools.schema.pcb.Segment` as kernel copper."""
    return KSegment(
        x1=seg.start[0],
        y1=seg.start[1],
        x2=seg.end[0],
        y2=seg.end[1],
        width=seg.width,
        layer=ALL_LAYERS,
    )


def via_shape(via: Via) -> KVia:
    """A :class:`~kicad_tools.schema.pcb.Via` as kernel copper, hole included."""
    return KVia(x=via.position[0], y=via.position[1], diameter=via.size, drill=via.drill)


def pad_center(pad: Pad, footprint: Footprint) -> tuple[float, float]:
    """The pad's board-frame centre.

    KiCad's forward transform rotates the footprint-local offset by the
    **negated** orientation (:func:`kicad_tools.core.geometry.rotate_pad_offset`,
    pcbnew-verified in #3739), which is what
    ``validate/rules/clearance.py:_transform_pad_position`` already does; this is
    that function's body, lifted here so the kernel translation does not have to
    import back into the rule module.
    """
    rotated_x, rotated_y = _rotate_pad_offset(pad.position[0], pad.position[1], footprint.rotation)
    return footprint.position[0] + rotated_x, footprint.position[1] + rotated_y


def pad_shape(pad: Pad, footprint: Footprint) -> KPad:
    """A :class:`~kicad_tools.schema.pcb.Pad` as kernel copper.

    ``pad.size`` is the pad's **local** size and ``pad.rotation`` its
    **absolute** board-frame angle -- KiCad folds ``footprint.rotation`` into
    every pad's stored angle (issue #3902), so adding the footprint rotation a
    second time is the #5227 sign/rotation defect.  Those are exactly
    :func:`~kicad_tools.router.clearance_kernel.make_pad`'s own argument
    conventions, which is unsurprising: ``make_pad`` is the Phase 1b port of
    ``_pad_polygon``, whose branches these arguments feed.

    A plated through-hole pad is copper on every layer and carries its drill, so
    hole queries (:func:`~kicad_tools.router.clearance_kernel.hole_gap`) have
    something to read.  ``np_thru_hole`` keeps its drill too: it is a bare hole
    with pad-sized copper in the file, and the hole rules are the ones that care.
    """
    cx, cy = pad_center(pad, footprint)
    return make_pad(
        pad.shape,
        pad.size[0],
        pad.size[1],
        getattr(pad, "roundrect_rratio", 0.25),
        getattr(pad, "rotation", 0.0),
        cx,
        cy,
        ALL_LAYERS,
        pad.drill,
    )


def zone_shape(rings: list[list[tuple[float, float]]] | tuple[KRing, ...]) -> KZonePoly:
    """Filled pour copper as kernel copper.

    Takes the *already resolved* fill rings (outer ring first, interior rings
    after) because the two zone rules get theirs from different places -- a
    repaired shapely polygon in one case, raw ``filled_polygons`` in the other.
    Rings are closed here if the caller left them open, which is the convention
    ``KZonePoly`` documents.
    """
    closed: list[KRing] = []
    for ring in rings:
        points = tuple((float(x), float(y)) for x, y in ring)
        if len(points) < 3:
            continue
        if points[0] != points[-1]:
            points = (*points, points[0])
        closed.append(points)
    return KZonePoly(rings=tuple(closed), layer=ALL_LAYERS)


_EdgeBBox = tuple[float, float, float, float]
# (min_x, min_y, max_x, max_y) over one outline edge's vertices.

_EMPTY_EDGE_BBOX: _EdgeBBox = (math.inf, math.inf, -math.inf, -math.inf)
# An edge with no vertices answers NO_INTERACTION, so its lower bound is
# +inf: the pruning below then never evaluates it, which is exactly what the
# brute-force scan's ``inf < best`` comparison already did.

# Pruning margins (Issue #9751).  An edge is skipped when a lower bound on
# its ``copper_gap`` already reaches the running best, so the bound must stay
# *strictly below* anything the kernel's own floating-point chain can report
# (every kernel operation is exactly rounded, so its total error is a few
# ulps *of the distance value itself*; a 1e-12 relative shave dominates that
# ~1e-15-scale error, and the 1e-8 mm absolute term -- four orders below
# ``CLEARANCE_EPSILON_MM`` -- covers coordinate-scale cancellation in the
# near-touching regime where a relative shave alone would not).  Both margins
# only ever fail to *skip* an edge, never skip one that could have lowered
# the minimum, so the returned value is bit-identical to the brute-force
# scan's.
_PRUNE_MARGIN_REL = 1e-12
_PRUNE_MARGIN_ABS_MM = 1e-8

# Below this many edges the pure-Python pruning loop beats numpy's per-call
# array overhead; at or above it the vectorized bound pass wins (Issue #9751).
_VECTOR_MIN_EDGES = 64


def _edge_bbox(edge: KEdge) -> _EdgeBBox:
    """The bounding box of one outline edge's vertices.

    Two-point edges (what :func:`outline_shapes` builds) take the unrolled
    min/max path; longer polylines fall back to the generic scan.
    """
    points = edge.points
    if not points:
        return _EMPTY_EDGE_BBOX
    if len(points) == 2:
        (x1, y1), (x2, y2) = points
        return (
            x1 if x1 < x2 else x2,
            y1 if y1 < y2 else y2,
            x1 if x1 > x2 else x2,
            y1 if y1 > y2 else y2,
        )
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return (min(xs), min(ys), max(xs), max(ys))


class _IndexedOutline(tuple[KEdge, ...]):
    """``tuple[KEdge, ...]`` carrying a lazily built per-edge bbox index.

    Issue #9751: certified arc tessellation (#9689) multiplies the outline
    segment count by 100-400x, and ``EdgeClearanceRule`` asks
    :func:`min_gap_to_outline` once per copper item against the *same* edge
    tuple.  Subclassing the tuple (instead of changing the return type) keeps
    every existing caller -- indexing, iteration, equality, hashing -- byte
    compatible while letting the bbox index be computed once per rule
    invocation and reused across the whole copper loop.
    """

    edge_bboxes: tuple[_EdgeBBox, ...] | None = None
    _bbox_array: Any = None

    def bboxes(self) -> tuple[_EdgeBBox, ...]:
        cached = self.edge_bboxes
        if cached is None:
            cached = tuple(_edge_bbox(edge) for edge in self)
            self.edge_bboxes = cached
        return cached

    def bbox_array(self) -> Any:
        """The bboxes as one ``(n, 4)`` float64 array (numpy imported lazily)."""
        cached = self._bbox_array
        if cached is None:
            import numpy as np

            cached = np.asarray(self.bboxes(), dtype=np.float64)
            self._bbox_array = cached
        return cached


def _shape_query_bbox(shape: KShape) -> tuple[float, float, float, float, float] | None:
    """``(min_x, min_y, max_x, max_y, half_extent)`` for the pruning query.

    The box must cover the shape's *core* geometry -- the point set whose
    distance to the edge the kernel measures -- with ``half_extent`` the
    Minkowski term the kernel then subtracts (half width for a segment, half
    diameter for a via, corner radius for a pad, nothing for a pour).  The
    core of every kind lies inside its vertices' bounding box, so the
    bbox-to-bbox distance minus ``half_extent`` is a lower bound on the
    kernel's gap.  ``None`` for kinds with no core (degenerate pads, empty
    pours -- whose every gap is NO_INTERACTION anyway), for outline-vs-outline
    pairs, and for anything unrecognized: the caller then runs the plain
    brute-force scan, which is bit-identical by construction.
    """
    if isinstance(shape, KSegment):
        x1, x2 = shape.x1, shape.x2
        y1, y2 = shape.y1, shape.y2
        return (
            x1 if x1 < x2 else x2,
            y1 if y1 < y2 else y2,
            x1 if x1 > x2 else x2,
            y1 if y1 > y2 else y2,
            shape.width / 2.0,
        )
    if isinstance(shape, KVia):
        return (shape.x, shape.y, shape.x, shape.y, shape.diameter / 2.0)
    if isinstance(shape, KPad):
        if not shape.core:
            return None
        xs = [x for x, _ in shape.core]
        ys = [y for _, y in shape.core]
        return (min(xs), min(ys), max(xs), max(ys), shape.corner_radius)
    if isinstance(shape, KZonePoly):
        zone_xs: list[float] = []
        zone_ys: list[float] = []
        for ring in shape.rings:
            for x, y in ring:
                zone_xs.append(x)
                zone_ys.append(y)
        if not zone_xs:
            return None
        return (min(zone_xs), min(zone_ys), max(zone_xs), max(zone_ys), 0.0)
    return None


def _min_gap_vectorized(
    shape: KShape,
    outline: tuple[KEdge, ...],
    boxes: Any,
    query: tuple[float, float, float, float, float],
) -> float:
    """The dense-outline pruning pass: one vectorized bound, few exact calls.

    Every edge's lower bound is computed in C, the tightest one is evaluated
    exactly to seed ``best``, and only edges whose bound is still below that
    seed pay the pure-Python :func:`copper_gap` call.  Edges left out had
    ``bound >= seed >= final minimum``, so the minimum over the evaluated
    candidates equals the brute-force minimum value exactly.
    """
    import numpy as np

    qx0, qy0, qx1, qy1, extent = query
    dx = np.maximum(np.maximum(qx0 - boxes[:, 2], boxes[:, 0] - qx1), 0.0)
    dy = np.maximum(np.maximum(qy0 - boxes[:, 3], boxes[:, 1] - qy1), 0.0)
    lower = np.hypot(dx, dy)
    lower *= 1.0 - _PRUNE_MARGIN_REL
    lower -= extent + _PRUNE_MARGIN_ABS_MM

    best = NO_INTERACTION
    gap = copper_gap(shape, outline[int(np.argmin(lower))])
    if not math.isnan(gap) and gap < best:
        best = gap
    for i in np.flatnonzero(lower < best).tolist():
        gap = copper_gap(shape, outline[i])
        if gap < best:
            best = gap
    return best


def outline_shapes(
    outline_segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> tuple[KEdge, ...]:
    """The board outline as kernel edge shapes, one per outline segment.

    :class:`~kicad_tools.router.clearance_kernel.KEdge` is a *polyline* --
    consecutive vertices form its segments -- while
    :meth:`kicad_tools.schema.pcb.PCB.get_board_outline_segments` returns an
    unordered bag of independent segments (arcs already flattened).  Chaining
    those into one polyline would invent edges between unrelated vertices, so
    each becomes its own two-point ``KEdge`` and callers take the minimum.  That
    is the same thing ``EdgeClearanceRule._min_distance_to_outline`` did, with
    the per-segment distance now coming from the kernel.

    The return value is a ``tuple[KEdge, ...]`` subclass carrying the
    per-edge bounding-box index :func:`min_gap_to_outline` prunes with (see
    :class:`_IndexedOutline`): identical to the plain tuple for every other
    use, while dense tessellated arc outlines (Issue #9751) get their
    per-edge bounding boxes computed once instead of once per copper item.
    """
    return _IndexedOutline(KEdge(points=(start, end)) for start, end in outline_segments)


def min_gap_to_outline(shape: KShape, outline: tuple[KEdge, ...]) -> float:
    """Smallest kernel copper gap between ``shape`` and any outline segment.

    :data:`~kicad_tools.router.clearance_kernel.NO_INTERACTION` when the outline
    is empty, which callers already treat as "no outline, nothing to check".

    Dense tessellated arc outlines (Issue #9751) made the one-``copper_gap``
    -call-per-segment scan the dominant ``EdgeClearanceRule`` cost, so the scan
    prunes: an edge is skipped once a conservative lower bound on its gap --
    the bounding-box-to-bounding-box distance minus the item's half extent,
    shaved by the :data:`_PRUNE_MARGIN_REL` / :data:`_PRUNE_MARGIN_ABS_MM`
    margins -- reaches the running best.  A skipped edge provably cannot lower
    the minimum, so the returned value is bit-identical to the brute-force
    scan's.  Dense outlines (``len(outline) >=`` :data:`_VECTOR_MIN_EDGES`)
    take one vectorized bound pass instead of the per-edge Python loop.
    """
    best = NO_INTERACTION
    query = _shape_query_bbox(shape)
    if query is None:
        for edge in outline:
            gap = copper_gap(shape, edge)
            if gap < best:
                best = gap
        return best

    qx0, qy0, qx1, qy1, extent = query
    if isinstance(outline, _IndexedOutline):
        if len(outline) >= _VECTOR_MIN_EDGES:
            return _min_gap_vectorized(shape, outline, outline.bbox_array(), query)
        bboxes = outline.bboxes()
    else:
        bboxes = tuple(_edge_bbox(edge) for edge in outline)

    # An edge is skippable once even its loosest bound (the larger axis
    # separation, before any sqrt) reaches ``best + half extent + margin``.
    reach = NO_INTERACTION
    for edge, (ex0, ey0, ex1, ey1) in zip(outline, bboxes, strict=True):
        dx = qx0 - ex1
        d = ex0 - qx1
        if d > dx:
            dx = d
        if dx >= reach:
            continue
        dy = qy0 - ey1
        d = ey0 - qy1
        if d > dy:
            dy = d
        if dy >= reach:
            continue
        if (
            dx > 0.0
            and dy > 0.0
            and math.hypot(dx, dy) * (1.0 - _PRUNE_MARGIN_REL) - _PRUNE_MARGIN_ABS_MM >= reach
        ):
            continue
        gap = copper_gap(shape, edge)
        if gap < best:
            best = gap
            reach = best + extent + _PRUNE_MARGIN_ABS_MM
    return best


def filled_region_contains(region: FilledOutlineRegion, x: float, y: float) -> bool:
    """Point-in-filled-region test for an Edge.Cuts cut-out (Issue #9690).

    A circle region is a centre/radius test; a poly/rect region is the
    shared ray-cast point-in-polygon with the boundary counted as inside --
    copper exactly on the cut-out boundary is at gap 0, matching the
    ``actual 0.0 mm`` kicad-cli reports there.
    """
    if region.shape_type == "circle":
        if region.center is None or region.radius is None:
            return False
        return math.hypot(x - region.center[0], y - region.center[1]) <= region.radius
    return _core_point_in_polygon(x, y, region.ring, boundary_inside=True)


def inside_filled_outline_region(
    regions: Iterable[FilledOutlineRegion], x: float, y: float
) -> FilledOutlineRegion | None:
    """The first filled Edge.Cuts region containing ``(x, y)``, else ``None``.

    Lets an edge-clearance caller attribute a gap-0 finding to the specific
    cut-out shape that caused it.
    """
    for region in regions:
        if filled_region_contains(region, x, y):
            return region
    return None
