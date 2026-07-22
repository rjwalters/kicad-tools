"""Classifier -> concrete placement-delta translator (issue #4466).

Phase 1 of the board-07 router<->placement feedback epic (#3438).

The stuck-net classifier (:mod:`kicad_tools.router.stuck_classifier`) already
PROVES the fix for a placement-bound net: :func:`_build_recommendation` ranks a
fix ladder (``DE_REVERSE_BUNDLE`` / ``REORDER_PINS`` / ``MOVE_PART`` / ...) and
:func:`_resolve_bundle_orientation` measures which facing part is reversed.  But
the ladder terminates at an *English sentence* -- nothing turns it into an
applyable ``(ref, dx, dy, rotation)``.  This module is the missing translator.

It is the "propose" half only: a **pure, read-only** function that maps one
:class:`~kicad_tools.router.stuck_classifier.StuckNetDiagnosis` onto a single
:class:`PlacementDelta` (JSON-serializable data).  It performs NO ``PCB``
mutation and attempts NO re-route -- applying the delta and re-routing is
Phase 2 (#4467).

Mapping rules (driven off the *top-ranked* action, honoring the ladder's
deliberate omissions -- e.g. a reversed bus never gets ``WIDEN_CHANNEL``):

* ``DE_REVERSE_BUNDLE`` -> ``kind="rotate_180"`` on the reversed facing part
  (:attr:`BundleOrientation.secondary_ref`), ``rotation_delta = 180.0``.  This
  is the geometric realization of "flip the facing pad column so the bundle
  stops self-crossing".
* ``MOVE_PART`` -> ``kind="translate"``: ``target_ref`` is the crowded foreign
  component nearest the stranded pad; ``(dx, dy)`` is a minimal bounded step
  toward the widest open escape arc.
* ``REORDER_PINS`` -> ``kind="reorder_pins"`` with rationale only (no geometry
  in P1; an applicator needs a pad-remap that does not yet exist).
* ``WIDEN_CHANNEL`` / ``ACCEPT_PLATEAU`` / no recommendation -> ``None`` (there
  is no placement move to emit -- never synthesize one the ladder dropped).

Generic: works for any board's ``PLACEMENT_BOUND`` / ``CONGESTION_SATURATED``
diagnosis, not just board-07.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kicad_tools.router.stuck_classifier import (
    DEFAULT_CONGESTION_RADIUS_MM,
    ESCAPE_SECTORS,
    RecommendedAction,
    StuckNetDiagnosis,
    _foreign_obstructions,
    _iter_board_pads,
)

if TYPE_CHECKING:
    from kicad_tools.router.stuck_classifier import StuckClassifierResult
    from kicad_tools.schema.pcb import PCB


__all__ = [
    "PlacementDelta",
    "MAX_TRANSLATE_MM",
    "delta_from_diagnosis",
    "deltas_from_result",
]


# Bound on a single ``translate`` proposal.  The move is deliberately minimal --
# a small nudge to open a lane, not a wholesale relocation -- so a Phase-2
# applicator can probe cheaply and a human can eyeball it.  "A few mm" per the
# #4466 design; the step magnitude equals this bound (minimal-first).
MAX_TRANSLATE_MM = 2.0

# Radius scanned around the stranded pad for the foreign obstructions that seed
# the translate direction.  Reuses the classifier's congestion ring so the
# delta agrees with the geometry that produced the MOVE_PART verdict.
_TRANSLATE_RADIUS_MM = DEFAULT_CONGESTION_RADIUS_MM


@dataclass
class PlacementDelta:
    """A concrete, applyable placement change proposed for one stuck net.

    Data only -- emitting a ``PlacementDelta`` mutates nothing.  ``kind`` is one
    of ``"translate"`` | ``"rotate_180"`` | ``"reorder_pins"``; the geometric
    fields carry the move for the kinds that have one (``translate`` uses
    ``dx``/``dy``; ``rotate_180`` uses ``rotation_delta``; ``reorder_pins``
    carries rationale only).
    """

    net_name: str
    target_ref: str
    kind: str
    dx: float = 0.0
    dy: float = 0.0
    rotation_delta: float = 0.0
    source_action: str = ""
    rationale: str = ""
    confidence: str = ""

    def to_dict(self) -> dict:
        return {
            "net_name": self.net_name,
            "target_ref": self.target_ref,
            "kind": self.kind,
            "dx": round(self.dx, 4),
            "dy": round(self.dy, 4),
            "rotation_delta": round(self.rotation_delta, 4),
            "source_action": self.source_action,
            "rationale": self.rationale,
            "confidence": self.confidence,
        }


def delta_from_diagnosis(pcb: PCB, diag: StuckNetDiagnosis) -> PlacementDelta | None:
    """Translate one classifier diagnosis into a concrete placement delta.

    Returns ``None`` when the top-ranked action is not an applyable placement
    move (``WIDEN_CHANNEL`` / ``ACCEPT_PLATEAU`` / empty ladder), or when the
    geometry needed to realize the move is unavailable (e.g. a reversed-bundle
    verdict whose facing part could not be resolved).  Pure/read-only: ``pcb``
    is never mutated.
    """
    if not diag.recommendation:
        return None

    top = diag.recommendation[0]
    action = top.action
    confidence = top.confidence.value
    rationale = top.rationale

    if action is RecommendedAction.DE_REVERSE_BUNDLE:
        return _rotate_180_delta(diag, confidence, rationale)
    if action is RecommendedAction.MOVE_PART:
        return _translate_delta(pcb, diag, confidence, rationale)
    if action is RecommendedAction.REORDER_PINS:
        return _reorder_pins_delta(diag, confidence, rationale)

    # WIDEN_CHANNEL / ACCEPT_PLATEAU (or anything else): no placement move.
    return None


def deltas_from_result(pcb: PCB, result: StuckClassifierResult) -> list[PlacementDelta]:
    """Emit a ``PlacementDelta`` for every diagnosis that yields one.

    Convenience wrapper over :func:`delta_from_diagnosis`; diagnoses that map to
    ``None`` (non-placement top action) are simply skipped.
    """
    out: list[PlacementDelta] = []
    for diag in result.diagnoses:
        delta = delta_from_diagnosis(pcb, diag)
        if delta is not None:
            out.append(delta)
    return out


# --- per-kind builders ------------------------------------------------------


def _rotate_180_delta(
    diag: StuckNetDiagnosis, confidence: str, rationale: str
) -> PlacementDelta | None:
    """DE_REVERSE_BUNDLE -> flip the reversed facing part 180 degrees.

    The reversed part is the bundle's ``secondary_ref`` (the classifier resolves
    the two facing rows and reports ``primary_ref`` / ``secondary_ref`` with the
    ``secondary`` being the one whose pad column runs opposite the primary).  A
    180-degree rotation is the geometric realization of "flip the facing pad
    column" so the bundle stops self-crossing.  Returns ``None`` when the facing
    part could not be resolved (no applyable target).
    """
    orientation = diag.bundle_orientation
    if orientation is None or not orientation.secondary_ref:
        return None
    return PlacementDelta(
        net_name=diag.net_name,
        target_ref=orientation.secondary_ref,
        kind="rotate_180",
        rotation_delta=180.0,
        source_action=RecommendedAction.DE_REVERSE_BUNDLE.value,
        rationale=rationale,
        confidence=confidence,
    )


def _reorder_pins_delta(
    diag: StuckNetDiagnosis, confidence: str, rationale: str
) -> PlacementDelta | None:
    """REORDER_PINS -> a rationale-only delta (no geometry in Phase 1).

    A pad-level re-map applicator does not exist yet, so this carries no
    ``(dx, dy, rotation)`` -- it names the part whose pin order should change
    (the reversed facing part when known) and defers the mechanics to a future
    phase.
    """
    orientation = diag.bundle_orientation
    target_ref = orientation.secondary_ref if orientation else ""
    return PlacementDelta(
        net_name=diag.net_name,
        target_ref=target_ref,
        kind="reorder_pins",
        source_action=RecommendedAction.REORDER_PINS.value,
        rationale=rationale,
        confidence=confidence,
    )


def _translate_delta(
    pcb: PCB, diag: StuckNetDiagnosis, confidence: str, rationale: str
) -> PlacementDelta | None:
    """MOVE_PART -> a minimal bounded translate of the crowding foreign part.

    ``target_ref`` is the foreign component whose pad sits nearest the stranded
    pad (the part physically walling the escape); ``(dx, dy)`` is a step of at
    most :data:`MAX_TRANSLATE_MM` from the foreign-congestion centroid toward
    the widest open escape arc.  Returns ``None`` when there is no foreign
    obstruction to move away from, or no resolvable direction / target.
    """
    positions = _stranded_pad_positions(pcb, diag.net_name)
    if not positions:
        return None

    # The densest stranded pad drives the move (it is the most walled-in).
    best_point: tuple[float, float] | None = None
    best_obstr: list[tuple[float, float, float, int]] = []
    best_count = -1
    for pt in positions:
        obstr = _foreign_obstructions(pcb, diag.net_number, pt, _TRANSLATE_RADIUS_MM)
        if len(obstr) > best_count:
            best_count = len(obstr)
            best_point = pt
            best_obstr = obstr
    if best_point is None or not best_obstr:
        return None

    direction = _widest_open_arc_direction(best_obstr, best_point, ESCAPE_SECTORS)
    if direction is None:
        return None

    target_ref = _nearest_foreign_component(pcb, diag.net_number, best_point)
    if not target_ref:
        return None

    dx = MAX_TRANSLATE_MM * math.cos(direction)
    dy = MAX_TRANSLATE_MM * math.sin(direction)
    return PlacementDelta(
        net_name=diag.net_name,
        target_ref=target_ref,
        kind="translate",
        dx=dx,
        dy=dy,
        source_action=RecommendedAction.MOVE_PART.value,
        rationale=rationale,
        confidence=confidence,
    )


# --- geometry helpers -------------------------------------------------------


def _stranded_pad_positions(pcb: PCB, net_name: str) -> list[tuple[float, float]]:
    """Board-frame positions of ``net_name``'s unconnected pads (read-only)."""
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    analysis = NetStatusAnalyzer(pcb).analyze()
    status = analysis.get_net(net_name)
    if status is None:
        return []
    return [p.position for p in status.unconnected_pads]


