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
from .primitives import Route, Segment, Via

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
    passes_run: int = 0

    def summary(self) -> str:
        """Return a human-readable summary."""
        resolved = self.initial_violations - self.remaining_violations
        lines = [
            f"DRC nudge: {resolved}/{self.initial_violations} violations resolved "
            f"in {self.passes_run} pass(es)",
        ]
        if self.segments_nudged:
            lines.append(f"  Segments nudged: {self.segments_nudged}")
        if self.vias_merged:
            lines.append(f"  Same-net vias merged: {self.vias_merged}")
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

    Args:
        seg: The segment to translate.
        nx, ny: Unit vector for the translation direction.
        amount: Translation distance (mm).
        router: Autorouter providing access to all routes and pads.
        chain_tol: Tolerance for matching adjacent segment endpoints.

    Returns:
        True if the segment was successfully nudged and the chain repaired;
        False if the segment was left untouched (e.g. pad-anchored).
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

    # Capture pre-nudge endpoints so we can update neighbour segments.
    old_x1, old_y1 = seg.x1, seg.y1
    old_x2, old_y2 = seg.x2, seg.y2

    # Apply the translation to seg itself.
    _nudge_segment(seg, nx, ny, amount)

    new_x1, new_y1 = seg.x1, seg.y1
    new_x2, new_y2 = seg.x2, seg.y2

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


# Tolerance in mm for considering a segment endpoint anchored to a pad
# centre.  Routed segments terminate at pad centres exactly (within float
# rounding), so a small tolerance here suffices.  This is intentionally
# tighter than ``_ENDPOINT_TOL`` (0.05 mm) because we only want to detect
# *actual* pad anchors, not nearby segment intersections.
_PAD_ANCHOR_TOL = 0.02


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
    return _nudge_segment_with_chain(target_seg, perp_x, perp_y, nudge_amount, router)


def _try_nudge_seg_via(
    violation: ClearanceViolation,
    router: Autorouter,
    max_displacement: float,
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
    return _nudge_segment_with_chain(target_seg, nx, ny, nudge_amount, router)


def _try_nudge_seg_pad(
    violation: ClearanceViolation,
    router: Autorouter,
    max_displacement: float,
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
    return _nudge_segment_with_chain(target_seg, nx, ny, nudge_amount, router)


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
    """
    result = DRCNudgeResult()

    # Phase 0: Merge coincident same-net vias (cheap, reduces noise).
    result.vias_merged = _merge_same_net_vias(router)

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
                success = _try_nudge_seg_seg(v, router, max_displacement)
            elif v.obstacle_type == "via":
                success = _try_nudge_seg_via(v, router, max_displacement)
            elif v.obstacle_type == "pad":
                success = _try_nudge_seg_pad(v, router, max_displacement)

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
