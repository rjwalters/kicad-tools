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


class StuckClass(Enum):
    """Why an unfinished signal net is stuck.

    The ``failure_cause`` property maps each class onto the pre-existing
    :class:`~kicad_tools.router.failure_analysis.FailureCause` enum so the
    classifier integrates with the rest of the failure-analysis machinery.
    """

    ESCAPE_BLOCKED = "escape_blocked"
    CONGESTION_SATURATED = "congestion_saturated"
    PLACEMENT_BOUND = "placement_bound"
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
            "pour_discontinuous": FailureCause.BLOCKED_PATH,
        }[self.value]


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
    """Yield ``(net_number, (x, y), (w, h))`` for every real pad in board frame.

    Mirrors the footprint->board transform used by
    :class:`kicad_tools.analysis.net_status.NetStatusAnalyzer` (KiCad negates
    the footprint orientation vs standard CCW math, issue #3739).
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
            yield pad.net_number, (bx, by), pad.size


def _foreign_obstructions(
    pcb: PCB,
    target_net: int,
    point: tuple[float, float],
    radius_mm: float,
) -> list[tuple[float, float, float]]:
    """Foreign obstructions within *radius_mm* of *point*.

    Returns a list of ``(x, y, dist)`` for foreign-net pad centres and foreign
    copper segment closest-points near *point*.  Used both for escape-sector
    analysis and for the local-congestion count.  "Foreign" == any net other
    than *target_net* (net 0 / unassigned copper counts as an obstruction too,
    since it still occupies the lane).
    """
    px, py = point
    obstructions: list[tuple[float, float, float]] = []

    for net_number, (bx, by), _size in _iter_board_pads(pcb):
        if net_number == target_net:
            continue
        d = math.hypot(bx - px, by - py)
        if d <= radius_mm:
            obstructions.append((bx, by, d))

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
            obstructions.append((mx, my, d))

    for via in pcb.vias:
        if via.net_number == target_net:
            continue
        vx, vy = via.position
        d = math.hypot(vx - px, vy - py)
        if d <= radius_mm:
            obstructions.append((vx, vy, d))

    return obstructions


def _widest_open_arc(
    obstructions: list[tuple[float, float, float]],
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
    for ox, oy, d in obstructions:
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
        #    stranded pads (wider radius than the escape ring).
        local_congestion = 0
        for pt in pad_points:
            obstr = _foreign_obstructions(pcb, net_number, pt, congestion_radius_mm)
            local_congestion = max(local_congestion, len(obstr))

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

    4. PLACEMENT_BOUND (no rippable copper) -- the pad escapes and there is no
       adjacent rippable strict copper.  Nothing to rip, so only a placement
       change can help.
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
        )

    if local_congestion >= dense_cluster_threshold:
        nb = (
            f"nearest strict blocker {nearest_blocker_mm:.3f}mm"
            if nearest_blocker_mm is not None
            else "no strict copper nearby"
        )
        evidence = (
            f"pad reachable ({open_deg:.0f} deg lane) but in a dense cluster "
            f"({local_congestion} foreign obstructions, {nb}); ripping the few "
            f"strict blockers cannot clear the surrounding copper -- a part "
            f"must move (analog/codec cluster)"
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
        )

    nb = (
        f"nearest strict copper {nearest_blocker_mm:.3f}mm away"
        if nearest_blocker_mm is not None
        else "no strict copper nearby"
    )
    dense = local_congestion >= congestion_threshold
    density_note = (
        f"dense local congestion ({local_congestion} foreign obstructions)"
        if dense
        else f"sparse neighbourhood ({local_congestion} foreign obstructions)"
    )
    evidence = (
        f"pad reachable ({open_deg:.0f} deg lane), no adjacent rippable strict "
        f"copper ({nb}); {density_note} -- ripping cannot help, a part must move"
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
