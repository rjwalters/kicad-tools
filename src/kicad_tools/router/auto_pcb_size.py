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
    get_mfr_size_tier_ladder,
)
from kicad_tools.spec.schema import EscalationPolicy

__all__ = [
    "DEFAULT_REACH_THRESHOLD",
    "EscalationContext",
    "EscalationDecision",
    "RoutingResultMetrics",
    "can_escalate_with_holes",
    "decide_escalation",
    "select_next_tier",
    "should_escalate",
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
        NO_ESCALATION_NEEDED: Trigger did not fire.  The current attempt
            is good enough (reach >= threshold AND DRC density <= threshold).
    """

    ESCALATE = "escalate"
    REFUSE_HARD_ENVELOPE = "refuse_hard_envelope"
    REFUSE_HOLES_DONT_FIT = "refuse_holes_dont_fit"
    REFUSE_MAX_TIER = "refuse_max_tier"
    NO_ESCALATION_NEEDED = "no_escalation_needed"


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
) -> EscalationDecision:
    """Compose trigger detection, ladder logic, and hole-fit check.

    The single public entry point for the auto-pcb-size escalation loop.
    Returns an :class:`EscalationDecision` enum the caller (P_AS3) uses to
    either grow the board or emit an actionable error.

    Decision precedence (most specific first):

      1. ``NO_ESCALATION_NEEDED`` when the trigger does not fire (reach
         is already at or above threshold, or density is at or below
         threshold).
      2. ``REFUSE_MAX_TIER`` when the trigger fires but the ladder is
         exhausted (already at policy / manufacturer maximum).
      3. ``REFUSE_HARD_ENVELOPE`` when the trigger fires, the ladder has
         room, but ``envelope_hard=True`` blocks the grow.
      4. ``REFUSE_HOLES_DONT_FIT`` when the trigger fires, the envelope is
         soft, the ladder has room, but the mounting-hole group falls
         outside the next-tier envelope.
      5. ``ESCALATE`` otherwise -- the grow is permitted.

    This ordering ensures the caller gets the most actionable refusal
    reason possible: "you can't escalate because you're already at the
    max tier" is more useful than "you can't escalate because your
    envelope is declared hard" when both happen to be true.

    Args:
        metrics: Per-attempt routing metrics.
        context: Slow-changing escalation state.
        reach_threshold: See :func:`should_escalate`.

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

    # Step 2: is there a next tier to escalate to?  This check comes
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
