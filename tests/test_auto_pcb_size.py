"""Tests for the auto-pcb-size escalation trigger + ladder logic (Issue #3352, P_AS2).

Covers the pure-logic core of the auto-pcb-size escalation loop:

  - Trigger detection (under threshold, over threshold, edge cases).
  - Ladder logic for each of the five EscalationPolicy strategies.
  - Mounting-hole-group fit check (Q3 reframe consumer).
  - Max-tier refusal.
  - No-escalation-needed for fully-routed PCBs.
  - The composed ``decide_escalation`` entry point.

No router behaviour is exercised here -- this is a unit-test boundary.
"""

from __future__ import annotations

import pytest

from kicad_tools.pcb.mounting_holes import MountingHoleGroup
from kicad_tools.router.auto_pcb_size import (
    DEFAULT_REACH_THRESHOLD,
    EscalationContext,
    EscalationDecision,
    RoutingResultMetrics,
    can_escalate_with_holes,
    decide_escalation,
    select_next_tier,
    should_escalate,
)
from kicad_tools.router.mfr_limits import (
    MFR_JLCPCB_SIZE_TIERS,
    get_mfr_size_tier_ladder,
)
from kicad_tools.spec.schema import EscalationPolicy

# ---------------------------------------------------------------------------
# RoutingResultMetrics: derived-property semantics
# ---------------------------------------------------------------------------


class TestRoutingResultMetrics:
    """Tests for the lightweight per-attempt metrics container."""

    def test_completion_default_zero_nets(self):
        """Zero signal nets total -> vacuous full completion (1.0)."""
        m = RoutingResultMetrics(
            signal_nets_routed=0,
            signal_nets_total=0,
            drc_violations=0,
            board_area_cm2=100.0,
        )
        assert m.completion == 1.0

    def test_completion_partial(self):
        """80/100 nets -> 0.8 completion."""
        m = RoutingResultMetrics(
            signal_nets_routed=80,
            signal_nets_total=100,
            drc_violations=0,
            board_area_cm2=100.0,
        )
        assert m.completion == pytest.approx(0.8)

    def test_completion_full(self):
        """100/100 nets -> 1.0 completion."""
        m = RoutingResultMetrics(
            signal_nets_routed=100,
            signal_nets_total=100,
            drc_violations=0,
            board_area_cm2=100.0,
        )
        assert m.completion == pytest.approx(1.0)

    def test_drc_density_softstart_revb(self):
        """Softstart rev B: 132 viols / 150 cm^2 = 0.88 /cm^2."""
        m = RoutingResultMetrics(
            signal_nets_routed=80,
            signal_nets_total=100,
            drc_violations=132,
            board_area_cm2=150.0,
        )
        assert m.drc_density == pytest.approx(0.88)

    def test_drc_density_zero_area_inf(self):
        """Degenerate zero-area board -> infinite density (never silently zero)."""
        m = RoutingResultMetrics(
            signal_nets_routed=0,
            signal_nets_total=0,
            drc_violations=5,
            board_area_cm2=0.0,
        )
        assert m.drc_density == float("inf")

    def test_drc_density_zero_violations(self):
        """Zero violations -> 0.0 density regardless of board area."""
        m = RoutingResultMetrics(
            signal_nets_routed=100,
            signal_nets_total=100,
            drc_violations=0,
            board_area_cm2=100.0,
        )
        assert m.drc_density == 0.0


# ---------------------------------------------------------------------------
# should_escalate: the single-shot threshold trigger
# ---------------------------------------------------------------------------


