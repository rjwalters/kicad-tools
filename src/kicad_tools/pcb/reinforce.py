"""Buttress-wire reinforcement: anchor-PTH row along a named net.

Unit A (MVP) of the #4218 buttress-wire reinforcement design (Part 2 of
the #4215 ampacity feature). This is a **post-route** pass that reads an
already-routed board, walks the routed copper of one named net into
ordered polylines, and emits a spaced row of **same-net** plated
through-hole (PTH) anchor vias along the longest run. A solid-core copper
wire ("buttress wire") is later soldered through the anchor row by hand to
carry additional current.

Structural template: ``kct stitch`` (``cli/stitch_cmd.py``) -- a
load -> add-geometry-to-a-named-net -> save pass. Anchors are emitted via
:meth:`kicad_tools.schema.pcb.PCB.add_via` (a large plated via IS a PTH).

Key properties:

* **Same-net, no shorts.** Anchors carry the target net, so they never
  short the trace to itself.
* **Clearance-refuse, not silent short.** Before committing each anchor,
  the pass runs
  :func:`kicad_tools.router.via_clearance.point_clear_of_copper` against
  all *other*-net copper (tracks / vias / pads / filled zone polygons) and
  the manufacturer hole-to-hole floor. A colliding anchor is REFUSED and
  reported -- never silently placed (a short) and never silently dropped.
* **Junction-aware chaining.** Segment chaining reuses the hardened
  junction-splitting chainer
  (:func:`kicad_tools.router.optimizer.chain.sort_into_chains`) via a thin
  adapter from :class:`kicad_tools.schema.pcb.Segment` to
  :class:`kicad_tools.router.primitives.Segment`. For the MVP the longest
  run is anchored and any additional branches are *reported* as unhandled.

Out of scope (later units, tracked on #4218): mask-opening channel
(Unit B), net-class ``reinforced`` wiring (Unit C), HV keep-away
(Unit D), ampacity-credit coupling in #4217 (Unit E), first-class
HV/creepage model (Unit F).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from kicad_tools.core.types import CopperLayer
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.physics.wire_gauge import (
    DEFAULT_WIRE_GAUGE_AWG,
    anchor_drill_for_awg,
    anchor_pad_for_drill,
)
from kicad_tools.router.optimizer.algorithms import merge_collinear
from kicad_tools.router.optimizer.chain import sort_into_chains
from kicad_tools.router.optimizer.config import OptimizationConfig
from kicad_tools.router.primitives import Segment as RouterSegment
from kicad_tools.router.via_clearance import (
    FilledPolygonLike,
    ForeignPadTuple,
    TrackSegmentLike,
    point_clear_of_copper,
)
from kicad_tools.schema.pcb import PCB, Segment

# Default spacing between mid-run anchors (mm). Matches the architect's
# ``ReinforcementSpec.anchor_spacing_mm`` default.
DEFAULT_ANCHOR_SPACING_MM = 15.0

# Tier-2 nudge-fallback bound (#4319). When a candidate anchor collides with
# other-net copper, the pass searches along the run's arc-length axis by up to
# ``NUDGE_MAX_FRACTION_OF_SPACING * spacing_mm`` (symmetric ±), in
# ``NUDGE_STEPS`` increments each side, and places the first clear position
# before recording a refusal. A genuinely blocked position (nothing clear in
# the whole window) is still hard-refused -- the search is finite and cheap.
NUDGE_MAX_FRACTION_OF_SPACING = 0.5
NUDGE_STEPS = 4

# Config used for the tier-3 within-run collinear merge (#4319). Only
# ``tolerance`` is consulted by :func:`merge_collinear`'s connectivity /
# direction predicates; the rest are inert here.
_MERGE_CONFIG = OptimizationConfig()

# Fallback design rules when no manufacturer profile is supplied. These are
# JLCPCB-tier-typical values; the annular-ring floor is what actually sizes
# the anchor pad. The reinforce pass NEVER hardcodes the annular ring at a
# call site -- it always reads it from these rules (real or fallback).
_FALLBACK_DESIGN_RULES = DesignRules(  # noqa: MFR001 reason="intentional manufacturer-agnostic fallback: reinforce is a post-route pass that runs when no manufacturer profile is supplied; a real profile overrides these JLCPCB-tier-typical defaults when threaded in"
    min_trace_width_mm=0.127,
    min_clearance_mm=0.2,
    min_via_drill_mm=0.3,
    min_via_diameter_mm=0.6,
    min_annular_ring_mm=0.25,
    min_hole_to_hole_mm=0.5,
)


@dataclass
class RefusedAnchor:
    """An anchor position that was refused for clearance reasons."""

    x: float
    y: float
    reason: str


@dataclass
class PlacedAnchor:
    """An anchor via that was placed."""

    x: float
    y: float


@dataclass
class RunSummary:
    """Per-run anchoring accounting (#4319, tier 2).

    One entry per chained run on the target layer -- including runs that were
    NOT anchored (filtered by ``min_run_length_mm`` or, in default mode, every
    run but the longest). This lets an agent distinguish "fully reinforced"
    from "2 of 31 runs reinforced" straight from ``--format json``.

    ``segment_count`` and ``length_mm`` reflect the *merged* geometric run
    (contiguous collinear fragments coalesced -- tier 3), not the raw
    fragment count.
    """

    run_index: int
    length_mm: float
    segment_count: int
    #: Number of anchor positions this run demands (endpoints + spacing).
    anchors_needed: int
    #: Anchor positions actually covered on this run (placed this pass, or
    #: already covered by a coincident anchor from another run).
    anchors_placed: int
    #: Anchor positions on this run that were hard-refused (no clear nudge).
    anchors_refused: int

    @property
    def fully_reinforced(self) -> bool:
        return self.anchors_needed > 0 and self.anchors_placed >= self.anchors_needed

    @property
    def partial(self) -> bool:
        return 0 < self.anchors_placed < self.anchors_needed

    @property
    def unanchored(self) -> bool:
        return self.anchors_placed == 0


@dataclass
class ReinforceResult:
    """Outcome of a :func:`reinforce_net` pass."""

    net_name: str
    wire_gauge_awg: int
    anchor_drill_mm: float
    anchor_pad_mm: float
    spacing_mm: float
    layer: str = ""
    #: Segments chained into the primary anchored (longest anchored) run,
    #: after the tier-3 collinear merge.
    run_segment_count: int = 0
    #: Cumulative arc length of the primary anchored run, in mm.
    run_length_mm: float = 0.0
    #: Number of chained runs on this net that were NOT anchored (branches /
    #: disconnected / below-threshold runs -- reported, not silently dropped).
    unhandled_runs: int = 0
    #: Per-run anchoring accounting (one entry per chained run). Additive over
    #: the legacy whole-net fields above.
    runs: list[RunSummary] = field(default_factory=list)
    placed: list[PlacedAnchor] = field(default_factory=list)
    refused: list[RefusedAnchor] = field(default_factory=list)

    @property
    def placed_count(self) -> int:
        return len(self.placed)

    @property
    def refused_count(self) -> int:
        return len(self.refused)

    @property
    def runs_fully_reinforced(self) -> int:
        return sum(1 for r in self.runs if r.fully_reinforced)

    @property
    def runs_partial(self) -> int:
        return sum(1 for r in self.runs if r.partial)

    @property
    def runs_unanchored(self) -> int:
        return sum(1 for r in self.runs if r.unanchored)


class ReinforceError(ValueError):
    """Raised when the reinforce pass cannot proceed (e.g. unknown net)."""


def _seg_length(seg: Segment) -> float:
    (x1, y1), (x2, y2) = seg.start, seg.end
    return math.hypot(x2 - x1, y2 - y1)


def _to_router_segment(seg: Segment) -> RouterSegment:
    """Adapt a schema ``Segment`` to the chainer's ``router`` ``Segment``.

    The junction-aware chainer (:func:`sort_into_chains`) operates on
    :class:`kicad_tools.router.primitives.Segment` (``x1/y1/x2/y2``), while
    a parsed board yields :class:`kicad_tools.schema.pcb.Segment`
    (``start``/``end`` tuples). This trivial field mapping lets the pass
    reuse the single hardened chaining implementation rather than fork it.
    """
    (x1, y1), (x2, y2) = seg.start, seg.end
    # ``sort_into_chains`` only reads the ``x1/y1/x2/y2`` endpoints; the
    # ``layer`` field (a ``CopperLayer`` enum on the router type) is carried
    # along but never consulted, so a placeholder is fine. Callers pre-filter
    # segments to a single layer before chaining.
    return RouterSegment(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        width=seg.width,
        layer=CopperLayer.F_CU,
        net=seg.net_number,
        net_name=seg.net_name,
    )


def _chain_polylines(segments: list[Segment]) -> list[list[Segment]]:
    """Chain schema segments into ordered, junction-split polylines.

    Returns a list of runs, each an ordered list of the *original* schema
    ``Segment`` objects walked endpoint-to-endpoint. Junctions (degree>=3)
    split runs so branched nets are not silently merged.
    """
    if not segments:
        return []

    router_segs = [_to_router_segment(s) for s in segments]
    chains = sort_into_chains(router_segs)

    # Map each returned (possibly flipped) router segment back to the
    # original schema segment by index. ``sort_into_chains`` preserves the
    # ``width``/``layer``/``net`` fields and only ever flips endpoints, so we
    # match on the unordered endpoint pair + width to recover the index.
    remaining = list(range(len(segments)))

    def _match(rseg: RouterSegment) -> int:
        target_pts = frozenset(
            {
                (round(rseg.x1, 6), round(rseg.y1, 6)),
                (round(rseg.x2, 6), round(rseg.y2, 6)),
            }
        )
        for idx in remaining:
            s = segments[idx]
            pts = frozenset(
                {
                    (round(s.start[0], 6), round(s.start[1], 6)),
                    (round(s.end[0], 6), round(s.end[1], 6)),
                }
            )
            if pts == target_pts and abs(s.width - rseg.width) < 1e-9:
                return idx
        # Fallback: endpoint-only match (width drift tolerance).
        for idx in remaining:
            s = segments[idx]
            pts = frozenset(
                {
                    (round(s.start[0], 6), round(s.start[1], 6)),
                    (round(s.end[0], 6), round(s.end[1], 6)),
                }
            )
            if pts == target_pts:
                return idx
        return -1

    result: list[list[Segment]] = []
    for chain in chains:
        run: list[Segment] = []
        for rseg in chain:
            idx = _match(rseg)
            if idx < 0:
                continue
            remaining.remove(idx)
            # Re-orient the original schema segment to match the chained
            # walk direction so the arc-length walk is continuous.
            orig = segments[idx]
            if abs(orig.start[0] - rseg.x1) < 1e-6 and abs(orig.start[1] - rseg.y1) < 1e-6:
                run.append(orig)
            else:
                run.append(
                    Segment(
                        start=orig.end,
                        end=orig.start,
                        width=orig.width,
                        layer=orig.layer,
                        net_number=orig.net_number,
                        net_name=orig.net_name,
                        uuid=orig.uuid,
                    )
                )
        if run:
            result.append(run)
    return result


def _run_length(run: list[Segment]) -> float:
    return sum(_seg_length(s) for s in run)


def _cumulative_arc(points: list[tuple[float, float]]) -> list[float]:
    """Cumulative arc length at each vertex of a polyline."""
    cum: list[float] = [0.0]
    for i in range(1, len(points)):
        (x0, y0), (x1, y1) = points[i - 1], points[i]
        cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
    return cum


def _anchor_arc_targets(total: float, spacing: float) -> list[float]:
    """Arc-length sample points: 0, spacing, 2*spacing, ... and the endpoint.

    Always includes the final endpoint (so both true endpoints are anchored
    even if the last interval is shorter than ``spacing``).
    """
    if total <= 0:
        return [0.0]
    targets: list[float] = []
    d = 0.0
    while d < total - 1e-9:
        targets.append(d)
        d += spacing
    targets.append(total)
    return targets


def _point_at_arc(
    points: list[tuple[float, float]], cum: list[float], t: float
) -> tuple[float, float]:
    """Interpolate the point at arc length ``t`` along a polyline."""
    if len(points) < 2:
        return points[0]
    total = cum[-1]
    t = max(0.0, min(total, t))
    seg_i = 0
    while seg_i < len(cum) - 2 and cum[seg_i + 1] < t:
        seg_i += 1
    seg_len = cum[seg_i + 1] - cum[seg_i]
    if seg_len <= 1e-12:
        return points[seg_i]
    frac = max(0.0, min(1.0, (t - cum[seg_i]) / seg_len))
    (x0, y0), (x1, y1) = points[seg_i], points[seg_i + 1]
    return (x0 + frac * (x1 - x0), y0 + frac * (y1 - y0))


def _anchor_positions(
    points: list[tuple[float, float]], spacing: float
) -> list[tuple[float, float]]:
    """Sample anchor positions along a polyline by arc length.

    Places anchors at arc-length 0, ``spacing``, ``2*spacing``, ... and
    always at the final endpoint (so both true endpoints are anchored even
    if the last interval is shorter than ``spacing``).
    """
    if len(points) < 2:
        return list(points)
    if spacing <= 0:
        raise ReinforceError(f"spacing must be positive, got {spacing}")
    cum = _cumulative_arc(points)
    return [_point_at_arc(points, cum, t) for t in _anchor_arc_targets(cum[-1], spacing)]


@dataclass
class _RunGeom:
    """Merged geometry of one chained run (tier-3 collinear-coalesced)."""

    points: list[tuple[float, float]]
    segment_count: int
    length_mm: float


def _run_geometry(run: list[Segment]) -> _RunGeom:
    """Collapse contiguous collinear fragments within an oriented run (#4319).

    Tier 3: the junction-aware chainer (:func:`sort_into_chains`) already
    walks collinear degree-2 fragments head-to-tail into ONE run, but leaves
    them as separate ``Segment`` objects, so ``run_segment_count`` reports the
    raw fragment count (e.g. a straight branch expressed as many short
    octilinear pieces). This step reuses
    :func:`kicad_tools.router.optimizer.algorithms.merge_collinear` to coalesce
    those fragments so the reported ``segment_count`` reflects the true
    geometric run.

    The merge operates ONLY within an already-split run, so it never bridges a
    junction (degree>=3) -- the #2389 no-branch-drop guarantee that
    ``sort_into_chains`` provides is preserved. It removes only *collinear*
    interior vertices, so the arc-length geometry (and therefore every anchor
    position) is byte-for-byte unchanged.
    """
    if not run:
        return _RunGeom(points=[], segment_count=0, length_mm=0.0)
    router_segs = [_to_router_segment(s) for s in run]
    merged = merge_collinear(router_segs, _MERGE_CONFIG)
    points: list[tuple[float, float]] = [(merged[0].x1, merged[0].y1)]
    length = 0.0
    for m in merged:
        points.append((m.x2, m.y2))
        length += math.hypot(m.x2 - m.x1, m.y2 - m.y1)
    return _RunGeom(points=points, segment_count=len(merged), length_mm=length)


def _nudge_arc_candidates(t: float, total: float, spacing: float):
    """Yield the base arc length then symmetric bounded nudges (#4319, tier 2).

    Order: the base position first, then +/- increments outward, up to
    ``NUDGE_MAX_FRACTION_OF_SPACING * spacing`` on each side. Candidates are
    clamped to ``[0, total]``.
    """
    yield max(0.0, min(total, t))
    max_off = spacing * NUDGE_MAX_FRACTION_OF_SPACING
    if max_off <= 0 or NUDGE_STEPS <= 0:
        return
    step = max_off / NUDGE_STEPS
    for k in range(1, NUDGE_STEPS + 1):
        off = step * k
        for cand in (t + off, t - off):
            if -1e-9 <= cand <= total + 1e-9:
                yield max(0.0, min(total, cand))


def _too_close(p: tuple[float, float], placed: list[tuple[float, float]], floor: float) -> bool:
    """True when ``p`` is within ``floor`` (center-to-center) of a placed anchor."""
    return any(math.hypot(p[0] - q[0], p[1] - q[1]) < floor - 1e-9 for q in placed)


def _position_clear(
    x: float,
    y: float,
    anchor_pad: float,
    anchor_drill: float,
    rules: DesignRules,
    other: _OtherNetCopper,
) -> bool:
    """Clearance re-check of a candidate anchor against all other-net copper."""
    return point_clear_of_copper(
        x,
        y,
        via_size=anchor_pad,
        clearance=rules.min_clearance_mm,
        other_net_tracks=other.tracks,
        other_net_vias=other.vias,
        other_net_pads=other.pads,
        other_net_filled_polygons=other.filled_polygons,
        via_drill=anchor_drill,
        other_net_drills=other.drills,
        min_hole_to_hole=rules.min_hole_to_hole_mm,
    )


class _TrackObstacle:
    """Minimal duck-typed track for ``point_clear_of_copper``."""

    __slots__ = ("start_x", "start_y", "end_x", "end_y", "width")

    def __init__(self, sx: float, sy: float, ex: float, ey: float, width: float) -> None:
        self.start_x = sx
        self.start_y = sy
        self.end_x = ex
        self.end_y = ey
        self.width = width


class _FilledPolyObstacle:
    """Minimal duck-typed filled polygon for ``point_clear_of_copper``."""

    __slots__ = ("points", "min_x", "min_y", "max_x", "max_y")

    def __init__(self, points: list[tuple[float, float]]) -> None:
        self.points = points
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        self.min_x = min(xs)
        self.min_y = min(ys)
        self.max_x = max(xs)
        self.max_y = max(ys)


@dataclass
class _OtherNetCopper:
    """Collected other-net copper for the clearance re-check."""

    tracks: list[TrackSegmentLike] = field(default_factory=list)
    vias: list[tuple[float, float, float, int]] = field(default_factory=list)
    pads: list[ForeignPadTuple] = field(default_factory=list)
    filled_polygons: list[FilledPolygonLike] = field(default_factory=list)
    drills: list[tuple[float, float, float, int]] = field(default_factory=list)


def _collect_other_net_copper(pcb: PCB, target_net: int) -> _OtherNetCopper:
    """Build the other-net obstacle set (everything NOT on ``target_net``)."""
    other = _OtherNetCopper()

    for seg in pcb.segments:
        if seg.net_number == target_net:
            continue
        (sx, sy), (ex, ey) = seg.start, seg.end
        other.tracks.append(_TrackObstacle(sx, sy, ex, ey, seg.width))

    for via in pcb.vias:
        if via.net_number == target_net:
            continue
        vx, vy = via.position
        other.vias.append((vx, vy, via.size, via.net_number))
        if via.drill > 0:
            other.drills.append((vx, vy, via.drill, via.net_number))

    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_number == target_net:
                continue
            pos = pcb.get_pad_position(fp.reference, pad.number)
            if pos is None:
                continue
            px, py = pos
            # Effective bounding radius of the pad (disc model).
            radius = max(pad.size) / 2.0 if pad.size else 0.0
            other.pads.append((px, py, radius, pad.net_number))
            if pad.drill > 0:
                other.drills.append((px, py, pad.drill, pad.net_number))

    for zone in pcb.zones:
        if zone.net_number == target_net:
            continue
        for poly in zone.filled_polygons:
            if len(poly) >= 3:
                other.filled_polygons.append(_FilledPolyObstacle(list(poly)))

    return other


def reinforce_net(
    pcb: PCB,
    net_name: str,
    *,
    wire_gauge_awg: int = DEFAULT_WIRE_GAUGE_AWG,
    spacing_mm: float = DEFAULT_ANCHOR_SPACING_MM,
    design_rules: DesignRules | None = None,
    layer: str | None = None,
    dry_run: bool = False,
    all_runs: bool = False,
    min_run_length_mm: float | None = None,
) -> ReinforceResult:
    """Emit a spaced same-net PTH anchor row along a routed net's trace.

    Post-route pass: chains the target net's routed segments into ordered
    polylines and places gauge-sized plated-PTH anchor vias at both endpoints
    and every ``spacing_mm`` along the selected run(s). Each candidate anchor
    is clearance-checked against all other-net copper; a colliding candidate is
    nudged a bounded distance along the run's arc-length axis and, only if no
    clear position is found, refused (reported, not placed).

    By default (``all_runs=False``) only the single longest run is anchored --
    the historical MVP behavior. A multi-branch HV net (T/Y fan-out at
    junctions) splits into several runs; passing ``all_runs=True`` anchors
    *every* run so N-1 branches are no longer left un-reinforced (#4319).

    Args:
        pcb: A loaded :class:`~kicad_tools.schema.pcb.PCB`.
        net_name: Target net name (must have routed copper).
        wire_gauge_awg: Buttress-wire gauge (default 16 AWG). Sizes the
            anchor drill/pad.
        spacing_mm: Arc-length spacing between mid-run anchors (mm).
        design_rules: Manufacturer :class:`DesignRules` supplying
            ``min_annular_ring_mm`` (anchor pad sizing), ``min_clearance_mm``
            (other-net clearance), and ``min_hole_to_hole_mm``. Falls back to
            JLCPCB-tier-typical values when ``None``.
        layer: Restrict to this copper layer. When ``None``, the layer with
            the most cumulative segment length for the net is chosen.
        dry_run: When True, compute the placed/refused plan without
            mutating the board (no vias added).
        all_runs: When True, anchor every chained run on the target layer
            (subject to ``min_run_length_mm``) instead of only the longest.
        min_run_length_mm: When set, only runs at least this long (mm) are
            anchored; shorter runs are still reported (never silently
            dropped). Composes with ``all_runs`` -- filter first, then anchor
            all that survive.

    Returns:
        A :class:`ReinforceResult` summarising placed/refused anchors, the
        primary anchored run, and a per-run :class:`RunSummary` list.

    Raises:
        ReinforceError: If the net does not exist, has no routed copper, or
            ``spacing_mm`` is non-positive.
    """
    if spacing_mm <= 0:
        raise ReinforceError(f"spacing_mm must be positive, got {spacing_mm}")

    rules = design_rules or _FALLBACK_DESIGN_RULES

    # Resolve the net. Accept a net that exists in the net table OR that has
    # segments carrying its name (some boards carry name-only segments).
    net_obj = pcb.get_net_by_name(net_name)
    net_segments = [
        s
        for s in pcb.segments
        if s.net_name == net_name or (net_obj is not None and s.net_number == net_obj.number)
    ]
    if net_obj is None and not net_segments:
        raise ReinforceError(f"net {net_name!r} not found on board")

    target_net_number = (
        net_obj.number
        if net_obj is not None
        else (net_segments[0].net_number if net_segments else 0)
    )

    anchor_drill = anchor_drill_for_awg(wire_gauge_awg)
    anchor_pad = anchor_pad_for_drill(anchor_drill, rules.min_annular_ring_mm)

    result = ReinforceResult(
        net_name=net_name,
        wire_gauge_awg=wire_gauge_awg,
        anchor_drill_mm=anchor_drill,
        anchor_pad_mm=anchor_pad,
        spacing_mm=spacing_mm,
    )

    if not net_segments:
        raise ReinforceError(f"net {net_name!r} has no routed copper to reinforce")

    # Group by layer; pick the layer with the most cumulative length (MVP).
    by_layer: dict[str, list[Segment]] = {}
    for s in net_segments:
        by_layer.setdefault(s.layer, []).append(s)

    if layer is not None:
        if layer not in by_layer:
            raise ReinforceError(f"net {net_name!r} has no routed copper on layer {layer!r}")
        target_layer = layer
    else:
        target_layer = max(by_layer, key=lambda lay: sum(_seg_length(s) for s in by_layer[lay]))
    result.layer = target_layer

    runs = _chain_polylines(by_layer[target_layer])
    if not runs:
        raise ReinforceError(f"net {net_name!r} produced no walkable polyline")

    # Longest-first so the "primary" run (default mode) is runs[0] and the
    # per-run summary is stably ordered.
    runs.sort(key=_run_length, reverse=True)

    # Tier 3: coalesce contiguous collinear fragments within each run so the
    # reported segment count reflects true geometric runs (anchor positions
    # unchanged -- merge only removes collinear interior vertices).
    geoms = [_run_geometry(run) for run in runs]

    # Selection: filter by min length (report -- do not drop -- shorter runs),
    # then anchor either all survivors (all_runs) or just the longest.
    if min_run_length_mm is not None:
        eligible = [i for i, g in enumerate(geoms) if g.length_mm >= min_run_length_mm]
    else:
        eligible = list(range(len(geoms)))
    anchored_idx: set[int] = set(eligible if all_runs else eligible[:1])

    # Backward-compat whole-net fields describe the primary (longest anchored)
    # run; zero when everything was filtered out.
    if anchored_idx:
        primary = geoms[min(anchored_idx)]
        result.run_segment_count = primary.segment_count
        result.run_length_mm = primary.length_mm
    result.unhandled_runs = len(geoms) - len(anchored_idx)

    other = _collect_other_net_copper(pcb, target_net_number)

    # Cross-run dedup floor: two placed anchors must be at least a pad diameter
    # apart center-to-center (no overlapping copper), which also satisfies the
    # hole-to-hole floor. Shared junction vertices between fanned-out runs land
    # at the same coordinate and are anchored once.
    dedup_floor = max(anchor_pad, anchor_drill + rules.min_hole_to_hole_mm)
    placed_positions: list[tuple[float, float]] = []

    for i, g in enumerate(geoms):
        cum = _cumulative_arc(g.points)
        targets = _anchor_arc_targets(g.length_mm, spacing_mm)
        run_placed = 0
        run_refused = 0

        if i in anchored_idx:
            for t in targets:
                base = _point_at_arc(g.points, cum, t)
                # Already covered by a coincident anchor (e.g. a shared
                # junction vertex from another run) -- count as reinforced,
                # not a new placement and not a refusal.
                if _too_close(base, placed_positions, dedup_floor):
                    run_placed += 1
                    continue

                chosen: tuple[float, float] | None = None
                for cand_t in _nudge_arc_candidates(t, g.length_mm, spacing_mm):
                    cx, cy = _point_at_arc(g.points, cum, cand_t)
                    if _too_close((cx, cy), placed_positions, dedup_floor):
                        continue
                    if _position_clear(cx, cy, anchor_pad, anchor_drill, rules, other):
                        chosen = (cx, cy)
                        break

                if chosen is None:
                    bx, by = base
                    result.refused.append(
                        RefusedAnchor(
                            x=bx,
                            y=by,
                            reason="other-net clearance/hole-to-hole violation",
                        )
                    )
                    run_refused += 1
                    continue

                cx, cy = chosen
                if not dry_run:
                    pcb.add_via(
                        cx,
                        cy,
                        size=anchor_pad,
                        drill=anchor_drill,
                        layers=("F.Cu", "B.Cu"),
                        net=net_name,
                        dedupe=True,
                    )
                result.placed.append(PlacedAnchor(x=cx, y=cy))
                placed_positions.append((cx, cy))
                run_placed += 1

        result.runs.append(
            RunSummary(
                run_index=i,
                length_mm=g.length_mm,
                segment_count=g.segment_count,
                anchors_needed=len(targets),
                anchors_placed=run_placed,
                anchors_refused=run_refused,
            )
        )

    return result
