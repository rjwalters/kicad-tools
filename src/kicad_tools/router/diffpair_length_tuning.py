"""Per-pair differential-pair length-match (skew) tuner.

Issue #2648, Epic #2556 Phase 3I.

This module implements :func:`tune_diff_pair_skew`, a router-internal
helper that inserts serpentines (trombones) on the *shorter* half of a
detected differential pair until the pair's skew is within the per-class
:meth:`~kicad_tools.router.rules.NetClassRouting.effective_skew_tolerance`
window, OR a cascade-safety budget of three insertion attempts is
exhausted.

Design notes
============

* **Outer-normal bulges only.**  Bulging the shorter half *toward* the
  partner trace would consume intra-pair coupling room and trigger an
  intra-pair clearance violation immediately.  The tuner therefore
  computes the partner trace's bearing at the chosen insertion segment,
  derives a unit outer-normal vector, and passes it to the trombone
  generator via the new :attr:`SerpentineConfig.outer_normal_hint`
  field (``side="outer"``).  See the
  :func:`kicad_tools.router.optimizer.serpentine.SerpentineGenerator.generate_trombone`
  implementation for how the hint is consumed.

* **Per-insertion DRC self-check.**  The first call in the chain has no
  collision-checking safety net -- :func:`add_serpentine` carries a
  literal ``TODO`` for that.  This module supplies the safety net via
  :func:`_post_insertion_clearance_ok`, which iterates the new
  serpentine segments against every other route's segments and rejects
  the insertion if any pair drops below the configured intra-pair
  clearance threshold.  On rejection the tuner discards the proposed
  ``new_route`` and returns the **original** ``route`` reference (and
  its original ``.segments`` list reference) -- the byte-for-byte
  rollback contract.

* **Cascade-safety budget N=3.**  A single trombone insertion can fall
  short of the target when the chosen segment is too crowded for enough
  amplitude; the tuner therefore re-evaluates and tries again, up to a
  bounded number of attempts.  Without this budget a near-zero-skew but
  unreachable target (a tight via cluster) could loop forever.

* **The longer half is never mutated.**  ``tune_diff_pair_skew`` only
  touches the shorter route.  The drift-prevention regression test
  asserts ``is``-identity on both the longer ``Route`` object and its
  ``.segments`` list.

Out of scope for Phase 3I:

* Multi-segment trombone splitting (very tight boards).
* Differential-pair coupling preservation during the bulge (today the
  bulge is on only the shorter half; the longer half is unchanged, so
  coupling is necessarily broken within the bulge.  Phase 3K's
  impedance-controlled rebuild is the place to restore coupling).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .optimizer.geometry import segment_length
from .optimizer.serpentine import (
    SerpentineConfig,
    SerpentineGenerator,
    SerpentineResult,
)

if TYPE_CHECKING:
    from .diffpair_detection import DetectedPair
    from .primitives import Route, Segment


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class DiffPairTuneResult:
    """Outcome of a per-pair length-match tuning attempt.

    Attributes:
        success: True if the pair's skew is now within ``tolerance_mm`` AND
            the modifications passed the post-insertion DRC self-check at
            every attempt.  False if either no progress could be made
            (no insertable segment, geometry too tight) or every attempt
            failed the self-check.
        reason: Short machine-readable reason code.  One of:
            * ``"already_within_tolerance"`` -- the pair was already
              acceptable; no change was made.
            * ``"tuned"`` -- one or more trombones brought the pair into
              tolerance.
            * ``"exceeded_max_inserts"`` -- the cascade budget was
              exhausted before reaching tolerance.
            * ``"post_insertion_drc_violation"`` -- a candidate trombone
              would violate intra-pair (or neighbor) clearance; the
              insertion was rolled back and no further attempts were
              made on this pair.
            * ``"no_suitable_segment"`` -- the shorter route has no
              segment long enough to host any trombone amplitude.
            * ``"unrouted"`` -- one or both halves were not in
              ``routes_by_net``.
        attempts: Number of trombone insertions actually attempted.
        inserts_applied: Number of trombones whose post-insertion check
            passed and were committed.
        skew_before_mm: Pair skew (``|L_p - L_n|``) at entry.
        skew_after_mm: Pair skew after the (possibly empty) sequence of
            successful insertions.
        message: Human-readable summary.
    """

    success: bool = False
    reason: str = ""
    attempts: int = 0
    inserts_applied: int = 0
    skew_before_mm: float = 0.0
    skew_after_mm: float = 0.0
    message: str = ""
    # The trombone results, kept for diagnostic / test inspection.  An
    # entry is present for every attempt (including rejected ones).
    serpentine_results: list[SerpentineResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


# Cascade-safety budget: cap the number of trombone insertions per pair.
# Three is enough to bring most realistic skews into tolerance at a 1 mm
# default amplitude, while keeping the worst case bounded for unreachable
# targets (very large skew on a congested board).
MAX_INSERTS_PER_PAIR: int = 3


def tune_diff_pair_skew(
    pair: DetectedPair,
    routes_by_net: dict[int, Route],
    *,
    tolerance_mm: float,
    intra_pair_clearance_mm: float,
    config: SerpentineConfig | None = None,
    max_inserts: int = MAX_INSERTS_PER_PAIR,
) -> tuple[Route, Route, DiffPairTuneResult]:
    """Tune the skew of a detected diff pair by serpentining the shorter half.

    Args:
        pair: A detected differential pair (positive + negative).
        routes_by_net: ``{net_id: Route}`` lookup for all routed nets on the
            board.  Used both to fetch the pair's two halves AND as the
            corpus against which the post-insertion DRC self-check looks
            for newly-introduced clearance violations.
        tolerance_mm: The per-class skew tolerance from
            :meth:`NetClassRouting.effective_skew_tolerance`.  Pair is
            considered "matched" when ``|L_p - L_n| <= tolerance_mm``.
        intra_pair_clearance_mm: The per-pair intra clearance threshold
            from :meth:`NetClassRouting.effective_intra_pair_clearance`.
            Used by the post-insertion DRC self-check to verify the new
            segments do not violate the pair's own coupling rule.
        config: Optional :class:`SerpentineConfig` override.  When supplied
            its ``side`` and ``outer_normal_hint`` fields are overwritten by
            the tuner; other fields (amplitude, gap_factor, ...) are honored.
        max_inserts: Cascade-safety budget (default :data:`MAX_INSERTS_PER_PAIR`
            = 3).  The tuner stops after this many trombone attempts even
            if the pair remains out of tolerance.

    Returns:
        ``(p_route, n_route, result)`` where ``p_route`` and ``n_route`` are
        the (possibly tuned) Route references for the positive and negative
        halves.  The half that was *not* lengthened is returned **by
        reference** -- the same object the caller passed in
        ``routes_by_net`` -- with the same ``.segments`` list object.  If
        rollback fired, BOTH halves are returned by reference.

    Rollback contract:
        When the post-insertion DRC self-check fails, the proposed new
        Route is discarded and the original Route reference (the one in
        ``routes_by_net``) is returned with its original ``.segments``
        list object intact.  The test ``test_drift_prevention`` asserts
        this contract via ``is`` identity on both the longer route and
        its segments list.
    """
    p_id = pair.pair.positive.net_id
    n_id = pair.pair.negative.net_id

    p_route = routes_by_net.get(p_id)
    n_route = routes_by_net.get(n_id)
    if p_route is None or n_route is None:
        result = DiffPairTuneResult(
            success=False,
            reason="unrouted",
            message=(
                f"Pair {pair.pair.positive.net_name}/{pair.pair.negative.net_name} "
                "has an unrouted half; nothing to tune."
            ),
        )
        return (
            p_route if p_route is not None else _empty_route(p_id, pair.pair.positive.net_name),
            n_route if n_route is not None else _empty_route(n_id, pair.pair.negative.net_name),
            result,
        )

    from .length import LengthTracker  # avoid cycle

    l_p = LengthTracker.calculate_route_length(p_route)
    l_n = LengthTracker.calculate_route_length(n_route)
    skew = abs(l_p - l_n)

    # Already within tolerance -- byte-for-byte unchanged.
    if skew <= tolerance_mm:
        return p_route, n_route, DiffPairTuneResult(
            success=True,
            reason="already_within_tolerance",
            skew_before_mm=skew,
            skew_after_mm=skew,
            message=f"Pair already matched: skew={skew:.4f}mm <= tol={tolerance_mm:.4f}mm",
        )

    # The shorter half gets the trombone; the longer half is the target
    # length and is NEVER touched.
    if l_p < l_n:
        shorter_id, shorter_route, longer_id, longer_route = p_id, p_route, n_id, n_route
        longer_is_p = False
    else:
        shorter_id, shorter_route, longer_id, longer_route = n_id, n_route, p_id, p_route
        longer_is_p = True
    target_length = max(l_p, l_n)
    original_longer_segments = longer_route.segments  # for rollback assertion

    # Snapshot the original shorter route -- we may need to roll back.
    original_shorter_route = shorter_route
    original_shorter_segments = shorter_route.segments

    base_config = config or SerpentineConfig()

    result = DiffPairTuneResult(skew_before_mm=skew)
    current_shorter = shorter_route
    current_skew = skew

    # Cascade loop.  Each iteration: compute outer normal, attempt one
    # trombone, run the self-check, commit or reject.
    for attempt in range(max_inserts):
        result.attempts = attempt + 1

        # Pick the insertion segment first (shared by both the trombone
        # generator and the outer-normal calculation).  This mirrors the
        # logic in SerpentineGenerator.find_best_segment so the hint is
        # computed for the same segment that the generator will use.
        generator = SerpentineGenerator(base_config)  # provisional
        best = generator.find_best_segment(current_shorter)
        if best is None:
            result.reason = "no_suitable_segment"
            result.message = (
                f"No segment long enough for a trombone on net {shorter_id} "
                f"(skew={current_skew:.4f}mm > tol={tolerance_mm:.4f}mm)"
            )
            current_shorter = original_shorter_route
            break
        seg_idx, insertion_segment = best

        # Compute the outer-normal hint: the half-plane *away* from the
        # partner trace at the insertion segment's midpoint.
        hint = _outer_normal_hint(insertion_segment, longer_route)

        # Build the per-attempt config with side="outer" + the hint.
        attempt_config = SerpentineConfig(
            style=base_config.style,
            amplitude=base_config.amplitude,
            min_spacing=base_config.min_spacing,
            min_segment_length=base_config.min_segment_length,
            gap_factor=base_config.gap_factor,
            max_iterations=base_config.max_iterations,
            side="outer",
            outer_normal_hint=hint,
        )
        attempt_generator = SerpentineGenerator(attempt_config)

        candidate_route, serp_result = attempt_generator.add_serpentine(
            current_shorter, target_length
        )
        result.serpentine_results.append(serp_result)

        if not serp_result.success:
            # The trombone generator itself refused (segment too short,
            # etc.).  Treat the same as no_suitable_segment.
            result.reason = "no_suitable_segment"
            result.message = (
                f"Trombone generation failed on net {shorter_id}: "
                f"{serp_result.message}"
            )
            current_shorter = original_shorter_route
            break

        # Post-insertion DRC self-check.  The candidate route's new
        # segments must not violate intra-pair clearance against the
        # partner OR collide with any other routed net's segments.
        # ``serp_result.new_segments`` contains *only* the trombone
        # segments (entry + loops + exit) -- those are the segments to
        # check.
        if not _post_insertion_clearance_ok(
            new_segments=serp_result.new_segments,
            shorter_net_id=shorter_id,
            longer_net_id=longer_id,
            routes_by_net=routes_by_net,
            intra_pair_clearance_mm=intra_pair_clearance_mm,
        ):
            # Rollback: discard the candidate, return the ORIGINAL shorter
            # route (and the original longer route reference).
            result.reason = "post_insertion_drc_violation"
            result.skew_after_mm = current_skew
            result.message = (
                f"Post-insertion DRC self-check failed on net {shorter_id} "
                f"(intra={intra_pair_clearance_mm:.4f}mm); rolled back."
            )
            # The drift-prevention contract:
            current_shorter = original_shorter_route
            assert current_shorter is original_shorter_route
            assert current_shorter.segments is original_shorter_segments
            break

        # Commit this attempt's new route and re-measure skew.
        current_shorter = candidate_route
        result.inserts_applied += 1
        new_shorter_length = LengthTracker.calculate_route_length(current_shorter)
        current_skew = abs(new_shorter_length - target_length) if longer_is_p else abs(
            target_length - new_shorter_length
        )
        # Both branches reduce to abs(target_length - new_shorter_length)
        current_skew = abs(target_length - new_shorter_length)

        if current_skew <= tolerance_mm:
            result.success = True
            result.reason = "tuned"
            break
    else:
        # for/else: completed max_inserts without break (no success path).
        result.reason = result.reason or "exceeded_max_inserts"

    if result.reason == "" or result.reason == "exceeded_max_inserts":
        result.message = result.message or (
            f"Cascade budget exhausted (attempts={result.attempts}, "
            f"inserts_applied={result.inserts_applied}, "
            f"skew={current_skew:.4f}mm vs tol={tolerance_mm:.4f}mm)"
        )

    result.skew_after_mm = (
        abs(target_length - LengthTracker.calculate_route_length(current_shorter))
        if result.inserts_applied > 0
        else skew
    )

    # Assemble return values, restoring P/N polarity ordering.  The
    # longer route is ALWAYS returned by the same reference the caller
    # passed in -- the drift-prevention regression test asserts on this.
    if longer_is_p:
        # longer = P, shorter = N
        assert longer_route is p_route
        assert longer_route.segments is original_longer_segments
        return p_route, current_shorter, result
    else:
        # longer = N, shorter = P
        assert longer_route is n_route
        assert longer_route.segments is original_longer_segments
        return current_shorter, n_route, result


# ---------------------------------------------------------------------------
# Outer-normal calculation
# ---------------------------------------------------------------------------


def _outer_normal_hint(
    segment: Segment,
    partner_route: Route,
) -> tuple[float, float]:
    """Return a unit vector pointing AWAY from the partner trace at ``segment``.

    The hint is computed at the midpoint of ``segment``: we find the
    closest point on the partner route, then return the unit vector from
    that closest point to the midpoint (i.e. AWAY from the partner).
    This guarantees the trombone will bulge into the outer half-plane
    of the pair, never toward the partner.

    Args:
        segment: The segment selected for trombone insertion on the
            shorter half.
        partner_route: The longer half (the partner trace).

    Returns:
        A unit vector ``(rx, ry)`` in the segment's layer plane.  When
        the partner has no segments (unusual but possible -- empty
        ``Route``), the segment's own geometric perpendicular is
        returned as a fallback so the trombone is still single-sided.
    """
    import math

    # Segment midpoint.
    mx = (segment.x1 + segment.x2) / 2.0
    my = (segment.y1 + segment.y2) / 2.0

    if not partner_route.segments:
        # Fallback: use the segment's own geometric perpendicular.  This
        # mirrors what the trombone would do without a hint, but the
        # caller still gets a single-sided bulge (``side="outer"``).
        dx = segment.x2 - segment.x1
        dy = segment.y2 - segment.y1
        length = math.sqrt(dx * dx + dy * dy)
        if length == 0.0:
            return (0.0, 1.0)
        return (-dy / length, dx / length)

    # Find the closest point on the partner route to the midpoint.
    best_dist_sq = float("inf")
    best_cx, best_cy = 0.0, 0.0
    for pseg in partner_route.segments:
        cx, cy = _closest_point_on_segment(mx, my, pseg.x1, pseg.y1, pseg.x2, pseg.y2)
        d2 = (cx - mx) ** 2 + (cy - my) ** 2
        if d2 < best_dist_sq:
            best_dist_sq = d2
            best_cx, best_cy = cx, cy

    # Unit vector from closest partner point to segment midpoint.
    rx = mx - best_cx
    ry = my - best_cy
    mag = math.sqrt(rx * rx + ry * ry)
    if mag == 0.0:
        # The partner crosses the midpoint exactly; pick the segment's
        # own perpendicular as a fallback.
        dx = segment.x2 - segment.x1
        dy = segment.y2 - segment.y1
        length = math.sqrt(dx * dx + dy * dy)
        if length == 0.0:
            return (0.0, 1.0)
        return (-dy / length, dx / length)
    return (rx / mag, ry / mag)


def _closest_point_on_segment(
    px: float,
    py: float,
    sx1: float,
    sy1: float,
    sx2: float,
    sy2: float,
) -> tuple[float, float]:
    """Return the point on segment ``(sx1,sy1)->(sx2,sy2)`` closest to ``(px,py)``."""
    dx = sx2 - sx1
    dy = sy2 - sy1
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0.0:
        return (sx1, sy1)
    t = ((px - sx1) * dx + (py - sy1) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))
    return (sx1 + t * dx, sy1 + t * dy)


# ---------------------------------------------------------------------------
# Post-insertion DRC self-check
# ---------------------------------------------------------------------------


def _post_insertion_clearance_ok(
    *,
    new_segments: list[Segment],
    shorter_net_id: int,
    longer_net_id: int,
    routes_by_net: dict[int, Route],
    intra_pair_clearance_mm: float,
) -> bool:
    """Return True if the proposed serpentine segments are DRC-safe.

    The check is two-pronged:

    1. Intra-pair clearance: every new segment is checked against every
       segment of the partner trace (``longer_net_id``).  Threshold is
       ``intra_pair_clearance_mm`` (the per-pair value the router used,
       passed in explicitly -- the curator's "don't go through
       check_diffpair_clearance_intra" guidance is satisfied because we
       use the same threshold without rebuilding the per-pair map).

    2. Inter-net clearance: every new segment is checked against every
       segment of every *other* routed net (excluding the shorter half
       itself, since the new segments will replace the original
       segment).  Threshold is ``intra_pair_clearance_mm`` as a
       conservative floor -- the architect's spec uses the same value
       so that bulging into a neighbor is rejected at least as
       aggressively as bulging into the partner.

    Args:
        new_segments: The trombone segments produced by
            :meth:`SerpentineGenerator.generate_trombone`.
        shorter_net_id: Net id of the trace receiving the trombone.
        longer_net_id: Net id of the partner trace.
        routes_by_net: ``{net_id: Route}`` lookup for all routed nets.
        intra_pair_clearance_mm: Edge-to-edge clearance floor in mm.

    Returns:
        ``True`` if no clearance violation is introduced; ``False``
        otherwise (the caller must roll back).
    """
    from kicad_tools.core.geometry import segment_clearance

    # Pair-internal check.
    partner = routes_by_net.get(longer_net_id)
    if partner is not None:
        for new_seg in new_segments:
            for pseg in partner.segments:
                if pseg.layer != new_seg.layer:
                    continue
                clearance = segment_clearance(
                    new_seg.x1,
                    new_seg.y1,
                    new_seg.x2,
                    new_seg.y2,
                    new_seg.width,
                    pseg.x1,
                    pseg.y1,
                    pseg.x2,
                    pseg.y2,
                    pseg.width,
                )
                if clearance + 1e-9 < intra_pair_clearance_mm:
                    return False

    # Neighbor check (all other nets).
    for other_net_id, other_route in routes_by_net.items():
        if other_net_id == shorter_net_id or other_net_id == longer_net_id:
            continue
        for new_seg in new_segments:
            for oseg in other_route.segments:
                if oseg.layer != new_seg.layer:
                    continue
                clearance = segment_clearance(
                    new_seg.x1,
                    new_seg.y1,
                    new_seg.x2,
                    new_seg.y2,
                    new_seg.width,
                    oseg.x1,
                    oseg.y1,
                    oseg.x2,
                    oseg.y2,
                    oseg.width,
                )
                if clearance + 1e-9 < intra_pair_clearance_mm:
                    return False

    return True


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _empty_route(net_id: int, net_name: str) -> Route:
    """Return an empty :class:`Route` shell for an unrouted net.

    Used as a placeholder in the return value of
    :func:`tune_diff_pair_skew` when one half of the pair is unrouted --
    the caller can detect the situation via ``result.reason == "unrouted"``
    and the empty ``.segments`` list.
    """
    from .primitives import Route

    return Route(net=net_id, net_name=net_name, segments=[], vias=[])


# Re-export for ``from kicad_tools.router.diffpair_length_tuning import *``.
__all__ = [
    "MAX_INSERTS_PER_PAIR",
    "DiffPairTuneResult",
    "tune_diff_pair_skew",
]


# Reference suppression for static linters: segment_length is used as a
# debugging aid in interactive exploration and re-imported for callers
# (Phase 3I tests import it from this module to avoid two import paths).
_ = segment_length  # noqa: F401
