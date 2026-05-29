"""Geometry-based FOM soft terms.

Issue #3186: this module implements the four geometry-flavoured terms of
the hybrid FOM:

* Trace length excess (term 1)
* Turning penalty (term 3)
* Net congestion variance (term 4)
* Crossing minimisation, pre-route (term 8)
* Compactness (term 10)

Each function takes the shared :class:`~kicad_tools.optim.fom_features.BoardFeatures`
snapshot and returns a float >= 0 with 0 = perfect.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from kicad_tools.optim.fom_features import (
    BoardFeatures,
    euclidean,
    routed_net_length,
    segment_length,
    steiner_lower_bound,
)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import Segment


__all__ = [
    "trace_length_excess",
    "turning_penalty",
    "net_congestion_variance",
    "crossing_count",
    "compactness",
]


def trace_length_excess(features: BoardFeatures) -> float:
    """Trace length excess: ``sum_net (actual - SMT_lower) / SMT_lower``.

    Normalisation
    -------------
    Each net contributes ``max(0, actual - lower) / lower`` to the sum, with
    the lower bound supplied by the rectilinear Steiner-minimum-tree of the
    net's pads (Manhattan metric).  0 = every routed net is at its Manhattan
    minimum (the structural floor); larger = more wasted copper.

    Unrouted or single-pad nets contribute 0.  Nets where the Steiner lower
    bound is itself 0 (degenerate, pads on the same point) contribute the
    raw actual length to avoid divide-by-zero blowups.
    """
    total = 0.0
    for net_id, pads in features.nets_to_pads.items():
        if len(pads) < 2:
            continue
        actual = routed_net_length(features, net_id)
        if actual <= 0.0:
            # Net has pads but no routed segments yet -- treat as 0 excess so
            # we don't double-count the "did not route" failure (the hard
            # constraint LVS gate handles that).
            continue
        lower = steiner_lower_bound(pads)
        if lower <= 1e-9:
            total += actual
        else:
            excess = max(0.0, actual - lower)
            total += excess / lower
    return total


def turning_penalty(features: BoardFeatures) -> float:
    """Wiggly-route penalty: ``sum_seg-pair (theta mod 45)^2 / path_length``.

    Normalisation
    -------------
    For each consecutive segment pair on the same net & layer, we compute
    the turn angle (deg) between the two segment direction vectors and
    score ``(angle_mod_45)^2``.  Sums are normalised by total routed
    length so longer boards aren't structurally penalised.

    0 = every turn is exactly a multiple of 45 deg (the routing
    convention KiCad enforces post-cleanup); larger = wigglier routes
    with off-grid turns.

    Note: an isolated 90 deg turn scores 0 (90 mod 45 == 0); two 1 deg
    detours score 1 + 1 = 2.  This is the desired behaviour -- the
    legitimate corner gets no penalty, the wiggle does.
    """
    total_penalty = 0.0
    total_length = 0.0

    for _, segs in features.segments_by_net.items():
        # Group by layer (turns across layer transitions aren't real turns).
        by_layer: dict[str, list[Segment]] = {}
        for seg in segs:
            by_layer.setdefault(seg.layer, []).append(seg)

        for layer_segs in by_layer.values():
            # Sort segments into chains by adjacency.  We use a simple
            # endpoint-match scan rather than a real graph walk -- the FOM
            # only needs approximate "consecutive segment pair" pairing.
            for i in range(len(layer_segs)):
                seg = layer_segs[i]
                total_length += segment_length(seg)
                for j in range(i + 1, len(layer_segs)):
                    other = layer_segs[j]
                    if _segments_meet(seg, other):
                        angle = _turn_angle_deg(seg, other)
                        if angle is None:
                            continue
                        mod45 = angle % 45.0
                        # Symmetric: a 5deg turn and a 40deg turn from grid
                        # are equally bad.
                        deviation = min(mod45, 45.0 - mod45)
                        total_penalty += deviation * deviation
    if total_length <= 1e-9:
        return 0.0
    return total_penalty / total_length


def _segments_meet(a: Segment, b: Segment, tol: float = 0.01) -> bool:
    """True if a and b share an endpoint within ``tol`` mm."""
    return (
        euclidean(a.end, b.start) < tol
        or euclidean(a.end, b.end) < tol
        or euclidean(a.start, b.start) < tol
        or euclidean(a.start, b.end) < tol
    )


def _turn_angle_deg(a: Segment, b: Segment) -> float | None:
    """Angle in degrees between segment direction vectors (0..180).

    Returns ``None`` if either segment is degenerate (zero length).
    """
    dx_a = a.end[0] - a.start[0]
    dy_a = a.end[1] - a.start[1]
    dx_b = b.end[0] - b.start[0]
    dy_b = b.end[1] - b.start[1]
    la = math.hypot(dx_a, dy_a)
    lb = math.hypot(dx_b, dy_b)
    if la < 1e-9 or lb < 1e-9:
        return None
    cos_theta = (dx_a * dx_b + dy_a * dy_b) / (la * lb)
    cos_theta = max(-1.0, min(1.0, cos_theta))
    return math.degrees(math.acos(cos_theta))


def net_congestion_variance(features: BoardFeatures, grid_size: int = 10) -> float:
    """Routing congestion stddev across a ``grid_size`` x ``grid_size`` grid.

    Normalisation
    -------------
    We bin the board's bounding box into ``grid_size^2`` cells, accumulate
    total routed segment length in each cell, then return the standard
    deviation across cells normalised by the mean cell length (the
    coefficient of variation).  Empty cells count as zero.

    0 = perfectly even distribution of copper across the board; larger =
    some regions hot-spotted while others empty.  High CV is the strongest
    pre-route signal of routability problems.

    Pre-route placements (no segments) score 0 -- the per-pair SMT
    projection done by :func:`crossing_count` is the matching pre-route
    congestion proxy.
    """
    if grid_size < 2:
        return 0.0

    min_x, min_y, max_x, max_y = features.board_bbox
    width = max_x - min_x
    height = max_y - min_y
    if width <= 1e-9 or height <= 1e-9:
        return 0.0

    cell_w = width / grid_size
    cell_h = height / grid_size

    grid = [[0.0] * grid_size for _ in range(grid_size)]

    for segs in features.segments_by_net.values():
        for seg in segs:
            # Bin the segment by sampling along it.  For short segments
            # (most routes), the start/end are in the same cell anyway.
            length = segment_length(seg)
            if length <= 1e-9:
                continue
            # Sample every 0.5mm along the segment.
            samples = max(2, int(length / 0.5) + 1)
            for k in range(samples):
                t = k / (samples - 1) if samples > 1 else 0.0
                sx = seg.start[0] + t * (seg.end[0] - seg.start[0])
                sy = seg.start[1] + t * (seg.end[1] - seg.start[1])
                ci = int((sx - min_x) / cell_w)
                cj = int((sy - min_y) / cell_h)
                if 0 <= ci < grid_size and 0 <= cj < grid_size:
                    grid[ci][cj] += length / samples

    flat = [c for row in grid for c in row]
    if not flat:
        return 0.0
    mean = sum(flat) / len(flat)
    if mean <= 1e-9:
        return 0.0
    var = sum((c - mean) ** 2 for c in flat) / len(flat)
    return math.sqrt(var) / mean


def crossing_count(features: BoardFeatures) -> float:
    """Pre-route crossing count via star-topology SMT projections.

    Each multi-pad net is approximated by edges from a centroid to each
    pad (star topology -- cheap and topology-equivalent to MST for the
    purposes of crossing-count). We then count segment-segment crossings
    *between distinct nets* (intra-net crossings are not a routability
    signal -- the router can split them across layers).

    Normalisation
    -------------
    0 = no crossings (board is planar at the placement level); larger =
    placements that will force the router to use more vias to escape.
    Pairs of nets where any pair of approximate edges crosses count as a
    single crossing event each, so the magnitude scales as
    ``O(crossings)``.
    """
    # Build star-topology edges per net.
    edges_by_net: dict[int, list[tuple[tuple[float, float], tuple[float, float]]]] = {}
    for net_id, pads in features.nets_to_pads.items():
        if len(pads) < 2:
            continue
        cx = sum(p.x for p in pads) / len(pads)
        cy = sum(p.y for p in pads) / len(pads)
        centroid = (cx, cy)
        edges_by_net[net_id] = [(centroid, (p.x, p.y)) for p in pads]

    nets = list(edges_by_net.keys())
    total_crossings = 0
    for i in range(len(nets)):
        for j in range(i + 1, len(nets)):
            a_edges = edges_by_net[nets[i]]
            b_edges = edges_by_net[nets[j]]
            for a in a_edges:
                for b in b_edges:
                    if _segments_cross(a[0], a[1], b[0], b[1]):
                        total_crossings += 1
    return float(total_crossings)


def _segments_cross(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
) -> bool:
    """Strict segment crossing test (no shared endpoints, no collinearity)."""

    def ccw(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1 = ccw(p3, p4, p1)
    d2 = ccw(p3, p4, p2)
    d3 = ccw(p1, p2, p3)
    d4 = ccw(p1, p2, p4)

    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    return False


def compactness(features: BoardFeatures) -> float:
    """Wasted area: ``(hull_area - essential_exterior) / pad_count``.

    Normalisation
    -------------
    We compute the convex hull of all pad positions and subtract the
    bounding box of "essential exterior" parts (connectors, mounting
    holes, locked footprints).  The remainder is wasted space per pad.

    This is intentionally NOT bounding-box area -- on boards with
    asymmetric footprint clusters (e.g. an edge connector strip + a
    central MCU) bounding box is a misleading metric.  Convex hull is
    closer to "the polygon the router actually has to escape from."

    The "essential exterior" approximation here is the bounding box of
    fixed-position parts.  Real essential exterior would be the perimeter
    polygon swept by mounting hardware + connector keep-outs; this
    approximation undercounts essential area (the real essential is
    larger), so compactness is slightly overestimated.  This is fine for
    Phase 1 -- the term is monotonic in wasted space, which is what
    matters for placement optimisation.  See PR follow-up for a precise
    polygon model.

    0 = no wasted space relative to mechanical constraints; larger =
    more sprawl per pad.
    """
    pads = [pf for fp in features.footprints for pf in fp.pad_features]
    if not pads:
        return 0.0
    pad_count = len(pads)

    points = [(p.x, p.y) for p in pads]
    hull_area = _convex_hull_area(points)

    # Essential exterior = bounding box of fixed parts.
    fixed_pads = [pf for fp in features.fixed_footprints for pf in fp.pad_features]
    if fixed_pads:
        fxs = [p.x for p in fixed_pads]
        fys = [p.y for p in fixed_pads]
        essential = (max(fxs) - min(fxs)) * (max(fys) - min(fys))
    else:
        essential = 0.0

    wasted = max(0.0, hull_area - essential)
    return wasted / pad_count if pad_count > 0 else 0.0


def _convex_hull_area(points: list[tuple[float, float]]) -> float:
    """Area of the convex hull of ``points`` via the Shoelace formula.

    Uses scipy.spatial.ConvexHull when scipy is available (handles
    degenerate inputs robustly); falls back to a simple Andrew's
    monotone chain implementation otherwise.
    """
    if len(points) < 3:
        return 0.0
    try:
        import numpy as np
        from scipy.spatial import ConvexHull

        arr = np.array(points)
        try:
            hull = ConvexHull(arr)
            return float(hull.volume)  # volume == area in 2D
        except Exception:
            # Degenerate (collinear) points; fall back.
            return 0.0
    except ImportError:
        # No scipy -- simple Andrew's monotone chain.
        return _andrew_monotone_chain_area(points)


def _andrew_monotone_chain_area(points: list[tuple[float, float]]) -> float:
    """Compute convex hull area via Andrew's monotone chain.

    A pure-Python fallback for when scipy isn't available.
    """
    pts = sorted(set(points))
    if len(pts) < 3:
        return 0.0

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    hull = lower[:-1] + upper[:-1]
    if len(hull) < 3:
        return 0.0
    # Shoelace formula.
    area = 0.0
    n = len(hull)
    for i in range(n):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0
