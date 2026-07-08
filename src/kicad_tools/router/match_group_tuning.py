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

* **Per-insertion DRC self-check.**  Five-pronged (extended in Issue
  #3317 follow-up to catch broader-DRC violations the legacy
  two-pass check missed):

  1. **Intra-group** -- every new serpentine segment is checked against
     every segment of every *other* group member at threshold
     ``intra_group_clearance_mm``.
  2. **Inter-net** -- every new segment is checked against every segment
     of every routed net that is NOT a group member at the same
     threshold.
  3. **Segment-vs-via** (Issue #3317 follow-up) -- every new segment is
     checked against every via of every OTHER routed net at threshold
     ``via_clearance_mm``.  Catches the board-07 ``[via] DM0 vs DQ6``
     class of underflow that segment-only checks miss.
  4. **Diff-pair intra-pair** (Issue #3317 follow-up) -- when the
     candidate net has a diff-pair partner in ``diff_pair_partners``,
     every new segment is checked against the partner's segments at
     the (tighter) ``intra_pair_clearance_mm`` threshold.  Catches
     the board-07 ``[segment] TMDS_D0_N vs TMDS_D0_P`` underflow.
  5. **Segment-vs-pad** (Issue #3317 follow-up) -- every new segment
     is checked against every foreign-net pad in ``foreign_pads`` at
     threshold ``pad_clearance_mm``.  Catches the board-07
     ``[pad] A1 vs A2`` underflow.

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

  **Auto-promotion when the pace car routes short (Issue #3440).**
  Strict pace-car semantics are structurally unsatisfiable for a
  lengthen-only tuner whenever ANY member routes longer than the
  declared reference by more than the group tolerance: the reference
  is untouchable and members above it cannot be shortened, so the
  max-min skew can never converge (board 07's ADDR_BUS declared
  ``length_match_reference="A0"`` while A0 routed shortest -> 15.4mm
  of skew left untouched).  In that case the single-ended tuner logs
  a structured warning and falls back to **longest-in-group**
  semantics for the run: the effective reference length becomes the
  longest member's length and every member -- INCLUDING the declared
  pace car -- becomes tunable.  This satisfies the
  ``match_group_length_skew`` DRC rule (which measures max-min
  spread, not distance to the declared reference).  When the declared
  reference IS the longest member (the satisfiable declaration),
  strict pace-car behavior is preserved byte-for-byte.

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

import logging
from collections import Counter
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
    from collections.abc import Iterable

    from .match_group_length import MatchGroup
    from .primitives import Pad, Route, Segment


logger = logging.getLogger(__name__)


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


#: Maximum number of candidate segments to try per cascade attempt before
#: declaring ``post_insertion_drc_violation``.  Issue #3274: the legacy
#: behavior tried ONLY the top-ranked segment (the one returned by
#: :meth:`SerpentineGenerator.find_best_segment`) and immediately rolled
#: back the whole member's cascade on a single DRC failure.  Trying a
#: small fan of next-best segments before giving up costs at most
#: ``MAX_SEGMENT_RETRY_CANDIDATES`` DRC self-checks per attempt but
#: significantly improves the tuner's yield on dense boards where the
#: best segment's outer-normal half-plane is blocked.  Set to 3 so the
#: bounded fan stays cheap (3 * O(N) DRC checks per attempt) while
#: covering the typical "best segment is blocked but the next two are
#: clear" case observed on board 07's ADDR_BUS group.
MAX_SEGMENT_RETRY_CANDIDATES: int = 3


#: Amplitude back-off ladder for distributed meanders (Issue #3440).
#: When the full-amplitude trombone on a candidate segment fails the
#: post-insertion DRC self-check, the tuner retries the SAME segment
#: with progressively smaller bulge amplitudes before moving to the
#: next candidate segment.  Smaller amplitudes produce many small humps
#: instead of one large blob: each loop adds ``2 * amplitude`` of
#: length, so a 15 mm correction at amplitude 1.0 needs ~8 deep loops
#: (which collide with parallel bus neighbors), while the same
#: correction at amplitude 0.25 spreads across ~30 shallow loops that
#: stay inside the candidate's own corridor.  The trade-off is forward
#: room (each loop consumes ``2 * gap`` along the segment), which
#: :meth:`SerpentineGenerator.generate_trombone` already handles by
#: reducing the loop count to fit -- partial corrections are committed
#: and the cascade continues on the next attempt / segment.
#:
#: The 0.125 rung exists for tight parallel-bus corridors: board 07's
#: A2 meander missed the 0.150mm floor against A1 by 8 microns at the
#: 0.25-rung exact-fit amplitude (0.241mm); halving the amplitude again
#: doubles the loop count and pulls the bulge envelope ~0.12mm back
#: into A2's own corridor.
AMPLITUDE_BACKOFF_FACTORS: tuple[float, ...] = (1.0, 0.5, 0.25, 0.125)


# ---------------------------------------------------------------------------
# Reason registry + summary formatting (Issue #3440 observability)
# ---------------------------------------------------------------------------


#: Canonical registry of EVERY value :attr:`TuneResult.reason` can carry.
#: Both summary printers (``Autorouter.apply_match_group_tuning`` per-group
#: line in ``router/core.py`` and the aggregate line in
#: ``cli/route_cmd.py``) derive their buckets from this tuple via
#: :func:`format_reason_counts`, so a future reason value added to the
#: tuner without a registry entry fires the exhaustiveness test in
#: ``tests/test_match_group_tuning.py`` rather than silently vanishing
#: from the logs (the Issue #3440 "all-zeros summary while 15.4mm of
#: skew goes untouched" defect).
TUNE_RESULT_REASONS: tuple[str, ...] = (
    "tuned",
    "already_within_tolerance",
    "reference",
    "longer_than_reference",
    "exceeded_max_inserts",
    "cascade_budget_exhausted",
    "post_insertion_drc_violation",
    "no_suitable_segment",
    "unrouted",
    "not_length_critical",
)


#: Human-readable summary-bucket label for each registered reason.
#: Multiple reasons may share a bucket (the two budget reasons both
#: surface as ``budget-exhausted`` -- preserves the legacy line shape).
REASON_SUMMARY_LABELS: dict[str, str] = {
    "tuned": "tuned",
    "already_within_tolerance": "clean",
    "post_insertion_drc_violation": "rolled back",
    "exceeded_max_inserts": "budget-exhausted",
    "cascade_budget_exhausted": "budget-exhausted",
    "not_length_critical": "skipped",
    "reference": "reference",
    "longer_than_reference": "longer-than-ref",
    "unrouted": "unrouted",
    "no_suitable_segment": "no-segment",
}

#: Buckets that are ALWAYS printed (the legacy five) in canonical order.
_ALWAYS_SHOWN_BUCKETS: tuple[str, ...] = (
    "tuned",
    "clean",
    "rolled back",
    "budget-exhausted",
    "skipped",
)

#: Buckets printed only when non-zero, in canonical order.  These are
#: the reason buckets that were previously counted by NOTHING (Issue
#: #3440 root cause): ``reference`` / ``longer_than_reference`` /
#: ``unrouted`` plus ``no_suitable_segment``.
_OPTIONAL_BUCKETS: tuple[str, ...] = (
    "reference",
    "longer-than-ref",
    "unrouted",
    "no-segment",
)


def format_reason_counts(reasons: Iterable[str]) -> str:
    """Format per-member tuner outcomes as a summary fragment.

    Counts EVERY member: the five legacy buckets (``tuned`` / ``clean``
    / ``rolled back`` / ``budget-exhausted`` / ``skipped``) always
    appear; the remaining registered buckets (``reference``,
    ``longer-than-ref``, ``unrouted``, ``no-segment``) appear when
    non-zero; any UNREGISTERED reason value appears as
    ``N other(<reason>)`` so no member can ever fall into a silent
    bucket (Issue #3440 observability AC).

    Args:
        reasons: ``TuneResult.reason`` values, one per group member.

    Returns:
        A comma-joined fragment such as
        ``"2 tuned, 0 clean, 5 rolled back, 0 budget-exhausted, 0
        skipped, 1 reference"``.  The sum of all displayed counts
        equals the number of input reasons.
    """
    bucket_counts: Counter[str] = Counter()
    other_counts: Counter[str] = Counter()
    for reason in reasons:
        label = REASON_SUMMARY_LABELS.get(reason)
        if label is None:
            other_counts[reason] += 1
        else:
            bucket_counts[label] += 1

    parts: list[str] = [f"{bucket_counts.get(label, 0)} {label}" for label in _ALWAYS_SHOWN_BUCKETS]
    parts.extend(
        f"{bucket_counts[label]} {label}"
        for label in _OPTIONAL_BUCKETS
        if bucket_counts.get(label, 0) > 0
    )
    parts.extend(f"{count} other({reason})" for reason, count in sorted(other_counts.items()))
    return ", ".join(parts)


def group_skew_before_after(
    lengths: Iterable[tuple[float, float]],
) -> tuple[float, float] | None:
    """Compute a group's length-skew before and after tuning.

    Issue #3924 AC2.  A match group's skew is the ``max(L) - min(L)`` spread
    across its members' routed lengths.  Given each member's
    ``(length_before_mm, length_after_mm)`` (from :attr:`TuneResult`), this
    returns ``(skew_before_mm, skew_after_mm)`` so the tuner's verbose
    summary can report the achieved improvement per group.

    Members whose *before* and *after* lengths are both ``0.0`` (unrouted
    placeholders) are excluded from the spread -- an unrouted member has no
    measured length and would otherwise drag the min to 0 and inflate the
    reported skew.

    Args:
        lengths: Iterable of ``(before_mm, after_mm)`` pairs, one per group
            member.

    Returns:
        ``(skew_before_mm, skew_after_mm)`` rounded to 4 decimals, or
        ``None`` when fewer than two members carry a measured length (skew
        is undefined for a single trace).
    """
    befores: list[float] = []
    afters: list[float] = []
    for before, after in lengths:
        if before == 0.0 and after == 0.0:
            continue
        befores.append(before)
        afters.append(after)
    if len(befores) < 2:
        return None
    skew_before = max(befores) - min(befores)
    skew_after = max(afters) - min(afters)
    return round(skew_before, 4), round(skew_after, 4)


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
    via_clearance_mm: float | None = None,
    diff_pair_partners: dict[int, int] | None = None,
    pads_by_net: dict[int, list[Pad]] | None = None,
    pad_clearance_mm: float | None = None,
    board_thickness_mm: float | None = None,
    num_copper_layers: int = 4,
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
            non-group nets.  **Updated in place** (Issue #3440): when a
            member commits one or more meanders, its entry is replaced
            with the tuned Route so subsequent members' self-checks see
            the committed geometry -- without this, two adjacent
            members could stack meanders on top of each other (each
            checked only against the other's pre-meander route).
            Untouched members' entries are never replaced.
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
        via_clearance_mm: Optional segment-to-via clearance floor in mm
            (Issue #3317 follow-up).  When supplied, the post-insertion
            DRC self-check additionally rejects inserts whose new
            segments come within ``via_clearance_mm`` of any other
            net's existing via.  Typical value:
            ``DesignRules.via_clearance`` (0.2 mm).  When ``None``
            (the default) the legacy segment-only check applies --
            preserves byte-for-byte behavior for tests / callers that
            don't supply the threshold.
        diff_pair_partners: Optional ``{net_id: partner_net_id}`` map
            for differential pairs (Issue #3317 follow-up).  When
            supplied (along with ``intra_pair_clearance_mm``), the
            post-insertion self-check additionally rejects inserts
            whose new segments come within
            ``intra_pair_clearance_mm`` of the candidate's diff-pair
            partner.  Skipped when omitted.  Only used by the
            single-ended (Phase 2E) path; the pair-aware path already
            handles intra-pair via :func:`_post_insertion_clearance_ok_pair_group`.
        pads_by_net: Optional ``{net_id: [Pad, ...]}`` map for the
            segment-vs-pad clearance pass (Issue #3317 follow-up).
            When supplied (along with ``pad_clearance_mm``), the
            post-insertion self-check additionally rejects inserts
            whose new segments come within ``pad_clearance_mm`` of
            ANY foreign-net pad.  Caller responsibility to populate
            from the autorouter's ``self.pads`` state.  Skipped when
            omitted (legacy behavior).
        pad_clearance_mm: Optional segment-to-pad clearance floor in
            mm (Issue #3317 follow-up).  Required when ``pads_by_net``
            is non-empty.  Typical value:
            ``DesignRules.trace_clearance`` (0.2 mm for JLCPCB).
        board_thickness_mm: Total stackup thickness in mm (Issue #3931).
            When supplied, member lengths are measured VIA-INCLUSIVELY
            (planar copper length + per-via drilled length) so the tuner
            compensates for via-count imbalance -- a member that escapes
            to an inner layer behind a full-stack via becomes the de-facto
            reference and the copper-only members receive F.Cu meander to
            match its drilled length.  When ``None`` (the default) vias
            contribute ``0.0`` and the measurement collapses to the legacy
            planar-only sum -- byte-for-byte prior behavior for callers
            without a stackup context.  Only the single-ended (Phase 2E)
            path is via-aware; the pair-aware path is unchanged.
        num_copper_layers: Number of copper layers in the stack
            (Issue #3931).  Used with ``board_thickness_mm`` to compute
            per-via drilled length.  Defaults to 4; ignored when
            ``board_thickness_mm`` is ``None``.

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
        via_clearance_mm=via_clearance_mm,
        diff_pair_partners=diff_pair_partners,
        intra_pair_clearance_mm=intra_pair_clearance_mm,
        pads_by_net=pads_by_net,
        pad_clearance_mm=pad_clearance_mm,
        board_thickness_mm=board_thickness_mm,
        num_copper_layers=num_copper_layers,
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
    via_clearance_mm: float | None = None,
    diff_pair_partners: dict[int, int] | None = None,
    intra_pair_clearance_mm: float | None = None,
    pads_by_net: dict[int, list[Pad]] | None = None,
    pad_clearance_mm: float | None = None,
    board_thickness_mm: float | None = None,
    num_copper_layers: int = 4,
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
    from .match_group_length import MatchGroupTracker  # avoid cycle

    # Issue #3931: measure member lengths VIA-INCLUSIVELY so the tuner
    # targets the same skew the ``match_group_length_skew`` DRC rule
    # measures.  ``LengthTracker.calculate_route_length`` sums only planar
    # (copper) segment length -- it is via-blind.  When a match group has
    # via-count imbalance (board 07 ADDR_BUS: A4/A6 escape to an inner
    # layer behind a full-stack via while A0-A3/A5/A7 route flat on F.Cu),
    # the copper lengths are within tolerance but the via drilled length
    # (~1.6mm for two full-stack vias on a 1.6mm / 4-layer stack) pushes
    # the group over its skew tolerance.  A planar-only tuner sees the
    # members as already matched and does nothing.  Delegating to
    # ``MatchGroupTracker._measure_route_total`` (which adds the per-via
    # drilled length when ``board_thickness_mm`` is supplied) makes the
    # tuner via-aware: the via-carrying members become the de-facto
    # reference and the copper-only members get ~1.6mm of F.Cu meander to
    # compensate their missing drilled length.  When ``board_thickness_mm``
    # is ``None`` (legacy callers with no stackup context) the measurement
    # collapses to the planar-only sum -- byte-for-byte prior behavior.
    def _measure(route: Route) -> float:
        return MatchGroupTracker._measure_route_total(route, board_thickness_mm, num_copper_layers)

    member_lengths: dict[int, float] = {}
    for net_id in group.net_ids:
        route = routes_by_net.get(net_id)
        if route is None:
            continue
        member_lengths[net_id] = _measure(route)

    # Resolve the reference length per policy.
    ref_length: float | None
    if group.reference_net_id is not None:
        ref_length = member_lengths.get(group.reference_net_id)
    else:
        # Longest-in-group default.
        ref_length = max(member_lengths.values()) if member_lengths else None

    # --- Issue #3440: auto-promote when the pace car is not the longest -
    # The tuner is lengthen-only.  When the declared reference routes
    # SHORTER than another member (by more than tolerance), strict
    # pace-car semantics are structurally unsatisfiable: the reference
    # is untouchable and the over-length members cannot be shortened,
    # so the group's max-min skew can never reach tolerance (board 07
    # ADDR_BUS, A0 declared as reference but routed shortest -> 15.4mm
    # skew left untouched).  Policy decision (curator option (a)): log
    # a structured warning and fall back to longest-in-group semantics
    # -- the effective reference length becomes the longest member's
    # length and EVERY member (including the declared pace car) becomes
    # tunable.  This satisfies the DRC ``match_group_length_skew`` rule,
    # which measures max-min spread, not distance-to-declared-reference.
    #
    # When the declared reference is longer than (or within tolerance
    # of) every member, strict pace-car semantics are preserved
    # byte-for-byte: the reference returns ``reason="reference"`` and
    # members within tolerance above it return
    # ``reason="longer_than_reference"``.
    effective_reference_net_id: int | None = group.reference_net_id
    if group.reference_net_id is not None and ref_length is not None and member_lengths:
        longest_id, longest_len = max(member_lengths.items(), key=lambda kv: kv[1])
        if longest_len > ref_length + tolerance_mm:
            logger.warning(
                "[match_group] group %r: declared reference net %s (%.3fmm) "
                "is shorter than member net %s (%.3fmm) by more than the "
                "%.3fmm tolerance.  The lengthen-only tuner cannot satisfy "
                "a shortest-member pace car; auto-promoting longest-in-group "
                "as the effective reference (the declared reference will be "
                "lengthened toward %.3fmm).",
                group.name,
                group.reference_net_id,
                ref_length,
                longest_id,
                longest_len,
                tolerance_mm,
                longest_len,
            )
            effective_reference_net_id = None
            ref_length = longest_len

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
        # (After Issue #3440 auto-promotion the effective reference may
        # be ``None`` even when ``group.reference_net_id`` is set -- the
        # declared reference is then tuned like any other member.)
        if effective_reference_net_id == net_id:
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

        # Issue #3440: normalize the working geometry by merging
        # adjacent collinear segments BEFORE ranking host candidates.
        # The tuner runs before ``_finalize_routes`` cleanup, so an A*
        # staircase route arrives as hundreds of grid-step micro
        # segments -- none long enough to host a trombone -- even
        # though the merged geometry has multi-mm straight runs (board
        # 07's A3 reported ``no_suitable_segment`` at 7mm of residual
        # skew for exactly this reason).  The merge is geometry- and
        # length-preserving (collinear + connected runs only), so the
        # member's measured length is unchanged.  Rollback paths still
        # return the ORIGINAL route reference; the merged route is only
        # ever returned when at least one insert commits.
        if len(route.segments) > 1:
            from .optimizer.algorithms import merge_collinear as _merge_collinear
            from .optimizer.config import OptimizationConfig as _OptConfig
            from .primitives import Route as _RouteForMerge

            merged_segments = _merge_collinear(route.segments, _OptConfig())
            if len(merged_segments) < len(route.segments):
                current_route = _RouteForMerge(
                    net=route.net,
                    net_name=route.net_name,
                    segments=merged_segments,
                    vias=route.vias.copy(),
                )

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

        # Each iteration: rank candidate segments, then for each (best
        # first) compute the outer-normal hint, attempt one trombone,
        # run the self-check.  Commit the FIRST candidate that passes
        # DRC.  If all candidates fail, declare
        # ``post_insertion_drc_violation`` for the whole member.
        #
        # Issue #3274 change: previously a single DRC failure on the
        # top-ranked segment terminated the whole member's cascade.  We
        # now try up to :data:`MAX_SEGMENT_RETRY_CANDIDATES` segments
        # per attempt before rolling back -- this is the curator's
        # "Option A: per-segment retry" recipe.
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

            # Rank up to MAX_SEGMENT_RETRY_CANDIDATES candidate
            # segments for this attempt.  The top-ranked candidate is
            # byte-for-byte what ``find_best_segment`` would return.
            host_floor = base_config.min_segment_length
            candidates = _rank_candidate_segments(
                current_route,
                min_segment_length=host_floor,
                max_candidates=MAX_SEGMENT_RETRY_CANDIDATES,
            )
            if not candidates:
                # Issue #3440 fallback: heavily-jogged routes (post
                # rip-up A* staircases) may have no segment at the
                # standard host length even after the collinear merge.
                # Retry at the geometric minimum hostable length -- the
                # shortest segment on which ONE trombone loop fits
                # (entry + 2 forward gaps + exit within the 90% margin
                # the generator enforces).
                gap_mm = base_config.min_spacing * base_config.gap_factor
                fallback_floor = (3.0 * gap_mm) / 0.9 + 1e-6
                if fallback_floor < host_floor:
                    host_floor = fallback_floor
                    candidates = _rank_candidate_segments(
                        current_route,
                        min_segment_length=host_floor,
                        max_candidates=MAX_SEGMENT_RETRY_CANDIDATES,
                    )
            if not candidates:
                per_member_result.reason = "no_suitable_segment"
                per_member_result.message = (
                    f"No segment long enough for a trombone on net {net_id} "
                    f"(skew={current_skew:.4f}mm > tol={tolerance_mm:.4f}mm)"
                )
                current_route = original_route
                break

            # Precompute the length needed (constant across this
            # attempt's per-segment retries).
            current_length_for_attempt = _measure(current_route)
            length_needed = target_length - current_length_for_attempt
            if length_needed <= 0:
                # Already at/over target -- nothing to add this attempt.
                # This should normally be caught by the success branch
                # at the end of the previous iteration; defensive.
                per_member_result.success = True
                per_member_result.reason = per_member_result.reason or "tuned"
                break

            # Per-attempt outer-normal exclusion set (same for every
            # candidate segment within an attempt -- it depends on the
            # candidate net id, not the chosen segment).
            other_group_routes = {
                other_id: routes_by_net[other_id]
                for other_id in group.net_ids
                if other_id != net_id and other_id in routes_by_net
            }

            # Try the candidates in rank order.  Commit the first one
            # whose trombone passes the post-insertion DRC self-check.
            # Track the LAST failure reason so we can surface a
            # diagnostic if every candidate fails.
            committed_this_attempt = False
            last_failure_reason = ""
            last_failure_message = ""

            # Compute foreign pads (every pad whose net != net_id) for
            # the segment-vs-pad clearance pass.  When ``pads_by_net``
            # is omitted (legacy callers / unit tests), the pass is
            # skipped silently inside the helper.  Constant across the
            # candidate x amplitude fan.
            foreign_pads: list[Pad] | None = None
            if pads_by_net is not None:
                foreign_pads = []
                for other_net_id, pads in pads_by_net.items():
                    if other_net_id == net_id:
                        continue
                    foreign_pads.extend(pads)

            for _cand_idx, (seg_idx, insertion_segment) in enumerate(candidates):
                # Outer-normal hint vs the NEAREST other group member.
                hint = _outer_normal_hint_group(
                    insertion_segment,
                    candidate_net_id=net_id,
                    group_routes=other_group_routes,
                )

                # Issue #3440 distributed meanders: try the candidate
                # segment with a descending amplitude ladder.  A large
                # correction (e.g. board 07's 15.4mm ADDR_BUS skew) at
                # the base amplitude produces one tall blob that fails
                # the DRC self-check against parallel bus neighbors;
                # the same correction at a smaller amplitude spreads
                # into many shallow humps that stay inside the
                # candidate's own corridor.
                for amp_factor in AMPLITUDE_BACKOFF_FACTORS:
                    import math as _math

                    # Exact-fit amplitude (Issue #3440): the trombone
                    # generator adds length in ``2 * amplitude`` quanta
                    # (``num_loops = ceil(needed / (2 * amplitude))``),
                    # so a fixed ladder amplitude overshoots the target
                    # by up to ``2 * amplitude - epsilon`` (board 07's
                    # A6 landed 1.8mm past the reference at the 1.0mm
                    # base amplitude).  Pick the loop count at the
                    # ladder amplitude, then shrink the amplitude so
                    # those loops add EXACTLY the needed length.  The
                    # tiny relative bump keeps the generator's own
                    # ceil() from rounding up to one extra loop.
                    ladder_amplitude = base_config.amplitude * amp_factor
                    planned_loops = max(1, _math.ceil(length_needed / (2.0 * ladder_amplitude)))
                    exact_amplitude = (length_needed / (2.0 * planned_loops)) * (1.0 + 1e-9)

                    # Build the per-(candidate, amplitude) config.
                    attempt_config = SerpentineConfig(
                        style=base_config.style,
                        amplitude=exact_amplitude,
                        min_spacing=base_config.min_spacing,
                        min_segment_length=host_floor,
                        gap_factor=base_config.gap_factor,
                        max_iterations=base_config.max_iterations,
                        side="outer",
                        outer_normal_hint=hint,
                    )
                    attempt_generator = SerpentineGenerator(attempt_config)

                    # Generate the trombone on THIS specific segment
                    # (not the one ``find_best_segment`` would pick --
                    # we splice by hand so the per-segment retry
                    # actually tries a different segment).
                    serp_result = attempt_generator.generate_trombone(
                        insertion_segment, length_needed
                    )
                    per_member_result.serpentine_results.append(serp_result)

                    if not serp_result.success:
                        last_failure_reason = "no_suitable_segment"
                        last_failure_message = (
                            f"Trombone generation failed on net {net_id} "
                            f"segment {seg_idx}: {serp_result.message}"
                        )
                        # Generation failure means the segment is too
                        # short to host ANY trombone -- a smaller
                        # amplitude cannot fix that; move to the next
                        # candidate segment.
                        break

                    # Splice the generated trombone into the route at
                    # the chosen segment index (mirrors
                    # ``SerpentineGenerator.add_serpentine`` verbatim).
                    new_segments_full = (
                        current_route.segments[:seg_idx]
                        + serp_result.new_segments
                        + current_route.segments[seg_idx + 1 :]
                    )
                    from .primitives import Route as _Route

                    candidate_route = _Route(
                        net=current_route.net,
                        net_name=current_route.net_name,
                        segments=new_segments_full,
                        vias=current_route.vias.copy(),
                    )

                    # Post-insertion DRC self-check.  Issue #3317
                    # follow-up: also check segment-vs-via at
                    # ``via_clearance_mm``, diff-pair intra-pair at
                    # ``intra_pair_clearance_mm``, and segment-vs-pad
                    # at ``pad_clearance_mm`` so inserts that would
                    # fail the broader DRC validator are rejected at
                    # insertion time.  Issue #3440: the detail variant
                    # names WHICH rule / neighbor rejected the
                    # candidate meander so rollbacks are actionable.
                    drc_detail = _post_insertion_clearance_detail_group(
                        new_segments=serp_result.new_segments,
                        candidate_net_id=net_id,
                        group_net_ids=set(group.net_ids),
                        routes_by_net=routes_by_net,
                        intra_group_clearance_mm=intra_group_clearance_mm,
                        via_clearance_mm=via_clearance_mm,
                        diff_pair_partners=diff_pair_partners,
                        intra_pair_clearance_mm=intra_pair_clearance_mm,
                        foreign_pads=foreign_pads,
                        pad_clearance_mm=pad_clearance_mm,
                    )
                    if drc_detail is not None:
                        last_failure_reason = "post_insertion_drc_violation"
                        last_failure_message = (
                            f"Candidate meander rejected on net {net_id} "
                            f"segment {seg_idx} (amplitude "
                            f"{attempt_config.amplitude:.3f}mm): {drc_detail}"
                        )
                        continue  # try the next (smaller) amplitude

                    # Commit this candidate.
                    current_route = candidate_route
                    per_member_result.inserts_applied += 1
                    total_inserts_committed += 1
                    committed_this_attempt = True

                    new_length = _measure(current_route)
                    current_skew = abs(target_length - new_length)

                    if current_skew <= tolerance_mm:
                        per_member_result.success = True
                        per_member_result.reason = "tuned"
                    break  # exit amplitude ladder on successful commit

                if committed_this_attempt:
                    break  # exit candidate loop on successful commit

            if not committed_this_attempt:
                # Every candidate in this attempt failed.  Surface the
                # LAST failure reason (typically
                # ``post_insertion_drc_violation``) and roll back the
                # whole member -- the cascade for this member is done.
                per_member_result.reason = last_failure_reason or "post_insertion_drc_violation"
                per_member_result.length_after_mm = current_length
                # Issue #3440 capacity diagnosis: name the shortfall
                # (requested mm vs achieved mm) so a rollback is
                # actionable rather than a bare count.
                achieved_mm = _measure(current_route) - current_length
                capacity_note = (
                    f" Capacity: requested {abs(delta):.3f}mm of added length, "
                    f"achieved {achieved_mm:.3f}mm before giving up "
                    f"(residual skew {current_skew:.3f}mm vs tol "
                    f"{tolerance_mm:.3f}mm)."
                )
                per_member_result.message = (
                    last_failure_message
                    or (
                        f"All {len(candidates)} candidate segments failed DRC "
                        f"on net {net_id} (intra={intra_group_clearance_mm:.4f}mm); "
                        "rolled back."
                    )
                ) + capacity_note
                # Byte-for-byte rollback to the ORIGINAL route -- we
                # discard any commits made earlier in this cascade so
                # the drift-prevention invariant
                # ``inserts_applied == 0 -> route is original`` is
                # honored for the "no-progress" case.  When
                # ``inserts_applied > 0`` we keep the partial progress
                # and surface ``post_insertion_drc_violation`` as the
                # last-attempt reason.
                if per_member_result.inserts_applied == 0:
                    current_route = original_route
                    assert current_route is original_route
                    assert current_route.segments is original_segments
                break

            if per_member_result.reason == "tuned":
                break
        else:
            # for/else: completed max_inserts_per_member without break.
            per_member_result.reason = per_member_result.reason or "exceeded_max_inserts"

        if per_member_result.reason in ("", "exceeded_max_inserts"):
            per_member_result.message = per_member_result.message or (
                f"Cascade budget exhausted on net {net_id} "
                f"(attempts={per_member_result.attempts}, "
                f"inserts_applied={per_member_result.inserts_applied}, "
                f"skew={current_skew:.4f}mm vs tol={tolerance_mm:.4f}mm; "
                f"capacity: requested {abs(delta):.3f}mm, achieved "
                f"{_measure(current_route) - current_length:.3f}mm)"
            )

        # Final length.
        if per_member_result.inserts_applied > 0:
            per_member_result.length_after_mm = _measure(current_route)
        else:
            per_member_result.length_after_mm = current_length

        # Drift-prevention: when rolled back / no inserts committed, the
        # returned route reference IS the original route reference.
        if per_member_result.inserts_applied == 0:
            assert current_route is original_route
            assert current_route.segments is original_segments

        results[net_id] = (current_route, per_member_result)

        # Issue #3440 staleness fix: publish the tuned route into
        # ``routes_by_net`` so SUBSEQUENT members' post-insertion DRC
        # self-checks see the committed meander geometry.  Without this,
        # member k+1 is checked against member k's PRE-meander route and
        # two adjacent meanders can be committed on top of each other
        # (the board 07 ``A2 vs A1`` clearance underflows at the
        # serpentine corners).  The orchestrator's own commit-back loop
        # (``Autorouter.apply_match_group_tuning``) is idempotent with
        # this in-place update.
        if per_member_result.inserts_applied > 0:
            routes_by_net[net_id] = current_route

    return results


# ---------------------------------------------------------------------------
# Candidate-segment ranking (Issue #3274 -- per-segment retry)
# ---------------------------------------------------------------------------


def _rank_candidate_segments(
    route: Route,
    min_segment_length: float,
    max_candidates: int = MAX_SEGMENT_RETRY_CANDIDATES,
) -> list[tuple[int, Segment]]:
    """Return up to ``max_candidates`` segments ranked for trombone insertion.

    Issue #3274 generalization of
    :meth:`~kicad_tools.router.optimizer.serpentine.SerpentineGenerator.find_best_segment`
    from "return the single best" to "return the top-K best", so the
    match-group cascade can fall back to the next-best segment when the
    top-ranked one's outer-normal half-plane is blocked by an
    intra-group or neighbor net.

    The score is byte-for-byte equivalent to ``find_best_segment``'s:

    * Skip segments shorter than ``min_segment_length``.
    * Base score = segment length.
    * 1.2x bonus for segments that are NOT at index 0 or
      ``len(route.segments) - 1`` (avoid pad-adjacent segments).
    * 1.5x bonus for segments that are nearly horizontal or vertical
      (``dx/length > 0.95`` OR ``dy/length > 0.95``).

    Returns the top-``max_candidates`` segments sorted by descending
    score.  Ties are broken by ascending segment index (stable sort on
    ``-score``) so the order is deterministic across Python's hashing
    randomization -- critical for board 07's
    ``PYTHONHASHSEED=42`` determinism requirement.

    The first element of the returned list IS what
    :meth:`SerpentineGenerator.find_best_segment` would return (with
    the same tie-break rule); existing callers that take ``result[0]``
    see no behavior change.

    Args:
        route: The route whose segments are being ranked.
        min_segment_length: Minimum segment length (mm) for a segment
            to be a serpentine host.  Matches
            ``SerpentineConfig.min_segment_length``.
        max_candidates: Cap on the returned list length.  Default
            :data:`MAX_SEGMENT_RETRY_CANDIDATES` (3).

    Returns:
        Up to ``max_candidates`` ``(segment_index, Segment)`` tuples,
        sorted by descending score (best first).  Empty list when no
        segment is long enough to host a trombone.
    """
    import math

    if not route.segments:
        return []

    scored: list[tuple[float, int, Segment]] = []
    last_idx = len(route.segments) - 1

    for i, seg in enumerate(route.segments):
        dx_full = seg.x2 - seg.x1
        dy_full = seg.y2 - seg.y1
        length = math.sqrt(dx_full * dx_full + dy_full * dy_full)

        # Skip segments that are too short.
        if length < min_segment_length:
            continue

        score = length

        # Prefer segments not at the start or end (near pads).
        if 0 < i < last_idx:
            score *= 1.2

        # Prefer horizontal or vertical segments.
        dx = abs(dx_full)
        dy = abs(dy_full)
        if length > 0 and (dx / length > 0.95 or dy / length > 0.95):
            score *= 1.5

        scored.append((score, i, seg))

    # Sort by descending score, stable on ascending index for tie-break.
    # Python's sort is stable, so first sort by index (ascending) then
    # by score (descending) gives "highest score, then lowest index".
    scored.sort(key=lambda t: t[1])
    scored.sort(key=lambda t: -t[0])

    return [(idx, seg) for (_score, idx, seg) in scored[:max_candidates]]


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
    via_clearance_mm: float | None = None,
    diff_pair_partners: dict[int, int] | None = None,
    intra_pair_clearance_mm: float | None = None,
    foreign_pads: list[Pad] | None = None,
    pad_clearance_mm: float | None = None,
) -> bool:
    """Boolean wrapper around :func:`_post_insertion_clearance_detail_group`.

    Preserved for callers / tests that only need the pass-fail verdict.
    Returns ``True`` when no clearance violation is introduced.
    """
    return (
        _post_insertion_clearance_detail_group(
            new_segments=new_segments,
            candidate_net_id=candidate_net_id,
            group_net_ids=group_net_ids,
            routes_by_net=routes_by_net,
            intra_group_clearance_mm=intra_group_clearance_mm,
            via_clearance_mm=via_clearance_mm,
            diff_pair_partners=diff_pair_partners,
            intra_pair_clearance_mm=intra_pair_clearance_mm,
            foreign_pads=foreign_pads,
            pad_clearance_mm=pad_clearance_mm,
        )
        is None
    )


def _post_insertion_clearance_detail_group(
    *,
    new_segments: list[Segment],
    candidate_net_id: int,
    group_net_ids: set[int],
    routes_by_net: dict[int, Route],
    intra_group_clearance_mm: float,
    via_clearance_mm: float | None = None,
    diff_pair_partners: dict[int, int] | None = None,
    intra_pair_clearance_mm: float | None = None,
    foreign_pads: list[Pad] | None = None,
    pad_clearance_mm: float | None = None,
) -> str | None:
    """Return ``None`` if the proposed serpentine segments are DRC-safe,
    else a human-readable description of the FIRST violation found.

    Issue #3440: the description names the violated rule (which of the
    five passes), the offending neighbor (net name / via / pad), the
    layer, and the measured-vs-required clearance so a tuner rollback
    in the route log is actionable instead of a bare count.

    Five-pronged generalization of
    :func:`~kicad_tools.router.diffpair_length_tuning._post_insertion_clearance_ok`
    from N=2 (one partner) to N>=3 (the rest of the group + the rest of
    the board), with broader-DRC awareness added in Issue #3317 follow-up
    (judge change-request on PR #3317 Refs #3274):

    1. **Intra-group clearance**: every new segment is checked against
       every segment of every OTHER group member (excluding the
       candidate, whose old segments are being replaced).  Threshold is
       ``intra_group_clearance_mm``.

    2. **Inter-net clearance**: every new segment is checked against
       every segment of every routed net that is NOT a group member.
       Threshold is also ``intra_group_clearance_mm`` as a conservative
       floor (mirrors the pair tuner's single-threshold policy).

    3. **Segment-vs-via clearance** (NEW Issue #3317 follow-up): every
       new segment is checked against every via of every OTHER routed
       net (group members AND non-group neighbors).  Threshold is
       ``via_clearance_mm`` (manufacturer's via_clearance, default
       0.2mm).  This pass is skipped when ``via_clearance_mm`` is
       ``None`` (legacy behavior preserved for unit tests that don't
       supply the threshold).  The judge identified that the legacy
       check only exercised segment-to-segment geometry, so trombone
       inserts could land within ``via_clearance`` of a foreign via
       (e.g., board 07's DM0 vs DQ6 via-pair on In1.Cu) and pass the
       self-check but fail downstream DRC.

    4. **Diff-pair intra-pair clearance** (NEW Issue #3317 follow-up):
       when the candidate net is half of a differential pair AND its
       partner net is routed, every new segment is checked against the
       PARTNER's segments at the (tighter) ``intra_pair_clearance_mm``
       threshold.  This pass is skipped when either
       ``diff_pair_partners`` or ``intra_pair_clearance_mm`` is
       ``None``.  The partner may or may not be a group member; for
       group-member partners pass 1 already covers the
       ``intra_group_clearance_mm`` floor and this pass adds the
       tighter ``intra_pair_clearance_mm`` floor (which is BELOW
       ``intra_group_clearance_mm`` -- the diff-pair signaling rule).
       Note: although a smaller threshold seems "looser", the rule's
       VIOLATION semantics are reversed -- intra-pair pairs are
       allowed to couple at 0.10 mm but anything BELOW that is a real
       violation (per-class ``intra_pair_clearance``).  The legacy
       0.20 mm threshold would have FALSE-rejected such legal pairs;
       this pass corrects that to the per-class floor while still
       catching the broader DRC violation that the judge observed
       (TMDS_D0_N vs TMDS_D0_P at -0.060 mm).

    5. **Segment-vs-pad clearance** (NEW Issue #3317 follow-up): every
       new segment is checked against every foreign-net pad supplied
       in ``foreign_pads`` at the ``pad_clearance_mm`` threshold.
       This pass is skipped when either ``foreign_pads`` is empty/None
       OR ``pad_clearance_mm`` is None.  The judge identified that
       board 07's ADDR_BUS tuning produced 5 ``clearance_pad_segment``
       violations (e.g., A1 trace vs J3-4 pad on net A2 at
       (17.46, 80.0) with -0.116 mm edge clearance).  The legacy
       check did not consider pads -- only segments and (since
       Issue #3317 follow-up) vias.

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
        via_clearance_mm: Optional segment-to-via clearance floor in
            mm.  When ``None`` (the default), the segment-vs-via pass
            is skipped -- preserves byte-for-byte legacy behavior for
            existing unit tests.  When supplied, every new segment is
            checked against every via of every OTHER routed net (i.e.,
            ``oseg.net != candidate_net_id`` AND the via's net id !=
            ``candidate_net_id``).  Typical value:
            ``DesignRules.via_clearance`` (0.2 mm for JLCPCB).
        diff_pair_partners: Optional ``{net_id: partner_net_id}`` map
            for differential pairs.  When supplied (along with
            ``intra_pair_clearance_mm``), if the candidate net has a
            partner in this map AND the partner is in
            ``routes_by_net``, the partner's segments are additionally
            checked at the (tighter) ``intra_pair_clearance_mm``
            threshold.  When omitted the intra-pair pass is skipped.
        intra_pair_clearance_mm: Optional within-pair clearance floor
            in mm.  Required when ``diff_pair_partners`` supplies a
            partner for the candidate net.  Typical value:
            ``NetClassRouting.effective_intra_pair_clearance()`` (0.1
            mm for HDMI TMDS pairs).
        foreign_pads: Optional list of :class:`Pad` instances NOT
            owned by ``candidate_net_id``.  When supplied (along with
            ``pad_clearance_mm``), every new segment is checked
            against every foreign pad at the ``pad_clearance_mm``
            threshold.  When omitted the pad-clearance pass is
            skipped.  The caller is responsible for excluding the
            candidate net's own pads (pad-vs-own-trace is handled by
            the route's terminal connections).
        pad_clearance_mm: Optional segment-to-pad edge clearance
            floor in mm.  Required when ``foreign_pads`` is non-empty.
            Typical value: ``DesignRules.trace_clearance`` (0.2 mm).

    Returns:
        ``None`` if no clearance violation is introduced; otherwise a
        description of the first violation found (the caller must roll
        back).
    """
    from kicad_tools.core.geometry import (
        point_to_segment_distance,
        segment_clearance,
    )

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
                    return (
                        f"intra-group clearance vs group member "
                        f"{other_route.net_name!r} on {new_seg.layer}: "
                        f"{clearance:.3f}mm < {intra_group_clearance_mm:.3f}mm"
                    )

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
                    return (
                        f"inter-net clearance vs neighbor net "
                        f"{other_route.net_name!r} on {new_seg.layer}: "
                        f"{clearance:.3f}mm < {intra_group_clearance_mm:.3f}mm"
                    )

    # Pass 3 (Issue #3317 follow-up): segment-vs-via clearance.
    # Skipped when via_clearance_mm is None (legacy behavior).  When
    # supplied, every new segment is checked against every via of every
    # OTHER routed net.  The check is "edge-to-edge": center-to-segment
    # distance minus (via_radius + segment_half_width).
    if via_clearance_mm is not None:
        for other_net_id, other_route in routes_by_net.items():
            if other_net_id == candidate_net_id:
                continue
            for via in other_route.vias:
                # Vias span (at least) two layers.  Check against any
                # new segment whose layer is one of the via's layers.
                via_layers = set(via.layers)
                via_radius = via.diameter / 2.0
                for new_seg in new_segments:
                    if new_seg.layer not in via_layers:
                        continue
                    center_dist = point_to_segment_distance(
                        via.x,
                        via.y,
                        new_seg.x1,
                        new_seg.y1,
                        new_seg.x2,
                        new_seg.y2,
                    )
                    edge_clearance = center_dist - via_radius - new_seg.width / 2.0
                    if edge_clearance + 1e-9 < via_clearance_mm:
                        return (
                            f"segment-vs-via clearance vs net "
                            f"{other_route.net_name!r} via at "
                            f"({via.x:.2f}, {via.y:.2f}) on {new_seg.layer}: "
                            f"{edge_clearance:.3f}mm < {via_clearance_mm:.3f}mm"
                        )

    # Pass 4 (Issue #3317 follow-up): diff-pair intra-pair clearance.
    # When the candidate net is half of a differential pair AND its
    # partner is routed, check segments at the tighter
    # ``intra_pair_clearance_mm`` threshold.  Skipped when either
    # ``diff_pair_partners`` or ``intra_pair_clearance_mm`` is None.
    if diff_pair_partners is not None and intra_pair_clearance_mm is not None:
        partner_id = diff_pair_partners.get(candidate_net_id)
        if partner_id is not None and partner_id != candidate_net_id:
            partner_route = routes_by_net.get(partner_id)
            if partner_route is not None:
                for new_seg in new_segments:
                    for pseg in partner_route.segments:
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
                            return (
                                f"diff-pair intra-pair clearance vs partner "
                                f"net {partner_route.net_name!r} on "
                                f"{new_seg.layer}: {clearance:.3f}mm < "
                                f"{intra_pair_clearance_mm:.3f}mm"
                            )

    # Pass 5 (Issue #3317 follow-up): segment-vs-pad clearance.  Reject
    # inserts whose new segments land within ``pad_clearance_mm`` of any
    # foreign-net pad.  Bounding-box approximation: treat each pad as
    # an axis-aligned rectangle (x +/- width/2, y +/- height/2) and use
    # the smallest distance from the segment to any side of the box.
    # For circular SMD pads (width == height) this collapses to a
    # center-to-segment distance minus the radius.  The caller is
    # responsible for supplying only NON-candidate-net pads in
    # ``foreign_pads``.
    if foreign_pads and pad_clearance_mm is not None:
        for pad in foreign_pads:
            # PTH pads block both outer layers; treat them as present
            # on every new segment's layer.  SMD pads are layer-
            # specific.
            pad_through_hole = getattr(pad, "through_hole", False)
            for new_seg in new_segments:
                if not pad_through_hole and pad.layer != new_seg.layer:
                    continue
                # Conservative bounding-circle: radius = half the
                # longer dimension.  Matches the legacy escape
                # router's pad-keepout policy (segment-to-pad clearance
                # uses the inscribed-circle approximation).
                pad_radius = max(pad.width, pad.height) / 2.0
                center_dist = point_to_segment_distance(
                    pad.x,
                    pad.y,
                    new_seg.x1,
                    new_seg.y1,
                    new_seg.x2,
                    new_seg.y2,
                )
                edge_clearance = center_dist - pad_radius - new_seg.width / 2.0
                if edge_clearance + 1e-9 < pad_clearance_mm:
                    return (
                        f"segment-vs-pad clearance vs foreign pad at "
                        f"({pad.x:.2f}, {pad.y:.2f}) on {new_seg.layer}: "
                        f"{edge_clearance:.3f}mm < {pad_clearance_mm:.3f}mm"
                    )

    return None


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
    from .quantize import dogleg_points, is_45_aligned

    _ = p_net_id  # caller-side clarity
    mirrored: list[_Segment] = []
    for pseg in new_p_segments:
        rx1, ry1 = _reflect_point_about_axis(pseg.x1, pseg.y1, cx, cy, nx, ny)
        rx2, ry2 = _reflect_point_about_axis(pseg.x2, pseg.y2, cx, cy, nx, ny)
        sx1 = _snap_to_grid(rx1, grid_resolution_mm)
        sy1 = _snap_to_grid(ry1, grid_resolution_mm)
        sx2 = _snap_to_grid(rx2, grid_resolution_mm)
        sy2 = _snap_to_grid(ry2, grid_resolution_mm)
        # Issue #3535: even though the P-side meander is 45-aligned by
        # construction, reflecting it about the pair centerline (whose
        # normal is the arbitrary outer-normal hint, not necessarily a
        # legal routing direction) and snapping each endpoint to the
        # grid INDEPENDENTLY can rotate a leg off the {0,45,90,135} set.
        # Re-quantize each mirrored leg through the shared dogleg helper:
        # an aligned leg stays a single segment, an off-angle leg becomes
        # an exact 45-degree leg + axis leg.  The shared endpoints stay
        # pinned, so the N-side chain stays contiguous and connected.
        if is_45_aligned(sx2 - sx1, sy2 - sy1):
            pts = [(sx1, sy1), (sx2, sy2)]
        else:
            pts = dogleg_points(sx1, sy1, sx2, sy2)
        for (ax, ay), (bx, by) in zip(pts[:-1], pts[1:], strict=True):
            if ax == bx and ay == by:
                continue  # skip a degenerate zero-length leg
            mirrored.append(
                _Segment(
                    x1=ax,
                    y1=ay,
                    x2=bx,
                    y2=by,
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
    """Boolean wrapper around :func:`_post_insertion_clearance_detail_pair_group`.

    Preserved for callers / tests that only need the pass-fail verdict.
    Returns ``True`` when no clearance violation is introduced.
    """
    return (
        _post_insertion_clearance_detail_pair_group(
            new_p_segments=new_p_segments,
            new_n_segments=new_n_segments,
            candidate_p_id=candidate_p_id,
            candidate_n_id=candidate_n_id,
            group_net_ids=group_net_ids,
            routes_by_net=routes_by_net,
            intra_group_clearance_mm=intra_group_clearance_mm,
            intra_pair_clearance_mm=intra_pair_clearance_mm,
        )
        is None
    )


def _post_insertion_clearance_detail_pair_group(
    *,
    new_p_segments: list[Segment],
    new_n_segments: list[Segment],
    candidate_p_id: int,
    candidate_n_id: int,
    group_net_ids: set[int],
    routes_by_net: dict[int, Route],
    intra_group_clearance_mm: float,
    intra_pair_clearance_mm: float,
) -> str | None:
    """Paired DRC self-check for pair-aware serpentine insertion.

    Issue #3440: returns ``None`` when DRC-safe, else a description of
    the FIRST violation found (rule + offending neighbor + layer +
    measured-vs-required clearance) so pair rollbacks are actionable.

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
        ``None`` if no clearance violation is introduced; otherwise a
        description of the first violation found (the caller must roll
        back BOTH halves).
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
                return (
                    f"within-pair coupling clearance (P vs N mirrored "
                    f"segments) on {new_p.layer}: {clearance:.3f}mm < "
                    f"{intra_pair_clearance_mm:.3f}mm"
                )

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
                    return (
                        f"intra-group clearance vs group member "
                        f"{other_route.net_name!r} on {new_seg.layer}: "
                        f"{clearance:.3f}mm < {intra_group_clearance_mm:.3f}mm"
                    )

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
                    return (
                        f"inter-net clearance vs neighbor net "
                        f"{other_route.net_name!r} on {new_seg.layer}: "
                        f"{clearance:.3f}mm < {intra_group_clearance_mm:.3f}mm"
                    )

    return None


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
                else:
                    # Issue #3440: zero inserts means the pair arrived
                    # with its P half already at/over the reference
                    # while the lane AVERAGE is short (N much shorter
                    # than P).  The mirrored-geometry tuner cannot fix
                    # within-pair asymmetry; classify explicitly so the
                    # member never lands in an empty / silent reason
                    # bucket (the ``other()`` line this branch produced
                    # on board 07's TMDS_D0 lane).
                    for r in (per_pair_result_p, per_pair_result_n):
                        r.success = True
                        r.reason = "longer_than_reference"
                        r.message = (
                            f"Pair ({p_id}, {n_id}): P half already at/over "
                            f"the reference ({current_p_length:.4f}mm >= "
                            f"{target_length:.4f}mm) while the lane average "
                            "is short; mirrored insertion cannot fix "
                            "within-pair asymmetry."
                        )
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
            pair_drc_detail = _post_insertion_clearance_detail_pair_group(
                new_p_segments=p_serp_result.new_segments,
                new_n_segments=new_n_segments,
                candidate_p_id=p_id,
                candidate_n_id=n_id,
                group_net_ids=group_net_ids,
                routes_by_net=routes_by_net,
                intra_group_clearance_mm=intra_group_clearance_mm,
                intra_pair_clearance_mm=intra_pair_clearance_mm,
            )
            if pair_drc_detail is not None:
                # Rollback BOTH halves atomically -- the drift-prevention
                # invariant that Phase 2F AC #4 tests.
                for r in (per_pair_result_p, per_pair_result_n):
                    r.reason = "post_insertion_drc_violation"
                    r.length_after_mm = member_lengths[p_id if r is per_pair_result_p else n_id]
                    r.message = (
                        f"Pair-aware DRC self-check failed on pair "
                        f"({p_id}, {n_id}): {pair_drc_detail}; rolled back."
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

        # Issue #3440 staleness fix (pair-path sibling of the
        # single-ended publish): subsequent lanes / the trailing scalar
        # sub-pass must self-check against the committed mirrored
        # meanders, not the pre-meander pair geometry.
        if per_pair_result_p.inserts_applied > 0:
            routes_by_net[p_id] = current_p
            routes_by_net[n_id] = current_n

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
    "AMPLITUDE_BACKOFF_FACTORS",
    "MAX_INSERTS_PER_GROUP_MEMBER",
    "MAX_INSERTS_PER_GROUP_MEMBER_LARGE",
    "MAX_INSERTS_PER_GROUP_MEMBER_SMALL",
    "MAX_TOTAL_INSERTS_PER_GROUP",
    "REASON_SUMMARY_LABELS",
    "TUNE_RESULT_REASONS",
    "TuneResult",
    "format_reason_counts",
    "tune_match_group_v2",
]


# Reference suppression for static linters: MAX_INSERTS_PER_PAIR is
# imported so the drift-prevention test
# (MAX_INSERTS_PER_GROUP_MEMBER_SMALL == MAX_INSERTS_PER_PAIR) does not
# require an extra import side.
_ = MAX_INSERTS_PER_PAIR  # noqa: F401
