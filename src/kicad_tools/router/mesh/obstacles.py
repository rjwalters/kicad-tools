"""Obstacle model shared by the mesh substrate and the 45-fit (issue #4268).

The #3906 lesson is *consult the same obstacle model* the mesh was built from
when validating the octilinear legs.  This module is that single source of
truth: the inflated pad keep-out rectangles here are BOTH the poly2tri mesh
holes and the clearance predicate the 45-fit rejects bulging doglegs against.

Clearance is handled by obstacle inflation (Minkowski growth by the agent
radius = half-trace + clearance).  Growing the obstacles and planning for a
point centreline is mathematically equivalent to the "agent-radius portal
narrowing" the ADR names, and composes cleanly with poly2tri holes.
"""

from __future__ import annotations

from .geometry import (
    Pt,
    point_in_polygon,
    segment_intersects_polygon,
    segment_intersects_rect,
)

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
    """Board outline plus inflated pad keep-out rectangles and pour polygons.

    Issue #4269 (mesh-router P2) adds ``pours`` -- filled-copper zone outlines
    a signal leg must clear, the polygon analogue of the rectangular pad
    keep-outs.  A leg entering a pour is a short to the pour net, so the 45-fit
    declines it exactly as it does a leg entering a pad keep-out.  Pours default
    to empty, so every P1 call site (`ObstacleModel(outline, keepouts)`) is
    byte-identical.
    """

    def __init__(
        self,
        outline: list[Pt],
        keepouts: list[Rect],
        pours: list[list[Pt]] | None = None,
    ) -> None:
        self.outline = outline
        self.keepouts = keepouts
        self.pours = pours or []

    def is_clear(self, a: Pt, b: Pt) -> bool:
        """True if straight leg ``a-b`` is inside the board and clears keep-outs.

        This is the exact predicate the 45-fit checks each dogleg leg against
        (the generalised ``subgrid.py:1004-1030`` per-leg obstacle consult).
        """
        if self.outline and not (
            point_in_polygon(a, self.outline) and point_in_polygon(b, self.outline)
        ):
            return False
        if any(segment_intersects_rect(a, b, r[0], r[1], r[2], r[3]) for r in self.keepouts):
            return False
        return not any(segment_intersects_polygon(a, b, poly) for poly in self.pours)
