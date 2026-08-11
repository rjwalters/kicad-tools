"""Topology-preserving collinear consolidation of routed copper (issue #4732).

Slice-1 instrumentation (``--report-stage-quality``, PR #4746) measured
the real shape of the problem.  On board 03 under its pinned production
recipe (seed 42, ``--backend cpp --deterministic-budget``)::

    stage             segs   frag%  stair%  45deg%  zero  median_mm
    pre-optimize      6838    99.1     0.1    19.9     0      0.050
    post-optimize     1486    93.1     0.1     7.3     0      0.050
    post-nudge        1486    93.1     0.1     7.3     0      0.050

The optimizer cuts segment count by 78%, yet **93% of what survives is
still sub-0.25 mm copper**, with a median segment length pinned exactly
at the 0.05 mm grid step, and the DRC nudge moves nothing at all.

Instrumenting the individual optimizer passes on that same route pins the
cause on **hypothesis 1 of the issue (collision conservatism)**, and rules
the others out:

======================  ==============
pass                    segments in/out
======================  ==============
``merge_collinear``     6838 -> 1490
``eliminate_zigzags``   1490 -> 1490
``compress_staircase``  1490 -> 1485
``convert_corners_45``  1485 -> 1488
``pull_tight``          1488 -> 1486
======================  ==============

Everything after ``merge_collinear`` is flat, and so is the DRC nudge
(hypothesis 4).  ``TraceOptimizer.optimize_route``'s revert-on-regression
connectivity guard fires **zero** times across all 37 routes, so it is not
discarding the work either.  What *does* happen is inside
``merge_collinear`` itself: every candidate merge is gated on
``path_is_clear``, and **2725 of its 8075 candidates (33.7%) are vetoed**.
Each veto chops one collinear run into two, and no later pass revisits it.

Those vetoes are false positives by construction.  A merged
``A-B`` + ``B-C`` is the *exact union* of two segments the board already
carries -- it adds no copper anywhere -- so nothing about it can be a new
hazard; the grid checker rejects it only because rasterizing one long
segment's clearance envelope touches cells that rasterizing the two short
ones did not.  This module is therefore the consolidation that does not
need a collision checker to be safe, because it never changes either the
copper or the topology it would be judged on:

* Only a vertex of **degree exactly 2** is ever removed, and only when
  its two incident segments are collinear, anti-parallel about it, and
  share layer / net / width.  Every junction, terminal, via and pad
  vertex survives by construction, so the connectivity graph after the
  pass is isomorphic to the one before it (a topological *smoothing*,
  not a rewrite).
* The merged segment is the exact union of the segments it replaces --
  same line, same width, same layer, same extent.  The copper footprint
  on the board is unchanged, so DRC state cannot move either.  This was
  verified on the written artifact, not just argued: densely sampling
  boards 02 and 03 before and after the pass, every point of the
  pre-pass copper lies on the post-pass copper and vice versa, to
  3e-14 mm, and per-``(net, layer)`` total trace length is identical to
  1e-6 mm.

That makes it safe to run *after* the DRC nudge, where the pipeline
previously had no consolidation at all, and its only observable effect
is a shorter, saner segment list.

**Why not just drop ``merge_collinear``'s collision gate instead?**
Because that gate is load-bearing *there*.  ``merge_collinear`` merges
whatever is adjacent in its chain's list order; it performs no vertex
degree check, so nothing stops it from merging through a junction that
belongs to a different chain or a sibling route -- the collision check is
the only thing standing between it and that class of mistake, and
``optimize_route``'s revert-on-regression guard would then throw away the
entire route rather than the one bad merge.  Relaxing it would also
change behaviour for every other ``TraceOptimizer`` consumer
(``kct optimize-traces``, ``TraceOptimizer.optimize_pcb``, the board
recipes).  A separate degree-aware pass recovers the vetoed merges
without touching any of that, and additionally closes the
"nudge output is never re-consolidated" ordering hole, since it runs
after the DRC nudge rather than before it.

**Vertex keys are deliberately layer-blind.**  Degree is counted over
``(x, y)`` alone, not ``(x, y, layer)``.  That is not an oversight: the
invariant this pass is judged against --
:func:`~kicad_tools.router.observability.validate_net_connectivity`, via
:mod:`~kicad_tools.router.connectivity_invariant` -- unions segment
endpoints on their ``(x, y)`` alone as well, so in *its* model a
coincident endpoint on another layer is a connection whether or not a via
is present.  Keying per layer here would let the pass smooth away a
vertex the invariant still treats as a junction, i.e. manufacture exactly
the false regression the design exists to avoid.  Coincidence across
layers merely makes the pass skip a merge; it can never make it perform
an unsafe one.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from ..primitives import Route, Segment

# Cross-product ceiling (on unit vectors) for treating two segments as
# collinear.  ~5.7e-5 degrees -- far below any real routing angle, wide
# enough to absorb the float noise in coordinates like 32.70000000000001.
COLLINEAR_CROSS_EPSILON = 1e-6

# Absolute tolerance for "same width" (widths are authored in mm).
WIDTH_EPSILON_MM = 1e-9

# Default coordinate quantization for endpoint coincidence (mm).  Matches
# the router-wide vertex tolerance used by the optimizer's own
# connectivity guard (``optimizer.trace._vertex_key``).
DEFAULT_TOLERANCE_MM = 1e-4


@dataclass(frozen=True)
class ConsolidationStats:
    """What one consolidation pass did.

    Attributes:
        segments_before: Segment count over the input.
        segments_after: Segment count over the output.
        runs_merged: Number of maximal collinear runs collapsed into a
            single segment.
        runs_vetoed: Number of candidate runs left as-is because the
            collision checker (or the union-length guard) rejected the
            merged segment.
        routes_changed: Number of routes whose segment list changed
            (0 for the segment-level API, which has no routes).
    """

    segments_before: int = 0
    segments_after: int = 0
    runs_merged: int = 0
    runs_vetoed: int = 0
    routes_changed: int = 0

    @property
    def segments_removed(self) -> int:
        """Segments eliminated by the pass."""
        return self.segments_before - self.segments_after

    def summary(self) -> str:
        """One-line human summary for the CLI."""
        pct = (
            (self.segments_removed / self.segments_before * 100.0) if self.segments_before else 0.0
        )
        text = (
            f"{self.segments_before} -> {self.segments_after} segments "
            f"({self.segments_removed} removed, {pct:.1f}%), "
            f"{self.runs_merged} collinear run(s) merged across "
            f"{self.routes_changed} route(s)"
        )
        if self.runs_vetoed:
            text += f", {self.runs_vetoed} vetoed"
        return text


def _qkey(x: float, y: float, tolerance: float) -> tuple[int, int]:
    """Quantize a point so coincident endpoints share a key."""
    if tolerance <= 0:
        return (round(x * 1e9), round(y * 1e9))
    return (round(x / tolerance), round(y / tolerance))


def _length(seg: Segment) -> float:
    """Euclidean length of ``seg`` in mm."""
    return math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)


def _unit_away(seg: Segment, at_start: bool) -> tuple[float, float] | None:
    """Unit vector of ``seg`` pointing away from the named endpoint.

    ``None`` for a zero-length segment: it has no direction, and such
    segments are never merged.
    """
    if at_start:
        dx, dy = seg.x2 - seg.x1, seg.y2 - seg.y1
    else:
        dx, dy = seg.x1 - seg.x2, seg.y1 - seg.y2
    norm = math.hypot(dx, dy)
    if norm <= 0.0:
        return None
    return (dx / norm, dy / norm)


def _anti_parallel(u: tuple[float, float], v: tuple[float, float]) -> bool:
    """True when unit vectors ``u`` and ``v`` point in opposite directions.

    Both point *away* from a shared vertex, so anti-parallel is exactly
    the condition "the vertex lies strictly between the two far
    endpoints on one straight line".  A non-negative dot product means
    the pair doubles back on itself; merging that would silently delete
    copper, so it is rejected.
    """
    cross = u[0] * v[1] - u[1] * v[0]
    dot = u[0] * v[0] + u[1] * v[1]
    return dot < 0.0 and abs(cross) <= COLLINEAR_CROSS_EPSILON


class _DisjointSet:
    """Minimal union-find over segment indices."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, i: int) -> int:
        root = i
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[i] != root:
            self._parent[i], i = root, self._parent[i]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)


