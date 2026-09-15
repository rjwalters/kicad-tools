"""Physical board-boundary checks for copper-overlap pad attachments."""

from __future__ import annotations

import math

from shapely.geometry import MultiLineString  # type: ignore[import-untyped]
from shapely.ops import polygonize_full  # type: ignore[import-untyped]

from .geometry import Pt, seg_seg_dist


class EscapeBoundary:
    """Closed edge contours with even/odd material and certified curve error.

    This guard only admits the new attachment fallback. Invalid/open outlines
    leave it disabled; they never become a permissive bounding-box substitute.
    """

    def __init__(self, edges: list[tuple[Pt, Pt]], clearance: float) -> None:
        self.edges = tuple(edges)
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
        required = half_width + self.margin
        return all(seg_seg_dist(a, b, c, d) >= required - 1e-9 for c, d in self.edges)
