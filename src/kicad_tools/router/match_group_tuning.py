"""N-trace match-group length-skew (serpentine) tuner.

Issue #2700, Epic #2661 Phase 2E.

This module implements :func:`tune_match_group_v2`, a router-internal
helper that inserts serpentines (trombones) on the *shorter* members of a
detected N-trace match group (DDR data byte, MIPI lane group, generic
parallel bus) until the group's skew is within the per-group
:attr:`~kicad_tools.router.match_group_length.MatchGroup.tolerance`
window, OR a cascade-safety budget is exhausted.

It is the direct generalization from N=2 (a differential pair, handled by
:mod:`~kicad_tools.router.diffpair_length_tuning`) to N>=3 of the
outer-normal bulges + per-insertion DRC self-check + byte-for-byte
rollback pattern landed for pairs in PR #2663 (Epic #2556 Phase 3I).

Design notes
============

* **Outer-normal bulges only, generalized to N-trace.**  For each
  candidate insertion segment on a non-reference member we compute the
  *nearest other group member's* closest point at the segment midpoint,
  then return the unit vector AWAY from that nearest neighbor (the
  curator's "Option (a): nearest-other-trace" heuristic).  Bulging the
  shorter member toward another group member would consume intra-group
  coupling room and trigger an intra-group clearance violation
  immediately.  See :func:`_outer_normal_hint_group`.

* **Per-insertion DRC self-check.**  Two-pronged:

  1. **Intra-group** -- every new serpentine segment is checked against
     every segment of every *other* group member at threshold
     ``intra_group_clearance_mm``.
  2. **Inter-net** -- every new segment is checked against every segment
     of every routed net that is NOT a group member at the same
     threshold.

  See :func:`_post_insertion_clearance_ok_group`.  On rejection the
  tuner discards the proposed ``new_route`` and returns the **original**
  ``route`` reference (and its original ``.segments`` list reference) --
  the byte-for-byte rollback contract.

* **Cascade-safety budget**.  For groups with ``len(net_ids) <= 4`` the
  budget per member is :data:`MAX_INSERTS_PER_GROUP_MEMBER_SMALL`
  (3, matching :data:`~kicad_tools.router.diffpair_length_tuning.MAX_INSERTS_PER_PAIR`);
  for larger groups (DDR bytes, MIPI lane groups) the budget drops to
  :data:`MAX_INSERTS_PER_GROUP_MEMBER_LARGE` (2) because the cumulative
  geometric perturbation against neighboring nets grows linearly with N.
  An absolute ceiling :data:`MAX_TOTAL_INSERTS_PER_GROUP` (16) is
  enforced across all members so a worst-case DDR-byte (N=10 x 2 = 20
  cumulative bulges) cannot saturate the per-insertion DRC check on
  dense boards.

* **Reference-net policy.**  Delegates to
  :meth:`MatchGroupTracker.get_reference_length`: ``None`` ->
  longest-in-group (legacy ``tune_match_group`` semantic); explicit net
  id -> "pace-car" semantic.  When a group member is already *longer*
  than the reference the tuner cannot remove length (we never cut), so
  that member is left unchanged with ``reason="longer_than_reference"``
  -- distinct from the pair tuner's ``"reference"`` reason because for
  N>=3 the over-length case is genuinely different from the
  "you are the reference" case.

* **Phase 2F handoff.**  ``MatchGroup.pair_ids`` is reserved for the
  group-of-pairs composition (Phase 2F) where both halves of a pair must
  receive identical serpentine geometry.  Phase 2E treats every member
  as an independent net -- the entry guard
  ``assert not group.pair_ids`` ensures that a future Phase 2F caller
  cannot silently fall through to per-net serpentines (which would break
  within-pair coupling).

Out of scope for Phase 2E:

* Group-of-pairs symmetric serpentine (Phase 2F, Issue #2701).
* Pipeline wiring -- ``Autorouter._finalize_routing`` calling
  :func:`tune_match_group_v2` is a separate Phase 2.5 wiring issue.
* CLI flag -- ``--length-match-groups`` (Phase 3H of this epic) is a
  separate issue.
* Modifying the legacy :func:`~kicad_tools.router.optimizer.serpentine.tune_match_group`
  at ``serpentine.py:484``.  Kept for backward compat with
  :func:`~kicad_tools.router.length_tuning.apply_length_tuning`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .diffpair_length_tuning import (
    MAX_INSERTS_PER_PAIR,
    _closest_point_on_segment,
)
from .optimizer.serpentine import (
    SerpentineConfig,
    SerpentineGenerator,
    SerpentineResult,
)

if TYPE_CHECKING:
    from .match_group_length import MatchGroup
    from .primitives import Route, Segment


# ---------------------------------------------------------------------------
# Cascade-safety budget constants
# ---------------------------------------------------------------------------


#: Cascade budget per group member for small groups (``len(net_ids) <= 4``).
#: Equal to :data:`MAX_INSERTS_PER_PAIR` (3) by design -- the small-group
#: case generalizes the N=2 pair budget without tightening it.  A drift-
#: prevention test asserts the byte-for-byte equality so a future change
#: touching one without the other fires.
MAX_INSERTS_PER_GROUP_MEMBER_SMALL: int = 3

#: Cascade budget per group member for large groups (``len(net_ids) > 4``).
#: Reduced to 2 because the cumulative geometric perturbation against
#: neighboring nets grows linearly with N -- a DDR byte (N=10) at the
#: small-group budget would attempt up to 30 cumulative serpentine
#: insertions, well past what the per-insertion DRC self-check can
#: absorb on a dense board.
MAX_INSERTS_PER_GROUP_MEMBER_LARGE: int = 2

#: Absolute ceiling on cumulative serpentine insertions per group, across
#: all members.  At the LARGE budget (2) a 10-net DDR byte could still
#: produce 20 cumulative bulges -- 16 is a conservative safety net that
#: lets a typical group reach tolerance while bounding the worst case.
#: Defended by a drift-prevention test:
#: ``MAX_TOTAL_INSERTS_PER_GROUP >= 2 * MAX_INSERTS_PER_GROUP_MEMBER_LARGE``.
MAX_TOTAL_INSERTS_PER_GROUP: int = 16


# Backwards-friendly alias for the "default per-member budget" used in
# the public API signature.  Set to the small-group default; the function
# downshifts to LARGE automatically when ``len(group.net_ids) > 4``.
MAX_INSERTS_PER_GROUP_MEMBER: int = MAX_INSERTS_PER_GROUP_MEMBER_SMALL


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class TuneResult:
    """Outcome of a per-member length-match tuning attempt.

    Per-member analog of
    :class:`~kicad_tools.router.diffpair_length_tuning.DiffPairTuneResult`.
    One ``TuneResult`` is returned for every member of a group from
    :func:`tune_match_group_v2`, keyed by net id in the returned
    ``dict[int, tuple[Route, TuneResult]]``.

    Attributes:
        success: True if this member is now within ``tolerance_mm`` of
            the reference length AND every committed insertion passed
            the post-insertion DRC self-check.  False otherwise.
        reason: Short machine-readable reason code.  One of:

            * ``"already_within_tolerance"`` -- this member was already
              within tolerance vs the reference; no change was made.
            * ``"tuned"`` -- one or more trombones brought the member
              into tolerance.
            * ``"reference"`` -- this member IS the reference net (per
              the explicit-net policy).  Always returned unchanged.
            * ``"longer_than_reference"`` -- this member is *longer*
              than the reference length and the tuner cannot remove
              length.  Distinct from ``"reference"``; useful for the
              "pace-car" policy where one member is intentionally
              shorter than another that the bus must be matched to.
            * ``"exceeded_max_inserts"`` -- the per-member cascade
              budget was exhausted before reaching tolerance.
            * ``"cascade_budget_exhausted"`` -- the group-level
              cumulative ceiling (:data:`MAX_TOTAL_INSERTS_PER_GROUP`)
              was hit; this member could not be tuned because the
              tuner refused to attempt any more insertions on the
              group as a whole.
            * ``"post_insertion_drc_violation"`` -- a candidate trombone
              would violate intra-group or neighbor clearance; the
              insertion was rolled back and no further attempts were
              made on this member.
            * ``"no_suitable_segment"`` -- the member has no segment
              long enough to host any trombone amplitude.
            * ``"unrouted"`` -- the member was not in ``routes_by_net``.
            * ``"not_length_critical"`` -- the engagement gate fired:
              ``length_critical=False``, no change was made.
        attempts: Number of trombone insertions actually attempted for
            this member.
        inserts_applied: Number of trombones whose post-insertion check
            passed and were committed.
        length_before_mm: Member's routed length at entry.
        length_after_mm: Member's routed length after the (possibly
            empty) sequence of successful insertions.
        message: Human-readable summary.
        serpentine_results: Per-attempt results from the trombone
            generator (including rejected attempts).
    """

    success: bool = False
    reason: str = ""
    attempts: int = 0
    inserts_applied: int = 0
    length_before_mm: float = 0.0
    length_after_mm: float = 0.0
    message: str = ""
    serpentine_results: list[SerpentineResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def tune_match_group_v2(
    group: MatchGroup,
    routes_by_net: dict[int, Route],
    *,
    tolerance_mm: float | None = None,
    intra_group_clearance_mm: float,
    config: SerpentineConfig | None = None,
    max_inserts_per_member: int | None = None,
    length_critical: bool = True,
) -> dict[int, tuple[Route, TuneResult]]:
    """Tune the lengths of an N-trace match group to within tolerance.

    Generalizes :func:`~kicad_tools.router.diffpair_length_tuning.tune_diff_pair_skew`
    from N=2 (a pair) to N>=3 (a match group) while preserving the same
    safety invariants: outer-normal bulges, per-insertion DRC
    self-check with byte-for-byte rollback, and an explicit cascade-
    safety budget.

    Args:
        group: A declared / detected
            :class:`~kicad_tools.router.match_group_length.MatchGroup`.
            ``group.pair_ids`` MUST be empty for Phase 2E -- pair-aware
            composition (group of pairs) is the responsibility of
            Phase 2F.  An assertion error fires with a clear pointer
            when this precondition is violated.
        routes_by_net: ``{net_id: Route}`` lookup for all routed nets on
            the board.  Used both to fetch the group's members AND as
            the corpus against which the post-insertion DRC self-check
            looks for newly-introduced clearance violations against
            non-group nets.
        tolerance_mm: Per-group skew tolerance.  When ``None`` defaults
            to ``group.tolerance`` (the value set when the group was
            declared / detected).  A member is considered "matched" to
            the reference when ``abs(L_member - L_ref) <= tolerance_mm``.
        intra_group_clearance_mm: Edge-to-edge clearance floor used by
            the post-insertion DRC self-check.  Applied as the
            conservative same-value threshold for both the intra-group
            pass and the neighbor pass (mirrors PR #2663's
            single-threshold policy for the pair case).
        config: Optional :class:`SerpentineConfig` override.  When
            supplied its ``side`` and ``outer_normal_hint`` fields are
            overwritten per-insertion by the tuner; other fields
            (amplitude, gap_factor, ...) are honored.
        max_inserts_per_member: Optional override for the per-member
            cascade budget.  When ``None`` (the default) the tuner
            picks :data:`MAX_INSERTS_PER_GROUP_MEMBER_SMALL` for
            ``len(group.net_ids) <= 4`` and
            :data:`MAX_INSERTS_PER_GROUP_MEMBER_LARGE` for larger
            groups.  Regardless of this argument the group-level
            ceiling :data:`MAX_TOTAL_INSERTS_PER_GROUP` is enforced.
        length_critical: Engagement gate (default ``True``).  When
            ``False`` every member is returned unchanged with
            ``reason="not_length_critical"`` (matches the pair tuner's
            gate at :func:`tune_diff_pair_skew`).

    Returns:
        ``{net_id: (route, result)}`` for every member of ``group``.
        The route for a member that was *not* tuned (reference,
        already-within-tolerance, longer-than-reference, unrouted,
        rolled back, etc.) is returned **by reference** -- the same
        object the caller passed in ``routes_by_net``, with the same
        ``.segments`` list object.  Drift-prevention tests assert this
        via ``is`` identity.

    Rollback contract:
        When the post-insertion DRC self-check fails for a candidate
        on a given member, the proposed new Route is discarded and the
        original Route reference (the one in ``routes_by_net``) is
        returned with its original ``.segments`` list object intact.
        Mirrors the pair tuner's contract; tested via ``is`` identity.
    """
    # --- Phase 2F handoff guard -----------------------------------------
    # MatchGroup.pair_ids is reserved for Phase 2F's group-of-pairs
    # composition (Issue #2701).  Phase 2E treats every member as an
    # independent single-ended net; silently allowing pair_ids here would
    # break within-pair coupling.  See diffpair_length_tuning.py for the
    # N=2 pair-aware case.
    assert not group.pair_ids, (
        f"tune_match_group_v2 (Phase 2E) does not support group-of-pairs "
        f"composition (group={group.name!r} has {len(group.pair_ids)} pair_ids). "
        f"Use Phase 2F's tune_match_pair_group_v2 (Issue #2701) instead."
    )

    # --- Defaults --------------------------------------------------------
    if tolerance_mm is None:
        tolerance_mm = group.tolerance

    if max_inserts_per_member is None:
        if len(group.net_ids) > 4:
            max_inserts_per_member = MAX_INSERTS_PER_GROUP_MEMBER_LARGE
        else:
            max_inserts_per_member = MAX_INSERTS_PER_GROUP_MEMBER_SMALL

    base_config = config or SerpentineConfig()
    results: dict[int, tuple[Route, TuneResult]] = {}

    # --- Engagement gate: length_critical=False -------------------------
    # Every member returned by reference with reason="not_length_critical".
    if not length_critical:
        for net_id in group.net_ids:
            route = routes_by_net.get(net_id)
            if route is None:
                results[net_id] = (
                    _empty_route(net_id),
                    TuneResult(
                        success=False,
                        reason="unrouted",
                        message=f"Net {net_id} is unrouted; nothing to tune.",
                    ),
                )
            else:
                results[net_id] = (
                    route,
                    TuneResult(
                        success=True,
                        reason="not_length_critical",
                        message=(
                            f"Group {group.name!r} is not length_critical; "
                            "skipping tuning per engagement gate."
                        ),
                    ),
                )
        return results

    # --- Determine the reference length ---------------------------------
    # Use the same policy as MatchGroupTracker.get_reference_length, but
    # compute it locally from the (possibly mutated) routes_by_net so
    # the tuner is self-contained and does not require a tracker
    # instance.  See match_group_length.py:407-436 for the canonical
    # spec.
    from .length import LengthTracker  # avoid cycle

    member_lengths: dict[int, float] = {}
    for net_id in group.net_ids:
        route = routes_by_net.get(net_id)
        if route is None:
            continue
        member_lengths[net_id] = LengthTracker.calculate_route_length(route)

    # Resolve the reference length per policy.
    ref_length: float | None
    if group.reference_net_id is not None:
        ref_length = member_lengths.get(group.reference_net_id)
    else:
        # Longest-in-group default.
        ref_length = max(member_lengths.values()) if member_lengths else None

    # --- Per-member iteration -------------------------------------------
    total_inserts_committed = 0  # group-level cumulative counter

    for net_id in group.net_ids:
        route = routes_by_net.get(net_id)
        if route is None:
            results[net_id] = (
                _empty_route(net_id),
                TuneResult(
                    success=False,
                    reason="unrouted",
                    message=f"Net {net_id} is unrouted; nothing to tune.",
                ),
            )
            continue

        current_length = member_lengths[net_id]

        # If we couldn't derive a reference, nothing to do.
        if ref_length is None:
            results[net_id] = (
                route,
                TuneResult(
                    success=False,
                    reason="unrouted",
                    length_before_mm=current_length,
                    length_after_mm=current_length,
                    message=(
                        f"Group {group.name!r} has no derivable reference length "
                        "(reference net unrouted or no members routed)."
                    ),
                ),
            )
            continue

        # Explicit-reference member is the pace car; never touch.
        if group.reference_net_id == net_id:
            results[net_id] = (
                route,
                TuneResult(
                    success=True,
                    reason="reference",
                    length_before_mm=current_length,
                    length_after_mm=current_length,
                    message=(
                        f"Net {net_id} is the explicit reference for group "
                        f"{group.name!r}; never modified."
                    ),
                ),
            )
            continue

        delta = ref_length - current_length

        # Already within tolerance -- byte-for-byte unchanged.
        if abs(delta) <= tolerance_mm:
            results[net_id] = (
                route,
                TuneResult(
                    success=True,
                    reason="already_within_tolerance",
                    length_before_mm=current_length,
                    length_after_mm=current_length,
                    message=(
                        f"Net {net_id} already matched: "
                        f"|delta|={abs(delta):.4f}mm <= tol={tolerance_mm:.4f}mm"
                    ),
                ),
            )
            continue

        # Member is LONGER than the reference -- we cannot shorten;
        # leave it alone.  This is the explicit "longer_than_reference"
        # case the curator called out as a new reason value vs the
        # pair tuner.  In the longest-in-group default policy this
        # branch never fires because the reference IS the longest.
        # In the explicit "pace-car" policy it fires for any member
        # that's already longer than the pace-car length.
        if delta < 0:
            results[net_id] = (
                route,
                TuneResult(
                    success=True,
                    reason="longer_than_reference",
                    length_before_mm=current_length,
                    length_after_mm=current_length,
                    message=(
                        f"Net {net_id} is longer than reference "
                        f"({current_length:.4f}mm > {ref_length:.4f}mm); "
                        "tuner cannot remove length."
                    ),
                ),
            )
            continue

        # --- Cascade loop on this member -----------------------------
        original_route = route
        original_segments = route.segments
        current_route = route
        current_skew = abs(delta)

        per_member_result = TuneResult(
            length_before_mm=current_length,
        )

        # Group-level ceiling check before entering the per-member loop.
        if total_inserts_committed >= MAX_TOTAL_INSERTS_PER_GROUP:
            per_member_result.reason = "cascade_budget_exhausted"
            per_member_result.length_after_mm = current_length
            per_member_result.message = (
                f"Group {group.name!r} cumulative budget "
                f"({MAX_TOTAL_INSERTS_PER_GROUP}) exhausted before tuning "
                f"net {net_id}; no insertion attempted."
            )
            results[net_id] = (original_route, per_member_result)
            continue

        target_length = ref_length

        # Each iteration: pick segment, compute outer-normal hint
        # against the rest of the group, attempt one trombone, run
        # the self-check, commit or reject.
        for attempt in range(max_inserts_per_member):
            per_member_result.attempts = attempt + 1

            # Group-level ceiling can trip mid-loop too.
            if total_inserts_committed >= MAX_TOTAL_INSERTS_PER_GROUP:
                per_member_result.reason = "cascade_budget_exhausted"
                per_member_result.message = (
                    f"Group {group.name!r} cumulative budget "
                    f"({MAX_TOTAL_INSERTS_PER_GROUP}) exhausted "
                    f"during tuning of net {net_id}."
                )
                current_route = original_route
                break

            # Pick the insertion segment (shared by both the generator
            # and the outer-normal calculation).
            provisional = SerpentineGenerator(base_config)
            best = provisional.find_best_segment(current_route)
            if best is None:
                per_member_result.reason = "no_suitable_segment"
                per_member_result.message = (
                    f"No segment long enough for a trombone on net {net_id} "
                    f"(skew={current_skew:.4f}mm > tol={tolerance_mm:.4f}mm)"
                )
                current_route = original_route
                break
            _seg_idx, insertion_segment = best

            # Outer-normal hint vs the NEAREST other group member.
            hint = _outer_normal_hint_group(
                insertion_segment,
                candidate_net_id=net_id,
                group_routes={
                    other_id: routes_by_net[other_id]
                    for other_id in group.net_ids
                    if other_id != net_id and other_id in routes_by_net
                },
            )

            # Build the per-attempt config.
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
                current_route, target_length
            )
            per_member_result.serpentine_results.append(serp_result)

            if not serp_result.success:
                per_member_result.reason = "no_suitable_segment"
                per_member_result.message = (
                    f"Trombone generation failed on net {net_id}: "
                    f"{serp_result.message}"
                )
                current_route = original_route
                break

            # Post-insertion DRC self-check.
            if not _post_insertion_clearance_ok_group(
                new_segments=serp_result.new_segments,
                candidate_net_id=net_id,
                group_net_ids=set(group.net_ids),
                routes_by_net=routes_by_net,
                intra_group_clearance_mm=intra_group_clearance_mm,
            ):
                per_member_result.reason = "post_insertion_drc_violation"
                per_member_result.length_after_mm = current_length
                per_member_result.message = (
                    f"Post-insertion DRC self-check failed on net {net_id} "
                    f"(intra={intra_group_clearance_mm:.4f}mm); rolled back."
                )
                current_route = original_route
                # Drift-prevention invariant:
                assert current_route is original_route
                assert current_route.segments is original_segments
                break

            # Commit this attempt.
            current_route = candidate_route
            per_member_result.inserts_applied += 1
            total_inserts_committed += 1

            new_length = LengthTracker.calculate_route_length(current_route)
            current_skew = abs(target_length - new_length)

            if current_skew <= tolerance_mm:
                per_member_result.success = True
                per_member_result.reason = "tuned"
                break
        else:
            # for/else: completed max_inserts_per_member without break.
            per_member_result.reason = (
                per_member_result.reason or "exceeded_max_inserts"
            )

        if per_member_result.reason in ("", "exceeded_max_inserts"):
            per_member_result.message = per_member_result.message or (
                f"Cascade budget exhausted on net {net_id} "
                f"(attempts={per_member_result.attempts}, "
                f"inserts_applied={per_member_result.inserts_applied}, "
                f"skew={current_skew:.4f}mm vs tol={tolerance_mm:.4f}mm)"
            )

        # Final length.
        if per_member_result.inserts_applied > 0:
            per_member_result.length_after_mm = LengthTracker.calculate_route_length(
                current_route
            )
        else:
            per_member_result.length_after_mm = current_length

        # Drift-prevention: when rolled back / no inserts committed, the
        # returned route reference IS the original route reference.
        if per_member_result.inserts_applied == 0:
            assert current_route is original_route
            assert current_route.segments is original_segments

        results[net_id] = (current_route, per_member_result)

    return results


# ---------------------------------------------------------------------------
# Outer-normal hint (N-trace generalization of _outer_normal_hint)
# ---------------------------------------------------------------------------


def _outer_normal_hint_group(
    segment: Segment,
    candidate_net_id: int,
    group_routes: dict[int, Route],
) -> tuple[float, float]:
    """Return a unit vector pointing AWAY from the nearest other group member.

    Generalization of
    :func:`~kicad_tools.router.diffpair_length_tuning._outer_normal_hint`
    from N=2 (a partner) to N>=3.  Strategy (the curator's "Option (a):
    nearest-other-trace"):

    1. Compute the midpoint of ``segment``.
    2. For every other group member, find the closest point on its
       route to that midpoint (reusing
       :func:`~kicad_tools.router.diffpair_length_tuning._closest_point_on_segment`
       byte-for-byte -- no math duplication).
    3. Pick the *nearest* such closest point as the "partner" for this
       insertion.
    4. Return the unit vector from that closest point to the midpoint
       (i.e. AWAY from that nearest neighbor).

    Args:
        segment: The segment selected for trombone insertion on the
            candidate member.
        candidate_net_id: The net id of the candidate member (excluded
            from the search; we don't want to bulge "away from ourself").
        group_routes: ``{net_id: Route}`` lookup for the *other* group
            members (the candidate is expected to be already removed by
            the caller; an empty dict triggers the fallback).

    Returns:
        A unit vector ``(rx, ry)`` in the segment's layer plane.
        Fallback paths (segment's own geometric perpendicular) fire
        when:

        * ``group_routes`` is empty (the candidate is the only routed
          member).
        * No other member has any segments.
        * The nearest closest point coincides with the midpoint
          (magnitude-zero case; same fallback as the pair helper at
          ``diffpair_length_tuning.py:458-466``).
    """
    import math

    # The candidate net id parameter is intentionally accepted for
    # caller-side clarity (the caller documents the exclusion).  We do
    # NOT re-filter inside this helper -- the contract is that
    # ``group_routes`` already excludes the candidate.
    _ = candidate_net_id

    mx = (segment.x1 + segment.x2) / 2.0
    my = (segment.y1 + segment.y2) / 2.0

    # Fallback path: no other group members to bulge away from.
    if not group_routes:
        dx = segment.x2 - segment.x1
        dy = segment.y2 - segment.y1
        length = math.sqrt(dx * dx + dy * dy)
        if length == 0.0:
            return (0.0, 1.0)
        return (-dy / length, dx / length)

    # Find the nearest closest-point across all other members.
    best_dist_sq = float("inf")
    best_cx, best_cy = 0.0, 0.0
    found_any = False
    for other_route in group_routes.values():
        for pseg in other_route.segments:
            cx, cy = _closest_point_on_segment(mx, my, pseg.x1, pseg.y1, pseg.x2, pseg.y2)
            d2 = (cx - mx) ** 2 + (cy - my) ** 2
            if d2 < best_dist_sq:
                best_dist_sq = d2
                best_cx, best_cy = cx, cy
                found_any = True

    if not found_any:
        # All other members have empty segments lists -- fallback.
        dx = segment.x2 - segment.x1
        dy = segment.y2 - segment.y1
        length = math.sqrt(dx * dx + dy * dy)
        if length == 0.0:
            return (0.0, 1.0)
        return (-dy / length, dx / length)

    rx = mx - best_cx
    ry = my - best_cy
    mag = math.sqrt(rx * rx + ry * ry)
    if mag == 0.0:
        # The nearest neighbor crosses the midpoint exactly; pick the
        # segment's own perpendicular (same fallback as the pair helper).
        dx = segment.x2 - segment.x1
        dy = segment.y2 - segment.y1
        length = math.sqrt(dx * dx + dy * dy)
        if length == 0.0:
            return (0.0, 1.0)
        return (-dy / length, dx / length)
    return (rx / mag, ry / mag)


# ---------------------------------------------------------------------------
# Post-insertion DRC self-check (N-trace generalization)
# ---------------------------------------------------------------------------


def _post_insertion_clearance_ok_group(
    *,
    new_segments: list[Segment],
    candidate_net_id: int,
    group_net_ids: set[int],
    routes_by_net: dict[int, Route],
    intra_group_clearance_mm: float,
) -> bool:
    """Return True if the proposed serpentine segments are DRC-safe.

    Two-pronged generalization of
    :func:`~kicad_tools.router.diffpair_length_tuning._post_insertion_clearance_ok`
    from N=2 (one partner) to N>=3 (the rest of the group + the rest of
    the board).

    1. **Intra-group clearance**: every new segment is checked against
       every segment of every OTHER group member (excluding the
       candidate, whose old segments are being replaced).  Threshold is
       ``intra_group_clearance_mm``.

    2. **Inter-net clearance**: every new segment is checked against
       every segment of every routed net that is NOT a group member.
       Threshold is also ``intra_group_clearance_mm`` as a conservative
       floor (mirrors the pair tuner's single-threshold policy).

    Reuses :func:`segment_clearance` and the ``clearance + 1e-9 <
    threshold`` epsilon byte-for-byte from
    :func:`~kicad_tools.router.diffpair_length_tuning._post_insertion_clearance_ok`
    (do NOT inline alternate geometry).

    Args:
        new_segments: The trombone segments produced by
            :meth:`SerpentineGenerator.generate_trombone`.
        candidate_net_id: Net id of the trace receiving the trombone.
            Used as the exclusion set for the inter-net pass; the
            intra-group pass excludes it implicitly (the candidate is
            checked against the OTHER group members).
        group_net_ids: The full set of group member net ids.  Used to
            partition the board into "group members" (pass 1) and
            "non-group neighbors" (pass 2).
        routes_by_net: ``{net_id: Route}`` lookup for all routed nets.
        intra_group_clearance_mm: Edge-to-edge clearance floor in mm.

    Returns:
        ``True`` if no clearance violation is introduced; ``False``
        otherwise (the caller must roll back).
    """
    from kicad_tools.core.geometry import segment_clearance

    # Pass 1: intra-group.  Every other group member.
    for other_id in group_net_ids:
        if other_id == candidate_net_id:
            continue
        other_route = routes_by_net.get(other_id)
        if other_route is None:
            continue
        for new_seg in new_segments:
            for pseg in other_route.segments:
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
                if clearance + 1e-9 < intra_group_clearance_mm:
                    return False

    # Pass 2: inter-net.  Every non-group routed net.
    for other_net_id, other_route in routes_by_net.items():
        if other_net_id == candidate_net_id:
            continue
        if other_net_id in group_net_ids:
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
                if clearance + 1e-9 < intra_group_clearance_mm:
                    return False

    return True


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _empty_route(net_id: int) -> Route:
    """Return an empty :class:`Route` shell for an unrouted net.

    Used as a placeholder in the return value of
    :func:`tune_match_group_v2` when a group member is unrouted.  The
    caller can detect the situation via
    ``result.reason == "unrouted"`` and the empty ``.segments`` list.
    """
    from .primitives import Route

    return Route(net=net_id, net_name=f"net_{net_id}", segments=[], vias=[])


# Compatibility re-exports.
__all__ = [
    "MAX_INSERTS_PER_GROUP_MEMBER",
    "MAX_INSERTS_PER_GROUP_MEMBER_LARGE",
    "MAX_INSERTS_PER_GROUP_MEMBER_SMALL",
    "MAX_TOTAL_INSERTS_PER_GROUP",
    "TuneResult",
    "tune_match_group_v2",
]


# Reference suppression for static linters: MAX_INSERTS_PER_PAIR is
# imported so the drift-prevention test
# (MAX_INSERTS_PER_GROUP_MEMBER_SMALL == MAX_INSERTS_PER_PAIR) does not
# require an extra import side.
_ = MAX_INSERTS_PER_PAIR  # noqa: F401