def _consolidate_indexed(
    segments: Sequence[Segment],
    *,
    protected_points: Iterable[tuple[float, float]],
    tolerance: float,
    path_is_clear: Callable[[Segment], bool] | None,
    same_owner: Callable[[int, int], bool] | None,
) -> tuple[list[tuple[int, Segment]], ConsolidationStats]:
    """Core pass.  Returns ``[(source_index, segment), ...]`` plus stats.

    ``source_index`` is the index of the input segment a result segment
    came from; for a merged run it is the lowest index in that run.  The
    caller uses it to re-attach results to whatever the input segments
    belonged to.  Output order follows input order.
    """
    n = len(segments)
    if n < 2:
        return (
            [(i, s) for i, s in enumerate(segments)],
            ConsolidationStats(segments_before=n, segments_after=n),
        )

    protected = {_qkey(px, py, tolerance) for px, py in protected_points}

    # vertex key -> [(segment index, is_start), ...].
    #
    # Zero-length segments ARE counted here even though they can never be
    # merged: a zero-length segment sitting on an otherwise degree-2
    # vertex raises that vertex's degree to 4, which blocks the smoothing.
    # Leaving them out would let the pass delete the vertex and strand the
    # zero-length stub as its own connectivity component -- and the DRC
    # nudge is a measured producer of exactly one such segment (#4732
    # slice-1 finding 2).
    incident: dict[tuple[int, int], list[tuple[int, bool]]] = {}
    usable: list[bool] = []
    for i, seg in enumerate(segments):
        usable.append(_length(seg) > 0.0)
        for x, y, is_start in ((seg.x1, seg.y1, True), (seg.x2, seg.y2, False)):
            incident.setdefault(_qkey(x, y, tolerance), []).append((i, is_start))

    dsu = _DisjointSet(n)
    smoothed: set[tuple[int, int]] = set()
    for key, hits in incident.items():
        if len(hits) != 2 or key in protected:
            continue
        (ia, a_start), (ib, b_start) = hits
        if ia == ib:
            continue  # a segment that loops back onto itself (or is zero-length)
        if not usable[ia] or not usable[ib]:
            continue
        sa, sb = segments[ia], segments[ib]
        if sa.layer != sb.layer or sa.net != sb.net:
            continue
        if abs(sa.width - sb.width) > WIDTH_EPSILON_MM:
            continue
        if same_owner is not None and not same_owner(ia, ib):
            continue
        ua = _unit_away(sa, a_start)
        ub = _unit_away(sb, b_start)
        if ua is None or ub is None or not _anti_parallel(ua, ub):
            continue
        dsu.union(ia, ib)
        smoothed.add(key)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        if usable[i]:
            groups.setdefault(dsu.find(i), []).append(i)

    replacement: dict[int, Segment] = {}
    dropped: set[int] = set()
    runs_merged = 0
    runs_vetoed = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        # A run is a simple path: its two ends are the endpoint keys that
        # were not smoothed away.  Endpoint coordinates come from the
        # member segments themselves (never from a shared quantization
        # bucket), so the merged segment lands on real routed copper.
        # Anything that is not exactly two distinct ends is a shape this
        # pass does not claim to understand -- leave it alone.
        ends: list[tuple[tuple[int, int], float, float]] = []
        for i in members:
            seg = segments[i]
            for x, y in ((seg.x1, seg.y1), (seg.x2, seg.y2)):
                key = _qkey(x, y, tolerance)
                if key not in smoothed:
                    ends.append((key, x, y))
        if len(ends) != 2 or ends[0][0] == ends[1][0]:
            continue
        head = min(members)
        template = segments[head]
        first, second = ends
        # Orient the merged segment the way its lowest-index member runs,
        # so the output geometry is deterministic.
        if _qkey(template.x1, template.y1, tolerance) == second[0]:
            first, second = second, first
        merged = dataclasses.replace(
            template,
            x1=first[1],
            y1=first[2],
            x2=second[1],
            y2=second[2],
        )
        merged_length = _length(merged)
        if merged_length <= 0.0:
            continue  # never introduce a zero-length segment
        # Union guard: the merged segment must be exactly as long as the
        # copper it replaces.  Collinearity + anti-parallelism already
        # imply it, so this can only fire on a logic error -- but it is
        # the cheap, always-correct invariant that makes "the copper is
        # unchanged" a checked property rather than a claim.
        parts_length = sum(_length(segments[m]) for m in members)
        if abs(merged_length - parts_length) > max(tolerance, 1e-9):
            runs_vetoed += 1
            continue
        if path_is_clear is not None and not path_is_clear(merged):
            runs_vetoed += 1
            continue
        replacement[head] = merged
        dropped.update(m for m in members if m != head)
        runs_merged += 1

    result: list[tuple[int, Segment]] = []
    for i, seg in enumerate(segments):
        if i in dropped:
            continue
        result.append((i, replacement.get(i, seg)))

    return result, ConsolidationStats(
        segments_before=n,
        segments_after=len(result),
        runs_merged=runs_merged,
        runs_vetoed=runs_vetoed,
    )


