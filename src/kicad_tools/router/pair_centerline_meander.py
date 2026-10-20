"""Coupling-preserving meanders for differential pairs (Issue #6675).

A lattice-routed coupled pair is emitted as two offset curves of one fat
centreline (#4270): the P and N rails are parallel at a constant pitch, but
their breakpoints differ at every miter.  Reflecting a P-side serpentine onto
an N host segment (the Phase 2F mirror strategy in
:mod:`~kicad_tools.router.match_group_tuning`) therefore never lands on the N
host's endpoints, and the #4984 splice guard correctly refuses it.

This module builds the meander the other way round: it puts a rectangular
serpentine on the pair **centreline** of a *coupled span* (a stretch where
both rails are straight, parallel and on the same layer) and re-offsets it to
both rails with mitered corners.  For 90-degree bumps the two offset rails
gain exactly the same length (``2 * amplitude`` per bump -- the outer rail's
longer top is paid back by its shorter base, and vice versa), and the rails
stay at the original pitch everywhere, so the pair keeps its gap and stays
coupled.

It is deliberately free of match-group policy so the intra-pair skew tuner
(Epic #6441 Phase 5, #6447) can reuse the same primitive:

* :func:`find_coupled_spans` -- rank the straight, parallel, same-layer
  P/N host pairs of a pair's routes.
* :func:`coupled_segment_ids` -- identities of every segment that is part of
  a coupled span (callers use it to keep single-ended meanders off coupled
  copper).
* :func:`centerline_meander` -- the replacement segments for both hosts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Route, Segment


#: Largest |sin(angle)| between two host directions that still counts as
#: parallel.  Board coordinates are written to 1e-4 mm, so a 45-degree rail a
#: few mm long can be off-parallel by ~1e-4 rad; 2e-3 (~0.1 deg) absorbs that
#: while rejecting any genuinely converging pair of segments.
PARALLEL_SIN_TOLERANCE: float = 2e-3


@dataclass(frozen=True)
class CoupledSpan:
    """A straight, parallel, same-layer P/N host pair.

    Coordinates are expressed in the P host's frame: ``s`` runs along the
    unit direction ``(ux, uy)`` from the P host's first endpoint, ``t`` along
    the left normal ``(nx, ny) = (-uy, ux)``.

    Attributes:
        p_index: Index of the P host in ``p_route.segments``.
        n_index: Index of the N host in ``n_route.segments``.
        ux, uy: Unit direction of the P host (snapped to the 8-direction set
            when the host is 45-aligned).
        nx, ny: Left normal of ``(ux, uy)``.
        pitch_mm: Signed centre-to-centre offset of the N rail from the P
            rail along ``(nx, ny)``.
        s_lo, s_hi: The interval of ``s`` covered by both hosts.
    """

    p_index: int
    n_index: int
    ux: float
    uy: float
    nx: float
    ny: float
    pitch_mm: float
    s_lo: float
    s_hi: float

    @property
    def overlap_mm(self) -> float:
        return self.s_hi - self.s_lo


@dataclass
class CenterlineMeander:
    """Replacement geometry for both hosts of a :class:`CoupledSpan`.

    ``p_segments`` replaces ``p_route.segments[span.p_index]`` and runs from
    that host's ``(x1, y1)`` to its ``(x2, y2)``; ``n_segments`` likewise for
    the N host.  Both rails gain ``added_length_mm`` of copper.
    """

    p_segments: list[Segment]
    n_segments: list[Segment]
    added_length_mm: float
    loops: int
    amplitude_mm: float
    side: int


def _seg_len(seg: Segment) -> float:
    return math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)


def _span_between(
    p_index: int,
    p_seg: Segment,
    n_index: int,
    n_seg: Segment,
    max_edge_gap_mm: float,
) -> CoupledSpan | None:
    """Return the coupled span of two host segments, or ``None``."""
    from .quantize import is_45_aligned, snap_direction_8

    if p_seg.layer != n_seg.layer:
        return None
    lp = _seg_len(p_seg)
    ln = _seg_len(n_seg)
    if lp <= 0.0 or ln <= 0.0:
        return None
    dx, dy = p_seg.x2 - p_seg.x1, p_seg.y2 - p_seg.y1
    if is_45_aligned(dx, dy):
        ux, uy = snap_direction_8(dx, dy)
    else:
        ux, uy = dx / lp, dy / lp
    vx, vy = (n_seg.x2 - n_seg.x1) / ln, (n_seg.y2 - n_seg.y1) / ln
    if abs(ux * vy - uy * vx) > PARALLEL_SIN_TOLERANCE:
        return None
    nx, ny = -uy, ux
    # Pitch from the N host's midpoint (robust to 1e-4 coordinate rounding).
    mx = (n_seg.x1 + n_seg.x2) / 2.0 - p_seg.x1
    my = (n_seg.y1 + n_seg.y2) / 2.0 - p_seg.y1
    pitch = mx * nx + my * ny
    width = (p_seg.width + n_seg.width) / 2.0
    if abs(pitch) - width > max_edge_gap_mm + 1e-6 or abs(pitch) <= width * 0.5:
        return None
    s_n1 = (n_seg.x1 - p_seg.x1) * ux + (n_seg.y1 - p_seg.y1) * uy
    s_n2 = (n_seg.x2 - p_seg.x1) * ux + (n_seg.y2 - p_seg.y1) * uy
    s_p2 = (p_seg.x2 - p_seg.x1) * ux + (p_seg.y2 - p_seg.y1) * uy
    lo = max(0.0, min(s_n1, s_n2))
    hi = min(s_p2, max(s_n1, s_n2))
    if hi <= lo:
        return None
    return CoupledSpan(p_index, n_index, ux, uy, nx, ny, pitch, lo, hi)


def find_coupled_spans(
    p_route: Route,
    n_route: Route,
    *,
    max_edge_gap_mm: float,
    min_overlap_mm: float = 0.0,
    fixed_segment_ids: set[int] | None = None,
) -> list[CoupledSpan]:
    """Return every coupled P/N host pair, longest overlap first.

    A pair of segments is a coupled span when both are on the same layer,
    parallel (within :data:`PARALLEL_SIN_TOLERANCE`), their edge-to-edge gap
    is at most ``max_edge_gap_mm``, and their projections overlap by more
    than ``min_overlap_mm``.  Segments whose ``id()`` is in
    ``fixed_segment_ids`` (fixed escapes) are never hosts.

    Ties break on ``(p_index, n_index)`` so the order is deterministic.
    """
    fixed = fixed_segment_ids or set()
    spans: list[CoupledSpan] = []
    for i, p_seg in enumerate(p_route.segments):
        if id(p_seg) in fixed:
            continue
        for j, n_seg in enumerate(n_route.segments):
            if id(n_seg) in fixed:
                continue
            span = _span_between(i, p_seg, j, n_seg, max_edge_gap_mm)
            if span is not None and span.overlap_mm > min_overlap_mm:
                spans.append(span)
    spans.sort(key=lambda sp: (-sp.overlap_mm, sp.p_index, sp.n_index))
    return spans


def coupled_segment_ids(
    p_route: Route,
    n_route: Route,
    *,
    max_edge_gap_mm: float,
) -> set[int]:
    """``id()`` of every P or N segment that belongs to some coupled span."""
    ids: set[int] = set()
    for span in find_coupled_spans(p_route, n_route, max_edge_gap_mm=max_edge_gap_mm):
        ids.add(id(p_route.segments[span.p_index]))
        ids.add(id(n_route.segments[span.n_index]))
    return ids


def _offset_polyline(
    pts: list[tuple[float, float]],
    offset: float,
) -> list[tuple[float, float]]:
    """Left-offset an axis-aligned ``(s, t)`` polyline by ``offset`` (mitered).

    Every interior vertex is a 90-degree turn, so the miter vector is the sum
    of the two adjacent left normals.
    """

    def left_normal(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
        ds, dt = b[0] - a[0], b[1] - a[1]
        mag = math.hypot(ds, dt)
        return (-dt / mag, ds / mag)

    out: list[tuple[float, float]] = []
    normals = [left_normal(a, b) for a, b in zip(pts[:-1], pts[1:], strict=True)]
    for k, (s, t) in enumerate(pts):
        if k == 0:
            ns, nt = normals[0]
        elif k == len(pts) - 1:
            ns, nt = normals[-1]
        else:
            ns = normals[k - 1][0] + normals[k][0]
            nt = normals[k - 1][1] + normals[k][1]
        out.append((s + offset * ns, t + offset * nt))
    return out


def meander_footprint_mm(pitch_mm: float, leg_spacing_mm: float, loops: int) -> float:
    """Span length :func:`centerline_meander` needs for ``loops`` bumps."""
    pitch = abs(pitch_mm)
    w = pitch + leg_spacing_mm
    lead = pitch / 2.0 + leg_spacing_mm / 4.0
    return 2.0 * lead + (2 * loops - 1) * w


def centerline_meander(
    span: CoupledSpan,
    p_host: Segment,
    n_host: Segment,
    *,
    length_add_mm: float,
    amplitude_mm: float,
    leg_spacing_mm: float,
    side: int,
    end_margin_mm: float | None = None,
    max_loops: int = 100,
) -> CenterlineMeander | None:
    """Build a centreline serpentine on ``span`` and offset it to both rails.

    The centreline meander is a run of rectangular bumps of height
    ``amplitude`` toward ``side`` (``+1`` = along the span's left normal,
    ``-1`` = away from it).  Bump width and the base gap between bumps are
    both ``|pitch| + leg_spacing_mm``, so after offsetting the inner rail's
    legs are still ``leg_spacing_mm`` apart.  The bump count is the fewest
    that add ``length_add_mm`` at ``amplitude_mm``; when the span is too short
    for that many, as many as fit are used (a partial correction, reported in
    :attr:`CenterlineMeander.added_length_mm`).  With the bump count fixed,
    the amplitude is shrunk so the bumps add exactly the requested length.

    Returns ``None`` when the span cannot host even one bump, or the host
    direction is not 45-aligned (the meander legs would leave the routing
    angle set).
    """
    from .primitives import Segment as _Segment
    from .quantize import is_45_aligned

    if length_add_mm <= 0.0 or amplitude_mm <= 0.0 or side not in (1, -1):
        return None
    if not is_45_aligned(span.ux, span.uy):
        return None

    h = span.pitch_mm
    pitch = abs(h)
    w = pitch + leg_spacing_mm  # bump width == base gap (centreline)
    # Lead-in from the span end to the first centreline corner.  At least
    # pitch/2, so the outer rail's first corner stays inside the span.
    lead = pitch / 2.0 + leg_spacing_mm / 4.0
    margin = 0.0 if end_margin_mm is None else end_margin_mm
    usable = span.overlap_mm - 2.0 * margin
    # footprint(K) = 2*lead + K*w + (K-1)*w = 2*lead + (2K - 1) * w
    max_fit = int(math.floor(((usable - 2.0 * lead) / w + 1.0) / 2.0 + 1e-9))
    if max_fit < 1:
        return None
    needed = max(1, math.ceil(length_add_mm / (2.0 * amplitude_mm) - 1e-9))
    loops = min(needed, max_fit, max_loops)
    amplitude = length_add_mm / (2.0 * loops) if loops == needed else amplitude_mm
    footprint = 2.0 * lead + (2 * loops - 1) * w
    s0 = span.s_lo + (span.overlap_mm - footprint) / 2.0

    # Centreline polyline in the P frame, t measured from the centreline.
    cl: list[tuple[float, float]] = [(s0, 0.0)]
    a = side * amplitude
    for k in range(loops):
        x = s0 + lead + 2 * k * w
        cl.extend([(x, 0.0), (x, a), (x + w, a), (x + w, 0.0)])
    cl.append((s0 + footprint, 0.0))

    def rail(offset: float, host: Segment, base_t: float) -> list[Segment]:
        """Offset the centreline by ``offset`` and splice it into ``host``."""
        pts = _offset_polyline(cl, offset)[1:-1]  # rail corners only
        ox, oy = p_host.x1, p_host.y1

        def world(s: float, t: float) -> tuple[float, float]:
            return (ox + s * span.ux + t * span.nx, oy + s * span.uy + t * span.ny)

        corners = [world(s, base_t + t) for (s, t) in pts]
        start, end = (host.x1, host.y1), (host.x2, host.y2)
        s_start = (start[0] - ox) * span.ux + (start[1] - oy) * span.uy
        s_end = (end[0] - ox) * span.ux + (end[1] - oy) * span.uy
        if s_start > s_end:
            corners.reverse()
        chain = [start, *corners, end]
        return [
            _Segment(
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                width=host.width,
                layer=host.layer,
                net=host.net,
                net_name=host.net_name,
            )
            for (x1, y1), (x2, y2) in zip(chain[:-1], chain[1:], strict=True)
        ]

    # The centreline sits at t = h/2 in the P frame; the P rail is offset by
    # -h/2 from it and the N rail by +h/2.  ``base_t`` re-expresses the rail's
    # offset polyline in the P frame.
    p_segments = rail(-h / 2.0, p_host, h / 2.0)
    n_segments = rail(h / 2.0, n_host, h / 2.0)
    added = 2.0 * amplitude * loops
    return CenterlineMeander(
        p_segments=p_segments,
        n_segments=n_segments,
        added_length_mm=added,
        loops=loops,
        amplitude_mm=amplitude,
        side=side,
    )


__all__ = [
    "PARALLEL_SIN_TOLERANCE",
    "CenterlineMeander",
    "CoupledSpan",
    "centerline_meander",
    "coupled_segment_ids",
    "find_coupled_spans",
    "meander_footprint_mm",
]