class TestShouldEscalate:
    """Tests for the trigger-detection helper."""

    def _policy(self, density: float = 0.5) -> EscalationPolicy:
        return EscalationPolicy(density_threshold_viols_per_cm2=density)

    def test_under_both_thresholds_no_escalate(self):
        """Reach above threshold AND density at or below threshold -> no escalate."""
        # 98% reach, 0.1 viols/cm^2 -- both signals say "good enough"
        m = RoutingResultMetrics(
            signal_nets_routed=98,
            signal_nets_total=100,
            drc_violations=10,
            board_area_cm2=100.0,
        )
        assert should_escalate(m, self._policy()) is False

    def test_over_both_thresholds_escalate(self):
        """Reach below threshold AND density above threshold -> escalate."""
        # softstart rev B case: 80% reach, 0.88 viols/cm^2
        m = RoutingResultMetrics(
            signal_nets_routed=80,
            signal_nets_total=100,
            drc_violations=132,
            board_area_cm2=150.0,
        )
        assert should_escalate(m, self._policy()) is True

    def test_high_reach_high_density_no_escalate(self):
        """High reach + high density = hot-spot, not envelope problem -> no escalate."""
        # 99% reach but 1.0 viols/cm^2 -- the few un-routed nets are
        # hot-spots best fixed manually, not by growing the board.
        m = RoutingResultMetrics(
            signal_nets_routed=99,
            signal_nets_total=100,
            drc_violations=100,
            board_area_cm2=100.0,
        )
        assert should_escalate(m, self._policy()) is False

    def test_low_reach_low_density_no_escalate(self):
        """Low reach + low density = router bug, not envelope problem -> no escalate."""
        # 50% reach but 0.0 viols/cm^2 -- the un-routed nets are
        # router-confusing, not density-blocked.  Growing won't help.
        m = RoutingResultMetrics(
            signal_nets_routed=50,
            signal_nets_total=100,
            drc_violations=0,
            board_area_cm2=100.0,
        )
        assert should_escalate(m, self._policy()) is False

    def test_reach_exactly_at_threshold_no_escalate(self):
        """Reach == threshold is treated as "good enough"."""
        m = RoutingResultMetrics(
            signal_nets_routed=95,
            signal_nets_total=100,  # 0.95 == DEFAULT_REACH_THRESHOLD
            drc_violations=100,
            board_area_cm2=100.0,
        )
        assert should_escalate(m, self._policy()) is False

    def test_density_exactly_at_threshold_no_escalate(self):
        """Density == threshold is treated as "good enough"."""
        # 80% reach, exactly 0.5 viols/cm^2
        m = RoutingResultMetrics(
            signal_nets_routed=80,
            signal_nets_total=100,
            drc_violations=50,
            board_area_cm2=100.0,
        )
        assert should_escalate(m, self._policy(density=0.5)) is False

    def test_custom_reach_threshold(self):
        """A custom reach_threshold flips the decision."""
        m = RoutingResultMetrics(
            signal_nets_routed=90,
            signal_nets_total=100,
            drc_violations=100,
            board_area_cm2=100.0,
        )
        # At 0.95 threshold + density 1.0: escalate (reach 0.90 < 0.95)
        assert should_escalate(m, self._policy(), reach_threshold=0.95) is True
        # At 0.85 threshold: no escalate (reach 0.90 >= 0.85)
        assert should_escalate(m, self._policy(), reach_threshold=0.85) is False

    def test_custom_density_threshold(self):
        """A custom policy density threshold flips the decision."""
        m = RoutingResultMetrics(
            signal_nets_routed=80,
            signal_nets_total=100,
            drc_violations=80,
            board_area_cm2=100.0,
        )
        # 0.8 density, threshold 0.5 -> escalate
        assert should_escalate(m, self._policy(density=0.5)) is True
        # 0.8 density, threshold 1.0 -> no escalate
        assert should_escalate(m, self._policy(density=1.0)) is False

    def test_default_reach_threshold_is_95(self):
        """The default reach threshold matches the architect's proposal."""
        assert DEFAULT_REACH_THRESHOLD == 0.95


# ---------------------------------------------------------------------------
# select_next_tier: per-strategy ladder logic
# ---------------------------------------------------------------------------