def consolidate_segments(
    segments: Sequence[Segment],
    *,
    protected_points: Iterable[tuple[float, float]] = (),
    tolerance: float = DEFAULT_TOLERANCE_MM,
    path_is_clear: Callable[[Segment], bool] | None = None,
) -> tuple[list[Segment], ConsolidationStats]:
    """Collapse maximal collinear runs of ``segments`` into single segments.

    A run is merged only when every interior vertex it removes has degree
    exactly 2 **in the whole input**, is not a protected point, and joins
    two segments that are collinear, anti-parallel about it, and share
    layer, net and width.  Everything else is returned untouched, in its
    original relative order.

    Args:
        segments: Segments to consolidate.  The caller decides what "the
            whole input" means -- pass every segment of a net so that
            cross-route T-junctions are visible as degree-3 vertices.
        protected_points: Points that must survive as vertices (via and
            pad positions), compared at the same ``tolerance``.
        tolerance: Coordinate quantization for endpoint coincidence (mm).
        path_is_clear: Optional collision check applied to each merged
            candidate; a veto keeps the originals.  **Off by default,
            deliberately.**  A merged segment is the exact union of its
            parts (enforced by the length guard in
            :func:`_consolidate_indexed`), so it occupies copper the
            board already carries -- the pipeline's own
            ``make_collision_checker`` reports that copper as blocked
            because it is *already committed on the grid*, which would
            veto every merge while describing no real hazard.  The
            parameter exists for callers whose checker models something
            copper-union reasoning does not cover.

    Returns:
        ``(consolidated_segments, stats)``.
    """
    indexed, stats = _consolidate_indexed(
        segments,
        protected_points=protected_points,
        tolerance=tolerance,
        path_is_clear=path_is_clear,
        same_owner=None,
    )
    return [seg for _i, seg in indexed], stats


