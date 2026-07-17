"""Stuck-net classifier (Milestone 1 of architect roadmap #3862, issue #3863).

READ-ONLY diagnostic that labels every unfinished signal net by *why* it is
stuck so downstream fixes (M2 region re-solve, M3 placement nudge, M4 escape
upgrade) and humans can target the right remedy.

The three architecturally-distinct failure modes (per #3862):

* ``ESCAPE_BLOCKED`` -- a stranded pad cannot leave its own footprint: foreign
  copper and foreign pads crowd every direction around the pad so there is no
  open sector wide enough for a trace to escape (the chorus J2 "0/8 escaped, no
  grid point reachable" class).

* ``CONGESTION_SATURATED`` -- the stranded pad *can* escape, and committed
  *strict* (fully-connected) signal-net copper sits adjacent to it.  Completing
  the net would require ripping that copper, which strands the blocker net (the
  1:1-trade local minimum confirmed on chorus #3861).  This is the M2 region
  re-solve target.

* ``PLACEMENT_BOUND`` -- the stranded pad can escape, but there is no nearby
  committed strict copper to rip (ripping blockers cannot help) AND the local
  neighbourhood is densely packed with foreign nets between the stranded pad
  and its partner island.  No routing order closes it; a part must move (the
  chorus AUDIO_R / U5 analog cluster class).  This is the M3 placement-nudge
  target.

* ``BUDGET_STARVED`` -- the stranded pad can escape, there is no rippable strict
  copper nearby, AND the local neighbourhood is *sparse* (few or no foreign
  obstructions).  Geometrically nothing is in the way: the net looks routable on
  current copper, but the batch negotiation simply never committed it (the
  cross-board long-haul that starves the negotiated per-net budget, #4159).
  Distinguished from ``PLACEMENT_BOUND`` because the remedy is a zero-cost
  re-route (route this net first / raise the per-net budget), NOT a part move.
  A sparse open-lane no-blocker net is close to self-contradictory as a
  placement problem, so it is split out here rather than escalated to M3.
  (Verdict reached by geometry alone -- no route is attempted -- so the evidence
  says "looks routable ... not confirmed".)

Design notes
------------
This is a *pure-geometry* analysis built entirely on
:class:`kicad_tools.schema.pcb.PCB`, which resolves pad positions and committed
copper into the SAME board-coordinate frame.  The frame-correct blocker
geometry (``_find_blocking_strict_nets_from_pcb`` / ``_point_to_segment_distance``,
below) is the load-bearing piece carried over from the #3861 controlled-rip-up
work: it reads BOTH the stranded pad positions AND the committed copper through
:class:`PCB` so they share the board frame.  Parsing the raw ``.kicad_pcb`` text
directly would mix page-space copper coordinates with board-space pad
coordinates (off by the page origin) and find no blockers -- the #3861
frame-mismatch defect.  These helpers are vendored here (rather than imported
from ``partial_rescue``) so the classifier is self-contained and carries no
dependency on routing-mutation code.

No routing grid is built and no routing is mutated, so the classifier is
zero-regression by construction and cheap enough to run on any board as a
routing-report feature.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.router.failure_analysis import FailureCause

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


__all__ = [
    "StuckClass",
    "RecommendedAction",
    "Confidence",
    "RankedAction",
    "StuckNetDiagnosis",
    "StuckClassifierResult",
    "classify_stuck_nets",
    "classify_stuck_nets_from_pcb",
]


# --- tunable geometry thresholds (mm) ---------------------------------------
#
# These defaults are tuned against the chorus stress fixture (31/51 strict, 20
# partial).  They are exposed as parameters so a caller can sweep them, but the
# defaults are what the CLI and the snapshot tests use.

# Radius around a stranded pad scanned for "blocking" strict-net copper.  Reuses
# the #3861 controlled-rip-up default so the classifier agrees with what that
# stage would actually try to rip.
DEFAULT_BLOCKER_RADIUS_MM = 2.0

# A trace needs roughly this much open width to escape a pad.  A foreign
# obstruction closer than this in an angular sector "blocks" that sector.
DEFAULT_ESCAPE_CLEARANCE_MM = 0.25

# How far around a pad to scan for foreign obstructions when testing escape.
# Kept tight so only obstructions in the immediate escape ring count (a
# neighbour at fine pitch ~0.5mm is seen; a part 1.5mm away is not a wall).
DEFAULT_NEIGHBORHOOD_RADIUS_MM = 1.0

# Wider radius used only for the local-congestion *count* that separates
# placement-bound analog clusters (dense) from copper-boxed nets.
DEFAULT_CONGESTION_RADIUS_MM = 2.0

# Number of angular sectors the escape ring is divided into.  A pad escapes if a
# contiguous open arc of at least ``ESCAPE_MIN_OPEN_SECTORS`` sectors exists.
ESCAPE_SECTORS = 16

# A pad is ESCAPE_BLOCKED when its widest contiguous open arc is narrower than
# this many sectors (i.e. no routing lane wide enough to leave the pad).  With
# 16 sectors (22.5 deg each), 2 sectors == a 45-degree lane.
ESCAPE_MIN_OPEN_SECTORS = 2

# Local congestion (foreign obstruction count within the congestion radius) at
# or above this is "dense" -- used to separate PLACEMENT_BOUND (dense) from a
# merely sparse stranded pad with no rippable copper.
DEFAULT_CONGESTION_THRESHOLD = 6

# Congestion at or above this is "analog-cluster dense" (e.g. chorus U5 QFN /
# AUDIO_R): even ripping the few adjacent strict nets cannot clear the wall of
# surrounding non-strict copper, so such a net is PLACEMENT_BOUND even when a
# strict blocker is technically adjacent.  The chorus codec cluster sits at
# 29-118 obstructions while the genuinely copper-boxed (M2-tractable) nets sit
# at 2-15, so 20 lands in the natural gap between the two populations.
DEFAULT_DENSE_CLUSTER_THRESHOLD = 20

# --- match-group topology (issue #4261 Defect 2 & 3) ------------------------
#
# A stuck net is flagged SELF_CROSSING_BUNDLE when the copper crowding its
# stranded pad is predominantly its OWN length-match-group siblings (a reversed
# / self-crossing bus), rather than genuinely foreign copper.  The share is the
# fraction of same-group obstruction copper (or the fraction of same-group
# strict blockers) around the pad; at or above this threshold the topology is
# self-crossing.  0.5 == "the majority of what boxes the pad in is its own bus".
SELF_CROSS_THRESHOLD = 0.5

# Topology labels attached to a diagnosis (empty string == not applicable).
TOPOLOGY_SELF_CROSSING = "self_crossing_bundle"
TOPOLOGY_FOREIGN_CLUSTER = "foreign_cluster"
# Issue #4286: a bundle whose facing pad rows carry the nets in the SAME order.
# Same-group blocker share fires for this topology too (co-oriented siblings
# saturate the corridor without crossing), but de-reversal would CREATE the
# crossing pathology instead of fixing it, so it gets its own label + ladder.
TOPOLOGY_CO_ORIENTED = "co_oriented_bundle"

# --- pin-order (inversion) verification (issue #4286) -----------------------
#
# Same-match-group blocker share alone cannot distinguish a genuinely REVERSED
# bundle (board-07's DDR byte: the facing pin columns carry the nets in
# opposite order, so every pair must cross) from a CO-ORIENTED bundle in a
# saturated corridor (board-07's TMDS lanes: both facing rows carry the nets in
# the SAME order; siblings compete for lanes without crossing).  Before
# recommending DE_REVERSE_BUNDLE the engine therefore measures the actual
# pad-order inversion count between the two facing components, reusing the
# ``bundle_river`` projection/inversion machinery (#4053).
#
# The verdict thresholds on the *inversion fraction* f = inversions / C(n, 2):
# a full reversal yields f == 1.0 and a co-oriented bundle f == 0.0.  The
# break-even point is exactly 0.5 -- de-reversing one row maps f -> 1 - f, so
# the move can only REDUCE crossings when f > 0.5.  We treat f >=
# REVERSAL_INVERSION_THRESHOLD as "reversed" (keep the de-reversal
# recommendation) and anything below as "co-oriented" (suppress it: at f < 0.5
# de-reversal adds more crossings than it removes).  Board-07 measures f = 1.0
# for DQ0..DQ7 (U1<->U2) and f = 0.0 for the TMDS lanes (J2<->U4), so both
# populations sit far from the boundary.
REVERSAL_INVERSION_THRESHOLD = 0.5

# Facing rows are only comparable when the two components share at least this
# many of the group's nets: below 3 the inversion fraction is degenerate
# (C(2,2)=1 pair -> f is 0 or 1 on a single coin flip) and
# ``detect_match_groups`` refuses groups smaller than 3 anyway.
MIN_FACING_ROW_NETS = 3

# Orientation verdicts (issue #4286).
ORIENT_REVERSED = "reversed"
ORIENT_CO_ORIENTED = "co_oriented"
ORIENT_UNRESOLVED = "unresolved"


class StuckClass(Enum):
    """Why an unfinished signal net is stuck.

    The ``failure_cause`` property maps each class onto the pre-existing
    :class:`~kicad_tools.router.failure_analysis.FailureCause` enum so the
    classifier integrates with the rest of the failure-analysis machinery.
    """

    ESCAPE_BLOCKED = "escape_blocked"
    CONGESTION_SATURATED = "congestion_saturated"
    PLACEMENT_BOUND = "placement_bound"
    BUDGET_STARVED = "budget_starved"
    POUR_DISCONTINUOUS = "pour_discontinuous"

    @property
    def description(self) -> str:
        return {
            "escape_blocked": (
                "Pad cannot escape its footprint -- no open lane through "
                "surrounding copper/pads (fine-pitch/connector). Fix: escape "
                "upgrade (M4)."
            ),
            "congestion_saturated": (
                "Pad reachable but boxed in by committed strict-net copper; "
                "completing it would strand the blocker (1:1 trade). Fix: "
                "region rip-up-and-reroute (M2)."
            ),
            "placement_bound": (
                "Pad reachable with no rippable copper nearby and dense local "
                "congestion -- no routing order closes it. Fix: placement "
                "nudge (M3)."
            ),
            "budget_starved": (
                "Pad reachable with no rippable copper nearby and a sparse "
                "neighbourhood -- looks routable on current copper but the batch "
                "negotiation never committed it. Fix: re-route this net first "
                "(kct route-auto --net '<name>') or raise the per-net search "
                "budget; not a placement problem."
            ),
            "pour_discontinuous": (
                "Pour-carried net has stranded pads -- copper zone does not reach "
                "all pads on this net. Fix: re-pour, add stitching vias, or bridge "
                "the zone island gap."
            ),
        }[self.value]

    @property
    def failure_cause(self) -> FailureCause:
        """Map onto the shared :class:`FailureCause` enum."""
        return {
            "escape_blocked": FailureCause.PIN_ACCESS,
            "congestion_saturated": FailureCause.CONGESTION,
            "placement_bound": FailureCause.ROUTING_ORDER,
            "budget_starved": FailureCause.ROUTING_ORDER,
            "pour_discontinuous": FailureCause.BLOCKED_PATH,
        }[self.value]


class RecommendedAction(Enum):
    """Closed taxonomy of fix moves the recommendation engine may rank.

    READ-ONLY advice to a human/agent -- the classifier never performs any of
    these; it only ranks them.  ``DE_REVERSE_BUNDLE`` / ``REORDER_PINS`` are the
    topology remedies for a self-crossing bus; ``MOVE_PART`` / ``WIDEN_CHANNEL``
    address genuinely foreign congestion; ``ACCEPT_PLATEAU`` acknowledges a
    topological limit no placement lever removes.
    """

    DE_REVERSE_BUNDLE = "de_reverse_bundle"
    REORDER_PINS = "reorder_pins"
    WIDEN_CHANNEL = "widen_channel"
    MOVE_PART = "move_part"
    ACCEPT_PLATEAU = "accept_plateau"


class Confidence(Enum):
    """How much trust to place in a recommendation.

    * ``HIGH``   -- match group from an explicit source, or a confirmed
      pad-order crossing proxy (neither available from a bare ``.kicad_pcb``
      today, so reserved for a future config-threaded caller).
    * ``MEDIUM`` -- match group inferred from a bus-suffix pattern and the share
      test fired; the geometry is a heuristic (board-07 lands here).
    * ``LOW``    -- group inferred from a borderline / high-false-positive suffix
      pattern (e.g. the generic ``A\\d+`` address bus).
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class RankedAction:
    """One entry in a ranked fix ladder."""

    action: RecommendedAction
    rationale: str
    confidence: Confidence

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "rationale": self.rationale,
            "confidence": self.confidence.value,
        }