class TestSelectNextTier:
    """Tests for the size-tier ladder logic."""

    def test_layers_first_returns_next_tier(self):
        """layers-first strategy still returns the next size tier
        (P_AS4 will gate the call)."""
        policy = EscalationPolicy(ladder="layers-first")
        tier = select_next_tier(0, policy, "jlcpcb")
        assert tier is not None
        assert tier == MFR_JLCPCB_SIZE_TIERS[1]

    def test_size_first_returns_next_tier(self):
        """size-first strategy returns the next size tier."""
        policy = EscalationPolicy(ladder="size-first")
        tier = select_next_tier(0, policy, "jlcpcb")
        assert tier is not None
        assert tier == MFR_JLCPCB_SIZE_TIERS[1]

    def test_layers_only_returns_none(self):
        """layers-only strategy disables size escalation."""
        policy = EscalationPolicy(ladder="layers-only")
        assert select_next_tier(0, policy, "jlcpcb") is None

    def test_size_only_returns_next_tier(self):
        """size-only strategy returns the next size tier."""
        policy = EscalationPolicy(ladder="size-only")
        tier = select_next_tier(0, policy, "jlcpcb")
        assert tier is not None
        assert tier == MFR_JLCPCB_SIZE_TIERS[1]

    def test_none_returns_none(self):
        """none strategy disables all escalation."""
        policy = EscalationPolicy(ladder="none")
        assert select_next_tier(0, policy, "jlcpcb") is None

    def test_at_top_of_ladder_returns_none(self):
        """current index at top of ladder -> no further escalation."""
        ladder = get_mfr_size_tier_ladder("jlcpcb")
        top_index = len(ladder) - 1
        policy = EscalationPolicy(ladder="size-first")
        assert select_next_tier(top_index, policy, "jlcpcb") is None

    def test_max_size_tier_ceiling_respected(self):
        """current index >= policy.max_size_tier -> no escalation."""
        # max_size_tier=2 means we cannot escalate beyond index 2
        policy = EscalationPolicy(ladder="size-first", max_size_tier=2)
        # From index 1, next would be index 2 (allowed)
        assert select_next_tier(1, policy, "jlcpcb") is not None
        # From index 2, next would be index 3 (refused by ceiling)
        assert select_next_tier(2, policy, "jlcpcb") is None

    def test_max_size_tier_zero_blocks_all_escalation(self):
        """max_size_tier=0 means no escalation is possible from any rung."""
        policy = EscalationPolicy(ladder="size-first", max_size_tier=0)
        assert select_next_tier(0, policy, "jlcpcb") is None

    def test_max_size_tier_none_uses_manufacturer_max(self):
        """max_size_tier=None falls back to manufacturer's top tier."""
        ladder = get_mfr_size_tier_ladder("jlcpcb")
        top_index = len(ladder) - 1
        policy = EscalationPolicy(ladder="size-first", max_size_tier=None)
        # We can escalate up to top_index (returns the top tier).
        tier = select_next_tier(top_index - 1, policy, "jlcpcb")
        assert tier is not None
        assert tier == ladder[top_index]
        # And refuses beyond that.
        assert select_next_tier(top_index, policy, "jlcpcb") is None

    def test_walks_through_all_tiers(self):
        """Sequential calls walk the ladder one rung at a time."""
        ladder = get_mfr_size_tier_ladder("jlcpcb")
        policy = EscalationPolicy(ladder="size-first")
        for i in range(len(ladder) - 1):
            tier = select_next_tier(i, policy, "jlcpcb")
            assert tier == ladder[i + 1], f"mismatch at index {i}"

    def test_unknown_manufacturer_raises(self):
        """An unrecognized manufacturer raises ValueError from the underlying ladder lookup."""
        policy = EscalationPolicy(ladder="size-first")
        with pytest.raises(ValueError):
            select_next_tier(0, policy, "nosuchmfr")


# ---------------------------------------------------------------------------
# can_escalate_with_holes: Q3 reframe consumer
# ---------------------------------------------------------------------------


class TestCanEscalateWithHoles:
    """Tests for the mounting-hole-group fit check."""

    def _group(
        self,
        holes: list[tuple[float, float]] | None = None,
        anchor: tuple[float, float] = (5.0, 5.0),
    ) -> MountingHoleGroup:
        if holes is None:
            # Four-corner pattern that fits in a 150x100 inset by 5 mm.
            holes = [(0, 0), (140, 0), (0, 90), (140, 90)]
        return MountingHoleGroup(holes=holes, anchor=anchor)

    def test_no_holes_soft_envelope_permits(self):
        """No hole group + soft envelope -> escalation permitted trivially."""
        tier = MFR_JLCPCB_SIZE_TIERS[2]  # 150x150
        ok, reason = can_escalate_with_holes(None, tier, envelope_hard=False)
        assert ok is True
        assert reason == ""

    def test_no_holes_hard_envelope_refuses(self):
        """No hole group BUT hard envelope -> structured envelope_hard refusal."""
        tier = MFR_JLCPCB_SIZE_TIERS[2]
        ok, reason = can_escalate_with_holes(None, tier, envelope_hard=True)
        assert ok is False
        assert reason == "envelope_hard=True"

    def test_holes_fit_soft_envelope_permits(self):
        """Group fits in next tier + soft envelope -> permitted."""
        group = self._group()  # fits in 150x100 with 5mm inset
        tier = MFR_JLCPCB_SIZE_TIERS[3]  # 150x200 (the softstart envelope)
        ok, reason = can_escalate_with_holes(group, tier, envelope_hard=False)
        assert ok is True
        assert reason == ""

    def test_holes_fit_hard_envelope_refuses(self):
        """Group fits BUT hard envelope -> envelope_hard refusal wins."""
        group = self._group()
        tier = MFR_JLCPCB_SIZE_TIERS[3]
        ok, reason = can_escalate_with_holes(group, tier, envelope_hard=True)
        assert ok is False
        assert reason == "envelope_hard=True"

    def test_holes_dont_fit_soft_envelope_refuses(self):
        """Group falls outside next tier + soft envelope -> hole-fit refusal."""
        # Group needs ~150x100 inset by 5; tier 0 is 100x100 (too small).
        group = self._group()
        tier = MFR_JLCPCB_SIZE_TIERS[0]  # 100x100
        ok, reason = can_escalate_with_holes(group, tier, envelope_hard=False)
        assert ok is False
        assert "doesn't fit" in reason
        assert "(5, 5)" in reason  # anchor label
        assert "100x100" in reason  # new envelope label

    def test_holes_dont_fit_hard_envelope_refuses_with_envelope_hard_reason(self):
        """Hard envelope wins over the hole-fit failure mode."""
        group = self._group()
        tier = MFR_JLCPCB_SIZE_TIERS[0]
        ok, reason = can_escalate_with_holes(group, tier, envelope_hard=True)
        assert ok is False
        # envelope_hard takes precedence over hole-fit failure
        assert reason == "envelope_hard=True"


