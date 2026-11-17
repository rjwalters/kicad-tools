"""Crossing-aware net ordering from flight-line inversions (Issue #5787 step 2).

Epic #5784 Phase 3.  ``monotone_certificate.py`` already counts *inversions*
(forced crossings) — but only for the narrow SER problem class it was written
for: two-terminal nets with one pin on each of two **parallel boundaries**
(board 07's mirrored DDR byte).  Its consumer,
:meth:`Autorouter._apply_byte_lane_inner_priority`, additionally requires a
detected match group of at least 5 members projecting onto one co-located row
on a single component.  Boards 02 (charlieplex 3x3) and 03 (USB joystick) have
no such group, so ``--monotone-certificate-order`` and
``--bundle-river-planner`` are *structurally* identity no-ops there — see
:func:`kicad_tools.router.core.Autorouter._apply_byte_lane_inner_priority` and
``docs/research/kct-route-net-order-experiment.md``.

This module generalises the same idea — *who is forced to cross whom* — to an
arbitrary board with arbitrary multi-pin nets, with no boundary, match-group or
pin-count precondition:

1. Each net is reduced to a **flight-line skeleton**: the Euclidean minimum
   spanning tree over its pad centres (a single segment for a 2-pin net).  This
   is the standard cheap stand-in for "where this net will roughly go" before
   any search has run.
2. Two nets **conflict** when any of their skeleton segments properly
   intersect.  For a single-layer realisation such a pair must cross, exactly
   as an inversion must in the monotone case; with layers available it is
   instead a pair that will compete for vias or detours.
3. Nets are ordered by **descending conflict degree** (the number of distinct
   other nets they cross), so the most-contended nets pick their corridors
   while the grid is still empty.

The ordering is applied *within* each net-class priority band, never across
one: class priority (clock/diff-pair before digital before default) is an
invariant of :meth:`Autorouter._get_net_priority` that this heuristic must not
silently invert.  Ties inside a band fall back to the caller's existing
priority key, so the order is a deterministic function of the board and is
byte-identical to the default sort whenever no net crosses another.

Pure geometry + combinatorics: no grid, router or pad-object dependency (pad
centres come in as plain coordinate tuples), so it is unit testable against
hand-constructed point sets.  Cost is O(N^2) in pads per net for the MST and
O(S^2) in total skeleton segments for the conflict scan, which on the boards
this targets (10-30 nets, a few dozen segments) is microseconds.

Caveat, inherited from the monotone-certificate caveat: a flight-line crossing
is a *prior*, not a proof.  Real routes are not straight, multi-layer boards
resolve crossings with vias, and obstacles can force a crossing the skeletons
do not show.  Treat the degree as a congestion-ordering signal and measure the
result, never as a feasibility claim.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]
Segment = tuple[Point, Point]

# Two skeleton endpoints closer than this (mm) are treated as the same point,
# so segments that merely share a pad do not register as a crossing.
_COINCIDENT_EPS_MM = 1e-9


@dataclass(frozen=True)
class CrossingProfile:
    """Flight-line conflict profile of one net.

    Attributes:
        net_id: The net this profile describes.
        degree: Number of *distinct* other nets whose skeleton this net's
            skeleton properly intersects.  This is the ordering key: it
            measures how many independent neighbours contend for the same
            space, which is what a corridor-reservation order wants to know.
        crossings: Total number of intersecting segment pairs involving this
            net.  Always ``>= degree``.  Used only as a deterministic
            tiebreak, because a single neighbour crossed three times is less
            constraining than three neighbours crossed once each.
    """

    net_id: int
    degree: int
    crossings: int


def minimum_spanning_skeleton(points: list[Point]) -> list[Segment]:
    """Return the Euclidean minimum spanning tree over ``points`` as segments.

    Prim's algorithm, O(n^2) — ``n`` is a single net's pad count, which is a
    handful on the boards this targets, so the dense formulation is both
    faster in practice and simpler to audit than a heap/Delaunay variant.

    Args:
        points: Pad centres in mm.  Duplicate coordinates are tolerated (they
            contribute zero-length edges, which are dropped).

    Returns:
        ``len(points) - 1`` segments at most, in the order Prim visited them.
        Empty for fewer than two distinct points.
    """
    if len(points) < 2:
        return []

    unvisited = set(range(1, len(points)))
    # best_cost[i] / best_from[i]: cheapest known edge from the tree to i.
    best_cost: dict[int, float] = {}
    best_from: dict[int, int] = {}
    for i in unvisited:
        best_cost[i] = math.dist(points[0], points[i])
        best_from[i] = 0

    segments: list[Segment] = []
    while unvisited:
        nearest = min(unvisited, key=lambda i: (best_cost[i], i))
        unvisited.discard(nearest)
        a, b = points[best_from[nearest]], points[nearest]
        if math.dist(a, b) > _COINCIDENT_EPS_MM:
            segments.append((a, b))
        for i in unvisited:
            cost = math.dist(points[nearest], points[i])
            if cost < best_cost[i]:
                best_cost[i] = cost
                best_from[i] = nearest
    return segments


def _orientation(p: Point, q: Point, r: Point) -> float:
    """Signed area of triangle ``p q r`` (>0 counter-clockwise)."""
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def segments_cross(a: Segment, b: Segment) -> bool:
    """Whether two skeleton segments *properly* intersect.

    "Properly" excludes the two degenerate cases that are not crossings in the
    routing sense:

    * **Shared endpoints.** Two nets never share a pad (a pad belongs to one
      net), but a shared *coordinate* can occur in synthetic inputs, and a
      net's own MST shares endpoints by construction.  Touching at an endpoint
      is not a crossing.
    * **Collinear overlap.** Two skeletons running along the same line are
      parallel contention, not a forced crossing; the degree counter would
      otherwise double-count a bus.
    * **T-touches.** One segment's endpoint landing *on* the other's interior
      is a touch, not a crossing.  Grid-aligned pad centres make this common,
      so the exclusion is symmetric in ``a``/``b``: swapping the arguments
      never changes the answer.

    Args:
        a: First segment.
        b: Second segment.

    Returns:
        ``True`` iff the open interiors of the two segments intersect at
        exactly one point.
    """
    (a0, a1), (b0, b1) = a, b
    for p in (a0, a1):
        for q in (b0, b1):
            if math.dist(p, q) <= _COINCIDENT_EPS_MM:
                return False

    d1 = _orientation(a0, a1, b0)
    d2 = _orientation(a0, a1, b1)
    d3 = _orientation(b0, b1, a0)
    d4 = _orientation(b0, b1, a1)
    # Strict sign change on both segments == single interior intersection.
    # Any zero means an endpoint lies on the other segment (touch) or the pair
    # is collinear; both are excluded.  All FOUR determinants need the
    # zero-guard, not just d1/d2: guarding only one pair made the predicate
    # asymmetric at a T-touch, where one segment's endpoint lies on the other's
    # interior (the zero lands in d3/d4 when the arguments are swapped).  Since
    # :func:`crossing_profiles` always passes the lower net id first, that made
    # a net's crossing degree depend on its net id rather than on geometry.
    return (
        ((d1 > 0) != (d2 > 0))
        and ((d3 > 0) != (d4 > 0))
        and d1 != 0
        and d2 != 0
        and d3 != 0
        and d4 != 0
    )


def crossing_profiles(net_skeletons: dict[int, list[Segment]]) -> dict[int, CrossingProfile]:
    """Count flight-line conflicts between every pair of nets.

    Args:
        net_skeletons: Net id -> that net's skeleton segments (see
            :func:`minimum_spanning_skeleton`).  Nets with an empty skeleton
            (single-pad or unplaced nets) are included with degree 0 so the
            caller can order the full set without a membership check.

    Returns:
        Net id -> :class:`CrossingProfile`, one entry per key of the input.
    """
    degree: dict[int, set[int]] = {net: set() for net in net_skeletons}
    crossings: dict[int, int] = dict.fromkeys(net_skeletons, 0)

    nets = sorted(net_skeletons)
    for idx, net_a in enumerate(nets):
        segs_a = net_skeletons[net_a]
        if not segs_a:
            continue
        for net_b in nets[idx + 1 :]:
            segs_b = net_skeletons[net_b]
            if not segs_b:
                continue
            hits = sum(1 for sa in segs_a for sb in segs_b if segments_cross(sa, sb))
            if hits:
                degree[net_a].add(net_b)
                degree[net_b].add(net_a)
                crossings[net_a] += hits
                crossings[net_b] += hits

    return {
        net: CrossingProfile(net_id=net, degree=len(degree[net]), crossings=crossings[net])
        for net in net_skeletons
    }


def crossing_aware_order(
    net_skeletons: dict[int, list[Segment]],
    priority_key: dict[int, tuple],
    class_band: dict[int, int],
) -> list[int]:
    """Order nets most-contended-first **within** each net-class priority band.

    Args:
        net_skeletons: Net id -> skeleton segments.
        priority_key: Net id -> the router's existing ``_get_net_priority``
            tuple, used as the intra-band tiebreak so the result degrades to
            the default order when no net crosses another.
        class_band: Net id -> net-class priority (the first element of
            ``_get_net_priority``).  Bands are emitted in ascending order and
            are never interleaved, preserving the clock/diff-pair-first
            invariant.

    Returns:
        A permutation of ``net_skeletons``' keys.
    """
    profiles = crossing_profiles(net_skeletons)
    return sorted(
        net_skeletons,
        key=lambda net: (
            class_band.get(net, 0),
            -profiles[net].degree,
            -profiles[net].crossings,
            priority_key.get(net, ()),
            net,
        ),
    )
