"""N-trace match-group length-skew (serpentine) tuner.

Phase 2E (Issue #2700) -- scalar N-trace tuner.
Phase 2F (Issue #2701) -- group-of-pairs symmetric tuner (additive).

This module implements :func:`tune_match_group_v2`, a router-internal
helper that inserts serpentines (trombones) on the *shorter* members of a
detected N-trace match group (DDR data byte, MIPI lane group, generic
parallel bus) until the group's skew is within the per-group
:attr:`~kicad_tools.router.match_group_length.MatchGroup.tolerance`
window, OR a cascade-safety budget is exhausted.

The public function :func:`tune_match_group_v2` is a thin dispatcher
that selects between two private helpers based on
:attr:`MatchGroup.pair_ids`:

* :func:`_tune_match_group_single_ended` (Phase 2E) -- scalar members in
  ``net_ids`` only.  Each net is tuned independently.
* :func:`_tune_match_group_of_pairs` (Phase 2F) -- pair members in
  ``pair_ids`` (and optionally scalar members in ``net_ids``).  For each
  pair, both halves receive **identical, mirrored** serpentine geometry
  so within-pair coupling is preserved across the meander.

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

  For pair-aware members the outer-normal is computed **at the pair
  centerline** (not per-half) so the bulge direction chosen is the same
  for both P and N halves -- this is what makes the mirror-about-centerline
  step geometrically meaningful.  See :func:`_outer_normal_hint_pair_group`.

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

  For pair-aware insertions the check is run on BOTH halves of the pair
  (the "paired DRC self-check") and BOTH halves rollback atomically on
  failure -- see :func:`_post_insertion_clearance_ok_pair_group`.

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

  For pair-aware groups the budget counts each (P+N mirrored) pair
  insertion as ONE -- a 4-pair HDMI group's worst-case insertion count
  equals the worst-case for a 4-net single-ended group, not 8 (Phase 2F
  AC #8).

* **Reference-net policy.**  Delegates to
  :meth:`MatchGroupTracker.get_reference_length`: ``None`` ->
  longest-in-group (legacy ``tune_match_group`` semantic); explicit net
  id -> "pace-car" semantic.  When a group member is already *longer*
  than the reference the tuner cannot remove length (we never cut), so
  that member is left unchanged with ``reason="longer_than_reference"``
  -- distinct from the pair tuner's ``"reference"`` reason because for
  N>=3 the over-length case is genuinely different from the
  "you are the reference" case.

  For pair-aware groups the **lane length** is the average of the pair's
  two halves: ``L_lane = (L_p + L_n) / 2``.  The reference selection
  policy is unchanged -- a scalar reference (clock) or a paired-half
  reference (resolves to the lane average) both work.

* **Mirror-about-centerline geometry**.  For each pair member tuned in
  the pair-aware path:

  1. Compute the pair centerline midpoint between corresponding P/N
     segment endpoints.
  2. Compute the outer-normal at the centerline (ONE normal that works
     for both halves, picked by the centerline outer-normal helper).
  3. Generate the P-side meander using the same Phase 2E single-ended
     serpentine engine.
  4. Mirror the P-side new segments by reflection-about-centerline,
     snapping each reflected endpoint to ``grid_resolution_mm``.
  5. Run the paired DRC self-check on BOTH halves; rollback BOTH on
     failure.

Out of scope for Phase 2F:

* Pipeline wiring -- ``Autorouter._finalize_routing`` calling
  :func:`tune_match_group_v2` is a separate Phase 2.5 wiring issue.
* CLI flag -- ``--length-match-groups`` (Phase 3H of this epic) is a
  separate issue.
* Modifying the legacy :func:`~kicad_tools.router.optimizer.serpentine.tune_match_group`
  at ``serpentine.py:484``.  Kept for backward compat with
  :func:`~kicad_tools.router.length_tuning.apply_length_tuning`.
* Mixed pair/scalar membership for the same net (same net appearing in
  both ``net_ids`` AND ``pair_ids``) -- raises ``ValueError`` at the
  dispatcher entry.
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
    intra_pair_clearance_mm: float | None = None,
    config: SerpentineConfig | None = None,
    max_inserts_per_member: int | None = None,
    length_critical: bool = True,
    grid_resolution_mm: float = 0.01,
) -> dict[int, tuple[Route, TuneResult]]:
    """Tune the lengths of an N-trace match group to within tolerance.

    Dispatcher: routes to :func:`_tune_match_group_single_ended`
    (Phase 2E -- scalar members) when ``group.pair_ids`` is empty, and
    to :func:`_tune_match_group_of_pairs` (Phase 2F -- pair-aware
    members) otherwise.

    The PUBLIC surface is a single function; the caller passes the same
    :class:`MatchGroup` regardless of shape and the dispatcher figures
    out which path applies.

    Generalizes :func:`~kicad_tools.router.diffpair_length_tuning.tune_diff_pair_skew`
    from N=2 (a pair) to N>=3 (a match group) while preserving the same
    safety invariants: outer-normal bulges, per-insertion DRC
    self-check with byte-for-byte rollback, and an explicit cascade-
    safety budget.

    Args:
        group: A declared / detected
            :class:`~kicad_tools.router.match_group_length.MatchGroup`.
            Members may live in ``net_ids`` (scalar -- Phase 2E path),
            ``pair_ids`` (pair-aware -- Phase 2F path), or BOTH (mixed
            group with a scalar clock + paired data lanes).  A net id
            appearing in BOTH ``net_ids`` AND ``pair_ids`` raises
            :class:`ValueError` (over-constrained declaration).
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
        intra_pair_clearance_mm: Edge-to-edge clearance floor for the
            *within-pair* coupling check (P vs N of the same pair).
            REQUIRED when ``group.pair_ids`` is non-empty; raises
            :class:`ValueError` if omitted.  Ignored when ``pair_ids``
            is empty (the scalar Phase 2E path).
        config: Optional :class:`SerpentineConfig` override.  When
            supplied its ``side`` and ``outer_normal_hint`` fields are
            overwritten per-insertion by the tuner; other fields
            (amplitude, gap_factor, ...) are honored.
        max_inserts_per_member: Optional override for the per-member
            cascade budget.  When ``None`` (the default) the tuner
            picks :data:`MAX_INSERTS_PER_GROUP_MEMBER_SMALL` for
            ``len(group.net_ids) + len(group.pair_ids) <= 4`` and
            :data:`MAX_INSERTS_PER_GROUP_MEMBER_LARGE` for larger
            groups.  Regardless of this argument the group-level
            ceiling :data:`MAX_TOTAL_INSERTS_PER_GROUP` is enforced.
            For pair-aware members one (P+N mirrored) attempt counts as
            ONE insertion against this budget.
        length_critical: Engagement gate (default ``True``).  When
            ``False`` every member is returned unchanged with
            ``reason="not_length_critical"`` (matches the pair tuner's
            gate at :func:`tune_diff_pair_skew`).
        grid_resolution_mm: Routing grid resolution used to snap the
            mirrored N-side endpoints in the pair-aware path.  Default
            ``0.01`` mm matches the typical 10um router grid; tests pass
            an explicit value when they need a coarser grid to verify
            the snap behavior.

    Returns:
        ``{net_id: (route, result)}`` for every member of ``group``.
        Pair-aware members yield TWO entries (one per half), keyed on
        each half's net id.  The route for a member that was *not* tuned
        (reference, already-within-tolerance, longer-than-reference,
        unrouted, rolled back, etc.) is returned **by reference** -- the
        same object the caller passed in ``routes_by_net``, with the
        same ``.segments`` list object.  Drift-prevention tests assert
        this via ``is`` identity.

    Rollback contract:
        When the post-insertion DRC self-check fails for a candidate
        on a given member, the proposed new Route is discarded and the
        original Route reference (the one in ``routes_by_net``) is
        returned with its original ``.segments`` list object intact.
        Mirrors the pair tuner's contract; tested via ``is`` identity.
        For pair-aware members BOTH halves rollback atomically: the P
        AND N references and their ``.segments`` lists are returned
        unchanged.

    Raises:
        ValueError: When ``group.pair_ids`` is non-empty but
            ``intra_pair_clearance_mm`` is ``None``.
        ValueError: When the same net id appears in both
            ``group.net_ids`` and ``group.pair_ids`` (mixed pair/scalar
            membership for the same net is unsupported -- see
            Phase 2F-follow-up).
    """
    # --- Validation: same-net overlap between net_ids and pair_ids -----
    # A net cannot be a "scalar" member AND a half of a pair
    # simultaneously -- the geometry of the two paths would conflict.
    # The dispatcher rejects up front rather than silently producing
    # ambiguous output.
    paired_net_ids: set[int] = set()
    for p_id, n_id in group.pair_ids:
        paired_net_ids.add(p_id)
        paired_net_ids.add(n_id)
    overlap = set(group.net_ids) & paired_net_ids
    if overlap:
        raise ValueError(
            f"MatchGroup {group.name!r}: nets {sorted(overlap)} appear in "
            f"BOTH net_ids and pair_ids.  Phase 2F-follow-up: mixed "
            "pair/scalar membership for the same net is unsupported."
        )

    # --- Pair-aware path requires intra_pair_clearance_mm --------------
    if group.pair_ids and intra_pair_clearance_mm is None:
        raise ValueError(
            f"MatchGroup {group.name!r} has {len(group.pair_ids)} pair_ids "
            "but intra_pair_clearance_mm was not supplied.  The pair-aware "
            "DRC self-check requires the within-pair clearance floor."
        )

    # --- Defaults --------------------------------------------------------
    if tolerance_mm is None:
        tolerance_mm = group.tolerance

    # The per-member budget defaults consider BOTH scalar nets and pairs
    # against the small/large threshold.  Each (P+N) pair counts as ONE
    # member for budget purposes -- the geometry is logically a single
    # "lane perturbation" (Phase 2F AC #8).
    member_count = len(group.net_ids) + len(group.pair_ids)
    if max_inserts_per_member is None:
        if member_count > 4:
            max_inserts_per_member = MAX_INSERTS_PER_GROUP_MEMBER_LARGE
        else:
            max_inserts_per_member = MAX_INSERTS_PER_GROUP_MEMBER_SMALL

    # --- Dispatch --------------------------------------------------------
    if group.pair_ids:
        return _tune_match_group_of_pairs(
            group,
            routes_by_net,
            tolerance_mm=tolerance_mm,
            intra_group_clearance_mm=intra_group_clearance_mm,
            intra_pair_clearance_mm=intra_pair_clearance_mm,  # type: ignore[arg-type]
            config=config,
            max_inserts_per_member=max_inserts_per_member,
            length_critical=length_critical,
            grid_resolution_mm=grid_resolution_mm,
        )

    return _tune_match_group_single_ended(
        group,
        routes_by_net,
        tolerance_mm=tolerance_mm,
        intra_group_clearance_mm=intra_group_clearance_mm,
        config=config,
        max_inserts_per_member=max_inserts_per_member,
        length_critical=length_critical,
    )


def _tune_match_group_single_ended(
    group: MatchGroup,
    routes_by_net: dict[int, Route],
    *,
    tolerance_mm: float,
    intra_group_clearance_mm: float,
    config: SerpentineConfig | None = None,
    max_inserts_per_member: int,
    length_critical: bool = True,
) -> dict[int, tuple[Route, TuneResult]]:
    """Scalar Phase 2E path: each net in ``group.net_ids`` tuned independently.

    Internal helper for the :func:`tune_match_group_v2` dispatcher.
    Implements the original Phase 2E single-ended path verbatim --
    nothing here is pair-aware.  The dispatcher routes to this helper
    when ``group.pair_ids`` is empty.

    See :func:`tune_match_group_v2` for the public docstring; the
    arguments are passed through unchanged except that ``tolerance_mm``
    and ``max_inserts_per_member`` have already been resolved from the
    public API's ``None`` defaults.
    """
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
                    f"Trombone generation failed on net {net_id}: {serp_result.message}"
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
            per_member_result.reason = per_member_result.reason or "exceeded_max_inserts"

        if per_member_result.reason in ("", "exceeded_max_inserts"):
            per_member_result.message = per_member_result.message or (
                f"Cascade budget exhausted on net {net_id} "
                f"(attempts={per_member_result.attempts}, "
                f"inserts_applied={per_member_result.inserts_applied}, "
                f"skew={current_skew:.4f}mm vs tol={tolerance_mm:.4f}mm)"
            )

        # Final length.
        if per_member_result.inserts_applied > 0:
            per_member_result.length_after_mm = LengthTracker.calculate_route_length(current_route)
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
# Phase 2F: group-of-pairs symmetric serpentine
# ---------------------------------------------------------------------------


def _pair_centerline_midpoint(p_seg: Segment, n_seg: Segment) -> tuple[float, float]:
    """Compute the centerline midpoint between two pair segments.

    For corresponding P/N segments, the pair centerline midpoint is the
    average of the two segments' midpoints.  Used by
    :func:`_outer_normal_hint_pair_group` to anchor the outer-normal
    computation and by :func:`_mirror_segments_about_centerline` to
    establish the reflection axis.

    Args:
        p_seg: The P-side segment.
        n_seg: The N-side segment.

    Returns:
        ``(mx, my)`` -- the centerline midpoint coordinate.
    """
    p_mx = (p_seg.x1 + p_seg.x2) / 2.0
    p_my = (p_seg.y1 + p_seg.y2) / 2.0
    n_mx = (n_seg.x1 + n_seg.x2) / 2.0
    n_my = (n_seg.y1 + n_seg.y2) / 2.0
    return ((p_mx + n_mx) / 2.0, (p_my + n_my) / 2.0)


def _outer_normal_hint_pair_group(
    p_seg: Segment,
    n_seg: Segment,
    candidate_p_id: int,
    candidate_n_id: int,
    group_routes: dict[int, Route],
) -> tuple[float, float]:
    """Return a unit vector at the pair centerline pointing AWAY from the
    nearest other group member.

    Pair-aware analog of :func:`_outer_normal_hint_group`.  The key
    difference: the midpoint used for the nearest-neighbor search is the
    pair *centerline* midpoint (the average of P and N midpoints) rather
    than either half's individual midpoint.  This guarantees the same
    normal is chosen for the P-side meander generation AND for the
    mirror-about-centerline reflection that produces the N-side
    geometry.

    Args:
        p_seg: The P-side segment selected for trombone insertion.
        n_seg: The corresponding N-side segment.
        candidate_p_id: Net id of the P half (excluded from the search).
        candidate_n_id: Net id of the N half (excluded from the search).
        group_routes: ``{net_id: Route}`` lookup for OTHER group members
            (callers must exclude both halves of the candidate pair).
            An empty dict triggers the fallback path.

    Returns:
        A unit vector ``(rx, ry)`` in the segment's layer plane.
        Fallback paths (segment's own geometric perpendicular taken at
        the P segment) fire when:

        * ``group_routes`` is empty.
        * No other member has segments.
        * The nearest closest point coincides with the centerline
          midpoint (the collinear-centerlines edge case Phase 2F
          calls out -- see the issue's AC additions).
    """
    import math

    # Acknowledge the candidate ids for caller-side clarity (the helper
    # contract is that ``group_routes`` already excludes them).
    _ = (candidate_p_id, candidate_n_id)

    # Centerline midpoint: the geometric anchor for the outer-normal.
    mx, my = _pair_centerline_midpoint(p_seg, n_seg)

    # Fallback: no other members -> use P-segment perpendicular.
    if not group_routes:
        dx = p_seg.x2 - p_seg.x1
        dy = p_seg.y2 - p_seg.y1
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
        # All other members empty -- fallback to P-perpendicular.
        dx = p_seg.x2 - p_seg.x1
        dy = p_seg.y2 - p_seg.y1
        length = math.sqrt(dx * dx + dy * dy)
        if length == 0.0:
            return (0.0, 1.0)
        return (-dy / length, dx / length)

    rx = mx - best_cx
    ry = my - best_cy
    mag = math.sqrt(rx * rx + ry * ry)
    if mag == 0.0:
        # Collinear-centerlines edge case: the nearest neighbor's
        # centerline coincides with the candidate's centerline.  Fall
        # back to the P-segment perpendicular (same as the empty-
        # neighbors case) -- this guarantees a well-defined outer
        # normal even for the "two pairs running parallel at the same
        # y but different x" case in the curator's AC #4.
        dx = p_seg.x2 - p_seg.x1
        dy = p_seg.y2 - p_seg.y1
        length = math.sqrt(dx * dx + dy * dy)
        if length == 0.0:
            return (0.0, 1.0)
        return (-dy / length, dx / length)
    return (rx / mag, ry / mag)


def _snap_to_grid(value: float, grid_resolution_mm: float) -> float:
    """Snap ``value`` to the nearest multiple of ``grid_resolution_mm``.

    Used by :func:`_mirror_segments_about_centerline` to ensure the
    N-side reflected endpoints land on the same grid as the P-side
    serpentine.  Off-by-one rounding here would re-introduce within-pair
    skew (the canary that Phase 2F AC #2 catches).
    """
    if grid_resolution_mm <= 0.0:
        return value
    return round(value / grid_resolution_mm) * grid_resolution_mm


def _reflect_point_about_axis(
    px: float,
    py: float,
    cx: float,
    cy: float,
    nx: float,
    ny: float,
) -> tuple[float, float]:
    """Reflect ``(px, py)`` across the axis through ``(cx, cy)`` perpendicular
    to the unit normal ``(nx, ny)``.

    The axis is the pair centerline: a line passing through the
    centerline midpoint ``(cx, cy)`` whose normal is ``(nx, ny)``
    (the outer-normal vector).  The reflection of ``(px, py)`` across
    this line is:

        d = ((px - cx) * nx + (py - cy) * ny)
        (rx, ry) = (px - 2*d*nx, py - 2*d*ny)

    Args:
        px, py: The point to reflect.
        cx, cy: A point on the axis (the centerline midpoint).
        nx, ny: A unit vector normal to the axis (the outer-normal).

    Returns:
        ``(rx, ry)`` -- the reflected point.
    """
    d = (px - cx) * nx + (py - cy) * ny
    return (px - 2.0 * d * nx, py - 2.0 * d * ny)


def _mirror_segments_about_centerline(
    new_p_segments: list[Segment],
    p_net_id: int,
    n_net_id: int,
    n_net_name: str,
    cx: float,
    cy: float,
    nx: float,
    ny: float,
    grid_resolution_mm: float,
) -> list[Segment]:
    """Mirror P-side serpentine segments across the pair centerline.

    For each P-side new segment, reflects both endpoints across the
    centerline axis (defined by point ``(cx, cy)`` and normal
    ``(nx, ny)``), snaps each reflected coordinate to the routing grid,
    and emits a new :class:`Segment` carrying the N-side net id / name.

    The reflection preserves segment length (modulo grid snapping
    rounding within ``grid_resolution_mm / 2``) and segment direction
    parallel-not-antiparallel (the curator's AC #3 sharpening point).

    Args:
        new_p_segments: P-side serpentine segments to mirror.
        p_net_id: Net id of the P half (used to preserve segment
            metadata symmetry; not assigned to the new segments).
        n_net_id: Net id of the N half (assigned to each mirrored
            segment).
        n_net_name: Net name for the N half.
        cx, cy: Centerline midpoint anchor.
        nx, ny: Outer-normal unit vector.
        grid_resolution_mm: Routing grid resolution; each reflected
            endpoint is snapped to the nearest multiple.

    Returns:
        A new list of :class:`Segment` instances ready to splice into
        the N-side route.
    """
    from .primitives import Segment as _Segment

    _ = p_net_id  # caller-side clarity
    mirrored: list[_Segment] = []
    for pseg in new_p_segments:
        rx1, ry1 = _reflect_point_about_axis(pseg.x1, pseg.y1, cx, cy, nx, ny)
        rx2, ry2 = _reflect_point_about_axis(pseg.x2, pseg.y2, cx, cy, nx, ny)
        mirrored.append(
            _Segment(
                x1=_snap_to_grid(rx1, grid_resolution_mm),
                y1=_snap_to_grid(ry1, grid_resolution_mm),
                x2=_snap_to_grid(rx2, grid_resolution_mm),
                y2=_snap_to_grid(ry2, grid_resolution_mm),
                width=pseg.width,
                layer=pseg.layer,
                net=n_net_id,
                net_name=n_net_name,
            )
        )
    return mirrored


def _splice_mirrored_n_route(
    n_route: Route,
    n_insertion_seg_index: int,
    mirrored_segments: list[Segment],
) -> Route:
    """Build a new N-side Route by splicing in mirrored serpentine segments.

    The strategy mirrors what :meth:`SerpentineGenerator.add_serpentine`
    does on the P side: replace ``n_route.segments[n_insertion_seg_index]``
    with the mirrored segments (which start at the original segment's
    one endpoint and end at the other) plus the surrounding segments
    untouched.

    Note: this is a simplified splice that assumes ``mirrored_segments``
    forms a continuous path from one endpoint of the original segment
    to the other.  When the mirror swaps the direction (because the
    reflection axis crosses through the segment) the splice prepends
    the reversed first/last endpoints onto the surrounding segments.

    Args:
        n_route: The original N-side route to splice into.
        n_insertion_seg_index: Index of the segment in ``n_route.segments``
            that the serpentine replaces.
        mirrored_segments: The mirrored serpentine segments (P-side
            geometry reflected about the centerline).

    Returns:
        A new :class:`Route` with the same ``net``/``net_name`` as
        ``n_route`` and the mirrored segments spliced in.  The original
        ``n_route`` object and its ``.segments`` list are NOT mutated.
    """
    from .primitives import Route as _Route

    before = list(n_route.segments[:n_insertion_seg_index])
    after = list(n_route.segments[n_insertion_seg_index + 1 :])
    new_segments = before + list(mirrored_segments) + after
    return _Route(
        net=n_route.net,
        net_name=n_route.net_name,
        segments=new_segments,
        vias=list(n_route.vias),
    )


def _find_corresponding_n_segment(
    n_route: Route,
    p_seg: Segment,
) -> tuple[int, Segment] | None:
    """Find the N-side segment "corresponding" to ``p_seg``.

    The correspondence heuristic: the N-side segment whose midpoint is
    closest to ``p_seg``'s midpoint AND which shares the same layer.
    This works for the canonical pair geometry (P and N traces running
    parallel to each other on the same layer) which is what Phase 2F is
    targeted at (MIPI lanes, HDMI TMDS lanes).

    Args:
        n_route: The N-side route.
        p_seg: The P-side segment selected for trombone insertion.

    Returns:
        ``(index, segment)`` -- the index into ``n_route.segments`` and
        the segment itself, or ``None`` if no same-layer N segment
        exists.
    """
    p_mx = (p_seg.x1 + p_seg.x2) / 2.0
    p_my = (p_seg.y1 + p_seg.y2) / 2.0

    best_idx: int | None = None
    best_seg: Segment | None = None
    best_d2 = float("inf")
    for i, nseg in enumerate(n_route.segments):
        if nseg.layer != p_seg.layer:
            continue
        n_mx = (nseg.x1 + nseg.x2) / 2.0
        n_my = (nseg.y1 + nseg.y2) / 2.0
        d2 = (n_mx - p_mx) ** 2 + (n_my - p_my) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best_idx = i
            best_seg = nseg
    if best_idx is None or best_seg is None:
        return None
    return (best_idx, best_seg)


def _post_insertion_clearance_ok_pair_group(
    *,
    new_p_segments: list[Segment],
    new_n_segments: list[Segment],
    candidate_p_id: int,
    candidate_n_id: int,
    group_net_ids: set[int],
    routes_by_net: dict[int, Route],
    intra_group_clearance_mm: float,
    intra_pair_clearance_mm: float,
) -> bool:
    """Paired DRC self-check for pair-aware serpentine insertion.

    Three-pronged generalization of
    :func:`_post_insertion_clearance_ok_group` for the pair-aware case:

    1. **Within-pair coupling** -- each new P segment is checked against
       each new N segment at threshold ``intra_pair_clearance_mm``.
       Ensures the mirrored geometry preserves the within-pair coupling
       window the router established at launch (Epic #2556 Phase 2F).
    2. **Intra-group** -- every new P AND every new N segment is checked
       against every segment of every OTHER group member (excluding
       both halves of the candidate pair) at threshold
       ``intra_group_clearance_mm``.
    3. **Inter-net** -- every new P AND every new N segment is checked
       against every segment of every routed net that is NOT a group
       member at the same ``intra_group_clearance_mm`` threshold.

    Reuses :func:`segment_clearance` and the ``clearance + 1e-9 <
    threshold`` epsilon byte-for-byte from the scalar Phase 2E helper.
    On rejection BOTH halves must roll back atomically.

    Args:
        new_p_segments: P-side new serpentine segments.
        new_n_segments: N-side new (mirrored) serpentine segments.
        candidate_p_id: Net id of the P half.
        candidate_n_id: Net id of the N half.
        group_net_ids: All group member net ids (both scalars in
            ``net_ids`` AND both halves of every ``pair_ids`` entry).
        routes_by_net: ``{net_id: Route}`` lookup for all routed nets.
        intra_group_clearance_mm: Threshold for the intra-group and
            inter-net passes.
        intra_pair_clearance_mm: Threshold for the within-pair coupling
            pass (must be smaller than ``intra_group_clearance_mm``).

    Returns:
        ``True`` if no clearance violation is introduced; ``False``
        otherwise (the caller must roll back BOTH halves).
    """
    from kicad_tools.core.geometry import segment_clearance

    # Pass 1: within-pair coupling.  P new vs N new.
    for new_p in new_p_segments:
        for new_n in new_n_segments:
            if new_p.layer != new_n.layer:
                continue
            clearance = segment_clearance(
                new_p.x1,
                new_p.y1,
                new_p.x2,
                new_p.y2,
                new_p.width,
                new_n.x1,
                new_n.y1,
                new_n.x2,
                new_n.y2,
                new_n.width,
            )
            if clearance + 1e-9 < intra_pair_clearance_mm:
                return False

    # Pass 2: intra-group.  Every other group member.
    for other_id in group_net_ids:
        if other_id in (candidate_p_id, candidate_n_id):
            continue
        other_route = routes_by_net.get(other_id)
        if other_route is None:
            continue
        for new_seg in list(new_p_segments) + list(new_n_segments):
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

    # Pass 3: inter-net.  Every non-group routed net.
    for other_net_id, other_route in routes_by_net.items():
        if other_net_id in (candidate_p_id, candidate_n_id):
            continue
        if other_net_id in group_net_ids:
            continue
        for new_seg in list(new_p_segments) + list(new_n_segments):
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


def _tune_match_group_of_pairs(
    group: MatchGroup,
    routes_by_net: dict[int, Route],
    *,
    tolerance_mm: float,
    intra_group_clearance_mm: float,
    intra_pair_clearance_mm: float,
    config: SerpentineConfig | None = None,
    max_inserts_per_member: int,
    length_critical: bool = True,
    grid_resolution_mm: float = 0.01,
) -> dict[int, tuple[Route, TuneResult]]:
    """Pair-aware Phase 2F path: mirrored serpentine geometry for pair members.

    Internal helper for the :func:`tune_match_group_v2` dispatcher.
    Handles the group-of-pairs case (MIPI lanes, HDMI TMDS lanes) by
    treating each ``(p_net_id, n_net_id)`` entry in ``group.pair_ids``
    as a single "lane" with length ``L_lane = (L_p + L_n) / 2``.

    For each non-reference lane that needs lengthening:

    1. Pick a candidate P-side segment (mirror the Phase 2E
       single-ended segment-selection heuristic).
    2. Find the corresponding N-side segment by midpoint proximity.
    3. Compute the outer-normal at the pair centerline (one normal for
       both halves).
    4. Generate the P-side meander using the Phase 2E single-ended
       trombone engine.
    5. Mirror the P-side new segments across the centerline, snapping
       each reflected endpoint to ``grid_resolution_mm``.
    6. Run the paired DRC self-check; rollback BOTH halves on failure.

    Scalar members in ``group.net_ids`` (e.g. a mixed group with a
    scalar clock + paired data lanes) are handled by an inner call to
    :func:`_tune_match_group_single_ended` on a virtual scalar-only
    group AFTER the pair-aware sweep.  This preserves the policy that
    scalars and pairs are tuned with their respective specialized
    geometries.

    See :func:`tune_match_group_v2` for the public docstring.
    """
    base_config = config or SerpentineConfig()
    results: dict[int, tuple[Route, TuneResult]] = {}

    # --- Engagement gate: length_critical=False -------------------------
    if not length_critical:
        all_net_ids: list[int] = list(group.net_ids)
        for p_id, n_id in group.pair_ids:
            all_net_ids.append(p_id)
            all_net_ids.append(n_id)
        for net_id in all_net_ids:
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

    # --- Measure every member length ------------------------------------
    from .length import LengthTracker  # avoid cycle

    member_lengths: dict[int, float] = {}
    for net_id in group.net_ids:
        route = routes_by_net.get(net_id)
        if route is not None:
            member_lengths[net_id] = LengthTracker.calculate_route_length(route)
    for p_id, n_id in group.pair_ids:
        for nid in (p_id, n_id):
            route = routes_by_net.get(nid)
            if route is not None:
                member_lengths[nid] = LengthTracker.calculate_route_length(route)

    # --- Compute per-lane length (pair average) ------------------------
    lane_lengths: dict[tuple[int, int], float] = {}
    for p_id, n_id in group.pair_ids:
        lp = member_lengths.get(p_id)
        ln = member_lengths.get(n_id)
        if lp is None or ln is None:
            # Skip un-or-half-routed pairs; mark each half as unrouted.
            continue
        lane_lengths[(p_id, n_id)] = (lp + ln) / 2.0

    # --- Resolve the reference length ----------------------------------
    # Policy:
    #   * group.reference_net_id None -> longest *lane average* across
    #     pairs (or longest scalar if no pairs are routable).
    #   * group.reference_net_id = scalar net in net_ids -> that scalar's
    #     length is the target.
    #   * group.reference_net_id = paired half -> that lane's average
    #     is the target.
    ref_length: float | None = None
    if group.reference_net_id is not None:
        # Is the reference a paired half?
        ref_pair: tuple[int, int] | None = None
        for p_id, n_id in group.pair_ids:
            if group.reference_net_id in (p_id, n_id):
                ref_pair = (p_id, n_id)
                break
        if ref_pair is not None and ref_pair in lane_lengths:
            ref_length = lane_lengths[ref_pair]
        elif group.reference_net_id in member_lengths:
            ref_length = member_lengths[group.reference_net_id]
    else:
        candidates: list[float] = list(lane_lengths.values())
        for net_id in group.net_ids:
            if net_id in member_lengths:
                candidates.append(member_lengths[net_id])
        if candidates:
            ref_length = max(candidates)

    # --- Per-pair iteration --------------------------------------------
    total_inserts_committed = 0

    for p_id, n_id in group.pair_ids:
        maybe_p_route = routes_by_net.get(p_id)
        maybe_n_route = routes_by_net.get(n_id)

        # Either half unrouted -> both reported unrouted; don't tune.
        if maybe_p_route is None or maybe_n_route is None:
            for nid in (p_id, n_id):
                results[nid] = (
                    routes_by_net.get(nid) or _empty_route(nid),
                    TuneResult(
                        success=False,
                        reason="unrouted",
                        message=f"Pair ({p_id}, {n_id}) has unrouted half(s); no tuning attempted.",
                    ),
                )
            continue

        # Type-narrowed: both halves are routed.
        p_route: Route = maybe_p_route
        n_route: Route = maybe_n_route
        original_p_route: Route = p_route
        original_n_route: Route = n_route
        original_p_segments = p_route.segments
        original_n_segments = n_route.segments

        lane_length = lane_lengths.get((p_id, n_id))
        if lane_length is None or ref_length is None:
            for nid in (p_id, n_id):
                results[nid] = (
                    routes_by_net[nid],
                    TuneResult(
                        success=False,
                        reason="unrouted",
                        length_before_mm=member_lengths.get(nid, 0.0),
                        length_after_mm=member_lengths.get(nid, 0.0),
                        message=(
                            f"Pair ({p_id}, {n_id}): no derivable lane length "
                            "or reference (unrouted half)."
                        ),
                    ),
                )
            continue

        # This pair IS the reference lane (by paired-half reference policy).
        if group.reference_net_id is not None and group.reference_net_id in (p_id, n_id):
            for nid in (p_id, n_id):
                results[nid] = (
                    routes_by_net[nid],
                    TuneResult(
                        success=True,
                        reason="reference",
                        length_before_mm=member_lengths[nid],
                        length_after_mm=member_lengths[nid],
                        message=(
                            f"Pair ({p_id}, {n_id}) is the explicit reference "
                            f"for group {group.name!r}; never modified."
                        ),
                    ),
                )
            continue

        delta = ref_length - lane_length

        # Already within tolerance -- byte-for-byte unchanged.
        if abs(delta) <= tolerance_mm:
            for nid in (p_id, n_id):
                results[nid] = (
                    routes_by_net[nid],
                    TuneResult(
                        success=True,
                        reason="already_within_tolerance",
                        length_before_mm=member_lengths[nid],
                        length_after_mm=member_lengths[nid],
                        message=(
                            f"Pair ({p_id}, {n_id}) lane already matched: "
                            f"|delta|={abs(delta):.4f}mm <= tol={tolerance_mm:.4f}mm"
                        ),
                    ),
                )
            continue

        # Lane is LONGER than reference -- cannot shorten.
        if delta < 0:
            for nid in (p_id, n_id):
                results[nid] = (
                    routes_by_net[nid],
                    TuneResult(
                        success=True,
                        reason="longer_than_reference",
                        length_before_mm=member_lengths[nid],
                        length_after_mm=member_lengths[nid],
                        message=(
                            f"Pair ({p_id}, {n_id}) lane is longer than "
                            f"reference ({lane_length:.4f}mm > "
                            f"{ref_length:.4f}mm); tuner cannot remove length."
                        ),
                    ),
                )
            continue

        # --- Cascade loop on this pair ------------------------------
        current_p = p_route
        current_n = n_route
        current_lane = lane_length
        current_skew = abs(delta)

        # Build the all-group-member net id set for the DRC self-check.
        group_net_ids: set[int] = set(group.net_ids)
        for ip, in_ in group.pair_ids:
            group_net_ids.add(ip)
            group_net_ids.add(in_)

        per_pair_result_p = TuneResult(length_before_mm=member_lengths[p_id])
        per_pair_result_n = TuneResult(length_before_mm=member_lengths[n_id])

        # Group-level ceiling check.
        if total_inserts_committed >= MAX_TOTAL_INSERTS_PER_GROUP:
            for r in (per_pair_result_p, per_pair_result_n):
                r.reason = "cascade_budget_exhausted"
                r.length_after_mm = member_lengths[p_id if r is per_pair_result_p else n_id]
                r.message = (
                    f"Group {group.name!r} cumulative budget "
                    f"({MAX_TOTAL_INSERTS_PER_GROUP}) exhausted before "
                    f"tuning pair ({p_id}, {n_id}); no insertion attempted."
                )
            results[p_id] = (original_p_route, per_pair_result_p)
            results[n_id] = (original_n_route, per_pair_result_n)
            continue

        # The pair-aware target on each half is the reference lane
        # length: we want both halves to end up at ``ref_length`` after
        # the mirrored serpentine raises BOTH by the same amount.
        target_length = ref_length

        # Inner attempt loop (mirrors Phase 2E single-ended structure
        # but operates on the pair as a single logical "member").
        for attempt in range(max_inserts_per_member):
            per_pair_result_p.attempts = attempt + 1
            per_pair_result_n.attempts = attempt + 1

            # Re-check the cascade exit condition BEFORE attempting a
            # new insertion.  ``add_serpentine`` returns the FULL route
            # segments as ``new_segments`` when the P-length already
            # meets the target (the "Route already meets target length"
            # early return on serpentine.py:409-414).  Splicing the FULL
            # P-route segments into N would catastrophically expand N's
            # length, which is exactly the multiply-during-cascade bug
            # the Phase 2F drift-prevention tests guard against.
            current_p_length = LengthTracker.calculate_route_length(current_p)
            if current_p_length >= target_length - 1e-9:
                # P-side already at-or-past target; lane average may be
                # off but no further insertion is meaningful.
                if per_pair_result_p.inserts_applied > 0:
                    for r in (per_pair_result_p, per_pair_result_n):
                        r.success = current_skew <= tolerance_mm
                        r.reason = "tuned" if r.success else "exceeded_max_inserts"
                break

            if total_inserts_committed >= MAX_TOTAL_INSERTS_PER_GROUP:
                for r in (per_pair_result_p, per_pair_result_n):
                    r.reason = "cascade_budget_exhausted"
                    r.message = (
                        f"Group {group.name!r} cumulative budget "
                        f"({MAX_TOTAL_INSERTS_PER_GROUP}) exhausted during "
                        f"tuning of pair ({p_id}, {n_id})."
                    )
                current_p = original_p_route
                current_n = original_n_route
                break

            # --- Step 1: pick a P-side segment.
            provisional = SerpentineGenerator(base_config)
            best = provisional.find_best_segment(current_p)
            if best is None:
                for r in (per_pair_result_p, per_pair_result_n):
                    r.reason = "no_suitable_segment"
                    r.message = (
                        f"No segment long enough for a trombone on net "
                        f"{p_id} (pair lane skew={current_skew:.4f}mm > "
                        f"tol={tolerance_mm:.4f}mm)"
                    )
                current_p = original_p_route
                current_n = original_n_route
                break
            _p_seg_idx, p_insertion_segment = best

            # --- Step 2: find the corresponding N-side segment.
            n_corr = _find_corresponding_n_segment(current_n, p_insertion_segment)
            if n_corr is None:
                for r in (per_pair_result_p, per_pair_result_n):
                    r.reason = "no_suitable_segment"
                    r.message = (
                        f"No corresponding N-side segment found for the "
                        f"P-side insertion segment on pair ({p_id}, {n_id})."
                    )
                current_p = original_p_route
                current_n = original_n_route
                break
            n_insertion_seg_idx, n_insertion_segment = n_corr

            # --- Step 3: pair centerline midpoint + outer-normal hint.
            cx, cy = _pair_centerline_midpoint(p_insertion_segment, n_insertion_segment)
            other_member_routes: dict[int, Route] = {}
            for other_id in group_net_ids:
                if other_id in (p_id, n_id):
                    continue
                if other_id in routes_by_net:
                    other_member_routes[other_id] = routes_by_net[other_id]
            hint = _outer_normal_hint_pair_group(
                p_insertion_segment,
                n_insertion_segment,
                candidate_p_id=p_id,
                candidate_n_id=n_id,
                group_routes=other_member_routes,
            )

            # --- Step 4: generate P-side meander.
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
            candidate_p_route, p_serp_result = attempt_generator.add_serpentine(
                current_p, target_length
            )
            per_pair_result_p.serpentine_results.append(p_serp_result)
            per_pair_result_n.serpentine_results.append(p_serp_result)

            if not p_serp_result.success:
                for r in (per_pair_result_p, per_pair_result_n):
                    r.reason = "no_suitable_segment"
                    r.message = (
                        f"P-side trombone generation failed for pair "
                        f"({p_id}, {n_id}): {p_serp_result.message}"
                    )
                current_p = original_p_route
                current_n = original_n_route
                break

            # --- Step 5: mirror P-side segments to N-side.
            new_n_segments = _mirror_segments_about_centerline(
                p_serp_result.new_segments,
                p_net_id=p_id,
                n_net_id=n_id,
                n_net_name=current_n.net_name,
                cx=cx,
                cy=cy,
                nx=hint[0],
                ny=hint[1],
                grid_resolution_mm=grid_resolution_mm,
            )
            candidate_n_route = _splice_mirrored_n_route(
                current_n,
                n_insertion_seg_index=n_insertion_seg_idx,
                mirrored_segments=new_n_segments,
            )

            # --- Step 6: paired DRC self-check.
            if not _post_insertion_clearance_ok_pair_group(
                new_p_segments=p_serp_result.new_segments,
                new_n_segments=new_n_segments,
                candidate_p_id=p_id,
                candidate_n_id=n_id,
                group_net_ids=group_net_ids,
                routes_by_net=routes_by_net,
                intra_group_clearance_mm=intra_group_clearance_mm,
                intra_pair_clearance_mm=intra_pair_clearance_mm,
            ):
                # Rollback BOTH halves atomically -- the drift-prevention
                # invariant that Phase 2F AC #4 tests.
                for r in (per_pair_result_p, per_pair_result_n):
                    r.reason = "post_insertion_drc_violation"
                    r.length_after_mm = member_lengths[p_id if r is per_pair_result_p else n_id]
                    r.message = (
                        f"Pair-aware DRC self-check failed on pair "
                        f"({p_id}, {n_id}) "
                        f"(intra_group={intra_group_clearance_mm:.4f}mm, "
                        f"intra_pair={intra_pair_clearance_mm:.4f}mm); "
                        "rolled back."
                    )
                current_p = original_p_route
                current_n = original_n_route
                assert current_p is original_p_route
                assert current_p.segments is original_p_segments
                assert current_n is original_n_route
                assert current_n.segments is original_n_segments
                break

            # Commit BOTH halves atomically.
            current_p = candidate_p_route
            current_n = candidate_n_route
            per_pair_result_p.inserts_applied += 1
            per_pair_result_n.inserts_applied += 1
            total_inserts_committed += 1

            new_p_length = LengthTracker.calculate_route_length(current_p)
            new_n_length = LengthTracker.calculate_route_length(current_n)
            current_lane = (new_p_length + new_n_length) / 2.0
            current_skew = abs(target_length - current_lane)

            if current_skew <= tolerance_mm:
                for r in (per_pair_result_p, per_pair_result_n):
                    r.success = True
                    r.reason = "tuned"
                break
        else:
            for r in (per_pair_result_p, per_pair_result_n):
                r.reason = r.reason or "exceeded_max_inserts"

        for r, nid, route in (
            (per_pair_result_p, p_id, current_p),
            (per_pair_result_n, n_id, current_n),
        ):
            if r.reason in ("", "exceeded_max_inserts"):
                r.message = r.message or (
                    f"Cascade budget exhausted on pair ({p_id}, {n_id}) "
                    f"(attempts={r.attempts}, "
                    f"inserts_applied={r.inserts_applied}, "
                    f"lane_skew={current_skew:.4f}mm vs "
                    f"tol={tolerance_mm:.4f}mm)"
                )
            if r.inserts_applied > 0:
                r.length_after_mm = LengthTracker.calculate_route_length(route)
            else:
                r.length_after_mm = member_lengths[nid]

        # Drift-prevention: rollback / no inserts -> route IS original.
        if per_pair_result_p.inserts_applied == 0:
            assert current_p is original_p_route
            assert current_p.segments is original_p_segments
            assert current_n is original_n_route
            assert current_n.segments is original_n_segments

        results[p_id] = (current_p, per_pair_result_p)
        results[n_id] = (current_n, per_pair_result_n)

    # --- Scalar pass: tune scalar members in net_ids -------------------
    # Mixed-group case (Phase 2F AC #5): scalar members are handled by
    # delegating to the Phase 2E single-ended path on a virtual group
    # containing ONLY the scalar members.  The reference is the same
    # ref_length we resolved above.  Note we re-measure inside the
    # single-ended path so routes that were mutated by the pair-aware
    # sweep are reflected in the scalar tuner's view.
    if group.net_ids:
        from .match_group_length import MatchGroup as _MatchGroup

        scalar_group = _MatchGroup(
            name=group.name + "__scalars",
            net_ids=list(group.net_ids),
            pair_ids=[],  # explicit scalar-only sub-group
            tolerance=group.tolerance,
            reference_net_id=group.reference_net_id
            if group.reference_net_id in group.net_ids
            else None,
            source=group.source,
        )
        # If the reference is a paired half, we need to set up the
        # scalar tuner with a *synthetic* reference length matching the
        # ref_length we already resolved.  Easiest way: leave
        # reference_net_id=None (longest-in-scalars policy) and add a
        # post-hoc filter that respects the global ref_length.
        # Simpler still: pass the scalars through the single-ended path
        # with their own reference resolution; the lane reference may
        # disagree slightly but for the canonical "scalar clock as
        # reference, paired data lanes match the clock" use case
        # (Phase 2F AC #5) the scalar IS the reference and the
        # paired lanes already pulled their lane average to match.
        scalar_results = _tune_match_group_single_ended(
            scalar_group,
            routes_by_net,
            tolerance_mm=tolerance_mm,
            intra_group_clearance_mm=intra_group_clearance_mm,
            config=config,
            max_inserts_per_member=max_inserts_per_member,
            length_critical=True,
        )
        for nid, (r_route, r_result) in scalar_results.items():
            results[nid] = (r_route, r_result)

    return results


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