def _nearest_foreign_component(pcb: PCB, target_net: int, point: tuple[float, float]) -> str:
    """Reference of the foreign component whose pad is nearest ``point``.

    "Foreign" == any net other than ``target_net``.  Ties break on the smaller
    distance; the scan is deterministic in footprint order.  Returns ``""`` when
    the board has no foreign pad at all.
    """
    px, py = point
    best_ref = ""
    best_dist = math.inf
    for ref, net_number, (bx, by), _size in _iter_board_pads(pcb):
        if net_number == target_net:
            continue
        d = math.hypot(bx - px, by - py)
        if d < best_dist:
            best_dist = d
            best_ref = ref
    return best_ref


def _widest_open_arc_direction(
    obstructions: list[tuple[float, float, float, int]],
    point: tuple[float, float],
    sectors: int,
) -> float | None:
    """Center angle (radians) of the widest open escape arc around ``point``.

    Mirrors :func:`~kicad_tools.router.stuck_classifier._widest_open_arc` but
    returns the *direction* of the widest open run rather than its width.  Every
    obstruction blocks its angular sector; the widest contiguous run of open
    sectors (wrapping around the circle) yields the escape direction.  Returns
    ``0.0`` when nothing blocks (pick +x) and ``None`` when every sector is
    blocked (no lane -> caller declines to emit a move).
    """
    px, py = point
    blocked = [False] * sectors
    for ox, oy, _d, _net in obstructions:
        ang = math.atan2(oy - py, ox - px)
        idx = int((ang + math.pi) / (2 * math.pi) * sectors) % sectors
        blocked[idx] = True

    if not any(blocked):
        return 0.0
    if all(blocked):
        return None

    # Widest run of open (False) sectors over the doubled circular array.
    best_len = 0
    best_start = 0
    run = 0
    run_start = 0
    for i in range(2 * sectors):
        if blocked[i % sectors]:
            run = 0
            run_start = i + 1
        else:
            if run == 0:
                run_start = i
            run += 1
            if run > best_len:
                best_len = run
                best_start = run_start

    center_idx = best_start + best_len / 2.0
    # Invert the sector bucketing: idx = (ang + pi) / (2pi) * sectors.
    return (center_idx / sectors) * 2 * math.pi - math.pi
