"""Buttress-wire reinforcement: same-net PTH anchors along a routed net.

Unit A of the #4218 buttress-wire reinforcement design (Part 2 of the #4215
ampacity feature), reworked for the owner's intended build in #8741. This is
a **post-route** pass that reads an already-routed board, walks the routed
copper of one named net into ordered polylines, and emits **same-net**
plated through-hole (PTH) anchor vias for a buttress wire.

The intended build: a hand-bent solid-core copper wire (default 16 AWG) is
laid along a high-current trace and soldered to that trace along its whole
length, held by **exactly two anchor holes, one at each end of the wire**.
One wire stays on one copper layer, so a current path that changes layer is
two wires.

Two placement modes (``anchor_mode``, Issue #8742):

* ``"ends"`` (the default): the net's copper is split into **legs** -- a
  maximal run on ONE copper layer with no junction inside it -- and each leg
  gets exactly two anchors, one near each end, none in between. Each end
  anchor's pad edge sits at least ``end_offset_mm`` clear, in straight-line
  distance, of the same-net copper the leg end lands on: a component pad or
  via the end of its track overlaps, or the whole trunk a branch tees off (a
  leg that bends near its end moves the anchor further inward until that
  holds). Both anchors are decided together: a leg with no room for two (too
  short, or folding back so they would overlap) is reported ``too_short``
  and gets none, and a leg ends with one anchor (``partial``) only when a
  clearance / hole-to-hole rule refused the other. A Y junction is a trunk (the longest
  through-path) plus a branch; loops and higher-degree junctions are
  reported ``unhandled``, never guessed. The per-leg result
  (:class:`LegSummary`, on :attr:`ReinforceResult.legs`) keeps each leg's
  polyline and its two anchor positions.
* ``"spaced"``: the historical row of anchors every ``spacing_mm`` along the
  longest run (or every run with ``all_runs``), unchanged.

Until #8743 lands, anchors are still plain same-net vias that inherit the
board's via tenting, and no solder-mask opening is emitted along the run, so
the trace is not yet exposed for soldering.

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
  :class:`kicad_tools.router.primitives.Segment`.

Out of scope (later units, tracked on #4218 / #8741): mask-opening channel
and mask-open anchors (#8743), wire-list sidecar (#8744), net-class
``reinforced`` wiring (Unit C), ampacity-credit coupling in #4217 (Unit E).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from kicad_tools.core.geometry import rotate_pad_offset
from kicad_tools.core.types import CopperLayer
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.physics.wire_gauge import (
    DEFAULT_WIRE_GAUGE_AWG,
    anchor_drill_for_awg,
    anchor_pad_for_drill,
)
from kicad_tools.router.board_clearance_rules import BoardClearanceRules
from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    reinforcement_eligible_segment_ids,
    reinforcement_path_resolutions,
)
from kicad_tools.router.optimizer.algorithms import merge_collinear
from kicad_tools.router.optimizer.chain import sort_into_chains
from kicad_tools.router.optimizer.config import OptimizationConfig
from kicad_tools.router.pairwise_clearance import PairwiseClearanceTable
from kicad_tools.router.primitives import Segment as RouterSegment
from kicad_tools.router.via_clearance import (
    FilledPolygonLike,
    ForeignPadTuple,
    TrackSegmentLike,
    drill_hole_to_hole_clear,
    point_clear_of_copper,
)
from kicad_tools.schema.pcb import PCB, Pad, Segment

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

# Anchor placement modes (Issue #8742). ``ends`` is the default: two anchors
# per single-layer leg, one at each end. ``spaced`` is the historical row.
ANCHOR_MODE_ENDS = "ends"
ANCHOR_MODE_SPACED = "spaced"
ANCHOR_MODES = (ANCHOR_MODE_ENDS, ANCHOR_MODE_SPACED)
DEFAULT_ANCHOR_MODE = ANCHOR_MODE_ENDS

# ``ends`` mode: minimum straight-line clearance (mm) between an end anchor's
# pad edge and the same-net copper the leg end lands on (component pad, via,
# or the trunk a branch tees off). Proposed default pending the
# operator's answer on #8741; a one-line change to override.
DEFAULT_END_OFFSET_MM = 1.0

# ``ends`` mode nudge: a refused end anchor is searched INWARD along the leg
# only (never outward onto the pad), by at most ``END_NUDGE_MAX_MM`` in
# ``END_NUDGE_STEPS`` increments, and never past the other end's anchor.
END_NUDGE_MAX_MM = 2.0
END_NUDGE_STEPS = 8

# Leg kinds and statuses reported on :class:`LegSummary` (Issue #8742).
LEG_KIND_RUN = "run"
LEG_KIND_TRUNK = "trunk"
LEG_KIND_BRANCH = "branch"
LEG_FULL = "full"
LEG_PARTIAL = "partial"
LEG_UNANCHORED = "unanchored"
LEG_TOO_SHORT = "too_short"
LEG_UNHANDLED = "unhandled"

# Step (mm) of the march that finds where a leg leaves the copper it starts
# on; the crossing is then bisected to well below a micron.
_EXIT_MARCH_STEP_MM = 0.02

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


# Governing-rule tags carried on :attr:`RefusedAnchor.rule` (#8085).
RULE_HOLE_TO_HOLE = "hole_to_hole"
RULE_DRU_CLASS_FLOOR = "dru_class_floor"
RULE_VOLTAGE_MAP_PAIRWISE = "voltage_map_pairwise"
RULE_DRU_CONDITIONAL = "dru_conditional"
#: ``ends`` mode: the candidate's pad would overlap an anchor already placed
#: for another leg (centre-to-centre below one anchor pad; Issue #8742).
RULE_ANCHOR_SPACING = "anchor_spacing"

_RULE_EPS = 1e-9


@dataclass
class RefusedAnchor:
    """An anchor position that was refused for clearance reasons."""

    x: float
    y: float
    reason: str
    #: Governing rule (``hole_to_hole`` / ``dru_class_floor`` /
    #: ``dru_conditional`` / ``voltage_map_pairwise``, and in ``ends`` mode
    #: ``anchor_spacing``) of the first candidate tried at this position.
    rule: str = ""
    #: Requirement of that rule (mm) and the measured gap (mm); ``0.0`` unknown.
    required_mm: float = 0.0
    actual_mm: float = 0.0
    #: Name of the obstacle net (empty for hole-to-hole against same-net holes).
    obstacle_net: str = ""


@dataclass
class PlacedAnchor:
    """An anchor via that was placed."""

    x: float
    y: float


@dataclass
class LegSummary:
    """One buttress-wire leg in ``ends`` mode (Issue #8742).

    A leg is a maximal run of the net's eligible copper on ONE copper layer
    with no junction inside it except at its ends -- one hand-bent wire. This
    is the public seam later slices consume (#8743 opens the solder mask
    along :attr:`polyline`; #8744 lists the wires), so the polyline is kept,
    not only the anchors.

    Coordinates are board-relative, like :class:`PlacedAnchor`.
    """

    net: str
    layer: str
    #: ``run`` (a plain leg), ``trunk`` (the longest through-path across a Y
    #: junction) or ``branch`` (the third arm of that Y, oriented from the
    #: junction to its free end).
    kind: str
    #: Ordered centreline vertices, collinear interior vertices removed.
    polyline: list[tuple[float, float]]
    length_mm: float
    #: The anchors actually placed on this leg this pass, in leg order
    #: (0, 1 or 2). In ``full`` status: ``[start-end anchor, far-end anchor]``.
    anchors: list[PlacedAnchor] = field(default_factory=list)
    #: ``full`` (2 placed) / ``partial`` (1: the other refused by a rule,
    #: listed in ``refused``) / ``unanchored`` (0: refused or
    #: not selected) / ``too_short`` / ``unhandled`` (loop, degree>=4 or
    #: multi-junction topology, or copper on a layer this call does not
    #: process).
    status: str = LEG_UNANCHORED
    #: Why the leg is not ``full`` (empty when it is).
    reason: str = ""
    #: Widest track width (mm) on the leg.
    width_mm: float = 0.0
    #: Names of the declared current paths covering this leg (empty without
    #: ``current_paths``).
    paths: list[str] = field(default_factory=list)


@dataclass
class UnresolvedPath:
    """A declared current path on the net that could not be used (#8742)."""

    path: str
    status: str
    reason: str


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
    #: Set (Issue #4980) to a human-readable reason when a ``current_paths``
    #: declaration excluded this run from anchoring -- it was not fully
    #: covered by a resolved, reinforcement-eligible ``CurrentPathSpec``
    #: (e.g. a Kelvin sense tap, an unresolved trunk declaration, or copper
    #: no declared path covers at all). ``None`` when the run was not
    #: excluded -- either no ``current_paths`` were supplied to
    #: :func:`reinforce_net` at all (legacy, ungated behavior), or the run
    #: passed the gate.
    path_excluded_reason: str | None = None

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
    #: ``ends`` or ``spaced`` (Issue #8742).
    anchor_mode: str = DEFAULT_ANCHOR_MODE
    #: ``ends`` mode end offset (mm); reported in ``spaced`` mode too.
    end_offset_mm: float = DEFAULT_END_OFFSET_MM
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
    #: ``ends`` mode: one entry per leg (empty in ``spaced`` mode).
    legs: list[LegSummary] = field(default_factory=list)
    #: Declared current paths on this net that did not resolve (both modes).
    unresolved_paths: list[UnresolvedPath] = field(default_factory=list)

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


def _chain_polylines(
    segments: list[Segment], *, original_ids: dict[int, int] | None = None
) -> list[list[Segment]]:
    """Chain schema segments into ordered, junction-split polylines.

    Returns ordered schema segments walked endpoint-to-endpoint, copying
    segments that need reversing. When supplied, ``original_ids`` maps each
    returned object's id to its original PCB object's id. Junctions
    (degree>=3) split runs so branched nets are not silently merged.
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
            if original_ids is not None:
                original_ids[id(run[-1])] = id(orig)
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


@dataclass(frozen=True)
class _Violation:
    """The rule an anchor candidate breaks, with required vs measured gap."""

    rule: str
    required_mm: float
    actual_mm: float
    obstacle_net: str = ""


@dataclass
class _ClearancePolicy:
    """Per-obstacle-net required clearance for the target net (#8085).

    ``required = max(fab floor, target class floor, obstacle class floor,
    pairwise voltage-map requirement)``; the pairwise term is only present
    when a :class:`PairwiseClearanceTable` was supplied.
    """

    fab_floor: float
    target_net: str
    class_floors: Mapping[str, float]
    pairwise: PairwiseClearanceTable | None
    #: The board's ``.kicad_pro`` / ``.kicad_dru`` resolver (#8494); its
    #: per-pair requirement (incl. condition-based custom rules) is max-ed in.
    board_rules: BoardClearanceRules | None = None

    def required(self, obstacle_net: str) -> tuple[float, str]:
        floor = max(
            self.fab_floor,
            self.class_floors.get(self.target_net, 0.0),
            self.class_floors.get(obstacle_net, 0.0),
        )
        pair = (
            self.pairwise.required_clearance(self.target_net, obstacle_net)
            if self.pairwise is not None and obstacle_net
            else 0.0
        )
        dru = (
            self.board_rules.net_requirement(self.target_net, [obstacle_net])
            if self.board_rules is not None
            else 0.0
        )
        if dru > max(floor, pair) + _RULE_EPS:
            return dru, RULE_DRU_CONDITIONAL
        if pair > floor + _RULE_EPS:
            return pair, RULE_VOLTAGE_MAP_PAIRWISE
        return floor, RULE_DRU_CLASS_FLOOR


def _group_clear(
    x: float, y: float, anchor_pad: float, clearance: float, group: _NetCopper
) -> bool:
    return point_clear_of_copper(
        x,
        y,
        via_size=anchor_pad,
        clearance=clearance,
        other_net_tracks=group.tracks,
        other_net_vias=group.vias,
        other_net_pads=group.pads,
        other_net_filled_polygons=group.filled_polygons,
    )


def _actual_gap(x: float, y: float, anchor_pad: float, required: float, group: _NetCopper) -> float:
    """Measured copper gap (mm) to ``group``, by bisecting the shared predicate."""
    lo, hi = 0.0, required
    if not _group_clear(x, y, anchor_pad, 0.0, group):
        return 0.0
    for _ in range(16):
        mid = (lo + hi) / 2.0
        if _group_clear(x, y, anchor_pad, mid, group):
            lo = mid
        else:
            hi = mid
    return lo


def _position_violation(
    x: float,
    y: float,
    anchor_pad: float,
    anchor_drill: float,
    rules: DesignRules,
    other: _OtherNetCopper,
    policy: _ClearancePolicy,
) -> _Violation | None:
    """Clearance re-check of a candidate anchor; ``None`` when it is clear.

    Hole-to-hole is checked against every drill on the board, same-net PTH
    pads and vias included (a same-net hole is still a separate drilled hole).
    Copper clearance is then checked per obstacle net against that net's own
    requirement (:class:`_ClearancePolicy`).
    """
    if other.all_drills and not drill_hole_to_hole_clear(
        x,
        y,
        anchor_drill,
        [(dx, dy, dd) for dx, dy, dd in other.all_drills],
        min_hole_to_hole=rules.min_hole_to_hole_mm,
    ):
        gap = min(
            math.hypot(dx - x, dy - y) - anchor_drill / 2.0 - dd / 2.0
            for dx, dy, dd in other.all_drills
        )
        return _Violation(RULE_HOLE_TO_HOLE, rules.min_hole_to_hole_mm, max(0.0, gap))

    for net_number, group in other.by_net.items():
        net = other.net_names.get(net_number, "")
        required, rule = policy.required(net)
        if not _group_clear(x, y, anchor_pad, required, group):
            return _Violation(rule, required, _actual_gap(x, y, anchor_pad, required, group), net)
    return None


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
    """Other-net copper grouped by net, plus every drill on the board."""

    by_net: dict[int, _NetCopper] = field(default_factory=dict)
    net_names: dict[int, str] = field(default_factory=dict)
    #: ``(x, y, drill_mm)`` of every via and PTH pad, same-net included.
    all_drills: list[tuple[float, float, float]] = field(default_factory=list)


@dataclass
class _NetCopper:
    """Copper of one obstacle net for the clearance re-check."""

    tracks: list[TrackSegmentLike] = field(default_factory=list)
    vias: list[tuple[float, float, float, int]] = field(default_factory=list)
    pads: list[ForeignPadTuple] = field(default_factory=list)
    filled_polygons: list[FilledPolygonLike] = field(default_factory=list)


def _collect_other_net_copper(
    pcb: PCB, target_net: int, anchor_drill: float, anchor_pad: float
) -> _OtherNetCopper:
    """Build the obstacle set: other-net copper by net, all drills (any net).

    A same-net via that already has this pass's anchor geometry is a previous
    anchor, not an obstacle: skipping it keeps a re-run idempotent (the same
    candidates are chosen and ``add_via(dedupe=True)`` drops the duplicates).
    """
    other = _OtherNetCopper()
    other.net_names = {n.number: n.name for n in pcb.nets.values()}

    def group(net_number: int) -> _NetCopper:
        return other.by_net.setdefault(net_number, _NetCopper())

    for seg in pcb.segments:
        if seg.net_number == target_net:
            continue
        (sx, sy), (ex, ey) = seg.start, seg.end
        group(seg.net_number).tracks.append(_TrackObstacle(sx, sy, ex, ey, seg.width))

    for via in pcb.vias:
        vx, vy = via.position
        own_anchor = (
            via.net_number == target_net
            and abs(via.drill - anchor_drill) < 1e-6
            and abs(via.size - anchor_pad) < 1e-6
        )
        if via.drill > 0 and not own_anchor:
            other.all_drills.append((vx, vy, via.drill))
        if via.net_number == target_net:
            continue
        group(via.net_number).vias.append((vx, vy, via.size, via.net_number))

    for fp in pcb.footprints:
        for pad in fp.pads:
            pos = pcb.get_pad_position(fp.reference, pad.number)
            if pos is None:
                continue
            px, py = pos
            if pad.drill > 0:
                other.all_drills.append((px, py, pad.drill))
            if pad.net_number == target_net:
                continue
            # Effective bounding radius of the pad (disc model).
            radius = max(pad.size) / 2.0 if pad.size else 0.0
            group(pad.net_number).pads.append((px, py, radius, pad.net_number))

    for zone in pcb.zones:
        if zone.net_number == target_net:
            continue
        for poly in zone.filled_polygons:
            if len(poly) >= 3:
                group(zone.net_number).filled_polygons.append(_FilledPolyObstacle(list(poly)))

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
    current_paths: Sequence[CurrentPathSpec] | None = None,
    net_class_clearances: Mapping[str, float] | None = None,
    pairwise_clearance: PairwiseClearanceTable | None = None,
    board_rules: BoardClearanceRules | None = None,
    anchor_mode: str = DEFAULT_ANCHOR_MODE,
    end_offset_mm: float = DEFAULT_END_OFFSET_MM,
) -> ReinforceResult:
    """Emit same-net PTH buttress-wire anchors along a routed net's trace.

    Post-route pass: chains the target net's routed segments into ordered
    polylines and places gauge-sized plated-PTH anchor vias. Each candidate
    anchor is clearance-checked against all other-net copper; a colliding
    candidate is nudged a bounded distance along the run and, only if no
    clear position is found, refused (reported, not placed).

    ``anchor_mode="ends"`` (default, Issue #8742) places exactly two anchors
    per single-layer leg, one near each end and none between, for a wire
    soldered along the trace and held only at its ends; see the module
    docstring and :class:`LegSummary`. With ``current_paths`` declared for the
    net, every leg of every resolved eligible path is anchored (on any copper
    layer, one leg per layer); without, one layer is processed per call and
    only the longest leg is anchored unless ``all_runs``.

    ``anchor_mode="spaced"`` is the historical behaviour: anchors at both
    endpoints and every ``spacing_mm`` along the selected run(s) of one layer.
    By default (``all_runs=False``) only the single longest run is anchored; a
    multi-branch net (T/Y fan-out at junctions) splits into several runs and
    ``all_runs=True`` anchors *every* run (#4319).

    Args:
        pcb: A loaded :class:`~kicad_tools.schema.pcb.PCB`.
        net_name: Target net name (must have routed copper).
        wire_gauge_awg: Buttress-wire gauge (default 16 AWG). Sizes the
            anchor drill/pad.
        spacing_mm: Arc-length spacing between mid-run anchors (mm);
            ``spaced`` mode only.
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
        current_paths: Optional declared branch-specific current-path
            intent (Issue #4980; see
            :mod:`kicad_tools.router.current_paths`). Specs not on
            ``net_name`` are ignored. When at least one spec targets
            ``net_name``, reinforcement gates to an **allow-list**: only
            runs fully covered by a resolved, reinforcement-eligible
            ``CurrentPathSpec`` may be anchored -- a declared sense/
            measurement/Kelvin branch (``reinforcement_eligible=False``),
            copper an ineligible-or-unresolved declaration cannot verify,
            and copper no declared path covers at all are all excluded and
            reported via :attr:`RunSummary.path_excluded_reason`, never
            silently anchored. ``None`` (the default) preserves the
            pre-#4980 ungated behavior.
        net_class_clearances: ``{net_name: mm}`` authored net-class clearance
            floors (the source ``kct check`` uses, #8085). The clearance to an
            other-net obstacle is at least the larger of the two nets' floors.
        board_rules: Optional :class:`BoardClearanceRules` (#8494): the board's
            ``.kicad_dru`` custom rules incl. condition-based ones, max-ed
            with the floors above; never lowers a requirement.
        pairwise_clearance: Optional ``--voltage-map`` pairwise table (#8085);
            when given, the requirement for (target, obstacle) is raised to
            the table's value. ``None`` keeps the scalar-floor behavior.
        anchor_mode: ``"ends"`` (default) or ``"spaced"`` (Issue #8742).
        end_offset_mm: ``ends`` mode: minimum straight-line clearance in mm
            from an end anchor's pad edge to the same-net copper the leg end
            lands on (default 1.0): a pad or via its track's end overlaps, or
            the whole trunk for a branch. The anchor starts that far along the
            leg past the copper edge and moves further inward while a bend
            brings it closer than that; a leg with no room for two such
            anchors, measured along the leg and straight-line, is
            ``too_short`` and gets none.

    Returns:
        A :class:`ReinforceResult` summarising placed/refused anchors, the
        primary anchored run, a per-run :class:`RunSummary` list, and (in
        ``ends`` mode) a per-leg :class:`LegSummary` list.

    Raises:
        ReinforceError: If the net does not exist, has no routed copper,
            ``spacing_mm`` is non-positive, ``end_offset_mm`` is negative or
            ``anchor_mode`` is unknown.
    """
    if spacing_mm <= 0:
        raise ReinforceError(f"spacing_mm must be positive, got {spacing_mm}")
    if anchor_mode not in ANCHOR_MODES:
        raise ReinforceError(
            f"anchor_mode must be one of {', '.join(ANCHOR_MODES)}, got {anchor_mode!r}"
        )
    if end_offset_mm < 0 or not math.isfinite(end_offset_mm):
        raise ReinforceError(f"end_offset_mm must be >= 0, got {end_offset_mm}")

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
        anchor_mode=anchor_mode,
        end_offset_mm=end_offset_mm,
    )

    if not net_segments:
        raise ReinforceError(f"net {net_name!r} has no routed copper to reinforce")

    # Declarations on this net that cannot be used are reported in both
    # modes, never silently skipped (Issue #8742).
    net_path_specs = [spec for spec in (current_paths or ()) if spec.net_name == net_name]
    partition = reinforcement_path_resolutions(pcb, net_path_specs) if net_path_specs else None
    if partition is not None:
        result.unresolved_paths = [
            UnresolvedPath(path=res.spec.name, status=res.status, reason=res.reason)
            for res in partition.unresolved
        ]

    if anchor_mode == ANCHOR_MODE_ENDS:
        if layer is not None and layer not in {s.layer for s in net_segments}:
            raise ReinforceError(f"net {net_name!r} has no routed copper on layer {layer!r}")
        return _reinforce_ends(
            pcb,
            result,
            net_segments=net_segments,
            target_net_number=target_net_number,
            eligible_resolutions=(partition.eligible if partition is not None else None),
            layer=layer,
            all_runs=all_runs,
            min_run_length_mm=min_run_length_mm,
            rules=rules,
            policy=_ClearancePolicy(
                fab_floor=rules.min_clearance_mm,
                target_net=net_name,
                class_floors=net_class_clearances or {},
                pairwise=pairwise_clearance,
                board_rules=board_rules,
            ),
            dry_run=dry_run,
        )

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

    # Chaining can reverse a segment by copying it. Carry its original PCB
    # identity through the walk so path eligibility remains physical.
    original_ids: dict[int, int] = {}
    runs = _chain_polylines(by_layer[target_layer], original_ids=original_ids)
    classified_runs: list[tuple[list[Segment], str | None]] = []
    if net_path_specs:
        allowed_ids = reinforcement_eligible_segment_ids(pcb, net_path_specs)
        excluded_reason = (
            "excluded by declared current-path intent (Issue #4980): not fully "
            "covered by a resolved, reinforcement-eligible CurrentPathSpec for "
            f"net {net_name!r}"
        )
        for run in runs:
            parts: list[tuple[list[Segment], str | None]] = []
            for seg in run:
                reason = None if original_ids[id(seg)] in allowed_ids else excluded_reason
                # A degree-two force pad can continue into a sense stub.
                # Split that eligibility boundary while retaining all of the
                # chainer's existing physical-junction boundaries.
                if not parts or parts[-1][1] != reason:
                    parts.append(([], reason))
                parts[-1][0].append(seg)
            classified_runs.extend(parts)
    else:
        classified_runs = [(run, None) for run in runs]
    if not classified_runs:
        raise ReinforceError(f"net {net_name!r} produced no walkable polyline")

    classified_runs.sort(key=lambda item: _run_length(item[0]), reverse=True)
    runs = [run for run, _ in classified_runs]
    geoms = [_run_geometry(run) for run in runs]
    path_excluded_reasons = {
        index: reason for index, (_, reason) in enumerate(classified_runs) if reason is not None
    }

    # Selection: filter by min length (report -- do not drop -- shorter runs),
    # then by the current-path gate (report -- do not drop -- excluded runs),
    # then anchor either all survivors (all_runs) or just the longest.
    if min_run_length_mm is not None:
        eligible = [i for i, g in enumerate(geoms) if g.length_mm >= min_run_length_mm]
    else:
        eligible = list(range(len(geoms)))
    eligible = [i for i in eligible if i not in path_excluded_reasons]
    anchored_idx: set[int] = set(eligible if all_runs else eligible[:1])

    # Backward-compat whole-net fields describe the primary (longest anchored)
    # run; zero when everything was filtered out.
    if anchored_idx:
        primary = geoms[min(anchored_idx)]
        result.run_segment_count = primary.segment_count
        result.run_length_mm = primary.length_mm
    result.unhandled_runs = len(geoms) - len(anchored_idx)

    other = _collect_other_net_copper(pcb, target_net_number, anchor_drill, anchor_pad)
    policy = _ClearancePolicy(
        fab_floor=rules.min_clearance_mm,
        target_net=net_name,
        class_floors=net_class_clearances or {},
        pairwise=pairwise_clearance,
        board_rules=board_rules,
    )

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
                first_violation: _Violation | None = None
                for cand_t in _nudge_arc_candidates(t, g.length_mm, spacing_mm):
                    cx, cy = _point_at_arc(g.points, cum, cand_t)
                    if _too_close((cx, cy), placed_positions, dedup_floor):
                        continue
                    violation = _position_violation(
                        cx, cy, anchor_pad, anchor_drill, rules, other, policy
                    )
                    if violation is None:
                        chosen = (cx, cy)
                        break
                    if first_violation is None:
                        first_violation = violation

                if chosen is None:
                    bx, by = base
                    v = first_violation
                    result.refused.append(
                        RefusedAnchor(
                            x=bx,
                            y=by,
                            reason="other-net clearance/hole-to-hole violation",
                            rule=v.rule if v else "",
                            required_mm=v.required_mm if v else 0.0,
                            actual_mm=v.actual_mm if v else 0.0,
                            obstacle_net=v.obstacle_net if v else "",
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
                path_excluded_reason=path_excluded_reasons.get(i),
            )
        )

    return result


# --------------------------------------------------------------------------
# ``ends`` mode (Issue #8742): two anchors per single-layer leg
# --------------------------------------------------------------------------


@dataclass
class _LegCandidate:
    """A leg before placement: oriented segments plus topology verdict."""

    layer: str
    kind: str
    run: list[Segment]
    #: ``LEG_UNHANDLED`` when topology (or the layer selection) rules the leg
    #: out before placement; ``None`` when it may be anchored.
    status: str | None = None
    reason: str = ""


def _run_points(run: list[Segment]) -> list[tuple[float, float]]:
    return [run[0].start] + [s.end for s in run]


def _reverse_run(run: list[Segment], original_ids: dict[int, int]) -> list[Segment]:
    """Walk ``run`` the other way, keeping each copy's original PCB identity."""
    out: list[Segment] = []
    for s in reversed(run):
        flipped = Segment(
            start=s.end,
            end=s.start,
            width=s.width,
            layer=s.layer,
            net_number=s.net_number,
            net_name=s.net_name,
            uuid=s.uuid,
        )
        original_ids[id(flipped)] = original_ids.get(id(s), id(s))
        out.append(flipped)
    return out


def _vertex(p: tuple[float, float]) -> tuple[float, float]:
    return (round(p[0], 4), round(p[1], 4))


def _derive_legs(
    segments: list[Segment], layer: str, original_ids: dict[int, int]
) -> list[_LegCandidate]:
    """Split one layer's copper into wire legs (Issue #8742).

    The junction-aware chainer already cuts the copper at every degree>=3
    vertex, so each chain is a junction-free run. Chains are then grouped
    into connected components by their end vertices:

    * a component that is a single chain is one ``run`` leg;
    * a single Y (exactly one degree-3 junction and three chains) becomes a
      ``trunk`` -- the two longest arms joined through the junction, i.e. the
      longest simple through-path -- plus one ``branch`` oriented from the
      junction to its free end;
    * a loop (a closed chain, or a component with as many chains as end
      vertices), a degree>=4 junction, or more than one junction is reported
      ``unhandled`` chain by chain: no single wire layout is implied, so none
      is guessed.
    """
    runs = _chain_polylines(segments, original_ids=original_ids)
    if not runs:
        return []

    ends = [(_vertex(r[0].start), _vertex(r[-1].end)) for r in runs]
    parent: dict[tuple[float, float], tuple[float, float]] = {}

    def find(v: tuple[float, float]) -> tuple[float, float]:
        parent.setdefault(v, v)
        while parent[v] != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v

    for a, b in ends:
        parent[find(a)] = find(b)

    components: dict[tuple[float, float], list[int]] = {}
    for i, (a, _) in enumerate(ends):
        components.setdefault(find(a), []).append(i)

    legs: list[_LegCandidate] = []
    for idxs in components.values():
        degree: dict[tuple[float, float], int] = {}
        for i in idxs:
            for v in ends[i]:
                degree[v] = degree.get(v, 0) + 1
        closed = any(ends[i][0] == ends[i][1] for i in idxs)
        junctions = [v for v, d in degree.items() if d >= 3]
        reason = ""
        if closed or len(idxs) >= len(degree):
            reason = "copper loop on this layer: a closed ring has no two wire ends"
        elif max(degree.values()) >= 4:
            reason = (
                f"junction of degree {max(degree.values())} on this layer: only a "
                "single Y (one trunk plus one branch) is handled"
            )
        elif len(junctions) > 1:
            reason = (
                f"{len(junctions)} junctions on one connected copper tree: only a "
                "single Y (one trunk plus one branch) is handled"
            )
        elif len(junctions) == 1 and len(idxs) != 3:
            reason = "unrecognised junction topology"
        if reason:
            legs.extend(
                _LegCandidate(layer, LEG_KIND_RUN, runs[i], status=LEG_UNHANDLED, reason=reason)
                for i in idxs
            )
            continue
        if not junctions:
            legs.extend(_LegCandidate(layer, LEG_KIND_RUN, runs[i]) for i in idxs)
            continue

        junction = junctions[0]
        # Every arm walked AWAY from the junction; the two longest form the
        # trunk (ties keep chainer order, so the choice is deterministic).
        arms = [
            runs[i]
            if _vertex(runs[i][0].start) == junction
            else _reverse_run(runs[i], original_ids)
            for i in idxs
        ]
        arms.sort(key=_run_length, reverse=True)
        trunk = _reverse_run(arms[0], original_ids) + arms[1]
        legs.append(_LegCandidate(layer, LEG_KIND_TRUNK, trunk))
        # The branch starts on the trunk: its end copper there is the WHOLE
        # trunk, found by ``_track_end_features`` (it touches the junction).
        legs.append(_LegCandidate(layer, LEG_KIND_BRANCH, arms[2]))
    return legs


def _pad_copper_distance(
    pad: Pad, center: tuple[float, float], point: tuple[float, float]
) -> float | None:
    """Signed straight-line distance (mm) from ``point`` to ``pad``'s copper.

    Positive outside the copper, ``<= 0`` on or inside it (board frame).
    ``None`` for a pad with no usable size.

    ``pad.rotation`` is absolute. Board offsets come from pad-local ones by
    :func:`~kicad_tools.core.geometry.rotate_pad_offset` (KiCad's negated-angle
    convention), so the inverse -- board to pad-local -- is the same function
    at the negated angle. Circle / oval (capsule) / rect / roundrect are exact;
    trapezoid and chamfered rect use their bounding rectangle, and custom pads
    the disc circumscribing the anchor rectangle -- both over-estimate the
    copper, which only moves the anchor further inward.
    """
    try:
        half_w = float(pad.size[0]) / 2.0
        half_h = float(pad.size[1]) / 2.0
    except (TypeError, ValueError, IndexError):
        return None
    if half_w <= 0.0 or half_h <= 0.0:
        return None
    lx, ly = rotate_pad_offset(
        point[0] - center[0], point[1] - center[1], -float(pad.rotation or 0.0)
    )
    shape = pad.shape
    if shape == "circle":
        return math.hypot(lx, ly) - max(half_w, half_h)
    radius = 0.0
    if shape == "oval":
        radius = min(half_w, half_h)
    elif shape == "roundrect":
        ratio = min(max(float(pad.roundrect_rratio), 0.0), 0.5)
        radius = 2.0 * min(half_w, half_h) * ratio
    elif shape not in ("rect", "trapezoid", "chamfered_rect"):
        return math.hypot(lx, ly) - math.hypot(half_w, half_h)
    # Rounded-box signed distance (radius 0 is the plain rectangle).
    qx = abs(lx) - (half_w - radius)
    qy = abs(ly) - (half_h - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - radius


def _pad_copper_contains(pad: Pad, center: tuple[float, float], point: tuple[float, float]) -> bool:
    """True when ``point`` lies on ``pad``'s copper (board frame)."""
    d = _pad_copper_distance(pad, center, point)
    return d is not None and d <= 1e-6


def _segment_distance(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Distance from ``p`` to the segment ``a``-``b``."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    seg2 = dx * dx + dy * dy
    if seg2 <= 0.0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    u = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / seg2))
    return math.hypot(p[0] - (a[0] + u * dx), p[1] - (a[1] + u * dy))


@dataclass
class _EndFeature:
    """Same-net copper a leg end can land on.

    A pad, a non-anchor via, or (for a branch's junction end) one trunk
    segment, modelled as a capsule from ``(x, y)`` to ``seg_end`` of
    half-width ``via_radius``.
    """

    x: float
    y: float
    #: Bounding radius, for a cheap reject before the exact test.
    bound: float
    layers: tuple[str, ...]
    pad: Pad | None = None
    via_radius: float = 0.0
    seg_end: tuple[float, float] | None = None

    def on_layer(self, layer: str) -> bool:
        return not self.layers or layer in self.layers or "*.Cu" in self.layers

    def distance(self, p: tuple[float, float]) -> float:
        """Straight-line distance (mm) from ``p`` to this copper; <= 0 inside."""
        if self.seg_end is not None:
            return _segment_distance(p, (self.x, self.y), self.seg_end) - self.via_radius
        if self.pad is not None:
            d = _pad_copper_distance(self.pad, (self.x, self.y), p)
            return math.inf if d is None else d
        return math.hypot(p[0] - self.x, p[1] - self.y) - self.via_radius

    def contains(self, p: tuple[float, float]) -> bool:
        if self.seg_end is None and math.hypot(p[0] - self.x, p[1] - self.y) > self.bound + 1e-6:
            return False
        return self.distance(p) <= 1e-6


def _same_net_end_features(
    pcb: PCB, target_net: int, anchor_drill: float, anchor_pad: float
) -> list[_EndFeature]:
    """Same-net pads and vias (previous anchors excluded) for end offsets."""
    features: list[_EndFeature] = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_number != target_net or pad.type == "np_thru_hole":
                continue
            off = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
            try:
                bound = math.hypot(float(pad.size[0]), float(pad.size[1])) / 2.0
            except (TypeError, ValueError, IndexError):
                continue
            # A plated through-hole pad has copper on every layer.
            layers = () if pad.type == "thru_hole" else tuple(pad.layers)
            features.append(
                _EndFeature(fp.position[0] + off[0], fp.position[1] + off[1], bound, layers, pad)
            )
    for via in pcb.vias:
        if via.net_number != target_net:
            continue
        if abs(via.drill - anchor_drill) < 1e-6 and abs(via.size - anchor_pad) < 1e-6:
            continue  # a previous anchor, not the copper a leg ends on
        vx, vy = via.position
        features.append(_EndFeature(vx, vy, via.size / 2.0, (), via_radius=via.size / 2.0))
    return features


def _exit_arc(points: list[tuple[float, float]], contains) -> float | None:
    """Arc length at which the polyline from ``points[0]`` leaves ``contains``.

    ``None`` when the copper covers the whole polyline.
    """
    cum = _cumulative_arc(points)
    total = cum[-1]
    prev = 0.0
    t = 0.0
    while t < total - 1e-12:
        t = min(total, t + _EXIT_MARCH_STEP_MM)
        if not contains(_point_at_arc(points, cum, t)):
            lo, hi = prev, t
            for _ in range(40):
                mid = (lo + hi) / 2.0
                if contains(_point_at_arc(points, cum, mid)):
                    lo = mid
                else:
                    hi = mid
            return hi
        prev = t
    return None


def _track_components(segments: list[Segment]) -> list[list[_EndFeature]]:
    """Same-net track copper as capsules, grouped into connected components.

    Two segments are connected when they share an end vertex or when one's
    end lies on the other's copper (a T onto a segment's middle). A leg end
    that touches any segment of a component lands on that whole component,
    so a branch's junction anchor is kept clear of the entire trunk, not
    just the trunk pieces that meet the junction (PR #8751 review).
    """
    feats = [
        _EndFeature(
            s.start[0], s.start[1], 0.0, (), via_radius=s.width / 2.0, seg_end=(s.end[0], s.end[1])
        )
        for s in segments
    ]
    parent = list(range(len(feats)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_vertex: dict[tuple[float, float], int] = {}
    for i, s in enumerate(segments):
        for v in (_vertex(s.start), _vertex(s.end)):
            j = by_vertex.setdefault(v, i)
            parent[find(i)] = find(j)
    # T-joins: bucket each capsule's bounding box on a coarse grid so an end
    # point is only tested against the segments near it.
    cell = 5.0
    buckets: dict[tuple[int, int], list[int]] = {}
    for i, s in enumerate(segments):
        r = s.width / 2.0
        x0, x1 = sorted((s.start[0], s.end[0]))
        y0, y1 = sorted((s.start[1], s.end[1]))
        for cx in range(math.floor((x0 - r) / cell), math.floor((x1 + r) / cell) + 1):
            for cy in range(math.floor((y0 - r) / cell), math.floor((y1 + r) / cell) + 1):
                buckets.setdefault((cx, cy), []).append(i)
    for i, s in enumerate(segments):
        for p in (s.start, s.end):
            for j in buckets.get((math.floor(p[0] / cell), math.floor(p[1] / cell)), ()):
                if find(i) != find(j) and feats[j].contains(p):
                    parent[find(i)] = find(j)
    groups: dict[int, list[_EndFeature]] = {}
    for i, f in enumerate(feats):
        groups.setdefault(find(i), []).append(f)
    return list(groups.values())


def _end_copper(
    points: list[tuple[float, float]],
    layer: str,
    features: list[_EndFeature],
    tracks: list[list[_EndFeature]],
    cap_radius: float,
) -> list[_EndFeature] | None:
    """The same-net copper on ``layer`` that the leg end ``points[0]`` lands on.

    "Lands on" is copper overlap, not centreline containment: the leg's
    track ends in a round cap of ``cap_radius`` (half its width) around
    ``points[0]``, so a pad or via whose copper comes within that radius is
    connected to it even when the centreline stops just outside the pad
    (PR #8751 review). Same-net track copper of another leg (``tracks``,
    one list per connected component) is taken WHOLE when any piece of it
    touches the cap: an anchor beside a trunk must clear every trunk
    segment, including fragments and bends away from the junction.

    ``None`` when a pad or via covers the whole leg.
    """
    start = points[0]
    found: list[_EndFeature] = []
    for feat in features:
        if not feat.on_layer(layer) or feat.distance(start) > cap_radius + 1e-6:
            continue
        if feat.contains(start) and _exit_arc(points, feat.contains) is None:
            return None
        found.append(feat)
    for component in tracks:
        if any(f.distance(start) <= cap_radius + 1e-6 for f in component):
            found.extend(component)
    return found


def _end_extent(points: list[tuple[float, float]], copper: list[_EndFeature]) -> float:
    """Arc length from ``points[0]`` to the edge of the end ``copper``.

    The largest exit, measured along the leg, from every feature in
    ``copper`` that contains the start point; ``0.0`` for a free track end
    or one that only overlaps the copper. This is only the starting guess:
    the straight-line clearance test then moves the anchor further inward.
    """
    extent = 0.0
    for feat in copper:
        if not feat.contains(points[0]):
            continue
        t = _exit_arc(points, feat.contains)
        if t is not None:
            extent = max(extent, t)
    return extent


def _clear_arc(
    points: list[tuple[float, float]],
    cum: list[float],
    t_from: float,
    t_limit: float,
    clear,
) -> float | None:
    """First arc from ``t_from`` towards ``t_limit`` where ``clear`` holds.

    Walks in ``_EXIT_MARCH_STEP_MM`` steps (either direction, so it serves
    both ends: ``t_limit < t_from`` walks backwards), then bisects onto the
    boundary. ``None`` when ``clear`` never holds in that range.
    """
    if clear(_point_at_arc(points, cum, t_from)):
        return t_from
    sign = 1.0 if t_limit >= t_from else -1.0
    span = abs(t_limit - t_from)
    prev = t_from
    walked = 0.0
    while walked < span - 1e-12:
        walked = min(span, walked + _EXIT_MARCH_STEP_MM)
        t = t_from + sign * walked
        if clear(_point_at_arc(points, cum, t)):
            bad, good = prev, t
            for _ in range(40):
                mid = (bad + good) / 2.0
                if clear(_point_at_arc(points, cum, mid)):
                    good = mid
                else:
                    bad = mid
            return good
        prev = t
    return None


def _reinforce_ends(
    pcb: PCB,
    result: ReinforceResult,
    *,
    net_segments: list[Segment],
    target_net_number: int,
    eligible_resolutions: Sequence | None,
    layer: str | None,
    all_runs: bool,
    min_run_length_mm: float | None,
    rules: DesignRules,
    policy: _ClearancePolicy,
    dry_run: bool,
) -> ReinforceResult:
    """``anchor_mode="ends"``: two anchors per leg (Issue #8742)."""
    net_name = result.net_name
    anchor_pad = result.anchor_pad_mm
    anchor_drill = result.anchor_drill_mm
    end_offset = result.end_offset_mm
    gated = eligible_resolutions is not None

    by_layer: dict[str, list[Segment]] = {}
    for s in net_segments:
        # A zero-length segment carries no copper of its own (its round cap
        # sits inside its neighbours'), but as a self-loop vertex it made the
        # chainer's component look closed and the whole leg ``unhandled``.
        if _seg_length(s) <= 1e-9:
            continue
        by_layer.setdefault(s.layer, []).append(s)

    if not by_layer:
        # Only zero-length (or no) segments: nothing to walk. Fail like the
        # later guard instead of crashing in ``max()`` below (Issue #8780).
        raise ReinforceError(f"net {net_name!r} produced no walkable polyline")

    original_ids: dict[int, int] = {}
    candidates: list[_LegCandidate] = []
    excluded_runs: list[list[Segment]] = []
    path_names: dict[int, list[str]] = {}
    target_layer: str | None = layer

    if gated:
        for res in eligible_resolutions or ():
            for seg in res.segments:
                covering = path_names.setdefault(id(seg), [])
                if res.spec.name not in covering:
                    covering.append(res.spec.name)
        for lay in sorted(by_layer):
            segs = by_layer[lay]
            eligible = [s for s in segs if id(s) in path_names]
            excluded_runs.extend(_chain_polylines([s for s in segs if id(s) not in path_names]))
            legs = _derive_legs(eligible, lay, original_ids)
            if layer is not None and lay != layer:
                for c in legs:
                    if c.status is None:
                        c.status = LEG_UNHANDLED
                        c.reason = f"on {lay}: --layer restricts this call to {layer}"
            candidates.extend(legs)
    else:
        if target_layer is None:
            target_layer = max(by_layer, key=lambda lay: sum(_seg_length(s) for s in by_layer[lay]))
        for lay in sorted(by_layer):
            legs = _derive_legs(by_layer[lay], lay, original_ids)
            if lay != target_layer:
                for c in legs:
                    if c.status is None:
                        c.status = LEG_UNHANDLED
                        c.reason = (
                            f"on {lay}: without --current-paths one copper layer is "
                            f"processed per call (this call: {target_layer}; pass "
                            f"--layer {lay})"
                        )
            candidates.extend(legs)

    if not candidates and not excluded_runs:
        raise ReinforceError(f"net {net_name!r} produced no walkable polyline")

    geoms = [_run_geometry(c.run) for c in candidates]

    # Selection mirrors ``spaced``: --min-run-length reports (never drops)
    # short legs; without declared paths only the longest leg is anchored
    # unless --all-runs. With declared paths the declaration IS the
    # selection: every eligible leg is anchored.
    not_selected: dict[int, str] = {}
    selectable = [i for i, c in enumerate(candidates) if c.status is None]
    if min_run_length_mm is not None:
        for i in selectable:
            if geoms[i].length_mm < min_run_length_mm:
                not_selected[i] = f"shorter than --min-run-length {min_run_length_mm:g} mm"
        selectable = [i for i in selectable if i not in not_selected]
    if not gated and not all_runs and selectable:
        longest = max(selectable, key=lambda i: (geoms[i].length_mm, -i))
        for i in selectable:
            if i != longest:
                not_selected[i] = (
                    "not selected: without --all-runs only the longest leg is anchored"
                )

    # Trunks / plain runs before branches, so a branch's junction-side anchor
    # steers clear of the trunk anchors; longest first within each.
    order = sorted(
        range(len(candidates)),
        key=lambda i: (candidates[i].kind == LEG_KIND_BRANCH, -geoms[i].length_mm, i),
    )

    other = _collect_other_net_copper(pcb, target_net_number, anchor_drill, anchor_pad)
    features = _same_net_end_features(pcb, target_net_number, anchor_drill, anchor_pad)
    dedup_floor = max(anchor_pad, anchor_drill + rules.min_hole_to_hole_mm)
    placed_positions: list[tuple[float, float]] = []
    step = END_NUDGE_MAX_MM / END_NUDGE_STEPS if END_NUDGE_STEPS > 0 else 0.0
    nudges = [k * step for k in range(END_NUDGE_STEPS + 1)] if step > 0 else [0.0]

    def violation_at(p: tuple[float, float]) -> _Violation | None:
        """Rule check of one anchor candidate; ``None`` when it may be placed.

        An anchor already placed for another leg is a drilled hole and a pad
        too: a hole gap below the hole-to-hole minimum is ``hole_to_hole``,
        otherwise overlapping anchor pads are ``anchor_spacing``.
        """
        if placed_positions:
            near = min(math.hypot(p[0] - q[0], p[1] - q[1]) for q in placed_positions)
            if near < dedup_floor - 1e-9:
                hole_gap = near - anchor_drill
                if hole_gap < rules.min_hole_to_hole_mm - 1e-9:
                    return _Violation(
                        RULE_HOLE_TO_HOLE, rules.min_hole_to_hole_mm, max(0.0, hole_gap)
                    )
                return _Violation(RULE_ANCHOR_SPACING, anchor_pad, near)
        return _position_violation(p[0], p[1], anchor_pad, anchor_drill, rules, other, policy)

    def plan_pair(points, cum, t0: float, t1: float, clear):
        """Decide BOTH end anchors before either is written (PR #8751 review).

        Start candidates step inward from ``t0``, far candidates inward from
        ``t1`` (``nudges``), each kept only where ``clear`` holds. A pair is
        geometrically compatible when the far anchor is at least the anchor
        spacing floor beyond the start one ALONG the leg (never past it) and
        at least that far apart STRAIGHT-LINE (a leg that folds back can
        meet the first and fail the second). Returns ``(pair, refusals)``:

        * ``pair is None`` and no refusals -- no compatible pair exists, the
          leg is ``too_short`` and nothing is placed;
        * both positions set -- the first compatible pair with no rule
          violation at either end;
        * one position set -- every far (start) candidate compatible with the
          placed start (far) anchor broke a clearance / hole-to-hole rule:
          a rule-based refusal, listed in ``refused``;
        * neither set, two refusals -- both ends refused by rules.
        """
        total = cum[-1]

        def side(base: float, sign: float):
            out = []
            for d in nudges:
                t = base + sign * d
                if t < -1e-9 or t > total + 1e-9:
                    continue
                pos = _point_at_arc(points, cum, t)
                if clear(pos):
                    out.append((t, pos))
            return out

        starts, ends = side(t0, 1.0), side(t1, -1.0)

        def compatible(s, e) -> bool:
            return (
                e[0] - s[0] >= dedup_floor - 1e-9
                and math.hypot(e[1][0] - s[1][0], e[1][1] - s[1][1]) >= dedup_floor - 1e-9
            )

        pairs = [
            (i, j)
            for i in range(len(starts))
            for j in range(len(ends))
            if compatible(starts[i], ends[j])
        ]
        if not pairs:
            return None, []
        cache: dict[tuple[str, int], _Violation | None] = {}

        def v(which: str, k: int) -> _Violation | None:
            if (which, k) not in cache:
                cache[(which, k)] = violation_at((starts if which == "s" else ends)[k][1])
            return cache[(which, k)]

        for i, j in pairs:
            if v("s", i) is None and v("e", j) is None:
                return (starts[i][1], ends[j][1]), []
        # No clean pair. One anchor stands only when every counterpart that
        # could pair with it is refused by a rule.
        for i in dict.fromkeys(i for i, _ in pairs):
            if v("s", i) is None:
                j = next(j for ii, j in pairs if ii == i)
                return (starts[i][1], None), [(ends[j][1], v("e", j))]
        for j in dict.fromkeys(j for _, j in pairs):
            if v("e", j) is None:
                i = next(i for i, jj in pairs if jj == j)
                return (None, ends[j][1]), [(starts[i][1], v("s", i))]
        i, j = pairs[0]
        return (None, None), [(starts[i][1], v("s", i)), (ends[j][1], v("e", j))]

    def refuse(pos: tuple[float, float], violation: _Violation) -> None:
        result.refused.append(
            RefusedAnchor(
                x=pos[0],
                y=pos[1],
                reason=(
                    "anchor pad overlaps another leg's anchor"
                    if violation.rule == RULE_ANCHOR_SPACING
                    else "other-net clearance/hole-to-hole violation"
                ),
                rule=violation.rule,
                required_mm=violation.required_mm,
                actual_mm=violation.actual_mm,
                obstacle_net=violation.obstacle_net,
            )
        )

    attempted_flags: list[bool] = []
    for index in order:
        cand = candidates[index]
        g = geoms[index]
        names: list[str] = []
        for s in cand.run:
            for name in path_names.get(original_ids.get(id(s), id(s)), ()):
                if name not in names:
                    names.append(name)
        leg = LegSummary(
            net=net_name,
            layer=cand.layer,
            kind=cand.kind,
            polyline=list(g.points),
            length_mm=g.length_mm,
            width_mm=max(s.width for s in cand.run),
            paths=names,
        )
        refused_here = 0
        attempted_here = False
        if cand.status is not None:
            leg.status, leg.reason = cand.status, cand.reason
        elif index in not_selected:
            leg.status, leg.reason = LEG_UNANCHORED, not_selected[index]
        else:
            attempted_here = True
            points = g.points
            cum = _cumulative_arc(points)
            total = cum[-1]
            own = {original_ids.get(id(s), id(s)) for s in cand.run}
            tracks = _track_components([s for s in by_layer[cand.layer] if id(s) not in own])
            c0 = _end_copper(points, cand.layer, features, tracks, cand.run[0].width / 2.0)
            c1 = _end_copper(points[::-1], cand.layer, features, tracks, cand.run[-1].width / 2.0)
            half = anchor_pad / 2.0
            if c0 is None or c1 is None:
                leg.status = LEG_TOO_SHORT
                leg.reason = "same-net pad/via copper covers the whole leg"
            else:
                e0 = _end_extent(points, c0)
                e1 = _end_extent(points[::-1], c1)
                # The along-leg target alone is not enough: a leg that bends
                # soon after leaving the pad (or an oblique branch leaving the
                # trunk) brings that point back near the end copper. Keep every
                # anchor's straight-line copper gap to BOTH ends' copper at
                # least ``end_offset``, moving inward only.
                end_copper = c0 + c1
                # Any other same-net SMD pad on this layer is end copper too
                # for the clearance test: a pad mid-leg, or the end pad of an
                # adjacent leg of a Y, is not what THIS leg ends on, yet an
                # anchor must not land in it (Issue #8772). THT pads and vias
                # stay with the hole-to-hole check in ``_position_violation``.
                seen = {id(f) for f in end_copper}
                end_copper = end_copper + [
                    f
                    for f in features
                    if f.pad is not None
                    and f.layers
                    and id(f) not in seen
                    and f.on_layer(cand.layer)
                ]

                def clear(p, _copper=end_copper):
                    return all(f.distance(p) - half >= end_offset - 1e-6 for f in _copper)

                t0_arc = e0 + end_offset + half
                t1_arc = total - (e1 + end_offset + half)
                t0 = _clear_arc(points, cum, t0_arc, total, clear) if t0_arc <= total else None
                t1 = _clear_arc(points, cum, t1_arc, 0.0, clear) if t1_arc >= 0.0 else None
                pair = None
                refusals: list = []
                if t0 is not None and t1 is not None:
                    pair, refusals = plan_pair(points, cum, t0, t1, clear)
                if pair is None:
                    # Never one anchor for geometry: the leg gets none.
                    leg.status = LEG_TOO_SHORT
                    if t0 is None or t1 is None:
                        leg.reason = (
                            f"{total:.3f} mm leg has no point whose {anchor_pad:.3f} mm anchor "
                            f"is {end_offset:g} mm clear of the end copper"
                        )
                    elif t1 - t0 < dedup_floor - 1e-9:
                        leg.reason = (
                            f"{total:.3f} mm leg needs "
                            f"{total - (t1 - t0) + dedup_floor:.3f} mm for two "
                            f"{anchor_pad:.3f} mm anchors {end_offset:g} mm clear of the "
                            "end copper"
                        )
                    else:
                        p0 = _point_at_arc(points, cum, t0)
                        p1 = _point_at_arc(points, cum, t1)
                        leg.reason = (
                            f"leg folds back: its two end anchors, {end_offset:g} mm clear "
                            f"of the end copper, would be {math.dist(p0, p1):.3f} mm apart "
                            f"(need {dedup_floor:.3f} mm between {anchor_pad:.3f} mm anchors)"
                        )
                else:
                    for pos in pair:
                        if pos is not None:
                            leg.anchors.append(PlacedAnchor(x=pos[0], y=pos[1]))
                            placed_positions.append(pos)
                    for pos, violation in refusals:
                        refuse(pos, violation)
                        refused_here += 1
                    for a in leg.anchors:
                        if not dry_run:
                            pcb.add_via(
                                a.x,
                                a.y,
                                size=anchor_pad,
                                drill=anchor_drill,
                                layers=("F.Cu", "B.Cu"),
                                net=net_name,
                                dedupe=True,
                            )
                        result.placed.append(PlacedAnchor(x=a.x, y=a.y))
                    if len(leg.anchors) == 2:
                        leg.status = LEG_FULL
                    elif leg.anchors:
                        leg.status = LEG_PARTIAL
                        leg.reason = (
                            "1 of 2 end anchors refused by a clearance/hole-to-hole rule "
                            "(see refused)"
                        )
                    else:
                        leg.status = LEG_UNANCHORED
                        leg.reason = "both end anchors refused (see refused)"
        result.legs.append(leg)
        attempted_flags.append(attempted_here)
        result.runs.append(
            RunSummary(
                run_index=len(result.runs),
                length_mm=g.length_mm,
                segment_count=g.segment_count,
                anchors_needed=2,
                anchors_placed=len(leg.anchors),
                anchors_refused=refused_here,
            )
        )

    excluded_reason = (
        "excluded by declared current-path intent (Issue #4980): not covered by a "
        f"resolved, reinforcement-eligible CurrentPathSpec for net {net_name!r}"
    )
    for run in sorted(excluded_runs, key=_run_length, reverse=True):
        g = _run_geometry(run)
        result.runs.append(
            RunSummary(
                run_index=len(result.runs),
                length_mm=g.length_mm,
                segment_count=g.segment_count,
                anchors_needed=0,
                anchors_placed=0,
                anchors_refused=0,
                path_excluded_reason=excluded_reason,
            )
        )

    attempted = [(leg, result.runs[i]) for i, leg in enumerate(result.legs) if attempted_flags[i]]
    if attempted:
        primary_leg, primary_run = max(attempted, key=lambda item: item[0].length_mm)
        result.run_segment_count = primary_run.segment_count
        result.run_length_mm = primary_run.length_mm
        result.layer = target_layer if target_layer is not None else primary_leg.layer
    else:
        result.layer = target_layer or (result.legs[0].layer if result.legs else "")
    result.unhandled_runs = len(result.runs) - len(attempted)
    return result
