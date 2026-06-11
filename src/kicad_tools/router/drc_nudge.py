"""Post-optimization DRC verify-and-nudge pass.

This module runs after TraceOptimizer and before save to detect and repair
clearance violations that the optimizer may have (re-)introduced.  It operates
on in-memory Route objects, not S-expression text.

Repair strategies
-----------------
1. **Segment nudge** -- slide a violating segment perpendicular to its axis by
   the minimum amount needed to restore clearance.
2. **Same-net via merge** -- two vias on the same net closer than
   ``via_diameter + min_drill_clearance`` are merged: one is removed and
   its connecting segments are reconnected to the surviving via.

The pass is iterative (up to ``max_passes`` rounds, default 3) and stops
early when no violations remain or no progress is made.

Usage::

    from kicad_tools.router.drc_nudge import drc_verify_and_nudge

    result = drc_verify_and_nudge(router, max_displacement=0.15)
    if result.remaining_violations:
        print(f"{result.remaining_violations} unresolved violations")
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Autorouter

from .geometry import (
    point_to_segment_distance as _geom_point_to_seg_dist,
    segment_to_segment_distance as _geom_seg_to_seg_dist,
)
from .io import ClearanceViolation, validate_routes
from .layers import Layer
from .primitives import Pad, Route, Segment, Via
from .via_clearance import segment_clears_foreign_via

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class DRCNudgeResult:
    """Summary of a DRC verify-and-nudge pass."""

    initial_violations: int = 0
    remaining_violations: int = 0
    segments_nudged: int = 0
    vias_merged: int = 0
    vias_nudged: int = 0
    passes_run: int = 0
    # Issue #2743: structured skip-reason counters so the user sees
    # "4/6 resolved; 2 unsupported (via-via anchored)" instead of a
    # silent 0/6 with no diagnostic.  Keyed by reason; integer counts.
    skipped: dict[str, int] = field(default_factory=dict)

    def _bump_skipped(self, reason: str) -> None:
        """Increment a structured skip-reason counter."""
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def summary(self) -> str:
        """Return a human-readable summary."""
        resolved = self.initial_violations - self.remaining_violations
        lines = [
            f"DRC nudge: {resolved}/{self.initial_violations} violations resolved "
            f"in {self.passes_run} pass(es)",
        ]
        if self.segments_nudged:
            lines.append(f"  Segments nudged: {self.segments_nudged}")
        if self.vias_nudged:
            lines.append(f"  Vias nudged: {self.vias_nudged}")
        if self.vias_merged:
            lines.append(f"  Same-net vias merged: {self.vias_merged}")
        if self.skipped:
            for reason, count in sorted(self.skipped.items()):
                lines.append(f"  Skipped ({reason}): {count}")
        if self.remaining_violations:
            lines.append(f"  Remaining violations: {self.remaining_violations}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _segment_length(seg: Segment) -> float:
    """Return the Euclidean length of a segment."""
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1
    return math.sqrt(dx * dx + dy * dy)


def _perpendicular_unit(seg: Segment) -> tuple[float, float]:
    """Return the unit vector perpendicular to *seg* (rotated +90 deg).

    For a zero-length segment the perpendicular is undefined; we return (0, 0).
    """
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1
    length = math.sqrt(dx * dx + dy * dy)
    if length < 1e-9:
        return (0.0, 0.0)
    # Perpendicular (rotate 90 deg counter-clockwise)
    return (-dy / length, dx / length)


def _nudge_segment(seg: Segment, nx: float, ny: float, amount: float) -> None:
    """Translate *seg* by ``amount`` along direction ``(nx, ny)`` **in place**."""
    seg.x1 += nx * amount
    seg.y1 += ny * amount
    seg.x2 += nx * amount
    seg.y2 += ny * amount


def _nudge_segment_with_chain(
    seg: Segment,
    nx: float,
    ny: float,
    amount: float,
    router: Autorouter,
    chain_tol: float | None = None,
    result: "DRCNudgeResult | None" = None,
) -> bool:
    """Translate *seg* and update connecting segments to preserve the chain.

    Issue #2475: The basic ``_nudge_segment`` only moves the segment itself,
    leaving any abutting segments stranded with mismatched endpoints — the
    routed chain becomes disconnected and the net silently drops a pad.
    This wrapper records the segment's endpoints **before** the nudge,
    applies the translation, then walks every same-net segment in the
    router and snaps any endpoint that previously coincided with the
    pre-nudge endpoint to the post-nudge endpoint.

    The chain update is a 1-D snap (replace any matching endpoint with
    the new one) so it only fixes connections that were *already* abutting
    within ``chain_tol``.  Endpoints that were already disconnected before
    the nudge are not touched.

    A segment is *not* nudged when one of its endpoints sits on a pad of
    the same net; sliding it would disconnect the pad.  In that case this
    function returns False and leaves ``seg`` unchanged so the caller
    can record the failure.

    Issue #3028 (Part A): the nudge is also DECLINED when the post-nudge
    position would introduce a NEW foreign-net via clearance violation.
    The pre-PR-#3028 nudge had no destination check whatsoever — its only
    guards were same-net anchor protections — so it could repair one
    clearance violation by translating a segment into a *different*
    foreign-net via and silently introduce a worse violation.  The board-
    04 SWDIO/BOOT0 violation at PCB (143.8, 119.7) on B.Cu was a strong
    suspect for this failure mode.  The gate uses the same
    :func:`segment_clears_foreign_via` predicate the 4-quadrant clearance
    matrix uses (PR #2999 / PR #3006 / PR #3019 / PR #3027), so the
    geometry is consistent across the routing pipeline.

    Args:
        seg: The segment to translate.
        nx, ny: Unit vector for the translation direction.
        amount: Translation distance (mm).
        router: Autorouter providing access to all routes and pads.
        chain_tol: Tolerance for matching adjacent segment endpoints.
        result: Optional :class:`DRCNudgeResult` used to record structured
            skip reasons (e.g. ``foreign_via_blocked``).

    Returns:
        True if the segment was successfully nudged and the chain repaired;
        False if the segment was left untouched (e.g. pad-anchored, or
        the post-nudge position would clip a foreign-net via).
    """
    if chain_tol is None:
        chain_tol = _ENDPOINT_TOL

    # Pad-anchor guard.
    if _segment_endpoints_anchored_to_net_pads(seg, seg.net, router):
        logger.debug(
            "Skipping nudge for net %s: segment is pad-anchored",
            seg.net,
        )
        return False

    # Via-anchor guard (Issue #2483).  A chain segment whose endpoint sits
    # on a via centre is anchored to that layer transition: translating the
    # segment slides it off the via and breaks the chain at the layer
    # change, since the same-layer chain walk below cannot drag the via
    # (and the via's other-layer continuation) along with it.  Decline the
    # nudge so the original DRC violation surfaces instead of producing a
    # silent disconnect.
    if _segment_endpoints_anchored_to_net_vias(seg, seg.net, router):
        logger.debug(
            "Skipping nudge for net %s: segment is via-anchored",
            seg.net,
        )
        return False

    # Capture pre-nudge endpoints so we can update neighbour segments
    # and revert if the post-nudge position would violate clearance.
    old_x1, old_y1 = seg.x1, seg.y1
    old_x2, old_y2 = seg.x2, seg.y2

    # Apply the translation to seg itself.
    _nudge_segment(seg, nx, ny, amount)

    new_x1, new_y1 = seg.x1, seg.y1
    new_x2, new_y2 = seg.x2, seg.y2

    # Issue #3028 (Part A): foreign-via destination gate.  Validate the
    # POST-nudge segment position against every foreign-net via in
    # ``router.routes`` BEFORE we commit the chain snap.  If the nudge
    # would introduce a NEW seg-via clearance violation we revert ``seg``
    # to its pre-nudge position (the chain snap has not run yet so the
    # neighbours are still consistent) and let the original DRC violation
    # surface in the post-save report.  The pre-existing violation is the
    # lesser evil; the alternative is silently swapping it for a worse
    # foreign-via violation that the existing 4-quadrant clearance matrix
    # would otherwise have prevented if the segment were committed inside
    # the negotiated routing loop.
    if _post_nudge_introduces_foreign_via_violation(seg, router):
        # Revert ``seg`` to its pre-nudge position.  No chain snap has
        # been applied yet so the neighbours are still consistent.
        seg.x1, seg.y1 = old_x1, old_y1
        seg.x2, seg.y2 = old_x2, old_y2
        if result is not None:
            result._bump_skipped("foreign_via_blocked")
        logger.debug(
            "Declining nudge for net %s: post-nudge position would clip "
            "a foreign-net via",
            seg.net,
        )
        return False

    # Walk all same-net segments and snap any endpoint that matched the
    # old position of seg's endpoint to the new position.  Skip ``seg``
    # itself.  We also restrict to the same routing layer because routed
    # chains rarely cross layers without a via in between (and a via
    # provides the freedom we need; segments do not).
    routes = getattr(router, "routes", None) or []
    for route in routes:
        if route.net != seg.net:
            continue
        for other in route.segments:
            if other is seg:
                continue
            if other.layer != seg.layer:
                continue
            # Endpoint 1 of "other" matches old endpoint 1 of seg?
            if (
                abs(other.x1 - old_x1) < chain_tol
                and abs(other.y1 - old_y1) < chain_tol
            ):
                other.x1 = new_x1
                other.y1 = new_y1
            elif (
                abs(other.x1 - old_x2) < chain_tol
                and abs(other.y1 - old_y2) < chain_tol
            ):
                other.x1 = new_x2
                other.y1 = new_y2
            # Endpoint 2 of "other" matches?
            if (
                abs(other.x2 - old_x1) < chain_tol
                and abs(other.y2 - old_y1) < chain_tol
            ):
                other.x2 = new_x1
                other.y2 = new_y1
            elif (
                abs(other.x2 - old_x2) < chain_tol
                and abs(other.y2 - old_y2) < chain_tol
            ):
                other.x2 = new_x2
                other.y2 = new_y2

    return True


def _post_nudge_introduces_foreign_via_violation(
    seg: Segment,
    router: Autorouter,
) -> bool:
    """Return True if ``seg`` at its current position clips a foreign-net via.

    Issue #3028 (Part A): destination gate for :func:`_nudge_segment_with_chain`.
    Walk every via in ``router.routes`` and apply
    :func:`segment_clears_foreign_via` (the same predicate used by the
    in-loop 4-quadrant matrix at PRs #2999 / #3006 / #3019 / #3027) to the
    current segment position.  Same-net vias are skipped — moving the
    segment closer to one of its own vias would be a chain-snap or a
    layer-transition concern, NOT a clearance violation.

    The trace_clearance defaults to ``router.rules.trace_clearance`` when
    available; otherwise we fall back to ``DesignRules`` default (0.2 mm)
    so the test fixtures that omit ``rules`` still exercise a meaningful
    threshold.

    Args:
        seg: The candidate segment in its proposed post-nudge position.
        router: The autorouter providing ``routes`` and ``rules``.

    Returns:
        True if ANY foreign-net via on ``seg``'s layer would be too close
        to ``seg`` (i.e. the predicate returns False); False when every
        foreign via clears.
    """
    rules = getattr(router, "rules", None)
    trace_clearance = getattr(rules, "trace_clearance", 0.2) if rules else 0.2

    routes = getattr(router, "routes", None) or []
    for route in routes:
        # Caller-side own-net filter: a segment moving closer to one of
        # its own vias is not a DRC violation -- that's a chain
        # adjacency.  Mirror the same-net filtering convention used by
        # the in-loop matrix (see ``segment_clears_foreign_via`` docs).
        if route.net == seg.net:
            continue
        for via in route.vias:
            if not segment_clears_foreign_via(seg, via, trace_clearance):
                return True
    return False


# Tolerance in mm for considering a segment endpoint anchored to a pad
# centre.  Routed segments terminate at pad centres exactly (within float
# rounding), so a small tolerance here suffices.  This is intentionally
# tighter than ``_ENDPOINT_TOL`` (0.05 mm) because we only want to detect
# *actual* pad anchors, not nearby segment intersections.
_PAD_ANCHOR_TOL = 0.02

# Tolerance in mm for considering a segment endpoint anchored to a via
# centre.  Vias also terminate segments at exact centres within float
# rounding, so the same tolerance as ``_PAD_ANCHOR_TOL`` is appropriate
# (Issue #2483).
_VIA_ANCHOR_TOL = 0.02


def _segment_endpoints_anchored_to_net_pads(
    seg: Segment,
    net: int,
    router: Autorouter,
) -> bool:
    """Return True when either endpoint of ``seg`` sits on a pad of ``net``.

    Issue #2475: ``drc_verify_and_nudge`` translates whole segments by up
    to ``max_displacement`` (0.2 mm) to repair clearance violations.  If the
    segment terminates at a pad centre, that translation moves the segment
    away from the pad and disconnects the net at that pin.  This was the
    mechanism by which board 05 PHASE_B silently dropped from 4/4 to 3/4
    pads after the router itself had achieved full connectivity: the
    PHASE_B vs GATE_CL pad clearance violation was "resolved" by sliding
    the PHASE_B segment off its J2:2 anchor.

    The right thing to do for an anchored segment is to leave it alone
    (preserve electrical connectivity) and let the unresolved clearance
    surface in the post-save report so the user sees a real violation
    rather than a silently-broken trace.

    Args:
        seg: The segment about to be nudged.
        net: The net the segment belongs to.
        router: The autorouter, used to look up pad positions.

    Returns:
        True if either endpoint is within ``_PAD_ANCHOR_TOL`` of any pad
        on ``net``; False otherwise (or when pad data is unavailable).
    """
    pads = getattr(router, "pads", None)
    nets = getattr(router, "nets", None)
    if not pads or not nets:
        return False

    pad_keys = nets.get(net) or []
    for key in pad_keys:
        pad = pads.get(key)
        if pad is None:
            continue
        # Endpoint 1
        if (
            abs(seg.x1 - pad.x) < _PAD_ANCHOR_TOL
            and abs(seg.y1 - pad.y) < _PAD_ANCHOR_TOL
        ):
            return True
        # Endpoint 2
        if (
            abs(seg.x2 - pad.x) < _PAD_ANCHOR_TOL
            and abs(seg.y2 - pad.y) < _PAD_ANCHOR_TOL
        ):
            return True
    return False


def _segment_endpoints_anchored_to_net_vias(
    seg: Segment,
    net: int,
    router: Autorouter,
) -> bool:
    """Return True when either endpoint of ``seg`` sits on a via centre of ``net``.

    Companion to :func:`_segment_endpoints_anchored_to_net_pads`.  Vias are
    layer transitions: the chain walk in :func:`_nudge_segment_with_chain`
    intentionally restricts itself to same-layer same-net segments, which
    means a via that links a top-layer segment to a bottom-layer segment
    is invisible to the snap-update logic.  Translating a segment off a
    via centre therefore disconnects the chain at the layer transition.

    Issue #2483: the chain-aware nudge introduced by #2479 fixed the
    same-layer chain disconnection case (board 05 PHASE_B) but did not
    consider vias.  When a net routes through a via, a nudge can slide
    the segment off the via centre, leaving the via dangling on the
    other-layer continuation.

    The conservative fix mirrors the pad-anchor guard: decline the nudge
    when an endpoint is via-anchored.  Declining surfaces the original
    DRC violation as a detectable signal rather than a silent disconnect.

    Args:
        seg: The segment about to be nudged.
        net: The net the segment belongs to.
        router: The autorouter, used to look up via positions per route.

    Returns:
        True if either endpoint of ``seg`` is within ``_VIA_ANCHOR_TOL`` of
        any via on ``net``; False otherwise (or when route data is
        unavailable).  Note that vias live on per-route ``Route.vias``
        lists, not on a top-level ``router.vias`` attribute.
    """
    routes = getattr(router, "routes", None) or []
    for route in routes:
        if route.net != net:
            continue
        for via in route.vias:
            # Endpoint 1
            if (
                abs(seg.x1 - via.x) < _VIA_ANCHOR_TOL
                and abs(seg.y1 - via.y) < _VIA_ANCHOR_TOL
            ):
                return True
            # Endpoint 2
            if (
                abs(seg.x2 - via.x) < _VIA_ANCHOR_TOL
                and abs(seg.y2 - via.y) < _VIA_ANCHOR_TOL
            ):
                return True
    return False


def _point_to_segment_distance(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    """Minimum distance from point (px, py) to segment (x1,y1)-(x2,y2)."""
    return _geom_point_to_seg_dist(px, py, x1, y1, x2, y2)


def _segment_to_segment_distance(
    x1: float, y1: float, x2: float, y2: float,
    x3: float, y3: float, x4: float, y4: float,
) -> float:
    """Minimum distance between two line segments."""
    return _geom_seg_to_seg_dist(x1, y1, x2, y2, x3, y3, x4, y4)


# ---------------------------------------------------------------------------
# Same-net via merging
# ---------------------------------------------------------------------------

COINCIDENT_THRESHOLD = 0.01  # mm -- legacy constant kept for backward compat
_ENDPOINT_TOL = 0.05  # mm -- tolerance for matching segment endpoints to via positions


def _expand_via_layers(surviving: Via, removed: Via) -> None:
    """Expand the surviving via's layers to span both vias' layer ranges.

    Issue #1802: When merging vias with different layer pairs (e.g.,
    F.Cu/In1.Cu and B.Cu/F.Cu), the merged via must connect all layers
    that either original via connected.  This converts the surviving via
    to a through-via (or wider span) when necessary.
    """
    min_layer = min(surviving.layers[0].value, surviving.layers[1].value,
                    removed.layers[0].value, removed.layers[1].value)
    max_layer = max(surviving.layers[0].value, surviving.layers[1].value,
                    removed.layers[0].value, removed.layers[1].value)
    if min_layer != surviving.layers[0].value or max_layer != surviving.layers[1].value:
        surviving.layers = (Layer(min_layer), Layer(max_layer))


def _compute_merge_threshold(router: Autorouter) -> float:
    """Return the drill-overlap merge threshold from design rules.

    Two same-net vias closer than ``via_diameter + min_drill_clearance``
    would create a drill overlap DRC violation and must be merged.
    Falls back to ``COINCIDENT_THRESHOLD`` when rules are unavailable.
    """
    rules = getattr(router, "rules", None)
    if rules is None:
        return COINCIDENT_THRESHOLD
    via_diameter = getattr(rules, "via_diameter", 0.0)
    min_drill = getattr(rules, "min_drill_clearance", 0.0)
    threshold = via_diameter + min_drill
    # Ensure we never go below the legacy coincident threshold
    return max(threshold, COINCIDENT_THRESHOLD)


def _merge_same_net_vias(router: Autorouter) -> int:
    """Merge same-net vias that are closer than the drill-overlap threshold.

    Issue #1796: The previous implementation only merged nearly-coincident
    vias (within 0.01 mm) and only within a single route.  Vias placed by
    independent routing passes (e.g. escape routing) on the same net could
    end up close enough to violate drill-to-drill clearance without being
    merged.

    This version:
    * Uses ``via_diameter + min_drill_clearance`` as the merge threshold.
    * Merges across different routes that share the same net.
    * Keeps the first via encountered and reconnects all segments from
      the removed via to the surviving one.

    Returns:
        Number of vias merged.
    """
    merge_threshold = _compute_merge_threshold(router)
    total_merged = 0

    # --- Phase 1: intra-route merges (vias within the same Route object) ---
    for route in router.routes:
        if len(route.vias) < 2:
            continue

        merged_indices: set[int] = set()

        for i in range(len(route.vias)):
            if i in merged_indices:
                continue
            via_a = route.vias[i]
            for j in range(i + 1, len(route.vias)):
                if j in merged_indices:
                    continue
                via_b = route.vias[j]
                dist = math.sqrt(
                    (via_a.x - via_b.x) ** 2 + (via_a.y - via_b.y) ** 2
                )
                if dist < merge_threshold:
                    _reconnect_segments(
                        route.segments, via_b.x, via_b.y,
                        via_a.x, via_a.y, _ENDPOINT_TOL,
                    )
                    _expand_via_layers(via_a, via_b)
                    merged_indices.add(j)

        if merged_indices:
            route.vias = [
                v for idx, v in enumerate(route.vias) if idx not in merged_indices
            ]
            total_merged += len(merged_indices)

    # --- Phase 2: cross-route merges (different Route objects, same net) ---
    # Group routes by net so we only compare routes that could conflict.
    from collections import defaultdict

    net_routes: dict[int, list[Route]] = defaultdict(list)
    for route in router.routes:
        if route.vias:
            net_routes[route.net].append(route)

    for net_id, routes in net_routes.items():
        if len(routes) < 2:
            continue

        # For each pair of routes on the same net, check their vias.
        for ri in range(len(routes)):
            route_a = routes[ri]
            for rj in range(ri + 1, len(routes)):
                route_b = routes[rj]
                remove_from_b: set[int] = set()

                for via_a in route_a.vias:
                    for bj, via_b in enumerate(route_b.vias):
                        if bj in remove_from_b:
                            continue
                        dist = math.sqrt(
                            (via_a.x - via_b.x) ** 2
                            + (via_a.y - via_b.y) ** 2
                        )
                        if dist < merge_threshold:
                            # Keep via_a, remove via_b, reconnect route_b segments
                            _reconnect_segments(
                                route_b.segments, via_b.x, via_b.y,
                                via_a.x, via_a.y, _ENDPOINT_TOL,
                            )
                            _expand_via_layers(via_a, via_b)
                            remove_from_b.add(bj)

                if remove_from_b:
                    route_b.vias = [
                        v for idx, v in enumerate(route_b.vias)
                        if idx not in remove_from_b
                    ]
                    total_merged += len(remove_from_b)

    # --- Phase 3: merge new-route vias against pre-existing vias ---
    # Pre-existing vias survive; conflicting new vias are removed/relocated.
    existing_routes: list[Route] = getattr(router, "existing_routes", [])
    if existing_routes:
        # Build lookup of existing vias grouped by net.
        existing_net_vias: dict[int, list["Via"]] = defaultdict(list)
        for eroute in existing_routes:
            for evia in eroute.vias:
                existing_net_vias[eroute.net].append(evia)

        for route in router.routes:
            ev_list = existing_net_vias.get(route.net)
            if not ev_list:
                continue

            remove_indices: set[int] = set()
            for ni, new_via in enumerate(route.vias):
                if ni in remove_indices:
                    continue
                for existing_via in ev_list:
                    dist = math.sqrt(
                        (new_via.x - existing_via.x) ** 2
                        + (new_via.y - existing_via.y) ** 2
                    )
                    if dist < merge_threshold:
                        # Keep existing via, remove new via.  Reconnect
                        # the new route's segments to the existing via pos.
                        _reconnect_segments(
                            route.segments, new_via.x, new_via.y,
                            existing_via.x, existing_via.y, _ENDPOINT_TOL,
                        )
                        # Expand existing via layers to cover new via layers
                        _expand_via_layers(existing_via, new_via)
                        remove_indices.add(ni)
                        break  # new_via already merged, move on

            if remove_indices:
                route.vias = [
                    v for idx, v in enumerate(route.vias)
                    if idx not in remove_indices
                ]
                total_merged += len(remove_indices)

    return total_merged


def _reconnect_segments(
    segments: list[Segment],
    old_x: float,
    old_y: float,
    new_x: float,
    new_y: float,
    tol: float | None = None,
) -> None:
    """Snap segment endpoints at ``(old_x, old_y)`` to ``(new_x, new_y)``.

    Args:
        segments: Segments to scan and update in place.
        old_x, old_y: Position of the removed via.
        new_x, new_y: Position of the surviving via.
        tol: Coordinate tolerance for matching endpoints.
            Defaults to ``COINCIDENT_THRESHOLD`` for backward compatibility.
    """
    if tol is None:
        tol = COINCIDENT_THRESHOLD
    for seg in segments:
        if abs(seg.x1 - old_x) < tol and abs(seg.y1 - old_y) < tol:
            seg.x1 = new_x
            seg.y1 = new_y
        if abs(seg.x2 - old_x) < tol and abs(seg.y2 - old_y) < tol:
            seg.x2 = new_x
            seg.y2 = new_y


# ---------------------------------------------------------------------------
# Segment-to-segment nudge
# ---------------------------------------------------------------------------

def _try_nudge_seg_seg(
    violation: ClearanceViolation,
    router: Autorouter,
    max_displacement: float,
    result: DRCNudgeResult | None = None,
) -> bool:
    """Attempt to nudge a segment to fix a seg-seg violation.

    We find the offending segment in the router's routes and push it
    away from the approximate violation location by the deficit + margin.

    Returns True if the nudge was applied (within budget).
    """
    deficit = violation.required - violation.distance
    if deficit <= 0:
        return False

    # Add a small margin (10% of required clearance, min 0.005mm)
    margin = max(0.005, violation.required * 0.10)
    nudge_amount = deficit + margin

    if nudge_amount > max_displacement:
        return False

    # Find the segment in the router routes
    target_seg = _find_segment(
        router, violation.net, violation.segment_index,
        violation.x1, violation.y1, violation.x2, violation.y2,
        layer=violation.layer,
    )
    if target_seg is None:
        return False

    # Determine nudge direction: away from the violation location (which
    # approximates the obstacle midpoint), projected onto the segment's
    # perpendicular axis.
    perp_x, perp_y = _perpendicular_unit(target_seg)
    if perp_x == 0.0 and perp_y == 0.0:
        return False

    if violation.location is not None:
        obs_x, obs_y = violation.location
        seg_mid_x = (target_seg.x1 + target_seg.x2) / 2
        seg_mid_y = (target_seg.y1 + target_seg.y2) / 2
        # Dot product of (seg_mid - obstacle) with perpendicular tells
        # us which side the obstacle is on.
        dot = (seg_mid_x - obs_x) * perp_x + (seg_mid_y - obs_y) * perp_y
        if dot < 0:
            perp_x, perp_y = -perp_x, -perp_y

    # Issue #2475: Use chain-aware nudge so abutting same-net segments are
    # snapped to the new endpoint and the routed chain stays connected.
    # Issue #3028: pass ``result`` so the foreign-via destination gate
    # can record a structured skip reason on refusal.
    return _nudge_segment_with_chain(
        target_seg, perp_x, perp_y, nudge_amount, router, result=result,
    )


def _try_nudge_seg_via(
    violation: ClearanceViolation,
    router: Autorouter,
    max_displacement: float,
    result: DRCNudgeResult | None = None,
) -> bool:
    """Attempt to nudge a segment away from a via."""
    deficit = violation.required - violation.distance
    if deficit <= 0:
        return False

    margin = max(0.005, violation.required * 0.10)
    nudge_amount = deficit + margin

    if nudge_amount > max_displacement:
        return False

    target_seg = _find_segment(
        router, violation.net, violation.segment_index,
        violation.x1, violation.y1, violation.x2, violation.y2,
        layer=violation.layer,
    )
    if target_seg is None:
        return False

    # Nudge away from the via location
    if violation.location is None:
        return False

    via_x, via_y = violation.location
    seg_mid_x = (target_seg.x1 + target_seg.x2) / 2
    seg_mid_y = (target_seg.y1 + target_seg.y2) / 2

    # Direction from via to segment midpoint
    away_dx = seg_mid_x - via_x
    away_dy = seg_mid_y - via_y
    away_len = math.sqrt(away_dx * away_dx + away_dy * away_dy)
    if away_len < 1e-9:
        # Segment midpoint is on the via -- use perpendicular
        nx, ny = _perpendicular_unit(target_seg)
    else:
        nx = away_dx / away_len
        ny = away_dy / away_len

    # Issue #2475: Use chain-aware nudge.
    # Issue #3028: pass ``result`` for the foreign-via destination gate.
    return _nudge_segment_with_chain(
        target_seg, nx, ny, nudge_amount, router, result=result,
    )


def _find_via_at(
    router: Autorouter,
    net: int,
    x: float,
    y: float,
    tol: float = 0.001,
) -> tuple[Route, Via] | None:
    """Locate a via on *net* at coordinate (x, y).

    Returns the (route, via) pair, or None when no match is found.
    Used by the via-via nudge handler (Issue #2743) which receives
    violations with ``segment_index == -1`` and ``x1 == x2 == via.x``,
    ``y1 == y2 == via.y``.
    """
    for route in router.routes:
        if route.net != net:
            continue
        for via in route.vias:
            if abs(via.x - x) < tol and abs(via.y - y) < tol:
                return route, via
    return None


def _via_is_pad_anchored(
    via: Via,
    net: int,
    router: Autorouter,
) -> bool:
    """Return True when ``via`` sits on a pad of the same net.

    A via that coincides with a pad centre is an in-pad escape via (or a
    through-hole pad that the routing layer treats as a via).  Moving such
    a via off its pad disconnects the net, so we decline to nudge it.
    Mirrors the segment pad-anchor guard at
    :func:`_segment_endpoints_anchored_to_net_pads`.
    """
    pads = getattr(router, "pads", None)
    nets = getattr(router, "nets", None)
    if not pads or not nets:
        return False
    pad_keys = nets.get(net) or []
    for key in pad_keys:
        pad = pads.get(key)
        if pad is None:
            continue
        if abs(via.x - pad.x) < _PAD_ANCHOR_TOL and abs(via.y - pad.y) < _PAD_ANCHOR_TOL:
            return True
    return False


def _nudge_via_with_chain(
    via: Via,
    new_x: float,
    new_y: float,
    router: Autorouter,
    chain_tol: float | None = None,
) -> bool:
    """Move ``via`` to (new_x, new_y) and reconnect same-net segments.

    Issue #2743: When repairing a via-via clearance violation we slide one
    of the two vias perpendicular to the line between them.  The same-net
    segments that terminate at the via centre must also be snapped to the
    new position so the chain stays electrically connected.

    Refuses to move a via that is pad-anchored (a via dropped on a pad
    centre is part of the connection to that pad — moving it would break
    the net).  Returns True on success, False when the move is declined.
    """
    if chain_tol is None:
        chain_tol = _ENDPOINT_TOL

    if _via_is_pad_anchored(via, via.net, router):
        logger.debug(
            "Skipping via nudge for net %s: via is pad-anchored",
            via.net,
        )
        return False

    old_x, old_y = via.x, via.y
    via.x = new_x
    via.y = new_y

    # Snap any same-net segment endpoint that previously coincided with
    # this via's centre to the new position.
    routes = getattr(router, "routes", None) or []
    for route in routes:
        if route.net != via.net:
            continue
        for seg in route.segments:
            if abs(seg.x1 - old_x) < chain_tol and abs(seg.y1 - old_y) < chain_tol:
                seg.x1 = new_x
                seg.y1 = new_y
            if abs(seg.x2 - old_x) < chain_tol and abs(seg.y2 - old_y) < chain_tol:
                seg.x2 = new_x
                seg.y2 = new_y
    return True


def _try_nudge_via_via(
    violation: ClearanceViolation,
    router: Autorouter,
    max_displacement: float,
    result: DRCNudgeResult | None = None,
) -> bool:
    """Attempt to repair a via-vs-via clearance violation.

    Issue #2743: The validator at :func:`validate_routes` emits via-via
    violations with ``obstacle_type="via"``, ``segment_index=-1``, and
    ``x1==x2==via_a.x``, ``y1==y2==via_a.y`` (the offending via centre).
    ``location`` holds the midpoint between the two vias.  The legacy
    ``_try_nudge_seg_via`` calls ``_find_segment(..., -1, x1, y1, x1, y1)``
    which can never match a zero-length segment, so via-via violations
    used to silently no-op — observed as the ``0/6 resolved`` regression
    on board 02.

    Repair strategy: slide one via perpendicular to the line between the
    two vias by ``(required - distance + margin)``.  ``_nudge_via_with_chain``
    refuses to move a pad-anchored via (preserving connectivity); when
    that happens we try the other via.  If both are anchored we record
    a structured skip reason and return False.

    Args:
        violation: The via-via clearance violation.
        router: Autorouter instance.
        max_displacement: Maximum displacement budget in mm.
        result: Optional ``DRCNudgeResult`` to record skip reasons and
            ``vias_nudged`` increments on.

    Returns:
        True if a via was successfully nudged; False otherwise.
    """
    deficit = violation.required - violation.distance
    if deficit <= 0:
        return False

    margin = max(0.005, violation.required * 0.10)
    nudge_amount = deficit + margin

    if nudge_amount > max_displacement:
        if result is not None:
            result._bump_skipped("via_via_budget")
        return False

    if violation.location is None:
        # Without a midpoint we can't compute the nudge direction.
        if result is not None:
            result._bump_skipped("via_via_no_location")
        return False

    # Locate via A on the violating net by (x1, y1) – this is one of the
    # two vias by the emit contract documented on ClearanceViolation.
    match_a = _find_via_at(router, violation.net, violation.x1, violation.y1)
    if match_a is None:
        if result is not None:
            result._bump_skipped("via_via_not_found")
        return False
    route_a, via_a = match_a

    # via B is on the obstacle net.  ``location`` is the midpoint between
    # the two via centres, so via_b = 2 * location - via_a.
    loc_x, loc_y = violation.location
    bx = 2 * loc_x - via_a.x
    by = 2 * loc_y - via_a.y
    match_b = _find_via_at(router, violation.obstacle_net, bx, by)

    # Direction from via_b -> via_a (move via_a further from via_b).
    if match_b is not None:
        _, via_b = match_b
        dx = via_a.x - via_b.x
        dy = via_a.y - via_b.y
    else:
        # Fallback: move via_a away from the midpoint.
        dx = via_a.x - loc_x
        dy = via_a.y - loc_y
    length = math.sqrt(dx * dx + dy * dy)
    if length < 1e-9:
        # Vias are coincident — cannot derive a direction.  Skip and
        # surface as a structured failure.
        if result is not None:
            result._bump_skipped("via_via_coincident")
        return False
    ux = dx / length
    uy = dy / length

    # Try via_a first.  Move only half of the deficit because shifting
    # via_a away from via_b by ``nudge_amount`` adds the full deficit to
    # the centre-to-centre distance.  Use the full amount to give a
    # small over-shoot for stability.
    new_x = via_a.x + ux * nudge_amount
    new_y = via_a.y + uy * nudge_amount
    if _nudge_via_with_chain(via_a, new_x, new_y, router):
        if result is not None:
            result.vias_nudged += 1
        return True

    # via_a was pad-anchored.  Try via_b in the opposite direction.
    if match_b is not None:
        _, via_b = match_b
        new_bx = via_b.x - ux * nudge_amount
        new_by = via_b.y - uy * nudge_amount
        if _nudge_via_with_chain(via_b, new_bx, new_by, router):
            if result is not None:
                result.vias_nudged += 1
            return True

    # Both vias declined — surface a structured skip.
    if result is not None:
        result._bump_skipped("via_via_anchored")
    return False


# ---------------------------------------------------------------------------
# Same-net via-in-pad nudge (Issue #3112)
# ---------------------------------------------------------------------------
#
# The negotiated A* router does not consult the manufacturer's
# ``via_in_pad_supported`` flag when placing escape vias.  On profiles that
# do NOT support filled/plated-over via-in-pad processing (default
# ``jlcpcb``, ``oshpark``, ``seeed``, ``flashpcb``) the router can leave
# a via drilled inside an SMD pad of the *same* net -- which the router
# treats as a no-op (the via is already connected via the pad) but which
# DRC flags as a manufacturability error
# (``src/kicad_tools/validate/rules/via_in_pad.py``).
#
# Detection surface (per Issue #3112 AC#2): ``router/io.py:validate_routes``
# explicitly skips same-net pads when emitting ``ClearanceViolation`` (see
# the ``Skip pads on the same net`` comment in ``io.py``).  The existing
# nudge dispatch therefore never surfaces the via-in-same-net-pad case,
# so we run an explicit sweep here -- iterating ``router.pads`` for SMD
# pads and ``route.vias`` for vias of the same net, gated on
# ``rules.manufacturer``'s ``via_in_pad_supported`` capability flag.
# (We use the inline scan rather than constructing a fresh ``PCB`` view
# and invoking ``ViaInPadRule`` because the router primitives carry the
# absolute pad coordinates and rotation-adjusted width/height directly,
# avoiding a save-load round-trip during the nudge pass.)


def _router_pad_bbox(pad: Pad) -> tuple[float, float, float, float]:
    """Return the axis-aligned bounding box for a router primitive ``Pad``.

    The router pad already carries absolute coordinates and (for cardinal
    footprint rotations) swapped ``width/height``, so the AABB is just
    ``(x ± w/2, y ± h/2)``.  Non-cardinal rotations are conservatively
    approximated by the same convention used elsewhere in this module --
    matches :func:`kicad_tools.validate.rules.via_in_pad._pad_absolute_bbox`
    for the cardinal cases which are the only ones the router emits.
    """
    half_w = pad.width / 2.0
    half_h = pad.height / 2.0
    return (pad.x - half_w, pad.y - half_h, pad.x + half_w, pad.y + half_h)


def _via_drill_inside_bbox(
    via: Via,
    pad_bbox: tuple[float, float, float, float],
    tol: float = 0.005,
) -> bool:
    """Return True if ``via``'s drill circle is fully inside ``pad_bbox``.

    Mirrors :func:`kicad_tools.validate.rules.via_in_pad._via_inside_pad`
    with the same DRC tolerance idiom so we trigger the nudge on
    *exactly* the violations DRC would report (and not on drill edges
    that merely touch the pad bbox -- those are neckdowns, not in-pad
    vias).
    """
    min_x, min_y, max_x, max_y = pad_bbox
    radius = via.drill / 2.0
    return (
        via.x - radius >= min_x - tol
        and via.x + radius <= max_x + tol
        and via.y - radius >= min_y - tol
        and via.y + radius <= max_y + tol
    )


def _snap_chain_endpoints(
    segments: list[Segment],
    old_x: float,
    old_y: float,
    new_x: float,
    new_y: float,
    tol: float = _ENDPOINT_TOL,
) -> None:
    """Snap any segment endpoint at ``(old_x, old_y)`` to ``(new_x, new_y)``.

    Thin alias around :func:`_reconnect_segments` with the
    nudge-appropriate default tolerance (``_ENDPOINT_TOL`` = 0.05 mm,
    matching :func:`_nudge_segment_with_chain`'s chain walk).  Issue
    #3112: factored out so the via-pad nudge handler can share the same
    chain-snap idiom used by :func:`_nudge_via_with_chain` and the
    same-net merge path.
    """
    _reconnect_segments(segments, old_x, old_y, new_x, new_y, tol=tol)


def _router_via_in_pad_supported(router: Autorouter) -> bool:
    """Return True when the router's manufacturer supports via-in-pad.

    Reads ``router.rules.manufacturer`` and consults
    :func:`kicad_tools.router.mfr_limits.get_mfr_limits`.  Returns False
    when no manufacturer is configured or the manufacturer is unknown
    (the conservative default that matches the escape router's behaviour
    -- see ``src/kicad_tools/router/escape.py``).
    """
    rules = getattr(router, "rules", None)
    if rules is None:
        return False
    mfr_id = getattr(rules, "manufacturer", None)
    if not mfr_id:
        return False
    try:
        from .mfr_limits import get_mfr_limits

        limits = get_mfr_limits(mfr_id)
    except (ValueError, ImportError):
        return False
    return bool(getattr(limits, "via_in_pad_supported", False))


def _try_nudge_via_pad(
    via: Via,
    pad_bbox: tuple[float, float, float, float],
    router: Autorouter,
    max_displacement: float,
    *,
    required_clearance: float | None = None,
    result: DRCNudgeResult | None = None,
) -> bool:
    """Slide a same-net via off an SMD pad it has been placed inside.

    Issue #3112: companion handler to :func:`_try_nudge_seg_pad`, but
    operates on a **via** rather than a segment.  Picks the cardinal
    exit (left/right/top/bottom) that minimises displacement, snapping
    every same-net segment endpoint that was within ``_ENDPOINT_TOL`` of
    the via's old position so the routed chain stays electrically
    connected.

    Refuses the move when the required displacement exceeds
    ``max_displacement`` (matches the segment handlers' over-budget
    semantics).  Schema-only surgery: zero ``str.replace()`` / regex on
    ``.kicad_pcb``; the via mutation happens on the in-memory
    :class:`Via` (router primitive, NOT the schema layer's ``Via`` --
    those have different attribute names; ``.x/.y`` here vs ``.position``
    there).

    Args:
        via: The router primitive via inside the pad bbox.
        pad_bbox: ``(min_x, min_y, max_x, max_y)`` of the offending pad.
        router: Autorouter providing access to all routes for chain
            snapping.
        max_displacement: Maximum displacement budget in mm.  Matches
            the budget used by the seg-side handlers.
        required_clearance: The trace clearance to leave outside the
            pad edge.  When ``None``, reads from
            ``router.rules.trace_clearance`` (default 0.2 mm).  The
            small extra margin (``max(0.005, required * 0.10)``)
            mirrors the seg-pad handler's formula.
        result: Optional :class:`DRCNudgeResult` for structured skip
            counters.

    Returns:
        True when the via was moved within budget and its connecting
        segments were snapped; False otherwise (over budget, no rule
        info, etc).
    """
    if required_clearance is None:
        rules = getattr(router, "rules", None)
        required_clearance = getattr(rules, "trace_clearance", 0.2) if rules else 0.2

    min_x, min_y, max_x, max_y = pad_bbox
    cx, cy = via.x, via.y
    r = via.diameter / 2.0
    margin = max(0.005, required_clearance * 0.10)
    # Each cardinal candidate places the via just outside the pad edge,
    # with the via's copper diameter (annular ring radius) + the
    # required trace clearance + the same 10% margin used elsewhere
    # in this module.
    offset = r + required_clearance + margin

    candidates: list[tuple[float, float]] = [
        (min_x - offset, cy),  # left
        (max_x + offset, cy),  # right
        (cx, min_y - offset),  # top (smaller y in PCB conventions)
        (cx, max_y + offset),  # bottom
    ]
    new_x, new_y = min(
        candidates,
        key=lambda c: math.hypot(c[0] - cx, c[1] - cy),
    )

    displacement = math.hypot(new_x - cx, new_y - cy)
    if displacement > max_displacement:
        if result is not None:
            result._bump_skipped("via_pad_budget")
        return False

    old_x, old_y = via.x, via.y
    via.x = new_x
    via.y = new_y

    # Snap every same-net segment endpoint that previously coincided
    # with the via centre to the new position.  Walks all routes (not
    # just the via's owning route) because the router occasionally
    # splits a chain across multiple Route objects on the same net.
    routes = getattr(router, "routes", None) or []
    for route in routes:
        if route.net != via.net:
            continue
        _snap_chain_endpoints(route.segments, old_x, old_y, new_x, new_y)

    return True


# Budget used for the via-in-pad sweep.  The general ``max_displacement``
# (0.2 mm) used by the seg-side handlers is far too small here -- exiting
# an SMD pad takes roughly ``pad_half_width + via_radius + clearance``,
# which for a typical 1.0 x 1.3 mm pad on JLCPCB is ~0.85 mm.  We
# default to 2.0 mm which comfortably covers the common SMD pad sizes
# (0805 / 1206 / SOIC / SOT-23) and leaves headroom for the small
# clearance margin without allowing pathological huge moves.
_VIA_IN_PAD_MAX_DISPLACEMENT = 2.0


def _scan_and_repair_via_in_pad(
    router: Autorouter,
    max_displacement: float,
    result: DRCNudgeResult,
) -> int:
    """Scan ``router`` for same-net via-in-pad cases and nudge them.

    Issue #3112: runs the explicit detection sweep that the
    :func:`validate_routes` stream cannot surface (it intentionally
    skips same-net pads at ``router/io.py:1756``).  Gated on the
    manufacturer's ``via_in_pad_supported`` capability flag -- when the
    profile supports via-in-pad (e.g. ``jlcpcb-tier1``, ``pcbway``)
    this is a no-op.

    Note on the displacement budget: the via-in-pad sweep uses its own
    budget (``_VIA_IN_PAD_MAX_DISPLACEMENT``, default 2.0 mm) rather
    than the seg-side handlers' ``max_displacement`` (0.2 mm).  Sliding
    a via off an SMD pad requires ``pad_half_width + via_radius +
    clearance`` of travel -- about 0.85 mm for a typical 1.0 x 1.3 mm
    pad -- so the seg-side budget would refuse every realistic case.
    The caller may pass a smaller value via ``max_displacement`` if they
    want to cap moves more tightly; the sweep uses the **max** of the
    caller-supplied budget and the sweep default so the seg-side budget
    never accidentally veto a legitimate via-pad rescue.

    Returns:
        Number of vias successfully nudged.
    """
    via_pad_budget = max(max_displacement, _VIA_IN_PAD_MAX_DISPLACEMENT)
    if _router_via_in_pad_supported(router):
        # Manufacturer supports via-in-pad -- nothing to do.
        return 0

    pads = getattr(router, "pads", None) or {}
    routes = getattr(router, "routes", None) or []
    if not pads or not routes:
        return 0

    # Group SMD pads by net.  Net 0 is unconnected copper and is
    # intentionally excluded -- the via-in-pad rule only fires for
    # vias that share the pad's net.
    pads_by_net: dict[int, list[Pad]] = {}
    for pad in pads.values():
        if getattr(pad, "through_hole", False):
            continue
        net = getattr(pad, "net", 0)
        if net == 0:
            continue
        pads_by_net.setdefault(net, []).append(pad)

    nudged = 0
    for route in routes:
        candidates = pads_by_net.get(route.net)
        if not candidates:
            continue
        for via in route.vias:
            for pad in candidates:
                bbox = _router_pad_bbox(pad)
                if not _via_drill_inside_bbox(via, bbox):
                    continue
                # Skip vias that sit DEAD-CENTRE on a pad of the same
                # net.  Such a via is a deliberate in-pad escape: the
                # via centre is the connection to the pad pin, and
                # moving the via off the pad centre would disconnect
                # the chain at the pad anchor (the trace tail meets the
                # via centre, not the pad edge).  This guard preserves
                # the contract enforced by :func:`_via_is_pad_anchored`
                # /  :func:`_nudge_via_with_chain` for the via-via
                # nudge handler.  An OFF-centre via that merely sits
                # *inside* the pad bbox is NOT pad-anchored (the trace
                # already lives outside the pad centre) -- those are
                # the cases the user wants us to repair, and this
                # branch lets them through.
                pad_center_x = (bbox[0] + bbox[2]) / 2.0
                pad_center_y = (bbox[1] + bbox[3]) / 2.0
                if (
                    abs(via.x - pad_center_x) < _PAD_ANCHOR_TOL
                    and abs(via.y - pad_center_y) < _PAD_ANCHOR_TOL
                ):
                    result._bump_skipped("via_pad_centred_escape")
                    break
                if _try_nudge_via_pad(
                    via, bbox, router, via_pad_budget, result=result,
                ):
                    nudged += 1
                # Whether we moved it or not, one pad per via is enough --
                # if budget refused, surface as remaining via-in-pad
                # violation in the DRC report rather than churning
                # through every same-net pad.
                break

    return nudged


def _try_nudge_seg_edge(
    violation: ClearanceViolation,
    router: Autorouter,
    max_displacement: float,
    result: DRCNudgeResult | None = None,
) -> bool:
    """Attempt to repair a trace-to-board-edge clearance violation.

    Issue #2743: ``edge_clearance_trace`` violations were previously
    invisible to the post-route nudge pass.  ``validate_routes`` now
    emits these as ``obstacle_type="edge"`` violations with the closest
    point on the board outline stored in ``location``.

    Repair strategy: slide the segment along the inward-facing direction
    (from outline point toward segment midpoint) by
    ``(required - distance + margin)``, using the chain-aware nudge so
    abutting same-net segments are snapped.  Refuses to nudge pad- or
    via-anchored segments (same guards as the existing handlers).
    """
    deficit = violation.required - violation.distance
    if deficit <= 0:
        return False

    margin = max(0.005, violation.required * 0.10)
    nudge_amount = deficit + margin

    if nudge_amount > max_displacement:
        if result is not None:
            result._bump_skipped("edge_budget")
        return False

    if violation.location is None:
        if result is not None:
            result._bump_skipped("edge_no_location")
        return False

    target_seg = _find_segment(
        router, violation.net, violation.segment_index,
        violation.x1, violation.y1, violation.x2, violation.y2,
        layer=violation.layer,
    )
    if target_seg is None:
        if result is not None:
            result._bump_skipped("edge_segment_not_found")
        return False

    edge_x, edge_y = violation.location

    # We want to slide the segment along the perpendicular to its own
    # axis (so its length is preserved) toward the board interior.  The
    # segment's perpendicular gives two candidate directions; we choose
    # the one whose dot product with (segment_midpoint - closest_edge_pt)
    # is positive (i.e. points away from the edge).
    px, py = _perpendicular_unit(target_seg)
    if px == 0.0 and py == 0.0:
        # Zero-length segment — fall back to away-vector.
        seg_mid_x = (target_seg.x1 + target_seg.x2) / 2
        seg_mid_y = (target_seg.y1 + target_seg.y2) / 2
        away_dx = seg_mid_x - edge_x
        away_dy = seg_mid_y - edge_y
        away_len = math.sqrt(away_dx * away_dx + away_dy * away_dy)
        if away_len < 1e-9:
            if result is not None:
                result._bump_skipped("edge_zero_length_seg")
            return False
        nx = away_dx / away_len
        ny = away_dy / away_len
    else:
        seg_mid_x = (target_seg.x1 + target_seg.x2) / 2
        seg_mid_y = (target_seg.y1 + target_seg.y2) / 2
        # Dot product of (midpoint - edge) with perpendicular: positive
        # means the perpendicular points away from the edge.
        dot = (seg_mid_x - edge_x) * px + (seg_mid_y - edge_y) * py
        if dot < 0:
            nx, ny = -px, -py
        else:
            nx, ny = px, py

    # Issue #3028: pass ``result`` so the foreign-via destination gate
    # can record a structured skip reason if it refuses the nudge.
    # Capture the skip count before the call so we can disambiguate
    # ``foreign_via_blocked`` (already recorded by the chain-aware nudge
    # on its own refusal path) from the legacy pad/via-anchor refusal
    # (which produces no structured reason from the inner helper).
    _fvb_before = result.skipped.get("foreign_via_blocked", 0) if result else 0
    success = _nudge_segment_with_chain(
        target_seg, nx, ny, nudge_amount, router, result=result,
    )
    if not success and result is not None:
        _fvb_after = result.skipped.get("foreign_via_blocked", 0)
        if _fvb_after == _fvb_before:
            # The inner helper did NOT bump foreign_via_blocked, so the
            # refusal was a pad/via anchor (the legacy edge-handler skip).
            result._bump_skipped("edge_seg_anchored")
    return success


def _try_nudge_seg_pad(
    violation: ClearanceViolation,
    router: Autorouter,
    max_displacement: float,
    result: DRCNudgeResult | None = None,
) -> bool:
    """Attempt to nudge a segment away from a pad."""
    deficit = violation.required - violation.distance
    if deficit <= 0:
        return False

    margin = max(0.005, violation.required * 0.10)
    nudge_amount = deficit + margin

    if nudge_amount > max_displacement:
        return False

    target_seg = _find_segment(
        router, violation.net, violation.segment_index,
        violation.x1, violation.y1, violation.x2, violation.y2,
        layer=violation.layer,
    )
    if target_seg is None:
        return False

    if violation.location is None:
        return False

    pad_x, pad_y = violation.location
    seg_mid_x = (target_seg.x1 + target_seg.x2) / 2
    seg_mid_y = (target_seg.y1 + target_seg.y2) / 2

    away_dx = seg_mid_x - pad_x
    away_dy = seg_mid_y - pad_y
    away_len = math.sqrt(away_dx * away_dx + away_dy * away_dy)
    if away_len < 1e-9:
        nx, ny = _perpendicular_unit(target_seg)
    else:
        nx = away_dx / away_len
        ny = away_dy / away_len

    # Issue #2475: Use chain-aware nudge so we don't break the routed
    # chain by translating a single segment in isolation.  The chain-aware
    # variant also refuses to nudge pad-anchored segments outright.
    # Issue #3028: pass ``result`` for the foreign-via destination gate.
    return _nudge_segment_with_chain(
        target_seg, nx, ny, nudge_amount, router, result=result,
    )


# ---------------------------------------------------------------------------
# Segment lookup helper
# ---------------------------------------------------------------------------

def _find_segment(
    router: Autorouter,
    net: int,
    seg_index: int,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    tol: float = 0.001,
    layer: "Layer | None" = None,
) -> Segment | None:
    """Locate a segment in *router.routes* by net + coordinates + optional layer.

    We first try the indexed lookup (net + seg_index), then fall back to a
    coordinate search across all segments of matching nets.  When *layer* is
    provided (non-None), only segments on that layer are considered.  This
    prevents inner-layer segments from being confused with outer-layer
    segments that share similar coordinates (Issue #1798).
    """
    def _coords_match(seg: Segment) -> bool:
        return (
            abs(seg.x1 - x1) < tol
            and abs(seg.y1 - y1) < tol
            and abs(seg.x2 - x2) < tol
            and abs(seg.y2 - y2) < tol
        )

    def _layer_match(seg: Segment) -> bool:
        return layer is None or seg.layer == layer

    for route in router.routes:
        if route.net != net:
            continue
        # Try index lookup
        if 0 <= seg_index < len(route.segments):
            seg = route.segments[seg_index]
            if _coords_match(seg) and _layer_match(seg):
                return seg
        # Fallback: coordinate search
        for seg in route.segments:
            if _coords_match(seg) and _layer_match(seg):
                return seg
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def drc_verify_and_nudge(
    router: Autorouter,
    *,
    max_displacement: float = 0.2,
    max_passes: int = 3,
) -> DRCNudgeResult:
    """Run post-optimization DRC verification and repair.

    This function should be called **after** ``TraceOptimizer`` and
    **before** ``to_sexp()`` / save.

    Args:
        router: Autorouter instance with optimized routes.
        max_displacement: Maximum segment displacement budget in mm.
            Defaults to 0.2 (the upper end of the 0.1-0.2 range
            recommended by the existing ClearanceRepairer precedent).
        max_passes: Maximum iterative passes. Stops early when no
            violations remain or no progress is made.

    Returns:
        :class:`DRCNudgeResult` with statistics.

    Issue #3507: this pass mutates segment/via geometry IN PLACE on the
    live Route objects without re-marking the routing grid.  A geometry
    snapshot of every route is taken at entry and, on every exit path,
    :meth:`RoutingGrid.resync_route_occupancy` re-derives the grid
    occupancy from the post-nudge geometry so downstream grid consumers
    (targeted repair re-routes, future nets in multi-pass flows) see the
    true copper state.  The pass's OWN checks (``validate_routes`` and
    the nudge gating helpers) are world-coordinate geometric and do not
    consult the grid, so a single resync at exit is sufficient.
    """
    # Issue #3507: snapshot pre-mutation geometry for the exit resync.
    # Defensive getattr: unit tests drive this pass with stub routers
    # that carry routes but no grid -- the resync is then a no-op.
    _grid = getattr(router, "grid", None)
    _grid_snapshot = (
        [(r.copy_geometry(), r) for r in router.routes] if _grid is not None else []
    )
    try:
        return _drc_verify_and_nudge_impl(
            router, max_displacement=max_displacement, max_passes=max_passes
        )
    finally:
        if _grid is not None:
            _grid.resync_route_occupancy(_grid_snapshot)


def _drc_verify_and_nudge_impl(
    router: Autorouter,
    *,
    max_displacement: float,
    max_passes: int,
) -> DRCNudgeResult:
    """Body of :func:`drc_verify_and_nudge` (wrapped for the #3507 grid resync)."""
    result = DRCNudgeResult()

    # Phase 0: Merge coincident same-net vias (cheap, reduces noise).
    result.vias_merged = _merge_same_net_vias(router)

    # Phase 0b (Issue #3112): same-net via-in-pad sweep.  The
    # ``validate_routes`` stream skips same-net pads
    # (``router/io.py`` ``Skip pads on the same net``), so this case
    # never appears in the ``ClearanceViolation`` list.  We run an
    # explicit pad-vs-via scan here, gated on
    # ``manufacturer.via_in_pad_supported``.
    in_pad_nudged = _scan_and_repair_via_in_pad(router, max_displacement, result)
    result.vias_nudged += in_pad_nudged

    # Detect initial violations.
    violations = validate_routes(router)
    # Only consider actionable (non-component-inherent) violations.
    actionable = [v for v in violations if not v.component_inherent]
    result.initial_violations = len(actionable)

    if not actionable:
        result.passes_run = 0
        return result

    prev_count = len(actionable)

    for pass_idx in range(max_passes):
        result.passes_run = pass_idx + 1
        nudged_this_pass = 0

        for v in actionable:
            success = False
            if v.obstacle_type == "segment":
                # Issue #3028: plumb ``result`` so the foreign-via gate
                # in ``_nudge_segment_with_chain`` can record the skip.
                success = _try_nudge_seg_seg(
                    v, router, max_displacement, result=result,
                )
            elif v.obstacle_type == "via":
                # Issue #2743: ``segment_index == -1`` marks a via-vs-via
                # violation (zero-length "segment" at via_a's centre).
                # The seg-vs-via handler would call ``_find_segment`` with
                # an unmatchable shape and silently fail.  Dispatch to
                # the via-via handler instead.
                if v.segment_index == -1:
                    success = _try_nudge_via_via(
                        v, router, max_displacement, result=result
                    )
                else:
                    # Issue #3028: same as seg-seg above.
                    success = _try_nudge_seg_via(
                        v, router, max_displacement, result=result,
                    )
            elif v.obstacle_type == "pad":
                success = _try_nudge_seg_pad(
                    v, router, max_displacement, result=result,
                )
            elif v.obstacle_type == "edge":
                # Issue #2743: trace-vs-board-edge violations now flow
                # through the same dispatch path.
                success = _try_nudge_seg_edge(
                    v, router, max_displacement, result=result
                )
            else:
                # Unknown obstacle type — record a structured skip so
                # the user sees "0/1 resolved; 1 unsupported" instead of
                # an opaque no-op.
                result._bump_skipped(f"unsupported_obstacle:{v.obstacle_type}")

            if success:
                nudged_this_pass += 1

        result.segments_nudged += nudged_this_pass

        # Re-validate after nudges.
        violations = validate_routes(router)
        actionable = [v for v in violations if not v.component_inherent]
        current_count = len(actionable)

        if current_count == 0:
            break

        if current_count >= prev_count:
            # No progress -- stop iterating.
            break

        prev_count = current_count

    result.remaining_violations = len(actionable)
    return result