# ---------------------------------------------------------------------------
# decide_escalation: composed entry point
# ---------------------------------------------------------------------------


class TestDecideEscalation:
    """Tests for the composed public entry point."""

    def _metrics_softstart_revb(self) -> RoutingResultMetrics:
        """Softstart rev B P4 metrics: 80% reach, 0.88 viols/cm^2.

        This is the motivating real-world case for the feature.
        """
        return RoutingResultMetrics(
            signal_nets_routed=80,
            signal_nets_total=100,
            drc_violations=132,
            board_area_cm2=150.0,
        )

    def _metrics_clean(self) -> RoutingResultMetrics:
        """Fully-routed PCB metrics: 100% reach, zero violations."""
        return RoutingResultMetrics(
            signal_nets_routed=100,
            signal_nets_total=100,
            drc_violations=0,
            board_area_cm2=150.0,
        )

    def test_clean_route_no_escalation_needed(self):
        """Fully-routed PCB -> NO_ESCALATION_NEEDED."""
        context = EscalationContext(
            current_tier_index=2,
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
        )
        assert (
            decide_escalation(self._metrics_clean(), context)
            == EscalationDecision.NO_ESCALATION_NEEDED
        )

    def test_softstart_revb_at_top_tier_refuse_max(self):
        """Trigger fires at the top of the ladder -> REFUSE_MAX_TIER."""
        ladder = get_mfr_size_tier_ladder("jlcpcb")
        context = EscalationContext(
            current_tier_index=len(ladder) - 1,
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
        )
        assert (
            decide_escalation(self._metrics_softstart_revb(), context)
            == EscalationDecision.REFUSE_MAX_TIER
        )

    def test_softstart_revb_hard_envelope_refuse(self):
        """Trigger fires + envelope_hard=True -> REFUSE_HARD_ENVELOPE."""
        context = EscalationContext(
            current_tier_index=3,  # 150x200 in JLCPCB ladder (softstart envelope)
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
            envelope_hard=True,
        )
        assert (
            decide_escalation(self._metrics_softstart_revb(), context)
            == EscalationDecision.REFUSE_HARD_ENVELOPE
        )

    def test_softstart_revb_soft_envelope_escalate(self):
        """Trigger fires + envelope soft + no holes -> ESCALATE."""
        context = EscalationContext(
            current_tier_index=2,  # 150x150
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
            envelope_hard=False,
        )
        assert (
            decide_escalation(self._metrics_softstart_revb(), context)
            == EscalationDecision.ESCALATE
        )

    def test_softstart_revb_holes_dont_fit_refuses(self):
        """Trigger fires + holes don't fit in next tier -> REFUSE_HOLES_DONT_FIT."""
        # A group that needs >= 150x150 with 5mm inset; next tier from idx 0
        # is 100x150 (too narrow at the width axis).
        group = MountingHoleGroup(
            holes=[(0, 0), (140, 0), (0, 140), (140, 140)],
            anchor=(5.0, 5.0),
        )
        context = EscalationContext(
            current_tier_index=0,  # at 100x100; next is 100x150
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
            hole_group=group,
            envelope_hard=False,
        )
        assert (
            decide_escalation(self._metrics_softstart_revb(), context)
            == EscalationDecision.REFUSE_HOLES_DONT_FIT
        )

    def test_softstart_revb_holes_fit_escalate(self):
        """Trigger fires + holes fit + soft envelope -> ESCALATE."""
        # Group fits comfortably in 150x150 (next tier from idx 1).
        group = MountingHoleGroup(
            holes=[(0, 0), (90, 0), (0, 90), (90, 90)],
            anchor=(5.0, 5.0),
        )
        context = EscalationContext(
            current_tier_index=1,  # at 100x150; next is 150x150
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
            hole_group=group,
            envelope_hard=False,
        )
        assert (
            decide_escalation(self._metrics_softstart_revb(), context)
            == EscalationDecision.ESCALATE
        )

    def test_layers_only_strategy_no_escalation(self):
        """layers-only strategy never returns ESCALATE."""
        context = EscalationContext(
            current_tier_index=0,
            policy=EscalationPolicy(ladder="layers-only"),
            manufacturer="jlcpcb",
        )
        # Trigger fires but select_next_tier returns None -> REFUSE_MAX_TIER
        assert (
            decide_escalation(self._metrics_softstart_revb(), context)
            == EscalationDecision.REFUSE_MAX_TIER
        )

    def test_none_strategy_no_escalation(self):
        """none strategy never returns ESCALATE."""
        context = EscalationContext(
            current_tier_index=0,
            policy=EscalationPolicy(ladder="none"),
            manufacturer="jlcpcb",
        )
        assert (
            decide_escalation(self._metrics_softstart_revb(), context)
            == EscalationDecision.REFUSE_MAX_TIER
        )

    def test_precedence_max_tier_over_envelope_hard(self):
        """When both max-tier AND envelope-hard apply, REFUSE_MAX_TIER wins.

        The max-tier failure is the more specific one (the ladder is empty);
        envelope_hard is only meaningful when there's a next rung to refuse.
        """
        ladder = get_mfr_size_tier_ladder("jlcpcb")
        context = EscalationContext(
            current_tier_index=len(ladder) - 1,
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
            envelope_hard=True,
        )
        assert (
            decide_escalation(self._metrics_softstart_revb(), context)
            == EscalationDecision.REFUSE_MAX_TIER
        )

    def test_precedence_envelope_hard_over_holes_dont_fit(self):
        """envelope_hard wins over hole-fit failure."""
        # Holes that wouldn't fit anyway, but envelope_hard refuses first.
        group = MountingHoleGroup(
            holes=[(0, 0), (290, 0), (0, 290), (290, 290)],
            anchor=(5.0, 5.0),
        )
        context = EscalationContext(
            current_tier_index=0,
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
            hole_group=group,
            envelope_hard=True,
        )
        assert (
            decide_escalation(self._metrics_softstart_revb(), context)
            == EscalationDecision.REFUSE_HARD_ENVELOPE
        )

    def test_decide_with_max_size_tier_policy(self):
        """policy.max_size_tier ceiling triggers REFUSE_MAX_TIER mid-ladder."""
        context = EscalationContext(
            current_tier_index=2,
            policy=EscalationPolicy(max_size_tier=2),
            manufacturer="jlcpcb",
            envelope_hard=False,
        )
        # Next index 3 > ceiling 2 -> refuse max tier
        assert (
            decide_escalation(self._metrics_softstart_revb(), context)
            == EscalationDecision.REFUSE_MAX_TIER
        )


