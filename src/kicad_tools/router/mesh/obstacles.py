"""Obstacle model shared by the mesh substrate and the 45-fit (issue #4268).

The #3906 lesson is *consult the same obstacle model* the mesh was built from
when validating the octilinear legs.  This module is that single source of
truth: the predicate the 45-fit rejects bulging doglegs against.

Pre-inflated obstacles are still handled by obstacle inflation (Minkowski
growth by the agent radius = half-trace + clearance).  Growing those obstacles
and planning for a point centreline is mathematically equivalent to the
"agent-radius portal narrowing" the ADR names, and composes cleanly with the
poly2tri holes the navmesh is built from.

Epic #5509 Phase 3e switched :meth:`ObstacleModel.is_clear` -- consumer group
10 -- onto the shared exact-geometry clearance kernel.  It no longer carries
its own rectangle, polygon or ray-cast arithmetic: every branch is a reading
taken from the Phase 1b kernel through :mod:`.kernel_adapter`, which is this
package's single import site for it.

That switch also closed the one place where inflation was a strictly *worse*
model than a measurement, quantified against kicad-cli in
``docs/clearance-conformance.md``'s group-10 row: a **foreign pad** now arrives
as a :class:`~kicad_tools.router.primitives.Pad` (``pads``) and is measured
exactly, instead of as its bounding box grown by the agent radius.  That box is
strictly larger than the copper for every circle, oval, roundrect or rotated
pad, so it refused legs ``kicad-cli pcb drc`` finds clean -- the #5410 failure
mode the epic exists to remove.

``keepouts`` (rectangles a caller already grew) and ``pours`` (pour outlines
and committed-trace capsules) keep their touch semantics exactly: those
obstacles really are handed over pre-inflated, so a clearance term on top of
them would be a second model rather than a port.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .geometry import Pt
from .kernel_adapter import (
    Box,
    MeshOutline,
    MeshPad,
    MeshRegion,
    copper_leg,
    leg,
    leg_clears,
    leg_touches,
    outline_of,
    pad_of,
    pad_reach_box,
    region_box,
    region_of_poly,
    region_of_rect,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..primitives import Pad

Rect = tuple[float, float, float, float]  # (xmin, ymin, xmax, ymax)


def rect_contains(r: Rect, p: Pt) -> bool:
    """True if point ``p`` is inside rectangle ``r`` (inclusive)."""
    return r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]


def rects_overlap(a: Rect, b: Rect) -> bool:
    """True if two AABBs overlap (touching edges count)."""
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def merge_overlapping(rects: list[Rect]) -> list[Rect]:
    """Union-find cluster overlapping rects into their bounding boxes.

    poly2tri holes must be disjoint and simple; a dense pin-field produces
    overlapping inflated keep-outs.  Merging each overlap-cluster into its
    bounding box keeps the holes disjoint while staying *conservative*
    (the merged keep-out only ever grows the avoided region).

    Epic #5509 Phase 3e retired this helper's one production caller: the mesh
    fit no longer inflates pads into rectangles at all (see the module
    docstring).  It is kept rather than deleted because ``keepouts`` remains a
    supported model input for a caller that *does* hold pre-inflated
    rectangles, and because the epic assigns deletion of the arithmetic each
    phase supersedes to its own Phase 4 pass rather than to the switching
    phase.
    """
    n = len(rects)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    # Iterate to a fixpoint: merging can create new overlaps.
    changed = True
    while changed:
        changed = False
        boxes = _cluster_boxes(rects, parent, find, n)
        roots = list(boxes.keys())
        for a in range(len(roots)):
            for b in range(a + 1, len(roots)):
                ra, rb = roots[a], roots[b]
                if find(ra) != find(rb) and rects_overlap(boxes[ra], boxes[rb]):
                    union(ra, rb)
                    changed = True
    return list(_cluster_boxes(rects, parent, find, n).values())


def _cluster_boxes(rects: list[Rect], parent: list[int], find, n: int) -> dict[int, Rect]:
    boxes: dict[int, Rect] = {}
    for i in range(n):
        r = find(i)
        if r not in boxes:
            boxes[r] = rects[i]
        else:
            cur = boxes[r]
            boxes[r] = (
                min(cur[0], rects[i][0]),
                min(cur[1], rects[i][1]),
                max(cur[2], rects[i][2]),
                max(cur[3], rects[i][3]),
            )
    return boxes


class ObstacleModel:
    """Board outline, foreign pad copper, keep-out rectangles and pour polygons.

    Issue #4269 (mesh-router P2) adds ``pours`` -- filled-copper zone outlines
    a signal leg must clear, the polygon analogue of the rectangular pad
    keep-outs.  A leg entering a pour is a short to the pour net, so the 45-fit
    declines it exactly as it does a leg entering a pad keep-out.  Pours default
    to empty, so every P1 call site (`ObstacleModel(outline, keepouts)`) is
    byte-identical.

    Epic #5509 Phase 3e adds ``pads`` and ``edge_clearance``, the two obstacles
    the model **measures** rather than inflates (see the module docstring and
    :mod:`.kernel_adapter`).  Both default to "absent", so a call site that
    passes neither keeps exactly the behaviour it had.
    """

    def __init__(
        self,
        outline: list[Pt],
        keepouts: list[Rect],
        pours: list[list[Pt]] | None = None,
        *,
        fixed_fills=None,
        layer: int = 0,
        half: float = 0.0,
        clearance: float = 0.0,
        pads: Sequence[Pad] = (),
        edge_clearance: float = 0.0,
    ) -> None:
        """Build an obstacle model.

        Args:
            outline: Board outline, closed but written WITHOUT a repeated final
                vertex.  Empty means "no containment restriction".
            keepouts: Pre-inflated keep-out rectangles, touch-tested.
            pours: Pour outlines and committed-trace capsules, touch-tested.
            fixed_fills: ``FixedFillObstacles`` for preserved copper, or
                ``None``.  Already kernel-backed by delegation (Phase 3f).
            layer: Routing-graph layer index; consulted by ``fixed_fills``.
            half: The candidate trace's copper half-width in mm.
            clearance: The copper-to-copper requirement applied to ``pads``.
            pads: Foreign pads, measured exactly against the leg's copper.
            edge_clearance: Copper-to-board-edge requirement in mm.  ``0.0``
                (the default) means the caller resolved no board-edge rule and
                the outline branch stays the pure containment test it was
                before Epic #5509 Phase 3e.
        """
        self.outline = outline
        self.keepouts = keepouts
        self.pours = pours or []
        self.fixed_fills = fixed_fills
        self.layer = layer
        self.half = half
        self.clearance = clearance
        self.pads = list(pads)
        self.edge_clearance = edge_clearance
        self._kernel: _KernelShapes | None = None

    def _shapes(self) -> _KernelShapes:
        """The obstacles projected onto kernel shapes, built once per model.

        A model is built once per net (``MeshPathfinder._route_with_portals``)
        or once per net per layer (``_route_layered``) and then queried by every
        A* portal test and every leg of the octilinear fit, so the projection is
        cached rather than repeated per call.  Lazy rather than eager because a
        model whose route is declined before the fit -- no corridor, no navmesh
        -- never asks a single clearance question.
        """
        cached = self._kernel
        if cached is None:
            # Broad-phase reach: a leg whose centreline bounding box misses a
            # pad's box grown by this much provably clears that pad.
            reach = self.half + self.clearance
            cached = _KernelShapes(
                outline=outline_of(self.outline),
                keepouts=tuple(
                    (region, region_box(region))
                    for region in (region_of_rect(r[0], r[1], r[2], r[3]) for r in self.keepouts)
                ),
                pours=tuple(
                    (region, region_box(region))
                    for region in (region_of_poly(poly) for poly in self.pours)
                    if region is not None
                ),
                pads=tuple(
                    (shape, pad_reach_box(shape, reach))
                    for shape in (pad_of(pad) for pad in self.pads)
                    if shape is not None
                ),
            )
            self._kernel = cached
        return cached

    def is_clear(self, a: Pt, b: Pt) -> bool:
        """True if straight leg ``a-b`` is inside the board and clears obstacles.

        This is the exact predicate the 45-fit checks each dogleg leg against
        (the generalised ``subgrid.py:1004-1030`` per-leg obstacle consult).

        Every branch below takes its answer from the shared clearance kernel
        (Epic #5509 Phase 3e) -- see :mod:`.kernel_adapter`.  Two legs are built
        because the model carries two kinds of obstacle: a **zero-width** one
        for the pre-inflated rectangles and capsules (whose inflation has
        already absorbed the trace's half width) and a **copper** one carrying
        the real width for the pads and the board edge, which are measured
        un-inflated.  Passing the copper leg to a pre-inflated obstacle would
        count the half width twice.

        Args:
            a: Leg start.
            b: Leg end.

        Returns:
            True when the leg may be emitted.
        """
        if self.fixed_fills and not self.fixed_fills.segment_clear(
            a,
            b,
            self.layer,
            self.half,
            self.clearance,
        ):
            return False

        shapes = self._shapes()
        outline = shapes.outline
        if outline is not None:
            if not (outline.contains(a) and outline.contains(b)):
                return False
            if not outline.clears_edge(copper_leg(a, b, self.half), self.edge_clearance):
                return False

        # Broad phase for every remaining branch (see ``kernel_adapter``'s
        # ``region_box`` / ``pad_reach_box``): an obstacle the leg's own
        # bounding box cannot reach is skipped without a kernel query.
        # Conservative by construction, so it can never move a verdict.
        lo_x, hi_x = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
        lo_y, hi_y = (a[1], b[1]) if a[1] <= b[1] else (b[1], a[1])

        if shapes.pads:
            copper = copper_leg(a, b, self.half)
            for pad, (px0, py0, px1, py1) in shapes.pads:
                if hi_x < px0 or lo_x > px1 or hi_y < py0 or lo_y > py1:
                    continue
                if not leg_clears(copper, pad, self.clearance):
                    return False

        if shapes.keepouts or shapes.pours:
            candidate = leg(a, b)
            for region, (rx0, ry0, rx1, ry1) in shapes.keepouts:
                if hi_x < rx0 or lo_x > rx1 or hi_y < ry0 or lo_y > ry1:
                    continue
                if leg_touches(candidate, region):
                    return False
            for region, (rx0, ry0, rx1, ry1) in shapes.pours:
                if hi_x < rx0 or lo_x > rx1 or hi_y < ry0 or lo_y > ry1:
                    continue
                if leg_touches(candidate, region):
                    return False
        return True


class _KernelShapes:
    """One :class:`ObstacleModel`'s obstacles, projected into kernel shapes.

    A plain cache built by :meth:`ObstacleModel._shapes`; see
    :mod:`.kernel_adapter` for the projection itself.  ``outline`` is ``None``
    when the model carries no usable outline, which is the case the original
    ``if self.outline and ...`` guard covered: no outline means no containment
    restriction, not "nothing is inside the board".
    """

    __slots__ = ("keepouts", "outline", "pads", "pours")

    def __init__(
        self,
        outline: MeshOutline | None,
        keepouts: tuple[tuple[MeshRegion, Box], ...],
        pours: tuple[tuple[MeshRegion, Box], ...],
        pads: tuple[tuple[MeshPad, Box], ...] = (),
    ) -> None:
        self.outline = outline
        self.keepouts = keepouts
        self.pours = pours
        self.pads = pads