def consolidate_net_routes(
    routes: Sequence[Route],
    *,
    pad_positions: Iterable[tuple[float, float]] = (),
    tolerance: float = DEFAULT_TOLERANCE_MM,
    path_is_clear: Callable[[Segment], bool] | None = None,
) -> tuple[list[Route], ConsolidationStats]:
    """Consolidate every route of a single net as one connectivity graph.

    Vertex degree is computed over the net's **whole** copper, so a point
    where one route's stub lands mid-run of another route is seen as
    degree 3 and is never smoothed away -- which is exactly what keeps
    :func:`~kicad_tools.router.observability.validate_net_connectivity`'s
    endpoint union-find intact.  Segments are still only merged with
    segments of their own route, so route bookkeeping (and
    ``grid.routes`` identity) stays aligned.

    Returns:
        ``(routes, stats)`` -- the original object where a route was
        unchanged, a new :class:`Route` where it was consolidated.
    """
    flat: list[Segment] = []
    owner_index: list[int] = []
    protected: list[tuple[float, float]] = list(pad_positions)
    for ri, route in enumerate(routes):
        for seg in route.segments:
            flat.append(seg)
            owner_index.append(ri)
        for via in route.vias:
            protected.append((via.x, via.y))

    if len(flat) < 2:
        return list(routes), ConsolidationStats(segments_before=len(flat), segments_after=len(flat))

    indexed, raw = _consolidate_indexed(
        flat,
        protected_points=protected,
        tolerance=tolerance,
        path_is_clear=path_is_clear,
        same_owner=lambda a, b: owner_index[a] == owner_index[b],
    )
    if raw.runs_merged == 0:
        return list(routes), ConsolidationStats(
            segments_before=len(flat),
            segments_after=len(flat),
            runs_vetoed=raw.runs_vetoed,
        )

    by_owner: list[list[Segment]] = [[] for _ in routes]
    for source, seg in indexed:
        by_owner[owner_index[source]].append(seg)

    out: list[Route] = []
    routes_changed = 0
    for ri, route in enumerate(routes):
        segs = by_owner[ri]
        if len(segs) == len(route.segments):
            out.append(route)
            continue
        routes_changed += 1
        # ``dataclasses.replace`` rather than a hand-written ``Route(...)``
        # so additive Route fields (``is_escape``, #3441) are carried
        # forward instead of silently reset to their defaults.
        out.append(dataclasses.replace(route, segments=segs, vias=list(route.vias)))

    return out, ConsolidationStats(
        segments_before=len(flat),
        segments_after=len(indexed),
        runs_merged=raw.runs_merged,
        runs_vetoed=raw.runs_vetoed,
        routes_changed=routes_changed,
    )