@dataclass
class BundleOrientation:
    """Measured pad-order orientation of a match-group bundle (issue #4286).

    Produced by :func:`_resolve_bundle_orientation`; consumed by the
    recommendation stage to verify a self-crossing claim before recommending
    DE_REVERSE_BUNDLE.  ``verdict`` is one of :data:`ORIENT_REVERSED`,
    :data:`ORIENT_CO_ORIENTED` or :data:`ORIENT_UNRESOLVED`; the remaining
    fields carry the evidence (``inverted_pairs`` of ``total_pairs`` facing pad
    pairs flip order between ``primary_ref`` and ``secondary_ref``).  When the
    facing rows cannot be resolved the verdict is UNRESOLVED and ``detail``
    explains why -- the caller then makes NO pin-order claim either way.
    """

    verdict: str
    inverted_pairs: int = 0
    total_pairs: int = 0
    member_count: int = 0
    inversion_fraction: float = 0.0
    primary_ref: str = ""
    secondary_ref: str = ""
    detail: str = ""


@dataclass
class StuckNetDiagnosis:
    """Per-net classification result with supporting evidence."""

    net_name: str
    net_number: int
    classification: StuckClass
    unconnected_pads: list[str]
    blocking_nets: list[str] = field(default_factory=list)
    evidence: str = ""
    # Diagnostics that fed the decision (handy for tuning / JSON consumers).
    escape_lane_deg: float = 0.0
    local_congestion: int = 0
    nearest_blocker_mm: float | None = None
    # Defect 2 (#4261): the local-congestion count split by match-group identity.
    # ``same_group_congestion`` counts obstruction copper belonging to the target
    # net's own length-match group (e.g. its DDR data-byte siblings); the rest is
    # genuinely ``foreign_congestion``.  ``same_group_congestion +
    # foreign_congestion == local_congestion``.
    same_group_congestion: int = 0
    foreign_congestion: int = 0
    # Name of the target net's inferred match group ("" when it belongs to none).
    match_group: str = ""
    # Subset of ``blocking_nets`` that are members of ``match_group`` -- these are
    # the net's own siblings, NOT foreign copper (Defect 2 relabel).
    same_group_blockers: list[str] = field(default_factory=list)
    # Defect 3 (#4261): detected obstruction topology and the ranked fix ladder.
    # ``topology`` is ``TOPOLOGY_SELF_CROSSING`` | ``TOPOLOGY_FOREIGN_CLUSTER`` |
    # "" (not applicable).  ``recommendation`` is empty unless a recommendation
    # is warranted (PLACEMENT_BOUND / CONGESTION_SATURATED).
    topology: str = ""
    recommendation: list[RankedAction] = field(default_factory=list)

    @property
    def classification_value(self) -> str:
        return self.classification.value

    def to_dict(self) -> dict:
        return {
            "net_name": self.net_name,
            "net_number": self.net_number,
            "classification": self.classification.value,
            "failure_cause": self.classification.failure_cause.value,
            "unconnected_pads": list(self.unconnected_pads),
            "blocking_nets": list(self.blocking_nets),
            "evidence": self.evidence,
            "escape_lane_deg": round(self.escape_lane_deg, 2),
            "local_congestion": self.local_congestion,
            "nearest_blocker_mm": (
                round(self.nearest_blocker_mm, 4) if self.nearest_blocker_mm is not None else None
            ),
            "same_group_congestion": self.same_group_congestion,
            "foreign_congestion": self.foreign_congestion,
            "match_group": self.match_group,
            "topology": self.topology,
            "recommendation": [a.to_dict() for a in self.recommendation],
        }

    def one_line(self) -> str:
        pads = ", ".join(self.unconnected_pads) or "(none)"
        blockers = f" blockers=[{', '.join(self.blocking_nets)}]" if self.blocking_nets else ""
        return (
            f"{self.net_name}: {self.classification.value.upper()} "
            f"({len(self.unconnected_pads)} pad(s): {pads}){blockers} -- {self.evidence}"
        )