# ---------------------------------------------------------------------------
# EscalationContext: dataclass construction
# ---------------------------------------------------------------------------


class TestEscalationContext:
    """Tests for the EscalationContext value type."""

    def test_minimal_construction(self):
        """Only the required fields need to be supplied."""
        policy = EscalationPolicy()
        ctx = EscalationContext(
            current_tier_index=0,
            policy=policy,
            manufacturer="jlcpcb",
        )
        assert ctx.current_tier_index == 0
        assert ctx.policy is policy
        assert ctx.manufacturer == "jlcpcb"
        assert ctx.hole_group is None
        assert ctx.envelope_hard is False

    def test_full_construction(self):
        """All fields explicit."""
        group = MountingHoleGroup(holes=[(0, 0)], anchor=(5.0, 5.0))
        policy = EscalationPolicy(ladder="size-only", max_size_tier=2)
        ctx = EscalationContext(
            current_tier_index=1,
            policy=policy,
            manufacturer="pcbway",
            hole_group=group,
            envelope_hard=True,
        )
        assert ctx.hole_group is group
        assert ctx.envelope_hard is True
        assert ctx.policy.ladder == "size-only"

    def test_immutable_frozen(self):
        """EscalationContext is frozen -- attributes cannot be reassigned."""
        ctx = EscalationContext(
            current_tier_index=0,
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
        )
        with pytest.raises((AttributeError, TypeError)):
            ctx.current_tier_index = 1  # type: ignore[misc]