def consolidate_routes_grid_synced(
    router,
    *,
    skip_nets: set[int] | None = None,
    path_is_clear: Callable[[Segment], bool] | None = None,
    tolerance: float = DEFAULT_TOLERANCE_MM,
) -> ConsolidationStats:
    """Consolidate every route on ``router``, keeping the grid in sync.

    Groups ``router.routes`` by net, runs :func:`consolidate_net_routes`
    per net, and commits the result through the same grid transaction the
    optimizer uses (:func:`~kicad_tools.router.optimizer.trace.apply_route_transform_grid_synced`)
    so cells, R-trees, ``grid.routes`` bookkeeping and the C++ mirror all
    follow the copper.  The transaction is a formality here -- a
    consolidated route occupies exactly the cells its fragments did --
    but going through it keeps ``grid.routes`` pointing at the live
    ``Route`` objects instead of stale twins.

    Pad positions come from ``router.nets`` / ``router.pads`` when
    available so a pad sitting mid-run stays a vertex; a router without
    that metadata simply protects vias and junctions.

    Args:
        router: ``Autorouter`` whose ``routes`` are consolidated in place.
        skip_nets: Net IDs to pass through untouched (#3508 parity).
            Consolidation is length- and geometry-preserving, so
            diff-pair serpentines are already safe -- the parameter
            exists so callers can be conservative.
        path_is_clear: Optional collision check for merged candidates.
        tolerance: Coordinate quantization for endpoint coincidence (mm).

    Returns:
        Aggregate :class:`ConsolidationStats` for the whole router.
    """
    from .trace import apply_route_transform_grid_synced

    routes_by_net: dict[int, list[Route]] = {}
    for route in router.routes:
        if skip_nets is not None and route.net in skip_nets:
            continue
        routes_by_net.setdefault(route.net, []).append(route)

    pads_by_net = _pad_positions_by_net(router)

    # ``Route`` is an unhashable (eq-able) dataclass, so identity is the
    # only usable key.  Every original stays alive in ``routes_by_net``
    # for the whole function, which is what makes ``id()`` safe here.
    replacements: dict[int, Route] = {}
    before = after = merged = vetoed = changed = 0
    for net, net_routes in routes_by_net.items():
        new_routes, stats = consolidate_net_routes(
            net_routes,
            pad_positions=pads_by_net.get(net, ()),
            tolerance=tolerance,
            path_is_clear=path_is_clear,
        )
        before += stats.segments_before
        after += stats.segments_after
        merged += stats.runs_merged
        vetoed += stats.runs_vetoed
        changed += stats.routes_changed
        for old, new in zip(net_routes, new_routes, strict=False):
            if new is not old:
                replacements[id(old)] = new

    if replacements:
        apply_route_transform_grid_synced(
            router,
            lambda route: replacements.get(id(route), route),
            skip_nets=skip_nets,
        )

    return ConsolidationStats(
        segments_before=before,
        segments_after=after,
        runs_merged=merged,
        runs_vetoed=vetoed,
        routes_changed=changed,
    )


def _pad_positions_by_net(router) -> dict[int, list[tuple[float, float]]]:
    """Best-effort ``{net: [(x, y), ...]}`` pad map for vertex protection.

    A pad that sits mid-run is a degree-2 vertex the topology argument
    would otherwise allow smoothing away; ``validate_net_connectivity``
    links pads to their nearest segment *endpoint*, so removing that
    vertex could move a pad's attachment point.  Protecting pad
    positions keeps that link exact.  Any router shape without the
    ``nets``/``pads`` metadata degrades to junction+via protection only.
    """
    pads_by_net: dict[int, list[tuple[float, float]]] = {}
    nets = getattr(router, "nets", None)
    pads = getattr(router, "pads", None)
    if not isinstance(nets, dict) or not isinstance(pads, dict):
        return pads_by_net
    for net_id, pad_keys in nets.items():
        positions: list[tuple[float, float]] = []
        for key in pad_keys:
            pad = pads.get(key)
            if pad is None:
                continue
            try:
                positions.append((pad.x, pad.y))
            except AttributeError:  # pragma: no cover - defensive
                continue
        if positions:
            pads_by_net[net_id] = positions
    return pads_by_net
