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

from typing import TYPE_CHECKING

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
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.schema.pcb import Footprint, Pad, Segment, Via

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
    "hole_gap",
    "min_gap_to_outline",
    "outline_shapes",
    "pad_center",
    "pad_outline",
    "pad_shape",
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
    """
    return tuple(KEdge(points=(start, end)) for start, end in outline_segments)


def min_gap_to_outline(shape: KShape, outline: tuple[KEdge, ...]) -> float:
    """Smallest kernel copper gap between ``shape`` and any outline segment.

    :data:`~kicad_tools.router.clearance_kernel.NO_INTERACTION` when the outline
    is empty, which callers already treat as "no outline, nothing to check".
    """
    best = NO_INTERACTION
    for edge in outline:
        gap = copper_gap(shape, edge)
        if gap < best:
            best = gap
    return best
