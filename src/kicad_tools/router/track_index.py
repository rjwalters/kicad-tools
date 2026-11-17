"""Uniform-grid spatial index over track segments for clearance queries.

Issue #5240: the stitcher's via-placement candidate ladders
(:func:`kicad_tools.cli.stitch_cmd.calculate_via_position`,
:func:`~kicad_tools.cli.stitch_cmd.calculate_dogleg_via_position`,
:func:`~kicad_tools.cli.stitch_cmd.calculate_extended_escape_position`)
re-scan *every* foreign-net track segment on the board for *every*
candidate position they try.  On board 06 that is ~6.4k segments against
up to ~1.1k candidates per pad, for 38 pads -- the measured dominant cost
of the recipe's "Pad-aware post-route stitching" phase (~175 s of the
878 s re-route in CI run 35681749300).

This index answers "which segments could possibly be within ``radius`` of
this query box?" from a small number of grid cells instead.  It is a
**conservative superset filter**, never a different predicate:

* Each segment is binned by the axis-aligned bounding box of its
  *centerline*, grown by its own half-width ``w/2`` (its copper extent).
* A query grows the caller's box by ``radius`` and returns every segment
  binned into an overlapping cell.

Correctness argument (why the verdict cannot change).  Let ``B`` be the
centerline bbox of a segment, ``h = w/2``, and ``E = B`` grown by ``h``.
For a query box ``Q`` and threshold ``radius``, suppose the true
geometric distance satisfies ``dist(Q, segment) < radius + h`` -- i.e. the
caller's per-segment test ``dist < radius + seg.width / 2`` could fire.
Along each axis the box-to-box overshoot ``o`` between ``Q`` and ``B``
obeys ``o <= dist(Q, B) <= dist(Q, segment) < radius + h``, so the
overshoot between ``Q`` and ``E`` is ``max(0, o - h) < radius``.  Both
axes overlap once ``Q`` is grown by ``radius``, so the grown query box
intersects ``E`` and the two share at least one grid cell.  Every segment
the caller's exact test would reject is therefore returned; the caller
still runs its unchanged exact distance test on what comes back.

The index is deliberately *not* wired into the exact tests themselves --
callers keep their own thresholds, tolerances and short-circuit order, so
the boolean they compute is bit-for-bit the boolean they computed before.
"""

from __future__ import annotations

import math
from typing import Generic, Protocol, TypeVar

__all__ = [
    "TRACK_INDEX_MIN_SEGMENTS",
    "TrackSegmentLike",
    "TrackSpatialIndex",
    "build_track_index",
]


class TrackSegmentLike(Protocol):
    """Structural type for an indexable track segment.

    Matches ``kicad_tools.cli.stitch_cmd.TrackSegment`` and the router's
    segment types: only the centerline endpoints and the trace width are
    read.
    """

    start_x: float
    start_y: float
    end_x: float
    end_y: float
    width: float


#: Grid cell size in mm.  Mirrors the 2 mm bucket
#: :class:`kicad_tools.router.component_hole_index.ComponentHoleIndex`
#: already uses for drill bins, which is a good match for the sub-mm
#: clearance radii these queries use against typical 0.1-1 mm traces.
_CELL_MM = 2.0

#: A segment whose copper bbox covers more cells than this is kept in an
#: "oversize" list that every query returns unconditionally, rather than
#: replicated into a long cell list.  Board-spanning plane stubs are rare;
#: paying a linear scan over just those beats inflating the bins.
_MAX_CELLS_PER_SEGMENT = 64

#: Invariant element type for the index: callers get their own concrete
#: segment class back out of a query, not the protocol.
_SegT = TypeVar("_SegT", bound=TrackSegmentLike)

#: Below this many segments the linear scan is cheaper than building an
#: index, so :func:`build_track_index` returns ``None`` and callers keep
#: their original loop verbatim.
TRACK_INDEX_MIN_SEGMENTS = 64


