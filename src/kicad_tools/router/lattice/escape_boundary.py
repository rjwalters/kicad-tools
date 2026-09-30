"""Physical board-boundary checks for routed copper.

Epic #5509 Phase 3d: :meth:`EscapeBoundary.segment_clear` is a clearance
predicate -- copper against the board outline -- so its distance arithmetic
now comes from the shared exact-geometry kernel through
:mod:`.kernel_adapter`, which models each outline edge as the kernel's
``KEdge``.  The even/odd containment test below is a *material* question
rather than a clearance one and keeps its own ray cast.
"""

from __future__ import annotations

import math

from shapely.geometry import MultiLineString  # type: ignore[import-untyped]
from shapely.ops import polygonize_full  # type: ignore[import-untyped]

from . import kernel_adapter as ka
from .geometry import Pt


class EscapeBoundary:
    """Closed edge contours with even/odd material and certified curve error.

    Invalid/open outlines reject copper; they never become a permissive
    bounding-box substitute.
    """

    def __init__(self, edges: list[tuple[Pt, Pt]], clearance: float) -> None:
        self.edges = tuple(edges)
        # The outline is static, so its kernel shapes are built once here
        # rather than per query -- an outline edge never moves.
        self._kernel_edges = tuple(ka.outline_edge(c, d) for c, d in self.edges)
        error = float(getattr(edges, "max_error_mm", 0.0))
        self.margin = clearance + error
        self.valid = False
        if (
            not edges
            or not math.isfinite(clearance)
            or not math.isfinite(error)
            or clearance < 0
            or error < 0
            or any(not math.isfinite(v) for edge in edges for point in edge for v in point)
        ):
            return
        lines = MultiLineString(edges)
        if not lines.is_simple:
            return
        polygons, cuts, dangles, invalid = polygonize_full(lines)
        self.valid = not polygons.is_empty and all(
            geometry.is_empty for geometry in (cuts, dangles, invalid)
        )

    def _inside(self, point: Pt) -> bool:
        x, y = point
        crossings = 0
        for (ax, ay), (bx, by) in self.edges:
            if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
                crossings += 1
        return bool(crossings % 2)

    def segment_clear(self, a: Pt, b: Pt, half_width: float) -> bool:
        if not self.valid or not self._inside(a) or not self._inside(b):
            return False
        # The kernel subtracts the copper half-width itself, so the
        # requirement here is the margin alone -- the same comparison the
        # pre-kernel ``seg_seg_dist(...) >= half_width + margin`` form made.
        copper = ka.trace(a, b, half_width)
        return all(ka.satisfies(ka.gap(copper, edge), self.margin) for edge in self._kernel_edges)
