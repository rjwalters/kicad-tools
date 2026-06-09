"""
Auto-pcb-size escalation: trigger detection and ladder logic (Issue #3352, P_AS2).

This module implements the *pure-logic* core of the auto-pcb-size escalation
loop:

  - :func:`should_escalate` -- decides whether the current routing attempt
    indicates the envelope (not the layer count, not the clearance) is the
    bottleneck.
  - :func:`select_next_tier` -- walks the manufacturer's size-tier ladder per
    the :class:`~kicad_tools.spec.schema.EscalationPolicy` strategy.
  - :func:`can_escalate_with_holes` -- enforces the Q3 reframe: mounting
    holes move as a placeable group; escalation refuses when growing the
    board would push them outside the new envelope.
  - :func:`decide_escalation` -- the single public entry point that composes
    the three checks and returns an :class:`EscalationDecision`.

No router behaviour is changed here.  P_AS3 will wire these helpers into
``route_cmd.py`` alongside the existing ``route_with_layer_escalation``
implementation; P_AS4 will compose them with auto-layers per the
``EscalationPolicy.ladder`` policy.

The single-shot threshold trigger is intentionally simpler than the
``route_with_layer_escalation`` cross-attempt monotonic-regression
detector.  P_AS4 may add a multi-attempt detector if real recipes need
it; for now, the Q4 hardcoded density threshold + reach floor is the
agreed-upon trigger.

Coordinate / units convention:
  - All board dimensions in millimetres.
  - Board area metrics in cm^2 (matches the EscalationPolicy field).
  - DRC violation counts are integer net-routability blockers (clearance
    violations + shorts), NOT including warnings.

Issue: https://github.com/rjwalters/kicad-tools/issues/3352
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from kicad_tools.pcb.mounting_holes import MountingHoleGroup
from kicad_tools.router.mfr_limits import (
    ManufacturerSizeTier,
    MfrLimits,
    get_mfr_size_tier_ladder,
)
from kicad_tools.spec.schema import EscalationPolicy

__all__ = [
    "DEFAULT_PACKING_OVERHEAD",
    "DEFAULT_REACH_THRESHOLD",
    "DEFAULT_ROUTING_CHANNEL_PER_NET_MM2",
    "SIZE_CONSECUTIVE_REGRESSIONS",
    "SIZE_HARD_DROP_NETS",
    "SIZE_REGRESSION_TOLERANCE",
    "AreaEstimate",
    "EscalationContext",
    "EscalationDecision",
    "RegressionVerdict",
    "RoutingResultMetrics",
    "can_escalate_with_holes",
    "decide_escalation",
    "detect_regression_history",
    "envelope_meets_area_estimate",
    "estimate_required_area",
    "select_next_tier",
    "should_escalate",
    "should_escalate_with_history",
]


# Per Issue #3352 architect proposal section 2: routing reach is the
# acceptance threshold for "the current envelope is the bottleneck".  When
# completion is at or above this floor, no escalation is needed even if
# DRC density is over the threshold (a few hot-spot violations on an
# almost-fully-routed PCB are best hand-fixed, not escalated).
#
# Hardcoded for now (Q4-style policy: hardcode until empirical evidence
# argues for tunability).  Promote to an EscalationPolicy field if recipe-
# by-recipe tuning becomes necessary -- the constant is intentionally
# named here so the future field has an obvious source.
DEFAULT_REACH_THRESHOLD: float = 0.95


# Issue #3403: Default packing-density multiplier for the sum-of-clearances
# pre-route area estimator.  Mirrors :attr:`EscalationPolicy.packing_overhead`
# default so callers that bypass the policy can still use a sane value.
#
# 2.5 is a moderate prototype-density figure -- empirical calibration against
# boards 02-07 + softstart (issue #3403 acceptance) shows ratios in the
# 1.2-4.0x range track routability reasonably well; 2.5 is the median-safe
# value that does NOT incorrectly skip the routable cases while still
# catching the obviously over-constrained ones.
#
# Rationale for the coarse multiplier (vs. fine-grained per-net channel
# modelling): keeps the estimator fast and recipe-independent.  A more
# sophisticated per-net model is future work (see issue body "out of
# scope" -- coarse multiplier is fine for v1).
DEFAULT_PACKING_OVERHEAD: float = 2.5


# Issue #3403: Per-signal-net routing-channel area estimate (mm^2) for the
# coarse routing-channel term in :func:`estimate_required_area`.
#
# Empirical derivation: a typical 0.2 mm trace at 0.15 mm clearance occupies
# a ~0.5 mm wide channel.  Average board traversal is ~30-50 mm at typical
# board sizes (1-15 cm^2 envelopes), giving ~15-25 mm^2 per net of channel
# real estate.  We round to 20 mm^2 as the central tendency.
#
# This is intentionally a single constant per net (not per-net-class or
# per-density-region) -- the coarse multiplier is fine for v1 per the
# Issue #3403 scope decision.  Future work could refine to a per-net-class
# rate that scales with trace width and net topology fanout.
DEFAULT_ROUTING_CHANNEL_PER_NET_MM2: float = 20.0


# Multi-attempt regression-detection constants (Issue #3352, P_AS4).
#
# Mirror the auto-layers ladder constants from PR #3244 (described in
# ``route_with_layer_escalation`` -- ``REGRESSION_TOLERANCE`` etc.) but
# applied across *size-tier* attempts rather than layer-stack attempts.
# Same semantics, distinct prefix so the two ladders' constants don't
# collide when both ladders are walked simultaneously by P_AS4's
# ``[size, layers]`` composite strategy.
#
# Rationale (per architect proposal Q2 in the issue):
#   - SIZE_REGRESSION_TOLERANCE = 2: small jitter (1-2 fewer nets routed
#     on a strictly larger envelope) is noise from routing-order changes
#     and does NOT count as a regression.  Matches the auto-layers
#     tolerance.
#   - SIZE_HARD_DROP_NETS = 5: a single attempt with >= 5 fewer nets
#     routed than the previous attempt is severe enough to exit the
#     ladder immediately (signal that the larger envelope is making
#     routing strictly worse -- a placement / BOM hot-spot, not an
#     envelope problem).
#   - SIZE_CONSECUTIVE_REGRESSIONS = 2: otherwise, require two
#     consecutive regressions (each exceeding SIZE_REGRESSION_TOLERANCE)
#     before exiting.  Single-attempt blips are tolerated.
SIZE_REGRESSION_TOLERANCE: int = 2
SIZE_HARD_DROP_NETS: int = 5
SIZE_CONSECUTIVE_REGRESSIONS: int = 2


@dataclass(frozen=True)
class RoutingResultMetrics:
    """Lightweight routing-attempt summary consumed by escalation logic.

    The full :class:`kicad_tools.router.strategies.RoutingResult` (one per
    net) is too granular for ladder-level decisions; this structure
    captures the per-attempt aggregate signals the trigger uses.

    Attributes:
        signal_nets_routed: Number of signal nets fully connected this attempt.
            "Signal" means non-pour, non-skipped nets -- the population the
            routing-reach metric is normalised against.
        signal_nets_total: Total signal nets the attempt tried to route.
            ``signal_nets_routed / signal_nets_total`` is the routing reach.
            When zero, completion is treated as 1.0 (vacuously full reach,
            since there's nothing to route).
        drc_violations: Count of DRC blocking violations (clearance
            violations + shorts).  Excludes warnings.
        board_area_cm2: Board area in cm^2 (used to normalise drc_violations
            into a density).  Must be > 0; the trigger asserts this.
    """

    signal_nets_routed: int
    signal_nets_total: int
    drc_violations: int
    board_area_cm2: float

    @property
    def completion(self) -> float:
        """Routing reach as a fraction in ``[0.0, 1.0]``.

        Defaults to ``1.0`` when ``signal_nets_total == 0`` (vacuously full
        completion -- nothing to route means nothing un-routed).
        """
        if self.signal_nets_total <= 0:
            return 1.0
        return self.signal_nets_routed / self.signal_nets_total

    @property
    def drc_density(self) -> float:
        """DRC violations per cm^2 of board area.

        Always non-negative.  When ``board_area_cm2 <= 0`` returns
        ``float("inf")`` so the trigger never silently false-negatives on
        a degenerate board (a zero-area board with any violations should
        flag immediately).
        """
        if self.board_area_cm2 <= 0:
            return float("inf")
        return self.drc_violations / self.board_area_cm2


class EscalationDecision(Enum):
    """The five possible outcomes of :func:`decide_escalation`.

    The naming convention is verb-first: ``ESCALATE`` means "grow the board",
    ``REFUSE_*`` means "the trigger fired but escalation is impossible for
    the named reason", and ``NO_ESCALATION_NEEDED`` means "the trigger did
    not fire -- the current attempt is good enough".

    Members:
        ESCALATE: Grow the board to the next admissible tier.
        REFUSE_HARD_ENVELOPE: Trigger fired but recipe declares
            ``envelope_hard=True`` AND a mounting-hole group is present.
            (Without a hole group, ``envelope_hard=True`` is the only
            blocker -- but P_AS3 will surface a different actionable
            error message in that case.)
        REFUSE_HOLES_DONT_FIT: Trigger fired and envelope is soft, but
            the mounting-hole group would not fit in the next-tier
            envelope at its declared anchor.
        REFUSE_MAX_TIER: Trigger fired but the current tier index is
            already at the policy's max (or the manufacturer's max),
            so there's no further size escalation to attempt.
        REFUSE_REGRESSION: Trigger fired but the routing history shows
            that escalating the envelope produces strictly worse
            results.  The bottleneck is not the envelope -- typically
            placement or BOM density.  (Issue #3352, P_AS4.)
        NO_ESCALATION_NEEDED: Trigger did not fire.  The current attempt
            is good enough (reach >= threshold AND DRC density <= threshold).
    """

    ESCALATE = "escalate"
    REFUSE_HARD_ENVELOPE = "refuse_hard_envelope"
    REFUSE_HOLES_DONT_FIT = "refuse_holes_dont_fit"
    REFUSE_MAX_TIER = "refuse_max_tier"
    REFUSE_REGRESSION = "refuse_regression"
    NO_ESCALATION_NEEDED = "no_escalation_needed"


@dataclass(frozen=True)
class RegressionVerdict:
    """Result of :func:`detect_regression_history` over an attempt sequence.

    Returned by the multi-attempt regression detector consumed by
    :func:`should_escalate_with_history` and (via the optional ``history``
    argument) :func:`decide_escalation`.

    Attributes:
        is_regressing: True iff the history indicates a structural
            regression (hard drop or N consecutive small regressions).
            When True, escalation should refuse rather than ESCALATE.
        reason: Human-readable explanation suitable for the
            ``_print_size_escalation_refusal`` UX.
        streak: Current consecutive-regression streak length (mostly for
            debugging / logging).  Zero when the latest attempt did not
            regress.
    """

    is_regressing: bool
    reason: str
    streak: int


@dataclass(frozen=True)
class EscalationContext:
    """Per-attempt context for the auto-pcb-size escalation loop.

    Encapsulates the slow-changing state (current rung in the ladder,
    policy declaration, manufacturer, mounting-hole geometry, hard-envelope
    declaration) so callers don't have to thread eight positional args
    through :func:`decide_escalation`.

    Attributes:
        current_tier_index: Index of the current rung in the manufacturer's
            size-tier ladder (0-based, matches ``get_mfr_size_tier_ladder``
            ordering).  ``None`` is not allowed -- callers must determine
            the starting rung from the board envelope before invoking
            the escalation logic (typically via
            :func:`kicad_tools.router.mfr_limits.find_smallest_admitting_tier`).
        policy: The :class:`EscalationPolicy` from the recipe spec.
        manufacturer: Manufacturer name (case-insensitive; aliases resolved
            internally).
        hole_group: Optional mounting-hole group whose placement governs
            whether escalation can grow the board.  ``None`` means no
            mounting holes are pinned -- escalation is free to grow.
        envelope_hard: Mirrors
            :attr:`kicad_tools.spec.schema.MechanicalRequirements.envelope_hard`.
            When ``True``, escalation refuses to grow the board.
    """

    current_tier_index: int
    policy: EscalationPolicy
    manufacturer: str
    hole_group: MountingHoleGroup | None = None
    envelope_hard: bool = False


def should_escalate(
    metrics: RoutingResultMetrics,
    policy: EscalationPolicy,
    reach_threshold: float = DEFAULT_REACH_THRESHOLD,
) -> bool:
    """Decide whether the current routing attempt warrants escalation.

    The trigger fires when **both** conditions hold:

      1. Routing reach (``signal_nets_routed / signal_nets_total``) is
         strictly below ``reach_threshold``.
      2. DRC violation density (``drc_violations / board_area_cm2``) is
         strictly above ``policy.density_threshold_viols_per_cm2``.

    Requiring *both* signals is intentional: a few hot-spot violations on
    an almost-fully-routed PCB are best hand-fixed (high reach, density
    over threshold -> no escalate), and a sparse incomplete routing on
    a board with few violations is more often a router bug than a true
    envelope problem (low reach, density below threshold -> no escalate).
    Only the "both signals fire" case is unambiguously an envelope issue.

    Single-shot threshold trigger only (per architect's P_AS2 recommendation
    in the issue).  Multi-attempt monotonic-regression detection -- mirroring
    the ``REGRESSION_TOLERANCE`` / ``HARD_DROP_NETS`` pattern in
    ``route_with_layer_escalation`` -- is a P_AS4 addition if needed.

    Args:
        metrics: Aggregate metrics for the current routing attempt.
        policy: The recipe's escalation policy (provides the density
            threshold).
        reach_threshold: Minimum reach below which escalation is considered.
            Defaults to :data:`DEFAULT_REACH_THRESHOLD` (0.95) per the
            Issue #3352 architect proposal.

    Returns:
        ``True`` if the routing attempt indicates the envelope is the
        bottleneck and escalation should be attempted.  ``False``
        otherwise.

    Example:
        >>> from kicad_tools.spec.schema import EscalationPolicy
        >>> policy = EscalationPolicy()  # default 0.5 viols/cm^2
        >>> # Softstart rev B P4: 132 violations on 150 cm^2, 80% reach
        >>> metrics = RoutingResultMetrics(
        ...     signal_nets_routed=80,
        ...     signal_nets_total=100,
        ...     drc_violations=132,
        ...     board_area_cm2=150.0,
        ... )
        >>> should_escalate(metrics, policy)
        True
    """
    if metrics.completion >= reach_threshold:
        return False
    if metrics.drc_density <= policy.density_threshold_viols_per_cm2:
        return False
    return True


def detect_regression_history(
    history: list[RoutingResultMetrics],
    *,
    regression_tolerance: int = SIZE_REGRESSION_TOLERANCE,
    hard_drop_nets: int = SIZE_HARD_DROP_NETS,
    consecutive_regressions: int = SIZE_CONSECUTIVE_REGRESSIONS,
) -> RegressionVerdict:
    """Detect cross-attempt regression across a size-escalation history.

    Inspects the attempt sequence (most-recent last) and reports whether
    the size ladder is making routing strictly worse.  Mirrors the
    auto-layers ladder's ``REGRESSION_TOLERANCE`` / ``HARD_DROP_NETS`` /
    ``CONSECUTIVE_REGRESSIONS`` pattern (see PR #3244 commit for the
    layer-side analogue in ``route_with_layer_escalation``).

    The detector is intentionally simple: it compares each attempt's
    ``signal_nets_routed`` to the previous attempt's value and counts
    drops.  A hard drop on a single attempt (>= ``hard_drop_nets``) is
    an immediate verdict; otherwise we require
    ``consecutive_regressions`` back-to-back drops each exceeding
    ``regression_tolerance``.

    For the auto-pcb-size ladder, a regression means "a strictly larger
    envelope routed strictly worse" -- which is structurally backwards.
    Growing further cannot cure it, so refusal is the correct response.

    Args:
        history: Ordered list of per-attempt metrics (oldest first,
            most-recent last).  Empty or single-element histories
            never report a regression (nothing to compare against).
        regression_tolerance: Drops <= this count are ignored as
            noise.  Default :data:`SIZE_REGRESSION_TOLERANCE`.
        hard_drop_nets: A single-attempt drop >= this count triggers
            immediate refusal.  Default :data:`SIZE_HARD_DROP_NETS`.
        consecutive_regressions: Number of back-to-back drops
            exceeding ``regression_tolerance`` required for refusal.
            Default :data:`SIZE_CONSECUTIVE_REGRESSIONS`.

    Returns:
        :class:`RegressionVerdict` describing whether the history
        regresses and why.

    Example:
        >>> # No regression: monotonic improvement
        >>> h = [
        ...     RoutingResultMetrics(signal_nets_routed=70, signal_nets_total=100,
        ...                          drc_violations=10, board_area_cm2=100.0),
        ...     RoutingResultMetrics(signal_nets_routed=85, signal_nets_total=100,
        ...                          drc_violations=5, board_area_cm2=150.0),
        ... ]
        >>> v = detect_regression_history(h)
        >>> v.is_regressing
        False
        >>> # Hard drop: 80 -> 70 = 10 nets
        >>> h = [
        ...     RoutingResultMetrics(signal_nets_routed=80, signal_nets_total=100,
        ...                          drc_violations=10, board_area_cm2=100.0),
        ...     RoutingResultMetrics(signal_nets_routed=70, signal_nets_total=100,
        ...                          drc_violations=15, board_area_cm2=150.0),
        ... ]
        >>> v = detect_regression_history(h)
        >>> v.is_regressing
        True
    """
    if len(history) < 2:
        return RegressionVerdict(is_regressing=False, reason="", streak=0)

    streak = 0
    last_drop = 0
    for prev, cur in zip(history, history[1:], strict=False):
        drop = prev.signal_nets_routed - cur.signal_nets_routed
        last_drop = drop
        if drop >= hard_drop_nets:
            return RegressionVerdict(
                is_regressing=True,
                reason=(
                    f"hard drop of {drop} nets (>= {hard_drop_nets} threshold) "
                    f"when growing the envelope -- larger board routed "
                    f"strictly worse, suggesting BOM/placement bottleneck "
                    f"rather than envelope over-constraint"
                ),
                streak=streak + 1,
            )
        if drop > regression_tolerance:
            streak += 1
            if streak >= consecutive_regressions:
                return RegressionVerdict(
                    is_regressing=True,
                    reason=(
                        f"{streak} consecutive size-tier escalations regressed "
                        f"(each by > {regression_tolerance} nets) -- growing "
                        f"the envelope is no longer helping; the bottleneck "
                        f"is upstream of envelope (placement/BOM)"
                    ),
                    streak=streak,
                )
        else:
            # Improvement or jitter within tolerance resets the streak.
            streak = 0

    return RegressionVerdict(
        is_regressing=False,
        reason="",
        streak=streak if last_drop > regression_tolerance else 0,
    )


def should_escalate_with_history(
    history: list[RoutingResultMetrics],
    policy: EscalationPolicy,
    reach_threshold: float = DEFAULT_REACH_THRESHOLD,
) -> bool:
    """Multi-attempt-aware variant of :func:`should_escalate`.

    Returns ``True`` iff the most-recent attempt's metrics fire the
    single-shot trigger AND the history does NOT exhibit a structural
    regression (see :func:`detect_regression_history`).

    This is the canonical way to consult both single-shot signals and
    cross-attempt history when deciding whether to walk the size ladder
    further.  Use :func:`detect_regression_history` directly when you
    need the structured verdict (e.g. to emit a refusal message).

    Args:
        history: Ordered list of per-attempt metrics (oldest first).
            Must be non-empty -- the last element is the current
            attempt's metrics.
        policy: The recipe's escalation policy.
        reach_threshold: See :func:`should_escalate`.

    Returns:
        ``True`` if the current attempt warrants escalation AND the
        history does not regress.  ``False`` otherwise.

    Raises:
        ValueError: If ``history`` is empty.
    """
    if not history:
        raise ValueError("should_escalate_with_history requires a non-empty history")
    if not should_escalate(history[-1], policy, reach_threshold):
        return False
    return not detect_regression_history(history).is_regressing


def select_next_tier(
    current_tier_index: int,
    policy: EscalationPolicy,
    manufacturer: str,
) -> ManufacturerSizeTier | None:
    """Pick the next size tier per the escalation policy strategy.

    Returns the next-tier-up (one rung up the manufacturer's size-tier
    ladder) when the policy permits size escalation; returns ``None``
    when no further escalation is permitted.

    Ladder strategy semantics (mirroring
    :class:`~kicad_tools.spec.schema.EscalationPolicy`):

      - ``"layers-first"`` -- this function returns the next size tier
        up.  P_AS4 will gate the call: layer escalation runs first, and
        :func:`select_next_tier` is only invoked after layers exhaust.
      - ``"size-first"`` -- this function returns the next size tier up.
        P_AS4 will run size escalation before layers.
      - ``"layers-only"`` -- returns ``None``: size escalation disabled.
      - ``"size-only"`` -- returns the next size tier up; P_AS4 will skip
        layer escalation.
      - ``"none"`` -- returns ``None``: no escalation of any axis.

    Independent of ladder strategy, this function returns ``None`` when:

      - ``current_tier_index >= len(ladder) - 1`` (already at top rung).
      - ``current_tier_index >= policy.max_size_tier`` (recipe-imposed
        ceiling reached).  ``policy.max_size_tier=None`` (default) means
        no recipe ceiling.

    Args:
        current_tier_index: 0-based index of the current rung in the
            manufacturer's size-tier ladder.
        policy: The recipe's escalation policy.
        manufacturer: Manufacturer name (case-insensitive; aliases resolved).

    Returns:
        The :class:`ManufacturerSizeTier` for the next rung up, or
        ``None`` if no further escalation is permitted.

    Raises:
        ValueError: If ``manufacturer`` is not recognized.
    """
    # Strategies that disable size escalation altogether.
    if policy.ladder in ("layers-only", "none"):
        return None

    ladder = get_mfr_size_tier_ladder(manufacturer)
    if not ladder:
        # Defensive: no ladder registered for this manufacturer (shouldn't
        # happen given get_mfr_size_tier_ladder's fallback, but guard).
        return None

    next_index = current_tier_index + 1
    if next_index >= len(ladder):
        # Already at the top of the manufacturer's ladder.
        return None

    if policy.max_size_tier is not None and next_index > policy.max_size_tier:
        # Recipe-imposed ceiling: refuse to escalate beyond max_size_tier.
        return None

    return ladder[next_index]


def can_escalate_with_holes(
    hole_group: MountingHoleGroup | None,
    new_tier: ManufacturerSizeTier,
    envelope_hard: bool,
) -> tuple[bool, str]:
    """Check whether mounting holes permit escalation to ``new_tier``.

    Implements the Issue #3352 Q3 reframe: mounting holes are a placeable
    group with fixed relative geometry.  When the envelope is *soft*, the
    group either fits in the new envelope at its declared anchor (escalation
    proceeds) or it doesn't (escalation refuses with a clear error).  When
    the envelope is *hard*, the presence of any mounting hole group
    immediately refuses -- the recipe author has declared the mechanical
    envelope as a non-negotiable constraint, so growing the board is not
    permitted at all.

    Note that the *envelope-hard refusal* fires even when ``hole_group`` is
    ``None``.  P_AS3 will use this signal at the route-cmd level to emit the
    architect's actionable error enumerating the layer / clearance / BOM
    levers; this function only returns the structured refusal flag here.

    Args:
        hole_group: The mounting-hole group, or ``None`` if no group is
            declared.  When ``None`` and ``envelope_hard=False``, the
            check passes trivially.
        new_tier: The proposed next-tier size envelope (max width / height).
        envelope_hard: The mechanical envelope-hard declaration from
            :attr:`MechanicalRequirements.envelope_hard`.

    Returns:
        A ``(can_escalate, reason)`` tuple.

          - ``(True, "")`` when escalation is permitted.
          - ``(False, "envelope_hard=True")`` when the hard-envelope
            declaration blocks the grow.
          - ``(False, "mounting hole group at <anchor> doesn't fit in
            <new>")`` when the envelope is soft but the group falls outside
            the new envelope at its current anchor.

    Example:
        >>> from kicad_tools.pcb.mounting_holes import MountingHoleGroup
        >>> from kicad_tools.router.mfr_limits import MFR_JLCPCB_SIZE_TIERS
        >>> group = MountingHoleGroup(
        ...     holes=[(0, 0), (140, 0), (0, 90), (140, 90)],
        ...     anchor=(5.0, 5.0),
        ... )
        >>> # Next tier: 150x150 mm
        >>> tier = MFR_JLCPCB_SIZE_TIERS[2]
        >>> can_escalate_with_holes(group, tier, envelope_hard=False)
        (True, '')
        >>> can_escalate_with_holes(group, tier, envelope_hard=True)
        (False, 'envelope_hard=True')
    """
    if envelope_hard:
        return (False, "envelope_hard=True")

    if hole_group is None:
        # No mounting-hole geometry to worry about; escalation is unrestricted.
        return (True, "")

    if hole_group.fits_in_envelope(new_tier.max_width_mm, new_tier.max_height_mm):
        return (True, "")

    new_label = f"{new_tier.max_width_mm:g}x{new_tier.max_height_mm:g} mm"
    anchor_label = f"({hole_group.anchor[0]:g}, {hole_group.anchor[1]:g})"
    reason = f"mounting hole group at {anchor_label} doesn't fit in {new_label}"
    return (False, reason)


def decide_escalation(
    metrics: RoutingResultMetrics,
    context: EscalationContext,
    reach_threshold: float = DEFAULT_REACH_THRESHOLD,
    history: list[RoutingResultMetrics] | None = None,
) -> EscalationDecision:
    """Compose trigger detection, ladder logic, and hole-fit check.

    The single public entry point for the auto-pcb-size escalation loop.
    Returns an :class:`EscalationDecision` enum the caller (P_AS3) uses to
    either grow the board or emit an actionable error.

    Decision precedence (most specific first):

      1. ``NO_ESCALATION_NEEDED`` when the trigger does not fire (reach
         is already at or above threshold, or density is at or below
         threshold).
      2. ``REFUSE_REGRESSION`` when the trigger fires but ``history`` is
         supplied and shows that the size ladder has been regressing
         (see :func:`detect_regression_history`).  Larger envelopes
         routing strictly worse implies the bottleneck is not the
         envelope; refusal is the correct response (P_AS4).
      3. ``REFUSE_MAX_TIER`` when the trigger fires but the ladder is
         exhausted (already at policy / manufacturer maximum).
      4. ``REFUSE_HARD_ENVELOPE`` when the trigger fires, the ladder has
         room, but ``envelope_hard=True`` blocks the grow.
      5. ``REFUSE_HOLES_DONT_FIT`` when the trigger fires, the envelope is
         soft, the ladder has room, but the mounting-hole group falls
         outside the next-tier envelope.
      6. ``ESCALATE`` otherwise -- the grow is permitted.

    This ordering ensures the caller gets the most actionable refusal
    reason possible: "you can't escalate because growing makes things
    worse" is more useful than "you can't escalate because the recipe
    says envelope_hard" when both happen to be true.

    Args:
        metrics: Per-attempt routing metrics.
        context: Slow-changing escalation state.
        reach_threshold: See :func:`should_escalate`.
        history: Optional ordered list of per-attempt metrics (oldest
            first; the *current* attempt's ``metrics`` need not be
            included -- ``decide_escalation`` appends it internally
            for the regression check).  When supplied, enables the
            ``REFUSE_REGRESSION`` outcome.

    Returns:
        The escalation decision.

    Example:
        >>> from kicad_tools.spec.schema import EscalationPolicy
        >>> # Softstart rev B P4: should trigger
        >>> metrics = RoutingResultMetrics(
        ...     signal_nets_routed=80,
        ...     signal_nets_total=100,
        ...     drc_violations=132,
        ...     board_area_cm2=150.0,
        ... )
        >>> context = EscalationContext(
        ...     current_tier_index=3,  # 150x200 in JLCPCB ladder
        ...     policy=EscalationPolicy(),
        ...     manufacturer="jlcpcb",
        ...     envelope_hard=True,
        ... )
        >>> decide_escalation(metrics, context)
        <EscalationDecision.REFUSE_HARD_ENVELOPE: 'refuse_hard_envelope'>
    """
    # Step 1: did the trigger fire?  If not, no escalation needed.
    if not should_escalate(metrics, context.policy, reach_threshold):
        return EscalationDecision.NO_ESCALATION_NEEDED

    # Step 2 (P_AS4): if multi-attempt history is supplied, check whether
    # the size ladder has been regressing.  If yes, refuse early -- the
    # envelope is not the bottleneck and further escalation can't help.
    # Build the full sequence by appending the current attempt's metrics
    # to the caller's history (callers may pass the prior-attempts list
    # without the current one already appended).
    if history is not None and len(history) >= 1:
        full_history = list(history)
        if not full_history or full_history[-1] is not metrics:
            full_history.append(metrics)
        if len(full_history) >= 2:
            verdict = detect_regression_history(full_history)
            if verdict.is_regressing:
                return EscalationDecision.REFUSE_REGRESSION

    # Step 3: is there a next tier to escalate to?  This check comes
    # before the envelope-hard check because "max tier" is the more
    # specific failure mode (no further escalation is possible *at all*
    # vs. "the recipe forbids growing"); the caller's error message can
    # be more actionable when we name the correct cause.
    next_tier = select_next_tier(
        context.current_tier_index,
        context.policy,
        context.manufacturer,
    )
    if next_tier is None:
        return EscalationDecision.REFUSE_MAX_TIER

    # Step 3: does the envelope-hard declaration or hole-group geometry
    # block the grow?  can_escalate_with_holes returns the structured
    # refusal flag for either failure mode.
    permitted, reason = can_escalate_with_holes(
        context.hole_group,
        next_tier,
        context.envelope_hard,
    )
    if not permitted:
        if reason == "envelope_hard=True":
            return EscalationDecision.REFUSE_HARD_ENVELOPE
        return EscalationDecision.REFUSE_HOLES_DONT_FIT

    return EscalationDecision.ESCALATE


# ---------------------------------------------------------------------------
# Issue #3403: sum-of-clearances area-escalation heuristic.
#
# Pre-route geometric lower-bound for the minimum envelope area required to
# route the design.  When the current envelope is smaller than this lower
# bound, we know the routing attempt is doomed without spending the routing
# budget; the escalation loop can skip directly to the next size tier.
#
# The reactive DRC-density backstop (``should_escalate``) stays as the
# fallback for cases where the heuristic under-estimates (e.g. a board
# with looser packing than the constant assumes).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AreaEstimate:
    """Sum-of-clearances pre-route area estimate (Issue #3403).

    Detailed breakdown of the :func:`estimate_required_area` result.  The
    individual terms are exposed so callers (logging, debugging,
    calibration) can inspect which contribution dominates a given board.

    Attributes:
        footprint_area_mm2: Sum of all footprint bounding-box areas, in mm^2.
            "Bounding box" means the axis-aligned envelope of the
            footprint's pad-array (the smallest rectangle that contains
            every pad's outer extent).  This is the minimum copper area
            the components themselves occupy.
        clearance_halo_mm2: Sum of perimeter-halo contributions, in mm^2.
            For each footprint, ``2*(W+H) * min_clearance`` -- the keep-out
            ring around the component that other copper cannot enter.
            Approximation: treats the halo as a thin rectangular border;
            corner overlap when components abut is not double-counted
            because the routing pass would also need that overlap region.
        routing_channel_mm2: Coarse estimate of routing-channel area in mm^2.
            Computed as ``signal_net_count * routing_channel_per_net``
            (default ``DEFAULT_ROUTING_CHANNEL_PER_NET_MM2``).  A more
            sophisticated per-net-class model is future work.
        packing_overhead: The multiplier applied to the sum of the above
            three terms.  Mirrors :attr:`EscalationPolicy.packing_overhead`.
        total_mm2: Final required-area estimate in mm^2.  Equals
            ``packing_overhead * (footprint_area_mm2 + clearance_halo_mm2 +
            routing_channel_mm2)``.  Compare with ``board_w * board_h`` to
            decide whether the current envelope can possibly accommodate
            the design.
        signal_net_count: The signal-net count used for the routing-channel
            term (logging / calibration helper).
        footprint_count: Number of footprints contributing to the
            footprint-area + halo terms (logging / calibration helper).

    Example:
        >>> # Synthetic small board: 4 footprints, 6 signal nets
        >>> # (this docstring snippet is illustrative -- see unit tests)
    """

    footprint_area_mm2: float
    clearance_halo_mm2: float
    routing_channel_mm2: float
    packing_overhead: float
    total_mm2: float
    signal_net_count: int
    footprint_count: int

    @property
    def total_cm2(self) -> float:
        """Total required-area estimate in cm^2 (convenience)."""
        return self.total_mm2 / 100.0


def _footprint_bbox_dimensions(footprint) -> tuple[float, float]:
    """Compute axis-aligned bounding-box ``(width, height)`` of a footprint.

    Approximation: derived from the pad-array extent rather than the full
    courtyard / silkscreen graphic.  Pads dominate the routed-copper
    footprint for SMD parts, and through-hole parts have their drill
    holes inside the pad bbox -- so this approximation is sound for the
    routing-area estimate (we're estimating copper-occupancy, not silk).

    For a footprint with no pads (e.g. a mechanical-only graphic), returns
    ``(0.0, 0.0)``.  The clearance halo will still be zero for such
    components, which is correct: they don't contribute to copper density.

    Args:
        footprint: A :class:`kicad_tools.schema.pcb.Footprint` instance.

    Returns:
        ``(width_mm, height_mm)`` of the bounding box that contains all
        pad outer extents.
    """
    pads = getattr(footprint, "pads", None)
    if not pads:
        return (0.0, 0.0)

    # Pad positions are footprint-local (KiCad convention); the bbox is
    # measured in the same local frame.  Footprint rotation matters in
    # absolute coords, but the bbox AREA is rotation-invariant for the
    # bbox we use (any rectangle's bbox area equals its width * height
    # regardless of orientation -- a 45deg rotation would inflate the
    # AABB but the un-rotated AABB is the canonical "minimum bbox" for
    # this estimate).
    min_x = float("inf")
    max_x = float("-inf")
    min_y = float("inf")
    max_y = float("-inf")
    for pad in pads:
        px, py = pad.position
        w, h = pad.size
        half_w = w / 2.0
        half_h = h / 2.0
        if px - half_w < min_x:
            min_x = px - half_w
        if px + half_w > max_x:
            max_x = px + half_w
        if py - half_h < min_y:
            min_y = py - half_h
        if py + half_h > max_y:
            max_y = py + half_h

    if min_x == float("inf"):
        return (0.0, 0.0)
    return (max_x - min_x, max_y - min_y)


def _count_signal_nets(pcb) -> int:
    """Count signal nets (non-empty, non-pour) on a :class:`PCB`.

    Heuristic: include every net with a non-empty name, exclude common
    pour-net names (``GND``, ``+3V3``, ``+5V``, etc.) and the unconnected
    net (``""``).  Mirrors the per-board "signal_nets_total" convention
    used by the rest of the routing code.

    The classification is intentionally permissive: false positives
    (counting a power net as signal) inflate the routing-channel term
    by ``DEFAULT_ROUTING_CHANNEL_PER_NET_MM2`` per net, which is a small
    contribution to the total area estimate.  False negatives (missing a
    signal net) under-estimate the channel term, which is the failure
    mode the reactive DRC-density backstop catches.

    Args:
        pcb: A :class:`kicad_tools.schema.pcb.PCB` instance.

    Returns:
        Non-negative signal-net count.
    """
    nets = getattr(pcb, "nets", None)
    if not nets:
        return 0

    # Common pour / plane net names that don't carry routed signals.
    POUR_PREFIXES = ("+", "-")
    POUR_NAMES = frozenset(
        {
            "GND",
            "GND1",
            "GND2",
            "GNDA",
            "GNDD",
            "AGND",
            "DGND",
            "PGND",
            "VCC",
            "VDD",
            "VSS",
            "VEE",
            "VBUS",
        }
    )

    count = 0
    for net in nets.values():
        name = getattr(net, "name", "") or ""
        if not name:
            continue
        # Skip pour/plane nets (typically power rails).
        if name in POUR_NAMES:
            continue
        if name.startswith(POUR_PREFIXES):
            # Power-rail convention: +3V3, +5V, +1V2, -12V, etc.
            continue
        count += 1
    return count


def estimate_required_area(
    pcb,
    mfr_limits: MfrLimits,
    packing_overhead: float = DEFAULT_PACKING_OVERHEAD,
    routing_channel_per_net_mm2: float = DEFAULT_ROUTING_CHANNEL_PER_NET_MM2,
) -> AreaEstimate:
    """Estimate the minimum board area required to route ``pcb`` (mm^2).

    Issue #3403: pre-route geometric lower bound on the envelope area
    required to accommodate the design.  The formula is::

        required = packing_overhead * (
            sum(footprint_area + clearance_perimeter for each component)
            + sum(routing_channel_estimate for each signal net)
        )

    where:

      - ``footprint_area`` = the KiCad footprint pad-bbox area (mm^2).
      - ``clearance_perimeter`` = ``2 * (W + H) * mfr.min_clearance`` --
        the keep-out perimeter halo around the component.
      - ``routing_channel_estimate`` = ``routing_channel_per_net_mm2``
        per signal net (a coarse multiplier; see
        :data:`DEFAULT_ROUTING_CHANNEL_PER_NET_MM2`).
      - ``packing_overhead`` = heuristic multiplier that accounts for
        routing-channel overlap, vias, fillets, and component keepouts
        not modeled by the per-footprint terms.

    The estimate is intentionally conservative -- it returns a LOWER
    bound on the area needed for routing to succeed.  If the current
    envelope area is below this estimate, the routing attempt is
    structurally infeasible.  If above, the attempt may or may not
    succeed depending on placement and net topology -- the reactive
    DRC-density check (``should_escalate``) catches the failures the
    heuristic doesn't.

    When ``packing_overhead == 0``, the function still computes the
    individual terms but returns ``total_mm2 = 0`` -- effectively
    disabling the pre-route check.  Callers may use this as the kill
    switch.

    Args:
        pcb: The PCB to analyse (a :class:`kicad_tools.schema.pcb.PCB`).
        mfr_limits: Manufacturer's design-rule limits.  Only
            ``min_clearance`` is consulted for the perimeter halo.
        packing_overhead: Multiplier applied to the sum of footprint +
            clearance-halo + routing-channel terms.  Defaults to
            :data:`DEFAULT_PACKING_OVERHEAD` (2.5).  Recipes that need
            recipe-specific tuning should pass
            ``EscalationPolicy.packing_overhead``.
        routing_channel_per_net_mm2: Coarse per-signal-net routing
            channel area in mm^2.  Defaults to
            :data:`DEFAULT_ROUTING_CHANNEL_PER_NET_MM2` (20 mm^2).

    Returns:
        :class:`AreaEstimate` with the breakdown.  The
        ``total_mm2`` field is the headline number callers compare
        against the current envelope area.

    Example:
        >>> # Synthetic two-pad SMD on a JLCPCB-spec board:
        >>> # bbox 5x3 mm, perimeter halo at 0.127 mm clearance,
        >>> # one signal net -> 20 mm^2 channel.
        >>> # See unit tests for full worked example.
    """
    footprint_area_sum = 0.0
    clearance_halo_sum = 0.0
    footprint_count = 0

    footprints = getattr(pcb, "footprints", None) or []
    for fp in footprints:
        w, h = _footprint_bbox_dimensions(fp)
        if w <= 0 or h <= 0:
            # Skip components with no pads (mechanical-only graphics);
            # they contribute neither copper nor a halo.
            continue
        footprint_area_sum += w * h
        # Perimeter halo: 2 * (W + H) * min_clearance.  This is the area
        # of a thin rectangular border of width ``min_clearance`` around
        # a W x H rectangle.  Exact formula would add 4 * min_clearance^2
        # for the corner squares; we omit because min_clearance^2 is
        # ~0.016 mm^2 at JLCPCB defaults (negligible vs. the W*H term).
        clearance_halo_sum += 2.0 * (w + h) * mfr_limits.min_clearance
        footprint_count += 1

    signal_nets = _count_signal_nets(pcb)
    routing_channel_sum = signal_nets * routing_channel_per_net_mm2

    base_sum = footprint_area_sum + clearance_halo_sum + routing_channel_sum
    total = packing_overhead * base_sum if packing_overhead > 0 else 0.0

    return AreaEstimate(
        footprint_area_mm2=footprint_area_sum,
        clearance_halo_mm2=clearance_halo_sum,
        routing_channel_mm2=routing_channel_sum,
        packing_overhead=packing_overhead,
        total_mm2=total,
        signal_net_count=signal_nets,
        footprint_count=footprint_count,
    )


def envelope_meets_area_estimate(
    envelope_area_mm2: float,
    estimate: AreaEstimate,
) -> bool:
    """Decide whether ``envelope_area_mm2`` is large enough to attempt routing.

    Issue #3403: the pre-route filter consumed by
    :func:`route_with_size_escalation`.

    Returns ``True`` when the current envelope area is greater than or
    equal to the estimated required area, i.e. the envelope COULD
    plausibly accommodate the design.  Returns ``False`` when the
    envelope is strictly smaller than the estimate, indicating the
    routing attempt is structurally infeasible and the escalation loop
    should skip directly to the next size tier.

    When the estimate is zero (``packing_overhead == 0`` disables the
    check), this function returns ``True`` unconditionally -- the
    pre-route filter is opted out.

    Args:
        envelope_area_mm2: The current envelope area in mm^2.
        estimate: The :class:`AreaEstimate` from
            :func:`estimate_required_area`.

    Returns:
        ``True`` if ``envelope_area_mm2 >= estimate.total_mm2``
        (or if the estimate is disabled).  ``False`` otherwise.
    """
    if estimate.total_mm2 <= 0.0:
        # Estimator disabled by zero packing_overhead -> trust the reactive
        # backstop.  Always return "meets".
        return True
    return envelope_area_mm2 >= estimate.total_mm2