@dataclass
class StuckClassifierResult:
    """Aggregate classification over all unfinished signal nets."""

    diagnoses: list[StuckNetDiagnosis]

    @property
    def counts(self) -> dict[str, int]:
        out = {c.value: 0 for c in StuckClass}
        for d in self.diagnoses:
            out[d.classification.value] += 1
        return out

    def to_dict(self) -> dict:
        return {
            "summary": {
                "stuck_nets": len(self.diagnoses),
                "counts": self.counts,
            },
            "nets": [d.to_dict() for d in self.diagnoses],
        }


# --- internal geometry helpers ----------------------------------------------
#
# The two functions below are the frame-correct blocker geometry carried over
# from the #3861 controlled-rip-up work.  They are vendored here so the
# classifier is self-contained and carries no dependency on routing-mutation
# code.  Both read the stranded pad positions AND the committed copper through
# :class:`PCB` so they share the SAME board-coordinate frame -- the load-bearing
# page-space-vs-board-space fix from #3861.


def _point_to_segment_distance(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> float:
    """Euclidean distance from point ``(px, py)`` to segment ``a->b``.

    Distance is measured pad-to-segment (point-to-line-segment), not
    pad-to-endpoint, so copper that *passes near* a pad without terminating
    there is still recognised as a blocker (#3861).
    """
    dx = bx - ax
    dy = by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx = ax + t * dx
    cy = ay + t * dy
    return math.hypot(px - cx, py - cy)


def _find_blocking_strict_nets_from_pcb(
    pcb: PCB,
    target_net: str,
    strict_nets: list[str],
    *,
    radius_mm: float = 2.0,
    max_blockers: int = 3,
) -> list[str]:
    """Strict signal nets whose committed copper boxes in *target_net*'s pads.

    A near-complete partial net is stuck because committed strict-net copper
    sits between its connected island and its last unreached pad(s).  We
    approximate "blocking" geometrically: a strict net is a candidate blocker
    when any of its committed segments/vias lie within *radius_mm* of any of
    *target_net*'s unconnected pads.  Candidates are ranked by proximity
    (closest first) and capped at *max_blockers*.

    Returns an ordered list of strict net names.  Empty when no committed copper
    is near the stranded pad(s) (the pad is placement-bound, not copper-boxed).

    Vendored from #3861 (``partial_rescue._find_blocking_strict_nets_from_pcb``)
    -- the frame-correct geometry is load-bearing; see module docstring.
    """
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    analysis = NetStatusAnalyzer(pcb).analyze()
    status = analysis.get_net(target_net)
    if status is None or not status.unconnected_pads:
        return []
    pad_positions = [p.position for p in status.unconnected_pads]

    name_by_id = {nid: net.name for nid, net in pcb.nets.items() if net.name}
    strict_set = set(strict_nets)
    best_dist: dict[str, float] = {}

    def _update(net_name: str, d: float) -> None:
        if net_name not in best_dist or d < best_dist[net_name]:
            best_dist[net_name] = d

    for seg in pcb.segments:
        name = name_by_id.get(seg.net_number)
        if name not in strict_set:
            continue
        ax, ay = seg.start
        bx, by = seg.end
        for tx, ty in pad_positions:
            _update(name, _point_to_segment_distance(tx, ty, ax, ay, bx, by))

    for via in pcb.vias:
        name = name_by_id.get(via.net_number)
        if name not in strict_set:
            continue
        vx, vy = via.position
        for tx, ty in pad_positions:
            _update(name, math.hypot(vx - tx, vy - ty))

    candidates = sorted(
        (name for name, d in best_dist.items() if d <= radius_mm),
        key=lambda n: best_dist[n],
    )
    return candidates[:max_blockers]


def _iter_board_pads(pcb: PCB):
    """Yield ``(reference, net_number, (x, y), (w, h))`` for every real pad.

    Positions are in the board frame.  Mirrors the footprint->board transform
    used by :class:`kicad_tools.analysis.net_status.NetStatusAnalyzer` (KiCad
    negates the footprint orientation vs standard CCW math, issue #3739).  The
    leading footprint reference lets the facing-row resolver (#4286) group a
    bundle's pads by component.
    """
    for fp in pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue
        fp_x, fp_y = fp.position
        angle = math.radians(-fp.rotation)
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        for pad in fp.pads:
            px, py = pad.position
            bx = fp_x + (px * cos_a - py * sin_a)
            by = fp_y + (px * sin_a + py * cos_a)
            yield fp.reference, pad.net_number, (bx, by), pad.size


def _foreign_obstructions(
    pcb: PCB,
    target_net: int,
    point: tuple[float, float],
    radius_mm: float,
) -> list[tuple[float, float, float, int]]:
    """Foreign obstructions within *radius_mm* of *point*.

    Returns a list of ``(x, y, dist, obstruction_net_number)`` for foreign-net
    pad centres and foreign copper segment closest-points near *point*.  Used
    both for escape-sector analysis and for the local-congestion count.
    "Foreign" == any net other than *target_net* (net 0 / unassigned copper
    counts as an obstruction too, since it still occupies the lane).

    The trailing ``obstruction_net_number`` is carried so callers can split the
    congestion count into same-match-group vs genuinely-foreign copper (issue
    #4261 Defect 2).  ``_widest_open_arc`` ignores the field.
    """
    px, py = point
    obstructions: list[tuple[float, float, float, int]] = []

    for _ref, net_number, (bx, by), _size in _iter_board_pads(pcb):
        if net_number == target_net:
            continue
        d = math.hypot(bx - px, by - py)
        if d <= radius_mm:
            obstructions.append((bx, by, d, net_number))

    for seg in pcb.segments:
        if seg.net_number == target_net:
            continue
        ax, ay = seg.start
        ex, ey = seg.end
        d = _point_to_segment_distance(px, py, ax, ay, ex, ey)
        if d <= radius_mm:
            # Use the segment midpoint direction for sector bucketing; distance
            # is the true point-to-segment distance.
            mx = (ax + ex) / 2.0
            my = (ay + ey) / 2.0
            obstructions.append((mx, my, d, seg.net_number))

    for via in pcb.vias:
        if via.net_number == target_net:
            continue
        vx, vy = via.position
        d = math.hypot(vx - px, vy - py)
        if d <= radius_mm:
            obstructions.append((vx, vy, d, via.net_number))

    return obstructions


def _widest_open_arc(
    obstructions: list[tuple[float, float, float, int]],
    point: tuple[float, float],
    sectors: int,
    escape_clearance_mm: float,
) -> int:
    """Width (in sectors) of the widest contiguous *open* escape arc.

    Divides the directions around *point* into *sectors* angular buckets.  A
    sector is "blocked" when a foreign obstruction sits within
    *escape_clearance_mm* of the pad in that direction, "open" otherwise.  The
    return value is the number of sectors in the widest contiguous run of open
    sectors (wrapping around the full circle).  A small value means the pad is
    walled in on (almost) all sides -> ESCAPE_BLOCKED; a large value means a
    routing lane exists.
    """
    px, py = point
    blocked = [False] * sectors
    for ox, oy, d, _net in obstructions:
        if d > escape_clearance_mm:
            continue
        ang = math.atan2(oy - py, ox - px)
        idx = int((ang + math.pi) / (2 * math.pi) * sectors) % sectors
        blocked[idx] = True

    if not any(blocked):
        return sectors
    if all(blocked):
        return 0

    # Widest run of False (open) sectors over the circular array.  Doubling the
    # array handles the wrap-around in one linear pass.
    best = run = 0
    for is_blocked in blocked + blocked:
        if is_blocked:
            run = 0
        else:
            run += 1
            best = max(best, run)
    return min(best, sectors)


def _resolve_match_groups(
    pcb: PCB,
) -> tuple[dict[int, str], dict[str, set[int]]]:
    """Infer length-match groups from a bare PCB (issue #4261 Defect 2 & 3).

    Returns ``(net_number -> group_name, group_name -> {member net_numbers})``.

    Uses the in-tree suffix-inference detector
    (:func:`kicad_tools.router.match_group_detection.detect_match_groups` with
    ``enable_suffix_inference=True``): board-07's ``DQ\\d+`` nets group as
    ``DDR_DATA``, TMDS lanes as ``HDMI_TMDS_DATA``, etc.  Explicit
    ``NetClassRouting`` groups are NOT available here (a raw ``.kicad_pcb`` has
    no router config), so this is a MEDIUM-confidence signal by construction --
    see the confidence grading in :func:`_grade_confidence`.
    """
    from kicad_tools.router.match_group_detection import detect_match_groups

    net_names = {nid: net.name for nid, net in pcb.nets.items() if net.name}
    groups = detect_match_groups(net_names, enable_suffix_inference=True)

    net_to_group: dict[int, str] = {}
    group_members: dict[str, set[int]] = {}
    for group in groups:
        members = set(group.net_ids)
        for p_id, n_id in group.pair_ids:
            members.add(p_id)
            members.add(n_id)
        group_members[group.name] = members
        for member in members:
            net_to_group[member] = group.name
    return net_to_group, group_members


def _resolve_bundle_orientation(
    pcb: PCB,
    group_ids: set[int],
) -> BundleOrientation:
    """Measure the pad-order orientation of a match-group bundle (issue #4286).

    Resolves the group's two facing pad rows and computes the inversion count
    between their net orders, REUSING the ``bundle_river`` projection/inversion
    primitives (:class:`~kicad_tools.router.bundle_river.RowMember` /
    :func:`~kicad_tools.router.bundle_river.compute_facing_row_inversions`,
    #4053) rather than reinventing them.

    Row resolution: the two components hosting the most *distinct* group nets
    are taken as the facing rows (deterministic: count desc, reference asc).
    A net with several pads on one component contributes its pad centroid.
    Each row is projected onto its own long axis (the axis with the larger
    coordinate spread -- y for a vertical pin column, x for a horizontal row);
    only relative order matters, so the absolute frame is irrelevant.

    Verdict (see :data:`REVERSAL_INVERSION_THRESHOLD` for the break-even
    rationale): inversion fraction ``>= 0.5`` -> :data:`ORIENT_REVERSED`,
    below -> :data:`ORIENT_CO_ORIENTED`.

    Returns :data:`ORIENT_UNRESOLVED` (with ``detail``) instead of guessing
    when the rows are not comparable: fewer than two host components, fewer
    than :data:`MIN_FACING_ROW_NETS` shared nets, rows with different long
    axes (an L-shaped bundle, where crossing depends on chirality the 1-D
    projection cannot see), or a degenerate row with no spread.  Never raises.
    """
    from kicad_tools.router.bundle_river import RowMember, compute_facing_row_inversions

    name_by_id = {nid: net.name for nid, net in pcb.nets.items() if net.name}

    # (reference -> net_id -> pad positions) for the group's nets only.
    by_ref: dict[str, dict[int, list[tuple[float, float]]]] = {}
    for ref, net_number, (bx, by), _size in _iter_board_pads(pcb):
        if net_number not in group_ids:
            continue
        by_ref.setdefault(ref, {}).setdefault(net_number, []).append((bx, by))

    if len(by_ref) < 2:
        return BundleOrientation(
            ORIENT_UNRESOLVED,
            detail=(
                f"group pads found on {len(by_ref)} component(s); two facing rows are required"
            ),
        )

    # The two components hosting the most distinct group nets are the facing
    # rows (reference-name tiebreak keeps the choice deterministic).
    ranked = sorted(by_ref.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    (ref_a, pads_a), (ref_b, pads_b) = ranked[0], ranked[1]
    shared = set(pads_a) & set(pads_b)
    if len(shared) < MIN_FACING_ROW_NETS:
        return BundleOrientation(
            ORIENT_UNRESOLVED,
            detail=(
                f"components {ref_a}/{ref_b} share only {len(shared)} group "
                f"net(s); need >= {MIN_FACING_ROW_NETS} for a pin-order verdict"
            ),
        )

    def _row(pads: dict[int, list[tuple[float, float]]]) -> tuple[list[RowMember], str] | None:
        """Project one component's shared-net pad centroids onto its long axis."""
        centroids: dict[int, tuple[float, float]] = {}
        for nid in shared:
            pts = pads[nid]
            centroids[nid] = (
                sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts),
            )
        xs = [c[0] for c in centroids.values()]
        ys = [c[1] for c in centroids.values()]
        spread_x = max(xs) - min(xs)
        spread_y = max(ys) - min(ys)
        if max(spread_x, spread_y) < 1e-6:
            return None  # degenerate row: all pads coincide, no order exists
        axis = "x" if spread_x >= spread_y else "y"
        members = [
            RowMember(
                net_id=nid,
                net_name=name_by_id.get(nid, str(nid)),
                projection=c[0] if axis == "x" else c[1],
            )
            for nid, c in centroids.items()
        ]
        return members, axis

    row_a = _row(pads_a)
    row_b = _row(pads_b)
    if row_a is None or row_b is None:
        return BundleOrientation(
            ORIENT_UNRESOLVED,
            primary_ref=ref_a,
            secondary_ref=ref_b,
            detail=f"a facing row on {ref_a}/{ref_b} has no spatial spread",
        )
    members_a, axis_a = row_a
    members_b, axis_b = row_b
    if axis_a != axis_b:
        return BundleOrientation(
            ORIENT_UNRESOLVED,
            primary_ref=ref_a,
            secondary_ref=ref_b,
            detail=(
                f"facing rows on {ref_a}/{ref_b} run along different axes "
                f"({axis_a} vs {axis_b}); pin order is not 1-D comparable"
            ),
        )

    inversions = compute_facing_row_inversions(members_a, members_b)
    n = len(shared)
    total_pairs = n * (n - 1) // 2
    fraction = len(inversions) / total_pairs if total_pairs else 0.0
    verdict = ORIENT_REVERSED if fraction >= REVERSAL_INVERSION_THRESHOLD else ORIENT_CO_ORIENTED
    return BundleOrientation(
        verdict=verdict,
        inverted_pairs=len(inversions),
        total_pairs=total_pairs,
        member_count=n,
        inversion_fraction=fraction,
        primary_ref=ref_a,
        secondary_ref=ref_b,
    )


def classify_stuck_nets_from_pcb(
    pcb: PCB,
    *,
    blocker_radius_mm: float = DEFAULT_BLOCKER_RADIUS_MM,
    escape_clearance_mm: float = DEFAULT_ESCAPE_CLEARANCE_MM,
    neighborhood_radius_mm: float = DEFAULT_NEIGHBORHOOD_RADIUS_MM,
    congestion_radius_mm: float = DEFAULT_CONGESTION_RADIUS_MM,
    congestion_threshold: int = DEFAULT_CONGESTION_THRESHOLD,
    dense_cluster_threshold: int = DEFAULT_DENSE_CLUSTER_THRESHOLD,
    max_blockers: int = 3,
    excluded_nets: frozenset[str] = frozenset(),
) -> StuckClassifierResult:
    """Classify every unfinished signal net on *pcb* (already-loaded PCB).

    Split out from :func:`classify_stuck_nets` so it can be unit-tested with a
    small synthetic board without round-tripping through a file.
    """
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    analysis = NetStatusAnalyzer(pcb).analyze()

    # Strict signal nets = fully-connected multi-pad signal nets.  This is the
    # rip-up pool the M2 stage would draw from, and the set #3861's blocker
    # finder ranks against.
    strict_nets = [
        n.net_name
        for n in analysis.nets
        if n.net_name not in excluded_nets
        and n.net_type == "signal"
        and n.total_pads >= 2
        and n.status == "complete"
    ]

    # Map net name -> net number for richer output.
    number_by_name = {net.name: nid for nid, net in pcb.nets.items() if net.name}

    # Match-group membership (issue #4261 Defect 2 & 3): resolve once per board.
    # Feeds BOTH the same-group-vs-foreign congestion split and the self-crossing
    # topology detection that drives the fix recommendation.
    net_to_group, group_members = _resolve_match_groups(pcb)

    # Pour-carried incomplete nets: connectivity is expected to be closed by
    # copper fill (zone/pour), so a signal-geometry analysis would misclassify
    # them.  Union of (a) the ML-tagged advisory set the analyzer populates and
    # (b) zone-backed plane/power nets that are still incomplete.  These are
    # routed to POUR_DISCONTINUOUS in the second pass below rather than dropped.
    pour_carried_names: set[str] = set(analysis.advisory_incomplete_names)
    pour_carried_names.update(
        n.net_name
        for n in analysis.nets
        if n.net_type in ("plane", "power") and n.status == "incomplete"
    )

    diagnoses: list[StuckNetDiagnosis] = []
    for net in analysis.nets:
        if net.net_name in excluded_nets:
            continue
        if net.net_type != "signal":
            continue
        if net.status != "incomplete":
            continue
        # Skip advisory (plane/pour) residuals -- not genuine signal gaps.
        if net.is_advisory_incomplete:
            continue
        # Signal-named nets the ML pour-net classifier tagged as pour-carried
        # pass the guards above; route them to POUR_DISCONTINUOUS instead of
        # letting them receive an incorrect signal-failure classification.
        if net.net_name in pour_carried_names:
            continue

        net_number = number_by_name.get(net.net_name, net.net_number)
        pad_names = [p.full_name for p in net.unconnected_pads]
        pad_points = [p.position for p in net.unconnected_pads]

        # 1) Escape test: the NARROWEST widest-open-arc across the stranded
        #    pads.  A pad walled in on all sides has a tiny open arc; a pad with
        #    a routing lane has a wide one.
        worst_open_arc = ESCAPE_SECTORS
        for pt in pad_points:
            obstr = _foreign_obstructions(pcb, net_number, pt, neighborhood_radius_mm)
            arc = _widest_open_arc(obstr, pt, ESCAPE_SECTORS, escape_clearance_mm)
            worst_open_arc = min(worst_open_arc, arc)

        # 2) Blocking strict copper near the stranded pad(s) (#3861 geometry).
        blockers = _find_blocking_strict_nets_from_pcb(
            pcb,
            net.net_name,
            strict_nets,
            radius_mm=blocker_radius_mm,
            max_blockers=max_blockers,
        )

        # 3) Local congestion = densest foreign-obstruction count over the
        #    stranded pads (wider radius than the escape ring).  Defect 2
        #    (#4261): at the densest pad, split the count by match-group
        #    identity so same-bundle siblings are not mislabelled "foreign".
        target_group = net_to_group.get(net_number, "")
        group_ids = group_members.get(target_group, set())
        local_congestion = 0
        same_group_congestion = 0
        foreign_congestion = 0
        for pt in pad_points:
            obstr = _foreign_obstructions(pcb, net_number, pt, congestion_radius_mm)
            if len(obstr) >= local_congestion:
                local_congestion = len(obstr)
                same_group_congestion = sum(1 for o in obstr if o[3] in group_ids)
                foreign_congestion = local_congestion - same_group_congestion

        # Which ranked strict blockers are the net's OWN match-group siblings.
        same_group_blockers = [b for b in blockers if number_by_name.get(b) in group_ids]

        # nearest blocker distance for evidence
        nearest_blocker_mm = _nearest_blocker_distance(
            pcb, net_number, pad_points, strict_nets, blocker_radius_mm
        )

        diag = _decide(
            net_name=net.net_name,
            net_number=net_number,
            pad_names=pad_names,
            blockers=blockers,
            worst_open_arc=worst_open_arc,
            local_congestion=local_congestion,
            nearest_blocker_mm=nearest_blocker_mm,
            congestion_threshold=congestion_threshold,
            dense_cluster_threshold=dense_cluster_threshold,
            same_group_congestion=same_group_congestion,
            foreign_congestion=foreign_congestion,
        )
        # Attach match-group identity (Defect 2): same-group vs foreign split and
        # which blockers are the net's own bundle siblings.
        diag.match_group = target_group
        diag.same_group_blockers = same_group_blockers

        # Defect 3: detect self-crossing-bundle vs foreign-cluster topology and
        # emit the ranked fix ladder (only for the two placement/congestion
        # classes; other classes keep topology="" and recommendation=[]).
        if diag.classification in (
            StuckClass.PLACEMENT_BOUND,
            StuckClass.CONGESTION_SATURATED,
        ):
            diag.topology = _detect_topology(
                match_group=target_group,
                group_size=len(group_ids),
                same_group_congestion=same_group_congestion,
                foreign_congestion=foreign_congestion,
                blockers=blockers,
                same_group_blockers=same_group_blockers,
            )
            # Pin-order verification (issue #4286): same-group blocker share
            # alone fires on BOTH a reversed bundle and a co-oriented bundle in
            # a saturated corridor.  Measure the actual facing-row inversion
            # count before letting the ladder recommend de-reversal; a measured
            # co-oriented bundle gets its own topology + ladder instead.  An
            # UNRESOLVED measurement keeps the share-based ladder unchanged (no
            # pin-order claim either way) -- and never crashes the diagnostic.
            orientation: BundleOrientation | None = None
            if diag.topology == TOPOLOGY_SELF_CROSSING:
                try:
                    orientation = _resolve_bundle_orientation(pcb, group_ids)
                except Exception as exc:  # pragma: no cover - defensive only
                    orientation = BundleOrientation(
                        ORIENT_UNRESOLVED,
                        detail=f"orientation resolution failed: {exc}",
                    )
                if orientation.verdict == ORIENT_CO_ORIENTED:
                    diag.topology = TOPOLOGY_CO_ORIENTED
            confidence = _grade_confidence(target_group)
            diag.recommendation = _build_recommendation(
                classification=diag.classification,
                topology=diag.topology,
                match_group=target_group,
                confidence=confidence,
                orientation=orientation,
            )
        diagnoses.append(diag)

    # Second pass: pour-carried incomplete nets.  Their connectivity is closed
    # by copper fill, so the stranded-pad inventory comes straight from
    # NetStatus.unconnected_pads (no zone-fill geometry analysis for this MVP --
    # island counting is a possible follow-on).
    for net in analysis.nets:
        if net.net_name in excluded_nets:
            continue
        if net.net_name not in pour_carried_names:
            continue
        if net.status != "incomplete":
            continue
        net_number = number_by_name.get(net.net_name, net.net_number)
        pad_names = [p.full_name for p in net.unconnected_pads]
        layers_str = ", ".join(net.plane_layers) if net.plane_layers else "no zone definition"
        evidence = (
            f"pour-carried net: {net.unconnected_count} pad(s) stranded from main "
            f"connected island (layers: {layers_str})"
        )
        diagnoses.append(
            StuckNetDiagnosis(
                net_name=net.net_name,
                net_number=net_number,
                classification=StuckClass.POUR_DISCONTINUOUS,
                unconnected_pads=pad_names,
                evidence=evidence,
            )
        )

    return StuckClassifierResult(diagnoses=diagnoses)


def _nearest_blocker_distance(
    pcb: PCB,
    target_net: int,
    pad_points: list[tuple[float, float]],
    strict_nets: list[str],
    radius_mm: float,
) -> float | None:
    """Distance to the closest strict-net copper near any stranded pad.

    Scans a slightly larger window than *radius_mm* (so PLACEMENT_BOUND can
    report "blocker exists but too far to be the cause") and returns ``None``
    when no strict copper is found in that window at all.
    """
    name_by_id = {nid: net.name for nid, net in pcb.nets.items() if net.name}
    strict_set = set(strict_nets)
    window = max(radius_mm * 3.0, 6.0)
    best = math.inf
    for seg in pcb.segments:
        if name_by_id.get(seg.net_number) not in strict_set:
            continue
        ax, ay = seg.start
        ex, ey = seg.end
        for px, py in pad_points:
            d = _point_to_segment_distance(px, py, ax, ay, ex, ey)
            if d <= window and d < best:
                best = d
    return None if best is math.inf else best


def _grade_confidence(match_group: str) -> Confidence:
    """Grade a suffix-inferred self-crossing detection.

    HIGH is reserved for an explicit match-group source or a confirmed pad-order
    crossing proxy -- neither is reachable from a bare ``.kicad_pcb`` in v1, so
    this returns MEDIUM for a normal bus-suffix group and LOW for the generic
    ``ADDR_BUS`` pattern (``A\\d+``), documented as the highest-false-positive
    inference in ``match_group_detection``.
    """
    if match_group == "ADDR_BUS":
        return Confidence.LOW
    return Confidence.MEDIUM


def _detect_topology(
    *,
    match_group: str,
    group_size: int,
    same_group_congestion: int,
    foreign_congestion: int,
    blockers: list[str],
    same_group_blockers: list[str],
) -> str:
    """Return the obstruction topology of a stuck net (issue #4261 Defect 3).

    ``TOPOLOGY_SELF_CROSSING`` when the copper crowding the pad is predominantly
    the net's OWN match-group siblings -- either the same-group share of nearby
    obstruction copper OR the same-group share of ranked strict blockers reaches
    :data:`SELF_CROSS_THRESHOLD`, and the group has at least three members
    (``detect_match_groups`` already refuses smaller groups).  Otherwise
    ``TOPOLOGY_FOREIGN_CLUSTER``.
    """
    if not match_group or group_size < 3:
        return TOPOLOGY_FOREIGN_CLUSTER
    total_congestion = same_group_congestion + foreign_congestion
    same_group_share = same_group_congestion / total_congestion if total_congestion else 0.0
    blocking_share = len(same_group_blockers) / len(blockers) if blockers else 0.0
    if same_group_share >= SELF_CROSS_THRESHOLD or blocking_share >= SELF_CROSS_THRESHOLD:
        return TOPOLOGY_SELF_CROSSING
    return TOPOLOGY_FOREIGN_CLUSTER


def _orientation_note(orientation: BundleOrientation | None) -> str:
    """Evidence suffix describing the measured pin order (issue #4286).

    Appended to the DE_REVERSE_BUNDLE rationale so a human can see whether the
    reversal claim was actually verified against the facing pad rows or merely
    inherited from the same-group-share heuristic (unresolved rows).
    """
    if orientation is None:
        return ""
    if orientation.verdict == ORIENT_REVERSED:
        return (
            f" (pin order verified: {orientation.inverted_pairs}/"
            f"{orientation.total_pairs} facing pad pairs invert between "
            f"{orientation.primary_ref} and {orientation.secondary_ref})"
        )
    if orientation.verdict == ORIENT_UNRESOLVED:
        return f" (pin order not verified: {orientation.detail})"
    return ""


def _co_oriented_ladder(
    grp: str,
    confidence: Confidence,
    orientation: BundleOrientation,
) -> list[RankedAction]:
    """The fix ladder for a measured co-oriented saturated bundle (#4286).

    De-reversal / pin re-ordering are DELIBERATELY ABSENT: the facing rows
    already carry the nets in the same order, so flipping one row would CREATE
    the crossing pathology (board-07 TMDS evidence, #4252 A3 / #4253).
    WIDEN_CHANNEL stays absent too -- same-group congestion did not respond to
    widening on board-07 (28->27/31 regression).
    """
    measured = (
        f"{orientation.inverted_pairs}/{orientation.total_pairs} facing pad "
        f"pairs invert between {orientation.primary_ref} and "
        f"{orientation.secondary_ref}"
    )
    return [
        RankedAction(
            RecommendedAction.MOVE_PART,
            f"the {grp} bundle is already co-oriented ({measured}) -- "
            f"de-reversing it would CREATE crossings, not remove them; the "
            f"corridor is saturated by co-oriented siblings, so relocate a "
            f"part to open more lanes",
            confidence,
        ),
        RankedAction(
            RecommendedAction.ACCEPT_PLATEAU,
            "if no placement lever helps, this is a topological plateau; do "
            "NOT widen the channel -- same-group congestion does not respond "
            "to widening (board-07 evidence, 28->27/31)",
            confidence,
        ),
    ]


def _build_recommendation(
    *,
    classification: StuckClass,
    topology: str,
    match_group: str,
    confidence: Confidence,
    orientation: BundleOrientation | None = None,
) -> list[RankedAction]:
    """Rank the fix ladder for one stuck net (issue #4261 Defect 3).

    Policy table keyed on ``(stuck_class x topology)`` -- see the architect
    design on #4261.  A recommendation is emitted only for PLACEMENT_BOUND and
    CONGESTION_SATURATED; every other class returns ``[]``.

    Board-07 hard evidence: for PLACEMENT_BOUND + SELF_CROSSING_BUNDLE,
    ``WIDEN_CHANNEL`` is DELIBERATELY OMITTED -- widening the U1/U2 channel
    regressed that board 28->27/31, so the engine must never suggest the move
    known to backfire on a reversed bundle.

    Issue #4286: *orientation* carries the measured facing-row pin order.  The
    caller has already re-labelled a measured co-oriented bundle as
    :data:`TOPOLOGY_CO_ORIENTED`, which routes to :func:`_co_oriented_ladder`
    (no de-reversal -- it would create crossings).  For a still-SELF_CROSSING
    topology the measurement (verified reversal, or unresolved rows) is folded
    into the DE_REVERSE_BUNDLE rationale via :func:`_orientation_note`.
    """
    grp = match_group or "the bundle"

    if topology == TOPOLOGY_CO_ORIENTED and orientation is not None:
        if classification in (StuckClass.PLACEMENT_BOUND, StuckClass.CONGESTION_SATURATED):
            return _co_oriented_ladder(grp, confidence, orientation)
        return []

    if classification is StuckClass.PLACEMENT_BOUND:
        if topology == TOPOLOGY_SELF_CROSSING:
            # NOTE: WIDEN_CHANNEL intentionally absent (regresses a reversed bus).
            return [
                RankedAction(
                    RecommendedAction.DE_REVERSE_BUNDLE,
                    f"the {grp} bus is reversed at the facing part -- co-orient "
                    f"(flip/re-order) its pad column so the bundle stops "
                    f"self-crossing" + _orientation_note(orientation),
                    confidence,
                ),
                RankedAction(
                    RecommendedAction.REORDER_PINS,
                    "re-order pins to un-cross the byte",
                    confidence,
                ),
                RankedAction(
                    RecommendedAction.ACCEPT_PLATEAU,
                    "topological limit; do NOT widen the channel -- widening "
                    "regressed board-07 28->27/31",
                    confidence,
                ),
            ]
        return [
            RankedAction(
                RecommendedAction.MOVE_PART,
                "genuinely foreign copper walls the pad -- relocate the crowded part",
                confidence,
            ),
            RankedAction(
                RecommendedAction.WIDEN_CHANNEL,
                "open the routing corridor around the pad",
                confidence,
            ),
            RankedAction(
                RecommendedAction.ACCEPT_PLATEAU,
                "if no placement lever helps, this is a plateau",
                confidence,
            ),
        ]

    if classification is StuckClass.CONGESTION_SATURATED:
        if topology == TOPOLOGY_SELF_CROSSING:
            return [
                RankedAction(
                    RecommendedAction.REORDER_PINS,
                    f"{grp} siblings box each other in a 1:1 trade -- re-order "
                    f"pins to remove the trade",
                    confidence,
                ),
                RankedAction(
                    RecommendedAction.DE_REVERSE_BUNDLE,
                    f"co-orient the {grp} bundle so siblings stop crossing"
                    + _orientation_note(orientation),
                    confidence,
                ),
            ]
        return [
            RankedAction(
                RecommendedAction.WIDEN_CHANNEL,
                "foreign strict blocker -- widen the corridor (region "
                "rip-up-and-reroute is the primary existing remedy)",
                confidence,
            ),
        ]

    return []


def _decide(
    *,
    net_name: str,
    net_number: int,
    pad_names: list[str],
    blockers: list[str],
    worst_open_arc: int,
    local_congestion: int,
    nearest_blocker_mm: float | None,
    congestion_threshold: int,
    dense_cluster_threshold: int,
    same_group_congestion: int = 0,
    foreign_congestion: int = 0,
) -> StuckNetDiagnosis:
    """Apply the decision tree to one net's diagnostics.

    Decision order (each branch is mutually exclusive):

    1. ESCAPE_BLOCKED -- the widest open arc around a stranded pad is narrower
       than a trace can use (``worst_open_arc < ESCAPE_MIN_OPEN_SECTORS``).  The
       pad cannot leave its footprint, so neither ripping copper (M2) nor moving
       a *distant* part (M3) helps until the escape itself is upgraded (M4).
       Checked first because an escape-blocked pad is upstream of everything.

    2. PLACEMENT_BOUND (dense analog cluster) -- the pad escapes but the local
       neighbourhood is *cluster-dense* (e.g. chorus U5 QFN / AUDIO_R): even
       ripping the few adjacent strict nets cannot clear the wall of
       surrounding non-strict copper, so a part must move.  Checked before the
       blocker branch because such a net usually has a nominal strict blocker
       too, yet ripping it cannot help.

    3. CONGESTION_SATURATED -- the pad escapes AND committed strict copper is
       adjacent (a blocker the M2 rip-up could try) AND the neighbourhood is not
       cluster-dense.  This is the 1:1-trade class.

    4. PLACEMENT_BOUND (no rippable copper, moderate congestion) -- the pad
       escapes, there is no adjacent rippable strict copper, but the local
       neighbourhood is moderately congested (``local_congestion >=
       congestion_threshold``, below the dense-cluster line).  Reachable, but
       locally busy enough that a solo route is not a safe default assumption --
       nothing to rip, so only a placement change can help.

    5. BUDGET_STARVED (no rippable copper, sparse neighbourhood) -- the pad
       escapes, there is no adjacent rippable strict copper, AND the local
       neighbourhood is sparse (``local_congestion < congestion_threshold``).
       Geometrically nothing is in the way: the net looks routable on current
       copper and the batch negotiation simply never committed it (#4159).  The
       remedy is a zero-cost re-route / budget bump, not a part move, so this is
       split out of PLACEMENT_BOUND.  Reached by geometry alone (no route is
       attempted), so the evidence explicitly disclaims confidence
       ("looks routable ... not confirmed").
    """
    open_deg = worst_open_arc * (360.0 / ESCAPE_SECTORS)

    if worst_open_arc < ESCAPE_MIN_OPEN_SECTORS:
        evidence = (
            f"no escape lane: widest open arc only {open_deg:.0f} deg "
            f"(pad walled in by foreign copper/pads on all sides)"
        )
        return StuckNetDiagnosis(
            net_name=net_name,
            net_number=net_number,
            classification=StuckClass.ESCAPE_BLOCKED,
            unconnected_pads=pad_names,
            blocking_nets=blockers,
            evidence=evidence,
            escape_lane_deg=open_deg,
            local_congestion=local_congestion,
            nearest_blocker_mm=nearest_blocker_mm,
            same_group_congestion=same_group_congestion,
            foreign_congestion=foreign_congestion,
        )

    if local_congestion >= dense_cluster_threshold:
        nb = (
            f"nearest strict blocker {nearest_blocker_mm:.3f}mm"
            if nearest_blocker_mm is not None
            else "no strict copper nearby"
        )
        evidence = (
            f"pad reachable ({open_deg:.0f} deg lane) but in a dense cluster "
            f"({same_group_congestion} same-group + {foreign_congestion} foreign "
            f"obstructions, {nb}); ripping the few strict blockers cannot clear "
            f"the surrounding copper"
        )
        return StuckNetDiagnosis(
            net_name=net_name,
            net_number=net_number,
            classification=StuckClass.PLACEMENT_BOUND,
            unconnected_pads=pad_names,
            blocking_nets=blockers,
            evidence=evidence,
            escape_lane_deg=open_deg,
            local_congestion=local_congestion,
            nearest_blocker_mm=nearest_blocker_mm,
            same_group_congestion=same_group_congestion,
            foreign_congestion=foreign_congestion,
        )

    if blockers:
        nb = f"{nearest_blocker_mm:.3f}mm" if nearest_blocker_mm is not None else "n/a"
        evidence = (
            f"pad reachable ({open_deg:.0f} deg lane) but boxed in by committed "
            f"strict copper (nearest blocker {nb}); ripping it would strand "
            f"{', '.join(blockers)} (1:1 trade)"
        )
        return StuckNetDiagnosis(
            net_name=net_name,
            net_number=net_number,
            classification=StuckClass.CONGESTION_SATURATED,
            unconnected_pads=pad_names,
            blocking_nets=blockers,
            evidence=evidence,
            escape_lane_deg=open_deg,
            local_congestion=local_congestion,
            nearest_blocker_mm=nearest_blocker_mm,
            same_group_congestion=same_group_congestion,
            foreign_congestion=foreign_congestion,
        )

    nb = (
        f"nearest strict copper {nearest_blocker_mm:.3f}mm away"
        if nearest_blocker_mm is not None
        else "no strict copper nearby"
    )

    if local_congestion < congestion_threshold:
        # Sparse neighbourhood, open lane, nothing rippable: geometrically
        # nothing is in the way.  This is indistinguishable, under pure geometry,
        # from "the batch router never committed this net" -- so we do NOT
        # overclaim a placement problem.  No route is attempted here (that would
        # break --why's cheap read-only contract), so the evidence disclaims
        # confidence.
        evidence = (
            f"pad reachable ({open_deg:.0f} deg lane), no adjacent rippable "
            f"strict copper ({nb}); sparse neighbourhood ({local_congestion} "
            f"foreign obstructions) -- looks routable on current copper (not "
            f"confirmed); the batch negotiation likely never committed it. "
            f"Re-route this net first or raise the per-net budget."
        )
        return StuckNetDiagnosis(
            net_name=net_name,
            net_number=net_number,
            classification=StuckClass.BUDGET_STARVED,
            unconnected_pads=pad_names,
            blocking_nets=[],
            evidence=evidence,
            escape_lane_deg=open_deg,
            local_congestion=local_congestion,
            nearest_blocker_mm=nearest_blocker_mm,
            same_group_congestion=same_group_congestion,
            foreign_congestion=foreign_congestion,
        )

    evidence = (
        f"pad reachable ({open_deg:.0f} deg lane), no adjacent rippable strict "
        f"copper ({nb}); moderate local congestion ({same_group_congestion} "
        f"same-group + {foreign_congestion} foreign obstructions) -- ripping "
        f"cannot help, a part must move"
    )
    return StuckNetDiagnosis(
        net_name=net_name,
        net_number=net_number,
        classification=StuckClass.PLACEMENT_BOUND,
        unconnected_pads=pad_names,
        blocking_nets=[],
        evidence=evidence,
        escape_lane_deg=open_deg,
        local_congestion=local_congestion,
        nearest_blocker_mm=nearest_blocker_mm,
        same_group_congestion=same_group_congestion,
        foreign_congestion=foreign_congestion,
    )


def classify_stuck_nets(
    pcb_path: str | Path,
    *,
    blocker_radius_mm: float = DEFAULT_BLOCKER_RADIUS_MM,
    escape_clearance_mm: float = DEFAULT_ESCAPE_CLEARANCE_MM,
    neighborhood_radius_mm: float = DEFAULT_NEIGHBORHOOD_RADIUS_MM,
    congestion_radius_mm: float = DEFAULT_CONGESTION_RADIUS_MM,
    congestion_threshold: int = DEFAULT_CONGESTION_THRESHOLD,
    dense_cluster_threshold: int = DEFAULT_DENSE_CLUSTER_THRESHOLD,
    max_blockers: int = 3,
    excluded_nets: frozenset[str] = frozenset(),
) -> StuckClassifierResult:
    """Classify every unfinished signal net on the PCB at *pcb_path*.

    READ-ONLY: loads the board, analyses connectivity + geometry, and returns
    the per-net classification.  Never mutates the board.
    """
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    return classify_stuck_nets_from_pcb(
        pcb,
        blocker_radius_mm=blocker_radius_mm,
        escape_clearance_mm=escape_clearance_mm,
        neighborhood_radius_mm=neighborhood_radius_mm,
        congestion_radius_mm=congestion_radius_mm,
        congestion_threshold=congestion_threshold,
        dense_cluster_threshold=dense_cluster_threshold,
        max_blockers=max_blockers,
        excluded_nets=excluded_nets,
    )