class TrackSpatialIndex(Generic[_SegT]):
    """Bin track segments into 2 mm cells for radius queries.

    Construction is ``O(n)`` in segments (plus the cells each covers);
    :meth:`near_box` is ``O(cells touched + segments in them)``.
    """

    __slots__ = ("_cell", "_cells", "_oversize", "segments")

    def __init__(
        self,
        segments: list[_SegT],
        cell: float = _CELL_MM,
        max_cells_per_segment: int = _MAX_CELLS_PER_SEGMENT,
    ) -> None:
        self.segments: list[_SegT] = segments
        self._cell = cell
        self._cells: dict[tuple[int, int], list[int]] = {}
        self._oversize: list[int] = []

        floor = math.floor
        cells = self._cells
        for i, seg in enumerate(segments):
            half = seg.width / 2.0
            sx, ex = seg.start_x, seg.end_x
            sy, ey = seg.start_y, seg.end_y
            cx0 = int(floor(((sx if sx < ex else ex) - half) / cell))
            cx1 = int(floor(((ex if sx < ex else sx) + half) / cell))
            cy0 = int(floor(((sy if sy < ey else ey) - half) / cell))
            cy1 = int(floor(((ey if sy < ey else sy) + half) / cell))
            if (cx1 - cx0 + 1) * (cy1 - cy0 + 1) > max_cells_per_segment:
                self._oversize.append(i)
                continue
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    bucket = cells.get((cx, cy))
                    if bucket is None:
                        cells[(cx, cy)] = [i]
                    else:
                        bucket.append(i)

    def near_box(
        self,
        min_x: float,
        min_y: float,
        max_x: float,
        max_y: float,
        radius: float,
    ) -> list[_SegT]:
        """Return a superset of the segments within ``radius`` of the box.

        ``radius`` must exclude the segments' own half-widths -- those are
        already folded into the bins (see the module docstring).  The
        returned list is in ascending insertion order, so a caller that
        short-circuits on the first hit sees the same segment ordering
        (restricted to the survivors) as the original full scan.
        """
        cell = self._cell
        cells = self._cells
        cx0 = int(math.floor((min_x - radius) / cell))
        cx1 = int(math.floor((max_x + radius) / cell))
        cy0 = int(math.floor((min_y - radius) / cell))
        cy1 = int(math.floor((max_y + radius) / cell))

        if cx0 == cx1 and cy0 == cy1:
            hits = cells.get((cx0, cy0))
            if not self._oversize:
                if hits is None:
                    return []
                segments = self.segments
                return [segments[i] for i in hits]
            picked = list(hits) if hits is not None else []
            picked.extend(self._oversize)
            picked.sort()
        else:
            seen: set[int] = set()
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    hits = cells.get((cx, cy))
                    if hits is not None:
                        seen.update(hits)
            if not seen and not self._oversize:
                return []
            seen.update(self._oversize)
            picked = sorted(seen)

        segments = self.segments
        return [segments[i] for i in picked]

    def near_point(self, x: float, y: float, radius: float) -> list[_SegT]:
        """Return a superset of the segments within ``radius`` of ``(x, y)``."""
        return self.near_box(x, y, x, y, radius)

    def near_segment(
        self,
        sx: float,
        sy: float,
        ex: float,
        ey: float,
        radius: float,
    ) -> list[_SegT]:
        """Return a superset of the segments within ``radius`` of a segment."""
        return self.near_box(
            sx if sx < ex else ex,
            sy if sy < ey else ey,
            ex if sx < ex else sx,
            ey if sy < ey else sy,
            radius,
        )


def build_track_index(
    segments: list[_SegT] | None,
    min_segments: int = TRACK_INDEX_MIN_SEGMENTS,
) -> TrackSpatialIndex[_SegT] | None:
    """Build an index for ``segments``, or ``None`` when a scan is cheaper.

    ``None`` means "keep your original linear scan": callers must treat it
    as a pure fast-path opt-out, so small boards and short obstacle lists
    execute exactly the code they did before this index existed.
    """
    if not segments or len(segments) < min_segments:
        return None
    return TrackSpatialIndex(segments)
